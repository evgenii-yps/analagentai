"""Этап 9.5, редакция 2, §5: суточная сводка после снимка конца суток."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from src.demo import ledger, report
from tests.demo_support import (
    Clock,
    FakeExchange,
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


def snap(**over) -> dict:
    row = {
        "day": date(2026, 10, 6), "equity_open": D("1000"), "equity_close": D("1012.5"),
        "cash_close": D("1000.5"), "in_market_close": D("12"), "opened_count": 9,
        "closed_count": 7, "wins": 5, "losses": 2, "skipped_count": 3,
        "fees_usd": D("0.0421"), "max_in_market_usd": D("20"), "late": False,
    }
    row.update(over)
    return row


def sample(**over) -> dict:
    data = {
        "day": date(2026, 10, 6), "snap": snap(), "start_capital": D("1000"),
        "skipped": {"stale": 2, "no_capital": 1}, "problems": {},
        "slip_in": D("0.2"), "slip_out": D("0.05"), "pairs": report.PairTotals(),
    }
    data.update(over)
    return data


def test_the_report_has_every_required_figure() -> None:
    text = report.format_report(sample())
    assert "сводка за 06.10.2026" in text
    # баланс на начало и конец суток
    assert "на начало $1 000.00 → на конец $1 012.50" in text
    # изменение за сутки и с начала, $ и %
    assert "Изменение за сутки: +12.50 $ (+1.25%)" in text
    assert "с начала: +12.50 $ (+1.25%)" in text
    # сделки
    assert "открыто 9 · закрыто 7 · прибыльных 5 · убыточных 2" in text
    # пропуски по причинам
    assert "no_capital 1" in text and "stale 2" in text
    # медианы проскальзывания, комиссии
    assert "вход +0.2000% · выход +0.0500%" in text
    assert "Комиссии за сутки: $0.0421" in text
    # lost и rejected при их отсутствии не упоминаются
    assert "lost" not in text and "rejected" not in text


def test_change_since_start_uses_the_start_capital_not_the_day_open() -> None:
    text = report.format_report(sample(
        snap=snap(equity_open=D("1010"), equity_close=D("1005")), start_capital=D("1000")))
    assert "Изменение за сутки: -5.00 $ (-0.50%)" in text
    assert "с начала: +5.00 $ (+0.50%)" in text


def test_lost_rejected_and_dust_are_shown_only_when_present() -> None:
    text = report.format_report(sample(problems={"lost": 1, "rejected": 2}))
    assert "Внимание: lost 1 · rejected 2" in text and "dust" not in text


def test_an_empty_day_does_not_crash() -> None:
    text = report.format_report(sample(
        snap=snap(opened_count=0, closed_count=0, wins=0, losses=0, skipped_count=0,
                  fees_usd=D("0"), equity_close=D("1000")),
        skipped={}, slip_in=None, slip_out=None))
    assert "Пропуски покупок: нет" in text and "нет данных" in text
    assert "Изменение за сутки: +0.00 $ (+0.00%)" in text


def test_a_late_snapshot_is_marked() -> None:
    assert "снимок дописан с опозданием" in report.format_report(sample(snap=snap(late=True)))
    assert "с опозданием" not in report.format_report(sample())


def test_the_comparison_with_the_virtual_result_is_shown_when_there_are_pairs() -> None:
    pairs = [
        {"buy_cost": D("2"), "buy_fee_usd": D("0.0018"), "buy_fee_ccy": "BTC",
         "sell_cost": D("2.04"), "sell_fee_usd": D("0.002"), "sell_fee_ccy": "USDT",
         "virtual_pnl_usd": D("0.05"), "notional_usd": D("2"), "virtual_exit_reason": "target"},
        {"buy_cost": D("2"), "buy_fee_usd": D("0.0018"), "buy_fee_ccy": "BTC",
         "sell_cost": D("1.9"), "sell_fee_usd": D("0.002"), "sell_fee_ccy": "USDT",
         "virtual_pnl_usd": D("0.0"), "notional_usd": D("2"), "virtual_exit_reason": "data_gap"},
    ]
    totals = report.pair_totals(pairs)
    assert totals.pairs == 1                          # data_gap в сравнение не входит
    assert totals.demo_usd == D("0.038") and totals.virtual_usd == D("0.05")
    text = report.format_report(sample(pairs=totals))
    assert "Против виртуальной прибыли тех же позиций (1)" in text
    assert "демо +0.0380 $ · виртуально +0.0500 $ · разница -0.0120 $" in text


# --- рассылка: после снимка, один раз в сутки ----------------------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demorep")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


async def ready(pool, now: datetime, notify_ok: bool = True):
    clock = Clock(now)
    notes: list[str] = []
    ctx = build_context(pool, FakeExchange(), clock, notified=notes, mirror_since=utc(5, 9),
                        start_capital=1000)
    if not notify_ok:
        async def refuse(text: str) -> bool:
            notes.append(text)
            return False
        ctx.notify = refuse
    pid = await add_position(pool, opened_at=utc(5, 12), status="closed", closed_at=utc(5, 13),
                             net_pnl_usd=0.05)
    await insert_buy_filled(pool, pid, filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60000", cost_usd="2.0", filled_at=utc(5, 12))
    await insert_sell_filled(pool, pid, cost_usd="2.04", fee="0.002", filled_at=utc(5, 13))
    return ctx, clock, notes


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_no_report_until_the_snapshot_exists(pool) -> None:
    ctx, _, notes = await ready(pool, utc(5, 20, 59))           # 23:59 МСК — сутки не кончились
    assert await report.daily_report(ctx) is False and notes == []


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_report_follows_the_snapshot_and_goes_once(pool) -> None:
    ctx, clock, notes = await ready(pool, utc(5, 21, 2))        # 00:02 МСК 06.10
    assert await report.daily_report(ctx) is False              # снимка ещё нет
    await ledger.ensure_snapshots(ctx, ctx.now())
    assert await report.daily_report(ctx) is True
    assert len(notes) == 1 and "сводка за 05.10.2026" in notes[0]
    assert "закрыто 1 · прибыльных 1 · убыточных 0" in notes[0]
    assert ctx.redis.data["demo:report:2026-10-05"] == "sent"
    for _ in range(3):
        clock.advance(15)
        assert await report.daily_report(ctx) is False          # одна сводка в сутки
    assert len(notes) == 1
    clock.value = utc(6, 21, 3)                                 # следующая полночь по Москве
    await ledger.ensure_snapshots(ctx, ctx.now())
    assert await report.daily_report(ctx) is True
    assert len(notes) == 2 and "сводка за 06.10.2026" in notes[1]


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_failed_send_is_retried_later_not_every_iteration(pool) -> None:
    ctx, clock, notes = await ready(pool, utc(5, 21, 2), notify_ok=False)
    await ledger.ensure_snapshots(ctx, ctx.now())
    assert await report.daily_report(ctx) is False
    assert ctx.redis.data["demo:report:2026-10-05"] == "retry"
    assert ctx.redis.ttl["demo:report:2026-10-05"] == 900
    assert await report.daily_report(ctx) is False
    assert len(notes) == 1                                      # пока ключ жив, повтора нет


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_collect_reads_the_day_from_the_real_tables(pool) -> None:
    ctx, _, _ = await ready(pool, utc(5, 21, 2))
    other = await add_position(pool, opened_at=utc(5, 15))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts, created_at) VALUES "
        "($1, 1, 'buy', 'lost', NULL, 'lost1', 'BTC/USDT', 'h', $2, $2);", other, utc(5, 15))
    await ledger.ensure_snapshots(ctx, ctx.now())
    data = await report.collect(ctx, date(2026, 10, 5))
    assert data["snap"]["closed_count"] == 1 and data["problems"] == {"lost": 1}
    assert data["slip_out"] is None and data["pairs"].pairs == 1
    assert data["pairs"].demo_usd == D("0.038") and data["pairs"].virtual_usd == D("0.05")
    assert await report.collect(ctx, date(2026, 10, 9)) is None
    text = report.format_report(data)
    assert "Внимание: lost 1" in text


def test_the_removed_report_hour_setting_is_gone_from_the_code() -> None:
    import re
    from pathlib import Path

    from src.core.config import Settings

    assert "DEMO_REPORT_HOUR_UTC" not in Settings.model_fields
    for path in (Path(__file__).resolve().parent.parent / "src").rglob("*.py"):
        assert not re.search(r"\.DEMO_REPORT_HOUR_UTC\b|report_hour_utc",
                             path.read_text(encoding="utf-8")), path
