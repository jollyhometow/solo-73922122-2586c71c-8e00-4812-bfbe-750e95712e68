import io
import os
import re
import sys
import threading

import httpx
import numpy as np
import tifffile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


def make_range_handler(
    content: bytes,
    etag: str | None = '"v1"',
    support_range: bool = True,
    counter: dict | None = None,
):
    """模拟标准静态文件服务：单段 Range + If-Range 语义。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
            counter["bytes"] = counter.get("bytes", 0)
        headers = {}
        if etag is not None:
            headers["ETag"] = etag
        rng = request.headers.get("range")
        if rng and support_range:
            if_range = request.headers.get("if-range")
            if if_range is not None and if_range != etag:
                return httpx.Response(200, content=content, headers=headers)
            m = RANGE_RE.match(rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(content) - 1
            end = min(end, len(content) - 1)
            if start > end or start >= len(content):
                return httpx.Response(
                    416, headers={"Content-Range": f"bytes */{len(content)}"}
                )
            body = content[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            if counter is not None:
                counter["bytes"] += len(body)
            return httpx.Response(206, content=body, headers=headers)
        if counter is not None:
            counter["bytes"] += len(content)
        return httpx.Response(200, content=content, headers=headers)

    return handler


class FlipServer:
    """读取中换版：前 flip_after 个请求给 v1，之后一律 v2。

    honor_if_range=False 时忽略 If-Range（仍回 206 + 新 ETag），
    用于覆盖「服务器不遵守 If-Range」的 ETag 比对路径。
    """

    def __init__(self, v1: bytes, v2: bytes, flip_after: int = 1, honor_if_range: bool = True):
        self.v1, self.v2 = v1, v2
        self.flip_after = flip_after
        self.honor_if_range = honor_if_range
        self.count = 0
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            n = self.count
            self.count += 1
        content, etag = (self.v1, '"v1"') if n < self.flip_after else (self.v2, '"v2"')
        headers = {"ETag": etag}
        rng = request.headers.get("range")
        if rng:
            if_range = request.headers.get("if-range")
            if self.honor_if_range and if_range is not None and if_range != etag:
                return httpx.Response(200, content=content, headers=headers)
            m = RANGE_RE.match(rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(content) - 1
            end = min(end, len(content) - 1)
            body = content[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            return httpx.Response(206, content=body, headers=headers)
        return httpx.Response(200, content=content, headers=headers)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def build_tiff_bytes(
    data: np.ndarray,
    *,
    tile=(256, 256),
    planar: str = "contig",
    compression: str | None = None,
    geo: bool = True,
    nodata: str | None = "65535",
) -> bytes:
    extratags = []
    if geo:
        geokeys = (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 32650)
        extratags += [
            (33550, "d", 3, (0.5, 0.5, 0.0), True),
            (33922, "d", 6, (0.0, 0.0, 0.0, 500000.0, 3400000.0, 0.0), True),
            (34735, "H", len(geokeys), geokeys, True),
        ]
    if nodata is not None:
        extratags.append((42113, "s", len(nodata) + 1, nodata, True))
    buf = io.BytesIO()
    kwargs = dict(
        photometric="minisblack", planarconfig=planar, metadata=None, extratags=extratags
    )
    if tile is not None:
        kwargs["tile"] = tile
    if compression:
        kwargs["compression"] = compression
    tifffile.imwrite(buf, data, **kwargs)
    return buf.getvalue()


def sample_image(h=1536, w=2048, shift=0) -> np.ndarray:
    rows = np.arange(h, dtype=np.uint32)[:, None]
    cols = np.arange(w, dtype=np.uint32)[None, :]
    band1 = (rows * 7 + cols * 13 + shift) % 60000 + 1
    band2 = (rows * 3 + cols * 5 + shift * 2) % 60000 + 1
    img = np.stack([band1, band2], axis=-1).astype(np.uint16)
    img[0:8, 0:8, :] = 65535
    return img
