"""Кадрирование: применяется до плёнки везде — чат, «Проявка», превью, полный файл; сильный кроп берёт оригинал."""
import time
from pathlib import Path
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    h = harness.start(port=8105)
    from proyavka import config, database      # после start: настройки читаются из окружения при импорте
    h.auth()
    MEDIA = h.post("/api/auth", {"initData": "test-1"}, auth=False)["media"]      # токен для адресов картинок
    h.drop(TD / "a6300_DSC00270.JPG")
    ph = h.wait(lambda: [p for p in h.photos() if p["msg_id"] and p["view"]], timeout=90)[0]
    pid = ph["id"]
    fb = h.fb
    W0, H0 = Image.open(ph["work"]).size
    print("рабочая копия", W0, H0)

    for bad in ([0, 0, 0.01, 0.01], ["a", 1, 2, 3], [0, 0, float("nan"), 1], 5):
        r = h.post(f"/api/photo/{pid}", {"crop": bad})
        check(f"мусор отклонён {bad}: {r.get('_status')}", r.get("_status") == 400)

    # квадрат по центру
    sq = [0.25, 0.0, (H0 / W0) * 1.0 * 0.5 / 0.5 * 0.5, 1.0]
    sq = [round((1 - H0 / W0) / 2, 4), 0.0, round(H0 / W0, 4), 1.0]
    t0 = time.time()
    r = h.post(f"/api/photo/{pid}", {"crop": sq})
    check(f"кадрирование принято: {r.get('crop')}", r.get("crop") == sq and r.get("pending"))
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    ph = database.get(pid)
    vw, vh = Image.open(ph["view"]).size
    check(f"«Проявка» квадратная: {vw}×{vh}", abs(vw - vh) <= 2)
    tw, th = Image.open(ph["thumb"]).size
    check(f"миниатюра квадратная: {tw}×{th}", abs(tw - th) <= 2)
    h.wait(lambda: h.calls("editMessageMedia", t0), timeout=20)
    check("в чате фото обновлено", True)

    # превью плёнок по ссылке с рамкой
    pv = h.get(f"/img/preview/{pid}/golden200?st=100&c={','.join(map(str, sq))}&m={MEDIA}")
    pw, phh = Image.open(__import__("io").BytesIO(pv)).size
    check(f"превью плёнки кадрировано: {pw}×{phh}", abs(pw - phh) <= 2)
    src = h.get(f"/img/source/{pid}?m={MEDIA}")
    sw, sh = Image.open(__import__("io").BytesIO(src)).size
    check(f"исходник для кадрирования некадрирован: {sw}×{sh}", abs(sw / sh - W0 / H0) < 0.01)

    # версия для чата и полный файл
    chat = config.TMP / "t_chat.jpg"
    fb.FAST.submit(fb.job_chat, ph, str(chat)).result()
    cw, ch = Image.open(chat).size
    check(f"версия для чата {cw}×{ch} (кроп ~{H0}px из рабочей копии)", abs(cw - ch) <= 2 and cw >= H0 - 2)
    full = config.TMP / "t_full.jpg"
    fb.HEAVY.submit(fb.job_full, ph, str(full)).result()
    ow, oh = Image.open(ph["src"]).size
    fw, fh = Image.open(full).size
    check(f"полный файл {fw}×{fh} из оригинала {ow}×{oh}", abs(fw - fh) <= 2 and fw >= min(ow, oh) - 4)

    # сильный кроп: 30% кадра — для чата берётся оригинал, не мыло из рабочей копии
    small = [0.35, 0.35, 0.3, 0.3]
    h.post(f"/api/photo/{pid}", {"crop": small})
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    ph = database.get(pid)
    fb.FAST.submit(fb.job_chat, ph, str(chat)).result()
    cw, ch = Image.open(chat).size
    from_work = round(0.3 * W0)
    check(f"сильный кроп: в чат {cw}×{ch} вместо {from_work}px из рабочей копии", max(cw, ch) > from_work * 1.5)
    t1 = time.time()
    fb.FAST.submit(fb.job_chat, ph, str(chat)).result()
    print(f"     (время версии для чата при сильном кропе: {time.time() - t1:.2f} с)")

    # сброс
    r = h.post(f"/api/photo/{pid}", {"crop": None})
    check(f"сброс кадрирования: {r.get('crop')}", r.get("crop") is None)
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    vw, vh = Image.open(database.get(pid)["view"]).size
    check(f"после сброса снова весь кадр: {vw}×{vh}", abs(vw / vh - W0 / H0) < 0.01)
    r = h.post(f"/api/photo/{pid}", {"crop": [0, 0, 1, 1]})
    check("рамка во весь кадр = без кадрирования", r.get("crop") is None and not r.get("pending"))
    harness.done()
