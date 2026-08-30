"""Тексты уведомлений об открытии и закрытии позиции (§9 ТЗ 9.1).

Модуль ЧИСТЫЙ: данные → строка. Отправка — существующей функцией
``src.notify.telegram.send_message``; логика сервиса ``notify`` НЕ ИЗМЕНЯЕТСЯ,
переиспользуется только отправка.

СЛОВО «ВИРТУАЛЬНО» В ТЕКСТЕ ОБЯЗАТЕЛЬНО и убираться не должно, пока позиции
виртуальные. Человек, читающий поток сообщений, не обязан помнить, какой этап
проекта сейчас идёт, — а сообщение «Открыта позиция BTC/USDT · вход 77 602.70»
без этого слова читается как отчёт о настоящей сделке.

ОГРАНИЧЕНИЯ ПОТОКА (NOTIFY_HOLD_MIN, NOTIFY_MAX_PER_HOUR) К ЭТИМ СООБЩЕНИЯМ НЕ
ПРИМЕНЯЮТСЯ: их не больше десяти в сутки по построению — пять слотов, открытие
и закрытие.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Часовой пояс отображения (заказчик в МСК). Сервер и БД живут в UTC.
_MSK = ZoneInfo("Europe/Moscow")

# Русские названия причин выхода. Ключи — те же машиночитаемые значения, что
# лежат в колонке exit_reason; человеку показывается перевод, запросу — ключ.
EXIT_RU = {
    "target": "цель достигнута",
    "stop": "сработал предел убытка",
    "timeout": "истёк срок",
    "ambiguous": "цель и предел в одной свече — итог по пределу",
}


def _esc(text: Any) -> str:
    """Экранирование под parse_mode=HTML."""
    return html.escape(str(text))


def _price(value: float) -> str:
    """Цена с разделителем тысяч. Знаков — по величине: у DOGE их нужно больше.

    Пять знаков после запятой у копеечных инструментов и два у дорогих — не
    украшение: 0.21 вместо 0.21437 у DOGE прячет ровно тот масштаб движения, в
    котором эта позиция и живёт.
    """
    value = float(value)
    digits = 2 if abs(value) >= 100 else (4 if abs(value) >= 1 else 6)
    return f"{value:,.{digits}f}".replace(",", " ")


def _msk(ts: datetime) -> str:
    """UTC → ``dd.mm HH:MM MSK``."""
    return ts.astimezone(_MSK).strftime("%d.%m %H:%M") + " MSK"


def signed_usd(value: float) -> str:
    """Сумма со ЗНАКОМ ВСЕГДА: ``+$0.03``, ``-$0.12``, и ``+$0.00`` при нуле.

    Знак печатается и при нуле намеренно: отсутствие знака читается как
    «величина неизвестна», а ноль — это измеренный ноль.
    """
    value = float(value)
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def balance_line(balance: dict[str, Any] | None) -> str | None:
    """Одна строка о состоянии счёта (Этап 9.1.1 §6.5).

    ``None`` означает «величины не получены» — тогда строка не печатается
    ВОВСЕ. Печатать вместо неё нули значило бы сообщить, что на счёте ничего
    нет, тогда как на самом деле неизвестно, сколько там.

    ПРИБЫЛЬ НЕ РЕИНВЕСТИРУЕТСЯ, и строка это показывает: «в позициях» считается
    по размеру слотов, а накопленный итог стоит отдельной величиной и в слоты
    не входит.
    """
    if not balance:
        return None
    return (
        f"Счёт: ${float(balance['capital_start']):.2f} старт · "
        f"${float(balance['in_positions']):.2f} в позициях · "
        f"${float(balance['free']):.2f} свободно · "
        f"итог {signed_usd(balance['realized_pnl'])}"
    )


def held_ru(seconds: float) -> str:
    """Длительность удержания словами: ``3 ч 41 мин``, ``17 мин``."""
    total = max(0, int(seconds))
    hours, minutes = divmod(total // 60, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def opened_text(
    *,
    symbol: str,
    entry_price: float,
    notional_usd: float,
    target_price: float,
    target_pct: float,
    stop_price: float,
    stop_pct: float,
    deadline_at: datetime,
    signal_id: int,
    probability: float | None,
    entry_lag_sec: int,
    balance: dict[str, Any] | None = None,
) -> str:
    """Сообщение об открытии позиции."""
    prob = "—" if probability is None else f"{float(probability):.2f}"
    lines = [
        "🟢 <b>Открыта позиция (виртуально)</b>",
        f"{_esc(symbol)} · вход {_price(entry_price)} · "
        f"${float(notional_usd):.2f}",
        f"Цель {_price(target_price)} (+{float(target_pct):.2f}%) · "
        f"предел {_price(stop_price)} (−{float(stop_pct):.2f}%)",
        f"Срок до {_msk(deadline_at)}",
        f"Сигнал #{int(signal_id)}, вероятность {prob}, "
        f"задержка входа {int(entry_lag_sec)} с",
    ]
    money = balance_line(balance)
    if money is not None:
        lines.append(money)
    return "\n".join(lines)


def closed_text(
    *,
    symbol: str,
    exit_reason: str,
    entry_price: float,
    exit_price: float,
    net_pnl_pct: float,
    net_pnl_usd: float,
    cost_pct: float,
    held_sec: float,
    balance: dict[str, Any] | None = None,
) -> str:
    """Сообщение о закрытии позиции.

    Издержки названы в тексте числом намеренно: итог показан УЖЕ за их вычетом,
    и без этой строки человек, сверяющий числа с ценами входа и выхода, каждый
    раз обнаруживал бы недостачу и искал ошибку.
    """
    reason = EXIT_RU.get(exit_reason, exit_reason)
    lines = [
        "🔵 <b>Закрыта позиция (виртуально)</b>",
        f"{_esc(symbol)} · {_esc(reason)}",
        f"Вход {_price(entry_price)} → выход {_price(exit_price)}",
        f"Итог {float(net_pnl_pct):+.2f}% (${float(net_pnl_usd):+.3f}) "
        f"с учётом издержек {float(cost_pct):.2f}%",
        f"В позиции {held_ru(held_sec)}",
    ]
    money = balance_line(balance)
    if money is not None:
        lines.append(money)
    return "\n".join(lines)
