"""Обработка кадра: открытие оригиналов (в том числе RAW), кадрирование, оверлеи (засветы и свои картинки), дата и рамка,
листы-превью, автовыбор плёнки."""

import hashlib
import io
import math
import numpy as np
import os
import re
import threading
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter, ImageFont, ImageOps
from datetime import datetime
from pathlib import Path

from .config import BASE, CHAT_ID, FULL_EDGE, LUMA, RAW_EXTS, VIEW_EDGE, WORK_EDGE
from .i18n import L, tr
from .film import PRESETS, film, fit, look, par_resize, pname


# ================= оверлеи: встроенные засветы =================
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
        return par_resize(Image.fromarray((np.clip(leak, 0, 1) * 255 + 0.5).astype(np.uint8)), (w, h), Image.BILINEAR)
    elif kind == "cold":
        m = 1.1 * np.exp(-(d / 0.3) ** 1.3) * np.exp(-((along - c) / 0.45) ** 2) * wob
        pal, gain = COLD, 0.9
    else:
        m = np.zeros((sh, sw), dtype=np.float32)
    m = np.clip(m, 0, 1.2).astype(np.float32)
    leak = _ramp(m, pal) * (np.clip(m, 0, 1)[..., None] * gain)
    return par_resize(Image.fromarray((np.clip(leak, 0, 1) * 255 + 0.5).astype(np.uint8)), (w, h), Image.BILINEAR)


def leak_seed(ph):
    return int(ph["id"]) * 7 + int(ph.get("leak_seed") or 0) * 7919


def light_leak(img, kind="edge", seed=0):
    return ImageChops.screen(img, leak_layer(img.size[0], img.size[1], kind, seed))


# ================= оверлеи: свои картинки, режим наложения, прозрачность =================
# Оверлей кадра — встроенный засвет (ключ из LEAKS) или своя картинка пользователя ("ov<id>": засвет, пыль, текстура бумаги).
# Ложится режимом наложения с прозрачностью. У встроенных засветов чёрный фон: в «Экране», «Добавлении» и «Замене светлым»
# он ничего не меняет, а в остальных режимах маской служит яркость самого засвета — иначе чёрный фон залил бы кадр.
BLENDS = {
    "screen": L("Экран", "Screen"),
    "add": L("Добавление", "Add"),
    "lighten": L("Светлее", "Lighten"),
    "overlay": L("Перекрытие", "Overlay"),
    "soft": L("Мягкий свет", "Soft light"),
    "multiply": L("Умножение", "Multiply"),
    "normal": L("Обычный", "Normal"),
}
BLACK_NEUTRAL = {"screen", "add", "lighten"}
OPACITIES = range(5, 101, 5)        # прозрачность шагом 5 %: варианты превью в кэше не плодятся без меры
OV_RE = re.compile(r"ov[1-9][0-9]{0,8}")
OV_NAMES, OV_OWNER = {}, {}         # "ov<id>" -> название, владелец (заполняет users.load_overlays)
OV_MAX_COUNT = 20
OV_MAX_BYTES = 30 * 1024 * 1024
OV_EDGE = 2400                      # своя картинка хранится не больше этого: на кадр она всё равно растягивается
OV_CACHE = {}                       # путь -> ((mtime, size), Image) — в процессах-работниках
OV_LOCK = threading.Lock()


def is_overlay(key):
    return bool(key) and OV_RE.fullmatch(str(key)) is not None


def overlay_dir(owner):
    return (BASE if owner == CHAT_ID else BASE / "users" / str(owner)) / "overlays"


def overlay_file(owner, key):
    """Своя картинка: .png — с прозрачностью, .jpg — без неё. None — нет такой (удалена или чужая)."""
    if not is_overlay(key) or owner is None:
        return None
    for ext in (".png", ".jpg"):
        p = overlay_dir(owner) / f"{key}{ext}"
        if p.is_file():
            return p
    return None


def overlay_name(key):
    if key in LEAKS:
        return tr(LEAKS[key][0])
    return OV_NAMES.get(key) or (L("Свой оверлей", "Own overlay") if is_overlay(key) else "")


def save_overlay(data, d, key):
    """Загруженная картинка -> файл оверлея (уменьшенная до OV_EDGE). Возвращает (путь, ширина, высота)."""
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w * h > 120_000_000:
            raise ValueError("too big")
        im.load()
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        raise ValueError(L("не получилось открыть картинку — нужен JPEG, PNG или WebP", "could not open the image — JPEG, PNG or WebP needed"))
    if min(im.size) < 16:
        raise ValueError(L("картинка слишком маленькая", "the image is too small"))
    im = ImageOps.exif_transpose(im)
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if alpha else "RGB")
    if alpha and im.getchannel("A").getextrema()[0] == 255:     # прозрачности на деле нет — хранить как JPEG
        im, alpha = im.convert("RGB"), False
    im.thumbnail((OV_EDGE, OV_EDGE), Image.LANCZOS)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{key}{'.png' if alpha else '.jpg'}"
    tmp = d / f"{key}.tmp{path.suffix}"
    if alpha:
        im.save(tmp, "PNG", optimize=False, compress_level=6)
    else:
        im.save(tmp, "JPEG", quality=93)
    os.replace(tmp, path)
    return path, im.size[0], im.size[1]


def _overlay_image(path):
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    with OV_LOCK:
        hit = OV_CACHE.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
    im = Image.open(path)
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "PA", "P") and path.suffix == ".png" else "RGB")
    with OV_LOCK:
        OV_CACHE[str(path)] = (sig, im)
        while len(OV_CACHE) > 4:
            OV_CACHE.pop(next(iter(OV_CACHE)))
    return im


def overlay_layer(path, w, h, variant=0):
    """Своя картинка на кадр: повёрнута под ориентацию кадра, растянута с обрезкой по центру (как «заполнить»);
    «Другой вариант» (variant) отражает её по-разному, начиная с картинки как есть."""
    im = _overlay_image(path)
    if im.size[0] != im.size[1] and (im.size[0] > im.size[1]) != (w > h) and w != h:
        im = im.transpose(Image.Transpose.ROTATE_90)
    v = variant % 4
    if v == 1:
        im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    elif v == 2:
        im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    elif v == 3:
        im = im.transpose(Image.Transpose.ROTATE_180)
    return ImageOps.fit(im, (w, h), Image.BILINEAR)


def _blend(img, layer, mode):
    if mode == "add":
        return ImageChops.add(img, layer)
    if mode == "lighten":
        return ImageChops.lighter(img, layer)
    if mode == "multiply":
        return ImageChops.multiply(img, layer)
    if mode == "overlay":
        return ImageChops.overlay(img, layer)
    if mode == "soft":
        return ImageChops.soft_light(img, layer)
    if mode == "normal":
        return layer
    return ImageChops.screen(img, layer)


def apply_overlay(img, kind, seed=0, blend="screen", opacity=100, owner=None, variant=0):
    """Наложить оверлей на кадр. seed — форма встроенного засвета, variant — отражение своей картинки.
    Своей картинки уже нет (удалили) — кадр как есть."""
    blend = blend if blend in BLENDS else "screen"
    opacity = max(0, min(100, int(opacity if opacity is not None else 100)))
    if not opacity:
        return img
    w, h = img.size
    mask = None
    if is_overlay(kind):
        path = overlay_file(owner, kind)
        if not path:
            return img
        layer = overlay_layer(path, w, h, variant)
        if layer.mode == "RGBA":
            layer, mask = layer.convert("RGB"), layer.getchannel("A")
    else:
        layer = leak_layer(w, h, kind, seed)
        if blend == "normal":
            # засвет уже домножен на свою яркость: «обычное» наложение — сам свет поверх кадра, притушенного под ним
            r, g, b = layer.split()
            a = ImageChops.lighter(ImageChops.lighter(r, g), b)
            out = ImageChops.add(layer, ImageChops.multiply(img, ImageOps.invert(a).convert("RGB")))
            return out if opacity >= 100 else Image.blend(img, out, opacity / 100)
        if blend not in BLACK_NEUTRAL:
            r, g, b = layer.split()
            mask = ImageChops.lighter(ImageChops.lighter(r, g), b)
    out = _blend(img, layer, blend)
    if mask is not None:
        if opacity < 100:
            mask = mask.point([v * opacity // 100 for v in range(256)])
        return Image.composite(out, img, mask)
    return out if opacity >= 100 else Image.blend(img, out, opacity / 100)


def frame_overlay(img, ph, kind=None):
    """Оверлей кадра по его настройкам (kind — другой оверлей для превью ленты)."""
    return apply_overlay(img, kind or ph.get("leak_kind") or "edge", leak_seed(ph), ph.get("ov_blend") or "screen",
                         ph.get("ov_opacity") if ph.get("ov_opacity") is not None else 100, ph.get("owner"),
                         int(ph.get("leak_seed") or 0))


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
# поэтому зерно, оверлей, дата и рамка ложатся уже на кадрированное фото.
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
        return fit(img, FULL_EDGE) if FULL_EDGE else img
    if not has(ph["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    if mode in ("full", "work"):
        img = crop_img(Image.open(ph["work"]), crop)
        if crop and mode == "work" and max(img.size) < WORK_EDGE * 0.75 and has(ph["src"]):
            # сильный кроп: из рабочей копии вышло бы мыльно — берём оригинал, декодируя его сразу уменьшенным
            x, y, w, h = map(float, crop.split(","))
            img = fit(crop_img(open_src(ph["src"], int(WORK_EDGE / max(w, h))), crop).convert("RGB"), WORK_EDGE)
        return img.convert("RGB")
    key = (ph["id"], VIEW_EDGE, crop)
    with BASE_LOCK:
        img = BASE_CACHE.get(key)
    if img is None:
        img = fit(crop_img(Image.open(ph["work"]), crop).convert("RGB"), VIEW_EDGE)
        with BASE_LOCK:
            BASE_CACHE[key] = img
            while len(BASE_CACHE) > 4:     # ~7 МБ на кадр в каждом процессе-работнике
                BASE_CACHE.pop(next(iter(BASE_CACHE)))
    return img.copy()                  # «Оригинал» отдаёт этот же объект дальше, а job_view уменьшает его на месте — кэш портить нельзя


def render(ph, full=False, mode=None):
    mode = mode or ("full" if full else "work")
    img = source_image(ph, mode)
    key = ph["preset"]
    out = look(img, ph, key, ph["strength"], ph["id"], fit=mode == "view")
    if ph["leak"]:
        out = frame_overlay(out, ph)
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
        tile = film(base, k, ph["strength"], seed=ph["id"], fit=True)
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


SRGB_PROFILE = None


def srgb_profile():
    global SRGB_PROFILE
    if SRGB_PROFILE is None:
        SRGB_PROFILE = ImageCms.createProfile("sRGB")
    return SRGB_PROFILE


def srgb_icc():
    """Профиль sRGB для готовых файлов: просмотрщики не гадают, в каком пространстве цвета кадр."""
    return ImageCms.ImageCmsProfile(srgb_profile()).tobytes()


def to_srgb(im):
    """Кадр с встроенным профилем (Display P3 с iPhone, Adobe RGB) — в sRGB, иначе цвета потускнеют или уйдут в сторону.
    Без профиля или с sRGB возвращается как есть."""
    icc = im.info.get("icc_profile")
    if not icc or im.mode not in ("RGB", "RGBA"):
        return im
    try:
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if "srgb" in ImageCms.getProfileDescription(prof).lower():
            return im
        out = ImageCms.profileToProfile(im, prof, srgb_profile(), outputMode=im.mode)
    except Exception:
        return im
    out.info.pop("icc_profile", None)
    return out


def lost_rotation(path):
    """Кадр приняли с потерянным поворотом: у него поворот в EXIF и профиль не sRGB (Display P3 с iPhone), а в 1.6
    перевод в sRGB шёл раньше поворота и терял EXIF — вертикальный кадр ложился горизонтально."""
    try:
        with Image.open(path) as im:
            if im.getexif().get(0x0112) not in (2, 3, 4, 5, 6, 7, 8):
                return False
            icc = im.info.get("icc_profile")
            if not icc or im.mode not in ("RGB", "RGBA"):
                return False
            desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))
    except Exception:
        return False
    return "srgb" not in desc.lower()


def open_src(path, need=None):
    """Оригинал, повёрнутый как надо и в sRGB. need — нужная длинная сторона (JPEG декодируется сразу уменьшенным)."""
    if is_raw(path):
        return open_raw(path, need)
    im = Image.open(path)
    if need:
        im.draft("RGB", (need, need))
    return to_srgb(ImageOps.exif_transpose(im))   # сначала поворот: перевод в sRGB даёт новую картинку без EXIF


def read_exif(im):
    taken, iso = None, None
    try:
        ex = im.getexif()
        ifd = ex.get_ifd(0x8769)
        raw = ifd.get(36867) or ex.get(306)
        if raw:
            taken = datetime.strptime(str(raw)[:16], "%Y:%m:%d %H:%M").strftime("%Y-%m-%d %H:%M")
        iso_v = ifd.get(34855)
        if isinstance(iso_v, (tuple, list)):
            iso_v = iso_v[0]
        iso = int(iso_v) if iso_v else None
    except Exception:
        pass
    return taken, iso


def fingerprint(path):
    """Отпечаток кадра: хеш начала файла (там EXIF с точным временем съёмки) + размер."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read(262144))
    return f"{h.hexdigest()}:{os.path.getsize(path)}"
