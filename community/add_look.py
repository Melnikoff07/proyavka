#!/usr/bin/env python3
"""Каталог сообщества: добавить плёнку из кода (то, что человек прислал заявкой в GitHub) и нарисовать к ней превью.

    python community/add_look.py "proyavka-look:1:eyJ..." [--id my-look] [--ru "описание"] [--en "description"] [--photo кадр.jpg]
    python community/add_look.py --rebuild          # заново нарисовать превью всех плёнок каталога

Превью (community/looks/<id>.jpg, 900 px) рисуется здесь же на образце community/sample.jpg — так приложению не нужно
рисовать десятки кадров, чтобы показать каталог. Если автор приложил свой кадр (--photo), превью рисуется на нём, а сам
кадр без плёнки сохраняется как community/looks/<id>-before.jpg: ползунок «до/после» в приложении возьмёт его.
Код проверяется тем же clean_params, что и в боте: лишние поля отбрасываются, числа ставятся в допустимые пределы.
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PREFIX = "proyavka-look:1:"
HERE = Path(__file__).resolve().parent
FILE = HERE / "looks.json"
LOOKS = HERE / "looks"
SAMPLE = HERE / "sample.jpg"
EDGE = 900


def load_filmbot():
    """filmbot при импорте создаёт папки и требует переменные окружения — даём ему временную."""
    tmp = tempfile.mkdtemp()
    os.environ.update(CHAT_ID="1", BASE_DIR=tmp)
    sys.path.insert(0, str(HERE.parent / "bot"))
    import filmbot
    return filmbot, tmp


def open_photo(path):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")      # без EXIF: место съёмки в каталог не попадает


def render(fb, photo, params, out):
    from PIL import Image
    im = photo.copy()
    im.thumbnail((EDGE, EDGE), Image.LANCZOS)
    fb.film(im, "custom", 100, 1, params).save(out, "JPEG", quality=80, optimize=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?")
    ap.add_argument("--id", help="короткий адрес, латиница, цифры и дефис (по умолчанию из названия)")
    ap.add_argument("--ru", default="")
    ap.add_argument("--en", default="")
    ap.add_argument("--photo", help="кадр автора для превью (необязательно)")
    ap.add_argument("--rebuild", action="store_true", help="перерисовать превью всех плёнок")
    a = ap.parse_args()
    LOOKS.mkdir(exist_ok=True)
    cat = json.loads(FILE.read_text(encoding="utf-8"))

    fb, tmp = load_filmbot()
    try:
        if a.rebuild:
            photo = open_photo(SAMPLE)
            for e in cat["looks"]:
                if not (LOOKS / f"{e['id']}-before.jpg").exists():           # у плёнок со своим кадром превью не трогаем
                    render(fb, photo, fb.clean_params(e["p"]), LOOKS / f"{e['id']}.jpg")
            print(f"превью перерисованы: {len(cat['looks'])}")
            return
        if not a.code or not a.code.strip().startswith(PREFIX):
            sys.exit("нужен код плёнки (proyavka-look:1:…) или --rebuild")
        b = a.code.strip()[len(PREFIX):]
        try:
            data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8"))
            params = fb.clean_params(data.get("p"))
        except (ValueError, TypeError) as e:
            sys.exit(f"код не прошёл проверку: {e}")
        name = fb.clean_text(data.get("name"), 32) or "Look"
        by = fb.clean_text(data.get("by"), 40)
        lid = a.id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "look"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", lid):
            sys.exit("неверный --id")
        if any(e["id"] == lid for e in cat["looks"]):
            sys.exit(f"id «{lid}» уже есть в каталоге — задай другой через --id")
        own = open_photo(a.photo) if a.photo else None
        render(fb, own or open_photo(SAMPLE), params, LOOKS / f"{lid}.jpg")
        if own:
            from PIL import Image
            before = own.copy()
            before.thumbnail((EDGE, EDGE), Image.LANCZOS)
            before.save(LOOKS / f"{lid}-before.jpg", "JPEG", quality=85, optimize=True)
        desc = {k: v for k, v in (("ru", a.ru), ("en", a.en)) if v}
        cat["looks"].append({"id": lid, "name": name, "by": by, "desc": desc or {"ru": "", "en": ""}, "p": fb.params_json(params)})
        FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"добавлено: {lid} — «{name}», автор {by or '—'}; превью: community/looks/{lid}.jpg")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
