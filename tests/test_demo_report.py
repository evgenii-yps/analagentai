"""Этап 9.5, §8: суточная сводка (форматирование — без базы, сбор — на PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.demo import report, runner
from tests.demo_support import (
    SECRETS,
    Clock,
    FakeExchange,
    FakeRedis,
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
NOW = datetime(2026, 10, 6, 6, 5, tzinfo=UTC)


def sample_data(**over) -> dict:
    data = {
        "now": NOW, "mirror_since": NOW - timedelta(days=2),
        "counts": [
            {"leg": "buy", "status": "filled", "skip_reason": None, "n": 7},
            {"leg": "sell", "status": "filled", "skip_reason": None, "n": 5},
            {"leg": "buy", "status": "skipped", "skip_reason": "stale", "n": 2},
            {"leg": "buy", "status": "skipped", "skip_reason": "no_balance", "n": 1},
            {"leg": "sell", "status": "dust", "skip_reason": "below_min_size", "n": 1},
            {"leg": "buy", "status": "lost", "skip_reason": None, "n": 0},
            {"leg": "buy", "status": "rejected", "skip_reason": None, "n": 3},
        ],
        "filled": [
            {"leg": "buy", "slippage_pct": D("0.10"), "lag_sec": 30},
            {"leg": "buy", "slippage_pct": D("0.30"), "lag_sec": 40},
            {"leg": "buy", "slippage_pct": D("0.20"), "lag_sec": 50},
            {"leg": "sell", "slippage_pct": D("-0.05"), "lag_sec": 20},
            {"leg": "sell", "slippage_pct": D("0.15"), "lag_sec": 60},
        ],
        "day": report.PairTotals(pairs=4, demo_usd=D("0.0400"), demo_cost_usd=D("8"),
                                 virtual_usd=D("0.0600"), virtual_notional_usd=D("8")),
        "total": report.PairTotals(pairs=9, demo_usd=D("-0.0200"), demo_cost_usd=D("18"),
                                   virtual_usd=D("0.0900"), virtual_notional_usd=D("18")),
        "balance": D("981.5"),
    }
    data.update(over)
    return data


def test_the_report_has_every_required_figure() -> None:
    text = report.format_report(sample_data())
    assert "сводка за 24 часа" in text
    assert "Покупок исполнено: 7" in text and "продаж исполнено: 5" in text
    assert "no_balance 1" in text and "stale 2" in text
    assert "dust: 1" in text and "lost: 0" in text and "rejected: 3" in text
    # медианы: вход 0.20, выход 0.05 (из -0.05 и 0.15), задержка 40
    assert "вход +0.2000%" in text and "выход +0.0500%" in text
    assert "40 с" in text
    assert "Пары, закрытые за сутки" in text and "пар 4" in text
    assert "демо: +0.0400 $ (+0.50%)" in text
    assert "виртуально: +0.0600 $ (+0.75%)" in text
    assert "разница: -0.0200 $ (-0.25 п.п.)" in text
    assert "Нарастающим итогом с 2026-10-04 06:05 UTC" in text and "пар 9" in text
    assert "Баланс USDT на демо-счёте: 981.50" in text


def test_the_report_is_in_russian_and_uses_html_only_for_bold() -> None:
    text = report.format_report(sample_data())
    assert "Демо-исполнение OKX" in text
    assert "<b>" in text and "</b>" in text
    assert "<script" not in text


def test_an_empty_day_does_not_crash() -> None:
    text = report.format_report(sample_data(
        counts=[], filled=[], day=report.PairTotals(), total=report.PairTotals(), balance=None))
    assert "Пропуски покупок: нет" in text
    assert "нет данных" in text and "пар нет" in text and "недоступен" in text


def test_pair_totals_use_pair_pnl_and_the_virtual_pnl() -> None:
    rows = [{
        "id": 1, "base": "BTC", "net_pnl_usd": D("0.05"), "notional_usd": D("2"),
        "buy_cost": D("2"), "buy_fee": D("0.000000033"), "buy_fee_ccy": "BTC", "buy_px": D("60000"),
        "sell_cost": D("2.04"), "sell_fee": D("0.002"), "sell_fee_ccy": "USDT",
        "sell_px": D("60600"),
    }]
    total = report.pair_totals(rows)
    assert total.pairs == 1
    assert total.demo_usd == D("0.038")            # 2.04 − 2 − 0.002
    assert total.demo_pct == D("1.9")
    assert total.virtual_usd == D("0.05") and total.virtual_pct == D("2.5")


def test_a_pair_with_an_unconvertible_fee_is_left_out_not_guessed() -> None:
    rows = [{
        "id": 1, "base": "BTC", "net_pnl_usd": D("0.05"), "notional_usd": D("2"),
        "buy_cost": D("2"), "buy_fee": D("1"), "buy_fee_ccy": "BTC", "buy_px": D("60000"),
        "sell_cost": D("2.04"), "sell_fee": D("1"), "sell_fee_ccy": "OKB", "sell_px": D("1"),
    }]
    assert report.pair_totals(rows).pairs == 0


# --- рассылка: час, один раз в сутки, повтор при сбое ----------------------------


class _Ctx:
    def __init__(self, hour: int, notify_ok: bool = True, day: int = 6) -> None:
        self.sent: list[str] = []
        self.redis = FakeRedis()
        self.clock = Clock(datetime(2026, 10, day, hour, 0, tzinfo=UTC))
        self.notify_ok = notify_ok
        self.pool = None
        self.mirror_since = NOW - timedelta(days=1)
        self.exchange = FakeExchange()
        self.secrets = SECRETS
        self.config = runner.DemoConfig(host="www.okx.com", report_hour_utc=6)

    async def notify(self, text: str) -> bool:
        self.sent.append(text)
        return self.notify_ok


async def _daily(monkeypatch, ctx: _Ctx) -> bool:
    async def fake_collect(_c, now):
        return sample_data(now=now)

    monkeypatch.setattr(report, "collect", fake_collect)
    shell = type("C", (), {})()
    shell.now, shell.redis, shell.config = ctx.clock.now, ctx.redis, ctx.config
    shell.notify, shell.secrets = ctx.notify, SECRETS
    return await report.daily_report(shell)


async def test_the_report_is_not_sent_before_the_hour(monkeypatch) -> None:
    ctx = _Ctx(hour=5)
    assert await _daily(monkeypatch, ctx) is False and ctx.sent == []


async def test_the_report_goes_once_per_day(monkeypatch) -> None:
    ctx = _Ctx(hour=6)
    assert await _daily(monkeypatch, ctx) is True
    assert await _daily(monkeypatch, ctx) is False
    ctx.clock.advance(3600 * 3)
    assert await _daily(monkeypatch, ctx) is False
    assert len(ctx.sent) == 1
    assert ctx.redis.data["demo:report:2026-10-06"] == "sent"
    ctx.clock.advance(3600 * 24)                     # следующие сутки
    assert await _daily(monkeypatch, ctx) is True
    assert len(ctx.sent) == 2 and "demo:report:2026-10-07" in ctx.redis.data


async def test_a_failed_send_is_retried_later_not_every_iteration(monkeypatch) -> None:
    ctx = _Ctx(hour=6, notify_ok=False)
    assert await _daily(monkeypatch, ctx) is False
    assert ctx.redis.data["demo:report:2026-10-06"] == "retry"
    assert ctx.redis.ttl["demo:report:2026-10-06"] == 900
    assert await _daily(monkeypatch, ctx) is False
    assert len(ctx.sent) == 1                        # пока ключ жив, повтора нет


# --- сбор из базы -----------------------------------------------------------------

db_ok = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")


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


@db_ok
async def test_collect_reads_pairs_and_counts_from_the_real_tables(pool) -> None:
    clock = Clock()
    ex = FakeExchange()
    ctx = build_context(pool, ex, clock, mirror_since=clock.now() - timedelta(hours=3))
    # пара: купили на 2.0, продали на 2.04, комиссия продажи 0.002 → +0.038 $;
    # виртуально позиция заработала 0.05
    pid = await add_position(
        pool, opened_at=clock.now() - timedelta(hours=2), status="closed",
        closed_at=clock.now() - timedelta(hours=1), net_pnl_usd=0.05)
    await insert_buy_filled(pool, pid, cost_usd="2.0", fee="0.00000003", fee_ccy="BTC")
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, symbol, "
        "host, virtual_ts, filled_qty, avg_price, cost_usd, fee, fee_ccy, slippage_pct, "
        "filled_at, lag_sec) SELECT $1, instrument_id, 'sell', 'filled', 'at95s1', 'BTC/USDT', "
        "'h', now(), 0.00003, 60600, 2.04, 0.002, 'USDT', 0.5, now(), 12 "
        "FROM demo_orders WHERE position_id = $1;", pid)
    # пара по позиции data_gap в сравнение не входит
    gap = await add_position(
        pool, opened_at=clock.now() - timedelta(hours=2), status="closed",
        closed_at=clock.now() - timedelta(hours=1), exit_reason="data_gap")
    await insert_buy_filled(pool, gap, cost_usd="2.0")
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, symbol, "
        "host, virtual_ts, filled_qty, avg_price, cost_usd, fee, fee_ccy, filled_at) "
        "SELECT $1, instrument_id, 'sell', 'filled', 'at95s2', 'BTC/USDT', 'h', now(), "
        "0.00003, 60600, 2.04, 0.002, 'USDT', now() FROM demo_orders WHERE position_id = $1;",
        gap)

    data = await report.collect(ctx, clock.now())

    assert data["day"].pairs == 1 and data["total"].pairs == 1
    assert data["day"].demo_usd == D("0.038000")
    assert data["day"].virtual_usd == D("0.050000")
    assert data["balance"] == D("1000")
    text = report.format_report(data)
    assert "Покупок исполнено: 2" in text and "продаж исполнено: 2" in text
    assert "Баланс USDT на демо-счёте: 1000.00" in text
    assert any(f["lag_sec"] == 12 for f in data["filled"])


@db_ok
async def test_collect_survives_a_dead_balance_request(pool) -> None:
    ctx = build_context(pool, FakeExchange(fail_balance=RuntimeError("нет сети")), Clock())
    data = await report.collect(ctx, datetime.now(UTC))
    assert data["balance"] is None
    assert "недоступен" in report.format_report(data)
