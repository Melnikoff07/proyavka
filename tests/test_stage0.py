"""Этап 0: кадр с камеры проходит весь путь, рисуется один раз, уходит в чат; правка обновляет сообщение."""
import time
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"

if __name__ == "__main__":
    h = harness.start(port=8101)
    t0 = time.time()
    h.drop(TD / "a6300_DSC00269.JPG")
    ph = h.wait(lambda: [p for p in h.photos() if p["msg_id"]], timeout=90)[0]
    print(f"кадр #{ph['id']} в чате за {time.time() - t0:.1f} с; плёнка {ph['preset']}, view={bool(ph['view'])}")
    sends = h.calls("sendPhoto")
    print("sendPhoto:", len(sends), "| рендеров для чата отдельно не было:", not any(f.name.startswith("chat_") for f in h.fb.TMP.iterdir()))
    # правка из мини-приложения
    h.auth()
    t1 = time.time()
    r = h.post(f"/api/photo/{ph['id']}", {"preset": "vivid50"})
    print("ответ правки:", r.get("preset"), "pending:", r.get("pending"))
    h.wait(lambda: h.calls("editMessageMedia", since=t1), timeout=60)
    print(f"сообщение в чате обновлено за {time.time() - t1:.1f} с")
    p2 = h.fb.get(ph["id"])
    print("rev/rendered:", p2["rev"], p2["rendered_rev"])
    # повтор кадра
    h.drop(TD / "a6300_DSC00269.JPG", name="again.JPG")
    time.sleep(3)
    print("повтор не добавился:", len(h.photos()) == 1)
    lst = h.get("/api/photos?offset=0&limit=10")
    print("API ленты:", lst["total"], "кадров")
    print("OK")
    harness.done()
