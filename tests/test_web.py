"""«Проявка» без Telegram: установка с QR, кадры только в приложении, настройки, приглашения кодом,
подключение бота из приложения и привязка своего Telegram."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def call(h, path, data=None, token=None):
    hd = {"Content-Type": "application/json"}
    if token:
        hd["X-Token"] = token
    body = json.dumps(data).encode() if data is not None else None
    rq = urllib.request.Request(h.url + path, data=body, headers=hd, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(rq, timeout=120) as r:
            b = r.read()
            return {"_status": r.status, **json.loads(b)} if "json" in r.headers.get("Content-Type", "") else {"_status": r.status, "_body": b}
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def sends(since=0):
    return [c for c in harness.CALLS if c[0] >= since and c[1] in ("sendPhoto", "sendMessage", "editMessageMedia", "sendDocument")]


if __name__ == "__main__":
    WEB = 900_000_000_000_000
    conf = Path(tempfile.mkdtemp()) / "config.env"
    conf.write_text("LANGUAGE=ru\nCHAT_ID=900000000000000\n", encoding="utf-8")
    h = harness.start(port=8111, uid=WEB, extra_env={"BOT_TOKEN": "", "CONFIG_FILE": str(conf), "DOMAIN": "test.sslip.io"})
    from proyavka import config, database, film, telegram, users      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    check("бот запущен без токена", config.BOT_TOKEN == "" and users.ADMIN == WEB and users.user(WEB)["tg"] is None)

    # мастер установки: filmbot.py --pair другим процессом -> код и QR
    r = subprocess.run([sys.executable, str(Path(fb.__file__)), "--pair"], capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), cwd=str(Path(fb.__file__).parent), timeout=60)
    out = r.stdout.splitlines()
    check(f"--pair: ссылка, QR из {len(out) - 2} строк, код {out[-1] if out else '?'}",
          r.returncode == 0 and "#pair=" in out[0] and len(out) > 15 and re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", out[-1]))
    a = call(h, "/api/pair", {"code": out[-1].lower(), "name": "iPhone · Safari (app)"})
    check("вход администратора кодом из установки", a["_status"] == 200 and a.get("device"))
    tok = a["token"]

    # кадры: проявляются, в Telegram ничего не уходит
    t0 = time.time()
    h.drop(TD / "a6300_DSC00266.JPG")
    h.drop(TD / "a6300_DSC00269.JPG")
    h.wait(lambda: len([p for p in h.photos() if p["view"] and p["rendered_rev"] == p["rev"]]) == 2, timeout=120)
    time.sleep(1.5)
    pids = [p["id"] for p in h.photos()]
    check(f"кадры проявлены, владелец — администратор приложения: {[p['owner'] for p in h.photos()]}",
          all(p["owner"] == WEB for p in h.photos()))
    check(f"в Telegram ничего не отправлено: {len(sends(t0))}", not sends(t0) and not [c for c in harness.CALLS if c[0] >= t0])
    j = call(h, "/api/photos", token=tok)
    check(f"лента: {j['total']} кадра", j["total"] == 2)

    # настройки
    me = call(h, "/api/me", token=tok)
    check(f"/api/me: админ, Telegram нет, {me['used'] / 1e6:.1f} МБ из {me['limit'] / 1e6:.0f}",
          me["admin"] and me["telegram"]["bot"] is None and not me["telegram"]["linked"] and me["frames"] == 2)
    film_key = list(film.PRESETS)[2]
    me = call(h, "/api/me", {"lang": "en", "default_film": film_key, "name": "Admin <x>"}, tok)
    check(f"сменил язык, плёнку новым кадрам ({film_key}) и имя: {me.get('name')!r}",
          me["lang"] == "en" and me["default_film"] == film_key and me["name"] == "Admin x")
    check("чужая плёнка не ставится", call(h, "/api/me", {"default_film": "lut999"}, tok)["_status"] == 400)
    call(h, f"/api/photo/{pids[0]}/hide", {}, tok)
    tr_ = call(h, "/api/trash", token=tok)
    check(f"корзина: {[p['id'] for p in tr_['photos']]}", [p["id"] for p in tr_["photos"]] == [pids[0]])
    check("/api/me видит корзину", call(h, "/api/me", token=tok)["trash"] == 1)
    r = call(h, f"/api/photo/{pids[0]}/restore", {}, tok)
    check("кадр вернулся из корзины", r.get("ok") and database.get(pids[0])["hidden"] == 0)
    cam = call(h, "/api/camera", token=tok)
    check(f"камера администратора: {cam.get('ftp_user')}@{cam.get('domain')}", cam["ftp_user"] == "camera" and cam["domain"] == "test.sslip.io")
    if cam["cert"]:
        r = call(h, "/api/camera/cacert.pem", token=tok)
        check("cacert.pem скачивается", r["_status"] == 200 and b"BEGIN CERTIFICATE" in r["_body"])

    # приглашение кодом: новый человек без Telegram
    inv = call(h, "/api/invite", {}, tok)
    check(f"приглашение: код {inv.get('code')}, ссылка, QR; без бота нет Telegram-ссылки",
          inv["url"].endswith("#pair=" + inv["code"].replace("-", "")) and inv["qr"].startswith("<svg") and inv["tg_url"] == "")
    r = call(h, "/api/pair", {"code": inv["code"], "name": "Android · Chrome"})
    check("код приглашения просит имя", r == {"_status": 200, "need_name": True})
    r = call(h, "/api/pair", {"code": inv["code"], "name": "Android · Chrome", "user_name": "Маша"})
    bob = database.q("SELECT * FROM users WHERE name='Маша'")
    check(f"Маша пришла: номер {bob[0]['id'] if bob else '?'}, сразу с устройством",
          r["_status"] == 200 and r.get("device") and bob and bob[0]["id"] == WEB + 1 and bob[0]["tg"] is None)
    bob_id, bob_tok = bob[0]["id"], r["token"]
    check("приглашение одноразовое", call(h, "/api/pair", {"code": inv["code"], "user_name": "Ещё"})["_status"] == 403)
    check("у Маши своя пустая лента", call(h, "/api/photos", token=bob_tok)["total"] == 0)
    mb = call(h, "/api/me", token=bob_tok)
    check(f"Маша не администратор, лимит {mb['limit'] / 1e9:g} ГБ", not mb["admin"] and mb["limit"] == users.USER_STORAGE_GB * 1e9)
    check("Маше нельзя список пользователей и приглашения", call(h, "/api/users", token=bob_tok)["_status"] == 403
          and call(h, "/api/invite", {}, bob_tok)["_status"] == 403)
    cb = call(h, "/api/camera", token=bob_tok)
    check(f"камера Маши заведена: {cb.get('ftp_user')}", cb["ftp_user"] == f"u{bob_id}" and harness.CAM and harness.CAM[-1][1] == f"u{bob_id}")
    r = call(h, "/api/camera/config.txt", token=bob_tok)
    check("config.txt Маши с её токеном", r["_status"] == 200 and users.user(bob_id)["cam_token"].encode() in r["_body"])
    old = cb["ftp_pass"]
    cb = call(h, "/api/camera/password", {}, bob_tok)
    check(f"новый пароль FTP Маши: {cb.get('ftp_pass')} (вместо {old})", re.fullmatch(r"[a-km-np-z2-9]{10}", cb["ftp_pass"] or "")
          and cb["ftp_pass"] != old and harness.CAM[-1][0] == "add" and harness.CAM[-1][2].startswith(cb["ftp_pass"] + "\n"))
    ca = call(h, "/api/camera/password", {}, tok)
    check(f"новый пароль FTP администратора: {ca.get('ftp_pass')}, записан в config.env",
          re.fullmatch(r"[a-km-np-z2-9]{10}", ca["ftp_pass"] or "") and harness.CAM[-1][:2] == ("passwd", "camera")
          and f"FTP_PASS={ca['ftp_pass']}" in conf.read_text())
    us = call(h, "/api/users", token=tok)
    check(f"пользователи: {[u['name'] for u in us['users']]}", [u["name"] for u in us["users"]] == ["Admin x", "Маша"])
    us = call(h, f"/api/user/{bob_id}/limit", {"gb": 10}, tok)
    check("лимит Маши 10 ГБ", users.storage_limit(bob_id) == 10)
    us = call(h, f"/api/user/{WEB}/limit", {"gb": 5}, tok)
    check("свой лимит администратора 5 ГБ — в config.env", users.storage_limit(WEB) == 5 and "STORAGE_GB=5" in conf.read_text()
          and call(h, "/api/me", token=tok)["limit"] == 5e9)
    check("себя удалить нельзя", call(h, f"/api/user/{WEB}/delete", {}, tok)["_status"] == 404)

    # подключить бота из приложения
    check("Маше нельзя подключать бота", call(h, "/api/tg/bot", {"token": "1:" + "x" * 35}, bob_tok)["_status"] == 403)
    check("кривой токен не принят", call(h, "/api/tg/bot", {"token": "abc"}, tok)["_status"] == 400)
    real_post = fb.requests.post

    class R:
        def json(self):
            return {"ok": True, "result": {"username": "proyavka_test_bot"}}
    fb.requests.post = lambda url, **kw: R() if url.endswith("/getMe") else real_post(url, **kw)
    r = call(h, "/api/tg/bot", {"token": "123456:" + "A" * 35}, tok)
    fb.requests.post = real_post
    check(f"бот подключён: @{r.get('bot')}, токен в config.env",
          r.get("bot") == "proyavka_test_bot" and config.BOT_TOKEN.startswith("123456:") and "BOT_TOKEN=123456:" in conf.read_text()
          and "CHAT_ID=900000000000000" in conf.read_text())
    time.sleep(1.5)
    check("кадры по-прежнему не уходят: Telegram ещё не привязан", not sends(t0))

    # привязать свой Telegram
    link = call(h, "/api/tg/link", {}, tok)
    code = link["url"].split("start=link_")[1]
    check(f"ссылка на бота: {link['url'][:45]}…", link["url"].startswith("https://t.me/proyavka_test_bot?start=link_"))
    t1 = time.time()
    fb.on_stranger({"from": {"id": 555, "first_name": "A"}, "chat": {"id": 555, "type": "private"}, "text": "/start link_" + code})
    check("Telegram 555 привязан к администратору", users.user(WEB)["tg"] == 555 and telegram.uid_of_tg(555) == WEB)
    m = [c for c in sends(t1) if c[2].get("chat_id") == 555]
    check("в бот пришло приветствие", m and "Telegram" in m[0][2]["text"])
    fb.on_stranger({"from": {"id": 556}, "chat": {"id": 556, "type": "private"}, "text": "/start link_" + code})
    check("та же ссылка второй раз не работает", telegram.uid_of_tg(556) is None)
    t2 = time.time()
    h.drop(TD / "a6300_DSC00270.JPG", name="NEW0001.JPG")
    h.wait(lambda: [c for c in sends(t2) if c[1] == "sendPhoto" and c[2].get("chat_id") == 555], timeout=120)
    check("новый кадр ушёл в Telegram администратора", True)
    ha = harness.Harness(fb, h.port, h.base)
    ha.auth(555)
    check("Mini App в Telegram (initData 555) входит как администратор", ha.get("/api/me")["id"] == WEB)
    me = call(h, "/api/me", token=tok)
    check("в настройках: привязан, можно отвязать", me["telegram"] == {"bot": "proyavka_test_bot", "linked": True, "can_unlink": True})

    # приглашение через Telegram тем же кодом
    inv = call(h, "/api/invite", {}, tok)
    check(f"теперь у приглашения и Telegram-ссылка: {inv['tg_url'][:40]}…", inv["tg_url"].endswith("?start=" + inv["code"].replace("-", "")))
    fb.on_stranger({"from": {"id": 777, "first_name": "Петя"}, "chat": {"id": 777, "type": "private"},
                    "text": "/start " + inv["code"].replace("-", "")})
    check("Петя пришёл через Telegram: номер = его id, чат = он сам", users.user(777) and users.user(777)["tg"] == 777)

    # отвязать
    me = call(h, "/api/tg/unlink", {}, tok)
    check("отвязал Telegram", me["telegram"]["linked"] is False and telegram.uid_of_tg(555) is None)
    t3 = time.time()
    import numpy as np
    from PIL import Image
    rnd = Path(tempfile.mkdtemp()) / "NEW0002.JPG"
    Image.fromarray(np.random.default_rng(7).integers(0, 255, (800, 1200, 3), dtype=np.uint8)).save(rnd, "JPEG")
    h.drop(rnd)
    h.wait(lambda: [p for p in h.photos() if p["name"] == "NEW0002.JPG" and p["view"]], timeout=120)
    time.sleep(2)
    check("после отвязки кадры в Telegram не уходят", not [c for c in sends(t3) if c[2].get("chat_id") == 555])
    hp = harness.Harness(fb, h.port, h.base)
    hp.auth(777)
    check("Пете (пришёл через Telegram) отвязать нельзя", call(h, "/api/tg/unlink", {}, hp.token)["_status"] == 400)

    # удалить Машу
    r = call(h, f"/api/user/{bob_id}/delete", {}, tok)
    check("Маша удалена вместе с устройствами", bob_id not in users.USERS and not database.q("SELECT 1 FROM devices WHERE owner=?", (bob_id,)))
    check("её сессия закрыта", call(h, "/api/photos", token=bob_tok)["_status"] == 401)
    harness.done()
