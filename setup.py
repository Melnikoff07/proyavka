#!/usr/bin/env python3
"""Proyavka setup wizard / мастер установки «Проявки».

    python3 setup.py

Первый запуск — установка: язык, бот в Telegram, сервер-приёмник, автозапуск, файл настроек для камеры.
Повторный — меню: Wi-Fi камеры, хранение, обновление, состояние. Руками файлы править не нужно.
Работает на Linux с systemd (Raspberry Pi OS, Debian, Ubuntu). Только стандартная библиотека Python.
"""
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONF = ROOT / "config.env"
VENV = ROOT / ".venv"
CAM_DIR = ROOT / "camera-config"
CAM_CONF = CAM_DIR / "config.txt"
SSH_DIR = Path.home() / ".ssh"
ADMIN_KEY = SSH_DIR / "proyavka_admin"
SYNC_KEY = SSH_DIR / "proyavka_sync"
# под root (свежий VPS) бот работает от отдельного пользователя proyavka, а программа живёт в /opt/proyavka
IS_ROOT = os.geteuid() == 0
SUDO = [] if IS_ROOT else ["sudo"]
USER = "proyavka" if IS_ROOT else getpass.getuser()
OPT = Path("/opt/proyavka")

B, D, G, R, Y, X = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"

# сначала IPv4: у многих VPS IPv6 есть на бумаге, но не работает, и подключение висит минутами
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: sorted(_getaddrinfo(*a, **k), key=lambda r: r[0] != socket.AF_INET)

LANG = "ru"


def T(ru, en):
    """Строка на выбранном языке."""
    return en if LANG == "en" else ru


# ---------------- мелочи ----------------
def say(t=""):
    print(t, flush=True)


def title(t):
    say(f"\n{B}━━ {t}{X}")


def ok(t):
    say(f"{G}✓{X} {t}")


def warn(t):
    say(f"{Y}!{X} {t}")


def die(t):
    say(f"{R}✗ {t}{X}")
    sys.exit(1)


def ask(prompt, default=None, secret=False, check=None):
    hint = f" [{default}]" if default not in (None, "") else ""
    while True:
        raw = (getpass.getpass if secret else input)(f"{prompt}{hint}: ").strip()
        v = raw or (default or "")
        if not v:
            continue
        if check:
            err = check(v)
            if err:
                warn(err)
                continue
        return v


def yes(prompt, default=True):
    d = T("Д/н", "Y/n") if default else T("д/Н", "y/N")
    while True:
        v = input(f"{prompt} [{d}]: ").strip().lower()
        if not v:
            return default
        if v in ("д", "да", "y", "yes"):
            return True
        if v in ("н", "нет", "n", "no"):
            return False


def run(cmd, check=True, capture=False, **kw):
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    r = subprocess.run(cmd, **kw)
    if check and r.returncode != 0:
        if capture:
            say(r.stdout[-2000:])
        die(T("не получилось: ", "failed: ") + " ".join(shlex.quote(str(c)) for c in cmd))
    return r


def sudo_write(path, text, mode="644"):
    run(SUDO + ["install", "-m", mode, "/dev/stdin", str(path)], input=text.encode())


def load_env(path=CONF):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV_ORDER = [
    "# Proyavka settings. Change them with  python3 setup.py  — no need to edit by hand.",
    "# --- bot ---",
    "LANGUAGE", "BOT_TOKEN", "CHAT_ID", "WEBAPP_URL", "PROJECT_URL", "BASE_DIR", "VPS", "SSH_KEY", "REMOTE_DIR",
    "STORAGE_GB", "ORIGINALS_DAYS", "FAST_WORKERS", "HEAVY_WORKERS",
    "# --- setup wizard (not used by the bot) ---",
    "MODE", "SERVER_IP", "SERVER_ADMIN", "DOMAIN", "CAMERA_TOKEN", "FTP_PASS",
]


def save_env(env):
    lines = [k if k.startswith("#") else f"{k}={env[k]}" for k in ENV_ORDER if k.startswith("#") or k in env]
    lines += [f"{k}={v}" for k, v in env.items() if k not in ENV_ORDER]
    CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
    CONF.chmod(0o600)


def restart_bot():
    run(SUDO + ["systemctl", "restart", "proyavka-bot"])


# ---------------- язык ----------------
def step_language(env):
    global LANG
    say(f"{B}Language / Язык{X}")
    say("  1 — English")
    say("  2 — Русский")
    cur = "2" if env.get("LANGUAGE") == "ru" else "1"
    LANG = "en" if ask("1 / 2", cur, check=lambda v: None if v in ("1", "2") else "1 / 2") == "1" else "ru"
    env["LANGUAGE"] = LANG


# ---------------- Telegram ----------------
def tg(token, method, **params):
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)
    except OSError as e:
        return {"ok": False, "description": str(e)}


def step_bot(env):
    title(T("1. Бот в Telegram", "1. Telegram bot"))
    say(T("Открой в Telegram @BotFather → /newbot → придумай имя. Он пришлёт токен вида 123456:ABC-…",
          "In Telegram open @BotFather → /newbot → pick a name. It sends a token like 123456:ABC-…"))
    while True:
        token = ask(T("Токен бота", "Bot token"), env.get("BOT_TOKEN"),
                    check=lambda v: None if re.match(r"^\d+:[\w-]{30,}$", v)
                    else T("не похоже на токен: цифры, двоеточие, длинная строка",
                           "doesn't look like a token: digits, a colon, a long string"))
        me = tg(token, "getMe")
        if me.get("ok"):
            break
        warn(T("Telegram не принял токен: ", "Telegram rejected the token: ") + str(me.get("description")))
    bot = me["result"]["username"]
    ok(T("бот", "bot") + f" @{bot}")
    env["BOT_TOKEN"] = token

    if env.get("CHAT_ID") and not yes(T(f"Кадры приходят в чат {env['CHAT_ID']}. Оставить?",
                                        f"Frames go to chat {env['CHAT_ID']}. Keep it?")):
        env.pop("CHAT_ID")
    if not env.get("CHAT_ID"):
        run(SUDO + ["systemctl", "stop", "proyavka-bot"], check=False, capture=True)   # иначе он сам заберёт сообщение
        tg(token, "deleteWebhook")
        say(T(f"Теперь открой @{bot} и нажми «Старт» (или напиши ему что угодно). Жду до 3 минут…",
              f"Now open @{bot} and press Start (or send it anything). Waiting up to 3 minutes…"))
        offset, until = None, time.time() + 180
        chat = None
        while time.time() < until and not chat:
            r = tg(token, "getUpdates", timeout=20, **({"offset": offset} if offset else {}))
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or u.get("edited_message")
                if m:
                    chat = m["chat"]
        if not chat:
            die(T("сообщение так и не пришло — запусти мастер ещё раз", "no message arrived — run the wizard again"))
        tg(token, "getUpdates", offset=offset, timeout=0)
        who = chat.get("first_name") or chat.get("title") or chat["id"]
        ok(T("чат", "chat") + f": {who} ({chat['id']})")
        env["CHAT_ID"] = str(chat["id"])


# ---------------- сервер ----------------
def valid_ip(v):
    parts = v.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return None
    return T("нужен IPv4, например 203.0.113.7", "an IPv4 address is needed, e.g. 203.0.113.7")


def my_ip():
    """Внешний IPv4 этой машины, если он висит прямо на интерфейсе (так у VPS)."""
    r = run(["ip", "-4", "route", "get", "1.1.1.1"], check=False, capture=True)
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", r.stdout or "")
    if m and not re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)", m.group(1)):
        return m.group(1)
    return None


def ssh_base(env, key=ADMIN_KEY):
    return ["ssh", "-i", str(key), "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]


def keygen(path, comment):
    if not path.exists():
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)])
    return path.with_suffix(".pub").read_text().strip()


def step_mode(env):
    title(T("2. Где будет сервер", "2. Where the server lives"))
    say(T("Камере нужен адрес в интернете, куда слать кадры. Варианты:",
          "The camera needs an address on the internet to send frames to. Options:"))
    say(f"  {B}1{X} — " + T("дома нет белого IP: берём недорогой VPS как «почтовый ящик», а обработка идёт здесь, дома.",
                            "no public IP at home: a cheap VPS works as a mailbox, processing happens here at home."))
    say(f"  {B}2{X} — " + T("всё на одном сервере: этот компьютер сам доступен из интернета (VPS или белый IP с портами 80, 443, 21).",
                            "everything on one server: this machine is reachable from the internet (a VPS or public IP with ports 80, 443, 21)."))
    if IS_ROOT:
        say("\n" + T(f"Ты запустил мастер прямо на сервере (под пользователем root), поэтому всё будет здесь — {B}вариант 2{X}.",
                     f"You started the wizard on the server itself (as root), so everything goes here — {B}option 2{X}."))
        say(D + T("Вариант 1 выбирают, когда мастер запускают на домашнем компьютере (например, Raspberry Pi).",
                  "Option 1 is for running the wizard on a home computer (e.g. a Raspberry Pi).") + X)
        env["MODE"] = "local"
    else:
        cur = {"relay": "1", "local": "2"}.get(env.get("MODE"), "1")
        env["MODE"] = "relay" if ask(T("Вариант", "Option"), cur,
                                     check=lambda v: None if v in ("1", "2") else "1 / 2") == "1" else "local"

    found = my_ip() if env["MODE"] == "local" else None
    say("\n" + T("IP-адрес — это «номер» сервера в интернете, по нему камера и Telegram его находят.",
                 "The IP address is the server's number on the internet — that's how the camera and Telegram find it."))
    if found and not env.get("SERVER_IP"):
        say(D + T(f"Я определил его сам: {found}. Если он совпадает с IP из письма хостинга — просто нажми Enter.",
                  f"I detected it myself: {found}. If it matches the IP from your hosting email, just press Enter.") + X)
    elif env["MODE"] == "relay":
        say(D + T("Он есть в письме или личном кабинете хостинга, где ты взял VPS (четыре числа через точку).",
                  "You'll find it in the email or control panel of your VPS hosting (four numbers with dots).") + X)
    env["SERVER_IP"] = ask(T("IP сервера", "Server IP"), env.get("SERVER_IP") or found, check=valid_ip)

    dom = env["SERVER_IP"].replace(".", "-") + ".sslip.io"
    say("\n" + T("Для защищённого соединения (HTTPS) серверу нужно имя, как у сайта. Покупать его не надо:",
                 "For a secure connection (HTTPS) the server needs a name, like a website. No need to buy one:"))
    say(f"{B}{dom}{X} — " + T("бесплатное имя, которое всегда ведёт на твой IP (сервис sslip.io).",
                              "a free name that always points to your IP (sslip.io service)."))
    say(D + T("Нажми Enter. Если у тебя есть свой домен, настроенный на этот IP, можешь вписать его.",
              "Press Enter. If you have your own domain pointing to this IP, you can type it instead.") + X)
    env["DOMAIN"] = ask(T("Имя сервера", "Server name"), env.get("DOMAIN") or dom)
    env.setdefault("CAMERA_TOKEN", secrets.token_urlsafe(32))
    env.setdefault("FTP_PASS", secrets.token_urlsafe(12))


def step_server(env):
    title(T("3. Настройка сервера", "3. Server setup"))
    say(T("Сертификат HTTPS выдаёт Let's Encrypt. Условия: https://letsencrypt.org/repository/",
          "The HTTPS certificate is issued by Let's Encrypt. Terms: https://letsencrypt.org/repository/"))
    if not yes(T("Согласен с условиями Let's Encrypt?", "Do you agree to the Let's Encrypt terms?")):
        die(T("без сертификата камера и Mini App не смогут подключиться",
              "without a certificate the camera and the Mini App cannot connect"))
    email = input(T("E-mail для писем Let's Encrypt об истечении сертификата (можно пусто): ",
                    "E-mail for Let's Encrypt expiry notices (may be empty): ")).strip()
    params = {"DOMAIN": env["DOMAIN"], "PUBLIC_IP": env["SERVER_IP"], "CAMERA_TOKEN": env["CAMERA_TOKEN"],
              "FTP_PASS": env["FTP_PASS"], "LE_AGREE": "1", "LE_EMAIL": email, "LANGUAGE": LANG}

    if env["MODE"] == "local":
        params["BOT_USER"] = USER
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env") as f:
            f.write("".join(f"{k}={shlex.quote(v)}\n" for k, v in params.items()))
        os.chmod(f.name, 0o600)
        run(SUDO + ["bash", str(ROOT / "relay" / "install-relay.sh"), f.name])
        env["VPS"] = "local"
        env.pop("SSH_KEY", None)
        return

    admin = ask(T("Под каким пользователем заходить на VPS", "User to log in to the VPS"), env.get("SERVER_ADMIN", "root"))
    env["SERVER_ADMIN"] = admin
    host = f"{admin}@{env['SERVER_IP']}"
    keygen(ADMIN_KEY, f"proyavka-admin@{os.uname().nodename}")
    probe = run(ssh_base(env) + ["-o", "BatchMode=yes", host, "true"], check=False, capture=True)
    if probe.returncode != 0:
        say(T(f"Сейчас один раз спросит пароль от {host} — дальше мастер будет входить по ключу.",
              f"It will ask for the password of {host} once — after that the wizard logs in with a key."))
        run(["ssh-copy-id", "-i", str(ADMIN_KEY.with_suffix(".pub")), "-o", "StrictHostKeyChecking=accept-new", host])
    sync_pub = keygen(SYNC_KEY, f"proyavka-sync@{os.uname().nodename}")
    params.update(SYNC_USER="proyavka", SYNC_PUBKEY=sync_pub)

    remote = "/tmp/proyavka-relay"
    run(ssh_base(env) + [host, f"rm -rf {remote} && mkdir -p {remote} && chmod 700 {remote}"])
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "params.env"
        pf.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in params.items()))
        pf.chmod(0o600)
        run(["scp", "-q", "-i", str(ADMIN_KEY), str(ROOT / "relay" / "install-relay.sh"),
             str(ROOT / "relay" / "camera-recv.py"), str(pf), f"{host}:{remote}/"])
    sudo = "" if admin == "root" else "sudo "
    run(ssh_base(env) + ["-t", host, f"{sudo}bash {remote}/install-relay.sh {remote}/params.env; "
                                      f"r=$?; rm -rf {remote}; exit $r"])
    env["VPS"] = f"proyavka@{env['SERVER_IP']}"
    env["SSH_KEY"] = str(SYNC_KEY)
    run(["ssh", "-i", str(SYNC_KEY), "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
         env["VPS"], "true"])     # запомнить ключ сервера для бота
    ok(T("сервер готов", "server is ready"))


# ---------------- бот ----------------
def step_home(env):
    title(T("4. Бот", "4. The bot"))
    if shutil.which("apt-get"):
        run(SUDO + ["apt-get", "install", "-y", "-qq", "python3-venv", "rsync", "openssh-client",
                    "fonts-dejavu-core", "inotify-tools"], capture=True)
    if not VENV.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(VENV / "bin" / "pip"), "install", "-q", "--upgrade", "pip"], capture=True)
    say(T("Ставлю библиотеки Python (на Raspberry Pi это пара минут)…",
          "Installing Python libraries (a couple of minutes on a Raspberry Pi)…"))
    run([str(VENV / "bin" / "pip"), "install", "-q", "-r", str(ROOT / "bot" / "requirements.txt")], capture=True)
    env.setdefault("BASE_DIR", "/var/lib/proyavka/data" if IS_ROOT else str(Path.home() / "proyavka-data"))
    env.setdefault("REMOTE_DIR", "/srv/camera/upload/")
    env.setdefault("STORAGE_GB", "20")
    env.setdefault("ORIGINALS_DAYS", "14")
    cpus = os.cpu_count() or 2
    env.setdefault("FAST_WORKERS", str(max(1, min(3, cpus - 1))))
    env.setdefault("HEAVY_WORKERS", "1")
    env["WEBAPP_URL"] = f"https://{env['DOMAIN']}/"
    url = project_url()
    if url:
        env["PROJECT_URL"] = url
    save_env(env)
    own_files()
    install_services(env)


def project_url():
    """Адрес репозитория, откуда скачана программа, — на него ведут ссылки на инструкции в боте."""
    try:
        r = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "remote", "get-url", "origin"],
                           capture_output=True, text=True)
    except OSError:
        return None
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", r.stdout.strip())
    return f"https://github.com/{m.group(1)}" if m else None


def install_services(env):
    unit = f"""[Unit]
Description=Proyavka: bot
After=network-online.target
Wants=network-online.target

[Service]
User={USER}
WorkingDirectory={ROOT / 'bot'}
EnvironmentFile={CONF}
ExecStart={VENV / 'bin' / 'python'} {ROOT / 'bot' / 'filmbot.py'}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    sudo_write("/etc/systemd/system/proyavka-bot.service", unit)
    services = ["proyavka-bot"]
    if env["MODE"] == "relay":
        tunnel = f"""[Unit]
Description=Proyavka: Mini App tunnel to the server
After=network-online.target
Wants=network-online.target

[Service]
User={USER}
ExecStart=/usr/bin/ssh -N -i {SYNC_KEY} -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -R 127.0.0.1:8088:127.0.0.1:8088 {env['VPS']}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        sudo_write("/etc/systemd/system/proyavka-tunnel.service", tunnel)
        services.append("proyavka-tunnel")
    run(SUDO + ["systemctl", "daemon-reload"])
    run(SUDO + ["systemctl", "enable", "--now"] + services, capture=True)
    run(SUDO + ["systemctl", "restart"] + services)
    ok(T("бот запущен и будет стартовать сам после перезагрузки", "the bot is running and will start by itself after a reboot"))


# ---------------- камера ----------------
def read_wifi():
    nets = []
    if CAM_CONF.exists():
        vals = {}
        for line in CAM_CONF.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(wifi\d)\.(ssid|pass)\s*=\s?(.*)$", line)
            if m:
                vals.setdefault(m.group(1), {})[m.group(2)] = m.group(3).replace("\\\\", "\\")
        for k in sorted(vals):
            if vals[k].get("ssid"):
                nets.append((vals[k]["ssid"], vals[k].get("pass", "")))
    return nets


def prop(v):
    return v.replace("\\", "\\\\")


def write_camera_config(env, nets, prefix=""):
    CAM_DIR.mkdir(exist_ok=True)
    lines = ["# " + T("Настройки приложения «Проявка» для камеры Sony.", "Settings of the Proyavka app for Sony cameras."),
             "# " + T("Положи этот файл на карту памяти в папку PROYAVKA (получится PROYAVKA/config.txt).",
                      "Put this file on the memory card into the PROYAVKA folder (PROYAVKA/config.txt)."), "",
             f"url = https://{env['DOMAIN']}", f"token = {env['CAMERA_TOKEN']}", f"lang = {LANG}"]
    lines.append(f"prefix = {prefix}" if prefix else
                 "# prefix = a6300_      " + T("(приставка к имени файла; по умолчанию — модель камеры)",
                                               "(file name prefix; the camera model by default)"))
    lines += ["", "# " + T("Сети Wi-Fi удобнее добавить на камере: Проявка → Wi-Fi. Здесь — по желанию.",
                           "Wi-Fi networks are easier to add on the camera: Proyavka → Wi-Fi. Optional here.")]
    for i, (s, p) in enumerate(nets, 1):
        lines += [f"wifi{i}.ssid = {prop(s)}", f"wifi{i}.pass = {prop(p)}"]
    CAM_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
    CAM_CONF.chmod(0o600)


def step_camera(env, first=False):
    title(T("Wi-Fi для камеры (можно пропустить)", "Wi-Fi for the camera (optional)"))
    nets = read_wifi()
    if first and not nets:
        say(T("Сети проще добавить прямо на камере: приложение «Проявка» → Wi-Fi → выбрать сеть → ввести пароль.",
              "Networks are easiest to add on the camera itself: Proyavka app → Wi-Fi → pick a network → enter the password."))
        say(D + T("Здесь можно заранее вписать сети, если удобнее набирать пароль на клавиатуре. Нет — просто Enter.",
                  "You can pre-fill networks here if typing on a keyboard is easier. Otherwise just press Enter.") + X)
    while True:
        for i, (s, _) in enumerate(nets, 1):
            say(f"  {i}. {s}")
        if not nets:
            say(f"  {D}" + T("(пока ни одной сети)", "(no networks yet)") + X)
        a = input(T("Enter — готово, + добавить сеть, номер — удалить: ",
                    "Enter — done, + add a network, number — remove: ")).strip()
        if not a:
            break
        if a == "+":
            ssid = ask(T("Имя сети (SSID)", "Network name (SSID)"))
            pw = input(T("Пароль (пусто — открытая сеть): ", "Password (empty — open network): "))
            nets.append((ssid, pw))
        elif a.isdigit() and 1 <= int(a) <= len(nets):
            nets.pop(int(a) - 1)
    write_camera_config(env, nets)
    ok(T("файл для карты: ", "file for the card: ") + str(CAM_CONF))


def show_camera_help(env):
    title(T("Дальше — камера", "Next — the camera"))
    say(T(f"Открой своего бота в Telegram и отправь ему {B}/camera{X}.",
          f"Open your bot in Telegram and send it {B}/camera{X}."))
    say(T("Он пришлёт всё, что нужно положить на карту памяти, и пошаговую инструкцию:",
          "It sends everything to put on the memory card, plus a step-by-step guide:"))
    say(T("  • config.txt — для Sony с приложениями (a6000–a6500, a7 II, RX100 III–V…);",
          "  • config.txt — for Sony cameras with apps (a6000–a6500, a7 II, RX100 III–V…);"))
    say(T("  • cacert.pem и данные FTP — для камер, которые сами отправляют по FTP (A7C II, A7 IV, A1…).",
          "  • cacert.pem and FTP details — for cameras that upload over FTP themselves (A7C II, A7 IV, A1…)."))
    say(D + T("Команда работает всегда — если потеряешь файлы, просто отправь /camera ещё раз.",
              "The command always works — if you lose the files, just send /camera again.") + X)
    say("\n" + B + T("Лента кадров:", "Frame feed:") + X +
        T(" в боте кнопка «Проявка» рядом с полем ввода.", " the Proyavka button next to the input field in the bot."))


# ---------------- обслуживание ----------------
def menu_storage(env):
    title(T("Хранение и скорость", "Storage and speed"))
    num = T("нужно число", "a number is needed")
    env["STORAGE_GB"] = ask(T("Сколько ГБ можно занять под кадры", "How many GB may frames use"), env.get("STORAGE_GB", "20"),
                            check=lambda v: None if re.match(r"^\d+(\.\d+)?$", v) else num)
    env["ORIGINALS_DAYS"] = ask(T("Сколько дней хранить оригиналы (после — можно только смотреть)",
                                  "Days to keep originals (after that frames are view-only)"),
                                env.get("ORIGINALS_DAYS", "14"), check=lambda v: None if v.isdigit() else num)
    env["FAST_WORKERS"] = ask(T("Процессов для обработки (больше — быстрее, но горячее)",
                                "Processing workers (more — faster, but hotter)"),
                              env.get("FAST_WORKERS", "2"), check=lambda v: None if v.isdigit() and int(v) > 0 else num)
    save_env(env)
    restart_bot()
    ok(T("сохранено, бот перезапущен", "saved, the bot was restarted"))


def menu_language(env):
    step_language(env)
    save_env(env)
    write_camera_config(env, read_wifi())
    own_files()
    restart_bot()
    ok(T("язык сменён. Возьми новый config.txt через /camera, чтобы сменить язык и на камере.",
         "language changed. Get a new config.txt via /camera to switch the camera app too."))


def menu_update(env):
    title(T("Обновление", "Update"))
    if (ROOT / ".git").exists():
        run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "pull", "--ff-only"])
        own_files()
    run([str(VENV / "bin" / "pip"), "install", "-q", "-r", str(ROOT / "bot" / "requirements.txt")], capture=True)
    restart_bot()
    ok(T("обновлено", "updated"))


def menu_status(env):
    title(T("Состояние", "Status"))
    names = ["proyavka-bot"] + (["proyavka-tunnel"] if env.get("MODE") == "relay" else [])
    for n in names:
        st = run(["systemctl", "is-active", n], check=False, capture=True).stdout.strip()
        (ok if st == "active" else warn)(f"{n}: {st}")
    run(["journalctl", "-u", "proyavka-bot", "-n", "15", "--no-pager", "-o", "cat"], check=False)


def prepare_root():
    """Свежий VPS, работаем под root: заводим пользователя proyavka и переносим программу в /opt/proyavka."""
    if run(["id", "proyavka"], check=False, capture=True).returncode != 0:
        run(["useradd", "--system", "--create-home", "--home-dir", "/var/lib/proyavka",
             "--shell", "/usr/sbin/nologin", "proyavka"])
    if ROOT != OPT:
        say(T(f"Переношу программу в {OPT} — оттуда её будет запускать система.",
              f"Moving the program to {OPT} — the system will run it from there."))
        OPT.mkdir(parents=True, exist_ok=True)
        run(["cp", "-a", f"{ROOT}/.", str(OPT)])
        run(["chown", "-R", "proyavka:proyavka", str(OPT)])
        os.execv(sys.executable, [sys.executable, str(OPT / "setup.py")] + sys.argv[1:] + [f"--lang={LANG}"])


def own_files():
    """Под root файлы создаются от root — отдаём их пользователю, от которого работает бот."""
    if IS_ROOT:
        run(["chown", "-R", f"{USER}:{USER}", str(ROOT)])


def install(env):
    global LANG
    lang_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--lang=")), None)
    if lang_arg in ("ru", "en"):
        LANG = env["LANGUAGE"] = lang_arg      # уже выбран до переноса в /opt/proyavka
    else:
        step_language(env)
    say(f"\n{B}" + T("Мастер установки «Проявки»", "Proyavka setup wizard") + f"{X}  {D}" +
        T("(Ctrl+C — выйти в любой момент)", "(Ctrl+C — quit at any time)") + X)
    if not shutil.which("systemctl"):
        die(T("нужен Linux с systemd (Raspberry Pi OS, Debian, Ubuntu)", "Linux with systemd is required (Raspberry Pi OS, Debian, Ubuntu)"))
    if IS_ROOT:
        prepare_root()
    step_bot(env)
    save_env(env)
    step_mode(env)
    save_env(env)
    step_server(env)
    save_env(env)
    step_home(env)
    step_camera(env, first=True)
    own_files()
    show_camera_help(env)
    say(f"\n{G}{B}" + T("Готово!", "Done!") + X + T(" Сними кадр — через полминуты он придёт в бота.",
                                                     " Take a shot — in half a minute it arrives in the bot."))


def main():
    global LANG
    if IS_ROOT and ROOT != OPT and (OPT / "config.env").exists():
        os.execv(sys.executable, [sys.executable, str(OPT / "setup.py")] + sys.argv[1:])   # уже установлено там
    env = load_env()
    if not env.get("DOMAIN") or "--install" in sys.argv:
        return install(env)
    LANG = env.get("LANGUAGE", "ru")
    while True:
        items = [(T("Wi-Fi для камеры (файл на карту)", "Wi-Fi for the camera (file for the card)"),
                  lambda: (step_camera(env), own_files(), show_camera_help(env))),
                 (T("Показать, что делать на камере", "Show what to do on the camera"), lambda: show_camera_help(env)),
                 (T("Хранение и скорость", "Storage and speed"), lambda: menu_storage(env)),
                 (T("Язык / Language", "Language / Язык"), lambda: menu_language(env)),
                 (T("Состояние и последние сообщения бота", "Status and recent bot log"), lambda: menu_status(env)),
                 (T("Обновить до новой версии", "Update to the latest version"), lambda: menu_update(env)),
                 (T("Сменить бота или чат", "Change the bot or chat"),
                  lambda: (step_bot(env), save_env(env), restart_bot())),
                 (T("Установить заново (сервер, бот, всё)", "Reinstall (server, bot, everything)"), lambda: install(env))]
        say(f"\n{B}" + T("«Проявка» — настройки", "Proyavka — settings") + f"{X}  {D}{env.get('DOMAIN')}{X}")
        for i, (name, _) in enumerate(items, 1):
            say(f"  {i}. {name}")
        a = input(T("Номер (Enter — выход): ", "Number (Enter — quit): ")).strip()
        if not a:
            return
        if a.isdigit() and 1 <= int(a) <= len(items):
            items[int(a) - 1][1]()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n" + T("выход", "bye"))
