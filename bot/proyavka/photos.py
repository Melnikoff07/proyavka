"""Жизнь кадра: убрать в корзину, вернуть, окончательно стереть файлы, скрыть; сообщения в чате при этом убираются."""

import time

from .config import PREVIEWS, log
from .i18n import L
from .util import fsize, remove
from .imaging import has
from .database import get, q, run
from .pools import NET
from .telegram import _delete_messages
from .scheduler import schedule_view


BATCH_MAX = 500


def batch_ids(data, uid, album=None):
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BATCH_MAX:
        raise ValueError(L(f"выбери от 1 до {BATCH_MAX} кадров", f"select 1 to {BATCH_MAX} frames"))
    try:
        ids = list(dict.fromkeys(int(i) for i in ids))
    except (TypeError, ValueError):
        raise ValueError(L("неверный список кадров", "invalid frame list"))
    marks = ",".join("?" * len(ids))
    if album:                                 # гость: только кадры своего альбома
        return [r["id"] for r in q(f"SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo=p.id WHERE ap.album=? "
                                   f"AND p.hidden=0 AND p.owner=? AND p.id IN ({marks}) ORDER BY p.id", (album, uid, *ids))]
    return [r["id"] for r in q(f"SELECT id FROM photos WHERE hidden=0 AND owner=? AND id IN ({marks}) ORDER BY id",
                               (uid, *ids))]


def delete_photos(ids):
    """Убрать кадры из ленты и из чата в корзину. Файлы остаются на диске: кадр можно вернуть через /trash,
    пока не понадобится место, — при нехватке места корзина чистится первой (cleanup).
    Отпечаток (fp) тоже остаётся, поэтому повторная выгрузка того же кадра его не вернёт."""
    now = time.time()
    gone = []
    for pid in ids:
        ph = get(pid)
        if not ph or ph["hidden"]:
            continue
        # rev+1 — рисование, которое уже идёт, не отправит кадр в чат (_view_done это проверяет)
        run("UPDATE photos SET hidden=1, deleted_at=?, file_id=NULL, rev=rev+1, updated=? WHERE id=?", (now, now, pid))
        for p in PREVIEWS.glob(f"{pid}_*.jpg"):      # превью — кэш, их не жалко
            remove(str(p))
        gone.append(ph)
    if gone:
        NET.submit(_delete_messages, gone)
    return len(gone)


def restore_photo(ph):
    """Вернуть кадр из корзины в ленту и в чат."""
    if not has(ph["work"]):
        raise RuntimeError(L("файлы кадра уже удалены, чтобы освободить место", "the frame's files were already deleted to free space"))
    run("UPDATE photos SET hidden=0, deleted_at=NULL, rev=rev+1, updated=? WHERE id=?", (time.time(), ph["id"]))
    schedule_view(ph["id"], prio=0, uid=ph["owner"])     # нарисуется и сам придёт в чат


def purge_files(ph):
    """Стереть файлы кадра из корзины окончательно (строка с отпечатком остаётся)."""
    size = 0
    for k in ("src", "work", "view", "thumb"):
        if ph[k]:
            size += fsize(ph[k])
            remove(ph[k])
    run("UPDATE photos SET src=NULL, work=NULL, view=NULL, thumb=NULL WHERE id=?", (ph["id"],))
    log.info("cleanup: #%d из корзины удалён (%.1f MB)", ph["id"], size / 1e6)
    return size


def purge_trash(uid, ids=None):
    """«Удалить навсегда» из корзины: свои кадры, только те, что уже в корзине. ids=None — вся корзина."""
    from .database import q
    rows = q("SELECT * FROM photos WHERE owner=? AND hidden=1 AND (src IS NOT NULL OR work IS NOT NULL "
             "OR view IS NOT NULL OR thumb IS NOT NULL)", (uid,))
    if ids is not None:
        want = set(ids)
        rows = [r for r in rows if r["id"] in want]
    for ph in rows:
        purge_files(ph)
        for f in PREVIEWS.glob(f"{ph['id']}_*.jpg"):
            remove(str(f))
    return len(rows)


def hide_photo(ph):
    delete_photos([ph["id"]])
