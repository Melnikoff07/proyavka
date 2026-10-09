"""Задачи, которые выполняются в процессах-работниках: подготовка кадра, экран для «Проявки», версия для чата, полный размер, превью. Только рендер и файлы: без базы и Telegram."""

import io
import os
from PIL import Image, ImageOps
from datetime import datetime

from .config import COMMUNITY_BUNDLED, PREVIEWS, VIEW_EDGE
from .util import save_atomic
from .film import PRESETS, film, look, preset_lut
from .imaging import (
    auto_pick, contact_sheet, crop_img, crop_tag, is_raw, leak_seed, light_leak, open_raw, raw_exif,
    read_exif, render, srgb_icc, to_srgb,
)


def job_warm():
    for k in PRESETS:
        preset_lut(k)
    return os.getpid()


def job_prepare(src, work_path, edge):
    if is_raw(src):
        taken, iso = raw_exif(src)
        im = open_raw(src, edge)
        im.thumbnail((edge, edge), Image.LANCZOS)
        taken = taken or datetime.now().strftime("%Y-%m-%d %H:%M")
        auto_key, reason = auto_pick(im, iso, int(taken[11:13]))
        save_atomic(im, work_path, 95)
        return taken, iso, auto_key, reason
    im = Image.open(src)
    taken, iso = read_exif(im)
    im.draft("RGB", (edge, edge))           # JPEG сразу декодируется в уменьшенном виде — в разы быстрее
    im = ImageOps.exif_transpose(to_srgb(im)).convert("RGB")
    im.thumbnail((edge, edge), Image.LANCZOS)
    taken = taken or datetime.now().strftime("%Y-%m-%d %H:%M")
    auto_key, reason = auto_pick(im, iso, int(taken[11:13]))
    save_atomic(im, work_path, 95)
    return taken, iso, auto_key, reason


def job_view(ph, view_path, thumb_path, chat_path=None):
    """Один рендер на правку: версия для чата (WORK_EDGE), из неё уменьшаются «Проявка» и миниатюра.
    Раньше чат и «Проявка» рисовались по отдельности — это лишние ~300 мс процессора на каждую правку.
    Без chat_path (догрузка старых кадров) рисуется только версия для «Проявки»."""
    if chat_path:
        img = render(ph)
        save_atomic(img, chat_path, 92)
        img.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    else:
        img = render(ph, mode="view")
    save_atomic(img, view_path, 88)
    img.thumbnail((500, 500))
    save_atomic(img, thumb_path, 85)
    return view_path, thumb_path


def job_chat(ph, path):
    return save_atomic(render(ph), path, 92)


# Что из съёмки переносим в готовый файл: когда, на что, с какими настройками. Координаты (GPS) не переносим.
EXIF_BASE = (0x010F, 0x0110)                                       # Make, Model
EXIF_SUB = (0x829A, 0x829D, 0x8827, 0x9003, 0x9004, 0x920A, 0xA434)  # выдержка, диафрагма, ISO, дата съёмки, фокусное, объектив


def full_exif(ph):
    """EXIF для готового файла: дата съёмки (иначе в «Фото» на телефоне кадр встаёт сегодняшним числом) и данные камеры из оригинала."""
    ex = Image.Exif()
    sub = ex.get_ifd(0x8769)
    src = ph.get("src")
    if src and os.path.exists(src) and not is_raw(src):
        try:
            with Image.open(src) as im:
                old = im.getexif()
                old_sub = old.get_ifd(0x8769)
            for t in EXIF_BASE:
                if old.get(t):
                    ex[t] = old[t]
            for t in EXIF_SUB:
                if old_sub.get(t):
                    sub[t] = old_sub[t]
        except Exception:
            pass
    taken = ph.get("taken")
    if taken and 0x9003 not in sub:
        try:
            stamp = datetime.strptime(str(taken)[:16], "%Y-%m-%d %H:%M").strftime("%Y:%m:%d %H:%M:00")
            sub[0x9003] = sub[0x9004] = stamp
        except ValueError:
            pass
    if ph.get("iso") and 0x8827 not in sub:
        sub[0x8827] = int(ph["iso"])
    if 0x9003 in sub:
        ex[0x0132] = sub[0x9003]
    ex[0x0131] = "Proyavka"
    return ex


def job_full(ph, path):
    return save_atomic(render(ph, full=True), path, 95, exif=full_exif(ph), icc_profile=srgb_icc())


BASE_EDGES = (420, 1000, 1600)     # 420 — полоска плёнок и редактор, 1000 — просмотр плёнки сообщества, 1600 — «до/после» в кадре


def base_path(ph, edge=420):
    return PREVIEWS / f"{ph['id']}_base{'' if edge == 420 else edge}{crop_tag(ph.get('crop'))}.jpg"


def preview_base(ph, edge=420):
    """Уменьшенный кадр без плёнки — основа превью; кадрированное сперва вырезается, потом уменьшается."""
    path = base_path(ph, edge)
    if not path.exists():
        b = Image.open(ph["work"])
        if ph.get("crop"):
            b = crop_img(b, ph["crop"])
        else:
            b.draft("RGB", (edge, edge))
        b = b.convert("RGB")
        b.thumbnail((edge, edge), Image.LANCZOS)
        save_atomic(b, str(path), 92)
    return Image.open(path).convert("RGB")


def job_base(ph, edge):
    preview_base(ph, edge)
    return str(base_path(ph, edge))


def job_preview(ph, key, strength, path, leak=""):
    base = preview_base(ph)
    out = look(base, ph, key, strength, ph["id"], fit=True)
    if leak:
        out = light_leak(out, leak, leak_seed(ph))
    return save_atomic(out, path, 84)


def job_sample(params, path, edge=900):
    """Плёнка на общем образце каталога (превью заявки и одобренной плёнки)."""
    im = Image.open(COMMUNITY_BUNDLED.parent / "sample.jpg").convert("RGB")
    im.thumbnail((edge, edge), Image.LANCZOS)
    img = film(im, "custom", 100, 1, params, fit=True)
    return save_atomic(img, path, 80)


def job_try(ph, params, strength, path=None, edge=420):
    """Кадр с плёнкой, которой ещё нет в базе: живой просмотр в редакторе и превью плёнок сообщества.
    Без path — JPEG байтами (редактор не засоряет диск), с path — файлом-кэшем."""
    out = film(preview_base(ph, edge), "custom", strength, ph["id"], params, fit=True)
    if path:
        return save_atomic(out, path, 84)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=84)
    return buf.getvalue()


def job_source(work, path):
    """Некадрированный кадр без плёнки — для экрана кадрирования."""
    b = Image.open(work)
    b.draft("RGB", (VIEW_EDGE, VIEW_EDGE))
    b = b.convert("RGB")
    b.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    return save_atomic(b, path, 85)


def job_contact(ph, path):
    return save_atomic(contact_sheet(ph), path, 88)
