"""Этап 9.5, редакция 2, §4: сообщения об открытии и закрытии демо-сделок."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.demo import messages, runner
from tests.demo_support import (
    Clock,
    FakeExchange,
    add_position,
    build_context,
    build_database,
    drop_database,
    insert_buy_filled,
    open_pool,
    reset_data,
    server_up,
)

D = Decimal
MSK = ZoneInfo("Europe/Moscow")

# --- чистые тексты ----------------------------------------------------------------


def opened(**over) -> str:
    args = dict(
        position_id=41, symbol="BTC/USDT", avg_price=D("60060"), signal_price=D("60000"),
        slippage_pct=D("0.10"), cost_usd=D("2"), qty=D("0.00003332"), fee_usd=D("0.0018"),
        probability=0.62, target_price=D("60720"), target_pct=D("1.1"),
        deadline_at=datetime(2026, 10, 7, 9, 30, tzinfo=UTC), cash=D("998"),
        equity=D("1000.01"), tz=MSK,
    )
    args.update(over)
    return messages.opened_text(**args)


def test_the_opened_text_has_every_required_field() -> None:
    text = opened()
    assert "🛒 <b>Куплено · BTC</b> #41 <i>(демо)</i>" in text
    assert "Цена 60 060,00 · сигнал 60 000,00 · проскальзывание +0,10%" in text
    assert "Сумма $2,00 · 0,00003332 BTC · комиссия $0,0018" in text
    assert "🎯 Цель 60 720,00 (+1,10%) · ⏳ до 07.10 12:30 МСК" in text   # время по NOTIFY_TIMEZONE
    assert "Согласие агентов 62%" in text                                  # Д10
    assert "Вероятность сигнала" not in text and "по истории" not in text
    assert "💰 Баланс $1 000,01 · свободно $998,00 · в рынке $2,01" in text


def test_the_calibrated_probability_is_a_separate_line_only_when_known() -> None:
    """Д10: «согласие агентов» — индекс; вероятность по истории — только если она посчитана."""
    assert "Вероятность успеха по истории 58%" in opened(calibrated_probability=0.58)
    assert "по истории" not in opened(calibrated_probability=None)
    assert "Согласие агентов 62%" in opened(calibrated_probability=0.58)


def test_the_opened_text_survives_missing_numbers() -> None:
    text = opened(signal_price=None, slippage_pct=None, fee_usd=None, probability=None,
                  cash=None, equity=None)
    assert "сигнал —" in text and "Согласие агентов —" in text
    assert "комиссия —" in text and "💰 Баланс: нет данных" in text


def closed(**over) -> str:
    args = dict(
        position_id=41, symbol="BTC/USDT", exit_reason="target", hold_hours=48,
        entry_price=D("60060"), exit_price=D("60800"), held_sec=3 * 3600 + 41 * 60,
        profit_usd=D("0.0234"), profit_pct=D("1.17"), fees_usd=D("0.0038"),
        equity=D("1010"), start_capital=D("1000"),
    )
    args.update(over)
    return messages.closed_text(**args)


def test_the_closed_text_has_every_required_field() -> None:
    text = closed()
    assert "✅ <b>Продано в плюс · BTC</b> #41 <i>(демо)</i>" in text
    assert "Причина: цель достигнута" in text
    assert "60 060,00 → 60 800,00 · в сделке 3 ч 41 мин" in text
    assert "Результат 🟢 +$0,0234 (+1,17%) · комиссии $0,0038" in text
    # баланс и «с начала» — от стартового капитала
    assert "💰 Баланс $1 010,00 · с начала 🟢 +$10,00 (+1,00%)" in text


def test_the_reason_uses_the_same_translation_as_the_positions_messages() -> None:
    from src.positions.messages import EXIT_RU, exit_ru

    for reason in EXIT_RU:
        assert exit_ru(reason) in closed(exit_reason=reason, hold_hours=None)
    assert "истёк срок 48 ч" in closed(exit_reason="timeout", hold_hours=48)
    assert "вышли в плюс после суток" in closed(exit_reason="plus_exit")


def test_a_loss_is_shown_with_a_minus() -> None:
    text = closed(profit_usd=D("-0.05"), profit_pct=D("-2.5"), equity=D("999"))
    assert "Результат 🔴 −$0,0500 (−2,50%)" in text
    assert "с начала 🔴 −$1,00 (−0,10%)" in text


def test_the_closing_mark_follows_the_result() -> None:
    """Д9: значок и слово — по результату; ноль считается «в минус» (✅ — только прибыль > 0)."""
    plus = closed(profit_usd=D("0.01"))
    zero = closed(profit_usd=D("0"), profit_pct=D("0"))
    minus = closed(profit_usd=D("-0.01"), profit_pct=D("-0.5"))
    assert plus.startswith("✅ <b>Продано в плюс")
    assert zero.startswith("❌ <b>Продано в минус") and "Результат ⚪" in zero
    assert minus.startswith("❌ <b>Продано в минус")
    assert "🔴 <b>ПРОДАНО" not in plus


def test_the_account_button_opens_the_account_in_a_new_message() -> None:
    from src.bot import nav

    button = messages.ACCOUNT_BUTTON["inline_keyboard"][0][0]
    assert button["text"] == "💰 Счёт" and button["callback_data"] == "v1:accn"
    assert nav.parse_nav(button["callback_data"]).screen == nav.ACCOUNT_NEW


# --- с базой -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demomsg")
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


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_bought_position_sends_the_opening_message_with_real_data(pool, clock) -> None:
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes)
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await pool.execute("UPDATE signals SET probability = 0.71;")

    await runner.run_once(ctx)

    assert len(notes) == 1
    text = notes[0]
    assert "Куплено · BTC" in text and f"#{pid}" in text and "<i>(демо)</i>" in text
    assert "Цена 60 060,00" in text                       # цена исполнения (60000 · 1.001)
    assert "проскальзывание +0,10%" in text
    assert "Сумма $2,00" in text and "BTC" in text and "комиссия $" in text
    assert "Согласие агентов 71%" in text                 # Д10: индекс согласия, не вероятность
    assert "🎯 Цель " in text and "⏳ до " in text and "МСК" in text
    assert "свободно $998,00" in text and "💰 Баланс $" in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_sold_position_sends_the_closing_message(pool, clock) -> None:
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes)
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(hours=3), status="closed",
        closed_at=clock.now() - timedelta(seconds=30), exit_price=60600.0, exit_reason="target")
    await insert_buy_filled(pool, pid, filled_qty="0.00003332", fee="0.00000003",
                            avg_price="60060", cost_usd="2.0",
                            filled_at=clock.now() - timedelta(hours=2, minutes=41))

    await runner.run_once(ctx)

    assert len(notes) == 1
    text = notes[0]
    # продажа на 0.1% ниже тикера 60000 → пара в минус → ❌ (Д9: значок по результату)
    assert text.startswith("❌ <b>Продано в минус · BTC</b>") and f"#{pid}" in text
    assert "Причина: цель достигнута" in text
    assert "60 060,00 → 59 940,00 · в сделке 2 ч 41 мин" in text
    assert "Результат " in text and "комиссии $" in text
    assert "💰 Баланс $" in text and "с начала " in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_no_trade_messages_when_notifications_are_off(pool, clock) -> None:
    notes: list[str] = []
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock, notified=notes, notify_enabled=False)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    done = await add_position(
        pool, opened_at=clock.now() - timedelta(hours=3), status="closed",
        closed_at=clock.now() - timedelta(seconds=30))
    await insert_buy_filled(pool, done)

    await runner.run_once(ctx)

    assert len(ex.sent("buy")) == 1 and len(ex.sent("sell")) == 1   # сделки идут как обычно
    assert notes == []                                             # а сообщений нет


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_skipped_position_produces_no_message(pool, clock) -> None:
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes)
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=300))        # stale
    await add_position(pool, symbol="FOO/USDT", opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    skipped = await pool.fetchval("SELECT count(*) FROM demo_orders WHERE status='skipped';")
    assert skipped == 2 and notes == []


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_failing_telegram_never_blocks_a_trade(pool, clock) -> None:
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock)

    async def boom(text: str) -> bool:
        raise RuntimeError("Telegram недоступен")

    ctx.notify = boom
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    await runner.run_once(ctx)
    assert (await pool.fetchval("SELECT status FROM demo_orders WHERE position_id=$1;", pid)
            ) == "filled"


# --- предохранитель потока ------------------------------------------------------------


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_messages_over_the_hourly_cap_are_held_and_rolled_up(pool, clock) -> None:
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes, trades_max_per_hour=2)
    for sec in (40, 30, 20, 10):
        await add_position(pool, opened_at=clock.now() - timedelta(seconds=sec))

    await runner.run_once(ctx)

    assert len(notes) == 2                                 # потолок: два сообщения в час
    assert len(ctx.redis.lists[messages.HELD_KEY]) == 2    # остальные придержаны, не потеряны
    assert await messages.flush_rollup(ctx) is True        # час отсчитывается от последней сводки
    assert len(notes) == 3
    assert "Придержано сообщений о демо-сделках: 2" in notes[2]
    assert notes[2].count("Куплено") == 2
    assert messages.HELD_KEY not in ctx.redis.lists
    # сводка в счёт потолка не входит
    assert await ctx.redis.zcard(messages.SENT_KEY) == 2
    # новая порция в тот же час сводкой не уходит
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=5))
    await runner.run_once(ctx)
    assert await messages.flush_rollup(ctx) is False
    clock.advance(3601)
    ctx.redis.zsets.clear()                                # окно потолка прошло
    assert await messages.flush_rollup(ctx) is True


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_zero_cap_means_no_ceiling_and_no_rollup(pool, clock) -> None:
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes, trades_max_per_hour=0)
    for sec in (40, 30, 20):
        await add_position(pool, opened_at=clock.now() - timedelta(seconds=sec))
    await runner.run_once(ctx)
    assert len(notes) == 3 and messages.HELD_KEY not in ctx.redis.lists
    assert await messages.flush_rollup(ctx) is False
    assert messages.SENT_KEY not in ctx.redis.zsets      # при нуле Redis о потолке не спрашивается
