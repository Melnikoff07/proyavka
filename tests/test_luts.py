"""Свои LUT: загрузка .cube, применение, приватность между пользователями, удаление."""
import io
import time
from pathlib import Path
import numpy as np
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"
ADMIN, BOB = 1, 2002


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def cube(n=17, fn=lambda r, g, b: (b, g, r), title="Swap RB"):
    """LUT: красный и синий местами — заметно на любом кадре."""
    lines = [f'TITLE "{title}"', "# test", f"LUT_3D_SIZE {n}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    x = np.linspace(0, 1, n)
    for b in x:
        for g in x:
            for r in x:
                lines.append("%.6f %.6f %.6f" % fn(r, g, b))
    return ("\n".join(lines) + "\n").encode()


def mean_rgb(data):
    a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
    return a[..., 0].mean(), a[..., 2].mean()


if __name__ == "__main__":
    h = harness.start(port=8107)
    from proyavka import database, users      # после start: настройки читаются из окружения при импорте
    from proyavka import film, i18n      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    h.add_user(BOB, "en", "Bob")
    h.auth(ADMIN)
    hb = h.as_user(BOB)
    h.req("/api/upload?name=a.jpg", raw=(TD / "a6300_DSC00270.JPG").read_bytes(), ctype="image/jpeg")
    hb.req("/api/upload?name=b.jpg", raw=(TD / "a6300_DSC00266.JPG").read_bytes(), ctype="image/jpeg")
    h.wait(lambda: len([p for p in h.photos() if p["msg_id"] and p["rendered_rev"] == p["rev"]]) == 2, timeout=90)
    pa = [p for p in h.photos() if p["owner"] == ADMIN][0]
    pb = [p for p in h.photos() if p["owner"] == BOB][0]

    for bad, why in ((b"hello", "мусор"), (b"LUT_1D_SIZE 4\n0 0 0\n1 1 1\n", "1D"), (cube(5)[:-40], "обрезанный")):
        r = h.req("/api/upload?lut=1&name=x.cube", raw=bad, ctype="application/octet-stream")
        check(f"{why} отклонён: {r.get('error')}", r.get("_status") == 400)
    r = h.req("/api/upload?lut=1&name=My%20Swap.cube", raw=cube(), ctype="application/octet-stream")
    check(f"LUT загружен: {r}", r.get("key", "").startswith("lut") and r.get("name") == "My Swap")
    key = r["key"]
    path = film.lut_dir(ADMIN) / f"{key}.npy"
    check(f"лежит в папке администратора: {path.parent}", path.exists())

    mine = [x["key"] for x in h.get("/api/presets")["presets"]]
    bobs = [x["key"] for x in hb.get("/api/presets")["presets"]]
    check("в «Проявке» у администратора есть", key in mine)
    check("у Боба его нет", key not in bobs)
    check("Боб не может поставить чужой LUT", hb.post(f"/api/photo/{pb['id']}", {"preset": key}).get("_status") == 400)
    check("и пачкой тоже", hb.post("/api/batch", {"action": "edit", "ids": [pb["id"]], "changes": {"preset": key}}).get("_status") == 400)
    check("и превью чужого LUT не получить", hb.get(f"/img/preview/{pb['id']}/{key}?st=100").get("_status") == 404)
    check("в кнопках чата у Боба его нет", key not in str(fb.preset_kb(database.get(pb["id"]))))
    check("а у администратора есть", key in str(fb.preset_kb(database.get(pa["id"]))))
    with i18n.speak(BOB):
        check("и в плёнке по умолчанию у Боба нет", key not in str(fb.default_kb(BOB)))

    before = mean_rgb(h.get(f"/img/preview/{pa['id']}/original?st=100"))
    after = mean_rgb(h.get(f"/img/preview/{pa['id']}/{key}?st=100"))
    check(f"превью с LUT: R/B {before[0]:.0f}/{before[1]:.0f} → {after[0]:.0f}/{after[1]:.0f}",
          abs(after[0] - before[1]) < 3 and abs(after[1] - before[0]) < 3)
    half = mean_rgb(h.get(f"/img/preview/{pa['id']}/{key}?st=50"))
    check(f"сила 50% — посередине: R {half[0]:.0f}", min(before[0], after[0]) < half[0] < max(before[0], after[0]))

    t0 = time.time()
    r = h.post(f"/api/photo/{pa['id']}", {"preset": key})
    check(f"администратор ставит свой LUT: {r.get('preset_name')}", r.get("preset") == key and r.get("preset_name") == "My Swap")
    h.wait(lambda: database.get(pa["id"])["rendered_rev"] == database.get(pa["id"])["rev"], timeout=60)
    h.wait(lambda: h.calls("editMessageMedia", t0), timeout=20)
    with i18n.speak(ADMIN):
        cap = fb.caption(database.get(pa["id"]))
    check(f"в чате подпись с названием LUT: {cap.splitlines()[0]!r}", "My Swap" in cap)
    fb.apply_changes(database.get(pa["id"]), {"frame": True})
    h.wait(lambda: database.get(pa["id"])["rendered_rev"] == database.get(pa["id"])["rev"], timeout=60)
    check("рамка с LUT рисуется", True)

    # своя плёнка по умолчанию для новых кадров
    with i18n.speak(ADMIN):
        fb.on_callback({"id": "1", "data": f"d:{key}", "message": {"message_id": 1}}, ADMIN)
    check("LUT можно сделать плёнкой по умолчанию", users.user(ADMIN)["default_film"] == key)
    with i18n.speak(BOB):
        fb.on_callback({"id": "1", "data": f"d:{key}", "message": {"message_id": 1}}, BOB)
    check("а Боб чужой — нет", users.user(BOB)["default_film"] != key)

    # удаление
    check("Боб не может удалить чужой LUT", hb.post(f"/api/lut/{key}/delete", {}).get("_status") == 400 and path.exists())
    r = h.post(f"/api/lut/{key}/delete", {})
    check(f"администратор удалил: {r}", r.get("moved") == 1 and not path.exists())
    ph = database.get(pa["id"])
    check(f"кадр перешёл на автоплёнку: {ph['preset']}", ph["preset"] == ph["auto_key"])
    check("плёнка по умолчанию вернулась на авто", users.user(ADMIN)["default_film"] == "auto")
    h.wait(lambda: database.get(pa["id"])["rendered_rev"] == database.get(pa["id"])["rev"], timeout=60)
    check("и перерисован", True)
    for i in range(film.LUT_MAX_COUNT):
        fb.add_lut(BOB, f"l{i}.cube", cube(3))
    r = hb.req("/api/upload?lut=1&name=x.cube", raw=cube(3), ctype="application/octet-stream")
    check(f"лимит {film.LUT_MAX_COUNT} LUT: {r.get('error')}", r.get("_status") == 400)
    harness.done()
