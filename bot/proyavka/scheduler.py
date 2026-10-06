"""Планировщик: очереди отрисовки по срочности и по кругу между пользователями, пакеты, экспорт, очередь обновления сообщений в Telegram, применение правок."""

import collections
import hashlib
import itertools
import json
import os
import queue
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait as futures_wait
from pathlib import Path

from .config import FAST_WORKERS, PREVIEWS, STRENGTHS, TMP, log
from .i18n import L, cur_lang, speak, _CTX
from .util import plural_ru, remove
from .film import PRESETS, canon, clean_params, is_lut, lut_dir, params_json
from .imaging import LEAKS, has, leak_seed, parse_crop
from .database import EDIT_LOCK, get, q, run, upd
from .users import udir, user_lang, valid_look
from .jobs import job_chat, job_full, job_view
from .pools import NET
from .telegram import caption, chat_of, main_kb, pace, safe, tg, touch
from .push import push_soon


EVENTS = queue.Queue()


JOB_LOCK = threading.Lock()


VIEW_INFLIGHT, VIEW_DIRTY = set(), set()


EXPORTING = {}


def dispatcher():
    """Результаты из процессов обрабатываем в одном потоке — без гонок."""
    while True:
        fn, args = EVENTS.get()
        try:
            fn(*args)
        except Exception:
            log.exception("dispatcher")


def jobs_in_work(uid):
    """Сколько кадров этого пользователя сейчас рисуется или ждёт очереди."""
    with JOB_LOCK:
        pids = set(VIEW_INFLIGHT) | set(RQ_QUEUED) | set(EXPORTING)
    if not pids:
        return 0
    pids = list(pids)[:900]
    marks = ",".join("?" * len(pids))
    rows = q(f"SELECT id FROM photos WHERE owner=? AND id IN ({marks})", (uid, *pids))
    mine = {r["id"] for r in rows}
    with JOB_LOCK:
        return sum(1 for p in mine if p in VIEW_INFLIGHT or p in RQ_QUEUED) + sum(EXPORTING.get(p, 0) for p in mine)


# Очередь отрисовки. Сразу в процессы отдаётся не больше задач, чем они успевают (VIEW_SLOTS): иначе сотня кадров
# из пакетной правки встала бы в очередь пула, и превью плёнок в «Проявке» ждали бы их все.
# Срочность: 0 — правка одного кадра (человек ждёт), 1 — пакетная правка и новые кадры, 2 — фоновая догрузка.
# Внутри одной срочности пользователи обслуживаются по кругу, чтобы один большой пакет не задерживал остальных.
VIEW_SLOTS = max(1, FAST_WORKERS - 1) if FAST_WORKERS > 2 else FAST_WORKERS


RQ_PENDING = {0: {}, 1: {}, 2: {}}    # срочность -> {пользователь: deque[pid]} (порядок ключей = очередь по кругу)


RQ_QUEUED = {}                        # pid -> (срочность, нужен ли чат)


VIEW_CHAT = {}                        # pid в работе -> нужна ли версия для чата


VIEW_PRIO = {}                        # pid в работе -> срочность (с ней же правка уйдёт в чат)


def schedule_view(pid, prio=0, chat=True, uid=None):
    """Перерисовать кадр. Если уже рисуется — дорисуем последнее состояние следом."""
    if uid is None:                      # очередь по кругу между владельцами кадров
        r = q("SELECT owner FROM photos WHERE id=?", (pid,))
        uid = r[0]["owner"] if r else 0
    with JOB_LOCK:
        if pid in VIEW_INFLIGHT:
            VIEW_DIRTY.add(pid)
            VIEW_CHAT[pid] = VIEW_CHAT.get(pid, False) or chat
            VIEW_PRIO[pid] = min(VIEW_PRIO.get(pid, prio), prio)
            return
        if pid in RQ_QUEUED:
            old_prio, old_chat = RQ_QUEUED[pid]
            RQ_QUEUED[pid] = (min(prio, old_prio), old_chat or chat)
            if prio < old_prio:                  # стал срочнее — переносим в нужную очередь
                for dq in RQ_PENDING[old_prio].values():
                    if pid in dq:
                        dq.remove(pid)
                RQ_PENDING[prio].setdefault(uid, collections.deque()).append(pid)
        else:
            RQ_QUEUED[pid] = (prio, chat)
            RQ_PENDING[prio].setdefault(uid, collections.deque()).append(pid)
    _pump()


def _pump():
    """Раздать задачи из очереди, пока есть свободные места в процессах."""
    while True:
        with JOB_LOCK:
            if len(VIEW_INFLIGHT) >= VIEW_SLOTS:
                return
            pick = None
            for prio in (0, 1, 2):
                users = RQ_PENDING[prio]
                while users and pick is None:
                    uid = next(iter(users))
                    dq = users.pop(uid)
                    if dq:
                        pid = dq.popleft()
                        if dq:
                            users[uid] = dq      # в конец круга
                        if pid in RQ_QUEUED:
                            pick = (pid, RQ_QUEUED.pop(pid)[1], prio)
                if pick:
                    break
            if pick is None:
                return
            VIEW_INFLIGHT.add(pick[0])
            VIEW_CHAT[pick[0]] = pick[1]
            VIEW_PRIO[pick[0]] = pick[2]
        _submit_view(pick[0])


def queue_size():
    with JOB_LOCK:
        return len(RQ_QUEUED)


def _release(pid):
    with JOB_LOCK:
        VIEW_INFLIGHT.discard(pid)
        VIEW_DIRTY.discard(pid)
        VIEW_CHAT.pop(pid, None)
        VIEW_PRIO.pop(pid, None)
    batch_step(pid, "gone")


# Уже нарисованные состояния кадра (плёнка, сила, засвет, кроп, дата, рамка) лежат в кэше превью: вернулся к плёнке,
# которую только что смотрел, — экран кадра берётся из кэша, а не рисуется заново. Файл копируется вместе со временем
# изменения, поэтому адрес картинки (?v=) тот же, что и в прошлый раз, и браузер показывает её из своего кэша сразу.
VIEW_CACHE_KEEP = 8                    # состояний на кадр; кэш превью и так чистится раз в сутки


_PRESETS_SIG = hashlib.sha1(json.dumps({k: params_json(clean_params(v)) for k, v in PRESETS.items()},
                                       sort_keys=True).encode()).hexdigest()[:12]


def _look_sig(ph):
    """Версия плёнки кадра: у встроенных — их параметры, у своих — содержимое файла плёнки (правка меняет вид)."""
    key = ph["preset"]
    if not is_lut(key):
        return _PRESETS_SIG
    h = hashlib.sha1()
    for ext in (".json", ".npy"):
        try:
            st = os.stat(lut_dir(ph["owner"]) / f"{key}{ext}")
            h.update(f"{ext}{st.st_mtime_ns}.{st.st_size}".encode())
        except OSError:
            pass
    try:
        h.update((lut_dir(ph["owner"]) / f"{key}.json").read_bytes())
    except OSError:
        pass
    return h.hexdigest()[:12]


def view_state(ph):
    """Всё, от чего зависит экран кадра в «Проявке», одной строкой."""
    leak = bool(ph["leak"])
    parts = [ph["work"], ph["preset"], _look_sig(ph), ph["strength"], ph.get("leak_kind") or "edge" if leak else "",
             leak_seed(ph) if leak else 0, ph.get("crop") or "", bool(ph["stamp"]), ph["taken"] if ph["stamp"] else "",
             bool(ph["frame"]), user_lang(ph["owner"]) if ph["frame"] else ""]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _cache_paths(pid, key):
    return PREVIEWS / f"{pid}_vc_{key}.jpg", PREVIEWS / f"{pid}_tc_{key}.jpg"


def _copy(src, dst):
    """Жёсткая ссылка (без копирования; время изменения то же, а от него зависит адрес картинки), подменяется целиком.
    Новая отрисовка пишет файл заново (save_atomic: новый файл и os.replace), так что кэш она не трогает."""
    tmp = f"{dst}.{threading.get_ident()}.tmp"
    remove(tmp)
    try:
        try:
            os.link(src, tmp)
        except OSError:                  # нет жёстких ссылок (редкие файловые системы) — обычная копия
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        remove(tmp)


def _cache_store(pid, key, view, thumb):
    vc, tc = _cache_paths(pid, key)
    try:
        if not (vc.exists() and tc.exists()):
            _copy(view, vc)
            _copy(thumb, tc)
        old = sorted(PREVIEWS.glob(f"{pid}_vc_*.jpg"), key=lambda f: f.stat().st_ctime, reverse=True)[VIEW_CACHE_KEEP:]
        for f in old:
            remove(str(f))
            remove(str(f.with_name(f.name.replace("_vc_", "_tc_", 1))))
    except OSError as e:
        log.warning("view cache #%d: %s", pid, e)


def _cache_take(pid, key, view, thumb):
    """Готовый экран этого состояния — на место текущего. True, если он был в кэше."""
    vc, tc = _cache_paths(pid, key)
    if not (vc.exists() and tc.exists()):
        return False
    try:
        _copy(vc, view)
        _copy(tc, thumb)
        return True
    except OSError as e:
        log.warning("view cache #%d: %s", pid, e)
        return False


def _submit_view(pid):
    from .pools import FAST
    ph = get(pid)
    if not ph or ph["hidden"] or not has(ph["work"]):
        _release(pid)
        _pump()
        return
    rev = ph["rev"]
    with JOB_LOCK:
        chat = VIEW_CHAT.get(pid, True) and bool(ph["msg_id"] or not ph["file_id"])
    view = str(udir(ph["owner"], "views") / f"{pid}.jpg")
    thumb = str(udir(ph["owner"], "thumbs") / f"{pid}.jpg")
    key = view_state(ph)
    chat_path = str(TMP / f"chat_{pid}_{rev}.jpg") if chat else None
    if _cache_take(pid, key, view, thumb):
        # это состояние уже рисовали: экран готов сразу (ответ на правку уже без «проявляется»). Версия для чата,
        # если нужна, рисуется здесь же, в этом месте очереди: по кругу между пользователями, как и раньше
        upd(pid, view=view, thumb=thumb, rendered_rev=rev, updated=time.time())
        if chat_path:
            # версия для чата рисуется отдельно и встаёт в процессы сейчас, в свою очередь по кругу; место в очереди
            # экрана при этом свободно — следующее переключение плёнки не ждёт Telegram
            with JOB_LOCK:
                urgent = VIEW_PRIO.get(pid, 1) == 0
            FAST.submit(job_chat, ph, chat_path).add_done_callback(
                lambda f: EVENTS.put((_chat_done, (pid, rev, chat_path, urgent, f))))
        out = Future()
        out.set_result((view, thumb, "cached"))
        EVENTS.put((_view_done, (pid, rev, False, out, key)))
        return
    fut = FAST.submit(job_view, ph, view, thumb, chat_path)
    fut.add_done_callback(lambda f: EVENTS.put((_view_done, (pid, rev, chat, f, key))))


def _chat_done(pid, rev, path, urgent, fut):
    """Версия для чата к экрану, взятому из кэша."""
    if fut.exception():
        log.warning("chat render #%d: %s", pid, fut.exception())
        remove(path)
        return
    cur = get(pid)
    if not cur or cur["hidden"] or cur["rev"] != rev:     # уже поменяли — свежий вариант в очереди
        remove(path)
        return
    queue_tg_update(pid, urgent=urgent)


def _view_done(pid, rev, chat, fut, key=None):
    err = fut.exception()
    result = "fail"
    if err:
        log.warning("view #%d: %s", pid, err)
    else:
        res = fut.result()
        view, thumb, cached = res[0], res[1], len(res) > 2
        if key and not cached:
            _cache_store(pid, key, view, thumb)
        cur = get(pid)
        if not cur or cur["hidden"]:            # кадр убрали в корзину, пока он рисовался — в чат не слать
            remove(str(TMP / f"chat_{pid}_{rev}.jpg"))
            result = "gone"
        elif cur["rev"] == rev:
            if not cached:
                upd(pid, view=view, thumb=thumb, rendered_rev=rev, updated=time.time())
            result = "ok"
            if not cur.get("view") and (cur.get("created") or 0) > time.time() - 600:
                push_soon(cur["owner"])          # новый кадр проявился впервые (не дорисовка старых)
            if chat:
                with JOB_LOCK:
                    prio = VIEW_PRIO.get(pid, 1)
                queue_tg_update(pid, urgent=prio == 0)
    with JOB_LOCK:
        again = pid in VIEW_DIRTY
        VIEW_DIRTY.discard(pid)
        if not again:
            VIEW_INFLIGHT.discard(pid)
            VIEW_CHAT.pop(pid, None)
            VIEW_PRIO.pop(pid, None)
    if again:
        _submit_view(pid)
    else:
        batch_step(pid, result)
        _pump()


# Пакетная правка из «Проявки»: кадры встают в общую очередь со срочностью 1, а когда проявится последний,
# в чат уходит одно итоговое сообщение. Сами фото в чате обновляются фоном, не чаще раза в секунду.
BATCHES = {}      # номер пакета -> {"left": set(pid), "ok", "fail", "total", "text"}


BATCH_OF = {}     # pid -> номер пакета, в котором он ещё не проявлен


_BATCH_SEQ = itertools.count(1)


def batch_track(pids, text, uid):
    finished = []
    with JOB_LOCK:
        bid = next(_BATCH_SEQ)
        BATCHES[bid] = {"left": set(pids), "pids": list(pids), "ok": 0, "fail": 0, "total": len(pids), "text": text,
                        "uid": uid, "lang": cur_lang()}
        for pid in pids:
            old = BATCH_OF.get(pid)
            if old in BATCHES:                   # кадр перешёл в новый пакет — в старом считаем его готовым
                b = BATCHES[old]
                b["left"].discard(pid)
                b["ok"] += 1
                if not b["left"]:
                    finished.append(BATCHES.pop(old))
            BATCH_OF[pid] = bid
    for b in finished:
        NET.submit(_batch_report, b)
    return bid


def batch_step(pid, result):
    """Кадр из пакета дорисован (ok), не получился (fail) или удалён (gone)."""
    with JOB_LOCK:
        bid = BATCH_OF.pop(pid, None)
        b = BATCHES.get(bid)
        if not b:
            return
        b["left"].discard(pid)
        b["ok" if result == "ok" else "fail"] += result != "gone"
        if result == "gone":
            b["total"] -= 1
        if b["left"]:
            return
        BATCHES.pop(bid)
    NET.submit(_batch_report, b)


def _batch_report(b):
    if b["total"] <= 0:
        return
    t = time.time()
    while time.time() - t < 900:                 # итог — после того, как сами фото в чате обновились
        with TG_COND:
            if not TG_BUSY.intersection(b["pids"]):
                break
        time.sleep(0.5)
    n = b["ok"]
    _CTX.lang = b["lang"]
    text = L(f"Готово: {n} {plural_ru(n, 'кадр', 'кадра', 'кадров')} → {b['text']}",
             f"Done: {n} frame{'' if n == 1 else 's'} → {b['text']}")
    if b["fail"]:
        text += L(f"\nНе получилось: {b['fail']}", f"\nFailed: {b['fail']}")
    pace(b["uid"])
    safe("sendMessage", chat_id=b["uid"], text=text, disable_notification=True)


def export_photo(pid):
    """Экспорт в полный размер — в отдельной очереди, не мешает переключению плёнок."""
    from .pools import HEAVY
    ph = get(pid)
    if not ph or ph["hidden"]:
        raise RuntimeError(L("кадр не найден", "frame not found"))
    if not has(ph["work"]) and not has(ph["src"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    with JOB_LOCK:
        EXPORTING[pid] = EXPORTING.get(pid, 0) + 1
    touch(pid)
    out = TMP / f"full_{pid}_{int(time.time() * 1000)}.jpg"
    fut = HEAVY.submit(job_full, ph, str(out))
    fut.add_done_callback(lambda f: NET.submit(_export_done, pid, ph, out, f))


def _export_done(pid, ph, out, fut):
    with speak(ph["owner"]):
        _export_send(pid, ph, out, fut)


def _export_send(pid, ph, out, fut):
    chat = ph["owner"]
    try:
        err = fut.exception()
        if err:
            safe("sendMessage", chat_id=chat, text=L(f"Не смог экспортировать #{pid}: {err}", f"Could not export #{pid}: {err}"))
            return
        note = None if has(ph["src"]) else L("Оригинал уже удалён для экономии места, это версия для чата.",
                                         "The original was deleted to save space; this is the chat version.")
        cur = get(pid) or ph
        if cur["hidden"]:                       # удалили, пока готовился файл
            return
        with open(out, "rb") as f:
            tg("sendDocument", files={"document": (f"{Path(ph['name']).stem}_{ph['preset']}.jpg", f)},
               chat_id=chat, reply_to_message_id=cur["msg_id"], caption=note)
    except Exception as e:
        log.exception("export upload #%d", pid)
        safe("sendMessage", chat_id=chat, text=L(f"Не смог отправить файл #{pid}: {e}", f"Could not send file #{pid}: {e}"))
    finally:
        remove(str(out))
        with JOB_LOCK:
            EXPORTING[pid] = EXPORTING.get(pid, 1) - 1
            if EXPORTING[pid] <= 0:
                EXPORTING.pop(pid, None)
        touch(pid)


TG_PENDING = set()


TG_URGENT = set()      # правки одного кадра — в чат вперёд пакетных


TG_BUSY = set()        # кадры, которые ещё не дошли до чата (в очереди, рисуются или заливаются)


TG_COND = threading.Condition()


def queue_tg_update(pid, urgent=False):
    r = q("SELECT owner FROM photos WHERE id=?", (pid,))
    if not r or chat_of(r[0]["owner"]) is None:
        return                   # у владельца нет Telegram — кадр живёт только в «Проявке»
    with TG_COND:
        TG_PENDING.add(pid)
        TG_BUSY.add(pid)
        if urgent:
            TG_URGENT.add(pid)
        TG_COND.notify()


def _tg_order(pid):
    return (pid not in TG_URGENT, pid)


def _tg_settled(pid):
    with TG_COND:
        if pid not in TG_PENDING:
            TG_BUSY.discard(pid)


TG_RENDER_AHEAD = 2   # сколько кадров для чата рисуем заранее, пока заливается текущий


def _tg_upload(pid, rev, path):
    ph = get(pid)
    if not ph or ph["hidden"] or ph["rev"] != rev:
        return   # кадр убрали или снова поменяли — свежий вариант уже в очереди
    chat = ph["owner"]
    pace(chat)
    with speak(chat), EDIT_LOCK, open(path, "rb") as f:
        res = None
        if ph["msg_id"]:
            media = {"type": "photo", "media": "attach://f", "caption": caption(ph)}
            try:
                res = tg("editMessageMedia", files={"f": ("p.jpg", f)}, chat_id=chat,
                         message_id=ph["msg_id"], media=media, reply_markup=main_kb(ph))
            except Exception as e:
                if "not modified" in str(e):
                    return
                log.warning("edit #%d failed (%s), sending new", pid, e)
                f.seek(0)
        if res is None:
            res = tg("sendPhoto", files={"photo": ("p.jpg", f)}, chat_id=chat,
                     caption=caption(ph), reply_markup=main_kb(ph))
            upd(pid, msg_id=res["message_id"], msg_at=time.time())
        upd(pid, file_id=res["photo"][-1]["file_id"])


def older_unsent(pid):
    """Есть ли более ранний новый кадр, ещё не отправленный в чат (не старше 2 минут, чтобы сбойный не держал очередь)."""
    return q("SELECT COUNT(*) AS n FROM photos WHERE id < ? AND msg_id IS NULL AND hidden=0 "
             "AND work IS NOT NULL AND created > ? AND owner=(SELECT owner FROM photos WHERE id=?)",
             (pid, time.time() - 120, pid))[0]["n"] > 0


def tg_worker():
    """Конвейер: следующие кадры рисуются, пока текущий заливается. Новые кадры уходят в чат по порядку."""
    from .pools import FAST
    inflight = {}   # pid -> (rev, future, path)
    ready = {}      # pid -> (rev, path): нарисован, ждёт отправки
    while True:
        start_now = []
        with TG_COND:
            if not TG_PENDING and not inflight:
                TG_COND.wait(timeout=0.5 if ready else None)   # ждём, не крутясь вхолостую
            for pid in sorted(TG_PENDING, key=_tg_order):
                if len(inflight) + len(start_now) >= TG_RENDER_AHEAD:
                    break
                if pid in inflight or pid in ready:
                    continue
                TG_PENDING.discard(pid)
                start_now.append(pid)
        for pid in start_now:
            ph = get(pid)
            if not ph or ph["hidden"] or not has(ph["work"]):
                _tg_settled(pid)
                continue
            path = TMP / f"chat_{pid}_{ph['rev']}.jpg"
            if path.exists():                  # обычно уже нарисован вместе с версией для «Проявки»
                ready[pid] = (ph["rev"], path)
                continue
            inflight[pid] = (ph["rev"], FAST.submit(job_chat, ph, str(path)), path)
        if inflight:
            done, _ = futures_wait([v[1] for v in inflight.values()], timeout=0.5, return_when=FIRST_COMPLETED)
            for pid, (rev, fut, path) in list(inflight.items()):
                if fut in done:
                    inflight.pop(pid)
                    if fut.exception():
                        log.warning("chat render #%d: %s", pid, fut.exception())
                        remove(str(path))
                        _tg_settled(pid)
                    else:
                        ready[pid] = (rev, path)
        for pid in sorted(ready, key=_tg_order):
            ph = get(pid)
            if ph and not ph["msg_id"] and older_unsent(pid):
                continue   # новый кадр не обгоняет более ранний, который ещё проявляется
            rev, path = ready.pop(pid)
            with TG_COND:
                TG_URGENT.discard(pid)
            try:
                _tg_upload(pid, rev, path)
            except Exception:
                log.exception("tg upload #%d", pid)
            finally:
                remove(str(path))
                _tg_settled(pid)


def apply_changes(ph, changes, sync_tg=None, prio=0):
    """Поставить изменения в очередь и сразу вернуть состояние. Рисуется фоном.
    prio: 0 — правка одного кадра, 1 — пакетная (уступает одиночным)."""
    fields = {}
    cur = get(ph["id"])
    if "preset" in changes:
        k = canon(changes["preset"])
        if k == "auto" and cur:                 # пакетом: каждому кадру его собственный автовыбор
            k = cur["auto_key"] or "original"
        if not valid_look(k, (cur or ph)["owner"]):
            raise ValueError(L("неизвестная плёнка", "unknown film"))
        fields["preset"] = k
    if "strength" in changes:
        s = int(changes["strength"])
        if s not in STRENGTHS:
            raise ValueError(L("неверная сила", "invalid strength"))
        fields["strength"] = s
    for f in ("stamp", "frame"):
        if f in changes:
            fields[f] = 1 if changes[f] else 0
    if not cur or not has(cur["work"]):
        raise RuntimeError(L("кадр в архиве: исходник удалён для экономии места", "frame is archived: the original was deleted to save space"))
    if "leak" in changes:
        v = changes["leak"]
        if v in (None, "", False, 0):
            fields["leak"] = 0
        elif v is True or v == 1:
            fields["leak"] = 1
        elif v in LEAKS:
            fields["leak"], fields["leak_kind"] = 1, v
        else:
            raise ValueError(L("неизвестный засвет", "unknown light leak"))
    if "crop" in changes:
        fields["crop"] = parse_crop(changes["crop"])
    if changes.get("leak_shift"):
        fields["leak_seed"] = int(cur.get("leak_seed") or 0) + 1
        fields.setdefault("leak", 1)
    fields = {k: v for k, v in fields.items() if cur.get(k) != v}   # уже так — не перерисовывать
    if not fields:
        return cur
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE photos SET {cols}, rev=rev+1, updated=? WHERE id=?", (*fields.values(), time.time(), ph["id"]))
    schedule_view(ph["id"], prio=prio)
    return get(ph["id"])
