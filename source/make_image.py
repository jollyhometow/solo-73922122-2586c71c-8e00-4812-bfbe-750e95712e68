"""生成演示用 GeoTIFF（无压缩、双波段、分块、uint16，含 NoData 与坐标系）。

* demoland.tif    —— v1 版本（shift=0）
* demoland_v2.tif —— v2 版本（shift=1000，模拟源换版后的新内容）

像元值是 (行, 列) 的确定函数，便于端到端校验窗口定位是否正确：
    band1 = (row*7 + col*13 + shift) % 60000 + 1
    band2 = (row*3 + col*5  + shift*2) % 60000 + 1
左上角 8×8 为 NoData 空洞（65535），用于演示 NoData 保留。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import tifffile

HEIGHT, WIDTH = 1536, 2048
TILE = 256
NODATA = 65535
SCALE = (0.5, 0.5, 0.0)
TIEPOINT = (0.0, 0.0, 0.0, 500000.0, 3400000.0, 0.0)  # 左上角地图坐标
GEOKEYS = (  # EPSG:32650（WGS 84 / UTM zone 50N）
    1, 1, 0, 3,
    1024, 0, 1, 1,      # GTModelType = Projected
    1025, 0, 1, 1,      # GTRasterType = PixelIsArea
    3072, 0, 1, 32650,  # ProjectedCSType = EPSG:32650
)


def pattern(shift: int = 0) -> np.ndarray:
    rows = np.arange(HEIGHT, dtype=np.uint32)[:, None]
    cols = np.arange(WIDTH, dtype=np.uint32)[None, :]
    band1 = (rows * 7 + cols * 13 + shift) % 60000 + 1
    band2 = (rows * 3 + cols * 5 + shift * 2) % 60000 + 1
    img = np.stack([band1, band2], axis=-1).astype(np.uint16)
    img[0:8, 0:8, :] = NODATA  # 左上角 NoData 空洞
    return img


def write(path: Path, img: np.ndarray) -> None:
    tifffile.imwrite(
        str(path),
        img,
        tile=(TILE, TILE),
        photometric="minisblack",
        planarconfig="contig",
        metadata=None,
        extratags=[
            (33550, "d", 3, SCALE, True),       # ModelPixelScale
            (33922, "d", 6, TIEPOINT, True),    # ModelTiepoint
            (34735, "H", len(GEOKEYS), GEOKEYS, True),  # GeoKeyDirectory
            (42113, "s", 6, str(NODATA), True),  # GDAL_NODATA
        ],
    )


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, shift in (("demoland.tif", 0), ("demoland_v2.tif", 1000)):
        path = out_dir / name
        write(path, pattern(shift))
        print(f"{path}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
