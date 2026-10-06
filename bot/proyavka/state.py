"""Состояние запуска: смещение чтения обновлений Telegram и перенос настроек из прежних версий (state.json)."""

import json

from .config import STATE_FILE
from .film import PRESETS, canon
from .users import ADMIN, set_user


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def migrate_state(state):
    """Плёнка по умолчанию жила в state.json — теперь она у каждого пользователя своя (у администратора — прежняя)."""
    old = canon(state.pop("default", "") or "")
    state.pop("preset", None)
    if old and (old == "auto" or old in PRESETS):           # один раз: после переноса в state.json его нет
        set_user(ADMIN, default_film=old)
    save_state(state)
