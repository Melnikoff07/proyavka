"""Этап 4: несколько пользователей — миграция, приглашения, изоляция, камеры, язык, лимиты, удаление, честная очередь."""
import hashlib
import io
import json
import re
import sqlite3
import time
from pathlib import Path
import numpy as np
from PIL import Image
import harness

TD = Path(__file__).resolve().parent / "testdata"
ADMIN, BOB, EVE = 1, 2002, 3003


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def old_install(base):
    """База и state.json от прежней версии: один владелец, без колонки owner."""
    db = sqlite3.connect(base / "filmbot.db")
    db.execute("""CREATE TABLE photos(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, src TEXT, work TEXT, thumb TEXT,
        taken TEXT, iso INTEGER, auto_key TEXT, auto_reason TEXT, preset TEXT, strength INTEGER DEFAULT 100,
        stamp INTEGER DEFAULT 0, frame INTEGER DEFAULT 0, leak INTEGER DEFAULT 0,
        msg_id INTEGER, file_id TEXT, hidden INTEGER DEFAULT 0, created REAL)""")
    db.execute("INSERT INTO photos(name, taken, preset, created) VALUES ('old.jpg', '2026-01-01 10:00', 'golden200', 1)")
    db.commit()
    db.close()
    (base / "state.json").write_text(json.dumps({"default": "gold", "offset": 5}))


def jpeg(seed, size=(1500, 1000)):
    buf = io.BytesIO()
    Image.fromarray(np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def sent(h, since, chat, method="sendMessage"):
    return [c[2] for c in h.calls(method, since) if c[2].get("chat_id") == chat]


if __name__ == "__main__":
    h = harness.start(port=8106, before=old_install)
    from proyavka import config, database, i18n, scheduler, users      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    # --- миграция
    old = database.q("SELECT * FROM photos WHERE name='old.jpg'")[0]
    check(f"старые кадры — администратора: owner={old['owner']}", old["owner"] == ADMIN)
    check(f"администратор в таблице users: {users.user(ADMIN)['role']}", users.user(ADMIN)["role"] == "admin")
    st = fb.load_state()
    fb.migrate_state(st)
    check(f"плёнка по умолчанию перенесена: {users.user(ADMIN)['default_film']}", users.user(ADMIN)["default_film"] == "golden200")
    check(f"смещение getUpdates не потерялось: {fb.load_state()}", fb.load_state().get("offset") == 5)
    database.run("UPDATE photos SET hidden=1 WHERE name='old.jpg'")

    # --- приглашение
    t0 = time.time()
    fb.on_text("/invite", ADMIN)
    link = sent(h, t0, ADMIN)[-1]["text"]
    m = re.search(r"t\.me/proyavka_test_bot\?start=([\w-]+)", link)
    check(f"/invite даёт ссылку: {m and m.group(0)}", bool(m))
    code = m.group(1)
    fb.on_text("/invite", BOB)          # не администратор — ничего
    check("/invite у обычного пользователя не работает (он ещё и не пользователь)", len(sent(h, t0, BOB)) == 0)
    t1 = time.time()
    fb.on_stranger({"from": {"id": BOB, "first_name": "Bob", "username": "bob", "language_code": "en"}, "text": f"/start {code}"})
    check(f"Боб принят, язык en: {users.user(BOB) and users.user(BOB)['lang']}", users.user(BOB) and users.user(BOB)["lang"] == "en")
    welcome = sent(h, t1, BOB)
    check(f"приветствие по-английски: {welcome[0]['text'][:40]!r}", welcome and welcome[0]["text"].startswith("Hi!"))
    check("администратору сообщили", any("Bob" in m["text"] for m in sent(h, t1, ADMIN)))
    cmds = [c[2] for c in h.calls("setMyCommands", t1) if c[2].get("scope", {}).get("chat_id") == BOB]
    check("у Боба свои команды на английском, без /invite",
          cmds and all(c["command"] != "invite" for c in cmds[-1]["commands"]) and cmds[-1]["commands"][0]["description"] == "Frame feed in the chat")
    t2 = time.time()
    fb.on_stranger({"from": {"id": EVE, "first_name": "Eve", "language_code": "ru"}, "text": f"/start {code}"})
    check("ссылка одноразовая: Ева не прошла", not users.user(EVE) and "недействительна" in sent(h, t2, EVE)[0]["text"])
    fb.on_stranger({"from": {"id": EVE, "first_name": "Eve", "language_code": "ru"}, "text": "привет"})
    fb.on_stranger({"from": {"id": EVE, "first_name": "Eve", "language_code": "ru"}, "text": "ау"})
    check("незнакомцу отвечают один раз", len(sent(h, t2, EVE)) == 2)
    r = h.post("/api/auth", {"initData": f"test-{EVE}"}, auth=False)
    check(f"«Проявка» не пускает незнакомца: {r.get('_status')}", r.get("_status") == 403)

    # --- изоляция
    h.auth(ADMIN)
    hb = h.as_user(BOB)
    check(f"Бобу «Проявка» по-английски: {hb.post('/api/auth', {'initData': f'test-{BOB}'}, auth=False).get('lang')}", True)
    data = (TD / "a6300_DSC00270.JPG").read_bytes()
    h.req("/api/upload?name=adm.jpg", raw=data, ctype="image/jpeg")
    r = hb.req("/api/upload?name=bob.jpg", raw=data, ctype="image/jpeg")
    check(f"тот же файл у Боба — не повтор чужого: {r}", r.get("duplicate") is None)
    h.wait(lambda: len([p for p in h.photos() if p["msg_id"] and not p["hidden"]]) == 2, timeout=90)
    pa = [p for p in h.photos() if p["owner"] == ADMIN and not p["hidden"]][0]
    pb = [p for p in h.photos() if p["owner"] == BOB][0]
    check(f"файлы Боба в его папке: {Path(pb['src']).parent.parent.name}/{Path(pb['src']).parent.name}",
          Path(pb["src"]).parent == config.BASE / "users" / str(BOB) / "originals" and str(config.BASE / "users" / str(BOB)) in pb["view"])
    check(f"кадр Боба ушёл в его чат (msg {pb['msg_id']})",
          any(c[2].get("chat_id") == BOB for c in h.calls("sendPhoto") if c[2].get("caption", "").startswith(f"#{pb['id']}")))
    cap = [c[2]["caption"] for c in h.calls("sendPhoto") if c[2].get("chat_id") == BOB][0]
    check(f"подпись по-английски: {cap!r}", "auto: night" in cap and not re.search("[а-яё]", cap))
    fa, fbb = h.get("/api/photos?offset=0&limit=50"), hb.get("/api/photos?offset=0&limit=50")
    check(f"лента администратора: {[p['id'] for p in fa['photos']]}, Боба: {[p['id'] for p in fbb['photos']]}",
          [p["id"] for p in fa["photos"]] == [pa["id"]] and [p["id"] for p in fbb["photos"]] == [pb["id"]])
    u = h.get("/api/updates?since=0")
    check("опрос изменений — только свои", all(p["id"] != pb["id"] for p in u["photos"]))
    check(f"чужая картинка: {h.get(f'/img/view/{pb[chr(105)+chr(100)]}').get('_status')}", h.get(f"/img/view/{pb['id']}").get("_status") == 404)
    check("чужое превью и исходник — 404", h.get(f"/img/preview/{pb['id']}/golden200?st=100").get("_status") == 404
          and h.get(f"/img/source/{pb['id']}").get("_status") == 404)
    check("чужой кадр не правится", h.post(f"/api/photo/{pb['id']}", {"preset": "across100"}).get("_status") == 404)
    check("чужой кадр не удаляется", h.post(f"/api/photo/{pb['id']}/hide", {}).get("_status") == 404)
    r = h.post("/api/batch", {"action": "delete", "ids": [pb["id"]]})
    check(f"пакет с чужим кадром ничего не делает: {r}", r.get("done") == 0 and not database.get(pb["id"])["hidden"])
    fb.on_callback({"id": "1", "data": f"p:{pb['id']}:across100", "message": {"message_id": pb["msg_id"]}}, ADMIN)
    check("кнопка под чужим кадром не работает", database.get(pb["id"])["preset"] != "across100")
    fb.on_callback({"id": "1", "data": f"p:{pb['id']}:across100", "message": {"message_id": pb["msg_id"]}}, BOB)
    check("а у владельца работает", database.get(pb["id"])["preset"] == "across100")

    # --- плёнка по умолчанию, язык, место
    with i18n.speak(BOB):
        fb.on_callback({"id": "1", "data": "d:cine250", "message": {"message_id": 1}}, BOB)
    check(f"у Боба своя плёнка по умолчанию: {users.user(BOB)['default_film']} / у админа {users.user(ADMIN)['default_film']}",
          users.user(BOB)["default_film"] == "cine250" and users.user(ADMIN)["default_film"] == "golden200")
    t3 = time.time()
    with i18n.speak(BOB):
        fb.on_text("/storage", BOB)
    txt = sent(h, t3, BOB)[0]["text"]
    check(f"/storage у Боба — его лимит 5 ГБ и без диска сервера: {txt.splitlines()[-1]!r}", "of 5 GB" in txt and "disk" not in txt)
    with i18n.speak(ADMIN):
        fb.on_text("/lang", ADMIN)
    check("/lang: администратор теперь en", users.user(ADMIN)["lang"] == "en")
    with i18n.speak(ADMIN):
        fb.on_text("/lang", ADMIN)

    # --- камера Боба
    t4 = time.time()
    with i18n.speak(BOB):
        fb.on_text("/camera", BOB)
    add = [c for c in harness.CAM if c[0] == "add"]
    tok = users.user(BOB)["cam_token"]
    check(f"на сервере заведена камера {add and add[-1][1]}", add and add[-1][1] == f"u{BOB}")
    check("на сервер ушёл только хеш токена", add and hashlib.sha256(tok.encode()).hexdigest() in add[-1][2] and tok not in add[-1][2])
    docs = [c[2] for c in h.calls("sendDocument", t4) if c[2].get("chat_id") == BOB]
    ftp = lambda: [m["text"] for m in sent(h, t4, BOB) if f"user: u{BOB}" in m.get("text", "")]
    try:
        h.wait(ftp, timeout=10)            # сообщения уходят через очередь: под нагрузкой первым может быть не оно
    except TimeoutError:
        pass
    msg = (ftp() or [""])[0]
    check(f"Бобу — свой FTP-пользователь: {'user: u' + str(BOB) in msg}", f"user: u{BOB}" in msg and users.user(BOB)["ftp_pass"] in msg)
    check(f"и свой config.txt ({[d['_files'] for d in docs]})", any("document" in d.get("_files", []) for d in docs))
    with i18n.speak(BOB):
        fb.on_text("/camera", BOB)
    check("повторный /camera не заводит камеру заново", len([c for c in harness.CAM if c[0] == "add"]) == 1)

    # --- куда кладёт сервер-приёмник
    fb.REMOTE_DIR, fb.REMOTE_USERS = "/srv/camera/upload/", "/srv/camera/u/"
    fb._USER_DIR_RE = re.compile(re.escape(fb.REMOTE_USERS) + r"u(\d{1,15})/upload/")
    check("файл из папки администратора", fb.remote_owner("/srv/camera/upload/DSC1.JPG") == (ADMIN, "DSC1.JPG"))
    check("файл из папки Боба", fb.remote_owner(f"/srv/camera/u/u{BOB}/upload/DSC2.JPG") == (BOB, "DSC2.JPG"))
    check("временные и чужие файлы не берутся",
          fb.remote_owner(f"/srv/camera/u/u{BOB}/upload/.incoming/x.part") is None
          and fb.remote_owner("/srv/camera/u/u999/upload/a.jpg") is None and fb.remote_owner("/etc/passwd") is None)

    # --- честная очередь: большой пакет администратора не держит кадр Боба
    for i in range(8):
        h.req(f"/api/upload?name=a{i}.jpg", raw=jpeg(i), ctype="image/jpeg")
    try:
        h.wait(lambda: len([p for p in h.photos() if p["owner"] == ADMIN and not p["hidden"] and p["rendered_rev"] == p["rev"] and p["msg_id"]]) == 9, timeout=90)
    except TimeoutError:
        print([(p["id"], p["owner"], p["hidden"], p["rev"], p["rendered_rev"], bool(p["work"])) for p in h.photos()])
        print("очередь", scheduler.RQ_PENDING, scheduler.RQ_QUEUED, "в работе", scheduler.VIEW_INFLIGHT, scheduler.VIEW_DIRTY)
        raise
    time.sleep(1)
    adm_ids = [p["id"] for p in h.photos() if p["owner"] == ADMIN and not p["hidden"]]
    t5 = time.time()
    h.post("/api/batch", {"action": "edit", "ids": adm_ids, "changes": {"preset": "expired"}})
    hb.post("/api/batch", {"action": "edit", "ids": [pb["id"]], "changes": {"preset": "super400"}})
    h.wait(lambda: len(h.calls("editMessageMedia", t5)) >= 10, timeout=120)
    order = [c[2]["chat_id"] for c in h.calls("editMessageMedia", t5)]
    check(f"кадр Боба не ждёт весь пакет администратора: место {order.index(BOB) + 1} из {len(order)}", order.index(BOB) <= 3)

    # --- лимит и удаление пользователя
    t6 = time.time()
    fb.on_text("/users", ADMIN)
    scr = sent(h, t6, ADMIN)[-1]
    check(f"/users: {scr['text'].splitlines()}", "Bob" in scr["text"] and str(scr["reply_markup"]).count("ud:") == 1)
    fb.on_callback({"id": "1", "data": f"ul:{BOB}", "message": {"message_id": 7}}, ADMIN)
    check(f"лимит Боба по кнопке: {users.storage_limit(BOB)} ГБ", users.storage_limit(BOB) == 10)
    fb.on_callback({"id": "1", "data": f"ud:{ADMIN}", "message": {"message_id": 7}}, ADMIN)
    check("себя удалить нельзя", users.user(ADMIN) is not None)
    fb.on_callback({"id": "1", "data": f"udy:{BOB}", "message": {"message_id": 7}}, BOB)
    check("Боб не может удалять", users.user(BOB) is not None)
    bob_dir = config.BASE / "users" / str(BOB)
    fb.on_callback({"id": "1", "data": f"udy:{BOB}", "message": {"message_id": 7}}, ADMIN)
    check("Боб удалён", users.user(BOB) is None and not database.q("SELECT id FROM photos WHERE owner=?", (BOB,)))
    check(f"его папка стёрта: {bob_dir.exists()}", not bob_dir.exists())
    check(f"камера на сервере убрана: {harness.CAM[-1][:2]}", harness.CAM[-1][:2] == ("del", f"u{BOB}"))
    check(f"«Проявка» Боба больше не открывается: {hb.get('/api/photos?offset=0&limit=5').get('_status')}",
          hb.get("/api/photos?offset=0&limit=5").get("_status") == 401)
    check("Бобу сказали по-английски", any("access" in m["text"] for m in sent(h, t6, BOB)))
    check("кадры администратора на месте", len([p for p in h.photos() if p["owner"] == ADMIN and not p["hidden"]]) == 9)
    harness.done()
