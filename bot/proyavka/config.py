"""Настройки из переменных окружения (config.env), папки с данными и общие константы."""
from PIL import Image
from pathlib import Path
import importlib.util
import logging
import numpy as np
import os
import warnings

# «Бомба» в картинке: крошечный файл, который при разборе раздувается в гигабайты памяти.
# Больше MAX_MEGAPIXELS — отказ сразу при открытии (у самых больших камер ~100 Мп).
Image.MAX_IMAGE_PIXELS = int(float(os.environ.get("MAX_MEGAPIXELS", "120")) * 1e6)
warnings.simplefilter("error", Image.DecompressionBombWarning)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

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


INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP = (
    BASE / d for d in ("incoming", "originals", "work", "thumbs", "views", "previews", "tmp"))
for d in (INCOMING, ORIGINALS, WORK, THUMBS, VIEWS, PREVIEWS, TMP):
    d.mkdir(parents=True, exist_ok=True)


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


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("filmbot")
