"""Приёмник плёнок: любой может прислать плёнку без входа, она попадает в очередь, администратор одобряет в приложении,
одобренная появляется в каталоге (и отдаётся другим серверам), всё проверяется и ограничивается."""
import io
from pathlib import Path
from PIL import Image
import harness


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


TD = Path(__file__).resolve().parent / "testdata"


def params(**kw):
    return dict({"contrast": 0.4, "sat": 1.1, "grain": 0.05}, **kw)


if __name__ == "__main__":
    h = harness.start(port=8119, extra_env={"COMMUNITY_HUB": "1", "COMMUNITY_URL": ""})
    fb = h.fb
    h.auth(1)
    pub = harness.Harness(fb, h.port, h.base)                      # без входа
    h.drop(TD / "a6300_DSC00266.JPG")
    h.wait(lambda: [p for p in h.photos() if p["view"]], timeout=120)

    # --- приём от кого угодно ---
    r = pub.post("/api/community/submit", {"name": "Тёплая <b>осень</b>", "by": "Аня", "p": params(sat=1.3)}, auth=False)
    check("плёнка принята без входа", r.get("ok") and not r.get("again"))
    check("такая же повторно не копится", pub.post("/api/community/submit", {"name": "x", "p": params(sat=1.3)}, auth=False).get("again"))
    check("битые параметры — 400", pub.post("/api/community/submit", {"name": "x", "p": {"sat": "много"}}, auth=False).get("_status") == 400)
    check("не плёнка — 400", pub.post("/api/community/submit", {"привет": 1}, auth=False).get("_status") == 400)
    check("огромная заявка — 400", pub.post("/api/community/submit", {"name": "x" * 20000, "p": params()}, auth=False).get("_status") == 400)
    codes = [pub.post("/api/community/submit", {"name": f"n{i}", "p": params(contrast=0.1 + i / 50)}, auth=False).get("_status", 200) for i in range(8)]
    check(f"лимит отправок с одного адреса: {codes}", 429 in codes and codes.count(200) == 4)          # 1 уже было из 5 в час

    # --- проверка администратором ---
    check("очередь видит администратор", len(h.get("/api/community/pending")["items"]) == 5)
    h.add_user(2)
    h2 = h.as_user(2)
    check("обычный пользователь очередь не видит и не решает", h2.get("/api/community/pending").get("_status") == 403
          and h2.post("/api/community/pending/1", {"action": "approve"}).get("_status") == 403)
    check("в списке каталога у админа счётчик, у остальных его нет", h.get("/api/community")["hub"]["pending"] == 5 and "hub" not in h2.get("/api/community"))
    first = h.get("/api/community/pending")["items"][0]
    check("угловые скобки из имени убраны (в интерфейсе оно всё равно показывается как текст)", "<" not in first["name"] and ">" not in first["name"] and first["by"] == "Аня")
    pv = h.get(f"/img/pending/{first['id']}")
    check("превью заявки рисуется на образце", isinstance(pv, bytes) and Image.open(io.BytesIO(pv)).size[0] == 900)
    check("превью — только администратору", h2.get(f"/img/pending/{first['id']}").get("_status") == 404)

    n0 = len(h.get("/api/community")["looks"])
    ok = h.post(f"/api/community/pending/{first['id']}", {"action": "approve", "name": "Тёплая осень", "desc_ru": "тёплый вечер", "desc_en": "a warm evening"})
    check(f"одобрена: {ok}", ok.get("ok") and ok["id"].startswith("teplaya"))
    cat = h.get("/api/community")
    mine = next((x for x in cat["looks"] if x["id"] == "teplaya-osen"), None)
    check("появилась в каталоге сразу", len(cat["looks"]) == n0 + 1 and mine and mine["by"] == "Аня" and mine["desc"] == "тёплый вечер")
    img = h.get("/img/cm/teplaya-osen")
    check("и её картинка отдаётся", isinstance(img, bytes) and Image.open(io.BytesIO(img)).size[0] == 900)
    check("из очереди ушла", len(h.get("/api/community/pending")["items"]) == 4)
    check("повторно одобрить нельзя", h.post(f"/api/community/pending/{first['id']}", {"action": "approve"}).get("_status") == 400)

    # --- другие серверы забирают каталог без входа ---
    pubcat = pub.get("/community/looks.json", auth=False)
    check("каталог для других серверов открыт без входа", isinstance(pubcat, dict) and any(e["id"] == "teplaya-osen" for e in pubcat["looks"]))
    jpg = pub.get("/community/looks/teplaya-osen.jpg", auth=False)
    seed = pub.get("/community/looks/warm-postcard.jpg", auth=False)
    check("картинки тоже (одобренные и из репозитория)", isinstance(jpg, bytes) and isinstance(seed, bytes))
    check("чужие имена и обход пути — 404", pub.get("/community/looks/..%2fx.jpg", auth=False).get("_status") == 404
          and pub.get("/community/etc", auth=False).get("_status") == 404)
    # каталог попадает и в разбор у скачивающего сервера
    check("каталог разбирается так же, как у остальных", [e["id"] for e in fb._community_entries(pubcat)].count("teplaya-osen") == 1)

    nxt = h.get("/api/community/pending")["items"][0]
    check("отклонить", h.post(f"/api/community/pending/{nxt['id']}", {"action": "reject"}).get("ok")
          and all(i["id"] != nxt["id"] for i in h.get("/api/community/pending")["items"]))

    # --- отправка из приложения: на этот же сервер и по сети ---
    fb.SUBMITS.clear()
    mk = h2.post("/api/look", {"name": "Моя", "params": params(contrast=0.9, vignette=0.4)})
    info = h2.get("/api/community")["submit"]
    check(f"отправка включена: {info}", info["on"])
    sent = h2.post(f"/api/look/{mk['key']}/submit")
    check("отправка автору: плёнка в очереди", sent.get("ok") and any(i["name"] == "Моя" for i in h.get("/api/community/pending")["items"]))
    flags = next(x for x in h2.get("/api/presets")["presets"] if x["key"] == mk["key"])
    check("плёнка помечена как отправленная", flags.get("sent") is True and flags.get("community") is False)
    check("повторная отправка не копит", h2.post(f"/api/look/{mk['key']}/submit").get("ok")
          and sum(1 for i in h.get("/api/community/pending")["items"] if i["name"] == "Моя") == 1)
    mk2 = h2.post("/api/look", {"name": "По сети", "params": params(contrast=0.2, vignette=0.1)})
    fb.COMMUNITY_SUBMIT_URL = f"http://127.0.0.1:{h.port}/api/community/submit"
    net = h2.post(f"/api/look/{mk2['key']}/submit")
    check("отправка по сети на приёмник", net.get("ok") and any(i["name"] == "По сети" for i in h.get("/api/community/pending")["items"]))
    fb.COMMUNITY_SUBMIT_URL = "http://127.0.0.1:9/api/community/submit"
    mk3 = h2.post("/api/look", {"name": "Недоступно", "params": params(contrast=0.3, vignette=0.2)})
    bad = h2.post(f"/api/look/{mk3['key']}/submit")
    check(f"приёмник недоступен — понятная ошибка: {bad.get('error')}", bad.get("_status") == 400 and "сервер" in bad["error"])
    fb.COMMUNITY_SUBMIT_URL = "http://evil.example/api/community/submit"
    check("чужой адрес без https не годится", h2.post(f"/api/look/{mk3['key']}/submit").get("_status") == 400)
    fb.COMMUNITY_SUBMIT_URL = ""
    imp = h2.post("/api/community/add", {"id": "teplaya-osen"})
    check("плёнку из каталога обратно не предлагают", h2.post(f"/api/look/{imp['key']}/submit").get("_status") == 400)
    check("чужую плёнку отправить нельзя", h.post(f"/api/look/{mk['key']}/submit").get("_status") == 400)

    # --- сервер, который не приёмник ---
    fb.COMMUNITY_HUB = False
    fb.COMMUNITY["at"] = 0
    check("не приёмник: приёма нет", pub.post("/api/community/submit", {"name": "x", "p": params()}, auth=False).get("_status") == 404)
    check("не приёмник: каталога для других нет", pub.get("/community/looks.json", auth=False).get("_status") in (401, 404))
    check("не приёмник и адрес не задан: отправка выключена", not h2.get("/api/community")["submit"]["on"]
          and h2.post(f"/api/look/{mk['key']}/submit").get("_status") == 400)
    harness.done()
