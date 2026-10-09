"""Устройства и аккаунт: одноразовые коды входа, ключи устройств, подключение бота из приложения, настройки пользователя для приложения."""

import os
import re
import requests
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config
from .config import CONTACT_TG, ORIG_DAYS, PROJECT_URL, WEBAPP_URL, WEB_BASE, log
from .i18n import L, cur_lang, speak
from .util import html_esc, key_hash, qr_png, segno
from .film import canon
from .database import DB_LOCK, q, run, run_count
from .users import ADMIN, USERS, set_user, storage_limit, user, user_lang, valid_look
from .telegram import btn, safe, scrub, tg
from .sessions import SESSIONS, SESSION_DEV
from .storage import user_usage


PAIR_TTL = 600                        # коды лежат в таблице pairs: их выдаёт и мастер установки (filmbot.py --pair)


PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # без 0/O, 1/I/L — чтобы не путать, вписывая руками


PAIR_LEN = 8                          # 31^8 ≈ 8·10^11 вариантов на 10 минут при 20 попытках с адреса


ZIP_MAX = 200                         # кадров в одном архиве


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
    from .database import db
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
        # ссылки на картинки с отвязанного устройства (в истории, журналах) больше не открываются; остальные устройства
        # получат новый токен со следующим опросом
        set_user(uid, media_epoch=int((user(uid) or {}).get("media_epoch") or 0) + 1)
    for tok in [t for t, d in SESSION_DEV.items() if d == did]:
        SESSION_DEV.pop(tok, None)
        SESSIONS.pop(tok, None)
    return n


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


BOT_RESET = threading.Event()        # бота подключили или сменили — читать обновления с начала


def connect_bot(token):
    """Подключить Telegram-бота из настроек «Проявки»: проверить токен, сохранить в config.env, включить без перезапуска."""
    from .invites import BOT_NAME
    token = str(token or "").strip()
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise ValueError(L("не похоже на токен: цифры, двоеточие, длинная строка", "doesn't look like a token: digits, a colon, a long string"))
    try:
        j = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as e:
        raise ValueError(L("Telegram недоступен", "Telegram is unreachable") + f": {scrub(str(e).replace(token, '<token>'))}")
    if not j.get("ok"):
        raise ValueError(L("Telegram не принял токен", "Telegram rejected the token") + f": {j.get('description')}")
    save_config("BOT_TOKEN", token)
    config.BOT_TOKEN = token
    BOT_NAME.clear()
    BOT_NAME["u"] = j["result"]["username"]
    BOT_RESET.set()
    safe("deleteWebhook")
    log.info("подключён бот @%s", BOT_NAME["u"])
    return BOT_NAME["u"]


def save_config(key, value):
    """Поменять одну строку в config.env (остальное как было)."""
    path = Path(os.environ.get("CONFIG_FILE") or Path(__file__).resolve().parent.parent.parent / "config.env")
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
    from .invites import bot_username
    try:
        return bot_username() if config.BOT_TOKEN else None
    except Exception:
        return None


def me_json(uid):
    u = user(uid) or {}
    n = q("SELECT SUM(hidden=0) AS n, SUM(hidden=1 AND work IS NOT NULL) AS t FROM photos WHERE owner=?", (uid,))[0]
    return {"id": uid, "name": u.get("name") or "", "admin": uid == ADMIN, "lang": user_lang(uid),
            "default_film": u.get("default_film") or "auto", "used": sum(user_usage(uid).values()),
            "limit": storage_limit(uid) * 1e9, "frames": n["n"] or 0, "trash": n["t"] or 0, "orig_days": ORIG_DAYS,
            "telegram": {"bot": bot_name_safe(), "linked": bool(u.get("tg")),
                         "can_unlink": uid >= WEB_BASE and bool(u.get("tg"))},
            "about": {"project": PROJECT_URL, "tg": CONTACT_TG}}


def set_me(uid, data):
    from .invites import set_commands
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
    from .camera import CAMERA_CONFIG, FTP_ROOT_CERT, camera_access
    domain = os.environ.get("DOMAIN", "")
    ftp_user, pw, conf = camera_access(uid)
    guide = f"{PROJECT_URL}/blob/main/docs"
    ext = ".md" if cur_lang() == "en" else ".ru.md"
    return {"domain": domain, "ftp_user": ftp_user, "ftp_pass": pw, "config": bool(conf) or CAMERA_CONFIG.exists(),
            "cert": FTP_ROOT_CERT.exists(), "guide_app": f"{guide}/sony-app{ext}", "guide_ftp": f"{guide}/ftp-cameras{ext}"}
