"""Минимальный DNG (байеровская мозаика RGGB, 12 бит) для проверки приёма RAW. python make_dng.py out.dng"""
import sys
import numpy as np
import tifffile

h, w = 2000, 3000
y, x = np.mgrid[0:h, 0:w]
r = (x / w * 3000 + 300).astype(np.uint16)        # красный растёт слева направо
g = (y / h * 3000 + 300).astype(np.uint16)        # зелёный — сверху вниз
b = np.full((h, w), 1200, np.uint16)
bayer = np.zeros((h, w), np.uint16)
bayer[0::2, 0::2] = r[0::2, 0::2]
bayer[0::2, 1::2] = g[0::2, 1::2]
bayer[1::2, 0::2] = g[1::2, 0::2]
bayer[1::2, 1::2] = b[1::2, 1::2]

srat = lambda v: (int(round(v * 10000)), 10000)
cm = [srat(v) for v in (1, 0, 0, 0, 1, 0, 0, 0, 1)]
extratags = [
    (254, "I", 1, 0, True),                       # NewSubfileType: основное изображение
    (33421, "H", 2, (2, 2), True),                # CFARepeatPatternDim
    (33422, "B", 4, (0, 1, 1, 2), True),          # CFAPattern: RGGB
    (50706, "B", 4, (1, 4, 0, 0), True),          # DNGVersion 1.4
    (50707, "B", 4, (1, 1, 0, 0), True),          # DNGBackwardVersion
    (50708, "s", 0, "Test Camera", True),         # UniqueCameraModel
    (50714, "H", 1, 0, True),                     # BlackLevel
    (50717, "H", 1, 4095, True),                  # WhiteLevel
    (50721, "2i", 9, [v for p in cm for v in p], True),          # ColorMatrix1
    (50778, "H", 1, 21, True),                    # CalibrationIlluminant1: D65
    (50728, "2I", 3, (1, 1, 1, 1, 1, 1), True),   # AsShotNeutral
    (271, "s", 0, "Test", True),                  # Make
    (272, "s", 0, "Camera", True),                # Model
    (306, "s", 0, sys.argv[2] if len(sys.argv) > 2 else "2026:10:01 12:34:56", True),   # DateTime (как у камер в IFD0)
]
tifffile.imwrite(sys.argv[1], bayer, photometric=32803, extratags=extratags, compression=None, bitspersample=16)
print("written", sys.argv[1])
