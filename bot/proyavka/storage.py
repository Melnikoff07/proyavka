"""Хранилище: сколько места занимает каждый, чистка по лимитам (оригиналы, рабочие копии, корзина), свободное место на диске."""

import shutil
import time

from .config import BASE, MIN_FREE_GB, ORIG_DAYS, PREVIEWS, TMP, log
from .i18n import L
from .util import fsize, remove
from .database import q, upd
from .users import ADMIN, USERS, storage_limit, udir


def user_usage(uid):
    """Сколько места занимают кадры пользователя: оригиналы, рабочие копии, картинки для просмотра."""
    use = {"src": 0, "work": 0, "pics": 0}
    for r in q("SELECT src, work, view, thumb FROM photos WHERE owner=?", (uid,)):
        use["src"] += fsize(r["src"]) if r["src"] else 0
        use["work"] += fsize(r["work"]) if r["work"] else 0
        use["pics"] += (fsize(r["view"]) if r["view"] else 0) + (fsize(r["thumb"]) if r["thumb"] else 0)
    return use


def _free_space(rows_sql, args, need):
    """Удалять оригиналы, потом рабочие копии, от самых старых, пока need() не скажет «хватит»."""
    for field in ("src", "work"):
        for r in q(rows_sql.format(f=field), args):
            if not need():
                return
            size = fsize(r["p"])
            remove(r["p"])
            upd(r["id"], **{field: None}, updated=time.time())
            yield size
            log.info("cleanup: #%d %s removed (%.1f MB)", r["id"], field, size / 1e6)


def cleanup():
    from .photos import purge_files
    now = time.time()
    for p in PREVIEWS.iterdir():
        if p.is_file() and now - p.stat().st_mtime > 86400:
            remove(p)
    for p in TMP.iterdir():                 # версии для чата, которые устарели, пока ждали отправки
        if p.is_file() and now - p.stat().st_mtime > 3600:
            remove(p)
    # 1) старые оригиналы (они есть на карте камеры)
    for r in q("SELECT id, src FROM photos WHERE src IS NOT NULL AND created < ?", (now - ORIG_DAYS * 86400,)):
        remove(r["src"])
        upd(r["id"], src=None)
    # 2) лимит каждого пользователя
    for uid in list(USERS):
        enforce_limit(uid)
    # 3) свободное место на диске — общее, от самых старых кадров всех пользователей
    min_free = MIN_FREE_GB * 1e9
    free = [shutil.disk_usage(BASE).free]
    if free[0] < min_free:
        for f in PREVIEWS.glob("*_full_*.jpg"):     # кэш полных кадров для альбомов — его не жалко
            free[0] += fsize(f)
            remove(f)
    if free[0] < min_free:
        for ph in q("SELECT * FROM photos WHERE hidden=1 AND (src IS NOT NULL OR work IS NOT NULL "
                    "OR view IS NOT NULL OR thumb IS NOT NULL) ORDER BY deleted_at, id"):
            if free[0] >= min_free:
                return
            free[0] += purge_files(ph)
        for size in _free_space("SELECT id, {f} AS p FROM photos WHERE {f} IS NOT NULL ORDER BY id", (),
                                lambda: free[0] < min_free):
            free[0] += size


def enforce_limit(uid):
    """Лимит места пользователя: сначала корзина, потом старые оригиналы, потом рабочие копии."""
    from .photos import purge_files
    use = user_usage(uid)
    total = [sum(use.values())]
    limit = storage_limit(uid) * 1e9
    if total[0] <= limit:
        return
    for ph in q("SELECT * FROM photos WHERE owner=? AND hidden=1 AND (src IS NOT NULL OR work IS NOT NULL "
                "OR view IS NOT NULL OR thumb IS NOT NULL) ORDER BY deleted_at, id", (uid,)):
        if total[0] <= limit:                # сначала корзина: самое давно удалённое
            break
        total[0] -= purge_files(ph)
    if total[0] <= limit:
        return
    for size in _free_space("SELECT id, {f} AS p FROM photos WHERE owner=? AND {f} IS NOT NULL ORDER BY id",
                            (uid,), lambda: total[0] > limit):
        total[0] -= size
    for p in sorted(udir(uid, "originals").glob("failed_*"), key=lambda x: x.stat().st_mtime):
        if total[0] <= limit:
            break
        total[0] -= fsize(p)
        remove(p)


def storage_text(uid):
    n = q("SELECT COUNT(*) AS n, SUM(src IS NOT NULL) AS o, SUM(work IS NOT NULL) AS w FROM photos "
          "WHERE hidden=0 AND owner=?", (uid,))[0]
    use = user_usage(uid)
    gb = lambda b: (f"{b / 1e9:.1f} " + L("ГБ", "GB")) if b >= 1e9 or storage_limit(uid) >= 1 else (f"{b / 1e6:.0f} " + L("МБ", "MB"))
    lim = (f"{storage_limit(uid):g} " + L("ГБ", "GB")) if storage_limit(uid) >= 1 else (f"{storage_limit(uid) * 1000:.0f} " + L("МБ", "MB"))
    text = L(f"Кадров в ленте: {n['n'] or 0}\n"
             f"С оригиналом: {n['o'] or 0}, можно менять плёнку: {n['w'] or 0}\n"
             f"Оригиналы: {gb(use['src'])}, рабочие копии: {gb(use['work'])}, превью: {gb(use['pics'])}\n"
             f"Занято {gb(sum(use.values()))} из {lim}, оригиналы живут {ORIG_DAYS:g} дн.",
             f"Frames in feed: {n['n'] or 0}\n"
             f"With original: {n['o'] or 0}, film can be changed: {n['w'] or 0}\n"
             f"Originals: {gb(use['src'])}, working copies: {gb(use['work'])}, previews: {gb(use['pics'])}\n"
             f"Used {gb(sum(use.values()))} of {lim}, originals kept {ORIG_DAYS:g} days")
    if uid == ADMIN:
        du = shutil.disk_usage(BASE)
        text += L(f"\nСвободно на диске: {gb(du.free)} из {gb(du.total)}", f"\nFree disk space: {gb(du.free)} of {gb(du.total)}")
    return text
