"""Анимация «как это работает» для страницы открытой беты: Lottie (bot/beta/guide.json), пять сцен по 96 кадров.

    python scripts/beta/make_guide.py

Сцены: 1 — приложение ставится на камеру по USB; 2 — карта с config.txt и Wi‑Fi; 3 — снимок уходит на сервер;
4 — кадр проявляется на 35-мм плёнке; 5 — кадр в ленте приложения и уведомление.

Правила рисунка: холст 800×500, обводка основных предметов 5 px, мелких деталей 3,5 px; скругления по размеру предмета
(корпус 26, экран 12, телефон 36, плитки 8); всё выровнено по центрам и общим осям. Плёнка — в настоящих пропорциях
35 мм (кадр 36×24, ширина 35, 8 перфораций на кадр). Экраны камеры и телефона — тёмные в обеих темах.
Цвета INK/PAPER/BACK страница перекрашивает под тему (CSS по атрибутам fill/stroke), остальные — постоянные.
Подписи к сценам — в самой странице (bot/beta/beta.js), по номеру сцены: кадр // SCENE."""

import base64
import io
import json
import math
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
IMG_W, IMG_H = 360, 240          # кадр 3:2
TILE_W, TILE_H = 160, 200        # плитки ленты 4:5


def rgb(r, g, b):
    return [(r + .5) / 255, (g + .5) / 255, (b + .5) / 255, 1]      # +.5: lottie округляет вниз, цвет должен совпасть с CSS


# перекрашиваются страницей: INK → текст, PAPER → фон, BACK → подложка сцены
INK, PAPER, BACK = rgb(17, 17, 17), rgb(250, 250, 250), rgb(246, 245, 242)
# постоянные
ACC = rgb(255, 74, 28)               # ореол из значка
LCD = rgb(28, 27, 25)                # экраны всегда тёмные
LCD_UI = rgb(241, 239, 234)          # интерфейс на тёмном экране
LCD_DIM = rgb(74, 71, 66)
FLASH = rgb(255, 253, 244)
FILM, FILM_FRAME = rgb(46, 32, 24), rgb(66, 49, 38)   # проявленная плёнка: тёплая тёмная основа, пустые кадры светлее
FILM_MARK = rgb(214, 120, 52)        # надписи по краю плёнки

S_MAIN, S_FINE = 5, 3.5
EASE = {"o": {"x": .33, "y": 0}, "i": {"x": .2, "y": 1}}
SOFT = {"o": {"x": .45, "y": 0}, "i": {"x": .55, "y": 1}}


def st(v):
    return {"a": 0, "k": v}


def an(*keys, ease=EASE):
    """an((кадр, значение), ...) — анимированное свойство; значение — число или список; третий элемент "h" — скачком."""
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
    return v if isinstance(v, dict) else st([v[0], v[1], 0])


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


def arc(cx, cy, r, a0, a1):
    """Дуга окружности (градусы в экранных координатах, y вниз), не больше 90° — одна кривая Безье."""
    k = 4 / 3 * math.tan(math.radians(a1 - a0) / 4) * r
    p0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    t0 = (-math.sin(math.radians(a0)) * k, math.cos(math.radians(a0)) * k)
    t1 = (math.sin(math.radians(a1)) * k, -math.cos(math.radians(a1)) * k)
    return path([p0, p1], ins=[[0, 0], list(t1)], outs=[list(t0), [0, 0]])


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
    tr = {"ty": "tr", "p": p if isinstance(p, dict) else st(list(p)), "a": st(list(a)),
          "s": s if isinstance(s, dict) else st(s if isinstance(s, list) else [s, s]), "r": prop(r), "o": prop(o),
          "sk": st(0), "sa": st(0)}
    return {"ty": "gr", "nm": nm, "np": len(items), "cix": 2, "bm": 0, "it": [*items, tr]}


LAYERS = []


def layer(nm, shapes=None, ip=0, op=TOTAL, parent=None, ty=4, ref=None, masks=None, ind=None, **t):
    """Слои идут сверху вниз: первый в списке рисуется поверх остальных."""
    ind = ind or len(LAYERS) + 1
    lay = {"ddd": 0, "ind": ind, "ty": ty, "nm": nm, "sr": 1, "ks": ks(**t), "ao": 0, "ip": ip, "op": op, "st": 0, "bm": 0}
    if ty == 4:
        lay["shapes"] = shapes
    if ref:
        lay["refId"] = ref
    if parent:
        lay["parent"] = parent
    if masks:
        lay["hasMask"] = True
        lay["masksProperties"] = [{"inv": False, "mode": mode, "pt": pt, "o": st(100), "x": st(0), "nm": f"m{n}"}
                                  for n, (mode, pt) in enumerate(masks)]
    LAYERS.append(lay)
    return ind


def rr(x0, y0, x1, y1, r):
    """Скруглённый прямоугольник контуром (8 точек) — для масок, у которых меняется размер."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    k = r * 0.5523
    v = [[x0 + r, y0], [x1 - r, y0], [x1, y0 + r], [x1, y1 - r], [x1 - r, y1], [x0 + r, y1], [x0, y1 - r], [x0, y0 + r]]
    o = [[0, 0], [k, 0], [0, 0], [0, k], [0, 0], [-k, 0], [0, 0], [0, -k]]
    i = [[0, 0], [0, 0], [0, -k], [0, 0], [k, 0], [0, 0], [-k, 0], [0, 0]]
    return {"v": v, "i": i, "o": o, "c": True}


def shape_anim(*keys):
    """Анимированный контур маски: (кадр, контур)."""
    k = []
    for n, (t, sh) in enumerate(keys):
        item = {"t": t, "s": [sh]}
        if n < len(keys) - 1:
            item.update({"o": EASE["o"], "i": EASE["i"]})
        k.append(item)
    return {"a": 1, "k": k}


def pop(t, dur=12, to=100):
    """Появление с лёгким перелётом: 0 → 110% → 100%."""
    return an((t, [0, 0]), (t + dur * .6, [to * 1.1, to * 1.1]), (t + dur, [to, to]))


def fade(t0, t1, t2=None, t3=None):
    keys = [(t0, 0), (t1, 100)]
    if t2 is not None:
        keys += [(t2, 100), (t3, 0)]
    return an(*keys)


def check_badge(d, nm):
    """Круг-галочка ореолом: «готово»."""
    return [group(path([(-d * .2, 0), (-d * .04, d * .16), (d * .23, -d * .14)]), stroke(LCD_UI, d * .13), nm=nm + "-tick"),
            group(ellipse(d), fill(ACC), nm=nm + "-disc")]


def jpeg_b64(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def build():
    src = Image.open(ROOT / "community" / "sample.jpg").convert("RGB")
    sw, sh = src.size
    big = src.resize((IMG_W * 2, IMG_H * 2), Image.LANCZOS)
    before = big.resize((IMG_W, IMG_H), Image.LANCZOS)
    after = film(big, "super400", 100, 1, fit=True).resize((IMG_W, IMG_H), Image.LANCZOS)

    def tile(key, box):
        im = src.crop(box)
        im = im.resize((TILE_W * 2, TILE_H * 2), Image.LANCZOS)
        return film(im, key, 100, 2, fit=True).resize((TILE_W, TILE_H), Image.LANCZOS)
    t1 = tile("portrait400", (60, 233, 60 + 560, 233 + 700))     # левый склон
    t2 = tile("across100", (800, 140, 800 + 520, 140 + 650))
    assets = [{"id": "before", "w": IMG_W, "h": IMG_H, "u": "", "p": jpeg_b64(before), "e": 1},
              {"id": "after", "w": IMG_W, "h": IMG_H, "u": "", "p": jpeg_b64(after), "e": 1},
              {"id": "tile1", "w": TILE_W, "h": TILE_H, "u": "", "p": jpeg_b64(t1), "e": 1},
              {"id": "tile2", "w": TILE_W, "h": TILE_H, "u": "", "p": jpeg_b64(t2), "e": 1}]

    S1, S2, S3, S4, S5 = (SCENE * i for i in range(5))
    END3 = S4 - 6
    CX, CY = W / 2, H / 2

    # ================= 5. телефон: лента «Проявки», новый кадр ложится первым, сверху уведомление =================
    PH_W, PH_H = 204, 408
    PH = (CX, CY)
    pin = PH_W / 2 - 12                        # внутренний край экрана: рамка и поле
    tw = 2 * pin                               # широкая плитка — во всю ширину экрана
    top = -PH_H / 2
    y_head = top + 40
    y_day = top + 76
    wide_h = tw * 2 / 3
    y_wide = top + 100 + wide_h / 2            # центр широкой плитки 3:2
    small_w = (tw - 6) / 2
    small_h = small_w * 5 / 4
    y_small = top + 100 + wide_h + 6 + small_h / 2
    out5 = an((S5, 100), (S5 + 84, 100), (S5 + 94, 0))
    # уведомление: тёмная пилюля, точка-ореол, две строки
    layer("notice", [
        group(ellipse(12, p=(-pin + 28, 0)), fill(ACC), nm="dot"),
        group(rect(92, 6, 3, p=(-pin + 46 + 46, -7)), fill(LCD_UI), nm="l1"),
        group(rect(60, 6, 3, p=(-pin + 46 + 30, 7)), fill(LCD_UI, 55), nm="l2"),
        group(rect(PH_W - 28, 44, 22), stroke(INK, 1.5, o=18), fill(LCD), nm="pill"),
    ], ip=S5, p=an((S5 + 40, [PH[0], PH[1] + top + 6]), (S5 + 54, [PH[0], PH[1] + top + 46]), (S5 + 76, [PH[0], PH[1] + top + 46]),
                   (S5 + 86, [PH[0], PH[1] + top + 6])), o=fade(S5 + 40, S5 + 47, S5 + 78, S5 + 86))
    # новый кадр: из плёнки — в широкую плитку
    sc_from, sc_to = 309 / IMG_W * 100, tw / IMG_W * 100
    layer("newframe", ty=2, ref="after", ip=S5, a=(IMG_W / 2, IMG_H / 2),
          p=an((S5, [CX, CY]), (S5 + 26, [PH[0], PH[1] + y_wide])),
          s=an((S5, [sc_from, sc_from, 100]), (S5 + 26, [sc_to, sc_to, 100])), o=out5,
          masks=[("a", shape_anim((S5, rr(0, 0, IMG_W, IMG_H, 4 / sc_from * 100)), (S5 + 26, rr(0, 0, IMG_W, IMG_H, 10 / sc_to * 100))))])
    for n, (ref, x) in enumerate((("tile1", -(small_w + 6) / 2), ("tile2", (small_w + 6) / 2))):
        sc = small_w / TILE_W * 100
        layer(f"tile{n}", ty=2, ref=ref, ip=S5, a=(TILE_W / 2, TILE_H / 2), p=(PH[0] + x, PH[1] + y_small),
              s=an((S5 + 18 + n * 3, [0, 0, 100]), (S5 + 26 + n * 3, [sc * 1.06, sc * 1.06, 100]), (S5 + 32 + n * 3, [sc, sc, 100])),
              o=out5, masks=[("a", st(rr(0, 0, TILE_W, TILE_H, 10 / sc * 100)))])
    tab_y = PH_H / 2 - 30
    layer("phone", [
        # нижняя панель вкладок: тёмная пилюля, выбранная половина — светлая
        group(rect(46, 22, 11, p=(-24, tab_y)), fill(PAPER), o=fade(S5 + 24, S5 + 30), nm="tab-on"),
        group(rect(100, 30, 15, p=(0, tab_y)), fill(INK), o=fade(S5 + 22, S5 + 28), nm="tabbar"),
        # место под широкую плитку, пока кадр летит
        group(rect(tw, wide_h, 10, p=(0, y_wide)), stroke(INK, 2.5, o=40, dash=(5, 6)), o=fade(S5 + 8, S5 + 14, S5 + 22, S5 + 26), nm="slot"),
        # шапка: точка-ореол и название слева, «+» и меню справа
        # строка дня: крупная «цифра» и две строки капсом, счётчик справа
        group(rect(34, 26, 5, p=(-pin + 17, y_day)), fill(INK), o=fade(S5 + 14, S5 + 20), nm="daynum"),
        group(rect(34, 5, 2.5, p=(-pin + 40 + 17, y_day - 5)), rect(24, 5, 2.5, p=(-pin + 40 + 12, y_day + 5)), fill(INK, 55),
              o=fade(S5 + 15, S5 + 21), nm="daytxt"),
        group(rect(14, 5, 2.5, p=(pin - 7, y_day + 5)), fill(INK, 35), o=fade(S5 + 16, S5 + 22), nm="daycnt"),
        group(ellipse(12, p=(-pin + 6, y_head)), fill(ACC), o=fade(S5 + 12, S5 + 18), nm="logo"),
        group(rect(52, 8, 4, p=(-pin + 18 + 26, y_head)), fill(INK), o=fade(S5 + 13, S5 + 19), nm="title"),
        group(rect(10, 2.4, 1.2, p=(pin - 40, y_head)), rect(2.4, 10, 1.2, p=(pin - 40, y_head)), fill(LCD_UI), o=fade(S5 + 18, S5 + 22), nm="plus-sign"),
        group(ellipse(24, p=(pin - 40, y_head)), fill(ACC), s=pop(S5 + 14, 10), a=(pin - 40, y_head), p=(pin - 40, y_head), nm="plus"),
        group(ellipse(24, p=(pin - 12, y_head)), fill(INK, 12), s=pop(S5 + 16, 10), a=(pin - 12, y_head), p=(pin - 12, y_head), nm="menu"),
        group(rect(64, 18, 9, p=(0, top + 17)), fill(INK), o=fade(S5 + 10, S5 + 16), nm="island"),
        group(rect(PH_W, PH_H, 36), trim(an((S5, 0), (S5 + 22, 100))), stroke(INK, S_MAIN), fill(PAPER, fade(S5 + 10, S5 + 20)), nm="body"),
    ], ip=S5, p=PH, o=out5)

    # ================= 4. проявка: кадр на 35-мм плёнке, проявитель идёт слева направо =================
    MM = 206 / 24                       # пикселей в миллиметре: высота кадра 24 мм = 206 px
    FW, FH = 36 * MM, 24 * MM           # кадр 309×206
    BAND = 35 * MM                      # ширина плёнки 300
    PITCH = 38 * MM                     # шаг кадров
    HOLE_P = 4.75 * MM                  # шаг перфорации: 8 на кадр
    HOLE_W, HOLE_H = 2.8 * MM, 1.98 * MM
    HOLE_Y = BAND / 2 - 3.0 * MM       # центр перфорации — 3 мм от края плёнки
    strip = 900                          # номер слоя заранее: кадр и проявитель над плёнкой, а привязаны к ней
    wipe0, wipe1 = S4 + 34, S4 + 66
    x0 = -IMG_W * (FW / IMG_W) / 2
    scan_x = lambda w: x0 + w * FW / IMG_W          # noqa: E731 — край проявки в координатах плёнки
    frame_mask = ("a", st(rr(0, 0, IMG_W, IMG_H, 3 * IMG_W / FW)))
    layer("scan", [group(rect(4, FH + 14, 2), fill(ACC), nm="line"),
                   group(rect(24, FH + 14, 12), fill(ACC, 22), nm="glow")],
          ip=S4, op=S5, parent=strip, p=an((wipe0, [scan_x(0), 0]), (wipe1, [scan_x(IMG_W), 0]), ease=SOFT),
          o=fade(wipe0 - 2, wipe0 + 2, wipe1 - 2, wipe1 + 4))
    layer("after", ty=2, ref="after", ip=S4, op=S5, parent=strip, a=(IMG_W / 2, IMG_H / 2), s=FW / IMG_W * 100,
          masks=[frame_mask, ("i", shape_anim((wipe0, rr(0, -2, 0.01, IMG_H + 2, 0)), (wipe1, rr(0, -2, IMG_W, IMG_H + 2, 0))))])
    layer("before4", ty=2, ref="before", ip=S4, op=S5, parent=strip, a=(IMG_W / 2, IMG_H / 2), s=FW / IMG_W * 100, masks=[frame_mask])
    holes, marks = [], []
    n_holes = int(1400 / HOLE_P)
    for k in range(-n_holes // 2, n_holes // 2 + 1):
        x = (k + .5) * HOLE_P
        for y in (-HOLE_Y, HOLE_Y):
            holes.append(group(rect(HOLE_W, HOLE_H, 3, p=(x, y)), fill(BACK), nm="hole"))
    for f in (-2, -1, 0, 1, 2):                     # надписи по краю: стрелка у границы кадра и чёрточки номера
        xb = f * PITCH + PITCH / 2
        yb = BAND / 2 - 8              # надписи — у самого края, снаружи перфорации
        marks.append(group(path([(xb - 4, yb - 4), (xb + 4, yb), (xb - 4, yb + 4)], closed=True), fill(FILM_MARK), nm="arrow"))
        marks.append(group(rect(14, 3, 1.5, p=(xb + 16, yb)), rect(6, 3, 1.5, p=(xb + 30, yb)), fill(FILM_MARK, 70), nm="code"))
    frames = [group(rect(FW, FH, 3, p=(f * PITCH, 0)), fill(FILM_FRAME), nm="blank") for f in (-2, -1, 1, 2)]
    layer("strip", [*holes, *marks, *frames, group(rect(1500, BAND, 0), fill(FILM), nm="band")],
          ip=S4, op=S5 + 12, p=an((S4 + 2, [CX + 760, CY]), (S4 + 28, [CX, CY])), o=an((S5, 100), (S5 + 10, 0)), ind=strip)

    # ================= 1–3. камера (вид сзади) =================
    BW, BH = 340, 220
    LCD_W, LCD_H, LCD_C = 196, 132, (-44, 28)
    lx, ly = LCD_C
    col_x = ((lx + LCD_W / 2 + S_MAIN / 2) + (BW / 2 - S_MAIN / 2)) / 2        # ось кнопок справа: посередине между экраном и краем
    CAM3 = (240, CY + 6)                 # камера в третьей сцене: слева, сервер справа, пара по центру холста
    cam_xy = an((S1, [CX, CY]), (S3, [CX, CY]), (S3 + 16, list(CAM3)))
    cam_sc = .70
    cam_s = an((S1, [100, 100, 100]), (S3, [100, 100, 100]), (S3 + 16, [cam_sc * 100, cam_sc * 100, 100]))
    SRV = (594, CY + 6)

    # --- 3: сервер, пунктир и летящий кадр ---
    def cam_abs(x, y):               # точка камеры после переезда (кадр S3+16 и дальше)
        return (CAM3[0] + x * cam_sc, CAM3[1] + y * cam_sc)
    shot_sc = (LCD_W - S_MAIN) / IMG_W * 100
    lcd_abs = cam_abs(lx, ly)
    srv_top = (SRV[0], SRV[1] - 58)
    layer("fly", ty=2, ref="before", ip=S3 + 40, op=S3 + 78, a=(IMG_W / 2, IMG_H / 2),
          p={"a": 1, "k": [{"t": S3 + 42, "s": [*lcd_abs, 0], "o": EASE["o"], "i": EASE["i"], "to": [70, -150, 0], "ti": [-60, -90, 0]},
                           {"t": S3 + 72, "s": [srv_top[0], srv_top[1], 0]}]},
          s=an((S3 + 42, [shot_sc * cam_sc, shot_sc * cam_sc, 100]), (S3 + 72, [9, 9, 100])),
          o=fade(S3 + 40, S3 + 42, S3 + 70, S3 + 76), masks=[("a", st(rr(0, 0, IMG_W, IMG_H, 26)))])
    t_from = cam_abs(lx + 30, ly - LCD_H / 2 - 10)
    layer("trail", [group(path([t_from, (srv_top[0] - 24, srv_top[1] - 8)], ins=[[0, 0], [-90, -96]], outs=[[60, -150], [0, 0]]),
                          trim(an((S3 + 40, 0), (S3 + 66, 100))), stroke(INK, 6, dash=(.1, 15)), nm="trail")],
          ip=S3, op=S4, o=an((S3 + 40, 100), (S3 + 74, 100), (S3 + 84, 0)))
    leds, units = [], []
    UW, UH, UG = 168, 48, 10
    for n in range(3):
        y = (n - 1) * (UH + UG)
        blink = an(*[(S3 + 18 + n * 5 + 8 * i, 100 if i % 2 == 0 else 30, "h") for i in range(10)])
        leds.append(group(ellipse(10, p=(-UW / 2 + 24, y)), fill(ACC, blink), nm=f"led{n}"))
        leds.append(group(rect(56, 6, 3, p=(-UW / 2 + 44 + 28, y - 7)), rect(36, 6, 3, p=(-UW / 2 + 44 + 18, y + 7)), fill(INK), nm=f"bars{n}"))
        leds.append(group(ellipse(6, p=(UW / 2 - 22, y)), ellipse(6, p=(UW / 2 - 36, y)), fill(INK, 30), nm=f"vents{n}"))
        units.append(group(rect(UW, UH, 12, p=(0, y)), stroke(INK, S_MAIN), fill(PAPER), nm=f"unit{n}"))
    badge = check_badge(26, "srv")
    layer("server-ok", badge, ip=S3, op=S4, p=(SRV[0] + UW / 2 - 4, SRV[1] - UH - UG - UH / 2 + 4),
          s=pop(S3 + 72, 10), o=an((S3 + 72, 100), (END3, 100), (S4 + 2, 0)))
    layer("server", [*leds, *units], ip=S3, op=S4, p=SRV,
          s=an((S3 + 6, [0, 0, 100]), (S3 + 16, [106, 106, 100]), (S3 + 22, [100, 100, 100]), (S3 + 70, [100, 100, 100]),
               (S3 + 74, [104, 104, 100]), (S3 + 80, [100, 100, 100])),
          o=an((S3, 100), (END3, 100), (S4 + 2, 0)))

    # --- камера: риг (нулевой слой) и всё, что к нему привязано ---
    cam = layer("camera-rig", ty=3, p=cam_xy, s=cam_s, op=S4)
    # интерфейс экрана камеры поверх снимка
    st_y = ly - LCD_H / 2 + 14                  # строка состояния
    bat = [group(rect(18, 9, 2.5, p=(lx + LCD_W / 2 - 24, st_y)), stroke(LCD_UI, 1.6), nm="bat"),
           group(rect(10, 5, 1, p=(lx + LCD_W / 2 - 26, st_y)), fill(LCD_UI), nm="bat-lvl"),
           group(rect(2, 4, 1, p=(lx + LCD_W / 2 - 13.5, st_y)), fill(LCD_UI), nm="bat-tip")]
    wx, wy = lx + LCD_W / 2 - 46, st_y + 4     # маленький Wi‑Fi в строке состояния (с третьей сцены)
    wifi_small = [group(arc(wx, wy, 5, 225, 315), arc(wx, wy, 10, 225, 315), stroke(LCD_UI, 1.8), nm="w-arcs"),
                  group(ellipse(2.8, p=(wx, wy)), fill(LCD_UI), nm="w-dot")]
    # большой Wi‑Fi в центре экрана (вторая сцена): три дуги рисуются по очереди, точка, галочка
    bx, by = lx, ly + 18
    wifi_big = []
    for n, r in enumerate((12, 24, 36)):
        t0 = S2 + 44 + n * 5
        wifi_big.append(group(arc(bx, by, r, 225, 315), trim(an((t0, 50), (t0 + 10, 100)), an((t0, 50), (t0 + 10, 0))),
                              stroke(LCD_UI, 5), nm=f"arc{n}"))
    wifi_big.append(group(ellipse(9, p=(bx, by)), fill(LCD_UI), s=pop(S2 + 42, 8), a=(bx, by), p=(bx, by), nm="dot"))
    # значок приложения: тёмный квадрат с точкой-ореолом, как настоящая иконка
    ix, iy = lx, ly - 10
    icon = [group(ellipse(12, p=(ix, iy)), fill(LCD_UI), nm="core"),
            group(ellipse(24, p=(ix, iy)), fill(ACC, 70), nm="halo"),
            group(ellipse(34, p=(ix, iy)), fill(ACC, 22), nm="glow"),
            group(rect(50, 50, 14, p=(ix, iy)), fill(LCD_DIM), nm="square")]
    prog_y, prog_w = ly + 36, 96
    screen_ui = [
        group(rect(LCD_W - S_MAIN, LCD_H - S_MAIN, 10, p=LCD_C), fill(FLASH, an((S3 + 20, 0), (S3 + 22, 100), (S3 + 36, 0))), nm="flash"),
        group(*bat, o=fade(S1 + 22, S1 + 28), nm="battery"),
        group(*wifi_small, o=fade(S3 + 4, S3 + 10), nm="wifi-small"),
        group(*wifi_big, o=an((S2 + 40, 100), (S3 - 2, 100), (S3 + 4, 0)), nm="wifi-big"),
        group(*check_badge(20, "wok"), p=(bx + 34, by + 4), a=(bx + 34, by + 4), s=pop(S2 + 66, 10),
              o=an((S2 + 66, 100), (S3 - 2, 100), (S3 + 4, 0)), nm="wifi-ok"),
        group(*check_badge(20, "iok"), p=(ix + 22, iy - 22), a=(ix + 22, iy - 22), s=pop(S1 + 64, 10),
              o=an((S1 + 64, 100), (S2 + 28, 100), (S2 + 34, 0)), nm="icon-ok"),
        group(*icon, p=(ix, iy), a=(ix, iy), s=pop(S1 + 28, 14), o=an((S1 + 28, 100), (S2 + 28, 100), (S2 + 36, 0)), nm="icon"),
        group(rect(prog_w, 6, 3, p=(prog_w / 2, 0)), fill(ACC), p=(lx - prog_w / 2, prog_y), a=(0, 0),
              s=an((S1 + 36, [0, 100]), (S1 + 62, [100, 100]), ease=SOFT), o=fade(S1 + 34, S1 + 36, S1 + 64, S1 + 70), nm="bar"),
        group(rect(prog_w, 6, 3, p=(lx, prog_y)), fill(LCD_UI, 18), o=fade(S1 + 32, S1 + 36, S1 + 64, S1 + 70), nm="track"),
    ]
    layer("screen-ui", screen_ui, parent=cam, op=S4, o=an((S3, 100), (END3, 100), (S4 + 2, 0)))
    layer("shot", ty=2, ref="before", ip=S3 + 22, op=S4, parent=cam, a=(IMG_W / 2, IMG_H / 2), p=LCD_C, s=shot_sc,
          o=an((S3 + 22, 0), (S3 + 26, 100), (END3, 100), (S4 + 2, 0)),
          masks=[("a", st(rr(0, IMG_H / 2 - (LCD_H - S_MAIN) / 2 * 100 / shot_sc, IMG_W, IMG_H / 2 + (LCD_H - S_MAIN) / 2 * 100 / shot_sc,
                              9.5 * 100 / shot_sc)))])
    shutter_y = an((S3 + 16, [col_x, -BH / 2 - 4]), (S3 + 19, [col_x, -BH / 2 + 1]), (S3 + 24, [col_x, -BH / 2 - 4]))
    layer("camera", [
        group(rect(LCD_W, LCD_H, 12, p=LCD_C), stroke(INK, S_MAIN), fill(LCD), s=pop(S1 + 16, 12), a=LCD_C, p=LCD_C, nm="lcd"),
        group(ellipse(66, p=(col_x, 40)), stroke(INK, S_MAIN), o=fade(S1 + 14, S1 + 22), nm="dial"),
        group(ellipse(20, p=(col_x, 40)), ellipse(13, p=(col_x, 92)), ellipse(13, p=(col_x - 16, -40)), ellipse(13, p=(col_x + 16, -40)),
              fill(INK), o=fade(S1 + 16, S1 + 24), nm="buttons"),
        group(rect(34, 12, 6, p=(col_x, -72)), fill(INK), o=fade(S1 + 16, S1 + 24), nm="c1"),
        group(rect(62, 30, 9, p=(-114, -72)), stroke(INK, S_FINE), fill(LCD), o=fade(S1 + 12, S1 + 20), nm="evf"),
        group(rect(BW, BH, 26), trim(an((S1 + 2, 0), (S1 + 24, 100))), stroke(INK, S_MAIN), fill(PAPER, fade(S1 + 12, S1 + 24)), nm="body"),
        group(rect(56, 16, 8), stroke(INK, S_MAIN), fill(PAPER), p=shutter_y, o=fade(S1 + 16, S1 + 24), nm="shutter"),
    ], parent=cam, op=S4, o=an((S3, 100), (END3, 100), (S4 + 2, 0)))
    # 2: карта памяти входит снизу в отсек справа (под корпусом)
    layer("card", [
        group(rect(26, 4, 2, p=(-2, -6)), rect(26, 4, 2, p=(-2, 4)), fill(PAPER), nm="label"),
        group(path([(-23, -30), (12, -30), (23, -19), (23, 30), (-23, 30)], closed=True), stroke(INK, S_FINE), fill(ACC), nm="card"),
    ], parent=cam, ip=S2, op=S3, p=an((S2 + 6, [col_x - 6, 190]), (S2 + 16, [col_x - 6, 176]), (S2 + 24, [col_x - 6, 176]), (S2 + 36, [col_x - 6, 60])),
        o=fade(S2 + 6, S2 + 11))
    # 1: кабель USB слева
    layer("usb", [
        group(rect(22, 16, 4, p=(-BW / 2 - 9, 44)), stroke(INK, S_FINE), fill(PAPER), nm="plug"),
        group(path([(-BW / 2 - 20, 44), (-BW / 2 - 150, 44)]), stroke(INK, S_MAIN), nm="cable"),
    ], parent=cam, ip=S1, op=S2, p=an((S1 + 8, [-90, 0]), (S1 + 22, [0, 0]), (S1 + 70, [0, 0]), (S1 + 84, [-150, 0])),
        o=fade(S1 + 8, S1 + 14, S1 + 76, S1 + 86))

    return {"v": "5.12.2", "fr": FR, "ip": 0, "op": TOTAL, "w": W, "h": H, "nm": "proyavka-guide", "ddd": 0,
            "assets": assets, "layers": LAYERS, "markers": [{"tm": SCENE * i, "cm": f"step{i + 1}", "dr": SCENE} for i in range(5)]}


if __name__ == "__main__":
    out = ROOT / "bot" / "beta" / "guide.json"
    data = json.dumps(build(), separators=(",", ":"))
    out.write_text(data, encoding="utf-8")
    print(f"{out}: {len(data) / 1024:.0f} KB, {TOTAL} frames")
