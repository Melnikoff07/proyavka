"""Приём кадров: с сервера-приёмника по SSH или из папки, разбор имён и владельцев, дубли, RAW+JPEG, загрузка из приложения, дневной лимит."""

import hashlib
import os
import posixpath
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from PIL import Image
from pathlib import Path

from . import config
from .config import (
    BASE, EXTS, INCOMING, LOCAL, POLL, POLL_BACKUP, RAW_EXTS, RAW_FILES, SETTLE, SSH_CMD, TMP,
    UPLOAD_MAX, VPS, WORK_EDGE, log, remote,
)
from .i18n import L, speak
from .util import plural_ru, remove
from .imaging import fingerprint, is_raw, read_exif
from .database import q, run, upd
from .users import ADMIN, DAILY_LIMIT, USERS, udir, user
from .jobs import job_prepare
from .telegram import safe
from .scheduler import schedule_view
from .storage import enforce_limit


RAW_WAIT = 90          # сек: RAW ждёт, не придёт ли JPEG той же съёмки


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
REMOTE_DIR = config.REMOTE_DIR.rstrip("/") + "/"


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
    from .pools import FAST
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
