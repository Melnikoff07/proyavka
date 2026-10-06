"""Вход и доверие: сессии, проверка входа из Telegram, подпись ссылок на картинки (токен «только смотреть»), защита от подбора кодов."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import parse_qsl

from . import config
from .config import BASE
from .users import USERS


SESSION_DEV = {}                      # токен сессии -> id устройства, с которого вошли


AUTH_FAILS = {}                       # ip -> времена неудачных попыток войти кодом или ключом


FAIL_WINDOW, FAIL_MAX = 600, 20


FAIL_MAX_ALL = 500                    # неудачных кодов со всех адресов за окно — дальше привязка ждёт


DEV_COOKIE = "proyavka_device"


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
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return False
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return False
    try:
        return int(json.loads(pairs.get("user", "{}")).get("id") or 0) or False
    except (ValueError, TypeError):
        return False
