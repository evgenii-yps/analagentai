"""Этап 9.5, часть 3: VIRTUAL_OUTPUT_ENABLED — скрыть виртуальные сделки полностью.

Для каждого пути (§2.1–§2.5 ТЗ): при ``true`` вывод тот же, что до этапа, при ``false`` —
скрыт или заменён демо-данными; при ``DEMO_ENABLED=false`` замены нет и ошибок нет.
Плюс §3: дефект окна потолка в ``src/notify/rate_limit.py``.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import structlog
import structlog.testing

from src import export_main
from src.bot import handlers, poller, screens
from src.core.config import Settings, settings
from src.demo import ledger
from src.health import daily_report as health
from src.notify import rate_limit
from src.positions import runner as positions_runner
from tests.demo_support import (
    FakeRedis,
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
ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)

# --------------------------------------------------------------------------
# §1: настройка
# --------------------------------------------------------------------------


def test_the_switch_exists_defaults_to_true_and_is_documented() -> None:
    assert Settings(POSTGRES_PASSWORD="x", _env_file=None).VIRTUAL_OUTPUT_ENABLED is True
    off = Settings(POSTGRES_PASSWORD="x", _env_file=None, VIRTUAL_OUTPUT_ENABLED=False)
    assert off.VIRTUAL_OUTPUT_ENABLED is False
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "VIRTUAL_OUTPUT_ENABLED=true" in env
    assert "POSITION_NOTIFY_ENABLED=false" in env      # порядок переключения описан рядом


# --------------------------------------------------------------------------
# §2.2: /positions и справка
# --------------------------------------------------------------------------

# Этап 9.7: справка переехала в screens.help_screen и команды /positions не показывает ни
# при каком значении переключателя (она нужна только для прежних данных; в меню команд её нет).


def test_help_never_lists_the_positions_command() -> None:
    for demo_enabled in (True, False):
        text, _ = screens.help_screen(
            flags=screens.Flags(demo_enabled=demo_enabled), coins=["BTC", "ETH"])
        assert "/positions" not in text
        assert "/demo" in text and "/status" in text


class StubQueries:
    """Заглушка запросов бота: считает обращения к данным о виртуальных позициях."""

    def __init__(self, demo: dict | None = None) -> None:
        self.calls: list[str] = []
        self._demo = demo

    async def positions_open(self):
        self.calls.append("positions_open")
        return []

    async def positions_summary(self, **kw):
        self.calls.append("positions_summary")
        return {}

    async def positions_capital(self):
        self.calls.append("positions_capital")
        return {"committed_usd": 0.0, "realized_usd": 0.0, "by_version": []}

    async def positions_state(self, pause_sec, logic_version):
        self.calls.append("positions_state")
        return {"open_count": 3, "pauses": []}

    async def demo_overview(self, days: int = 7):
        self.calls.append("demo_overview")
        return self._demo

    async def status_facts(self):
        return {}

    async def freshness_by_instrument(self, instruments):
        return []

    async def spot_instruments(self):
        return [(1, "BTC/USDT"), (2, "ETH/USDT")]


def make_poller(queries: StubQueries) -> poller.BotPoller:
    bot = object.__new__(poller.BotPoller)
    bot.queries = queries
    bot._log = structlog.get_logger()

    async def heartbeats():
        return []

    async def chat_settings(chat_id):
        return types.SimpleNamespace(instruments=None)

    async def read_refusals(*a, **k):
        return {}

    bot.tz = ZoneInfo("Europe/Moscow")
    bot.max_rows = 20
    bot._read_heartbeats = heartbeats
    bot._chat_settings = chat_settings
    bot._read_refusals = read_refusals
    return bot


async def test_positions_command_answers_as_before_when_the_switch_is_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", True)
    queries = StubQueries()
    reply = await make_poller(queries)._answer_command("/positions", 1)
    assert "Виртуальные позиции" in reply and "Открытых позиций нет." in reply
    assert "positions_open" in queries.calls


async def test_positions_command_is_one_line_when_the_switch_is_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    queries = StubQueries()
    reply = await make_poller(queries)._answer_command("/positions", 1)
    assert reply == "Виртуальные сделки скрыты. Демо-счёт — /demo"
    assert queries.calls == []                         # за позициями в базу не ходили
    assert "demo" in handlers.KNOWN_COMMANDS and "positions" in handlers.KNOWN_COMMANDS


async def test_help_command_does_not_depend_on_the_switch(monkeypatch) -> None:
    queries = StubQueries()
    for value in (True, False):
        monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", value)
        for command in ("/help", "/start", "/menu"):
            text, _ = await make_poller(queries)._answer_command(command, 1)
            assert "/positions" not in text


# --------------------------------------------------------------------------
# §2.3: /status
# --------------------------------------------------------------------------


def demo_data() -> dict:
    return {"state": ledger.LedgerState(D("1000"), D("990"), D("12.5"), D("1002.5"), 2),
            "open_trades": [], "days": 7, "closed": 0, "wins": 0, "profit_usd": D("0")}


async def test_status_keeps_the_virtual_section_when_the_switch_is_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    queries = StubQueries(demo_data())
    text = await make_poller(queries)._status_text(NOW, 1)
    assert "Сделки (виртуальные):" in text and "Открытых позиций: 3" in text
    assert "Демо-счёт:" not in text                    # прежний вывод, без добавок
    assert "demo_overview" not in queries.calls


async def test_status_swaps_the_section_for_the_demo_account_when_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    queries = StubQueries(demo_data())
    text = await make_poller(queries)._status_text(NOW, 1)
    assert "Сделки (виртуальные)" not in text and "Открытых позиций" not in text
    assert "positions_state" not in queries.calls
    assert "<b>Демо-счёт:</b>" in text
    assert "Баланс итого: $1 002,50" in text
    assert "Открытых сделок: 2" in text
    assert "С начала: +0,25%" in text


async def test_status_has_no_section_when_off_and_the_demo_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_ENABLED", False)
    queries = StubQueries(demo_data())
    text = await make_poller(queries)._status_text(NOW, 1)
    assert "Сделки (виртуальные)" not in text and "Демо-счёт" not in text
    assert queries.calls == []                         # ни виртуальные, ни демо-данные не читались
    assert "Всё работает нормально" in text            # ответ целый, ошибок нет


async def test_status_survives_a_broken_demo_query(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)

    class Broken(StubQueries):
        async def demo_overview(self, days: int = 7):
            raise RuntimeError("таблицы нет")

    text = await make_poller(Broken())._status_text(NOW, 1)
    assert "Демо-счёт" not in text and "Всё работает нормально" in text


def test_render_status_is_unchanged_without_the_new_argument() -> None:
    base = handlers.render_status([], {}, NOW, None, {"open_count": 1, "token_pause_min": 0})
    assert "Сделки (виртуальные):" in base and "Демо-счёт" not in base


# --------------------------------------------------------------------------
# §2.4: алерты выгрузки
# --------------------------------------------------------------------------

PREVIOUS_ALERT_POSITIONS_ONLY = (
    "ошибка листа\nНе записано в торговый журнал: открытий=4, закрытий=2. "
    "Повтор на следующем запуске."
)
PREVIOUS_ALERT_FULL = (
    "ошибка листа\nНе выгружено: Sheets=3, Notion=5, торговый журнал: открытий=4, "
    "закрытий=2. Повтор на следующем запуске."
)


def test_alert_texts_are_identical_to_the_previous_ones_when_the_queue_is_shown() -> None:
    assert export_main.alert_text(
        "ошибка листа", positions_only=True, show_queue=True, to_open=4, to_close=2
    ) == PREVIOUS_ALERT_POSITIONS_ONLY
    assert export_main.alert_text(
        "ошибка листа", positions_only=False, show_queue=True, to_open=4, to_close=2,
        left_sheets=3, left_notion=5) == PREVIOUS_ALERT_FULL


def test_alert_texts_lose_only_the_queue_counters_when_hidden() -> None:
    only = export_main.alert_text("ошибка листа", positions_only=True, show_queue=False,
                                  to_open=4, to_close=2)
    full = export_main.alert_text("ошибка листа", positions_only=False, show_queue=False,
                                  to_open=4, to_close=2, left_sheets=3, left_notion=5)
    assert only == "ошибка листа\nПовтор на следующем запуске."
    assert full == "ошибка листа\nНе выгружено: Sheets=3, Notion=5. Повтор на следующем запуске."
    for text in (only, full):
        assert "открытий" not in text and "закрытий" not in text and "торговый журнал" not in text


class FakeConn:
    async def close(self) -> None:
        return None


@pytest.fixture
def export_env(monkeypatch):
    """Прогон ``export_main._run`` на заглушках: цель Sheets падает, остальное — нет."""
    sent: list[str] = []
    calls: list[str] = []

    async def connect(dsn):
        return FakeConn()

    async def noop(conn):
        return None

    async def failing_sheets(conn, log):
        raise export_main.ExportError("лист «Сигналы»: HTTP 500")

    async def trades(conn, log):
        return (0, 0)

    async def notion(conn, log):
        return 0

    async def pending(conn):
        calls.append("count_positions_pending")
        return (4, 2)

    async def unexported(conn, target):
        return 3 if target == "sheets" else 5

    async def alert(text, log):
        sent.append(text)

    monkeypatch.setattr(export_main.asyncpg, "connect", connect)
    monkeypatch.setattr(export_main.queries, "apply_migrations", noop)
    monkeypatch.setattr(export_main.queries, "count_positions_pending", pending)
    monkeypatch.setattr(export_main.queries, "count_unexported", unexported)
    monkeypatch.setattr(export_main, "_export_sheets", failing_sheets)
    monkeypatch.setattr(export_main, "_export_trades", trades)
    monkeypatch.setattr(export_main, "_export_notion", notion)
    monkeypatch.setattr(export_main, "_alert", alert)
    monkeypatch.setattr(settings, "EXPORT_ENABLED", True)
    monkeypatch.setattr(settings, "DEMO_SHEETS_ENABLED", False)
    return sent, calls


@pytest.mark.parametrize("positions_only", [False, True])
def test_the_run_alert_is_the_previous_text_when_the_switch_is_on(
    monkeypatch, export_env, positions_only
) -> None:
    sent, calls = export_env
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", True)
    if positions_only:                         # в этом режиме ошибку даёт журнал сделок
        async def failing_trades(conn, log):
            raise export_main.ExportError("ошибка листа")
        monkeypatch.setattr(export_main, "_export_trades", failing_trades)
    code = asyncio.run(export_main._run(positions_only=positions_only))
    assert code == 1 and len(sent) == 1 and calls == ["count_positions_pending"]
    if positions_only:
        assert sent[0] == PREVIOUS_ALERT_POSITIONS_ONLY
    else:
        assert sent[0] == PREVIOUS_ALERT_FULL.replace(
            "ошибка листа", "лист «Сигналы»: HTTP 500")


@pytest.mark.parametrize("positions_only", [False, True])
def test_the_run_alert_hides_the_queue_and_skips_its_query_when_off(
    monkeypatch, export_env, positions_only
) -> None:
    sent, calls = export_env
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    if positions_only:
        async def failing_trades(conn, log):
            raise export_main.ExportError("ошибка листа")
        monkeypatch.setattr(export_main, "_export_trades", failing_trades)
    code = asyncio.run(export_main._run(positions_only=positions_only))
    assert code == 1 and len(sent) == 1
    assert calls == []                                  # счётчик очереди даже не читался
    assert "открытий" not in sent[0] and "закрытий" not in sent[0]
    assert "Повтор на следующем запуске." in sent[0]
    if not positions_only:
        assert "Sheets=3, Notion=5" in sent[0]


# --------------------------------------------------------------------------
# §2.5: почасовая сводка придержанных сообщений positions
# --------------------------------------------------------------------------


@pytest.fixture
def rollup_env(monkeypatch):
    redis = FakeRedis()
    sent: list[str] = []
    monkeypatch.setattr(positions_runner, "get_redis", lambda: redis)

    async def send(text, *a, **k):
        sent.append(text)
        return True

    monkeypatch.setattr(positions_runner, "send_message", send)
    cap = structlog.testing.LogCapture()
    proxy = structlog.wrap_logger(
        None, processors=[cap], wrapper_class=structlog.make_filtering_bound_logger(0)
    ).bind(component="positions")
    monkeypatch.setattr(positions_runner, "_log", proxy)
    return redis, sent, cap


async def test_the_rollup_is_sent_as_before_when_notifications_are_on(
    monkeypatch, rollup_env
) -> None:
    redis, sent, cap = rollup_env
    monkeypatch.setattr(settings, "POSITION_NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_TRADES_MAX_PER_HOUR", 2)
    await redis.rpush(positions_runner._TRADE_HELD_KEY, "сообщение 1", "сообщение 2")
    await positions_runner._flush_trade_rollup(NOW)
    assert len(sent) == 1 and "Придержано сообщений о сделках: 2" in sent[0]
    assert "сообщение 1" in sent[0] and "сообщение 2" in sent[0]
    assert positions_runner._TRADE_HELD_KEY not in redis.lists
    assert not [e for e in cap.entries if e["event"] == "notify_trade_rollup_discarded=1"]


@pytest.mark.parametrize("virtual_output", [True, False])
@pytest.mark.parametrize("cap_per_hour", [0, 2])
async def test_held_messages_are_discarded_not_sent_when_notifications_are_off(
    monkeypatch, rollup_env, virtual_output, cap_per_hour
) -> None:
    """Правило не зависит ни от VIRTUAL_OUTPUT_ENABLED, ни от потолка."""
    redis, sent, cap = rollup_env
    monkeypatch.setattr(settings, "POSITION_NOTIFY_ENABLED", False)
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", virtual_output)
    monkeypatch.setattr(settings, "NOTIFY_TRADES_MAX_PER_HOUR", cap_per_hour)
    await redis.rpush(positions_runner._TRADE_HELD_KEY, "a", "b", "c")

    await positions_runner._flush_trade_rollup(NOW)

    assert sent == []                                              # ничего не ушло
    assert positions_runner._TRADE_HELD_KEY not in redis.lists     # придержанное отброшено
    entries = [e for e in cap.entries if e["event"] == "notify_trade_rollup_discarded=1"]
    assert len(entries) == 1 and entries[0]["count"] == 3          # в журнале число и причина
    assert "POSITION_NOTIFY_ENABLED=false" in entries[0]["reason"]


async def test_nothing_held_means_no_discard_record_and_no_error(monkeypatch, rollup_env) -> None:
    redis, sent, cap = rollup_env
    monkeypatch.setattr(settings, "POSITION_NOTIFY_ENABLED", False)
    await positions_runner._flush_trade_rollup(NOW)
    assert sent == [] and cap.entries == []


async def test_a_dead_redis_does_not_break_the_discard(monkeypatch, rollup_env) -> None:
    redis, sent, cap = rollup_env

    class Dead:
        async def lrange(self, *a):
            raise ConnectionError("redis недоступен")

    monkeypatch.setattr(positions_runner, "get_redis", lambda: Dead())
    monkeypatch.setattr(settings, "POSITION_NOTIFY_ENABLED", False)
    await positions_runner._flush_trade_rollup(NOW)
    assert sent == []
    assert any(e["event"] == "notify_trade_rollup_discard_failed=1" for e in cap.entries)


# --------------------------------------------------------------------------
# §2.1: суточный отчёт здоровья
# --------------------------------------------------------------------------


@pytest.fixture
def health_env(monkeypatch):
    """Отчёт здоровья без docker: остальные разделы — заглушки, запросы к базе — в журнал."""
    queries: list[str] = []

    def fake_psql(sql: str, field_sep: str = "|") -> str:
        queries.append(sql)
        if "FROM demo_state" in sql:
            return "990|12.5|1000|5|3|1|2"
        if "count(*) || '|'" in sql:
            return "4|3|0.052|2"
        return "7"

    for name in ("section_server", "section_containers", "section_heartbeats",
                 "section_data_24h", "section_signals_24h", "section_agent_failures",
                 "section_agent_silence", "section_funding_reserve", "section_db_and_errors"):
        monkeypatch.setattr(health, name, lambda: ["Х"])
    monkeypatch.setattr(health, "_psql", fake_psql)
    monkeypatch.setattr(health, "_redis_get", lambda key: "0")
    return queries


def test_health_report_is_unchanged_when_the_switch_is_on(monkeypatch, health_env) -> None:
    monkeypatch.setattr(health, "VIRTUAL_OUTPUT_ENABLED", True)
    monkeypatch.setattr(health, "DEMO_ENABLED", True)          # на прежний вывод не влияет
    message = health.build_message()
    assert "💼 Сделки за 24 часа" in message and "Демо-счёт" not in message
    block = "\n".join(health.section_positions_24h())
    assert block in message and "Открыто: 7" in block and "Закрыто: 4" in block
    assert not any("demo_state" in q for q in health_env)


def test_health_report_replaces_the_section_with_the_demo_account_when_off(
    monkeypatch, health_env
) -> None:
    monkeypatch.setattr(health, "VIRTUAL_OUTPUT_ENABLED", False)
    monkeypatch.setattr(health, "DEMO_ENABLED", True)
    message = health.build_message()
    assert "Сделки за 24 часа" not in message
    assert not any("FROM positions" in q for q in health_env)    # виртуальные не читались
    assert "<b>🧪 Демо-счёт</b>" in message
    assert "Баланс итого: $1,002.50 · свободно $990.00 · в рынке $12.50" in message
    assert "С начала: +2.50 $ (+0.25%)" in message
    assert "Сделок за 24 часа: открыто 5, закрыто 3" in message
    assert "lost: 1 · rejected: 2 (за 24 часа)" in message


def test_health_report_has_no_section_when_off_and_the_demo_is_disabled(
    monkeypatch, health_env
) -> None:
    monkeypatch.setattr(health, "VIRTUAL_OUTPUT_ENABLED", False)
    monkeypatch.setattr(health, "DEMO_ENABLED", False)
    message = health.build_message()
    assert "Сделки за 24 часа" not in message and "Демо-счёт" not in message
    assert health_env == []                                       # база за сделками не читалась
    assert "\n\n\n" not in message                                # и пустого блока нет
    assert message.count("Х") == 9


def test_the_demo_section_says_so_when_there_is_no_data(monkeypatch, health_env) -> None:
    monkeypatch.setattr(health, "_psql", lambda sql, field_sep="|": "")   # таблиц нет
    lines = health.section_demo_24h()
    assert lines[0] == "<b>🧪 Демо-счёт</b>" and "Данных пока нет" in lines[1]
    assert health.format_demo_section("мусор|не|числа")[1].startswith("Данных пока нет")


def test_the_env_flags_of_the_host_script_are_parsed_with_comments(monkeypatch) -> None:
    monkeypatch.setattr(health, "ENV", {"A": "false   # выключено", "B": "TRUE", "C": ""})
    monkeypatch.delenv("A", raising=False)
    assert health._env_bool("A", True) is False
    assert health._env_bool("B", False) is True
    assert health._env_bool("C", True) is True and health._env_bool("NOPE", False) is False


# --- формулы раздела совпадают с ledger на одной и той же базе ------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("virtout")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_health_demo_section_matches_the_ledger_on_the_same_database(pool) -> None:
    now = datetime.now(UTC)
    await set_state(pool, 1000, now - timedelta(days=5))
    ids = {}
    for key, symbol, price, closed in (("p1", "BTC/USDT", 60000.0, True),
                                       ("p2", "DOGE/USDT", 0.1, True),
                                       ("p3", "BTC/USDT", 60000.0, False)):
        ids[key] = await add_position(
            pool, symbol=symbol, opened_at=now - timedelta(days=3), entry_price=price,
            status="closed" if closed else "open",
            closed_at=now - timedelta(days=3) + timedelta(hours=1) if closed else None)
    # p1: покупка 3 суток назад (вне 24 ч), продажа час назад (внутри);
    # p2 — обе ноги внутри окна; p3 — открыта
    await insert_buy_filled(pool, ids["p1"], filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60000", cost_usd="2.0", filled_at=now - timedelta(days=3))
    await insert_sell_filled(pool, ids["p1"], cost_usd="2.04", fee="0.002",
                             filled_at=now - timedelta(hours=1))
    await insert_buy_filled(pool, ids["p2"], symbol="DOGE/USDT", filled_qty="20", fee="0.002",
                            fee_ccy="USDT", avg_price="0.1", cost_usd="2.0",
                            filled_at=now - timedelta(hours=5))
    await insert_sell_filled(pool, ids["p2"], symbol="DOGE/USDT", cost_usd="1.98", fee="0.002",
                             filled_qty="20", avg_price="0.099",
                             filled_at=now - timedelta(hours=4))
    await insert_buy_filled(pool, ids["p3"], filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60000", cost_usd="2.0", filled_at=now - timedelta(hours=3))
    other = await add_position(pool, opened_at=now - timedelta(hours=2))
    for status, cl in (("lost", "lost1"), ("rejected", "rej1")):
        await pool.execute(
            "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
            "symbol, host, virtual_ts) VALUES ($1, 1, 'sell', $2, $3, 'BTC/USDT', 'h', now());",
            other if status == "lost" else ids["p3"], status, cl)
    inst = await pool.fetchval("SELECT id FROM instruments WHERE symbol = 'BTC/USDT';")
    await pool.execute(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, '1m', now() - interval '5 minutes', 62000, 62000, 62000, 62000, 1);", inst)

    row = await pool.fetchrow(health.DEMO_SECTION_SQL)
    state = await ledger.compute_state(pool, D("1000"))          # без тикеров: свеча → ohlcv

    assert D(str(row[0])) == state.cash and D(str(row[1])) == state.in_market
    assert D(str(row[2])) == D("1000")
    assert state.in_market == D("0.0000333") * D("62000")
    assert (row[3], row[4], row[5], row[6]) == (2, 2, 1, 1)      # открыто/закрыто/lost/rejected
    raw = "|".join(str(v) for v in row)
    text = "\n".join(health.format_demo_section(raw))
    assert f"Баланс итого: ${float(state.equity):,.2f}" in text
    assert "Сделок за 24 часа: открыто 2, закрыто 2" in text
    assert "lost: 1 · rejected: 1" in text


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_health_demo_section_query_is_valid_on_an_empty_demo(pool) -> None:
    assert await pool.fetchrow(health.DEMO_SECTION_SQL) is None          # demo_state ещё нет
    assert health.format_demo_section("")[1].startswith("Данных пока нет")
    await set_state(pool, 500, NOW)
    row = await pool.fetchrow(health.DEMO_SECTION_SQL)
    assert D(str(row[0])) == D("500") and D(str(row[1])) == 0 and row[3:] == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# §3: окно потолка в src/notify/rate_limit.py
# --------------------------------------------------------------------------


async def test_two_messages_with_the_same_timestamp_count_as_two(monkeypatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: redis)
    key = "test:window"
    await rate_limit.record_sent(key, NOW)
    await rate_limit.record_sent(key, NOW)                    # та же метка времени
    await rate_limit.record_sent(key, NOW)
    assert await rate_limit.sent_last_hour(key, NOW, True) == 3
    # окно по-прежнему скользящее: через час и секунду все три вышли из окна
    later = NOW + timedelta(seconds=3601)
    assert await rate_limit.sent_last_hour(key, later, True) == 0
    # а промежуточные метки считаются каждая своим весом
    await rate_limit.record_sent(key, NOW)
    await rate_limit.record_sent(key, NOW + timedelta(seconds=1800))
    assert await rate_limit.sent_last_hour(key, NOW + timedelta(seconds=3000), True) == 2
    assert await rate_limit.sent_last_hour(key, NOW + timedelta(seconds=4000), True) == 1


async def test_a_disabled_ceiling_still_asks_redis_nothing(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "get_redis", lambda: (_ for _ in ()).throw(AssertionError))
    assert await rate_limit.sent_last_hour("k", NOW, False) == 0
