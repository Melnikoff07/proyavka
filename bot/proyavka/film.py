"""Движок плёнок: параметры и встроенные плёнки, цвет (кривые, полосы, смешивание), свечение, зерно, виньетка, свои LUT и свои плёнки (проверка параметров, коды обмена)."""
from PIL import Image
from PIL import ImageChops
from PIL import ImageFilter
from pathlib import Path
import base64
import json
import math
import numpy as np
import os
import re
import threading

from .config import BASE
from .config import CHAT_ID
from .config import LUMA
from .i18n import L

# ================= плёнки =================
DEFAULTS = dict(
    contrast=0.35, lift=(0.03, 0.03, 0.03), top=0.975, shoulder=0.78, sat=0.9,
    gamma=(1.0, 1.0, 1.0), shadow_tint=(0, 0, 0), high_tint=(0, 0, 0),
    halation=0.3, hal_thr=0.72, bloom=0.08, soften=0.5,
    grain=0.04, grain_size=1.6, grain_color=0.3, vignette=0.2, bw=None,
    mix=(0.0,) * 6,                        # перетекание каналов: R←G, R←B, G←R, G←B, B←R, B←G (диагональ подбирается: серое остаётся серым)
    hue=(0.0,) * 6, bsat=(1.0,) * 6,       # сдвиг оттенка (°) и множитель насыщенности по полосам R, Y, G, C, B, M
    grain_shadow=0.0,                      # насколько крупнее и заметнее зерно в тенях (0 — как раньше)
    linear=0.0,                            # 1 — свечение и дымка считаются в линейном свете (физичнее), 0 — как раньше
)

PRESETS = {
    "street_neg": dict(
        name="Street Neg", when=L("улица, город", "street, city"),
        desc=L("жёсткие тени с бирюзой, тёплые света, приглушённый цвет", "hard teal shadows, warm highlights, muted colour"),
        contrast=0.5, lift=(0.02, 0.035, 0.04), sat=0.8,
        shadow_tint=(-0.03, 0.01, 0.02), high_tint=(0.035, 0.005, -0.01),
        halation=0.35, grain=0.045,
        hue=(5, 0, -8, -10, -5, 0), bsat=(1.0, 0.95, 0.85, 1.08, 0.88, 1.0), grain_shadow=0.35),
    "muted_chrome": dict(
        name="Muted Chrome", when=L("пасмурно, документалка", "overcast, documentary"),
        desc=L("сдержанный цвет, плотные тени, приглушённое небо", "restrained colour, dense shadows, muted sky"),
        contrast=0.45, lift=(0.025, 0.025, 0.03), sat=0.7, gamma=(1.0, 1.0, 1.05),
        shadow_tint=(-0.01, 0.0, 0.01), high_tint=(0.015, 0.01, -0.005),
        halation=0.25, grain=0.04,
        hue=(0, 0, -12, 0, -4, 0), bsat=(0.9, 0.88, 0.72, 0.88, 0.8, 0.92), grain_shadow=0.2),
    "amber_neg": dict(
        name="Amber Neg", when=L("золотой час, закат", "golden hour, sunset"),
        desc=L("янтарные света, мягкие тени, тёплая ностальгия", "amber highlights, soft shadows, warm nostalgia"),
        contrast=0.3, lift=(0.05, 0.035, 0.02), sat=0.95,
        shadow_tint=(0.01, 0.0, -0.01), high_tint=(0.05, 0.025, -0.04),
        halation=0.45, bloom=0.14, grain=0.045,
        hue=(6, 4, -14, 0, 4, 0), bsat=(1.08, 1.12, 0.9, 0.9, 0.8, 1.0), grain_shadow=0.3),
    "vivid50": dict(
        name="Vivid 50", when=L("пейзаж, природа", "landscape, nature"),
        desc=L("сочный цвет, глубокое небо, яркая зелень", "rich colour, deep sky, vivid greens"),
        contrast=0.55, lift=(0.015, 0.015, 0.02), sat=1.35, gamma=(1.0, 0.97, 0.98),
        shadow_tint=(0.0, 0.0, 0.01), high_tint=(0.01, 0.005, -0.01),
        halation=0.2, bloom=0.05, grain=0.025, grain_size=1.3, vignette=0.25,
        hue=(-3, 0, 6, 0, 0, -3), bsat=(1.15, 1.08, 1.12, 1.05, 1.18, 1.12), grain_shadow=0.1),
    "cine250": dict(
        name="Cine 250D", when=L("кино, настроение", "cinema, mood"),
        desc=L("плоский кинолук, мало цвета, бирюзовый оттенок", "flat cine look, low colour, teal cast"),
        contrast=0.2, lift=(0.04, 0.045, 0.05), shoulder=0.7, sat=0.65,
        shadow_tint=(-0.02, 0.01, 0.02), high_tint=(0.02, 0.012, -0.005),
        halation=0.35, bloom=0.12, grain=0.035,
        hue=(0, -3, 10, 5, -5, 0), bsat=(0.88, 0.82, 0.88, 1.0, 0.93, 0.88), grain_shadow=0.25),
    "portrait400": dict(
        name="Portrait 400", when=L("люди, портреты", "people, portraits"),
        desc=L("тёплая кожа, мягкий контраст, пастель", "warm skin, soft contrast, pastel"),
        contrast=0.3, lift=(0.04, 0.035, 0.035), sat=0.88,
        shadow_tint=(-0.01, 0.005, 0.02), high_tint=(0.03, 0.012, -0.02),
        halation=0.35, bloom=0.1, grain=0.04,
        hue=(9, 4, -10, -5, -9, 0), bsat=(0.95, 0.95, 0.72, 0.88, 0.78, 0.95), grain_shadow=0.2),
    "golden200": dict(
        name="Golden 200", when=L("солнце, лето", "sun, summer"),
        desc=L("тёплый, насыщенный, отпускной", "warm, saturated, holiday feel"),
        contrast=0.4, lift=(0.035, 0.03, 0.02), sat=1.1,
        shadow_tint=(0.005, 0.0, -0.015), high_tint=(0.045, 0.022, -0.04),
        halation=0.35, grain=0.045,
        hue=(6, 5, -12, 0, 2, 0), bsat=(1.1, 1.15, 0.95, 0.9, 0.85, 1.0), grain_shadow=0.3),
    "super400": dict(
        name="Super 400", when=L("повседневка, нулевые", "everyday, 2000s"),
        desc=L("зеленоватые тени, бодрый цвет мыльницы", "greenish shadows, punchy point-and-shoot colour"),
        contrast=0.45, lift=(0.02, 0.035, 0.03), sat=1.05, gamma=(1.0, 0.96, 1.0),
        shadow_tint=(-0.015, 0.02, 0.01), high_tint=(0.02, 0.01, -0.01),
        halation=0.35, grain=0.05,
        hue=(0, -3, 5, 3, 2, -4), bsat=(1.05, 1.0, 1.15, 1.1, 1.05, 1.0), grain_shadow=0.35),
    "night800": dict(
        name="Night 800T", when=L("ночь, огни", "night, lights"),
        desc=L("холодные тени, сильное красное свечение вокруг огней", "cold shadows, strong red glow around lights"),
        contrast=0.45, lift=(0.02, 0.03, 0.05), sat=0.95,
        shadow_tint=(-0.025, 0.01, 0.045), high_tint=(0.02, 0.0, -0.005),
        halation=0.9, hal_thr=0.62, bloom=0.14, grain=0.055, grain_size=1.8, vignette=0.25,
        hue=(0, 0, 0, -10, -14, 0), bsat=(1.1, 1.0, 0.9, 1.15, 1.05, 1.0), grain_shadow=0.5, linear=1.0),
    "across100": dict(
        name="Across 100", when=L("ч/б, мягко", "b&w, soft"),
        desc=L("гладкая ч/б, тонкое зерно, богатые полутона", "smooth b&w, fine grain, rich midtones"),
        bw=(0.25, 0.6, 0.15), contrast=0.45, lift=(0.02, 0.02, 0.02),
        halation=0.15, grain=0.035, grain_size=1.3, grain_color=0.0, vignette=0.2,
        grain_shadow=0.15),
    "grainx400": dict(
        name="Grain X 400", when=L("ч/б, улица, жёстко", "b&w, street, gritty"),
        desc=L("контрастная ч/б с крупным зерном", "contrasty b&w with coarse grain"),
        bw=(0.45, 0.45, 0.10), contrast=0.7, lift=(0.025, 0.025, 0.025),
        halation=0.15, grain=0.08, grain_size=2.0, grain_color=0.0, vignette=0.28,
        grain_shadow=0.45),
    "expired": dict(
        name="Expired", when=L("эксперимент", "experiment"),
        desc=L("выцветший цвет, сдвиг оттенков, много зерна", "faded colour, shifted hues, lots of grain"),
        contrast=0.25, lift=(0.07, 0.05, 0.06), top=0.93, sat=0.75, gamma=(0.95, 1.02, 1.05),
        shadow_tint=(0.02, -0.01, 0.03), high_tint=(0.04, 0.03, -0.03),
        halation=0.45, bloom=0.14, grain=0.07, grain_size=2.0, grain_color=0.5, vignette=0.3,
        mix=(0.05, 0.09, -0.03, -0.07, 0.06, 0.03), hue=(0, 0, -22, 8, 16, 0), bsat=(1.0, 1.0, 0.9, 0.9, 1.15, 1.2), grain_shadow=0.6),
}
for k in PRESETS:
    PRESETS[k] = {**DEFAULTS, **PRESETS[k]}

# ключи до v5 — ещё живут в базе, state.json и кнопках уже отправленных сообщений
OLD_KEYS = {
    "classic_neg": "street_neg", "chrome": "muted_chrome", "nostalgic": "amber_neg", "velvia": "vivid50",
    "eterna": "cine250", "portra": "portrait400", "gold": "golden200", "superia": "super400",
    "cinestill": "night800", "acros": "across100", "trix": "grainx400",
}


def canon(key):
    return OLD_KEYS.get(key, key)


def pname(key, owner=None):
    if key == "original":
        return L("Оригинал", "Original")
    if is_lut(key):
        return LUT_NAMES.get(key) or lut_meta(owner, key).get("name") or "LUT"
    return PRESETS[key]["name"] if key in PRESETS else key


# ================= обработка =================
def v3(t):
    return np.array(t, dtype=np.float32)


def _box(a, r, axis):
    a = np.moveaxis(a, axis, 0)
    p = np.concatenate([np.repeat(a[:1], r + 1, 0), a, np.repeat(a[-1:], r, 0)])
    c = np.cumsum(p, axis=0, dtype=np.float32)
    out = (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)
    return np.moveaxis(out, 0, axis)


def blur_mask(m, sigma):
    """Гауссово размытие во float (без потери слабого свечения), через уменьшенную копию."""
    h, w = m.shape
    k = max(1, int(sigma / 4))
    small = np.asarray(Image.fromarray(m.astype(np.float32)).resize((max(1, w // k), max(1, h // k)), Image.BOX),
                       dtype=np.float32)
    sig = sigma / k
    r = max(1, int(round((math.sqrt(12 * sig * sig / 3 + 1) - 1) / 2)))
    for _ in range(3):
        small = _box(small, r, 0)
        small = _box(small, r, 1)
    big = Image.fromarray(np.ascontiguousarray(small, dtype=np.float32)).resize((w, h), Image.BICUBIC)
    return np.clip(np.asarray(big, dtype=np.float32), 0, None)


def screen(a, b):
    return 1 - (1 - a) * (1 - b)


def grain_field(h, w, size, rng):
    gh, gw = max(1, int(h / size)), max(1, int(w / size))
    n = rng.standard_normal((gh, gw), dtype=np.float32)
    return np.asarray(Image.fromarray(n).resize((w, h), Image.BICUBIC), dtype=np.float32)


def band_adjust(a, hue, bsat):
    """Оттенок и насыщенность отдельно по шести цветовым полосам (красный, жёлтый, зелёный, голубой, синий, пурпурный).
    Именно такие сдвиги делают зелень «кодаковской», а кожу — «портровской»; серое они не трогают. a — (N,3), 0..1."""
    mx, mn = a.max(axis=1), a.min(axis=1)
    d = mx - mn
    safe = np.where(d == 0, 1.0, d)
    r, g, b = a[:, 0], a[:, 1], a[:, 2]
    h = np.where(mx == r, ((g - b) / safe) % 6, np.where(mx == g, (b - r) / safe + 2, (r - g) / safe + 4)) * 60.0
    h = np.where(d == 0, 0.0, h)
    s = np.where(mx == 0, 0.0, d / np.where(mx == 0, 1.0, mx))
    dist = np.abs(((h[:, None] - np.arange(6, dtype=np.float32)[None, :] * 60.0 + 180.0) % 360.0) - 180.0)
    w = np.clip(1.0 - dist / 60.0, 0.0, 1.0)                  # веса соседних полос плавно перетекают, в сумме дают 1
    dh = (w @ v3(hue)) * np.clip(s * 4.0, 0.0, 1.0)           # у почти серых оттенок неустойчив — сдвиг гасим
    h2 = (h + dh) % 360.0
    s2 = np.clip(s * (w @ v3(bsat)), 0.0, 1.0)
    c = mx * s2
    x = c * (1.0 - np.abs((h2 / 60.0) % 2.0 - 1.0))
    sector = (h2 // 60.0).astype(np.int64) % 6
    z = np.zeros_like(c)
    out = np.stack([np.choose(sector, [c, x, z, z, x, c]), np.choose(sector, [x, c, c, x, z, z]), np.choose(sector, [z, z, x, c, c, x])], axis=1)
    return out + (mx - c)[:, None]


def mix_matrix(v):
    """Матрица 3×3 из шести «перетеканий»; сумма каждой строки — 1, поэтому нейтральные тона не меняют цвет."""
    rg, rb, gr, gb, br, bg = v
    return np.array([[1 - rg - rb, rg, rb], [gr, 1 - gr - gb, gb], [br, bg, 1 - br - bg]], dtype=np.float32)


def color_fn(a, p):
    """Цвет плёнки для массива пикселей (N,3). Запекается в 3D-LUT."""
    if p["bw"]:
        g = a @ v3(p["bw"])
        a = np.repeat(g[:, None], 3, axis=1)
    else:
        if any(p["mix"]):                                            # без смешивания — ровно прежний результат
            a = np.clip(a @ mix_matrix(p["mix"]).T, 0, 1)
        l = a @ LUMA
        a = l[:, None] + (a - l[:, None]) * p["sat"]
        if any(p["hue"]) or any(x != 1.0 for x in p["bsat"]):        # без полос — ровно прежний результат
            a = band_adjust(np.clip(a, 0, 1), p["hue"], p["bsat"])
    a = np.clip(a, 0, 1) ** v3(p["gamma"])
    s = a * a * (3 - 2 * a)
    a = a + p["contrast"] * (s - a)
    t = p["shoulder"]
    over = np.clip(a - t, 0, None)
    a = a - 0.35 * over * over / (1 - t)
    l3 = a @ LUMA
    a = a + ((1 - l3) ** 2)[:, None] * v3(p["shadow_tint"]) + (l3 ** 2)[:, None] * v3(p["high_tint"])
    a = np.clip(a, 0, 1)
    lift = v3(p["lift"])
    return np.clip(lift + a * (p["top"] - lift), 0, 1)


LUT_CACHE = {}
LUT_LOCK = threading.Lock()


def _bake(p):
    n = 33
    x = np.linspace(0, 1, n, dtype=np.float32)
    b, g, r = np.meshgrid(x, x, x, indexing="ij")          # красный меняется быстрее всех
    rgb = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)
    out = color_fn(rgb, p).astype(np.float32)
    # numpy-таблица вместо списка: 0,4 МБ вместо 3,5 МБ на плёнку в каждом процессе, результат тот же
    return ImageFilter.Color3DLUT(n, np.ascontiguousarray(out.ravel()), channels=3)


PARAM_LUT_CACHE = {}      # параметры своей плёнки -> таблица; ограничен: ползунки редактора дают много вариантов


def preset_lut(key, p=None):
    """Цветовая таблица плёнки. p — параметры своей плёнки (у встроенных берутся из PRESETS)."""
    with LUT_LOCK:
        if p is not None:
            k = json.dumps(p, sort_keys=True)
            if k not in PARAM_LUT_CACHE:
                PARAM_LUT_CACHE[k] = _bake(p)
                while len(PARAM_LUT_CACHE) > 12:
                    PARAM_LUT_CACHE.pop(next(iter(PARAM_LUT_CACHE)))
            return PARAM_LUT_CACHE[k]
        if key not in LUT_CACHE:
            LUT_CACHE[key] = _bake(PRESETS[key])
        return LUT_CACHE[key]


def fx_layer(img, p):
    """Халяция + дымка одним слоем: считаем на копии ~700 px, растягиваем один раз."""
    if p["halation"] <= 0 and p["bloom"] <= 0:
        return None
    w, h = img.size
    f = max(1, int(max(w, h) / 700))
    sw, sh = max(1, w // f), max(1, h // f)
    acc = np.zeros((sh, sw, 3), dtype=np.float32)
    if p["halation"] > 0:
        thr = p["hal_thr"]
        lut = [max(0, min(255, int(round((i / 255 - thr) / (1 - thr) * 255)))) for i in range(256)]
        m = np.asarray(img.convert("L").point(lut).resize((sw, sh), Image.BOX), dtype=np.float32) / 255.0
        r = max(w, h) * 0.008 / f
        near = 1 - np.exp(-blur_mask(m, r) * 4)          # плотное кольцо у источника
        far = 1 - np.exp(-blur_mask(m, r * 5) * 20)      # широкое красное облако
        halo = (near * 0.5 + far * 0.5) * (1 - m * 0.7)  # сам источник не краснеет
        acc = halo[..., None] * (p["halation"] * v3((1.0, 0.22, 0.05)))
    if p["bloom"] > 0:
        small = img.resize((sw, sh), Image.BOX).filter(ImageFilter.GaussianBlur(max(w, h) * 0.012 / f))
        b = np.asarray(small, dtype=np.float32) / 255.0
        acc = 1 - (1 - acc) * (1 - b * b * p["bloom"])
    return Image.fromarray((np.clip(acc, 0, 1) * 255 + 0.5).astype(np.uint8)).resize((w, h), Image.BILINEAR)


def to_linear(a):
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def from_linear(a):
    a = np.clip(a, 0.0, 1.0)
    return np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)


def fx_linear(img, p):
    """То же свечение и дымка, но энергией в линейном свете: яркие огни дают мягкое красное облако пропорционально
    своей яркости, а не «потолку» sRGB. Считается на копии ~700 px; результат — float (h, w, 3) в линейных единицах."""
    if p["halation"] <= 0 and p["bloom"] <= 0:
        return None
    w, h = img.size
    f = max(1, int(max(w, h) / 700))
    sw, sh = max(1, w // f), max(1, h // f)
    sl = to_linear(np.asarray(img.resize((sw, sh), Image.BOX), dtype=np.float32) / 255.0)
    acc = np.zeros((sh, sw, 3), dtype=np.float32)
    if p["halation"] > 0:
        thr = float(to_linear(np.float32(p["hal_thr"])))
        m = np.clip(((sl @ LUMA) - thr) / (1 - thr), 0, 1)
        r = max(w, h) * 0.008 / f
        near = 1 - np.exp(-blur_mask(m, r) * 4)
        far = 1 - np.exp(-blur_mask(m, r * 5) * 20)
        halo = (near * 0.5 + far * 0.5) * (1 - m * 0.7)
        acc += halo[..., None] * (p["halation"] * 0.5 * v3((1.0, 0.22, 0.05)))
    if p["bloom"] > 0:
        sig = max(w, h) * 0.012 / f
        blur = np.stack([blur_mask(sl[..., c], sig) for c in range(3)], axis=-1)
        acc += blur * blur * (p["bloom"] * 0.6)
    return acc


def apply_fx_linear(img, acc, strip=256):
    """Прибавить свечение к кадру в линейном свете. Идём полосами по strip строк (256: пик памяти ~100 МБ на кадр 24 Мп): полный кадр во float — сотни мегабайт."""
    w, h = img.size
    sh, sw = acc.shape[:2]
    out = Image.new("RGB", (w, h))
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        box = (0, y0 * sh / h, sw, y1 * sh / h)
        fx = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(acc[..., c])).resize((w, y1 - y0), Image.BILINEAR, box=box),
                                  dtype=np.float32) for c in range(3)], axis=-1)
        lin = to_linear(np.asarray(img.crop((0, y0, w, y1)), dtype=np.float32) / 255.0) + fx
        out.paste(Image.fromarray((from_linear(lin) * 255 + 0.5).astype(np.uint8)), (0, y0))
    return out


GRAIN_GAIN = 1.75  # калибровка под прежний вид зерна


def grain_layer(w, h, p, k, rng):
    """Шум вокруг серого 128: накладывается «мягким светом» — сильнее в полутонах, как у плёнки."""
    scale = max(w, h) / 3000.0
    gs = max(1.0, p["grain_size"] * scale)
    amp = p["grain"] * min(k, 1.5) * 255 * GRAIN_GAIN
    chroma = 0 if p["bw"] else p["grain_color"] * 0.6

    def noise(size, weight):
        """Одноканальный шум, растянутый до кадра: втрое дешевле, чем растягивать цветной."""
        gh, gw = max(1, round(h / size)), max(1, round(w / size))
        n = rng.standard_normal((gh, gw), dtype=np.float32)
        n *= amp * weight
        n += 128
        np.clip(n, 0, 255, out=n)
        return Image.fromarray(n.astype(np.uint8)).resize((w, h), Image.BICUBIC)

    lum = ImageChops.add(noise(gs, 0.75), noise(gs * 2.5, 0.45), scale=1.0, offset=-128)   # мелкое + крупное зерно
    if chroma <= 0:
        return Image.merge("RGB", (lum, lum, lum))
    c = noise(gs, chroma)   # цветной шум: к красному прибавляется, из синего вычитается
    return Image.merge("RGB", (ImageChops.add(lum, c, scale=1.0, offset=-128), lum,
                               ImageChops.subtract(lum, c, scale=1.0, offset=128)))


def vignette_layer(w, h, strength):
    if w >= h:
        sw, sh = 320, max(1, round(320 * h / w))
    else:
        sw, sh = max(1, round(320 * w / h)), 320
    yy = np.linspace(-1, 1, sh, dtype=np.float32)[:, None]
    xx = np.linspace(-1, 1, sw, dtype=np.float32)[None, :]
    v = 1 - strength * ((xx * xx + yy * yy) / 2) ** 1.5
    L = Image.fromarray((np.clip(v, 0, 1) * 255 + 0.5).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    return Image.merge("RGB", (L, L, L))


# ================= свои LUT пользователей =================
# Файл .cube (3D LUT) загружается в чат или в «Проявку». Хранится в папке владельца (BASE/luts или
# BASE/users/<id>/luts) таблицей numpy, ключ плёнки — "lut<id>". Видит и применяет его только владелец.
LUT_RE = re.compile(r"lut(\d{1,9})")
LUT_MAX_COUNT = 30
LUT_MAX_BYTES = 16 * 1024 * 1024
LUT_NAMES = {}            # "lut<id>" -> название (основной процесс)
LUT_OWNER = {}            # "lut<id>" -> владелец
USER_LUT_CACHE = {}       # путь -> (mtime, Color3DLUT) — в процессах-работниках


def is_lut(key):
    return bool(key) and LUT_RE.fullmatch(str(key)) is not None


def lut_dir(owner):
    return (BASE if owner == CHAT_ID else BASE / "users" / str(owner)) / "luts"


def lut_meta(owner, key):
    try:
        return json.loads((lut_dir(owner) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def parse_cube(data):
    """3D LUT в формате .cube (Adobe/Resolve): размер N, затем N³ строк «r g b», красный меняется быстрее всех."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    size, dmin, dmax, rows = None, 0.0, 1.0, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split()[0].upper()
        if head == "LUT_1D_SIZE":
            raise ValueError(L("это одномерный LUT, нужен трёхмерный (.cube с LUT_3D_SIZE)",
                               "this is a 1D LUT, a 3D one is needed (.cube with LUT_3D_SIZE)"))
        if head == "LUT_3D_SIZE":
            size = int(line.split()[1])
        elif head == "DOMAIN_MAX":
            dmax = max(float(v) for v in line.split()[1:4])
        elif head == "DOMAIN_MIN":
            dmin = min(float(v) for v in line.split()[1:4])
        elif head[0].isalpha() or head.startswith('"'):
            continue                                # TITLE и прочие заголовки
        else:
            rows.append(line)
    if not size or not 2 <= size <= 65:
        raise ValueError(L("не похоже на 3D LUT .cube (нужен LUT_3D_SIZE от 2 до 65)",
                           "doesn't look like a 3D .cube LUT (LUT_3D_SIZE from 2 to 65 needed)"))
    try:
        t = np.array(" ".join(rows).split(), dtype=np.float32).reshape(-1, 3)
    except ValueError:
        raise ValueError(L("в файле LUT есть битые строки", "the LUT file has broken lines"))
    if t.shape[0] != size ** 3:
        raise ValueError(L(f"в LUT {t.shape[0]} строк вместо {size ** 3}", f"the LUT has {t.shape[0]} rows instead of {size ** 3}"))
    if dmax > 1.5:                                  # редкие LUT в целых числах (0–1023 и т. п.)
        t = (t - dmin) / (dmax - dmin)
    return size, np.clip(t, 0, 1).astype(np.float32)


def user_lut(owner, key):
    path = str(lut_dir(owner) / f"{key}.npy")
    mtime = os.path.getmtime(path)                  # нет файла — OSError, кадр рисуется без LUT
    with LUT_LOCK:
        hit = USER_LUT_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    t = np.load(path)
    n = round((t.size // 3) ** (1 / 3))
    f = ImageFilter.Color3DLUT(n, np.ascontiguousarray(t.ravel()), channels=3)
    with LUT_LOCK:
        USER_LUT_CACHE[path] = (mtime, f)
        while len(USER_LUT_CACHE) > 8:
            USER_LUT_CACHE.pop(next(iter(USER_LUT_CACHE)))
    return f


# ---- свои плёнки: те же параметры, что у встроенных (контраст, зерно, халяция…), но задаёт их человек ----
# Лежат рядом с LUT: строка в таблице luts (size=0) и файл lut<id>.json с параметрами. Видит и применяет владелец.
# Параметры — несколько чисел, поэтому плёнкой легко поделиться: текстовый код или каталог сообщества.
LOOK_FIELDS = {                    # поле -> (сколько чисел, минимум, максимум)
    "contrast": (1, 0.0, 1.0), "lift": (3, 0.0, 0.15), "top": (1, 0.8, 1.0), "shoulder": (1, 0.5, 0.95),
    "sat": (1, 0.0, 2.0), "gamma": (3, 0.7, 1.3), "shadow_tint": (3, -0.1, 0.1), "high_tint": (3, -0.1, 0.1),
    "halation": (1, 0.0, 1.2), "hal_thr": (1, 0.4, 0.95), "bloom": (1, 0.0, 0.3), "soften": (1, 0.0, 1.5),
    "grain": (1, 0.0, 0.12), "grain_size": (1, 1.0, 3.0), "grain_color": (1, 0.0, 1.0), "vignette": (1, 0.0, 0.5),
    "mix": (6, -0.5, 0.5), "hue": (6, -40.0, 40.0), "bsat": (6, 0.4, 1.8), "grain_shadow": (1, 0.0, 1.0), "linear": (1, 0.0, 1.0),
}
LOOK_CODE_PREFIX = "proyavka-look:1:"
LOOK_META_CACHE = {}               # путь -> (mtime, параметры или None) — в процессах-работниках


def clean_params(d):
    """Параметры плёнки из чужих рук (редактор, код, каталог): только известные поля, числа в допустимых пределах."""
    if not isinstance(d, dict):
        raise ValueError(L("нет параметров плёнки", "no film parameters"))
    out = {}
    for k, (n, lo, hi) in LOOK_FIELDS.items():
        base = DEFAULTS[k]
        v = d.get(k, base)
        try:
            if n == 1:
                vals = float(v)
                if not math.isfinite(vals):
                    raise ValueError
                out[k] = round(min(hi, max(lo, vals)), 4)
            else:
                if not isinstance(v, (list, tuple)) or len(v) != n:
                    raise ValueError
                if not all(math.isfinite(float(x)) for x in v):
                    raise ValueError
                out[k] = tuple(round(min(hi, max(lo, float(x))), 4) for x in v)
        except (TypeError, ValueError):
            raise ValueError(L(f"неверное значение «{k}»", f"invalid value for \"{k}\""))
    bw = d.get("bw")
    if bw:
        try:
            if len(bw) != 3 or not all(math.isfinite(float(x)) for x in bw):
                raise ValueError
            w = [min(1.0, max(0.0, float(x))) for x in bw]
            s = sum(w) or 1.0
            out["bw"] = tuple(round(x / s, 4) for x in w)      # веса каналов в сумме дают 1
        except (TypeError, ValueError):
            raise ValueError(L("неверные веса ч/б", "invalid b&w weights"))
    else:
        out["bw"] = None
    return out


def params_json(p):
    """Параметры в JSON (кортежи — списки). Одинаковые параметры дают одинаковую строку."""
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()}


def look_params(owner, key):
    """Параметры своей плёнки или None, если это обычный LUT. Читается в процессах-работниках: без базы, по файлу."""
    path = str(lut_dir(owner) / f"{key}.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with LUT_LOCK:
        hit = LOOK_META_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8")).get("params")
        p = clean_params(raw) if raw else None
    except (OSError, ValueError, AttributeError):
        p = None
    with LUT_LOCK:
        LOOK_META_CACHE[path] = (mtime, p)
        while len(LOOK_META_CACHE) > 64:
            LOOK_META_CACHE.pop(next(iter(LOOK_META_CACHE)))
    return p


def clean_text(s, n):
    return re.sub(r"[\x00-\x1f\x7f<>]", "", str(s or "")).strip()[:n]


def look_code(name, author, p):
    """Текст, которым можно поделиться где угодно: плёнка целиком в одной строке."""
    defaults = params_json(clean_params({}))
    blob = json.dumps({"name": name, "by": author, "p": {k: v for k, v in params_json(p).items() if v != defaults[k]}},
                      ensure_ascii=False, separators=(",", ":"))      # поля по умолчанию не пишем: код короче, а у старых серверов он тот же
    return LOOK_CODE_PREFIX + base64.urlsafe_b64encode(blob.encode("utf-8")).decode().rstrip("=")


def parse_look_code(code):
    """Код (или сырой JSON) -> (название, автор, параметры). Всё проверяется: код приходит от кого угодно."""
    code = str(code or "").strip()
    try:
        if code.startswith(LOOK_CODE_PREFIX):
            b = code[len(LOOK_CODE_PREFIX):]
            data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8"))
        else:
            data = json.loads(code)
        return clean_text(data.get("name"), 32) or "Look", clean_text(data.get("by"), 40), clean_params(data.get("p"))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(L("это не код плёнки Проявки", "this is not a Proyavka film code"))


def look(img, ph, key, strength, seed):
    """Плёнка, свой LUT или оригинал. LUT — только цвет; сила смешивает его с исходником (больше 100% — усиливает)."""
    if key == "original":
        return img
    if is_lut(key):
        p = look_params(ph.get("owner"), key)
        if p is not None:                           # своя плёнка из редактора или сообщества — те же эффекты, что у встроенных
            return film(img, key, strength, seed, p)
        try:
            lut = user_lut(ph.get("owner"), key)
        except OSError:
            return img
        a = img.filter(lut)
        k = strength / 100.0
        return a if k == 1 else Image.blend(img, a, k)
    return film(img, key, strength, seed)


def film(img, key, strength=100, seed=0, p=None):
    pr = p
    p = pr or PRESETS[key]
    rng = np.random.default_rng(seed)
    w, h = img.size
    soft = p["soften"] * max(w, h) / 3000.0
    if soft >= 0.6:  # убираем цифровую «звонкость»; на малых размерах эффект невидим
        img = img.filter(ImageFilter.GaussianBlur(soft))
    orig = img
    if p["linear"] > 0.5:
        acc = fx_linear(img, p)
        a = apply_fx_linear(img, acc) if acc is not None else img
    else:
        fx = fx_layer(img, p)
        a = ImageChops.screen(img, fx) if fx is not None else img
    a = a.filter(preset_lut(key, pr))
    k = strength / 100.0
    if k != 1:
        a = Image.blend(orig, a, k)
    if p["grain"] > 0:
        g = grain_layer(w, h, p, k, rng)
        a = ImageChops.soft_light(a, g)
        if p["grain_shadow"] > 0:       # у негатива в тенях зерно крупнее и заметнее: второй проход тем же шумом, только по теням
            gs = p["grain_shadow"]
            mask = a.convert("L").point([int(255 * min(1.0, gs * (1 - i / 255) ** 1.5)) for i in range(256)])
            a = ImageChops.soft_light(a, Image.composite(g, Image.new("RGB", (w, h), (128, 128, 128)), mask))
    if p["vignette"] > 0:
        a = ImageChops.multiply(a, vignette_layer(w, h, p["vignette"] * min(k, 1.5)))
    return a
