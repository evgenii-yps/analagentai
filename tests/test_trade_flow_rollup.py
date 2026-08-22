"""Свёртка ленты сделок в поминутные итоги (решение по §4.3 отчёта 8.1).

Проверяется РОВНО тот SQL, который выполняет ежесуточная задача: функция
``scripts.retention.rollup_sql`` — единственный источник этого запроса, и тест
берёт его оттуда же, а не переписывает своими словами. Иначе тест проверял бы
свою копию логики, а в проде работала бы другая.

Тестам нужна настоящая PostgreSQL: свёртка — это агрегирующий SQL, и проверять
его подделкой бессмысленно. База берётся из ``AGENT_TEST_DSN`` (или
``BT_TEST_DSN``); без переменной тесты ПРОПУСКАЮТСЯ с явной причиной — они не
«зелёные», они не выполнялись.

    AGENT_TEST_DSN=postgresql://user:pass@host:5432/db python -m pytest \\
        tests/test_trade_flow_rollup.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scripts.retention import ROLLUP_LAG_MINUTES, rollup_sql

TEST_DSN = (
    os.environ.get("AGENT_TEST_DSN", "").strip()
    or os.environ.get("BT_TEST_DSN", "").strip()
)

requires_db = pytest.mark.skipif(
    not TEST_DSN,
    reason="нужна тестовая БД: задайте AGENT_TEST_DSN (свёртка проверяется в SQL)",
)

pytestmark = requires_db

# Схема, на которой работает свёртка. Создаётся во временной схеме теста, чтобы
# не трогать данные работающей системы.
_SCHEMA = """
DROP SCHEMA IF EXISTS rollup_test CASCADE;
CREATE SCHEMA rollup_test;
SET search_path TO rollup_test;

CREATE TABLE instruments (id SERIAL PRIMARY KEY, symbol TEXT NOT NULL);
CREATE TABLE trades (
    id            BIGSERIAL PRIMARY KEY,
    instrument_id INT NOT NULL REFERENCES instruments(id),
    trade_id      TEXT,
    ts            TIMESTAMPTZ NOT NULL,
    price         DOUBLE PRECISION NOT NULL,
    amount        DOUBLE PRECISION NOT NULL,
    side          TEXT,
    UNIQUE (instrument_id, trade_id)
);
CREATE TABLE trade_flow_1m (
    instrument_id INTEGER       NOT NULL REFERENCES instruments(id),
    ts            TIMESTAMPTZ   NOT NULL,
    trades_n      INTEGER       NOT NULL,
    buy_volume    NUMERIC(30,8) NOT NULL,
    sell_volume   NUMERIC(30,8) NOT NULL,
    buy_n         INTEGER       NOT NULL,
    sell_n        INTEGER       NOT NULL,
    vwap          NUMERIC(20,8) NOT NULL,
    PRIMARY KEY (instrument_id, ts)
);
"""


@pytest.fixture
async def conn():
    """Соединение с временной схемой и тестовыми данными."""
    import asyncpg

    connection = await asyncpg.connect(dsn=TEST_DSN)
    await connection.execute(_SCHEMA)
    await connection.execute("SET search_path TO rollup_test;")
    yield connection
    await connection.execute("DROP SCHEMA IF EXISTS rollup_test CASCADE;")
    await connection.close()


async def _seed(conn, minutes_ago: int, rows: list[tuple[float, float, str | None]]):
    """Кладёт сделки в минуту ``minutes_ago`` минут назад. Возвращает её начало."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (symbol) VALUES ('BTC/USDT') "
        "ON CONFLICT DO NOTHING RETURNING id;"
    )
    if instrument_id is None:
        instrument_id = await conn.fetchval("SELECT id FROM instruments LIMIT 1;")
    minute = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(
        second=0, microsecond=0
    )
    for index, (price, amount, side) in enumerate(rows):
        await conn.execute(
            "INSERT INTO trades (instrument_id, trade_id, ts, price, amount, side) "
            "VALUES ($1, $2, $3, $4, $5, $6);",
            instrument_id, f"{minutes_ago}-{index}",
            minute + timedelta(seconds=index % 60), price, amount, side,
        )
    return instrument_id, minute


# --- Точность свёртки -------------------------------------------------------


async def test_rollup_preserves_counts_and_volumes_exactly(conn) -> None:
    """§5: число сделок и суммарный объём за минуту сохраняются В ТОЧНОСТИ."""
    instrument_id, minute = await _seed(
        conn, minutes_ago=ROLLUP_LAG_MINUTES + 10,
        rows=[
            (100.0, 1.5, "buy"), (101.0, 2.25, "sell"), (99.5, 0.125, "buy"),
            (100.5, 3.0, "sell"), (100.25, 0.875, "buy"),
        ],
    )
    await conn.execute(rollup_sql())

    row = await conn.fetchrow(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    )
    assert row is not None, "минута не свёрнута"

    # Сравниваем с суммами по сырью, посчитанными тем же способом (numeric).
    raw = await conn.fetchrow(
        """
        SELECT count(*) AS n,
               sum(amount::numeric) AS volume,
               sum(amount::numeric) FILTER (WHERE side='buy')  AS buy_volume,
               sum(amount::numeric) FILTER (WHERE side='sell') AS sell_volume,
               count(*) FILTER (WHERE side='buy')  AS buy_n,
               count(*) FILTER (WHERE side='sell') AS sell_n,
               sum(price::numeric * amount::numeric) / sum(amount::numeric) AS vwap
          FROM trades WHERE instrument_id=$1 AND date_trunc('minute', ts)=$2;
        """,
        instrument_id, minute,
    )
    assert row["trades_n"] == raw["n"] == 5
    assert row["buy_n"] == raw["buy_n"]
    assert row["sell_n"] == raw["sell_n"]
    assert row["buy_volume"] == raw["buy_volume"]
    assert row["sell_volume"] == raw["sell_volume"]
    # Суммарный объём сохранён целиком: у всех сделок известна сторона.
    assert row["buy_volume"] + row["sell_volume"] == raw["volume"]
    assert row["vwap"] == round(raw["vwap"], 8)


async def test_trades_without_side_are_counted_but_not_attributed(conn) -> None:
    """Сделка без стороны попадает в счётчик и VWAP, но не в объёмы по сторонам.

    Приписать её произвольной стороне значило бы выдумать данные.
    """
    instrument_id, minute = await _seed(
        conn, minutes_ago=ROLLUP_LAG_MINUTES + 10,
        rows=[(100.0, 1.0, "buy"), (100.0, 2.0, None)],
    )
    await conn.execute(rollup_sql())
    row = await conn.fetchrow(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    )
    assert row["trades_n"] == 2
    assert row["buy_n"] == 1 and row["sell_n"] == 0
    assert row["buy_volume"] == Decimal("1.00000000")
    assert row["sell_volume"] == Decimal("0.00000000")


# --- Завершённость минуты ---------------------------------------------------


async def test_unfinished_minute_is_not_rolled_up(conn) -> None:
    """§2 решения: незавершённая минута не сворачивается."""
    instrument_id, minute = await _seed(
        conn, minutes_ago=0, rows=[(100.0, 1.0, "buy")]
    )
    await conn.execute(rollup_sql())
    assert await conn.fetchval(
        "SELECT count(*) FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    ) == 0


async def test_minute_inside_the_lag_is_not_rolled_up(conn) -> None:
    """Только что закончившаяся минута тоже ждёт: её хвост может быть не записан."""
    instrument_id, minute = await _seed(
        conn, minutes_ago=max(ROLLUP_LAG_MINUTES - 2, 1),
        rows=[(100.0, 1.0, "buy")],
    )
    await conn.execute(rollup_sql())
    assert await conn.fetchval(
        "SELECT count(*) FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    ) == 0


# --- Идемпотентность и удаление сырья ---------------------------------------


async def test_rollup_is_idempotent(conn) -> None:
    """§2 решения: повторный запуск не создаёт дублей и не меняет значения."""
    instrument_id, minute = await _seed(
        conn, minutes_ago=ROLLUP_LAG_MINUTES + 10,
        rows=[(100.0, 1.0, "buy"), (101.0, 2.0, "sell")],
    )
    await conn.execute(rollup_sql())
    first = dict(await conn.fetchrow(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    ))

    await conn.execute(rollup_sql())
    await conn.execute(rollup_sql())

    rows = await conn.fetch(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    )
    assert len(rows) == 1, "повторный запуск создал дубль"
    assert dict(rows[0]) == first, "повторный запуск изменил значения"


async def test_deleting_raw_trades_does_not_change_the_rollup(conn) -> None:
    """§5: удаление сырья после свёртки не меняет trade_flow_1m."""
    instrument_id, minute = await _seed(
        conn, minutes_ago=ROLLUP_LAG_MINUTES + 10,
        rows=[(100.0, 1.5, "buy"), (102.0, 2.5, "sell"), (101.0, 1.0, "buy")],
    )
    await conn.execute(rollup_sql())
    before = dict(await conn.fetchrow(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    ))

    deleted = await conn.execute("DELETE FROM trades;")
    assert deleted.startswith("DELETE")
    assert await conn.fetchval("SELECT count(*) FROM trades;") == 0

    # И сама свёртка после удаления сырья ничего не портит: пересчитывать
    # нечего, а записанный итог остаётся прежним.
    await conn.execute(rollup_sql())
    after = dict(await conn.fetchrow(
        "SELECT * FROM trade_flow_1m WHERE instrument_id=$1 AND ts=$2;",
        instrument_id, minute,
    ))
    assert after == before


async def test_each_instrument_is_rolled_up_separately(conn) -> None:
    """Итоги считаются по инструментам раздельно: пять токенов не смешиваются."""
    first_id, minute = await _seed(
        conn, minutes_ago=ROLLUP_LAG_MINUTES + 10, rows=[(100.0, 1.0, "buy")]
    )
    second_id = await conn.fetchval(
        "INSERT INTO instruments (symbol) VALUES ('ETH/USDT') RETURNING id;"
    )
    await conn.execute(
        "INSERT INTO trades (instrument_id, trade_id, ts, price, amount, side) "
        "VALUES ($1, 'eth-1', $2, 50.0, 4.0, 'sell');",
        second_id, minute + timedelta(seconds=5),
    )
    await conn.execute(rollup_sql())

    rows = {
        r["instrument_id"]: dict(r)
        for r in await conn.fetch(
            "SELECT * FROM trade_flow_1m WHERE ts=$1;", minute
        )
    }
    assert set(rows) == {first_id, second_id}
    assert rows[first_id]["trades_n"] == 1
    assert rows[second_id]["sell_volume"] == Decimal("4.00000000")
