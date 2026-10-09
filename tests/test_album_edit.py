"""Правка по ссылке альбома: гость входит по ссылке, правит только кадры альбома, хозяин видит то же и не получает сообщений;
выключили правку — гость выходит; чужое и служебное гостю недоступно."""
import time
from pathlib import Path
import numpy as np
from PIL import Image
import harness


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def frame(seed, name):
    p = Path(harness.tempfile.mkdtemp()) / name
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (600, 900, 3), dtype=np.uint8)).save(p, "JPEG")
    return p


if __name__ == "__main__":
    h = harness.start(port=8122)
    h.auth(1)
    for i in range(3):
        h.drop(frame(i, f"E{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 3, timeout=120)
    ids = sorted(p["id"] for p in h.photos())
    inside, outside = ids[:2], ids[2]

    off = h.post("/api/albums", {"ids": inside, "edit": False})
    tok = off["url"].rsplit("/", 1)[1]
    pub = harness.Harness(h.fb, h.port, h.base)
    check("правка выключена — гость не входит", pub.post("/api/auth", {"album": tok}, auth=False).get("_status") == 403)
    check("страница альбома не зовёт править", pub.get(f"/a/{tok}/list", auth=False)["edit"] is False)
    on = h.post(f"/api/album/{off['id']}", {"edit": True})
    check("хозяин включил правку", on["edit"] is True)
    check("список альбома сообщает о правке", pub.get(f"/a/{tok}/list", auth=False)["edit"] is True)
    check("плохая ссылка не пускает", pub.post("/api/auth", {"album": "x" * 16}, auth=False).get("_status") == 403)

    g = pub.post("/api/auth", {"album": tok}, auth=False)
    check("гость вошёл", bool(g.get("token")) and g.get("guest") is True)
    pub.token = g["token"]
    photos = pub.get("/api/photos")
    check(f"лента гостя: только кадры альбома ({photos['total']})", sorted(p["id"] for p in photos["photos"]) == inside and photos["total"] == 2)
    pr = pub.get("/api/presets")
    check("плёнки гостя — встроенные", not any(p.get("lut") for p in pr["presets"]))
    check("служебное закрыто: me, albums, trash, devices, users, camera",
          all(pub.get(x).get("_status") == 403 for x in ("/api/me", "/api/albums", "/api/trash", "/api/devices", "/api/users", "/api/camera")))
    check("загрузка и настройки закрыты", pub.req("/api/upload?name=a.jpg", raw=b"x", ctype="image/jpeg").get("_status") == 403
          and pub.post("/api/me", {"name": "z"}).get("_status") == 403)
    check("кадр вне альбома: правка 404", pub.post(f"/api/photo/{outside}", {"preset": pr["presets"][2]["key"]}).get("_status") == 404)
    check("кадр вне альбома: картинка 404", pub.get(f"/img/thumb/{outside}").get("_status") == 404)
    check("удалить и отправить в чат гость не может", pub.post(f"/api/photo/{inside[0]}/hide").get("_status") == 403
          and pub.post(f"/api/photo/{inside[0]}/file").get("_status") == 403
          and pub.post("/api/batch", {"action": "delete", "ids": inside}).get("_status") == 400)
    r0 = pub.post("/api/batch", {"action": "edit", "ids": [outside], "changes": {"preset": pr["presets"][3]["key"]}})
    check("пакет: чужой кадр отбрасывается", r0.get("queued") == 0 and h.photos("SELECT preset FROM photos WHERE id=?", (outside,))[0]["preset"] != pr["presets"][3]["key"])

    key = pr["presets"][2]["key"]
    since = time.time()
    r = pub.post(f"/api/photo/{inside[0]}", {"preset": key})
    check(f"гость сменил плёнку на {key}", r.get("preset") == key)
    ok = pub.post("/api/batch", {"action": "edit", "ids": inside, "changes": {"strength": 50}})
    check("пакетная правка гостем", ok.get("ok") and ok.get("batch") is None)
    check("своя плёнка хозяина гостю недоступна", pub.post(f"/api/photo/{inside[0]}", {"preset": "lut1"}).get("_status") == 400)
    h.wait(lambda: all(not p["pending"] for p in pub.get("/api/photos")["photos"]), timeout=120)
    mine = {p["id"]: p for p in h.get("/api/photos")["photos"]}
    check("хозяин видит правки гостя", mine[inside[0]]["preset"] == key and mine[inside[0]]["strength"] == 50)
    check("хозяину не пришло ни сообщений, ни файлов",
          not h.calls("sendMessage", since) and not h.calls("sendPhoto", since) and not h.calls("editMessageMedia", since)
          and not h.calls("sendDocument", since))

    # картинки по токену гостя — только из альбома
    m = pub.get("/api/updates?since=0")["media"]
    check("токен картинок гостя отдаёт кадр альбома", isinstance(pub.req(f"/img/thumb/{inside[0]}?m={m}", auth=False), bytes))
    check("…но не чужой", pub.req(f"/img/thumb/{outside}?m={m}", auth=False).get("_status") == 404)
    check("…и не служебное", pub.req(f"/api/me?m={m}", auth=False).get("_status") == 401)
    dl = pub.req(f"/img/full/{inside[0]}?m={m}", auth=False)
    check("полный размер скачивается", isinstance(dl, bytes) and dl[:3] == b"\xff\xd8\xff")

    # хозяин выключил правку — сессия и токен гаснут
    h.post(f"/api/album/{off['id']}", {"edit": False})
    check("правка выключена — сессия гостя закрыта", pub.get("/api/photos").get("_status") == 401)
    check("…и токен картинок тоже", pub.req(f"/img/thumb/{inside[0]}?m={m}", auth=False).get("_status") == 401)
    harness.done()
