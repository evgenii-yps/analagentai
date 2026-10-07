"""Этап 9.5, редакция 2, §7: команда бота /demo (только чтение)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.bot import handlers, screens
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


MSK = ZoneInfo("Europe/Moscow")


def test_demo_is_a_known_command_and_listed_in_help() -> None:
    # Этап 9.7: /demo открывает экран «Счёт»; /positions остаётся известной командой, но
    # в справке и в меню команд её больше нет.
    assert "demo" in handlers.KNOWN_COMMANDS and "positions" in handlers.KNOWN_COMMANDS
    help_text, _ = screens.help_screen(flags=screens.Flags(demo_enabled=True), coins=["BTC"])
    assert "/demo" in help_text and "/positions" not in help_text
    assert handlers.parse_command("/demo")[0] == "demo"


def test_the_account_screen_with_open_trades() -> None:
    demo = {"state": state(), "mirror_since": NOW - timedelta(days=1)}
    week = {"closed": 12, "wins": 8, "losses": 4, "profit_usd": D("0.1234"),
            "fees_usd": D("0.05")}
    text, _ = screens.account_screen(
        demo=demo, yesterday_close=D("1000"), week=week, now=NOW, tz=MSK)
    assert "Демо-счёт OKX" in text and "Деньги не настоящие" in text
    assert "Итого: <b>$1 002,50</b>" in text
    assert "Свободно: $990,00" in text and "В рынке: $12,50" in text
    assert "С начала: 🟢 +$2,50 (+0,25%)" in text
    assert "За сегодня: 🟢 +$2,50 (+0,25%)" in text
    assert "Закрыто 12 · ✅ 8 · ❌ 4 · в плюс 67% из 12" in text
    assert "Прибыль: 🟢 +$0,1234 · комиссии $0,0500" in text


def test_the_trades_screen_with_open_trades_an_expired_one_and_none() -> None:
    text, _ = screens.trades_screen(open_trades=data()["open_trades"], recent_closed=[],
                                    now=NOW, tz=MSK)
    assert "<b>Открыто: 1</b>" in text
    assert "<b>BTC</b> #41 · +0,35%" in text and "вход 60 060,00" in text
    assert "осталось 5 ч 12 мин" in text
    empty, _ = screens.trades_screen(open_trades=[], recent_closed=[], now=NOW, tz=MSK)
    assert "Открытых сделок нет" in empty
    late, _ = screens.trades_screen(open_trades=[{
        "position_id": 1, "token": "ETH", "entry_price": D("2500"), "current_pct": None,
        "deadline_at": NOW - timedelta(minutes=1)}], recent_closed=[], now=NOW, tz=MSK)
    assert "срок вышел" in late and "сейчас —" in late


def test_the_screens_before_the_service_has_ever_started() -> None:
    account, _ = screens.account_screen(demo=None, yesterday_close=None, week=None,
                                        now=NOW, tz=MSK)
    trades, _ = screens.trades_screen(open_trades=None, recent_closed=[], now=NOW, tz=MSK)
    assert "Демо-счёт ещё не запущен" in account and "Демо-счёт ещё не запущен" in trades


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
    assert await BotQueries(pool).demo_state() is None


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_overview_with_the_state_but_no_trades(pool) -> None:
    await set_state(pool, 500, NOW - timedelta(days=1))
    overview = await BotQueries(pool).demo_overview()
    assert overview["open_trades"] == [] and overview["closed"] == 0
    assert overview["state"].equity == D("500") and overview["state"].cash == D("500")
    text, _ = screens.trades_screen(open_trades=overview["open_trades"], recent_closed=[],
                                    now=NOW, tz=MSK)
    assert overview["state"].equity == D("500") and "Открытых сделок нет" in text


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
    text, _ = screens.trades_screen(open_trades=overview["open_trades"], recent_closed=[],
                                    now=NOW, tz=MSK)
    assert f"<b>BTC</b> #{open_id} · +9,89%" in text and "осталось 6 ч" in text
    assert trade["current_price"] == D("66000") and trade["target_price"] is not None


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_command_is_routed_by_the_poller(pool, monkeypatch) -> None:
    from src.core.config import settings

    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    await set_state(pool, 1000, NOW - timedelta(days=1))
    poller = BotPoller(BotQueries(pool))
    text, markup = await poller._answer_command("/demo", 1)
    assert "Демо-счёт OKX" in text and "Итого: <b>$1 000,00</b>" in text
    assert markup["inline_keyboard"]
    # /positions по-прежнему отвечает своим текстом
    assert "Демо-счёт OKX" not in str(await poller._answer_command("/positions", 1))


def test_the_demo_query_only_reads() -> None:
    import re
    from pathlib import Path

    src = Path(handlers.__file__).with_name("queries.py").read_text(encoding="utf-8")
    block = src[src.index("async def demo_state"):src.index("async def positions_capital")]
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", block)
