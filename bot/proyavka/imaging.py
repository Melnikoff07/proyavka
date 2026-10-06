"""Обработка кадра: открытие оригиналов (в том числе RAW), кадрирование, засветы, дата и рамка, листы-превью, автовыбор плёнки."""
from PIL import Image
from PIL import ImageChops
from PIL import ImageDraw
from PIL import ImageFilter
from PIL import ImageFont
from PIL import ImageOps
from datetime import datetime
from pathlib import Path
import hashlib
import math
import numpy as np
import os
import threading

from .config import FULL_EDGE
from .config import LUMA
from .config import RAW_EXTS
from .config import VIEW_EDGE
from .config import WORK_EDGE
from .film import PRESETS
from .film import film
from .film import look
from .film import pname
from .i18n import L
from .i18n import tr

# ================= засветы =================
FIRE = [(0.0, (0.55, 0.05, 0.02)), (0.35, (0.95, 0.25, 0.05)), (0.7, (1.0, 0.6, 0.15)), (1.0, (1.0, 0.92, 0.7))]
RED = [(0.0, (0.5, 0.02, 0.02)), (0.6, (1.0, 0.15, 0.05)), (1.0, (1.0, 0.55, 0.3))]
PINK = [(0.0, (0.45, 0.04, 0.2)), (0.6, (1.0, 0.3, 0.45)), (1.0, (1.0, 0.78, 0.82))]
AMBER = [(0.0, (0.6, 0.3, 0.1)), (1.0, (1.0, 0.7, 0.38))]
COLD = [(0.0, (0.02, 0.15, 0.35)), (0.6, (0.2, 0.6, 0.9)), (1.0, (0.8, 0.95, 1.0))]
RAINBOW = [(0.0, (1.0, 0.2, 0.1)), (0.2, (1.0, 0.55, 0.1)), (0.4, (1.0, 0.9, 0.3)), (0.6, (0.4, 0.9, 0.5)),
           (0.8, (0.3, 0.5, 1.0)), (1.0, (0.8, 0.3, 0.9))]

LEAKS = {
    "edge": (L("Край", "Edge"), L("тёплое свечение с края", "warm glow from the edge")),
    "leader": (L("Начало плёнки", "Film leader"), L("широкая засветка первых кадров", "wide flare of the first frames")),
    "burn": (L("Прожог", "Burn"), L("край выжжен почти до белого", "edge burnt almost to white")),
    "streak": (L("Полоса", "Streak"), L("свет через щель крышки", "light through a gap in the back")),
    "double": (L("Две полосы", "Double"), L("две полосы разной силы", "two streaks of different strength")),
    "slit": (L("Щель кассеты", "Cassette slit"), L("тонкая красная линия у края", "thin red line at the edge")),
    "corner": (L("Угол", "Corner"), L("горячее пятно из угла", "hot spot from a corner")),
    "orb": (L("Ореол", "Orb"), L("круглое тёплое пятно", "round warm spot")),
    "haze": (L("Янтарная дымка", "Amber haze"), L("тёплая вуаль на весь кадр", "warm veil over the whole frame")),
    "pink": (L("Малиновый", "Raspberry"), L("как на просроченной плёнке", "like expired film")),
    "rainbow": (L("Радуга", "Rainbow"), L("призматическая засветка", "prismatic flare")),
    "cold": (L("Холодный", "Cold"), L("редкий голубой засвет", "rare blue leak")),
}


def _ramp(t, stops):
    t = np.clip(t, 0, 1)
    pos = [p for p, _ in stops]
    return np.stack([np.interp(t, pos, [c[i] for _, c in stops]) for i in range(3)], axis=-1).astype(np.float32)


def _wobble(rng, sh, sw, amount):
    """Неровность, как у настоящего засвета: плавный шум 0.75..1.25."""
    n = rng.standard_normal((5, 7)).astype(np.float32)
    n = np.asarray(Image.fromarray(n).resize((sw, sh), Image.BICUBIC), dtype=np.float32)
    return np.clip(1 + n * amount, 0.3, 1.7)


def leak_layer(w, h, kind, seed):
    rng = np.random.default_rng(seed)
    f = max(1, int(max(w, h) / 480))
    sw, sh = max(1, w // f), max(1, h // f)
    yy = np.linspace(0, 1, sh, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, sw, dtype=np.float32)[None, :]
    ax, ay = w / max(w, h), h / max(w, h)          # чтобы круги были круглыми
    side = int(rng.integers(0, 4))                # 0 лево, 1 право, 2 верх, 3 низ
    d = [xx, 1 - xx, yy, 1 - yy][side] + 0 * yy + 0 * xx       # расстояние от края
    along = [yy, yy, xx, xx][side] + 0 * yy + 0 * xx           # координата вдоль края
    c = float(rng.uniform(0.25, 0.75))
    wob = _wobble(rng, sh, sw, 0.22)
    pal, gain = FIRE, 0.9

    if kind == "edge":
        m = 1.15 * np.exp(-(d / 0.26) ** 1.4) * np.exp(-((along - c) / 0.42) ** 2) * wob
        gain = 1.0
    elif kind == "leader":
        m = np.clip(1 - d / 0.8, 0, 1) ** 1.6 * (0.8 + 0.3 * wob)
        gain = 1.0
    elif kind == "burn":
        m = np.clip(1.3 - d / 0.3, 0, 1) ** 0.8 * (0.9 + 0.15 * wob)
        gain = 1.0
    elif kind in ("streak", "double"):
        vertical = rng.random() < 0.7
        x = xx + 0 * yy if vertical else yy + 0 * xx
        fade = (0.55 + 0.45 * np.exp(-(((yy if vertical else xx) + 0 * xx * yy) - rng.uniform(0.2, 0.8)) ** 2 / 0.18))
        m = (0.5 * np.exp(-((x - c) / 0.07) ** 2) + 0.4 * np.exp(-((x - c) / 0.2) ** 2)) * fade
        if kind == "double":
            c2 = c + rng.choice([-1, 1]) * rng.uniform(0.14, 0.24)
            m = m + 0.6 * (0.55 * np.exp(-((x - c2) / 0.05) ** 2) + 0.35 * np.exp(-((x - c2) / 0.15) ** 2)) * fade
        m = m * wob
        gain = 0.95
    elif kind == "slit":
        off = float(rng.uniform(0.05, 0.14))
        m = (np.exp(-((d - off) / 0.022) ** 2) * 0.95 + np.exp(-((d - off) / 0.11) ** 2) * 0.5) * (0.75 + 0.35 * wob)
        pal, gain = RED, 1.0
    elif kind == "corner":
        cx, cy = [(0, 0), (1, 0), (0, 1), (1, 1)][int(rng.integers(0, 4))]
        r = np.sqrt(((xx - cx) * ax) ** 2 + ((yy - cy) * ay) ** 2)
        m = np.exp(-(r / 0.42) ** 2) * wob
    elif kind == "orb":
        cx, cy = rng.uniform(0.2, 0.8), rng.uniform(0.15, 0.45)
        r = np.sqrt(((xx - cx) * ax) ** 2 + ((yy - cy) * ay) ** 2)
        m = np.exp(-(r / 0.2) ** 2) * 0.6 + np.exp(-(r / 0.45) ** 2) * 0.5
        m = m * (0.85 + 0.2 * wob)
        gain = 0.9
    elif kind == "haze":
        m = (0.35 + 0.65 * (1 - d) ** 2) * (0.9 + 0.2 * wob)
        pal, gain = AMBER, 0.7
    elif kind == "pink":
        m = np.exp(-(d / 0.35) ** 1.3) * np.exp(-((along - c) / 0.5) ** 2) * wob
        pal = PINK
    elif kind == "rainbow":
        band = np.exp(-(d / 0.3) ** 1.3) * (0.8 + 0.3 * wob)
        col = _ramp((along - c + 0.5) * 1.1, RAINBOW)
        leak = col * (band[..., None] * 0.75)
        return Image.fromarray((np.clip(leak, 0, 1) * 255 + 0.5).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    elif kind == "cold":
        m = 1.1 * np.exp(-(d / 0.3) ** 1.3) * np.exp(-((along - c) / 0.45) ** 2) * wob
        pal, gain = COLD, 0.9
    else:
        m = np.zeros((sh, sw), dtype=np.float32)
    m = np.clip(m, 0, 1.2).astype(np.float32)
    leak = _ramp(m, pal) * (np.clip(m, 0, 1)[..., None] * gain)
    return Image.fromarray((np.clip(leak, 0, 1) * 255 + 0.5).astype(np.uint8)).resize((w, h), Image.BILINEAR)


def leak_seed(ph):
    return int(ph["id"]) * 7 + int(ph.get("leak_seed") or 0) * 7919


def light_leak(img, kind="edge", seed=0):
    return ImageChops.screen(img, leak_layer(img.size[0], img.size[1], kind, seed))


FONT_FILES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def font(size):
    for path in FONT_FILES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def date_stamp(img, taken):
    try:
        dt = datetime.strptime(taken, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        dt = datetime.now()
    txt = f"'{dt:%y} {dt.month} {dt.day}"
    w, h = img.size
    f = font(int(max(w, h) * 0.03))
    x0, y0, x1, y1 = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), txt, font=f)
    pos = (w - (x1 - x0) - int(w * 0.05), h - (y1 - y0) - int(h * 0.06))
    g = max(1, int(max(w, h) * 0.004))
    m = g * 3 + 2
    box = (max(0, pos[0] + x0 - m), max(0, pos[1] + y0 - m), min(w, pos[0] + x1 + m), min(h, pos[1] + y1 + m))
    layer = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), (0, 0, 0))
    ImageDraw.Draw(layer).text((pos[0] - box[0], pos[1] - box[1]), txt, fill=(255, 135, 35), font=f)
    glow = layer.filter(ImageFilter.GaussianBlur(g))
    region = ImageChops.screen(ImageChops.screen(img.crop(box), glow), layer)
    out = img.copy()
    out.paste(region, box[:2])
    return out


def add_frame(img, number, label):
    w, h = img.size
    b = int(max(w, h) * 0.035)
    out = Image.new("RGB", (w + 2 * b, h + 2 * b + b // 2), (14, 13, 12))
    out.paste(img, (b, b))
    d = ImageDraw.Draw(out)
    f = font(max(10, int(b * 0.45)))
    col = (235, 150, 60)
    y = h + b + int(b * 0.35)
    d.text((b, y), f"{number}  >  {number}A", fill=col, font=f)
    x0, _, x1, _ = d.textbbox((0, 0), label.upper(), font=f)
    d.text((w + b - (x1 - x0), y), label.upper(), fill=col, font=f)
    return out


def has(path):
    return bool(path) and os.path.exists(path)


# Кадрирование: доли кадра "x,y,w,h" после поворота по EXIF (так кадр и виден). Применяется до плёнки,
# поэтому зерно, засвет, дата и рамка ложатся уже на кадрированное фото.
def parse_crop(v):
    if v in (None, "", False, []):
        return None
    if isinstance(v, str):
        v = v.split(",")
    try:
        x, y, w, h = (float(t) for t in v)
    except (TypeError, ValueError):
        raise ValueError(L("неверная рамка кадрирования", "invalid crop"))
    if not all(map(math.isfinite, (x, y, w, h))):
        raise ValueError(L("неверная рамка кадрирования", "invalid crop"))
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < 0.05 or h < 0.05:
        raise ValueError(L("слишком маленькая рамка", "crop is too small"))
    if x < 0.002 and y < 0.002 and w > 0.996 and h > 0.996:
        return None                              # весь кадр — значит без кадрирования
    return f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}"


def crop_img(img, crop):
    if not crop:
        return img
    x, y, w, h = map(float, crop.split(","))
    W, H = img.size
    return img.crop((round(x * W), round(y * H), round((x + w) * W), round((y + h) * H)))


def crop_tag(crop):
    return "_c" + hashlib.sha1(crop.encode()).hexdigest()[:8] if crop else ""


BASE_CACHE = {}           # (id, edge, crop) -> уменьшенный исходник
BASE_LOCK = threading.Lock()


def source_image(ph, mode):
    """mode: full — оригинал для «Файл», work — для чата, view — для «Проявки»."""
    crop = ph.get("crop")
    if mode == "full" and has(ph["src"]):
        img = crop_img(open_src(ph["src"], FULL_EDGE if FULL_EDGE and not crop else None), crop).convert("RGB")
        if FULL_EDGE:
            img.thumbnail((FULL_EDGE, FULL_EDGE), Image.LANCZOS)
        return img
    if not has(ph["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    if mode in ("full", "work"):
        img = crop_img(Image.open(ph["work"]), crop)
        if crop and mode == "work" and max(img.size) < WORK_EDGE * 0.75 and has(ph["src"]):
            # сильный кроп: из рабочей копии вышло бы мыльно — берём оригинал, декодируя его сразу уменьшенным
            x, y, w, h = map(float, crop.split(","))
            img = crop_img(open_src(ph["src"], int(WORK_EDGE / max(w, h))), crop).convert("RGB")
            img.thumbnail((WORK_EDGE, WORK_EDGE), Image.LANCZOS)
        return img.convert("RGB")
    key = (ph["id"], VIEW_EDGE, crop)
    with BASE_LOCK:
        img = BASE_CACHE.get(key)
    if img is None:
        img = crop_img(Image.open(ph["work"]), crop).convert("RGB")
        img.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
        with BASE_LOCK:
            BASE_CACHE[key] = img
            while len(BASE_CACHE) > 4:     # ~7 МБ на кадр в каждом процессе-работнике
                BASE_CACHE.pop(next(iter(BASE_CACHE)))
    return img


def render(ph, full=False, mode=None):
    mode = mode or ("full" if full else "work")
    img = source_image(ph, mode)
    key = ph["preset"]
    out = look(img, ph, key, ph["strength"], ph["id"])
    if ph["leak"]:
        out = light_leak(out, ph.get("leak_kind") or "edge", leak_seed(ph))
    if ph["stamp"]:
        out = date_stamp(out, ph["taken"])
    if ph["frame"]:
        out = add_frame(out, ph["id"], pname(key, ph.get("owner")))
    return out


def contact_sheet(ph):
    if not has(ph["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    base = crop_img(Image.open(ph["work"]), ph.get("crop")).convert("RGB")
    base.thumbnail((700, 700), Image.LANCZOS)
    tw, th = base.size
    keys = list(PRESETS)
    cols, lab, gap = 4, 44, 8
    rows = math.ceil(len(keys) / cols)
    sheet = Image.new("RGB", (cols * (tw + gap) + gap, rows * (th + lab + gap) + gap), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    f = font(26)
    for i, k in enumerate(keys):
        tile = film(base, k, ph["strength"], seed=ph["id"])
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + lab + gap)
        sheet.paste(tile, (x, y))
        d.text((x + 6, y + th + 8), f"{i + 1}. {PRESETS[k]['name']}", fill=(235, 235, 235), font=f)
    return sheet


def gallery_image(rows):
    cell, cols, gap = 360, 3, 6
    n = max(1, len(rows))
    r = math.ceil(n / cols)
    g = Image.new("RGB", (cols * (cell + gap) + gap, r * (cell + gap) + gap), (18, 18, 18))
    d = ImageDraw.Draw(g)
    f = font(34)
    for i, ph in enumerate(rows):
        try:
            t = ImageOps.fit(Image.open(ph["thumb"]).convert("RGB"), (cell, cell))
        except Exception:
            t = Image.new("RGB", (cell, cell), (60, 60, 60))
        x = gap + (i % cols) * (cell + gap)
        y = gap + (i // cols) * (cell + gap)
        g.paste(t, (x, y))
        label = f"#{ph['id']}"
        x0, y0, x1, y1 = d.textbbox((0, 0), label, font=f)
        d.rectangle((x, y, x + x1 - x0 + 16, y + y1 - y0 + 16), fill=(0, 0, 0))
        d.text((x + 8, y + 8 - y0), label, fill=(255, 170, 60), font=f)
    return g


# Причина автовыбора однозначно следует из плёнки — поэтому показывается по ключу, на языке того, кто читает
# (сам выбор считает процесс-работник, который не знает, чей это кадр).
AUTO_REASONS = {"night800": L("ночь", "night"), "amber_neg": L("тёплый свет", "warm light"),
                "muted_chrome": L("пасмурно", "overcast"), "vivid50": L("пейзаж", "landscape"),
                "street_neg": L("улица", "street")}


def auto_reason(ph):
    r = AUTO_REASONS.get(ph.get("auto_key"))
    return tr(r) if r else (ph.get("auto_reason") or "")


def auto_pick(img, iso, hour):
    s = img.copy()
    s.thumbnail((256, 256))
    a = np.asarray(s, dtype=np.float32) / 255.0
    lum = float((a @ LUMA).mean())
    sat = float((a.max(axis=2) - a.min(axis=2)).mean())
    r, g, b = (float(a[..., i].mean()) for i in range(3))
    sky = a[: a.shape[0] // 3]
    sky_blue = float(sky[..., 2].mean() - sky[..., 0].mean())
    late = hour is not None and (hour >= 21 or hour < 5)
    if (iso and iso >= 3200) or lum < 0.16 or (late and lum < 0.3):
        key = "night800"
    elif r - b > 0.10 and lum > 0.25:
        key = "amber_neg"
    elif sat < 0.12:
        key = "muted_chrome"
    elif sat > 0.25 and (g - (r + b) / 2 > 0.03 or sky_blue > 0.12):
        key = "vivid50"
    else:
        key = "street_neg"
    return key, tr(AUTO_REASONS[key])


def is_raw(path):
    return Path(str(path)).suffix.lower() in RAW_EXTS


def open_raw(path, edge=None):
    """RAW -> RGB через LibRaw. Если нужен размер edge, а половинный режим его даёт — проявляем вдвое быстрее."""
    import rawpy
    with rawpy.imread(str(path)) as r:
        sz = r.sizes
        if sz.raw_width * sz.raw_height > Image.MAX_IMAGE_PIXELS:
            raise ValueError(L("слишком большое изображение", "the image is too large"))
        half = bool(edge) and max(sz.width, sz.height) // 2 >= edge
        rgb = r.postprocess(use_camera_wb=True, half_size=half, output_bps=8)
    return Image.fromarray(rgb)


def raw_exif(path):
    """Дата и ISO из заголовка RAW. Большинство RAW (ARW, NEF, DNG, CR2…) внутри — TIFF: читаем каталоги тегов,
    не разбирая изображение (Pillow такие файлы целиком открыть не может)."""
    from PIL import TiffImagePlugin
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            order = {b"II": "little", b"MM": "big"}.get(head[:2])
            if not order:
                return None, None

            def ifd_at(off):
                d = TiffImagePlugin.ImageFileDirectory_v2((b"II*\x00" if order == "little" else b"MM\x00*") + head[4:8])
                f.seek(off)
                d.load(f)
                return d

            d0 = ifd_at(int.from_bytes(head[4:8], order))
            raw, iso = d0.get(306), None
            if 34665 in d0:                          # Exif-каталог: точное время съёмки и ISO
                ex = ifd_at(d0[34665])
                raw = ex.get(36867) or raw
                iso = ex.get(34855)
                iso = iso[0] if isinstance(iso, (tuple, list)) else iso
        taken = datetime.strptime(str(raw)[:16], "%Y:%m:%d %H:%M").strftime("%Y-%m-%d %H:%M") if raw else None
        return taken, int(iso) if iso else None
    except Exception:
        return None, None


def open_src(path, need=None):
    """Оригинал, повёрнутый как надо. need — нужная длинная сторона (JPEG декодируется сразу уменьшенным)."""
    if is_raw(path):
        return open_raw(path, need)
    im = Image.open(path)
    if need:
        im.draft("RGB", (need, need))
    return ImageOps.exif_transpose(im)
