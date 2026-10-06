"""Демо-сделки в ЛИСТ ВЛАДЕЛЬЦА «торговля демо апи окх» (Этап 9.5.2, редакция 1).

Владелец сам сделал в книге лист с формулами: выручку, прибыль, процент, баланс и лист «по дням»
считают ОНИ. Система вписывает только ИСХОДНЫЕ данные сделок и больше ничего:

* ячейка ``A3`` — стартовый капитал (``demo_state.start_capital``);
* со строки 6 — по строке на позицию, у которой есть покупка в ``demo_orders`` (любой статус),
  старые сверху; пишутся колонки A–D, F–K, M, T. Колонки E, L, N–S, U, V — формулы владельца
  или пусто: ни писать, ни очищать их нельзя, и ни один диапазон ниже их не задевает.

Модуль ТОЛЬКО ЧИТАЕТ ``demo_orders``, ``demo_state``, ``positions``, ``signals``,
``instruments`` (и ``ohlcv`` через ``ledger.ohlcv_prices``): ни одного INSERT/UPDATE/DELETE.
Построение строк — чистое, без сети; отправка — ``src.export.sheets.write_cells_values``,
оркестрация — ``src.export_main``.

СМЫСЛ КОЛОНОК K И M. Комиссия покупки в МОНЕТЕ уменьшает проданное количество и уже сидит внутри
``sell.cost_usd``. Чтобы колонка «комиссия» показывала всю комиссию, а прибыль не задвоилась,
её стоимость (``fee_qty × sell.avg_price``) возвращается в K и показывается в M::

    K = sell.cost_usd + fee_qty_покупки × sell.avg_price
    M = комиссия покупки в $ + комиссия продажи в USDT
    (K − G) − M = sell.cost_usd − buy.cost_usd − комиссия продажи [− комиссия покупки в USDT]

Это ровно ``ledger.pair_profit`` — то, что показывает ``/demo`` и на чём держится ``cash``.
Формула владельца N = (K − G) − M обязана с ним совпасть.

Каждый прогон ПЕРЕСТРАИВАЕТ область целиком и не оставляет меток в базе: повторный прогон без
новых данных даёт те же значения.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from src.demo import ledger
from src.export.transform import position_hold_hours, position_marker
from src.positions.messages import exit_ru

FIRST_ROW = 6
CAPITAL_CELL = "A3"

# Блоки записи: ключ → (первая колонка, последняя колонка). Порядок значений в строке блока —
# порядок колонок. Колонки E, L, N–S, U, V в блоки НЕ входят.
BLOCK_COLUMNS: dict[str, tuple[str, str]] = {
    "A:D": ("A", "D"),
    "F:K": ("F", "K"),
    "M": ("M", "M"),
    "T": ("T", "T"),
}

# Нулевая точка дат Google Таблиц (и Excel с поправкой на ошибку 1900 года).
_EPOCH = date(1899, 12, 30)
_USDT = "USDT"

SKIP_RU = {
    "no_capital": "нет свободного капитала",
    "no_balance": "мало USDT на демо-счёте",
    "stale": "опоздание входа",
    "before_start": "позиция до запуска демо",
    "not_buy": "не покупка",
    "no_market": "нет спот-рынка",
    "closed_before_buy": "закрылась до покупки",
}

# Пояснения к частым кодам ошибок OKX. Сырой текст ошибки биржи в лист НЕ идёт: он приходит
# извне, и «без ключей, подписей и заголовков запросов» надёжнее всего обеспечивается тем, что
# в заметку попадает только наш собственный текст.
ERROR_RU = {
    "51008": "не хватает средств на демо-счёте",
    "51020": "сумма ордера меньше минимума биржи",
    "51000": "биржа отклонила параметр ордера",
    "51001": "инструмента нет на бирже",
    "51006": "цена вне допустимого диапазона",
    "51010": "режим счёта не позволяет сделку",
    "51121": "количество не кратно шагу лота",
    "50011": "слишком частые запросы к бирже",
    "50013": "биржа временно перегружена",
    "50111": "ключ API недействителен",
    "50113": "подпись запроса отклонена биржей",
    "50114": "недопустимое значение ключа API",
}


class OwnerSheetNotReady(Exception):
    """Демо-учёт ещё не стартовал (нет строки ``demo_state``): писать в лист нечего."""


@dataclass(frozen=True)
class OwnerSheetPayload:
    """Что записать в лист владельца. Деньги в блоках — числа, текст только C, D, T."""

    start_capital: Decimal
    blocks: dict[str, list[list]]   # 'A:D', 'F:K', 'M', 'T' — по строке на позицию
    first_row: int                  # 6
    last_written_row: int           # first_row − 1, если строк данных нет
    # Служебное для предупреждений о конце строк (§3.8): сколько позиций есть всего и
    # сколько не поместилось ниже последней строки с формулами.
    total_positions: int = 0
    dropped: int = 0
    # Последняя строка с формулами владельца, под которую построено (для очистки хвоста).
    last_row: int = 1000
    # Сколько позиций каждого вида (для журнала и проверки).
    statuses: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Числа, даты, время
# --------------------------------------------------------------------------


def _num(value: Any, places: int) -> float | str:
    """Число — числом (float, округлённый); пустое значение — пустая ячейка."""
    if value is None or value == "":
        return ""
    return round(float(value), places)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _local(ts: datetime, tz: str | ZoneInfo) -> datetime:
    """Момент в часовом поясе уведомлений, округлённый до секунды (без пояса).

    Округляется до ближайшей секунды ДО разбора на дату и время: иначе 23:59:59.7 дало бы
    дату этих суток и время «24:00:00».
    """
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    local = ts.astimezone(zone).replace(tzinfo=None)
    if local.microsecond >= 500_000:
        local += timedelta(seconds=1)
    return local.replace(microsecond=0)


def split_serial(dt_local: datetime) -> tuple[float, float]:
    """``(дата, время)``: целое число дней от 30.12.1899 и доля суток. Для колонок A/B, H/I."""
    day = float((dt_local.date() - _EPOCH).days)
    seconds = dt_local.hour * 3600 + dt_local.minute * 60 + dt_local.second
    return day, seconds / 86400


def to_sheet_serial(dt_local: datetime) -> float:
    """Дата-время числом Google Таблиц: дни от 30.12.1899, время — дробная часть.

    ``dt_local`` — местное время (часовой пояс уведомлений); пояс самого значения
    отбрасывается без пересчёта, пересчёт делает вызывающий (:func:`_local`).
    """
    day, frac = split_serial(dt_local.replace(tzinfo=None, microsecond=0))
    return day + frac


def _date_time(ts: datetime | None, tz: str | ZoneInfo) -> tuple[float | str, float | str]:
    if ts is None:
        return "", ""
    return split_serial(_local(ts, tz))


# --------------------------------------------------------------------------
# Комиссии
# --------------------------------------------------------------------------


def buy_fee_parts(buy: dict[str, Any], base: str) -> tuple[Decimal, Decimal]:
    """Комиссия покупки: ``(в монете, в USDT)``.

    Комиссия в базовой монете — количество монет (стоимость считается по цене продажи или
    покупки, смотря по состоянию сделки); в USDT — деньги как есть, как их видит
    ``ledger.usdt_fee``. Комиссия в любой другой валюте учёт не списывает (так в ``ledger``),
    и здесь она тоже нулевая — иначе формула листа разошлась бы с ``cash``.
    """
    fee = buy.get("fee")
    ccy = str(buy.get("fee_ccy") or "").upper()
    if not fee:
        return ledger.ZERO, ledger.ZERO
    if ccy == base.upper():
        return abs(Decimal(str(fee))), ledger.ZERO
    if ccy == _USDT:
        amount = buy.get("fee_usd")
        return ledger.ZERO, abs(Decimal(str(amount if amount is not None else fee)))
    return ledger.ZERO, ledger.ZERO


def sell_fee_usdt(sell: dict[str, Any] | None) -> Decimal:
    """Комиссия продажи в USDT (``ledger.usdt_fee``): другая валюта учётом не списывается."""
    if not sell:
        return ledger.ZERO
    return ledger.usdt_fee(sell.get("fee_usd"), sell.get("fee_ccy"))


# --------------------------------------------------------------------------
# Заметка (колонка T)
# --------------------------------------------------------------------------


def _price_text(value: Any) -> str:
    """Цена в заметке: два знака у дорогих инструментов, шесть у копеечных."""
    if value is None:
        return "—"
    number = float(value)
    digits = 2 if abs(number) >= 100 else (4 if abs(number) >= 1 else 6)
    return f"{number:.{digits}f}"


def _pct_text(value: Any, places: int = 3) -> str:
    return "—" if value is None else f"{float(value):+.{places}f}%"


def _sold(sell: dict[str, Any] | None) -> bool:
    return bool(sell) and sell["status"] == "filled"


def _body(pos: dict[str, Any], buy: dict[str, Any], sell: dict[str, Any] | None) -> str:
    """Тело заметки купленной позиции (§3.5): метка, сигнал, цель, вход, [выход], правило."""
    probability = pos.get("probability")
    prob = "—" if probability is None else f"{float(probability):.2f}"
    target_pct = pos.get("target_pct")
    target = (
        f"цель {_price_text(pos.get('target_price'))}"
        + ("" if target_pct is None else f" (+{float(target_pct):.2f}%)")
    )
    lag = buy.get("lag_sec")
    parts = [
        position_marker(pos["id"]) + f" сигнал #{int(pos['signal_id'])}",
        f"вероятность {prob}",
        target,
        f"цена сигнала {_price_text(buy.get('virtual_price'))}, "
        f"проскальзывание входа {_pct_text(buy.get('slippage_pct'))}",
        "задержка входа " + ("—" if lag is None else f"{int(lag)} с"),
    ]
    if _sold(sell):
        assert sell is not None
        reason = sell.get("exit_reason") or pos.get("p_exit_reason")
        why = exit_ru(str(reason), position_hold_hours(pos)) if reason else "причина не указана"
        parts.append(
            f"выход: {why}, цена системы {_price_text(sell.get('virtual_price'))}, "
            f"проскальзывание выхода {_pct_text(sell.get('slippage_pct'))}"
        )
    parts.append(f"правило v{int(pos['logic_version'])}")
    return " · ".join(parts)


def _error_explanation(code: Any) -> str:
    return ERROR_RU.get(str(code), "отказ биржи") if code else "отказ биржи"


def note_text(
    pos: dict[str, Any],
    buy: dict[str, Any],
    sell: dict[str, Any] | None,
    current_price: Decimal | None,
) -> str:
    """Заметка колонки T по-русски, через « · » (§3.4–3.5). Без ключей, подписей, заголовков.

    ``current_price`` — цена для открытой позиции (как в ``ledger``: последняя закрытая
    минутная свеча), ``None`` — цены нет.
    """
    marker = position_marker(pos["id"])
    status = buy["status"]
    if status == "skipped":
        reason = str(buy.get("skip_reason") or "")
        return f"ПРОПУЩЕНА: {SKIP_RU.get(reason, reason or 'причина не указана')} · {marker}"
    if status == "rejected":
        code = buy.get("error_code")
        label = str(code) if code else "без кода"
        return f"ОШИБКА {label}: {_error_explanation(code)} · {marker}"
    if status == "lost":
        return f"ПОТЕРЯНА: ордер не найден на бирже · {marker}"
    if status != "filled":
        return "ОРДЕР ОТПРАВЛЕН"

    body = _body(pos, buy, sell)
    if _sold(sell):
        return body
    sell_status = sell["status"] if sell else None
    if sell_status == "dust":
        return f"ОСТАТОК: количество меньше минимума биржи, не продано · {body}"
    if sell_status == "rejected":
        code = sell.get("error_code") if sell else None
        return f"ПРОДАЖА: ОШИБКА {code or 'без кода'} · {body}"
    if sell_status == "lost":
        return f"ПРОДАЖА ПОТЕРЯНА · {body}"
    if current_price is None:
        return f"ОТКРЫТА · текущая прибыль: нет цены · {body}"
    pct = current_profit_pct(pos, buy, current_price)
    if pct is None:
        return f"ОТКРЫТА · текущая прибыль: нет цены · {body}"
    return f"ОТКРЫТА · текущая прибыль {pct:+.2f}% по цене демо-рынка · {body}"


def current_profit_pct(
    pos: dict[str, Any], buy: dict[str, Any], price: Decimal
) -> Decimal | None:
    """Текущая прибыль открытой позиции, %: как в ``ledger`` (количество на руках × цена)."""
    qty = ledger.holding_qty(
        Decimal(str(buy["filled_qty"])), _dec(buy.get("fee")), buy.get("fee_ccy"),
        str(pos["base"]),
    )
    spent = Decimal(str(buy["cost_usd"])) + ledger.usdt_fee(buy.get("fee_usd"), buy.get("fee_ccy"))
    if spent <= 0:
        return None
    return (qty * price - spent) / spent * 100


# --------------------------------------------------------------------------
# Строка листа
# --------------------------------------------------------------------------

_STATUS_D = {"skipped": "пропущено", "rejected": "ошибка", "lost": "потеряна"}


def owner_row(
    pos: dict[str, Any],
    buy: dict[str, Any],
    sell: dict[str, Any] | None,
    price: Decimal | None,
    tz: str | ZoneInfo,
) -> dict[str, list]:
    """Одна позиция → значения четырёх блоков (по одной строке на блок)."""
    token = str(pos["base"])
    status = buy["status"]
    note = note_text(pos, buy, sell, price)
    if status != "filled":
        # Не куплено: A, B — момент позиции; F–K и M пусты. Ордер в пути — «покупать».
        d_text = _STATUS_D.get(status, "покупать")
        a, b = _date_time(pos["opened_at"], tz)
        return {
            "A:D": [a, b, token, d_text],
            "F:K": [""] * 6,
            "M": [""],
            "T": [note],
        }

    a, b = _date_time(buy.get("filled_at") or pos["opened_at"], tz)
    sold = _sold(sell)
    coin_fee, usdt_fee = buy_fee_parts(buy, token)
    h = i = j = k = ""
    if sold:
        assert sell is not None
        h, i = _date_time(sell["filled_at"], tz)
        sell_px = Decimal(str(sell["avg_price"]))
        j = _num(sell_px, 8)
        k = _num(Decimal(str(sell["cost_usd"])) + coin_fee * sell_px, 8)
        fee_price = sell_px
    else:
        fee_price = Decimal(str(buy["avg_price"]))
    fees = coin_fee * fee_price + usdt_fee + (sell_fee_usdt(sell) if sold else ledger.ZERO)
    return {
        "A:D": [a, b, token, "покупать"],
        "F:K": [_num(buy["avg_price"], 8), _num(buy["cost_usd"], 6), h, i, j, k],
        "M": [_num(fees, 8)],
        "T": [note],
    }


# --------------------------------------------------------------------------
# Чтение и сборка
# --------------------------------------------------------------------------

_POSITIONS_SQL = (
    "SELECT p.id, p.opened_at, p.signal_id, p.logic_version, p.target_price, p.target_pct, "
    "p.deadline_at, p.exit_reason AS p_exit_reason, i.id AS instrument_id, i.base, i.symbol, "
    "sg.probability "
    "FROM positions p "
    "JOIN instruments i ON i.id = p.instrument_id "
    "LEFT JOIN signals sg ON sg.id = p.signal_id "
    "WHERE EXISTS (SELECT 1 FROM demo_orders b WHERE b.position_id = p.id AND b.leg = 'buy' "
    # Этап 9.5.3: позиции, открытые ДО запуска демо (buy skipped / before_start), — не сделки
    # демо-счёта, в лист владельца они не идут. Остальные пропуски остаются.
    "AND NOT (b.status = 'skipped' AND b.skip_reason = 'before_start')) "
    "ORDER BY p.opened_at, p.id;"
)

_ORDERS_SQL = (
    "SELECT position_id, leg, status, skip_reason, error_code, avg_price, cost_usd, filled_qty, "
    "fee, fee_ccy, fee_usd, filled_at, virtual_price, slippage_pct, lag_sec, exit_reason "
    "FROM demo_orders;"
)


async def build_owner_rows(
    conn: Any, now: datetime, tz: str, *, last_row: int | None = None
) -> OwnerSheetPayload:
    """Собирает данные для листа владельца: чистое построение, только чтение базы, без сети.

    ``last_row`` — последняя строка с формулами владельца (по умолчанию
    ``DEMO_OWNER_SHEET_LAST_ROW``). Позиций больше, чем ``last_row − 5``, — пишутся первые
    (``dropped`` > 0). Бросает :class:`OwnerSheetNotReady`, если демо-учёт не стартовал.
    """
    if last_row is None:
        from src.core.config import settings

        last_row = int(settings.DEMO_OWNER_SHEET_LAST_ROW)
    start = await ledger.read_start_capital(conn)
    if start is None:
        raise OwnerSheetNotReady("в demo_state нет строки: демо-учёт ещё не стартовал")

    positions = [dict(r) for r in await conn.fetch(_POSITIONS_SQL)]
    legs: dict[tuple[int, str], dict[str, Any]] = {
        (int(r["position_id"]), str(r["leg"])): dict(r) for r in await conn.fetch(_ORDERS_SQL)
    }
    total = len(positions)
    capacity = max(0, last_row - (FIRST_ROW - 1))
    kept = positions[:capacity]

    # Цены нужны только открытым: покупка исполнена, продажа ещё нет.
    open_ids = sorted({
        int(p["instrument_id"]) for p in kept
        if legs[(int(p["id"]), "buy")]["status"] == "filled"
        and legs.get((int(p["id"]), "sell"), {}).get("status") not in ("filled", "dust",
                                                                         "rejected", "lost")
    })
    prices = await ledger.ohlcv_prices(conn, open_ids, asof=now)

    blocks: dict[str, list[list]] = {key: [] for key in BLOCK_COLUMNS}
    statuses: dict[str, int] = {}
    for pos in kept:
        buy = legs[(int(pos["id"]), "buy")]
        sell = legs.get((int(pos["id"]), "sell"))
        statuses[buy["status"]] = statuses.get(buy["status"], 0) + 1
        row = owner_row(pos, buy, sell, prices.get(int(pos["instrument_id"])), tz)
        for key in BLOCK_COLUMNS:
            blocks[key].append(row[key])
    return OwnerSheetPayload(
        start_capital=start,
        blocks=blocks,
        first_row=FIRST_ROW,
        last_written_row=FIRST_ROW - 1 + len(kept),
        total_positions=total,
        dropped=total - len(kept),
        last_row=last_row,
        statuses=statuses,
    )


def to_requests(
    payload: OwnerSheetPayload, sheet_rows_limit: int | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Диапазоны запроса cells_values: ``(writes, clears)``.

    * ``writes`` — A3 и четыре блока строк (пустой блок не пишется: нечего);
    * ``clears`` — те же четыре блока ниже последней записанной строки до ``last_row``
      включительно (``clearContent``: значения, но не формат и не формулы).
    """
    limit = payload.last_row if sheet_rows_limit is None else sheet_rows_limit
    first, last = payload.first_row, payload.last_written_row
    writes: list[dict[str, Any]] = [
        {"range": CAPITAL_CELL, "values": [[float(payload.start_capital)]]}
    ]
    clears: list[dict[str, Any]] = []
    for key, (c1, c2) in BLOCK_COLUMNS.items():
        rows = payload.blocks[key]
        if rows:
            writes.append({"range": f"{c1}{first}:{c2}{last}", "values": rows})
        if last + 1 <= limit:
            clears.append({"range": f"{c1}{last + 1}:{c2}{limit}"})
    return writes, clears
