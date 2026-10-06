"""Этап 1: свои фото через «+»: загрузка, повторы, мусор, лимит, PNG."""
import io
import time
from pathlib import Path
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    h = harness.start(port=8102, extra_env={"UPLOAD_MAX_MB": "20"})
    from proyavka import config, ingest, sessions      # после start: настройки читаются из окружения при импорте
    h.auth()
    data = (TD / "a6300_DSC00270.JPG").read_bytes()
    t0 = time.time()
    r = h.req("/api/upload?name=IMG_0001.JPG", raw=data, ctype="image/jpeg")
    check(f"загрузка принята: {r}", r.get("ok") and r.get("duplicate") is None)
    ph = h.wait(lambda: [p for p in h.photos() if p["msg_id"]], timeout=90)[0]
    check(f"кадр #{ph['id']} {ph['name']} в ленте и в чате за {time.time() - t0:.1f} с", ph["name"].startswith("IMG_0001"))
    r = h.req("/api/upload?name=copy.jpg", raw=data, ctype="image/jpeg")
    check(f"повтор распознан сразу: {r}", r.get("duplicate") == ph["id"])
    r = h.req("/api/upload?name=evil.jpg", raw=b"<?php echo 1; ?>" * 100, ctype="image/jpeg")
    check(f"не фото отклонено: {r}", r.get("_status") == 400)
    big = b"\xff\xd8\xff" + b"0" * (21 * 1024 * 1024)
    try:
        r = h.req("/api/upload?name=big.jpg", raw=big, ctype="image/jpeg")
    except OSError as e:          # сервер отвечает отказом сразу и рвёт соединение, не дочитывая 21 МБ
        r = {"_status": "разрыв", "error": type(e).__name__}
    check(f"больше лимита отклонено: {r.get('_status')} {r.get('error')}", r.get("_status") in (400, "разрыв"))
    check("после отказа кадр не появился", len(h.photos()) == 1)
    buf = io.BytesIO()
    Image.new("RGB", (1200, 800), (40, 120, 200)).save(buf, "PNG")
    r = h.req("/api/upload?name=../../etc/passwd", raw=buf.getvalue(), ctype="image/png")
    check(f"PNG с кривым именем принят: {r}", r.get("ok"))
    p2 = h.wait(lambda: [p for p in h.photos() if p["id"] != ph["id"] and p["msg_id"]], timeout=60)[0]
    check(f"PNG стал кадром #{p2['id']} с безопасным именем {p2['name']}", "/" not in p2["name"] and ".." not in p2["name"].replace("....", ""))
    h.token = None
    r = h.req("/api/upload?name=x.jpg", raw=data, ctype="image/jpeg", auth=False)
    check(f"без входа нельзя: {r.get('_status')}", r.get("_status") == 401)
    # два одинаковых файла подряд, пока первый ещё не обработан
    h.auth()
    buf2 = io.BytesIO()
    Image.new("RGB", (3000, 2000), (200, 60, 30)).save(buf2, "JPEG", quality=95)
    r1 = h.req("/api/upload?name=a.jpg", raw=buf2.getvalue(), ctype="image/jpeg")
    r2 = h.req("/api/upload?name=b.jpg", raw=buf2.getvalue(), ctype="image/jpeg")
    check(f"копия, ждущая обработки, распознана: {r1.get('duplicate')} / {r2.get('duplicate')}",
          r1.get("duplicate") is None and r2.get("duplicate") == -1)
    h.wait(lambda: len(h.photos()) == 3, timeout=60)
    time.sleep(2)
    check(f"в ленте один кадр из двух копий (всего {len(h.photos())})", len(h.photos()) == 3)
    check("очередь отпечатков пуста", not ingest.PENDING_FP)
    old = h.auth()
    sessions.SESSIONS[old] = (time.time() - 1, 1)     # сессия истекла
    r = h.post("/api/auth", {"initData": "test-1", "token": old}, auth=False)
    check("повторный вход продлил тот же токен", r.get("token") == old)
    h.token = old
    r = h.get("/api/photos?offset=0&limit=5")
    check(f"после продления лента открывается: {r.get('total')} кадров", r.get("total") == 3)
    leftovers = [f.name for f in config.TMP.iterdir() if f.name.startswith("up_")]
    check(f"временных файлов не осталось: {leftovers}", not leftovers)
    harness.done()
