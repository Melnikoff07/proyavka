#!/usr/bin/env python3
"""
filmbot v5 — камера → сервер-приёмник → домашний компьютер → плёночный лук → Telegram + Mini App «Проявка».
Настройки — переменные окружения (config.env), их пишет мастер setup.py.

Бот: кнопки под каждой фоткой (плёнка, сила, дата/рамка/засвет, сравнение, файл).
Mini App: лента-контактный лист по дням, просмотр со свайпами и живыми превью плёнок.
Хранилище чистится само: старые оригиналы и рабочие копии удаляются по лимитам.
"""
import collections
import hashlib
import hmac
import io
import itertools
import json
import logging
import math
import os
import queue
import shlex
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlparse
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

# ================= настройки =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
VPS = os.environ.get("VPS", "local")      # user@host сервера-приёмника; "local" — бот живёт на нём же
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/srv/camera/upload/")
SSH_KEY = os.path.expanduser(os.environ.get("SSH_KEY", "~/.ssh/id_vps"))
BASE = Path(os.environ.get("BASE_DIR", "~/filmbot")).expanduser()
POLL = int(os.environ.get("POLL_SECONDS", "8"))                   # опрос VPS, если мгновенные уведомления недоступны
POLL_BACKUP = int(os.environ.get("POLL_BACKUP_SECONDS", "60"))    # подстраховочный опрос при работающих уведомлениях
SETTLE = int(os.environ.get("SETTLE_SECONDS", "15"))
WORK_EDGE = int(os.environ.get("WORK_EDGE", "2560"))   # размер для чата (Telegram больше не показывает)
VIEW_EDGE = int(os.environ.get("VIEW_EDGE", "1600"))   # размер для просмотра в «Проявке»
FULL_EDGE = int(os.environ.get("FULL_EDGE", "0"))      # «Файл»: 0 = полный размер камеры

WEBAPP_URL = os.environ.get("WEBAPP_URL", "")          # https://1-2-3-4.sslip.io/
WEB_PORT = int(os.environ.get("WEB_PORT", "8088"))
ORIG_DAYS = float(os.environ.get("ORIGINALS_DAYS", "14"))   # сколько дней держать оригиналы
STORAGE_GB = float(os.environ.get("STORAGE_GB", "20"))      # общий лимит на фото
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "5"))     # сколько места оставлять на диске
FAST_WORKERS = int(os.environ.get("FAST_WORKERS", "3"))     # процессы для экрана и превью
WORKER_NICE = int(os.environ.get("WORKER_NICE", "10"))       # приоритет рендера ниже, чем у Pi-hole и бота
HEAVY_WORKERS = int(os.environ.get("HEAVY_WORKERS", "1"))   # процессы для экспорта в полный размер
LANG = "en" if os.environ.get("LANGUAGE", "ru").lower().startswith("en") else "ru"   # язык интерфейса: ru / en


def L(ru, en):
    """Строка интерфейса на выбранном языке."""
    return en if LANG == "en" else ru


APP_NAME = L("Проявка", "Proyavka")

INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP = (
    BASE / d for d in ("incoming", "originals", "work", "thumbs", "views", "previews", "tmp"))
for d in (INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP):
    d.mkdir(parents=True, exist_ok=True)
WEBAPP_HTML = Path(__file__).resolve().parent / "webapp.html"
DB_PATH = BASE / "filmbot.db"
STATE_FILE = BASE / "state.json"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
EXTS = {".jpg", ".jpeg", ".hif", ".heif", ".heic", ".png", ".webp"}   # png/webp — только свои фото из телефона
UPLOAD_MAX = int(float(os.environ.get("UPLOAD_MAX_MB", "50")) * 1024 * 1024)   # «+» в «Проявке»: предел одного файла
SSH_BIN = os.environ.get("SSH_BIN", "ssh")
# одно постоянное соединение на все запросы к VPS (ControlMaster), без нового рукопожатия каждый раз
SSH_CMD = [SSH_BIN, "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "ControlMaster=auto", "-o", f"ControlPath={BASE}/.ssh-%C", "-o", "ControlPersist=120",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
LOCAL = VPS in ("", "local")


def remote(cmd):
    """Команда на сервере-приёмнике: по SSH или прямо здесь, если бот живёт на том же сервере."""
    return ["sh", "-c", cmd] if LOCAL else SSH_CMD + [VPS, cmd]
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
STRENGTHS = [25, 50, 75, 100, 125, 150]
PAGE = 12

_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Сначала IPv4: у многих VPS IPv6 есть на бумаге, но пакеты уходят в никуда, и подключение висит минутами."""
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda r: r[0] != socket.AF_INET)


socket.getaddrinfo = _ipv4_first

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("filmbot")

# ================= плёнки =================
DEFAULTS = dict(
    contrast=0.35, lift=(0.03, 0.03, 0.03), top=0.975, shoulder=0.78, sat=0.9,
    gamma=(1.0, 1.0, 1.0), shadow_tint=(0, 0, 0), high_tint=(0, 0, 0),
    halation=0.3, hal_thr=0.72, bloom=0.08, soften=0.5,
    grain=0.04, grain_size=1.6, grain_color=0.3, vignette=0.2, bw=None,
)

PRESETS = {
    "street_neg": dict(
        name="Street Neg", when=L("улица, город", "street, city"),
        desc=L("жёсткие тени с бирюзой, тёплые света, приглушённый цвет", "hard teal shadows, warm highlights, muted colour"),
        contrast=0.5, lift=(0.02, 0.035, 0.04), sat=0.8,
        shadow_tint=(-0.03, 0.01, 0.02), high_tint=(0.035, 0.005, -0.01),
        halation=0.35, grain=0.045),
    "muted_chrome": dict(
        name="Muted Chrome", when=L("пасмурно, документалка", "overcast, documentary"),
        desc=L("сдержанный цвет, плотные тени, приглушённое небо", "restrained colour, dense shadows, muted sky"),
        contrast=0.45, lift=(0.025, 0.025, 0.03), sat=0.7, gamma=(1.0, 1.0, 1.05),
        shadow_tint=(-0.01, 0.0, 0.01), high_tint=(0.015, 0.01, -0.005),
        halation=0.25, grain=0.04),
    "amber_neg": dict(
        name="Amber Neg", when=L("золотой час, закат", "golden hour, sunset"),
        desc=L("янтарные света, мягкие тени, тёплая ностальгия", "amber highlights, soft shadows, warm nostalgia"),
        contrast=0.3, lift=(0.05, 0.035, 0.02), sat=0.95,
        shadow_tint=(0.01, 0.0, -0.01), high_tint=(0.05, 0.025, -0.04),
        halation=0.45, bloom=0.14, grain=0.045),
    "vivid50": dict(
        name="Vivid 50", when=L("пейзаж, природа", "landscape, nature"),
        desc=L("сочный цвет, глубокое небо, яркая зелень", "rich colour, deep sky, vivid greens"),
        contrast=0.55, lift=(0.015, 0.015, 0.02), sat=1.35, gamma=(1.0, 0.97, 0.98),
        shadow_tint=(0.0, 0.0, 0.01), high_tint=(0.01, 0.005, -0.01),
        halation=0.2, bloom=0.05, grain=0.025, grain_size=1.3, vignette=0.25),
    "cine250": dict(
        name="Cine 250D", when=L("кино, настроение", "cinema, mood"),
        desc=L("плоский кинолук, мало цвета, бирюзовый оттенок", "flat cine look, low colour, teal cast"),
        contrast=0.2, lift=(0.04, 0.045, 0.05), shoulder=0.7, sat=0.65,
        shadow_tint=(-0.02, 0.01, 0.02), high_tint=(0.02, 0.012, -0.005),
        halation=0.35, bloom=0.12, grain=0.035),
    "portrait400": dict(
        name="Portrait 400", when=L("люди, портреты", "people, portraits"),
        desc=L("тёплая кожа, мягкий контраст, пастель", "warm skin, soft contrast, pastel"),
        contrast=0.3, lift=(0.04, 0.035, 0.035), sat=0.88,
        shadow_tint=(-0.01, 0.005, 0.02), high_tint=(0.03, 0.012, -0.02),
        halation=0.35, bloom=0.1, grain=0.04),
    "golden200": dict(
        name="Golden 200", when=L("солнце, лето", "sun, summer"),
        desc=L("тёплый, насыщенный, отпускной", "warm, saturated, holiday feel"),
        contrast=0.4, lift=(0.035, 0.03, 0.02), sat=1.1,
        shadow_tint=(0.005, 0.0, -0.015), high_tint=(0.045, 0.022, -0.04),
        halation=0.35, grain=0.045),
    "super400": dict(
        name="Super 400", when=L("повседневка, нулевые", "everyday, 2000s"),
        desc=L("зеленоватые тени, бодрый цвет мыльницы", "greenish shadows, punchy point-and-shoot colour"),
        contrast=0.45, lift=(0.02, 0.035, 0.03), sat=1.05, gamma=(1.0, 0.96, 1.0),
        shadow_tint=(-0.015, 0.02, 0.01), high_tint=(0.02, 0.01, -0.01),
        halation=0.35, grain=0.05),
    "night800": dict(
        name="Night 800T", when=L("ночь, огни", "night, lights"),
        desc=L("холодные тени, сильное красное свечение вокруг огней", "cold shadows, strong red glow around lights"),
        contrast=0.45, lift=(0.02, 0.03, 0.05), sat=0.95,
        shadow_tint=(-0.025, 0.01, 0.045), high_tint=(0.02, 0.0, -0.005),
        halation=0.9, hal_thr=0.62, bloom=0.14, grain=0.055, grain_size=1.8, vignette=0.25),
    "across100": dict(
        name="Across 100", when=L("ч/б, мягко", "b&w, soft"),
        desc=L("гладкая ч/б, тонкое зерно, богатые полутона", "smooth b&w, fine grain, rich midtones"),
        bw=(0.25, 0.6, 0.15), contrast=0.45, lift=(0.02, 0.02, 0.02),
        halation=0.15, grain=0.035, grain_size=1.3, grain_color=0.0, vignette=0.2),
    "grainx400": dict(
        name="Grain X 400", when=L("ч/б, улица, жёстко", "b&w, street, gritty"),
        desc=L("контрастная ч/б с крупным зерном", "contrasty b&w with coarse grain"),
        bw=(0.45, 0.45, 0.10), contrast=0.7, lift=(0.025, 0.025, 0.025),
        halation=0.15, grain=0.08, grain_size=2.0, grain_color=0.0, vignette=0.28),
    "expired": dict(
        name="Expired", when=L("эксперимент", "experiment"),
        desc=L("выцветший цвет, сдвиг оттенков, много зерна", "faded colour, shifted hues, lots of grain"),
        contrast=0.25, lift=(0.07, 0.05, 0.06), top=0.93, sat=0.75, gamma=(0.95, 1.02, 1.05),
        shadow_tint=(0.02, -0.01, 0.03), high_tint=(0.04, 0.03, -0.03),
        halation=0.45, bloom=0.14, grain=0.07, grain_size=2.0, grain_color=0.5, vignette=0.3),
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


def pname(key):
    return L("Оригинал", "Original") if key == "original" else PRESETS[key]["name"]


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


def color_fn(a, p):
    """Цвет плёнки для массива пикселей (N,3). Запекается в 3D-LUT."""
    if p["bw"]:
        g = a @ v3(p["bw"])
        a = np.repeat(g[:, None], 3, axis=1)
    else:
        l = a @ LUMA
        a = l[:, None] + (a - l[:, None]) * p["sat"]
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


def preset_lut(key):
    with LUT_LOCK:
        if key not in LUT_CACHE:
            n = 33
            x = np.linspace(0, 1, n, dtype=np.float32)
            b, g, r = np.meshgrid(x, x, x, indexing="ij")          # красный меняется быстрее всех
            rgb = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)
            out = color_fn(rgb, PRESETS[key]).astype(np.float32)
            # numpy-таблица вместо списка: 0,4 МБ вместо 3,5 МБ на плёнку в каждом процессе, результат тот же
            LUT_CACHE[key] = ImageFilter.Color3DLUT(n, np.ascontiguousarray(out.ravel()), channels=3)
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


def film(img, key, strength=100, seed=0):
    p = PRESETS[key]
    rng = np.random.default_rng(seed)
    w, h = img.size
    soft = p["soften"] * max(w, h) / 3000.0
    if soft >= 0.6:  # убираем цифровую «звонкость»; на малых размерах эффект невидим
        img = img.filter(ImageFilter.GaussianBlur(soft))
    orig = img
    fx = fx_layer(img, p)
    a = ImageChops.screen(img, fx) if fx is not None else img
    a = a.filter(preset_lut(key))
    k = strength / 100.0
    if k != 1:
        a = Image.blend(orig, a, k)
    if p["grain"] > 0:
        a = ImageChops.soft_light(a, grain_layer(w, h, p, k, rng))
    if p["vignette"] > 0:
        a = ImageChops.multiply(a, vignette_layer(w, h, p["vignette"] * min(k, 1.5)))
    return a


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


BASE_CACHE = {}           # (id, edge) -> уменьшенный исходник
BASE_LOCK = threading.Lock()


def source_image(ph, mode):
    """mode: full — оригинал для «Файл», work — для чата, view — для «Проявки»."""
    if mode == "full" and has(ph["src"]):
        img = ImageOps.exif_transpose(Image.open(ph["src"]))
        if FULL_EDGE:
            img.draft("RGB", (FULL_EDGE, FULL_EDGE))
            img = img.convert("RGB")
            img.thumbnail((FULL_EDGE, FULL_EDGE), Image.LANCZOS)
        return img.convert("RGB")
    if not has(ph["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    if mode in ("full", "work"):
        return Image.open(ph["work"]).convert("RGB")
    key = (ph["id"], VIEW_EDGE)
    with BASE_LOCK:
        img = BASE_CACHE.get(key)
    if img is None:
        img = Image.open(ph["work"]).convert("RGB")
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
    out = img if key == "original" else film(img, key, ph["strength"], seed=ph["id"])
    if ph["leak"]:
        out = light_leak(out, ph.get("leak_kind") or "edge", leak_seed(ph))
    if ph["stamp"]:
        out = date_stamp(out, ph["taken"])
    if ph["frame"]:
        out = add_frame(out, ph["id"], pname(key))
    return out


def contact_sheet(ph):
    if not has(ph["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    base = Image.open(ph["work"]).convert("RGB")
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
        return "night800", L("ночь", "night")
    if r - b > 0.10 and lum > 0.25:
        return "amber_neg", L("тёплый свет", "warm light")
    if sat < 0.12:
        return "muted_chrome", L("пасмурно", "overcast")
    if sat > 0.25 and (g - (r + b) / 2 > 0.03 or sky_blue > 0.12):
        return "vivid50", L("пейзаж", "landscape")
    return "street_neg", L("улица", "street")


# ================= база (потокобезопасно) =================
DB_LOCK = threading.RLock()      # доступ к sqlite
EDIT_LOCK = threading.RLock()    # перерисовка кадра + правка сообщения
db = None                        # открывается в init_db() только в главном процессе: работникам база не нужна


def init_db():
    global db
    db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")
    with DB_LOCK:
        db.execute("""CREATE TABLE IF NOT EXISTS photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, src TEXT, work TEXT, thumb TEXT,
            taken TEXT, iso INTEGER, auto_key TEXT, auto_reason TEXT,
            preset TEXT, strength INTEGER DEFAULT 100,
            stamp INTEGER DEFAULT 0, frame INTEGER DEFAULT 0, leak INTEGER DEFAULT 0,
            msg_id INTEGER, file_id TEXT, hidden INTEGER DEFAULT 0, created REAL)""")
        cols = {r[1] for r in db.execute("PRAGMA table_info(photos)")}
        if "view" not in cols:
            db.execute("ALTER TABLE photos ADD COLUMN view TEXT")
        for col, decl in (("rev", "INTEGER DEFAULT 0"), ("rendered_rev", "INTEGER DEFAULT 0"), ("updated", "REAL DEFAULT 0"),
                          ("leak_kind", "TEXT DEFAULT 'edge'"), ("leak_seed", "INTEGER DEFAULT 0"), ("fp", "TEXT"),
                          ("msg_at", "REAL")):
            if col not in cols:
                db.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
        db.execute("CREATE INDEX IF NOT EXISTS photos_fp ON photos(fp)")
        # мини-приложение каждые 1–5 секунд спрашивает «что изменилось» и листает ленту — без индексов это полный перебор
        db.execute("CREATE INDEX IF NOT EXISTS photos_updated ON photos(updated)")
        db.execute("CREATE INDEX IF NOT EXISTS photos_feed ON photos(hidden, id)")
        for old, new in OLD_KEYS.items():
            db.execute("UPDATE photos SET preset=? WHERE preset=?", (new, old))
            db.execute("UPDATE photos SET auto_key=? WHERE auto_key=?", (new, old))
        db.commit()


def q(sql, args=()):
    with DB_LOCK:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def run(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def get(pid):
    rows = q("SELECT * FROM photos WHERE id=?", (pid,))
    return rows[0] if rows else None


def upd(pid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    run(f"UPDATE photos SET {cols} WHERE id=?", (*kw.values(), pid))


# ================= Telegram =================
def tg(method, files=None, **params):
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    for attempt in range(3):
        r = requests.post(f"{API}/{method}", data=data, files=files, timeout=(10, 120))   # (подключение, ответ)
        j = r.json()
        if j.get("ok"):
            return j["result"]
        wait = (j.get("parameters") or {}).get("retry_after")
        if r.status_code != 429 or not wait or attempt == 2:
            break
        log.warning("%s: Telegram просит подождать %s с", method, wait)   # слишком часто пишем в чат
        _pace_hold(params.get("chat_id"), float(wait))
        time.sleep(min(float(wait), 60))
        for v in (files or {}).values():          # файлы отправляются заново с начала
            f = v[1] if isinstance(v, tuple) else v
            if hasattr(f, "seek"):
                f.seek(0)
    raise RuntimeError(f"{method}: {j.get('description')}")


# Фоновые правки чата (пакеты, удаление, новые кадры) — не чаще раза в CHAT_PACE секунд на чат:
# Telegram ограничивает бота примерно одним сообщением в секунду на чат, при превышении отвечает 429.
# Ответы на нажатия кнопок идут без ожидания, но сдвигают время следующей фоновой правки.
CHAT_PACE = float(os.environ.get("CHAT_PACE_SECONDS", "1"))
_PACE = {}                    # чат -> когда можно следующую фоновую правку (time.monotonic)
_PACE_LOCK = threading.Lock()


def pace(chat_id):
    """Дождаться своей очереди на фоновую правку чата."""
    with _PACE_LOCK:
        now = time.monotonic()
        at = max(now, _PACE.get(chat_id, 0.0))
        _PACE[chat_id] = at + CHAT_PACE
    if at > now:
        time.sleep(at - now)


def _pace_hold(chat_id, seconds):
    with _PACE_LOCK:
        _PACE[chat_id] = max(_PACE.get(chat_id, 0.0), time.monotonic() + seconds)


def safe(method, **kw):
    try:
        return tg(method, **kw)
    except Exception as e:
        log.warning("%s", e)
        return None


def jpeg(img, q=92):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q, subsampling=0)
    buf.seek(0)
    return buf


def btn(text, data):
    return {"text": text, "callback_data": data}


def caption(ph):
    meta = []
    if ph["taken"]:
        meta.append(datetime.strptime(ph["taken"], "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M"))
    if ph["iso"]:
        meta.append(f"ISO {ph['iso']}")
    if ph["auto_reason"]:
        meta.append(L("авто", "auto") + f": {ph['auto_reason']} → {pname(ph['auto_key'])}")
    leak = " · " + L("засвет", "leak") + f": {LEAKS.get(ph.get('leak_kind') or 'edge', ('',))[0]}" if ph["leak"] else ""
    return f"#{ph['id']} · {pname(ph['preset'])} · {ph['strength']}%{leak}\n" + " · ".join(meta)


def main_kb(ph):
    pid = ph["id"]
    if not has(ph["work"]):
        return {"inline_keyboard": [[btn(L("🗄 В архиве", "🗄 Archived"), "x"), btn(L("🗑 Удалить", "🗑 Delete"), f"del:{pid}")]]}
    on = lambda f: "✅ " if ph[f] else ""
    return {"inline_keyboard": [
        [btn(f"🎞 {pname(ph['preset'])} ▾", f"m:{pid}"), btn(L("🔍 Сравнить", "🔍 Compare"), f"c:{pid}")],
        [btn("➖", f"s:{pid}:-"), btn(L("Сила", "Strength") + f" {ph['strength']}%", "x"), btn("➕", f"s:{pid}:+")],
        [btn(on("stamp") + L("📅 Дата", "📅 Date"), f"t:{pid}:stamp"), btn(on("frame") + L("🖼 Рамка", "🖼 Frame"), f"t:{pid}:frame"),
         btn(on("leak") + L("✨ Засвет ▾", "✨ Leak ▾"), f"lm:{pid}")],
        [btn(L("⬇️ Файл", "⬇️ File"), f"f:{pid}"), btn(L("🗑 Удалить", "🗑 Delete"), f"del:{pid}")],
    ]}


def preset_kb(ph, prefix="p"):
    pid = ph["id"]
    keys = list(PRESETS)
    rows = []
    for i in range(0, len(keys), 3):
        rows.append([btn(("• " if ph["preset"] == k else "") + PRESETS[k]["name"], f"{prefix}:{pid}:{k}")
                     for k in keys[i:i + 3]])
    if prefix == "p":
        rows.append([btn(L("↩️ Оригинал", "↩️ Original"), f"p:{pid}:original"),
                     btn(L("🤖 Авто", "🤖 Auto") + f" ({pname(ph['auto_key'])})", f"p:{pid}:{ph['auto_key']}")])
        rows.append([btn(L("← Назад", "← Back"), f"b:{pid}")])
    else:
        rows.append([btn(L("✖️ Закрыть", "✖️ Close"), "cx")])
    return {"inline_keyboard": rows}


def leak_kb(ph):
    pid = ph["id"]
    cur = (ph.get("leak_kind") or "edge") if ph["leak"] else ""
    keys = list(LEAKS)
    rows = [[btn(("• " if cur == k else "") + LEAKS[k][0], f"l:{pid}:{k}") for k in keys[i:i + 3]]
            for i in range(0, len(keys), 3)]
    rows.append([btn(("• " if not cur else "") + L("Без засвета", "No leak"), f"l:{pid}:"), btn(L("↻ Сдвинуть", "↻ Shift"), f"ls:{pid}")])
    rows.append([btn(L("← Назад", "← Back"), f"b:{pid}")])
    return {"inline_keyboard": rows}


KB_FEED, KB_TODAY = L("📚 Лента", "📚 Feed"), L("📅 Сегодня", "📅 Today")
KB_FILM, KB_HELP = L("🎞 Плёнка по умолчанию", "🎞 Default film"), L("❓ Помощь", "❓ Help")
MENU = {"keyboard": [[{"text": KB_FEED}, {"text": KB_TODAY}],
                     [{"text": KB_FILM}, {"text": KB_HELP}]],
        "resize_keyboard": True, "is_persistent": True}


def touch(pid):
    upd(pid, updated=time.time())


def send_new(ph):
    """Прислать кадр новым сообщением (из ленты в чате), по кешу Telegram."""
    if not ph["file_id"]:
        tg("sendMessage", chat_id=CHAT_ID, text=L(f"Кадр #{ph['id']} ещё проявляется.", f"Frame #{ph['id']} is still developing."))
        return
    with EDIT_LOCK:
        res = tg("sendPhoto", chat_id=CHAT_ID, photo=ph["file_id"], caption=caption(ph), reply_markup=main_kb(ph))
        upd(ph["id"], msg_id=res["message_id"], msg_at=time.time(), file_id=res["photo"][-1]["file_id"])


MSG_DELETE_WINDOW = 47 * 3600     # Telegram даёт боту удалить сообщение только в первые 48 часов


def delete_photos(ids):
    """Удалить кадры насовсем: из ленты, из чата и с диска. В базе остаётся строка с отпечатком (fp),
    поэтому повторная выгрузка того же кадра с камеры или телефона его не вернёт."""
    now = time.time()
    gone = []
    for pid in ids:
        ph = get(pid)
        if not ph or ph["hidden"]:
            continue
        # rev+1 — результаты рисования, которое уже идёт, будут выброшены (_view_done это проверяет)
        run("UPDATE photos SET hidden=1, src=NULL, work=NULL, view=NULL, thumb=NULL, file_id=NULL, "
            "rev=rev+1, updated=? WHERE id=?", (now, pid))
        for p in (ph["src"], ph["work"], ph["view"], ph["thumb"]):
            remove(p)
        for p in PREVIEWS.glob(f"{pid}_*.jpg"):
            remove(str(p))
        gone.append(ph)
    if gone:
        NET.submit(_delete_messages, gone)
    return len(gone)


def _delete_messages(phs):
    fresh, old = [], []
    for ph in phs:
        if ph["msg_id"]:
            sent = ph.get("msg_at") or ph["created"] or 0
            (fresh if time.time() - sent < MSG_DELETE_WINDOW else old).append(ph["msg_id"])
    for i in range(0, len(fresh), 100):
        chunk = fresh[i:i + 100]
        pace(CHAT_ID)
        if not safe("deleteMessages", chat_id=CHAT_ID, message_ids=chunk):
            for mid in chunk:                    # на всякий случай по одному
                if not safe("deleteMessage", chat_id=CHAT_ID, message_id=mid):
                    old.append(mid)
    for mid in old:                              # старше 48 часов: удалить нельзя — меняем фото на заглушку
        pace(CHAT_ID)
        with open(deleted_placeholder(), "rb") as f:
            if not safe("editMessageMedia", files={"f": ("deleted.jpg", f)}, chat_id=CHAT_ID, message_id=mid,
                        media={"type": "photo", "media": "attach://f", "caption": L("Удалено", "Deleted")},
                        reply_markup={"inline_keyboard": []}):
                safe("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid, reply_markup={"inline_keyboard": []})


def deleted_placeholder():
    path = BASE / f"deleted_{LANG}.jpg"
    if not path.exists():
        img = Image.new("RGB", (640, 400), (24, 24, 24))
        d = ImageDraw.Draw(img)
        f = font(40)
        txt = L("удалено", "deleted")
        x0, y0, x1, y1 = d.textbbox((0, 0), txt, font=f)
        d.text(((640 - (x1 - x0)) // 2, (400 - (y1 - y0)) // 2 - y0), txt, fill=(140, 140, 140), font=f)
        save_atomic(img, str(path), 85)
    return path


def hide_photo(ph):
    delete_photos([ph["id"]])


# ================= задачи в отдельных процессах =================
# Эти функции выполняются в процессах-работниках: только рендер и файлы, без базы и Telegram.
def save_atomic(img, path, quality):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # уникально: несколько процессов могут писать один файл
    img.save(tmp, "JPEG", quality=quality, subsampling=0 if quality >= 90 else 2)
    try:
        os.replace(tmp, path)
    except PermissionError:          # Windows: файл как раз читает другой процесс — его копия не хуже
        if not os.path.exists(path):
            raise
        os.remove(tmp)
    return path


def job_warm():
    for k in PRESETS:
        preset_lut(k)
    return os.getpid()


def job_prepare(src, work_path, edge):
    im = Image.open(src)
    taken, iso = read_exif(im)
    im.draft("RGB", (edge, edge))           # JPEG сразу декодируется в уменьшенном виде — в разы быстрее
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((edge, edge), Image.LANCZOS)
    taken = taken or datetime.now().strftime("%Y-%m-%d %H:%M")
    auto_key, reason = auto_pick(im, iso, int(taken[11:13]))
    save_atomic(im, work_path, 95)
    return taken, iso, auto_key, reason


def job_view(ph, view_path, thumb_path, chat_path=None):
    """Один рендер на правку: версия для чата (WORK_EDGE), из неё уменьшаются «Проявка» и миниатюра.
    Раньше чат и «Проявка» рисовались по отдельности — это лишние ~300 мс процессора на каждую правку.
    Без chat_path (догрузка старых кадров) рисуется только версия для «Проявки»."""
    if chat_path:
        img = render(ph)
        save_atomic(img, chat_path, 92)
        img.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    else:
        img = render(ph, mode="view")
    save_atomic(img, view_path, 88)
    img.thumbnail((500, 500))
    save_atomic(img, thumb_path, 85)
    return view_path, thumb_path


def job_chat(ph, path):
    return save_atomic(render(ph), path, 92)


def job_full(ph, path):
    return save_atomic(render(ph, full=True), path, 95)


def job_preview(ph, key, strength, path, leak=""):
    base_path = PREVIEWS / f"{ph['id']}_base.jpg"
    if not base_path.exists():
        b = Image.open(ph["work"])
        b.draft("RGB", (420, 420))
        b = b.convert("RGB")
        b.thumbnail((420, 420), Image.LANCZOS)
        save_atomic(b, str(base_path), 92)
    base = Image.open(base_path).convert("RGB")
    out = base if key == "original" else film(base, key, strength, seed=ph["id"])
    if leak:
        out = light_leak(out, leak, leak_seed(ph))
    return save_atomic(out, path, 84)


def job_contact(ph, path):
    return save_atomic(contact_sheet(ph), path, 88)


# ================= планировщик =================
FAST = HEAVY = None
NET = ThreadPoolExecutor(max_workers=2, thread_name_prefix="net")   # загрузки в Telegram
EVENTS = queue.Queue()
JOB_LOCK = threading.Lock()
VIEW_INFLIGHT, VIEW_DIRTY = set(), set()
EXPORTING = {}


def _worker_init(nice):
    try:
        os.nice(nice)
    except (OSError, AttributeError):   # AttributeError — Windows, где нет nice (запуск для разработки)
        pass


def init_pools():
    global FAST, HEAVY
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    FAST = ProcessPoolExecutor(max_workers=FAST_WORKERS, mp_context=ctx,
                               initializer=_worker_init, initargs=(WORKER_NICE,))
    HEAVY = ProcessPoolExecutor(max_workers=HEAVY_WORKERS, mp_context=ctx, max_tasks_per_child=8,
                                initializer=_worker_init, initargs=(WORKER_NICE + 5,))
    warm = [FAST.submit(job_warm) for _ in range(FAST_WORKERS)] + [HEAVY.submit(job_warm)]
    for f in warm:
        f.result()
    log.info("render pools ready: fast=%d heavy=%d", FAST_WORKERS, HEAVY_WORKERS)


def dispatcher():
    """Результаты из процессов обрабатываем в одном потоке — без гонок."""
    while True:
        fn, args = EVENTS.get()
        try:
            fn(*args)
        except Exception:
            log.exception("dispatcher")


def jobs_in_work():
    with JOB_LOCK:
        return len(VIEW_INFLIGHT) + len(RQ_QUEUED) + sum(EXPORTING.values())


# Очередь отрисовки. Сразу в процессы отдаётся не больше задач, чем они успевают (VIEW_SLOTS): иначе сотня кадров
# из пакетной правки встала бы в очередь пула, и превью плёнок в «Проявке» ждали бы их все.
# Срочность: 0 — правка одного кадра (человек ждёт), 1 — пакетная правка и новые кадры, 2 — фоновая догрузка.
# Внутри одной срочности пользователи обслуживаются по кругу, чтобы один большой пакет не задерживал остальных.
VIEW_SLOTS = max(1, FAST_WORKERS - 1) if FAST_WORKERS > 2 else FAST_WORKERS
RQ_PENDING = {0: {}, 1: {}, 2: {}}    # срочность -> {пользователь: deque[pid]} (порядок ключей = очередь по кругу)
RQ_QUEUED = {}                        # pid -> (срочность, нужен ли чат)
VIEW_CHAT = {}                        # pid в работе -> нужна ли версия для чата
VIEW_PRIO = {}                        # pid в работе -> срочность (с ней же правка уйдёт в чат)


def schedule_view(pid, prio=0, chat=True, uid=0):
    """Перерисовать кадр. Если уже рисуется — дорисуем последнее состояние следом."""
    with JOB_LOCK:
        if pid in VIEW_INFLIGHT:
            VIEW_DIRTY.add(pid)
            VIEW_CHAT[pid] = VIEW_CHAT.get(pid, False) or chat
            VIEW_PRIO[pid] = min(VIEW_PRIO.get(pid, prio), prio)
            return
        if pid in RQ_QUEUED:
            old_prio, old_chat = RQ_QUEUED[pid]
            RQ_QUEUED[pid] = (min(prio, old_prio), old_chat or chat)
            if prio < old_prio:                  # стал срочнее — переносим в нужную очередь
                for dq in RQ_PENDING[old_prio].values():
                    if pid in dq:
                        dq.remove(pid)
                RQ_PENDING[prio].setdefault(uid, collections.deque()).append(pid)
        else:
            RQ_QUEUED[pid] = (prio, chat)
            RQ_PENDING[prio].setdefault(uid, collections.deque()).append(pid)
    _pump()


def _pump():
    """Раздать задачи из очереди, пока есть свободные места в процессах."""
    while True:
        with JOB_LOCK:
            if len(VIEW_INFLIGHT) >= VIEW_SLOTS:
                return
            pick = None
            for prio in (0, 1, 2):
                users = RQ_PENDING[prio]
                while users and pick is None:
                    uid = next(iter(users))
                    dq = users.pop(uid)
                    if dq:
                        pid = dq.popleft()
                        if dq:
                            users[uid] = dq      # в конец круга
                        if pid in RQ_QUEUED:
                            pick = (pid, RQ_QUEUED.pop(pid)[1], prio)
                if pick:
                    break
            if pick is None:
                return
            VIEW_INFLIGHT.add(pick[0])
            VIEW_CHAT[pick[0]] = pick[1]
            VIEW_PRIO[pick[0]] = pick[2]
        _submit_view(pick[0])


def queue_size():
    with JOB_LOCK:
        return len(RQ_QUEUED)


def _release(pid):
    with JOB_LOCK:
        VIEW_INFLIGHT.discard(pid)
        VIEW_DIRTY.discard(pid)
        VIEW_CHAT.pop(pid, None)
        VIEW_PRIO.pop(pid, None)
    batch_step(pid, "gone")


def _submit_view(pid):
    ph = get(pid)
    if not ph or ph["hidden"] or not has(ph["work"]):
        _release(pid)
        _pump()
        return
    rev = ph["rev"]
    with JOB_LOCK:
        chat = VIEW_CHAT.get(pid, True) and bool(ph["msg_id"] or not ph["file_id"])
    chat_path = str(TMP / f"chat_{pid}_{rev}.jpg") if chat else None
    fut = FAST.submit(job_view, ph, str(VIEWS / f"{pid}.jpg"), str(THUMBS / f"{pid}.jpg"), chat_path)
    fut.add_done_callback(lambda f: EVENTS.put((_view_done, (pid, rev, chat, f))))


def _view_done(pid, rev, chat, fut):
    err = fut.exception()
    result = "fail"
    if err:
        log.warning("view #%d: %s", pid, err)
    else:
        view, thumb = fut.result()
        cur = get(pid)
        if not cur or cur["hidden"]:            # кадр удалили, пока он рисовался — не оставлять файлы
            for p in (view, thumb, str(TMP / f"chat_{pid}_{rev}.jpg")):
                remove(p)
            result = "gone"
        elif cur["rev"] == rev:
            upd(pid, view=view, thumb=thumb, rendered_rev=rev, updated=time.time())
            result = "ok"
            if chat:
                with JOB_LOCK:
                    prio = VIEW_PRIO.get(pid, 1)
                queue_tg_update(pid, urgent=prio == 0)
    with JOB_LOCK:
        again = pid in VIEW_DIRTY
        VIEW_DIRTY.discard(pid)
        if not again:
            VIEW_INFLIGHT.discard(pid)
            VIEW_CHAT.pop(pid, None)
            VIEW_PRIO.pop(pid, None)
    if again:
        _submit_view(pid)
    else:
        batch_step(pid, result)
        _pump()


# Пакетная правка из «Проявки»: кадры встают в общую очередь со срочностью 1, а когда проявится последний,
# в чат уходит одно итоговое сообщение. Сами фото в чате обновляются фоном, не чаще раза в секунду.
BATCHES = {}      # номер пакета -> {"left": set(pid), "ok", "fail", "total", "text"}
BATCH_OF = {}     # pid -> номер пакета, в котором он ещё не проявлен
_BATCH_SEQ = itertools.count(1)


def batch_track(pids, text):
    finished = []
    with JOB_LOCK:
        bid = next(_BATCH_SEQ)
        BATCHES[bid] = {"left": set(pids), "pids": list(pids), "ok": 0, "fail": 0, "total": len(pids), "text": text}
        for pid in pids:
            old = BATCH_OF.get(pid)
            if old in BATCHES:                   # кадр перешёл в новый пакет — в старом считаем его готовым
                b = BATCHES[old]
                b["left"].discard(pid)
                b["ok"] += 1
                if not b["left"]:
                    finished.append(BATCHES.pop(old))
            BATCH_OF[pid] = bid
    for b in finished:
        NET.submit(_batch_report, b)
    return bid


def batch_step(pid, result):
    """Кадр из пакета дорисован (ok), не получился (fail) или удалён (gone)."""
    with JOB_LOCK:
        bid = BATCH_OF.pop(pid, None)
        b = BATCHES.get(bid)
        if not b:
            return
        b["left"].discard(pid)
        b["ok" if result == "ok" else "fail"] += result != "gone"
        if result == "gone":
            b["total"] -= 1
        if b["left"]:
            return
        BATCHES.pop(bid)
    NET.submit(_batch_report, b)


def _batch_report(b):
    if b["total"] <= 0:
        return
    t = time.time()
    while time.time() - t < 900:                 # итог — после того, как сами фото в чате обновились
        with TG_COND:
            if not TG_BUSY.intersection(b["pids"]):
                break
        time.sleep(0.5)
    n = b["ok"]
    text = L(f"Готово: {n} {plural_ru(n, 'кадр', 'кадра', 'кадров')} → {b['text']}",
             f"Done: {n} frame{'' if n == 1 else 's'} → {b['text']}")
    if b["fail"]:
        text += L(f"\nНе получилось: {b['fail']}", f"\nFailed: {b['fail']}")
    pace(CHAT_ID)
    safe("sendMessage", chat_id=CHAT_ID, text=text, disable_notification=True)


def plural_ru(n, one, few, many):
    a, b = n % 10, n % 100
    return one if a == 1 and b != 11 else few if 2 <= a <= 4 and not 12 <= b <= 14 else many


def export_photo(pid):
    """Экспорт в полный размер — в отдельной очереди, не мешает переключению плёнок."""
    ph = get(pid)
    if not ph or ph["hidden"]:
        raise RuntimeError(L("кадр не найден", "frame not found"))
    if not has(ph["work"]) and not has(ph["src"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    with JOB_LOCK:
        EXPORTING[pid] = EXPORTING.get(pid, 0) + 1
    touch(pid)
    out = TMP / f"full_{pid}_{int(time.time() * 1000)}.jpg"
    fut = HEAVY.submit(job_full, ph, str(out))
    fut.add_done_callback(lambda f: NET.submit(_export_done, pid, ph, out, f))


def _export_done(pid, ph, out, fut):
    try:
        err = fut.exception()
        if err:
            safe("sendMessage", chat_id=CHAT_ID, text=L(f"Не смог экспортировать #{pid}: {err}", f"Could not export #{pid}: {err}"))
            return
        note = None if has(ph["src"]) else L("Оригинал уже удалён для экономии места, это версия для чата.",
                                         "The original was deleted to save space; this is the chat version.")
        cur = get(pid) or ph
        if cur["hidden"]:                       # удалили, пока готовился файл
            return
        with open(out, "rb") as f:
            tg("sendDocument", files={"document": (f"{Path(ph['name']).stem}_{ph['preset']}.jpg", f)},
               chat_id=CHAT_ID, reply_to_message_id=cur["msg_id"], caption=note)
    except Exception as e:
        log.exception("export upload #%d", pid)
        safe("sendMessage", chat_id=CHAT_ID, text=L(f"Не смог отправить файл #{pid}: {e}", f"Could not send file #{pid}: {e}"))
    finally:
        remove(str(out))
        with JOB_LOCK:
            EXPORTING[pid] = EXPORTING.get(pid, 1) - 1
            if EXPORTING[pid] <= 0:
                EXPORTING.pop(pid, None)
        touch(pid)


TG_PENDING = set()
TG_URGENT = set()      # правки одного кадра — в чат вперёд пакетных
TG_BUSY = set()        # кадры, которые ещё не дошли до чата (в очереди, рисуются или заливаются)
TG_COND = threading.Condition()


def queue_tg_update(pid, urgent=False):
    with TG_COND:
        TG_PENDING.add(pid)
        TG_BUSY.add(pid)
        if urgent:
            TG_URGENT.add(pid)
        TG_COND.notify()


def _tg_order(pid):
    return (pid not in TG_URGENT, pid)


def _tg_settled(pid):
    with TG_COND:
        if pid not in TG_PENDING:
            TG_BUSY.discard(pid)


TG_RENDER_AHEAD = 2   # сколько кадров для чата рисуем заранее, пока заливается текущий


def _tg_upload(pid, rev, path):
    ph = get(pid)
    if not ph or ph["hidden"] or ph["rev"] != rev:
        return   # кадр убрали или снова поменяли — свежий вариант уже в очереди
    pace(CHAT_ID)
    with EDIT_LOCK, open(path, "rb") as f:
        res = None
        if ph["msg_id"]:
            media = {"type": "photo", "media": "attach://f", "caption": caption(ph)}
            try:
                res = tg("editMessageMedia", files={"f": ("p.jpg", f)}, chat_id=CHAT_ID,
                         message_id=ph["msg_id"], media=media, reply_markup=main_kb(ph))
            except Exception as e:
                if "not modified" in str(e):
                    return
                log.warning("edit #%d failed (%s), sending new", pid, e)
                f.seek(0)
        if res is None:
            res = tg("sendPhoto", files={"photo": ("p.jpg", f)}, chat_id=CHAT_ID,
                     caption=caption(ph), reply_markup=main_kb(ph))
            upd(pid, msg_id=res["message_id"], msg_at=time.time())
        upd(pid, file_id=res["photo"][-1]["file_id"])


def older_unsent(pid):
    """Есть ли более ранний новый кадр, ещё не отправленный в чат (не старше 2 минут, чтобы сбойный не держал очередь)."""
    return q("SELECT COUNT(*) AS n FROM photos WHERE id < ? AND msg_id IS NULL AND hidden=0 "
             "AND work IS NOT NULL AND created > ?", (pid, time.time() - 120))[0]["n"] > 0


def tg_worker():
    """Конвейер: следующие кадры рисуются, пока текущий заливается. Новые кадры уходят в чат по порядку."""
    inflight = {}   # pid -> (rev, future, path)
    ready = {}      # pid -> (rev, path): нарисован, ждёт отправки
    while True:
        start_now = []
        with TG_COND:
            if not TG_PENDING and not inflight:
                TG_COND.wait(timeout=0.5 if ready else None)   # ждём, не крутясь вхолостую
            for pid in sorted(TG_PENDING, key=_tg_order):
                if len(inflight) + len(start_now) >= TG_RENDER_AHEAD:
                    break
                if pid in inflight or pid in ready:
                    continue
                TG_PENDING.discard(pid)
                start_now.append(pid)
        for pid in start_now:
            ph = get(pid)
            if not ph or ph["hidden"] or not has(ph["work"]):
                _tg_settled(pid)
                continue
            path = TMP / f"chat_{pid}_{ph['rev']}.jpg"
            if path.exists():                  # обычно уже нарисован вместе с версией для «Проявки»
                ready[pid] = (ph["rev"], path)
                continue
            inflight[pid] = (ph["rev"], FAST.submit(job_chat, ph, str(path)), path)
        if inflight:
            done, _ = futures_wait([v[1] for v in inflight.values()], timeout=0.5, return_when=FIRST_COMPLETED)
            for pid, (rev, fut, path) in list(inflight.items()):
                if fut in done:
                    inflight.pop(pid)
                    if fut.exception():
                        log.warning("chat render #%d: %s", pid, fut.exception())
                        remove(str(path))
                        _tg_settled(pid)
                    else:
                        ready[pid] = (rev, path)
        for pid in sorted(ready, key=_tg_order):
            ph = get(pid)
            if ph and not ph["msg_id"] and older_unsent(pid):
                continue   # новый кадр не обгоняет более ранний, который ещё проявляется
            rev, path = ready.pop(pid)
            with TG_COND:
                TG_URGENT.discard(pid)
            try:
                _tg_upload(pid, rev, path)
            except Exception:
                log.exception("tg upload #%d", pid)
            finally:
                remove(str(path))
                _tg_settled(pid)


def apply_changes(ph, changes, sync_tg=None, prio=0):
    """Поставить изменения в очередь и сразу вернуть состояние. Рисуется фоном.
    prio: 0 — правка одного кадра, 1 — пакетная (уступает одиночным)."""
    fields = {}
    cur = get(ph["id"])
    if "preset" in changes:
        k = canon(changes["preset"])
        if k == "auto" and cur:                 # пакетом: каждому кадру его собственный автовыбор
            k = cur["auto_key"] or "original"
        if k != "original" and k not in PRESETS:
            raise ValueError(L("неизвестная плёнка", "unknown film"))
        fields["preset"] = k
    if "strength" in changes:
        s = int(changes["strength"])
        if s not in STRENGTHS:
            raise ValueError(L("неверная сила", "invalid strength"))
        fields["strength"] = s
    for f in ("stamp", "frame"):
        if f in changes:
            fields[f] = 1 if changes[f] else 0
    if not cur or not has(cur["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    if "leak" in changes:
        v = changes["leak"]
        if v in (None, "", False, 0):
            fields["leak"] = 0
        elif v is True or v == 1:
            fields["leak"] = 1
        elif v in LEAKS:
            fields["leak"], fields["leak_kind"] = 1, v
        else:
            raise ValueError(L("неизвестный засвет", "unknown light leak"))
    if changes.get("leak_shift"):
        fields["leak_seed"] = int(cur.get("leak_seed") or 0) + 1
        fields.setdefault("leak", 1)
    fields = {k: v for k, v in fields.items() if cur.get(k) != v}   # уже так — не перерисовывать
    if not fields:
        return cur
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE photos SET {cols}, rev=rev+1, updated=? WHERE id=?", (*fields.values(), time.time(), ph["id"]))
    schedule_view(ph["id"], prio=prio)
    return get(ph["id"])


# ================= хранилище =================
def fsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dir_bytes(*dirs):
    return sum(fsize(p) for d in dirs for p in d.iterdir() if p.is_file())


def remove(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup():
    now = time.time()
    for p in PREVIEWS.iterdir():
        if p.is_file() and now - p.stat().st_mtime > 86400:
            remove(p)
    for p in TMP.iterdir():                 # версии для чата, которые устарели, пока ждали отправки
        if p.is_file() and now - p.stat().st_mtime > 3600:
            remove(p)
    # 1) старые оригиналы (они есть на карте камеры)
    for r in q("SELECT id, src FROM photos WHERE src IS NOT NULL AND created < ?", (now - ORIG_DAYS * 86400,)):
        remove(r["src"])
        upd(r["id"], src=None)
    # 2) общий лимит и свободное место: сначала оригиналы, потом рабочие копии, от самых старых
    limit = STORAGE_GB * 1e9
    min_free = MIN_FREE_GB * 1e9
    total = dir_bytes(ORIGINALS, WORK, VIEWS, THUMBS)
    free = shutil.disk_usage(BASE).free
    if total <= limit and free >= min_free:
        return
    for field in ("src", "work"):
        for r in q(f"SELECT id, {field} AS p FROM photos WHERE {field} IS NOT NULL ORDER BY id"):
            if total <= limit and free >= min_free:
                return
            size = fsize(r["p"])
            remove(r["p"])
            upd(r["id"], **{field: None}, updated=time.time())
            total -= size
            free += size
            log.info("cleanup: #%d %s removed (%.1f MB)", r["id"], field, size / 1e6)
    for p in sorted(ORIGINALS.glob("failed_*"), key=lambda x: x.stat().st_mtime):
        if total <= limit and free >= min_free:
            return
        total -= fsize(p)
        remove(p)


def storage_text():
    n = q("SELECT COUNT(*) AS n, SUM(src IS NOT NULL) AS o, SUM(work IS NOT NULL) AS w FROM photos WHERE hidden=0")[0]
    gb = lambda b: f"{b / 1e9:.1f} " + L("ГБ", "GB")
    du = shutil.disk_usage(BASE)
    if LANG == "en":
        return (f"Frames in feed: {n['n'] or 0}\n"
                f"With original: {n['o'] or 0}, film can be changed: {n['w'] or 0}\n"
                f"Originals: {gb(dir_bytes(ORIGINALS))}, working copies: {gb(dir_bytes(WORK))}, "
                f"previews: {gb(dir_bytes(VIEWS, THUMBS))}\n"
                f"Limit: {STORAGE_GB:g} GB, originals kept {ORIG_DAYS:g} days\n"
                f"Free disk space: {gb(du.free)} of {gb(du.total)}")
    return (f"Кадров в ленте: {n['n'] or 0}\n"
            f"С оригиналом: {n['o'] or 0}, можно менять плёнку: {n['w'] or 0}\n"
            f"Оригиналы: {gb(dir_bytes(ORIGINALS))}, рабочие копии: {gb(dir_bytes(WORK))}, "
            f"превью: {gb(dir_bytes(VIEWS, THUMBS))}\n"
            f"Лимит: {STORAGE_GB:g} ГБ, оригиналы живут {ORIG_DAYS:g} дн.\n"
            f"Свободно на диске: {gb(du.free)} из {gb(du.total)}")


# ================= экраны бота =================
def help_text():
    lines = [L("Как пользоваться:", "How to use:"),
             L("• Снимаешь — через ~30 сек фото прилетает сюда уже с плёнкой.",
               "• Take a shot — in ~30 s it arrives here, already on film."),
             L("• 🎞 под фото — сменить плёнку, 🔍 — все плёнки разом на одном листе.",
               "• 🎞 under a photo — change film, 🔍 — all films at once on one sheet."),
             L("• ➖/➕ — сила эффекта, 📅 дата как у мыльницы, 🖼 рамка негатива, ✨ засвет.",
               "• ➖/➕ — effect strength, 📅 point-and-shoot date, 🖼 negative frame, ✨ light leak."),
             L("• ⬇️ Файл — полный размер без сжатия Telegram.", "• ⬇️ File — full size without Telegram compression."),
             L("• 📚 Лента — сетка кадров в чате, 📅 Сегодня — альбом за день.",
               "• 📚 Feed — grid of frames in the chat, 📅 Today — album of the day."),
             L("• /storage — сколько места занято.", "• /storage — how much space is used."),
             L("• /camera — файлы и инструкция для настройки камеры.", "• /camera — files and guide to set up your camera."),
             L("• Можно прислать любое фото файлом — обработаю.", "• Send any photo as a file — I will develop it.")]
    if WEBAPP_URL:
        lines.append(L("• Кнопка «Проявка» слева от поля ввода — лента-приложение со свайпами и превью плёнок.",
                       "• The Proyavka button next to the input field — feed app with swipes and live film previews."))
    lines += ["", L("Плёнки:", "Films:")]
    for k, p in PRESETS.items():
        lines.append(f"{p['name']} — {p['when']}: {p['desc']}")
    return "\n".join(lines)


def gallery_page(page):
    total = q("SELECT COUNT(*) AS n FROM photos WHERE hidden=0")[0]["n"]
    pages = max(1, math.ceil(total / PAGE))
    page = max(0, min(page, pages - 1))
    rows = q("SELECT * FROM photos WHERE hidden=0 ORDER BY id DESC LIMIT ? OFFSET ?", (PAGE, page * PAGE))
    kb = []
    for i in range(0, len(rows), 4):
        kb.append([btn(f"#{r['id']}", f"o:{r['id']}") for r in rows[i:i + 4]])
    kb.append([btn("◀", f"g:{page - 1}"), btn(f"{page + 1}/{pages}", "x"), btn("▶", f"g:{page + 1}")])
    if WEBAPP_URL:
        kb.append([{"text": L("📱 Открыть «Проявку»", "📱 Open Proyavka"), "web_app": {"url": WEBAPP_URL}}])
    return gallery_image(rows), {"inline_keyboard": kb}, L(f"Лента: {total} кадров. Нажми номер — пришлю кадр с кнопками.", f"Feed: {total} frames. Tap a number to get the frame with buttons.")


def default_kb(state):
    cur = state.get("default", "auto")
    rows = [[btn(("• " if cur == "auto" else "") + L("🤖 Авто по ситуации", "🤖 Auto by scene"), "d:auto")]]
    keys = list(PRESETS)
    for i in range(0, len(keys), 3):
        rows.append([btn(("• " if cur == k else "") + PRESETS[k]["name"], f"d:{k}") for k in keys[i:i + 3]])
    return {"inline_keyboard": rows}


def send_today():
    today = datetime.now().strftime("%Y-%m-%d")
    rows = q("SELECT * FROM photos WHERE hidden=0 AND file_id IS NOT NULL AND substr(taken,1,10)=? ORDER BY id",
             (today,))
    if not rows:
        tg("sendMessage", chat_id=CHAT_ID, text=L("Сегодня кадров пока нет.", "No frames today yet."))
        return
    for i in range(0, len(rows), 10):
        chunk = rows[i:i + 10]
        media = [{"type": "photo", "media": r["file_id"]} for r in chunk]
        media[0]["caption"] = L("Прогулка", "Walk") + f" {datetime.now():%d.%m} · {len(rows)} " + L("кадров", "frames")
        tg("sendMediaGroup", chat_id=CHAT_ID, media=media)


# ================= события бота =================
def on_text(text, state):
    t = text.strip().lower()
    if t in ("/start", "/help", KB_HELP.lower()):
        tg("sendMessage", chat_id=CHAT_ID, text=help_text(), reply_markup=MENU)
    elif t in ("/gallery", KB_FEED.lower()):
        img, kb, cap = gallery_page(0)
        tg("sendPhoto", files={"photo": ("g.jpg", jpeg(img, 88))}, chat_id=CHAT_ID, caption=cap, reply_markup=kb)
    elif t in ("/today", KB_TODAY.lower()):
        send_today()
    elif t in ("/film", KB_FILM.lower()):
        tg("sendMessage", chat_id=CHAT_ID, text=L("Какую плёнку ставить новым кадрам?", "Which film for new frames?"), reply_markup=default_kb(state))
    elif t == "/storage":
        tg("sendMessage", chat_id=CHAT_ID, text=storage_text())
    elif t == "/camera":
        send_camera_setup()


# ================= настройка камеры прямо из чата =================
PROJECT_URL = os.environ.get("PROJECT_URL", "https://github.com/Melnikoff07/proyavka")
APP_ROOT = Path(__file__).resolve().parent.parent
CAMERA_CONFIG = APP_ROOT / "camera-config" / "config.txt"
FTP_ROOT_CERT = APP_ROOT / "camera-app" / "certs" / "isrgrootx1.pem"


def send_camera_setup():
    """Всё, что нужно положить в камеру, — файлами в чат: скачал, скинул на карту, готово."""
    domain = os.environ.get("DOMAIN", "")
    pw = os.environ.get("FTP_PASS", "—")
    guide = f"{PROJECT_URL}/blob/main/docs"
    ext = ".md" if LANG == "en" else ".ru.md"
    text = L(
        "📷 Настройка камеры\n\n"
        "Sony с приложениями (a6000–a6500, a7 II, RX100 III–V…)\n"
        f"1. Поставь приложение Proyavka.apk — пошагово: {guide}/sony-app{ext}\n"
        "2. На карте памяти создай папку PROYAVKA и положи в неё файл config.txt (ниже).\n"
        "3. В камере: Меню → Приложение → Проявка → Wi-Fi → выбери сеть (точку доступа телефона или дом) "
        "и введи пароль. Один раз — дальше камера подключается сама.\n"
        "4. Снимай в JPEG или RAW+JPEG → Проявка → Отправить новые.\n\n"
        "Камеры с отправкой по FTP (Sony A7C II, A7 IV, A1…)\n"
        f"сервер: {domain}\nпорт: 21\nпользователь: camera\nпароль: {pw}\n"
        "папка: upload · FTPS (явный TLS) · пассивный режим\n"
        "Камере нужен корневой сертификат: положи файл cacert.pem (ниже) в корень карты "
        f"и импортируй его в меню сети. Пошагово: {guide}/ftp-cameras{ext}",
        "📷 Camera setup\n\n"
        "Sony with PlayMemories apps (a6000–a6500, a7 II, RX100 III–V…)\n"
        f"1. Install the Proyavka.apk app — step by step: {guide}/sony-app{ext}\n"
        "2. On the memory card create a folder PROYAVKA and put config.txt (below) into it.\n"
        "3. On the camera: Menu → Application → Proyavka → Wi-Fi → pick a network (phone hotspot or home) "
        "and enter the password. Once — after that the camera connects by itself.\n"
        "4. Shoot JPEG or RAW+JPEG → Proyavka → Send new.\n\n"
        "Cameras with FTP transfer (Sony A7C II, A7 IV, A1…)\n"
        f"server: {domain}\nport: 21\nuser: camera\npassword: {pw}\n"
        "folder: upload · FTPS (explicit TLS) · passive mode\n"
        "The camera needs a root certificate: put cacert.pem (below) in the root of the card "
        f"and import it in the network menu. Step by step: {guide}/ftp-cameras{ext}")
    tg("sendMessage", chat_id=CHAT_ID, disable_web_page_preview=True, text=text)
    if CAMERA_CONFIG.exists():
        with open(CAMERA_CONFIG, "rb") as f:
            tg("sendDocument", files={"document": ("config.txt", f)}, chat_id=CHAT_ID,
               caption=L("config.txt → на карту в папку PROYAVKA", "config.txt → onto the card, into the PROYAVKA folder"))
    if FTP_ROOT_CERT.exists():
        with open(FTP_ROOT_CERT, "rb") as f:
            tg("sendDocument", files={"document": ("cacert.pem", f)}, chat_id=CHAT_ID,
               caption=L("cacert.pem → в корень карты, для камер с FTP", "cacert.pem → root of the card, for FTP cameras"))


def on_callback(cb, state):
    data = cb["data"]
    mid = cb.get("message", {}).get("message_id")
    parts = data.split(":")
    kind = parts[0]
    dev = L("Проявляю…", "Developing…")
    notes = {"p": dev, "cp": dev, "s": dev, "t": dev, "l": dev, "ls": dev,
             "f": L("Готовлю файл, пришлю в чат", "Preparing the file, will send it to the chat"),
             "c": L("Собираю лист…", "Building the sheet…"), "dely": L("Удаляю", "Deleting")}
    safe("answerCallbackQuery", callback_query_id=cb["id"], text=notes.get(kind))
    if kind == "x":
        return
    if kind == "cx":
        safe("deleteMessage", chat_id=CHAT_ID, message_id=mid)
        return
    if kind == "g":
        img, kb, cap = gallery_page(int(parts[1]))
        media = {"type": "photo", "media": "attach://f", "caption": cap}
        safe("editMessageMedia", files={"f": ("g.jpg", jpeg(img, 88))}, chat_id=CHAT_ID,
             message_id=mid, media=media, reply_markup=kb)
        return
    if kind == "d":
        key = canon(parts[1])
        if key != "auto" and key not in PRESETS:
            return
        state["default"] = key
        save_state(state)
        label = L("Авто по ситуации", "Auto by scene") if key == "auto" else pname(key)
        safe("editMessageText", chat_id=CHAT_ID, message_id=mid,
             text=L("Новые кадры", "New frames") + f": {label}", reply_markup=default_kb(state))
        return

    ph = get(int(parts[1]))
    if not ph:
        return
    if kind == "m":
        safe("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid, reply_markup=preset_kb(ph))
    elif kind == "b":
        safe("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid, reply_markup=main_kb(ph))
    elif kind == "lm":
        safe("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid, reply_markup=leak_kb(ph))
    elif kind == "l":
        apply_changes(ph, {"leak": parts[2]})
    elif kind == "ls":
        apply_changes(ph, {"leak_shift": 1})
    elif kind in ("p", "cp"):
        if kind == "cp":
            safe("deleteMessage", chat_id=CHAT_ID, message_id=mid)
        apply_changes(ph, {"preset": parts[2]})
    elif kind == "s":
        i = STRENGTHS.index(ph["strength"]) if ph["strength"] in STRENGTHS else 3
        i = max(0, min(len(STRENGTHS) - 1, i + (1 if parts[2] == "+" else -1)))
        if STRENGTHS[i] != ph["strength"]:
            apply_changes(ph, {"strength": STRENGTHS[i]})
    elif kind == "t" and parts[2] in ("stamp", "frame"):
        apply_changes(ph, {parts[2]: not ph[parts[2]]})
    elif kind == "f":
        export_photo(ph["id"])
    elif kind == "c":
        NET.submit(send_contact, ph)
    elif kind == "o":
        send_new(ph)
    elif kind == "del":       # сначала спросить: удаление стирает и файлы
        safe("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid, reply_markup={"inline_keyboard": [
            [btn(L("🗑 Да, удалить", "🗑 Yes, delete"), f"dely:{ph['id']}"), btn(L("← Нет", "← No"), f"b:{ph['id']}")]]})
    elif kind == "dely":
        hide_photo(ph)


def send_contact(ph):
    path = TMP / f"contact_{ph['id']}.jpg"
    try:
        FAST.submit(job_contact, ph, str(path)).result(timeout=300)
        with open(path, "rb") as f:
            tg("sendPhoto", files={"photo": ("c.jpg", f)}, chat_id=CHAT_ID,
               caption=f"#{ph['id']}: " + L("все плёнки. Нажми нужную — применю к кадру.", "all films. Tap one to apply it to the frame."),
               reply_to_message_id=ph["msg_id"], reply_markup=preset_kb(ph, prefix="cp"))
    except Exception as e:
        safe("sendMessage", chat_id=CHAT_ID, text=L("Не смог собрать лист", "Could not build the sheet") + f": {e}")
    finally:
        remove(str(path))


def download_tg_file(file_id, name):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
    with requests.get(url, timeout=(10, 120), stream=True) as r:
        r.raise_for_status()
        with open(INCOMING / name, "wb") as f:
            shutil.copyfileobj(r.raw, f)


def handle_updates(state):
    try:
        updates = tg("getUpdates", offset=state.get("offset", 0), timeout=5)
    except Exception as e:
        log.warning("getUpdates: %s", e)
        time.sleep(3)
        return
    for u in updates:
        state["offset"] = u["update_id"] + 1
        save_state(state)
        try:
            if "callback_query" in u:
                cb = u["callback_query"]
                if cb["from"]["id"] == CHAT_ID:
                    on_callback(cb, state)
                continue
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("id") != CHAT_ID:
                continue
            if "document" in msg:
                d = msg["document"]
                download_tg_file(d["file_id"], d.get("file_name") or f"tg_{u['update_id']}.jpg")
            elif "photo" in msg:
                download_tg_file(msg["photo"][-1]["file_id"], f"tg_{u['update_id']}.jpg")
            elif "text" in msg:
                on_text(msg["text"], state)
        except Exception as e:
            log.exception("update failed")
            safe("sendMessage", chat_id=CHAT_ID, text=L("Ошибка", "Error") + f": {e}")


# ================= синхронизация и приём =================
STAGING = BASE / "staging"
STAGING.mkdir(parents=True, exist_ok=True)
VPS_EVENTS = queue.Queue()
WATCH_ALIVE = threading.Event()
PARTIAL_GRACE = 600   # недокачанный файл старше 10 минут всё равно забираем (камера так и не дослала)


def file_complete(path):
    """JPEG целый, если в конце есть маркер FFD9. Защита от обрыва связи посреди загрузки с камеры.
    a6300 добивает файл нулями после FFD9 (~12 КБ), их отбрасываем."""
    if Path(path).suffix.lower() not in (".jpg", ".jpeg"):
        return True
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return size > 1024 and b"\xff\xd9" in f.read().rstrip(b"\x00")[-64:]
    except OSError:
        return False


def fetch_names(names, ages=None):
    """Забрать файлы с VPS, проверить и только потом удалить их там."""
    names = [n for n in dict.fromkeys(names) if n and "/" not in n and Path(n).suffix.lower() in EXTS]
    if not names:
        return
    lst = BASE / "fetch.txt"
    lst.write_text("\n".join(names) + "\n")
    src = ["rsync", "-a", "--files-from", str(lst)] + ([REMOTE_DIR] if LOCAL else
                                                      ["-e", " ".join(SSH_CMD), f"{VPS}:{REMOTE_DIR}"])
    res = subprocess.run(src + [str(STAGING) + "/"], capture_output=True, text=True, timeout=600)
    if res.returncode not in (0, 23, 24):   # 23/24: часть файлов уже исчезла — не страшно
        log.warning("rsync: %s", res.stderr.strip()[-300:])
    done = []
    for n in names:
        p = STAGING / n
        if not p.exists():
            continue
        old_enough = ages is not None and ages.get(n, 0) > PARTIAL_GRACE
        if file_complete(p) or old_enough:
            os.replace(p, INCOMING / n)
            done.append(n)
        else:
            remove(str(p))
            log.info("%s недокачан, ждём, пока камера дошлёт", n)
    if done:
        rm = "cd " + shlex.quote(REMOTE_DIR) + " && rm -f -- " + " ".join(shlex.quote(n) for n in done)
        subprocess.run(remote(rm), capture_output=True, text=True, timeout=30)
        log.info("получено с VPS: %s", ", ".join(done))


def fetch_from_vps():
    """Подстраховочный опрос: всё, что лежит на VPS дольше SETTLE секунд."""
    find = f"find {shlex.quote(REMOTE_DIR)} -maxdepth 1 -type f ! -newermt '{SETTLE} seconds ago' -printf '%T@ %f\\n'"
    try:
        res = subprocess.run(remote(find), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        log.warning("ssh timeout")
        return
    if res.returncode != 0:
        log.warning("ssh find failed: %s", res.stderr.strip())
        return
    now = time.time()
    ages = {}
    for line in res.stdout.splitlines():
        ts, _, name = line.partition(" ")
        try:
            ages[name] = now - float(ts)
        except ValueError:
            pass
    fetch_names(list(ages), ages)


def vps_watch():
    """Постоянное соединение с VPS: inotifywait сообщает о файле, как только FTP закончил его писать."""
    cmd = remote(f"inotifywait -m -q -e close_write -e moved_to --format '%f' {shlex.quote(REMOTE_DIR)}")
    while True:
        started = time.time()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            WATCH_ALIVE.set()
            log.info("VPS: мгновенные уведомления включены")
            for line in p.stdout:
                VPS_EVENTS.put(line.strip())
            p.wait()
            err = p.stderr.read().strip()
            if "not found" in err:
                log.warning("на VPS нет inotifywait (apt install inotify-tools), работаю опросом")
            elif err:
                log.warning("VPS watch: %s", err[-200:])
        except Exception:
            log.exception("vps watch")
        WATCH_ALIVE.clear()
        time.sleep(5 if time.time() - started > 30 else 30)


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


def find_duplicate(f, fp):
    row = q("SELECT id FROM photos WHERE fp=? LIMIT 1", (fp,))
    if row:
        return row[0]["id"]
    # кадры из прежних версий без отпечатка: сравниваем имя файла и время съёмки
    try:
        taken, _ = read_exif(Image.open(f))
    except Exception:
        taken = None
    if taken:
        row = q("SELECT id FROM photos WHERE fp IS NULL AND name=? AND taken=? LIMIT 1", (f.name, taken))
        if row:
            return row[0]["id"]
    return None


def ingest(f, state):
    fp = fingerprint(f)
    try:
        return _ingest(f, state, fp)
    finally:
        with PENDING_LOCK:
            PENDING_FP.discard(fp)


def _ingest(f, state, fp):
    t0 = time.time()
    dup = find_duplicate(f, fp)
    if dup:
        remove(str(f))
        log.info("%s — повтор кадра #%d, пропускаю", f.name, dup)
        return False
    tmp_work = WORK / f"incoming_{f.stem}.jpg"
    taken, iso, auto_key, reason = FAST.submit(job_prepare, str(f), str(tmp_work), WORK_EDGE).result(timeout=300)
    default = state.get("default", "auto")
    preset = auto_key if default == "auto" else default
    now = time.time()
    pid = run("INSERT INTO photos(name, taken, iso, auto_key, auto_reason, preset, created, rev, rendered_rev, updated, fp) "
              "VALUES (?,?,?,?,?,?,?,1,0,?,?)", (f.name, taken, iso, auto_key, reason, preset, now, now, fp))
    src = ORIGINALS / f"{pid}_{f.name}"
    work = WORK / f"{pid}.jpg"
    os.replace(tmp_work, work)
    shutil.move(str(f), src)
    upd(pid, src=str(src), work=str(work))
    schedule_view(pid, prio=1)   # после отрисовки кадр сам уйдёт в чат
    log.info("#%d %s → %s, подготовка %.1fs", pid, f.name, preset, time.time() - t0)
    return True


DUP_REPORT = {"n": 0, "since": 0.0}


def process_incoming(state):
    for f in sorted(INCOMING.iterdir()):
        if not f.is_file() or f.suffix.lower() not in EXTS:
            continue
        try:
            if ingest(f, state) is False:
                DUP_REPORT["n"] += 1
                DUP_REPORT["since"] = time.time()
        except Exception as e:
            log.exception("failed on %s", f.name)
            hint = L("\nЕсли это HIF — переключи камеру на JPEG.", "\nIf this is HIF, switch the camera to JPEG.") if f.suffix.lower() in (".hif", ".heif", ".heic") else ""
            safe("sendMessage", chat_id=CHAT_ID, text=L("Не смог обработать", "Could not process") + f" {f.name}: {e}{hint}")
            if f.exists():
                shutil.move(str(f), ORIGINALS / f"failed_{f.name}")
    # одно сообщение на пачку: когда повторы перестали приходить хотя бы на 20 секунд
    if DUP_REPORT["n"] and time.time() - DUP_REPORT["since"] > 20:
        n = DUP_REPORT["n"]
        DUP_REPORT["n"] = 0
        word = "повтор" if n % 10 == 1 and n % 100 != 11 else ("повтора" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "повторов")
        safe("sendMessage", chat_id=CHAT_ID, text=L(f"Пропущено {n} {word}: эти кадры уже были в ленте или удалены.",
                                                    f"Skipped {n} duplicate(s): these frames are already in the feed or were deleted."))


def ingest_loop(state):
    """Отдельный поток: забирает кадры с VPS по уведомлениям, с подстраховочным опросом."""
    last_poll = 0.0
    while True:
        try:
            names = []
            try:
                names.append(VPS_EVENTS.get(timeout=1))
                time.sleep(0.3)                      # соберём пачку, если кадров несколько
                while True:
                    names.append(VPS_EVENTS.get_nowait())
            except queue.Empty:
                pass
            if names:
                fetch_names(names)
            interval = POLL_BACKUP if WATCH_ALIVE.is_set() else POLL
            if time.time() - last_poll >= interval:
                fetch_from_vps()
                last_poll = time.time()
            process_incoming(state)
        except Exception:
            log.exception("ingest loop")
            time.sleep(1)


# ================= Mini App: веб-сервер =================
SESSIONS = {}          # token -> срок годности
SESSION_TTL = 12 * 3600


def check_init_data(init_data):
    """Проверка подписи Telegram: открыть ленту можешь только ты."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return False
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return False
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return False
    try:
        return json.loads(pairs.get("user", "{}")).get("id") == CHAT_ID
    except ValueError:
        return False


def photo_json(ph):
    v = int(os.path.getmtime(ph["view"])) if has(ph.get("view")) else 0
    return {"id": ph["id"], "taken": ph["taken"], "iso": ph["iso"],
            "preset": ph["preset"], "preset_name": pname(ph["preset"]),
            "auto_key": ph["auto_key"], "auto_reason": ph["auto_reason"],
            "strength": ph["strength"], "stamp": bool(ph["stamp"]), "frame": bool(ph["frame"]),
            "leak": (ph.get("leak_kind") or "edge") if ph["leak"] else "",
            "leak_seed": int(ph.get("leak_seed") or 0), "archived": not has(ph["work"]), "original": has(ph["src"]),
            "ready": bool(v), "v": v, "pending": (ph["rev"] or 0) != (ph["rendered_rev"] or 0),
            "exporting": EXPORTING.get(ph["id"], 0) > 0, "hidden": bool(ph["hidden"])}


def preview_file(ph, key, strength, leak="", lseed=0):
    tag = f"_{leak}{lseed}" if leak else ""
    path = PREVIEWS / f"{ph['id']}_{key}_{strength}{tag}.jpg"
    if path.exists():
        return path
    snap = dict(ph, leak_seed=lseed)
    return Path(FAST.submit(job_preview, snap, key, strength, str(path), leak).result(timeout=120))


def sniff_ext(head):
    """Тип файла по первым байтам, а не по имени: телефоны называют файлы как угодно."""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"):
        return ".heic"
    return None


BATCH_MAX = 500


def batch_ids(data):
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BATCH_MAX:
        raise ValueError(L(f"выбери от 1 до {BATCH_MAX} кадров", f"select 1 to {BATCH_MAX} frames"))
    try:
        ids = list(dict.fromkeys(int(i) for i in ids))
    except (TypeError, ValueError):
        raise ValueError(L("неверный список кадров", "invalid frame list"))
    marks = ",".join("?" * len(ids))
    return [r["id"] for r in q(f"SELECT id FROM photos WHERE hidden=0 AND id IN ({marks}) ORDER BY id", ids)]


def batch_edit(ids, changes):
    """Плёнка, засвет, сила для многих кадров: в базу сразу, рисуются очередью (срочность 1)."""
    if not isinstance(changes, dict):
        raise ValueError(L("нет изменений", "no changes"))
    changes = {k: v for k, v in changes.items() if k in ("preset", "strength", "leak")}
    if not changes:
        raise ValueError(L("нет изменений", "no changes"))
    if "preset" in changes and changes["preset"] != "auto" and canon(str(changes["preset"])) not in (*PRESETS, "original"):
        raise ValueError(L("неизвестная плёнка", "unknown film"))
    if "strength" in changes and changes["strength"] not in STRENGTHS:
        raise ValueError(L("неверная сила", "invalid strength"))
    if "leak" in changes and changes["leak"] not in ("", None, *LEAKS):
        raise ValueError(L("неизвестный засвет", "unknown light leak"))
    parts = []
    if "preset" in changes:
        parts.append(L("авто по ситуации", "auto by scene") if changes["preset"] == "auto" else pname(canon(changes["preset"])))
    if "strength" in changes:
        parts.append(L("сила", "strength") + f" {changes['strength']}%")
    if "leak" in changes:
        parts.append(L("засвет", "leak") + f": {LEAKS[changes['leak']][0]}" if changes["leak"] in LEAKS
                     else L("без засвета", "no leak"))
    live = [pid for pid in ids if has((get(pid) or {}).get("work"))]
    bid = batch_track(live, ", ".join(parts))   # до постановки в очередь: быстрый кадр не должен проскочить мимо учёта
    queued = 0
    for pid in live:
        ph = get(pid)
        try:
            before = ph["rev"]
            after = apply_changes(ph, changes, prio=1)
        except RuntimeError:                      # успели заархивировать
            batch_step(pid, "gone")
            continue
        if after["rev"] == before:                # и так уже такой — рисовать нечего
            batch_step(pid, "gone")
        else:
            queued += 1
    return {"ok": True, "queued": queued, "same": len(live) - queued, "skipped": len(ids) - len(live), "batch": bid}


def batch_action(data):
    """Действия над выбранными кадрами из «Проявки»: правка, удаление, файлы."""
    action = data.get("action")
    ids = batch_ids(data)
    if action == "edit":
        return batch_edit(ids, data.get("changes"))
    if action == "delete":
        return {"ok": True, "done": delete_photos(ids)}
    if action == "files":
        sent = 0
        for pid in ids:
            ph = get(pid)
            if ph and (has(ph["work"]) or has(ph["src"])):
                export_photo(pid)
                sent += 1
        return {"ok": True, "done": sent, "skipped": len(ids) - sent}
    raise ValueError(L("неизвестное действие", "unknown action"))


PENDING_FP = set()          # отпечатки принятых загрузок, которые ещё не дошли до базы
PENDING_LOCK = threading.Lock()


def receive_upload(stream, length, name):
    """Своё фото из телефона («+» в «Проявке»): принять потоком, проверить и отдать в обычный приём."""
    if length <= 0:
        raise ValueError(L("пустой файл", "empty file"))
    if length > UPLOAD_MAX:
        raise ValueError(L(f"файл больше {UPLOAD_MAX // 1048576} МБ", f"file is larger than {UPLOAD_MAX // 1048576} MB"))
    tmp = TMP / f"up_{secrets.token_hex(8)}.part"
    h = hashlib.sha1()
    hashed, left, head = 0, length, b""
    try:
        with open(tmp, "wb") as f:
            while left:
                chunk = stream.read(min(left, 262144))
                if not chunk:
                    raise ValueError(L("загрузка оборвалась", "upload was interrupted"))
                if len(head) < 16:
                    head = (head + chunk)[:16]
                if hashed < 262144:            # тот же отпечаток, что у кадров с камеры (fingerprint)
                    part = chunk[:262144 - hashed]
                    h.update(part)
                    hashed += len(part)
                f.write(chunk)
                left -= len(chunk)
        ext = sniff_ext(head)
        if not ext:
            raise ValueError(L("это не фото (нужен JPEG, HEIC, PNG или WebP)", "not a photo (JPEG, HEIC, PNG or WebP expected)"))
        fp = f"{h.hexdigest()}:{length}"
        dup = q("SELECT id, hidden FROM photos WHERE fp=? LIMIT 1", (fp,))
        if dup:     # удалённые кадры тоже помнятся по отпечатку — повторная загрузка их не вернёт
            return {"ok": True, "duplicate": dup[0]["id"], "deleted": bool(dup[0]["hidden"])}
        with PENDING_LOCK:      # тот же файл уже принят и ждёт обработки (выбрали одно фото дважды)
            if fp in PENDING_FP:
                return {"ok": True, "duplicate": -1, "deleted": False}
            PENDING_FP.add(fp)
        stem = "".join(c for c in Path(name).stem if c.isalnum() or c in "-_.")[:40] or "photo"
        dst = INCOMING / f"{stem}{ext}"
        while dst.exists():
            dst = INCOMING / f"{stem}_{secrets.token_hex(2)}{ext}"
        try:
            os.replace(tmp, dst)
        except OSError:
            with PENDING_LOCK:
                PENDING_FP.discard(fp)
            raise
        VPS_EVENTS.put("")                     # разбудить приём, не ждать секунду
        return {"ok": True, "duplicate": None}
    finally:
        remove(str(tmp))


class Handler(BaseHTTPRequestHandler):
    server_version = "filmbot"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def js(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False))

    def err(self, code, text):
        self.js({"error": text}, code)

    def authed(self, qs):
        tok = self.headers.get("X-Token") or (qs.get("s") or [""])[0]
        exp = SESSIONS.get(tok)
        now = time.time()
        if not tok or exp is None or exp <= now:
            return False
        if exp - now < SESSION_TTL - 600:      # пока «Проявкой» пользуются, сессия продлевается сама
            SESSIONS[tok] = now + SESSION_TTL
        return True

    def file(self, path):
        if not has(path):
            return self.err(404, L("нет файла", "no file"))
        with open(path, "rb") as f:
            self.send(200, f.read(), "image/jpeg", "private, max-age=31536000, immutable")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ValueError(L("слишком большой запрос", "request too large"))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)
        try:
            if not parts:
                if not WEBAPP_HTML.exists():
                    return self.err(500, "webapp.html not found next to filmbot.py")
                page = WEBAPP_HTML.read_bytes().replace(b'<html lang="ru"', f'<html lang="{LANG}"'.encode(), 1)
                return self.send(200, page, "text/html; charset=utf-8")
            if not self.authed(qs):
                return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
            if parts == ["api", "presets"]:
                items = [{"key": "original", "name": L("Оригинал", "Original"), "when": L("без обработки", "unprocessed")}]
                items += [{"key": k, "name": p["name"], "when": p["when"], "desc": p["desc"]} for k, p in PRESETS.items()]
                leaks = [{"key": k, "name": v[0], "desc": v[1]} for k, v in LEAKS.items()]
                return self.js({"presets": items, "strengths": STRENGTHS, "leaks": leaks})
            if parts == ["api", "updates"]:
                since = float((qs.get("since") or ["0"])[0])
                now = time.time()
                rows = q("SELECT * FROM photos WHERE updated > ? ORDER BY id DESC LIMIT 500", (since,))
                total = q("SELECT COUNT(*) AS n FROM photos WHERE hidden=0")[0]["n"]
                return self.js({"now": now, "total": total, "jobs": jobs_in_work(),
                                "photos": [photo_json(r) for r in rows]})
            if parts == ["api", "photos"]:
                off = int((qs.get("offset") or ["0"])[0])
                lim = min(120, int((qs.get("limit") or ["60"])[0]))
                total = q("SELECT COUNT(*) AS n FROM photos WHERE hidden=0")[0]["n"]
                rows = q("SELECT * FROM photos WHERE hidden=0 ORDER BY taken DESC, id DESC LIMIT ? OFFSET ?", (lim, off))
                return self.js({"total": total, "photos": [photo_json(r) for r in rows]})
            if len(parts) == 3 and parts[0] == "img" and parts[1] in ("thumb", "view"):
                ph = get(int(parts[2]))
                return self.file(ph and ph[parts[1]])
            if len(parts) == 4 and parts[:2] == ["img", "preview"]:
                ph = get(int(parts[2]))
                key = canon(parts[3])
                strength = int((qs.get("st") or ["100"])[0])
                leak = (qs.get("lk") or [""])[0]
                lseed = int((qs.get("ls") or ["0"])[0])
                if (not ph or (key != "original" and key not in PRESETS) or strength not in STRENGTHS
                        or (leak and leak not in LEAKS)):
                    return self.err(404, L("нет такого превью", "no such preview"))
                if not has(ph["work"]):
                    return self.err(410, L("кадр в архиве", "frame is archived"))
                return self.file(str(preview_file(ph, key, strength, leak, lseed)))
            return self.err(404, L("не найдено", "not found"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("GET %s", self.path)
            self.err(500, str(e))

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)
        try:
            if parts == ["api", "upload"]:      # тело — сам файл, а не JSON, поэтому до self.body()
                if not self.authed(qs):
                    # дочитать и выбросить: иначе соединение рвётся и вместо «войди заново» человек видит «нет связи»
                    left = min(int(self.headers.get("Content-Length") or 0), UPLOAD_MAX)
                    while left > 0:
                        chunk = self.rfile.read(min(left, 262144))
                        if not chunk:
                            break
                        left -= len(chunk)
                    return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
                return self.js(receive_upload(self.rfile, int(self.headers.get("Content-Length") or 0),
                                              (qs.get("name") or [""])[0]))
            data = self.body()
            if parts == ["api", "auth"]:
                if not check_init_data(data.get("initData", "")):
                    return self.err(403, L("открой ленту из своего бота в Telegram", "open the feed from your bot in Telegram"))
                now = time.time()
                for k in [k for k, v in SESSIONS.items() if v < now]:
                    SESSIONS.pop(k, None)
                # повторный вход из уже открытой ленты продлевает прежний токен: на нём ссылки на все картинки
                old = str(data.get("token") or "")
                tok = old if len(old) >= 32 and old not in SESSIONS else secrets.token_urlsafe(24)
                SESSIONS[tok] = now + SESSION_TTL
                return self.js({"token": tok})
            if not self.authed(qs):
                return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
            if parts == ["api", "batch"]:
                return self.js(batch_action(data))
            if len(parts) >= 3 and parts[:2] == ["api", "photo"]:
                ph = get(int(parts[2]))
                if not ph or ph["hidden"]:
                    return self.err(404, L("кадр не найден", "frame not found"))
                action = parts[3] if len(parts) > 3 else "edit"
                if action == "edit":
                    return self.js(photo_json(apply_changes(ph, data, sync_tg=False)))
                if action == "file":
                    export_photo(ph["id"])
                    return self.js(photo_json(get(ph["id"])))
                if action == "hide":
                    hide_photo(ph)
                    return self.js({"ok": True})
            return self.err(404, L("не найдено", "not found"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, RuntimeError) as e:
            self.err(400, str(e))
        except Exception as e:
            log.exception("POST %s", self.path)
            self.err(500, str(e))


def backfill_fingerprints():
    rows = q("SELECT id, src FROM photos WHERE fp IS NULL AND src IS NOT NULL")
    for r in rows:
        if has(r["src"]):
            try:
                upd(r["id"], fp=fingerprint(r["src"]))
            except OSError:
                pass
    if rows:
        log.info("отпечатки посчитаны для %d кадров", len(rows))


def backfill_views():
    """Кадрам от прошлых версий дорисовать картинки для «Проявки»."""
    for r in q("SELECT id FROM photos WHERE view IS NULL AND work IS NOT NULL AND hidden=0 ORDER BY id DESC"):
        run("UPDATE photos SET rev=rev+1 WHERE id=?", (r["id"],))
        schedule_view(r["id"], prio=2, chat=False)   # только картинка для «Проявки», в чате всё уже есть


def start_web():
    srv = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web app on 127.0.0.1:%d", WEB_PORT)


# ================= состояние и запуск =================
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def main():
    init_db()
    state = load_state()
    state.setdefault("default", "auto")
    state["default"] = canon(state["default"])
    if state["default"] != "auto" and state["default"] not in PRESETS:
        state["default"] = "auto"
    state.pop("preset", None)
    save_state(state)
    safe("setMyCommands", commands=[
        {"command": "gallery", "description": L("Лента кадров в чате", "Frame feed in the chat")},
        {"command": "today", "description": L("Альбом за сегодня", "Today's album")},
        {"command": "film", "description": L("Плёнка по умолчанию", "Default film")},
        {"command": "storage", "description": L("Сколько места занято", "Storage used")},
        {"command": "camera", "description": L("Настройка камеры: файлы и инструкция", "Camera setup: files and guide")},
        {"command": "help", "description": L("Как пользоваться", "How to use")},
    ])
    if WEBAPP_URL:
        safe("setChatMenuButton", chat_id=CHAT_ID,
             menu_button={"type": "web_app", "text": APP_NAME, "web_app": {"url": WEBAPP_URL}})
    for p in TMP.iterdir():
        remove(str(p))
    threading.Thread(target=dispatcher, daemon=True, name="dispatcher").start()
    init_pools()
    threading.Thread(target=tg_worker, daemon=True, name="tg").start()
    start_web()
    threading.Thread(target=vps_watch, daemon=True, name="vps-watch").start()
    threading.Thread(target=ingest_loop, args=(state,), daemon=True, name="ingest").start()
    backfill_fingerprints()
    backfill_views()
    log.info("filmbot v5.0 started (%s)", "локально" if LOCAL else VPS)
    last_clean = 0.0
    while True:
        handle_updates(state)       # главный поток занят только кнопками бота
        now = time.time()
        if now - last_clean >= 3600:
            try:
                cleanup()
            except Exception:
                log.exception("cleanup failed")
            last_clean = now


if __name__ == "__main__":
    main()
