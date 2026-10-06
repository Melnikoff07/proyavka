"""Веб-сервер приложения: запросы к API и картинкам, правка кадров, пакетные действия, загрузка, запуск."""

import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .config import COMMUNITY_BUNDLED, LANG, PREVIEWS, STRENGTHS, TMP, UPLOAD_MAX, WEB_BASE, WEB_PORT, log
from .i18n import L, tr, _CTX
from .util import disposition, download_name, qr_svg, remove
from .film import LUT_MAX_BYTES, PRESETS, canon, clean_params, params_json, parse_look_code, pname
from .imaging import LEAKS, auto_reason, crop_tag, fingerprint, has, parse_crop
from .pwa import CSP, SW_JS, app_icon, manifest
from .database import get, q, run, upd
from .users import ADMIN, DAILY_LIMIT, INVITE_DAYS, USERS, set_user, user, user_lang, user_luts, valid_look
from .jobs import BASE_EDGES, job_full, job_preview, job_source
from .telegram import safe, uid_of_tg
from .sessions import (
    DEV_COOKIE, SESSIONS, SESSION_DEV, SESSION_TTL, file_token, file_uid, media_token, media_uid, note_fail,
    too_many_fails,
)
from .push import LAST_POLL, push_key, push_subscribe
from .scheduler import (
    EXPORTING, apply_changes, batch_step, batch_track, export_photo, jobs_in_work, schedule_view,
)
from .photos import delete_photos, hide_photo, purge_trash, restore_photo
from .looks import (
    HUB_DIR, RateLimit, add_look, add_lut, community_add, community_image, community_json,
    community_preview, delete_lut, edit_look, hub_decide, hub_pending, hub_raw, hub_receive, look_flags,
    look_share, original_file, pending_preview, submit_look, try_look, user_look,
)
from .albums import (
    ALBUM_CSP, ALBUM_ZIPS, ZIP_POOL, album_by_token, album_page, album_public, album_rows, delete_album,
    edit_album, full_file, make_album, user_albums,
)
from .ingest import over_daily, receive_upload
from .devices import (
    PAIR_TTL, ZIP_MAX, camera_json, connect_bot, device_by_key, drop_device, me_json, new_pair,
    pair_device, set_me, show_code, user_devices, users_json,
)
from .camera import FTP_ROOT_CERT, camera_access, new_ftp_password
from .invites import (
    LIMITS_GB, bot_username, delete_user, invite_links, invite_open, join_by_invite, make_invite,
    set_limit,
)


def photo_json(ph):
    v = int(os.path.getmtime(ph["view"]) * 1000) if has(ph.get("view")) else 0      # мс: два вида за секунду — разные адреса
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
    from .pools import FAST
    tag = (f"_{leak}{lseed}" if leak else "") + crop_tag(crop)
    path = PREVIEWS / f"{ph['id']}_{key}_{strength}{tag}.jpg"
    if path.exists():
        return path
    snap = dict(ph, leak_seed=lseed, crop=crop)
    return Path(FAST.submit(job_preview, snap, key, strength, str(path), leak).result(timeout=120))


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
        from .pools import HEAVY
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
        from .pools import HEAVY
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
        from .looks import COMMUNITY_HUB
        from .pools import FAST
        from .push import webpush
        from .config import WEBAPP_HTML
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
            if parts[0] == "community" and COMMUNITY_HUB:          # каталог для остальных серверов: без входа, только чтение
                if parts == ["community", "looks.json"]:
                    return self.send(200, json.dumps(hub_raw(), ensure_ascii=False), "application/json; charset=utf-8", "public, max-age=300")
                if len(parts) == 3 and parts[1] == "looks" and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}\.jpg", parts[2]):
                    for d in (HUB_DIR / "looks", COMMUNITY_BUNDLED.parent / "looks"):
                        if (d / parts[2]).is_file():
                            return self.send(200, (d / parts[2]).read_bytes(), "image/jpeg", "public, max-age=3600")
                return self.err(404, L("не найдено", "not found"))
            # config.txt камеры несёт ключ для загрузки кадров — его по токену «только смотреть» не отдаём, только по ссылке
            # на этот один файл (живёт 5 минут); корневой сертификат — публичный
            if parts == ["api", "camera", "config.txt"] and not self.headers.get("X-Token"):
                uid = file_uid((qs.get("d") or [""])[0], "camera/config.txt")
                if uid:
                    _CTX.lang = user_lang(uid)
            else:
                files = parts[:1] == ["img"] or parts == ["api", "zip"] or parts == ["api", "camera", "cacert.pem"]
                uid = self.authed(qs, media=files)
            if not uid:
                return self.err(401, L("нужна авторизация через Telegram", "Telegram authorization required"))
            if parts == ["api", "presets"]:
                items = [{"key": "original", "name": L("Оригинал", "Original"), "when": L("без обработки", "unprocessed")}]
                items += [{"key": k, "name": p["name"], "when": tr(p["when"]), "desc": tr(p["desc"]),
                           "p": params_json(clean_params(p))} for k, p in PRESETS.items()]     # p — основа для редактора
                items += [{"key": f"lut{r['id']}", "name": r["name"], "desc": "", "lut": True,
                           "when": f"LUT {r['size']}³" if r["size"] else L("Своя плёнка", "Your film"),
                           **(look_flags(uid, f"lut{r['id']}") if not r["size"] else {})}
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
            if len(parts) == 3 and parts[:2] in (["img", "look"], ["img", "orig"]):
                try:
                    edge = int((qs.get("e") or ["420"])[0])
                except ValueError:
                    edge = 0
                ph = self.mine((qs.get("p") or [parts[2]])[0], uid)
                if not ph or ph["hidden"] or not has(ph["work"]) or edge not in BASE_EDGES:
                    return self.err(404, L("нет кадра", "no frame"))
                path = original_file(ph, edge) if parts[1] == "orig" else community_preview(ph, parts[2], edge)
                if not path:
                    return self.err(404, L("нет такой плёнки", "no such film"))
                return self.file(str(path))
            if len(parts) == 3 and parts[:2] == ["img", "pending"] and parts[2].isdigit():
                path = pending_preview(int(parts[2])) if COMMUNITY_HUB and uid == ADMIN else None
                if not path:
                    return self.err(404, L("нет картинки", "no image"))
                return self.file(str(path))
            if parts == ["api", "community", "pending"]:
                if not COMMUNITY_HUB or uid != ADMIN:
                    return self.err(403, L("только для администратора приёмника", "receiver admin only"))
                return self.js({"items": hub_pending()})
            if len(parts) == 3 and parts[:2] == ["img", "cm"]:
                path = community_image(parts[2]) if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", parts[2]) else None
                if not path:
                    return self.err(404, L("нет картинки", "no image"))
                return self.file(str(path))
            if parts == ["api", "albums"]:
                return self.js({"albums": user_albums(uid)})
            if parts == ["api", "trash"]:
                rows = q("SELECT * FROM photos WHERE owner=? AND hidden=1 AND work IS NOT NULL "
                         "ORDER BY deleted_at DESC, id DESC LIMIT 300", (uid,))
                return self.js({"photos": [photo_json(r) for r in rows]})
            if parts == ["api", "camera"]:
                return self.js(camera_json(uid))
            if parts == ["api", "camera", "link"]:            # ссылка на config.txt для <a download>
                return self.js({"url": f"/api/camera/config.txt?d={file_token(uid, 'camera/config.txt')}"})
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
        except RateLimit as e:
            self.err(429, str(e))
        except (ValueError, RuntimeError) as e:
            self.err(400, str(e))
        except Exception as e:
            log.exception("GET %s", self.path)
            self.err(500, str(e))

    def do_POST(self):
        from .looks import COMMUNITY_HUB
        from .push import webpush
        from .sessions import check_init_data
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
            if parts == ["api", "community", "submit"]:          # от других серверов, без входа
                if not COMMUNITY_HUB:
                    return self.err(404, L("не найдено", "not found"))
                try:
                    return self.js(hub_receive(data, self.ip()))
                except RateLimit as e:
                    return self.err(429, str(e))
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
            if len(parts) == 4 and parts[:2] == ["api", "look"] and parts[3] == "submit":
                try:
                    return self.js(submit_look(uid, parts[2]))
                except RateLimit as e:
                    return self.err(429, str(e))
            if len(parts) == 4 and parts[:3] == ["api", "community", "pending"] and parts[3].isdigit():
                if not COMMUNITY_HUB or uid != ADMIN:
                    return self.err(403, L("только для администратора приёмника", "receiver admin only"))
                return self.js(hub_decide(int(parts[3]), data))
            if parts == ["api", "community", "add"]:
                return self.js(community_add(uid, str(data.get("id") or "")))
            if parts == ["api", "albums"]:
                return self.js(make_album(uid, data))
            if len(parts) in (3, 4) and parts[:2] == ["api", "album"] and parts[2].isdigit():
                if len(parts) == 4 and parts[3] == "delete":
                    return self.js({"ok": bool(delete_album(uid, int(parts[2])))})
                if len(parts) == 3:
                    return self.js(edit_album(uid, int(parts[2]), data))
            if parts == ["api", "trash", "purge"]:          # «Удалить навсегда»: ids — выбранные, all — вся корзина
                if data.get("all") is True:
                    n = purge_trash(uid)
                else:
                    ids = data.get("ids")
                    if not isinstance(ids, list) or not ids or len(ids) > BATCH_MAX:
                        raise ValueError(L("нет кадров", "no frames"))
                    try:
                        ids = [int(i) for i in ids]
                    except (TypeError, ValueError):
                        raise ValueError(L("неверный список кадров", "invalid frame list"))
                    n = purge_trash(uid, ids)
                return self.js({"ok": True, "purged": n})
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
                if not config.BOT_TOKEN:
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


FILMS_VERSION = "2"        # 2 — встроенные плёнки перенастроены (полосы цвета, зерно в тенях); 1 — bot/films_v1.json


def refilm_builtin():
    """Встроенные плёнки перенастроили: кадры с ними перерисовать в фоне, иначе в ленте — прежний вид, а «Скачать»,
    альбом и любая правка рисуют уже новый. Один раз после обновления; в чате Telegram остаются прежние картинки."""
    from .config import BASE
    mark = BASE / "films.version"
    try:
        if mark.read_text(encoding="utf-8").strip() == FILMS_VERSION:
            return 0
    except OSError:
        pass
    keys = list(PRESETS)
    marks = ",".join("?" * len(keys))
    rows = q(f"SELECT id, owner FROM photos WHERE hidden=0 AND work IS NOT NULL AND preset IN ({marks}) ORDER BY id DESC", keys)
    for f in PREVIEWS.iterdir():                 # превью — кэш, в нём старый вид плёнок
        if f.is_file():
            remove(str(f))
    for r in rows:
        run("UPDATE photos SET rev=rev+1, updated=? WHERE id=?", (time.time(), r["id"]))
        schedule_view(r["id"], prio=2, chat=False, uid=r["owner"])
    mark.write_text(FILMS_VERSION + "\n", encoding="utf-8")
    if rows:
        log.info("плёнки обновлены: перерисовываю %d кадров в фоне", len(rows))
    return len(rows)


def start_web():
    srv = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web app on 127.0.0.1:%d", WEB_PORT)
