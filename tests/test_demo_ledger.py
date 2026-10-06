"""Этап 9.5, редакция 2, §3: учёт капитала (cash / in_market / equity), no_capital, поля ног."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from src.demo import ledger, runner
from tests.demo_support import (
    Clock,
    FakeExchange,
    add_candle,
    add_position,
    build_context,
    build_database,
    capture_demo_logs,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    reset_data,
    server_up,
)

D = Decimal

# --- чистые формулы (без базы) --------------------------------------------------


def test_cash_formula_signs() -> None:
    # старт − покупки − комиссия покупок в USDT + продажи − комиссия продаж в USDT
    assert ledger.cash_from_sums(D("1000"), D("6"), D("0.002"), D("4.02"), D("0.004")) \
        == D("998.014")
    assert ledger.cash_from_sums(D("1000"), D("0"), D("0"), D("0"), D("0")) == D("1000")


def test_only_usdt_fees_reduce_cash() -> None:
    assert ledger.usdt_fee(D("0.002"), "USDT") == D("0.002")
    assert ledger.usdt_fee(D("-0.002"), "usdt") == D("0.002")      # знак комиссии не важен
    assert ledger.usdt_fee(D("0.0018"), "BTC") == D("0")            # в монете — уже в количестве
    assert ledger.usdt_fee(None, None) == D("0")


def test_holding_qty_excludes_the_fee_taken_in_the_same_coin() -> None:
    assert ledger.holding_qty(D("0.00003333"), D("0.00000003"), "BTC", "BTC") == D("0.0000333")
    assert ledger.holding_qty(D("20"), D("0.002"), "USDT", "DOGE") == D("20")
    assert ledger.holding_qty(D("1"), D("5"), "BTC", "BTC") == D("0")           # не уходит в минус


def test_pair_profit_is_money_in_minus_money_out() -> None:
    pair = {"buy_cost": D("2"), "buy_fee_usd": D("0.0018"), "buy_fee_ccy": "BTC",
            "sell_cost": D("2.04"), "sell_fee_usd": D("0.002"), "sell_fee_ccy": "USDT"}
    assert ledger.pair_profit(pair) == D("0.038")
    pair["buy_fee_ccy"] = "USDT"
    assert ledger.pair_profit(pair) == D("0.0362")
    assert ledger.pair_cost(pair) == D("2.0018")


def test_change_in_dollars_and_percent() -> None:
    assert ledger.change(D("1010"), D("1000")) == (D("10"), D("1"))
    assert ledger.change(D("5"), D("0")) == (D("5"), None)


# --- на настоящей PostgreSQL -----------------------------------------------------



@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demoledger")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def scenario(pool, clock) -> dict[str, int]:
    """Три покупки и две продажи: комиссии в монете (BTC) и в USDT (DOGE)."""
    at = clock.now() - timedelta(hours=1)
    ids = {}
    for key, symbol, price in (("p1", "BTC/USDT", 60000.0), ("p2", "DOGE/USDT", 0.1),
                               ("p3", "BTC/USDT", 60000.0)):
        closed = key != "p3"
        ids[key] = await add_position(
            pool, symbol=symbol, opened_at=at, entry_price=price,
            status="closed" if closed else "open",
            closed_at=at + timedelta(minutes=30) if closed else None)
    await insert_buy_filled(pool, ids["p1"], filled_qty="0.00003333", fee="0.00000003",
                            fee_ccy="BTC", avg_price="60000", cost_usd="2.0")
    await insert_buy_filled(pool, ids["p2"], symbol="DOGE/USDT", filled_qty="20", fee="0.002",
                            fee_ccy="USDT", avg_price="0.1", cost_usd="2.0")
    await insert_buy_filled(pool, ids["p3"], filled_qty="0.00003333", fee="0.00000003",
                            fee_ccy="BTC", avg_price="60000", cost_usd="2.0")
    await insert_sell_filled(pool, ids["p1"], cost_usd="2.04", fee="0.002")
    await insert_sell_filled(pool, ids["p2"], symbol="DOGE/USDT", cost_usd="1.98", fee="0.002")
    return ids


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_ledger_three_buys_and_two_sells_with_coin_and_usdt_fees(pool, clock) -> None:
    await scenario(pool, clock)
    ex = FakeExchange(prices={"BTC/USDT": 61000.0, "DOGE/USDT": 0.1, "FOO/USDT": 1.0})
    ctx = build_context(pool, ex, clock, start_capital=1000)

    state = await ledger.ledger_state(ctx, clock.now())

    # cash = 1000 − 6.0 − 0.002 (комиссия покупки DOGE в USDT) + 4.02 − 0.004
    assert state.cash == D("998.014")
    # в рынке одна нога (p3): 0.0000333 BTC × 61000
    assert state.in_market == D("0.0000333") * D("61000") == D("2.0313")
    assert state.equity == D("1000.0453") and state.open_count == 1
    assert state.start_capital == D("1000")
    assert [c for c in ex.calls if c[0] == "fetch_tickers"] == [("fetch_tickers", ("BTC/USDT",))]


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_ledger_price_falls_back_to_the_last_closed_minute_candle(
    pool, clock, monkeypatch
) -> None:
    await scenario(pool, clock)
    await add_candle(pool, "BTC/USDT", 62000.0, clock.now() - timedelta(minutes=5))
    # более свежая, но ещё НЕ закрытая свеча (формируется) в расчёт не входит
    await add_candle(pool, "BTC/USDT", 99999.0, clock.now() - timedelta(seconds=10))
    with capture_demo_logs(monkeypatch) as logs:
        ex = FakeExchange(missing_tickers=("BTC/USDT",))
        ctx = build_context(pool, ex, clock)
        state = await ledger.ledger_state(ctx, clock.now())
        assert state.in_market == D("0.0000333") * D("62000")
        # тикеры не отвечают вовсе — тот же запасной путь
        ex2 = FakeExchange(fail_tickers=True)
        state2 = await ledger.ledger_state(build_context(pool, ex2, clock), clock.now())
        assert state2.in_market == state.in_market
    warnings = [e for e in logs.entries if e["event"] == "demo_ledger_price_fallback=1"]
    assert warnings and warnings[0]["source"] == "ohlcv"
    assert any(e["event"] == "demo_ledger_tickers_failed=1" for e in logs.entries)


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_ledger_without_any_price_values_the_leg_at_the_purchase_price(
    pool, clock
) -> None:
    await scenario(pool, clock)
    ex = FakeExchange(fail_tickers=True)
    state = await ledger.ledger_state(build_context(pool, ex, clock), clock.now())
    assert state.in_market == D("0.0000333") * D("60000")


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_ledger_ignores_unfilled_rows_and_counts_only_unsold_legs(pool, clock) -> None:
    ids = await scenario(pool, clock)
    # строки без исполнения в деньги не входят
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) SELECT $1, 1, 'buy', 'skipped', 'stale', 'x1', "
        "'BTC/USDT', 'h', now() WHERE NOT EXISTS (SELECT 1 FROM demo_orders WHERE cl_ord_id='x1');",
        await add_position(pool, opened_at=clock.now() - timedelta(minutes=3)))
    state = await ledger.compute_state(pool, D("1000"))
    assert state.cash == D("998.014") and state.open_count == 1
    # продали и p3 — ног в рынке не осталось, equity == cash
    await pool.execute("UPDATE positions SET status='closed', closed_at=now(), "
                       "exit_price=61000, exit_reason='target', outcome_certain=true, "
                       "net_pnl_pct=1 WHERE id=$1;", ids["p3"])
    await insert_sell_filled(pool, ids["p3"], cost_usd="2.03", fee="0.002")
    final = await ledger.compute_state(pool, D("1000"))
    assert final.open_count == 0 and final.in_market == 0 and final.equity == final.cash


# --- no_capital -----------------------------------------------------------------


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_buy_is_skipped_as_no_capital_when_cash_is_short(pool, clock) -> None:
    ex = FakeExchange()
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes, start_capital=3)
    a = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    b = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    c = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))

    stats = await runner.run_once(ctx)

    rows = {r["position_id"]: r for r in await pool.fetch("SELECT * FROM demo_orders;")}
    assert rows[a]["status"] == "filled"                 # cash 3 ≥ 2
    assert rows[b]["status"] == "skipped" and rows[b]["skip_reason"] == "no_capital"
    assert rows[c]["skip_reason"] == "no_capital"        # cash после первой покупки: 1 < 2
    assert len(ex.sent("buy")) == 1                      # на биржу ушла одна покупка
    assert stats.no_capital == 2
    # алерт один раз в час, а не по сообщению на пропуск
    assert sum("no_capital" in n for n in notes) == 1
    await runner.run_once(ctx)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    assert sum("no_capital" in n for n in notes) == 1
    assert ctx.redis.ttl["demo:alert:no_capital"] == 3600


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_cash_equal_to_the_order_cost_is_enough(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock, start_capital=2)       # cash == order_cost_usd == 2
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))
    await runner.run_once(ctx)
    row = await pool.fetchrow("SELECT status, skip_reason FROM demo_orders WHERE position_id=$1;",
                              pid)
    assert row["status"] == "filled" and row["skip_reason"] is None
    assert len(ex.sent("buy")) == 1


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_money_under_an_unresolved_order_is_reserved(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock, start_capital=3)
    held = await add_position(pool, opened_at=clock.now() - timedelta(seconds=40))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, symbol, "
        "host, requested_cost_usd, virtual_ts, sent_at) VALUES ($1, 1, 'buy', 'sent', $2, "
        "'BTC/USDT', 'h', 2, now(), now());", held, f"at95b{held}")
    ex.orders[f"at95b{held}"] = ex._store("BTC/USDT", "buy", 0.00003, 60000.0, f"at95b{held}")
    new = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))
    # исход «sent» не разобран (open_polls), деньги под ним — зарезервированы
    ex.open_polls = 10 ** 6
    await runner.run_once(ctx)
    row = await pool.fetchrow("SELECT skip_reason FROM demo_orders WHERE position_id=$1;", new)
    assert row["skip_reason"] == "no_capital"


# --- стартовый капитал ------------------------------------------------------------


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_editing_the_start_capital_later_does_not_change_the_ledger(
    pool, clock, monkeypatch
) -> None:
    with capture_demo_logs(monkeypatch) as logs:
        first = await runner.ensure_state(pool, "www.okx.com", clock.now(), D("1000"))
        again = await runner.ensure_state(pool, "www.okx.com", clock.now(), D("5000"))
    assert first.start_capital == D("1000") and again.start_capital == D("1000")
    assert await pool.fetchval("SELECT start_capital FROM demo_state;") == D("1000.0000")
    warning = [e for e in logs.entries if e["event"] == "demo_start_capital_differs=1"]
    assert len(warning) == 1 and warning[0]["recorded"] == "1000.0000"
    assert warning[0]["configured"] == "5000"
    # учёт ведётся от записанного: состояние без сделок == записанному капиталу
    ctx = build_context(pool, FakeExchange(), clock, start_capital=again.start_capital)
    state = await ledger.ledger_state(ctx, clock.now())
    assert state.equity == D("1000") and state.cash == D("1000")


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_no_warning_when_the_start_capital_is_unchanged(pool, clock, monkeypatch) -> None:
    with capture_demo_logs(monkeypatch) as logs:
        await runner.ensure_state(pool, "www.okx.com", clock.now(), D("1000"))
        await runner.ensure_state(pool, "www.okx.com", clock.now(), D("1000"))
    assert not [e for e in logs.entries if e["event"] == "demo_start_capital_differs=1"]


# --- поля, которые пишутся в каждую исполненную ногу ---------------------------------


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_fee_usd_cash_after_equity_after_and_exit_reason_are_recorded(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await runner.run_once(ctx)

    buy = dict(await pool.fetchrow("SELECT * FROM demo_orders WHERE position_id=$1;", pid))
    # комиссия покупки в монете: fee_usd = fee × цена исполнения
    assert buy["fee_ccy"] == "BTC"
    assert buy["fee_usd"] == (buy["fee"] * buy["avg_price"]).quantize(D("0.000001"))
    assert buy["exit_reason"] is None
    # cash_after: комиссия в монете в деньги не входит → ровно 1000 − 2.0
    assert buy["cash_after_usd"] == D("998.000000")
    state = await ledger.ledger_state(ctx, clock.now())
    assert buy["equity_after_usd"] == state.equity.quantize(D("0.000001"))
    in_market = state.in_market.quantize(D("0.000001"))
    assert buy["equity_after_usd"] - buy["cash_after_usd"] == in_market

    # закрытая позиция с купленной ногой → продажа с причиной выхода из positions
    closed = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20), exit_reason="plus_exit")
    await insert_buy_filled(pool, closed)
    await runner.run_once(ctx)
    sell = dict(await pool.fetchrow(
        "SELECT * FROM demo_orders WHERE position_id=$1 AND leg='sell';", closed))
    assert sell["status"] == "filled" and sell["exit_reason"] == "plus_exit"
    assert sell["fee_ccy"] == "USDT" and sell["fee_usd"] == sell["fee"].quantize(D("0.000001"))
    after = await ledger.ledger_state(ctx, clock.now())
    assert sell["cash_after_usd"] == after.cash.quantize(D("0.000001"))
    assert sell["equity_after_usd"] == after.equity.quantize(D("0.000001"))


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_sell_reason_is_stored_even_for_dust_rows(pool, clock) -> None:
    ctx = build_context(pool, FakeExchange(), clock)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20), exit_reason="timeout")
    await insert_buy_filled(pool, pid, filled_qty="0.000011", fee="0.000002")
    await runner.run_once(ctx)
    row = await pool.fetchrow("SELECT status, exit_reason FROM demo_orders WHERE leg='sell';")
    assert row["status"] == "dust" and row["exit_reason"] == "timeout"


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_daily_maximum_in_market_is_tracked_in_redis(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await runner.run_once(ctx)
    key = ledger.max_in_market_key(ledger.local_day(clock.now(), ctx.tz))
    assert key.startswith("demo:max_in_market:") and ctx.redis.ttl[key] == 3 * 86400
    first = D(ctx.redis.data[key])
    assert first > 0
    # цена упала — максимум не уменьшается
    ex.prices["BTC/USDT"] = 30000.0
    await runner.run_once(ctx)
    assert D(ctx.redis.data[key]) == first
    # цена выросла — максимум растёт
    ex.prices["BTC/USDT"] = 90000.0
    await runner.run_once(ctx)
    assert D(ctx.redis.data[key]) > first
