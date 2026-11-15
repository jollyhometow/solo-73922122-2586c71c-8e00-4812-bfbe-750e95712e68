"""农情数据湖地块窗口取数 API。

提交遥感影像的 HTTP URL 与像素行列的左闭右开矩形窗口，
返回保留双波段、NoData、坐标系及正确窗口定位的 GeoTIFF。

* 仅支持：无压缩、双波段、分块存储的 uint16 GeoTIFF
* 窗口最大 512×512 像素
* 通过 HTTP Range 按需读取，不整件下载
* 每次取数绑定首次取得的强 ETag；读取中换版 / 缺强 ETag /
  不支持 Range 均明确失败，不返回半成品
* 每次取数使用独立的 HTTP 客户端与缓存，并发读取互不串版
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from errors import ClipError
from geotiff import extract_window, open_and_validate, write_window
from range_reader import HttpRangeReader

MAX_WINDOW = 512

DESCRIPTION = """\
## 用法

`POST /clip`，JSON 请求体：

```json
{
  "url": "http://source:8000/images/demoland.tif",
  "col_off": 300, "row_off": 200,
  "width": 512, "height": 512
}
```

窗口为像素行列的**左闭右开**矩形：
列 `[col_off, col_off+width)`，行 `[row_off, row_off+height)`，最大 512×512。

## 失败语义（绝不返回半成品）

| 状态码 | 含义 |
| --- | --- |
| 409 | 源在读取中换版（ETag 变化 / If-Range 失效 / 长度变化） |
| 415 | 影像不满足约束（非无压缩/双波段/分块/uint16，或缺地理参考） |
| 422 | 请求参数非法或窗口越界 |
| 502 | 源不可用：不支持 Range、缺少强 ETag、连接失败等 |
"""

app = FastAPI(
    title="农情数据湖地块窗口取数 API",
    description=DESCRIPTION,
    version="1.0.0",
)


class ClipRequest(BaseModel):
    """取数请求：影像 URL + 左闭右开像素窗口。"""

    url: str = Field(description="源 GeoTIFF 的 HTTP(S) URL", examples=["http://source:8000/images/demoland.tif"])
    col_off: int = Field(ge=0, description="起始列（含）", examples=[300])
    row_off: int = Field(ge=0, description="起始行（含）", examples=[200])
    width: int = Field(ge=1, le=MAX_WINDOW, description="窗口宽（像素，≤512）", examples=[512])
    height: int = Field(ge=1, le=MAX_WINDOW, description="窗口高（像素，≤512）", examples=[512])


def make_client() -> httpx.Client:
    """每次取数创建独立客户端：无共享缓存、无共享请求配置。

    ``Accept-Encoding: identity`` 保证 Range 作用于原始字节，
    避免压缩编码干扰字节范围语义。
    """
    return httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        headers={"Accept-Encoding": "identity", "Accept": "image/tiff, */*"},
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
    )


@app.exception_handler(ClipError)
async def clip_error_handler(_request: Request, exc: ClipError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.message, "error": exc.error_code},
    )


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/clip",
    tags=["clip"],
    response_class=Response,
    responses={
        200: {"content": {"image/tiff": {}}, "description": "裁剪后的 GeoTIFF"},
        409: {"description": "源在读取中换版"},
        415: {"description": "影像格式不在支持范围内"},
        422: {"description": "参数非法或窗口越界"},
        502: {"description": "源不支持 Range / 缺强 ETag / 源不可用"},
    },
)
def clip(req: ClipRequest) -> Response:
    """从远程 GeoTIFF 裁剪指定像素窗口，返回 GeoTIFF。"""
    if not req.url.lower().startswith(("http://", "https://")):
        return JSONResponse(
            status_code=422,
            content={"detail": "url 必须是 http(s) 地址", "error": "invalid_url"},
        )

    with make_client() as client:
        reader = HttpRangeReader(client, req.url)
        try:
            info = open_and_validate(reader)
            window = extract_window(
                reader, info, req.col_off, req.row_off, req.width, req.height
            )
            payload = write_window(window, info.geo, req.col_off, req.row_off)
            etag, size = reader.etag, reader.size
        finally:
            reader.close()

    filename = f"clip_{req.col_off}_{req.row_off}_{req.width}x{req.height}.tif"
    return Response(
        content=payload,
        media_type="image/tiff",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Source-ETag": etag or "",
            "X-Source-Bytes": str(size or 0),
            "X-Window": f"cols={req.col_off}:{req.col_off + req.width} rows={req.row_off}:{req.row_off + req.height}",
        },
    )
