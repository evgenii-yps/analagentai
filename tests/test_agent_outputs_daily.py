"""Суточная свёртка выводов агентов (решение заказчика по §4.3, пункт 1).

Проверяется РОВНО тот SQL, который выполняет ежесуточная задача:
``scripts.retention.rollup_daily_sql`` — единственный его источник.

Тестам нужна настоящая PostgreSQL (свёртка — агрегирующий SQL с оконными
функциями и перцентилями). База берётся из ``AGENT_TEST_DSN``; без переменной
тесты ПРОПУСКАЮТСЯ с явной причиной.
"""

from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scripts.retention import UNKNOWN_LOGIC_VERSION, rollup_daily_sql

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


async def test_outputs_before_any_known_window_get_unknown_version(conn) -> None:
    """Вывод раньше самой ранней границы получает признак «неизвестно», а не версию.

    Тест на дефект, найденный на сервере 22.08.2026: свёртка подставляла таким
    выводам минимальную известную версию, и 33 895 выводов версий 1-3 были
    записаны как версия 4. agent_outputs_daily не удаляется никогда, сырьё
    живёт 90 суток — проверить утверждение стало бы нечем.
    """
    instrument_id = await _instrument(conn)
    await _version(conn, 5, _yesterday() + timedelta(hours=12))
    await _outputs(conn, instrument_id, [(0, "bullish", 0.5)])   # раньше окна
    await conn.execute(rollup_daily_sql())

    row = await conn.fetchrow("SELECT logic_version FROM agent_outputs_daily;")
    assert row["logic_version"] == UNKNOWN_LOGIC_VERSION, (
        "выводу раньше самой ранней известной границы подставлена реальная "
        "версия — это суррогатные данные вместо честного «неизвестно»"
    )


async def test_first_known_window_inside_a_day_splits_unknown_from_version(conn) -> None:
    """Сутки, на которые пришлась ПЕРВАЯ известная граница, дают две строки."""
    instrument_id = await _instrument(conn)
    day = _yesterday()
    await _version(conn, 4, day + timedelta(hours=12))   # самая ранняя граница
    await _outputs(conn, instrument_id, [
        (0, "bullish", 0.5), (60, "bullish", 0.5),             # раньше → неизвестно
        (13 * 60, "bearish", 0.9), (14 * 60, "neutral", 0.1),  # позже  → версия 4
    ])
    await conn.execute(rollup_daily_sql())

    rows = await conn.fetch(
        "SELECT logic_version, n_total FROM agent_outputs_daily ORDER BY logic_version;"
    )
    assert [(r["logic_version"], r["n_total"]) for r in rows] == [
        (UNKNOWN_LOGIC_VERSION, 2), (4, 2),
    ], "неизвестный период слился с версией 4 в одну строку"


async def test_outputs_after_the_last_known_window_get_the_last_version(conn) -> None:
    """Вывод ПОЗЖЕ последней границы получает последнюю версию — и это верно.

    Случай не симметричен предыдущему. У последнего окна нет конца: оно
    действует до следующей границы, поэтому версия здесь ИЗВЕСТНА — та, что
    работает сейчас. Незнание было только «слева», до первой записанной
    границы.
    """
    instrument_id = await _instrument(conn)
    day = _yesterday()
    await _version(conn, 4, day - timedelta(days=10))
    await _version(conn, 5, day - timedelta(days=2))   # последняя известная
    await _outputs(conn, instrument_id, [(0, "bullish", 0.5), (60, "bearish", 0.7)])
    await conn.execute(rollup_daily_sql())

    rows = await conn.fetch("SELECT logic_version, n_total FROM agent_outputs_daily;")
    assert [(r["logic_version"], r["n_total"]) for r in rows] == [(5, 2)], (
        "вывод позже последней границы обязан получать последнюю версию"
    )


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


# --- Исправление уже записанного: миграция 012 ------------------------------
#
# Дефект найден на сервере 22.08.2026: свёртка подставила выводам версий 1-3
# минимальную известную версию 4 — 42 строки в вечной таблице. Миграция 012
# переводит их в «неизвестно», а сутки границы пересчитывает ТЕМ ЖЕ SQL, что
# и ежесуточная задача: второй реализации расчёта в проекте нет.

_MIGRATION_012 = (
    pathlib.Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "012_unknown_logic_version.sql"
)


def _migration_012_sql() -> str:
    return _MIGRATION_012.read_text(encoding="utf-8")


def _day(offset: int) -> datetime:
    """Начало суток UTC, отстоящих от вчерашних на ``offset`` суток назад."""
    return _yesterday() - timedelta(days=offset)


async def _outputs_on(conn, instrument_id: int, day: datetime, rows,
                      agent: str = "market") -> None:
    """rows: список (минута от начала суток, сигнал, уверенность)."""
    for minute, signal, confidence in rows:
        await conn.execute(
            "INSERT INTO agent_outputs (agent, instrument_id, ts, signal, confidence) "
            "VALUES ($1, $2, $3, $4, $5);",
            agent, instrument_id, day + timedelta(minutes=minute), signal, confidence,
        )


async def _daily_row(conn, day: datetime, instrument_id: int, version: int,
                     n_total: int, agent: str = "market") -> None:
    """Строка итогов, записанная СТАРЫМ (неверным) способом — для проверки правки."""
    await conn.execute(
        "INSERT INTO agent_outputs_daily "
        "(day, agent, instrument_id, logic_version, n_total, n_bullish, n_bearish, "
        " n_neutral, conf_avg, conf_p50, conf_p90, repeat_rate) "
        "VALUES ($1::date, $2, $3, $4, $5, $5, 0, 0, 0.5, 0.5, 0.5, 0);",
        day.date(), agent, instrument_id, version, n_total,
    )


async def test_migration_012_moves_whole_days_to_unknown(conn) -> None:
    """Сутки целиком раньше границы переводятся в «неизвестно» без сырья."""
    instrument_id = await _instrument(conn)
    await _version(conn, 4, _day(0) + timedelta(hours=12))
    await _daily_row(conn, _day(3), instrument_id, 4, 100)
    await _daily_row(conn, _day(2), instrument_id, 4, 200)

    await conn.execute(_migration_012_sql())

    rows = await conn.fetch(
        "SELECT day, logic_version, n_total FROM agent_outputs_daily ORDER BY day;"
    )
    assert [(r["logic_version"], r["n_total"]) for r in rows] == [
        (UNKNOWN_LOGIC_VERSION, 100), (UNKNOWN_LOGIC_VERSION, 200),
    ], "строки за период раньше границы остались с подставленной версией"


async def test_migration_012_splits_the_boundary_day_through_the_rollup(conn) -> None:
    """Сутки границы разделяются на две строки — пересчётом из живого сырья."""
    instrument_id = await _instrument(conn)
    boundary_day, last_day = _day(1), _day(0)
    await _version(conn, 4, boundary_day + timedelta(hours=12))
    # Сырьё: двое суток; в первых — граница внутри суток.
    await _outputs_on(conn, instrument_id, boundary_day, [
        (0, "bullish", 0.5), (60, "bullish", 0.5),             # раньше границы
        (13 * 60, "bearish", 0.9), (14 * 60, "neutral", 0.1),  # позже границы
    ])
    await _outputs_on(conn, instrument_id, last_day, [
        (0, "bullish", 0.3), (60, "bearish", 0.4),
    ])
    # Итоги, записанные старым способом: сутки границы одной смешанной строкой.
    await _daily_row(conn, boundary_day, instrument_id, 4, 4)
    await _daily_row(conn, last_day, instrument_id, 4, 2)

    await conn.execute(_migration_012_sql())
    await conn.execute(rollup_daily_sql())

    rows = await conn.fetch(
        "SELECT day, logic_version, n_total FROM agent_outputs_daily "
        " ORDER BY day, logic_version;"
    )
    assert [(r["day"], r["logic_version"], r["n_total"]) for r in rows] == [
        (boundary_day.date(), UNKNOWN_LOGIC_VERSION, 2),
        (boundary_day.date(), 4, 2),
        (last_day.date(), 4, 2),
    ], "сутки границы не разделились на «неизвестно» и версию 4"


async def test_migration_012_is_idempotent(conn) -> None:
    """Повторное применение миграции и свёртки ничего не меняет."""
    instrument_id = await _instrument(conn)
    boundary_day, last_day = _day(1), _day(0)
    await _version(conn, 4, boundary_day + timedelta(hours=12))
    await _outputs_on(conn, instrument_id, boundary_day, [
        (0, "bullish", 0.5), (13 * 60, "bearish", 0.9),
    ])
    await _outputs_on(conn, instrument_id, last_day, [(0, "bullish", 0.3)])
    await _daily_row(conn, boundary_day, instrument_id, 4, 2)
    await _daily_row(conn, last_day, instrument_id, 4, 1)

    await conn.execute(_migration_012_sql())
    await conn.execute(rollup_daily_sql())
    first = await conn.fetch(
        "SELECT day, logic_version, n_total, conf_avg FROM agent_outputs_daily "
        " ORDER BY day, logic_version;"
    )
    await conn.execute(_migration_012_sql())
    await conn.execute(rollup_daily_sql())
    second = await conn.fetch(
        "SELECT day, logic_version, n_total, conf_avg FROM agent_outputs_daily "
        " ORDER BY day, logic_version;"
    )
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


async def test_migration_012_keeps_boundary_rows_when_raw_is_gone(conn) -> None:
    """Без сырья сутки границы не трогаются: потерять строку хуже, чем оставить."""
    instrument_id = await _instrument(conn)
    boundary_day = _day(1)
    await _version(conn, 4, boundary_day + timedelta(hours=12))
    await _daily_row(conn, boundary_day, instrument_id, 4, 4)   # сырья нет вовсе

    await conn.execute(_migration_012_sql())

    rows = await conn.fetch("SELECT logic_version, n_total FROM agent_outputs_daily;")
    assert [(r["logic_version"], r["n_total"]) for r in rows] == [(4, 4)], (
        "строка суток границы удалена при отсутствии сырья — данные потеряны"
    )


async def test_zero_cannot_be_a_real_logic_version(conn) -> None:
    """После миграции ноль в logic_version_windows невозможен по построению."""
    import asyncpg

    await conn.execute(_migration_012_sql())
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "INSERT INTO logic_version_windows (logic_version, started_at) "
            "VALUES (0, now());"
        )


# --- Дыра в итогах ----------------------------------------------------------


async def test_rollup_fills_a_hole_in_daily_rows(conn) -> None:
    """Сутки, по которым сырьё есть, а итогов нет, досчитываются, а не теряются."""
    instrument_id = await _instrument(conn)
    hole_day, last_day = _day(1), _day(0)
    await _version(conn, 5, _day(9))
    await _outputs_on(conn, instrument_id, hole_day, [(0, "bullish", 0.5)])
    await _outputs_on(conn, instrument_id, last_day, [(0, "bearish", 0.7)])
    await _daily_row(conn, last_day, instrument_id, 5, 1)   # итоги только за последние сутки

    await conn.execute(rollup_daily_sql())

    days = [r["day"] for r in await conn.fetch(
        "SELECT day FROM agent_outputs_daily ORDER BY day;"
    )]
    assert days == [hole_day.date(), last_day.date()], (
        "дыра в итогах не закрылась — счёт пошёл со следующих суток после последних"
    )


async def test_a_day_without_raw_is_not_a_hole(conn) -> None:
    """Сутки простоя итогов не имеют законно и счёт на себе не держат."""
    instrument_id = await _instrument(conn)
    idle_day, last_day = _day(1), _day(0)
    await _version(conn, 5, _day(9))
    await _outputs_on(conn, instrument_id, _day(2), [(0, "bullish", 0.5)])
    # За idle_day сырья нет вовсе — система не работала.
    await _outputs_on(conn, instrument_id, last_day, [(0, "bearish", 0.7)])
    await _daily_row(conn, _day(2), instrument_id, 5, 1)
    await _daily_row(conn, last_day, instrument_id, 5, 1)

    await conn.execute(rollup_daily_sql())

    days = [r["day"] for r in await conn.fetch(
        "SELECT day FROM agent_outputs_daily ORDER BY day;"
    )]
    assert idle_day.date() not in days
    assert days == [_day(2).date(), last_day.date()]
