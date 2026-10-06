"""База SQLite: подключение, блокировки, схема и миграции, запросы q/run и кадр по номеру (get/upd)."""

import sqlite3
import threading
import time

from .config import CHAT_ID, DB_PATH, LANG, WEB_BASE
from .film import OLD_KEYS


DB_LOCK = threading.RLock()      # доступ к sqlite


EDIT_LOCK = threading.RLock()    # перерисовка кадра + правка сообщения


db = None                        # открывается в init_db() только в главном процессе: работникам база не нужна


def init_db():
    from .users import load_luts, load_users
    global db
    db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=5000")
    with DB_LOCK:
        db.execute("""CREATE TABLE IF NOT EXISTS photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, src TEXT, work TEXT, thumb TEXT,
            taken TEXT, iso INTEGER, auto_key TEXT, auto_reason TEXT,
            preset TEXT, strength INTEGER DEFAULT 100,
            stamp INTEGER DEFAULT 0, frame INTEGER DEFAULT 0, leak INTEGER DEFAULT 0,
            msg_id INTEGER, file_id TEXT, hidden INTEGER DEFAULT 0, created REAL)""")
        cols = {r[1] for r in db.execute("PRAGMA table_info(photos)")}
        if "view" not in cols:
            db.execute("ALTER TABLE photos ADD COLUMN view TEXT")
        for col, decl in (("rev", "INTEGER DEFAULT 0"), ("rendered_rev", "INTEGER DEFAULT 0"), ("updated", "REAL DEFAULT 0"),
                          ("leak_kind", "TEXT DEFAULT 'edge'"), ("leak_seed", "INTEGER DEFAULT 0"), ("fp", "TEXT"),
                          ("msg_at", "REAL"), ("crop", "TEXT"), ("owner", "INTEGER"),
                          ("deleted_at", "REAL")):
            if col not in cols:
                db.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
        # несколько пользователей: все кадры прежних версий — администратора (того, кто ставил бота)
        db.execute("UPDATE photos SET owner=? WHERE owner IS NULL", (CHAT_ID,))
        db.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, role TEXT DEFAULT 'user', name TEXT, lang TEXT, default_film TEXT DEFAULT 'auto',
            storage_gb REAL, cam_token TEXT, ftp_pass TEXT, created REAL, invited_by INTEGER)""")
        db.execute("""CREATE TABLE IF NOT EXISTS luts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, name TEXT, size INTEGER, created REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS submissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, by TEXT, params TEXT, h TEXT, created REAL, ip TEXT, status TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS invites(
            code TEXT PRIMARY KEY, created REAL, by INTEGER, used_by INTEGER, used_at REAL)""")
        ucols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        if "tg" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN tg INTEGER")       # чат в Telegram (пусто — без Telegram)
        if "media_epoch" not in ucols:                                   # растёт при отвязке устройства: старые ссылки на картинки гаснут
            db.execute("ALTER TABLE users ADD COLUMN media_epoch INTEGER DEFAULT 0")
        db.execute("""CREATE TABLE IF NOT EXISTS pairs(
            code TEXT PRIMARY KEY, uid INTEGER, exp REAL, kind TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS push_subs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, device INTEGER, endpoint TEXT UNIQUE,
            p256dh TEXT, auth TEXT, created REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, name TEXT, hash TEXT UNIQUE, created REAL, seen REAL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS albums(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, token TEXT UNIQUE, title TEXT,
            created REAL, updated REAL, views INTEGER DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS album_photos(
            album INTEGER, photo INTEGER, PRIMARY KEY(album, photo))""")
        db.execute("INSERT OR IGNORE INTO users(id, role, lang, created) VALUES (?, 'admin', ?, ?)", (CHAT_ID, LANG, time.time()))
        db.execute("UPDATE users SET role = CASE WHEN id=? THEN 'admin' ELSE 'user' END", (CHAT_ID,))
        db.execute("UPDATE users SET tg=id WHERE tg IS NULL AND id < ?", (WEB_BASE,))   # пришедшие через Telegram
        db.execute("CREATE INDEX IF NOT EXISTS photos_owner ON photos(owner, hidden, taken)")
        db.execute("CREATE INDEX IF NOT EXISTS photos_fp ON photos(fp)")
        # мини-приложение каждые 1–5 секунд спрашивает «что изменилось» и листает ленту — без индексов это полный перебор
        db.execute("CREATE INDEX IF NOT EXISTS photos_updated ON photos(updated)")
        db.execute("CREATE INDEX IF NOT EXISTS photos_feed ON photos(hidden, id)")
        for old, new in OLD_KEYS.items():
            db.execute("UPDATE photos SET preset=? WHERE preset=?", (new, old))
            db.execute("UPDATE photos SET auto_key=? WHERE auto_key=?", (new, old))
        db.commit()
    load_users()
    load_luts()


def q(sql, args=()):
    with DB_LOCK:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def run(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def run_count(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


def get(pid):
    rows = q("SELECT * FROM photos WHERE id=?", (pid,))
    return rows[0] if rows else None


def upd(pid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    run(f"UPDATE photos SET {cols} WHERE id=?", (*kw.values(), pid))
