"""Свои плёнки и сообщество: список и правка своих плёнок и LUT, каталог сообщества, приём и проверка присланных плёнок (сервер-приёмник), превью."""

import hashlib
import io
import json
import numpy as np
import os
import re
import requests
import threading
import time
from PIL import Image
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import BASE, COMMUNITY_BUNDLED, CONTACT_TG, PREVIEWS, STRENGTHS, WEBAPP_URL, log
from .i18n import L
from .util import remove
from .film import (
    LUT_MAX_BYTES, LUT_MAX_COUNT, LUT_NAMES, LUT_OWNER, clean_params, clean_text, is_lut, look_code,
    look_params, lut_dir, lut_meta, params_json, parse_cube,
)
from .imaging import has
from .database import get, q, run
from .users import ADMIN, load_luts, set_user, user, user_lang, user_luts
from .jobs import base_path, job_base, job_sample, job_try
from .scheduler import schedule_view


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
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue                                     # одна битая запись не должна ронять каталог
        if len(out) >= COMMUNITY_MAX:
            log.warning("в каталоге больше %d плёнок — остальные не показываются", COMMUNITY_MAX)
            break
    return out


COMMUNITY_FETCH = threading.Lock()      # каталог качает один поток; остальные пока берут прежний


def _fetch_limited(url, limit, timeout=8, total=20):
    """GET с пределом размера и общего времени (таймаут requests — на каждое чтение, а не на весь ответ)."""
    t0 = time.time()
    with requests.get(url, timeout=timeout, stream=True) as r:
        if r.status_code != 200:
            return r.status_code, b""
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > limit:
                raise ValueError("too large")
            if time.time() - t0 > total:
                raise ValueError("too slow")
        return 200, bytes(buf)


def community_catalog(force=False):
    """Каталог плёнок сообщества: из сети (кэш на час), иначе с диска, иначе копия из репозитория."""
    with COMMUNITY_LOCK:
        fresh = COMMUNITY["looks"] and time.time() - COMMUNITY["at"] < COMMUNITY_TTL
        if not force and fresh:
            return COMMUNITY["looks"]
        if COMMUNITY_HUB:                                   # приёмник ведёт каталог сам
            COMMUNITY["looks"] = _community_entries(hub_raw())
            COMMUNITY["at"] = time.time()
            return COMMUNITY["looks"]
    # скачивание — без общей блокировки: пока каталог едет, остальные запросы отдают прежний, а не ждут
    if not COMMUNITY_FETCH.acquire(blocking=not COMMUNITY["looks"]):
        return COMMUNITY["looks"]
    try:
        cache = BASE / "community.json"
        raw = None
        if COMMUNITY_URL.startswith("https://"):
            try:
                code, data = _fetch_limited(COMMUNITY_URL, 2 * 1024 * 1024)
                if code != 200:
                    raise ValueError(f"HTTP {code}")
                raw = json.loads(data)
                if _community_entries(raw) or raw.get("looks") == []:
                    cache.write_bytes(data)
                else:
                    raw = None
            except (requests.RequestException, ValueError, OSError, AttributeError) as e:
                log.warning("каталог сообщества не скачался: %s", e)
                raw = None
        for src in (cache, COMMUNITY_BUNDLED):
            if raw is not None:
                break
            try:
                raw = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        looks = _community_entries(raw)
        with COMMUNITY_LOCK:
            COMMUNITY["looks"] = looks
            COMMUNITY["at"] = time.time()
        return looks
    finally:
        COMMUNITY_FETCH.release()


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


SUBMIT_EXPIRE_DAYS = 30           # заявка, которую так и не посмотрели, уходит из очереди: забитая очередь не держится вечно


HUB_LOCK = threading.Lock()       # очередь и каталог приёмника меняются по одному: приём, одобрение, отклонение


def hub_receive(payload, ip):
    """Приём плёнки от кого угодно: всё проверяется и ограничивается, в каталог ничего не попадает без одобрения.
    ip — адрес из X-Real-IP (его ставит nginx); от другого сервера Проявки это адрес сервера, а не человека."""
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 8192:
        raise ValueError(L("это не плёнка Проявки", "this is not a Proyavka film"))
    params = clean_params(payload.get("p"))
    name = clean_text(payload.get("name"), 32) or "Look"
    by = clean_text(payload.get("by"), 40)
    h = hashlib.sha1(json.dumps(params_json(params), sort_keys=True).encode()).hexdigest()[:10]
    in_catalog = any(e["h"] == h for e in community_catalog())
    with HUB_LOCK:                                         # проверка дубля и запись — одним шагом
        run("UPDATE submissions SET status='expired' WHERE status='new' AND created < ?",
            (time.time() - SUBMIT_EXPIRE_DAYS * 86400,))
        if in_catalog or q("SELECT 1 FROM submissions WHERE h=? AND status='new'", (h,)):
            return {"ok": True, "again": True}              # такая уже есть — повторно не копим
        _submit_rate(ip or "?")
        if q("SELECT COUNT(*) AS n FROM submissions WHERE status='new'")[0]["n"] >= SUBMIT_PENDING_MAX:
            raise RateLimit(L("очередь на проверку переполнена — попробуй позже", "the review queue is full — try again later"))
        run("INSERT INTO submissions(name, by, params, h, created, ip, status) VALUES (?,?,?,?,?,?, 'new')",
            (name, by, json.dumps(params_json(params)), h, time.time(), ip or ""))
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
                body = r.json()
                msg = clean_text(body.get("error"), 200) if isinstance(body, dict) else ""
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
    from .pools import FAST
    rows = q("SELECT * FROM submissions WHERE id=? AND status='new'", (sid,))
    if not rows:
        return None
    path = PREVIEWS / f"pending_{sid}_{rows[0]['h']}.jpg"
    if not path.exists():
        FAST.submit(job_sample, clean_params(json.loads(rows[0]["params"])), str(path)).result(timeout=120)
    return path


def look_slug(name, taken):
    """Адрес плёнки в каталоге из названия. «sample» и «…-before» заняты картинками каталога (образец и «до»)."""
    base = re.sub(r"[^a-z0-9]+", "-", "".join(CYR.get(c, c) for c in name.lower())).strip("-")[:30].strip("-") or "look"
    if base == "sample" or base.endswith("-before"):
        base = f"{base}-look"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def hub_decide(sid, data):
    """Одобрить заявку (плёнка попадает в каталог вместе с превью на образце) или отклонить.
    Под одной блокировкой: два одобрения подряд (двойной тап, две вкладки) не затирают каталог друг другу."""
    from .pools import FAST
    action = data.get("action")
    if action not in ("approve", "reject"):
        raise ValueError("action")
    with HUB_LOCK:
        rows = q("SELECT * FROM submissions WHERE id=? AND status='new'", (sid,))
        if not rows:
            raise ValueError(L("заявка уже обработана", "this submission was already handled"))
        row = rows[0]
        if action == "reject":
            run("UPDATE submissions SET status='rejected' WHERE id=?", (sid,))
            return {"ok": True}
        params = clean_params(json.loads(row["params"]))
        name = clean_text(data.get("name"), 32) or row["name"]
        by = clean_text(data.get("by"), 40) if "by" in data else row["by"]
        raw = hub_raw()
        looks = raw.setdefault("looks", [])
        slug = look_slug(name, {e.get("id") for e in looks if isinstance(e, dict)})
        (HUB_DIR / "looks").mkdir(parents=True, exist_ok=True)
        FAST.submit(job_sample, params, str(HUB_DIR / "looks" / f"{slug}.jpg")).result(timeout=120)
        desc = {"ru": clean_text(data.get("desc_ru"), 140), "en": clean_text(data.get("desc_en"), 140)}
        looks.append({"id": slug, "name": name, "by": by, "desc": desc, "p": params_json(params)})
        tmp = HUB_DIR / "looks.tmp.json"
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, HUB_DIR / "looks.json")
        run("UPDATE submissions SET status='approved' WHERE id=?", (sid,))
        COMMUNITY["at"] = 0                                 # каталог перечитается сразу
    log.info("плёнка одобрена: %s", slug)
    return {"ok": True, "id": slug}


def try_look(uid, data):
    """Живой просмотр редактора: кадр пользователя с плёнкой из ползунков (картинка, ничего не сохраняется)."""
    from .pools import FAST
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
    edge = data.get("edge") if data.get("edge") in (420, 1000) else 420     # 1000 — редактор на широком экране
    params = clean_params(data.get("params"))
    return FAST.submit(job_try, dict(ph), params, strength, None, edge).result(timeout=120)


PREVIEW_RATE = {}                 # пользователь -> времена отрисовок плёнок каталога на его кадрах
PREVIEW_PER_MIN = 30


def _preview_rate(uid):
    """Плёнок в каталоге сотни, кадров тысячи: перебирать их скриптом — занять процессор сервера. Готовые (из кэша) не считаются."""
    now = time.time()
    with SUBMIT_LOCK:
        ts = [t for t in PREVIEW_RATE.get(uid, []) if now - t < 60]
        if len(ts) >= PREVIEW_PER_MIN:
            raise RateLimit(L("слишком часто — подожди минуту", "too fast — wait a minute"))
        ts.append(now)
        PREVIEW_RATE[uid] = ts


def community_preview(ph, cid, edge=420):
    """Плёнка каталога на кадре пользователя; файл-кэш на сутки."""
    from .pools import FAST
    for e in community_catalog():
        if e["id"] == cid:
            path = PREVIEWS / f"{ph['id']}_cm{e['h']}_100{'' if edge == 420 else '_' + str(edge)}.jpg"
            if not path.exists():
                _preview_rate(ph["owner"])
                FAST.submit(job_try, dict(ph), e["p"], 100, str(path), edge).result(timeout=120)
            return path
    return None


def original_file(ph, edge):
    """Кадр без плёнки того же размера, что и готовый, — для ползунка «до/после»."""
    from .pools import FAST
    path = base_path(ph, edge)
    if not path.exists():
        FAST.submit(job_base, dict(ph), edge).result(timeout=120)
    return path


IMAGE_MISS = {}                   # картинка каталога -> когда её не удалось скачать
IMAGE_MISS_TTL = 600


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
    if remote and time.time() - IMAGE_MISS.get(name, 0) > IMAGE_MISS_TTL:
        IMAGE_MISS[name] = time.time()               # пока не скачалась — не спрашивать снова каждый показ
        try:
            code, data = _fetch_limited(f"{remote}{name}.jpg", 1_500_000)
            if 0 < len(data) <= 1_500_000:
                im = Image.open(io.BytesIO(data))
                if im.format == "JPEG" and max(im.size) <= 2400:           # чужой файл: только небольшой JPEG
                    im.load()
                    cache.mkdir(exist_ok=True)
                    for old in cache.glob(f"{name}.*.jpg"):
                        remove(str(old))
                    cached.write_bytes(data)
                    IMAGE_MISS.pop(name, None)
                    return cached
        except (requests.RequestException, OSError, ValueError):
            pass
    local = COMMUNITY_BUNDLED.parent / "looks" / f"{name}.jpg"
    if local.exists():
        return local
    return sample if name.endswith("-before") and sample.exists() else None
