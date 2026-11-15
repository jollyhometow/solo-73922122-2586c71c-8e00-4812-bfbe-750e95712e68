"""HttpRangeReader 的版本绑定与 Range 语义测试。"""

import threading

import pytest

from conftest import FlipServer, client_for, make_range_handler
from errors import SourceError, VersionChangedError
from range_reader import HttpRangeReader

CONTENT = bytes(range(256)) * 4000  # 约 1 MB 的确定内容
URL = "http://test/img.tif"


def make_reader(handler, **kw):
    return HttpRangeReader(client_for(handler), URL, **kw)


def test_normal_range_read_binds_etag():
    reader = make_reader(make_range_handler(CONTENT))
    assert reader.etag == '"v1"'
    assert reader.size == len(CONTENT)
    assert reader.read_at(1000, 100) == CONTENT[1000:1100]
    reader.seek(-50, 2)
    assert reader.read() == CONTENT[-50:]
    reader.seek(0)
    assert reader.read(16) == CONTENT[:16]


def test_no_range_support_fails():
    handler = make_range_handler(CONTENT, support_range=False)
    with pytest.raises(SourceError, match="Range"):
        make_reader(handler)


def test_missing_etag_fails():
    handler = make_range_handler(CONTENT, etag=None)
    with pytest.raises(SourceError, match="ETag"):
        make_reader(handler)


def test_weak_etag_fails():
    handler = make_range_handler(CONTENT, etag='W/"weak"')
    with pytest.raises(SourceError, match="弱 ETag"):
        make_reader(handler)


def test_etag_change_mid_read_fails():
    # 服务器不遵守 If-Range，仍回 206 但 ETag 已变 → 必须识别为换版
    server = FlipServer(CONTENT, CONTENT + b"x" * 100, flip_after=2, honor_if_range=False)
    reader = make_reader(server)          # 请求 1（探测）→ v1
    reader.read_exact(300000, 1000)       # 请求 2 → 仍是 v1
    with pytest.raises(VersionChangedError, match="ETag"):
        reader.read_exact(900000, 1000)   # 请求 3 → v2，ETag 不一致


def test_if_range_200_means_version_changed():
    server = FlipServer(CONTENT, CONTENT, flip_after=1)  # 第 2 个请求起换版
    reader = make_reader(server)  # 探测请求绑定 "v1"
    with pytest.raises(VersionChangedError, match="If-Range|200"):
        reader.read_exact(500000, 1000)


def test_size_change_mid_read_fails():
    # ETag 不变但 Content-Range 总长变化 → 同样视为换版
    state = {"n": 0}
    base = make_range_handler(CONTENT)

    def handler(request):
        state["n"] += 1
        resp = base(request)
        if state["n"] > 1 and "Content-Range" in resp.headers:
            cr = resp.headers["Content-Range"]
            start_end, _total = cr.rsplit("/", 1)
            resp.headers["Content-Range"] = f"{start_end}/{len(CONTENT) + 1}"
        return resp

    reader = make_reader(handler)
    with pytest.raises(VersionChangedError, match="长度"):
        reader.read_exact(700000, 1000)


def test_read_beyond_eof_clamped():
    reader = make_reader(make_range_handler(CONTENT))
    data = reader.read_at(len(CONTENT) - 10, 100)
    assert data == CONTENT[-10:]


def test_concurrent_readers_never_mix_versions():
    """换版瞬间并发取数：每个 reader 要么拿到一致版本，要么明确失败。"""
    v1 = CONTENT
    v2 = bytes(b ^ 0xFF for b in CONTENT)  # 内容完全不同的新版本
    # 仅第 1 个请求给 v1：绑定 v1 的 reader 后续读取必然失败，
    # 其余 reader 绑定 v2 并完整成功 —— 任何混拼都逃不过断言。
    server = FlipServer(v1, v2, flip_after=1)

    results, errors = [], []

    def work():
        try:
            reader = make_reader(server)
            data = reader.read_exact(0, 100000) + reader.read_exact(500000, 100000)
            results.append(data)
        except VersionChangedError:
            errors.append("changed")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors, "换版期间应至少有一个 reader 明确失败"
    for data in results:
        first, second = data[:100000], data[100000:]
        # 两段必须来自同一版本，不允许 v1+v2 混拼
        assert (first == v1[:100000] and second == v1[500000:600000]) or (
            first == v2[:100000] and second == v2[500000:600000]
        )
