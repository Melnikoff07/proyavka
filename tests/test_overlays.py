"""Оверлеи: встроенные засветы с режимом наложения и непрозрачностью, свои картинки (загрузка, приватность, гость альбома,
удаление), кнопки в чате."""
import io
from pathlib import Path
import numpy as np
from PIL import Image
import harness

ADMIN, BOB = 1, 2002


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def frame(seed, name):
    p = Path(harness.tempfile.mkdtemp()) / name
    rng = np.random.default_rng(seed)
    Image.fromarray((rng.integers(60, 190, (600, 900, 3))).astype(np.uint8)).save(p, "JPEG")
    return p


def png(w=400, h=300, alpha=True):
    """Тёплое пятно слева на прозрачном фоне — как оверлей-засвет из набора."""
    a = np.zeros((h, w, 4), dtype=np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = 255, 120, 20
    a[:, : w // 3, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(a if alpha else a[..., :3]).save(buf, "PNG")
    return buf.getvalue()


def img(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)


def rendered(pid):
    from proyavka import database
    ph = database.get(pid)
    return ph["rendered_rev"] == ph["rev"]


if __name__ == "__main__":
    h = harness.start(port=8125)
    from proyavka import botui, database, i18n, imaging, telegram      # после start: настройки читаются из окружения при импорте
    from PIL import ImageChops
    h.add_user(BOB, "en", "Bob")
    h.auth(ADMIN)
    hb = h.as_user(BOB)
    for i in range(2):
        h.drop(frame(i, f"O{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 2, timeout=120)
    ids = sorted(p["id"] for p in h.photos())
    pid = ids[0]

    # движок: «экран» на 100 % — ровно прежний засвет; непрозрачность и режимы
    base = Image.fromarray(np.full((200, 300, 3), 100, dtype=np.uint8))
    old = imaging.light_leak(base, "edge", 7)
    new = imaging.apply_overlay(base, "edge", 7)
    check("экран 100 % — как прежний засвет", ImageChops.difference(old, new).getbbox() is None)
    half = np.asarray(imaging.apply_overlay(base, "edge", 7, "screen", 50), dtype=np.float32)
    full = np.asarray(new, dtype=np.float32)
    check(f"непрозрачность 50 % — посередине: {half.mean():.1f} между 100 и {full.mean():.1f}", 100 < half.mean() < full.mean())
    for m in imaging.BLENDS:
        out = imaging.apply_overlay(base, "corner", 3, m, 100)
        dark = np.asarray(out, dtype=np.float32).mean()
        check(f"режим {m}: кадр не залит чёрным фоном засвета ({dark:.0f})", out.size == base.size and dark > 60)

    # API: список, превью, правка
    pr = h.get("/api/presets")
    check(f"режимы наложения в /api/presets: {[b['key'] for b in pr['blends']]}", [b["key"] for b in pr["blends"]][:1] == ["screen"]
          and len(pr["blends"]) == len(imaging.BLENDS))
    check("встроенные засветы среди оверлеев", {"edge", "cold"} <= {x["key"] for x in pr["leaks"]})
    a = img(h.get(f"/img/preview/{pid}/original?st=100&lk=edge&ls=0"))
    b = img(h.get(f"/img/preview/{pid}/original?st=100&lk=edge&ls=0&lb=screen&lo=30"))
    c = img(h.get(f"/img/preview/{pid}/original?st=100"))
    check(f"превью с непрозрачностью 30 % слабее: {c.mean():.1f} < {b.mean():.1f} < {a.mean():.1f}", c.mean() < b.mean() < a.mean())
    check("неверный режим — 404", h.get(f"/img/preview/{pid}/original?st=100&lk=edge&lb=xor").get("_status") == 404)
    check("неверная непрозрачность — 404", h.get(f"/img/preview/{pid}/original?st=100&lk=edge&lo=33").get("_status") == 404)
    r = h.post(f"/api/photo/{pid}", {"leak": "edge", "ov_blend": "soft", "ov_opacity": 60})
    check(f"правка: режим и непрозрачность: {r.get('ov_blend')} {r.get('ov_opacity')}", r.get("ov_blend") == "soft" and r.get("ov_opacity") == 60)
    for bad in ({"ov_blend": "xor"}, {"ov_opacity": 0}, {"ov_opacity": 101}, {"ov_opacity": 33}, {"ov_opacity": True},
                {"ov_blend": ["screen"]}, {"leak": ["edge"]}, {"leak": "nope"}):
        check(f"отклонено: {bad}", h.post(f"/api/photo/{pid}", bad).get("_status") == 400)
    h.wait(lambda: rendered(pid), timeout=60)
    with i18n.speak(ADMIN):
        cap = telegram.caption(database.get(pid))
    check(f"подпись в чате: {cap.splitlines()[0]!r}", "оверлей" in cap and "Мягкий свет" in cap and "60%" in cap)
    with i18n.speak(ADMIN):
        kb = str(telegram.main_kb(database.get(pid)))
    check("кнопка в чате — «Оверлей»", "Оверлей" in kb and "Засвет" not in kb)

    # свои картинки
    for bad, why in ((b"hello", "мусор"), (png(8, 8), "крошечная")):
        r = h.req("/api/upload?overlay=1&name=x.png", raw=bad, ctype="application/octet-stream")
        check(f"{why} отклонена: {r.get('error')}", r.get("_status") == 400)
    r = h.req("/api/upload?overlay=1&name=Warm%20spot.png", raw=png(), ctype="image/png")
    check(f"свой оверлей загружен: {r}", r.get("key", "").startswith("ov") and r.get("name") == "Warm spot")
    key = r["key"]
    path = imaging.overlay_file(ADMIN, key)
    check(f"лежит PNG с прозрачностью: {path}", path is not None and path.suffix == ".png")
    r2 = h.req("/api/upload?overlay=1&name=flat.png", raw=png(alpha=False), ctype="image/png")
    check("без прозрачности хранится как JPEG", imaging.overlay_file(ADMIN, r2["key"]).suffix == ".jpg")
    check("в списке у хозяина", key in {x["key"] for x in h.get("/api/presets")["leaks"]})
    check("у Боба нет", key not in {x["key"] for x in hb.get("/api/presets")["leaks"]})
    pb = hb.req("/api/upload?name=b.jpg", raw=frame(9, "b.jpg").read_bytes(), ctype="image/jpeg")
    h.wait(lambda: len(h.photos()) == 3, timeout=60)
    bid = [p["id"] for p in h.photos() if p["owner"] == BOB][0]
    check("Боб не может поставить чужой оверлей", hb.post(f"/api/photo/{bid}", {"leak": key}).get("_status") == 400)
    check("и пачкой", hb.post("/api/batch", {"action": "edit", "ids": [bid], "changes": {"leak": key}}).get("_status") == 400)
    check("и превью не получить", hb.get(f"/img/preview/{bid}/original?st=100&lk={key}").get("_status") == 404)
    with i18n.speak(BOB):
        check("в кнопках чата у Боба его нет", key not in str(telegram.leak_kb(database.get(bid))))
    with i18n.speak(ADMIN):
        check("а у хозяина есть", key in str(telegram.leak_kb(database.get(pid))))

    plain = img(h.get(f"/img/preview/{pid}/original?st=100"))
    ov = img(h.get(f"/img/preview/{pid}/original?st=100&lk={key}"))
    w = plain.shape[1]
    left, right = (ov - plain)[:, : w // 4].mean(), np.abs(ov - plain)[:, w // 2:].mean()
    check(f"своя картинка легла: слева светлее на {left:.0f}, справа (прозрачно) разница {right:.1f}", left > 20 and right < 2)
    mul = img(h.get(f"/img/preview/{pid}/original?st=100&lk={key}&lb=multiply&lo=100"))
    check(f"умножение затемняет синий слева: {mul[:, : w // 4, 2].mean():.0f} < {plain[:, : w // 4, 2].mean():.0f}",
          mul[:, : w // 4, 2].mean() < plain[:, : w // 4, 2].mean() - 20)
    r = h.post(f"/api/photo/{pid}", {"leak": key, "ov_blend": "screen", "ov_opacity": 100})
    check("свой оверлей поставлен на кадр", r.get("leak") == key)
    h.wait(lambda: rendered(pid), timeout=60)
    r = h.post(f"/api/photo/{pid}", {"leak_shift": 1})
    check("«другой вариант» — отражение", r.get("leak") == key and r.get("leak_seed") == 1)
    h.wait(lambda: rendered(pid), timeout=60)
    r = h.post("/api/batch", {"action": "edit", "ids": [ids[1]], "changes": {"leak": key, "ov_blend": "overlay", "ov_opacity": 50}})
    check(f"пачкой: оверлей, режим, непрозрачность: {r}", r.get("queued") == 1)
    ph1 = database.get(ids[1])
    check("у второго кадра всё сохранилось", ph1["leak_kind"] == key and ph1["ov_blend"] == "overlay" and ph1["ov_opacity"] == 50)
    with i18n.speak(ADMIN):
        botui.on_callback({"id": "1", "data": f"l:{ids[1]}:edge", "message": {"message_id": 1}}, ADMIN)
    check("кнопка в чате ставит встроенный засвет", database.get(ids[1])["leak_kind"] == "edge")

    # гость альбома: встроенные можно, свои картинки хозяина — нет
    al = h.post("/api/albums", {"ids": [pid], "edit": True})
    pub = harness.Harness(h.fb, h.port, h.base)
    pub.token = pub.post("/api/auth", {"album": al["url"].rsplit("/", 1)[1]}, auth=False)["token"]
    check("гость не видит свои оверлеи хозяина", key not in {x["key"] for x in pub.get("/api/presets")["leaks"]})
    check("гость не ставит их", pub.post(f"/api/photo/{pid}", {"leak": r2["key"]}).get("_status") == 400)
    check("гость не грузит свои", pub.req("/api/upload?overlay=1&name=x.png", raw=png(), ctype="image/png").get("_status") in (401, 403))
    check("встроенный гостю можно", pub.post(f"/api/photo/{pid}", {"leak": "cold", "ov_opacity": 75}).get("leak") == "cold")
    h.wait(lambda: rendered(pid), timeout=60)
    h.post(f"/api/photo/{pid}", {"leak": key})
    h.wait(lambda: rendered(pid), timeout=60)

    # удаление
    check("Боб не может удалить чужой", hb.post(f"/api/overlay/{key}/delete", {}).get("_status") == 400 and path.exists())
    r = h.post(f"/api/overlay/{key}/delete", {})
    check(f"хозяин удалил: {r}", r.get("moved") == 1 and not path.exists())
    ph = database.get(pid)
    check("кадр остался без оверлея", not ph["leak"])
    h.wait(lambda: rendered(pid), timeout=60)
    check("и перерисован", True)
    check("из списка пропал", key not in {x["key"] for x in h.get("/api/presets")["leaks"]})
    harness.done()
