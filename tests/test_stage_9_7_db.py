"""Этап 9.7, §8: новые запросы бота (только SELECT) на настоящей PostgreSQL."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.bot import nav
from src.bot.poller import BotPoller
from src.bot.queries import BotQueries
from src.core.config import settings
from tests.demo_support import (
    add_candle,
    add_position,
    build_database,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    reset_data,
    server_up,
    set_state,
)

D = Decimal
NOW = datetime.now(UTC)

pytestmark = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("bot97")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


async def closed_pair(pool, *, symbol="BTC/USDT", days_ago=1.0, hours=2, sell_cost="2.04",
                      reason="target", buy_cost="2.0"):
    """Закрытая демо-пара: покупка за ``hours`` часов до продажи, продажа ``days_ago`` назад."""
    sold = NOW - timedelta(days=days_ago)
    pid = await add_position(pool, symbol=symbol, opened_at=sold - timedelta(hours=hours),
                             status="closed", closed_at=sold, exit_reason=reason)
    await insert_buy_filled(pool, pid, symbol=symbol, cost_usd=buy_cost,
                            filled_at=sold - timedelta(hours=hours))
    await insert_sell_filled(pool, pid, symbol=symbol, cost_usd=sell_cost, filled_at=sold,
                             exit_reason=reason)
    return pid


async def skipped_buy(pool, reason, *, created_ago_h=1, symbol="BTC/USDT", status="skipped"):
    pid = await add_position(pool, symbol=symbol, opened_at=NOW - timedelta(hours=created_ago_h))
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol=$1;", symbol)
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts, created_at) "
        "VALUES ($1, $2, 'buy', $3, $4, $5, $6, 'www.okx.com', $7, $7);",
        pid, inst["id"], status, reason, f"sk{pid}", symbol, NOW - timedelta(hours=created_ago_h))
    return pid


async def test_the_report_counts_pairs_with_the_ledger_profit(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=30))
    plus = await closed_pair(pool, sell_cost="2.10", hours=1)             # +0.098...
    minus = await closed_pair(pool, symbol="DOGE/USDT", sell_cost="1.90", hours=3, days_ago=2)
    flat = await closed_pair(pool, sell_cost="2.002", days_ago=3)         # ровно издержки → ≤ 0
    old = await closed_pair(pool, sell_cost="3.0", days_ago=20)
    queries = BotQueries(pool)

    week = await queries.demo_report(NOW - timedelta(days=7))
    assert week["closed"] == 3 and week["wins"] == 1 and week["losses"] == 2
    # прибыль пары — ТОЛЬКО ledger.pair_profit (продажа минус покупка, комиссии в USDT)
    from src.demo import ledger

    pairs = await ledger.fetch_pairs(pool, NOW - timedelta(days=7), None)
    assert week["profit_usd"] == sum((ledger.pair_profit(p) for p in pairs), D(0))
    assert week["best"]["position_id"] == plus and week["best"]["token"] == "BTC"
    assert week["worst"]["position_id"] == minus and week["worst"]["token"] == "DOGE"
    assert week["best"]["pct"] > 0 > week["worst"]["pct"]
    assert week["fees_usd"] > 0 and week["avg_hold_sec"] > 0
    assert week["errors"] == 0 and week["skipped_by_reason"] == {}
    assert flat > 0 and old > 0

    everything = await queries.demo_report(None)
    assert everything["closed"] == 4
    today = await queries.demo_report(NOW - timedelta(hours=1))
    assert today["closed"] == 0 and today["avg_pct"] is None and today["best"] is None


async def test_before_start_is_not_a_skip_and_reasons_are_counted(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=30))
    await skipped_buy(pool, "before_start")
    await skipped_buy(pool, "before_start")
    for reason in ("stale", "stale", "no_capital", "no_balance"):
        await skipped_buy(pool, reason)
    await skipped_buy(pool, "stale", created_ago_h=24 * 10)                # вне окна
    sell_dust = await skipped_buy(pool, "below_min_size", status="dust")
    assert sell_dust
    report = await BotQueries(pool).demo_report(NOW - timedelta(days=7))
    assert report["skipped_by_reason"] == {
        "stale": 2, "no_capital": 1, "no_balance": 1, "below_min_size": 1}
    assert "before_start" not in report["skipped_by_reason"]
    whole = await BotQueries(pool).demo_report(None)
    assert whole["skipped_by_reason"]["stale"] == 3


async def test_exchange_errors_are_counted_separately(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=30))
    for status in ("rejected", "lost"):
        pid = await add_position(pool, opened_at=NOW - timedelta(hours=1))
        inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol='BTC/USDT';")
        await pool.execute(
            "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
            "symbol, host, virtual_ts) VALUES ($1, $2, 'buy', $3, $4, 'BTC/USDT', 'h', now());",
            pid, inst["id"], status, f"err{status}")
    report = await BotQueries(pool).demo_report(NOW - timedelta(days=1))
    assert report["errors"] == 2 and report["skipped_by_reason"] == {}


async def test_recent_closed_newest_first_with_reason_and_hold_hours(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=30))
    ids = [await closed_pair(pool, days_ago=day, reason="target" if day % 2 else "timeout")
           for day in (6, 5, 4, 3, 2, 1)]
    recent = await BotQueries(pool).demo_recent_closed(5)
    assert [r["position_id"] for r in recent] == list(reversed(ids))[:5]
    first = recent[0]
    assert first["token"] == "BTC" and first["exit_reason"] == "target"
    assert first["hold_hours"] == 48 and first["hold_sec"] == pytest.approx(7200, abs=1)
    assert first["profit"] > 0 and first["pct"] is not None
    assert await BotQueries(pool).demo_recent_closed(0) == []


async def test_yesterdays_close_comes_from_the_snapshot_by_local_date(pool) -> None:
    await pool.execute(
        "INSERT INTO demo_balance_daily (day, equity_open, equity_close, cash_close, "
        "in_market_close, opened_count, closed_count, wins, losses, skipped_count, fees_usd, "
        "max_in_market_usd) VALUES ('2026-10-06', 1000, 1001.5, 1001.5, 0, 0, 0, 0, 0, 0, 0, 0);")
    queries = BotQueries(pool)
    assert await queries.demo_yesterday_close(date(2026, 10, 7)) == D("1001.5")
    assert await queries.demo_yesterday_close(date(2026, 10, 8)) is None        # нет строки


async def test_agents_are_grouped_by_coin_and_the_futures_contract_counts_as_its_coin(pool) -> None:
    await pool.execute(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) VALUES "
        "('okx', 'BTC/USDT:USDT', 'BTC', 'USDT', 'swap'), "
        "('okx', 'DOGE/USDT:USDT', 'DOGE', 'USDT', 'swap');")
    ids = {r["symbol"]: r["id"] for r in await pool.fetch("SELECT id, symbol FROM instruments;")}

    async def out(agent, symbol, signal, conf, ago_s):
        await pool.execute(
            "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) "
            "VALUES ($1, $2, $3, $4, $5);",
            agent, ids[symbol], NOW - timedelta(seconds=ago_s), signal, conf)

    await out("market", "BTC/USDT", "bullish", 0.7, 100)
    await out("market", "BTC/USDT", "bearish", 0.9, 500)                    # старее — не берётся
    await out("liquidity", "BTC/USDT", "neutral", 0.4, 20)
    await out("futures", "BTC/USDT:USDT", "bearish", 0.55, 30)              # контракт
    await out("market", "DOGE/USDT", "bearish", 0.6, 10)
    queries = BotQueries(pool)
    grouped = await queries.agents_by_token(["market", "liquidity", "futures"])
    assert set(grouped) == {"BTC", "DOGE"}
    assert grouped["BTC"]["market"]["signal"] == "bullish" and grouped["BTC"]["market"][
        "confidence"] == pytest.approx(0.7)
    assert grouped["BTC"]["futures"]["signal"] == "bearish"
    assert grouped["BTC"]["liquidity"]["signal"] == "neutral"
    assert grouped["DOGE"] == {"market": grouped["DOGE"]["market"]}
    assert grouped["DOGE"]["market"]["signal"] == "bearish"                  # у каждой — своё

    await pool.execute(
        "INSERT INTO signals (instrument_id, ts, decision, probability) VALUES "
        "($1, $3, 'wait', 0.4), ($1, $4, 'buy', 0.74), ($2, $4, 'sell', 0.6);",
        ids["BTC/USDT"], ids["DOGE/USDT"], NOW - timedelta(minutes=5), NOW - timedelta(minutes=1))
    latest = await queries.latest_signal_by_token()
    assert latest["BTC"]["decision"] == "buy" and latest["BTC"]["probability"] == 0.74
    assert latest["DOGE"]["decision"] == "sell" and "FOO" not in latest


async def test_last_signals_now_carry_the_symbol_and_instruments_are_only_active_spot(pool) -> None:
    await pool.execute(
        "INSERT INTO instruments (exchange, symbol, base, quote, type, active) VALUES "
        "('okx', 'OLD/USDT', 'OLD', 'USDT', 'spot', FALSE), "
        "('okx', 'BTC/USDT:USDT', 'BTC', 'USDT', 'swap', TRUE);")
    queries = BotQueries(pool)
    symbols = [s for _, s in await queries.spot_instruments()]
    assert symbols == ["BTC/USDT", "DOGE/USDT", "FOO/USDT"]
    await add_position(pool, opened_at=NOW - timedelta(hours=1))
    [signal] = await queries.last_signals(False, 5, None)
    assert signal["symbol"] == "BTC/USDT" and signal["decision"] == "buy"


async def test_demo_state_and_overview_carry_what_the_screens_need(pool) -> None:
    queries = BotQueries(pool)
    assert await queries.demo_state() is None
    await set_state(pool, 1000, NOW - timedelta(days=2))
    pid = await add_position(pool, opened_at=NOW - timedelta(hours=1))
    await insert_buy_filled(pool, pid, avg_price="60000", cost_usd="2.0",
                            filled_at=NOW - timedelta(minutes=59))
    await add_candle(pool, "BTC/USDT", 60300.0, NOW - timedelta(minutes=3))
    demo = await queries.demo_state()
    assert demo["state"].open_count == 1 and demo["mirror_since"] is not None
    overview = await queries.demo_overview()
    trade = overview["open_trades"][0]
    assert trade["current_price"] == D("60300") and trade["target_pct"] == D("1")
    assert trade["target_price"] == D("60600") and trade["opened_at"] is not None
    assert overview["state"] == demo["state"]


async def test_the_screens_render_from_the_real_queries_and_are_fast(pool, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", False)
    await set_state(pool, 1000, NOW - timedelta(days=5))
    for day in (1, 2, 3):
        await closed_pair(pool, days_ago=day)
    open_id = await add_position(pool, opened_at=NOW - timedelta(hours=1))
    await insert_buy_filled(pool, open_id, filled_at=NOW - timedelta(minutes=59))
    await add_candle(pool, "BTC/USDT", 60100.0, NOW - timedelta(minutes=3))
    await skipped_buy(pool, "stale")

    async def read_hb():
        return []

    bot = BotPoller(BotQueries(pool))
    bot._read_heartbeats = read_hb
    for target in (nav.NavTarget(nav.MENU), nav.NavTarget(nav.ACCOUNT), nav.NavTarget(nav.TRADES),
                   nav.NavTarget(nav.RESULTS, period="7d"), nav.NavTarget(nav.AGENTS),
                   nav.NavTarget(nav.SIGNALS, mode="a"), nav.NavTarget(nav.SYSTEM),
                   nav.NavTarget(nav.SETTINGS), nav.NavTarget(nav.HELP)):
        started = time.perf_counter()
        text, markup = await bot._render(target, 1, NOW)
        assert time.perf_counter() - started < 1.0, target
        assert text and markup["inline_keyboard"]
    results, _ = await bot._render(nav.NavTarget(nav.RESULTS, period="7d"), 1, NOW)
    assert "Закрыто сделок: 3" in results and "опоздание 1" in results
    trades, _ = await bot._render(nav.NavTarget(nav.TRADES), 1, NOW)
    assert "<b>Открыто: 1</b>" in trades and "Последние закрытые" in trades
    main, _ = await bot._render(nav.NavTarget(nav.MENU), 1, NOW)
    assert "Следит за 3 монетами: BTC · DOGE · FOO" in main


async def test_the_screens_say_not_started_on_an_empty_database(pool, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    bot = BotPoller(BotQueries(pool))

    async def read_hb():
        return []

    bot._read_heartbeats = read_hb
    for target in (nav.NavTarget(nav.ACCOUNT), nav.NavTarget(nav.TRADES),
                   nav.NavTarget(nav.RESULTS, period="all")):
        text, _ = await bot._render(target, 1, NOW)
        assert "Демо-счёт ещё не запущен" in text, target
    main, _ = await bot._render(nav.NavTarget(nav.MENU), 1, NOW)
    assert "💰 Демо-счёт ещё не запущен" in main


def test_the_new_queries_only_read() -> None:
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "bot" / "queries.py").read_text(
        encoding="utf-8")
    start = src.index("    async def agents_by_token")
    block = src[start:src.index("    async def stats_versions")] + src[
        src.index("    async def demo_state"):src.index("    async def positions_capital")]
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", block)
    assert nav.PERIODS == ("today", "7d", "30d", "all")


async def test_a_reader_role_can_run_every_new_query(db_name) -> None:
    """Роль agenttrade_ro получает SELECT на таблицы, которые читают новые запросы."""
    import asyncpg

    from src.bot.queries import ensure_readonly_role
    from tests.demo_support import HOST, PORT, USER

    admin = await asyncpg.connect(host=HOST, port=int(PORT), user=USER, database=db_name)
    try:
        await ensure_readonly_role(admin, "ro_pass_97", db_name)
    except asyncpg.PostgresError as exc:       # роль уже есть на кластере другой БД — права те же
        pytest.skip(f"роль не подготовить в тестовом кластере: {exc}")
    finally:
        await admin.close()
    ro = await asyncpg.create_pool(host=HOST, port=int(PORT), user="agenttrade_ro",
                                   password="ro_pass_97", database=db_name, min_size=1,
                                   max_size=2)
    try:
        queries = BotQueries(ro)
        await queries.demo_report(None)
        await queries.demo_recent_closed(5)
        await queries.demo_yesterday_close(date(2026, 10, 7))
        await queries.agents_by_token(["market"])
        await queries.latest_signal_by_token()
        await queries.demo_state()
        await queries.last_signals(False, 3, None)
    finally:
        await ro.close()


async def test_agent_outputs_older_than_the_window_are_not_read(pool) -> None:
    """Граница по времени — и скорость (журнал растёт каждую минуту), и смысл: всё, что
    старше порога свежести, на экране всё равно «нет свежих данных»."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol='BTC/USDT';")
    await pool.execute(
        "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) VALUES "
        "('market', $1, $2, 'bullish', 0.7);", inst["id"], NOW - timedelta(hours=2))
    queries = BotQueries(pool)
    assert await queries.agents_by_token(["market"]) == {}
    wide = await queries.agents_by_token(["market"], max_age_sec=86_400)
    assert wide["BTC"]["market"]["signal"] == "bullish"


async def test_freshness_by_instrument_keeps_its_answers_after_the_rewrite(pool) -> None:
    await add_candle(pool, "BTC/USDT", 1.0, NOW - timedelta(minutes=9))
    await add_candle(pool, "BTC/USDT", 2.0, NOW - timedelta(minutes=2))
    await add_candle(pool, "DOGE/USDT", 3.0, NOW - timedelta(minutes=30))
    queries = BotQueries(pool)
    rows = dict(await queries.freshness_by_instrument(None))
    assert set(rows) == {"BTC/USDT", "DOGE/USDT", "FOO/USDT"}
    assert abs((NOW - rows["BTC/USDT"]).total_seconds() - 120) < 5      # самая свежая свеча
    assert rows["FOO/USDT"] is None                                       # свечей нет
    btc = await pool.fetchval("SELECT id FROM instruments WHERE symbol='BTC/USDT';")
    assert [s for s, _ in await queries.freshness_by_instrument([btc])] == ["BTC/USDT"]
