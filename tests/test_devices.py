"""Устройства без Telegram: /link, привязка по коду, вход ключом, отзыв, скачивание файлом и архивом, PWA."""
import io
import json
import re
import time
import urllib.request
import zipfile
from pathlib import Path
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"
ADMIN, BOB = 1, 2002


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def raw_get(h, path, token=None, headers=None):
    hd = dict(headers or {})
    if token:
        hd["X-Token"] = token
    rq = urllib.request.Request(h.url + path, headers=hd)
    try:
        with urllib.request.urlopen(rq, timeout=300) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(h, path, data, token=None, ip=None, cookie=None, want_headers=False):
    hd = {"Content-Type": "application/json"}
    if cookie:
        hd["Cookie"] = cookie
    if token:
        hd["X-Token"] = token
    if ip:
        hd["X-Real-IP"] = ip
    rq = urllib.request.Request(h.url + path, data=json.dumps(data).encode(), headers=hd, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=60) as r:
            out = {"_status": r.status, **json.loads(r.read())}
            if want_headers:
                out["_headers"] = dict(r.headers)
            return out
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}


def code_of(link):
    return re.search(r"#pair=([\w-]+)", link).group(1)


if __name__ == "__main__":
    h = harness.start(port=8110)
    fb = h.fb
    h.add_user(BOB, "en", "Bob")
    h.auth(ADMIN)
    tg_token = h.token

    # кадр администратора
    h.drop(TD / "a6300_DSC00266.JPG")
    h.drop(TD / "a6300_DSC00269.JPG")
    h.wait(lambda: len([p for p in h.photos() if p["msg_id"]]) == 2, timeout=120)
    pids = [p["id"] for p in h.photos()]

    # PWA: страница, манифест, сервис-воркер, значки — без входа
    st, hd, body = raw_get(h, "/")
    check(f"страница с CSP и no-referrer: {hd.get('Content-Security-Policy', '')[:40]}…",
          st == 200 and "connect-src 'self'" in hd.get("Content-Security-Policy", "") and hd.get("Referrer-Policy") == "no-referrer")
    check("в странице манифест и значок для iPhone", b'rel="manifest"' in body and b"apple-touch-icon" in body)
    st, hd, body = raw_get(h, "/manifest.webmanifest")
    m = json.loads(body)
    check(f"манифест: {m['name']}, {m['display']}, {len(m['icons'])} значка", st == 200 and m["display"] == "standalone" and m["start_url"] == "/")
    st, hd, body = raw_get(h, "/sw.js")
    check("сервис-воркер отдаётся как JS", st == 200 and "javascript" in hd["Content-Type"] and b"fetch" in body)
    for n in (192, 512, 180):
        path = "/apple-touch-icon.png" if n == 180 else f"/icon-{n}.png"
        st, hd, body = raw_get(h, path)
        check(f"значок {path}: {Image.open(io.BytesIO(body)).size}", st == 200 and Image.open(io.BytesIO(body)).size == (n, n))

    # /link в боте: QR и ссылка с одноразовым кодом
    t0 = time.time()
    fb.on_text("/link", ADMIN)
    c = h.calls("sendPhoto", t0)
    cap = c[-1][2]["caption"] if c else ""
    link = c[-1][2]["reply_markup"]["inline_keyboard"][0][0]["url"] if c else ""
    check(f"/link прислал QR, ссылка в кнопке: {link[:35]}…", "#pair=" in link and c[-1][2]["_files"] == ["photo"])
    code = code_of(link)
    shown = re.search(r"<code>([A-Z0-9-]+)</code>", cap).group(1)
    check(f"в подписи короткий код {shown} (HTML, нажатием копируется)", shown.replace("-", "") == code and len(code) == 8
          and c[-1][2].get("parse_mode") == "HTML" and not set(code) & set("0O1IL"))

    # вписанный руками: строчными, с дефисом и пробелами
    r = post(h, "/api/pair", {"code": " " + shown.lower() + " ", "name": "iPhone · Safari <b>"}, want_headers=True)
    sc = r.get("_headers", {}).get("Set-Cookie", "")
    check(f"cookie с ключом: {sc.split('=')[0]}, HttpOnly, SameSite=Strict",
          "HttpOnly" in sc and "SameSite=Strict" in sc and "Secure" in sc and r.get("device", "x") in sc)
    check("привязка по коду дала ключ и сессию", r["_status"] == 200 and len(r.get("device", "")) >= 40 and r.get("token"))
    dev_key, dev_tok = r["device"], r["token"]
    r2 = post(h, "/api/pair", {"code": code, "name": "чужой"})
    check(f"код второй раз не срабатывает: {r2.get('error', '')[:50]}", r2["_status"] == 403)
    row = fb.q("SELECT * FROM devices")[0]
    check(f"на сервере хеш, а не ключ; имя очищено: {row['name']!r}",
          row["hash"] != dev_key and len(row["hash"]) == 64 and "<" not in row["name"] and row["owner"] == ADMIN)

    # приложение на экране «Домой» без localStorage, но с cookie из Safari
    r = post(h, "/api/auth", {"cookie": 1}, cookie=f"other=1; proyavka_device={dev_key}")
    check("вход по cookie отдаёт ключ в localStorage", r["_status"] == 200 and r.get("device") == dev_key and r.get("token"))
    r = post(h, "/api/auth", {"cookie": 1})
    check(f"без cookie — «не привязано»: {r.get('error')}", r["_status"] == 401)
    r = post(h, "/api/auth", {"cookie": 1}, cookie="proyavka_device=" + "x" * 43)
    check("чужая cookie не пускает", r["_status"] == 401)

    # вход ключом устройства и работа с лентой
    r = post(h, "/api/auth", {"device": dev_key})
    check("вход ключом устройства", r["_status"] == 200 and r.get("token"))
    dev_tok2 = r["token"]
    st, hd, body = raw_get(h, "/api/photos", dev_tok2)
    check(f"лента с устройства: {json.loads(body)['total']} кадра", st == 200 and json.loads(body)["total"] == 2)
    r = post(h, "/api/auth", {"device": dev_key, "token": dev_tok2})
    check("повторный вход продлевает тот же токен", r.get("token") == dev_tok2)
    devs = json.loads(raw_get(h, "/api/devices", dev_tok2)[2])["devices"]
    check(f"список устройств, текущее отмечено: {devs}", len(devs) == 1 and devs[0]["current"])
    devs = json.loads(raw_get(h, "/api/devices", tg_token)[2])["devices"]
    check("из Telegram то же устройство не «текущее»", len(devs) == 1 and not devs[0]["current"])

    # скачать файлом
    st, hd, body = raw_get(h, f"/img/full/{pids[0]}", dev_tok2)
    im = Image.open(io.BytesIO(body))
    check(f"полный размер файлом: {im.size}, {hd.get('Content-Disposition')}",
          st == 200 and im.size[0] >= 6000 and hd.get("Content-Disposition", "").startswith("attachment;"))
    st, hd, body = raw_get(h, f"/img/full/{pids[0]}?s={dev_tok2}")
    check("ссылкой ?s= тоже (для <a download>)", st == 200 and body[:2] == b"\xff\xd8")
    t = time.time()
    st, hd, body = raw_get(h, f"/api/zip?ids={pids[0]},{pids[1]},999999", dev_tok2)
    z = zipfile.ZipFile(io.BytesIO(body))
    names = z.namelist()
    check(f"архив за {time.time() - t:.1f} с: {names}", st == 200 and len(names) == 2 and z.testzip() is None
          and all(Image.open(z.open(n)).size[0] >= 6000 for n in names))
    check("временные файлы убраны", not list(fb.TMP.glob("zip_*")) and not list(fb.TMP.glob("dl_*")))

    # Боб: своё устройство из «Проявки», чужие кадры не видит
    hb = h.as_user(BOB)
    r = post(h, "/api/devices/new", {}, hb.token)
    check(f"Боб создал ссылку в «Проявке», QR есть: {r.get('url', '')[:40]}…", r["_status"] == 200 and r.get("qr", "").startswith("<svg"))
    rb = post(h, "/api/pair", {"code": code_of(r["url"]), "name": "Windows · Chrome"})
    bob_tok = rb["token"]
    check(f"язык Боба пришёл с привязкой: {rb.get('lang')}", rb.get("lang") == "en")
    st, hd, body = raw_get(h, "/api/photos", bob_tok)
    check("у Боба на устройстве пусто", json.loads(body)["total"] == 0)
    st, hd, body = raw_get(h, f"/img/full/{pids[0]}", bob_tok)
    check(f"чужой кадр Бобу не скачать: {st}", st == 404)
    st, hd, body = raw_get(h, f"/api/zip?ids={pids[0]}", bob_tok)
    check(f"и архивом тоже: {st}", st == 404)
    bob_dev = fb.q("SELECT id FROM devices WHERE owner=?", (BOB,))[0]["id"]
    r = post(h, f"/api/device/{bob_dev}/delete", {}, dev_tok2)
    check("чужое устройство не отключить", r.get("ok") is False and fb.q("SELECT 1 FROM devices WHERE id=?", (bob_dev,)))

    # бот: /devices и отключение кнопкой
    t0 = time.time()
    fb.on_text("/devices", ADMIN)
    msg = h.calls("sendMessage", t0)[-1][2]
    kb = msg["reply_markup"]["inline_keyboard"]
    check(f"/devices: {msg['text'].splitlines()[1]}", "iPhone" in msg["text"] and kb[0][0]["callback_data"] == f"dv:{row['id']}")
    fb.on_callback({"id": "1", "data": kb[0][0]["callback_data"], "message": {"message_id": 5}}, ADMIN)
    check("устройство отключено кнопкой", not fb.q("SELECT 1 FROM devices WHERE owner=?", (ADMIN,)))
    st, hd, body = raw_get(h, "/api/photos", dev_tok2)
    check(f"его сессия сразу закрыта: {st}", st == 401)
    r = post(h, "/api/auth", {"device": dev_key})
    check(f"ключ больше не пускает: {r.get('error')}", r["_status"] == 401)
    st, hd, body = raw_get(h, "/api/photos", tg_token)
    check("Telegram-сессия администратора живёт", st == 200)

    # просроченный код и подбор
    link = fb.make_pair(ADMIN)
    fb.run("UPDATE pairs SET exp=? WHERE code=?", (time.time() - 1, code_of(link)))
    check("просроченный код не работает", post(h, "/api/pair", {"code": code_of(link)})["_status"] == 403)
    t = time.time()
    codes = [post(h, "/api/pair", {"code": f"guess{i:020d}"}, ip="10.0.0.9")["_status"] for i in range(fb.FAIL_MAX + 1)]
    check(f"подбор: {fb.FAIL_MAX} неудач за {time.time() - t:.0f} с, потом 429", codes[:-1] == [403] * fb.FAIL_MAX and codes[-1] == 429)
    good = fb.make_pair(ADMIN)
    check("с того же адреса даже верный код ждёт", post(h, "/api/pair", {"code": code_of(good)}, ip="10.0.0.9")["_status"] == 429)
    check("с другого адреса — работает", post(h, "/api/pair", {"code": code_of(good)}, ip="10.0.0.10")["_status"] == 200)

    # подбор с множества адресов: общий предел
    fb.FAIL_MAX_ALL = len(fb.AUTH_FAILS["*"]) + 3
    st = [post(h, "/api/pair", {"code": "AAAAAAAA"}, ip=f"10.1.0.{i}")["_status"] for i in range(5)]
    check(f"общий предел неудач со всех адресов: {st}", st == [403, 403, 403, 429, 429])
    fb.FAIL_MAX_ALL = 10 ** 6

    # удаление пользователя убирает и его устройства
    fb.delete_user(BOB)
    check("Боб удалён — его устройства тоже", not fb.q("SELECT 1 FROM devices WHERE owner=?", (BOB,)))
    st, hd, body = raw_get(h, "/api/photos", bob_tok)
    check(f"сессия устройства Боба закрыта: {st}", st == 401)
    harness.done()
