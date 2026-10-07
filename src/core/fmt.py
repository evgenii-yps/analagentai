"""Единый русский формат чисел, денег, процентов и времени для бота и сообщений демо.

Русский формат: пробел между тысячами, запятая в дробной части. Функции ЧИСТЫЕ: без
сети, базы и часов. ``None`` на входе любой функции даёт прочерк «—».

Минус в знаковых числах — настоящий минус U+2212, а не дефис: в строках вида
«−$0,40» дефис выглядел бы переносом. Округление — «половина вверх» по десятичной
записи числа, а не по двоичному ``float``: 0,125 при двух знаках — 0,13, как и ждёт
человек с калькулятором.

Листы Google (``src/export``) эти функции НЕ используют: там числа идут числами.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

DASH = "—"
MINUS = "−"


def _dec(value: Any) -> Decimal | None:
    """Число → Decimal через строку (float не тянет двоичный хвост). Мусор → ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _round(number: Decimal, places: int) -> Decimal:
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _group(digits: str) -> str:
    """``1234567`` → ``1 234 567``."""
    out: list[str] = []
    while len(digits) > 3:
        out.append(digits[-3:])
        digits = digits[:-3]
    out.append(digits)
    return " ".join(reversed(out))


def _plain(number: Decimal, places: int) -> str:
    """Модуль числа с группами разрядов и запятой, ровно ``places`` знаков."""
    rounded = _round(abs(number), places)
    whole, _, frac = f"{rounded:f}".partition(".")
    text = _group(whole)
    return f"{text},{frac}" if places > 0 else text


def _sign(rounded_is_zero: bool, negative: bool, plus: bool) -> str:
    """Знак по ОКРУГЛЁННОМУ значению: «−0,00%» человеку ничего не говорит."""
    if rounded_is_zero:
        return ""
    if negative:
        return MINUS
    return "+" if plus else ""


def _zero_after_rounding(number: Decimal, places: int) -> bool:
    return _round(abs(number), places) == 0


def money(v: Any, places: int = 2) -> str:
    """``1000`` → ``$1 000,00``; отрицательное — ``−$3,00``."""
    number = _dec(v)
    if number is None:
        return DASH
    sign = _sign(_zero_after_rounding(number, places), number < 0, plus=False)
    return f"{sign}${_plain(number, places)}"


def money_signed(v: Any, places: int = 2) -> str:
    """Со знаком: ``+$0,12`` / ``−$0,40``; ноль — ``$0,00`` без знака."""
    number = _dec(v)
    if number is None:
        return DASH
    sign = _sign(_zero_after_rounding(number, places), number < 0, plus=True)
    return f"{sign}${_plain(number, places)}"


def pct(v: Any, places: int = 2, sign: bool = True) -> str:
    """Проценты: ``+0,16%`` / ``−0,19%``; ``sign=False`` — без плюса (минус остаётся)."""
    number = _dec(v)
    if number is None:
        return DASH
    mark = _sign(_zero_after_rounding(number, places), number < 0, plus=sign)
    return f"{mark}{_plain(number, places)}%"


def share(v01: Any) -> str:
    """Доля 0..1 → целые проценты: ``0.62`` → ``62%``."""
    number = _dec(v01)
    if number is None:
        return DASH
    return f"{_round(number * 100, 0):f}%"


def num(v: Any, places: int = 2) -> str:
    """Просто число с группами и запятой: ``0.7234`` → ``0,72``."""
    number = _dec(v)
    if number is None:
        return DASH
    mark = MINUS if number < 0 and not _zero_after_rounding(number, places) else ""
    return f"{mark}{_plain(number, places)}"


def price(v: Any) -> str:
    """Цена: от 100 — 2 знака; от 1 — 4; меньше 1 — 6. ``85614.3`` → ``85 614,30``."""
    number = _dec(v)
    if number is None:
        return DASH
    magnitude = abs(number)
    places = 2 if magnitude >= 100 else (4 if magnitude >= 1 else 6)
    return num(number, places)


def qty(v: Any) -> str:
    """Количество монеты без хвостовых нулей, до 8 знаков: ``0,0166``."""
    number = _dec(v)
    if number is None:
        return DASH
    rounded = _round(abs(number), 8)
    whole, _, frac = f"{rounded:f}".partition(".")
    frac = frac.rstrip("0")
    text = _group(whole) + (f",{frac}" if frac else "")
    return f"{MINUS}{text}" if number < 0 and rounded != 0 else text


def duration(sec: Any) -> str:
    """Длительность: ``5 ч 12 мин``; меньше часа — ``12 мин``; от суток — ``1 д 3 ч``.

    Нулевая вторая часть опускается: ``2 ч``, ``2 д``. Меньше минуты — «меньше минуты»
    (знак «<» в тексте под ``parse_mode=HTML`` Telegram принял бы за начало тега).
    """
    number = _dec(sec)
    if number is None:
        return DASH
    total = max(0, int(number))
    if total < 60:
        return "меньше минуты"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч" if hours else f"{days} д"


def tz_label(tz: ZoneInfo | str) -> str:
    """Подпись пояса: ``МСК`` для Москвы, иначе сокращение пояса (``UTC``, ``CEST``)."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    if zone.key == "Europe/Moscow":
        return "МСК"
    return datetime.now(zone).strftime("%Z") or zone.key


def local_dt(ts: datetime | None, tz: ZoneInfo | str) -> str:
    """``07.10 14:05 МСК`` — момент в часовом поясе уведомлений."""
    if ts is None:
        return DASH
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ts.astimezone(zone).strftime("%d.%m %H:%M") + " " + tz_label(zone)


def sign_emoji(v: Any) -> str:
    """Больше нуля — 🟢, меньше — 🔴, ноль или нет данных — ⚪."""
    number = _dec(v)
    if number is None or number == 0:
        return "⚪"
    return "🟢" if number > 0 else "🔴"
