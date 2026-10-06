"""RAW (RAW_FILES=1): DNG через «+», одиночный RAW с камеры, пара RAW+JPEG -> JPEG, полный размер из RAW."""
import os
import shutil
import sys
import time
from pathlib import Path
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    on = sys.argv[1:] != ["off"]
    h = harness.start(port=8109, extra_env={"RAW_FILES": "1" if on else "0"})
    fb = h.fb
    h.auth(1)
    dng = (TD / "TEST0001.dng").read_bytes()
    if not on:
        r = h.req("/api/upload?name=TEST0001.dng", raw=dng, ctype="application/octet-stream")
        check(f"без RAW_FILES DNG отклонён: {r.get('error')}", r.get("_status") == 400)
        harness.done()

    fb.RAW_WAIT = 2
    r = h.req("/api/upload?name=TEST0001.dng", raw=dng, ctype="application/octet-stream")
    check(f"DNG через «+» принят: {r}", r.get("ok") and r.get("duplicate") is None)
    ph = h.wait(lambda: [p for p in h.photos() if p["name"].lower().endswith(".dng") and p["msg_id"]], timeout=60)[0]
    w, hh = Image.open(ph["work"]).size
    a = Image.open(ph["work"]).convert("RGB")
    left, right = a.getpixel((20, 20)), a.getpixel((w - 20, hh - 20))
    check(f"проявлен {w}×{hh}, слева красного меньше, чем справа: {left} → {right}", w == 2560 and left[0] < right[0])
    check(f"дата съёмки из EXIF RAW: {ph['taken']}", ph["taken"] == "2026-10-01 12:34")

    full = fb.TMP / "f.jpg"
    fb.HEAVY.submit(fb.job_full, fb.get(ph["id"]), str(full)).result()
    check(f"полный размер из RAW: {Image.open(full).size}", Image.open(full).size == (3000, 2000))
    fb.apply_changes(fb.get(ph["id"]), {"crop": [0.4, 0.4, 0.2, 0.2]})
    h.wait(lambda: fb.get(ph["id"])["rendered_rev"] == fb.get(ph["id"])["rev"], timeout=60)
    fb.FAST.submit(fb.job_chat, fb.get(ph["id"]), str(full)).result()
    check(f"сильный кроп берёт RAW: {Image.open(full).size}", Image.open(full).size == (600, 400))

    # пара RAW+JPEG с камеры: остаётся JPEG
    n0 = len(h.photos())
    shutil.copy(TD / "a6300_DSC00266.JPG", fb.INCOMING / "PAIR0001.JPG.tmp")
    shutil.copy(TD / "TEST0003.dng", fb.INCOMING / "PAIR0001.DNG.tmp")
    os.replace(fb.INCOMING / "PAIR0001.DNG.tmp", fb.INCOMING / "PAIR0001.DNG")
    os.replace(fb.INCOMING / "PAIR0001.JPG.tmp", fb.INCOMING / "PAIR0001.JPG")
    h.wait(lambda: not any(fb.INCOMING.iterdir()), timeout=60)
    time.sleep(1)
    names = [p["name"] for p in h.photos()][n0:]
    check(f"пара RAW+JPEG — один кадр из JPEG: {names}", names == ["PAIR0001.JPG"])

    # одиночный RAW с камеры ждёт JPEG, потом проявляется сам
    t = time.time()
    shutil.copy(TD / "TEST0002.dng", fb.INCOMING / "SOLO0002.tmp")
    os.replace(fb.INCOMING / "SOLO0002.tmp", fb.INCOMING / "SOLO0002.DNG")
    h.wait(lambda: [p for p in h.photos() if p["name"] == "SOLO0002.DNG"], timeout=60)
    check(f"одиночный RAW проявлен через {time.time() - t:.1f} с (ждал JPEG {fb.RAW_WAIT} с)", time.time() - t >= fb.RAW_WAIT)
    # JPEG опоздал после проявленного RAW — не дублируется
    shutil.copy(TD / "a6300_DSC00269.JPG", fb.INCOMING / "SOLO0002.tmp")
    os.replace(fb.INCOMING / "SOLO0002.tmp", fb.INCOMING / "SOLO0002.JPG")
    h.wait(lambda: not any(fb.INCOMING.iterdir()), timeout=30)
    time.sleep(1)
    check("опоздавший JPEG той же съёмки не задвоил кадр", len([p for p in h.photos() if p["name"].startswith("SOLO0002")]) == 1)
    harness.done()
