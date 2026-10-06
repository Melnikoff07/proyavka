#!/usr/bin/env python3
"""
filmbot v5 — камера → сервер-приёмник → домашний компьютер → плёночный лук → Telegram + Mini App «Проявка».
Настройки — переменные окружения (config.env), их пишет мастер setup.py.

Бот: кнопки под каждой фоткой (плёнка, сила, дата/рамка/засвет, сравнение, файл).
Mini App: лента-контактный лист по дням, просмотр со свайпами и живыми превью плёнок.
Хранилище чистится само: старые оригиналы и рабочие копии удаляются по лимитам.
"""
import base64
import collections
import hashlib
import hmac
import importlib.util
import io
import itertools
import json
import logging
import math
import os
import posixpath
import queue
import re
import shlex
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, urlparse
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

# «Бомба» в картинке: крошечный файл, который при разборе раздувается в гигабайты памяти.
# Больше MAX_MEGAPIXELS — отказ сразу при открытии (у самых больших камер ~100 Мп).
Image.MAX_IMAGE_PIXELS = int(float(os.environ.get("MAX_MEGAPIXELS", "120")) * 1e6)
warnings.simplefilter("error", Image.DecompressionBombWarning)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

# ================= настройки =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()    # пусто — «Проявка» без Telegram (только приложение)
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
LANG = "en" if os.environ.get("LANGUAGE", "ru").lower().startswith("en") else "ru"   # язык по умолчанию: ru / en
_CTX = threading.local()     # язык того, кому сейчас отвечаем (у каждого пользователя свой)


def cur_lang():
    return getattr(_CTX, "lang", None) or LANG


class Bi(str):
    """Строка на двух языках. Значение — язык текущего пользователя; tr() выбирает заново
    (для строк, созданных один раз при запуске: названия засветов, описания плёнок, кнопки меню)."""
    def __new__(cls, ru, en):
        s = super().__new__(cls, en if cur_lang() == "en" else ru)
        s.ru, s.en = ru, en
        return s

    def __reduce__(self):
        return Bi, (self.ru, self.en)


def L(ru, en):
    """Строка интерфейса на языке текущего пользователя."""
    return Bi(ru, en)


def tr(s):
    return (s.en if cur_lang() == "en" else s.ru) if isinstance(s, Bi) else s


class speak:
    """with speak(uid): тексты в этом потоке — на языке этого пользователя."""
    def __init__(self, uid):
        self.lang = user_lang(uid)

    def __enter__(self):
        self.prev = getattr(_CTX, "lang", None)
        _CTX.lang = self.lang

    def __exit__(self, *exc):
        _CTX.lang = self.prev


APP_NAME = L("Проявка", "Proyavka")

INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP = (
    BASE / d for d in ("incoming", "originals", "work", "thumbs", "views", "previews", "tmp"))
for d in (INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP):
    d.mkdir(parents=True, exist_ok=True)
WEBAPP_HTML = Path(__file__).resolve().parent / "webapp.html"
DB_PATH = BASE / "filmbot.db"
STATE_FILE = BASE / "state.json"

# Пользователи, пришедшие без Telegram (приложение, приглашение кодом), получают номера с WEB_BASE: Telegram до них
# не дорастёт ещё долго, а папки, FTP-логины и всё, что завязано на номер, работают как раньше.
WEB_BASE = 900_000_000_000_000
EXTS = {".jpg", ".jpeg", ".hif", ".heif", ".heic", ".png", ".webp"}   # png/webp — только свои фото из телефона
# RAW (через LibRaw: pip install rawpy) — включён по умолчанию, RAW_FILES=0 выключает. Если камера шлёт RAW+JPEG, берётся JPEG.
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".dng", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}
RAW_FILES = os.environ.get("RAW_FILES", "1") == "1"
RAW_MISSING = RAW_FILES and importlib.util.find_spec("rawpy") is None   # включили, а библиотеку не поставили
if RAW_MISSING:
    RAW_FILES = False          # иначе каждый RAW падал бы с «No module named 'rawpy'»
RAW_WAIT = 90          # сек: RAW ждёт, не придёт ли JPEG той же съёмки
if RAW_FILES:
    EXTS |= RAW_EXTS
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
    blob = json.dumps({"name": name, "by": author, "p": params_json(p)}, ensure_ascii=False, separators=(",", ":"))
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
    fx = fx_layer(img, p)
    a = ImageChops.screen(img, fx) if fx is not None else img
    a = a.filter(preset_lut(key, pr))
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
                          ("msg_at", "REAL"), ("crop", "TEXT"), ("owner", "INTEGER"),
                          ("deleted_at", "REAL")):
            if col not in cols:
                db.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
        # несколько пользователей: все кадры прежних версий — администратора (того, кто ставил бота)
        db.execute("UPDATE photos SET owner=? WHERE owner IS NULL", (CHAT_ID,))
        db.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, role TEXT DEFAULT 'user', name TEXT, lang TEXT, default_film TEXT DEFAULT 'auto',
            storage_gb REAL, cam_token TEXT, ftp_pass TEXT, created REAL, invited_by INTEGER)""")
        db.execute("""CREATE TABLE IF NOT EXISTS luts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, name TEXT, size INTEGER, created REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS invites(
            code TEXT PRIMARY KEY, created REAL, by INTEGER, used_by INTEGER, used_at REAL)""")
        if "tg" not in {r[1] for r in db.execute("PRAGMA table_info(users)")}:
            db.execute("ALTER TABLE users ADD COLUMN tg INTEGER")       # чат в Telegram (пусто — без Telegram)
        db.execute("""CREATE TABLE IF NOT EXISTS pairs(
            code TEXT PRIMARY KEY, uid INTEGER, exp REAL, kind TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS push_subs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, device INTEGER, endpoint TEXT UNIQUE,
            p256dh TEXT, auth TEXT, created REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, name TEXT, hash TEXT UNIQUE, created REAL, seen REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS albums(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, token TEXT UNIQUE, title TEXT,
            created REAL, updated REAL, views INTEGER DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS album_photos(
            album INTEGER, photo INTEGER, PRIMARY KEY(album, photo))""")
        db.execute("INSERT OR IGNORE INTO users(id, role, lang, created) VALUES (?, 'admin', ?, ?)", (CHAT_ID, LANG, time.time()))
        db.execute("UPDATE users SET role = CASE WHEN id=? THEN 'admin' ELSE 'user' END", (CHAT_ID,))
        db.execute("UPDATE users SET tg=id WHERE tg IS NULL AND id < ?", (WEB_BASE,))   # пришедшие через Telegram
        db.execute("CREATE INDEX IF NOT EXISTS photos_owner ON photos(owner, hidden, taken)")
        db.execute("CREATE INDEX IF NOT EXISTS photos_fp ON photos(fp)")
        # мини-приложение каждые 1–5 секунд спрашивает «что изменилось» и листает ленту — без индексов это полный перебор
        db.execute("CREATE INDEX IF NOT EXISTS photos_updated ON photos(updated)")
        db.execute("CREATE INDEX IF NOT EXISTS photos_feed ON photos(hidden, id)")
        for old, new in OLD_KEYS.items():
            db.execute("UPDATE photos SET preset=? WHERE preset=?", (new, old))
            db.execute("UPDATE photos SET auto_key=? WHERE auto_key=?", (new, old))
        db.commit()
    load_users()
    load_luts()


def q(sql, args=()):
    with DB_LOCK:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def run(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def run_count(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


# ================= пользователи =================
# Бот один, пользователей несколько: администратор (тот, кто ставил) приглашает остальных через /invite.
# У каждого кадра есть владелец; лента, кнопки, «Проявка», экспорт и место на диске — у каждого свои.
ADMIN = CHAT_ID
USER_STORAGE_GB = float(os.environ.get("USER_STORAGE_GB", "5"))   # лимит места для приглашённых (меняется в /users)
DAILY_LIMIT = int(os.environ.get("DAILY_UPLOAD_LIMIT", "300"))     # кадров в сутки у приглашённых; 0 — без лимита
CLEANUP_MINUTES = float(os.environ.get("CLEANUP_MINUTES", "15"))   # как часто проверять лимиты места
INVITE_DAYS = 7
USERS = {}                    # id -> строка таблицы users (кэш, перечитывается при изменениях)
USERS_LOCK = threading.Lock()


def load_users():
    rows = q("SELECT * FROM users")
    with USERS_LOCK:
        USERS.clear()
        USERS.update({r["id"]: r for r in rows})


def user(uid):
    return USERS.get(uid)


def user_lang(uid):
    u = USERS.get(uid)
    return (u and u["lang"]) or LANG


def set_user(uid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    run(f"UPDATE users SET {cols} WHERE id=?", (*kw.values(), uid))
    load_users()


def load_luts():
    rows = q("SELECT id, owner, name FROM luts")
    LUT_NAMES.clear()
    LUT_OWNER.clear()
    for r in rows:
        LUT_NAMES[f"lut{r['id']}"] = r["name"]
        LUT_OWNER[f"lut{r['id']}"] = r["owner"]


def user_luts(uid):
    return q("SELECT * FROM luts WHERE owner=? ORDER BY id", (uid,))


def valid_look(key, owner):
    """Можно ли этому пользователю ставить такую плёнку: встроенные — всем, свой LUT — только владельцу."""
    return key == "original" or key in PRESETS or (is_lut(key) and LUT_OWNER.get(key) == owner)


def add_lut(owner, name, data):
    if len(data) > LUT_MAX_BYTES:
        raise ValueError(L("файл LUT больше 16 МБ", "the LUT file is larger than 16 MB"))
    if len(user_luts(owner)) >= LUT_MAX_COUNT:
        raise ValueError(L(f"уже {LUT_MAX_COUNT} LUT — удали ненужные (/luts)", f"already {LUT_MAX_COUNT} LUTs — delete some (/luts)"))
    size, table = parse_cube(data)
    name = re.sub(r"[\x00-\x1f]", "", Path(name or "LUT").stem).strip()[:32] or "LUT"
    lid = run("INSERT INTO luts(owner, name, size, created) VALUES (?,?,?,?)", (owner, name, size, time.time()))
    d = lut_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"lut{lid}.tmp.npy"
    np.save(tmp, table)
    os.replace(tmp, d / f"lut{lid}.npy")
    (d / f"lut{lid}.json").write_text(json.dumps({"name": name, "size": size}, ensure_ascii=False), encoding="utf-8")
    load_luts()
    log.info("LUT lut%d «%s» (%d³) у %d", lid, name, size, owner)
    return {"key": f"lut{lid}", "name": name, "size": size}


def delete_lut(owner, key):
    """Убрать свой LUT. Кадры с ним переходят на их автоплёнку и перерисовываются."""
    if not is_lut(key) or LUT_OWNER.get(key) != owner:
        raise ValueError(L("нет такого LUT", "no such LUT"))
    rows = q("SELECT id, auto_key FROM photos WHERE owner=? AND preset=?", (owner, key))
    for r in rows:
        run("UPDATE photos SET preset=?, rev=rev+1, updated=? WHERE id=?", (r["auto_key"] or "original", time.time(), r["id"]))
        schedule_view(r["id"], prio=1, uid=owner)
    if (user(owner) or {}).get("default_film") == key:
        set_user(owner, default_film="auto")
    run("DELETE FROM luts WHERE id=? AND owner=?", (int(key[3:]), owner))
    for ext in (".npy", ".json"):
        remove(str(lut_dir(owner) / f"{key}{ext}"))
    for f in PREVIEWS.glob(f"*_{key}_*.jpg"):
        remove(str(f))
    load_luts()
    return len(rows)


# ================= свои плёнки и сообщество =================
# Редактор в «Проявке» собирает плёнку из ползунков; сообщество — каталог community/looks.json в GitHub: сервер
# скачивает его сам (раз в час), так что отдельный сайт не нужен, а читателей каталога GitHub не видит.
COMMUNITY_URL = os.environ.get("COMMUNITY_URL", "https://raw.githubusercontent.com/Melnikoff07/proyavka/main/community/looks.json")
COMMUNITY_REPO = os.environ.get("COMMUNITY_REPO", "Melnikoff07/proyavka")    # куда ведёт «Предложить в каталог»
COMMUNITY_TTL = 3600
COMMUNITY_MAX = 600
COMMUNITY = {"at": 0.0, "looks": []}
COMMUNITY_LOCK = threading.Lock()
COMMUNITY_BUNDLED = Path(__file__).resolve().parent.parent / "community" / "looks.json"


def _write_look(owner, key, name, params, author, src):
    d = lut_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "size": 0, "params": params_json(params), "author": author, "src": src}
    tmp = d / f"{key}.tmp.json"
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{key}.json")


def add_look(owner, name, params, author="", src=""):
    """Новая своя плёнка (из редактора, по коду или из каталога). Лимит общий с LUT."""
    if len(user_luts(owner)) >= LUT_MAX_COUNT:
        raise ValueError(L(f"уже {LUT_MAX_COUNT} своих плёнок и LUT — удали ненужные", f"already {LUT_MAX_COUNT} films and LUTs — delete some"))
    params = clean_params(params)
    name = clean_text(name, 32) or "Look"
    lid = run("INSERT INTO luts(owner, name, size, created) VALUES (?,?,?,?)", (owner, name, 0, time.time()))
    key = f"lut{lid}"
    _write_look(owner, key, name, params, clean_text(author, 40), clean_text(src, 40))
    load_luts()
    log.info("плёнка %s «%s» у %d", key, name, owner)
    return {"key": key, "name": name}


def user_look(owner, key):
    """Своя плёнка для редактора: название, параметры, автор. Чужую или обычный LUT не отдаём."""
    if not is_lut(key) or LUT_OWNER.get(key) != owner:
        raise ValueError(L("нет такой плёнки", "no such film"))
    meta = lut_meta(owner, key)
    p = look_params(owner, key)
    if p is None:
        raise ValueError(L("это LUT-файл, его параметров нет", "this is a LUT file, it has no parameters"))
    return {"key": key, "name": meta.get("name") or LUT_NAMES.get(key) or "Look", "params": params_json(p),
            "author": meta.get("author") or "", "src": meta.get("src") or ""}


def edit_look(owner, key, name, params):
    """Сохранить правку своей плёнки. Кадры с ней перерисовываются, ссылка на плёнку остаётся прежней."""
    cur = user_look(owner, key)
    params = clean_params(params)
    name = clean_text(name, 32) or cur["name"]
    run("UPDATE luts SET name=? WHERE id=? AND owner=?", (name, int(key[3:]), owner))
    _write_look(owner, key, name, params, cur["author"], cur["src"])
    rows = q("SELECT id FROM photos WHERE owner=? AND preset=?", (owner, key))
    for r in rows:
        run("UPDATE photos SET rev=rev+1, updated=? WHERE id=?", (time.time(), r["id"]))
        schedule_view(r["id"], prio=1, uid=owner)
    for f in PREVIEWS.glob(f"*_{key}_*.jpg"):
        remove(str(f))
    load_luts()
    return {"key": key, "name": name, "redrawn": len(rows)}


def look_share(uid, key):
    """Код плёнки и ссылка «Предложить в каталог» (готовая заявка в GitHub — человек сам решает, отправлять ли)."""
    cur = user_look(uid, key)
    author = clean_text((user(uid) or {}).get("name"), 40)
    code = look_code(cur["name"], author, clean_params(cur["params"]))
    body = f"Author: {author or '—'}\nName: {cur['name']}\n\n```\n{code}\n```\n"
    url = (f"https://github.com/{COMMUNITY_REPO}/issues/new?title={quote('Look: ' + cur['name'])}&body={quote(body)}"
           if COMMUNITY_REPO else "")
    return {"code": code, "suggest_url": url, "author": author}


def _community_entries(raw):
    out = []
    for e in (raw.get("looks") if isinstance(raw, dict) else None) or []:
        try:
            cid = str(e["id"])
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", cid):
                continue
            p = clean_params(e["p"])
            desc = e.get("desc") or ""
            if isinstance(desc, dict):
                desc = {k: clean_text(v, 140) for k, v in desc.items() if k in ("ru", "en")}
            else:
                desc = {"ru": clean_text(desc, 140), "en": clean_text(desc, 140)}
            out.append({"id": cid, "name": clean_text(e.get("name"), 32) or cid, "by": clean_text(e.get("by"), 40),
                        "desc": desc, "p": p,
                        "h": hashlib.sha1(json.dumps(params_json(p), sort_keys=True).encode()).hexdigest()[:10]})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue                                     # одна битая запись не должна ронять каталог
        if len(out) >= COMMUNITY_MAX:
            break
    return out


def community_catalog(force=False):
    """Каталог плёнок сообщества: из сети (кэш на час), иначе с диска, иначе копия из репозитория."""
    with COMMUNITY_LOCK:
        if not force and COMMUNITY["looks"] and time.time() - COMMUNITY["at"] < COMMUNITY_TTL:
            return COMMUNITY["looks"]
        cache = BASE / "community.json"
        raw = None
        if COMMUNITY_URL.startswith("https://"):
            try:
                r = requests.get(COMMUNITY_URL, timeout=8, stream=True)
                r.raise_for_status()
                data = r.raw.read(2 * 1024 * 1024 + 1, decode_content=True)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("catalog too large")
                raw = json.loads(data)
                if _community_entries(raw) or raw.get("looks") == []:
                    cache.write_bytes(data)
                else:
                    raw = None
            except (requests.RequestException, ValueError, OSError) as e:
                log.warning("каталог сообщества не скачался: %s", e)
        for src in (cache, COMMUNITY_BUNDLED):
            if raw is not None:
                break
            try:
                raw = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        COMMUNITY["looks"] = _community_entries(raw)
        COMMUNITY["at"] = time.time()
        return COMMUNITY["looks"]


def community_json(uid):
    have = {}
    for r in user_luts(uid):
        src = lut_meta(uid, f"lut{r['id']}").get("src")
        if src:
            have[src] = f"lut{r['id']}"
    sample = None
    for r in q("SELECT id, work FROM photos WHERE owner=? AND hidden=0 AND work IS NOT NULL ORDER BY taken DESC, id DESC LIMIT 5", (uid,)):
        if has(r["work"]):
            sample = r["id"]
            break
    en = user_lang(uid) == "en"
    items = []
    for e in community_catalog():
        d = e["desc"]
        items.append({"id": e["id"], "name": e["name"], "by": e["by"], "h": e["h"], "bw": bool(e["p"]["bw"]),
                      "desc": (d.get("en") if en else d.get("ru")) or d.get("ru") or d.get("en") or "",
                      "installed": have.get(e["id"], "")})
    return {"looks": items, "sample": sample, "repo": COMMUNITY_REPO}


def community_add(uid, cid):
    for e in community_catalog():
        if e["id"] == cid:
            for r in user_luts(uid):                     # уже добавлена — вторую копию не делаем
                if lut_meta(uid, f"lut{r['id']}").get("src") == cid:
                    return {"key": f"lut{r['id']}", "name": r["name"], "again": True}
            return add_look(uid, e["name"], e["p"], e["by"], cid)
    raise ValueError(L("в каталоге нет такой плёнки", "no such film in the catalog"))


def try_look(uid, data):
    """Живой просмотр редактора: кадр пользователя с плёнкой из ползунков (картинка, ничего не сохраняется)."""
    try:
        ph = get(int(data.get("id")))
        strength = int(data.get("strength") or 100)
    except (TypeError, ValueError):
        ph = None
    if not ph or ph["owner"] != uid or ph["hidden"]:
        raise ValueError(L("кадр не найден", "frame not found"))
    if not has(ph["work"]):
        raise ValueError(L("кадр в архиве", "frame is archived"))
    if strength not in STRENGTHS:
        strength = 100
    params = clean_params(data.get("params"))
    return FAST.submit(job_try, dict(ph), params, strength).result(timeout=120)


def community_preview(ph, cid):
    """Плёнка каталога на кадре пользователя; файл-кэш на сутки."""
    for e in community_catalog():
        if e["id"] == cid:
            path = PREVIEWS / f"{ph['id']}_cm{e['h']}_100.jpg"
            if not path.exists():
                FAST.submit(job_try, dict(ph), e["p"], 100, str(path)).result(timeout=120)
            return path
    return None


def storage_limit(uid):
    u = USERS.get(uid) or {}
    if u.get("storage_gb"):
        return u["storage_gb"]
    return STORAGE_GB if uid == ADMIN else USER_STORAGE_GB


def udir(uid, kind):
    """Папка пользователя: у администратора — прежние папки в BASE, у остальных — BASE/users/<id>/."""
    d = (BASE if uid == ADMIN else BASE / "users" / str(uid)) / kind
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
    return d


def get(pid):
    rows = q("SELECT * FROM photos WHERE id=?", (pid,))
    return rows[0] if rows else None


def upd(pid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    run(f"UPDATE photos SET {cols} WHERE id=?", (*kw.values(), pid))


# ================= Telegram =================
# Telegram необязателен: бота может не быть вовсе, а у пользователя может не быть привязанного чата.
# Все отправки идут через tg(): номер пользователя превращается в его чат; некуда — NoChat (safe() её глотает).
class NoChat(Exception):
    pass


def chat_of(uid):
    if not BOT_TOKEN or uid is None:
        return None
    u = USERS.get(uid)
    return uid if u is None else u.get("tg")       # незнакомцу отвечаем в его же чат


def uid_of_tg(tid):
    if not tid:
        return None
    for uid, u in list(USERS.items()):
        if u.get("tg") == tid:
            return uid
    return None


def tg(method, files=None, **params):
    if not BOT_TOKEN:
        raise NoChat(method)
    if "chat_id" in params:
        params["chat_id"] = chat_of(params["chat_id"])
        if params["chat_id"] is None:
            raise NoChat(method)
    sc = params.get("scope")
    if isinstance(sc, dict) and "chat_id" in sc:
        c = chat_of(sc["chat_id"])
        if c is None:
            raise NoChat(method)
        params["scope"] = dict(sc, chat_id=c)
    return tg_send(method, files, **params)


def tg_send(method, files=None, **params):
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    for attempt in range(3):
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=data, files=files, timeout=(10, 120))   # (подключение, ответ)
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
    except NoChat:
        return None
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
        meta.append(L("авто", "auto") + f": {auto_reason(ph)} → {pname(ph['auto_key'])}")
    leak = " · " + L("засвет", "leak") + f": {tr(LEAKS.get(ph.get('leak_kind') or 'edge', ('',))[0])}" if ph["leak"] else ""
    return f"#{ph['id']} · {pname(ph['preset'], ph['owner'])} · {ph['strength']}%{leak}\n" + " · ".join(meta)


def main_kb(ph):
    pid = ph["id"]
    if not has(ph["work"]):
        return {"inline_keyboard": [[btn(L("🗄 В архиве", "🗄 Archived"), "x"), btn(L("🗑 Удалить", "🗑 Delete"), f"del:{pid}")]]}
    on = lambda f: "✅ " if ph[f] else ""
    return {"inline_keyboard": [
        [btn(f"🎞 {pname(ph['preset'], ph['owner'])} ▾", f"m:{pid}"), btn(L("🔍 Сравнить", "🔍 Compare"), f"c:{pid}")],
        [btn("➖", f"s:{pid}:-"), btn(L("Сила", "Strength") + f" {ph['strength']}%", "x"), btn("➕", f"s:{pid}:+")],
        [btn(on("stamp") + L("📅 Дата", "📅 Date"), f"t:{pid}:stamp"), btn(on("frame") + L("🖼 Рамка", "🖼 Frame"), f"t:{pid}:frame"),
         btn(on("leak") + L("✨ Засвет ▾", "✨ Leak ▾"), f"lm:{pid}")],
        [btn(L("⬇️ Файл", "⬇️ File"), f"f:{pid}"), btn(L("🗑 Удалить", "🗑 Delete"), f"del:{pid}")],
    ]}


def preset_kb(ph, prefix="p"):
    pid = ph["id"]
    keys = list(PRESETS) + [f"lut{r['id']}" for r in user_luts(ph["owner"])]
    rows = []
    for i in range(0, len(keys), 3):
        rows.append([btn(("• " if ph["preset"] == k else "") + pname(k), f"{prefix}:{pid}:{k}")
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
    rows = [[btn(("• " if cur == k else "") + tr(LEAKS[k][0]), f"l:{pid}:{k}") for k in keys[i:i + 3]]
            for i in range(0, len(keys), 3)]
    rows.append([btn(("• " if not cur else "") + L("Без засвета", "No leak"), f"l:{pid}:"), btn(L("↻ Сдвинуть", "↻ Shift"), f"ls:{pid}")])
    rows.append([btn(L("← Назад", "← Back"), f"b:{pid}")])
    return {"inline_keyboard": rows}


KB_FEED, KB_TODAY = L("📚 Лента", "📚 Feed"), L("📅 Сегодня", "📅 Today")
KB_FILM, KB_HELP = L("🎞 Плёнка по умолчанию", "🎞 Default film"), L("❓ Помощь", "❓ Help")


def menu():
    return {"keyboard": [[{"text": tr(KB_FEED)}, {"text": tr(KB_TODAY)}],
                         [{"text": tr(KB_FILM)}, {"text": tr(KB_HELP)}]],
            "resize_keyboard": True, "is_persistent": True}


def kb_is(t, kb):
    return t in (kb.ru.lower(), kb.en.lower())


def touch(pid):
    upd(pid, updated=time.time())


def send_new(ph):
    """Прислать кадр новым сообщением (из ленты в чате), по кешу Telegram."""
    if not ph["file_id"]:
        tg("sendMessage", chat_id=ph["owner"], text=L(f"Кадр #{ph['id']} ещё проявляется.", f"Frame #{ph['id']} is still developing."))
        return
    with EDIT_LOCK:
        res = tg("sendPhoto", chat_id=ph["owner"], photo=ph["file_id"], caption=caption(ph), reply_markup=main_kb(ph))
        upd(ph["id"], msg_id=res["message_id"], msg_at=time.time(), file_id=res["photo"][-1]["file_id"])


MSG_DELETE_WINDOW = 47 * 3600     # Telegram даёт боту удалить сообщение только в первые 48 часов


def delete_photos(ids):
    """Убрать кадры из ленты и из чата в корзину. Файлы остаются на диске: кадр можно вернуть через /trash,
    пока не понадобится место, — при нехватке места корзина чистится первой (cleanup).
    Отпечаток (fp) тоже остаётся, поэтому повторная выгрузка того же кадра его не вернёт."""
    now = time.time()
    gone = []
    for pid in ids:
        ph = get(pid)
        if not ph or ph["hidden"]:
            continue
        # rev+1 — рисование, которое уже идёт, не отправит кадр в чат (_view_done это проверяет)
        run("UPDATE photos SET hidden=1, deleted_at=?, file_id=NULL, rev=rev+1, updated=? WHERE id=?", (now, now, pid))
        for p in PREVIEWS.glob(f"{pid}_*.jpg"):      # превью — кэш, их не жалко
            remove(str(p))
        gone.append(ph)
    if gone:
        NET.submit(_delete_messages, gone)
    return len(gone)


def restore_photo(ph):
    """Вернуть кадр из корзины в ленту и в чат."""
    if not has(ph["work"]):
        raise RuntimeError(L("файлы кадра уже удалены, чтобы освободить место", "the frame's files were already deleted to free space"))
    run("UPDATE photos SET hidden=0, deleted_at=NULL, rev=rev+1, updated=? WHERE id=?", (time.time(), ph["id"]))
    schedule_view(ph["id"], prio=0, uid=ph["owner"])     # нарисуется и сам придёт в чат


def purge_files(ph):
    """Стереть файлы кадра из корзины окончательно (строка с отпечатком остаётся)."""
    size = 0
    for k in ("src", "work", "view", "thumb"):
        if ph[k]:
            size += fsize(ph[k])
            remove(ph[k])
    run("UPDATE photos SET src=NULL, work=NULL, view=NULL, thumb=NULL WHERE id=?", (ph["id"],))
    log.info("cleanup: #%d из корзины удалён (%.1f MB)", ph["id"], size / 1e6)
    return size


def _delete_messages(phs):
    by_owner = {}
    for ph in phs:
        if ph["msg_id"]:
            by_owner.setdefault(ph["owner"], []).append(ph)
    for chat, items in by_owner.items():
        with speak(chat):
            _delete_chat_messages(chat, items)


def _delete_chat_messages(chat, phs):
    fresh, old = [], []
    for ph in phs:
        sent = ph.get("msg_at") or ph["created"] or 0
        (fresh if time.time() - sent < MSG_DELETE_WINDOW else old).append(ph["msg_id"])
    for i in range(0, len(fresh), 100):
        chunk = fresh[i:i + 100]
        pace(chat)
        if not safe("deleteMessages", chat_id=chat, message_ids=chunk):
            for mid in list(chunk):              # на всякий случай по одному
                if not safe("deleteMessage", chat_id=chat, message_id=mid):
                    old.append(mid)
                    chunk.remove(mid)
        marks = ",".join("?" * len(chunk))       # сообщения больше нет — при возврате из корзины придёт новое
        if chunk:
            run(f"UPDATE photos SET msg_id=NULL WHERE owner=? AND msg_id IN ({marks})", (chat, *chunk))
    for mid in old:                              # старше 48 часов: удалить нельзя — меняем фото на заглушку
        pace(chat)
        with open(deleted_placeholder(), "rb") as f:
            if not safe("editMessageMedia", files={"f": ("deleted.jpg", f)}, chat_id=chat, message_id=mid,
                        media={"type": "photo", "media": "attach://f", "caption": L("Удалено", "Deleted")},
                        reply_markup={"inline_keyboard": []}):
                safe("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})


def deleted_placeholder():
    path = BASE / f"deleted_{cur_lang()}.jpg"
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


def job_prepare(src, work_path, edge):
    if is_raw(src):
        taken, iso = raw_exif(src)
        im = open_raw(src, edge)
        im.thumbnail((edge, edge), Image.LANCZOS)
        taken = taken or datetime.now().strftime("%Y-%m-%d %H:%M")
        auto_key, reason = auto_pick(im, iso, int(taken[11:13]))
        save_atomic(im, work_path, 95)
        return taken, iso, auto_key, reason
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


def preview_base(ph):
    """Уменьшенный кадр (420 px) — основа всех превью; кадрированное сперва вырезается, потом уменьшается."""
    crop = ph.get("crop")
    base_path = PREVIEWS / f"{ph['id']}_base{crop_tag(crop)}.jpg"
    if not base_path.exists():
        b = Image.open(ph["work"])
        if crop:
            b = crop_img(b, crop)
        else:
            b.draft("RGB", (420, 420))
        b = b.convert("RGB")
        b.thumbnail((420, 420), Image.LANCZOS)
        save_atomic(b, str(base_path), 92)
    return Image.open(base_path).convert("RGB")


def job_preview(ph, key, strength, path, leak=""):
    base = preview_base(ph)
    out = look(base, ph, key, strength, ph["id"])
    if leak:
        out = light_leak(out, leak, leak_seed(ph))
    return save_atomic(out, path, 84)


def job_try(ph, params, strength, path=None):
    """Кадр с плёнкой, которой ещё нет в базе: живой просмотр в редакторе и превью плёнок сообщества.
    Без path — JPEG байтами (редактор не засоряет диск), с path — файлом-кэшем."""
    out = film(preview_base(ph), "custom", strength, ph["id"], params)
    if path:
        return save_atomic(out, path, 84)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=84)
    return buf.getvalue()


def job_source(work, path):
    """Некадрированный кадр без плёнки — для экрана кадрирования."""
    b = Image.open(work)
    b.draft("RGB", (VIEW_EDGE, VIEW_EDGE))
    b = b.convert("RGB")
    b.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    return save_atomic(b, path, 85)


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


def jobs_in_work(uid):
    """Сколько кадров этого пользователя сейчас рисуется или ждёт очереди."""
    with JOB_LOCK:
        pids = set(VIEW_INFLIGHT) | set(RQ_QUEUED) | set(EXPORTING)
    if not pids:
        return 0
    pids = list(pids)[:900]
    marks = ",".join("?" * len(pids))
    rows = q(f"SELECT id FROM photos WHERE owner=? AND id IN ({marks})", (uid, *pids))
    mine = {r["id"] for r in rows}
    with JOB_LOCK:
        return sum(1 for p in mine if p in VIEW_INFLIGHT or p in RQ_QUEUED) + sum(EXPORTING.get(p, 0) for p in mine)


# Очередь отрисовки. Сразу в процессы отдаётся не больше задач, чем они успевают (VIEW_SLOTS): иначе сотня кадров
# из пакетной правки встала бы в очередь пула, и превью плёнок в «Проявке» ждали бы их все.
# Срочность: 0 — правка одного кадра (человек ждёт), 1 — пакетная правка и новые кадры, 2 — фоновая догрузка.
# Внутри одной срочности пользователи обслуживаются по кругу, чтобы один большой пакет не задерживал остальных.
VIEW_SLOTS = max(1, FAST_WORKERS - 1) if FAST_WORKERS > 2 else FAST_WORKERS
RQ_PENDING = {0: {}, 1: {}, 2: {}}    # срочность -> {пользователь: deque[pid]} (порядок ключей = очередь по кругу)
RQ_QUEUED = {}                        # pid -> (срочность, нужен ли чат)
VIEW_CHAT = {}                        # pid в работе -> нужна ли версия для чата
VIEW_PRIO = {}                        # pid в работе -> срочность (с ней же правка уйдёт в чат)


def schedule_view(pid, prio=0, chat=True, uid=None):
    """Перерисовать кадр. Если уже рисуется — дорисуем последнее состояние следом."""
    if uid is None:                      # очередь по кругу между владельцами кадров
        r = q("SELECT owner FROM photos WHERE id=?", (pid,))
        uid = r[0]["owner"] if r else 0
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
    fut = FAST.submit(job_view, ph, str(udir(ph["owner"], "views") / f"{pid}.jpg"),
                      str(udir(ph["owner"], "thumbs") / f"{pid}.jpg"), chat_path)
    fut.add_done_callback(lambda f: EVENTS.put((_view_done, (pid, rev, chat, f))))


def _view_done(pid, rev, chat, fut):
    err = fut.exception()
    result = "fail"
    if err:
        log.warning("view #%d: %s", pid, err)
    else:
        view, thumb = fut.result()
        cur = get(pid)
        if not cur or cur["hidden"]:            # кадр убрали в корзину, пока он рисовался — в чат не слать
            remove(str(TMP / f"chat_{pid}_{rev}.jpg"))
            result = "gone"
        elif cur["rev"] == rev:
            upd(pid, view=view, thumb=thumb, rendered_rev=rev, updated=time.time())
            result = "ok"
            if not cur.get("view") and (cur.get("created") or 0) > time.time() - 600:
                push_soon(cur["owner"])          # новый кадр проявился впервые (не дорисовка старых)
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


def batch_track(pids, text, uid):
    finished = []
    with JOB_LOCK:
        bid = next(_BATCH_SEQ)
        BATCHES[bid] = {"left": set(pids), "pids": list(pids), "ok": 0, "fail": 0, "total": len(pids), "text": text,
                        "uid": uid, "lang": cur_lang()}
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
    _CTX.lang = b["lang"]
    text = L(f"Готово: {n} {plural_ru(n, 'кадр', 'кадра', 'кадров')} → {b['text']}",
             f"Done: {n} frame{'' if n == 1 else 's'} → {b['text']}")
    if b["fail"]:
        text += L(f"\nНе получилось: {b['fail']}", f"\nFailed: {b['fail']}")
    pace(b["uid"])
    safe("sendMessage", chat_id=b["uid"], text=text, disable_notification=True)


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
    with speak(ph["owner"]):
        _export_send(pid, ph, out, fut)


def _export_send(pid, ph, out, fut):
    chat = ph["owner"]
    try:
        err = fut.exception()
        if err:
            safe("sendMessage", chat_id=chat, text=L(f"Не смог экспортировать #{pid}: {err}", f"Could not export #{pid}: {err}"))
            return
        note = None if has(ph["src"]) else L("Оригинал уже удалён для экономии места, это версия для чата.",
                                         "The original was deleted to save space; this is the chat version.")
        cur = get(pid) or ph
        if cur["hidden"]:                       # удалили, пока готовился файл
            return
        with open(out, "rb") as f:
            tg("sendDocument", files={"document": (f"{Path(ph['name']).stem}_{ph['preset']}.jpg", f)},
               chat_id=chat, reply_to_message_id=cur["msg_id"], caption=note)
    except Exception as e:
        log.exception("export upload #%d", pid)
        safe("sendMessage", chat_id=chat, text=L(f"Не смог отправить файл #{pid}: {e}", f"Could not send file #{pid}: {e}"))
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
    r = q("SELECT owner FROM photos WHERE id=?", (pid,))
    if not r or chat_of(r[0]["owner"]) is None:
        return                   # у владельца нет Telegram — кадр живёт только в «Проявке»
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
    chat = ph["owner"]
    pace(chat)
    with speak(chat), EDIT_LOCK, open(path, "rb") as f:
        res = None
        if ph["msg_id"]:
            media = {"type": "photo", "media": "attach://f", "caption": caption(ph)}
            try:
                res = tg("editMessageMedia", files={"f": ("p.jpg", f)}, chat_id=chat,
                         message_id=ph["msg_id"], media=media, reply_markup=main_kb(ph))
            except Exception as e:
                if "not modified" in str(e):
                    return
                log.warning("edit #%d failed (%s), sending new", pid, e)
                f.seek(0)
        if res is None:
            res = tg("sendPhoto", files={"photo": ("p.jpg", f)}, chat_id=chat,
                     caption=caption(ph), reply_markup=main_kb(ph))
            upd(pid, msg_id=res["message_id"], msg_at=time.time())
        upd(pid, file_id=res["photo"][-1]["file_id"])


def older_unsent(pid):
    """Есть ли более ранний новый кадр, ещё не отправленный в чат (не старше 2 минут, чтобы сбойный не держал очередь)."""
    return q("SELECT COUNT(*) AS n FROM photos WHERE id < ? AND msg_id IS NULL AND hidden=0 "
             "AND work IS NOT NULL AND created > ? AND owner=(SELECT owner FROM photos WHERE id=?)",
             (pid, time.time() - 120, pid))[0]["n"] > 0


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
        if not valid_look(k, (cur or ph)["owner"]):
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
    if "crop" in changes:
        fields["crop"] = parse_crop(changes["crop"])
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


def user_usage(uid):
    """Сколько места занимают кадры пользователя: оригиналы, рабочие копии, картинки для просмотра."""
    use = {"src": 0, "work": 0, "pics": 0}
    for r in q("SELECT src, work, view, thumb FROM photos WHERE owner=?", (uid,)):
        use["src"] += fsize(r["src"]) if r["src"] else 0
        use["work"] += fsize(r["work"]) if r["work"] else 0
        use["pics"] += (fsize(r["view"]) if r["view"] else 0) + (fsize(r["thumb"]) if r["thumb"] else 0)
    return use


def _free_space(rows_sql, args, need):
    """Удалять оригиналы, потом рабочие копии, от самых старых, пока need() не скажет «хватит»."""
    for field in ("src", "work"):
        for r in q(rows_sql.format(f=field), args):
            if not need():
                return
            size = fsize(r["p"])
            remove(r["p"])
            upd(r["id"], **{field: None}, updated=time.time())
            yield size
            log.info("cleanup: #%d %s removed (%.1f MB)", r["id"], field, size / 1e6)


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
    # 2) лимит каждого пользователя
    for uid in list(USERS):
        enforce_limit(uid)
    # 3) свободное место на диске — общее, от самых старых кадров всех пользователей
    min_free = MIN_FREE_GB * 1e9
    free = [shutil.disk_usage(BASE).free]
    if free[0] < min_free:
        for f in PREVIEWS.glob("*_full_*.jpg"):     # кэш полных кадров для альбомов — его не жалко
            free[0] += fsize(f)
            remove(f)
    if free[0] < min_free:
        for ph in q("SELECT * FROM photos WHERE hidden=1 AND (src IS NOT NULL OR work IS NOT NULL "
                    "OR view IS NOT NULL OR thumb IS NOT NULL) ORDER BY deleted_at, id"):
            if free[0] >= min_free:
                return
            free[0] += purge_files(ph)
        for size in _free_space("SELECT id, {f} AS p FROM photos WHERE {f} IS NOT NULL ORDER BY id", (),
                                lambda: free[0] < min_free):
            free[0] += size


def enforce_limit(uid):
    """Лимит места пользователя: сначала корзина, потом старые оригиналы, потом рабочие копии."""
    use = user_usage(uid)
    total = [sum(use.values())]
    limit = storage_limit(uid) * 1e9
    if total[0] <= limit:
        return
    for ph in q("SELECT * FROM photos WHERE owner=? AND hidden=1 AND (src IS NOT NULL OR work IS NOT NULL "
                "OR view IS NOT NULL OR thumb IS NOT NULL) ORDER BY deleted_at, id", (uid,)):
        if total[0] <= limit:                # сначала корзина: самое давно удалённое
            break
        total[0] -= purge_files(ph)
    if total[0] <= limit:
        return
    for size in _free_space("SELECT id, {f} AS p FROM photos WHERE owner=? AND {f} IS NOT NULL ORDER BY id",
                            (uid,), lambda: total[0] > limit):
        total[0] -= size
    for p in sorted(udir(uid, "originals").glob("failed_*"), key=lambda x: x.stat().st_mtime):
        if total[0] <= limit:
            break
        total[0] -= fsize(p)
        remove(p)


def storage_text(uid):
    n = q("SELECT COUNT(*) AS n, SUM(src IS NOT NULL) AS o, SUM(work IS NOT NULL) AS w FROM photos "
          "WHERE hidden=0 AND owner=?", (uid,))[0]
    use = user_usage(uid)
    gb = lambda b: (f"{b / 1e9:.1f} " + L("ГБ", "GB")) if b >= 1e9 or storage_limit(uid) >= 1 else (f"{b / 1e6:.0f} " + L("МБ", "MB"))
    lim = (f"{storage_limit(uid):g} " + L("ГБ", "GB")) if storage_limit(uid) >= 1 else (f"{storage_limit(uid) * 1000:.0f} " + L("МБ", "MB"))
    text = L(f"Кадров в ленте: {n['n'] or 0}\n"
             f"С оригиналом: {n['o'] or 0}, можно менять плёнку: {n['w'] or 0}\n"
             f"Оригиналы: {gb(use['src'])}, рабочие копии: {gb(use['work'])}, превью: {gb(use['pics'])}\n"
             f"Занято {gb(sum(use.values()))} из {lim}, оригиналы живут {ORIG_DAYS:g} дн.",
             f"Frames in feed: {n['n'] or 0}\n"
             f"With original: {n['o'] or 0}, film can be changed: {n['w'] or 0}\n"
             f"Originals: {gb(use['src'])}, working copies: {gb(use['work'])}, previews: {gb(use['pics'])}\n"
             f"Used {gb(sum(use.values()))} of {lim}, originals kept {ORIG_DAYS:g} days")
    if uid == ADMIN:
        du = shutil.disk_usage(BASE)
        text += L(f"\nСвободно на диске: {gb(du.free)} из {gb(du.total)}", f"\nFree disk space: {gb(du.free)} of {gb(du.total)}")
    return text


# ================= экраны бота =================
def help_text(uid):
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
             L("• /storage — сколько места занято, /trash — корзина: удалённые кадры можно вернуть, пока хватает места.",
               "• /storage — how much space is used, /trash — deleted frames, can be brought back while there is space."),
             L("• /camera — файлы и инструкция для настройки камеры.", "• /camera — files and guide to set up your camera."),
             L("• /link — «Проявка» в браузере на компьютере или как приложение на телефоне (без Telegram), /devices — "
               "где она открыта.",
               "• /link — Proyavka in a browser on a computer or as an app on a phone (no Telegram), /devices — where "
               "it is open."),
             L("• /lang — сменить язык (English).", "• /lang — switch language (русский)."),
             L("• Свой LUT: пришли файл .cube — он появится среди плёнок, видишь его только ты. Список — /luts.",
               "• Your own LUT: send a .cube file — it appears among the films, only you can see it. List: /luts."),
             L("• Можно прислать любое фото файлом — обработаю.", "• Send any photo as a file — I will develop it.")]
    if uid == ADMIN:
        lines += [L("• /invite — ссылка-приглашение для ещё одного человека, /users — кто пользуется ботом.",
                    "• /invite — an invite link for one more person, /users — who uses the bot.")]
    if WEBAPP_URL:
        lines.append(L("• Кнопка «Проявка» слева от поля ввода — лента-приложение со свайпами и превью плёнок.",
                       "• The Proyavka button next to the input field — feed app with swipes and live film previews."))
        lines.append(L("  Там же: «+» — фото с телефона, долгое нажатие на кадр — выбрать несколько (плёнка, засвет, "
                       "удаление пачкой), «Кадр» — кадрирование.",
                       "  There: \"+\" adds phone photos, long-press a frame to select several (film, leak, delete at once), "
                       "\"Crop\" crops."))
    lines += ["", L("Плёнки:", "Films:")]
    for k, p in PRESETS.items():
        lines.append(f"{p['name']} — {tr(p['when'])}: {tr(p['desc'])}")
    return "\n".join(lines)


def gallery_page(page, uid):
    total = q("SELECT COUNT(*) AS n FROM photos WHERE hidden=0 AND owner=?", (uid,))[0]["n"]
    pages = max(1, math.ceil(total / PAGE))
    page = max(0, min(page, pages - 1))
    rows = q("SELECT * FROM photos WHERE hidden=0 AND owner=? ORDER BY taken DESC, id DESC LIMIT ? OFFSET ?",
             (uid, PAGE, page * PAGE))
    kb = []
    for i in range(0, len(rows), 4):
        kb.append([btn(f"#{r['id']}", f"o:{r['id']}") for r in rows[i:i + 4]])
    kb.append([btn("◀", f"g:{page - 1}"), btn(f"{page + 1}/{pages}", "x"), btn("▶", f"g:{page + 1}")])
    if WEBAPP_URL:
        kb.append([{"text": L("📱 Открыть «Проявку»", "📱 Open Proyavka"), "web_app": {"url": WEBAPP_URL}}])
    return gallery_image(rows), {"inline_keyboard": kb}, L(f"Лента: {total} кадров. Нажми номер — пришлю кадр с кнопками.", f"Feed: {total} frames. Tap a number to get the frame with buttons.")


def trash_page(page, uid):
    """Корзина: удалённые кадры, у которых ещё есть файлы. Номер под картинкой — вернуть кадр."""
    where = "owner=? AND hidden=1 AND work IS NOT NULL"
    total = q(f"SELECT COUNT(*) AS n FROM photos WHERE {where}", (uid,))[0]["n"]
    if not total:
        return None, None, L("Корзина пуста.", "The trash is empty.")
    pages = max(1, math.ceil(total / PAGE))
    page = max(0, min(page, pages - 1))
    rows = q(f"SELECT * FROM photos WHERE {where} ORDER BY deleted_at DESC, id DESC LIMIT ? OFFSET ?", (uid, PAGE, page * PAGE))
    kb = [[btn(f"↩ #{r['id']}", f"r:{r['id']}") for r in rows[i:i + 4]] for i in range(0, len(rows), 4)]
    if pages > 1:
        kb.append([btn("◀", f"tp:{page - 1}"), btn(f"{page + 1}/{pages}", "x"), btn("▶", f"tp:{page + 1}")])
    cap = L(f"Корзина: {total}. Нажми номер — кадр вернётся в ленту и в чат. Когда места не хватает, "
            "корзина чистится первой, начиная с давно удалённого.",
            f"Trash: {total}. Tap a number to bring the frame back to the feed and the chat. When space runs low, "
            "the trash is emptied first, oldest deletions first.")
    return gallery_image(rows), {"inline_keyboard": kb}, cap


def send_trash(uid, page=0, mid=None):
    img, kb, cap = trash_page(page, uid)
    if img is None:
        if mid:
            safe("editMessageCaption", chat_id=uid, message_id=mid, caption=cap, reply_markup={"inline_keyboard": []})
        else:
            tg("sendMessage", chat_id=uid, text=cap)
        return
    if mid:
        safe("editMessageMedia", files={"f": ("t.jpg", jpeg(img, 88))}, chat_id=uid, message_id=mid,
             media={"type": "photo", "media": "attach://f", "caption": cap}, reply_markup=kb)
    else:
        tg("sendPhoto", files={"photo": ("t.jpg", jpeg(img, 88))}, chat_id=uid, caption=cap, reply_markup=kb)


def default_kb(uid):
    cur = (user(uid) or {}).get("default_film") or "auto"
    rows = [[btn(("• " if cur == "auto" else "") + L("🤖 Авто по ситуации", "🤖 Auto by scene"), "d:auto")]]
    keys = list(PRESETS) + [f"lut{r['id']}" for r in user_luts(uid)]
    for i in range(0, len(keys), 3):
        rows.append([btn(("• " if cur == k else "") + pname(k), f"d:{k}") for k in keys[i:i + 3]])
    return {"inline_keyboard": rows}


def send_today(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = q("SELECT * FROM photos WHERE hidden=0 AND owner=? AND file_id IS NOT NULL AND substr(taken,1,10)=? "
             "ORDER BY taken, id", (uid, today))
    if not rows:
        tg("sendMessage", chat_id=uid, text=L("Сегодня кадров пока нет.", "No frames today yet."))
        return
    for i in range(0, len(rows), 10):
        chunk = rows[i:i + 10]
        media = [{"type": "photo", "media": r["file_id"]} for r in chunk]
        media[0]["caption"] = L("Прогулка", "Walk") + f" {datetime.now():%d.%m} · {len(rows)} " + L("кадров", "frames")
        tg("sendMediaGroup", chat_id=uid, media=media)


# ================= события бота =================
def on_text(text, uid):
    t = text.strip().lower()
    if t in ("/start", "/help") or t.startswith("/start ") or kb_is(t, KB_HELP):
        tg("sendMessage", chat_id=uid, text=help_text(uid), reply_markup=menu())
    elif t == "/gallery" or kb_is(t, KB_FEED):
        img, kb, cap = gallery_page(0, uid)
        tg("sendPhoto", files={"photo": ("g.jpg", jpeg(img, 88))}, chat_id=uid, caption=cap, reply_markup=kb)
    elif t == "/today" or kb_is(t, KB_TODAY):
        send_today(uid)
    elif t == "/film" or kb_is(t, KB_FILM):
        tg("sendMessage", chat_id=uid, text=L("Какую плёнку ставить новым кадрам?", "Which film for new frames?"), reply_markup=default_kb(uid))
    elif t == "/storage":
        tg("sendMessage", chat_id=uid, text=storage_text(uid))
    elif t == "/trash":
        send_trash(uid)
    elif t == "/luts":
        text, kb = luts_screen(uid)
        tg("sendMessage", chat_id=uid, text=text, reply_markup=kb)
    elif t == "/camera":
        send_camera_setup(uid)
    elif t == "/link":
        send_link(uid)
    elif t == "/devices":
        text, kb = devices_screen(uid)
        tg("sendMessage", chat_id=uid, text=text, reply_markup=kb)
    elif t == "/lang":
        set_user(uid, lang="en" if user_lang(uid) == "ru" else "ru")
        with speak(uid):
            set_commands(uid)
            tg("sendMessage", chat_id=uid, text=L("Язык: русский.", "Language: English."), reply_markup=menu())
    elif t.startswith("/start link_"):
        tg("sendMessage", chat_id=uid, text=L("Этот Telegram уже привязан к «Проявке».", "This Telegram is already linked to Proyavka."))
    elif t == "/invite" and uid == ADMIN:
        inv = invite_links(make_invite(uid))
        tg("sendMessage", chat_id=uid, disable_web_page_preview=True, text=L(
            f"Приглашение — одноразовое, живёт {INVITE_DAYS} дней. Перешли одну из ссылок:\n"
            f"• через Telegram: {inv['tg_url']}\n• в приложении, без Telegram: {inv['url']}\n"
            f"(или код {inv['code']} на экране входа «Проявки»)\n\n"
            "У него будет своя лента, свои кадры и своя камера; его кадры не видны тебе, а твои — ему. "
            "Технически администратор сервера может открыть любые файлы на нём.",
            f"Invite — single use, valid for {INVITE_DAYS} days. Forward one of the links:\n"
            f"• via Telegram: {inv['tg_url']}\n• in the app, no Telegram: {inv['url']}\n"
            f"(or the code {inv['code']} on the Proyavka sign-in screen)\n\n"
            "They get their own feed, frames and camera; you don't see their frames and they don't see yours. "
            "Technically, the server admin can open any file on it."))
    elif t == "/users" and uid == ADMIN:
        text, kb = users_screen()
        tg("sendMessage", chat_id=uid, text=text, reply_markup=kb)


# ================= приглашения и пользователи =================
BOT_NAME = {}


def bot_username():
    if "u" not in BOT_NAME:
        BOT_NAME["u"] = tg("getMe")["username"]
    return BOT_NAME["u"]


def make_invite(by):
    """Одноразовое приглашение: тот же код годится и в «Проявке» (без Telegram), и в боте (/start <код>)."""
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_LEN))
    run("INSERT INTO invites(code, created, by) VALUES (?,?,?)", (code, time.time(), by))
    return code


def invite_links(code):
    return {"code": show_code(code), "url": f"{WEBAPP_URL.rstrip('/')}/#pair={code}" if WEBAPP_URL else "",
            "tg_url": f"https://t.me/{bot_username()}?start={code}" if BOT_TOKEN else ""}


INVITED_BY = {}


def use_invite(code, new_uid):
    """Погасить приглашение за новым пользователем. Кто пригласил — в INVITED_BY[new_uid]."""
    fresh = time.time() - INVITE_DAYS * 86400
    for c in dict.fromkeys((str(code or "").strip(), norm_code(code))):
        if c and run_count("UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL AND created > ?",
                           (new_uid, time.time(), c, fresh)) == 1:
            INVITED_BY[new_uid] = q("SELECT by FROM invites WHERE code=?", (c,))[0]["by"]
            return True
    return False


def invite_open(code):
    return bool(q("SELECT 1 FROM invites WHERE code=? AND used_by IS NULL AND created > ?",
                  (norm_code(code), time.time() - INVITE_DAYS * 86400)))


JOIN_LOCK = threading.Lock()


def join_by_invite(code, name, device_name):
    """Новый пользователь из «Проявки» по коду приглашения: номер с WEB_BASE, сразу и устройство."""
    name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:40]
    if not name:
        raise ValueError(L("как тебя зовут?", "what is your name?"))
    with JOIN_LOCK:
        top = q("SELECT MAX(id) AS m FROM users WHERE id >= ?", (WEB_BASE,))[0]["m"]
        uid = max(top or WEB_BASE, WEB_BASE) + 1
        if not use_invite(code, uid):
            return None
        run("INSERT INTO users(id, role, name, lang, default_film, created, invited_by) VALUES (?, 'user', ?, ?, 'auto', ?, ?)",
            (uid, name, cur_lang(), time.time(), INVITED_BY.pop(uid, None)))
        load_users()
    log.info("новый пользователь %s (%d) по приглашению, без Telegram", name, uid)
    with speak(ADMIN):
        safe("sendMessage", chat_id=ADMIN, text=L("По приглашению пришёл", "Joined by invite") + f": {name}")
    return create_device(uid, device_name)


def tg_name(fr):
    name = " ".join(x for x in (fr.get("first_name"), fr.get("last_name")) if x) or str(fr.get("id"))
    return name + (f" (@{fr['username']})" if fr.get("username") else "")


STRANGERS = {}       # кому уже ответили «нужно приглашение» (чтобы не отвечать на каждое сообщение)


def on_stranger(msg):
    """Сообщение от того, кого нет среди пользователей: принять приглашение или вежливо отказать."""
    fr = msg.get("from") or {}
    uid = fr.get("id")
    lang = "ru" if (fr.get("language_code") or "").split("-")[0] in ("ru", "uk", "be", "kk") else "en"
    _CTX.lang = lang
    text = (msg.get("text") or "").strip()
    if text.startswith("/start link_"):          # «Подключить Telegram» в настройках «Проявки»
        owner = take_pair(text.split("link_", 1)[1], "tg")
        if owner in USERS and not USERS[owner].get("tg"):
            set_user(owner, tg=uid)
            log.info("Telegram %d привязан к %d", uid, owner)
            with speak(owner):
                set_commands(owner)
                tg("sendMessage", chat_id=owner, reply_markup=menu(), text=L(
                    "Telegram привязан к «Проявке». Новые кадры будут приходить и сюда, с кнопками.\n\n",
                    "Telegram is linked to Proyavka. New frames will arrive here too, with buttons.\n\n") + help_text(owner))
        else:
            safe("sendMessage", chat_id=uid, text=L("Ссылка устарела — возьми новую в «Проявке»: ⋯ → Telegram.",
                                                    "The link has expired — get a new one in Proyavka: ⋯ → Telegram."))
        return
    if text.startswith("/start "):
        code = text.split(maxsplit=1)[1].strip()
        if use_invite(code, uid):
            run("INSERT OR REPLACE INTO users(id, role, name, lang, default_film, created, invited_by, tg) "
                "VALUES (?, 'user', ?, ?, 'auto', ?, ?, ?)", (uid, tg_name(fr), lang, time.time(), INVITED_BY.pop(uid, None), uid))
            load_users()
            log.info("новый пользователь %s (%d)", tg_name(fr), uid)
            with speak(uid):
                set_commands(uid)
                tg("sendMessage", chat_id=uid, text=L("Привет! Это «Проявка»: твои фото как на плёнку.\n\n",
                                                      "Hi! This is Proyavka: your photos, as if shot on film.\n\n")
                   + help_text(uid), reply_markup=menu())
            with speak(ADMIN):
                safe("sendMessage", chat_id=ADMIN, text=L("По приглашению пришёл", "Joined by invite") + f": {tg_name(fr)}")
            return
        safe("sendMessage", chat_id=uid, text=L("Ссылка-приглашение недействительна: она одноразовая и живёт "
                                                f"{INVITE_DAYS} дней. Попроси новую у того, кто тебя позвал.",
                                                f"This invite link is not valid: it works once and for {INVITE_DAYS} days. "
                                                "Ask the person who invited you for a new one."))
        return
    if time.time() - STRANGERS.get(uid, 0) > 3600:
        STRANGERS[uid] = time.time()
        safe("sendMessage", chat_id=uid, text=L("Это личный бот «Проявки». Чтобы им пользоваться, нужна ссылка-приглашение "
                                                "от владельца.", "This is a private Proyavka bot. You need an invite link "
                                                "from its owner to use it."))


def users_screen():
    rows = q("SELECT u.*, (SELECT COUNT(*) FROM photos p WHERE p.owner=u.id AND p.hidden=0) AS n FROM users u "
             "ORDER BY u.role='admin' DESC, u.created")
    lines, kb = [L("Пользователи:", "Users:")], []
    for i, u in enumerate(rows, 1):
        used = sum(user_usage(u["id"]).values()) / 1e9
        role = L(" — администратор", " — admin") if u["role"] == "admin" else ""
        lines.append(f"{i}. {u['name'] or u['id']}{role}\n   {u['n']} "
                     + L(plural_ru(u["n"], "кадр", "кадра", "кадров"), "frame" if u["n"] == 1 else "frames")
                     + f" · {used:.1f} / {storage_limit(u['id']):g} " + L("ГБ", "GB"))
        if u["id"] != ADMIN:
            short = (u["name"] or str(u["id"])).split(" (@")[0][:16]
            kb.append([btn(f"{i}. {short}: {storage_limit(u['id']):g} " + L("ГБ ▸", "GB ▸"), f"ul:{u['id']}"),
                       btn(L("🗑 Удалить", "🗑 Remove"), f"ud:{u['id']}")])
    if len(rows) == 1:
        lines.append(L("\nПока только ты. Позови кого-нибудь: /invite", "\nJust you so far. Invite someone: /invite"))
    return "\n".join(lines), {"inline_keyboard": kb}


LIMITS_GB = [1, 2, 5, 10, 20, 50, 100, 200, 500]


def set_limit(uid, gb):
    """Лимит места. Свой у администратора — это STORAGE_GB в config.env (его же меняет мастер установки)."""
    global STORAGE_GB
    if uid == ADMIN:
        save_config("STORAGE_GB", f"{gb:g}")
        STORAGE_GB = gb
        set_user(uid, storage_gb=None)
    else:
        set_user(uid, storage_gb=gb)
    threading.Thread(target=enforce_limit, args=(uid,), daemon=True).start()   # лимит уменьшили — освободить место


def delete_user(uid):
    """Убрать пользователя: все его кадры и файлы, камеру на сервере, доступ к боту."""
    rows = q("SELECT * FROM photos WHERE owner=?", (uid,))
    for ph in rows:
        for p in (ph["src"], ph["work"], ph["view"], ph["thumb"]):
            remove(p)
        for p in PREVIEWS.glob(f"{ph['id']}_*.jpg"):
            remove(str(p))
    run("DELETE FROM photos WHERE owner=?", (uid,))
    run("DELETE FROM luts WHERE owner=?", (uid,))
    load_luts()
    if uid != ADMIN:
        shutil.rmtree(BASE / "users" / str(uid), ignore_errors=True)
    u = user(uid) or {}
    if u.get("cam_token"):
        try:
            cam_helper("del", f"u{uid}")
        except Exception as e:
            log.warning("camera of %d: %s", uid, e)
    for tok in [t for t, v in SESSIONS.items() if v[1] == uid]:
        SESSIONS.pop(tok, None)
        SESSION_DEV.pop(tok, None)
    run("DELETE FROM devices WHERE owner=?", (uid,))
    run("DELETE FROM pairs WHERE uid=?", (uid,))
    run("DELETE FROM push_subs WHERE owner=?", (uid,))
    run("DELETE FROM album_photos WHERE album IN (SELECT id FROM albums WHERE owner=?)", (uid,))
    run("DELETE FROM albums WHERE owner=?", (uid,))
    with speak(uid):
        bye = L("Доступ к боту закрыт.", "Your access to the bot was removed.")
    run("DELETE FROM users WHERE id=?", (uid,))
    load_users()
    safe("deleteMyCommands", scope={"type": "chat", "chat_id": uid})
    safe("setChatMenuButton", chat_id=uid, menu_button={"type": "default"})
    safe("sendMessage", chat_id=uid, text=bye, reply_markup={"remove_keyboard": True})
    return len(rows)


def set_commands(uid):
    """Команды бота в меню — на языке пользователя; у администратора ещё /invite и /users."""
    cmds = [
        {"command": "gallery", "description": L("Лента кадров в чате", "Frame feed in the chat")},
        {"command": "today", "description": L("Альбом за сегодня", "Today's album")},
        {"command": "film", "description": L("Плёнка по умолчанию", "Default film")},
        {"command": "storage", "description": L("Сколько места занято", "Storage used")},
        {"command": "trash", "description": L("Корзина: вернуть удалённое", "Trash: bring back deleted frames")},
        {"command": "luts", "description": L("Свои LUT (.cube)", "Your own LUTs (.cube)")},
        {"command": "camera", "description": L("Настройка камеры: файлы и инструкция", "Camera setup: files and guide")},
        {"command": "link", "description": L("Открыть «Проявку» в браузере, на ПК", "Open Proyavka in a browser, on a PC")},
        {"command": "devices", "description": L("Устройства с «Проявкой» без Telegram", "Devices using Proyavka outside Telegram")},
        {"command": "lang", "description": L("English", "Русский")},
        {"command": "help", "description": L("Как пользоваться", "How to use")},
    ]
    if uid == ADMIN:
        cmds += [{"command": "invite", "description": L("Пригласить человека", "Invite a person")},
                 {"command": "users", "description": L("Пользователи бота", "Bot users")}]
    safe("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": uid})
    if WEBAPP_URL:
        safe("setChatMenuButton", chat_id=uid,
             menu_button={"type": "web_app", "text": tr(APP_NAME), "web_app": {"url": WEBAPP_URL}})


# ================= настройка камеры прямо из чата =================
PROJECT_URL = os.environ.get("PROJECT_URL", "https://github.com/Melnikoff07/proyavka")
APP_ROOT = Path(__file__).resolve().parent.parent
CAMERA_CONFIG = APP_ROOT / "camera-config" / "config.txt"
FTP_ROOT_CERT = APP_ROOT / "camera-app" / "certs" / "isrgrootx1.pem"


CAM_HELPER = os.environ.get("CAM_HELPER", "/usr/local/lib/proyavka/proyavka-user")


def cam_helper(action, name, stdin=""):
    """Пользователи камер на сервере-приёмнике: FTP-вход, папка и токен приложения. Root-скрипт через sudo."""
    res = subprocess.run(remote(f"sudo -n {CAM_HELPER} {action} {shlex.quote(name)}"), input=stdin,
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"exit {res.returncode}")
    return res.stdout


# пароль FTP вводят на камере: только строчные и цифры без похожих (l/1, o/0) — 32^10 ≈ 10^15 вариантов
FTP_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def easy_password(n=10):
    return "".join(secrets.choice(FTP_ALPHABET) for _ in range(n))


def new_ftp_password(uid):
    """Сменить пароль FTP камеры на новый короткий (старый перестаёт работать)."""
    pw = easy_password()
    try:
        if uid == ADMIN:
            cam_helper("passwd", "camera", pw + "\n")
            save_config("FTP_PASS", pw)
            os.environ["FTP_PASS"] = pw
        else:
            u = ensure_camera(uid)
            cam_helper("add", f"u{uid}", f"{pw}\n{hashlib.sha256(u['cam_token'].encode()).hexdigest()}\n")
            set_user(uid, ftp_pass=pw)
    except Exception as e:
        log.warning("ftp password for %d: %s", uid, e)
        raise RuntimeError(L("Не получилось сменить пароль: сервер-приёмник от прежней версии — администратору: setup.py → «Обновить».",
                             "Could not change the password: the receiving server is from an older version — admin: setup.py → Update."))
    log.info("пароль FTP сменён у %d", uid)


def ensure_camera(uid):
    """Свои ключи камеры: токен приложения на Sony и FTP-пользователь со своей папкой. Создаются при первом /camera."""
    u = user(uid)
    if u.get("cam_token"):
        return u
    token, pw = secrets.token_urlsafe(32), easy_password()
    cam_helper("add", f"u{uid}", f"{pw}\n{hashlib.sha256(token.encode()).hexdigest()}\n")
    set_user(uid, cam_token=token, ftp_pass=pw)
    VPS_WATCH_RESTART.set()                   # новая папка — пусть мгновенные уведомления смотрят и её
    return user(uid)


def camera_access(uid):
    """FTP-вход и config.txt для камеры пользователя (у администратора — общий вход и файл мастера установки)."""
    domain = os.environ.get("DOMAIN", "")
    if uid == ADMIN:
        conf = CAMERA_CONFIG.read_bytes() if CAMERA_CONFIG.exists() else None
        return "camera", os.environ.get("FTP_PASS", "—"), conf
    try:
        u = ensure_camera(uid)
    except Exception as e:
        log.warning("camera for %d: %s", uid, e)
        with speak(ADMIN):
            safe("sendMessage", chat_id=ADMIN, text=L(
                f"Не получилось завести камеру для {u_name(uid)}: {e}\n"
                "Скорее всего, сервер-приёмник от прежней версии: запусти setup.py → «Обновить».",
                f"Could not set up a camera for {u_name(uid)}: {e}\n"
                "The receiving server is probably from an older version: run setup.py → \"Update\"."))
        raise RuntimeError(L("Не получилось завести камеру на сервере. Напиши администратору.",
                             "Could not set up a camera on the server. Please tell the admin."))
    conf = (L("# Настройки приложения «Проявка» для камеры Sony. Положи на карту в папку PROYAVKA.",
              "# Settings of the Proyavka app for Sony cameras. Put on the card into the PROYAVKA folder.")
            + f"\n\nurl = https://{domain}\ntoken = {u['cam_token']}\nlang = {user_lang(uid)}\n").encode()
    return f"u{uid}", u["ftp_pass"], conf


def send_camera_setup(uid):
    """Всё, что нужно положить в камеру, — файлами в чат: скачал, скинул на карту, готово."""
    domain = os.environ.get("DOMAIN", "")
    try:
        ftp_user, pw, conf = camera_access(uid)
    except RuntimeError as e:
        tg("sendMessage", chat_id=uid, text=str(e))
        return
    guide = f"{PROJECT_URL}/blob/main/docs"
    ext = ".md" if cur_lang() == "en" else ".ru.md"
    text = L(
        "📷 Настройка камеры\n\n"
        "Sony с приложениями (a6000–a6500, a7 II, RX100 III–V…)\n"
        f"1. Поставь приложение Proyavka.apk — пошагово: {guide}/sony-app{ext}\n"
        "2. На карте памяти создай папку PROYAVKA и положи в неё файл config.txt (ниже).\n"
        "3. В камере: Меню → Приложение → Проявка → Wi-Fi → выбери сеть (точку доступа телефона или дом) "
        "и введи пароль. Один раз — дальше камера подключается сама.\n"
        "4. Снимай в JPEG или RAW+JPEG → Проявка → Отправить новые.\n\n"
        "Камеры с отправкой по FTP (Sony A7C II, A7 IV, A1…)\n"
        f"сервер: {domain}\nпорт: 21\nпользователь: {ftp_user}\nпароль: {pw}\n"
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
        f"server: {domain}\nport: 21\nuser: {ftp_user}\npassword: {pw}\n"
        "folder: upload · FTPS (explicit TLS) · passive mode\n"
        "The camera needs a root certificate: put cacert.pem (below) in the root of the card "
        f"and import it in the network menu. Step by step: {guide}/ftp-cameras{ext}")
    tg("sendMessage", chat_id=uid, disable_web_page_preview=True, text=text)
    cap = L("config.txt → на карту в папку PROYAVKA", "config.txt → onto the card, into the PROYAVKA folder")
    if conf:
        tg("sendDocument", files={"document": ("config.txt", io.BytesIO(conf))}, chat_id=uid, caption=cap)
    if FTP_ROOT_CERT.exists():
        with open(FTP_ROOT_CERT, "rb") as f:
            tg("sendDocument", files={"document": ("cacert.pem", f)}, chat_id=uid,
               caption=L("cacert.pem → в корень карты, для камер с FTP", "cacert.pem → root of the card, for FTP cameras"))


def u_name(uid):
    return ((user(uid) or {}).get("name") or str(uid))


def on_callback(cb, uid):
    data = cb["data"]
    mid = cb.get("message", {}).get("message_id")
    parts = data.split(":")
    kind = parts[0]
    dev = L("Проявляю…", "Developing…")
    notes = {"p": dev, "cp": dev, "s": dev, "t": dev, "l": dev, "ls": dev,
             "f": L("Готовлю файл, пришлю в чат", "Preparing the file, will send it to the chat"),
             "c": L("Собираю лист…", "Building the sheet…"), "dely": L("Убираю в корзину", "Moving to trash"),
             "r": L("Возвращаю", "Restoring"), "dv": L("Устройство отключено", "Device removed")}
    safe("answerCallbackQuery", callback_query_id=cb["id"], text=notes.get(kind))
    if kind == "x":
        return
    if kind == "cx":
        safe("deleteMessage", chat_id=uid, message_id=mid)
        return
    if kind == "g":
        img, kb, cap = gallery_page(int(parts[1]), uid)
        media = {"type": "photo", "media": "attach://f", "caption": cap}
        safe("editMessageMedia", files={"f": ("g.jpg", jpeg(img, 88))}, chat_id=uid,
             message_id=mid, media=media, reply_markup=kb)
        return
    if kind == "tp":
        send_trash(uid, int(parts[1]), mid)
        return
    if kind == "dv":
        if len(parts) > 1 and parts[1].isdigit():
            drop_device(uid, int(parts[1]))
        text, kb = devices_screen(uid)
        safe("editMessageText", chat_id=uid, message_id=mid, text=text, reply_markup=kb)
        return
    if kind in ("lx", "lxy", "lxb"):
        key = f"lut{parts[1]}" if len(parts) > 1 else ""
        if kind == "lx" and LUT_OWNER.get(key) == uid:
            safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup={"inline_keyboard": [
                [btn(L(f"🗑 Да, удалить «{LUT_NAMES[key][:20]}»", f"🗑 Yes, delete \"{LUT_NAMES[key][:20]}\""), f"lxy:{parts[1]}")],
                [btn(L("← Нет", "← No"), "lxb")]]})
            return
        if kind == "lxy" and LUT_OWNER.get(key) == uid:
            n = delete_lut(uid, key)
            if n:
                safe("sendMessage", chat_id=uid, text=L(f"Кадры с этим LUT ({n}) переведены на автоплёнку.",
                                                         f"Frames with this LUT ({n}) switched to their auto film."))
        text, kb = luts_screen(uid)
        safe("editMessageText", chat_id=uid, message_id=mid, text=text, reply_markup=kb)
        return
    if kind == "r":
        ph = get(int(parts[1]))
        if ph and ph["owner"] == uid and ph["hidden"]:
            try:
                restore_photo(ph)
            except RuntimeError as e:
                safe("sendMessage", chat_id=uid, text=str(e))
            send_trash(uid, 0, mid)
        return
    if kind == "d":
        key = canon(parts[1])
        if key != "auto" and not valid_look(key, uid):
            return
        set_user(uid, default_film=key)
        label = L("Авто по ситуации", "Auto by scene") if key == "auto" else pname(key)
        safe("editMessageText", chat_id=uid, message_id=mid,
             text=L("Новые кадры", "New frames") + f": {label}", reply_markup=default_kb(uid))
        return
    if kind in ("ul", "ud", "udy") and uid == ADMIN:
        target = int(parts[1])
        if target == ADMIN or not user(target):
            return
        if kind == "ul":                      # лимит места по кругу
            cur = storage_limit(target)
            nxt = next((g for g in LIMITS_GB if g > cur), LIMITS_GB[0])
            set_user(target, storage_gb=nxt)
        elif kind == "ud":
            safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup={"inline_keyboard": [
                [btn(L(f"🗑 Да, удалить {u_name(target)[:20]} и все кадры", f"🗑 Yes, remove {u_name(target)[:20]} and all frames"),
                     f"udy:{target}")], [btn(L("← Нет", "← No"), "ub")]]})
            return
        else:
            n = delete_user(target)
            safe("sendMessage", chat_id=uid, text=L(f"Удалён пользователь и {n} его кадров.", f"User removed with {n} frames."))
        text, kb = users_screen()
        safe("editMessageText", chat_id=uid, message_id=mid, text=text, reply_markup=kb)
        return
    if kind == "ub" and uid == ADMIN:
        text, kb = users_screen()
        safe("editMessageText", chat_id=uid, message_id=mid, text=text, reply_markup=kb)
        return

    ph = get(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
    if not ph or ph["owner"] != uid:          # чужой кадр — кнопки не работают
        return
    if kind == "m":
        safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup=preset_kb(ph))
    elif kind == "b":
        safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup=main_kb(ph))
    elif kind == "lm":
        safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup=leak_kb(ph))
    elif kind == "l":
        apply_changes(ph, {"leak": parts[2]})
    elif kind == "ls":
        apply_changes(ph, {"leak_shift": 1})
    elif kind in ("p", "cp"):
        if kind == "cp":
            safe("deleteMessage", chat_id=uid, message_id=mid)
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
        safe("editMessageReplyMarkup", chat_id=uid, message_id=mid, reply_markup={"inline_keyboard": [
            [btn(L("🗑 Да, в корзину", "🗑 Yes, to trash"), f"dely:{ph['id']}"), btn(L("← Нет", "← No"), f"b:{ph['id']}")]]})
    elif kind == "dely":
        hide_photo(ph)


def send_contact(ph):
    with speak(ph["owner"]):
        _send_contact(ph)


def _send_contact(ph):
    path = TMP / f"contact_{ph['id']}.jpg"
    try:
        FAST.submit(job_contact, ph, str(path)).result(timeout=300)
        with open(path, "rb") as f:
            tg("sendPhoto", files={"photo": ("c.jpg", f)}, chat_id=ph["owner"],
               caption=f"#{ph['id']}: " + L("все плёнки. Нажми нужную — применю к кадру.", "all films. Tap one to apply it to the frame."),
               reply_to_message_id=ph["msg_id"], reply_markup=preset_kb(ph, prefix="cp"))
    except Exception as e:
        safe("sendMessage", chat_id=ph["owner"], text=L("Не смог собрать лист", "Could not build the sheet") + f": {e}")
    finally:
        remove(str(path))


def download_tg_file(file_id, name, uid):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
    name = "".join(c for c in Path(name).name if c.isalnum() or c in "-_.")[:60] or "photo.jpg"
    tmp = TMP / f"tg_{secrets.token_hex(6)}.part"
    with requests.get(url, timeout=(10, 120), stream=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r.raw, f)
    os.replace(tmp, udir(uid, "incoming") / name)


def download_tg_bytes(file_id, limit):
    info = tg("getFile", file_id=file_id)
    if (info.get("file_size") or 0) > limit:
        raise ValueError(L("файл слишком большой", "file is too large"))
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
    r = requests.get(url, timeout=(10, 120))
    r.raise_for_status()
    return r.content[:limit + 1]


def luts_screen(uid):
    rows = user_luts(uid)
    if not rows:
        return L("Своих LUT пока нет. Пришли файл .cube сюда в чат (или «+ LUT» в «Проявке») — он появится "
                 "в списке плёнок. Видишь его только ты.",
                 "No LUTs of your own yet. Send a .cube file here (or \"+ LUT\" in Proyavka) — it will appear in the "
                 "film list. Only you can see it."), None
    text = L("Твои LUT (видишь только ты):", "Your LUTs (only you can see them):") + "\n" + "\n".join(
        f"• {r['name']} ({r['size']}³)" for r in rows)
    kb = [[btn("🗑 " + r["name"][:24], f"lx:{r['id']}")] for r in rows]
    return text, {"inline_keyboard": kb}


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
        uid = None
        try:
            if "callback_query" in u:
                cb = u["callback_query"]
                uid = uid_of_tg(cb["from"]["id"])
                if user(uid):
                    with speak(uid):
                        on_callback(cb, uid)
                else:
                    safe("answerCallbackQuery", callback_query_id=cb["id"])
                continue
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") != "private":
                continue
            uid = uid_of_tg((msg.get("from") or {}).get("id"))
            if not user(uid):
                on_stranger(msg)
                continue
            if uid < WEB_BASE and user(uid)["name"] != tg_name(msg["from"]):    # для /users: имя, как в Telegram
                set_user(uid, name=tg_name(msg["from"]))
            with speak(uid):
                if "document" in msg and (msg["document"].get("file_name") or "").lower().endswith(".cube"):
                    d = msg["document"]
                    try:
                        r = add_lut(uid, d["file_name"], download_tg_bytes(d["file_id"], LUT_MAX_BYTES))
                        tg("sendMessage", chat_id=uid, text=L(f"LUT «{r['name']}» добавлен: он в списке плёнок под фото и в «Проявке». "
                                                              "Видишь его только ты. Список — /luts",
                                                              f"LUT \"{r['name']}\" added: it's in the film list under photos and in "
                                                              "Proyavka. Only you can see it. List: /luts"))
                    except ValueError as e:
                        tg("sendMessage", chat_id=uid, text=L("Не получилось добавить LUT", "Could not add the LUT") + f": {e}")
                elif "document" in msg:
                    d = msg["document"]
                    download_tg_file(d["file_id"], d.get("file_name") or f"tg_{u['update_id']}.jpg", uid)
                elif "photo" in msg:
                    download_tg_file(msg["photo"][-1]["file_id"], f"tg_{u['update_id']}.jpg", uid)
                elif "text" in msg:
                    on_text(msg["text"], uid)
        except Exception as e:
            log.exception("update failed")
            if uid and user(uid):
                with speak(uid):
                    safe("sendMessage", chat_id=uid, text=L("Ошибка", "Error") + f": {e}")


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


# На сервере-приёмнике: кадры администратора — в REMOTE_DIR (как в прежних версиях),
# кадры остальных — в <REMOTE_USERS>/u<id>/upload/ (свой FTP-пользователь и свой токен приложения камеры).
REMOTE_DIR = REMOTE_DIR.rstrip("/") + "/"
REMOTE_USERS = os.environ.get("REMOTE_USERS_DIR", posixpath.join(posixpath.dirname(REMOTE_DIR.rstrip("/")), "u")).rstrip("/") + "/"
_USER_DIR_RE = re.compile(re.escape(REMOTE_USERS) + r"u(\d{1,15})/upload/")
VPS_WATCH_RESTART = threading.Event()


def remote_dir(uid):
    return REMOTE_DIR if uid == ADMIN else f"{REMOTE_USERS}u{uid}/upload/"


def remote_owner(path):
    """Полный путь файла на сервере -> (владелец, имя) или None (временные файлы, чужие папки)."""
    d, name = posixpath.split(path)
    d += "/"
    if d == REMOTE_DIR:
        return ADMIN, name
    m = _USER_DIR_RE.fullmatch(d)
    if m and int(m.group(1)) in USERS:
        return int(m.group(1)), name
    return None


def fetch_names(items, ages=None):
    """Забрать файлы с VPS, проверить и только потом удалить их там. items: [(владелец, имя)]."""
    groups = {}
    for uid, n in dict.fromkeys(items):
        if n and "/" not in n and not n.startswith(".") and Path(n).suffix.lower() in EXTS:
            groups.setdefault(uid, []).append(n)
    for uid, names in groups.items():
        _fetch_dir(uid, names, ages)


def _fetch_dir(uid, names, ages):
    rdir = remote_dir(uid)
    stage = STAGING / str(uid)
    stage.mkdir(parents=True, exist_ok=True)
    lst = BASE / "fetch.txt"
    lst.write_text("\n".join(names) + "\n")
    src = ["rsync", "-a", "--files-from", str(lst)] + ([rdir] if LOCAL else ["-e", " ".join(SSH_CMD), f"{VPS}:{rdir}"])
    res = subprocess.run(src + [str(stage) + "/"], capture_output=True, text=True, timeout=600)
    if res.returncode not in (0, 23, 24):   # 23/24: часть файлов уже исчезла — не страшно
        log.warning("rsync: %s", res.stderr.strip()[-300:])
    done = []
    for n in names:
        p = stage / n
        if not p.exists():
            continue
        old_enough = ages is not None and ages.get((uid, n), 0) > PARTIAL_GRACE
        if file_complete(p) or old_enough:
            os.replace(p, udir(uid, "incoming") / n)
            done.append(n)
        else:
            remove(str(p))
            log.info("%s недокачан, ждём, пока камера дошлёт", n)
    if done:
        rm = "cd " + shlex.quote(rdir) + " && rm -f -- " + " ".join(shlex.quote(n) for n in done)
        subprocess.run(remote(rm), capture_output=True, text=True, timeout=30)
        log.info("получено с VPS (%s): %s", uid, ", ".join(done))


def _remote_dirs():
    """Папки для поиска: администратора всегда, папку пользователей — если она уже есть на сервере."""
    users = shlex.quote(REMOTE_USERS)
    return f"{shlex.quote(REMOTE_DIR)} $([ -d {users} ] && echo {users})"


def fetch_from_vps():
    """Подстраховочный опрос: всё, что лежит на VPS дольше SETTLE секунд."""
    find = (f"find {_remote_dirs()} -maxdepth 3 -type f ! -path '*/.*' ! -newermt '{SETTLE} seconds ago' "
            f"-printf '%T@ %p\\n'")
    try:
        res = subprocess.run(remote(find), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        log.warning("ssh timeout")
        return
    if res.returncode != 0 and not res.stdout:
        log.warning("ssh find failed: %s", res.stderr.strip())
        return
    now = time.time()
    ages = {}
    for line in res.stdout.splitlines():
        ts, _, path = line.partition(" ")
        own = remote_owner(path)
        if own:
            try:
                ages[own] = now - float(ts)
            except ValueError:
                pass
    fetch_names(list(ages), ages)


def sweep_vps():
    """Мусор в папках приёма: всё, что бот не забирает (RAW, не-фото, файлы в подпапках), — через 10 минут,
    недокачанное приложением камеры — через 2 часа, пустые подпапки — тоже. Иначе по FTP можно забить диск."""
    keep = " ".join(f"! -iname '*{e}'" for e in sorted(EXTS))
    cmd = (f"find {_remote_dirs()} -mindepth 1 "
           "'(' -type f -path '*/.incoming/*' -mmin +120 -delete ')' -o "
           f"'(' -type f ! -path '*/.incoming/*' -mmin +10 '(' -path '*/upload/*/*' -o {keep} ')' -print -delete ')' -o "
           "'(' -type d -empty -path '*/upload/*' ! -name .incoming -mmin +10 -delete ')'")
    try:
        res = subprocess.run(remote(cmd), capture_output=True, text=True, timeout=60)
        if res.stdout.strip():
            log.info("на сервере-приёмнике убран мусор: %s", " ".join(res.stdout.split()[:10]))
    except Exception as e:                    # уборка — не повод останавливать приём кадров
        log.warning("уборка на сервере-приёмнике: %s", e)


def vps_watch():
    """Постоянное соединение с VPS: inotifywait сообщает о файле, как только FTP закончил его писать."""
    while True:
        started = time.time()
        cmd = remote(f"inotifywait -m -q -r -e close_write -e moved_to --format '%w%f' {_remote_dirs()}")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            WATCH_ALIVE.set()
            VPS_WATCH_RESTART.clear()
            log.info("VPS: мгновенные уведомления включены")
            threading.Thread(target=_watch_restart, args=(p,), daemon=True).start()
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
        if VPS_WATCH_RESTART.is_set():
            continue
        time.sleep(5 if time.time() - started > 30 else 30)


def _watch_restart(p):
    """Появилась папка нового пользователя — перезапустить слежение, чтобы оно смотрело и её."""
    while p.poll() is None:
        if VPS_WATCH_RESTART.wait(5):
            p.terminate()
            return


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


def find_duplicate(f, fp, owner):
    row = q("SELECT id FROM photos WHERE fp=? AND owner=? LIMIT 1", (fp, owner))
    if row:
        return row[0]["id"]
    # кадры из прежних версий без отпечатка: сравниваем имя файла и время съёмки
    try:
        taken, _ = read_exif(Image.open(f))
    except Exception:
        taken = None
    if taken:
        row = q("SELECT id FROM photos WHERE fp IS NULL AND owner=? AND name=? AND taken=? LIMIT 1", (owner, f.name, taken))
        if row:
            return row[0]["id"]
    return None


def ingest(f, owner):
    fp = fingerprint(f)
    try:
        return _ingest(f, owner, fp)
    finally:
        with PENDING_LOCK:
            PENDING_FP.discard((owner, fp))


def _ingest(f, owner, fp):
    t0 = time.time()
    dup = find_duplicate(f, fp, owner)
    if dup:
        remove(str(f))
        log.info("%s — повтор кадра #%d, пропускаю", f.name, dup)
        return False
    work_dir = udir(owner, "work")
    tmp_work = work_dir / f"incoming_{f.stem}.jpg"
    taken, iso, auto_key, reason = FAST.submit(job_prepare, str(f), str(tmp_work), WORK_EDGE).result(timeout=300)
    default = (user(owner) or {}).get("default_film") or "auto"
    preset = auto_key if default == "auto" else default
    now = time.time()
    pid = run("INSERT INTO photos(name, taken, iso, auto_key, auto_reason, preset, created, rev, rendered_rev, updated, fp, owner) "
              "VALUES (?,?,?,?,?,?,?,1,0,?,?,?)", (f.name, taken, iso, auto_key, reason, preset, now, now, fp, owner))
    src = udir(owner, "originals") / f"{pid}_{f.name}"
    work = work_dir / f"{pid}.jpg"
    os.replace(tmp_work, work)
    shutil.move(str(f), src)
    upd(pid, src=str(src), work=str(work))
    schedule_view(pid, prio=1, uid=owner)   # после отрисовки кадр сам уйдёт в чат
    log.info("#%d %s → %s, подготовка %.1fs", pid, f.name, preset, time.time() - t0)
    return True


DUP_REPORT = {}      # владелец -> {"n": сколько повторов, "since": когда был последний}


def incoming_dirs():
    yield ADMIN, INCOMING
    root = BASE / "users"
    if root.is_dir():
        for d in root.iterdir():
            if d.name.isdigit() and (d / "incoming").is_dir():
                yield int(d.name), d / "incoming"


def process_incoming(state=None):
    for owner, folder in list(incoming_dirs()):
        files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in EXTS)
        if files and owner not in USERS:          # пользователя удалили, а кадры ещё долетали
            for f in files:
                remove(str(f))
            continue
        for f in files:
            if raw_twin(f, files, owner):
                continue
            with speak(owner):
                _process_one(f, owner)
        if files:
            enforce_limit(owner)                # лимит места — сразу, а не раз в час
    # одно сообщение на пачку: когда повторы перестали приходить хотя бы на 20 секунд
    for owner, rep_ in list(DUP_REPORT.items()):
        if rep_["n"] and time.time() - rep_["since"] > 20:
            n = rep_["n"]
            DUP_REPORT.pop(owner, None)
            with speak(owner):
                safe("sendMessage", chat_id=owner, text=L(
                    f"Пропущено {n} {plural_ru(n, 'повтор', 'повтора', 'повторов')}: эти кадры уже были в ленте или удалены.",
                    f"Skipped {n} duplicate(s): these frames are already in the feed or were deleted."))


def over_daily(owner):
    """Приглашённый уже загрузил за сутки DAILY_LIMIT кадров (удалённые тоже считаются)."""
    if owner == ADMIN or not DAILY_LIMIT:
        return False
    n = q("SELECT COUNT(*) AS n FROM photos WHERE owner=? AND created > ?", (owner, time.time() - 86400))[0]["n"]
    return n >= DAILY_LIMIT


DAILY_TOLD = {}      # владелец -> когда сказали про суточный лимит


def raw_twin(f, files, owner):
    """RAW+JPEG одной съёмки: оставляем JPEG (цвет камеры), RAW выбрасываем. True — файл пропустить сейчас."""
    if not RAW_FILES:
        return False
    recent = {Path(r["name"]).stem.lower(): r["name"] for r in
              q("SELECT name FROM photos WHERE owner=? AND created > ?", (owner, time.time() - 1800))}
    stem = f.stem.lower()
    if is_raw(f):
        if any(o.stem.lower() == stem and not is_raw(o) for o in files) or (stem in recent and not is_raw(recent[stem])):
            remove(str(f))
            return True
        try:
            return time.time() - f.stat().st_mtime < RAW_WAIT      # ждём: вдруг JPEG этой съёмки ещё летит
        except OSError:
            return True
    if stem in recent and is_raw(recent[stem]):                     # JPEG опоздал, RAW уже проявлен
        remove(str(f))
        return True
    return False


def _process_one(f, owner):
    if over_daily(owner):
        remove(str(f))
        if time.time() - DAILY_TOLD.get(owner, 0) > 3 * 3600:
            DAILY_TOLD[owner] = time.time()
            safe("sendMessage", chat_id=owner, text=L(
                f"За сутки уже {DAILY_LIMIT} кадров — это предел. Новые кадры пропускаю, завтра можно снова.",
                f"{DAILY_LIMIT} frames in 24 hours is the limit. New frames are skipped; try again tomorrow."))
        return
    try:
        if ingest(f, owner) is False:
            r = DUP_REPORT.setdefault(owner, {"n": 0, "since": 0.0})
            r["n"] += 1
            r["since"] = time.time()
    except Exception as e:
        log.exception("failed on %s", f.name)
        hint = L("\nЕсли это HIF — переключи камеру на JPEG.", "\nIf this is HIF, switch the camera to JPEG.") if f.suffix.lower() in (".hif", ".heif", ".heic") else ""
        safe("sendMessage", chat_id=owner, text=L("Не смог обработать", "Could not process") + f" {f.name}: {e}{hint}")
        if f.exists():
            shutil.move(str(f), udir(owner, "originals") / f"failed_{f.name}")


def ingest_loop(state):
    """Отдельный поток: забирает кадры с VPS по уведомлениям, с подстраховочным опросом."""
    last_poll = last_sweep = 0.0
    while True:
        try:
            if time.time() - last_sweep >= 600:
                last_sweep = time.time()
                sweep_vps()
            names = []
            try:
                names.append(VPS_EVENTS.get(timeout=1))
                time.sleep(0.3)                      # соберём пачку, если кадров несколько
                while True:
                    names.append(VPS_EVENTS.get_nowait())
            except queue.Empty:
                pass
            items = [own for own in map(remote_owner, filter(None, names)) if own]
            if items:
                fetch_names(items)
            interval = POLL_BACKUP if WATCH_ALIVE.is_set() else POLL
            if time.time() - last_poll >= interval:
                fetch_from_vps()
                last_poll = time.time()
            process_incoming(state)
        except Exception:
            log.exception("ingest loop")
            time.sleep(1)


# ================= устройства: «Проявка» в браузере и как приложение =================
# Вне Telegram устройство входит своим ключом. Привязка — одноразовый код на 10 минут (короткий, чтобы вписать руками,
# и он же в ссылке и QR): его дают /link в боте или уже привязанное устройство. Браузер меняет код на постоянный ключ;
# на сервере — только sha256 ключа. Ключ лежит и в localStorage, и в cookie: iPhone при добавлении на экран «Домой»
# может перенести cookie из Safari, а localStorage — нет. Отозвать — /devices или «Устройства» в «Проявке».
try:
    import segno                      # QR-коды; без него — только ссылки
except ImportError:
    segno = None

PAIR_TTL = 600                        # коды лежат в таблице pairs: их выдаёт и мастер установки (filmbot.py --pair)
SESSION_DEV = {}                      # токен сессии -> id устройства, с которого вошли
AUTH_FAILS = {}                       # ip -> времена неудачных попыток войти кодом или ключом
FAIL_WINDOW, FAIL_MAX = 600, 20
FAIL_MAX_ALL = 500                    # неудачных кодов со всех адресов за окно — дальше привязка ждёт
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # без 0/O, 1/I/L — чтобы не путать, вписывая руками
PAIR_LEN = 8                          # 31^8 ≈ 8·10^11 вариантов на 10 минут при 20 попытках с адреса
DEV_COOKIE = "proyavka_device"
ZIP_MAX = 200                         # кадров в одном архиве


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_pair(uid, kind="device"):
    """Одноразовый код (и ссылка с ним): kind=device — войти новым устройством, tg — привязать Telegram."""
    if kind == "device" and not WEBAPP_URL:
        raise RuntimeError(L("«Проявка» не настроена: пустой WEBAPP_URL", "Proyavka is not set up: WEBAPP_URL is empty"))
    now = time.time()
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_LEN))
    run("DELETE FROM pairs WHERE exp < ?", (now,))
    run("INSERT INTO pairs(code, uid, exp, kind) VALUES (?,?,?,?)", (code, uid, now + PAIR_TTL, kind))
    return code, f"{WEBAPP_URL.rstrip('/')}/#pair={code}"      # код в «#» не уходит на сервер и в журналы nginx


def take_pair(code, kind):
    """Погасить код: пользователь или None. Срабатывает один раз."""
    code = norm_code(code)
    with DB_LOCK:
        row = db.execute("SELECT uid, exp FROM pairs WHERE code=? AND kind=?", (code, kind)).fetchone()
        if row:
            db.execute("DELETE FROM pairs WHERE code=?", (code,))
            db.commit()
    return row[0] if row and row[1] >= time.time() else None


def make_pair(uid):
    return new_pair(uid)[1]


def show_code(code):
    return f"{code[:4]}-{code[4:]}"


def norm_code(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def qr_svg(text):
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff") if segno else None


def qr_png(text):
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=10, border=3)
    return buf.getvalue()


def clean_device_name(name):
    name = re.sub(r"[\x00-\x1f<>]", "", str(name or ""))[:60].strip()
    return name or L("Браузер", "Browser")


def pair_device(code, name):
    """Код из ссылки -> (ключ, пользователь, id устройства) или None. Код срабатывает один раз."""
    uid = take_pair(code, "device")
    if uid not in USERS:
        return None
    return create_device(uid, name)


def create_device(uid, name):
    key = secrets.token_urlsafe(32)
    now = time.time()
    did = run("INSERT INTO devices(owner, name, hash, created, seen) VALUES (?,?,?,?,?)",
              (uid, clean_device_name(name), key_hash(key), now, now))
    log.info("устройство #%d привязано к %d", did, uid)
    return key, uid, did


def device_by_key(key):
    key = str(key or "")
    if not 20 <= len(key) <= 100:
        return None
    rows = q("SELECT * FROM devices WHERE hash=?", (key_hash(key),))
    if not rows or rows[0]["owner"] not in USERS:
        return None
    run("UPDATE devices SET seen=? WHERE id=?", (time.time(), rows[0]["id"]))
    return rows[0]


def user_devices(uid):
    return q("SELECT id, name, created, seen FROM devices WHERE owner=? ORDER BY seen DESC", (uid,))


def drop_device(uid, did):
    n = run_count("DELETE FROM devices WHERE id=? AND owner=?", (did, uid))
    if n:
        run("DELETE FROM push_subs WHERE device=?", (did,))
    for tok in [t for t, d in SESSION_DEV.items() if d == did]:
        SESSION_DEV.pop(tok, None)
        SESSIONS.pop(tok, None)
    return n


def too_many_fails(ip, everyone=False):
    now = time.time()
    for k in [ip] + (["*"] if everyone else []):
        if k:
            AUTH_FAILS[k] = [t for t in AUTH_FAILS.get(k, []) if now - t < FAIL_WINDOW]
    if everyone and len(AUTH_FAILS.get("*", [])) >= FAIL_MAX_ALL:     # подбор с множества адресов сразу
        return True
    return bool(ip) and len(AUTH_FAILS.get(ip, [])) >= FAIL_MAX


def note_fail(ip):
    AUTH_FAILS.setdefault("*", []).append(time.time())
    if ip:
        AUTH_FAILS.setdefault(ip, []).append(time.time())
    time.sleep(0.5)          # подбирать 128-битный код и так безнадёжно, а так ещё и медленно


def devices_screen(uid):
    rows = user_devices(uid)
    if not rows:
        return L("Устройств без Telegram пока нет. /link — ссылка и QR, чтобы открыть «Проявку» в браузере "
                 "на компьютере или поставить на телефон как приложение.",
                 "No devices outside Telegram yet. /link gives a link and a QR code to open Proyavka in a browser "
                 "on a computer or install it on a phone as an app."), None
    lines = [L("Где открыта «Проявка» без Telegram:", "Where Proyavka is open outside Telegram:")]
    lines += [f"• {r['name']} — " + L("заходил ", "last seen ") + datetime.fromtimestamp(r["seen"]).strftime("%d.%m %H:%M")
              for r in rows]
    lines.append(L("\nНажми на устройство, чтобы отключить его.", "\nTap a device to remove it."))
    return "\n".join(lines), {"inline_keyboard": [[btn("✕ " + r["name"][:30], f"dv:{r['id']}")] for r in rows]}


def send_link(uid):
    try:
        code, link = new_pair(uid)
    except RuntimeError as e:
        tg("sendMessage", chat_id=uid, text=str(e))
        return
    site = html_esc(WEBAPP_URL)
    text = L(f"Код для нового устройства: <code>{show_code(code)}</code>\n(нажми на код — он скопируется; одноразовый, 10 минут)\n\n"
             f"<b>iPhone:</b> открой {site} в Safari → «Поделиться» → «На экран „Домой“», запусти «Проявку» с иконки "
             "и вставь код. Вход из браузера в приложение на iPhone не переносится — код вводится уже в приложении.\n"
             "<b>Android, компьютер:</b> наведи камеру на QR или открой ссылку кнопкой ниже, потом "
             "«Установить приложение» в меню браузера.\n\nСписок устройств и отключение — /devices.",
             f"Code for a new device: <code>{show_code(code)}</code>\n(tap the code to copy it; single use, 10 minutes)\n\n"
             f"<b>iPhone:</b> open {site} in Safari → Share → Add to Home Screen, launch Proyavka from the icon and "
             "paste the code. On iPhone a browser sign-in does not carry over to the app — enter the code in the app.\n"
             "<b>Android, computer:</b> point the camera at the QR code or open the link with the button below, then "
             "Install app in the browser menu.\n\nDevices and removing them: /devices.")
    kb = {"inline_keyboard": [[{"text": L("Открыть в браузере", "Open in browser"), "url": link}]]}
    if segno:
        tg("sendPhoto", files={"photo": ("qr.png", qr_png(link))}, chat_id=uid, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        tg("sendMessage", chat_id=uid, text=text, reply_markup=kb, disable_web_page_preview=True, parse_mode="HTML")


def html_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


BOT_RESET = threading.Event()        # бота подключили или сменили — читать обновления с начала


def connect_bot(token):
    """Подключить Telegram-бота из настроек «Проявки»: проверить токен, сохранить в config.env, включить без перезапуска."""
    global BOT_TOKEN
    token = str(token or "").strip()
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise ValueError(L("не похоже на токен: цифры, двоеточие, длинная строка", "doesn't look like a token: digits, a colon, a long string"))
    try:
        j = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as e:
        raise ValueError(L("Telegram недоступен", "Telegram is unreachable") + f": {e}")
    if not j.get("ok"):
        raise ValueError(L("Telegram не принял токен", "Telegram rejected the token") + f": {j.get('description')}")
    save_config("BOT_TOKEN", token)
    BOT_TOKEN = token
    BOT_NAME.clear()
    BOT_NAME["u"] = j["result"]["username"]
    BOT_RESET.set()
    safe("deleteWebhook")
    log.info("подключён бот @%s", BOT_NAME["u"])
    return BOT_NAME["u"]


def save_config(key, value):
    """Поменять одну строку в config.env (остальное как было)."""
    path = Path(os.environ.get("CONFIG_FILE") or Path(__file__).resolve().parent.parent / "config.env")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def bot_name_safe():
    try:
        return bot_username() if BOT_TOKEN else None
    except Exception:
        return None


def me_json(uid):
    u = user(uid) or {}
    n = q("SELECT SUM(hidden=0) AS n, SUM(hidden=1 AND work IS NOT NULL) AS t FROM photos WHERE owner=?", (uid,))[0]
    return {"id": uid, "name": u.get("name") or "", "admin": uid == ADMIN, "lang": user_lang(uid),
            "default_film": u.get("default_film") or "auto", "used": sum(user_usage(uid).values()),
            "limit": storage_limit(uid) * 1e9, "frames": n["n"] or 0, "trash": n["t"] or 0, "orig_days": ORIG_DAYS,
            "telegram": {"bot": bot_name_safe(), "linked": bool(u.get("tg")),
                         "can_unlink": uid >= WEB_BASE and bool(u.get("tg"))}}


def set_me(uid, data):
    kw = {}
    if "lang" in data:
        if data["lang"] not in ("ru", "en"):
            raise ValueError("lang")
        kw["lang"] = data["lang"]
    if "default_film" in data:
        k = canon(str(data["default_film"]))
        if k != "auto" and not valid_look(k, uid):
            raise ValueError(L("неизвестная плёнка", "unknown film"))
        kw["default_film"] = k
    if "name" in data:
        name = re.sub(r"[\x00-\x1f<>]", "", str(data["name"])).strip()[:40]
        if name:
            kw["name"] = name
    if kw:
        set_user(uid, **kw)
        if "lang" in kw:
            with speak(uid):
                set_commands(uid)
    return me_json(uid)


def users_json():
    rows = q("SELECT u.*, (SELECT COUNT(*) FROM photos p WHERE p.owner=u.id AND p.hidden=0) AS n FROM users u "
             "ORDER BY u.role='admin' DESC, u.created")
    return [{"id": u["id"], "name": u["name"] or (L("Администратор", "Admin") if u["id"] == ADMIN else str(u["id"])), "admin": u["id"] == ADMIN, "frames": u["n"],
             "used": sum(user_usage(u["id"]).values()), "limit": storage_limit(u["id"]) * 1e9, "telegram": bool(u["tg"])}
            for u in rows]


def camera_json(uid):
    domain = os.environ.get("DOMAIN", "")
    ftp_user, pw, conf = camera_access(uid)
    guide = f"{PROJECT_URL}/blob/main/docs"
    ext = ".md" if cur_lang() == "en" else ".ru.md"
    return {"domain": domain, "ftp_user": ftp_user, "ftp_pass": pw, "config": bool(conf) or CAMERA_CONFIG.exists(),
            "cert": FTP_ROOT_CERT.exists(), "guide_app": f"{guide}/sony-app{ext}", "guide_ftp": f"{guide}/ftp-cameras{ext}"}


def download_name(ph):
    return f"{Path(ph['name']).stem}_{ph['preset']}.jpg"


def disposition(name):
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "photo.jpg"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


ICONS = {}


def app_icon(size):
    """Значок приложения: светлое пятно с ореолом халяции на чёрном (рисуется один раз)."""
    if size not in ICONS:
        s = size * 2
        c, r = s / 2, s * 0.24
        glow = Image.new("RGB", (s, s))
        ImageDraw.Draw(glow).ellipse((c - r * 1.25, c - r * 1.25, c + r * 1.25, c + r * 1.25), fill=(255, 72, 24))
        im = glow.filter(ImageFilter.GaussianBlur(s * 0.07))
        ImageDraw.Draw(im).ellipse((c - r, c - r, c + r, c + r), fill=(255, 248, 236))
        buf = io.BytesIO()
        im.resize((size, size), Image.LANCZOS).save(buf, "PNG", optimize=True)
        ICONS[size] = buf.getvalue()
    return ICONS[size]


def manifest():
    name = L("Проявка", "Proyavka")
    icons = [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"} for n in (192, 512)]
    return {"name": name, "short_name": name, "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#000000", "theme_color": "#000000", "icons": icons}


# Сервис-воркер нужен, чтобы «Проявку» можно было поставить как приложение. Хранит только саму страницу —
# на случай, если сеть пропала; кадры и API идут мимо него.
SW_JS = """const CACHE = "proyavka-shell-v1";
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (x) {}
  e.waitUntil(self.registration.showNotification(d.title || "Proyavka", {
    body: d.body || "", tag: d.tag || "proyavka", renotify: true, icon: "/icon-192.png", badge: "/icon-192.png",
    data: { url: d.url || "/" } }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) if ("focus" in c) return c.focus();
    return self.clients.openWindow((e.notification.data && e.notification.data.url) || "/");
  }));
});
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate" || new URL(e.request.url).pathname !== "/") return;
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); }
    return res;
  }).catch(() => caches.match("/")));
});
"""

# Страница — из одного файла, поэтому скрипт и стили встроенные; зато грузить что-то с чужих адресов и
# отправлять куда-то, кроме своего сервера, ей нельзя.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")


# ================= уведомления (Web Push) =================
# «Проявлено 3 новых кадра» на телефон и компьютер, когда «Проявка» закрыта. Подписка — у каждого устройства своя
# (включается и выключается в настройках). Кадры, проявленные подряд, — одним уведомлением. Если приложение открыто
# (оно спрашивает сервер каждые 1–5 с), не уведомляем: кадр и так виден. Библиотека pywebpush необязательна.
try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:
    webpush = None

PUSH_DELAY = 20            # сек: собрать кадры, пришедшие подряд
PUSH_QUIET = 30            # сек: приложение спрашивало сервер недавно — оно открыто
LAST_POLL = {}             # устройство -> когда его приложение последний раз спрашивало сервер (открыто ли)
PUSH_PENDING = {}          # пользователь -> сколько новых кадров ждут уведомления
PUSH_LOCK = threading.Lock()
_VAPID = {}


def vapid():
    """Ключ сервера для Web Push: создаётся один раз и лежит рядом с базой."""
    with PUSH_LOCK:
        if "v" not in _VAPID:
            path = BASE / "vapid.pem"
            if path.exists():
                v = Vapid02.from_file(str(path))
            else:
                v = Vapid02()
                v.generate_keys()
                v.save_key(str(path))
                os.chmod(path, 0o600)
            raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            _VAPID["v"], _VAPID["pub"] = v, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return _VAPID["v"]


def push_key():
    vapid()
    return _VAPID["pub"]


def push_subscribe(uid, did, sub):
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or len(endpoint) > 2000 or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError(L("неверная подписка", "invalid subscription"))
    run("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
    run("INSERT INTO push_subs(owner, device, endpoint, p256dh, auth, created) VALUES (?,?,?,?,?,?)",
        (uid, did, endpoint, str(keys["p256dh"])[:200], str(keys["auth"])[:100], time.time()))
    log.info("push: подписка %s… от %d, устройство #%s", endpoint[:40], uid, did)


def push_soon(uid):
    if not webpush:
        return
    with PUSH_LOCK:
        n = PUSH_PENDING.get(uid, 0)
        PUSH_PENDING[uid] = n + 1
    if not n:
        t = threading.Timer(PUSH_DELAY, _push_flush, args=(uid,))
        t.daemon = True
        t.start()


def _push_flush(uid):
    with PUSH_LOCK:
        n = PUSH_PENDING.pop(uid, 0)
    now = time.time()
    # тишина у каждого устройства своя: открытое окно на компьютере не глушит телефон
    subs = [s for s in q("SELECT * FROM push_subs WHERE owner=?", (uid,))
            if not (s["device"] and now - LAST_POLL.get(s["device"], 0) < PUSH_QUIET)]
    if not n or not subs:
        return
    with speak(uid):
        body = (L("Проявлен новый кадр", "A new frame is developed") if n == 1 else
                L(f"Проявлено {n} {plural_ru(n, 'новый кадр', 'новых кадра', 'новых кадров')}", f"{n} new frames developed"))
        data = json.dumps({"title": L("Проявка", "Proyavka"), "body": body, "tag": "new-frames", "url": "/"}, ensure_ascii=False)
    sub_mail = "mailto:proyavka@" + (os.environ.get("DOMAIN") or "localhost")
    sent = 0
    for s in subs:
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}, data,
                    vapid_private_key=vapid(), vapid_claims={"sub": sub_mail}, ttl=6 * 3600, timeout=15)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):              # устройство отписалось или подписка протухла
                run("DELETE FROM push_subs WHERE id=?", (s["id"],))
                log.info("push: подписка #%d больше не действует (%s), удалена", s["id"], code)
            else:
                log.warning("push to %d: %s", uid, e)
        except Exception as e:
            log.warning("push to %d: %s", uid, e)
    log.info("уведомление «%s» для %d: отправлено на %d из %d устройств", body, uid, sent, len(subs))


# ================= альбомы по ссылке =================
# Выбранные кадры — одной ссылкой для кого угодно, без входа: смотреть и скачивать (по одному или архивом).
# В ссылке длинный случайный ключ; удалил альбом — ссылка перестала работать. Кадр, убранный в корзину, из альбома
# пропадает, новая плёнка видна сразу. Полный размер для чужих рисуется один раз и лежит в кэше превью (сутки).
ALBUM_HTML = Path(__file__).resolve().parent / "album.html"
ALBUM_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
             "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
ALBUM_ZIPS = set()                    # альбомы, чей архив сейчас собирается: по одному за раз
FULL_LOCKS = collections.defaultdict(threading.Lock)
ZIP_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zip")


def album_url(tok):
    return f"{WEBAPP_URL.rstrip('/')}/a/{tok}"


def clean_title(t):
    return re.sub(r"[\x00-\x1f<>]", "", str(t or "")).strip()[:80]


def album_rows(a, pid=None):
    """Кадры альбома, которые можно показать: не в корзине, уже проявленные, по времени съёмки."""
    one = " AND p.id=?" if pid is not None else ""
    return q("SELECT p.* FROM album_photos ap JOIN photos p ON p.id=ap.photo WHERE ap.album=? AND p.owner=? "
             f"AND p.hidden=0 AND p.view IS NOT NULL{one} ORDER BY p.taken, p.id",
             (a["id"], a["owner"], *(() if pid is None else (pid,))))


def album_json(a):
    rows = album_rows(a)
    ids = [r["photo"] for r in q("SELECT ap.photo FROM album_photos ap JOIN photos p ON p.id=ap.photo "
                                 "WHERE ap.album=? AND p.hidden=0", (a["id"],))]
    return {"id": a["id"], "title": a["title"] or "", "url": album_url(a["token"]), "n": len(rows), "ids": ids,
            "cover": rows[0]["id"] if rows else None, "created": a["created"], "views": a["views"] or 0}


def user_albums(uid):
    return [album_json(a) for a in q("SELECT * FROM albums WHERE owner=? ORDER BY created DESC", (uid,))]


def my_album(uid, aid):
    rows = q("SELECT * FROM albums WHERE id=? AND owner=?", (aid, uid))
    if not rows:
        raise ValueError(L("нет такого альбома", "no such album"))
    return rows[0]


def album_photo_ids(uid, data):
    ids = batch_ids(data, uid)
    if not ids:
        raise ValueError(L("в альбоме нет ни одного кадра", "the album has no frames"))
    return ids


def save_album_photos(aid, ids):
    with DB_LOCK:
        db.execute("DELETE FROM album_photos WHERE album=?", (aid,))
        db.executemany("INSERT INTO album_photos(album, photo) VALUES (?,?)", [(aid, i) for i in ids])
        db.commit()


def make_album(uid, data):
    if not WEBAPP_URL:
        raise RuntimeError(L("«Проявка» не настроена: пустой WEBAPP_URL", "Proyavka is not set up: WEBAPP_URL is empty"))
    ids = album_photo_ids(uid, data)
    now = time.time()
    aid = run("INSERT INTO albums(owner, token, title, created, updated) VALUES (?,?,?,?,?)",
              (uid, secrets.token_urlsafe(12), clean_title(data.get("title")), now, now))
    save_album_photos(aid, ids)
    log.info("альбом #%d от %d: %d кадров", aid, uid, len(ids))
    return album_json(my_album(uid, aid))


def edit_album(uid, aid, data):
    a = my_album(uid, aid)
    if "title" in data:
        run("UPDATE albums SET title=? WHERE id=?", (clean_title(data["title"]), aid))
    if "ids" in data:
        save_album_photos(aid, album_photo_ids(uid, data))
    run("UPDATE albums SET updated=? WHERE id=?", (time.time(), aid))
    return album_json(my_album(uid, a["id"]))


def delete_album(uid, aid):
    n = run_count("DELETE FROM albums WHERE id=? AND owner=?", (aid, uid))
    if n:
        run("DELETE FROM album_photos WHERE album=?", (aid,))
    return n


def album_by_token(tok):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16}", tok or ""):
        return None
    rows = q("SELECT * FROM albums WHERE token=?", (tok,))
    return rows[0] if rows and rows[0]["owner"] in USERS else None


def album_public(a):
    rows = album_rows(a)
    return {"title": a["title"] or "", "zip_max": ZIP_MAX,
            "photos": [{"id": r["id"], "taken": r["taken"], "v": int(os.path.getmtime(r["view"])) if has(r["view"]) else 0,
                        "name": download_name(r)} for r in rows]}


def album_page(a):
    """Страница альбома: заголовок и картинка для превью ссылки в мессенджерах вписываются сервером."""
    page = ALBUM_HTML.read_text(encoding="utf-8")
    rows = album_rows(a) if a else []
    title = (a["title"] or L("Альбом", "Album")) if a else L("Альбом не найден", "Album not found")
    image = f"{album_url(a['token'])}/view/{rows[0]['id']}" if rows and WEBAPP_URL else ""
    meta = (f'<meta property="og:title" content="{html_esc(title)}">\n'
            f'<meta property="og:description" content="{html_esc(L("Проявка", "Proyavka"))} · {len(rows)}">\n'
            + (f'<meta property="og:image" content="{html_esc(image)}">\n' if image else ""))
    return (page.replace("<!--META-->", meta).replace("{{TITLE}}", html_esc(title))
                .replace("{{PROJECT}}", html_esc(PROJECT_URL)))


def full_file(ph):
    """Кадр в полном размере: рисуется один раз на каждую правку и лежит в кэше превью (чистится через сутки)."""
    path = PREVIEWS / f"{ph['id']}_full_{ph['rev'] or 0}.jpg"
    with FULL_LOCKS[ph["id"]]:
        if path.exists():
            os.utime(path)
        else:
            HEAVY.submit(job_full, ph, str(path)).result(timeout=300)
    return path


# ================= Mini App: веб-сервер =================
SESSIONS = {}          # token -> (срок годности, пользователь)
SESSION_TTL = 12 * 3600

# Картинки и файлы грузятся через <img src> и <a download>, туда не приложить заголовок, и токен приходилось класть в адрес —
# а адреса попадают в журналы nginx и историю браузера. Поэтому в адрес идёт не сессия, а отдельный токен «только смотреть и
# скачивать свои кадры»: подписан ключом сервера, живёт 1–2 суток и сутки остаётся тем же (кэш браузера работает).
MEDIA_KEY = None


def media_secret():
    global MEDIA_KEY
    if MEDIA_KEY is None:
        path = BASE / "media.key"
        try:
            MEDIA_KEY = path.read_bytes()
        except OSError:
            MEDIA_KEY = secrets.token_bytes(32)
            path.write_bytes(MEDIA_KEY)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    return MEDIA_KEY


def _media_sig(uid, exp):
    return base64.urlsafe_b64encode(hmac.new(media_secret(), f"{uid}.{exp}".encode(), hashlib.sha256).digest()[:16]).decode().rstrip("=")


def media_token(uid):
    exp = (int(time.time() // 86400) + 2) * 86400
    return f"{uid}.{exp}.{_media_sig(uid, exp)}"


def media_uid(tok):
    """Пользователь по токену для картинок или None."""
    try:
        uid, exp, sig = str(tok).split(".")
        uid, exp = int(uid), int(exp)
    except ValueError:
        return None
    if exp <= time.time() or uid not in USERS or not hmac.compare_digest(sig, _media_sig(uid, exp)):
        return None
    return uid


def check_init_data(init_data):
    """Проверка подписи Telegram. Возвращает id пользователя Telegram (пускать ли его — решает список users)."""
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
        return int(json.loads(pairs.get("user", "{}")).get("id") or 0) or False
    except (ValueError, TypeError):
        return False


def photo_json(ph):
    v = int(os.path.getmtime(ph["view"])) if has(ph.get("view")) else 0
    return {"id": ph["id"], "taken": ph["taken"], "iso": ph["iso"],
            "preset": ph["preset"], "preset_name": pname(ph["preset"], ph["owner"]),
            "auto_key": ph["auto_key"], "auto_reason": auto_reason(ph) if ph["auto_reason"] else "",
            "strength": ph["strength"], "stamp": bool(ph["stamp"]), "frame": bool(ph["frame"]),
            "leak": (ph.get("leak_kind") or "edge") if ph["leak"] else "",
            "leak_seed": int(ph.get("leak_seed") or 0), "archived": not has(ph["work"]), "original": has(ph["src"]),
            "ready": bool(v), "v": v, "pending": (ph["rev"] or 0) != (ph["rendered_rev"] or 0),
            "exporting": EXPORTING.get(ph["id"], 0) > 0, "hidden": bool(ph["hidden"]),
            "crop": [float(t) for t in ph["crop"].split(",")] if ph.get("crop") else None}


def preview_file(ph, key, strength, leak="", lseed=0, crop=None):
    tag = (f"_{leak}{lseed}" if leak else "") + crop_tag(crop)
    path = PREVIEWS / f"{ph['id']}_{key}_{strength}{tag}.jpg"
    if path.exists():
        return path
    snap = dict(ph, leak_seed=lseed, crop=crop)
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


def batch_ids(data, uid):
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BATCH_MAX:
        raise ValueError(L(f"выбери от 1 до {BATCH_MAX} кадров", f"select 1 to {BATCH_MAX} frames"))
    try:
        ids = list(dict.fromkeys(int(i) for i in ids))
    except (TypeError, ValueError):
        raise ValueError(L("неверный список кадров", "invalid frame list"))
    marks = ",".join("?" * len(ids))
    return [r["id"] for r in q(f"SELECT id FROM photos WHERE hidden=0 AND owner=? AND id IN ({marks}) ORDER BY id",
                               (uid, *ids))]


def batch_edit(ids, changes, uid):
    """Плёнка, засвет, сила для многих кадров: в базу сразу, рисуются очередью (срочность 1)."""
    if not isinstance(changes, dict):
        raise ValueError(L("нет изменений", "no changes"))
    changes = {k: v for k, v in changes.items() if k in ("preset", "strength", "leak")}
    if not changes:
        raise ValueError(L("нет изменений", "no changes"))
    if "preset" in changes and changes["preset"] != "auto" and not valid_look(canon(str(changes["preset"])), uid):
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
        parts.append(L("засвет", "leak") + f": {tr(LEAKS[changes['leak']][0])}" if changes["leak"] in LEAKS
                     else L("без засвета", "no leak"))
    live = [pid for pid in ids if has((get(pid) or {}).get("work"))]
    bid = batch_track(live, ", ".join(parts), uid)   # до постановки в очередь: быстрый кадр не должен проскочить мимо учёта
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


def batch_action(data, uid):
    """Действия над выбранными кадрами из «Проявки»: правка, удаление, файлы."""
    action = data.get("action")
    ids = batch_ids(data, uid)
    if action == "edit":
        return batch_edit(ids, data.get("changes"), uid)
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


def receive_upload(stream, length, name, uid):
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
        raw_ext = Path(name).suffix.lower()
        if RAW_FILES and raw_ext in RAW_EXTS and (head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIU\x00")
                                                  or head[4:8] == b"ftyp" or head[:8] == b"FUJIFILM"):
            ext = raw_ext
        if not ext:
            raise ValueError(L("это не фото (нужен JPEG, HEIC, PNG или WebP)", "not a photo (JPEG, HEIC, PNG or WebP expected)"))
        try:
            if ext in RAW_EXTS:
                w = h_ = 0                             # размер RAW проверит open_raw при проявке
            else:
                with Image.open(tmp) as im:            # только заголовок: размер, без разбора всего файла
                    w, h_ = im.size
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError(L("слишком большое изображение", "the image is too large"))
        except Exception:
            raise ValueError(L("файл повреждён или это не фото", "the file is damaged or not a photo"))
        if w * h_ > Image.MAX_IMAGE_PIXELS:
            raise ValueError(L("слишком большое изображение", "the image is too large"))
        fp = f"{h.hexdigest()}:{length}"
        dup = q("SELECT id, hidden FROM photos WHERE fp=? AND owner=? LIMIT 1", (fp, uid))
        if dup:     # удалённые кадры тоже помнятся по отпечатку — повторная загрузка их не вернёт
            return {"ok": True, "duplicate": dup[0]["id"], "deleted": bool(dup[0]["hidden"])}
        with PENDING_LOCK:      # тот же файл уже принят и ждёт обработки (выбрали одно фото дважды)
            if (uid, fp) in PENDING_FP:
                return {"ok": True, "duplicate": -1, "deleted": False}
            PENDING_FP.add((uid, fp))
        stem = "".join(c for c in Path(name).stem if c.isalnum() or c in "-_.")[:40] or "photo"
        inbox = udir(uid, "incoming")
        dst = inbox / f"{stem}{ext}"
        while dst.exists():
            dst = inbox / f"{stem}_{secrets.token_hex(2)}{ext}"
        try:
            os.replace(tmp, dst)
        except OSError:
            with PENDING_LOCK:
                PENDING_FP.discard((uid, fp))
            raise
        VPS_EVENTS.put("")                     # разбудить приём, не ждать секунду
        return {"ok": True, "duplicate": None}
    finally:
        remove(str(tmp))


class Handler(BaseHTTPRequestHandler):
    server_version = "filmbot"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.common_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")       # токен для картинок бывает в ссылках

    def device_cookie(self, key):
        return {"Set-Cookie": f"{DEV_COOKIE}={key}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Strict"}

    def cookie_key(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return ""
        return c[DEV_COOKIE].value if DEV_COOKIE in c else ""

    def ip(self):
        """Адрес клиента от nginx (бот слушает только 127.0.0.1). Без заголовка — неизвестен."""
        return self.headers.get("X-Real-IP")

    def new_session(self, uid, old="", did=None):
        now = time.time()
        for k in [k for k, v in SESSIONS.items() if v[0] < now]:
            SESSIONS.pop(k, None)
            SESSION_DEV.pop(k, None)
        # повторный вход из уже открытой ленты продлевает прежний токен: на нём ссылки на все картинки
        old = str(old or "")
        was = SESSIONS.get(old)
        tok = old if len(old) >= 32 and (was is None or was[1] == uid) and SESSION_DEV.get(old) in (None, did) else secrets.token_urlsafe(24)
        SESSIONS[tok] = (now + SESSION_TTL, uid)
        if did:
            SESSION_DEV[tok] = did
        return tok

    def send_full(self, ph):
        """Кадр в полном размере файлом — «Скачать» вне Telegram."""
        if has(ph["work"]) or has(ph["src"]):
            out = TMP / f"dl_{ph['id']}_{secrets.token_hex(4)}.jpg"
            try:
                HEAVY.submit(job_full, ph, str(out)).result(timeout=300)
                body = out.read_bytes()
            finally:
                remove(str(out))
        elif has(ph["view"]):                  # исходник удалён ради места — отдаём то, что осталось
            body = Path(ph["view"]).read_bytes()
        else:
            return self.err(404, L("нет файла", "no file"))
        return self.send(200, body, "image/jpeg", "private, no-store", {"Content-Disposition": disposition(download_name(ph))})

    def send_zip(self, rows, name=None, cached=False):
        """Несколько кадров одним архивом. Пишется на ходу: кадр проявился — сразу ушёл, nginx не ждёт весь архив.
        cached — для альбомов: полные кадры берутся из кэша (и остаются в нём для следующих скачиваний)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", disposition(name or f"proyavka_{datetime.now():%Y-%m-%d_%H%M}.zip"))
        self.send_header("Cache-Control", "no-store")
        self.common_headers()
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def start(ph):
            if not (has(ph["work"]) or has(ph["src"])):
                return ph, None, None
            if cached:
                return ph, None, ZIP_POOL.submit(full_file, ph)
            out = TMP / f"zip_{ph['id']}_{secrets.token_hex(4)}.jpg"
            return ph, out, HEAVY.submit(job_full, ph, str(out))

        todo, jobs, names = list(rows), [], set()
        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                while todo or jobs:
                    while todo and len(jobs) < 2:          # следующий кадр проявляется, пока текущий уходит
                        jobs.append(start(todo.pop(0)))
                    ph, out, fut = jobs.pop(0)
                    try:
                        got = fut.result(timeout=300) if fut else None
                        src = (out or got) if fut else (ph["view"] if has(ph["view"]) else None)
                        if not src:
                            continue
                        name = download_name(ph)
                        while name in names:
                            name = f"{Path(name).stem}_{ph['id']}.jpg"
                        names.add(name)
                        z.write(src, name)
                    finally:
                        if out:
                            remove(str(out))
        finally:
            for ph, out, fut in jobs:              # браузер оборвал скачивание — убрать недоделанное
                if fut and out:
                    fut.add_done_callback(lambda f, o=out: remove(str(o)))

    def album_get(self, rest):
        """/a/<ключ>[/list|thumb/<id>|view/<id>|full/<id>|zip] — без входа, только кадры этого альбома."""
        a = album_by_token(rest[0]) if rest else None
        html = "text/html; charset=utf-8"
        hdr = {"Content-Security-Policy": ALBUM_CSP, "X-Robots-Tag": "noindex"}
        if not a:
            time.sleep(0.3)                     # ключ на 96 бит не подобрать, но и спешить незачем
            if len(rest) == 1:
                return self.send(404, album_page(None), html, headers=hdr)
            return self.err(404, L("альбом не найден", "album not found"))
        _CTX.lang = user_lang(a["owner"])
        if len(rest) == 1:
            run("UPDATE albums SET views=views+1 WHERE id=?", (a["id"],))
            return self.send(200, album_page(a), html, headers=hdr)
        if rest[1:] == ["list"]:
            return self.js(album_public(a))
        if rest[1:] == ["zip"]:
            rows = album_rows(a)[:ZIP_MAX]
            if not rows:
                return self.err(404, L("кадры не найдены", "frames not found"))
            if a["id"] in ALBUM_ZIPS:
                return self.err(429, L("архив уже собирается — попробуй через минуту", "the archive is being built — try again in a minute"))
            ALBUM_ZIPS.add(a["id"])
            try:
                name = re.sub(r"[\\/:*?\"<>|]", "_", a["title"] or "") or "proyavka"
                return self.send_zip(rows, f"{name}.zip", cached=True)
            finally:
                ALBUM_ZIPS.discard(a["id"])
        if len(rest) == 3 and rest[1] in ("thumb", "view", "full") and rest[2].isdigit():
            rows = album_rows(a, int(rest[2]))
            if not rows:
                return self.err(404, L("кадр не найден", "frame not found"))
            ph = rows[0]
            if rest[1] != "full":
                if not has(ph[rest[1]]):
                    return self.err(404, L("нет файла", "no file"))
                with open(ph[rest[1]], "rb") as f:
                    return self.send(200, f.read(), "image/jpeg", "public, max-age=86400")
            if has(ph["work"]) or has(ph["src"]):
                path = full_file(ph)
            else:                                # исходник удалён ради места — отдаём то, что осталось
                path = Path(ph["view"])
            return self.send(200, path.read_bytes(), "image/jpeg", "no-store",
                             {"Content-Disposition": disposition(download_name(ph))})
        return self.err(404, L("не найдено", "not found"))

    def js(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False))

    def err(self, code, text):
        self.js({"error": text}, code)

    def authed(self, qs, media=False):
        """Пользователь по токену сессии (или None). Заодно язык ответов — его.
        Для картинок и файлов (media) годится и токен из адреса: ?m= — токен для картинок; ?s= — прежний вид (сессия
        в адресе), оставлен на время перехода: открытые у людей страницы ещё присылают его. Убрать в следующей версии."""
        tok = self.headers.get("X-Token") or ((qs.get("s") or [""])[0] if media else "")
        exp, uid = SESSIONS.get(tok) or (0, None)
        now = time.time()
        if not tok or exp <= now or uid not in USERS:
            if media and not self.headers.get("X-Token"):
                muid = media_uid((qs.get("m") or [""])[0])
                if muid:
                    _CTX.lang = user_lang(muid)
                    return muid
            return None
        if exp - now < SESSION_TTL - 600:      # пока «Проявкой» пользуются, сессия продлевается сама
            SESSIONS[tok] = (now + SESSION_TTL, uid)
        _CTX.lang = user_lang(uid)
        return uid

    def drain(self, length):
        left = min(length, UPLOAD_MAX)
        while left > 0:
            chunk = self.rfile.read(min(left, 262144))
            if not chunk:
                break
            left -= len(chunk)

    def mine(self, pid, uid):
        """Кадр, только если он этого пользователя: чужие для него не существуют."""
        try:
            ph = get(int(pid))
        except ValueError:
            return None
        return ph if ph and ph["owner"] == uid else None

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
                return self.send(200, page, "text/html; charset=utf-8", headers={"Content-Security-Policy": CSP})
            if parts == ["manifest.webmanifest"]:
                return self.send(200, json.dumps(manifest(), ensure_ascii=False), "application/manifest+json", "max-age=3600")
            if parts == ["sw.js"]:
                return self.send(200, SW_JS, "text/javascript; charset=utf-8", "no-cache")
            if parts in (["icon-192.png"], ["icon-512.png"], ["apple-touch-icon.png"]):
                size = 180 if parts[0].startswith("apple") else int(parts[0][5:8])
                return self.send(200, app_icon(size), "image/png", "max-age=86400")
            if parts[0] == "a":
                return self.album_get(parts[1:])
            files = parts[:1] == ["img"] or parts == ["api", "zip"] or (parts[:2] == ["api", "camera"] and len(parts) == 3)
            uid = self.authed(qs, media=files)
            if not uid:
                return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
            if parts == ["api", "presets"]:
                items = [{"key": "original", "name": L("Оригинал", "Original"), "when": L("без обработки", "unprocessed")}]
                items += [{"key": k, "name": p["name"], "when": tr(p["when"]), "desc": tr(p["desc"]),
                           "p": params_json(clean_params(p))} for k, p in PRESETS.items()]     # p — основа для редактора
                items += [{"key": f"lut{r['id']}", "name": r["name"], "desc": "", "lut": True,
                           "when": f"LUT {r['size']}³" if r["size"] else L("Своя плёнка", "Your film"),
                           **({"look": True} if not r["size"] else {})}
                          for r in user_luts(uid)]
                leaks = [{"key": k, "name": tr(v[0]), "desc": tr(v[1])} for k, v in LEAKS.items()]
                return self.js({"presets": items, "strengths": STRENGTHS, "leaks": leaks})
            if parts == ["api", "me"]:
                return self.js(me_json(uid))
            if parts == ["api", "community"]:
                return self.js(community_json(uid))
            if len(parts) == 3 and parts[:2] == ["api", "look"]:
                return self.js(user_look(uid, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "look"] and parts[3] == "share":
                return self.js(look_share(uid, parts[2]))
            if len(parts) == 3 and parts[:2] == ["img", "look"]:
                ph = self.mine((qs.get("p") or [""])[0], uid)
                if not ph or ph["hidden"] or not has(ph["work"]):
                    return self.err(404, L("нет кадра", "no frame"))
                path = community_preview(ph, parts[2])
                if not path:
                    return self.err(404, L("нет такой плёнки", "no such film"))
                return self.file(str(path))
            if parts == ["api", "albums"]:
                return self.js({"albums": user_albums(uid)})
            if parts == ["api", "trash"]:
                rows = q("SELECT * FROM photos WHERE owner=? AND hidden=1 AND work IS NOT NULL "
                         "ORDER BY deleted_at DESC, id DESC LIMIT 300", (uid,))
                return self.js({"photos": [photo_json(r) for r in rows]})
            if parts == ["api", "camera"]:
                return self.js(camera_json(uid))
            if parts in (["api", "camera", "config.txt"], ["api", "camera", "cacert.pem"]):
                if parts[2] == "cacert.pem":
                    body = FTP_ROOT_CERT.read_bytes() if FTP_ROOT_CERT.exists() else None
                else:
                    body = camera_access(uid)[2]
                if not body:
                    return self.err(404, L("нет файла", "no file"))
                return self.send(200, body, "application/octet-stream", "no-store", {"Content-Disposition": disposition(parts[2])})
            if parts == ["api", "users"]:
                if uid != ADMIN:
                    return self.err(403, L("только для администратора", "admin only"))
                return self.js({"users": users_json(), "limits": LIMITS_GB})
            if parts == ["api", "devices"]:
                me = SESSION_DEV.get(self.headers.get("X-Token") or "")
                return self.js({"devices": [dict(r, current=r["id"] == me) for r in user_devices(uid)]})
            if len(parts) == 3 and parts[:2] == ["img", "full"]:
                ph = self.mine(parts[2], uid)
                if not ph or ph["hidden"]:
                    return self.err(404, L("кадр не найден", "frame not found"))
                return self.send_full(ph)
            if parts == ["api", "zip"]:
                ids = [int(x) for x in (qs.get("ids") or [""])[0].split(",") if x.isdigit()][:ZIP_MAX]
                rows = [ph for ph in (self.mine(i, uid) for i in ids) if ph and not ph["hidden"]]
                if not rows:
                    return self.err(404, L("кадры не найдены", "frames not found"))
                return self.send_zip(rows)
            if parts == ["api", "push"]:
                return self.js({"supported": bool(webpush), "key": push_key() if webpush else None})
            if parts == ["api", "updates"]:
                LAST_POLL[SESSION_DEV.get(self.headers.get("X-Token") or "") or ("u", uid)] = time.time()
                since = float((qs.get("since") or ["0"])[0])
                now = time.time()
                rows = q("SELECT * FROM photos WHERE owner=? AND updated > ? ORDER BY id DESC LIMIT 500", (uid, since))
                total = q("SELECT COUNT(*) AS n FROM photos WHERE owner=? AND hidden=0", (uid,))[0]["n"]
                return self.js({"now": now, "total": total, "jobs": jobs_in_work(uid), "media": media_token(uid),
                                "photos": [photo_json(r) for r in rows]})
            if parts == ["api", "photos"]:
                off = int((qs.get("offset") or ["0"])[0])
                lim = min(120, int((qs.get("limit") or ["60"])[0]))
                total = q("SELECT COUNT(*) AS n FROM photos WHERE owner=? AND hidden=0", (uid,))[0]["n"]
                rows = q("SELECT * FROM photos WHERE owner=? AND hidden=0 ORDER BY taken DESC, id DESC LIMIT ? OFFSET ?",
                         (uid, lim, off))
                return self.js({"total": total, "photos": [photo_json(r) for r in rows]})
            if len(parts) == 3 and parts[0] == "img" and parts[1] in ("thumb", "view"):
                ph = self.mine(parts[2], uid)
                return self.file(ph and ph[parts[1]])
            if len(parts) == 4 and parts[:2] == ["img", "preview"]:
                ph = self.mine(parts[2], uid)
                key = canon(parts[3])
                strength = int((qs.get("st") or ["100"])[0])
                leak = (qs.get("lk") or [""])[0]
                lseed = int((qs.get("ls") or ["0"])[0])
                if (not ph or not valid_look(key, uid) or strength not in STRENGTHS
                        or (leak and leak not in LEAKS)):
                    return self.err(404, L("нет такого превью", "no such preview"))
                if not has(ph["work"]):
                    return self.err(410, L("кадр в архиве", "frame is archived"))
                # рамка берётся из ссылки: превью для только что выбранной рамки может прийти раньше самой правки
                crop = parse_crop((qs.get("c") or [""])[0])
                return self.file(str(preview_file(ph, key, strength, leak, lseed, crop)))
            if len(parts) == 3 and parts[:2] == ["img", "source"]:
                ph = self.mine(parts[2], uid)
                if not ph or ph["hidden"] or not has(ph["work"]):
                    return self.err(404, L("нет файла", "no file"))
                path = PREVIEWS / f"{ph['id']}_source.jpg"
                if not path.exists():
                    FAST.submit(job_source, ph["work"], str(path)).result(timeout=120)
                return self.file(str(path))
            return self.err(404, L("не найдено", "not found"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, RuntimeError) as e:
            self.err(400, str(e))
        except Exception as e:
            log.exception("GET %s", self.path)
            self.err(500, str(e))

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)
        try:
            if parts == ["api", "upload"]:      # тело — сам файл, а не JSON, поэтому до self.body()
                uid = self.authed(qs)
                if not uid:
                    # дочитать и выбросить: иначе соединение рвётся и вместо «войди заново» человек видит «нет связи»
                    self.drain(int(self.headers.get("Content-Length") or 0))
                    return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
                length = int(self.headers.get("Content-Length") or 0)
                if not (qs.get("lut") or [""])[0] and over_daily(uid):
                    self.drain(length)
                    return self.err(400, L(f"за сутки уже {DAILY_LIMIT} кадров — это предел, завтра можно снова",
                                           f"{DAILY_LIMIT} frames in 24 hours is the limit, try again tomorrow"))
                if (qs.get("lut") or [""])[0]:          # свой LUT (.cube)
                    if length > LUT_MAX_BYTES:
                        return self.err(400, L("файл LUT больше 16 МБ", "the LUT file is larger than 16 MB"))
                    return self.js(add_lut(uid, (qs.get("name") or ["LUT"])[0], self.rfile.read(length)))
                return self.js(receive_upload(self.rfile, length, (qs.get("name") or [""])[0], uid))
            data = self.body()
            if parts == ["api", "pair"]:            # новое устройство: одноразовый код из ссылки -> постоянный ключ
                if too_many_fails(self.ip(), everyone=True):
                    return self.err(429, L("слишком много попыток, подожди 10 минут", "too many attempts, wait 10 minutes"))
                got = pair_device(data.get("code"), data.get("name"))
                if not got and invite_open(data.get("code")):      # код приглашения: новый пользователь
                    if not str(data.get("user_name") or "").strip():
                        return self.js({"need_name": True})
                    got = join_by_invite(data.get("code"), data.get("user_name"), data.get("name"))
                if not got:
                    note_fail(self.ip())
                    return self.err(403, L("код неверный, устарел или уже использован — возьми новый: /link в боте "
                                           "или «⋯» → «Привязать устройство» в «Проявке»",
                                           "the code is wrong, expired or already used — get a new one: /link in the bot "
                                           "or ⋯ → Link a device in Proyavka"))
                key, uid, did = got
                body = {"token": self.new_session(uid, did=did), "media": media_token(uid), "device": key, "lang": user_lang(uid)}
                return self.send(200, json.dumps(body, ensure_ascii=False), headers=self.device_cookie(key))
            if parts == ["api", "auth"]:
                key = data.get("device") or (self.cookie_key() if data.get("cookie") else "")
                if key:                             # браузер или приложение без Telegram
                    if too_many_fails(self.ip()):
                        return self.err(429, L("слишком много попыток, подожди 10 минут", "too many attempts, wait 10 minutes"))
                    dev = device_by_key(key)
                    if not dev:
                        note_fail(self.ip())
                        return self.err(401, L("это устройство отключено — привяжи его заново", "this device was removed — link it again"))
                    body = {"token": self.new_session(dev["owner"], data.get("token"), dev["id"]), "media": media_token(dev["owner"]),
                            "lang": user_lang(dev["owner"])}
                    if not data.get("device"):
                        body["device"] = key        # пришёл по cookie (iPhone перенёс её в приложение) — ключ себе в localStorage
                    return self.send(200, json.dumps(body, ensure_ascii=False), headers=self.device_cookie(key))
                if data.get("cookie"):
                    return self.err(401, L("это устройство ещё не привязано", "this device is not linked yet"))
                uid = uid_of_tg(check_init_data(data.get("initData", "")))
                if not uid or uid not in USERS:
                    return self.err(403, L("открой ленту из своего бота в Telegram", "open the feed from your bot in Telegram"))
                return self.js({"token": self.new_session(uid, data.get("token")), "media": media_token(uid), "lang": user_lang(uid)})
            uid = self.authed(qs)
            if not uid:
                return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
            if parts == ["api", "batch"]:
                return self.js(batch_action(data, uid))
            if parts == ["api", "me"]:
                return self.js(set_me(uid, data))
            if parts == ["api", "look", "try"]:
                return self.send(200, try_look(uid, data), "image/jpeg", "no-store")
            if parts == ["api", "look"]:            # новая своя плёнка из редактора; с key — правка существующей
                if data.get("key"):
                    return self.js(edit_look(uid, str(data["key"]), data.get("name"), data.get("params")))
                return self.js(add_look(uid, data.get("name"), data.get("params"), (user(uid) or {}).get("name") or ""))
            if parts == ["api", "look", "import"]:
                name, by, params = parse_look_code(data.get("code"))
                return self.js(add_look(uid, name, params, by))
            if parts == ["api", "community", "add"]:
                return self.js(community_add(uid, str(data.get("id") or "")))
            if parts == ["api", "albums"]:
                return self.js(make_album(uid, data))
            if len(parts) in (3, 4) and parts[:2] == ["api", "album"] and parts[2].isdigit():
                if len(parts) == 4 and parts[3] == "delete":
                    return self.js({"ok": bool(delete_album(uid, int(parts[2])))})
                if len(parts) == 3:
                    return self.js(edit_album(uid, int(parts[2]), data))
            if len(parts) == 4 and parts[:2] == ["api", "photo"] and parts[3] == "restore":
                ph = self.mine(parts[2], uid)
                if not ph or not ph["hidden"]:
                    return self.err(404, L("кадр не найден", "frame not found"))
                restore_photo(ph)
                return self.js({"ok": True})
            if parts == ["api", "push", "subscribe"]:
                if not webpush:
                    return self.err(400, L("на сервере нет библиотеки pywebpush — обнови «Проявку»",
                                           "the server has no pywebpush library — update Proyavka"))
                push_subscribe(uid, SESSION_DEV.get(self.headers.get("X-Token") or ""), data)
                return self.js({"ok": True})
            if parts == ["api", "push", "unsubscribe"]:
                run("DELETE FROM push_subs WHERE endpoint=? AND owner=?", (str(data.get("endpoint") or ""), uid))
                return self.js({"ok": True})
            if parts == ["api", "camera", "password"]:
                new_ftp_password(uid)
                return self.js(camera_json(uid))
            if parts == ["api", "tg", "link"]:
                if not BOT_TOKEN:
                    return self.err(400, L("на этом сервере Telegram-бот не подключён", "no Telegram bot on this server"))
                code = new_pair(uid, "tg")[0]
                return self.js({"url": f"https://t.me/{bot_username()}?start=link_{code}", "bot": bot_username()})
            if parts == ["api", "tg", "unlink"]:
                if uid < WEB_BASE:
                    return self.err(400, L("ты пришёл через Telegram — отвязать его нельзя", "you joined via Telegram — it can't be unlinked"))
                safe("deleteMyCommands", scope={"type": "chat", "chat_id": uid})
                set_user(uid, tg=None)
                return self.js(me_json(uid))
            if parts == ["api", "tg", "bot"]:
                if uid != ADMIN:
                    return self.err(403, L("только для администратора", "admin only"))
                bot = connect_bot(data.get("token"))
                return self.js({"bot": bot, **me_json(uid)})
            if parts == ["api", "invite"]:
                if uid != ADMIN:
                    return self.err(403, L("только для администратора", "admin only"))
                inv = invite_links(make_invite(uid))
                return self.js({**inv, "qr": qr_svg(inv["url"]) if inv["url"] else None, "days": INVITE_DAYS})
            if len(parts) == 4 and parts[:2] == ["api", "user"] and parts[2].isdigit() and uid == ADMIN:
                target = int(parts[2])
                if target not in USERS:
                    return self.err(404, L("нет такого пользователя", "no such user"))
                if parts[3] == "limit":
                    gb = float(data.get("gb") or 0)
                    if gb not in LIMITS_GB:
                        return self.err(400, "gb")
                    set_limit(target, gb)
                    return self.js({"users": users_json()})
                if parts[3] == "delete" and target != ADMIN:
                    n = delete_user(target)
                    return self.js({"users": users_json(), "deleted_frames": n})
            if parts == ["api", "devices", "new"]:
                code, link = new_pair(uid)
                return self.js({"url": link, "code": show_code(code), "qr": qr_svg(link), "ttl": PAIR_TTL})
            if len(parts) == 4 and parts[:2] == ["api", "device"] and parts[3] == "delete" and parts[2].isdigit():
                return self.js({"ok": bool(drop_device(uid, int(parts[2])))})
            if len(parts) == 4 and parts[:2] == ["api", "lut"] and parts[3] == "delete":
                return self.js({"ok": True, "moved": delete_lut(uid, parts[2])})
            if len(parts) >= 3 and parts[:2] == ["api", "photo"]:
                ph = self.mine(parts[2], uid)
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


def migrate_state(state):
    """Плёнка по умолчанию жила в state.json — теперь она у каждого пользователя своя (у администратора — прежняя)."""
    old = canon(state.pop("default", "") or "")
    state.pop("preset", None)
    if old and (old == "auto" or old in PRESETS):           # один раз: после переноса в state.json его нет
        set_user(ADMIN, default_film=old)
    save_state(state)


def main():
    init_db()
    state = load_state()
    migrate_state(state)
    if BOT_TOKEN:
        safe("deleteMyCommands")              # команды теперь у каждого свои (язык, /invite у администратора)
        for uid in list(USERS):
            with speak(uid):
                set_commands(uid)
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
    if RAW_MISSING:
        log.warning("RAW включён, но нет библиотеки rawpy — RAW выключен. Поставить: .venv/bin/pip install rawpy "
                    "(или setup.py --raw=1)")
    log.info("filmbot v6.0 started (%s), пользователей: %d, Telegram: %s", "локально" if LOCAL else VPS, len(USERS),
             "да" if BOT_TOKEN else "нет — только приложение")
    last_clean = 0.0
    while True:
        if BOT_RESET.is_set():                # бота подключили из «Проявки»
            BOT_RESET.clear()
            state["offset"] = 0
            for uid in list(USERS):
                with speak(uid):
                    set_commands(uid)
        if BOT_TOKEN:
            handle_updates(state)   # главный поток занят только кнопками бота
        else:
            time.sleep(1)
        now = time.time()
        if now - last_clean >= CLEANUP_MINUTES * 60:
            try:
                cleanup()
            except Exception:
                log.exception("cleanup failed")
            last_clean = now


def pair_cli():
    """filmbot.py --pair: код и QR для первого устройства администратора (зовёт мастер установки)."""
    init_db()
    code, link = new_pair(ADMIN)
    print(link)
    if segno:
        segno.make(link, error="m").terminal(compact=True)
    print(show_code(code))


if __name__ == "__main__":
    pair_cli() if "--pair" in sys.argv else main()
