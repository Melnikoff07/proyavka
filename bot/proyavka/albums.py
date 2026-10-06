"""Альбомы по ссылке: создание из выбранных кадров, публичная страница, кадры и архив без входа, кэш полных размеров."""

import collections
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import ALBUM_HTML, CONTACT_TG, PREVIEWS, PROJECT_URL, WEBAPP_URL, log
from .i18n import L
from .util import download_name, html_esc
from .imaging import has
from .database import DB_LOCK, q, run, run_count
from .users import USERS
from .jobs import job_full


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
    from .web import batch_ids
    ids = batch_ids(data, uid)
    if not ids:
        raise ValueError(L("в альбоме нет ни одного кадра", "the album has no frames"))
    return ids


def save_album_photos(aid, ids):
    from .database import db
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
    from .devices import ZIP_MAX
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
    from .pools import HEAVY
    path = PREVIEWS / f"{ph['id']}_full_{ph['rev'] or 0}.jpg"
    with FULL_LOCKS[ph["id"]]:
        if path.exists():
            os.utime(path)
        else:
            HEAVY.submit(job_full, ph, str(path)).result(timeout=300)
    return path
