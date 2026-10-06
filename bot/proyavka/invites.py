"""Приглашения и пользователи: коды-приглашения, вступление, чужие в чате, список пользователей, лимиты, удаление, команды бота."""

import re
import secrets
import shutil
import threading
import time

from . import config
from .config import BASE, PREVIEWS, WEBAPP_URL, WEB_BASE, log
from .i18n import APP_NAME, L, cur_lang, speak, tr, _CTX
from .util import plural_ru, remove
from .database import q, run, run_count
from .users import ADMIN, INVITE_DAYS, USERS, load_luts, load_users, set_user, storage_limit, user
from .telegram import btn, menu, safe, tg
from .sessions import SESSIONS, SESSION_DEV
from .storage import enforce_limit, user_usage
from .devices import PAIR_ALPHABET, PAIR_LEN, create_device, norm_code, save_config, show_code, take_pair


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
            "tg_url": f"https://t.me/{bot_username()}?start={code}" if config.BOT_TOKEN else ""}


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
    from .botui import help_text
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
    if uid == ADMIN:
        save_config("STORAGE_GB", f"{gb:g}")
        config.STORAGE_GB = gb
        set_user(uid, storage_gb=None)
    else:
        set_user(uid, storage_gb=gb)
    threading.Thread(target=enforce_limit, args=(uid,), daemon=True).start()   # лимит уменьшили — освободить место


def delete_user(uid):
    """Убрать пользователя: все его кадры и файлы, камеру на сервере, доступ к боту."""
    from .camera import cam_helper
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
