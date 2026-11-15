"""GeoTIFF 校验、窗口裁剪、成果写出的单元测试。"""

import io

import numpy as np
import pytest
import tifffile

from conftest import build_tiff_bytes, client_for, make_range_handler, sample_image
from errors import UnsupportedFormatError, WindowError
from geotiff import extract_window, open_and_validate, write_window
from range_reader import HttpRangeReader

URL = "http://test/img.tif"


def reader_for(content: bytes, counter=None) -> HttpRangeReader:
    return HttpRangeReader(
        client_for(make_range_handler(content, counter=counter)), URL
    )


@pytest.fixture()
def img():
    return sample_image()


@pytest.fixture()
def tif_bytes(img):
    return build_tiff_bytes(img)


def test_validate_ok(tif_bytes):
    reader = reader_for(tif_bytes)
    info = open_and_validate(reader)
    assert (info.width, info.height) == (2048, 1536)
    assert (info.tile_width, info.tile_height) == (256, 256)
    assert info.geo.nodata == "65535"
    assert info.geo.geokeys[-1] == 32650


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(compression="deflate"), "无压缩"),
        (dict(tile=None), "分块"),
        (dict(geo=False), "地理参考|坐标系"),
    ],
)
def test_validate_rejects(img, kwargs, match):
    data = build_tiff_bytes(img, **kwargs)
    with pytest.raises(UnsupportedFormatError, match=match):
        open_and_validate(reader_for(data))


def test_validate_rejects_band_count_and_dtype():
    one_band = np.zeros((256, 256), np.uint16)
    with pytest.raises(UnsupportedFormatError, match="双波段"):
        open_and_validate(reader_for(build_tiff_bytes(one_band)))
    u8 = np.zeros((512, 512, 2), np.uint8)
    with pytest.raises(UnsupportedFormatError, match="uint16"):
        open_and_validate(reader_for(build_tiff_bytes(u8)))


@pytest.mark.parametrize("col,row,w,h", [(0, 0, 64, 64), (300, 200, 512, 512),
                                          (255, 255, 130, 7), (2048 - 512, 1536 - 512, 512, 512)])
def test_extract_window_matches_reference(img, tif_bytes, col, row, w, h):
    reader = reader_for(tif_bytes)
    info = open_and_validate(reader)
    out = extract_window(reader, info, col, row, w, h)
    assert out.shape == (h, w, 2)
    np.testing.assert_array_equal(out, img[row : row + h, col : col + w, :])


def test_extract_window_separate_planar(img):
    data = build_tiff_bytes(np.moveaxis(img, -1, 0).copy(), planar="separate")
    reader = reader_for(data)
    info = open_and_validate(reader)
    assert info.planar_config == 2
    out = extract_window(reader, info, 300, 200, 512, 300)
    np.testing.assert_array_equal(out, img[200:500, 300:812, :])


def test_extract_window_out_of_bounds(tif_bytes):
    reader = reader_for(tif_bytes)
    info = open_and_validate(reader)
    with pytest.raises(WindowError):
        extract_window(reader, info, 2000, 0, 100, 100)


def test_no_full_download(img, tif_bytes):
    """小窗口只应下载元数据块 + 相交 tile，远小于整件。"""
    counter = {}
    reader = reader_for(tif_bytes, counter=counter)
    info = open_and_validate(reader)
    extract_window(reader, info, 300, 200, 128, 128)
    assert counter["bytes"] < len(tif_bytes) // 4
    assert counter["bytes"] < 1 << 20  # 远小于 12MB 整件


def test_write_window_preserves_geo_and_nodata(img, tif_bytes):
    reader = reader_for(tif_bytes)
    info = open_and_validate(reader)
    col, row, w, h = 300, 200, 512, 512
    out = extract_window(reader, info, col, row, w, h)
    payload = write_window(out, info.geo, col, row)

    with tifffile.TiffFile(io.BytesIO(payload)) as tif:
        page = tif.pages[0]
        assert page.samplesperpixel == 2
        assert page.dtype == np.uint16
        assert (page.imagewidth, page.imagelength) == (w, h)
        assert tuple(page.tags[33550].value[:2]) == (0.5, 0.5)
        tie = page.tags[33922].value
        assert tie[3] == 500000.0 + col * 0.5
        assert tie[4] == 3400000.0 - row * 0.5
        assert tuple(int(k) for k in page.tags[34735].value) == info.geo.geokeys
        assert str(page.tags[42113].value).strip("\x00 ") == "65535"
        np.testing.assert_array_equal(page.asarray(), out)
