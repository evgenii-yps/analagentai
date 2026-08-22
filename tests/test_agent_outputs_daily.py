"""Суточная свёртка выводов агентов (решение заказчика по §4.3, пункт 1).

Проверяется РОВНО тот SQL, который выполняет ежесуточная задача:
``scripts.retention.rollup_daily_sql`` — единственный его источник.

Тестам нужна настоящая PostgreSQL (свёртка — агрегирующий SQL с оконными
функциями и перцентилями). База берётся из ``AGENT_TEST_DSN``; без переменной
тесты ПРОПУСКАЮТСЯ с явной причиной.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scripts.retention import rollup_daily_sql

TEST_DSN = (
    os.environ.get("AGENT_TEST_DSN", "").strip()
    or os.environ.get("BT_TEST_DSN", "").strip()
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="нужна тестовая БД: задайте AGENT_TEST_DSN (свёртка проверяется в SQL)",
)

_SCHEMA = """
DROP SCHEMA IF EXISTS daily_test CASCADE;
CREATE SCHEMA daily_test;
SET search_path TO daily_test;

CREATE TABLE instruments (id SERIAL PRIMARY KEY, symbol TEXT NOT NULL);
CREATE TABLE agent_outputs (
    id            BIGSERIAL PRIMARY KEY,
    agent         TEXT NOT NULL,
    instrument_id INT NOT NULL REFERENCES instruments(id),
    ts            TIMESTAMPTZ NOT NULL,
    signal        TEXT NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL DEFAULT 0,
    metrics       JSONB,
    rationale     TEXT
);
CREATE TABLE logic_version_windows (
    logic_version SMALLINT    PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL,
    note          TEXT
);
CREATE TABLE agent_outputs_daily (
    day            DATE          NOT NULL,
    agent          TEXT          NOT NULL,
    instrument_id  INTEGER       NOT NULL REFERENCES instruments(id),
    logic_version  SMALLINT      NOT NULL,
    n_total        INTEGER       NOT NULL,
    n_bullish      INTEGER       NOT NULL,
    n_bearish      INTEGER       NOT NULL,
    n_neutral      INTEGER       NOT NULL,
    conf_avg       NUMERIC(10,6) NOT NULL,
    conf_p50       NUMERIC(10,6) NOT NULL,
    conf_p90       NUMERIC(10,6) NOT NULL,
    repeat_rate    NUMERIC(5,4)  NOT NULL,
    PRIMARY KEY (day, agent, instrument_id, logic_version)
);
"""


@pytest.fixture
async def conn():
    import asyncpg

    connection = await asyncpg.connect(dsn=TEST_DSN)
    await connection.execute(_SCHEMA)
    await connection.execute("SET search_path TO daily_test;")
    yield connection
    await connection.execute("DROP SCHEMA IF EXISTS daily_test CASCADE;")
    await connection.close()


def _yesterday() -> datetime:
    """Начало вчерашних (завершённых) суток UTC."""
    return (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def _instrument(conn, symbol: str = "BTC/USDT") -> int:
    return await conn.fetchval(
        "INSERT INTO instruments (symbol) VALUES ($1) RETURNING id;", symbol
    )


async def _outputs(conn, instrument_id: int, rows, agent: str = "market") -> None:
    """rows: список (минута от начала суток, сигнал, уверенность)."""
    day = _yesterday()
    for minute, signal, confidence in rows:
        await conn.execute(
            "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) "
            "VALUES ($1, $2, $3, $4, $5);",
            agent, instrument_id, day + timedelta(minutes=minute), signal, confidence,
        )


async def _version(conn, version: int, started_at: datetime) -> None:
    await conn.execute(
        "INSERT INTO logic_version_windows (logic_version, started_at) "
        "VALUES ($1, $2);",
        version, started_at,
    )


# --- Точность ---------------------------------------------------------------


async def test_daily_counts_and_confidence_are_exact(conn) -> None:
    """Счётчики направлений и уверенность считаются точно."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.8), (1, "bullish", 0.6), (2, "bearish", 0.4),
        (3, "neutral", 0.2), (4, "insufficient_data", 0.0),
    ])
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow("SELECT * FROM agent_outputs_daily;")
    assert row["n_total"] == 5, "n_total считает ВСЕ выводы суток"
    assert (row["n_bullish"], row["n_bearish"], row["n_neutral"]) == (2, 1, 1)
    # insufficient_data не направление: сумма трёх меньше n_total — это норма.
    assert row["n_bullish"] + row["n_bearish"] + row["n_neutral"] < row["n_total"]
    assert row["conf_avg"] == Decimal("0.400000")   # (0.8+0.6+0.4+0.2+0.0)/5
    assert row["conf_p50"] == Decimal("0.400000")
    assert row["logic_version"] == 5


async def test_repeat_rate_counts_full_repeats(conn) -> None:
    """Повтор — совпадение И направления, И уверенности (Расчёт 4 из 7.1)."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    # 1-й: не с чем сравнивать; 2-й и 3-й — полные повторы; 4-й — та же
    # уверенность, но другое направление (НЕ повтор); 5-й — то же направление,
    # но другая уверенность (НЕ повтор).
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.5), (1, "bullish", 0.5), (2, "bullish", 0.5),
        (3, "bearish", 0.5), (4, "bearish", 0.7),
    ])
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow("SELECT * FROM agent_outputs_daily;")
    # Сравнимых пар 4, полных повторов 2 → 0.5.
    assert row["repeat_rate"] == Decimal("0.5000")


async def test_agents_and_instruments_are_separate(conn) -> None:
    """Свёртка раздельна по агенту и инструменту: пять токенов не смешиваются."""
    first = await _instrument(conn, "BTC/USDT")
    second = await _instrument(conn, "ETH/USDT")
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    await _outputs(conn, first, [(0, "bullish", 0.5)], agent="market")
    await _outputs(conn, first, [(0, "bearish", 0.9)], agent="futures")
    await _outputs(conn, second, [(0, "neutral", 0.1)], agent="market")
    await conn.execute(rollup_daily_sql())

    rows = await conn.fetch(
        "SELECT agent, instrument_id, n_total FROM agent_outputs_daily "
        "ORDER BY agent, instrument_id;"
    )
    assert [(r["agent"], r["instrument_id"], r["n_total"]) for r in rows] == [
        ("futures", first, 1), ("market", first, 1), ("market", second, 1),
    ]


# --- Границы версий ---------------------------------------------------------


async def test_version_boundary_inside_a_day_gives_two_rows(conn) -> None:
    """Сутки на границе версий дают ДВЕ строки, а не одну смешанную."""
    instrument_id = await _instrument(conn)
    day = _yesterday()
    await _version(conn, 4, day - timedelta(days=5))
    await _version(conn, 5, day + timedelta(hours=12))   # граница внутри суток
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.5), (60, "bullish", 0.5),           # до границы → v4
        (13 * 60, "bearish", 0.9), (14 * 60, "neutral", 0.1),  # после → v5
    ])
    await conn.execute(rollup_daily_sql())

    rows = await conn.fetch(
        "SELECT logic_version, n_total FROM agent_outputs_daily ORDER BY logic_version;"
    )
    assert [(r["logic_version"], r["n_total"]) for r in rows] == [(4, 2), (5, 2)], (
        "выводы двух версий слились в одну строку — смешивать версии запрещено"
    )


async def test_outputs_before_any_known_window_get_the_lowest_version(conn) -> None:
    """Вывод раньше самого раннего окна получает минимальную известную версию."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() + timedelta(hours=12))
    await _outputs(conn, instrument_id, [(0, "bullish", 0.5)])   # раньше окна
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow("SELECT logic_version FROM agent_outputs_daily;")
    assert row["logic_version"] == 5


# --- Завершённость суток, идемпотентность, удаление сырья -------------------


async def test_unfinished_day_is_not_rolled_up(conn) -> None:
    """Текущие сутки не сворачиваются: они ещё не закончились."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    now = datetime.now(UTC)
    await conn.execute(
        "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) "
        "VALUES ('market', $1, $2, 'bullish', 0.5);",
        instrument_id, now,
    )
    await conn.execute(rollup_daily_sql())
    assert await conn.fetchval(
        "SELECT count(*) FROM agent_outputs_daily WHERE day = $1;", now.date()
    ) == 0


async def test_daily_rollup_is_idempotent(conn) -> None:
    """Повторный запуск не создаёт дублей и не меняет значения."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.5), (1, "bearish", 0.7), (2, "bullish", 0.5),
    ])
    await conn.execute(rollup_daily_sql())
    first = [dict(r) for r in await conn.fetch(
        "SELECT * FROM agent_outputs_daily ORDER BY day, agent;"
    )]

    await conn.execute(rollup_daily_sql())
    await conn.execute(rollup_daily_sql())

    second = [dict(r) for r in await conn.fetch(
        "SELECT * FROM agent_outputs_daily ORDER BY day, agent;"
    )]
    assert first == second, "повторный запуск изменил итоги"
    assert len(second) == 1, "повторный запуск создал дубль"


async def test_deleting_raw_outputs_does_not_change_the_daily_rollup(conn) -> None:
    """Удаление журнала после свёртки не меняет суточные итоги."""
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.5), (1, "bullish", 0.5), (2, "neutral", 0.25),
    ])
    await conn.execute(rollup_daily_sql())
    before = dict(await conn.fetchrow("SELECT * FROM agent_outputs_daily;"))

    await conn.execute("DELETE FROM agent_outputs;")
    await conn.execute(rollup_daily_sql())

    after = dict(await conn.fetchrow("SELECT * FROM agent_outputs_daily;"))
    assert after == before


async def test_repeat_rate_compares_across_the_day_boundary(conn) -> None:
    """Первая запись суток сравнивается с последней записью предыдущих.

    Иначе каждый день терял бы одно сравнение, а при 1440 выводах в сутки это
    систематическое смещение доли повторов.
    """
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    previous_day = _yesterday() - timedelta(days=1)
    # Последний вывод позапрошлых суток и первый вывод вчерашних — одинаковые.
    await conn.execute(
        "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) "
        "VALUES ('market', $1, $2, 'bullish', 0.5);",
        instrument_id, previous_day + timedelta(hours=23, minutes=59),
    )
    await _outputs(conn, instrument_id, [(0, "bullish", 0.5), (1, "bearish", 0.3)])

    # Сворачиваем позапрошлые сутки, затем вчерашние (как это делает задача,
    # запускаемая ежесуточно).
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow(
        "SELECT * FROM agent_outputs_daily WHERE day = $1;", _yesterday().date()
    )
    # Во вчерашних сутках два вывода, оба сравнимы (первый — с записью
    # предыдущих суток), повтор один → 0.5.
    assert row["n_total"] == 2
    assert row["repeat_rate"] == Decimal("0.5000")


async def test_single_output_gives_zero_repeat_rate(conn) -> None:
    """Единственный вывод за сутки: сравнивать не с чем → доля повторов ноль.

    Колонка не допускает пустого значения, и «повторов не было» — верное
    утверждение для одной записи. Без этого свёртка падала бы на первой же
    редкой ситуации (агент выдал один вывод за сутки).
    """
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() - timedelta(days=10))
    await _outputs(conn, instrument_id, [(0, "bullish", 0.5)])
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow("SELECT * FROM agent_outputs_daily;")
    assert row["repeat_rate"] == Decimal("0.0000")
    assert row["n_total"] == 1
