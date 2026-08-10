"""SQL-запросы выгрузки: выборка сигналов, агрегаты, учёт выгруженного.

Функции принимают соединение/пул asyncpg и возвращают простые словари, чтобы
оставаться независимыми от остального слоя доступа к БД (скрипт работает на
хосте по 127.0.0.1 и держит собственный пул).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

# Общий набор колонок сигнала с приджойненными оценками горизонтов 1h/4h.
# agents_payload берём как текст (::text), чтобы сохранить исходный JSON дословно.
_SIGNAL_COLUMNS = """
    s.id,
    s.ts,
    s.decision,
    s.probability,
    s.status,
    s.rationale,
    s.notified_at,
    s.agents_payload::text AS agents_payload,
    i.base AS token,
    e1.price_at_signal AS p_signal_1h,
    e1.price_at_close  AS p_close_1h,
    e1.pnl_pct         AS pnl_1h,
    e1.drawdown_pct    AS dd_1h,
    e1.success         AS succ_1h,
    e4.price_at_signal AS p_signal_4h,
    e4.price_at_close  AS p_close_4h,
    e4.pnl_pct         AS pnl_4h,
    e4.drawdown_pct    AS dd_4h,
    e4.success         AS succ_4h
"""

_SIGNAL_JOINS = """
    FROM signals s
    JOIN instruments i ON i.id = s.instrument_id
    LEFT JOIN signal_evaluations e1 ON e1.signal_id = s.id AND e1.horizon = '1h'
    LEFT JOIN signal_evaluations e4 ON e4.signal_id = s.id AND e4.horizon = '4h'
"""

# Выражение начала 4-часового окна (совпадает с transform.window_4h_start).
_WINDOW_EXPR = (
    "date_trunc('hour', s.ts) - (extract(hour from s.ts)::int % 4) * interval '1 hour'"
)


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Идемпотентно применяет схему §4 (таблица учёта + колонка notified_at)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_exports (
            id          BIGSERIAL PRIMARY KEY,
            signal_id   BIGINT NOT NULL REFERENCES signals(id),
            target      TEXT NOT NULL,
            exported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (signal_id, target)
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_exports_target "
        "ON signal_exports (target, signal_id);"
    )
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;"
    )


async def fetch_unexported_for_sheets(
    conn: asyncpg.Connection,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Закрытые сигналы без отметки target='sheets', ts ASC, не более batch_size."""
    query = f"""
        SELECT {_SIGNAL_COLUMNS}
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
          AND NOT EXISTS (
              SELECT 1 FROM signal_exports x
              WHERE x.signal_id = s.id AND x.target = 'sheets'
          )
        ORDER BY s.ts ASC
        LIMIT $1;
    """
    rows = await conn.fetch(query, batch_size)
    return [dict(r) for r in rows]


async def fetch_independent_windows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """По одному (самому раннему) закрытому сигналу на каждое 4-часовое окно."""
    query = f"""
        SELECT DISTINCT ON (win) {_SIGNAL_COLUMNS},
               {_WINDOW_EXPR} AS win
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
        ORDER BY win, s.ts ASC;
    """
    rows = await conn.fetch(query)
    return [dict(r) for r in rows]


async def fetch_notion_pending(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Закрытые сигналы с notified_at, без отметки target='notion', ts ASC."""
    query = f"""
        SELECT {_SIGNAL_COLUMNS}
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
          AND s.notified_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM signal_exports x
              WHERE x.signal_id = s.id AND x.target = 'notion'
          )
        ORDER BY s.ts ASC;
    """
    rows = await conn.fetch(query)
    return [dict(r) for r in rows]


async def fetch_daily_summary(
    conn: asyncpg.Connection,
    min_probability: float,
) -> list[dict[str, Any]]:
    """Агрегаты по календарным суткам UTC для листа «Сводка по дням»."""
    query = """
        SELECT
            (s.ts AT TIME ZONE 'UTC')::date AS day,
            count(*) AS decisions_total,
            count(*) FILTER (WHERE s.decision = 'buy')  AS buy,
            count(*) FILTER (WHERE s.decision = 'sell') AS sell,
            count(*) FILTER (WHERE s.decision = 'wait') AS wait,
            count(*) FILTER (WHERE s.probability >= $1) AS candidates,
            count(*) FILTER (WHERE s.notified_at IS NOT NULL) AS notified,
            count(*) FILTER (WHERE s.status = 'closed') AS closed_4h,
            avg(CASE WHEN s.decision = 'buy'  AND e4.success IS NOT NULL
                     THEN e4.success::int END) AS sr_buy,
            avg(CASE WHEN s.decision = 'sell' AND e4.success IS NOT NULL
                     THEN e4.success::int END) AS sr_sell,
            avg(CASE WHEN s.decision = 'buy'  THEN e4.pnl_pct END) AS avg_pnl_buy,
            avg(CASE WHEN s.decision = 'sell' THEN e4.pnl_pct END) AS avg_pnl_sell,
            avg(e4.drawdown_pct) AS avg_dd,
            avg(s.probability)   AS avg_prob
        FROM signals s
        LEFT JOIN signal_evaluations e4
               ON e4.signal_id = s.id AND e4.horizon = '4h'
        GROUP BY day
        ORDER BY day ASC;
    """
    rows = await conn.fetch(query, float(min_probability))
    return [dict(r) for r in rows]


async def mark_exported(
    conn: asyncpg.Connection,
    signal_ids: list[int],
    target: str,
) -> None:
    """Ставит отметки о выгрузке (идемпотентно, ON CONFLICT DO NOTHING)."""
    if not signal_ids:
        return
    await conn.executemany(
        "INSERT INTO signal_exports (signal_id, target) VALUES ($1, $2) "
        "ON CONFLICT (signal_id, target) DO NOTHING;",
        [(sid, target) for sid in signal_ids],
    )


async def resolve_reliable_since(conn: asyncpg.Connection) -> datetime | None:
    """Момент, с которого notified_at достоверен: минимальный непустой notified_at."""
    return await conn.fetchval("SELECT min(notified_at) FROM signals;")


async def count_unexported(conn: asyncpg.Connection, target: str) -> int:
    """Сколько закрытых сигналов ещё не выгружено в указанную цель."""
    if target == "notion":
        query = """
            SELECT count(*) FROM signals s
            WHERE s.status = 'closed' AND s.notified_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM signal_exports x
                  WHERE x.signal_id = s.id AND x.target = 'notion'
              );
        """
    else:
        query = """
            SELECT count(*) FROM signals s
            WHERE s.status = 'closed'
              AND NOT EXISTS (
                  SELECT 1 FROM signal_exports x
                  WHERE x.signal_id = s.id AND x.target = 'sheets'
              );
        """
    return int(await conn.fetchval(query) or 0)


async def count_exported(conn: asyncpg.Connection, target: str) -> int:
    """Сколько строк учтено в signal_exports по цели (для сверки в отчёте)."""
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM signal_exports WHERE target = $1;", target
        )
        or 0
    )
