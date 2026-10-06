"""Движок плёнок: старые плёнки не изменились (сверка с эталоном), цветовые полосы двигают только свои цвета, зерно в тенях,
свечение в линейном свете, параметры и коды обмена. Стенд не нужен — только функции обработки."""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.update(CHAT_ID="1", BASE_DIR=tempfile.mkdtemp())
sys.path.insert(0, str(HERE.parent / "bot"))
sys.path.insert(0, str(HERE))
import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402
import synth                            # noqa: E402
from proyavka import film               # noqa: E402


def check(name, ok):
    print(("OK   " if ok else "FAIL ") + name)
    return ok


def hue_of(rgb):
    r, g, b = [float(x) for x in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d < 1e-6:
        return None
    h = ((g - b) / d) % 6 if mx == r else (b - r) / d + 2 if mx == g else (r - g) / d + 4
    return h * 60


def lab_l(rgb):
    return float(np.dot(rgb, [0.2126, 0.7152, 0.0722]))


def sat_of(rgb):
    mx, mn = max(rgb), min(rgb)
    return 0 if mx == 0 else (mx - mn) / mx


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


FLAT = dict(contrast=0.0, lift=(0, 0, 0), top=1.0, shoulder=0.95, sat=1.0, grain=0.0, halation=0.0, bloom=0.0, soften=0.0, vignette=0.0)


def flat(**kw):
    return film.clean_params(dict(FLAT, **kw))


def lut_apply(p, rgb):
    """Что плёнка с параметрами p делает с одним цветом (через ту же функцию, что запекается в LUT)."""
    return film.color_fn(np.array([rgb], dtype=np.float32), p)[0]


if __name__ == "__main__":
    # --- движок по-прежнему воспроизводит старые плёнки (v1) точь-в-точь ---
    ref = np.load(HERE / "film_ref_v1.npz")
    v1 = json.loads((HERE.parent / "bot" / "films_v1.json").read_text(encoding="utf-8"))["films"]
    scene = synth.scene(7, 1500, 1000)
    sample = Image.open(HERE.parent / "community" / "sample.jpg").convert("RGB")
    worst_lut, worst_img, mean_img = 0.0, 0, 0.0
    for key in v1:                                   # все 12 (5 эталонов нарисованы прежним движком, 7 — этим, см. make_film_ref.py)
        params = film.clean_params(v1[key]["params"])
        t = np.asarray(film._bake(params).table, dtype=np.float32)[::97]
        worst_lut = max(worst_lut, float(np.abs(t - ref[f"{key}_lut"]).max()))
        for name, img in (("scene", scene), ("sample", sample)):
            r = np.asarray(film.film(img.copy(), "custom", 100, 3, params).resize((96, 64), Image.BOX), dtype=np.int16)
            d = np.abs(r - ref[f"{key}_{name}"].astype(np.int16))
            worst_img, mean_img = max(worst_img, int(d.max())), max(mean_img, float(d.mean()))
    check(f"таблицы цвета старых плёнок воспроизводятся (расхождение {worst_lut:.6f})", worst_lut < 1e-5)
    # запас на версии Pillow — по отдельным точкам; в среднем кадр должен совпадать почти точно
    check(f"готовые кадры старых плёнок воспроизводятся (расхождение {worst_img} из 255, в среднем {mean_img:.2f})", worst_img <= 6 and mean_img < 0.5)
    check("снимок старых плёнок полный: 12 штук", len(v1) == 12 and set(v1) == set(film.PRESETS))

    # --- плёнки v2 (1.4: полосы, зерно в тенях, линейное свечение) движок тоже воспроизводит точь-в-точь ---
    ref2 = np.load(HERE / "film_ref_v2.npz")
    v2 = json.loads((HERE.parent / "bot" / "films_v2.json").read_text(encoding="utf-8"))["films"]
    wl2, wi2, wm2 = 0.0, 0, 0.0
    for key in v2:
        params = film.clean_params(v2[key]["params"])
        wl2 = max(wl2, float(np.abs(np.asarray(film._bake(params).table, dtype=np.float32)[::97] - ref2[f"{key}_lut"]).max()))
        for name, img in (("scene", scene), ("sample", sample)):
            r = np.asarray(film.film(img.copy(), "custom", 100, 3, params).resize((96, 64), Image.BOX), dtype=np.int16)
            d = np.abs(r - ref2[f"{key}_{name}"].astype(np.int16))
            wi2, wm2 = max(wi2, int(d.max())), max(wm2, float(d.mean()))
    check(f"плёнки v2 воспроизводятся (таблицы {wl2:.6f}, кадры {wi2} из 255, в среднем {wm2:.2f})", wl2 < 1e-5 and wi2 <= 6 and wm2 < 0.5)
    check("Super 400 и Portrait 400 остались как в v2", all({f: v for f, v in film.params_json(film.clean_params(film.PRESETS[k])).items() if f in v2[k]["params"]}
                                                              == v2[k]["params"] for k in ("super400", "portrait400")))

    # --- встроенные плёнки сейчас: вид закреплён эталоном ---
    cur = np.load(HERE / "film_ref.npz")
    wl, wi, wm = 0.0, 0, 0.0
    for key in film.PRESETS:
        t = np.asarray(film.preset_lut(key).table, dtype=np.float32)[::97]
        wl = max(wl, float(np.abs(t - cur[f"{key}_lut"]).max()))
        for name, img in (("scene", scene), ("sample", sample)):
            r = np.asarray(film.film(img.copy(), key, 100, 3).resize((96, 64), Image.BOX), dtype=np.int16)
            d = np.abs(r - cur[f"{key}_{name}"].astype(np.int16))
            wi, wm = max(wi, int(d.max())), max(wm, float(d.mean()))
    check(f"все 12 встроенных плёнок выглядят как закреплено в эталоне (таблицы {wl:.6f}, кадры {wi} из 255, в среднем {wm:.2f})",
          wl < 1e-5 and wi <= 6 and wm < 0.5)
    chart = Image.new("RGB", (6, 1))
    chart.putdata([(214, 126, 44), (87, 108, 67), (98, 122, 157), (194, 150, 130), (70, 148, 73), (80, 91, 166)])      # оранжевый, листва, небо, кожа, зелёный, синий
    quiet = dict(grain=0.0, halation=0.0, bloom=0.0, vignette=0.0, soften=0.0, hue=(0,) * 6, bsat=(1,) * 6, grain_shadow=0.0, linear=0.0)
    diffs = {}
    for key, v in v1.items():
        old_p = film.clean_params(dict(v["params"], **quiet))
        new_p = film.clean_params(dict(film.params_json(film.clean_params(film.PRESETS[key])), **dict(quiet, hue=film.PRESETS[key]["hue"], bsat=film.PRESETS[key]["bsat"])))
        diffs[key] = float(np.abs(np.asarray(film.film(chart.copy(), "custom", 100, 1, new_p), dtype=np.float32) - np.asarray(film.film(chart.copy(), "custom", 100, 1, old_p), dtype=np.float32)).max())
    colour = [k for k in v1 if not v1[k]["params"]["bw"]]
    check(f"у всех цветных плёнок есть свои цветовые полосы (сдвиг цвета от {min(diffs[k] for k in colour):.0f} до {max(diffs[k] for k in colour):.0f} из 255)", all(diffs[k] > 4 for k in colour))
    bwk = [k for k in v1 if v1[k]["params"]["bw"]]
    check("чёрно-белые остаются без цвета, с зерном в тенях", all(film.PRESETS[k]["bw"] and film.PRESETS[k]["grain_shadow"] > 0 and
          float(np.ptp(np.asarray(film.film(chart.copy(), k, 100, 1)).astype(int), axis=-1).max()) <= 2 for k in bwk))

    # --- цветовые полосы ---
    rng = np.random.default_rng(1)
    rnd = rng.random((2000, 3)).astype(np.float32)
    check("полосы без сдвигов — цвет не меняется", float(np.abs(film.band_adjust(rnd, (0,) * 6, (1,) * 6) - rnd).max()) < 1e-5)
    green, red, blue, grey = (0.1, 0.8, 0.1), (0.8, 0.1, 0.1), (0.1, 0.1, 0.8), (0.5, 0.5, 0.5)
    shifted = flat(hue=(0, 0, -30, 0, 0, 0))
    gh = hue_of(lut_apply(shifted, green))
    check(f"зелень (120°) сдвинута к жёлтому: {gh:.0f}°", 85 < gh < 95)
    check("красный, синий и серое при этом на месте", all(np.allclose(lut_apply(shifted, c), lut_apply(flat(), c), atol=1e-3) for c in (red, blue, grey)))
    blues = flat(hue=(0, 0, 0, 0, 40, 0))
    check("полоса синего сдвигает синий, зелень не трогает", hue_of(lut_apply(blues, blue)) > 255 and np.allclose(lut_apply(blues, green), lut_apply(flat(), green), atol=1e-3))
    dull = flat(bsat=(1, 1, 0.5, 1, 1, 1))
    check("насыщенность зелёной полосы ×0,5: у зелени вдвое меньше, у красного прежняя",
          abs(sat_of(lut_apply(dull, green)) - sat_of(lut_apply(flat(), green)) * 0.5) < 0.03 and abs(sat_of(lut_apply(dull, red)) - sat_of(lut_apply(flat(), red))) < 0.01)
    mix = lut_apply(shifted, (0.1, 0.55, 0.45))               # между зелёным и голубым — плавный переход, а не скачок
    check("между полосами сдвиг плавный", 120 < hue_of(mix) < 165 and abs(hue_of(mix) - hue_of((0.1, 0.55, 0.45))) < 25)
    bwp = film.clean_params(dict(film.params_json(flat()), bw=(0.3, 0.55, 0.15), hue=(0, 0, 30, 0, 0, 0)))
    check("в чёрно-белой плёнке полосы ни на что не влияют", hue_of(lut_apply(bwp, green)) is None)

    # --- смешивание каналов ---
    check("матрица без перетеканий — единичная", np.allclose(film.mix_matrix((0,) * 6), np.eye(3)))
    check("в любой матрице сумма строк — 1 (серое не красится)", all(np.allclose(film.mix_matrix(m).sum(axis=1), 1) for m in ((0.3, -0.2, 0.1, 0.4, -0.5, 0.25), (0.5,) * 6, (-0.5,) * 6)))
    mixed = flat(mix=(0.3, 0.1, -0.2, 0.2, 0.15, -0.3))
    check("серые тона после смешивания не меняются", all(np.allclose(lut_apply(mixed, (g, g, g)), lut_apply(flat(), (g, g, g)), atol=1e-3) for g in (0.1, 0.5, 0.9)))
    rg = flat(mix=(0.3, 0, 0, 0, 0, 0))
    check("зелёный в красный: зелень уходит к жёлтому", hue_of(lut_apply(rg, green)) < hue_of(lut_apply(flat(), green)) - 10)
    check("красный канал красного не меняется, если он не участвует", np.allclose(lut_apply(rg, (0.8, 0.0, 0.0))[1:], lut_apply(flat(), (0.8, 0.0, 0.0))[1:], atol=1e-3))
    check("в чёрно-белой плёнке смешивание ничего не меняет", np.allclose(lut_apply(film.clean_params(dict(film.params_json(flat()), bw=(0.3, 0.55, 0.15), mix=(0.4,) * 6)), green),
                                                                     lut_apply(film.clean_params(dict(film.params_json(flat()), bw=(0.3, 0.55, 0.15))), green)))
    check("mix ограничен допустимым и битый отклоняется", film.clean_params({"mix": [9, -9, 0, 0, 0, 0]})["mix"][:2] == (0.5, -0.5)
          and all(_raises(lambda b=b: film.clean_params({"mix": b})) for b in ([1, 2, 3], "abc")))
    check("код с перетеканием проходит без потерь", film.parse_look_code(film.look_code("Микс", "A", film.clean_params({"mix": [0.1, 0, 0, -0.1, 0, 0]})))[2]["mix"][3] == -0.1)

    # --- параметры и коды ---
    p = film.clean_params({"hue": [90, -90, 0, 0, 0, 0], "bsat": [5, 0, 1, 1, 1, 1], "grain_shadow": 3, "linear": 7})
    check("новые параметры ограничены допустимым", p["hue"][:2] == (40.0, -40.0) and p["bsat"][:2] == (1.8, 0.4) and p["grain_shadow"] == 1.0 and p["linear"] == 1.0)
    for bad in ({"hue": [1, 2]}, {"hue": "abc"}, {"bsat": [1] * 5}, {"grain_shadow": "x"}):
        try:
            film.clean_params(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"битые новые параметры отклонены: {str(bad)[:24]}", ok)
    old_code = "proyavka-look:1:" + base64.urlsafe_b64encode(json.dumps({"name": "Старая", "p": {"contrast": 0.3}}).encode()).decode()
    name, by, op = film.parse_look_code(old_code)
    check("код, сделанный до этих параметров, читается; новые поля — по умолчанию", op["hue"] == (0.0,) * 6 and op["bsat"] == (1.0,) * 6 and op["linear"] == 0.0)
    plain = film.look_code("Обычная", "A", film.clean_params({"contrast": 0.3, "sat": 1.1}))
    rich = film.look_code("С полосами", "A", film.clean_params(dict(film.params_json(film.clean_params(film.PRESETS["vivid50"])), hue=[0, 0, -20, 0, 0, 0], linear=1)))
    dec = lambda c: json.loads(base64.urlsafe_b64decode(c[len(film.LOOK_CODE_PREFIX):] + "==").decode())["p"]
    check("в коде нет полей по умолчанию (он короче и совместим со старыми серверами)", not ({"hue", "bsat", "grain_shadow", "linear"} & set(dec(plain))) and "hue" in dec(rich))
    back = film.parse_look_code(rich)[2]
    check("плёнка с полосами проходит через код без потерь", back["hue"][2] == -20.0 and back["linear"] == 1.0)

    # --- зерно в тенях ---
    ramp = Image.fromarray(np.tile(np.linspace(30, 225, 600, dtype=np.uint8)[None, :, None], (200, 1, 3)))      # слева тень, справа свет

    def noise_std(gs):
        p = flat(grain=0.06, grain_size=1.6, grain_color=0.0, grain_shadow=gs)
        a = np.asarray(film.film(ramp.copy(), "custom", 100, 5, p), dtype=np.float32)[..., 0]
        fine = a - np.asarray(Image.fromarray(a.astype(np.uint8)).resize((600, 200)).filter(__import__("PIL.ImageFilter", fromlist=["x"]).GaussianBlur(4)), dtype=np.float32)
        return float(fine[:, 30:150].std()), float(fine[:, 450:570].std())
    d0, l0 = noise_std(0.0)
    d1, l1 = noise_std(1.0)
    check(f"зерно в тенях сильнее, в светах почти то же ({d0:.1f}→{d1:.1f} в тенях, {l0:.1f}→{l1:.1f} в светах)", d1 > d0 * 1.25 and l1 < l0 * 1.15)

    # --- линейный свет ---
    night = Image.new("RGB", (900, 600), (6, 6, 12))
    spot = np.asarray(night, dtype=np.uint8).copy()
    yy, xx = np.mgrid[0:600, 0:900]
    spot[((xx - 450) ** 2 + (yy - 300) ** 2) < 18 ** 2] = (255, 245, 220)
    night = Image.fromarray(spot)
    glow = dict(halation=0.8, hal_thr=0.6, bloom=0.1)
    o = np.asarray(film.film(night.copy(), "custom", 100, 1, flat(**glow)), dtype=np.float32)
    n = np.asarray(film.film(night.copy(), "custom", 100, 1, flat(**glow, linear=1)), dtype=np.float32)
    base = np.asarray(film.film(night.copy(), "custom", 100, 1, flat()), dtype=np.float32)
    ring = lambda a: float((a[..., 0] - base[..., 0])[:, :][(xx - 450) ** 2 + (yy - 300) ** 2 > 60 ** 2].mean())
    check(f"линейное свечение отличается от прежнего и светится вокруг огня ({ring(o):.2f} / {ring(n):.2f})", ring(n) > 0.3 and abs(ring(n) - ring(o)) > 0.05)
    acc = film.fx_linear(night, film.clean_params(dict(FLAT, **glow, linear=1)))
    a1 = np.asarray(film.apply_fx_linear(night, acc, strip=64), dtype=np.int16)
    a2 = np.asarray(film.apply_fx_linear(night, acc, strip=4000), dtype=np.int16)
    check(f"полосами и целиком — одно и то же (расхождение {int(np.abs(a1 - a2).max())})", int(np.abs(a1 - a2).max()) <= 1)
    check("без свечения в линейном режиме кадр не меняется", np.array_equal(np.asarray(film.film(night.copy(), "custom", 100, 1, flat(linear=1))), np.asarray(film.film(night.copy(), "custom", 100, 1, flat()))))
    sample_big = Image.open(HERE / "testdata" / "a6300_DSC00266.JPG").convert("RGB")
    check("на полном кадре 6000×4000 линейный режим отрабатывает", film.film(sample_big, "custom", 100, 1, film.clean_params(dict(film.params_json(film.clean_params(film.PRESETS["night800"])), linear=1))).size == (6000, 4000))

    # --- чужие данные: огромные числа и пустые веса ч/б ---
    huge = 10 ** 400                                   # целое из 400 цифр JSON разбирает, а float() на нём — OverflowError
    check("огромное число в параметрах — обычная ошибка, а не падение",
          all(_raises(lambda b=b: film.clean_params(b)) for b in ({"contrast": huge}, {"hue": [huge, 0, 0, 0, 0, 0]}, {"bw": [huge, 1, 1]})))
    check("и в коде плёнки тоже", _raises(lambda: film.parse_look_code('{"p":{"contrast":' + "9" * 400 + '}}')))
    check("ч/б с нулевыми весами (сплошной чёрный) отклоняется", _raises(lambda: film.clean_params({"bw": [0, 0, 0]})))
    # --- зерно в тенях не трогает света: где маска нулевая, кадр тот же, что без него ---
    bright = Image.new("RGB", (400, 300), (235, 235, 235))
    g0 = np.asarray(film.film(bright.copy(), "custom", 100, 2, flat(grain=0.03, grain_shadow=0.0)), dtype=np.int16)
    g1 = np.asarray(film.film(bright.copy(), "custom", 100, 2, flat(grain=0.03, grain_shadow=1.0)), dtype=np.int16)
    check(f"зерно в тенях: в светах ни одного сдвига (расхождение {int(np.abs(g0 - g1).max())})", int(np.abs(g0 - g1).max()) == 0)

    # --- халяция «как у плёнки»: ореол только у ярких источников на тёмном, голубое небо не краснеет ---
    sky = Image.new("RGB", (600, 400), (120, 170, 230))
    hp = flat(halation=1.2, hal_thr=0.7, halo=1)
    d_sky = np.asarray(film.film(sky.copy(), "custom", 100, 1, hp), np.int16) - np.asarray(film.film(sky.copy(), "custom", 100, 1, flat()), np.int16)
    check(f"на ярком небе ореола нет (сдвиг красного {int(np.abs(d_sky[..., 0]).max())})", int(np.abs(d_sky).max()) <= 2)
    lamp = np.zeros((400, 600, 3), np.uint8)
    lamp[(yy[:400, :600] - 200) ** 2 + (xx[:400, :600] - 300) ** 2 < 15 ** 2] = 255
    lamp = Image.fromarray(lamp)
    d_l = np.asarray(film.film(lamp.copy(), "custom", 100, 1, hp), np.float32) - np.asarray(film.film(lamp.copy(), "custom", 100, 1, flat()), np.float32)
    ring = d_l[200, 300 + 25:300 + 60].mean(axis=0)
    check(f"у огня на тёмном — красно-оранжевый ореол (R/G/B {ring.round(1).tolist()})", ring[0] > 8 and ring[0] > ring[1] > ring[2])
    # --- плотность цвета: серое не трогает, насыщенное тёмное — глубже ---
    dn = flat(dens=0.8)
    check("плотность цвета не трогает серое", all(np.allclose(lut_apply(dn, (g, g, g)), lut_apply(flat(), (g, g, g)), atol=1e-3) for g in (0.1, 0.5, 0.9)))
    deep_red = (0.6, 0.12, 0.12)
    check("насыщенный тёмный цвет становится темнее и насыщеннее",
          lab_l(lut_apply(dn, deep_red)) < lab_l(lut_apply(flat(), deep_red)) and sat_of(lut_apply(dn, deep_red)) >= sat_of(lut_apply(flat(), deep_red)) - 0.01)
    check("старые коды без новых полей — прежняя халяция и без плотности", film.clean_params({})["halo"] == 0.0 and film.clean_params({})["dens"] == 0.0)

    # --- встроенные плёнки по-прежнему отдаются редактору целиком ---
    check("у встроенных плёнок в параметрах есть и новые поля", all({"hue", "bsat", "grain_shadow", "linear", "dens", "halo"} <= set(film.params_json(film.clean_params(v))) for v in film.PRESETS.values()))
    os._exit(0)
