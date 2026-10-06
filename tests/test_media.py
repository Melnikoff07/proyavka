"""Токен в адресах картинок: сессия в адрес не кладётся, вместо неё токен «только смотреть и скачивать свои кадры» —
подписан, ограничен по сроку, не годится для остального API и не открывает чужие кадры."""
import time
from pathlib import Path
import harness


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


TD = Path(__file__).resolve().parent / "testdata"

if __name__ == "__main__":
    h = harness.start(port=8118)
    from proyavka import config, database, users      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    a = h.post("/api/auth", {"initData": "test-1"}, auth=False)
    h.token = a["token"]
    media = a["media"]
    check(f"вход выдаёт токен для картинок: {media.split('.')[0]}.…", media.startswith("1.") and media != a["token"])
    check("токен для картинок — не сессия", media not in fb.SESSIONS)
    h.drop(TD / "a6300_DSC00266.JPG")
    h.wait(lambda: [p for p in h.photos() if p["view"]], timeout=120)
    pid = h.photos()[0]["id"]

    for what, path in (("миниатюра", f"/img/thumb/{pid}"), ("кадр", f"/img/view/{pid}"), ("превью", f"/img/preview/{pid}/golden200?st=100"),
                       ("полный размер", f"/img/full/{pid}")):
        sep = "&" if "?" in path else "?"
        r = h.req(f"{path}{sep}m={media}", auth=False)
        check(f"{what} открывается по токену из адреса", isinstance(r, bytes) and r[:3] == b"\xff\xd8\xff")
    z = h.req(f"/api/zip?ids={pid}&m={media}", auth=False)
    check("архив скачивается по токену из адреса", isinstance(z, bytes) and z[:2] == b"PK")
    cam = h.req(f"/api/camera/config.txt?m={media}", auth=False)
    check("файл настройки камеры тоже", isinstance(cam, bytes) or cam.get("_status") != 401)

    # токен для картинок — не ключ от всего
    for what, path in (("лента", "/api/photos"), ("настройки", "/api/me"), ("устройства", "/api/devices"), ("опрос", "/api/updates?since=0"),
                       ("альбомы", "/api/albums")):
        r = h.req(f"{path}{'&' if '?' in path else '?'}m={media}", auth=False)
        check(f"{what}: по токену из адреса — 401", isinstance(r, dict) and r.get("_status") == 401)
    check("изменять по нему нельзя", h.req(f"/api/photo/{pid}?m={media}", data={"strength": 50}, auth=False).get("_status") == 401)
    check("создать устройство по нему нельзя", h.req(f"/api/devices/new?m={media}", data={}, auth=False).get("_status") == 401)
    check("сессия в адресе на не-картинках больше не работает", h.req(f"/api/photos?s={h.token}", auth=False).get("_status") == 401)

    # подделка и срок
    uid, exp, sig = media.split(".")
    bad = {"подпись испорчена": f"{uid}.{exp}.{sig[:-2]}AA", "чужой пользователь": f"2.{exp}.{sig}", "срок продлён": f"{uid}.{int(exp) + 86400}.{sig}",
           "мусор": "abc", "пусто": ""}
    for why, tok in bad.items():
        r = h.req(f"/img/thumb/{pid}?m={tok}", auth=False)
        check(f"{why}: 401", isinstance(r, dict) and r.get("_status") == 401)
    old_exp = int(time.time()) - 10
    expired = f"{uid}.{old_exp}.{fb._media_sig(int(uid), old_exp)}"
    check("просроченный токен: 401", h.req(f"/img/thumb/{pid}?m={expired}", auth=False).get("_status") == 401)
    check(f"срок 1–2 суток ({(int(exp) - time.time()) / 3600:.0f} ч)", 24 * 3600 - 5 <= int(exp) - time.time() <= 48 * 3600 + 5)
    check("в течение дня токен тот же (кэш браузера)", fb.media_token(1) == media)

    # чужой кадр
    h.add_user(2)
    h2 = h.as_user(2)
    m2 = h.post("/api/auth", {"initData": "test-2"}, auth=False)["media"]
    check("токен другого пользователя чужой кадр не открывает", h.req(f"/img/thumb/{pid}?m={m2}", auth=False).get("_status") == 404
          and h.req(f"/img/full/{pid}?m={m2}", auth=False).get("_status") == 404)
    check("удалённого пользователя токен не работает",
          (database.run("DELETE FROM users WHERE id=2"), users.load_users(), h.req(f"/img/thumb/{pid}?m={m2}", auth=False).get("_status") == 401)[2])

    # опрос обновляет токен; ключ переживает перезапуск
    check("опрос присылает токен для картинок", h.get("/api/updates?since=0").get("media") == media)
    key = (config.BASE / "media.key").read_bytes()
    fb.MEDIA_KEY = None
    check("ключ хранится в файле и после перезапуска тот же", fb.media_token(1) == media and (config.BASE / "media.key").read_bytes() == key)
    harness.done()
