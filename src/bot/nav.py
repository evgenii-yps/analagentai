"""Навигация по экранам бота: разбор и сборка ``callback_data`` с префиксом ``v1:``.

Данные кнопки Telegram ограничены 64 байтами, поэтому они короткие и несут только
адрес экрана. «🔄 Обновить» шлёт ТУ ЖЕ строку, что открыла экран: обновление — это
повторный показ того же адреса.

Прежние callback меню настроек (``tok``/``hor``/``thr``/``quiet``/``qoff``/``qf``/
``qt``/``menu``) сюда не входят и не меняются: их разбирает :mod:`src.bot.settings_menu`.
Неизвестная строка — ``None``, без исключений: нажатие на кнопку старого сообщения
после обновления бота не должно ронять обработчик.
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "v1:"
MAX_CALLBACK_BYTES = 64

# Экраны (имя совпадает с суффиксом callback_data).
MENU = "m"
ACCOUNT = "acc"
ACCOUNT_NEW = "accn"      # счёт НОВЫМ сообщением (кнопка под сообщением о сделке)
TRADES = "tr"
RESULTS = "res"
QUALITY = "sq"
SIGNALS = "sig"
SIGNAL_CARD = "sc"
AGENTS = "ag"
SYSTEM = "sys"
SYSTEM_DETAIL = "sysd"
SETTINGS = "set"
HELP = "help"

PERIODS = ("today", "7d", "30d", "all")
SIGNAL_MODES = ("s", "a")      # s — сильные (отправленные), a — все решения
DEFAULT_PERIOD = "7d"

_SIMPLE = {
    MENU, ACCOUNT, ACCOUNT_NEW, TRADES, QUALITY, AGENTS, SYSTEM, SYSTEM_DETAIL,
    SETTINGS, HELP,
}


@dataclass(frozen=True)
class NavTarget:
    """Адрес экрана: имя и необязательные уточнения (период, режим списка, номер)."""

    screen: str
    period: str | None = None       # RESULTS
    mode: str | None = None         # SIGNALS, SIGNAL_CARD
    signal_id: int | None = None    # SIGNAL_CARD

    @property
    def data(self) -> str:
        """Строка ``callback_data`` этого адреса."""
        if self.screen == RESULTS:
            return f"{PREFIX}{RESULTS}:{self.period or DEFAULT_PERIOD}"
        if self.screen == SIGNALS:
            return f"{PREFIX}{SIGNALS}:{self.mode or 's'}"
        if self.screen == SIGNAL_CARD:
            return f"{PREFIX}{SIGNAL_CARD}:{self.signal_id}:{self.mode or 's'}"
        return f"{PREFIX}{self.screen}"


def parse_nav(data: str | None) -> NavTarget | None:
    """``v1:res:7d`` → ``NavTarget``. Всё незнакомое и испорченное — ``None``."""
    raw = (data or "").strip()
    if not raw.startswith(PREFIX):
        return None
    parts = raw[len(PREFIX):].split(":")
    head, args = parts[0], parts[1:]
    if head in _SIMPLE and not args:
        return NavTarget(head)
    if head == RESULTS and len(args) == 1 and args[0] in PERIODS:
        return NavTarget(RESULTS, period=args[0])
    if head == SIGNALS and len(args) == 1 and args[0] in SIGNAL_MODES:
        return NavTarget(SIGNALS, mode=args[0])
    if head == SIGNAL_CARD and len(args) == 2 and args[1] in SIGNAL_MODES:
        if not args[0].isascii() or not args[0].isdigit():
            return None
        signal_id = int(args[0])
        if signal_id <= 0 or signal_id > 2**63 - 1:
            return None
        return NavTarget(SIGNAL_CARD, mode=args[1], signal_id=signal_id)
    return None


def button(text: str, target: NavTarget) -> dict[str, str]:
    """Кнопка инлайн-клавиатуры, ведущая на экран."""
    return {"text": text, "callback_data": target.data}


# Часто нужные кнопки.
HOME = NavTarget(MENU)


def home_button() -> dict[str, str]:
    return button("🏠 Меню", HOME)


def footer_row(refresh: NavTarget) -> list[dict[str, str]]:
    """Нижний ряд каждого экрана, кроме главного: «Обновить» и «Меню»."""
    return [button("🔄 Обновить", refresh), home_button()]
