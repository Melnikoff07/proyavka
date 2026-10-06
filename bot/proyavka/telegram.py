"""Telegram: вызовы API с ограничением частоты, кнопки под кадрами, отправка и правка сообщений, загрузка файлов."""

import json
import os
import requests
import secrets
import shutil
import threading
import time
from PIL import Image, ImageDraw
from datetime import datetime
from pathlib import Path

from . import config
from .config import BASE, TMP, log
from .i18n import L, cur_lang, speak, tr
from .util import save_atomic
from .film import PRESETS, pname
from .imaging import LEAKS, auto_reason, font, has
from .database import EDIT_LOCK, run, upd
from .users import USERS, udir, user_luts


# Telegram необязателен: бота может не быть вовсе, а у пользователя может не быть привязанного чата.
# Все отправки идут через tg(): номер пользователя превращается в его чат; некуда — NoChat (safe() её глотает).
class NoChat(Exception):
    pass


def chat_of(uid):
    if not config.BOT_TOKEN or uid is None:
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
    if not config.BOT_TOKEN:
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
        r = requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}", data=data, files=files, timeout=(10, 120))   # (подключение, ответ)
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


def download_tg_file(file_id, name, uid):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{info['file_path']}"
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
    url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{info['file_path']}"
    r = requests.get(url, timeout=(10, 120))
    r.raise_for_status()
    return r.content[:limit + 1]
