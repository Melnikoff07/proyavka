"""Эталоны вида плёнок для test_film.py: python tests/make_film_ref.py [--v1] [--new].

--new  перезаписать film_ref.npz (встроенные плёнки как сейчас) — только когда вид меняют сознательно;
--v1   дописать в film_ref_v1.npz недостающие плёнки из bot/films_v1.json. Записи, что уже есть, не трогаются:
       их рисовал ещё прежний движок, по ним и видно, что новый воспроизводит старые плёнки.
Таблица цвета — каждая 97-я точка 3D-LUT, кадры — синтетическая сцена и общий образец, уменьшенные до 96×64."""
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


def images():
    return (("scene", synth.scene(7, 1500, 1000)),
            ("sample", Image.open(HERE.parent / "community" / "sample.jpg").convert("RGB")))


def shot(img, key, params=None):
    return np.asarray(film.film(img.copy(), key, 100, 3, params).resize((96, 64), Image.BOX), dtype=np.uint8)


def table(lut):
    return np.asarray(lut.table, dtype=np.float32)[::97]


def make_new():
    out = {}
    for key in film.PRESETS:
        out[f"{key}_lut"] = table(film.preset_lut(key))
        for name, img in images():
            out[f"{key}_{name}"] = shot(img, key)
    np.savez_compressed(HERE / "film_ref.npz", **out)
    print("film_ref.npz:", len(film.PRESETS), "плёнок")


def add_v1():
    path = HERE / "film_ref_v1.npz"
    out = dict(np.load(path)) if path.exists() else {}
    v1 = json.loads((HERE.parent / "bot" / "films_v1.json").read_text(encoding="utf-8"))["films"]
    added = []
    for key, v in v1.items():
        if f"{key}_lut" in out:
            continue
        params = film.clean_params(v["params"])
        out[f"{key}_lut"] = table(film._bake(params))
        for name, img in images():
            out[f"{key}_{name}"] = shot(img, "custom", params)
        added.append(key)
    np.savez_compressed(path, **out)
    print("film_ref_v1.npz: добавлены", ", ".join(added) or "—")


if __name__ == "__main__":
    if "--new" in sys.argv:
        make_new()
    if "--v1" in sys.argv:
        add_v1()
    if not {"--new", "--v1"} & set(sys.argv):
        sys.exit(__doc__)
