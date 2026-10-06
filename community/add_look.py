#!/usr/bin/env python3
"""Добавить плёнку в каталог сообщества из кода (то, что человек прислал заявкой в GitHub).

    python community/add_look.py "proyavka-look:1:eyJ..." [--id my-look] [--ru "описание"] [--en "description"]

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
FILE = Path(__file__).with_name("looks.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--id", help="короткий адрес, латиница, цифры и дефис (по умолчанию из названия)")
    ap.add_argument("--ru", default="")
    ap.add_argument("--en", default="")
    a = ap.parse_args()

    code = a.code.strip()
    if not code.startswith(PREFIX):
        sys.exit("это не код плёнки Проявки")
    b = code[len(PREFIX):]
    data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8"))

    # проверка — тем же кодом, что в боте; filmbot при импорте создаёт папки, поэтому даём ему временную
    tmp = tempfile.mkdtemp()
    os.environ.update(CHAT_ID="1", BASE_DIR=tmp)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
    try:
        import filmbot                                     # noqa: E402
        params = filmbot.params_json(filmbot.clean_params(data.get("p")))
        name = filmbot.clean_text(data.get("name"), 32) or "Look"
        by = filmbot.clean_text(data.get("by"), 40)
    except ValueError as e:
        sys.exit(f"код не прошёл проверку: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lid = a.id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "look"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", lid):
        sys.exit("неверный --id")

    cat = json.loads(FILE.read_text(encoding="utf-8"))
    if any(e["id"] == lid for e in cat["looks"]):
        sys.exit(f"id «{lid}» уже есть в каталоге — задай другой через --id")
    desc = {k: v for k, v in (("ru", a.ru), ("en", a.en)) if v}
    cat["looks"].append({"id": lid, "name": name, "by": by, "desc": desc or {"ru": "", "en": ""}, "p": params})
    FILE.write_text(json.dumps(cat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"добавлено: {lid} — «{name}», автор {by or '—'}")


if __name__ == "__main__":
    main()
