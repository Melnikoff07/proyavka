"""Этап 3: пакетная правка — очередь, темп правок в чате, одно итоговое сообщение, 429."""
import time
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def chat_writes(h, since):
    return [c for c in h.calls(since=since) if c[1] in ("editMessageMedia", "sendPhoto", "sendMessage", "deleteMessages")]


if __name__ == "__main__":
    h = harness.start(port=8104)
    h.auth()
    for f in sorted(TD.glob("*.JPG")):
        h.drop(f)
    h.wait(lambda: (lambda r: len(r) == 3 and all(p["msg_id"] and p["view"] for p in r))(h.photos()), timeout=120)
    ids = [p["id"] for p in h.photos()]
    time.sleep(1.5)

    r = h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"preset": "bogus"}})
    check(f"неизвестная плёнка отклонена: {r.get('_status')}", r.get("_status") == 400)
    r = h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"strength": 33}})
    check(f"неверная сила отклонена: {r.get('_status')}", r.get("_status") == 400)

    t0 = time.time()
    r = h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"preset": "golden200", "strength": 75}})
    check(f"пакет принят: {r}", r.get("queued") == 3)
    rows = h.photos()
    check("в базе сразу новая плёнка", all(p["preset"] == "golden200" and p["strength"] == 75 for p in rows))
    j = h.get("/api/photos?offset=0&limit=10")
    check("в ленте кадры помечены «проявляется»", all(p["pending"] for p in j["photos"]))
    h.wait(lambda: [c for c in h.calls("sendMessage", t0) if "Готово" in str(c[2].get("text"))], timeout=60)
    summ = [c for c in h.calls("sendMessage", t0) if "Готово" in str(c[2].get("text"))]
    check(f"одно итоговое сообщение: {summ[0][2]['text']!r}", len(summ) == 1 and "3 кадра" in summ[0][2]["text"])
    h.wait(lambda: len(h.calls("editMessageMedia", t0)) >= 3, timeout=30)
    w = chat_writes(h, t0)
    gaps = [round(b[0] - a[0], 2) for a, b in zip(w, w[1:])]
    check(f"правки в чате не чаще раза в секунду: {[c[1] for c in w]} {gaps}", all(g >= 0.95 for g in gaps))
    check("все кадры проявлены", all(p["rendered_rev"] == p["rev"] for p in h.photos()))

    # повтор того же — рисовать нечего
    t1 = time.time()
    r = h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"preset": "golden200"}})
    check(f"повтор ничего не перерисовывает: {r}", r.get("queued") == 0 and r.get("same") == 3)
    time.sleep(2)
    check("и итог не шлёт", not [c for c in h.calls("sendMessage", t1) if "Готово" in str(c[2].get("text"))])

    # авто: каждому своё
    r = h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"preset": "auto", "leak": "edge"}})
    rows = h.photos()
    check(f"авто — у каждого свой: {[(p['preset'], p['auto_key'], p['leak']) for p in rows]}",
          all(p["preset"] == p["auto_key"] and p["leak"] == 1 for p in rows))
    h.wait(lambda: all(p["rendered_rev"] == p["rev"] for p in h.photos()), timeout=60)

    # одиночная правка во время большого пакета идёт вперёд
    import io
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(1)
    for i in range(6):
        buf = io.BytesIO()
        Image.fromarray(rng.integers(0, 255, (1000, 1500, 3), dtype=np.uint8)).save(buf, "JPEG", quality=80)
        h.req(f"/api/upload?name=n{i}.jpg", raw=buf.getvalue(), ctype="image/jpeg")
    h.wait(lambda: (lambda r: len(r) == 9 and all(p["msg_id"] and p["rendered_rev"] == p["rev"] for p in r))(h.photos()), timeout=120)
    time.sleep(1.5)
    big = [p["id"] for p in h.photos()]
    t2 = time.time()
    h.post("/api/batch", {"action": "edit", "ids": big, "changes": {"preset": "across100"}})
    h.post(f"/api/photo/{big[-1]}", {"preset": "cine250"})
    h.wait(lambda: len(h.calls("editMessageMedia", t2)) >= 9, timeout=90)
    order = [c[2]["message_id"] for c in h.calls("editMessageMedia", t2)]
    pos = order.index(h.fb.get(big[-1])["msg_id"])
    check(f"одиночная правка ушла в чат среди первых: место {pos + 1} из {len(order)}", pos <= 2)
    h.wait(lambda: [c for c in h.calls("sendMessage", t2) if "Готово" in str(c[2].get("text"))], timeout=60)
    last_edit = max(c[0] for c in h.calls("editMessageMedia", t2))
    summ = [c for c in h.calls("sendMessage", t2) if "Готово" in str(c[2].get("text"))][0]
    check(f"итог пришёл после всех правок в чате: {summ[2]['text']!r}", summ[0] > last_edit)
    ids = big[:3]

    # удаление посреди пакета не вешает итог
    t3 = time.time()
    h.post("/api/batch", {"action": "edit", "ids": ids, "changes": {"preset": "super400"}})
    h.post("/api/batch", {"action": "delete", "ids": [ids[0]]})
    h.wait(lambda: [c for c in h.calls("sendMessage", t3) if "Готово" in str(c[2].get("text"))], timeout=60)
    summ = [c for c in h.calls("sendMessage", t3) if "Готово" in str(c[2].get("text"))]
    check(f"итог после удаления в пакете: {summ[0][2]['text']!r}", "2 кадра" in summ[0][2]["text"])
    check("учёт пакетов пуст", not h.fb.BATCHES and not h.fb.BATCH_OF)

    # 429: Telegram просит подождать — повтор с тем же файлом
    import io
    fb = h.fb
    calls = []

    class R:
        def __init__(self, code, j):
            self.status_code, self._j = code, j

        def json(self):
            return self._j

    def fake_post(url, data=None, files=None, timeout=None):
        f = files["f"][1]
        calls.append(f.read())
        if len(calls) == 1:
            return R(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 1}})
        return R(200, {"ok": True, "result": {"message_id": 1}})

    orig = fb.requests.post
    fb.requests.post = fake_post
    t4 = time.time()
    try:
        res = fb.real_tg("editMessageMedia", files={"f": ("p.jpg", io.BytesIO(b"JPEGDATA"))}, chat_id=1, message_id=5)
    finally:
        fb.requests.post = orig
    check(f"429: подождал {time.time() - t4:.1f} с и повторил с тем же файлом",
          res == {"message_id": 1} and calls == [b"JPEGDATA", b"JPEGDATA"] and time.time() - t4 >= 0.9)
    check("после 429 фоновые правки придержаны", fb._PACE.get(1, 0) > time.monotonic() - 0.2)
    harness.done()
