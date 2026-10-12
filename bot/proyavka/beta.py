"""Открытая бета: страница /beta, слоты на сервере (место под каждого, срок), вход без приглашения, уход по сроку."""

import os
import re
import shutil
import threading
import time

from PIL import Image

from .config import BASE, COMMUNITY_BUNDLED, MIN_FREE_GB, log
from .i18n import L, speak
from .util import plural_ru, save_atomic
from .film import PRESETS, film
from .database import q, run
from .users import ADMIN, USERS, storage_limit
from .telegram import safe
from .push import push_send
from .storage import user_usage
from .devices import create_device
from .invites import JOIN_LOCK, add_web_user, delete_user, next_web_uid


BETA = os.environ.get("BETA", "0") == "1"                  # открытая бета: /beta и вход без приглашения
SLOT_MB = float(os.environ.get("BETA_SLOT_MB", "500"))      # место на одного
DAYS = float(os.environ.get("BETA_DAYS", "3"))              # сколько живёт слот
MAX_SLOTS = int(os.environ.get("BETA_SLOTS", "30"))         # больше не даём, даже если места хватает (процессор один)
JOINS_PER_IP = 2                                            # новых слотов с одного адреса за сутки
WARN_HOURS = 12                                             # за сколько предупредить, что слот кончается

LANDING_FILMS = ["super400", "portrait400", "golden200", "cine250", "across100", "expired"]

BETA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "beta")   # bot/beta/

_CACHE = {}
JOIN_IPS = {}
WARNED = set()


def _status():
    """Сколько слотов всего: занятые + сколько ещё влезает на диск. Место, обещанное остальным (лимит минус занятое),
    и запас MIN_FREE_GB не трогаем; у беты незаполненная часть слота тоже считается занятой."""
    slot = SLOT_MB * 1e6
    rows = q("SELECT id, name, expires FROM users WHERE expires IS NOT NULL ORDER BY created")
    beta = {r["id"] for r in rows}
    promised = 0.0
    for uid in list(USERS):
        used = sum(user_usage(uid).values())
        promised += max(0.0, (slot if uid in beta else storage_limit(uid) * 1e9) - used)
    free = shutil.disk_usage(BASE).free - MIN_FREE_GB * 1e9 - promised
    room = max(0, int(free // slot))
    total = max(len(rows), min(MAX_SLOTS, len(rows) + room))
    return rows, total


def status(me=None):
    """Для страницы: слоты, ники и сколько осталось. Подсчёт места дорогой — кэш на 20 секунд."""
    now = time.time()
    if now - _CACHE.get("t", 0) > 20:
        _CACHE["v"] = _status()
        _CACHE["t"] = now
    rows, total = _CACHE["v"]
    users = [{"name": r["name"], "left": max(0, int(r["expires"] - now)), "me": r["id"] == me}
             for r in rows if r["expires"] > now]
    return {"on": True, "slot_mb": SLOT_MB, "days": DAYS, "total": max(total, len(users)), "free": max(0, total - len(users)),
            "users": users, "next": min((u["left"] for u in users), default=0)}


def clean_nick(nick):
    nick = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f<>\"'&]", "", str(nick or ""))).strip()
    if not 2 <= len(nick) <= 24:
        raise ValueError(L("ник — от 2 до 24 символов", "a nickname is 2 to 24 characters"))
    return nick


def join(nick, ip, device_name, lang=None, accept=False):
    """Занять свободный слот: новый пользователь без Telegram, сразу с устройством. -> (ключ, id, устройство)."""
    if not BETA:
        raise PermissionError(L("бета закрыта", "the beta is closed"))
    nick = clean_nick(nick)
    if not accept:
        raise ValueError(L("нужно согласие с условиями", "you need to accept the terms"))
    now = time.time()
    if ip:
        recent = [t for t in JOIN_IPS.get(ip, []) if now - t < 86400]
        if len(recent) >= JOINS_PER_IP:
            raise PermissionError(L("с этого адреса уже заняли слот — попробуй завтра", "a slot was already taken from this address — try tomorrow"))
    with JOIN_LOCK:
        rows, total = _status()
        if any(r["name"].casefold() == nick.casefold() for r in rows):
            raise ValueError(L("такой ник уже есть — возьми другой", "this nickname is taken — pick another"))
        if len(rows) >= total:
            raise PermissionError(L("свободных слотов нет — загляни позже", "no free slots — check back later"))
        uid = next_web_uid()
        kw = {"lang": lang} if lang in ("ru", "en") else {}
        add_web_user(uid, nick, storage_gb=SLOT_MB / 1000, expires=now + DAYS * 86400, terms=now, **kw)
        _CACHE.clear()
    if ip:
        JOIN_IPS[ip] = recent + [now]
    log.info("бета: %s (%d) занял слот %d из %d", nick, uid, len(rows) + 1, total)
    with speak(ADMIN):
        safe("sendMessage", chat_id=ADMIN, text=L("Бета: новый участник", "Beta: new tester") + f" — {nick} ({len(rows) + 1}/{total})")
    return create_device(uid, device_name)


def extend(uid, days=None):
    """Продлить слот (администратор): от сейчас или от прежнего срока — что позже."""
    u = USERS.get(uid) or {}
    if not u.get("expires"):
        raise ValueError(L("это не участник беты", "this is not a beta tester"))
    run("UPDATE users SET expires=? WHERE id=?", (max(u["expires"], time.time()) + (days or DAYS) * 86400, uid))
    WARNED.discard(uid)
    _CACHE.clear()
    from .users import load_users
    load_users()


def left_text(sec):
    d, h = int(sec // 86400), int(sec % 86400 // 3600)
    if d:
        return L(f"{d} д {h} ч", f"{d}d {h}h")
    return L(f"{h} {plural_ru(h, 'час', 'часа', 'часов')}", f"{h} h") if h else L("меньше часа", "under an hour")


def tick(now=None):
    """Раз в минуту: предупредить тех, у кого слот кончается, и убрать тех, у кого кончился."""
    now = now or time.time()
    for r in q("SELECT id, name, expires FROM users WHERE expires IS NOT NULL"):
        if r["expires"] <= now:
            with JOIN_LOCK:
                run("INSERT OR REPLACE INTO beta_gone(id, name, gone) VALUES (?,?,?)", (r["id"], r["name"], now))
                n = delete_user(r["id"])
                _CACHE.clear()
            WARNED.discard(r["id"])
            log.info("бета: слот %s (%d) кончился, удалено кадров: %d", r["name"], r["id"], n)
        elif r["expires"] - now < WARN_HOURS * 3600 and r["id"] not in WARNED:
            WARNED.add(r["id"])
            with speak(r["id"]):
                text = L(f"Бета закончится через {left_text(r['expires'] - now)}: аккаунт и кадры удалятся. Скачай то, что хочешь сохранить.",
                         f"Your beta slot ends in {left_text(r['expires'] - now)}: the account and frames will be deleted. Download what you want to keep.")
                safe("sendMessage", chat_id=r["id"], text=text)
            try:
                push_send(r["id"], text, "beta-end")
            except Exception as e:
                log.warning("beta push %d: %s", r["id"], e)


def loop():
    while True:
        try:
            tick()
        except Exception:
            log.exception("beta tick failed")
        time.sleep(60)


# Типографика страниц: короткие предлоги, союзы и частицы не остаются в конце строки, тире не начинает строку,
# число не отрывается от единицы. Только в тексте между тегами (не в атрибутах, скриптах и стилях).
SHORT = ("в|во|и|с|со|к|ко|о|об|у|а|на|по|не|ни|за|из|от|до|для|без|при|но|же|ли|да|то|или|как|что|это|"
         "a|an|the|of|to|in|on|at|by|or|and|is|it|no|for")
TYPO = [(re.compile(rf"(?<![\w\-])({SHORT})[ \t]+(?=\S)", re.I), "\\1\u00a0"),
        (re.compile(r"[ \t]+—"), "\u00a0—"),
        (re.compile(r"(\d)[ \t]+(?=[^\d\s—–-])"), "\\1\u00a0")]
SPLIT = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->|<[^>]*>)", re.S | re.I)


def typo(html):
    parts = SPLIT.split(html)
    for i in range(0, len(parts), 2):
        for rx, to in TYPO:
            parts[i] = rx.sub(to, parts[i])
    return "".join(parts)


PAGES = {}


def page(name):
    """Страница беты с типографикой (кэш до изменения файла)."""
    path = os.path.join(BETA_DIR, name)
    mtime = os.path.getmtime(path)
    if PAGES.get(name, (0,))[0] != mtime:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        gb = SLOT_MB >= 1000
        size = f"{SLOT_MB / 1000:g}" if gb else f"{SLOT_MB:g}"
        days = f"{DAYS:g}"
        for k, v in {"SLOT_RU": f"{size} {'ГБ' if gb else 'МБ'}", "SLOT_EN": f"{size} {'GB' if gb else 'MB'}",
                     "DAYS_RU": f"{days} {plural_ru(int(DAYS), 'день', 'дня', 'дней') if DAYS == int(DAYS) else 'дня'}",
                     "DAYS_EN": f"{days} day{'' if DAYS == 1 else 's'}"}.items():
            text = text.replace("{{" + k + "}}", v)
        PAGES[name] = (mtime, typo(text))
    return PAGES[name][1]


SAMPLE_LOCK = threading.Lock()


def sample_film(key):
    """Общий образец каталога под встроенной плёнкой — для страницы беты (до/после). Рисуется один раз."""
    from .web import FILMS_VERSION
    if key not in LANDING_FILMS or key not in PRESETS:
        return None
    path = BASE / "beta" / f"{key}.{FILMS_VERSION}.jpg"
    with SAMPLE_LOCK:
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            for old in path.parent.glob(f"{key}.*.jpg"):
                old.unlink(missing_ok=True)
            im = Image.open(COMMUNITY_BUNDLED.parent / "sample.jpg").convert("RGB")
            save_atomic(film(im, key, 100, 1, fit=True), str(path), 84)
    return path


def start():
    if not BETA:
        return
    threading.Thread(target=loop, daemon=True, name="beta").start()

    def warm():                          # первый посетитель не ждёт отрисовки
        for k in LANDING_FILMS:
            try:
                sample_film(k)
            except Exception as e:
                log.warning("beta sample %s: %s", k, e)
    threading.Thread(target=warm, daemon=True, name="beta-warm").start()
