#!/usr/bin/env python3
"""Прогнать фото через плёнки прямо на компьютере — без Telegram и сервера.
Try the films on a photo locally — no Telegram, no server.

    python3 bot/try_films.py photo.jpg                   # лист со всеми плёнками -> photo_films.jpg
    python3 bot/try_films.py photo.jpg night800          # одна плёнка в полном размере -> photo_night800.jpg
    python3 bot/try_films.py photo.jpg night800 150 orb  # сила 150 % и засвет «orb»

Удобно, когда подбираешь параметры своей плёнки в PRESETS (bot/filmbot.py): поменял число — запустил — посмотрел.
"""
import os
import sys
import tempfile
from pathlib import Path

# бот при импорте читает настройки из окружения — для пробы хватит заглушек
os.environ.setdefault("BOT_TOKEN", "0:local")
os.environ.setdefault("CHAT_ID", "0")
os.environ.setdefault("BASE_DIR", tempfile.mkdtemp(prefix="proyavka-try-"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import filmbot as fb  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    key = fb.canon(sys.argv[2]) if len(sys.argv) > 2 else None
    strength = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    leak = sys.argv[4] if len(sys.argv) > 4 else ""
    if key and key not in fb.PRESETS:
        sys.exit("films: " + ", ".join(fb.PRESETS))
    if leak and leak not in fb.LEAKS:
        sys.exit("leaks: " + ", ".join(fb.LEAKS))

    if not key:
        out = src.with_name(f"{src.stem}_films.jpg")
        fb.contact_sheet({"id": 1, "work": str(src), "strength": strength}).save(out, quality=90)
    else:
        img = fb.ImageOps.exif_transpose(fb.Image.open(src)).convert("RGB")
        img = fb.film(img, key, strength, seed=1)
        if leak:
            img = fb.light_leak(img, leak, seed=1)
        out = src.with_name(f"{src.stem}_{key}.jpg")
        img.save(out, quality=92)
    print(out)


if __name__ == "__main__":
    main()
