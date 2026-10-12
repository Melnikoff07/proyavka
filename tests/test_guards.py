"""Защиты для чужих людей на сервере: «бомба» в картинке, суточный лимит, лимит места сразу, экранирование названий."""
import io
import os
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


def jpeg(seed, size=(1200, 800)):
    buf = io.BytesIO()
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


if __name__ == "__main__":
    h = harness.start(port=8108, extra_env={"DAILY_UPLOAD_LIMIT": "3", "MAX_MEGAPIXELS": "50"})
    from proyavka import i18n, storage, users      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    h.add_user(BOB, "ru", "Bob")
    h.auth(ADMIN)
    hb = h.as_user(BOB)

    # «бомба»: PNG 20000×20000 одного цвета весит килобайты, а в памяти — 1,2 ГБ
    buf = io.BytesIO()
    Image.new("L", (20000, 20000)).save(buf, "PNG", optimize=True)
    print(f"     бомба: {len(buf.getvalue()) // 1024} КБ на диске, {20000 * 20000 * 3 / 1e9:.1f} ГБ в памяти")
    t = time.time()
    r = hb.req("/api/upload?name=bomb.png", raw=buf.getvalue(), ctype="image/png")
    check(f"«бомба» отклонена за {time.time() - t:.1f} с: {r.get('error')}", r.get("_status") == 400)
    check("в папке приёма её нет", not any(users.udir(BOB, "incoming").iterdir()))
    # та же бомба через «камеру» (FTP): кладём прямо в папку приёма
    bomb = users.udir(BOB, "incoming") / "bomb2.png"
    bomb.write_bytes(buf.getvalue())
    h.wait(lambda: not bomb.exists(), timeout=60)
    check("через камеру — не обработана, бот жив", not h.photos() and any("Не смог" in str(c[2].get("text")) for c in h.calls("sendMessage")))

    # суточный лимит у приглашённого
    oks = [hb.req(f"/api/upload?name=b{i}.jpg", raw=jpeg(i), ctype="image/jpeg").get("ok") for i in range(3)]
    h.wait(lambda: len([p for p in h.photos() if p["owner"] == BOB]) == 3, timeout=60)
    r = hb.req("/api/upload?name=b4.jpg", raw=jpeg(9), ctype="image/jpeg")
    check(f"3 кадра приняты, 4-й — отказ: {r.get('error')}", all(oks) and r.get("_status") == 400)
    (users.udir(BOB, "incoming") / "cam.jpg").write_bytes(jpeg(10))
    h.wait(lambda: not (users.udir(BOB, "incoming") / "cam.jpg").exists(), timeout=30)
    time.sleep(1)
    check("и с камеры тоже не принят", len([p for p in h.photos() if p["owner"] == BOB]) == 3)
    check("ему сказали один раз", len([c for c in h.calls("sendMessage") if c[2].get("chat_id") == BOB and "предел" in str(c[2].get("text"))]) == 1)
    for i in range(4):
        h.req(f"/api/upload?name=a{i}.jpg", raw=jpeg(20 + i), ctype="image/jpeg")
    h.wait(lambda: len([p for p in h.photos() if p["owner"] == ADMIN]) == 4, timeout=60)
    check("у администратора суточного лимита нет", True)

    # лимит места — сразу после приёма
    h.wait(lambda: all(p["rendered_rev"] == p["rev"] for p in h.photos()), timeout=60)
    used = sum(storage.user_usage(ADMIN).values())
    users.set_user(ADMIN, storage_gb=used * 0.6 / 1e9)
    h.req("/api/upload?name=a9.jpg", raw=jpeg(99), ctype="image/jpeg")
    h.wait(lambda: len([p for p in h.photos() if p["owner"] == ADMIN]) == 5, timeout=60)
    time.sleep(1)
    after = sum(storage.user_usage(ADMIN).values())
    check(f"лимит места соблюдён сразу: {after / 1e6:.1f} МБ из {users.storage_limit(ADMIN) * 1e3:.1f}", after <= users.storage_limit(ADMIN) * 1e9)
    newest = max(h.photos(), key=lambda p: p["id"])
    check("новый кадр цел, срезаны старые оригиналы", newest["work"] and os.path.exists(newest["work"]))
    with i18n.speak(ADMIN):
        txt = storage.storage_text(ADMIN)
    check(f"/storage в мегабайтах: {txt.splitlines()[-2]!r}", "МБ" in txt.splitlines()[-2])

    # название LUT с HTML — только текст
    cube = ("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n").encode()
    r = h.req("/api/upload?lut=1&name=" + "%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E.cube", raw=cube, ctype="application/octet-stream")
    page = h.get("/").decode() if isinstance(h.get("/"), bytes) else ""
    check(f"LUT с HTML в названии принят как текст: {r.get('name')!r}", r.get("key"))
    check("в «Проявке» названия экранируются", "${esc(it.name)}" in page and "${it.name}</div>" not in page)
    # токен бота не попадает в тексты ошибок; пустой токен не даёт подделать вход из Telegram
    from proyavka import config, sessions, telegram
    config.BOT_TOKEN = "123456:SECRET-TOKEN-ABCDEFGHIJKLMNOPQRSTUV"
    check("scrub вырезает токен", "SECRET-TOKEN" not in telegram.scrub(f"Max retries exceeded with url: /bot{config.BOT_TOKEN}/sendPhoto"))
    config.BOT_TOKEN = ""
    check("без токена бота вход из Telegram закрыт", sessions.check_init_data("auth_date=1&hash=abc") is False)
    r = h.get("/api/photos?limit=-1&offset=-3")
    check("limit/offset вне границ не ломают ленту", isinstance(r, dict) and len(r.get("photos", [])) <= 120)

    # нагрузка: новые отрисовки — из ведра пользователя (готовые из кэша — бесплатно), архив по одному, кэш превью с пределом
    from proyavka import looks, web
    pid = [p for p in h.photos() if p["owner"] == BOB and not p["hidden"]][0]["id"]
    looks.RENDER_BURST, looks.RENDER_PER_MIN = 3, 1
    looks.RENDER_BUCKET.clear()
    got = [hb.req(f"/img/preview/{pid}/super400?st=100&lk=edge&ls={i}") for i in range(5)]
    st = [g.get("_status") if isinstance(g, dict) else 200 for g in got]
    check(f"перебор вариантов засвета упирается в предел: {st}", st[:3] == [200] * 3 and st[3:] == [429, 429])
    again = hb.req(f"/img/preview/{pid}/super400?st=100&lk=edge&ls=0")
    check("уже нарисованное отдаётся и после предела", isinstance(again, bytes))
    r = hb.post("/api/look/try", {"id": pid, "params": {}})
    check(f"живой просмотр тоже из ведра: {r.get('_status') if isinstance(r, dict) else 200}", isinstance(r, dict) and r.get("_status") == 429)
    check("засвет с огромным номером — отказ", hb.req(f"/img/preview/{pid}/super400?st=100&lk=edge&ls=99999999").get("_status") == 404)
    looks.RENDER_BURST, looks.RENDER_PER_MIN = 90, 180
    web.USER_ZIPS.add(BOB)
    check("второй архив, пока собирается первый, — 429", hb.req(f"/api/zip?ids={pid}").get("_status") == 429)
    web.USER_ZIPS.discard(BOB)
    check("а после — собирается", isinstance(hb.req(f"/api/zip?ids={pid}"), bytes))
    web.UPLOADS[ADMIN] = web.UPLOADS_AT_ONCE
    r = h.req("/api/upload?name=x.jpg", raw=jpeg(42), ctype="image/jpeg")
    check(f"больше {web.UPLOADS_AT_ONCE} загрузок разом — 429", r.get("_status") == 429)
    web.UPLOADS[ADMIN] = 0
    from proyavka.config import PREVIEWS
    for i in range(6):
        (PREVIEWS / f"999999_junk_{i}.jpg").write_bytes(b"x" * 100_000)
        os.utime(PREVIEWS / f"999999_junk_{i}.jpg", (1000 + i, 1000 + i))
    total = sum(f.stat().st_size for f in PREVIEWS.iterdir() if f.is_file())
    freed = storage.trim_previews(total - 250_000)
    left = sorted(f.name for f in PREVIEWS.glob("999999_junk_*"))
    check(f"кэш превью урезан со старых: убрано {freed // 1000} КБ, остались {left}",
          freed >= 250_000 and "999999_junk_0.jpg" not in left)
    for f in PREVIEWS.glob("999999_junk_*"):
        f.unlink()
    harness.done()
