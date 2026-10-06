"""Камера пользователя: FTP-вход и config.txt, пароль, настройка на сервере-приёмнике, инструкция в чате."""

import hashlib
import io
import os
import secrets
import shlex
import subprocess
from pathlib import Path

from .config import PROJECT_URL, log, remote
from .i18n import L, cur_lang, speak
from .users import ADMIN, set_user, user, user_lang
from .telegram import safe, tg
from .ingest import VPS_WATCH_RESTART
from .devices import save_config


APP_ROOT = Path(__file__).resolve().parent.parent.parent      # корень репозитория


CAMERA_CONFIG = APP_ROOT / "camera-config" / "config.txt"


FTP_ROOT_CERT = APP_ROOT / "camera-app" / "certs" / "isrgrootx1.pem"


CAM_HELPER = os.environ.get("CAM_HELPER", "/usr/local/lib/proyavka/proyavka-user")


def cam_helper(action, name, stdin=""):
    """Пользователи камер на сервере-приёмнике: FTP-вход, папка и токен приложения. Root-скрипт через sudo."""
    res = subprocess.run(remote(f"sudo -n {CAM_HELPER} {action} {shlex.quote(name)}"), input=stdin,
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"exit {res.returncode}")
    return res.stdout


# пароль FTP вводят на камере: только строчные и цифры без похожих (l/1, o/0) — 32^10 ≈ 10^15 вариантов
FTP_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def easy_password(n=10):
    return "".join(secrets.choice(FTP_ALPHABET) for _ in range(n))


def new_ftp_password(uid):
    """Сменить пароль FTP камеры на новый короткий (старый перестаёт работать)."""
    pw = easy_password()
    try:
        if uid == ADMIN:
            cam_helper("passwd", "camera", pw + "\n")
            save_config("FTP_PASS", pw)
            os.environ["FTP_PASS"] = pw
        else:
            u = ensure_camera(uid)
            cam_helper("add", f"u{uid}", f"{pw}\n{hashlib.sha256(u['cam_token'].encode()).hexdigest()}\n")
            set_user(uid, ftp_pass=pw)
    except Exception as e:
        log.warning("ftp password for %d: %s", uid, e)
        raise RuntimeError(L("Не получилось сменить пароль: сервер-приёмник от прежней версии — администратору: setup.py → «Обновить».",
                             "Could not change the password: the receiving server is from an older version — admin: setup.py → Update."))
    log.info("пароль FTP сменён у %d", uid)


def ensure_camera(uid):
    """Свои ключи камеры: токен приложения на Sony и FTP-пользователь со своей папкой. Создаются при первом /camera."""
    u = user(uid)
    if u.get("cam_token"):
        return u
    token, pw = secrets.token_urlsafe(32), easy_password()
    cam_helper("add", f"u{uid}", f"{pw}\n{hashlib.sha256(token.encode()).hexdigest()}\n")
    set_user(uid, cam_token=token, ftp_pass=pw)
    VPS_WATCH_RESTART.set()                   # новая папка — пусть мгновенные уведомления смотрят и её
    return user(uid)


def camera_access(uid):
    """FTP-вход и config.txt для камеры пользователя (у администратора — общий вход и файл мастера установки)."""
    from .botui import u_name
    domain = os.environ.get("DOMAIN", "")
    if uid == ADMIN:
        conf = CAMERA_CONFIG.read_bytes() if CAMERA_CONFIG.exists() else None
        return "camera", os.environ.get("FTP_PASS", "—"), conf
    try:
        u = ensure_camera(uid)
    except Exception as e:
        log.warning("camera for %d: %s", uid, e)
        with speak(ADMIN):
            safe("sendMessage", chat_id=ADMIN, text=L(
                f"Не получилось завести камеру для {u_name(uid)}: {e}\n"
                "Скорее всего, сервер-приёмник от прежней версии: запусти setup.py → «Обновить».",
                f"Could not set up a camera for {u_name(uid)}: {e}\n"
                "The receiving server is probably from an older version: run setup.py → \"Update\"."))
        raise RuntimeError(L("Не получилось завести камеру на сервере. Напиши администратору.",
                             "Could not set up a camera on the server. Please tell the admin."))
    conf = (L("# Настройки приложения «Проявка» для камеры Sony. Положи на карту в папку PROYAVKA.",
              "# Settings of the Proyavka app for Sony cameras. Put on the card into the PROYAVKA folder.")
            + f"\n\nurl = https://{domain}\ntoken = {u['cam_token']}\nlang = {user_lang(uid)}\n").encode()
    return f"u{uid}", u["ftp_pass"], conf


def send_camera_setup(uid):
    """Всё, что нужно положить в камеру, — файлами в чат: скачал, скинул на карту, готово."""
    domain = os.environ.get("DOMAIN", "")
    try:
        ftp_user, pw, conf = camera_access(uid)
    except RuntimeError as e:
        tg("sendMessage", chat_id=uid, text=str(e))
        return
    guide = f"{PROJECT_URL}/blob/main/docs"
    ext = ".md" if cur_lang() == "en" else ".ru.md"
    text = L(
        "📷 Настройка камеры\n\n"
        "Sony с приложениями (a6000–a6500, a7 II, RX100 III–V…)\n"
        f"1. Поставь приложение Proyavka.apk — пошагово: {guide}/sony-app{ext}\n"
        "2. На карте памяти создай папку PROYAVKA и положи в неё файл config.txt (ниже).\n"
        "3. В камере: Меню → Приложение → Проявка → Wi-Fi → выбери сеть (точку доступа телефона или дом) "
        "и введи пароль. Один раз — дальше камера подключается сама.\n"
        "4. Снимай в JPEG или RAW+JPEG → Проявка → Отправить новые.\n\n"
        "Камеры с отправкой по FTP (Sony A7C II, A7 IV, A1…)\n"
        f"сервер: {domain}\nпорт: 21\nпользователь: {ftp_user}\nпароль: {pw}\n"
        "папка: upload · FTPS (явный TLS) · пассивный режим\n"
        "Камере нужен корневой сертификат: положи файл cacert.pem (ниже) в корень карты "
        f"и импортируй его в меню сети. Пошагово: {guide}/ftp-cameras{ext}",
        "📷 Camera setup\n\n"
        "Sony with PlayMemories apps (a6000–a6500, a7 II, RX100 III–V…)\n"
        f"1. Install the Proyavka.apk app — step by step: {guide}/sony-app{ext}\n"
        "2. On the memory card create a folder PROYAVKA and put config.txt (below) into it.\n"
        "3. On the camera: Menu → Application → Proyavka → Wi-Fi → pick a network (phone hotspot or home) "
        "and enter the password. Once — after that the camera connects by itself.\n"
        "4. Shoot JPEG or RAW+JPEG → Proyavka → Send new.\n\n"
        "Cameras with FTP transfer (Sony A7C II, A7 IV, A1…)\n"
        f"server: {domain}\nport: 21\nuser: {ftp_user}\npassword: {pw}\n"
        "folder: upload · FTPS (explicit TLS) · passive mode\n"
        "The camera needs a root certificate: put cacert.pem (below) in the root of the card "
        f"and import it in the network menu. Step by step: {guide}/ftp-cameras{ext}")
    tg("sendMessage", chat_id=uid, disable_web_page_preview=True, text=text)
    cap = L("config.txt → на карту в папку PROYAVKA", "config.txt → onto the card, into the PROYAVKA folder")
    if conf:
        tg("sendDocument", files={"document": ("config.txt", io.BytesIO(conf))}, chat_id=uid, caption=cap)
    if FTP_ROOT_CERT.exists():
        with open(FTP_ROOT_CERT, "rb") as f:
            tg("sendDocument", files={"document": ("cacert.pem", f)}, chat_id=uid,
               caption=L("cacert.pem → в корень карты, для камер с FTP", "cacert.pem → root of the card, for FTP cameras"))
