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
    from proyavka import config, database, jobs, pools      # после start: настройки читаются из окружения при импорте
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
    pools.FAST.submit(jobs.job_chat, ph, str(chat)).result()
    cw, ch = Image.open(chat).size
    check(f"версия для чата {cw}×{ch} (кроп ~{H0}px из рабочей копии)", abs(cw - ch) <= 2 and cw >= H0 - 2)
    full = config.TMP / "t_full.jpg"
    pools.HEAVY.submit(jobs.job_full, ph, str(full)).result()
    ow, oh = Image.open(ph["src"]).size
    fw, fh = Image.open(full).size
    check(f"полный файл {fw}×{fh} из оригинала {ow}×{oh}", abs(fw - fh) <= 2 and fw >= min(ow, oh) - 4)

    # сильный кроп: 30% кадра — для чата берётся оригинал, не мыло из рабочей копии
    small = [0.35, 0.35, 0.3, 0.3]
    h.post(f"/api/photo/{pid}", {"crop": small})
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    ph = database.get(pid)
    pools.FAST.submit(jobs.job_chat, ph, str(chat)).result()
    cw, ch = Image.open(chat).size
    from_work = round(0.3 * W0)
    check(f"сильный кроп: в чат {cw}×{ch} вместо {from_work}px из рабочей копии", max(cw, ch) > from_work * 1.5)
    t1 = time.time()
    pools.FAST.submit(jobs.job_chat, ph, str(chat)).result()
    print(f"     (время версии для чата при сильном кропе: {time.time() - t1:.2f} с)")

    # сброс
    r = h.post(f"/api/photo/{pid}", {"crop": None})
    check(f"сброс кадрирования: {r.get('crop')}", r.get("crop") is None)
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    vw, vh = Image.open(database.get(pid)["view"]).size
    check(f"после сброса снова весь кадр: {vw}×{vh}", abs(vw / vh - W0 / H0) < 0.01)
    r = h.post(f"/api/photo/{pid}", {"crop": [0, 0, 1, 1]})
    check("рамка во весь кадр = без кадрирования", r.get("crop") is None and not r.get("pending"))

    # поворот на 90° и наклон: хранятся в той же рамке, x,y,w,h — в долях уже повёрнутого кадра
    import numpy as np
    from proyavka import imaging
    for bad in ([0, 0, 1, 1, 45, 0], [0, 0, 1, 1, 90, 46], [0, 0, 1, 1, 90], [0, 0, 1, 1, 90, float("inf")]):
        r = h.post(f"/api/photo/{pid}", {"crop": bad})
        check(f"неверный поворот отклонён {bad}: {r.get('_status')}", r.get("_status") == 400)
    check("старая рамка без поворота — прежняя строка", imaging.parse_crop([0.1, 0.1, 0.5, 0.5, 0, 0]) == "0.1000,0.1000,0.5000,0.5000")
    check("−90 = 270", imaging.parse_crop([0, 0, 1, 1, -90, 0]) == "0.0000,0.0000,1.0000,1.0000,270,0.0")
    r = h.post(f"/api/photo/{pid}", {"crop": [0, 0, 1, 1, 90, 0]})
    check(f"весь кадр, повёрнутый на 90°: {r.get('crop')}", r.get("crop") == [0, 0, 1, 1, 90, 0] and r.get("pending"))
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    vw, vh = Image.open(database.get(pid)["view"]).size
    check(f"экран повёрнут: {vw}×{vh} (исходник {W0}×{H0})", abs(vw / vh - H0 / W0) < 0.01)
    p0 = h.get(f"/img/preview/{pid}/original?st=100&m={MEDIA}&c=0,0,1,1,90,0")
    pw, ph_ = Image.open(__import__("io").BytesIO(p0)).size
    check(f"превью по ссылке тоже повёрнуто: {pw}×{ph_}", (pw < ph_) == (W0 > H0))
    src = Image.open(ph["work"]).convert("RGB")
    cw_ = imaging.crop_img(src, "0.0000,0.0000,1.0000,1.0000,90,0.0")
    check("90° по часовой: левый верхний угол уходит вправо вверх",
          np.abs(np.asarray(cw_.crop((cw_.width - 4, 0, cw_.width, 4)), dtype=float).mean()
                 - np.asarray(src.crop((0, src.height - 4, 4, src.height)), dtype=float).mean()) < 30)
    tilt = imaging.crop_img(Image.new("RGB", (900, 600), (200, 200, 200)), "0.0000,0.0000,1.0000,1.0000,0,7.5")
    a = np.asarray(tilt, dtype=float)
    k = imaging.tilt_scale(900, 600, 7.5)
    check(f"наклон 7,5°: {tilt.size}, без пустых углов (минимум {a.min():.0f})",
          abs(tilt.size[0] - 900 * k) <= 2 and abs(tilt.size[0] / tilt.size[1] - 1.5) < 0.01 and a.min() > 150)
    r = h.post(f"/api/photo/{pid}", {"crop": [0.1, 0.2, 0.5, 0.5, 270, -3.25]})
    check(f"рамка с поворотом и наклоном: {r.get('crop')}", r.get("crop") == [0.1, 0.2, 0.5, 0.5, 270, -3.2])
    h.wait(lambda: (lambda p: p["rendered_rev"] == p["rev"])(database.get(pid)), timeout=60)
    ph = database.get(pid)
    pools.HEAVY.submit(jobs.job_full, ph, str(full)).result()
    fw, fh = Image.open(full).size
    check(f"полный файл с поворотом и наклоном: {fw}×{fh}", abs(fw / fh - (H0 / W0)) < 0.02)
    r = h.post(f"/api/photo/{pid}", {"crop": None})
    check("сброс снимает и поворот", r.get("crop") is None)
    harness.done()
