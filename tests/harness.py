"""Тестовый стенд «Проявки»: бот целиком, но Telegram заменён записью вызовов.

    venv\\Scripts\\python.exe harness.py              # интерактивно: веб на http://127.0.0.1:8099
    import harness; h = harness.start()             # из тестов

В мини-приложение вместо Telegram подставляется заглушка (window.__FAKE_TG__) — открывается в обычном браузере.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth  # noqa: E402
synth.ensure()

CALLS = []
_mid = [100]
_lock = threading.Lock()


def fake_tg(method, files=None, **params):
    with _lock:
        rec = {k: v for k, v in params.items() if k not in ("media",)}
        if files:
            rec["_files"] = list(files)
        CALLS.append((time.time(), method, rec))
        if method in ("sendPhoto", "sendDocument", "sendMessage", "sendMediaGroup"):
            _mid[0] += 1
            m = _mid[0]
            if method == "sendMediaGroup":
                return [{"message_id": m + i, "photo": [{"file_id": f"F{m + i}"}]} for i in range(len(params.get("media", [])))]
            return {"message_id": m, "photo": [{"file_id": f"F{m}"}], "document": {"file_id": f"D{m}"}}
        if method == "editMessageMedia":
            _mid[0] += 1
            return {"message_id": params.get("message_id"), "photo": [{"file_id": f"F{_mid[0]}"}]}
        if method == "getMe":
            return {"id": 999, "username": "proyavka_test_bot"}
        if method == "getUpdates":
            time.sleep(1)
            return []
        return True


CAM = []      # вызовы скрипта камер на сервере-приёмнике


def fake_cam_helper(action, name, stdin=""):
    CAM.append((action, name, stdin))
    return ""


FAKE_TG_JS = """<script>
window.__FAKE_TG__ = { initData: "test-%(uid)s", colorScheme: "light", ready(){}, expand(){}, disableVerticalSwipes(){},
  setHeaderColor(){}, setBackgroundColor(){}, setBottomBarColor(){}, onEvent(){},
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { selectionChanged(){}, notificationOccurred(){}, impactOccurred(){} },
  showConfirm(t, cb){ cb(window.confirm(t)); }, showAlert(t){ alert(t); } };
</script>"""


def start(port=8099, lang="ru", workers="2", uid=1, extra_env=None, before=None):
    base = tempfile.mkdtemp(prefix="proyavka-h-")
    if before:
        before(Path(base))           # например, база от прежней версии — проверить миграцию
    os.environ.update(BOT_TOKEN="123:TEST", CHAT_ID=str(uid), BASE_DIR=base, FAST_WORKERS=workers, HEAVY_WORKERS="1",
                      WEB_PORT=str(port), LANGUAGE=lang, WEBAPP_URL=f"http://127.0.0.1:{port}/", VPS="local",
                      REMOTE_DIR=str(Path(base) / "remote") + os.sep)
    os.environ.update(extra_env or {})
    import filmbot as fb
    from proyavka import config
    fb.real_tg = fb.tg_send
    fb.tg_send = fake_tg          # tg() по-прежнему переводит номер пользователя в его чат
    fb.fetch_from_vps = lambda *a, **k: None
    fb.vps_watch = lambda *a, **k: None
    fb.sweep_vps = lambda *a, **k: None
    # мини-приложение с заглушкой Telegram
    page = config.WEBAPP_HTML.read_text(encoding="utf-8")
    page = page.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', FAKE_TG_JS % {"uid": uid})
    # ?browser — как вне Telegram (вход ключом устройства)
    tg_line = "const tg = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData ? window.Telegram.WebApp : null;"
    assert tg_line in page
    page = page.replace(tg_line, 'const tg = new URLSearchParams(location.search).has("browser") ? null : window.__FAKE_TG__;')
    fake_page = Path(base) / "webapp.html"
    fake_page.write_text(page, encoding="utf-8")
    config.WEBAPP_HTML = fake_page
    fb.cam_helper = fake_cam_helper
    orig_check = fb.check_init_data
    fb.check_init_data = lambda init: (int(init.split("-", 1)[1]) if str(init).startswith("test-") else orig_check(init))
    if hasattr(fb, "harness_start"):
        fb.harness_start()
    else:
        _start_like_main(fb)
    return Harness(fb, port, base)


def _start_like_main(fb):
    fb.init_db()
    state = fb.load_state()
    state.setdefault("default", "auto")
    threading.Thread(target=fb.dispatcher, daemon=True).start()
    fb.init_pools()
    threading.Thread(target=fb.tg_worker, daemon=True).start()
    fb.start_web()
    threading.Thread(target=fb.ingest_loop, args=(state,), daemon=True).start()
    fb._harness_state = state


def done(code=0):
    """Конец теста: пул отрисовки порождает процессы-работники, и если выйти просто так, они остаются жить и держат
    вывод (запуск из run_all или CI тогда не заканчивается). Гасим их и выходим."""
    import multiprocessing
    sys.stdout.flush()
    for c in multiprocessing.active_children():
        c.kill()
    os._exit(code)


class Harness:
    def __init__(self, fb, port, base):
        self.fb, self.port, self.base = fb, port, Path(base)
        self.url = f"http://127.0.0.1:{port}"
        self.token = None

    def auth(self, uid=None):
        uid = uid or int(os.environ["CHAT_ID"])
        r = self.post("/api/auth", {"initData": f"test-{uid}"}, auth=False)
        self.token = r["token"]
        return self.token

    def req(self, path, data=None, raw=None, auth=True, ctype="application/json", method=None):
        h = {}
        if auth and self.token:
            h["X-Token"] = self.token
        body = None
        if raw is not None:
            body, h["Content-Type"] = raw, ctype
        elif data is not None:
            body, h["Content-Type"] = json.dumps(data).encode(), "application/json"
        rq = urllib.request.Request(self.url + path, data=body, headers=h, method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(rq, timeout=120) as r:
                ct = r.headers.get("Content-Type", "")
                b = r.read()
                return json.loads(b) if "json" in ct else b
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return {"_status": e.code, **json.loads(b)}
            except ValueError:
                return {"_status": e.code, "_body": b[:200]}

    def get(self, path, **kw):
        return self.req(path, **kw)

    def post(self, path, data=None, **kw):
        return self.req(path, data=data if data is not None else {}, **kw)

    def drop(self, src, name=None):
        """Положить кадр, как будто его забрали с сервера-приёмника."""
        import shutil
        dst = self.fb.INCOMING / (name or Path(src).name)
        shutil.copy(src, str(dst) + ".tmp")
        os.replace(str(dst) + ".tmp", dst)
        return dst

    def add_user(self, uid, lang="ru", name=None):
        """Пользователь, как будто пришёл по приглашению."""
        self.fb.run("INSERT OR REPLACE INTO users(id, role, name, lang, default_film, created, tg) VALUES (?, 'user', ?, ?, 'auto', ?, ?)",
                    (uid, name or f"user{uid}", lang, time.time(), None if uid >= self.fb.WEB_BASE else uid))
        self.fb.load_users()

    def as_user(self, uid):
        """Другой клиент «Проявки» с тем же стендом."""
        other = Harness(self.fb, self.port, self.base)
        other.auth(uid)
        return other

    def wait(self, cond, timeout=60, step=0.2):
        t = time.time()
        while time.time() - t < timeout:
            v = cond()
            if v:
                return v
            time.sleep(step)
        raise TimeoutError("не дождался")

    def photos(self, sql="SELECT * FROM photos ORDER BY id", args=()):
        return self.fb.q(sql, args)

    def calls(self, method=None, since=0):
        return [c for c in CALLS if (method is None or c[1] == method) and c[0] >= since]


if __name__ == "__main__":
    h = start()
    print("стенд:", h.url, "данные:", h.base)
    td = Path(__file__).resolve().parent / "testdata"
    for f in sorted(td.glob("*.JPG")):
        h.drop(f)
    while True:
        time.sleep(3600)
