"""校验取数 API 产出的 GeoTIFF 是否与演示源的确定模式一致。

用法:
    python verify_clip.py <clip.tif> <col_off> <row_off> <width> <height> [shift]

校验项:
    * 双波段、uint16、窗口尺寸正确
    * ModelPixelScale / ModelTiepoint（窗口左上角定位）
    * GeoKeyDirectory 坐标系与源一致（EPSG:32650）
    * GDAL_NODATA 保留
    * 每个像元值与源模式逐点一致（含 NoData 空洞）
"""

from __future__ import annotations

import sys

import numpy as np
import tifffile

NODATA = 65535
SCALE = (0.5, 0.5, 0.0)
TIE_X, TIE_Y = 500000.0, 3400000.0
GEOKEYS = (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 32650)


def expected_window(row_off: int, col_off: int, height: int, width: int, shift: int) -> np.ndarray:
    rows = (np.arange(height, dtype=np.uint32) + row_off)[:, None]
    cols = (np.arange(width, dtype=np.uint32) + col_off)[None, :]
    band1 = (rows * 7 + cols * 13 + shift) % 60000 + 1
    band2 = (rows * 3 + cols * 5 + shift * 2) % 60000 + 1
    img = np.stack([band1, band2], axis=-1).astype(np.uint16)
    # 源影像左上角 8×8 为 NoData 空洞
    r0, r1 = max(0, 0 - row_off), min(height, 8 - row_off)
    c0, c1 = max(0, 0 - col_off), min(width, 8 - col_off)
    if r0 < r1 and c0 < c1:
        img[r0:r1, c0:c1, :] = NODATA
    return img


def main() -> int:
    path, col, row, w, h = sys.argv[1], *map(int, sys.argv[2:6])
    # shift 缺省 = auto：依次尝试 v1(0)/v2(1000)，任一完全一致即通过，
    # 两个版本都不匹配说明像元被混版污染。
    shifts = [int(sys.argv[6])] if len(sys.argv) > 6 else [0, 1000]
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        check("双波段", page.samplesperpixel == 2, f"samples={page.samplesperpixel}")
        check("uint16", page.dtype == np.uint16, f"dtype={page.dtype}")
        check(
            "窗口尺寸",
            (page.imagelength, page.imagewidth) == (h, w),
            f"{page.imagewidth}x{page.imagelength} 期望 {w}x{h}",
        )
        scale = page.tags[33550].value
        check("像元分辨率保留", tuple(scale[:2]) == SCALE[:2], str(scale))
        tie = page.tags[33922].value
        exp_x, exp_y = TIE_X + col * SCALE[0], TIE_Y - row * SCALE[1]
        check(
            "窗口定位（ModelTiepoint）",
            abs(tie[3] - exp_x) < 1e-9 and abs(tie[4] - exp_y) < 1e-9,
            f"({tie[3]}, {tie[4]}) 期望 ({exp_x}, {exp_y})",
        )
        geokeys = page.tags.get(34735)
        check(
            "坐标系保留（EPSG:32650）",
            geokeys is not None and tuple(int(k) for k in geokeys.value) == GEOKEYS,
        )
        nodata = page.tags.get(42113)
        check(
            "NoData 保留",
            nodata is not None and str(nodata.value).strip("\x00 ").strip() == str(NODATA),
            None if nodata is None else repr(nodata.value),
        )
        data = page.asarray()

    matched = None
    for cand in shifts:
        if np.array_equal(data, expected_window(row, col, h, w, cand)):
            matched = cand
            break
    check(
        "像元值逐点一致",
        matched is not None,
        "全部一致" if matched is not None else "与任何已知版本都不一致（疑似混版）",
    )

    if failures:
        print(f"校验失败: {failures}")
        return 1
    print("全部校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
