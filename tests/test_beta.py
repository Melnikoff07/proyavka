"""Открытая бета: страница /beta, слоты (сколько, ники, срок), вход без приглашения, предел с одного адреса,
продление администратором, предупреждение и уход по сроку (номер не переиспользуется), выключенная бета."""
import json
import time
import urllib.error
import urllib.request
import harness


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def join(h, name, ip=None, lang="ru", accept=True):
    hdr = {"Content-Type": "application/json", **({"X-Real-IP": ip} if ip else {})}
    rq = urllib.request.Request(h.url + "/api/beta/join", data=json.dumps({"name": name, "lang": lang, "accept": accept}).encode(), headers=hdr)
    try:
        with urllib.request.urlopen(rq, timeout=60) as r:
            return {"_status": 200, "_cookie": r.headers.get("Set-Cookie", ""), **json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read())}


if __name__ == "__main__":
    h = harness.start(port=8124, extra_env={"BETA": "1", "BETA_SLOTS": "3", "MIN_FREE_GB": "0", "STORAGE_GB": "0.1"})
    from proyavka import beta, database, users      # после start: настройки читаются из окружения при импорте

    s = h.get("/api/beta", auth=False)
    check(f"слоты видны без входа: {s.get('free')} из {s.get('total')}, {s.get('slot_mb')} МБ, {s.get('days')} дн.",
          s.get("total") == 3 and s.get("free") == 3 and s.get("slot_mb") == 500 and s.get("days") == 3 and s.get("users") == [])
    page = h.req("/beta", auth=False)
    check("страница беты отдаётся, адрес сервера вписан", isinstance(page, bytes) and b"/beta/beta.js" in page
          and b"{{BASE}}" not in page and h.url.encode() in page)
    guide = h.req("/beta/guide.json", auth=False)
    check(f"анимация-гайд — Lottie: {len(guide.get('layers', []))} слоёв, {guide.get('op')} кадров",
          isinstance(guide, dict) and guide.get("layers") and guide.get("op") == 480 and len(guide.get("assets", [])) == 2)
    terms = h.req("/beta/terms", auth=False)
    check("условия и политика конфиденциальности отдаются", isinstance(terms, bytes) and b"GDPR" in terms and b"{{" not in terms)
    check("типографика: предлог не висит («в\u00a0», «на\u00a0»), тире не начинает строку",
          "в\u00a0".encode() in page and "на\u00a0".encode() in page and "\u00a0—".encode() in page)
    from proyavka import beta as b
    check("типограф не трогает атрибуты и скрипты", b.typo('<a title="в доме">в доме</a><script>и в</script>')
          == '<a title="в доме">в\u00a0доме</a><script>и в</script>')
    js = h.req("/beta/lottie.min.js", auth=False)
    check("плеер Lottie — свой, без внешних адресов", isinstance(js, bytes) and b"lottie" in js[:300])
    img = h.req("/beta/film/portrait400.jpg", auth=False)
    check("образец под плёнкой рисуется для страницы", isinstance(img, bytes) and img[:3] == b"\xff\xd8\xff")
    check("чужие имена плёнок и файлов — 404", h.req("/beta/film/../x.jpg", auth=False).get("_status") == 404
          and h.req("/beta/film/sepia.jpg", auth=False).get("_status") == 404 and h.req("/beta/index.html", auth=False).get("_status") == 404)

    check("короткий ник — 400", join(h, "a")["_status"] == 400)
    r = join(h, "без_согласия", ip="10.0.0.20", accept=False)
    check(f"без согласия с условиями — 400 ({r.get('error')})", r["_status"] == 400)
    a = join(h, "Плёнка_42", ip="10.0.0.1")
    uid_a = database.q("SELECT id FROM users WHERE name='Плёнка_42'")[0]["id"]
    u = users.user(uid_a)
    check(f"слот занят: номер {uid_a}, лимит {u['storage_gb']} ГБ, срок ~3 дня, согласие записано",
          a["_status"] == 200 and a.get("device") and uid_a > 900_000_000_000_000 and u["storage_gb"] == 0.5
          and abs(u["expires"] - time.time() - 3 * 86400) < 60 and abs(u["terms"] - time.time()) < 60)
    check("ключ устройства — и в cookie (iPhone перенесёт вход в приложение)", a["device"] in a["_cookie"])
    check("тот же ник в другом регистре — 400", join(h, "ПЛЁНКА_42", ip="10.0.0.2")["_status"] == 400)
    dev = h.post("/api/auth", {"device": a["device"]}, auth=False)
    check("вход ключом: срок слота в ответе", dev.get("token") and abs(dev.get("expires", 0) - u["expires"]) < 1)
    me = harness.Harness(h.fb, h.port, h.base)
    me.token = dev["token"]
    check("/api/me знает срок", me.get("/api/me").get("expires") == u["expires"])
    s = me.get("/api/beta")
    check("в списке слотов — ник и «это ты»", [x["name"] for x in s["users"]] == ["Плёнка_42"] and s["users"][0]["me"] and s["free"] == 2)
    check("второй слот с того же адреса — можно", join(h, "второй", ip="10.0.0.1")["_status"] == 200)
    r = join(h, "третий", ip="10.0.0.1")
    check(f"третий с того же адреса за сутки — 403 ({r.get('error')})", r["_status"] == 403)
    check("последний свободный слот", join(h, "третий", ip="10.0.0.3")["_status"] == 200)
    r = join(h, "четвёртый", ip="10.0.0.4")
    check(f"мест нет — 403 ({r.get('error')})", r["_status"] == 403)
    check("на странице: свободных 0", h.get("/api/beta", auth=False)["free"] == 0)

    h.auth()                                       # администратор
    old = users.user(uid_a)["expires"]
    lst = h.post(f"/api/user/{uid_a}/extend", {})
    check("администратор продлевает слот на срок беты", abs(users.user(uid_a)["expires"] - old - 3 * 86400) < 1
          and [x for x in lst["users"] if x["id"] == uid_a][0]["expires"] == users.user(uid_a)["expires"])
    check("продлить не-участника — 400", h.post(f"/api/user/{users.ADMIN}/extend", {}).get("_status") == 400)

    exp = users.user(uid_a)["expires"]
    beta.tick(now=exp - 3600)
    check("за 12 часов — предупреждение (один раз)", uid_a in beta.WARNED)
    beta.tick(now=exp + 1)
    check("срок вышел — участник удалён", users.user(uid_a) is None
          and database.q("SELECT name FROM beta_gone WHERE id=?", (uid_a,))[0]["name"] == "Плёнка_42")
    check("его ключ больше не входит", h.post("/api/auth", {"device": a["device"]}, auth=False).get("_status") == 401)
    database.run("UPDATE users SET expires=? WHERE expires IS NOT NULL", (time.time() - 1,))
    beta.tick()
    check("все слоты освободились", h.get("/api/beta", auth=False)["free"] == 3)
    b = join(h, "новый", ip="10.0.0.9")
    nid = database.q("SELECT id FROM users WHERE name='новый'")[0]["id"]
    check(f"номер ушедших не переиспользуется: {nid}", b["_status"] == 200 and nid > uid_a + 2)

    beta.BETA = False
    check("бета выключена: страница и слоты — 404", h.req("/beta", auth=False).get("_status") == 404
          and h.get("/api/beta", auth=False).get("_status") == 404)
    check("бета выключена: занять слот нельзя", join(h, "поздний", ip="10.0.0.10")["_status"] == 403)
    harness.done()
