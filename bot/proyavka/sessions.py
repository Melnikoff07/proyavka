"""Вход и доверие: сессии, проверка входа из Telegram, подпись ссылок на картинки (токен «только смотреть»), защита от подбора кодов."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qsl

from . import config
from .config import BASE
from .users import USERS


SESSION_DEV = {}                      # токен сессии -> id устройства, с которого вошли


SESSION_ALBUM = {}                    # токен гостевой сессии -> id альбома, которым она ограничена


AUTH_FAILS = {}                    # ip -> времена неудачных попыток войти кодом или ключом


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


MEDIA_KEY_LOCK = threading.Lock()


def media_secret():
    global MEDIA_KEY
    with MEDIA_KEY_LOCK:
        if MEDIA_KEY is None:
            path = BASE / "media.key"
            try:                            # создаётся сразу с правами 600 и только один раз (O_EXCL)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                MEDIA_KEY = path.read_bytes()
            else:
                key = secrets.token_bytes(32)
                with os.fdopen(fd, "wb") as f:
                    f.write(key)
                MEDIA_KEY = key
    return MEDIA_KEY


def media_epoch(uid):
    """Поколение токенов пользователя: отвязал устройство — растёт, и все прежние ссылки на картинки перестают работать."""
    return int((USERS.get(uid) or {}).get("media_epoch") or 0)


def _media_sig(uid, exp, scope="", epoch=0):
    msg = f"{uid}.{exp}" + (f".{epoch}" if epoch else "") + (f".{scope}" if scope else "")
    return base64.urlsafe_b64encode(hmac.new(media_secret(), msg.encode(), hashlib.sha256).digest()[:16]).decode().rstrip("=")


def media_token(uid):
    exp = (int(time.time() // 86400) + 2) * 86400
    return f"{uid}.{exp}.{_media_sig(uid, exp, epoch=media_epoch(uid))}"


def _check(tok, scope=""):
    try:
        uid, exp, sig = str(tok).split(".")
        uid, exp = int(uid), int(exp)
        sig = sig.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return None
    if exp <= time.time() or uid not in USERS:
        return None
    want = _media_sig(uid, exp, scope, media_epoch(uid)).encode()
    return uid if hmac.compare_digest(sig, want) else None


def media_uid(tok):
    """Пользователь по токену для картинок или None."""
    return _check(tok)


def album_media_token(uid, aid):
    """Токен для картинок гостя альбома: годится только для кадров этого альбома (хозяин в токене — чтобы найти его кадры)."""
    exp = (int(time.time() // 86400) + 2) * 86400
    return f"{uid}.{exp}.{_media_sig(uid, exp, f'album:{aid}', media_epoch(uid))}.{aid}"


def album_media(tok):
    """(хозяин, id альбома) по токену гостя или None."""
    try:
        uid, exp, sig, aid = str(tok).split(".")
        uid, exp, aid = int(uid), int(exp), int(aid)
        sig = sig.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return None
    if exp <= time.time() or uid not in USERS:
        return None
    want = _media_sig(uid, exp, f"album:{aid}", media_epoch(uid)).encode()
    return (uid, aid) if hmac.compare_digest(sig, want) else None


FILE_TTL = 300


def file_token(uid, name):
    """Ссылка на один файл (настройки камеры: в них ключ для загрузки кадров) — живёт 5 минут и годится только для него."""
    exp = int(time.time()) + FILE_TTL
    return f"{uid}.{exp}.{_media_sig(uid, exp, 'file:' + name, media_epoch(uid))}"


def file_uid(tok, name):
    return _check(tok, "file:" + name)


def check_init_data(init_data):
    """Проверка подписи Telegram. Возвращает id пользователя Telegram (пускать ли его — решает список users)."""
    if not config.BOT_TOKEN:               # без бота подпись считалась бы пустым ключом — её мог бы подделать кто угодно
        return False
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return False
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return False
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return False
        return int(json.loads(pairs.get("user", "{}")).get("id") or 0) or False
    except (ValueError, TypeError):
        return False
