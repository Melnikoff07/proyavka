"""Свои плёнки: параметры проверяются и ограничиваются, редактор рисует живой просмотр, плёнка сохраняется и применяется
к кадру, правка перерисовывает, код для обмена, каталог сообщества, чужие плёнки недоступны, удаление."""
import io
import base64
import json
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


def mean_color(b):
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.float32).mean(axis=(0, 1))


if __name__ == "__main__":
    h = harness.start(port=8117, extra_env={"COMMUNITY_URL": ""})      # без сети: каталог из репозитория
    from proyavka import database, film      # после start: настройки читаются из окружения при импорте
    fb = h.fb
    h.auth(1)
    for i in range(2):
        h.drop(frame(i, f"L{i:04d}.JPG"))
    h.wait(lambda: len([p for p in h.photos() if p["view"]]) == 2, timeout=120)
    pid = h.photos()[0]["id"]

    # --- проверка параметров ---
    p = film.clean_params({"contrast": 5, "grain": -1, "lift": [9, 0, 0], "bw": [1, 1, 2], "evil": "x"})
    check("лишнее отброшено, числа в пределах", "evil" not in p and p["contrast"] == 1.0 and p["grain"] == 0.0 and p["lift"][0] == 0.15)
    check(f"ч/б-веса приведены к сумме 1: {p['bw']}", abs(sum(p["bw"]) - 1) < 1e-3)
    for bad in ({"contrast": "много"}, {"lift": [1, 2]}, {"gamma": "abc"}, {"contrast": float("nan")}, [], None):
        try:
            film.clean_params(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"битые параметры отклонены: {str(bad)[:30]}", ok)
    check("встроенные плёнки проходят проверку без изменений",
          all(film.clean_params(v)["contrast"] == round(v["contrast"], 4) for v in film.PRESETS.values()))

    # --- список плёнок: у встроенных есть основа для редактора ---
    pr = h.get("/api/presets")["presets"]
    check("у встроенных плёнок есть параметры", all("p" in x for x in pr if x["key"] != "original" and not x.get("lut")))

    # --- живой просмотр ---
    base = next(x for x in pr if x["key"] == "street_neg")["p"]
    warm = dict(base, shadow_tint=[0.08, 0.0, -0.08], high_tint=[0.08, 0.0, -0.08], sat=1.6)
    cold = dict(base, shadow_tint=[-0.08, 0.0, 0.08], high_tint=[-0.08, 0.0, 0.08], sat=1.6)
    a = h.post("/api/look/try", {"id": pid, "params": warm})
    b = h.post("/api/look/try", {"id": pid, "params": cold})
    ma, mb = (mean_color(a), mean_color(b)) if isinstance(a, bytes) and isinstance(b, bytes) else (None, None)
    check("просмотр приходит картинкой", isinstance(a, bytes) and a[:3] == b"\xff\xd8\xff")
    check(f"тёплая плёнка краснее холодной ({None if ma is None else ma.round(0)} / {None if mb is None else mb.round(0)})",
          ma is not None and (ma[0] - ma[2]) > (mb[0] - mb[2]) + 10)
    check("чужой кадр для просмотра недоступен", h.post("/api/look/try", {"id": 999999, "params": warm}).get("_status") == 400)
    check("битые параметры в просмотре — 400", h.post("/api/look/try", {"id": pid, "params": {"sat": "x"}}).get("_status") == 400)

    # --- сохранение и применение ---
    r = h.post("/api/look", {"name": "Моя <плёнка>", "params": warm})
    key = r["key"]
    check(f"плёнка сохранена: {key}, имя очищено: {r['name']!r}", key.startswith("lut") and r["name"] == "Моя плёнка")
    items = h.get("/api/presets")["presets"]
    mine = next((x for x in items if x["key"] == key), None)
    check("в списке: своя, редактируемая", mine and mine.get("look") and mine.get("lut"))
    got = h.get(f"/api/look/{key}")
    check("параметры читаются для редактора", got["name"] == "Моя плёнка" and abs(got["params"]["sat"] - 1.6) < 1e-6)
    h.post(f"/api/photo/{pid}", {"preset": key})
    h.wait(lambda: not database.get(pid)["rev"] or database.get(pid)["rev"] == database.get(pid)["rendered_rev"], timeout=60)
    v1 = Path(database.get(pid)["view"]).read_bytes()
    img1 = mean_color(v1)
    check(f"кадр проявлен своей плёнкой: теплее нейтрального ({img1.round(0)})", img1[0] - img1[2] > 5)

    # --- правка перерисовывает кадры с этой плёнкой ---
    rev0 = database.get(pid)["rev"]
    e = h.post("/api/look", {"key": key, "name": "Холодная", "params": cold})
    check("правка принята, кадр поставлен на перерисовку", e["name"] == "Холодная" and e["redrawn"] == 1)
    h.wait(lambda: database.get(pid)["rev"] > rev0 and database.get(pid)["rev"] == database.get(pid)["rendered_rev"], timeout=60)
    img2 = mean_color(Path(database.get(pid)["view"]).read_bytes())
    check(f"теперь холоднее ({img1[0] - img1[2]:.0f} → {img2[0] - img2[2]:.0f})", img2[0] - img2[2] < img1[0] - img1[2] - 10)
    check("название обновилось в списке", any(x["key"] == key and x["name"] == "Холодная" for x in h.get("/api/presets")["presets"]))

    # --- обычный LUT не путается с плёнкой ---
    cube = "LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n"
    lut = h.req("/api/upload?lut=1&name=id.cube", raw=cube.encode(), ctype="application/octet-stream")
    check("LUT-файл по-прежнему загружается", lut.get("key", "").startswith("lut"))
    check("у LUT нет параметров плёнки", h.get(f"/api/look/{lut['key']}").get("_status") == 400)
    lut_item = next(x for x in h.get("/api/presets")["presets"] if x["key"] == lut["key"])
    check("в списке LUT без пометки «плёнка»", lut_item.get("lut") and not lut_item.get("look"))

    # --- чужая плёнка ---
    h.add_user(2)
    h2 = h.as_user(2)
    check("чужую плёнку не прочитать", h2.get(f"/api/look/{key}").get("_status") == 400)
    check("чужую плёнку не изменить", h2.post("/api/look", {"key": key, "name": "x", "params": warm}).get("_status") == 400)
    check("чужую плёнку не поделить", h2.get(f"/api/look/{key}/share").get("_status") == 400)
    check("чужая плёнка не ставится кадру", h2.post("/api/batch", {"action": "edit", "ids": [pid], "changes": {"preset": key}}).get("_status") == 400)

    # --- код для обмена ---
    s = h.get(f"/api/look/{key}/share")
    check("код с префиксом", s["code"].startswith("proyavka-look:1:"))
    check("ссылка «Предложить» ведёт в GitHub с кодом", s["suggest_url"].startswith("https://github.com/") and "issues/new" in s["suggest_url"]
          and "proyavka-look" in s["suggest_url"])
    check("для отправки есть контакт автора и название", s["contact"] == "Sashkere" and s["name"] == "Холодная")
    check("своя плёнка не помечена как из сообщества", next(x for x in h.get("/api/presets")["presets"] if x["key"] == key).get("community") is False)
    imp = h2.post("/api/look/import", {"code": s["code"]})
    check(f"код импортируется у другого: {imp.get('name')}", imp.get("name") == "Холодная")
    h2got = h2.get(f"/api/look/{imp['key']}")
    check("параметры дошли без искажений", h2got["params"] == got["params"] or abs(h2got["params"]["sat"] - 1.6) < 1e-6)
    check("мусорный код отклонён", h2.post("/api/look/import", {"code": "proyavka-look:1:!!!"}).get("_status") == 400
          and h2.post("/api/look/import", {"code": "привет"}).get("_status") == 400)
    evil = "proyavka-look:1:" + base64.urlsafe_b64encode(json.dumps({"name": "<script>x</script>", "p": {"contrast": 99, "sat": 1}}).encode()).decode()
    ev = h2.post("/api/look/import", {"code": evil})
    check("чужой код: имя очищено, числа ограничены", "<" not in ev["name"] and h2.get(f"/api/look/{ev['key']}")["params"]["contrast"] == 1.0)

    # --- каталог сообщества (копия из репозитория) ---
    c = h.get("/api/community")
    check(f"каталог: {len(c['looks'])} плёнок", len(c["looks"]) >= 3 and all(x["name"] and x["h"] for x in c["looks"]))
    check("каталог не рисует превью сам: образца-кадра в ответе нет", "sample" not in c)
    first = c["looks"][0]
    pv = h.get(f"/img/look/{first['id']}?p={pid}")
    check("превью плёнки каталога — картинка на моём кадре", isinstance(pv, bytes) and pv[:3] == b"\xff\xd8\xff")
    check("превью на чужом кадре — 404", h2.get(f"/img/look/{first['id']}?p={pid}").get("_status") == 404)
    check("несуществующая плёнка — 404", h.get(f"/img/look/net-takoy?p={pid}").get("_status") == 404)
    ad = h.post("/api/community/add", {"id": first["id"]})
    check(f"добавлена из каталога: {ad.get('name')}", ad.get("key", "").startswith("lut") and not ad.get("again"))
    ad2 = h.post("/api/community/add", {"id": first["id"]})
    check("повторно не дублируется", ad2.get("again") and ad2["key"] == ad["key"])
    check("плёнка из каталога помечена: предлагать её обратно не нужно",
          next(x for x in h.get("/api/presets")["presets"] if x["key"] == ad["key"]).get("community") is True)
    check("в каталоге помечена как добавленная", next(x for x in h.get("/api/community")["looks"] if x["id"] == first["id"])["installed"] == ad["key"])
    check("неизвестная плёнка каталога — 400", h.post("/api/community/add", {"id": "net-takoy"}).get("_status") == 400)

    # --- готовые картинки каталога и «до/после» ---
    def size(b):
        return Image.open(io.BytesIO(b)).size
    cm_img = h.get(f"/img/cm/{first['id']}")
    check(f"картинка плёнки каталога отдаётся готовой: {size(cm_img) if isinstance(cm_img, bytes) else cm_img}", isinstance(cm_img, bytes) and max(size(cm_img)) == 900)
    bf = h.get(f"/img/cm/{first['id']}-before")
    sm = h.get("/img/cm/sample")
    check("исходник под неё — по умолчанию общий образец", isinstance(bf, bytes) and bf == sm and max(size(sm)) == 1400)
    check("чужое имя и обход пути — 404", all(h.get(f"/img/cm/{n}").get("_status") == 404 for n in ("net-takoy", "net-takoy-before", "..%2f..%2fetc", "WARM")))
    check("образец — без метаданных (место и время съёмки не раскрываются)", not Image.open(io.BytesIO(sm)).getexif())
    o1 = h.get(f"/img/orig/{pid}?e=1000")
    o16 = h.get(f"/img/orig/{pid}?e=1600")
    check(f"кадр без плёнки для «до/после»: {size(o1)} и {size(o16)}", max(size(o1)) == 900 and max(size(o16)) == 900 and size(o1) == size(o16))      # кадры этого теста 900 px: увеличивать нечего
    check("размер вне списка — 404", h.get(f"/img/orig/{pid}?e=777").get("_status") == 404 and h.get(f"/img/look/{first['id']}?p={pid}&e=5000").get("_status") == 404)
    big = h.get(f"/img/look/{first['id']}?p={pid}&e=1000")
    check(f"плёнка каталога на моём кадре крупно: {size(big)}", max(size(big)) == 900 and size(big) == size(o1))
    check("чужой кадр без плёнки не отдаётся", h2.get(f"/img/orig/{pid}?e=1000").get("_status") == 404)
    check("без входа — 401", h.req(f"/img/orig/{pid}?e=420", auth=False).get("_status") == 401)

    # --- битый каталог из сети не ломает ничего ---
    raw = {"looks": [{"id": "ok-one", "name": "Ok", "p": {"contrast": 0.3}},
                     {"id": "BAD ID", "name": "x", "p": {}}, {"id": "no-params", "name": "x"},
                     {"id": "bad-num", "name": "x", "p": {"sat": "abc"}}, "мусор", None]}
    ents = fb._community_entries(raw)
    check(f"в каталоге остаются только годные записи: {[e['id'] for e in ents]}", [e["id"] for e in ents] == ["ok-one"])

    # --- лимит и удаление ---
    d = h.post(f"/api/lut/{key}/delete")
    check("удаление плёнки: кадр вернулся на автоплёнку", d.get("ok") and database.get(pid)["preset"] != key)
    check("файлы стёрты", not (film.lut_dir(1) / f"{key}.json").exists())
    check("после удаления её нет в списке", all(x["key"] != key for x in h.get("/api/presets")["presets"]))
    harness.done()
