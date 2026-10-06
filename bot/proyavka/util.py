"""Мелкие помощники без зависимостей от остального: файлы, JPEG в память, QR, имена файлов, множественное число."""
import hashlib
import io
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

def jpeg(img, q=92):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q, subsampling=0)
    buf.seek(0)
    return buf


# Эти функции выполняются в процессах-работниках: только рендер и файлы, без базы и Telegram.
def save_atomic(img, path, quality):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # уникально: несколько процессов могут писать один файл
    img.save(tmp, "JPEG", quality=quality, subsampling=0 if quality >= 90 else 2)
    try:
        os.replace(tmp, path)
    except PermissionError:          # Windows: файл как раз читает другой процесс — его копия не хуже
        if not os.path.exists(path):
            raise
        os.remove(tmp)
    return path


def plural_ru(n, one, few, many):
    a, b = n % 10, n % 100
    return one if a == 1 and b != 11 else few if 2 <= a <= 4 and not 12 <= b <= 14 else many


def fsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dir_bytes(*dirs):
    return sum(fsize(p) for d in dirs for p in d.iterdir() if p.is_file())


def remove(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


# Вне Telegram устройство входит своим ключом. Привязка — одноразовый код на 10 минут (короткий, чтобы вписать руками,
# и он же в ссылке и QR): его дают /link в боте или уже привязанное устройство. Браузер меняет код на постоянный ключ;
# на сервере — только sha256 ключа. Ключ лежит и в localStorage, и в cookie: iPhone при добавлении на экран «Домой»
# может перенести cookie из Safari, а localStorage — нет. Отозвать — /devices или «Устройства» в «Проявке».
try:
    import segno                      # QR-коды; без него — только ссылки
except ImportError:
    segno = None


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def qr_svg(text):
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff") if segno else None


def qr_png(text):
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=10, border=3)
    return buf.getvalue()


def html_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def download_name(ph):
    return f"{Path(ph['name']).stem}_{ph['preset']}.jpg"


def disposition(name):
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "photo.jpg"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
