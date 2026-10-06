#!/usr/bin/env python3
"""
filmbot — камера → сервер-приёмник → плёночный лук → приложение «Проявка» и (по желанию) Telegram.
Настройки — переменные окружения (config.env), их пишет мастер setup.py.

Код делится на пакет proyavka/ (движок: film — плёнки и цвет, imaging — обработка кадра, config — настройки,
i18n — языки) и этот файл — пока всё остальное: база, пользователи, планировщик, Telegram, веб-сервер.
Разделы файла помечены «# ================= <раздел>»; пакет растёт по мере выноса разделов.

Бот: кнопки под каждой фоткой (плёнка, сила, дата/рамка/засвет, сравнение, файл).
Mini App: лента-контактный лист по дням, просмотр со свайпами и живыми превью плёнок.
Хранилище чистится само: старые оригиналы и рабочие копии удаляются по лимитам.
"""
import collections
import hashlib
import io
import json
import math
import os
import posixpath
import queue
import re
import shlex
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from proyavka.jobs import (
    BASE_EDGES, base_path, job_base, job_contact, job_full, job_prepare, job_preview, job_sample,
    job_source, job_try,
)
from proyavka.pools import NET, init_pools
from proyavka.telegram import (
    KB_FEED, KB_FILM, KB_HELP, KB_TODAY, btn, download_tg_bytes, download_tg_file, kb_is, leak_kb,
    main_kb, menu, preset_kb, safe, send_new, tg, uid_of_tg,
)
from proyavka.sessions import (
    DEV_COOKIE, SESSIONS, SESSION_DEV, SESSION_TTL, media_token, media_uid, note_fail, too_many_fails,
)
from proyavka.push import LAST_POLL, push_key, push_subscribe
from proyavka.scheduler import (
    EXPORTING, apply_changes, batch_step, batch_track, dispatcher, export_photo, jobs_in_work,
    schedule_view, tg_worker,
)
from proyavka.storage import cleanup, enforce_limit, storage_text, user_usage
from proyavka.photos import delete_photos, hide_photo, restore_photo

from proyavka import config
from proyavka.config import ALBUM_HTML, COMMUNITY_BUNDLED, CONTACT_TG, PROJECT_URL
from proyavka.i18n import APP_NAME
from proyavka.util import (
    disposition, download_name, html_esc, jpeg, key_hash, plural_ru, qr_png, qr_svg, remove, segno,
)
from proyavka.imaging import fingerprint, read_exif
from proyavka.pwa import CSP, SW_JS, app_icon, manifest
from proyavka.database import DB_LOCK, get, init_db, q, run, run_count, upd
from proyavka.users import (
    ADMIN, CLEANUP_MINUTES, DAILY_LIMIT, INVITE_DAYS, USERS, load_luts, load_users, set_user,
    storage_limit, udir, user, user_lang, user_luts, valid_look,
)

from proyavka.config import (
    BASE, EXTS, INCOMING, LANG, LOCAL, ORIG_DAYS, PAGE, POLL, POLL_BACKUP, PREVIEWS, RAW_EXTS,
    RAW_FILES, RAW_MISSING, REMOTE_DIR, SETTLE, SSH_CMD, STATE_FILE, STRENGTHS, TMP, UPLOAD_MAX, VPS,
    WEBAPP_URL, WEB_BASE, WEB_PORT, WORK_EDGE, log, remote,
)
from proyavka.film import (
    LUT_MAX_BYTES, LUT_MAX_COUNT, LUT_NAMES, LUT_OWNER, PRESETS, canon, clean_params, clean_text,
    is_lut, look_code, look_params, lut_dir, lut_meta, params_json, parse_cube, parse_look_code, pname,
)
from proyavka.i18n import (
    L, cur_lang, speak, tr, _CTX,
)
from proyavka.imaging import LEAKS, auto_reason, crop_tag, gallery_image, has, is_raw, parse_crop


# ================= настройки и запуск-мелочи =================

RAW_WAIT = 90          # сек: RAW ждёт, не придёт ли JPEG той же съёмки
_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Сначала IPv4: у многих VPS IPv6 есть на бумаге, но пакеты уходят в никуда, и подключение висит минутами."""
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda r: r[0] != socket.AF_INET)


socket.getaddrinfo = _ipv4_first


# ================= база (потокобезопасно) =================


# ================= пользователи =================


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
# «Отправить автору» без GitHub и логинов: приложение шлёт плёнку на сервер-приёмник, автор одобряет её у себя в приложении.
# Приёмником может быть любой сервер Проявки: COMMUNITY_HUB=1 включает приём, каталог по адресу /community/looks.json и вкладку
# «На проверке» у администратора. COMMUNITY_SUBMIT_URL — куда шлёт остальные серверы (https://<приёмник>/api/community/submit);
# пусто на приёмнике — плёнки пишутся прямо в его очередь, пусто на обычном сервере — «Отправить автору» уходит через Telegram/GitHub.
COMMUNITY_HUB = os.environ.get("COMMUNITY_HUB", "0").strip() == "1"
COMMUNITY_SUBMIT_URL = os.environ.get("COMMUNITY_SUBMIT_URL", "").strip()
HUB_DIR = BASE / "community_hub"
SUBMIT_PER_HOUR, SUBMIT_PER_DAY, SUBMIT_PENDING_MAX = 5, 20, 300
SUBMITS = {}
SUBMIT_LOCK = threading.Lock()
COMMUNITY_MAX = 600
COMMUNITY = {"at": 0.0, "looks": []}
COMMUNITY_LOCK = threading.Lock()


def _write_look(owner, key, name, params, author, src, sent=0):
    d = lut_dir(owner)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "size": 0, "params": params_json(params), "author": author, "src": src}
    if sent:
        meta["sent"] = sent
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
    return {"code": code, "suggest_url": url, "author": author, "contact": CONTACT_TG, "name": cur["name"],
            "submit": submit_info(), "sent": bool(lut_meta(uid, key).get("sent"))}


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
        if COMMUNITY_HUB:                                   # приёмник ведёт каталог сам
            COMMUNITY["looks"] = _community_entries(hub_raw())
            COMMUNITY["at"] = time.time()
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
    en = user_lang(uid) == "en"
    items = []
    for e in community_catalog():
        d = e["desc"]
        items.append({"id": e["id"], "name": e["name"], "by": e["by"], "h": e["h"], "bw": bool(e["p"]["bw"]),
                      "desc": (d.get("en") if en else d.get("ru")) or d.get("ru") or d.get("en") or "",
                      "installed": have.get(e["id"], "")})
    out = {"looks": items, "repo": COMMUNITY_REPO, "submit": submit_info()}
    if COMMUNITY_HUB and uid == ADMIN:
        out["hub"] = {"pending": q("SELECT COUNT(*) AS n FROM submissions WHERE status='new'")[0]["n"]}
    return out


def community_add(uid, cid):
    for e in community_catalog():
        if e["id"] == cid:
            for r in user_luts(uid):                     # уже добавлена — вторую копию не делаем
                if lut_meta(uid, f"lut{r['id']}").get("src") == cid:
                    return {"key": f"lut{r['id']}", "name": r["name"], "again": True}
            return add_look(uid, e["name"], e["p"], e["by"], cid)
    raise ValueError(L("в каталоге нет такой плёнки", "no such film in the catalog"))


class RateLimit(ValueError):
    pass


def look_flags(uid, key):
    m = lut_meta(uid, key)
    return {"look": True, "community": bool(m.get("src")), "sent": bool(m.get("sent"))}


def submit_info():
    """Куда уходит «Отправить автору»: адрес приёмника или этот же сервер, если он приёмник."""
    if COMMUNITY_SUBMIT_URL:
        return {"on": True, "host": urlparse(COMMUNITY_SUBMIT_URL).hostname or ""}
    if COMMUNITY_HUB:
        return {"on": True, "host": urlparse(WEBAPP_URL).hostname or ""}
    return {"on": False, "host": ""}


def hub_raw():
    try:
        return json.loads((HUB_DIR / "looks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            return json.loads(COMMUNITY_BUNDLED.read_text(encoding="utf-8"))      # первый запуск: каталог из репозитория
        except (OSError, ValueError):
            return {"v": 1, "looks": []}


def _submit_rate(key):
    now = time.time()
    with SUBMIT_LOCK:
        ts = [t for t in SUBMITS.get(key, []) if now - t < 86400]
        if len([t for t in ts if now - t < 3600]) >= SUBMIT_PER_HOUR or len(ts) >= SUBMIT_PER_DAY:
            raise RateLimit(L("слишком много отправок — попробуй позже", "too many submissions — try again later"))
        ts.append(now)
        SUBMITS[key] = ts
        while len(SUBMITS) > 5000:
            SUBMITS.pop(next(iter(SUBMITS)))


def hub_receive(payload, ip):
    """Приём плёнки от кого угодно: всё проверяется и ограничивается, в каталог ничего не попадает без одобрения."""
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 8192:
        raise ValueError(L("это не плёнка Проявки", "this is not a Proyavka film"))
    params = clean_params(payload.get("p"))
    name = clean_text(payload.get("name"), 32) or "Look"
    by = clean_text(payload.get("by"), 40)
    h = hashlib.sha1(json.dumps(params_json(params), sort_keys=True).encode()).hexdigest()[:10]
    if q("SELECT 1 FROM submissions WHERE h=? AND status='new'", (h,)) or any(e["h"] == h for e in community_catalog()):
        return {"ok": True, "again": True}                  # такая уже есть — повторно не копим
    _submit_rate(ip or "?")
    if q("SELECT COUNT(*) AS n FROM submissions WHERE status='new'")[0]["n"] >= SUBMIT_PENDING_MAX:
        raise RateLimit(L("очередь на проверку переполнена — попробуй позже", "the review queue is full — try again later"))
    run("INSERT INTO submissions(name, by, params, h, created, ip, status) VALUES (?,?,?,?,?,?, 'new')",
        (name, by, json.dumps(params_json(params)), h, time.time(), ip or "", ))
    log.info("плёнка на проверку: «%s» от «%s»", name, by)
    return {"ok": True}


def submit_look(uid, key):
    """«Отправить автору»: своя плёнка уходит на сервер-приёмник (или в очередь этого сервера, если он приёмник)."""
    cur = user_look(uid, key)
    if cur["src"]:
        raise ValueError(L("эта плёнка из каталога — предлагать её обратно не нужно", "this film came from the catalog — no need to suggest it back"))
    params = clean_params(cur["params"])
    payload = {"v": 1, "name": cur["name"], "by": clean_text((user(uid) or {}).get("name"), 40), "p": params_json(params)}
    url = COMMUNITY_SUBMIT_URL
    if url:
        if not (url.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)[:/]", url)):
            raise ValueError(L("адрес приёмника задан неверно", "the submission address is invalid"))
        try:
            r = requests.post(url, json=payload, timeout=10)
        except requests.RequestException:
            raise ValueError(L("не удалось связаться с сервером проекта — попробуй позже", "could not reach the project server — try again later"))
        if r.status_code != 200:
            try:
                msg = r.json().get("error")
            except ValueError:
                msg = ""
            raise ValueError(msg or L("сервер проекта не принял плёнку", "the project server did not accept the film"))
    elif COMMUNITY_HUB:
        hub_receive(payload, f"u{uid}")
    else:
        raise ValueError(L("отправка автору здесь не настроена", "sending to the author is not set up here"))
    _write_look(uid, key, cur["name"], params, cur["author"], cur["src"], sent=time.time())
    return {"ok": True}


CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "a b v g d e e zh z i y k l m n o p r s t u f h c ch sh sch _ y _ e yu ya".split()))


def hub_pending():
    return [{"id": r["id"], "name": r["name"], "by": r["by"], "created": r["created"], "h": r["h"],
             "bw": bool(json.loads(r["params"]).get("bw"))} for r in q("SELECT * FROM submissions WHERE status='new' ORDER BY id")]


def pending_preview(sid):
    from proyavka.pools import FAST
    rows = q("SELECT * FROM submissions WHERE id=? AND status='new'", (sid,))
    if not rows:
        return None
    path = PREVIEWS / f"pending_{sid}_{rows[0]['h']}.jpg"
    if not path.exists():
        FAST.submit(job_sample, clean_params(json.loads(rows[0]["params"])), str(path)).result(timeout=120)
    return path


def hub_decide(sid, data):
    """Одобрить заявку (плёнка попадает в каталог вместе с превью на образце) или отклонить."""
    from proyavka.pools import FAST
    rows = q("SELECT * FROM submissions WHERE id=? AND status='new'", (sid,))
    if not rows:
        raise ValueError(L("заявка уже обработана", "this submission was already handled"))
    row = rows[0]
    if data.get("action") == "reject":
        run("UPDATE submissions SET status='rejected' WHERE id=?", (sid,))
        return {"ok": True}
    if data.get("action") != "approve":
        raise ValueError("action")
    params = clean_params(json.loads(row["params"]))
    name = clean_text(data.get("name"), 32) or row["name"]
    by = clean_text(data.get("by"), 40) if "by" in data else row["by"]
    raw = hub_raw()
    taken = {e["id"] for e in raw["looks"]}
    base = re.sub(r"[^a-z0-9]+", "-", "".join(CYR.get(c, c) for c in name.lower())).strip("-")[:30] or "look"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    (HUB_DIR / "looks").mkdir(parents=True, exist_ok=True)
    FAST.submit(job_sample, params, str(HUB_DIR / "looks" / f"{slug}.jpg")).result(timeout=120)
    desc = {"ru": clean_text(data.get("desc_ru"), 140), "en": clean_text(data.get("desc_en"), 140)}
    raw.setdefault("looks", []).append({"id": slug, "name": name, "by": by, "desc": desc, "p": params_json(params)})
    tmp = HUB_DIR / "looks.tmp.json"
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, HUB_DIR / "looks.json")
    run("UPDATE submissions SET status='approved' WHERE id=?", (sid,))
    COMMUNITY["at"] = 0                                     # каталог перечитается сразу
    log.info("плёнка одобрена: %s", slug)
    return {"ok": True, "id": slug}


def try_look(uid, data):
    """Живой просмотр редактора: кадр пользователя с плёнкой из ползунков (картинка, ничего не сохраняется)."""
    from proyavka.pools import FAST
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


def community_preview(ph, cid, edge=420):
    """Плёнка каталога на кадре пользователя; файл-кэш на сутки."""
    from proyavka.pools import FAST
    for e in community_catalog():
        if e["id"] == cid:
            path = PREVIEWS / f"{ph['id']}_cm{e['h']}_100{'' if edge == 420 else '_' + str(edge)}.jpg"
            if not path.exists():
                FAST.submit(job_try, dict(ph), e["p"], 100, str(path), edge).result(timeout=120)
            return path
    return None


def original_file(ph, edge):
    """Кадр без плёнки того же размера, что и готовый, — для ползунка «до/после»."""
    from proyavka.pools import FAST
    path = base_path(ph, edge)
    if not path.exists():
        FAST.submit(job_base, dict(ph), edge).result(timeout=120)
    return path


def community_image(name):
    """Готовая картинка плёнки каталога: <id> — как она выглядит на образце, <id>-before — исходник под неё (по умолчанию
    общий образец), sample — сам образец. Они лежат рядом с каталогом (community/looks/): сервер забирает их по мере надобности
    и кэширует, поэтому показывать каталог можно без единой отрисовки у себя."""
    sample = COMMUNITY_BUNDLED.parent / "sample.jpg"
    if name == "sample":
        return sample if sample.exists() else None
    cid = name.removesuffix("-before")
    entry = next((e for e in community_catalog() if e["id"] == cid), None)
    if not entry:
        return None
    if COMMUNITY_HUB and (HUB_DIR / "looks" / f"{name}.jpg").exists():
        return HUB_DIR / "looks" / f"{name}.jpg"
    cache = BASE / "community"
    cached = cache / f"{name}.{entry['h']}.jpg"
    if cached.exists():
        return cached
    remote = COMMUNITY_URL.rsplit("/", 1)[0] + "/looks/" if COMMUNITY_URL.startswith("https://") else ""
    if remote:
        try:
            r = requests.get(f"{remote}{name}.jpg", timeout=8, stream=True)
            data = r.raw.read(1_500_001, decode_content=True) if r.status_code == 200 else b""
            if 0 < len(data) <= 1_500_000:
                im = Image.open(io.BytesIO(data))
                if im.format == "JPEG" and max(im.size) <= 2400:           # чужой файл: только небольшой JPEG
                    im.load()
                    cache.mkdir(exist_ok=True)
                    for old in cache.glob(f"{name}.*.jpg"):
                        remove(str(old))
                    cached.write_bytes(data)
                    return cached
        except (requests.RequestException, OSError, ValueError):
            pass
    local = COMMUNITY_BUNDLED.parent / "looks" / f"{name}.jpg"
    if local.exists():
        return local
    return sample if name.endswith("-before") and sample.exists() else None


# ================= Telegram =================


# ================= задачи в отдельных процессах =================


# ================= планировщик =================


# ================= хранилище =================


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
if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", CONTACT_TG):
    CONTACT_TG = ""
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
    from proyavka.pools import FAST
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
    from proyavka.pools import FAST
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
    from proyavka.database import db
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
    config.BOT_TOKEN = token
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


# ================= уведомления (Web Push) =================


# ================= альбомы по ссылке =================
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
    from proyavka.database import db
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
                .replace("{{PROJECT}}", html_esc(PROJECT_URL))
                .replace("{{CONTACT}}", f' · <a href="https://t.me/{CONTACT_TG}" target="_blank" rel="noopener">Telegram @{CONTACT_TG}</a>'
                         if CONTACT_TG else ""))


def full_file(ph):
    """Кадр в полном размере: рисуется один раз на каждую правку и лежит в кэше превью (чистится через сутки)."""
    from proyavka.pools import HEAVY
    path = PREVIEWS / f"{ph['id']}_full_{ph['rev'] or 0}.jpg"
    with FULL_LOCKS[ph["id"]]:
        if path.exists():
            os.utime(path)
        else:
            HEAVY.submit(job_full, ph, str(path)).result(timeout=300)
    return path


# ================= Mini App: веб-сервер =================


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
    from proyavka.pools import FAST
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
        from proyavka.pools import HEAVY
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
        from proyavka.pools import HEAVY
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
        from proyavka.pools import FAST
        from proyavka.push import webpush
        from proyavka.config import WEBAPP_HTML
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
        from proyavka.push import webpush
        from proyavka.sessions import check_init_data
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
    if config.BOT_TOKEN:
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
             "да" if config.BOT_TOKEN else "нет — только приложение")
    last_clean = 0.0
    while True:
        if BOT_RESET.is_set():                # бота подключили из «Проявки»
            BOT_RESET.clear()
            state["offset"] = 0
            for uid in list(USERS):
                with speak(uid):
                    set_commands(uid)
        if config.BOT_TOKEN:
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
