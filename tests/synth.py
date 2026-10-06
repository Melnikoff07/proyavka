"""Тестовые кадры без чужих фото: «ночная улица» с огнями, EXIF как у камеры и минимальные DNG.
Создаются при первом запуске в tests/testdata (там же их найдут тесты и стенды), в git не попадают."""
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "testdata"

JPEGS = {                      # имя -> (зерно сцены, время съёмки)
    "a6300_DSC00266.JPG": (1, "2026:09:29 23:59:10"),
    "a6300_DSC00269.JPG": (2, "2026:09:30 00:11:03"),
    "a6300_DSC00270.JPG": (3, "2026:09:30 00:11:41"),
}
DNGS = {"TEST0001.dng": "2026:10:01 12:34:56", "TEST0002.dng": "2026:10:01 12:35:56", "TEST0003.dng": "2026:10:01 12:36:56"}


def scene(seed, w=3000, h=2000):
    """Тёмная улица: градиент неба, силуэты, несколько ярких огней (для халяции) и немного шума."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    sky = np.stack([0.10 + 0.10 * (1 - y / h), 0.12 + 0.12 * (1 - y / h), 0.22 + 0.20 * (1 - y / h)], axis=-1)
    img = sky * (0.6 + 0.4 * np.sin(x / w * np.pi))[..., None]
    for _ in range(14):                                     # дома
        x0, x1 = sorted(rng.integers(0, w, 2))
        top = rng.integers(int(h * 0.35), int(h * 0.7))
        img[top:, x0:x0 + max(80, (x1 - x0) // 3)] *= 0.25
    for _ in range(9):                                      # огни: мягкие яркие пятна
        cx, cy = rng.integers(0, w), rng.integers(int(h * 0.3), int(h * 0.8))
        r = rng.integers(20, 60)
        d = ((x - cx) ** 2 + (y - cy) ** 2) / (r * r)
        img += np.exp(-d)[..., None] * rng.uniform(0.6, 1.4, 3).astype(np.float32) * np.array([1.0, 0.85, 0.6], np.float32)
    img += rng.normal(0, 0.012, img.shape).astype(np.float32)
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))


def make_jpeg(path, seed, taken):
    im = scene(seed).resize((6000, 4000), Image.BICUBIC)
    ex = Image.Exif()
    ex[271], ex[272], ex[306] = "SONY", "ILCE-6300", taken
    ex[0x8769] = {36867: taken, 36868: taken, 34855: 3200, 33434: 0.016666, 33437: 5.6}   # время съёмки, ISO, выдержка, диафрагма
    im.save(path, "JPEG", quality=88, exif=ex)


def ensure(td=TESTDATA):
    td.mkdir(exist_ok=True)
    for name, (seed, taken) in JPEGS.items():
        if not (td / name).exists():
            make_jpeg(td / name, seed, taken)
    for name, taken in DNGS.items():
        if not (td / name).exists():
            try:
                subprocess.run([sys.executable, str(HERE / "make_dng.py"), str(td / name), taken], check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                pass                                        # нет tifffile — тесты RAW сами сообщат об этом
    return td


if __name__ == "__main__":
    print(ensure())
