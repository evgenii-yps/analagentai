"""Этап 9.5, редакция 2, §7: команда бота /demo (только чтение)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.bot import handlers
from src.bot.poller import BotPoller
from src.bot.queries import BotQueries
from src.demo import ledger
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


def state(**over) -> ledger.LedgerState:
    values = dict(start_capital=D("1000"), cash=D("990"), in_market=D("12.5"),
                  equity=D("1002.5"), open_count=1)
    values.update(over)
    return ledger.LedgerState(**values)


def data(**over) -> dict:
    values = {
        "state": state(),
        "open_trades": [{
            "position_id": 41, "token": "BTC", "entry_price": D("60060"),
            "current_pct": D("0.35"),
            "deadline_at": NOW + timedelta(hours=5, minutes=12, seconds=30),
        }],
        "days": 7, "closed": 12, "wins": 8, "profit_usd": D("0.1234"),
    }
    values.update(over)
    return values


def test_demo_is_a_known_command_and_listed_in_help() -> None:
    assert "demo" in handlers.KNOWN_COMMANDS and "positions" in handlers.KNOWN_COMMANDS
    help_text = handlers.render_help()
    assert "/demo" in help_text and "/positions" in help_text      # /positions не удалён
    assert handlers.parse_command("/demo")[0] == "demo"


def test_the_reply_with_open_trades() -> None:
    text = handlers.render_demo(data(), NOW)
    assert "Демо-счёт OKX" in text and "деньги не настоящие" in text
    assert "Баланс итого: $1,002.50" in text and "с начала +2.50 $ (+0.25%)" in text
    assert "Свободно $990.00 · в рынке $12.50" in text
    assert "стартовый капитал $1,000.00" in text
    assert "Открытые демо-сделки: 1" in text
    assert "BTC (#41)" in text and "вход 60 060.00" in text and "сейчас +0.35%" in text
    assert "до срока 5 ч 12 мин" in text
    assert "<b>За 7 дней:</b> закрыто 12 · прибыльных 8 · сумма прибыли +0.1234 $" in text


def test_the_reply_with_no_open_trades_and_with_an_expired_one() -> None:
    text = handlers.render_demo(data(open_trades=[], state=state(open_count=0, in_market=D("0"),
                                                                 equity=D("990"), cash=D("990"))),
                                NOW)
    assert "Открытые демо-сделки: 0" in text and "Открытых демо-сделок нет." in text
    assert "с начала -10.00 $ (-1.00%)" in text
    late = handlers.render_demo(data(open_trades=[{
        "position_id": 1, "token": "ETH", "entry_price": D("2500"), "current_pct": None,
        "deadline_at": NOW - timedelta(minutes=1)}]), NOW)
    assert "срок истёк" in late and "сейчас —" in late


def test_the_reply_before_the_service_has_ever_started() -> None:
    text = handlers.render_demo(None, NOW)
    assert "ещё не запускалось" in text and "Демо-счёт OKX" in text


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demobot")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_overview_on_an_empty_database_is_none(pool) -> None:
    assert await BotQueries(pool).demo_overview() is None
    text = handlers.render_demo(None, NOW)
    assert "ещё не запускалось" in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_overview_with_the_state_but_no_trades(pool) -> None:
    await set_state(pool, 500, NOW - timedelta(days=1))
    overview = await BotQueries(pool).demo_overview()
    assert overview["open_trades"] == [] and overview["closed"] == 0
    assert overview["state"].equity == D("500") and overview["state"].cash == D("500")
    text = handlers.render_demo(overview, NOW)
    assert "Баланс итого: $500.00" in text and "Открытых демо-сделок нет." in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_overview_with_open_and_closed_trades(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=10))
    # открытая сделка со сроком через 6 часов
    open_id = await add_position(pool, opened_at=NOW - timedelta(hours=1))
    await pool.execute("UPDATE positions SET deadline_at = $2 WHERE id = $1;", open_id,
                       NOW + timedelta(hours=6, minutes=5))
    await insert_buy_filled(pool, open_id, filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60000", cost_usd="2.0",
                            filled_at=NOW - timedelta(minutes=59))
    await add_candle(pool, "BTC/USDT", 66000.0, NOW - timedelta(minutes=3))
    # закрытая за последние 7 дней — в плюс; закрытая 20 дней назад — в итог не входит
    fresh = await add_position(pool, opened_at=NOW - timedelta(days=2), status="closed",
                               closed_at=NOW - timedelta(days=2) + timedelta(hours=1))
    await insert_buy_filled(pool, fresh, filled_at=NOW - timedelta(days=2))
    await insert_sell_filled(pool, fresh, cost_usd="2.04", fee="0.002",
                             filled_at=NOW - timedelta(days=2) + timedelta(hours=1))
    old = await add_position(pool, opened_at=NOW - timedelta(days=20), status="closed",
                             closed_at=NOW - timedelta(days=20) + timedelta(hours=1))
    await insert_buy_filled(pool, old, filled_at=NOW - timedelta(days=20))
    await insert_sell_filled(pool, old, cost_usd="1.5", fee="0.002",
                             filled_at=NOW - timedelta(days=20) + timedelta(hours=1))

    overview = await BotQueries(pool).demo_overview()

    assert overview["closed"] == 1 and overview["wins"] == 1
    assert overview["profit_usd"] > 0
    trade = overview["open_trades"][0]
    assert trade["position_id"] == open_id and trade["token"] == "BTC"
    assert trade["entry_price"] == D("60000")
    # по свече 66000: 0.0000333 × 66000 = 2.1978 против вложенных 2.0 → +9.89%
    assert float(trade["current_pct"]) == pytest.approx(9.89, abs=0.01)
    state_now = overview["state"]
    assert state_now.open_count == 1 and state_now.equity == state_now.cash + state_now.in_market
    text = handlers.render_demo(overview, NOW)
    assert f"BTC (#{open_id})" in text and "сейчас +9.89%" in text
    assert "до срока 6 ч" in text and "закрыто 1 · прибыльных 1" in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_command_is_routed_by_the_poller(pool) -> None:
    await set_state(pool, 1000, NOW - timedelta(days=1))
    poller = BotPoller(BotQueries(pool))
    reply = await poller._route("/demo", 1)
    assert "Демо-счёт OKX" in reply and "Баланс итого: $1,000.00" in reply
    # /positions по-прежнему отвечает своим текстом
    assert "Демо-счёт OKX" not in await poller._route("/positions", 1)


def test_the_demo_query_only_reads() -> None:
    import re
    from pathlib import Path

    src = Path(handlers.__file__).with_name("queries.py").read_text(encoding="utf-8")
    block = src[src.index("async def demo_overview"):src.index("async def positions_capital")]
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", block)
