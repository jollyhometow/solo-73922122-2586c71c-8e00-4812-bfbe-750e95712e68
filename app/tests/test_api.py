"""API 层测试：正常裁剪 + 各类失败的 HTTP 状态码映射。"""

import io

import httpx
import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

import main
from conftest import (
    FlipServer,
    build_tiff_bytes,
    make_range_handler,
    sample_image,
)

IMG = sample_image()
TIF = build_tiff_bytes(IMG)
URL = "http://test/img.tif"


@pytest.fixture()
def client():
    return TestClient(main.app)


def mount(monkeypatch, handler):
    def fake_make_client():
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main, "make_client", fake_make_client)


def post(client, **kw):
    body = {"url": URL, "col_off": 0, "row_off": 0, "width": 64, "height": 64}
    body.update(kw)
    return client.post("/clip", json=body)


def test_clip_ok(client, monkeypatch):
    mount(monkeypatch, make_range_handler(TIF))
    resp = post(client, col_off=300, row_off=200, width=512, height=512)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/tiff"
    assert resp.headers["x-source-etag"] == '"v1"'
    with tifffile.TiffFile(io.BytesIO(resp.content)) as tif:
        page = tif.pages[0]
        assert page.samplesperpixel == 2
        np.testing.assert_array_equal(page.asarray(), IMG[200:712, 300:812, :])
        assert page.tags[33922].value[3] == 500000.0 + 300 * 0.5


def test_clip_version_changed_409(client, monkeypatch):
    mount(monkeypatch, FlipServer(TIF, TIF + b"\x00" * 8, flip_after=1))
    resp = post(client)
    assert resp.status_code == 409
    assert resp.json()["error"] == "version_changed"


def test_clip_no_range_502(client, monkeypatch):
    mount(monkeypatch, make_range_handler(TIF, support_range=False))
    resp = post(client)
    assert resp.status_code == 502
    assert "Range" in resp.json()["detail"]


def test_clip_weak_etag_502(client, monkeypatch):
    mount(monkeypatch, make_range_handler(TIF, etag='W/"x"'))
    assert post(client).status_code == 502


def test_clip_no_etag_502(client, monkeypatch):
    mount(monkeypatch, make_range_handler(TIF, etag=None))
    assert post(client).status_code == 502


def test_clip_unsupported_format_415(client, monkeypatch):
    compressed = build_tiff_bytes(IMG, compression="deflate")
    mount(monkeypatch, make_range_handler(compressed))
    resp = post(client)
    assert resp.status_code == 415


def test_clip_window_validation_422(client, monkeypatch):
    mount(monkeypatch, make_range_handler(TIF))
    assert post(client, width=513).status_code == 422      # 超过 512
    assert post(client, width=0).status_code == 422        # 非法尺寸
    assert post(client, col_off=-1).status_code == 422     # 负偏移
    assert post(client, col_off=2000).status_code == 422   # 越界
    assert post(client, url="ftp://x/y.tif").status_code == 422


def test_clip_source_unreachable_502(client, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    mount(monkeypatch, handler)
    assert post(client).status_code == 502
