"""Этап 9.6: манименеджмент демо-покупок (ТЗ 9.6 редакция 1, §10).

Чистая функция ``mm_order_cost`` проверяется таблицей; ``fetch_token_exposure`` и
``run_buys`` — на настоящей PostgreSQL (схема из ``db/init.sql`` и всех миграций), биржа —
заглушка. Нет PostgreSQL — тесты с базой пропускаются (как в тестах 9.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.bot import screens
from src.bot.poller import BotPoller
from src.bot.queries import BotQueries
from src.core.config import Settings
from src.demo import ledger, messages, report, rules, runner
from tests.demo_support import (
    ROOT,
    Clock,
    FakeExchange,
    FakeRedis,
    _market,
    add_candle,
    add_position,
    build_context,
    build_database,
    capture_demo_logs,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    psql_raw,
    reset_data,
    server_up,
    set_state,
)

D = Decimal
needs_pg = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")

MM = {
    "step_pct": D(5), "token_cap_pct": D(15), "reserve_pct": D(25), "min_order_usd": D(5),
}
MIGRATION = ROOT / "db" / "migrations" / "031_demo_sizing.sql"
ROLLBACK = ROOT / "db" / "migrations" / "031_demo_sizing_rollback.sql"


def make_settings(**kw) -> Settings:
    return Settings(POSTGRES_PASSWORD="x", _env_file=None, DEMO_INTERVAL=1, **kw)


# --------------------------------------------------------------------------
# §4. mm_order_cost — таблица случаев
# --------------------------------------------------------------------------

CASES = [
    # equity, free_cash, exposure, ожидаемая сумма, ожидаемая причина
    (1, "1000", "1000", "0", "50.00", None),
    (2, "1000", "1000", "100", "50.00", None),                  # 3-я ступень
    (3, "1000", "1000", "120", "30.00", None),                  # добор до потолка
    (4, "1000", "1000", "146", "0", rules.SKIP_TOKEN_CAP),      # 4 < 5
    (5, "1000", "1000", "150", "0", rules.SKIP_TOKEN_CAP),
    (6, "1000", "280", "0", "30.00", None),                     # резерв 250
    (7, "1000", "252", "0", "0", rules.SKIP_RESERVE),
    (8, "1000", "250", "150", "0", rules.SKIP_RESERVE),         # оба нулевые → reserve
    (9, "0", "0", "0", "0", rules.SKIP_RESERVE),
    (10, "999.999", "999.999", "0", "49.99", None),             # округление вниз
    (11, "1200", "1200", "0", "60.00", None),                   # растёт с балансом
]


@pytest.mark.parametrize(("n", "equity", "cash", "held", "cost", "why"), CASES)
def test_mm_order_cost_table(n, equity, cash, held, cost, why) -> None:
    got_cost, got_why = rules.mm_order_cost(D(equity), D(cash), D(held), **MM)
    assert (got_cost, got_why) == (D(cost), why), n


def test_mm_order_cost_edges() -> None:
    # ровно минимальная сумма — покупка делается; на цент меньше — нет
    assert rules.mm_order_cost(D(100), D(1000), D(0), **{**MM, "min_order_usd": D(5)}) \
        == (D("5.00"), None)
    # шаг (4,9995 → 4,99) меньше минимума: причина по п. 7 §4 — меньшее из «резерв / потолок»
    assert rules.mm_order_cost(D("99.99"), D(1000), D(0), **MM) == (D(0), rules.SKIP_TOKEN_CAP)
    # отрицательные остатки не дают отрицательной покупки
    assert rules.mm_order_cost(D(1000), D(100), D(0), **MM) == (D(0), rules.SKIP_RESERVE)
    assert rules.mm_order_cost(D(1000), D(1000), D(200), **MM) == (D(0), rules.SKIP_TOKEN_CAP)
    # потолок не выше 100 % баланса и шаг = потолок: одна покупка до потолка
    one = {**MM, "step_pct": D(15)}
    assert rules.mm_order_cost(D(1000), D(1000), D(0), **one) == (D("150.00"), None)
    # результат — Decimal, шаг цента
    cost, _ = rules.mm_order_cost(D("1000.123456"), D(1000), D(0), **MM)
    assert isinstance(cost, D) and cost == D("50.00")


def test_order_cost_usd_is_kept_for_the_fixed_mode() -> None:
    assert rules.order_cost_usd({"notional_usd": 2}) == D("2")


def test_the_new_skip_reasons_have_human_text() -> None:
    assert messages.DEMO_SKIP_RU[rules.SKIP_TOKEN_CAP] == "монета на потолке"
    assert messages.DEMO_SKIP_RU[rules.SKIP_RESERVE] == "сработал резерв"


def test_the_daily_report_shows_the_new_skip_reasons_without_changes() -> None:
    data = {
        "day": date(2026, 10, 6),
        "snap": {"equity_open": D("1000"), "equity_close": D("1000"), "cash_close": D("900"),
                 "in_market_close": D("100"), "opened_count": 3, "closed_count": 0,
                 "wins": 0, "losses": 0, "fees_usd": D("0"), "late": False},
        "start_capital": D("1000"),
        "skipped": {rules.SKIP_TOKEN_CAP: 4, rules.SKIP_RESERVE: 2}, "problems": {},
        "slip_in": None, "slip_out": None, "pairs": report.PairTotals(),
    }
    text = report.format_report(data)
    assert "монета на потолке 4" in text and "сработал резерв 2" in text
    assert "token_cap" not in text


# --------------------------------------------------------------------------
# §3. Настройки
# --------------------------------------------------------------------------


def test_settings_defaults_match_the_owner_decisions() -> None:
    cfg = make_settings()
    assert cfg.DEMO_MM_ENABLED is False                      # выключен по умолчанию
    assert (cfg.DEMO_MM_RESERVE_PCT, cfg.DEMO_MM_STEP_PCT) == (25.0, 5.0)
    assert (cfg.DEMO_MM_TOKEN_CAP_PCT, cfg.DEMO_MM_MIN_ORDER_USD) == (15.0, 5.0)
    assert cfg.demo_config_errors() == []


def test_env_example_carries_the_five_lines_with_the_default_values() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    found = {}
    for line in lines:
        if line.startswith("DEMO_MM_"):
            key, _, rest = line.partition("=")
            found[key] = rest.split("#")[0].strip()
    assert found == {
        "DEMO_MM_ENABLED": "false", "DEMO_MM_RESERVE_PCT": "25", "DEMO_MM_STEP_PCT": "5",
        "DEMO_MM_TOKEN_CAP_PCT": "15", "DEMO_MM_MIN_ORDER_USD": "5",
    }


@pytest.mark.parametrize(("over", "name"), [
    ({"DEMO_MM_STEP_PCT": 0}, "DEMO_MM_STEP_PCT"),
    ({"DEMO_MM_STEP_PCT": 20, "DEMO_MM_TOKEN_CAP_PCT": 10}, "DEMO_MM_STEP_PCT"),
    ({"DEMO_MM_TOKEN_CAP_PCT": 101}, "DEMO_MM_TOKEN_CAP_PCT"),
    ({"DEMO_MM_RESERVE_PCT": 100}, "DEMO_MM_RESERVE_PCT"),
    ({"DEMO_MM_RESERVE_PCT": -1}, "DEMO_MM_RESERVE_PCT"),
    ({"DEMO_MM_MIN_ORDER_USD": 0}, "DEMO_MM_MIN_ORDER_USD"),
])
def test_wrong_mm_settings_are_reported_only_when_the_rule_is_on(over, name) -> None:
    errors = make_settings(DEMO_MM_ENABLED=True, **over).demo_config_errors()
    assert len(errors) == 1 and name in errors[0]
    assert make_settings(DEMO_MM_ENABLED=False, **over).demo_config_errors() == []


def test_the_mm_boundaries_are_accepted() -> None:
    ok = make_settings(DEMO_MM_ENABLED=True, DEMO_MM_STEP_PCT=15, DEMO_MM_TOKEN_CAP_PCT=15,
                       DEMO_MM_RESERVE_PCT=0, DEMO_MM_MIN_ORDER_USD=0.01)
    assert ok.demo_config_errors() == []
    full = make_settings(DEMO_MM_ENABLED=True, DEMO_MM_STEP_PCT=100,
                         DEMO_MM_TOKEN_CAP_PCT=100)
    assert full.demo_config_errors() == []


def test_demo_config_takes_the_mm_values_as_decimals() -> None:
    cfg = runner.DemoConfig.from_settings(make_settings(
        DEMO_MM_ENABLED=True, DEMO_MM_RESERVE_PCT=12.5, DEMO_MM_STEP_PCT=2.5,
        DEMO_MM_TOKEN_CAP_PCT=10, DEMO_MM_MIN_ORDER_USD=7))
    assert cfg.mm_enabled is True
    assert cfg.mm_reserve_pct == D("12.5") and cfg.mm_step_pct == D("2.5")
    assert cfg.mm_token_cap_pct == D("10.0") and cfg.mm_min_order_usd == D("7.0")
    assert all(isinstance(v, D) for v in (
        cfg.mm_reserve_pct, cfg.mm_step_pct, cfg.mm_token_cap_pct, cfg.mm_min_order_usd))
    assert runner.DemoConfig.from_settings(make_settings()).mm_enabled is False


def test_overcommit_is_only_a_warning_condition() -> None:
    on = runner.DemoConfig(host="h", mm_enabled=True)
    assert not runner.mm_overcommitted(on, 5)                # 25 + 15 × 5 = 100
    assert runner.mm_overcommitted(on, 6)                    # 115 > 100
    assert not runner.mm_overcommitted(runner.DemoConfig(host="h"), 50)   # правило выключено


# --------------------------------------------------------------------------
# БД: общие фикстуры
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("mm96")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    await p.execute(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) VALUES "
        "('okx', 'ETH/USDT', 'ETH', 'USDT', 'spot'), "
        "('okx', 'SOL/USDT', 'SOL', 'USDT', 'spot'), "
        "('okx', 'XRP/USDT', 'XRP', 'USDT', 'spot');"
    )
    yield p
    await p.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]
PRICES = {"BTC/USDT": 60000.0, "ETH/USDT": 2500.0, "SOL/USDT": 150.0, "XRP/USDT": 2.0,
          "DOGE/USDT": 0.1, "FOO/USDT": 1.0}


class CoinsExchange(FakeExchange):
    """Биржа-заглушка с пятью спотовыми монетами системы."""

    def known_markets(self):
        out = super().known_markets()
        for symbol, lot, min_sz in (("ETH/USDT", "0.000001", "0.0001"),
                                    ("SOL/USDT", "0.0001", "0.01"),
                                    ("XRP/USDT", "0.000001", "1")):
            out[symbol] = _market(symbol, lot, min_sz)
        return out


def mm_context(pool, clock, ex=None, **config):
    config.setdefault("mm_enabled", True)
    return build_context(pool, ex or CoinsExchange(prices=dict(PRICES)), clock,
                         symbols=list(COINS), **config)


@contextlib.asynccontextmanager
async def many_open_per_coin(pool):
    """Снимает «одна открытая позиция на инструмент» на время теста.

    В жизни у монеты одна открытая позиция, но купленное и не проданное копится, пока
    продажи не исполнены; тесту нужна лестница в одной итерации, поэтому индекс снимается.
    Возвращается он после очистки данных теста.
    """
    index = "ux_positions_one_open_per_instrument"
    definition = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = $1;",
                                     index)
    assert definition
    await pool.execute(f"DROP INDEX {index};")
    try:
        yield
    finally:
        await reset_data(pool)
        await pool.execute(definition)


async def buys(ctx) -> runner.IterStats:
    """Только покупки: рынки загружены, остальные шаги итерации не нужны."""
    stats = runner.IterStats()
    ctx.usdt_free = None
    assert await runner.ensure_markets(ctx)
    await runner.run_buys(ctx, stats)
    return stats


async def candidates(pool, clock, symbols: list[str]) -> list[int]:
    """Открытые позиции-кандидаты в порядке списка, с шагом в секунду."""
    out = []
    for i, symbol in enumerate(symbols):
        out.append(await add_position(
            pool, symbol=symbol, entry_price=PRICES[symbol],
            opened_at=clock.now() - timedelta(seconds=60 - i)))
    return out


async def order_of(pool, position_id: int) -> dict:
    return dict(await pool.fetchrow(
        "SELECT * FROM demo_orders WHERE position_id = $1 AND leg = 'buy';", position_id))


class SpyPool:
    """Пул-обёртка: записывает текст каждого запроса к базе."""

    def __init__(self, pool) -> None:
        self._pool = pool
        self.sql: list[str] = []

    def __getattr__(self, name):
        target = getattr(self._pool, name)
        if name not in ("fetch", "fetchrow", "fetchval", "execute"):
            return target

        async def call(query, *args, **kwargs):
            self.sql.append(str(query))
            return await target(query, *args, **kwargs)

        return call


# --------------------------------------------------------------------------
# §5. fetch_token_exposure на настоящей PostgreSQL
# --------------------------------------------------------------------------


async def raw_order(pool, position_id: int, leg: str, status: str, *, symbol="BTC/USDT",
                    requested=None, cost=None, skip_reason=None) -> None:
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol = $1;", symbol)
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, symbol, "
        "host, requested_cost_usd, cost_usd, skip_reason, virtual_ts) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'www.okx.com', $7, $8, $9, now());",
        position_id, inst["id"], leg, status, f"at95{leg[0]}{position_id}", symbol,
        requested, cost, skip_reason,
    )


@needs_pg
async def test_exposure_counts_held_legs_and_unknown_outcomes_only(pool, clock) -> None:
    now = clock.now()
    held = await add_position(pool, opened_at=now - timedelta(hours=3))
    await insert_buy_filled(pool, held, cost_usd="50", avg_price="60060", filled_qty="0.0008")
    sold = await add_position(pool, status="closed", opened_at=now - timedelta(hours=9),
                              closed_at=now - timedelta(hours=8))
    await insert_buy_filled(pool, sold, cost_usd="50")
    await insert_sell_filled(pool, sold, cost_usd="51")
    sell_sent = await add_position(pool, symbol="ETH/USDT", status="closed",
                                   opened_at=now - timedelta(hours=7),
                                   closed_at=now - timedelta(hours=6), entry_price=2500.0)
    await insert_buy_filled(pool, sell_sent, symbol="ETH/USDT", cost_usd="40",
                            avg_price="2500", fee_ccy="ETH", fee="0.00001")
    await raw_order(pool, sell_sent, "sell", "sent", symbol="ETH/USDT", cost=None)
    pending = await add_position(pool, symbol="SOL/USDT", opened_at=now - timedelta(minutes=2))
    await raw_order(pool, pending, "buy", "pending", symbol="SOL/USDT", requested="30")
    sent = await add_position(pool, symbol="XRP/USDT", opened_at=now - timedelta(minutes=3))
    await raw_order(pool, sent, "buy", "sent", symbol="XRP/USDT", requested="20")
    rejected = await add_position(pool, symbol="DOGE/USDT", opened_at=now - timedelta(minutes=4))
    await raw_order(pool, rejected, "buy", "rejected", symbol="DOGE/USDT", requested="25")
    skipped = await add_position(pool, symbol="DOGE/USDT", status="closed",
                                 opened_at=now - timedelta(minutes=9),
                                 closed_at=now - timedelta(minutes=8))
    await raw_order(pool, skipped, "buy", "skipped", symbol="DOGE/USDT", requested=None,
                    skip_reason=rules.SKIP_TOKEN_CAP)
    lost = await add_position(pool, symbol="DOGE/USDT", status="closed",
                              opened_at=now - timedelta(minutes=19),
                              closed_at=now - timedelta(minutes=18))
    await raw_order(pool, lost, "buy", "lost", symbol="DOGE/USDT", requested="15")

    got = await ledger.fetch_token_exposure(pool)

    by_symbol = {
        await pool.fetchval("SELECT symbol FROM instruments WHERE id = $1;", k): v
        for k, v in got.items()
    }
    assert by_symbol == {
        "BTC/USDT": D("50"),            # проданная пара не учитывается
        "ETH/USDT": D("40"),            # продажа не исполнена — нога на руках
        "SOL/USDT": D("30"),            # pending — по requested_cost_usd
        "XRP/USDT": D("20"),            # sent — по requested_cost_usd
    }, by_symbol                        # rejected / skipped / lost денег не держат


@needs_pg
async def test_exposure_is_the_invested_money_not_the_current_value(pool, clock) -> None:
    """Потолок считается по цене покупки: цена упала вдвое — вложено по-прежнему $50."""
    now = clock.now()
    for i in range(2):
        pid = await add_position(pool, opened_at=now - timedelta(hours=2 + i), entry_price=60000.0)
        await insert_buy_filled(pool, pid, cost_usd="50", avg_price="60060",
                                filled_qty="0.00083250")
    await add_candle(pool, "BTC/USDT", 30000.0, now - timedelta(minutes=5))   # цена вдвое ниже

    got = await ledger.fetch_token_exposure(pool)
    btc = await pool.fetchval("SELECT id FROM instruments WHERE symbol = 'BTC/USDT';")
    legs = await ledger.value_legs(pool, await ledger.fetch_held_legs(pool))

    assert got == {btc: D("100")}
    assert sum(leg["value"] for leg in legs) < D("50")        # а стоимость — другая


@needs_pg
async def test_exposure_of_an_empty_book_is_empty(pool) -> None:
    assert await ledger.fetch_token_exposure(pool) == {}


# --------------------------------------------------------------------------
# §6. run_buys: манименеджмент выключен — поведение 9.5
# --------------------------------------------------------------------------


@needs_pg
async def test_fixed_mode_buys_the_notional_with_no_new_reads(pool, clock, monkeypatch) -> None:
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=30))
    ex = FakeExchange()
    spy = SpyPool(pool)
    ctx = build_context(spy, ex, clock)                          # mm_enabled=False
    assert ctx.config.mm_enabled is False

    async def boom(*a, **k):
        raise AssertionError("выключенный манименеджмент обратился к новым запросам")

    monkeypatch.setattr(ledger, "fetch_token_exposure", boom)
    original_state = ledger.ledger_state
    state_calls: list[str] = []

    async def counted_state(*a, **k):
        state_calls.append("state")
        return await original_state(*a, **k)

    monkeypatch.setattr(ledger, "ledger_state", counted_state)

    stats = await buys(ctx)

    assert stats.buys == 1 and stats.token_cap == 0 and stats.reserve == 0
    row = await order_of(pool, pid)
    assert row["status"] == "filled" and row["requested_cost_usd"] == D("2")
    assert row["sizing_rule"] == "fixed"
    assert row["sizing_equity_usd"] is None and row["sizing_exposure_usd"] is None
    assert ex.sent("buy")[0][2] == 2.0
    # единственный расчёт состояния — прежний, после исполнения ноги (_record_balance_after)
    assert state_calls == ["state"]
    assert not any("GROUP BY b.instrument_id" in q for q in spy.sql)


@needs_pg
async def test_fixed_mode_skip_rows_carry_the_fixed_marker_only(pool, clock) -> None:
    stale = await add_position(pool, opened_at=clock.now() - timedelta(seconds=400))
    ctx = build_context(pool, FakeExchange(), clock)
    await buys(ctx)
    row = await order_of(pool, stale)
    assert row["status"] == "skipped" and row["skip_reason"] == rules.SKIP_STALE
    assert row["sizing_rule"] == "fixed" and row["sizing_equity_usd"] is None


# --------------------------------------------------------------------------
# §6. run_buys: манименеджмент включён
# --------------------------------------------------------------------------


@needs_pg
async def test_a_ladder_of_three_then_the_coin_is_capped(pool, clock) -> None:
    async with many_open_per_coin(pool):
        ids = await candidates(pool, clock, ["BTC/USDT"] * 5)
        notified: list[str] = []
        redis = FakeRedis()
        ctx = mm_context(pool, clock, notified=notified, redis=redis)

        stats = await buys(ctx)

        rows = [await order_of(pool, pid) for pid in ids]
        assert [r["status"] for r in rows] == ["filled"] * 3 + ["skipped"] * 2
        assert [r["requested_cost_usd"] for r in rows[:3]] == [D("50")] * 3
        assert [r["skip_reason"] for r in rows[3:]] == [rules.SKIP_TOKEN_CAP] * 2
        assert all(r["sizing_rule"] == "mm1" for r in rows)
        assert all(r["sizing_equity_usd"] == D("1000") for r in rows)
        assert [r["sizing_exposure_usd"] for r in rows] == [D(0), D(50), D(100), D(150), D(150)]
        assert (stats.buys, stats.token_cap, stats.reserve) == (3, 2, 0)
        assert stats.skipped == {rules.SKIP_TOKEN_CAP: 2}
        assert stats.any()
        # штатная работа правила: ни одного алерта
        assert not [t for t in notified if t.startswith("⚠️")]
        assert not [k for k in redis.data if k.startswith(runner.ALERT_KEY_PREFIX)]


@needs_pg
async def test_the_next_iteration_reads_the_invested_money_from_the_database(
    pool, clock
) -> None:
    async with many_open_per_coin(pool):
        await candidates(pool, clock, ["BTC/USDT"] * 3)
        ctx = mm_context(pool, clock)
        await buys(ctx)
        exposure = await ledger.fetch_token_exposure(pool)
        assert list(exposure.values()) == [D("150")]

        late = await add_position(pool, symbol="BTC/USDT", opened_at=clock.now())
        stats = await buys(mm_context(pool, clock))

        row = await order_of(pool, late)
        assert row["skip_reason"] == rules.SKIP_TOKEN_CAP and stats.token_cap == 1
        # баланс чуть ниже $1 000 (комиссия и проскальзывание): снимок виден в строке
        assert D("990") < row["sizing_equity_usd"] < D("1000")
        assert row["sizing_exposure_usd"] == D("150")


@needs_pg
async def test_the_reserve_stops_buying_once_75_percent_is_spent(pool, clock) -> None:
    async with many_open_per_coin(pool):
        # потолок 30 %: монета ещё не упёрлась в него, когда кончаются свободные деньги
        ids = await candidates(pool, clock, (COINS * 4)[:16])
        notified: list[str] = []
        ctx = mm_context(pool, clock, notified=notified, mm_token_cap_pct=D(30))

        stats = await buys(ctx)

        rows = [await order_of(pool, pid) for pid in ids]
        assert [r["status"] for r in rows] == ["filled"] * 15 + ["skipped"]
        assert [r["requested_cost_usd"] for r in rows[:15]] == [D("50")] * 15
        assert rows[15]["skip_reason"] == rules.SKIP_RESERVE
        assert (stats.buys, stats.reserve, stats.token_cap) == (15, 1, 0)
        assert ctx.usdt_free == D(250)                       # резерв нетронут
        assert not [t for t in notified if t.startswith("⚠️")]


@needs_pg
async def test_money_under_unknown_orders_counts_against_the_reserve(pool, clock) -> None:
    held = await add_position(pool, symbol="ETH/USDT", opened_at=clock.now() - timedelta(minutes=1))
    await raw_order(pool, held, "buy", "sent", symbol="ETH/USDT", requested="710")
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))

    await buys(mm_context(pool, clock))

    row = await order_of(pool, pid)
    # свободно 1000 − 710 = 290; сверх резерва 250 остаётся 40
    assert row["status"] == "filled" and row["requested_cost_usd"] == D("40")


@needs_pg
async def test_a_failure_while_computing_the_state_skips_the_iteration(
    pool, clock, monkeypatch
) -> None:
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    ex = CoinsExchange(prices=dict(PRICES))

    async def broken(db):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(ledger, "fetch_token_exposure", broken)
    with capture_demo_logs(monkeypatch) as logs:
        stats = await buys(mm_context(pool, clock, ex))

    assert await pool.fetchval("SELECT count(*) FROM demo_orders;") == 0
    assert ex.sent() == [] and stats.buys == 0
    assert [e for e in logs.entries if "demo_mm_state_failed=1" in str(e["event"])]
    assert not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM demo_orders "
                                   "WHERE position_id = $1);", pid)


@needs_pg
async def test_a_stale_position_never_reaches_the_sizing_rule(pool, clock, monkeypatch) -> None:
    stale = await add_position(pool, opened_at=clock.now() - timedelta(seconds=400))

    async def boom(*a, **k):
        raise AssertionError("правило размера вызвано для устаревшей позиции")

    monkeypatch.setattr(ledger, "ledger_state", boom)
    monkeypatch.setattr(ledger, "fetch_token_exposure", boom)

    stats = await buys(mm_context(pool, clock))

    row = await order_of(pool, stale)
    assert row["skip_reason"] == rules.SKIP_STALE and row["sizing_rule"] == "mm1"
    assert row["sizing_equity_usd"] is None                 # правило не считалось
    assert stats.skipped == {rules.SKIP_STALE: 1}


@needs_pg
async def test_low_exchange_balance_is_checked_before_the_sizing_rule(
    pool, clock, monkeypatch
) -> None:
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))

    async def boom(*a, **k):
        raise AssertionError("правило размера вызвано до проверки баланса")

    monkeypatch.setattr(ledger, "ledger_state", boom)
    ex = CoinsExchange(prices=dict(PRICES), usdt=D(5))

    await buys(mm_context(pool, clock, ex))

    assert (await order_of(pool, pid))["skip_reason"] == rules.SKIP_NO_BALANCE


@needs_pg
async def test_a_rejected_buy_does_not_count_as_invested_in_the_iteration(pool, clock) -> None:
    async with many_open_per_coin(pool):
        ids = await candidates(pool, clock, ["BTC/USDT"] * 4)
        ex = CoinsExchange(prices=dict(PRICES), mode="reject")
        stats = await buys(mm_context(pool, clock, ex))
        # первая отклонена биржей — вложено 0, вторая пробует ту же сумму
        statuses = [(await order_of(pool, pid))["status"] for pid in ids]
        assert statuses[0] == "rejected" and stats.buys == 0
        assert all(s != "skipped" for s in statuses)


@needs_pg
async def test_only_buy_rows_get_the_sizing_columns(pool, clock) -> None:
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    ctx = mm_context(pool, clock)
    await runner.run_once(ctx)                                  # покупка
    await pool.execute(
        "UPDATE positions SET status = 'closed', closed_at = $2, exit_price = 61000, "
        "exit_reason = 'target', outcome_certain = TRUE, net_pnl_pct = 0.5, "
        "net_pnl_usd = 0.01 WHERE id = $1;", pid, clock.now())
    await runner.run_once(ctx)                                  # продажа
    sell = dict(await pool.fetchrow("SELECT * FROM demo_orders WHERE leg = 'sell';"))
    assert sell["status"] == "filled" and sell["sizing_rule"] is None
    assert sell["sizing_equity_usd"] is None and sell["sizing_exposure_usd"] is None
    buy = await order_of(pool, pid)
    assert buy["sizing_rule"] == "mm1" and buy["sizing_equity_usd"] == D("1000")


def test_iter_stats_any_sees_the_sizing_skips() -> None:
    assert not runner.IterStats().any()
    assert runner.IterStats(token_cap=1).any() and runner.IterStats(reserve=1).any()


# --------------------------------------------------------------------------
# Журнал запуска
# --------------------------------------------------------------------------


@needs_pg
async def test_startup_fields_carry_the_mm_values_in_one_json_line(pool, clock) -> None:
    ctx = mm_context(pool, clock, mm_reserve_pct=D("12.5"), mm_step_pct=D(4),
                     mm_token_cap_pct=D(20), mm_min_order_usd=D(7))
    fields = runner.startup_fields(ctx)
    assert fields["mm_enabled"] is True
    assert (fields["mm_reserve_pct"], fields["mm_step_pct"]) == ("12.5", "4")
    assert (fields["mm_token_cap_pct"], fields["mm_min_order_usd"]) == ("20", "7")
    line = json.dumps(fields, ensure_ascii=False)
    assert "\n" not in line and '"mm_enabled": true' in line


@needs_pg
async def test_the_start_line_and_the_overcommit_warning_are_logged(
    pool, clock, monkeypatch
) -> None:
    ctx = mm_context(pool, clock, mm_token_cap_pct=D(30))            # 25 + 30 × 5 = 175 > 100

    async def stop(_seconds):
        raise asyncio.CancelledError

    ctx.sleep = stop
    with capture_demo_logs(monkeypatch) as logs, pytest.raises(asyncio.CancelledError):
        await runner.run(ctx)

    start = [e for e in logs.entries if str(e["event"]).startswith("Сервис demo запущен")]
    assert len(start) == 1 and start[0]["mm_token_cap_pct"] == "30"
    assert start[0]["mm_enabled"] is True
    assert [e for e in logs.entries if "demo_mm_overcommit=1" in str(e["event"])]

    quiet = mm_context(pool, clock)                                  # 25 + 15 × 5 = 100
    quiet.sleep = stop
    with capture_demo_logs(monkeypatch) as logs, pytest.raises(asyncio.CancelledError):
        await runner.run(quiet)
    assert not [e for e in logs.entries if "demo_mm_overcommit=1" in str(e["event"])]


# --------------------------------------------------------------------------
# §7. Миграция 031
# --------------------------------------------------------------------------


def columns(db: str) -> set[str]:
    return set(psql_raw(
        db, "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'demo_orders' AND column_name LIKE 'sizing_%';"
    ).split())


def has_constraint(db: str) -> bool:
    return psql_raw(db, "SELECT count(*) FROM pg_constraint "
                        "WHERE conname = 'demo_orders_sizing_rule_chk';") == "1"


@needs_pg
def test_migration_031_applies_rolls_back_and_applies_again() -> None:
    db = build_database("mig031")
    try:
        want = {"sizing_rule", "sizing_equity_usd", "sizing_exposure_usd"}
        assert columns(db) == want and has_constraint(db)
        psql_raw(db, stdin=MIGRATION.read_text(encoding="utf-8"))          # идемпотентна
        assert columns(db) == want
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))
        assert columns(db) == set() and not has_constraint(db)
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))           # откат повторим
        psql_raw(db, stdin=MIGRATION.read_text(encoding="utf-8"))
        assert columns(db) == want and has_constraint(db)
    finally:
        drop_database(db)


@needs_pg
async def test_the_sizing_rule_constraint_and_old_rows(db_name) -> None:
    p = await open_pool(db_name)
    try:
        await reset_data(p)
        pid = await add_position(p, opened_at=datetime.now(UTC))
        await insert_buy_filled(p, pid)                         # «старая» строка без разметки
        row = await p.fetchrow("SELECT sizing_rule, sizing_equity_usd, sizing_exposure_usd "
                               "FROM demo_orders;")
        assert tuple(row.values()) == (None, None, None)
        import asyncpg

        with pytest.raises(asyncpg.CheckViolationError):
            await p.execute("UPDATE demo_orders SET sizing_rule = 'other';")
        await p.execute("UPDATE demo_orders SET sizing_rule = 'mm1';")
        await p.execute("UPDATE demo_orders SET sizing_rule = 'fixed';")
    finally:
        await p.close()


def test_the_migration_header_gives_the_stdin_command_not_a_mount() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "docker-entrypoint-initdb.d/migrations" not in compose           # путь не смонтирован
    for path in (MIGRATION, ROLLBACK):
        text = path.read_text(encoding="utf-8")
        assert "docker compose exec -T postgres" in text and f"< db/migrations/{path.name}" in text
        assert "docker-entrypoint-initdb.d/migrations" not in text.replace("НЕ смонтирован", "")


# --------------------------------------------------------------------------
# §8. Экран «Счёт» в боте
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)
MSK = __import__("zoneinfo").ZoneInfo("Europe/Moscow")


def account_state() -> ledger.LedgerState:
    return ledger.LedgerState(start_capital=D(1000), cash=D(850), in_market=D(150),
                              equity=D(1000), open_count=3)


def mm_view(**over) -> dict:
    view = {
        "step_pct": 5.0, "cap_pct": 15.0, "reserve_pct": 25.0, "reserved": D(0),
        "tokens": [("BTC", D(100)), ("ETH", D(50)), ("SOL", D(0)), ("XRP", D(0)),
                   ("DOGE", D(0))],
    }
    view.update(over)
    return view


def render(mm) -> list[str]:
    demo = {"state": account_state(), "mirror_since": NOW - timedelta(days=1)}
    text, _ = screens.account_screen(demo=demo, yesterday_close=D(1000), week={"closed": 0},
                                     now=NOW, tz=MSK, mm=mm)
    return text.splitlines()


def test_the_account_screen_shows_the_sizing_block() -> None:
    lines = render(mm_view())
    i = lines.index("Правило размера: покупка 5% баланса · до 15% на монету · резерв 25%")
    assert lines[i + 1:i + 7] == [
        "<b>Загрузка по монетам:</b>",
        "BTC  $100 из $150", "ETH  $50 из $150", "SOL  $0 из $150", "XRP  $0 из $150",
        "DOGE  $0 из $150",
    ]
    # свободно 850, резерв 250 → сверх резерва 600
    assert lines[i + 7] == "Резерв: $250 · свободно сверх резерва: $600"
    assert lines[-1].startswith("Обновлено")


def test_the_sizing_block_is_absent_when_the_rule_is_off() -> None:
    assert not [ln for ln in render(None) if "Правило размера" in ln or "Загрузка" in ln]


def test_the_sizing_block_follows_the_current_balance_and_settings() -> None:
    lines = render(mm_view(step_pct=2.5, cap_pct=10.0, reserve_pct=30.0, reserved=D(900)))
    assert "Правило размера: покупка 2,5% баланса · до 10% на монету · резерв 30%" in lines
    assert "BTC  $100 из $100" in lines
    # деньги под ордерами и резерв больше свободных — сверх резерва не меньше нуля
    assert "Резерв: $300 · свободно сверх резерва: $0" in lines


def test_the_account_screen_without_the_rule_is_unchanged() -> None:
    plain = render(None)
    assert plain[0] == "💰 <b>Демо-счёт OKX</b>"
    assert not any(ln.startswith(("BTC", "Резерв")) for ln in plain)


@needs_pg
async def test_bot_queries_report_the_exposure_per_coin_read_only(pool, clock) -> None:
    now = clock.now()
    btc = await add_position(pool, opened_at=now - timedelta(hours=2))
    await insert_buy_filled(pool, btc, cost_usd="100", avg_price="60060", filled_qty="0.0016")
    eth = await add_position(pool, symbol="ETH/USDT", opened_at=now - timedelta(minutes=2))
    await raw_order(pool, eth, "buy", "pending", symbol="ETH/USDT", requested="50")

    data = await BotQueries(pool).demo_mm()

    assert dict(data["tokens"]) == {
        "BTC": D("100"), "ETH": D("50"), "DOGE": D(0), "FOO": D(0), "SOL": D(0), "XRP": D(0)}
    assert data["reserved"] == D("50")
    assert [t for t, _ in data["tokens"]][:2] == ["BTC", "DOGE"]       # порядок инструментов


class _FakeQueries:
    def __init__(self, failing: bool = False) -> None:
        self.failing = failing
        self.mm_calls = 0

    async def demo_state(self):
        return {"state": account_state(), "mirror_since": NOW - timedelta(days=1)}

    async def demo_yesterday_close(self, today):
        return D(1000)

    async def demo_report(self, since):
        return {"closed": 0}

    async def demo_mm(self):
        self.mm_calls += 1
        if self.failing:
            raise RuntimeError("база недоступна")
        return {"tokens": [("BTC", D(100))], "reserved": D(0)}


def _bot(queries, monkeypatch, **cfg):
    import structlog

    from src.bot import nav, poller
    from src.bot.nav import NavTarget

    bot = object.__new__(BotPoller)
    bot.queries = queries
    bot.tz = MSK
    bot._log = structlog.get_logger()
    monkeypatch.setattr(poller, "settings", SimpleNamespace(
        DEMO_ENABLED=True, DEMO_NOTIFY_ENABLED=True, NOTIFY_SIGNALS_ENABLED=False,
        DEMO_MM_ENABLED=cfg.get("enabled", True), DEMO_MM_STEP_PCT=5.0,
        DEMO_MM_TOKEN_CAP_PCT=15.0, DEMO_MM_RESERVE_PCT=25.0))
    return bot, NavTarget(nav.ACCOUNT)


async def test_the_poller_adds_the_block_with_percentages_from_the_settings(monkeypatch) -> None:
    queries = _FakeQueries()
    bot, target = _bot(queries, monkeypatch)
    text, _ = await bot._build(target, 1, NOW, None)
    assert "Правило размера: покупка 5% баланса · до 15% на монету · резерв 25%" in text
    assert "BTC  $100 из $150" in text


async def test_the_poller_leaves_the_block_out_when_the_rule_is_off(monkeypatch) -> None:
    queries = _FakeQueries()
    bot, target = _bot(queries, monkeypatch, enabled=False)
    text, _ = await bot._build(target, 1, NOW, None)
    assert "Правило размера" not in text and queries.mm_calls == 0


async def test_a_failure_in_the_block_does_not_break_the_screen(monkeypatch) -> None:
    bot, target = _bot(_FakeQueries(failing=True), monkeypatch)
    text, _ = await bot._build(target, 1, NOW, None)
    assert "Итого:" in text and "Правило размера" not in text


# --------------------------------------------------------------------------
# Границы этапа (§2, §11 п. 4)
# --------------------------------------------------------------------------


def test_requirements_and_logic_version_are_untouched() -> None:
    from tests.conftest import CURRENT_LOGIC_VERSION

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for pin in ("asyncpg==0.30.0", "pydantic-settings==2.7.1", "ccxt==4.4.100",
                "pytest==8.3.4", "pytest-asyncio==0.24.0", "ruff==0.8.4"):
        assert pin in requirements
    from src.core.config import settings

    assert settings.LOGIC_VERSION == CURRENT_LOGIC_VERSION


def test_the_sizing_rule_names_are_the_ones_the_migration_allows() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert f"'{rules.SIZING_FIXED}'" in sql and f"'{rules.SIZING_MM1}'" in sql
    assert (rules.SIZING_FIXED, rules.SIZING_MM1) == ("fixed", "mm1")


# --------------------------------------------------------------------------
# §11 п. 7. deploy/verify_9_6.sh — на настоящей базе, docker подменён заглушкой
# --------------------------------------------------------------------------

STARTUP_LINE = (
    '{{"enabled": true, "host": "www.okx.com", "mm_enabled": {enabled}, '
    '"mm_reserve_pct": "{reserve}", "mm_step_pct": "{step}", "mm_token_cap_pct": "{cap}", '
    '"mm_min_order_usd": "{min}", "event": "Сервис demo запущен (демо-счёт OKX, спот)", '
    '"timestamp": "2000-01-01T00:00:00Z"}}'
)
VERIFY = ROOT / "deploy" / "verify_9_6.sh"


def _verify_runner(tmp_path, db_name, *, env_lines=None, startup=None):
    import os
    import stat
    import subprocess

    from tests.demo_support import HOST, PORT, USER

    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    logfile = tmp_path / "demo.log"
    logfile.write_text(
        (startup if startup is not None else STARTUP_LINE.format(
            enabled="true", reserve="25", step="5", cap="15", min="5")) + "\n",
        encoding="utf-8")
    docker = bindir / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
  *" psql "*) sql="${{@: -1}}"
    exec psql -h "{HOST}" -p "{PORT}" -U "{USER}" -d "{db_name}" -tA -c "$sql" ;;
  *"ps --format"*) echo running ;;
  *"compose logs"*) cat "{logfile}" ;;
esac
""", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (app / ".env").write_text("\n".join(env_lines if env_lines is not None else [
        "DEMO_MM_ENABLED=true", "DEMO_MM_RESERVE_PCT=25", "DEMO_MM_STEP_PCT=5",
        "DEMO_MM_TOKEN_CAP_PCT=15.0   # потолок", "DEMO_MM_MIN_ORDER_USD=5"]) + "\n",
        encoding="utf-8")

    def run(*args: str):
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "APP_DIR": str(app),
               "HOME": str(tmp_path)}
        return subprocess.run(["bash", str(VERIFY), *args], env=env, capture_output=True,
                              text=True, check=False)

    return run


needs_bash = pytest.mark.skipif(
    __import__("shutil").which("bash") is None, reason="нужен bash")


@needs_pg
@needs_bash
async def test_verify_script_is_green_on_a_healthy_ladder(pool, clock, tmp_path, db_name) -> None:
    async with many_open_per_coin(pool):
        await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
        await candidates(pool, clock, ["BTC/USDT"] * 5)
        await buys(mm_context(pool, clock))
        result = _verify_runner(tmp_path, db_name)()

    out = result.stdout
    assert result.returncode == 0, out
    assert "✅ три колонки и ограничение" in out
    assert "✅ запущен" in out
    assert "✅ все пять величин в журнале совпадают с .env" in out
    assert "✅ у всех 5 новых строк sizing_rule='mm1'" in out
    assert "✅ ни одна покупка не больше шага" in out
    assert "✅ ни одна покупка не вывела монету за потолок" in out
    assert "✅ ни одна монета не выше потолка" in out
    assert "BTC/USDT  $150.00 / $150.00" in out
    assert "✅ свободные деньги не ниже резерва" in out
    assert "token_cap=2" in out, out
    assert "❌" not in out and "ИТОГ: ✅ всё в порядке" in out and "ЧТО ДЕЛАТЬ" in out


@needs_pg
@needs_bash
async def test_verify_script_flags_a_coin_above_its_cap(pool, clock, tmp_path, db_name) -> None:
    async with many_open_per_coin(pool):
        await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
        ids = await candidates(pool, clock, ["BTC/USDT"] * 3)
        await buys(mm_context(pool, clock))
        # «ручная правка»: вложенное в монету выросло за потолок
        await pool.execute("UPDATE demo_orders SET cost_usd = 400 WHERE position_id = $1 "
                           "AND leg = 'buy';", ids[0])
        result = _verify_runner(tmp_path, db_name)()

    assert result.returncode == 1, result.stdout
    assert "❌ вложено больше потолка: BTC/USDT" in result.stdout
    assert "ИТОГ: ❌" in result.stdout and "ЧТО ДЕЛАТЬ:" in result.stdout


@needs_pg
@needs_bash
async def test_verify_script_flags_a_purchase_above_the_step(
    pool, clock, tmp_path, db_name
) -> None:
    await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    await buys(mm_context(pool, clock))
    await pool.execute("UPDATE demo_orders SET requested_cost_usd = 90 WHERE position_id = $1;",
                       pid)
    result = _verify_runner(tmp_path, db_name)()
    assert result.returncode == 1
    assert "❌ покупок больше шага: 1" in result.stdout


@needs_pg
@needs_bash
async def test_verify_script_flags_a_broken_reserve(pool, clock, tmp_path, db_name) -> None:
    await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
    pid = await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    await buys(mm_context(pool, clock))
    # купили на $800: свободно $200 < резерва $250
    await pool.execute("UPDATE demo_orders SET cost_usd = 800 WHERE position_id = $1;", pid)
    result = _verify_runner(tmp_path, db_name)()
    assert "❌ свободно $200.00 — ниже резерва $250.00" in result.stdout
    assert result.returncode == 1


@needs_pg
@needs_bash
async def test_verify_script_reports_env_that_differs_from_the_running_service(
    pool, clock, tmp_path, db_name
) -> None:
    await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
    run = _verify_runner(tmp_path, db_name, env_lines=[
        "DEMO_MM_ENABLED=true", "DEMO_MM_STEP_PCT=7", "DEMO_MM_TOKEN_CAP_PCT=15"])
    result = run()
    assert "❌ расходятся с .env: DEMO_MM_STEP_PCT" in result.stdout
    assert result.returncode == 1
    # «25» и «25.0» — одно значение
    same = _verify_runner(tmp_path, db_name, startup=STARTUP_LINE.format(
        enabled="true", reserve="25.0", step="5.0", cap="15", min="5.00"))()
    assert "✅ все пять величин в журнале совпадают с .env" in same.stdout


@needs_pg
@needs_bash
async def test_verify_script_in_the_fixed_mode_expects_fixed_rows(
    pool, clock, tmp_path, db_name
) -> None:
    await set_state(pool, 1000, clock.now() - timedelta(minutes=10))
    await add_position(pool, opened_at=clock.now() - timedelta(seconds=20))
    await buys(build_context(pool, FakeExchange(), clock))          # MM выключен
    run = _verify_runner(tmp_path, db_name, env_lines=["DEMO_MM_ENABLED=false"],
                         startup=STARTUP_LINE.format(enabled="false", reserve="25", step="5",
                                                     cap="15", min="5"))
    result = run()
    assert result.returncode == 0, result.stdout
    assert "✅ у всех 1 новых строк sizing_rule='fixed'" in result.stdout
    assert "правило выключено" in result.stdout and "ИТОГ: ⚪" in result.stdout


@needs_pg
@needs_bash
async def test_verify_script_is_not_green_when_nothing_has_happened_yet(
    pool, clock, tmp_path, db_name
) -> None:
    await set_state(pool, 1000, clock.now())
    result = _verify_runner(tmp_path, db_name)()
    assert result.returncode == 0
    assert "новых строк покупки в окне нет" in result.stdout
    assert "ИТОГ: ⚪" in result.stdout and "✅ всё в порядке" not in result.stdout


@needs_bash
def test_verify_script_rejects_unknown_arguments(tmp_path) -> None:
    run = _verify_runner(tmp_path, "postgres")
    assert run("--bogus").returncode == 2
    assert run("--hours", "0").returncode == 2
    assert run("--hours").returncode == 2


def test_verify_script_is_read_only() -> None:
    import re

    text = VERIFY.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    code = "\n".join(ln for ln in code.splitlines() if "advice " not in ln)   # подсказки — текст
    assert not re.search(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE|"
                         r"ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+TABLE)\b", code, re.I)
    assert set(re.findall(r"docker compose (\w+)", code)) <= {"exec", "ps", "logs"}
    assert not re.search(r"\brm\s+-|\bkill\b|systemctl|redis-cli", code)
    assert "set -uo pipefail" in text and "ИТОГ" in text and "ЧТО ДЕЛАТЬ" in text
