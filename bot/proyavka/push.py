"""Уведомления о новых кадрах (Web Push): подписки устройств, ключ сервера, сбор уведомлений за короткое окно."""

import base64
import json
import os
import threading
import time

from .config import BASE, log
from .i18n import L, speak
from .util import plural_ru
from .database import q, run


# «Проявлено 3 новых кадра» на телефон и компьютер, когда «Проявка» закрыта. Подписка — у каждого устройства своя
# (включается и выключается в настройках). Кадры, проявленные подряд, — одним уведомлением. Если приложение открыто
# (оно спрашивает сервер каждые 1–5 с), не уведомляем: кадр и так виден. Библиотека pywebpush необязательна.
try:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:
    webpush = None


PUSH_DELAY = 20            # сек: собрать кадры, пришедшие подряд


PUSH_QUIET = 30            # сек: приложение спрашивало сервер недавно — оно открыто


LAST_POLL = {}             # устройство -> когда его приложение последний раз спрашивало сервер (открыто ли)


PUSH_PENDING = {}          # пользователь -> сколько новых кадров ждут уведомления


PUSH_LOCK = threading.Lock()


_VAPID = {}


def vapid():
    """Ключ сервера для Web Push: создаётся один раз и лежит рядом с базой."""
    with PUSH_LOCK:
        if "v" not in _VAPID:
            path = BASE / "vapid.pem"
            if path.exists():
                v = Vapid02.from_file(str(path))
            else:
                v = Vapid02()
                v.generate_keys()
                old = os.umask(0o077)           # закрытый ключ сразу с правами 600, а не после chmod
                try:
                    v.save_key(str(path))
                finally:
                    os.umask(old)
                os.chmod(path, 0o600)
            raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            _VAPID["v"], _VAPID["pub"] = v, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return _VAPID["v"]


def push_key():
    vapid()
    return _VAPID["pub"]


PUSH_HOSTS = ("googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com", "mozaws.net")


def push_host_ok(endpoint):
    """Адрес подписки — только у настоящих push-сервисов браузеров: сервер сам шлёт туда запросы, и по чужому адресу
    (внутренняя сеть, служебные порты) слать нельзя. PUSH_EXTRA_HOSTS — свои через запятую (для тестов и своих сервисов)."""
    from urllib.parse import urlparse
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    extra = tuple(h.strip().lower() for h in os.environ.get("PUSH_EXTRA_HOSTS", "").split(",") if h.strip())
    return u.scheme == "https" and u.port in (None, 443) and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS + extra)


def push_subscribe(uid, did, sub):
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if (not endpoint.startswith("https://") or len(endpoint) > 2000 or not push_host_ok(endpoint)
            or not keys.get("p256dh") or not keys.get("auth")):
        raise ValueError(L("неверная подписка", "invalid subscription"))
    run("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
    run("INSERT INTO push_subs(owner, device, endpoint, p256dh, auth, created) VALUES (?,?,?,?,?,?)",
        (uid, did, endpoint, str(keys["p256dh"])[:200], str(keys["auth"])[:100], time.time()))
    log.info("push: подписка %s… от %d, устройство #%s", endpoint[:40], uid, did)


def push_soon(uid):
    if not webpush:
        return
    with PUSH_LOCK:
        n = PUSH_PENDING.get(uid, 0)
        PUSH_PENDING[uid] = n + 1
    if not n:
        t = threading.Timer(PUSH_DELAY, _push_flush, args=(uid,))
        t.daemon = True
        t.start()


def _push_flush(uid):
    with PUSH_LOCK:
        n = PUSH_PENDING.pop(uid, 0)
    now = time.time()
    # тишина у каждого устройства своя: открытое окно на компьютере не глушит телефон
    subs = [s for s in q("SELECT * FROM push_subs WHERE owner=?", (uid,))
            if not (s["device"] and now - LAST_POLL.get(s["device"], 0) < PUSH_QUIET)]
    if not n or not subs:
        return
    with speak(uid):
        body = (L("Проявлен новый кадр", "A new frame is developed") if n == 1 else
                L(f"Проявлено {n} {plural_ru(n, 'новый кадр', 'новых кадра', 'новых кадров')}", f"{n} new frames developed"))
        data = json.dumps({"title": L("Проявка", "Proyavka"), "body": body, "tag": "new-frames", "url": "/"}, ensure_ascii=False)
    sub_mail = "mailto:proyavka@" + (os.environ.get("DOMAIN") or "localhost")
    sent = 0
    for s in subs:
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}, data,
                    vapid_private_key=vapid(), vapid_claims={"sub": sub_mail}, ttl=6 * 3600, timeout=15)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):              # устройство отписалось или подписка протухла
                run("DELETE FROM push_subs WHERE id=?", (s["id"],))
                log.info("push: подписка #%d больше не действует (%s), удалена", s["id"], code)
            else:
                log.warning("push to %d: %s", uid, e)
        except Exception as e:
            log.warning("push to %d: %s", uid, e)
    log.info("уведомление «%s» для %d: отправлено на %d из %d устройств", body, uid, sent, len(subs))
