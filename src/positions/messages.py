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
    "data_gap": "пробел в данных — исход не измерен",
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


def bars_ru(count: int) -> str:
    """Число баров по-русски: ``1 бар``, ``3 бара``, ``11 баров``.

    Склонение здесь не украшение: «увидели 3 баров» читается как опечатка, а
    сообщение, выглядящее опечаткой, читают невнимательно.
    """
    count = int(count)
    tail = abs(count) % 100
    last = abs(count) % 10
    if 11 <= tail <= 14 or last == 0 or last >= 5:
        word = "баров"
    elif last == 1:
        word = "бар"
    else:
        word = "бара"
    return f"{count} {word}"


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
) -> str:
    """Сообщение об открытии позиции."""
    prob = "—" if probability is None else f"{float(probability):.2f}"
    return (
        "🟢 <b>Открыта позиция (виртуально)</b>\n"
        f"{_esc(symbol)} · вход {_price(entry_price)} · "
        f"${float(notional_usd):.2f}\n"
        f"Цель {_price(target_price)} (+{float(target_pct):.2f}%) · "
        f"предел {_price(stop_price)} (−{float(stop_pct):.2f}%)\n"
        f"Срок до {_msk(deadline_at)}\n"
        f"Сигнал #{int(signal_id)}, вероятность {prob}, "
        f"задержка входа {int(entry_lag_sec)} с"
    )


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
) -> str:
    """Сообщение о закрытии позиции.

    Издержки названы в тексте числом намеренно: итог показан УЖЕ за их вычетом,
    и без этой строки человек, сверяющий числа с ценами входа и выхода, каждый
    раз обнаруживал бы недостачу и искал ошибку.
    """
    reason = EXIT_RU.get(exit_reason, exit_reason)
    return (
        "🔵 <b>Закрыта позиция (виртуально)</b>\n"
        f"{_esc(symbol)} · {_esc(reason)}\n"
        f"Вход {_price(entry_price)} → выход {_price(exit_price)}\n"
        f"Итог {float(net_pnl_pct):+.2f}% (${float(net_pnl_usd):+.3f}) "
        f"с учётом издержек {float(cost_pct):.2f}%\n"
        f"В позиции {held_ru(held_sec)}"
    )


def data_gap_text(
    *,
    symbol: str,
    entry_price: float,
    exit_price: float,
    last_bar_ts: datetime,
    gap_sec: float,
    net_pnl_pct: float,
    net_pnl_usd: float,
    bars_held: int,
) -> str:
    """Сообщение о закрытии позиции ПО ПРОБЕЛУ В ДАННЫХ (§6.6 ТЗ 9.1.1).

    ОТДЕЛЬНЫЙ ТЕКСТ, А НЕ ПЕРЕИСПОЛЬЗОВАННЫЙ ``closed_text``, и это главное в
    этой функции. Обычное сообщение о закрытии утверждало бы РЕЗУЛЬТАТ: «итог
    −0.22%, цель не достигнута». Здесь результата нет — есть последняя
    известная цена и признание того, что исход измерить не удалось. Человек,
    читающий поток, обязан увидеть разницу с первой строки, а не вычислять её
    из мелкого примечания.

    Числа названы своими именами: сколько времени не было данных, от какого
    момента взята цена и что итог в статистику не идёт.
    """
    # ДВА РАЗНЫХ СЛУЧАЯ, И ПУТАТЬ ИХ НЕЛЬЗЯ. Если хоть один бар видели, цена
    # выхода — настоящая цена из прошлого, и назвать её момент обязательно.
    # Если не видели ни одного, никакой «последней известной цены» не было
    # вовсе: выход посчитан по цене ВХОДА, и написать «по последней известной
    # цене от такого-то времени» значило бы сослаться на наблюдение, которого
    # не существует.
    if int(bars_held) > 0:
        price_line = (
            f"Вход {_price(entry_price)} → выход {_price(exit_price)} "
            f"по последней известной цене от {_msk(last_bar_ts)}"
        )
        seen_line = f"Успели увидеть {bars_ru(bars_held)} движения"
    else:
        price_line = (
            f"Вход {_price(entry_price)} → выход {_price(exit_price)}: "
            "ни одной свечи после входа не пришло, выход посчитан по цене входа"
        )
        seen_line = "Движения не наблюдали вовсе — оценить сделку нечем"
    return (
        "🟠 <b>Позиция закрыта по пробелу в данных (виртуально)</b>\n"
        f"{_esc(symbol)} · данных по инструменту не было {held_ru(gap_sec)}\n"
        f"{price_line}\n"
        f"Формальный итог {float(net_pnl_pct):+.2f}% "
        f"(${float(net_pnl_usd):+.3f}) — В СТАТИСТИКУ НЕ ИДЁТ: цена выхода не "
        "наблюдалась, а восстановлена\n"
        f"{seen_line}"
    )
