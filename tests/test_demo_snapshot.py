"""Этап 9.5, редакция 2, §5: снимок конца суток по NOTIFY_TIMEZONE и граница суток."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.demo import ledger, runner
from tests.demo_support import (
    Clock,
    FakeExchange,
    add_candle,
    add_position,
    build_context,
    build_database,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    reset_data,
    server_up,
)

D = Decimal
MSK = ZoneInfo("Europe/Moscow")


def utc(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# --- граница суток (без базы) ------------------------------------------------------


def test_the_day_boundary_is_local_midnight_not_utc() -> None:
    start, end = ledger.day_bounds(date(2026, 10, 5), MSK)
    assert start == utc(4, 21) and end == utc(5, 21)              # 00:00 МСК = 21:00 UTC


def test_a_trade_at_2230_utc_belongs_to_the_next_moscow_day() -> None:
    ts = utc(4, 22, 30)
    assert ts.date() == date(2026, 10, 4)                            # по UTC — 4 октября
    assert ledger.local_day(ts, MSK) == date(2026, 10, 5)            # по Москве — уже 5-е
    start5, end5 = ledger.day_bounds(date(2026, 10, 5), MSK)
    start4, end4 = ledger.day_bounds(date(2026, 10, 4), MSK)
    assert start5 <= ts < end5 and not start4 <= ts < end4


def test_days_follow_local_dates_across_a_dst_change() -> None:
    berlin = ZoneInfo("Europe/Berlin")      # 25.10.2026 переход на зимнее время: сутки в 25 часов
    start, end = ledger.day_bounds(date(2026, 10, 25), berlin)
    assert end - start == timedelta(hours=25)
    start, end = ledger.day_bounds(date(2026, 3, 29), berlin)
    assert end - start == timedelta(hours=23)


# --- с базой ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demosnap")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


async def build_history(pool) -> dict[str, int]:
    """Четыре сделки на трёх московских сутках (см. ожидаемые числа в тестах)."""
    ids = {}
    # P2 (DOGE): куплена и продана 03.10 по Москве, в минус
    ids["p2"] = await add_position(
        pool, symbol="DOGE/USDT", opened_at=utc(3, 10), entry_price=0.1, status="closed",
        closed_at=utc(3, 11))
    await insert_buy_filled(pool, ids["p2"], symbol="DOGE/USDT", filled_qty="20", fee="0.002",
                            fee_ccy="USDT", avg_price="0.1", cost_usd="2.0", filled_at=utc(3, 10))
    await insert_sell_filled(pool, ids["p2"], symbol="DOGE/USDT", cost_usd="1.98", fee="0.002",
                             filled_qty="20", avg_price="0.099", filled_at=utc(3, 11))
    # P1 (BTC): куплена 22:30 UTC 4 октября = 05.10 01:30 МСК, продана 05.10 13:00 МСК
    ids["p1"] = await add_position(
        pool, opened_at=utc(4, 22, 30), status="closed", closed_at=utc(5, 10))
    await insert_buy_filled(pool, ids["p1"], filled_qty="0.00003333", fee="0.00000003",
                            fee_ccy="BTC", avg_price="60000", cost_usd="2.0",
                            filled_at=utc(4, 22, 30))
    await insert_sell_filled(pool, ids["p1"], cost_usd="2.04", fee="0.002", filled_at=utc(5, 10))
    # P3 (BTC): куплена 06.10 и остаётся открытой
    ids["p3"] = await add_position(pool, opened_at=utc(6, 8))
    await insert_buy_filled(pool, ids["p3"], filled_qty="0.00003333", fee="0.00000003",
                            fee_ccy="BTC", avg_price="60000", cost_usd="2.0",
                            filled_at=utc(6, 8))
    # пропуск 06.10 (created_at задан явно)
    ids["p4"] = await add_position(pool, opened_at=utc(6, 9))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts, created_at) VALUES ($1, 1, 'buy', 'skipped', "
        "'stale', $2, 'BTC/USDT', 'h', $3, $3);", ids["p4"], f"at95b{ids['p4']}", utc(6, 9))
    await add_candle(pool, "BTC/USDT", 60000.0, utc(6, 20, 50))
    return ids


def ctx_at(pool, now: datetime, **kw):
    clock = Clock(now)
    return build_context(pool, FakeExchange(), clock, mirror_since=utc(3, 9), start_capital=1000,
                         **kw), clock


async def rows(pool) -> dict[date, dict]:
    got = await pool.fetch("SELECT * FROM demo_balance_daily ORDER BY day;")
    return {r["day"]: dict(r) for r in got}


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_snapshot_numbers_per_moscow_day(pool) -> None:
    await build_history(pool)
    ctx, _ = ctx_at(pool, utc(6, 21, 10))                 # 00:10 МСК 07.10

    written = await ledger.ensure_snapshots(ctx, ctx.now())

    assert written == [date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)]
    got = await rows(pool)
    d3, d4, d5, d6 = (got[date(2026, 10, d)] for d in (3, 4, 5, 6))
    # 03.10: DOGE куплена и продана с убытком 0.026 (комиссия покупки в USDT учтена)
    assert (d3["opened_count"], d3["closed_count"], d3["wins"], d3["losses"]) == (1, 1, 0, 1)
    assert d3["equity_open"] == D("1000") and d3["cash_close"] == D("999.976")
    assert d3["in_market_close"] == 0 and d3["equity_close"] == D("999.976")
    assert d3["fees_usd"] == D("0.004")                   # 0.002 + 0.002
    # 04.10 по Москве: ничего не происходило; покупка 22:30 UTC сюда НЕ попала
    assert (d4["opened_count"], d4["closed_count"]) == (0, 0)
    assert d4["equity_open"] == d3["equity_close"] == d4["equity_close"]
    # 05.10 по Москве: и покупка (01:30 МСК), и продажа (13:00 МСК), в плюс 0.038
    assert (d5["opened_count"], d5["closed_count"], d5["wins"], d5["losses"]) == (1, 1, 1, 0)
    assert d5["equity_open"] == d4["equity_close"] and d5["cash_close"] == D("1000.014")
    assert d5["equity_close"] == D("1000.014") and d5["in_market_close"] == 0
    # 06.10: открыта нога P3 (оценена по свече на конец суток), один пропуск
    assert (d6["opened_count"], d6["closed_count"], d6["skipped_count"]) == (1, 0, 1)
    assert d6["cash_close"] == D("998.014")
    assert d6["in_market_close"] == D("0.0000333") * D("60000") == D("1.998")
    assert d6["equity_close"] == D("1000.012") and d6["equity_open"] == d5["equity_close"]
    assert d6["max_in_market_usd"] == D("1.998")          # Redis пуст — берётся значение на конец


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_missed_days_are_written_late_and_the_last_day_on_time(pool) -> None:
    await build_history(pool)
    ctx, _ = ctx_at(pool, utc(6, 21, 10))
    await ledger.ensure_snapshots(ctx, ctx.now())
    got = await rows(pool)
    assert [got[date(2026, 10, d)]["late"] for d in (3, 4, 5, 6)] == [True, True, True, False]


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_exactly_one_snapshot_per_day(pool) -> None:
    await build_history(pool)
    ctx, clock = ctx_at(pool, utc(6, 21, 10))
    await ledger.ensure_snapshots(ctx, ctx.now())
    for _ in range(3):
        clock.advance(15)
        assert await ledger.ensure_snapshots(ctx, ctx.now()) == []     # повтор не пишет
    assert await pool.fetchval("SELECT count(*) FROM demo_balance_daily;") == 4
    # внутри тех же московских суток (23:00 UTC 06.10 = 02:00 МСК 07.10) нового снимка нет
    clock.advance(3600)
    assert await ledger.ensure_snapshots(ctx, ctx.now()) == []
    # а на следующую московскую полночь (21:00 UTC 07.10) появляется ровно один
    clock.value = utc(7, 21, 5)
    assert await ledger.ensure_snapshots(ctx, ctx.now()) == [date(2026, 10, 7)]
    assert await pool.fetchval("SELECT count(*) FROM demo_balance_daily;") == 5
    assert (await rows(pool))[date(2026, 10, 7)]["late"] is False


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_no_snapshot_before_the_first_midnight(pool) -> None:
    ctx, _ = ctx_at(pool, utc(3, 18))                       # 21:00 МСК 03.10 — сутки не кончились
    assert await ledger.ensure_snapshots(ctx, ctx.now()) == []
    assert await pool.fetchval("SELECT count(*) FROM demo_balance_daily;") == 0
    # сразу после московской полуночи — первый снимок, equity_open = стартовый капитал
    ctx.now = lambda: utc(3, 21, 1)
    assert await ledger.ensure_snapshots(ctx, ctx.now()) == [date(2026, 10, 3)]
    first = (await rows(pool))[date(2026, 10, 3)]
    assert first["equity_open"] == D("1000") and first["equity_close"] == D("1000")
    assert first["late"] is False


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_gap_in_the_middle_is_filled_in_order_from_the_previous_close(pool) -> None:
    await build_history(pool)
    ctx, clock = ctx_at(pool, utc(4, 21, 5))                # снимки за 03 и 04
    await ledger.ensure_snapshots(ctx, ctx.now())
    assert set(await rows(pool)) == {date(2026, 10, 3), date(2026, 10, 4)}
    clock.value = utc(6, 21, 10)                            # сервис «простоял» два дня
    written = await ledger.ensure_snapshots(ctx, ctx.now())
    assert written == [date(2026, 10, 5), date(2026, 10, 6)]
    got = await rows(pool)
    assert got[date(2026, 10, 5)]["late"] is True and got[date(2026, 10, 6)]["late"] is False
    for day in (4, 5, 6):                                   # цепочка: open = close предыдущих
        assert got[date(2026, 10, day)]["equity_open"] == got[date(2026, 10, day - 1)][
            "equity_close"]


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_observed_daily_maximum_from_redis_is_used(pool) -> None:
    await build_history(pool)
    ctx, _ = ctx_at(pool, utc(6, 21, 10))
    await ctx.redis.set(ledger.max_in_market_key(date(2026, 10, 6)), "7.5")
    await ledger.ensure_snapshots(ctx, ctx.now())
    assert (await rows(pool))[date(2026, 10, 6)]["max_in_market_usd"] == D("7.5")


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_service_loop_takes_the_snapshot(pool) -> None:
    await build_history(pool)
    ctx, _ = ctx_at(pool, utc(6, 21, 10))
    await runner.run_once(ctx)
    assert await pool.fetchval("SELECT count(*) FROM demo_balance_daily;") == 4


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_wins_and_losses_are_strict_and_a_break_even_pair_is_neither(pool) -> None:
    """Выигрыш — прибыль > 0, проигрыш — < 0; сделка ровно в ноль — ни то ни другое."""
    day = 8
    for sell_cost in ("2.04", "1.90", "2.00"):             # выигрыш, проигрыш, ровно ноль
        pid = await add_position(pool, opened_at=utc(day, 5), status="closed",
                                 closed_at=utc(day, 6), net_pnl_usd=0.0)
        await insert_buy_filled(pool, pid, filled_qty="0.00003333", fee="0.00000003",
                                avg_price="60000", cost_usd="2.0", filled_at=utc(day, 5))
        await insert_sell_filled(pool, pid, cost_usd=sell_cost, fee="0", avg_price="60000",
                                 filled_at=utc(day, 6))
    ctx, _ = ctx_at(pool, utc(day, 21, 10))
    await ledger.ensure_snapshots(ctx, ctx.now())
    row = (await rows(pool))[date(2026, 10, day)]
    assert (row["closed_count"], row["wins"], row["losses"]) == (3, 1, 1)


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_taking_the_same_day_twice_writes_one_row_and_reports_false(pool) -> None:
    await build_history(pool)
    ctx, _ = ctx_at(pool, utc(6, 21, 10))
    assert await ledger.snapshot_day(ctx, date(2026, 10, 3), ctx.now()) is True
    assert await ledger.snapshot_day(ctx, date(2026, 10, 3), ctx.now()) is False
    assert await pool.fetchval("SELECT count(*) FROM demo_balance_daily;") == 1
