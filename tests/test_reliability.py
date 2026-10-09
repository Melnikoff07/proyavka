"""Надёжность основного пути: недорисованные после перезапуска кадры, сбой посреди приёма, EXIF и профиль в готовом файле,
перевод входного профиля в sRGB, оригиналы не уходят по возрасту, постраничные обновления ленты."""
import io
import time
from pathlib import Path
import numpy as np
from PIL import Image
import harness
import icc_synth


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def frame(seed, name, icc=None, color=None):
    p = Path(harness.tempfile.mkdtemp()) / name
    if color:
        im = Image.new("RGB", (900, 600), color)
        ys = np.random.default_rng(seed).integers(0, 3, (600, 900, 3), dtype=np.uint8)
        im = Image.fromarray(np.clip(np.asarray(im, dtype=np.int16) + ys, 0, 255).astype(np.uint8))
    else:
        im = Image.fromarray(np.random.default_rng(seed).integers(0, 255, (600, 900, 3), dtype=np.uint8))
    kw = {"icc_profile": icc} if icc else {}
    im.save(p, "JPEG", quality=95, **kw)
    return p


if __name__ == "__main__":
    h = harness.start(port=8123)
    from proyavka import database, ingest, storage, web
    h.auth(1)
    for i in range(2):
        h.drop(frame(i, f"R{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"] and p["rendered_rev"] == p["rev"]]) == 2, timeout=120)
    ids = [p["id"] for p in h.photos()]

    # --- 1. недорисованный после перезапуска кадр ставится в очередь ---
    database.run("UPDATE photos SET rev=rev+1, updated=? WHERE id=?", (time.time(), ids[0]))
    check("правка записана, но не нарисована", h.photos("SELECT rev, rendered_rev FROM photos WHERE id=?", (ids[0],))[0]["rev"]
          != h.photos("SELECT rev, rendered_rev FROM photos WHERE id=?", (ids[0],))[0]["rendered_rev"])
    web.backfill_views()
    ok = h.wait(lambda: (lambda r: r["rev"] == r["rendered_rev"])(h.photos("SELECT rev, rendered_rev FROM photos WHERE id=?", (ids[0],))[0]), timeout=60)
    check("после «перезапуска» кадр дорисовался сам", bool(ok))

    # --- 2. приём: сбой посреди — кадр не теряется ---
    f = frame(7, "RECOV.JPG")
    fp = ingest.fingerprint(f)
    pid = database.run("INSERT INTO photos(name, owner, hidden, taken, preset, strength, created, rev, rendered_rev, updated, fp) "
                       "VALUES ('RECOV.JPG', 1, 0, '2024-01-01 10:00', 'original', 100, ?, 1, 0, ?, ?)", (time.time(), time.time(), fp))
    dst = h.drop(f)                          # входящий файл остался, а строка в базе уже есть
    ingest.recover_ingest()
    check("строка без файлов убрана", not database.q("SELECT 1 FROM photos WHERE id=?", (pid,)))
    h.wait(lambda: not dst.exists(), timeout=60)
    check("входящий файл не потерян как «повтор»: кадр принят заново", bool(h.wait(lambda: database.q("SELECT 1 FROM photos WHERE name='RECOV.JPG'"), timeout=60)))

    # файлы уже на месте, а запись о них не дописана
    row = database.q("SELECT * FROM photos WHERE name='RECOV.JPG'")[0]
    work, src = row["work"], row["src"]
    database.run("UPDATE photos SET work=NULL, src=NULL, view=NULL, thumb=NULL, rev=1, rendered_rev=0 WHERE id=?", (row["id"],))
    ingest.recover_ingest()
    got = database.q("SELECT * FROM photos WHERE id=?", (row["id"],))
    check("файлы на месте — запись доведена", bool(got) and got[0]["work"] == work and got[0]["src"] == src)
    h.wait(lambda: database.q("SELECT view FROM photos WHERE id=?", (row["id"],))[0]["view"], timeout=60)

    # сбой при переносе: строка не остаётся, файл возвращается во входящие
    real_move = ingest.shutil.move
    def boom(a, b, *k, **kw):
        if "failed_" in str(b):
            return real_move(a, b, *k, **kw)           # уход в failed_ после сбоя должен работать
        raise OSError("диск отвалился")
    n0 = len(h.photos())
    ingest.shutil.move = boom
    g = h.drop(frame(8, "BOOM.JPG"))
    h.wait(lambda: not g.exists() or len(h.photos()) > n0, timeout=30)
    time.sleep(1.5)
    ingest.shutil.move = real_move
    check("сбой при переносе: лишней строки нет", not database.q("SELECT 1 FROM photos WHERE name='BOOM.JPG'"))

    # --- 3. EXIF и профиль в готовом файле ---
    r = h.get(f"/img/full/{ids[1]}")
    im = Image.open(io.BytesIO(r)) if isinstance(r, bytes) else None
    ex = im.getexif() if im else {}
    sub = ex.get_ifd(0x8769) if im else {}
    row1 = h.photos("SELECT taken FROM photos WHERE id=?", (ids[1],))[0]
    check(f"в готовом файле дата съёмки ({sub.get(0x9003)}) и профиль sRGB ({len(im.info.get('icc_profile', b'')) if im else 0} байт)",
          im is not None and str(sub.get(0x9003, ""))[:10].replace(":", "-") == str(row1["taken"])[:10] and len(im.info.get("icc_profile", b"")) > 100)
    check("повороты и GPS не переносятся", 0x0112 not in ex and not ex.get_ifd(0x8825))

    # --- 4. входной профиль -> sRGB ---
    p3 = h.drop(frame(9, "P3.JPG", icc=icc_synth.p3_profile(), color=(30, 200, 60)))
    plain = h.drop(frame(10, "PLAIN.JPG", color=(30, 200, 60)))
    h.wait(lambda: len(h.photos("SELECT 1 FROM photos WHERE name IN ('P3.JPG','PLAIN.JPG') AND work IS NOT NULL")) == 2, timeout=60)
    px = lambda nm: Image.open(h.photos("SELECT work FROM photos WHERE name=?", (nm,))[0]["work"]).convert("RGB").getpixel((450, 300))
    a, b = px("P3.JPG"), px("PLAIN.JPG")
    check(f"Display P3 переведён в sRGB: {a}, без профиля как есть: {b}", a[0] < 15 and b[0] > 20 and abs(b[1] - 200) < 12)

    # --- 5. оригиналы не удаляются по возрасту ---
    old = time.time() - 100 * 86400
    database.run("UPDATE photos SET created=? WHERE id=?", (old, ids[1]))
    storage.cleanup()
    check("оригинал старше двух недель остался (место есть)", bool(database.q("SELECT src FROM photos WHERE id=?", (ids[1],))[0]["src"]))
    storage.ORIG_DAYS = 30
    storage.cleanup()
    check("с ORIGINALS_DAYS=30 уходит по возрасту", database.q("SELECT src FROM photos WHERE id=?", (ids[1],))[0]["src"] is None)
    storage.ORIG_DAYS = 0

    # --- 6. обновления ленты порциями ---
    t0 = time.time()
    base = t0 + 5
    for i in range(1203):
        database.run("INSERT INTO photos(name, owner, hidden, taken, preset, strength, created, rev, rendered_rev, updated) "
                     "VALUES (?, 1, 0, '2024-01-01 10:00', 'original', 100, ?, 1, 1, ?)", (f"U{i}.jpg", t0, base + i * 0.001))
    seen, since, rounds = set(), t0, 0
    while rounds < 20:
        j = h.get(f"/api/updates?since={since!r}")
        seen |= {p["id"] for p in j["photos"] if database.q("SELECT 1 FROM photos WHERE id=? AND name LIKE 'U%'", (p["id"],))}
        since = j["now"]
        rounds += 1
        if not j.get("more"):
            break
    check(f"1203 изменения доехали все ({len(seen)}) за {rounds} запроса", len(seen) == 1203 and rounds >= 3)

    # --- мелочи: сессия, токен бота в ошибке ---
    fake = "x" * 40
    r = h.post("/api/auth", {"initData": "test-1", "token": fake}, auth=False)
    check("присланный клиентом неизвестный токен сессией не становится", r["token"] != fake)
    r2 = h.post("/api/auth", {"initData": "test-1", "token": r["token"]}, auth=False)
    check("известный токен продлевается тем же", r2["token"] == r["token"])
    from proyavka import devices
    secret = "123456:" + "A" * 35
    def boom_post(url, **kw):
        raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    real_post = devices.requests.post
    devices.requests.post = boom_post
    try:
        devices.connect_bot(secret)
        msg = ""
    except ValueError as e:
        msg = str(e)
    devices.requests.post = real_post
    check("токен бота не попадает в текст ошибки подключения", msg != "" and secret not in msg)
    harness.done()
