"""基于 HTTP Range 的只读、可寻址文件对象，带强 ETag 版本绑定。

设计要点（对应需求）：

* **按需读取**：只发 ``Range: bytes=start-end`` 请求，绝不整件下载。
  元数据（TIFF 头 / IFD / 标签数组）走固定大小块缓存；
  像元数据（tile）走精确范围请求，相邻范围合并为一次请求。
* **版本绑定**：首个响应必须是 ``206`` 且携带**强 ETag**，否则立即失败；
  之后每个请求带 ``If-Range: <etag>``，且响应 ETag 必须逐字节一致。
  服务器换版的任何迹象（返回 200 全量、ETag 变化、ETag 消失、
  退化为弱 ETag、资源长度变化）都会抛出 :class:`VersionChangedError`。
* **不共享状态**：每次取数创建一个独立实例，块缓存是实例私有的；
  并发读取同一 URL 时，各自的 ETag 绑定互不影响，绝不混用版本。
"""

from __future__ import annotations

import re
import threading

import httpx

from errors import SourceError, VersionChangedError

_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")

DEFAULT_CHUNK_SIZE = 1 << 18  # 256 KiB，元数据读取的缓存块大小


def _is_weak_etag(etag: str) -> bool:
    return etag.lstrip().lower().startswith("w/")


class HttpRangeReader:
    """把远程 GeoTIFF 包装成 seekable 文件对象（供 tifffile 解析）。"""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._client = client
        self._url = url
        self._chunk_size = chunk_size
        self._etag: str | None = None
        self._size: int | None = None
        self._cache: dict[int, bytes] = {}
        self._pos = 0
        self._closed = False
        self._lock = threading.Lock()
        # 探测请求：同时完成「支持 Range？」「强 ETag？」的判定并绑定版本，
        # 取回的首块直接进缓存（通常覆盖 TIFF 头 + IFD + 标签数组）。
        self._cache[0] = self._fetch(0, chunk_size - 1)

    # ------------------------------------------------------------------ #
    # 版本绑定信息
    # ------------------------------------------------------------------ #
    @property
    def etag(self) -> str | None:
        """本次取数绑定的强 ETag。"""
        return self._etag

    @property
    def size(self) -> int | None:
        """源资源总字节数。"""
        return self._size

    # ------------------------------------------------------------------ #
    # 底层：带版本校验的 Range 请求
    # ------------------------------------------------------------------ #
    def _fetch(self, start: int, end: int) -> bytes:
        """抓取闭区间 [start, end] 的字节，强制版本一致性。"""
        if self._size is not None:
            end = min(end, self._size - 1)
            if start > end:
                return b""

        headers = {"Range": f"bytes={start}-{end}"}
        if self._etag is not None:
            # RFC 7233：若资源已换版，服务器应返回 200 全量内容，
            # 我们把它作为换版信号显式失败。
            headers["If-Range"] = self._etag

        try:
            resp = self._client.get(self._url, headers=headers)
        except httpx.HTTPError as exc:
            raise SourceError(f"源服务器请求失败: {exc}") from exc

        if resp.status_code == 200:
            if self._etag is None:
                raise SourceError(
                    "源服务器不支持 HTTP Range（对 Range 请求返回 200 全量内容）"
                )
            raise VersionChangedError(
                "源在读取中换版：If-Range 校验未通过，服务器回退为 200 全量响应"
            )
        if resp.status_code == 416 and self._size is not None and start >= self._size:
            return b""
        if resp.status_code != 206:
            raise SourceError(f"源服务器对 Range 请求返回了异常状态 {resp.status_code}")

        etag = resp.headers.get("etag")
        if etag is None:
            if self._etag is None:
                raise SourceError("源服务器未提供 ETag，无法保证读取期间版本一致")
            raise VersionChangedError("源在读取中换版：后续响应不再携带 ETag")
        if _is_weak_etag(etag):
            if self._etag is None:
                raise SourceError(f"源服务器仅提供弱 ETag ({etag})，不予接受")
            raise VersionChangedError("源在读取中换版：后续响应退化为弱 ETag")
        if self._etag is None:
            self._etag = etag  # 绑定首次取得的强 ETag
        elif etag != self._etag:
            raise VersionChangedError(
                f"源在读取中换版：ETag 由 {self._etag} 变为 {etag}"
            )

        content_range = resp.headers.get("content-range", "")
        m = _CONTENT_RANGE_RE.match(content_range)
        if not m:
            raise SourceError(f"源服务器 Content-Range 缺失或无法解析: {content_range!r}")
        if m.group(3) == "*":
            raise SourceError("源服务器未告知资源总长度")
        total = int(m.group(3))
        if self._size is None:
            self._size = total
        elif total != self._size:
            raise VersionChangedError(
                f"源在读取中换版：资源长度由 {self._size} 变为 {total}"
            )

        body = resp.content
        # 期望长度须按 Content-Range 宣告的总长折算（首请求时 self._size 尚未知，
        # 小于一个缓存块的文件会合法地返回短于请求区间的内容）
        clamped_end = min(end, total - 1)
        expected = clamped_end - start + 1
        if len(body) != expected:
            raise SourceError(
                f"源服务器返回长度不符：期望 {expected} 字节，实收 {len(body)} 字节"
            )
        return body

    # ------------------------------------------------------------------ #
    # 中层：按块缓存的读取（供 TIFF 元数据解析）
    # ------------------------------------------------------------------ #
    def _ensure_chunks(self, first: int, last: int) -> None:
        idx = first
        while idx <= last:
            if idx in self._cache:
                idx += 1
                continue
            # 合并连续缺失块为一次 Range 请求
            j = idx
            while j + 1 <= last and (j + 1) not in self._cache:
                j += 1
            data = self._fetch(idx * self._chunk_size, (j + 1) * self._chunk_size - 1)
            for k in range(idx, j + 1):
                lo = (k - idx) * self._chunk_size
                self._cache[k] = data[lo : lo + self._chunk_size]
            idx = j + 1

    def read_at(self, offset: int, size: int) -> bytes:
        """读取 [offset, offset+size)，元数据路径（带块缓存）。"""
        if size <= 0:
            return b""
        if self._size is not None and offset >= self._size:
            return b""
        if size > self._chunk_size:
            return self._fetch(offset, offset + size - 1)
        first = offset // self._chunk_size
        last = (offset + size - 1) // self._chunk_size
        with self._lock:
            self._ensure_chunks(first, last)
            out = bytearray()
            for idx in range(first, last + 1):
                chunk = self._cache[idx]
                chunk_start = idx * self._chunk_size
                lo = max(offset - chunk_start, 0)
                hi = min(offset + size - chunk_start, len(chunk))
                out += chunk[lo:hi]
        return bytes(out)

    def read_exact(self, offset: int, size: int) -> bytes:
        """精确范围读取（像元 tile 路径），不进块缓存。"""
        if size <= 0:
            return b""
        return self._fetch(offset, offset + size - 1)

    # ------------------------------------------------------------------ #
    # 文件对象接口（tifffile 需要 read/seek/tell）
    # ------------------------------------------------------------------ #
    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed reader")
        if size is None or size < 0:
            if self._size is None:
                raise SourceError("资源长度未知，无法全量读取")
            size = self._size - self._pos
        data = self.read_at(self._pos, size)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            pos = offset
        elif whence == 1:
            pos = self._pos + offset
        elif whence == 2:
            if self._size is None:
                raise SourceError("资源长度未知，无法从末尾定位")
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if pos < 0:
            raise ValueError("negative seek position")
        self._pos = pos
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True
        self._cache.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "HttpRangeReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
