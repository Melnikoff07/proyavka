"""Оверлеи: список для «Проявки» (встроенные засветы и свои картинки), загрузка и удаление своих картинок."""

import re
import time
from pathlib import Path

from .config import PREVIEWS, log
from .i18n import L, tr
from .util import remove
from .imaging import BLENDS, LEAKS, OV_MAX_COUNT, OV_MAX_BYTES, OV_OWNER, is_overlay, overlay_dir, save_overlay
from .database import q, run
from .users import load_overlays, user_overlays
from .scheduler import schedule_view


def overlays_json(uid, guest=False):
    """Оверлеи для экрана кадра: встроенные засветы, потом свои картинки (гостю альбома свои хозяина не показываются)."""
    items = [{"key": k, "name": tr(v[0]), "desc": tr(v[1])} for k, v in LEAKS.items()]
    if not guest:
        items += [{"key": f"ov{r['id']}", "name": r["name"], "desc": L("свой оверлей", "your overlay"), "own": True}
                  for r in user_overlays(uid)]
    return items


def blends_json():
    return [{"key": k, "name": tr(v)} for k, v in BLENDS.items()]


def add_overlay(owner, name, data):
    if len(data) > OV_MAX_BYTES:
        raise ValueError(L("картинка больше 30 МБ", "the image is larger than 30 MB"))
    if len(user_overlays(owner)) >= OV_MAX_COUNT:
        raise ValueError(L(f"уже {OV_MAX_COUNT} своих оверлеев — удали ненужные (долгое нажатие)",
                           f"already {OV_MAX_COUNT} own overlays — delete some (long press)"))
    name = re.sub(r"[\x00-\x1f]", "", Path(name or "").stem).strip()[:32] or L("Оверлей", "Overlay")
    oid = run("INSERT INTO overlays(owner, name, created) VALUES (?,?,?)", (owner, name, time.time()))
    try:
        _, w, h = save_overlay(data, overlay_dir(owner), f"ov{oid}")
    except Exception:
        run("DELETE FROM overlays WHERE id=?", (oid,))
        raise
    run("UPDATE overlays SET w=?, h=? WHERE id=?", (w, h, oid))
    load_overlays()
    log.info("overlay ov%d «%s» (%dx%d) у %d", oid, name, w, h, owner)
    return {"key": f"ov{oid}", "name": name, "w": w, "h": h}


def delete_overlay(owner, key):
    """Убрать свою картинку. Кадры с ней остаются без оверлея и перерисовываются."""
    if not is_overlay(key) or OV_OWNER.get(key) != owner:
        raise ValueError(L("нет такого оверлея", "no such overlay"))
    rows = q("SELECT id FROM photos WHERE owner=? AND leak_kind=?", (owner, key))
    for r in rows:
        run("UPDATE photos SET leak=0, leak_kind='edge', rev=rev+1, updated=? WHERE id=?", (time.time(), r["id"]))
        schedule_view(r["id"], prio=1, uid=owner)
    run("DELETE FROM overlays WHERE id=? AND owner=?", (int(key[2:]), owner))
    for ext in (".png", ".jpg"):
        remove(str(overlay_dir(owner) / f"{key}{ext}"))
    for f in PREVIEWS.glob(f"*_{key}-*.jpg"):
        remove(str(f))
    load_overlays()
    return len(rows)

