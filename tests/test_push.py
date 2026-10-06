"""Уведомления о новых кадрах (Web Push): ключ сервера, подписка, сборка кадров в одно уведомление,
тишина при открытом приложении, отписка протухших, отключение вместе с устройством."""
import json
import time
from pathlib import Path
import numpy as np
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"
SENT = []


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def frame(seed, name):
    p = Path(harness.tempfile.mkdtemp()) / name
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (600, 900, 3), dtype=np.uint8)).save(p, "JPEG")
    return p


if __name__ == "__main__":
    h = harness.start(port=8112)
    from proyavka import config, database      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    h.auth(1)
    check("pywebpush есть", fb.webpush is not None)
    fb.PUSH_DELAY = 2
    fb.PUSH_QUIET = 4
    gone = set()

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_push(sub, data, vapid_private_key=None, vapid_claims=None, ttl=0, timeout=None):
        if sub["endpoint"] in gone:
            raise fb.WebPushException("gone", response=Resp(410))
        SENT.append((time.time(), sub["endpoint"], json.loads(data), dict(vapid_claims)))
    fb.webpush = fake_push

    j = h.get("/api/push")
    check(f"ключ сервера: {len(j['key'])} символов", j["supported"] and len(j["key"]) == 87)
    check("ключ сохранён и тот же после перезагрузки", (config.BASE / "vapid.pem").exists()
          and (fb._VAPID.clear() or fb.push_key()) == j["key"])

    # устройство с подпиской
    link = fb.make_pair(1)
    r = h.post("/api/pair", {"code": link.split("#pair=")[1], "name": "iPhone · Safari (app)"}, auth=False)
    dev_tok = r["token"]
    hd = harness.Harness(fb, h.port, h.base)
    hd.token = dev_tok
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}}
    check("кривая подписка не принята", hd.post("/api/push/subscribe", {"endpoint": "http://x", "keys": {}}).get("_status") == 400)
    check("подписка принята", hd.post("/api/push/subscribe", sub).get("ok"))
    row = database.q("SELECT * FROM push_subs")
    check(f"подписка привязана к устройству #{row[0]['device'] if row else '?'}", len(row) == 1 and row[0]["device"] and row[0]["owner"] == 1)
    check("повторная подписка того же адреса не задваивается", hd.post("/api/push/subscribe", sub).get("ok") and len(database.q("SELECT * FROM push_subs")) == 1)

    # три кадра подряд, приложение закрыто -> одно уведомление
    fb.LAST_POLL.clear()
    t0 = time.time()
    for i in range(3):
        h.drop(frame(i, f"P{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 3, timeout=120)
    h.wait(lambda: [s for s in SENT if s[0] > t0], timeout=30)
    time.sleep(fb.PUSH_DELAY + 1)
    got = [s for s in SENT if s[0] > t0]
    check(f"одно уведомление на три кадра: «{got[0][2]['body'] if got else '?'}»", len(got) == 1 and got[0][2]["body"] == "Проявлено 3 новых кадра")
    check(f"подпись сервера: {got[0][3].get('sub')}", got and got[0][3]["sub"].startswith("mailto:"))

    # открыто другое окно (Telegram-сессия без устройства) — телефону всё равно приходит
    t1 = time.time()
    h.get("/api/updates?since=0")
    h.drop(frame(10, "Q0001.JPG"))
    h.wait(lambda: [p for p in h.photos() if p["name"] == "Q0001.JPG" and p["view"]], timeout=60)
    h.wait(lambda: [s for s in SENT if s[0] > t1], timeout=fb.PUSH_DELAY + 10)
    check("открыто на другом устройстве — этому уведомление приходит", True)

    # открыто на этом же устройстве -> тишина
    t1 = time.time()
    hd.get("/api/updates?since=0")
    h.drop(frame(11, "Q0002.JPG"))
    h.wait(lambda: [p for p in h.photos() if p["name"] == "Q0002.JPG" and p["view"]], timeout=60)
    time.sleep(fb.PUSH_DELAY + 1.5)
    check("приложение открыто на этом устройстве — уведомления нет", not [s for s in SENT if s[0] > t1])

    # правка кадра — не новый кадр
    time.sleep(fb.PUSH_QUIET)
    t2 = time.time()
    pid = h.photos()[0]["id"]
    fb.apply_changes(database.get(pid), {"strength": 50})
    h.wait(lambda: database.get(pid)["rendered_rev"] == database.get(pid)["rev"], timeout=60)
    time.sleep(fb.PUSH_DELAY + 1)
    check("смена плёнки не уведомляет", not [s for s in SENT if s[0] > t2])

    # один кадр, подписка протухла -> удалена
    gone.add(sub["endpoint"])
    h.drop(frame(20, "R0001.JPG"))
    h.wait(lambda: [p for p in h.photos() if p["name"] == "R0001.JPG" and p["view"]], timeout=60)
    h.wait(lambda: not database.q("SELECT 1 FROM push_subs"), timeout=fb.PUSH_DELAY + 10)
    check("протухшая подписка (410) удалена", True)

    # отписка и отключение устройства
    gone.clear()
    hd.post("/api/push/subscribe", sub)
    check("выключить — подписки нет", hd.post("/api/push/unsubscribe", {"endpoint": sub["endpoint"]}).get("ok")
          and not database.q("SELECT 1 FROM push_subs"))
    hd.post("/api/push/subscribe", sub)
    did = database.q("SELECT device FROM push_subs")[0]["device"]
    fb.drop_device(1, did)
    check("отключили устройство — его подписка ушла", not database.q("SELECT 1 FROM push_subs"))
    harness.done()
