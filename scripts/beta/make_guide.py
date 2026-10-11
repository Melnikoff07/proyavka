"""Анимация «как это работает» для страницы открытой беты: Lottie (bot/beta/guide.json), пять сцен по 96 кадров.

    python scripts/beta/make_guide.py

Картинки до/после — общий образец каталога (community/sample.jpg) без плёнки и с плёнкой Super 400.
Цвета INK/PAPER/TILE страница перекрашивает под тему (CSS по атрибутам fill/stroke), ACC — один для обеих тем.
Подписи к сценам — в самой странице (bot/beta/index.html), по номеру сцены: кадр // SCENE."""

import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("CHAT_ID", "1")
os.environ.setdefault("BASE_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(ROOT / "bot"))

from PIL import Image  # noqa: E402

from proyavka.film import film  # noqa: E402

W, H, FR, SCENE = 800, 500, 30, 96
TOTAL = SCENE * 5
IMG_W, IMG_H = 360, 240


def rgb(r, g, b):
    return [(r + .5) / 255, (g + .5) / 255, (b + .5) / 255, 1]      # +.5: lottie округляет вниз, цвет должен совпасть с CSS


INK, PAPER, TILE, ACC, FLASH = rgb(17, 17, 17), rgb(250, 250, 250), rgb(242, 241, 239), rgb(232, 68, 26), rgb(255, 253, 244)
FILM, HOLE = rgb(28, 25, 22), rgb(238, 233, 224)        # плёнка в обеих темах тёмная

EASE = {"o": {"x": .33, "y": 0}, "i": {"x": .2, "y": 1}}           # плавный выход и мягкая посадка
LIN = {"o": {"x": 0, "y": 0}, "i": {"x": 1, "y": 1}}


def st(v):
    return {"a": 0, "k": v}


def an(*keys, ease=EASE):
    """an((кадр, значение), ...) — анимированное свойство; значение — число или список; ("h", ...) — без перехода."""
    k = []
    for n, key in enumerate(keys):
        t, v = key[0], key[1]
        item = {"t": t, "s": v if isinstance(v, list) else [v]}
        if n < len(keys) - 1:
            if len(key) > 2 and key[2] == "h":
                item["h"] = 1
            else:
                item.update({"o": ease["o"], "i": ease["i"]})
        k.append(item)
    return {"a": 1, "k": k}


def prop(v):
    return v if isinstance(v, dict) else st(v)


def p3(v):
    if isinstance(v, dict):
        return v
    return st([v[0], v[1], 0])


def s3(v):
    if isinstance(v, dict):
        return v
    v = v if isinstance(v, list) else [v, v]
    return st([v[0], v[1], 100])


def ks(p=(0, 0), a=(0, 0), s=100, r=0, o=100):
    return {"o": prop(o), "r": prop(r), "p": p3(p), "a": p3(a), "s": s3(s)}


def rect(w, h, r=0, p=(0, 0)):
    return {"ty": "rc", "d": 1, "s": prop([w, h]), "p": prop(list(p)), "r": prop(r)}


def ellipse(w, h=None, p=(0, 0)):
    return {"ty": "el", "d": 1, "s": prop([w, h or w]), "p": prop(list(p))}


def path(points, closed=False, ins=None, outs=None):
    z = [[0, 0]] * len(points)
    return {"ty": "sh", "d": 1, "ks": st({"v": [list(p) for p in points], "i": ins or z, "o": outs or z, "c": closed})}


def fill(c, o=100):
    return {"ty": "fl", "c": prop(c), "o": prop(o), "r": 1, "bm": 0}


def stroke(c, w, o=100, dash=None):
    s = {"ty": "st", "c": prop(c), "o": prop(o), "w": prop(w), "lc": 2, "lj": 2, "ml": 4, "bm": 0}
    if dash:
        s["d"] = [{"n": "d", "nm": "dash", "v": st(dash[0])}, {"n": "g", "nm": "gap", "v": st(dash[1])}]
    return s


def trim(e, s=0):
    return {"ty": "tm", "s": prop(s), "e": prop(e), "o": st(0), "m": 1}


def group(*items, p=(0, 0), a=(0, 0), s=100, r=0, o=100, nm="g"):
    tr = {"ty": "tr", "p": prop(list(p)) if not isinstance(p, dict) else p, "a": prop(list(a)),
          "s": prop(s if isinstance(s, (dict, list)) else [s, s]), "r": prop(r), "o": prop(o), "sk": st(0), "sa": st(0)}
    return {"ty": "gr", "nm": nm, "np": len(items), "cix": 2, "bm": 0, "it": [*items, tr]}


LAYERS = []


def layer(nm, shapes=None, ip=0, op=TOTAL, parent=None, ty=4, ref=None, mask=None, ind=None, **t):
    ind = ind or len(LAYERS) + 1
    lay = {"ddd": 0, "ind": ind, "ty": ty, "nm": nm, "sr": 1, "ks": ks(**t), "ao": 0, "ip": ip, "op": op, "st": 0, "bm": 0}
    if ty == 4:
        lay["shapes"] = shapes
    if ref:
        lay["refId"] = ref
    if parent:
        lay["parent"] = parent
    if mask:
        lay["hasMask"] = True
        lay["masksProperties"] = [{"inv": False, "mode": "a", "pt": mask, "o": st(100), "x": st(0), "nm": "m"}]
    LAYERS.append(lay)
    return ind


def box(x0, y0, x1, y1):
    return {"v": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "i": [[0, 0]] * 4, "o": [[0, 0]] * 4, "c": True}


def mask_anim(*keys):
    """Маска-прямоугольник по кадрам: (кадр, (x0, y0, x1, y1))."""
    k = []
    for n, (t, b) in enumerate(keys):
        item = {"t": t, "s": [box(*b)]}
        if n < len(keys) - 1:
            item.update({"o": EASE["o"], "i": EASE["i"]})
        k.append(item)
    return {"a": 1, "k": k}


def pop(t, dur=12, to=100):
    """Появление с лёгким перелётом: 0 → 112% → 100%."""
    return an((t, [0, 0]), (t + dur * .65, [to * 1.12, to * 1.12]), (t + dur, [to, to]))


def fade(t0, t1, t2=None, t3=None):
    keys = [(t0, 0), (t1, 100)]
    if t2 is not None:
        keys += [(t2, 100), (t3, 0)]
    return an(*keys)


def jpeg_b64(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def build():
    src = Image.open(ROOT / "community" / "sample.jpg").convert("RGB")
    src = src.resize((IMG_W * 2, IMG_H * 2), Image.LANCZOS)
    before = src.resize((IMG_W, IMG_H), Image.LANCZOS)
    after = film(src, "super400", 100, 1, fit=True).resize((IMG_W, IMG_H), Image.LANCZOS)
    assets = [{"id": "before", "w": IMG_W, "h": IMG_H, "u": "", "p": jpeg_b64(before), "e": 1},
              {"id": "after", "w": IMG_W, "h": IMG_H, "u": "", "p": jpeg_b64(after), "e": 1}]

    S1, S2, S3, S4, S5 = (SCENE * i for i in range(5))
    END3 = S4 - 6

    # ---------- 5. телефон с лентой: новый кадр ложится первым, сверху уведомление ----------
    # (слои идут сверху вниз: первый в списке рисуется поверх остальных)
    PH = (400, 252)
    layer("notice", [group(
        group(ellipse(14, p=(-66, 0)), fill(ACC), nm="dot"),
        group(rect(92, 6, 3, p=(0, -7)), fill(PAPER), nm="l1"),
        group(rect(60, 6, 3, p=(-16, 7)), fill(PAPER, 70), nm="l2"),
        group(rect(178, 46, 23), fill(INK), nm="pill"),
        nm="notice")],
        ip=S5, p=an((S5 + 40, [PH[0], PH[1] - 236]), (S5 + 54, [PH[0], PH[1] - 152]), (S5 + 76, [PH[0], PH[1] - 152]),
                    (S5 + 86, [PH[0], PH[1] - 236])),
        o=fade(S5 + 40, S5 + 48, S5 + 78, S5 + 86))
    tile = (PH[0] - 42, PH[1] - 92)
    layer("newframe", ty=2, ref="after", ip=S5, a=(IMG_W / 2, IMG_H / 2),
          p=an((S5, [400, 250]), (S5 + 24, list(tile))),
          s=an((S5, [112, 112, 100]), (S5 + 24, [32.5, 32.5, 100])),
          o=an((S5, 100), (S5 + 84, 100), (S5 + 94, 0)),
          mask=mask_anim((S5, (0, 0, IMG_W, IMG_H)), (S5 + 20, (60, 0, 300, 240))))
    tiles = []
    for n, (x, y) in enumerate([(42, -92), (-42, -6), (42, -6), (-42, 80), (42, 80)]):
        tiles.append(group(rect(78, 78, 6), fill(TILE), p=(x, y), s=pop(S5 + 10 + n * 3, 12), nm=f"t{n}"))
    layer("phone", [
        group(rect(78, 78, 6, p=(-42, -92)), stroke(INK, 2.5, dash=(4, 6)), o=fade(S5 + 8, S5 + 14, S5 + 22, S5 + 26), nm="slot"),
        *tiles,
        group(rect(70, 10, 5, p=(-48, -150)), fill(INK), o=fade(S5 + 12, S5 + 18), nm="title"),
        group(rect(56, 10, 5, p=(0, -178)), fill(INK), o=fade(S5 + 14, S5 + 20), nm="notch"),
        group(rect(196, 392, 34), trim(an((S5, 0), (S5 + 22, 100))), stroke(INK, 6), fill(PAPER, fade(S5 + 12, S5 + 22)), nm="body"),
    ], ip=S5, p=PH, o=an((S5, 100), (S5 + 84, 100), (S5 + 94, 0)))

    # ---------- 4. проявка: кадр в плёнке, проявитель проходит слева направо ----------
    strip = 900                                       # номер слоя заранее: кадр и проявитель над плёнкой, а привязаны к ней
    scan_x = lambda w: -IMG_W * .56 + w * 1.12        # noqa: E731 — край маски в координатах плёнки
    layer("scan", [group(rect(6, 286, 3), fill(ACC), nm="line"),
                   group(rect(26, 286, 13), fill(ACC, 25), nm="glow")],
          ip=S4, op=S5, parent=strip, p=an((S4 + 34, [scan_x(0), 0]), (S4 + 66, [scan_x(IMG_W), 0])),
          o=fade(S4 + 32, S4 + 36, S4 + 64, S4 + 70))
    layer("after", ty=2, ref="after", ip=S4, op=S5, parent=strip, a=(IMG_W / 2, IMG_H / 2), s=112,
          mask=mask_anim((S4 + 34, (0, 0, 0, IMG_H)), (S4 + 66, (0, 0, IMG_W, IMG_H))))
    layer("before4", ty=2, ref="before", ip=S4, op=S5, parent=strip, a=(IMG_W / 2, IMG_H / 2), s=112)
    layer("strip", [
        *[group(rect(18, 12, 2, p=(x, y)), fill(HOLE), nm="hole") for x in range(-238, 239, 34) for y in (-148, 148)],
        group(rect(520, 330, 8), fill(FILM), nm="band"),
    ], ip=S4, op=S5 + 12, p=an((S4 + 2, [1180, 250]), (S4 + 26, [400, 250])), o=an((S5, 100), (S5 + 10, 0)), ind=strip)

    # ---------- 3. снимок и отправка: камера уезжает влево, справа сервер, кадр летит по пунктиру ----------
    SRV = (596, 262)
    cam_xy = an((S1, [400, 262]), (S3, [400, 262]), (S3 + 14, [226, 262]))
    cam_s = an((S1, [100, 100, 100]), (S3, [100, 100, 100]), (S3 + 14, [72, 72, 100]))
    screen_abs = (226 - 48 * .72, 262 + 12 * .72)
    layer("fly", ty=2, ref="before", ip=S3 + 38, op=S3 + 76, a=(IMG_W / 2, IMG_H / 2),
          p={"a": 1, "k": [{"t": S3 + 40, "s": [*screen_abs, 0], "o": EASE["o"], "i": EASE["i"],
                            "to": [90, -150, 0], "ti": [-90, -60, 0]},
                           {"t": S3 + 70, "s": [SRV[0], SRV[1] - 60, 0]}]},
          s=an((S3 + 40, [36, 36, 100]), (S3 + 70, [10, 10, 100])), o=fade(S3 + 38, S3 + 40, S3 + 68, S3 + 74))
    layer("trail", [group(path([(268, 200), (560, 180)], ins=[[0, 0], [-120, -110]], outs=[[120, -110], [0, 0]]),
                          trim(an((S3 + 36, 0), (S3 + 64, 100))), stroke(INK, 5, dash=(.1, 14)), nm="trail")],
          ip=S3, op=S4, o=an((S3 + 36, 100), (S3 + 72, 100), (S3 + 82, 0)))
    leds = []
    for n, y in enumerate((-60, 0, 60)):
        blink = an(*[(S3 + 16 + n * 5 + 8 * i, 100 if i % 2 == 0 else 25, "h") for i in range(10)])
        leds.append(group(ellipse(10, p=(-58, y)), fill(ACC, blink), nm=f"led{n}"))
        leds.append(group(rect(56, 6, 3, p=(22, y - 6)), rect(36, 6, 3, p=(12, y + 6)), fill(INK), nm=f"bars{n}"))
    units = [group(rect(170, 50, 10, p=(0, y)), stroke(INK, 5), fill(PAPER), nm=f"unit{n}") for n, y in enumerate((-60, 0, 60))]
    layer("server", [*leds, *units], ip=S3, op=S4, p=SRV,
          s=an((S3 + 6, [0, 0, 100]), (S3 + 16, [108, 108, 100]), (S3 + 22, [100, 100, 100]), (S3 + 70, [100, 100, 100]),
               (S3 + 74, [106, 106, 100]), (S3 + 80, [100, 100, 100])),
          o=an((S3, 100), (END3, 100), (S4 + 2, 0)))

    # ---------- 1–3. камера (вид сзади) ----------
    cam = layer("camera-rig", ty=3, p=cam_xy, s=cam_s, op=S4)
    layer("shot", ty=2, ref="before", ip=S3 + 22, op=S4, parent=cam, a=(IMG_W / 2, IMG_H / 2), p=(-48, 12), s=50,
          o=an((S3 + 22, 0), (S3 + 28, 100), (END3, 100), (S4 + 2, 0)))
    screen_ui = [
        group(rect(184, 128, 10, p=(-48, 12)), fill(FLASH, an((S3 + 18, 0), (S3 + 20, 100), (S3 + 34, 0))), nm="flash"),
        # 2: Wi-Fi найден
        group(path([(-14, -6), (0, -16), (14, -6)], ins=[[0, 0], [-8, 0], [0, 0]], outs=[[0, 0], [8, 0], [0, 0]]),
              path([(-22, -14), (0, -28), (22, -14)], ins=[[0, 0], [-13, 0], [0, 0]], outs=[[0, 0], [13, 0], [0, 0]]),
              stroke(INK, 3.5), p=(-48 + 60, 12 - 34), o=fade(S2 + 44, S2 + 50, S3 - 4, S3 + 2), nm="wifi"),
        group(ellipse(6, p=(-48 + 60, 12 - 36)), fill(INK), o=fade(S2 + 44, S2 + 50, S3 - 4, S3 + 2), nm="wifi-dot"),
        group(path([(-8, 0), (-2, 6), (9, -6)]), stroke(ACC, 4), trim(an((S2 + 52, 0), (S2 + 60, 100))),
              p=(-48 + 60, 12 + 18), o=fade(S2 + 52, S2 + 53, S3 - 4, S3 + 2), nm="check"),
        # 1: установка — полоса, потом значок приложения
        group(group(rect(22, 16, 2), stroke(PAPER, 3), nm="glyph"), group(rect(54, 54, 14), fill(ACC), nm="tile"),
              p=(-78, 4), s=pop(S1 + 62, 14), o=an((S3 - 4, 100), (S3 + 2, 0)), nm="app"),
        group(rect(40, 5, 2.5, p=(20, 0)), fill(INK), p=(-48 + 6, -6), s=an((S1 + 70, [0, 100]), (S1 + 80, [100, 100])),
              o=an((S3 - 4, 100), (S3 + 2, 0)), nm="name"),
        group(rect(28, 5, 2.5, p=(14, 0)), fill(INK, 45), p=(-48 + 6, 6), s=an((S1 + 74, [0, 100]), (S1 + 84, [100, 100])),
              o=an((S3 - 4, 100), (S3 + 2, 0)), nm="sub"),
        group(rect(120, 8, 4, p=(60, 0)), fill(ACC), p=(-108, 40), s=an((S1 + 30, [0, 100]), (S1 + 58, [100, 100])),
              o=fade(S1 + 28, S1 + 30, S1 + 60, S1 + 64), nm="bar"),
        group(rect(120, 8, 4, p=(60, 0)), fill(INK, 12), p=(-108, 40), o=fade(S1 + 26, S1 + 30, S1 + 60, S1 + 64), nm="track"),
    ]
    layer("screen-ui", screen_ui, parent=cam, op=S4)
    shutter_y = an((S3 + 14, [108, -106]), (S3 + 17, [108, -100]), (S3 + 22, [108, -106]))
    layer("camera", [
        group(ellipse(64, p=(108, 24)), stroke(INK, 5), o=fade(S1 + 14, S1 + 24), nm="dial"),
        group(ellipse(12, p=(108, 24)), ellipse(16, p=(108, -46)), fill(INK), o=fade(S1 + 16, S1 + 26), nm="buttons"),
        group(rect(184, 128, 10, p=(-48, 12)), stroke(INK, 4), fill(TILE), s=pop(S1 + 18, 12), a=(-48, 12), p=(-48, 12), nm="screen"),
        group(rect(320, 200, 24), trim(an((S1 + 2, 0), (S1 + 24, 100))), stroke(INK, 6), fill(PAPER, fade(S1 + 12, S1 + 24)), nm="body"),
        group(rect(50, 16, 6), stroke(INK, 5), fill(PAPER), p=shutter_y, o=fade(S1 + 16, S1 + 24), nm="shutter"),
        group(rect(96, 44, 10, p=(-96, -108)), stroke(INK, 5), fill(PAPER), o=fade(S1 + 14, S1 + 24), nm="finder"),
    ], parent=cam, op=S4, o=an((S3, 100), (END3, 100), (S4 + 2, 0)))
    # 2: карта памяти с config.txt входит снизу (под корпусом); 1: кабель USB слева
    layer("card", [
        group(rect(30, 4, 2, p=(0, -6)), rect(30, 4, 2, p=(0, 4)), fill(PAPER), nm="label"),
        group(path([(-25, -32), (14, -32), (25, -21), (25, 32), (-25, 32)], closed=True), stroke(INK, 4), fill(ACC), nm="card"),
    ], parent=cam, ip=S2, op=S3, p=an((S2 + 6, [62, 200]), (S2 + 18, [62, 186]), (S2 + 34, [62, 186]), (S2 + 46, [62, 70])),
        o=fade(S2 + 6, S2 + 12))
    layer("usb", [
        group(rect(26, 18, 4, p=(-170, 40)), stroke(INK, 4), fill(PAPER), nm="plug"),
        group(path([(-183, 40), (-300, 40)]), stroke(INK, 5), nm="cable"),
    ], parent=cam, ip=S1, op=S2, p=an((S1 + 8, [-80, 0]), (S1 + 22, [0, 0]), (S1 + 70, [0, 0]), (S1 + 84, [-140, 0])),
        o=fade(S1 + 8, S1 + 14, S1 + 76, S1 + 86))

    return {"v": "5.12.2", "fr": FR, "ip": 0, "op": TOTAL, "w": W, "h": H, "nm": "proyavka-guide", "ddd": 0,
            "assets": assets, "layers": LAYERS, "markers": [{"tm": SCENE * i, "cm": f"step{i + 1}", "dr": SCENE} for i in range(5)]}


if __name__ == "__main__":
    out = ROOT / "bot" / "beta" / "guide.json"
    data = json.dumps(build(), separators=(",", ":"))
    out.write_text(data, encoding="utf-8")
    print(f"{out}: {len(data) / 1024:.0f} KB, {TOTAL} frames")
