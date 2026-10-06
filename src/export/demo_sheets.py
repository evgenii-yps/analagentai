"""Два демо-листа Google Таблицы (Этап 9.5, редакция 2, §6 ТЗ): сделки и баланс по дням.

Пишет служба ``export`` при ``DEMO_SHEETS_ENABLED=true``, штатным режимом ``replace``:
каждый прогон ПЕРЕСТРАИВАЕТ ОБА ЛИСТА ЦЕЛИКОМ. Приёмник Apps Script не меняется.

РАСКЛАДКА: строка 1 — предупреждение, строка 2 — заголовок, дальше данные. Поэтому
предупреждение и заголовок идут в самом списке строк (``rows``), а параметр ``header``
запроса не используется: приёмник кладёт ``header`` первой строкой листа, а ТЗ требует,
чтобы первой стояла оговорка. Числа — числами (не текстом); даты и время — по
``NOTIFY_TIMEZONE``; новые строки сверху.

Лист «торговля тест апи окх чтение» этим модулем не затрагивается никак.

ЦЕНЫ ТЕКУЩИХ ПОЗИЦИЙ — последняя закрытая минутная свеча из ``public.ohlcv`` (реальный рынок):
у службы export нет клиента демо-биржи. Это оценка, и лист говорит об этом в первой строке.

Модуль только читает базу; сборка строк — чистые функции, отправка — в ``export_main``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from src.demo import ledger
from src.positions.messages import exit_ru, hhmm

SHEET_TRADES = "Демо OKX — сделки"
SHEET_BALANCE = "Демо OKX — баланс по дням"

WARNING = [
    "Лист заполняет система, ручные правки затираются. Проскальзывание на демо-счёте — "
    "оценка, не точная копия реального рынка."
]

TRADES_HEADER: list[str] = [
    "№ позиции", "дата входа", "время входа", "токен", "сигнал", "вероятность",
    "цена сигнала", "цена покупки", "проскальзывание входа %", "количество монет",
    "сумма входа $", "комиссия входа $", "дата выхода", "время выхода", "причина выхода",
    "цена продажи", "проскальзывание выхода %", "сумма выхода $", "комиссия выхода $",
    "прибыль $", "прибыль %", "время в сделке", "баланс после закрытия $", "статус",
]

BALANCE_HEADER: list[str] = [
    "дата", "баланс на начало $", "баланс на конец $", "изменение за день $",
    "изменение за день %", "с начала $", "с начала %", "свободно на конец $",
    "в рынке на конец $", "открыто сделок", "закрыто сделок", "прибыльных", "убыточных",
    "пропущено", "комиссии за день $", "наибольшая сумма в рынке за день $",
]

SIGNAL_RU = {"buy": "покупать", "sell": "продавать", "wait": "ждать"}
SKIP_RU = {
    "before_start": "открыта до старта зеркала",
    "stale": "вход устарел",
    "not_buy": "не покупка",
    "no_balance": "мало USDT на демо-счёте",
    "no_capital": "нет свободного капитала",
    "no_market": "нет спот-рынка",
    "closed_before_buy": "закрылась до покупки",
}


# --------------------------------------------------------------------------
# Форматирование ячеек
# --------------------------------------------------------------------------


def num(value: Any, places: int = 6) -> float | str:
    """Число — числом (float, округлённый); пустое значение — пустая ячейка."""
    if value is None or value == "":
        return ""
    return round(float(value), places)


def date_str(ts: datetime | None, tz: ZoneInfo) -> str:
    return "" if ts is None else ts.astimezone(tz).strftime("%d.%m.%Y")


def time_str(ts: datetime | None, tz: ZoneInfo) -> str:
    return "" if ts is None else ts.astimezone(tz).strftime("%H:%M:%S")


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --------------------------------------------------------------------------
# Сделки
# --------------------------------------------------------------------------

_TRADES_SQL = (
    "SELECT p.id AS position_id, p.opened_at, p.signal_price, p.deadline_at, "
    "i.id AS instrument_id, i.symbol, i.base, sg.decision, sg.probability, "
    "b.status AS b_status, b.skip_reason, b.error_code AS b_err, b.avg_price AS b_px, "
    "b.slippage_pct AS b_slip, b.filled_qty AS b_qty, b.fee AS b_fee, b.fee_ccy AS b_fee_ccy, "
    "b.cost_usd AS b_cost, b.fee_usd AS b_fee_usd, b.filled_at AS b_at, "
    "s.status AS s_status, s.error_code AS s_err, s.avg_price AS s_px, "
    "s.slippage_pct AS s_slip, s.cost_usd AS s_cost, s.fee_usd AS s_fee_usd, "
    "s.fee_ccy AS s_fee_ccy, s.filled_at AS s_at, s.exit_reason AS s_exit, "
    "s.equity_after_usd AS s_equity "
    "FROM demo_orders b "
    "JOIN positions p ON p.id = b.position_id "
    "JOIN instruments i ON i.id = p.instrument_id "
    "LEFT JOIN signals sg ON sg.id = p.signal_id "
    "LEFT JOIN demo_orders s ON s.position_id = b.position_id AND s.leg = 'sell' "
    "WHERE b.leg = 'buy' AND p.opened_at >= (SELECT mirror_since FROM demo_state WHERE id = 1) "
    "ORDER BY p.opened_at DESC, p.id DESC;"
)


def trade_status(row: dict[str, Any]) -> str:
    """Статус сделки по-русски: открыта / закрыта / пропущена / ошибка / остаток / потеряна."""
    if row["b_status"] == "skipped":
        return "пропущена: " + SKIP_RU.get(str(row["skip_reason"]), str(row["skip_reason"]))
    if row["b_status"] == "rejected":
        return f"ошибка: {row['b_err'] or 'отказ биржи'}"
    if row["b_status"] == "lost":
        return "потеряна (lost)"
    if row["b_status"] != "filled":
        return "покупка в пути"
    sell = row["s_status"]
    if sell == "filled":
        return "закрыта"
    if sell == "dust":
        return "остаток (dust)"
    if sell == "rejected":
        return f"ошибка: {row['s_err'] or 'отказ биржи'}"
    if sell == "lost":
        return "потеряна (lost)"
    return "открыта (текущая)"


def trade_row(row: dict[str, Any], prices: dict[int, Decimal], now: datetime,
              tz: ZoneInfo) -> list[Any]:
    """Одна строка листа сделок. У пропущенной сделки нет цен и сумм — ячейки пусты."""
    status = trade_status(row)
    filled_buy = row["b_status"] == "filled"
    closed = row["s_status"] == "filled"
    profit: Decimal | None = None
    profit_pct: Decimal | None = None
    held = ""
    if filled_buy and closed:
        pair = {
            "buy_cost": row["b_cost"], "buy_fee_usd": row["b_fee_usd"],
            "buy_fee_ccy": row["b_fee_ccy"], "sell_cost": row["s_cost"],
            "sell_fee_usd": row["s_fee_usd"], "sell_fee_ccy": row["s_fee_ccy"],
        }
        profit = ledger.pair_profit(pair)
        cost = ledger.pair_cost(pair)
        profit_pct = profit / cost * 100 if cost > 0 else None
        held = hhmm((row["s_at"] - row["b_at"]).total_seconds())
    elif filled_buy and status.startswith("открыта"):
        # Открытая сделка: прибыль текущая — по цене рынка, без будущей комиссии продажи.
        price = prices.get(int(row["instrument_id"]))
        if price is not None:
            qty = ledger.holding_qty(row["b_qty"], _dec(row["b_fee"]), row["b_fee_ccy"],
                                     str(row["base"]))
            spent = Decimal(str(row["b_cost"])) + ledger.usdt_fee(row["b_fee_usd"],
                                                                 row["b_fee_ccy"])
            profit = qty * price - spent
            profit_pct = profit / spent * 100 if spent > 0 else None
        held = hhmm((now - row["b_at"]).total_seconds())
    hold_hours = (
        round((row["deadline_at"] - row["opened_at"]).total_seconds() / 3600)
        if row["deadline_at"] else None
    )
    exit_reason = exit_ru(str(row["s_exit"]), hold_hours) if closed and row["s_exit"] else ""
    return [
        int(row["position_id"]),
        date_str(row["opened_at"], tz), time_str(row["opened_at"], tz),
        str(row["base"]),
        SIGNAL_RU.get(str(row["decision"]), str(row["decision"] or "")),
        num(row["probability"], 4),
        num(row["signal_price"], 8),
        num(row["b_px"], 8) if filled_buy else "",
        num(row["b_slip"], 4) if filled_buy else "",
        num(row["b_qty"], 8) if filled_buy else "",
        num(row["b_cost"], 6) if filled_buy else "",
        num(row["b_fee_usd"], 6) if filled_buy else "",
        date_str(row["s_at"], tz) if closed else "",
        time_str(row["s_at"], tz) if closed else "",
        exit_reason,
        num(row["s_px"], 8) if closed else "",
        num(row["s_slip"], 4) if closed else "",
        num(row["s_cost"], 6) if closed else "",
        num(row["s_fee_usd"], 6) if closed else "",
        num(profit, 6), num(profit_pct, 4), held,
        num(row["s_equity"], 6) if closed else "",
        status,
    ]


async def build_trades_sheet(conn: Any, now: datetime, tz: ZoneInfo) -> list[list[Any]]:
    """Строки листа сделок: предупреждение, заголовок, затем сделки (новые сверху)."""
    rows = [dict(r) for r in await conn.fetch(_TRADES_SQL)]
    open_ids = sorted({
        int(r["instrument_id"]) for r in rows
        if r["b_status"] == "filled" and r["s_status"] not in ("filled", "dust", "rejected", "lost")
    })
    prices = await ledger.ohlcv_prices(conn, open_ids, asof=now)
    return [list(WARNING), list(TRADES_HEADER), *(trade_row(r, prices, now, tz) for r in rows)]


# --------------------------------------------------------------------------
# Баланс по дням
# --------------------------------------------------------------------------


def balance_day_row(snap: dict[str, Any], start_capital: Decimal) -> list[Any]:
    open_, close = Decimal(str(snap["equity_open"])), Decimal(str(snap["equity_close"]))
    day_diff, day_pct = ledger.change(close, open_)
    all_diff, all_pct = ledger.change(close, start_capital)
    return [
        snap["day"].strftime("%d.%m.%Y"), num(open_), num(close), num(day_diff),
        num(day_pct, 4), num(all_diff), num(all_pct, 4), num(snap["cash_close"]),
        num(snap["in_market_close"]), int(snap["opened_count"]), int(snap["closed_count"]),
        int(snap["wins"]), int(snap["losses"]), int(snap["skipped_count"]),
        num(snap["fees_usd"]), num(snap["max_in_market_usd"]),
    ]


def balance_now_row(state: ledger.LedgerState | None, now: datetime, tz: ZoneInfo) -> list[Any]:
    """Строка 3 — итог «сейчас» в тех же колонках, что и сутки."""
    label = "сейчас " + now.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    row: list[Any] = [label]
    if state is not None:
        diff, pct = ledger.change(state.equity, state.start_capital)
        row = [label, "", num(state.equity), "", "", num(diff), num(pct, 4), num(state.cash),
               num(state.in_market)]
    return row + [""] * (len(BALANCE_HEADER) - len(row))


async def build_balance_sheet(conn: Any, now: datetime, tz: ZoneInfo) -> list[list[Any]]:
    """Строки листа баланса: предупреждение, заголовок, «сейчас», затем сутки (новые сверху)."""
    start = await ledger.read_start_capital(conn)
    state = (
        await ledger.compute_state(conn, start) if start is not None else None
    )
    days = [dict(r) for r in await conn.fetch(
        "SELECT * FROM demo_balance_daily ORDER BY day DESC;")]
    return [
        list(WARNING), list(BALANCE_HEADER), balance_now_row(state, now, tz),
        *(balance_day_row(d, start) for d in days if start is not None),
    ]
