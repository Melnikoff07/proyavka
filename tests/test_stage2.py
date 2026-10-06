"""Этап 2: выбор и удаление: пачкой из «Проявки», из чата с подтверждением, старые сообщения, повторная выгрузка."""
import os
import time
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    h = harness.start(port=8103)
    from proyavka import config, database, users      # после start: настройки читаются из окружения при импорте
    h.auth()
    files = sorted(TD.glob("*.JPG"))
    for f in files:
        h.drop(f)
    phs = h.wait(lambda: (lambda r: r if len(r) == 3 and all(p["msg_id"] and p["view"] for p in r) else None)(h.photos()), timeout=120)
    a, b, c = phs
    # кадр «c» как будто отправлен 3 дня назад: удалить сообщение уже нельзя
    database.run("UPDATE photos SET msg_at=? WHERE id=?", (time.time() - 3 * 86400, c["id"]))

    t0 = time.time()
    r = h.post("/api/batch", {"action": "delete", "ids": [a["id"], c["id"], 99999]})
    check(f"пакетное удаление: {r}", r.get("done") == 2)
    h.wait(lambda: h.calls("deleteMessages", t0) and h.calls("editMessageMedia", t0), timeout=20)
    dm = h.calls("deleteMessages", t0)
    check(f"свежее сообщение удалено пачкой: {dm[0][2].get('message_ids')}", dm[0][2].get("message_ids") == [a["msg_id"]])
    em = [x for x in h.calls("editMessageMedia", t0) if x[2].get("message_id") == c["msg_id"]]
    check("старое сообщение заменено заглушкой", bool(em))
    rows = {p["id"]: p for p in h.photos()}
    check("в базе скрыты, отпечаток остался",
          all(rows[i]["hidden"] == 1 and rows[i]["deleted_at"] and rows[i]["fp"] for i in (a["id"], c["id"])))
    kept = [p for p in (a["src"], a["work"], a["view"], a["thumb"]) if p and os.path.exists(p)]
    check(f"файлы остались в корзине на диске: {len(kept)} из 4", len(kept) == 4)
    check("у удалённого сообщения забыт msg_id, у старого (заглушка) — нет",
          h.wait(lambda: rows and database.get(a["id"])["msg_id"] is None, timeout=10) and database.get(c["id"])["msg_id"] == c["msg_id"])
    check("в ленте остался один кадр", h.get("/api/photos?offset=0&limit=10").get("total") == 1)

    # повторная выгрузка удалённого: и с камеры, и через «+»
    h.drop(files[0], "again_" + files[0].name)
    r = h.req("/api/upload?name=x.jpg", raw=files[2].read_bytes(), ctype="image/jpeg")
    check(f"«+» удалённого кадра: {r}", r.get("deleted") is True)
    h.wait(lambda: not any(config.INCOMING.iterdir()), timeout=60)
    time.sleep(2)
    check(f"удалённое не вернулось (кадров в базе {len(h.photos())})", len(h.photos()) == 3)

    # удаление из чата: сначала спрашивает
    t1 = time.time()
    cb = {"id": "1", "data": f"del:{b['id']}", "message": {"message_id": b["msg_id"]}}
    h.fb.on_callback(cb, 1)
    kb = h.calls("editMessageReplyMarkup", t1)
    check("«Удалить» в чате спрашивает подтверждение",
          kb and "dely:" in str(kb[-1][2].get("reply_markup")) and not database.get(b["id"])["hidden"])
    h.fb.on_callback(dict(cb, data=f"dely:{b['id']}"), 1)
    check("«Да, удалить» удаляет", database.get(b["id"])["hidden"] == 1)
    h.wait(lambda: [x for x in h.calls("deleteMessages", t1) if x[2].get("message_ids") == [b["msg_id"]]], timeout=10)
    check("сообщение удалено из чата", True)
    r = h.post("/api/batch", {"action": "delete", "ids": [b["id"]]})
    check(f"повторное удаление ничего не ломает: {r}", r.get("done") == 0 or r.get("_status") == 400)

    # корзина в чате и возврат
    t2 = time.time()
    h.fb.on_text("/trash", 1)
    tr = [x for x in h.calls("sendPhoto", t2) if "Корзина" in str(x[2].get("caption"))]
    check(f"/trash показывает корзину: {tr and tr[0][2]['caption'][:20]}", bool(tr) and f"r:{a['id']}" in str(tr[0][2]["reply_markup"]))
    t3 = time.time()
    h.fb.on_callback({"id": "1", "data": f"r:{a['id']}", "message": {"message_id": 555}}, 1)
    check("кадр вернулся в ленту", not database.get(a["id"])["hidden"] and h.get("/api/photos?offset=0&limit=10").get("total") == 1)
    h.wait(lambda: [x for x in h.calls("sendPhoto", t3) if str(x[2].get("caption", "")).startswith(f"#{a['id']}")], timeout=30)
    check(f"и снова пришёл в чат новым сообщением (msg {database.get(a['id'])['msg_id']})", database.get(a["id"])["msg_id"])
    t4 = time.time()
    h.fb.on_callback({"id": "1", "data": f"r:{c['id']}", "message": {"message_id": 555}}, 1)
    h.wait(lambda: [x for x in h.calls("editMessageMedia", t4) if x[2].get("message_id") == c["msg_id"]], timeout=30)
    check("старый кадр вернулся на место заглушки", True)

    # нехватка места: корзина чистится первой
    h.post("/api/batch", {"action": "delete", "ids": [a["id"]]})
    ph_a = database.get(a["id"])
    total = sum(h.fb.user_usage(1).values())
    binned = [p for p in h.photos() if p["hidden"] and p["work"]]
    trash = sum(os.path.getsize(f) for p in binned for f in (p["src"], p["work"], p["view"], p["thumb"]) if f and os.path.exists(f))
    first = min(binned, key=lambda p: p["deleted_at"])
    print(f"     в корзине {[p['id'] for p in binned]}, раньше всех удалён #{first['id']}")
    users.set_user(1, storage_gb=(total - trash + 1000) / 1e9)   # не хватает ровно на всю корзину
    live_before = [p for p in h.photos() if not p["hidden"] and p["src"]]
    h.fb.cleanup()
    live_after = [p for p in h.photos() if not p["hidden"] and p["src"]]
    check(f"оригиналы живых кадров не тронуты: {len(live_after)} из {len(live_before)}", len(live_after) == len(live_before) > 0)
    ph_a = database.get(a["id"])
    gone = [p for p in (a["src"], a["work"], a["view"], a["thumb"]) if p and os.path.exists(p)]
    check(f"при нехватке места корзина стёрта первой: файлов осталось {len(gone)}", not gone and not ph_a["work"])
    users.set_user(1, storage_gb=None)
    h.fb.on_text("/trash", 1)
    harness.done()
