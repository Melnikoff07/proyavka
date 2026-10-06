"""Корзина: «Удалить навсегда» — по одному и всю сразу; только свои и только то, что уже в корзине."""
import os
from pathlib import Path
import harness

TD = Path(__file__).resolve().parent / "testdata"


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


if __name__ == "__main__":
    h = harness.start(port=8121)
    from proyavka import database      # после start: настройки читаются из окружения при импорте
    h.auth()
    for f in sorted(TD.glob("*.JPG"))[:3]:
        h.drop(f)
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 3, timeout=180)
    a, b, c = [p["id"] for p in h.photos()]
    files = {p["id"]: [p[k] for k in ("src", "work", "view", "thumb") if p[k]] for p in h.photos()}

    h.add_user(2)
    h2 = h.as_user(2)
    check("кадр не из корзины удалить навсегда нельзя", h.post("/api/trash/purge", {"ids": [a]}).get("purged") == 0
          and all(os.path.exists(f) for f in files[a]))
    h.post("/api/batch", {"action": "delete", "ids": [a, b]})
    check("чужую корзину не тронуть", h2.post("/api/trash/purge", {"ids": [a, b]}).get("purged") == 0
          and h2.post("/api/trash/purge", {"all": True}).get("purged") == 0 and all(os.path.exists(f) for f in files[a]))
    check("мусор в списке — 400", h.post("/api/trash/purge", {"ids": ["x"]}).get("_status") == 400
          and h.post("/api/trash/purge", {}).get("_status") == 400)

    r = h.post("/api/trash/purge", {"ids": [a]})
    check(f"один кадр — навсегда: {r}", r.get("purged") == 1 and not any(os.path.exists(f) for f in files[a]))
    check("в корзине остался второй", [p["id"] for p in h.get("/api/trash")["photos"]] == [b])
    check("вернуть удалённый навсегда нельзя", h.post(f"/api/photo/{a}/restore").get("_status") == 400)

    h.post("/api/batch", {"action": "delete", "ids": [c]})
    r = h.post("/api/trash/purge", {"all": True})
    check(f"вся корзина — навсегда: {r}", r.get("purged") == 2 and not h.get("/api/trash")["photos"]
          and not any(os.path.exists(f) for i in (b, c) for f in files[i]))
    check("строки с отпечатками остались (тот же кадр не вернётся повторной выгрузкой)",
          len(database.q("SELECT id FROM photos WHERE fp IS NOT NULL AND id IN (?,?,?)", (a, b, c))) == 3)
    harness.done()
