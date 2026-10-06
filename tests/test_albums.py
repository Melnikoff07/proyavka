"""Альбомы по ссылке: создать из выбранных кадров, открыть без входа, скачать кадр и архив,
чужие кадры и корзина не видны, правка состава, переименование, удаление — ссылка умирает."""
import io
import time
import zipfile
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
    h = harness.start(port=8113)
    from proyavka import config, database      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    h.auth(1)
    for i in range(4):
        h.drop(frame(i, f"A{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 4, timeout=120)
    ids = [p["id"] for p in h.photos()]

    # чужой кадр
    h.add_user(2)
    other = database.run("INSERT INTO photos(name, owner, hidden, view, taken) VALUES ('x.jpg', 2, 0, ?, '2024-01-01 10:00:00')",
                   (h.photos()[0]["view"],))

    check("пустой альбом не создаётся", h.post("/api/albums", {"ids": [other]}).get("_status") == 400)
    a = h.post("/api/albums", {"ids": ids[:3] + [other], "title": "Прогулка <b>"})
    check(f"альбом создан: {a.get('n')} кадра, чужой не попал", a.get("n") == 3 and other not in a["ids"])
    check(f"название очищено: {a.get('title')!r}", a["title"] == "Прогулка b")
    tok = a["url"].rsplit("/", 1)[1]
    check(f"ключ в ссылке: {len(tok)} символов", len(tok) == 16 and a["url"].startswith(config.WEBAPP_URL.rstrip("/") + "/a/"))

    pub = harness.Harness(fb, h.port, h.base)          # без входа
    page = pub.get(f"/a/{tok}", auth=False)
    check("страница открывается без входа", isinstance(page, bytes) and "Прогулка b".encode() in page)
    check("в подвале контакт автора и ссылка на проект", b"https://t.me/Sashkere" in page and b"github.com/Melnikoff07/proyavka" in page)
    check("превью ссылки: og:image", b'og:image' in page and f"/a/{tok}/view/".encode() in page)
    lst = pub.get(f"/a/{tok}/list", auth=False)
    check(f"список: {len(lst.get('photos', []))} кадра по времени", [p["id"] for p in lst["photos"]] == sorted(ids[:3]))
    check("счётчик открытий", database.q("SELECT views FROM albums")[0]["views"] == 1)
    t = pub.get(f"/a/{tok}/thumb/{ids[0]}", auth=False)
    check("миниатюра отдаётся", isinstance(t, bytes) and t[:3] == b"\xff\xd8\xff")
    check("кадр не из альбома — 404", pub.get(f"/a/{tok}/view/{ids[3]}", auth=False).get("_status") == 404)
    check("чужой кадр — 404", pub.get(f"/a/{tok}/view/{other}", auth=False).get("_status") == 404)
    check("«О проекте» в настройках: GitHub и Telegram", h.get("/api/me")["about"] == {"project": config.PROJECT_URL, "tg": "Sashkere"})
    check("лента без входа закрыта", pub.get("/api/photos", auth=False).get("_status") == 401)

    t0 = time.time()
    full = pub.get(f"/a/{tok}/full/{ids[0]}", auth=False)
    t1 = time.time()
    check(f"полный размер: {Image.open(io.BytesIO(full)).size}", Image.open(io.BytesIO(full)).size[0] >= 900)
    pub.get(f"/a/{tok}/full/{ids[0]}", auth=False)
    t2 = time.time()
    check(f"второй раз из кэша ({t1 - t0:.2f} с → {t2 - t1:.2f} с)", list(config.PREVIEWS.glob(f"{ids[0]}_full_*.jpg")))
    z = zipfile.ZipFile(io.BytesIO(pub.get(f"/a/{tok}/zip", auth=False)))
    check(f"архив: {len(z.namelist())} файла", len(z.namelist()) == 3)

    bad = pub.get("/a/AAAAAAAAAAAAAAAA", auth=False)
    check("неверная ссылка — 404", bad.get("_status") == 404)

    # корзина и правки
    fb.delete_photos([ids[1]])
    check("кадр в корзине из альбома пропал", len(pub.get(f"/a/{tok}/list", auth=False)["photos"]) == 2
          and pub.get(f"/a/{tok}/view/{ids[1]}", auth=False).get("_status") == 404)
    check("и его полный кадр из кэша стёрт", not list(config.PREVIEWS.glob(f"{ids[1]}_full_*.jpg")))
    e = h.post(f"/api/album/{a['id']}", {"ids": [ids[0], ids[3]], "title": "Новое"})
    check(f"состав изменён: {e.get('n')}", e.get("n") == 2 and e["title"] == "Новое" and e["url"] == a["url"])
    h.add_user(3)
    h3 = harness.Harness(fb, h.port, h.base)
    h3.auth(3)
    check("чужой альбом не изменить и не удалить", h3.post(f"/api/album/{a['id']}", {"title": "x"}).get("_status") == 400
          and not h3.post(f"/api/album/{a['id']}/delete").get("ok"))
    check("список альбомов", [x["id"] for x in h.get("/api/albums")["albums"]] == [a["id"]] and h3.get("/api/albums")["albums"] == [])

    check("удаление", h.post(f"/api/album/{a['id']}/delete").get("ok"))
    check("после удаления ссылка не работает", pub.get(f"/a/{tok}/list", auth=False).get("_status") == 404)
    check("строк не осталось", not database.q("SELECT 1 FROM album_photos"))

    # удаление пользователя убирает его альбомы
    a2 = h.post("/api/albums", {"ids": [ids[0]]})
    h.add_user(4)
    database.run("UPDATE albums SET owner=4")
    fb.delete_user(4)
    check("удалили пользователя — его альбомы тоже", not database.q("SELECT 1 FROM albums") and a2.get("n") == 1)
    harness.done()
