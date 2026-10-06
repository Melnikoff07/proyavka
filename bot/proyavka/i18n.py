"""Два языка: строки интерфейса на языке того, кому отвечаем сейчас (у каждого пользователя свой)."""
import threading

from .config import LANG

def user_lang(uid):
    """Язык пользователя; ядро подменяет эту функцию, когда есть база пользователей."""
    return LANG

_CTX = threading.local()     # язык того, кому сейчас отвечаем (у каждого пользователя свой)


def cur_lang():
    return getattr(_CTX, "lang", None) or LANG


class Bi(str):
    """Строка на двух языках. Значение — язык текущего пользователя; tr() выбирает заново
    (для строк, созданных один раз при запуске: названия засветов, описания плёнок, кнопки меню)."""
    def __new__(cls, ru, en):
        s = super().__new__(cls, en if cur_lang() == "en" else ru)
        s.ru, s.en = ru, en
        return s

    def __reduce__(self):
        return Bi, (self.ru, self.en)


def L(ru, en):
    """Строка интерфейса на языке текущего пользователя."""
    return Bi(ru, en)


def tr(s):
    return (s.en if cur_lang() == "en" else s.ru) if isinstance(s, Bi) else s


class speak:
    """with speak(uid): тексты в этом потоке — на языке этого пользователя."""
    def __init__(self, uid):
        self.lang = user_lang(uid)

    def __enter__(self):
        self.prev = getattr(_CTX, "lang", None)
        _CTX.lang = self.lang

    def __exit__(self, *exc):
        _CTX.lang = self.prev
