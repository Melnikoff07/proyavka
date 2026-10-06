"""Бот в чате: экраны (лента, корзина, помощь), реакция на текст и кнопки, получение обновлений от Telegram."""

import math
import time
from datetime import datetime

from .config import PAGE, STRENGTHS, TMP, WEBAPP_URL, WEB_BASE, log
from .i18n import L, speak, tr, user_lang
from .util import jpeg, remove
from .film import LUT_MAX_BYTES, LUT_NAMES, LUT_OWNER, PRESETS, canon, pname
from .imaging import gallery_image
from .database import get, q
from .users import ADMIN, INVITE_DAYS, set_user, storage_limit, user, user_luts, valid_look
from .jobs import job_contact
from .pools import NET
from .telegram import (
    KB_FEED, KB_FILM, KB_HELP, KB_TODAY, btn, download_tg_bytes, download_tg_file, kb_is, leak_kb,
    main_kb, menu, preset_kb, safe, send_new, tg, uid_of_tg,
)
from .scheduler import apply_changes, export_photo
from .storage import storage_text
from .photos import hide_photo, restore_photo
from .state import save_state
from .looks import add_lut, delete_lut
from .devices import devices_screen, drop_device, send_link
from .camera import send_camera_setup
from .invites import (
    LIMITS_GB, delete_user, invite_links, make_invite, on_stranger, set_commands, tg_name, users_screen,
)


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
    from .pools import FAST
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
