"""本地演示影像源：模拟远端 GeoTIFF 服务的各种行为。

* ``GET /images/{name}``       正常服务：支持 Range + 强 ETag
* ``GET /flaky/image.tif``     读取中换版：第 1 个请求给 v1，之后给 v2
* ``POST /flaky/reset``        复位换版计数器
* ``GET /norange/image.tif``   不支持 Range（始终 200 全量）
* ``GET /weak-etag/image.tif`` 仅提供弱 ETag
* ``GET /no-etag/image.tif``   不提供 ETag
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
V1_NAME = "demoland.tif"
V2_NAME = "demoland_v2.tif"

app = FastAPI(title="演示影像源", version="1.0.0")

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_NAME_RE = re.compile(r"^[\w][\w.-]*$")

_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()


def _load(name: str) -> bytes:
    with _cache_lock:
        data = _cache.get(name)
        if data is None:
            data = (DATA_DIR / name).read_bytes()
            _cache[name] = data
        return data


def _strong_etag(content: bytes) -> str:
    return '"' + hashlib.sha256(content).hexdigest()[:24] + '"'


def _range_response(
    content: bytes,
    request: Request,
    *,
    etag: str | None,
    support_range: bool = True,
) -> Response:
    """按 RFC 7233 处理单段 Range / If-Range，模拟标准静态文件服务。"""
    headers = {
        "Content-Type": "image/tiff",
        "Accept-Ranges": "bytes" if support_range else "none",
    }
    if etag is not None:
        headers["ETag"] = etag

    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")

    if range_header and support_range:
        # If-Range 不匹配 → 按规范返回 200 全量（新版本）
        if if_range is not None and etag is not None and if_range.strip() != etag:
            return Response(content, status_code=200, headers=headers)
        m = _RANGE_RE.match(range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(content) - 1
            end = min(end, len(content) - 1)
            if start > end or start >= len(content):
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{len(content)}"},
                )
            body = content[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            headers["Content-Length"] = str(len(body))
            return Response(body, status_code=206, headers=headers)

    return Response(content, status_code=200, headers=headers)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "endpoints": [
            "/images/demoland.tif  (正常: Range + 强 ETag)",
            "/flaky/image.tif      (读取中换版; POST /flaky/reset 复位)",
            "/norange/image.tif    (不支持 Range)",
            "/weak-etag/image.tif  (弱 ETag)",
            "/no-etag/image.tif    (无 ETag)",
        ]
    }


@app.get("/images/{name}")
def images(name: str, request: Request) -> Response:
    if not _NAME_RE.match(name) or not (DATA_DIR / name).is_file():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    content = _load(name)
    return _range_response(content, request, etag=_strong_etag(content))


# --------------------------------------------------------------------- #
# 读取中换版：第一个请求给 v1，其后一律给 v2（带新 ETag 与新内容）
# --------------------------------------------------------------------- #
_flaky = {"count": 0}
_flaky_lock = threading.Lock()


@app.post("/flaky/reset")
def flaky_reset() -> dict:
    with _flaky_lock:
        _flaky["count"] = 0
    return {"reset": True}


@app.get("/flaky/image.tif")
def flaky(request: Request) -> Response:
    with _flaky_lock:
        count = _flaky["count"]
        _flaky["count"] += 1
    name = V1_NAME if count == 0 else V2_NAME
    content = _load(name)
    return _range_response(content, request, etag=_strong_etag(content))


@app.get("/norange/image.tif")
def norange(request: Request) -> Response:
    content = _load(V1_NAME)
    return _range_response(
        content, request, etag=_strong_etag(content), support_range=False
    )


@app.get("/weak-etag/image.tif")
def weak_etag(request: Request) -> Response:
    content = _load(V1_NAME)
    return _range_response(content, request, etag='W/"weak-0001"')


@app.get("/no-etag/image.tif")
def no_etag(request: Request) -> Response:
    content = _load(V1_NAME)
    return _range_response(content, request, etag=None)
