"""Анимации мастера подключения камеры (bot/ui/setup.ru.json и setup.en.json): Lottie, сцены — отрезки одной шкалы
с маркерами; приложение проигрывает нужный отрезок по кругу (anim.playSegments).

    python scripts/setup_anim/make_setup.py          # нужны pillow, fonttools, brotli (шрифт берётся из bot/fonts)

Сцены: install — приложение ставится с компьютера по USB (pmca-gui); card-config — config.txt в папку PROYAVKA
(сначала ошибка: внутрь DCIM); card-cert — cacert.pem в корень карты; app-wifi — путь по меню до Wi-Fi и пароль;
send — «Отправить новые»; import — импорт корневого сертификата; ftp-form — поля сервера FTP; ftp-auto — автоотправка.

Подписи — картинки PNG из Inter Tight (как в приложении), поэтому всё, на чём есть текст, — постоянных цветов:
тёмный экран камеры и светлое окно проводника; рамки и корпус перекрашиваются под тему (INK/PAPER/BACK).
Стиль — как у анимации на странице беты (scripts/beta/make_guide.py): обводка 5–6 px, скругления по размеру."""

import base64
import importlib.util
import io
import json
import math
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("guide", ROOT / "scripts" / "beta" / "make_guide.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)
an, st, rect, ellipse, path, fill, stroke, trim, group, rr, pop, fade, check_badge, ks = (
    G.an, G.st, G.rect, G.ellipse, G.path, G.fill, G.stroke, G.trim, G.group, G.rr, G.pop, G.fade, G.check_badge, G.ks)
INK, PAPER, ACC, LCD, LCD_UI, LCD_DIM = G.INK, G.PAPER, G.ACC, G.LCD, G.LCD_UI, G.LCD_DIM
rgb = G.rgb

W, H, FR = 800, 500, 30
WIN, WIN_BAR, WIN_LINE = rgb(250, 249, 246), rgb(236, 234, 229), rgb(222, 219, 212)
FOLDER, BAD = rgb(196, 190, 180), rgb(214, 48, 49)
T_LCD, T_DIM, T_DARK, T_MUTE, T_ACC, T_WHITE, T_BAD = (241, 239, 234), (150, 146, 140), (28, 27, 25), (128, 124, 118), (255, 74, 28), (255, 255, 255), (214, 48, 49)

TEXTS = {
    "ru": dict(card="SD-карта", new_folder="создай эту папку", root_hint="в корень, рядом с DCIM", task_ok="Task completed successfully",
               app="Приложение", app_list="Список приложений", intro="Введение", proyavka="Проявка", remote="Smart Remote Control",
               send_new="Отправить новые", exit="Выход", wifi="Wi-Fi", my_phone="Мой телефон", home="Дом", password="Пароль",
               saved="Сохранено", connecting="Подключаюсь к Wi-Fi", sending="Отправляю кадры…", done="Готово: 12 кадров отправлено",
               network="Сеть", transfer="Передача/Удалённо", net_opt="Параметры сети", dev_name="Имя устройства",
               import_cert="Импорт корн. сертификата", reset_net="Сброс сетевых настроек", ftp_func="Функция FTP", ok="OK",
               imported="Сертификат импортирован", server1="Сервер 1",
               form=[("Имя хоста", "адрес сервера"), ("Защищ. протокол", "Вкл"), ("Ошибка корн. серт.", "Не подключаться"),
                     ("Порт", "21"), ("Пассивный режим", "Вкл"), ("Каталог", "upload"), ("Пользователь", "логин"), ("Пароль", "••••••")],
               ftp_title="Функция передачи FTP", ftp_on="Функция передачи FTP", server="Подключ. сервер", auto="Автоперед. при съёмке",
               ftype="Тип файла", jpeg="Только JPEG", sent="Передано на сервер"),
    "en": dict(card="SD card", new_folder="create this folder", root_hint="in the root, next to DCIM", task_ok="Task completed successfully",
               app="Application", app_list="Application List", intro="Introduction", proyavka="Proyavka", remote="Smart Remote Control",
               send_new="Send new", exit="Exit", wifi="Wi-Fi", my_phone="My phone", home="Home", password="Password",
               saved="Saved", connecting="Connecting to Wi-Fi", sending="Sending frames…", done="Done: 12 frames sent",
               network="Network", transfer="Transfer/Remote", net_opt="Network Option", dev_name="Device Name",
               import_cert="Import Root Certificate", reset_net="Reset Network Set.", ftp_func="FTP Function", ok="OK",
               imported="Certificate imported", server1="Server 1",
               form=[("Host Name", "server address"), ("Secure Protocol", "On"), ("Root Cert. Error", "Do Not Connect"),
                     ("Port", "21"), ("Passive Mode", "On"), ("Directory", "upload"), ("User", "login"), ("Password", "••••••")],
               ftp_title="FTP Transfer Func.", ftp_on="FTP Transfer Func.", server="Connected Server", auto="Auto FTP Trans.",
               ftype="Trans. Target File", jpeg="JPEG only", sent="Sent to the server"),
}

SEGMENTS = [("install", 150), ("card-config", 190), ("card-cert", 160), ("app-wifi", 230), ("send", 160),
            ("import", 190), ("ftp-form", 220), ("ftp-auto", 190)]


# ---------------- текст: PNG из Inter Tight (кириллица и латиница — разные файлы шрифта) ----------------
_FONT_BYTES, _FONTS = {}, {}


def _font(script, px, weight):
    key = (script, px, weight)
    if key not in _FONTS:
        if script not in _FONT_BYTES:
            f = TTFont(ROOT / "bot" / "fonts" / f"intertight-{script}.woff2")
            f.flavor = None
            buf = io.BytesIO()
            f.save(buf)
            _FONT_BYTES[script] = buf.getvalue()
        ft = ImageFont.truetype(io.BytesIO(_FONT_BYTES[script]), px)
        ft.set_variation_by_axes([weight])
        _FONTS[key] = ft
    return _FONTS[key]


def _runs(s):
    runs = []
    for ch in s:
        sc = "cyrillic" if "Ѐ" <= ch <= "ӿ" else "latin"
        if runs and runs[-1][0] == sc:
            runs[-1][1] += ch
        else:
            runs.append([sc, ch])
    return runs


class Doc:
    def __init__(self):
        self.layers, self.assets, self._txt = [], [], {}

    def text(self, s, size, color, weight=400):
        """-> (id, ширина, высота) в пикселях холста; картинка рисуется вдвое крупнее и показывается в 50 %."""
        key = (s, size, color, weight)
        if key not in self._txt:
            S = 2
            px = size * S
            runs = _runs(s)
            width = sum(_font(sc, px, weight).getlength(r) for sc, r in runs)
            asc, desc = _font("latin", px, weight).getmetrics()
            im = Image.new("RGBA", (math.ceil(width) + 4, asc + desc + 4), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            x = 2
            for sc, r in runs:
                f = _font(sc, px, weight)
                d.text((x, 2), r, font=f, fill=color + (255,))
                x += f.getlength(r)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            aid = f"t{len(self._txt)}"
            self.assets.append({"id": aid, "w": im.width, "h": im.height, "u": "",
                                "p": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "e": 1})
            self._txt[key] = (aid, im.width / S, im.height / S)
        return self._txt[key]

    def image(self, aid, im, q=82):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=q, optimize=True)
        self.assets.append({"id": aid, "w": im.width, "h": im.height, "u": "",
                            "p": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), "e": 1})

    def layer(self, nm, shapes=None, ip=0, op=1, parent=None, ty=4, ref=None, ind=None, **t):
        ind = ind or len(self.layers) + 1
        lay = {"ddd": 0, "ind": ind, "ty": ty, "nm": nm, "sr": 1, "ks": ks(**t), "ao": 0, "ip": ip, "op": op, "st": 0, "bm": 0}
        if ty == 4:
            lay["shapes"] = shapes
        if ref:
            lay["refId"] = ref
        if parent:
            lay["parent"] = parent
        self.layers.append(lay)
        return ind

    def label(self, s, x, y, size, color, ip, op, weight=400, align="left", o=100, p=None, s_anim=None, parent=None):
        """Подпись: x — левый край (или правый при align="right"), y — середина строки."""
        aid, w, h = self.text(s, size, color, weight)
        ax = w * 2 if align == "right" else (w if align == "center" else 0)
        return self.layer(f"txt:{s[:18]}", ty=2, ref=aid, ip=ip, op=op, a=(ax, h), p=p if p is not None else (x, y),
                          s=s_anim if s_anim is not None else 50, o=o, parent=parent)


def slide(x, y, t_in, t_out, first=False, last=False, d=36):
    """Положение экрана меню: въезжает справа, уезжает влево."""
    keys = [(t_in, [x if first else x + d, y]), (t_in + 8, [x, y])]
    if not last:
        keys += [(t_out - 8, [x, y]), (t_out, [x - d, y])]
    return an(*keys)


def vis(t_in, t_out, first=False, last=False):
    keys = [(t_in, 100 if first else 0), (t_in + 8, 100)]
    if not last:
        keys += [(t_out - 8, 100), (t_out, 0)]
    return an(*keys)


# ---------------- общие предметы ----------------
LCD_C, LCD_W, LCD_H = (340, 250), 540, 350
LX0, LX1 = LCD_C[0] - LCD_W / 2 + 26, LCD_C[0] + LCD_W / 2 - 26      # поля текста на экране
DIAL = (722, 300)


def closeup(doc, t0, t1, presses=()):
    """Крупный план задника камеры: большой экран слева, колесо справа; нажатия центральной кнопки — вспышкой ореола."""
    for n, t in enumerate(presses):
        doc.layer(f"press{n}", [group(ellipse(58), stroke(ACC, 5), nm="ring")], ip=t0, op=t1, p=DIAL,
                  s=an((t, [80, 80]), (t + 12, [180, 180])), o=an((t, 0), (t + 1, 100), (t + 12, 0)))
    btn = an(*[(t + k, v) for t in presses for k, v in ((0, [100, 100]), (3, [88, 88]), (8, [100, 100]))]) if presses else 100
    doc.layer("dial-btn", [group(ellipse(46), fill(INK), s=btn, nm="btn")], ip=t0, op=t1, p=DIAL)
    doc.layer("closeup", [
        group(ellipse(150, p=DIAL), stroke(INK, 6), nm="dial"),
        group(ellipse(14, p=(DIAL[0], DIAL[1] - 120)), ellipse(14, p=(DIAL[0], DIAL[1] + 120)), fill(INK), nm="dots"),
        group(rect(LCD_W, LCD_H, 22, p=LCD_C), stroke(INK, 6), fill(LCD), nm="lcd"),
        group(rect(880, 580, 70, p=(462, 292)), stroke(INK, 6), fill(PAPER), nm="body"),
    ], ip=t0, op=t1, o=an((t0, 0), (t0 + 6, 100)))


def menu_screen(doc, t0, t1, t_in, t_out, title, rows, hl, first=False, last=False, size=24, step=54, y0=170,
                values=None, icons=None, value_at=None, label_dim=False):
    """Экран меню камеры: заголовок, строки, ползунок выделения ореолом. hl — [(кадр, номер строки)].
    values — подписи справа; value_at — с какого кадра показывать значение строки (для формы)."""
    ip, op = max(t0, t_in), t1 if last else t_out
    o = vis(t_in, t_out, first, last)
    doc.label(title, LX0, 112, 24, T_LCD, ip, op, weight=500, o=o, p=slide(LX0, 112, t_in, t_out, first, last))
    for i, r in enumerate(rows):
        y = y0 + i * step
        x = LX0 + 12 + (44 if icons and icons[i] else 0)
        col = T_DIM if label_dim else T_LCD
        doc.label(r, x, y, size, col, ip, op, o=o, p=slide(x, y, t_in, t_out, first, last))
        if values and values[i]:
            vo = o if not value_at else an((value_at[i], 0), (value_at[i] + 5, 100))
            doc.label(values[i], LX1 - 12, y, size, T_LCD, ip, op, align="right", o=vo, p=slide(LX1 - 12, y, t_in, t_out, first, last))
        if icons and icons[i]:
            icons[i](doc, LX0 + 34, y, ip, op, o, slide(LX0 + 34, y, t_in, t_out, first, last))
    if len(hl) > 1:                      # стоит на строке, к следующей переезжает за 6 кадров
        keys = [(hl[0][0], [LCD_C[0], y0 + hl[0][1] * step])]
        for (_, prev), (t, i) in zip(hl, hl[1:]):
            keys += [(t - 6, [LCD_C[0], y0 + prev * step]), (t, [LCD_C[0], y0 + i * step])]
        pos = an(*keys)
    else:
        pos = st([LCD_C[0], y0 + hl[0][1] * step, 0])
    doc.layer("hl", [group(rect(LX1 - LX0, step - 8, 12), fill(ACC), nm="hl")], ip=ip, op=op, p=pos, o=o)
    doc.layer("rule", [group(rect(LX1 - LX0, 2, 1), fill(LCD_UI, 18), nm="rule")], ip=ip, op=op,
              p=slide(LCD_C[0], 138, t_in, t_out, first, last), o=o)


def app_icon_small(doc, x, y, ip, op, o, p):
    doc.layer("appicon", [group(ellipse(9), fill(LCD_UI), nm="core"), group(ellipse(18), fill(ACC, 75), nm="halo"),
                          group(rect(34, 34, 9), fill(LCD_DIM), nm="sq")], ip=ip, op=op, p=p, o=o)


def window(doc, t0, t1, title):
    """Светлое окно проводника с картой памяти."""
    doc.label(title, 196, WY, 22, T_DARK, t0, t1, weight=500, o=an((t0, 0), (t0 + 6, 100)))
    doc.layer("window", [
        group(rect(22, 28, 4, p=(162, WY)), fill(ACC), nm="card-icon"),
        *[group(ellipse(12, p=(x, WY)), fill(WIN_LINE), nm="ctl") for x in (604, 626, 648)],
        group(rect(560, 2, 0, p=(400, WY + 26)), fill(WIN_LINE), nm="bar-line"),
        group(rect(560, 27, 0, p=(400, WY + 13.5)), fill(WIN_BAR), nm="bar-low"),
        group(rect(560, 54, 18, p=(400, WY)), fill(WIN_BAR), nm="bar"),
        group(rect(560, 310, 18, p=(400, WY + 128)), fill(WIN), nm="win"),
        group(rect(560, 310, 18, p=(400, WY + 136)), fill(rgb(0, 0, 0), 9), nm="shadow"),
    ], ip=t0, op=t1, o=an((t0, 0), (t0 + 6, 100)), s=an((t0, [96, 96]), (t0 + 8, [100, 100])), a=(400, 250), p=(400, 250))


WY = 122                       # середина строки заголовка окна; окно 560×310, низ на 403
ROW0, ROW_STEP = 186, 58


def folder_shape(color):
    return [group(path([(-17, -9), (-17, 11), (17, 11), (17, -6), (-1, -6), (-5, -11), (-15, -11)], closed=True), fill(color), nm="folder")]


def file_shape():
    return [group(path([(-11, -15), (5, -15), (11, -9), (11, 15), (-11, 15)], closed=True), stroke(rgb(150, 146, 140), 2), fill(rgb(255, 255, 255)), nm="doc"),
            group(path([(5, -15), (5, -9), (11, -9)]), stroke(rgb(150, 146, 140), 2), nm="fold")]


def row(doc, name, idx, t0, t1, kind, t_in=None, indent=0, color=None, hint=None, hint_t=None):
    """Строка проводника: значок и имя. t_in — появление (въезжает сверху), иначе с начала сцены."""
    y = ROW0 + idx * ROW_STEP
    x = 164 + indent
    ip = t_in if t_in is not None else t0
    o = an((ip, 0), (ip + 8, 100)) if t_in is not None else an((t0, 0), (t0 + 6, 100))
    shapes = folder_shape(color or FOLDER) if kind == "folder" else file_shape()
    doc.layer(f"icon:{name}", shapes, ip=ip, op=t1, p=an((ip, [x, y - 10]), (ip + 8, [x, y])) if t_in is not None else (x, y), o=o)
    doc.label(name, x + 32, y, 24, T_DARK, ip, t1, weight=500 if kind == "folder" else 400,
              p=an((ip, [x + 32, y - 10]), (ip + 8, [x + 32, y])) if t_in is not None else None, o=o)
    if hint:
        doc.label(hint, 616, y, 18, T_ACC, hint_t[0], t1, align="right", weight=500, o=an((hint_t[0], 0), (hint_t[0] + 6, 100), (hint_t[1], 100), (hint_t[1] + 6, 0)))
    return y


def badge_ok(doc, x, y, t, t1, d=30):
    doc.layer("ok", check_badge(d, "ok"), ip=t, op=t1, p=(x, y), s=pop(t, 10))


def badge_bad(doc, x, y, t, t_out, d=30):
    k = d * .18
    doc.layer("bad", [group(path([(-k, -k), (k, k)]), path([(k, -k), (-k, k)]), stroke(LCD_UI, d * .13), nm="x"),
                      group(ellipse(d), fill(BAD), nm="disc")], ip=t, op=t_out, p=(x, y), s=pop(t, 10), o=an((t_out - 6, 100), (t_out, 0)))


def chip(doc, name, keys, ip, op, s_keys=None, o=None):
    """Перетаскиваемый файл: белая пилюля со значком и именем."""
    aid, w, h = doc.text(name, 22, T_DARK, 500)
    pw = w + 64
    base = doc.layer("chip-rig", ty=3, ip=ip, op=op, p=an(*keys), s=s_keys or 100)
    doc.layer("chip-name", ty=2, ref=aid, ip=ip, op=op, a=(0, h), p=(-pw / 2 + 48, 0), s=50, parent=base, o=o or 100)
    doc.layer("chip", [group(*file_shape(), p=(-pw / 2 + 26, 0), s=78, nm="doc"),
                       group(rect(pw, 50, 25), fill(rgb(255, 255, 255)), stroke(rgb(214, 210, 203), 2), nm="pill"),
                       group(rect(pw, 50, 25, p=(0, 6)), fill(rgb(0, 0, 0), 12), nm="shadow")], ip=ip, op=op, parent=base, o=o or 100)


def hl_row(doc, idx, t, t_out, color, o_val=18):
    y = ROW0 + idx * ROW_STEP
    doc.layer("rowhl", [group(rect(520, 48, 10), fill(color, o_val), nm="hl")], ip=t, op=t_out, p=(400, y),
              o=an((t, 0), (t + 4, 100), (t_out - 6, 100), (t_out, 0)))


# ---------------- сцены ----------------
def scene_install(doc, T, t0, t1):
    L = (212, 228)                        # ноутбук
    cam = doc.layer("cam-rig", ty=3, ip=t0, op=t1, p=(606, 236), s=62)
    # экран камеры: полоса установки, потом значок приложения с галочкой
    doc.layer("cam-ok", check_badge(26, "cok"), ip=t0, op=t1, parent=cam, p=(-44 + 26, 28 - 26), s=pop(t0 + 108, 10))
    doc.layer("cam-icon", [group(ellipse(14), fill(LCD_UI), nm="core"), group(ellipse(28), fill(ACC, 75), nm="halo"),
                           group(rect(54, 54, 15), fill(LCD_DIM), nm="sq")], ip=t0, op=t1, parent=cam, p=(-44, 28), s=pop(t0 + 100, 12))
    doc.layer("cam-bar", [group(rect(110, 8, 4, p=(55, 0)), fill(ACC), nm="fill")], ip=t0, op=t1, parent=cam, p=(-99, 60),
              s=an((t0 + 72, [0, 100]), (t0 + 98, [100, 100]), ease=G.SOFT), o=an((t0 + 70, 0), (t0 + 72, 100), (t0 + 98, 100), (t0 + 104, 0)))
    doc.layer("cam-track", [group(rect(110, 8, 4), fill(LCD_UI, 18), nm="track")], ip=t0, op=t1, parent=cam, p=(-44, 60),
              o=an((t0 + 68, 0), (t0 + 72, 100), (t0 + 98, 100), (t0 + 104, 0)))
    doc.layer("cam", [
        group(rect(196, 132, 12, p=(-44, 28)), stroke(INK, 5), fill(LCD), nm="lcd"),
        group(ellipse(66, p=(112, 40)), stroke(INK, 5), nm="dial"),
        group(ellipse(20, p=(112, 40)), fill(INK), nm="btn"),
        group(rect(62, 30, 9, p=(-114, -72)), stroke(INK, 3.5), fill(LCD), nm="evf"),
        group(rect(340, 220, 26), stroke(INK, 5), fill(PAPER), nm="body"),
        group(rect(56, 16, 8, p=(112, -114)), stroke(INK, 5), fill(PAPER), nm="shutter"),
    ], ip=t0, op=t1, parent=cam, o=an((t0, 0), (t0 + 6, 100)))
    # файл едет по кабелю
    chip(doc, "Proyavka.apk", [(t0 + 26, [L[0], L[1] - 20]), (t0 + 40, [L[0] + 40, L[1] + 70]), (t0 + 66, [470, 300]), (t0 + 74, [520, 262])],
         t0 + 24, t0 + 78, s_keys=an((t0 + 66, [100, 100]), (t0 + 74, [40, 40])), o=fade(t0 + 24, t0 + 28, t0 + 70, t0 + 76))
    # кабель USB
    doc.layer("cable", [group(path([(L[0] + 160, 300), (606 - 105 - 8, 268)], outs=[[60, 30], [0, 0]], ins=[[0, 0], [-70, 40]]),
                              trim(an((t0 + 4, 0), (t0 + 20, 100))), stroke(INK, 5), nm="cable"),
                        group(rect(16, 12, 3, p=(606 - 105 - 6, 268)), fill(INK), o=fade(t0 + 18, t0 + 20), nm="plug")], ip=t0, op=t1)
    # окно pmca-gui на экране ноутбука
    wx, wy = L[0], L[1] - 26
    doc.label("pmca-gui", wx - 112, wy - 64, 15, T_DARK, t0, t1, weight=500)
    doc.label("Install app", wx - 112, wy - 34, 14, T_ACC, t0, t1, weight=500)
    doc.label("Install selected app", wx, wy + 52, 14, T_WHITE, t0, t1, weight=500, align="center")
    doc.label(T["task_ok"], wx - 112, wy + 20, 13, T_DARK, t0 + 106, t1, o=an((t0 + 106, 0), (t0 + 112, 100)))
    doc.layer("pmca", [
        group(rect(150, 30, 15, p=(wx, wy + 52)), fill(ACC), s=an((t0 + 18, [100, 100]), (t0 + 21, [92, 92]), (t0 + 26, [100, 100])),
              a=(wx, wy + 52), p=(wx, wy + 52), nm="button"),
        group(rect(224, 6, 3, p=(112, 0)), fill(ACC), p=(wx - 112, wy + 0), s=an((t0 + 24, [0, 100]), (t0 + 98, [100, 100]), ease=G.SOFT),
              o=fade(t0 + 22, t0 + 24, t0 + 100, t0 + 104), nm="prog"),
        group(rect(224, 6, 3, p=(wx, wy)), fill(rgb(222, 219, 212)), o=fade(t0 + 20, t0 + 24, t0 + 100, t0 + 104), nm="track"),
        group(rect(60, 2, 1, p=(wx - 82, wy - 24)), fill(ACC), nm="tab-line"),
        group(rect(256, 1.5, 0, p=(wx, wy - 48)), fill(rgb(222, 219, 212)), nm="title-line"),
        group(rect(256, 160, 8, p=(wx, wy)), fill(WIN), nm="win"),
    ], ip=t0, op=t1, o=an((t0, 0), (t0 + 6, 100)))
    doc.layer("laptop", [
        group(path([(L[0] - 200, L[1] + 106), (L[0] + 200, L[1] + 106), (L[0] + 184, L[1] + 122), (L[0] - 184, L[1] + 122)], closed=True), fill(INK), nm="base"),
        group(rect(300, 206, 14, p=(L[0], L[1] - 4)), stroke(INK, 5), fill(PAPER), nm="screen"),
    ], ip=t0, op=t1, o=an((t0, 0), (t0 + 6, 100)))


def scene_card(doc, T, t0, t1, cert):
    row(doc, "DCIM", 0, t0, t1, "folder")
    name = "cacert.pem" if cert else "config.txt"
    hover0 = [400, ROW0]
    keys = [(t0 + 12, [720, 40]), (t0 + 38, hover0), (t0 + 44, [hover0[0] - 7, ROW0]), (t0 + 48, [hover0[0] + 7, ROW0]),
            (t0 + 52, [hover0[0] - 5, ROW0]), (t0 + 56, hover0), (t0 + 70, [600, 50])]
    hl_row(doc, 0, t0 + 40, t0 + 62, BAD, 14)
    badge_bad(doc, 618, ROW0, t0 + 42, t0 + 64)
    if cert:
        keys += [(t0 + 92, [400, ROW0 + ROW_STEP])]
        chip(doc, name, keys, t0 + 10, t0 + 96, s_keys=an((t0 + 88, [100, 100]), (t0 + 96, [60, 60])), o=fade(t0 + 10, t0 + 14, t0 + 90, t0 + 96))
        row(doc, name, 1, t0, t1, "file", t_in=t0 + 92, hint=T["root_hint"], hint_t=(t0 + 96, t1 - 10))
        hl_row(doc, 1, t0 + 94, t1 - 4, ACC, 14)
        badge_ok(doc, 652, ROW0 + ROW_STEP, t0 + 100, t1)
    else:
        row(doc, "PROYAVKA", 1, t0, t1, "folder", t_in=t0 + 70, color=ACC, hint=T["new_folder"], hint_t=(t0 + 76, t0 + 118))
        hl_row(doc, 1, t0 + 72, t0 + 120, ACC, 14)
        keys += [(t0 + 108, [400, ROW0 + ROW_STEP])]
        chip(doc, name, keys, t0 + 10, t0 + 116, s_keys=an((t0 + 106, [100, 100]), (t0 + 116, [50, 50])), o=fade(t0 + 10, t0 + 14, t0 + 110, t0 + 116))
        row(doc, name, 2, t0, t1, "file", t_in=t0 + 114, indent=40)
        hl_row(doc, 2, t0 + 116, t1 - 4, ACC, 14)
        badge_ok(doc, 652, ROW0 + 2 * ROW_STEP, t0 + 122, t1)
    window(doc, t0, t1, T["card"])            # окно — под строками и файлом: рисуется последним


def scene_app_wifi(doc, T, t0, t1):
    a, b, c, d = t0, t0 + 46, t0 + 92, t0 + 138
    # пароль: поле с точками, потом «Сохранено»
    px, py = LCD_C[0], 330
    for i in range(8):
        doc.layer(f"dot{i}", [group(ellipse(12), fill(LCD_UI), nm="d")], ip=d + 22 + i * 3, op=t1, p=(px - 64 + i * 18 + 40, py), s=pop(d + 22 + i * 3, 6))
    doc.label(T["password"], LX0 + 12, py, 20, T_DIM, d + 16, t1, o=an((d + 16, 0), (d + 20, 100)))
    doc.layer("pwbox", [group(rect(LX1 - LX0, 50, 12), stroke(LCD_UI, 2, o=40), nm="box")], ip=d + 16, op=t1, p=(LCD_C[0], py), o=an((d + 16, 0), (d + 20, 100)))
    doc.label(T["saved"], LX1 - 50, 170, 22, T_LCD, d + 56, t1, align="right", weight=500, o=an((d + 56, 0), (d + 60, 100)))
    badge_ok(doc, LX1 - 26, 170, d + 56, t1, d=26)
    menu_screen(doc, t0, t1, a, b, T["app"], [T["app_list"], T["intro"]], [(a, 0)], first=True)
    menu_screen(doc, t0, t1, b, c, T["app_list"], [T["proyavka"], T["remote"]], [(b, 0)], icons=[app_icon_small, None])
    menu_screen(doc, t0, t1, c, d, T["proyavka"], [T["send_new"], T["wifi"], T["exit"]], [(c, 0), (c + 16, 1)])
    menu_screen(doc, t0, t1, d, t1, T["wifi"], [T["my_phone"], T["home"]], [(d, 0)], last=True)
    closeup(doc, t0, t1, presses=(a + 36, b + 36, c + 36, d + 12, d + 50))


def scene_send(doc, T, t0, t1):
    a, b = t0, t0 + 34
    menu_screen(doc, t0, t1, a, b, T["proyavka"], [T["send_new"], T["wifi"], T["exit"]], [(a, 0)], first=True)
    # отправка: заголовок, строка состояния, полоса, итог
    o = an((b, 0), (b + 8, 100))
    doc.label(T["proyavka"], LX0, 112, 24, T_LCD, b, t1, weight=500, o=o)
    doc.label(T["connecting"], LX0 + 12, 186, 24, T_LCD, b, b + 30, o=an((b, 0), (b + 6, 100), (b + 24, 100), (b + 30, 0)))
    doc.label(T["sending"], LX0 + 12, 186, 24, T_LCD, b + 26, b + 92, o=an((b + 26, 0), (b + 32, 100), (b + 86, 100), (b + 92, 0)))
    doc.label(T["done"], LX0 + 12, 186, 24, T_LCD, b + 88, t1, weight=500, o=an((b + 88, 0), (b + 94, 100)))
    badge_ok(doc, LX1 - 26, 186, b + 92, t1, d=28)
    doc.layer("send-bar", [group(rect(LX1 - LX0 - 24, 10, 5, p=((LX1 - LX0 - 24) / 2, 0)), fill(ACC), nm="fill")], ip=b, op=t1,
              p=(LX0 + 12, 236), s=an((b + 30, [0, 100]), (b + 88, [100, 100]), ease=G.SOFT), o=an((b + 26, 0), (b + 30, 100)))
    doc.layer("send-track", [group(rect(LX1 - LX0 - 24, 10, 5), fill(LCD_UI, 18), nm="track")], ip=b, op=t1, p=(LCD_C[0], 236), o=an((b + 26, 0), (b + 30, 100)))
    doc.layer("rule2", [group(rect(LX1 - LX0, 2, 1), fill(LCD_UI, 18), nm="rule")], ip=b, op=t1, p=(LCD_C[0], 138), o=o)
    # кадры улетают вверх-вправо
    for n in range(3):
        t = b + 36 + n * 16
        doc.layer(f"frame{n}", ty=2, ref="photo", ip=t, op=t + 26, a=(60, 40), p=an((t, [LCD_C[0] - 60 + n * 60, 330]), (t + 24, [LCD_C[0] + 160 + n * 30, 40])),
                  s=an((t, [70, 70]), (t + 24, [30, 30])), o=fade(t, t + 4, t + 18, t + 24))
    closeup(doc, t0, t1, presses=(a + 24,))


def scene_import(doc, T, t0, t1):
    a, b, c, d = t0, t0 + 50, t0 + 100, t0 + 150
    menu_screen(doc, t0, t1, a, b, T["network"], [T["transfer"], T["wifi"], T["net_opt"]], [(a, 0), (a + 14, 1), (a + 24, 2)], first=True)
    menu_screen(doc, t0, t1, b, c, T["net_opt"], [T["dev_name"], T["import_cert"], T["reset_net"]], [(b, 0), (b + 16, 1)])
    menu_screen(doc, t0, t1, c, d, T["import_cert"], [T["ftp_func"]], [(c, 0)])
    # подтверждение: кнопка OK, потом галочка и «Сертификат импортирован»
    o = an((d, 0), (d + 8, 100))
    doc.label(T["import_cert"], LX0, 112, 24, T_LCD, d, t1, weight=500, o=o)
    doc.label(T["ok"], LCD_C[0], 300, 26, T_WHITE, d, t1, weight=500, align="center", o=an((d, 0), (d + 8, 100), (d + 20, 100), (d + 24, 0)))
    doc.layer("okbtn", [group(rect(160, 52, 26), fill(ACC), nm="ok")], ip=d, op=t1, p=(LCD_C[0], 300),
              s=an((d + 12, [100, 100]), (d + 15, [90, 90]), (d + 20, [100, 100])), o=an((d, 0), (d + 8, 100), (d + 20, 100), (d + 24, 0)))
    doc.label(T["imported"], LCD_C[0] + 22, 300, 24, T_LCD, d + 24, t1, weight=500, align="center", o=an((d + 24, 0), (d + 28, 100)))
    aid, w, _ = doc.text(T["imported"], 24, T_LCD, 500)
    badge_ok(doc, LCD_C[0] - w / 2 + 2, 300, d + 24, t1, d=28)
    closeup(doc, t0, t1, presses=(a + 40, b + 40, c + 40, d + 12))


def scene_ftp_form(doc, T, t0, t1):
    rows = [r for r, _ in T["form"]]
    vals = [v for _, v in T["form"]]
    start, dt = t0 + 14, 22
    hl = [(t0, 0)] + [(start + i * dt, i) for i in range(len(rows))]
    menu_screen(doc, t0, t1, t0, t1, T["server1"], rows, hl, first=True, last=True, size=19, step=33, y0=158,
                values=vals, value_at=[start + i * dt + 4 for i in range(len(rows))])
    closeup(doc, t0, t1, presses=tuple(start + i * dt + 2 for i in range(len(rows))))


def toggle(doc, x, y, t_on, t0, t1):
    doc.layer("knob", [group(ellipse(22), fill(LCD_UI), nm="k")], ip=t0, op=t1, p=an((t_on, [x - 11, y]), (t_on + 6, [x + 11, y])))
    doc.layer("track", [group(rect(52, 30, 15), fill(ACC, an((t_on, 0), (t_on + 6, 100))), nm="on"),
                        group(rect(52, 30, 15), fill(LCD_DIM), nm="off")], ip=t0, op=t1, p=(x, y))


def scene_ftp_auto(doc, T, t0, t1):
    rows = [T["ftp_on"], T["server"], T["auto"], T["ftype"]]
    step, y0 = 52, 172
    toggle(doc, LX1 - 40, y0, t0 + 20, t0, t1)
    toggle(doc, LX1 - 40, y0 + 2 * step, t0 + 66, t0, t1)
    menu_screen(doc, t0, t1, t0, t1, T["ftp_title"], rows, [(t0, 0), (t0 + 34, 1), (t0 + 50, 2), (t0 + 84, 3)], first=True, last=True,
                size=22, step=step, y0=y0, values=[None, T["server1"], None, T["jpeg"]], value_at=[0, t0 + 38, 0, t0 + 88])
    # кадр улетает на сервер
    t = t0 + 112
    doc.layer("photo-fly", ty=2, ref="photo", ip=t, op=t + 40, a=(60, 40), p=an((t, [LCD_C[0], 400]), (t + 30, [LCD_C[0] + 220, 30])),
              s=an((t, [90, 90]), (t + 30, [40, 40])), o=fade(t, t + 4, t + 26, t + 32))
    doc.label(T["sent"], LCD_C[0], 400, 20, T_LCD, t + 30, t1, align="center", weight=500, o=an((t + 30, 0), (t + 36, 100)))
    closeup(doc, t0, t1, presses=(t0 + 16, t0 + 62, t0 + 96))


def build(lang):
    T = TEXTS[lang]
    doc = Doc()
    src = Image.open(ROOT / "community" / "sample.jpg").convert("RGB")
    im = src.resize((240, 160), Image.LANCZOS)
    doc.image("photo", im.resize((120, 80), Image.LANCZOS))
    t, markers = 0, []
    for name, dur in SEGMENTS:
        t0, t1 = t, t + dur
        markers.append({"tm": t0, "cm": name, "dr": dur})
        {"install": lambda: scene_install(doc, T, t0, t1),
         "card-config": lambda: scene_card(doc, T, t0, t1, cert=False),
         "card-cert": lambda: scene_card(doc, T, t0, t1, cert=True),
         "app-wifi": lambda: scene_app_wifi(doc, T, t0, t1),
         "send": lambda: scene_send(doc, T, t0, t1),
         "import": lambda: scene_import(doc, T, t0, t1),
         "ftp-form": lambda: scene_ftp_form(doc, T, t0, t1),
         "ftp-auto": lambda: scene_ftp_auto(doc, T, t0, t1)}[name]()
        t = t1
    return {"v": "5.12.2", "fr": FR, "ip": 0, "op": t, "w": W, "h": H, "nm": f"proyavka-setup-{lang}", "ddd": 0,
            "assets": doc.assets, "layers": doc.layers, "markers": markers}


if __name__ == "__main__":
    out = ROOT / "bot" / "ui"
    out.mkdir(exist_ok=True)
    for lang in ("ru", "en"):
        data = json.dumps(build(lang), separators=(",", ":"), ensure_ascii=False)
        (out / f"setup.{lang}.json").write_text(data, encoding="utf-8")
        print(f"setup.{lang}.json: {len(data) / 1024:.0f} KB")
    sys.exit(0)
