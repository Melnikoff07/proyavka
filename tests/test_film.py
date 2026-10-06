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
import filmbot as fb                    # noqa: E402


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


def sat_of(rgb):
    mx, mn = max(rgb), min(rgb)
    return 0 if mx == 0 else (mx - mn) / mx


FLAT = dict(contrast=0.0, lift=(0, 0, 0), top=1.0, shoulder=0.95, sat=1.0, grain=0.0, halation=0.0, bloom=0.0, soften=0.0, vignette=0.0)


def flat(**kw):
    return fb.clean_params(dict(FLAT, **kw))


def lut_apply(p, rgb):
    """Что плёнка с параметрами p делает с одним цветом (через ту же функцию, что запекается в LUT)."""
    return fb.color_fn(np.array([rgb], dtype=np.float32), p)[0]


if __name__ == "__main__":
    # --- старые плёнки не изменились ---
    ref = np.load(HERE / "film_ref.npz")
    scene = synth.scene(7, 1500, 1000)
    sample = Image.open(HERE.parent / "community" / "sample.jpg").convert("RGB")
    worst_lut, worst_img = 0.0, 0
    for key in ("street_neg", "night800", "vivid50", "across100", "expired"):
        t = np.asarray(fb.preset_lut(key).table, dtype=np.float32)[::97]
        worst_lut = max(worst_lut, float(np.abs(t - ref[f"{key}_lut"]).max()))
        for name, img in (("scene", scene), ("sample", sample)):
            r = np.asarray(fb.film(img.copy(), key, 100, 3).resize((96, 64), Image.BOX), dtype=np.int16)
            worst_img = max(worst_img, int(np.abs(r - ref[f"{key}_{name}"].astype(np.int16)).max()))
    check(f"таблицы цвета встроенных плёнок прежние (расхождение {worst_lut:.6f})", worst_lut < 1e-5)
    check(f"готовые кадры встроенных плёнок прежние (расхождение {worst_img} из 255)", worst_img <= 6)    # запас на версии Pillow

    # --- цветовые полосы ---
    rng = np.random.default_rng(1)
    rnd = rng.random((2000, 3)).astype(np.float32)
    check("полосы без сдвигов — цвет не меняется", float(np.abs(fb.band_adjust(rnd, (0,) * 6, (1,) * 6) - rnd).max()) < 1e-5)
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
    bwp = fb.clean_params(dict(fb.params_json(flat()), bw=(0.3, 0.55, 0.15), hue=(0, 0, 30, 0, 0, 0)))
    check("в чёрно-белой плёнке полосы ни на что не влияют", hue_of(lut_apply(bwp, green)) is None)

    # --- параметры и коды ---
    p = fb.clean_params({"hue": [90, -90, 0, 0, 0, 0], "bsat": [5, 0, 1, 1, 1, 1], "grain_shadow": 3, "linear": 7})
    check("новые параметры ограничены допустимым", p["hue"][:2] == (40.0, -40.0) and p["bsat"][:2] == (1.8, 0.4) and p["grain_shadow"] == 1.0 and p["linear"] == 1.0)
    for bad in ({"hue": [1, 2]}, {"hue": "abc"}, {"bsat": [1] * 5}, {"grain_shadow": "x"}):
        try:
            fb.clean_params(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"битые новые параметры отклонены: {str(bad)[:24]}", ok)
    old_code = "proyavka-look:1:" + base64.urlsafe_b64encode(json.dumps({"name": "Старая", "p": {"contrast": 0.3}}).encode()).decode()
    name, by, op = fb.parse_look_code(old_code)
    check("код, сделанный до этих параметров, читается; новые поля — по умолчанию", op["hue"] == (0.0,) * 6 and op["bsat"] == (1.0,) * 6 and op["linear"] == 0.0)
    plain = fb.look_code("Обычная", "A", fb.clean_params(fb.PRESETS["vivid50"]))
    rich = fb.look_code("С полосами", "A", fb.clean_params(dict(fb.params_json(fb.clean_params(fb.PRESETS["vivid50"])), hue=[0, 0, -20, 0, 0, 0], linear=1)))
    dec = lambda c: json.loads(base64.urlsafe_b64decode(c[len(fb.LOOK_CODE_PREFIX):] + "==").decode())["p"]
    check("в коде нет полей по умолчанию (он короче и совместим со старыми серверами)", not ({"hue", "bsat", "grain_shadow", "linear"} & set(dec(plain))) and "hue" in dec(rich))
    back = fb.parse_look_code(rich)[2]
    check("плёнка с полосами проходит через код без потерь", back["hue"][2] == -20.0 and back["linear"] == 1.0)

    # --- зерно в тенях ---
    ramp = Image.fromarray(np.tile(np.linspace(30, 225, 600, dtype=np.uint8)[None, :, None], (200, 1, 3)))      # слева тень, справа свет

    def noise_std(gs):
        p = flat(grain=0.06, grain_size=1.6, grain_color=0.0, grain_shadow=gs)
        a = np.asarray(fb.film(ramp.copy(), "custom", 100, 5, p), dtype=np.float32)[..., 0]
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
    o = np.asarray(fb.film(night.copy(), "custom", 100, 1, flat(**glow)), dtype=np.float32)
    n = np.asarray(fb.film(night.copy(), "custom", 100, 1, flat(**glow, linear=1)), dtype=np.float32)
    base = np.asarray(fb.film(night.copy(), "custom", 100, 1, flat()), dtype=np.float32)
    ring = lambda a: float((a[..., 0] - base[..., 0])[:, :][(xx - 450) ** 2 + (yy - 300) ** 2 > 60 ** 2].mean())
    check(f"линейное свечение отличается от прежнего и светится вокруг огня ({ring(o):.2f} / {ring(n):.2f})", ring(n) > 0.3 and abs(ring(n) - ring(o)) > 0.05)
    acc = fb.fx_linear(night, fb.clean_params(dict(FLAT, **glow, linear=1)))
    a1 = np.asarray(fb.apply_fx_linear(night, acc, strip=64), dtype=np.int16)
    a2 = np.asarray(fb.apply_fx_linear(night, acc, strip=4000), dtype=np.int16)
    check(f"полосами и целиком — одно и то же (расхождение {int(np.abs(a1 - a2).max())})", int(np.abs(a1 - a2).max()) <= 1)
    check("без свечения в линейном режиме кадр не меняется", np.array_equal(np.asarray(fb.film(night.copy(), "custom", 100, 1, flat(linear=1))), np.asarray(fb.film(night.copy(), "custom", 100, 1, flat()))))
    sample_big = Image.open(HERE / "testdata" / "a6300_DSC00266.JPG").convert("RGB")
    check("на полном кадре 6000×4000 линейный режим отрабатывает", fb.film(sample_big, "custom", 100, 1, fb.clean_params(dict(fb.params_json(fb.clean_params(fb.PRESETS["night800"])), linear=1))).size == (6000, 4000))

    # --- встроенные плёнки по-прежнему отдаются редактору целиком ---
    check("у встроенных плёнок в параметрах есть и новые поля", all({"hue", "bsat", "grain_shadow", "linear"} <= set(fb.params_json(fb.clean_params(v))) for v in fb.PRESETS.values()))
    os._exit(0)
