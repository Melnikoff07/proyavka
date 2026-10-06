"""Кэш состояний кадра: вернулся к плёнке, которую уже смотрел, — экран кадра берётся из кэша, а не рисуется заново,
и адрес картинки тот же, что в прошлый раз (браузер показывает её из своего кэша). Чат Telegram при этом обновляется."""
import time
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    h = harness.start(port=8120)
    from proyavka import database, scheduler      # после start: настройки читаются из окружения при импорте
    h.auth()
    h.drop(TD / "a6300_DSC00266.JPG")
    pid = h.wait(lambda: [p for p in h.photos() if p["msg_id"] and p["view"]], timeout=120)[0]["id"]
    done = lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid))
    h.wait(done, timeout=60)

    renders = []                                    # сколько раз экран кадра действительно рисовался (мимо кэша)
    orig_take = scheduler._cache_take
    scheduler._cache_take = lambda *a: orig_take(*a) or renders.append(1) or False

    def switch(film):
        r = h.post(f"/api/photo/{pid}", {"preset": film})
        h.wait(done, timeout=60)
        return r, h.get("/api/updates?since=0")["photos"]

    def v_of(photos):
        return next(p["v"] for p in photos if p["id"] == pid)

    r, ps = switch("super400")
    v_super = v_of(ps)
    check("первый раз super400 рисуется", r.get("pending") is True and len(renders) == 1)
    r, ps = switch("original")
    v_orig = v_of(ps)
    check("первый раз оригинал рисуется", r.get("pending") is True and len(renders) == 2)

    t = time.time()
    r = h.post(f"/api/photo/{pid}", {"preset": "super400"})
    dt = time.time() - t
    check(f"снова super400 — сразу готов, без отрисовки ({dt * 1000:.0f} мс)", r.get("pending") is False and len(renders) == 2)
    check("и адрес картинки тот же, что в первый раз (браузер берёт из кэша)", r.get("v") == v_super)
    r = h.post(f"/api/photo/{pid}", {"preset": "original"})
    check("снова оригинал — тоже из кэша, адрес прежний", r.get("pending") is False and r.get("v") == v_orig and len(renders) == 2)
    h.wait(done, timeout=30)

    # другая сила — другое состояние: рисуется
    r, _ = switch("super400")
    r = h.post(f"/api/photo/{pid}", {"strength": 50})
    check("другая сила — рисуется заново", r.get("pending") is True)
    h.wait(done, timeout=60)
    check("после отрисовки — счётчик вырос", len(renders) == 3)

    # чат Telegram получает новый вид и при попадании в кэш
    t0 = time.time()
    h.post(f"/api/photo/{pid}", {"strength": 100})
    h.wait(lambda: [c for c in h.calls("editMessageMedia", since=t0)], timeout=60)
    check("сообщение в чате обновилось и без отрисовки экрана", bool(h.calls("editMessageMedia", since=t0)) and len(renders) == 3)

    # кэш ограничен: не больше 8 состояний на кадр
    for s in (25, 75, 125, 150):
        for film in ("vivid50", "cine250"):
            h.post(f"/api/photo/{pid}", {"preset": film, "strength": s})
            h.wait(done, timeout=60)
    from proyavka.config import PREVIEWS
    n = len(list(PREVIEWS.glob(f"{pid}_vc_*.jpg")))
    check(f"в кэше не больше 8 состояний кадра: {n}", 0 < n <= scheduler.VIEW_CACHE_KEEP)

    harness.done()
