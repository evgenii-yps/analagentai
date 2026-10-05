"""Этап 9.5, §6 и §12 п. 6–9, 11: цикл сервиса на настоящей PostgreSQL.

Биржа — заглушка (сети нет), база — настоящая: схема из ``db/init.sql`` и всех
миграций. Нет PostgreSQL — тесты пропускаются (как в тестах 9.4).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from src.demo import rules, runner
from tests.demo_support import (
    SECRETS,
    Clock,
    FakeExchange,
    FakeRedis,
    add_position,
    build_context,
    build_database,
    capture_demo_logs,
    drop_database,
    insert_buy_filled,
    open_pool,
    reset_data,
    server_up,
)

pytestmark = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")

D = Decimal


@pytest.fixture(scope="module")
def db_name():
    name = build_database()
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


async def rows(pool, where: str = "TRUE") -> list[dict]:
    got = await pool.fetch(f"SELECT * FROM demo_orders WHERE {where} ORDER BY id;")
    return [dict(r) for r in got]


async def one(pool, position_id: int, leg: str) -> dict | None:
    got = await rows(pool, f"position_id = {position_id} AND leg = '{leg}'")
    return got[0] if got else None


# --- покупка ----------------------------------------------------------------


async def test_a_new_position_is_bought_and_the_execution_is_recorded(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))

    stats = await runner.run_once(ctx)

    assert stats.buys == 1
    buy = await one(pool, pid, "buy")
    assert buy["status"] == "filled" and buy["skip_reason"] is None
    assert buy["cl_ord_id"] == f"at95b{pid}"
    assert buy["symbol"] == "BTC/USDT" and buy["host"] == "www.okx.com"
    assert buy["requested_cost_usd"] == D("2")
    assert buy["avg_price"] == D("60060")                      # 60000 · 1.001
    assert buy["cost_usd"] == D("2.0")
    assert buy["fee_ccy"] == "BTC" and buy["fee"] > 0
    assert buy["virtual_price"] == D("60000")
    assert buy["slippage_pct"] == pytest.approx(D("0.1"), abs=D("0.000001"))
    assert buy["lag_sec"] == 30 or buy["lag_sec"] in (29, 30, 31)
    assert buy["exchange_order_id"] and buy["filled_at"] is not None
    # на биржу ушла ровно одна покупка: на сумму, только спот, только cash
    assert ex.sent("buy") == [("buy", "BTC/USDT", 2.0,
                               {"clOrdId": f"at95b{pid}", "tdMode": "cash"})]


async def test_the_amount_comes_only_from_order_cost_usd(pool, clock, monkeypatch) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    monkeypatch.setattr(rules, "order_cost_usd", lambda position: D("3"))

    await runner.run_once(ctx)

    assert ex.sent("buy")[0][2] == 3.0
    assert (await one(pool, pid, "buy"))["requested_cost_usd"] == D("3")


async def test_the_position_size_is_never_written(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    before = dict(await pool.fetchrow("SELECT * FROM positions WHERE id = $1;", pid))

    await runner.run_once(ctx)

    after = dict(await pool.fetchrow("SELECT * FROM positions WHERE id = $1;", pid))
    assert before == after


# --- п. 6: двойная отправка невозможна ---------------------------------------


async def test_a_repeated_iteration_does_not_resend_an_existing_order(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))

    await runner.run_once(ctx)
    await runner.run_once(ctx)
    clock.advance(5)
    await runner.run_once(ctx)

    assert len(ex.sent("buy")) == 1
    assert len(await rows(pool)) == 1


async def test_a_second_service_instance_cannot_send_the_same_order_either(pool, clock) -> None:
    ex = FakeExchange()
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await runner.run_once(build_context(pool, ex, clock))
    await runner.run_once(build_context(pool, ex, clock))   # «перезапуск»
    assert len(ex.sent("buy")) == 1


async def test_a_sent_row_without_a_reply_is_resolved_by_clordid_not_resent(pool, clock) -> None:
    """Сеть оборвалась ПОСЛЕ того, как ордер дошёл: ответа нет, строка остаётся sent."""
    ex = FakeExchange(mode="timeout_reached")
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))

    await runner.run_once(ctx)
    row = await one(pool, pid, "buy")
    assert row["status"] == "sent" and row["filled_qty"] is None
    assert len(ex.sent("buy")) == 1

    # «перезапуск»: новый контекст, биржа снова отвечает
    ex.mode = "normal"
    clock.advance(30)
    stats = await runner.run_once(build_context(pool, ex, clock))

    assert stats.recovered == 1
    row = await one(pool, pid, "buy")
    assert row["status"] == "filled" and row["filled_qty"] is not None
    assert len(ex.sent("buy")) == 1                       # повторной отправки нет
    assert ("fetch_order", f"at95b{pid}") in ex.calls     # разобрано запросом по clOrdId


async def test_an_order_the_exchange_never_saw_becomes_lost_and_is_not_resent(
    pool, clock
) -> None:
    ex = FakeExchange(mode="timeout_lost")
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))

    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "sent"

    ex.mode = "normal"
    clock.advance(5)                       # меньше запаса: ордер ещё может быть в пути
    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "sent"

    clock.advance(runner.RECOVERY_GRACE_SEC + 5)
    stats = await runner.run_once(ctx)
    row = await one(pool, pid, "buy")
    assert stats.lost == 1 and row["status"] == "lost"
    assert len(ex.sent("buy")) == 1        # повторно такой ордер НЕ отправляется
    assert any("lost" in n for n in notes)


async def test_a_pending_row_left_by_a_crash_is_resolved_not_resent(pool, clock) -> None:
    """Процесс упал между вставкой строки и отправкой: строка pending, ордера нет."""
    ex = FakeExchange()
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
        "symbol, host, requested_cost_usd, virtual_price, virtual_ts) "
        "SELECT p.id, p.instrument_id, 'buy', 'pending', $2, 'BTC/USDT', 'www.okx.com', 2, "
        "p.entry_price, p.opened_at FROM positions p WHERE p.id = $1;",
        pid, f"at95b{pid}",
    )
    ctx = build_context(pool, ex, clock)
    clock.advance(runner.RECOVERY_GRACE_SEC + 60)
    await runner.run_once(ctx)
    assert ex.sent() == []
    assert (await one(pool, pid, "buy"))["status"] == "lost"


async def test_a_pending_row_whose_order_reached_the_exchange_is_filled(pool, clock) -> None:
    ex = FakeExchange()
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    ex._store("BTC/USDT", "buy", 0.00003, 60060.0, f"at95b{pid}")      # ордер уже на бирже
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
        "symbol, host, requested_cost_usd, virtual_price, virtual_ts) "
        "SELECT p.id, p.instrument_id, 'buy', 'pending', $2, 'BTC/USDT', 'www.okx.com', 2, "
        "p.entry_price, p.opened_at FROM positions p WHERE p.id = $1;",
        pid, f"at95b{pid}",
    )
    await runner.run_once(build_context(pool, ex, clock))
    assert ex.sent() == []
    assert (await one(pool, pid, "buy"))["status"] == "filled"


async def test_a_slow_fill_is_picked_up_later_without_resending(pool, clock) -> None:
    ex = FakeExchange(open_polls=100)      # ордер долго «open»
    ctx = build_context(pool, ex, clock, fill_wait_sec=3)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))

    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "sent"

    ex.open_polls = 0
    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "filled"
    assert len(ex.sent("buy")) == 1


async def test_a_fill_that_arrives_during_the_wait_is_recorded_at_once(pool, clock) -> None:
    ex = FakeExchange(open_polls=3)
    ctx = build_context(pool, ex, clock, fill_wait_sec=10)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))
    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "filled"


# --- п. 7: порядок — продажа раньше покупки ----------------------------------


async def test_sells_go_before_buys_in_one_iteration(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    old = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20), exit_price=60600.0)
    await insert_buy_filled(pool, old)
    new = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))

    await runner.run_once(ctx)

    kinds = [c[0] for c in ex.calls if c[0] in ("buy", "sell")]
    assert kinds == ["sell", "buy"]
    assert (await one(pool, old, "sell"))["status"] == "filled"
    assert (await one(pool, new, "buy"))["status"] == "filled"


# --- продажа ----------------------------------------------------------------


async def test_the_sell_quantity_excludes_the_buy_fee_and_is_rounded_down(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20), exit_price=60600.0)
    await insert_buy_filled(pool, pid, filled_qty="0.00003332", fee="0.00000003", fee_ccy="BTC")

    stats = await runner.run_once(ctx)

    assert stats.sells == 1
    call = ex.sent("sell")[0]
    assert D(str(call[2])) == D("0.00003329")                    # 0.00003332 − 0.00000003
    assert call[3] == {"clOrdId": f"at95s{pid}", "tdMode": "cash"}
    sell = await one(pool, pid, "sell")
    assert sell["status"] == "filled" and sell["virtual_price"] == D("60600")
    assert sell["avg_price"] == D("59940")                        # 60000 · 0.999
    # продали дешевле виртуальной цены 60600 → проскальзывание положительное (хуже)
    assert sell["slippage_pct"] == pytest.approx(D("1.089109"), abs=D("0.00001"))
    assert sell["fee_ccy"] == "USDT" and sell["lag_sec"] in (19, 20, 21)


async def test_a_sell_is_made_for_every_close_reason_even_data_gap(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    ids = []
    for reason in ("target", "timeout", "data_gap", "plus_exit"):
        pid = await add_position(
            pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
            closed_at=clock.now() - timedelta(seconds=20), exit_reason=reason)
        await insert_buy_filled(pool, pid)
        ids.append(pid)
    await runner.run_once(ctx)
    assert len(ex.sent("sell")) == 4
    for pid in ids:
        assert (await one(pool, pid, "sell"))["status"] == "filled"


async def test_dust_is_recorded_and_no_order_is_sent(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20))
    # куплено 0.000011, комиссия 0.000002 → к продаже 0.000009 < минимума 0.00001
    await insert_buy_filled(pool, pid, filled_qty="0.000011", fee="0.000002")

    stats = await runner.run_once(ctx)
    await runner.run_once(ctx)

    sell = await one(pool, pid, "sell")
    assert stats.dust == 1 and sell["status"] == "dust"
    assert sell["skip_reason"] == rules.SKIP_DUST
    assert ex.sent("sell") == []
    assert len(await rows(pool, f"position_id = {pid} AND leg = 'sell'")) == 1


async def test_no_sell_without_a_filled_buy(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20))
    await runner.run_once(ctx)
    assert ex.sent("sell") == []


async def test_a_sell_is_not_repeated_once_the_row_exists(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20))
    await insert_buy_filled(pool, pid)
    for _ in range(3):
        await runner.run_once(ctx)
    assert len(ex.sent("sell")) == 1


# --- п. 8: что не зеркалится -------------------------------------------------


async def test_a_position_opened_before_mirror_since_is_never_mirrored(pool, clock) -> None:
    ex = FakeExchange()
    since = clock.now() - timedelta(minutes=1)
    ctx = build_context(pool, ex, clock, mirror_since=since)
    # открыта до старта и ещё открыта
    open_old = await add_position(pool, opened_at=since - timedelta(seconds=1))
    # открыта до старта, закрыта после — продавать нечего
    closed_old = await add_position(
        pool, opened_at=since - timedelta(hours=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=10))
    # закрытая история до старта
    history = await add_position(
        pool, opened_at=since - timedelta(days=1), status="closed",
        closed_at=since - timedelta(hours=1))

    await runner.run_once(ctx)
    await runner.run_once(ctx)

    assert ex.sent() == []
    row = await one(pool, open_old, "buy")
    assert row["status"] == "skipped" and row["skip_reason"] == "before_start"
    assert await one(pool, closed_old, "buy") is None
    assert await one(pool, closed_old, "sell") is None
    assert await rows(pool, f"position_id = {history}") == []


async def test_a_stale_position_is_skipped_with_the_reason(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=200))
    await runner.run_once(ctx)
    row = await one(pool, pid, "buy")
    assert row["status"] == "skipped" and row["skip_reason"] == "stale"
    assert ex.sent() == []


async def test_a_position_opened_and_closed_between_iterations_gets_only_a_skip(
    pool, clock
) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(seconds=60), status="closed",
        closed_at=clock.now() - timedelta(seconds=30))
    await runner.run_once(ctx)
    await runner.run_once(ctx)
    buy = await one(pool, pid, "buy")
    assert buy["status"] == "skipped" and buy["skip_reason"] == "closed_before_buy"
    assert await one(pool, pid, "sell") is None
    assert ex.sent() == []


async def test_an_instrument_that_is_not_on_the_spot_market_is_skipped(pool, clock) -> None:
    ex = FakeExchange()
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes,
                        symbols=["BTC/USDT", "DOGE/USDT", "FOO/USDT"])
    pid = await add_position(pool, symbol="FOO/USDT", opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    row = await one(pool, pid, "buy")
    assert row["status"] == "skipped" and row["skip_reason"] == "no_market"
    assert ex.sent() == []
    assert any("FOO/USDT" in n for n in notes)
    assert "FOO/USDT" not in ctx.valid_symbols and "BTC/USDT" in ctx.valid_symbols


async def test_low_usdt_balance_skips_the_buy_and_alerts(pool, clock) -> None:
    ex = FakeExchange(usdt=D("5"))
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    row = await one(pool, pid, "buy")
    assert row["status"] == "skipped" and row["skip_reason"] == "no_balance"
    assert ex.sent() == [] and any("USDT" in n for n in notes)


async def test_the_balance_is_decremented_by_buys_within_an_iteration(pool, clock) -> None:
    ex = FakeExchange(usdt=D("21"))        # порог 20, покупка $2: после первой остаётся 19
    ctx = build_context(pool, ex, clock)
    a = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    b = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))
    await runner.run_once(ctx)
    assert (await one(pool, a, "buy"))["status"] == "filled"
    assert (await one(pool, b, "buy"))["skip_reason"] == "no_balance"
    assert len(ex.sent("buy")) == 1
    assert sum(1 for c in ex.calls if c[0] == "fetch_balance") == 1


# --- §6.3: ошибки ------------------------------------------------------------


async def test_an_exchange_refusal_is_rejected_and_never_retried(pool, clock) -> None:
    ex = FakeExchange(mode="reject")
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes)
    a = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    b = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))

    stats = await runner.run_once(ctx)
    await runner.run_once(ctx)
    clock.advance(5)
    await runner.run_once(ctx)

    assert stats.rejected == 2
    row = await one(pool, a, "buy")
    assert row["status"] == "rejected" and row["error_code"] == "51008"
    assert (await one(pool, b, "buy"))["status"] == "rejected"
    assert len(ex.sent("buy")) == 2                       # повтора нет
    # секреты, попавшие в текст ответа биржи, вычищены
    assert all(s not in (row["error_text"] or "") for s in SECRETS)
    # антидребезг: два отказа подряд — один алерт
    assert sum("rejected" in n or "отклонила" in n for n in notes) == 1
    assert ctx.halted is None                             # обычный отказ сервис не останавливает


async def test_error_50101_stops_sending_orders_until_restart(pool, clock) -> None:
    """§12 п. 9: ключ не того режима — ордера прекращаются до перезапуска."""
    ex = FakeExchange(mode="auth")
    notes: list[str] = []
    ctx = build_context(pool, ex, clock, notified=notes)
    first = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))

    await runner.run_once(ctx)

    assert ctx.halted == "key_mode"
    assert len(ex.sent()) == 1
    assert await one(pool, first, "buy") is None          # ордер не принят — строка снята
    assert len(notes) == 1 and "ключ" in notes[0]
    assert all(s not in notes[0] for s in SECRETS)

    # новые позиции и продажи после остановки не отправляются
    ex.mode = "normal"
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    closed = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20))
    await insert_buy_filled(pool, closed)
    for _ in range(3):
        await runner.run_once(ctx)
    assert len(ex.sent()) == 1
    assert len(notes) == 1

    # «перезапуск» с исправленным ключом: нога доделывается, продажа уходит
    clock.advance(10)
    await runner.run_once(build_context(pool, ex, clock))
    assert (await one(pool, first, "buy"))["status"] == "filled"
    assert (await one(pool, closed, "sell"))["status"] == "filled"


async def test_an_authentication_error_on_balance_also_stops_the_service(pool, clock) -> None:
    import ccxt.async_support as ccxt

    ex = FakeExchange(fail_balance=ccxt.AuthenticationError('okx {"code":"50113"}'))
    ctx = build_context(pool, ex, clock)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    assert ctx.halted == "key_mode" and ex.sent() == []


async def test_the_demo_guard_halts_the_service_and_sends_nothing(pool, clock) -> None:
    ex = FakeExchange()
    ex.headers["x-simulated-trading"] = "0"
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    assert ctx.halted is not None
    assert ex.calls == [("load_markets",)]
    assert await one(pool, pid, "buy") is None


async def test_markets_unavailable_means_no_iteration_work_and_a_retry(pool, clock) -> None:
    ex = FakeExchange(fail_markets=True)
    ctx = build_context(pool, ex, clock)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    assert ctx.markets_loaded is False and await rows(pool) == []
    ex.fail_markets = False
    await runner.run_once(ctx)
    assert (await one(pool, pid, "buy"))["status"] == "filled"


# --- сервис не падает, heartbeat, строка запуска -------------------------------


async def test_the_loop_survives_iteration_errors_and_sets_the_heartbeat(pool, clock) -> None:
    ex = FakeExchange()
    redis = FakeRedis()
    ctx = build_context(pool, ex, clock, redis=redis)
    real_pool = ctx.pool
    ticks = {"n": 0}

    class Broken:
        async def fetch(self, *a, **k):
            raise RuntimeError("база недоступна")

        fetchval = fetchrow = execute = fetch

    async def sleep(_s: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            ctx.pool = real_pool           # база «вернулась»
        if ticks["n"] >= 3:
            raise asyncio.CancelledError

    ctx.pool = Broken()
    ctx.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await runner.run(ctx)
    # первая итерация упала (heartbeat не обновлялся), следующие прошли
    assert runner.HEARTBEAT_KEY in redis.data
    assert redis.ttl[runner.HEARTBEAT_KEY] == 300
    assert ticks["n"] == 3


async def test_the_loop_calls_the_report_hook_each_iteration(pool, clock) -> None:
    ctx = build_context(pool, FakeExchange(), clock)
    calls = []

    async def report(c) -> None:
        calls.append(c)

    async def sleep(_s: float) -> None:
        raise asyncio.CancelledError

    ctx.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await runner.run(ctx, report=report)
    assert calls == [ctx]


async def test_ensure_state_records_mirror_since_once(pool, clock) -> None:
    first = await runner.ensure_state(pool, "www.okx.com", clock.now(), D("1000"))
    clock.advance(3600)
    second = await runner.ensure_state(pool, "eea.okx.com", clock.now(), D("1000"))
    assert first.mirror_since == second.mirror_since
    assert await pool.fetchval("SELECT count(*) FROM demo_state;") == 1


async def test_the_startup_fields_are_the_actual_values_without_keys(pool, clock) -> None:
    ctx = build_context(pool, FakeExchange(), clock, host="eea.okx.com", interval_sec=7,
                        max_entry_delay_sec=99, min_usdt_balance=D("12"))
    fields = runner.startup_fields(ctx)
    assert {"enabled", "host", "mirror_since", "interval", "max_entry_delay_sec",
            "min_usdt_balance", "instruments"} <= set(fields)
    assert fields["start_capital"] == "1000" and fields["timezone"] == "Europe/Moscow"
    assert fields["host"] == "eea.okx.com" and fields["interval"] == 7
    assert fields["max_entry_delay_sec"] == 99 and fields["min_usdt_balance"] == "12"
    assert fields["instruments"] == ["BTC/USDT", "DOGE/USDT"]
    assert fields["enabled"] is True
    assert all(s not in repr(fields) for s in SECRETS)


# --- п. 11: ключи не попадают в журнал ---------------------------------------


async def test_keys_never_reach_the_log_the_alerts_or_the_database(
    pool, clock, monkeypatch
) -> None:
    notes: list[str] = []
    with capture_demo_logs(monkeypatch) as logs:
        ex = FakeExchange(mode="reject")
        ctx = build_context(pool, ex, clock, notified=notes)
        await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
        await runner.run_once(ctx)
        ex.mode = "auth"
        await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))
        await runner.run_once(ctx)
        # и строка запуска, и итог итерации
        ex.mode = "normal"
        ctx2 = build_context(pool, ex, clock)
        ctx2.sleep = _cancel
        with pytest.raises(asyncio.CancelledError):
            await runner.run(ctx2)

    assert logs.entries, "логов не перехвачено — проверка ничего не проверяет"
    blob = logs.text() + " ".join(notes)
    blob += repr(await rows(pool))
    for secret in SECRETS:
        assert secret not in blob
    # при этом сами события на месте: отказ и остановка залогированы
    events = {e["event"] for e in logs.entries}
    assert "demo_rejected=1" in events and "demo_halted=1" in events


async def _cancel(_s: float) -> None:
    raise asyncio.CancelledError


def test_scrub_replaces_every_secret() -> None:
    text = f"ошибка {SECRETS[0]} и {SECRETS[2]}"
    out = runner.scrub(text, SECRETS)
    assert SECRETS[0] not in out and SECRETS[2] not in out and "***" in out
    assert runner.scrub("без секретов", SECRETS) == "без секретов"
    assert runner.scrub("текст", ("",)) == "текст"


def test_error_code_is_read_from_the_exchange_reply() -> None:
    assert runner.error_code_of(
        Exception('okx {"code":"1","data":[{"sCode":"51008"}]}')) == "51008"
    assert runner.error_code_of(Exception('okx {"code":"50101","msg":"x"}')) == "50101"
    assert runner.error_code_of(Exception("таймаут")) is None


# --- алерты: антидребезг -----------------------------------------------------


async def test_alerts_are_throttled_by_a_redis_key_with_a_one_hour_ttl(pool, clock) -> None:
    redis = FakeRedis()
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, redis=redis, notified=notes)
    await runner.alert(ctx, "lost", "первый")
    await runner.alert(ctx, "lost", "второй")
    await runner.alert(ctx, "other", "третий")
    assert len(notes) == 2
    assert redis.ttl["demo:alert:lost"] == 3600 and "demo:alert:other" in redis.data


async def test_a_dead_redis_does_not_silence_an_alert(pool, clock) -> None:
    class Dead:
        async def set(self, *a, **k):
            raise ConnectionError("redis недоступен")

    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes)
    ctx.redis = Dead()
    await runner.alert(ctx, "lost", "важное")
    assert len(notes) == 1


# --- вторая линия защиты: уникальные ограничения базы ---------------------------


async def test_the_unique_constraints_alone_stop_a_double_send(pool, clock, monkeypatch) -> None:
    """Гонка двух экземпляров: отбор «видит» позицию, у которой строка уже есть.

    Запросы отбора здесь подменены на такие, что НЕ исключают обработанные позиции, —
    тем проверяется именно база: ``ON CONFLICT DO NOTHING`` по уникальным ключам
    ``(position_id, leg)`` и ``cl_ord_id`` не даёт отправить ордер второй раз.
    """
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)
    sold = await add_position(
        pool, opened_at=clock.now() - timedelta(minutes=5), status="closed",
        closed_at=clock.now() - timedelta(seconds=20))
    await insert_buy_filled(pool, sold)
    bought = await add_position(pool, opened_at=clock.now() - timedelta(seconds=10))

    await runner.run_once(ctx)
    assert len(ex.sent("sell")) == 1 and len(ex.sent("buy")) == 1

    monkeypatch.setattr(runner, "_SELL_CANDIDATES_SQL", runner._SELL_CANDIDATES_SQL.replace(
        "AND NOT EXISTS (SELECT 1 FROM demo_orders s WHERE s.position_id = p.id "
        "AND s.leg = 'sell') ", ""))
    monkeypatch.setattr(runner, "_buy_candidates_sql", lambda: (
        "SELECT p.id, p.instrument_id, p.side, p.status, p.opened_at, p.entry_price, "
        "p.notional_usd, i.symbol, i.base FROM positions p "
        "JOIN instruments i ON i.id = p.instrument_id "
        "WHERE p.is_virtual AND p.status = 'open' AND p.opened_at >= $1 ORDER BY p.id;"))
    assert "NOT EXISTS" not in runner._SELL_CANDIDATES_SQL

    for _ in range(3):
        await runner.run_once(build_context(pool, ex, clock))

    assert len(ex.sent("sell")) == 1 and len(ex.sent("buy")) == 1
    assert len(await rows(pool, f"position_id IN ({sold}, {bought})")) == 3
