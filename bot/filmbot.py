#!/usr/bin/env python3
"""
filmbot — камера → сервер-приёмник → плёночный лук → приложение «Проявка» и (по желанию) Telegram.
Настройки — переменные окружения (config.env), их пишет мастер setup.py.

Это точка входа (её запускает служба и мастер установки). Весь код — в пакете proyavka/, снизу вверх:

  config, i18n, util   настройки из окружения, два языка, мелкие помощники
  film, imaging        движок плёнок (цвет, свечение, зерно, LUT, свои плёнки) и обработка кадра (RAW, кроп, засветы)
  pwa, database, users значки и манифест приложения; SQLite; пользователи и их настройки
  jobs, pools          задачи в процессах-работниках и пулы процессов
  telegram, sessions   Telegram API и кнопки; сессии, вход, подпись ссылок на картинки
  push, scheduler      уведомления; очереди отрисовки, пакеты, экспорт, обновление сообщений
  storage, photos      лимиты места и чистка; корзина кадров
  state, looks         state.json; свои плёнки и сообщество (в том числе приём плёнок на сервере-приёмнике)
  albums, ingest       альбомы по ссылке; приём кадров с камеры и из приложения
  devices, camera      устройства и аккаунт; камера пользователя
  invites, botui       приглашения и пользователи; бот в чате
  web                  веб-сервер приложения

Модуль импортирует только нижние слои; обращение вверх (и к подменяемым в тестах именам) — импортом внутри функции.
"""
import socket
import sys
import threading
import time

from proyavka import config
from proyavka.botui import handle_updates
from proyavka.config import LOCAL, RAW_MISSING, TMP, VPS, log
from proyavka.database import init_db
from proyavka.devices import BOT_RESET, new_pair, show_code
from proyavka.i18n import speak
from proyavka.ingest import ingest_loop
from proyavka.invites import set_commands
from proyavka.pools import init_pools
from proyavka.scheduler import dispatcher, tg_worker
from proyavka.state import load_state, migrate_state
from proyavka.storage import cleanup
from proyavka.telegram import safe
from proyavka.users import ADMIN, CLEANUP_MINUTES, USERS
from proyavka.util import remove, segno
from proyavka.web import backfill_fingerprints, backfill_views, start_web

_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    """Сначала IPv4: у многих VPS IPv6 есть на бумаге, но пакеты уходят в никуда, и подключение висит минутами."""
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda r: r[0] != socket.AF_INET)


socket.getaddrinfo = _ipv4_first


def main():
    from proyavka.ingest import vps_watch
    init_db()
    state = load_state()
    migrate_state(state)
    if config.BOT_TOKEN:
        safe("deleteMyCommands")              # команды теперь у каждого свои (язык, /invite у администратора)
        for uid in list(USERS):
            with speak(uid):
                set_commands(uid)
    for p in TMP.iterdir():
        remove(str(p))
    threading.Thread(target=dispatcher, daemon=True, name="dispatcher").start()
    init_pools()
    threading.Thread(target=tg_worker, daemon=True, name="tg").start()
    start_web()
    threading.Thread(target=vps_watch, daemon=True, name="vps-watch").start()
    threading.Thread(target=ingest_loop, args=(state,), daemon=True, name="ingest").start()
    backfill_fingerprints()
    backfill_views()
    if RAW_MISSING:
        log.warning("RAW включён, но нет библиотеки rawpy — RAW выключен. Поставить: .venv/bin/pip install rawpy "
                    "(или setup.py --raw=1)")
    log.info("filmbot v6.0 started (%s), пользователей: %d, Telegram: %s", "локально" if LOCAL else VPS, len(USERS),
             "да" if config.BOT_TOKEN else "нет — только приложение")
    last_clean = 0.0
    while True:
        if BOT_RESET.is_set():                # бота подключили из «Проявки»
            BOT_RESET.clear()
            state["offset"] = 0
            for uid in list(USERS):
                with speak(uid):
                    set_commands(uid)
        if config.BOT_TOKEN:
            handle_updates(state)   # главный поток занят только кнопками бота
        else:
            time.sleep(1)
        now = time.time()
        if now - last_clean >= CLEANUP_MINUTES * 60:
            try:
                cleanup()
            except Exception:
                log.exception("cleanup failed")
            last_clean = now


def pair_cli():
    """filmbot.py --pair: код и QR для первого устройства администратора (зовёт мастер установки)."""
    init_db()
    code, link = new_pair(ADMIN)
    print(link)
    if segno:
        segno.make(link, error="m").terminal(compact=True)
    print(show_code(code))


if __name__ == "__main__":
    pair_cli() if "--pair" in sys.argv else main()
