"""Учёт капитала демо-счёта и снимок конца суток (Этап 9.5, редакция 2, §3 и §5 ТЗ).

ПОЧЕМУ СВОЙ УЧЁТ, А НЕ БАЛАНС СЧЁТА. Демо-счёт OKX выдаётся с большим набором чужих
виртуальных монет: его баланс описывает не нашу систему. Поэтому баланс системы
считается от СТАРТОВОГО КАПИТАЛА по строкам ``demo_orders``:

    cash      = start_capital
                − Σ cost_usd исполненных покупок − Σ fee_usd покупок, взятых в USDT
                + Σ cost_usd исполненных продаж  − Σ fee_usd продаж в USDT
    in_market = Σ по купленным и ещё не проданным ногам (количество × цена рынка)
    equity    = cash + in_market

Комиссия покупки в МОНЕТЕ в ``cash`` не входит: она уже съела часть купленного
количества, и это видно в ``in_market`` (количество меньше) и в проданном.

Количество для оценки — ``filled_qty`` минус комиссия в базовой монете БЕЗ
округления до шага лота: оценка, а не ордер. Цена — тикер демо-рынка, одним
вызовом на итерацию; нет цены — последняя закрытая минутная свеча из
``public.ohlcv`` (с предупреждением в журнале); нет и её — цена покупки.

Модуль только ЧИТАЕТ ``demo_orders`` / ``demo_state`` / ``ohlcv`` и пишет в одну
таблицу — ``demo_balance_daily`` (снимок суток). Функции ``compute_state`` и
``fetch_pairs`` принимают пул или соединение asyncpg: ими пользуются и сервис,
и бот (роль только для чтения), и выгрузка в Таблицу.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

if TYPE_CHECKING:
    from src.demo.runner import Context

_log = structlog.get_logger().bind(component="demo")

ZERO = Decimal(0)
MAX_IN_MARKET_KEY_PREFIX = "demo:max_in_market:"
MAX_IN_MARKET_TTL = 3 * 86400
# Снимок, снятый позже этого срока после полуночи, помечается late = true.
SNAPSHOT_ON_TIME_SEC = 3600

TickersFn = Callable[[list[str]], Awaitable[dict[str, Decimal]]]


@dataclass(frozen=True)
class LedgerState:
    """Состояние учёта: капитал, свободные деньги, деньги в рынке, баланс, число ног."""

    start_capital: Decimal
    cash: Decimal
    in_market: Decimal
    equity: Decimal
    open_count: int


# --------------------------------------------------------------------------
# Чистые формулы
# --------------------------------------------------------------------------


def usdt_fee(fee_usd: Decimal | None, fee_ccy: str | None) -> Decimal:
    """Комиссия, которая списывается ДЕНЬГАМИ: только в USDT. В монете — не учитывается."""
    if not fee_usd or (fee_ccy or "").upper() != "USDT":
        return ZERO
    return abs(Decimal(str(fee_usd)))


def cash_from_sums(
    start_capital: Decimal,
    buy_cost: Decimal,
    buy_fee_usdt: Decimal,
    sell_cost: Decimal,
    sell_fee_usdt: Decimal,
) -> Decimal:
    """Свободные деньги: капитал − покупки − их комиссия в USDT + продажи − их комиссия."""
    return start_capital - buy_cost - buy_fee_usdt + sell_cost - sell_fee_usdt


def holding_qty(
    filled_qty: Decimal, fee: Decimal | None, fee_ccy: str | None, base: str
) -> Decimal:
    """Сколько монеты на руках после покупки: исполненное минус комиссия в этой монете."""
    qty = Decimal(str(filled_qty))
    if fee and fee_ccy and fee_ccy.upper() == base.upper():
        qty -= abs(Decimal(str(fee)))
    return max(qty, ZERO)


def pair_profit(pair: dict[str, Any]) -> Decimal:
    """Прибыль пары «покупка + продажа» ДЕНЬГАМИ: что вернулось минус что ушло."""
    spent = Decimal(str(pair["buy_cost"])) + usdt_fee(pair["buy_fee_usd"], pair["buy_fee_ccy"])
    got = Decimal(str(pair["sell_cost"])) - usdt_fee(pair["sell_fee_usd"], pair["sell_fee_ccy"])
    return got - spent


def pair_cost(pair: dict[str, Any]) -> Decimal:
    """Сколько денег ушло на покупку пары (с комиссией в USDT) — основа процента."""
    return Decimal(str(pair["buy_cost"])) + usdt_fee(pair["buy_fee_usd"], pair["buy_fee_ccy"])


def change(now_value: Decimal, base_value: Decimal) -> tuple[Decimal, Decimal | None]:
    """Изменение в долларах и в процентах от базы (``None`` — база нулевая)."""
    diff = now_value - base_value
    return diff, (diff / base_value * 100 if base_value > 0 else None)


# --------------------------------------------------------------------------
# Сутки по часовому поясу уведомлений
# --------------------------------------------------------------------------


def local_day(ts: datetime, tz: ZoneInfo) -> date:
    """Календарная дата момента в часовом поясе уведомлений."""
    return ts.astimezone(tz).date()


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Начало и конец суток (UTC): полуночи ``day`` и ``day + 1`` по местному времени.

    Сутки задаются ЛОКАЛЬНЫМИ датами, а не прибавлением 24 часов: на переходе
    летнего времени сутки бывают 23 или 25 часов.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


# --------------------------------------------------------------------------
# Чтение из базы
# --------------------------------------------------------------------------


async def read_start_capital(db: Any) -> Decimal | None:
    """Стартовый капитал из ``demo_state``; ``None`` — сервис ещё не стартовал."""
    value = await db.fetchval("SELECT start_capital FROM demo_state WHERE id = 1;")
    return Decimal(str(value)) if value is not None else None


async def fetch_cash(
    db: Any, start_capital: Decimal, asof: datetime | None = None
) -> Decimal:
    """Свободные деньги по исполненным строкам (до ``asof`` включительно, если задано)."""
    rows = await db.fetch(
        "SELECT leg, COALESCE(sum(cost_usd), 0) AS cost, "
        "COALESCE(sum(fee_usd) FILTER (WHERE upper(fee_ccy) = 'USDT'), 0) AS fee_usdt "
        "FROM demo_orders WHERE status = 'filled' "
        "AND ($1::timestamptz IS NULL OR filled_at <= $1::timestamptz) GROUP BY leg;",
        asof,
    )
    sums = {r["leg"]: r for r in rows}
    buy, sell = sums.get("buy"), sums.get("sell")
    return cash_from_sums(
        Decimal(str(start_capital)),
        Decimal(str(buy["cost"])) if buy else ZERO,
        abs(Decimal(str(buy["fee_usdt"]))) if buy else ZERO,
        Decimal(str(sell["cost"])) if sell else ZERO,
        abs(Decimal(str(sell["fee_usdt"]))) if sell else ZERO,
    )


async def fetch_reserved(db: Any) -> Decimal:
    """Деньги под ордерами, исход которых ещё неизвестен (``pending``/``sent``)."""
    value = await db.fetchval(
        "SELECT COALESCE(sum(requested_cost_usd), 0) FROM demo_orders "
        "WHERE leg = 'buy' AND status IN ('pending', 'sent');"
    )
    return Decimal(str(value))


async def fetch_held_legs(db: Any, asof: datetime | None = None) -> list[dict[str, Any]]:
    """Купленные и ещё не проданные ноги (покупка исполнена, продажа — нет)."""
    rows = await db.fetch(
        "SELECT b.position_id, b.instrument_id, b.symbol, b.filled_qty, b.fee, b.fee_ccy, "
        "b.avg_price, b.cost_usd, b.fee_usd, b.filled_at, i.base "
        "FROM demo_orders b JOIN instruments i ON i.id = b.instrument_id "
        "WHERE b.leg = 'buy' AND b.status = 'filled' "
        "AND ($1::timestamptz IS NULL OR b.filled_at <= $1::timestamptz) "
        "AND NOT EXISTS (SELECT 1 FROM demo_orders s WHERE s.position_id = b.position_id "
        "AND s.leg = 'sell' AND s.status = 'filled' "
        "AND ($1::timestamptz IS NULL OR s.filled_at <= $1::timestamptz)) "
        "ORDER BY b.filled_at, b.position_id;",
        asof,
    )
    return [dict(r) for r in rows]


async def ohlcv_prices(
    db: Any, instrument_ids: list[int], asof: datetime | None = None
) -> dict[int, Decimal]:
    """Последняя ЗАКРЫТАЯ минутная свеча по инструментам (на момент ``asof``)."""
    if not instrument_ids:
        return {}
    rows = await db.fetch(
        "SELECT DISTINCT ON (o.instrument_id) o.instrument_id, o.close FROM ohlcv o "
        "WHERE o.instrument_id = ANY($1::int[]) AND o.timeframe = '1m' "
        "AND o.ts <= COALESCE($2::timestamptz, now()) - interval '1 minute' "
        "ORDER BY o.instrument_id, o.ts DESC;",
        list(instrument_ids), asof,
    )
    return {int(r["instrument_id"]): Decimal(str(r["close"])) for r in rows}


async def value_legs(
    db: Any,
    legs: list[dict[str, Any]],
    *,
    tickers_fn: TickersFn | None = None,
    asof: datetime | None = None,
) -> list[dict[str, Any]]:
    """Добавляет к ногам ``qty``, ``price``, ``price_source`` и ``value``.

    Цена: тикер (если передан) → минутная свеча → цена покупки. Каждый переход на
    запасной путь пишется в журнал предупреждением ``demo_ledger_price_fallback=1``.
    """
    if not legs:
        return []
    tickers: dict[str, Decimal] = {}
    if tickers_fn is not None:
        try:
            tickers = await tickers_fn(sorted({str(leg["symbol"]) for leg in legs}))
        except Exception as exc:  # noqa: BLE001 — запасной путь ниже
            _log.warning("demo_ledger_tickers_failed=1", error=str(exc))
    missing = [leg for leg in legs if str(leg["symbol"]) not in tickers]
    candles = (
        await ohlcv_prices(db, sorted({int(leg["instrument_id"]) for leg in missing}), asof)
        if missing else {}
    )
    out: list[dict[str, Any]] = []
    for leg in legs:
        symbol = str(leg["symbol"])
        if symbol in tickers:
            price, source = tickers[symbol], "ticker"
        elif int(leg["instrument_id"]) in candles:
            price, source = candles[int(leg["instrument_id"])], "ohlcv"
        else:
            price, source = Decimal(str(leg["avg_price"])), "entry"
        if source != "ticker" and tickers_fn is not None:
            _log.warning("demo_ledger_price_fallback=1", symbol=symbol, source=source)
        qty = holding_qty(leg["filled_qty"], leg["fee"], leg["fee_ccy"], str(leg["base"]))
        out.append({**leg, "qty": qty, "price": price, "price_source": source,
                    "value": qty * price})
    return out


async def compute_state(
    db: Any,
    start_capital: Decimal,
    *,
    tickers_fn: TickersFn | None = None,
    asof: datetime | None = None,
) -> LedgerState:
    """Состояние учёта: ``cash``, ``in_market``, ``equity`` и число открытых ног."""
    cash = await fetch_cash(db, start_capital, asof)
    valued = await value_legs(db, await fetch_held_legs(db, asof), tickers_fn=tickers_fn,
                              asof=asof)
    in_market = sum((leg["value"] for leg in valued), ZERO)
    return LedgerState(
        start_capital=Decimal(str(start_capital)), cash=cash, in_market=in_market,
        equity=cash + in_market, open_count=len(valued),
    )


async def tickers_prices(ctx: Context, symbols: list[str]) -> dict[str, Decimal]:
    """Цены демо-рынка одним вызовом ``fetch_tickers`` (публичный запрос)."""
    tickers = await ctx.exchange.fetch_tickers(symbols)
    return {
        str(symbol): Decimal(str(ticker["last"]))
        for symbol, ticker in (tickers or {}).items()
        if ticker and ticker.get("last")
    }


async def ledger_state(ctx: Context, now: datetime) -> LedgerState:
    """Текущее состояние учёта сервиса: цены — тикеры демо-рынка, запасной путь — ohlcv."""
    del now   # состояние «сейчас»: момент задаёт сама база и биржа

    async def tickers(symbols: list[str]) -> dict[str, Decimal]:
        return await tickers_prices(ctx, symbols)

    return await compute_state(ctx.pool, ctx.start_capital, tickers_fn=tickers)


async def fetch_pairs(
    db: Any, since: datetime | None = None, until: datetime | None = None
) -> list[dict[str, Any]]:
    """Закрытые пары (исполнены обе ноги), у которых продажа исполнена в ``[since, until)``."""
    rows = await db.fetch(
        "SELECT b.position_id, i.base, i.symbol, b.cost_usd AS buy_cost, "
        "b.fee_usd AS buy_fee_usd, b.fee_ccy AS buy_fee_ccy, b.avg_price AS buy_px, "
        "b.filled_at AS buy_at, s.cost_usd AS sell_cost, s.fee_usd AS sell_fee_usd, "
        "s.fee_ccy AS sell_fee_ccy, s.avg_price AS sell_px, s.filled_at AS sell_at, "
        "s.exit_reason, s.equity_after_usd, p.net_pnl_usd AS virtual_pnl_usd, "
        "p.notional_usd, p.exit_reason AS virtual_exit_reason "
        "FROM demo_orders b "
        "JOIN demo_orders s ON s.position_id = b.position_id AND s.leg = 'sell' "
        "AND s.status = 'filled' "
        "JOIN instruments i ON i.id = b.instrument_id "
        "JOIN positions p ON p.id = b.position_id "
        "WHERE b.leg = 'buy' AND b.status = 'filled' "
        "AND ($1::timestamptz IS NULL OR s.filled_at >= $1::timestamptz) "
        "AND ($2::timestamptz IS NULL OR s.filled_at < $2::timestamptz) "
        "ORDER BY s.filled_at, b.position_id;",
        since, until,
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Наибольшая сумма в рынке за сутки (Redis)
# --------------------------------------------------------------------------


def max_in_market_key(day: date) -> str:
    return f"{MAX_IN_MARKET_KEY_PREFIX}{day.isoformat()}"


async def note_in_market(ctx: Context, state: LedgerState, now: datetime) -> None:
    """Запоминает наибольшее ``in_market`` за сутки (ключ живёт три суток)."""
    key = max_in_market_key(local_day(now, ctx.tz))
    try:
        raw = await ctx.redis.get(key)
        if raw is None or state.in_market > Decimal(str(raw)):
            await ctx.redis.set(key, str(state.in_market), ex=MAX_IN_MARKET_TTL)
    except Exception as exc:  # noqa: BLE001 — максимум не важнее сделки
        _log.warning("demo_max_in_market_failed=1", error=str(exc))


async def track_max_in_market(ctx: Context, now: datetime) -> LedgerState | None:
    """Считает состояние и обновляет суточный максимум. Без открытых ног — ничего не делает."""
    if not await ctx.pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM demo_orders WHERE leg = 'buy' AND status = 'filled' "
        "AND NOT EXISTS (SELECT 1 FROM demo_orders s WHERE s.position_id = demo_orders.position_id "
        "AND s.leg = 'sell' AND s.status = 'filled'));"
    ):
        return None
    state = await ledger_state(ctx, now)
    await note_in_market(ctx, state, now)
    return state


# --------------------------------------------------------------------------
# Снимок конца суток (§5 ТЗ)
# --------------------------------------------------------------------------


async def snapshot_day(ctx: Context, day: date, now: datetime) -> bool:
    """Снимает строку ``demo_balance_daily`` за сутки ``day``. ``False`` — строка уже есть.

    ``equity_open`` — это ``equity_close`` предыдущей строки (для первых суток —
    стартовый капитал). Баланс на конец считается КАК НА МОМЕНТ КОНЦА СУТОК: деньги —
    по исполненным до него строкам, ноги в рынке — по минутной свече на этот момент.
    Поэтому снимок за пропущенные сутки (``late``) считается тем же способом и даёт
    то же число, что и снимок вовремя.
    """
    start, end = day_bounds(day, ctx.tz)
    prev = await ctx.pool.fetchval(
        "SELECT equity_close FROM demo_balance_daily WHERE day < $1 ORDER BY day DESC LIMIT 1;",
        day,
    )
    equity_open = Decimal(str(prev)) if prev is not None else ctx.start_capital
    state = await compute_state(ctx.pool, ctx.start_capital, asof=end)
    counts = await ctx.pool.fetchrow(
        "SELECT "
        "count(*) FILTER (WHERE leg = 'buy' AND status = 'filled' "
        "AND filled_at >= $1 AND filled_at < $2) AS opened, "
        "count(*) FILTER (WHERE leg = 'sell' AND status = 'filled' "
        "AND filled_at >= $1 AND filled_at < $2) AS closed, "
        "count(*) FILTER (WHERE leg = 'buy' AND status = 'skipped' "
        "AND created_at >= $1 AND created_at < $2) AS skipped, "
        "COALESCE(sum(fee_usd) FILTER (WHERE status = 'filled' "
        "AND filled_at >= $1 AND filled_at < $2), 0) AS fees "
        "FROM demo_orders;",
        start, end,
    )
    pairs = await fetch_pairs(ctx.pool, start, end)
    profits = [pair_profit(pair) for pair in pairs]
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p < 0)
    try:
        raw = await ctx.redis.get(max_in_market_key(day))
        observed = Decimal(str(raw)) if raw is not None else ZERO
    except Exception:  # noqa: BLE001 — максимум не важнее снимка
        observed = ZERO
    max_in_market = max(observed, state.in_market)
    late = (now - end).total_seconds() > SNAPSHOT_ON_TIME_SEC
    inserted = await ctx.pool.fetchval(
        "INSERT INTO demo_balance_daily (day, equity_open, equity_close, cash_close, "
        "in_market_close, opened_count, closed_count, wins, losses, skipped_count, "
        "fees_usd, max_in_market_usd, late) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
        "ON CONFLICT (day) DO NOTHING RETURNING day;",
        day, equity_open, state.equity, state.cash, state.in_market,
        int(counts["opened"]), int(counts["closed"]), wins, losses, int(counts["skipped"]),
        Decimal(str(counts["fees"])), max_in_market, late,
    )
    if inserted is not None:
        _log.info(
            "demo_snapshot=1", day=day.isoformat(), equity_open=str(equity_open),
            equity_close=str(state.equity), late=late,
        )
    return inserted is not None


async def ensure_snapshots(ctx: Context, now: datetime) -> list[date]:
    """Дописывает снимки за все завершённые сутки, которых ещё нет.

    Сутки считаются по часовому поясу уведомлений: первый снимок нужен за сутки
    ``mirror_since`` (если они уже завершились), последний — за вчерашние. Сутки,
    пропущенные из-за простоя, дописываются в порядке дат с ``late = true``.
    """
    assert ctx.mirror_since is not None
    today = local_day(now, ctx.tz)
    last = await ctx.pool.fetchval("SELECT max(day) FROM demo_balance_daily;")
    day = last + timedelta(days=1) if last is not None else local_day(ctx.mirror_since, ctx.tz)
    written: list[date] = []
    while day < today:
        if await snapshot_day(ctx, day, now):
            written.append(day)
        day += timedelta(days=1)
    return written
