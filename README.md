# 农情数据湖地块窗口取数服务

从远端遥感影像（HTTP GeoTIFF）按需裁剪地块窗口，交给下游植被指数作业。
FastAPI 取数 API + 本地演示影像源 + Docker Compose 一键启动。

## 功能与约束

- 提交 **HTTP URL** 和像素行列的**左闭右开**矩形窗口
  （列 `[col_off, col_off+width)`，行`[row_off, row_off+height)`），返回 GeoTIFF。
- 成果保留**双波段、NoData、坐标系**及**正确的窗口定位**（重算 ModelTiepoint）。
- 仅支持**无压缩、双波段、分块（tiled）存储的 uint16** GeoTIFF；
  窗口最大 **512×512** 像素。
- 全程通过 **HTTP Range** 按需读取（元数据块缓存 + 相交 tile 精确范围下载，
  相邻 tile 合并请求），**绝不整件下载**。
- 每次取数**绑定首次取得的强 ETag**；后续请求带 `If-Range` 并逐响应比对 ETag。
  源在读取中换版（HTTP 409）、缺少强 ETag 或不支持 Range（HTTP 502）时
  **明确失败，不返回半成品**。
- 每次取数创建**独立的 HTTP 客户端与缓存**（无共享缓存、无共享请求配置），
  并发读取同一 URL 不会串版。

## 快速开始

```bash
# Docker Compose 一键构建、启动并跑完全部演示场景
bash scripts/demo.sh
```

或手动：

```bash
docker compose up -d --build --wait
# 取数 API: http://localhost:8000  (交互文档 http://localhost:8000/docs)
# 影像源:   http://localhost:8001
bash scripts/checks.sh   # VERIFY_MODE=docker SRC_INTERNAL=http://source:8000
```

无 Docker 的本地环境（需要 Python 3.11+ 及 requirements 依赖）：

```bash
PYTHON=/path/to/python bash scripts/run_local.sh
```

## API

`POST /clip`

```json
{
  "url": "http://source:8000/images/demoland.tif",
  "col_off": 300, "row_off": 200,
  "width": 512, "height": 512
}
```

- `200 image/tiff`：裁剪成果（响应头带 `X-Source-ETag`）。
- `409`：读取中源换版（ETag 变化 / `If-Range` 失效 / 资源长度变化）。
- `415`：影像不满足无压缩/双波段/分块/uint16/地理参考约束。
- `422`：参数非法（窗口 >512、负偏移、非 http URL）或窗口越界。
- `502`：源不支持 Range、缺强 ETag、弱 ETag、源不可达。

交互文档见 `/docs`（Swagger UI）。

## 演示影像源（source 服务）

| 端点 | 行为 |
| --- | --- |
| `GET /images/demoland.tif` | 正常：支持 Range + 强 ETag |
| `GET /flaky/image.tif` | **读取中换版**：第 1 个请求给 v1，之后给 v2（`POST /flaky/reset` 复位） |
| `GET /norange/image.tif` | 不支持 Range（始终 200 全量） |
| `GET /weak-etag/image.tif` | 仅弱 ETag |
| `GET /no-etag/image.tif` | 无 ETag |

演示影像为 2048×1536、256×256 分块、双波段 uint16（EPSG:32650，
NoData=65535，左上角 8×8 为 NoData 空洞），像元值是 `(行,列)` 的确定函数，
`app/verify_clip.py` 据此逐像元校验裁剪正确性。

`scripts/checks.sh` 覆盖场景：正常裁剪（含 NoData 窗口）、读取中换版、
不支持 Range、弱/无 ETag、窗口约束、**并发 6 路读取换版中的同一 URL**
（结果要么完整一致、要么明确 409，绝不串版）。

## 设计要点

```
app/
  main.py         FastAPI 入口；每次取数新建独立 httpx.Client（无共享缓存/配置）
  range_reader.py Range 文件对象：首响应必须 206+强 ETag 并绑定；
                  后续请求带 If-Range，ETag/长度任何变化即失败
  geotiff.py      tifffile 解析校验（无压缩/双波段/分块/uint16/地理参考），
                  只下载相交 tile，重算 ModelTiepoint 写出成果
source/
  main.py         演示影像源（正常/换版/无 Range/弱 ETag/无 ETag）
  make_image.py   生成确定模式的演示 GeoTIFF
```

- **版本绑定**：首个响应必须是 `206` 且带强 ETag（弱 ETag `W/` 拒绝）；
  之后每个请求带 `If-Range: <etag>`，服务器若换版会回 `200`（显式失败）；
  即便服务器不遵守 `If-Range`，逐响应的 ETag 与 `Content-Range` 总长比对
  也能识别换版。
- **按需读取**：TIFF 头/IFD/标签数组走 256KiB 块缓存；tile 走精确 Range，
  同窗口内相邻 tile 合并为一次请求。512×512 窗口（256 分块）最多 9 个 tile。
- **不串版**：ETag 绑定、块缓存都是每次取数私有；客户端也是每请求新建，
  并强制 `Accept-Encoding: identity` 保证 Range 作用于原始字节。

## 测试

```bash
cd app && python -m pytest tests/ -q     # 30 个单元/集成测试
```
