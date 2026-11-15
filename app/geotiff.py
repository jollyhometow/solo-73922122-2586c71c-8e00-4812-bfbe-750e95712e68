"""GeoTIFF 校验、窗口裁剪与成果写出。

输入约束（不满足即 415 明确失败）：
  * 无压缩（Compression = 1）
  * 双波段（SamplesPerPixel = 2）
  * 分块存储（tiled，非 strip）
  * uint16（BitsPerSample = 16，SampleFormat = uint）
  * 具备地理参考：ModelPixelScale + ModelTiepoint + GeoKeyDirectory

输出成果：
  * 保留双波段、NoData（GDAL_NODATA）、坐标系（GeoKeyDirectory 原样透传）
  * 按窗口左上角重算 ModelTiepoint，保证窗口定位正确
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import tifffile

from errors import (
    SourceError,
    UnsupportedFormatError,
    VersionChangedError,
    WindowError,
)
from range_reader import HttpRangeReader

TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_MODEL_TRANSFORMATION = 34264
TAG_GEOKEY_DIRECTORY = 34735
TAG_GDAL_NODATA = 42113

PLANAR_CONTIG = 1
PLANAR_SEPARATE = 2


@dataclass(frozen=True)
class SourceGeo:
    """源影像的地理参考信息。"""

    scale_x: float
    scale_y: float
    tie_i: float
    tie_j: float
    tie_x: float
    tie_y: float
    geokeys: tuple[int, ...]
    nodata: str | None


@dataclass(frozen=True)
class TiffInfo:
    """校验通过后提取的源影像结构信息。"""

    width: int
    height: int
    tile_width: int
    tile_height: int
    planar_config: int
    tile_offsets: tuple[int, ...]
    tile_bytecounts: tuple[int, ...]
    geo: SourceGeo

    @property
    def tiles_across(self) -> int:
        return -(-self.width // self.tile_width)

    @property
    def tiles_down(self) -> int:
        return -(-self.height // self.tile_height)


def open_and_validate(reader: HttpRangeReader) -> TiffInfo:
    """解析 TIFF 结构并校验全部约束。版本类错误原样向上抛。"""
    try:
        tif = tifffile.TiffFile(reader)
        page = tif.pages[0]
        # 触发所需标签的读取（标签值数组可能不在首个块内）
        _ = page.tags[324].value  # TileOffsets
        _ = page.tags[325].value  # TileByteCounts
    except (SourceError, VersionChangedError):
        raise
    except Exception as exc:
        raise UnsupportedFormatError(f"无法解析为分块 GeoTIFF：{exc}") from exc

    if not page.is_tiled:
        raise UnsupportedFormatError("仅支持分块（tiled）存储的 GeoTIFF，该影像为条带存储")
    if int(page.compression) != 1:
        raise UnsupportedFormatError(
            f"仅支持无压缩影像，该影像压缩方式为 {page.compression.name}"
        )
    if page.samplesperpixel != 2:
        raise UnsupportedFormatError(
            f"仅支持双波段影像，该影像波段数为 {page.samplesperpixel}"
        )
    if page.bitspersample != 16 or page.dtype != np.uint16:
        raise UnsupportedFormatError(
            f"仅支持 uint16 影像，该影像为 {page.bitspersample}bit/{page.dtype}"
        )
    planar = int(page.planarconfig)
    if planar not in (PLANAR_CONTIG, PLANAR_SEPARATE):
        raise UnsupportedFormatError(f"不支持的 PlanarConfiguration: {planar}")

    if TAG_MODEL_TRANSFORMATION in page.tags:
        raise UnsupportedFormatError("不支持带仿射变换矩阵（ModelTransformation）的影像")
    scale_tag = page.tags.get(TAG_MODEL_PIXEL_SCALE)
    tie_tag = page.tags.get(TAG_MODEL_TIEPOINT)
    geokey_tag = page.tags.get(TAG_GEOKEY_DIRECTORY)
    if scale_tag is None or tie_tag is None:
        raise UnsupportedFormatError("源影像缺少 ModelPixelScale/ModelTiepoint 地理参考")
    if geokey_tag is None:
        raise UnsupportedFormatError("源影像缺少 GeoKeyDirectory 坐标系信息")
    tie = tie_tag.value
    if len(tie) < 6:
        raise UnsupportedFormatError("ModelTiepoint 标签内容不完整")

    nodata_tag = page.tags.get(TAG_GDAL_NODATA)
    nodata = None
    if nodata_tag is not None:
        nodata = str(nodata_tag.value).strip("\x00").strip() or None

    geo = SourceGeo(
        scale_x=float(scale_tag.value[0]),
        scale_y=float(scale_tag.value[1]),
        tie_i=float(tie[0]),
        tie_j=float(tie[1]),
        tie_x=float(tie[3]),
        tie_y=float(tie[4]),
        geokeys=tuple(int(k) for k in geokey_tag.value),
        nodata=nodata,
    )

    return TiffInfo(
        width=int(page.imagewidth),
        height=int(page.imagelength),
        tile_width=int(page.tilewidth),
        tile_height=int(page.tilelength),
        planar_config=planar,
        tile_offsets=tuple(int(v) for v in page.tags[324].value),
        tile_bytecounts=tuple(int(v) for v in page.tags[325].value),
        geo=geo,
    )


def extract_window(
    reader: HttpRangeReader,
    info: TiffInfo,
    col_off: int,
    row_off: int,
    width: int,
    height: int,
) -> np.ndarray:
    """按左闭右开窗口 [col_off, col_off+width) × [row_off, row_off+height) 取数。

    只下载与窗口相交的 tile，返回 (height, width, 2) 的 uint16 数组。
    """
    if col_off + width > info.width or row_off + height > info.height:
        raise WindowError(
            f"窗口 [{col_off}:{col_off + width}, {row_off}:{row_off + height}] "
            f"超出影像范围（宽 {info.width} × 高 {info.height}）"
        )

    tw, th = info.tile_width, info.tile_height
    tiles_x, tiles_y = info.tiles_across, info.tiles_down
    contig = info.planar_config == PLANAR_CONTIG

    tx0, tx1 = col_off // tw, (col_off + width - 1) // tw
    ty0, ty1 = row_off // th, (row_off + height - 1) // th

    # 收集需要下载的 tile：(offset, bytecount, ty, tx, band)
    requests: list[tuple[int, int, int, int, int | None]] = []
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            if contig:
                idx = ty * tiles_x + tx
                requests.append(
                    (info.tile_offsets[idx], info.tile_bytecounts[idx], ty, tx, None)
                )
            else:
                for band in (0, 1):
                    idx = band * tiles_x * tiles_y + ty * tiles_x + tx
                    requests.append(
                        (info.tile_offsets[idx], info.tile_bytecounts[idx], ty, tx, band)
                    )

    # 无压缩标准分块：字节数必须恰好等于完整分块大小
    expected = tw * th * 2 * (2 if contig else 1)
    for offset, count, _, _, _ in requests:
        if count != expected:
            raise UnsupportedFormatError(
                f"分块字节数异常（期望 {expected}，实际 {count}），非标准无压缩分块影像"
            )

    # 相邻 tile 合并为一次 Range 请求，减少往返
    requests.sort(key=lambda r: r[0])
    spans: list[tuple[int, int, list[int]]] = []  # (start, end, 请求下标)
    for i, (offset, count, *_rest) in enumerate(requests):
        if spans and offset == spans[-1][1]:
            start, _end, idxs = spans[-1]
            spans[-1] = (start, offset + count, idxs + [i])
        else:
            spans.append((offset, offset + count, [i]))

    buffers: list[bytes] = [b""] * len(requests)
    for start, end, idxs in spans:
        data = reader.read_exact(start, end - start)
        pos = start
        for i in idxs:
            offset, count = requests[i][0], requests[i][1]
            assert offset == pos  # 合并逻辑保证连续
            buffers[i] = data[pos - start : pos - start + count]
            pos += count

    out = np.empty((height, width, 2), dtype=np.uint16)
    for (offset, _count, ty, tx, band), buf in zip(requests, buffers):
        arr = np.frombuffer(buf, dtype=np.uint16)
        # 窗口与 tile 的交集（tile 坐标系 / 输出坐标系）
        gx0, gy0 = tx * tw, ty * th
        ix0, iy0 = max(col_off, gx0), max(row_off, gy0)
        ix1, iy1 = min(col_off + width, gx0 + tw), min(row_off + height, gy0 + th)
        dst = (slice(iy0 - row_off, iy1 - row_off), slice(ix0 - col_off, ix1 - col_off))
        src = (slice(iy0 - gy0, iy1 - gy0), slice(ix0 - gx0, ix1 - gx0))
        if contig:
            tile = arr.reshape(th, tw, 2)
            out[dst[0], dst[1], :] = tile[src[0], src[1], :]
        else:
            tile = arr.reshape(th, tw)
            out[dst[0], dst[1], band] = tile[src[0], src[1]]
    return out


def write_window(
    window: np.ndarray,
    geo: SourceGeo,
    col_off: int,
    row_off: int,
) -> bytes:
    """把窗口数组写成 GeoTIFF：保留双波段、NoData、坐标系与正确定位。"""
    # 窗口左上角对应的地图坐标 → 新的 ModelTiepoint
    origin_x = geo.tie_x + (col_off - geo.tie_i) * geo.scale_x
    origin_y = geo.tie_y - (row_off - geo.tie_j) * geo.scale_y

    extratags: list[tuple] = [
        (TAG_MODEL_PIXEL_SCALE, "d", 3, (geo.scale_x, geo.scale_y, 0.0), True),
        (TAG_MODEL_TIEPOINT, "d", 6, (0.0, 0.0, 0.0, origin_x, origin_y, 0.0), True),
        (TAG_GEOKEY_DIRECTORY, "H", len(geo.geokeys), geo.geokeys, True),
    ]
    if geo.nodata is not None:
        extratags.append(
            (TAG_GDAL_NODATA, "s", len(geo.nodata) + 1, geo.nodata, True)
        )

    buf = io.BytesIO()
    tifffile.imwrite(
        buf,
        window,
        photometric="minisblack",
        planarconfig="contig",
        metadata=None,
        extratags=extratags,
    )
    return buf.getvalue()
