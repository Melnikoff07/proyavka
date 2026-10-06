"""Пользователи и их настройки: кто есть, язык, плёнка по умолчанию, свои LUT (список и проверка), папки и лимиты места."""
import os
import threading

from . import config, i18n
from .config import BASE, CHAT_ID, LANG
from .film import LUT_NAMES, LUT_OWNER, PRESETS, is_lut
from .database import q, run

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


def storage_limit(uid):
    u = USERS.get(uid) or {}
    if u.get("storage_gb"):
        return u["storage_gb"]
    return config.STORAGE_GB if uid == ADMIN else USER_STORAGE_GB


def udir(uid, kind):
    """Папка пользователя: у администратора — прежние папки в BASE, у остальных — BASE/users/<id>/."""
    d = (BASE if uid == ADMIN else BASE / "users" / str(uid)) / kind
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
    return d


i18n.user_lang = user_lang          # speak(uid) из proyavka.i18n берёт язык пользователя отсюда
