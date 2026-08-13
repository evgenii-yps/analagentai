"""SELECT-запросы бота и идемпотентное создание роли только на чтение.

Сервис бота подключается к БД ролью ``agenttrade_ro`` (права SELECT), поэтому все
запросы здесь — исключительно чтение. Даже при ошибке в коде бот не сможет
испортить данные (ТЗ §3.5, §8).

Границу 4-часового окна считаем ТЕМ ЖЕ выражением, что и выгрузка 6.6
(src.export.queries) и §7.1 ТЗ 6.6 — чтобы «честная выборка» совпадала.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog

_log = structlog.get_logger().bind(component="bot-queries")

# Выражение начала непересекающегося 4-часового окна UTC.
_WINDOW_EXPR = (
    "date_trunc('hour', s.ts) - (extract(hour from s.ts)::int % 4) * interval '1 hour'"
)

# Потоки данных для /summary (те же таблицы, что в daily_report.py — §5).
DATA_STREAMS: list[tuple[str, str]] = [
    ("OHLCV", "ohlcv"),
    ("Сделки (trades)", "trades"),
    ("Стакан (orderbook)", "orderbook_snapshots"),
    ("Funding", "funding"),
    ("Open interest", "open_interest"),
    ("Выводы агентов", "agent_outputs"),
]

# Секунды в периоде /stats. 0 — «за всё время» (без ограничения по ts).
PERIOD_SECONDS: dict[str, int] = {
    "24h": 86_400,
    "7d": 604_800,
    "30d": 2_592_000,
    "all": 0,
}


async def ensure_readonly_role(
    conn: asyncpg.Connection,
    ro_password: str,
    db_name: str,
) -> None:
    """Идемпотентно создаёт роль ``agenttrade_ro`` (SELECT-only) и выдаёт права.

    Пароль подставляется через ``quote_literal`` (%L) на стороне PostgreSQL —
    без ручной сборки строки. Пароль синхронизируется с .env при каждом старте
    (ALTER), чтобы вход роли работал даже после ротации пароля установщиком.
    Соединение ``conn`` — под основным пользователем (владельцем БД), только для
    этого одноразового действия; запросы бота идут уже под ролью ro.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro';"
    )
    if not exists:
        stmt = await conn.fetchval(
            "SELECT format('CREATE ROLE agenttrade_ro LOGIN PASSWORD %L', $1::text);",
            ro_password,
        )
        await conn.execute(stmt)
        _log.info("Роль agenttrade_ro создана")
    else:
        stmt = await conn.fetchval(
            "SELECT format('ALTER ROLE agenttrade_ro LOGIN PASSWORD %L', $1::text);",
            ro_password,
        )
        await conn.execute(stmt)

    grant_db = await conn.fetchval(
        "SELECT format('GRANT CONNECT ON DATABASE %I TO agenttrade_ro', $1::text);",
        db_name,
    )
    await conn.execute(grant_db)
    await conn.execute("GRANT USAGE ON SCHEMA public TO agenttrade_ro;")
    await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO agenttrade_ro;")
    await conn.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT ON TABLES TO agenttrade_ro;"
    )
    _log.info("Права SELECT для agenttrade_ro выданы")


class BotQueries:
    """Обёртка над RO-пулом asyncpg: только SELECT-запросы для команд бота."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # --- /status ---

    async def status_facts(self) -> dict[str, Any]:
        """Свежесть данных и счётчики сигналов для /status."""
        row = await self._pool.fetchrow(
            """
            SELECT
                (SELECT max(ts) FROM ohlcv)                               AS last_ohlcv_ts,
                (SELECT max(ts) FROM orderbook_snapshots)                 AS last_orderbook_ts,
                (SELECT max(ts) FROM signals)                             AS last_signal_ts,
                (SELECT count(*) FROM signals WHERE status = 'open')      AS open_count,
                (SELECT count(*) FROM signals WHERE status = 'closed')    AS closed_count;
            """
        )
        return dict(row) if row else {}

    # --- /last ---

    async def last_signals(self, notified_only: bool, limit: int) -> list[dict[str, Any]]:
        """Последние сигналы. notified_only → только реально отправленные (§5)."""
        rows = await self._pool.fetch(
            """
            SELECT id, ts, decision, probability, status,
                   pnl_pct, drawdown_pct, success
            FROM signals
            WHERE (NOT $1 OR notified_at IS NOT NULL)
            ORDER BY ts DESC
            LIMIT $2;
            """,
            notified_only,
            limit,
        )
        return [dict(r) for r in rows]

    # --- /signal <id> ---

    async def signal_card(self, signal_id: int) -> dict[str, Any] | None:
        """Полная карточка сигнала: поля, оценки 1ч/4ч и цена на момент сигнала."""
        row = await self._pool.fetchrow(
            """
            SELECT id, instrument_id, ts, decision, probability, rationale,
                   notified, notified_at, status,
                   agents_payload::text AS agents_payload
            FROM signals WHERE id = $1;
            """,
            signal_id,
        )
        if row is None:
            return None
        card = dict(row)

        evals = await self._pool.fetch(
            """
            SELECT horizon, price_at_close, pnl_pct, drawdown_pct, success
            FROM signal_evaluations WHERE signal_id = $1;
            """,
            signal_id,
        )
        by_horizon = {e["horizon"]: dict(e) for e in evals}
        card["eval_1h"] = by_horizon.get("1h")
        card["eval_4h"] = by_horizon.get("4h")

        # Цена на момент сигнала — close ближайшей 1m-свечи на/до ts.
        card["price_at_signal"] = await self._pool.fetchval(
            """
            SELECT close FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = '1m' AND ts <= $2
            ORDER BY ts DESC LIMIT 1;
            """,
            card["instrument_id"],
            card["ts"],
        )
        return card

    # --- /agents ---

    async def latest_agents(self, agents: list[str]) -> dict[str, dict[str, Any] | None]:
        """Последний вывод каждого агента (по всем инструментам берём самый свежий)."""
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (agent) agent, signal, confidence, ts
            FROM agent_outputs
            WHERE agent = ANY($1::text[])
            ORDER BY agent, ts DESC;
            """,
            agents,
        )
        by_agent = {r["agent"]: dict(r) for r in rows}
        return {name: by_agent.get(name) for name in agents}

    # --- /stats ---

    async def stats_versions(self, period_sec: int) -> list[int]:
        """Версии логики (logic_version), встречающиеся у закрытых сигналов периода.

        Нужно, чтобы /stats считал статистику по ОДНОЙ версии (§D.4): смешивать
        сигналы «до» и «после» правок Этапа 7.0 статистически некорректно.
        """
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT logic_version FROM signals
            WHERE status = 'closed'
              AND ($1 = 0 OR ts > now() - $1 * interval '1 second')
            ORDER BY logic_version;
            """,
            period_sec,
        )
        return [int(r["logic_version"]) for r in rows]

    async def stats_block(
        self,
        period_sec: int,
        independent: bool,
        logic_version: int | None,
    ) -> dict[str, Any]:
        """Агрегаты по закрытым сигналам за период И одну версию логики.

        ``independent=True`` — по одному (самому раннему) сигналу на каждое
        4-часовое окно (честная выборка). Иначе — по всей массе подряд.
        ``logic_version=None`` → без фильтра версии (когда закрытых сигналов нет).
        """
        if independent:
            query = f"""
                WITH windowed AS (
                    SELECT DISTINCT ON (win) s.decision,
                           e4.success AS success, e4.pnl_pct AS pnl,
                           e4.drawdown_pct AS dd, {_WINDOW_EXPR} AS win
                    FROM signals s
                    LEFT JOIN signal_evaluations e4
                           ON e4.signal_id = s.id AND e4.horizon = '4h'
                    WHERE s.status = 'closed'
                      AND ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
                      AND ($2::smallint IS NULL OR s.logic_version = $2)
                    ORDER BY win, s.ts ASC
                )
                SELECT
                    count(*) AS n,
                    count(*) FILTER (WHERE decision = 'buy')  AS buy,
                    count(*) FILTER (WHERE decision = 'sell') AS sell,
                    count(*) FILTER (WHERE decision = 'wait') AS wait,
                    avg(CASE WHEN decision = 'buy'  AND success IS NOT NULL
                             THEN success::int END) AS sr_buy,
                    avg(CASE WHEN decision = 'sell' AND success IS NOT NULL
                             THEN success::int END) AS sr_sell,
                    avg(pnl) AS avg_pnl,
                    avg(dd)  AS avg_dd
                FROM windowed;
            """
        else:
            query = """
                SELECT
                    count(*) AS n,
                    count(*) FILTER (WHERE s.decision = 'buy')  AS buy,
                    count(*) FILTER (WHERE s.decision = 'sell') AS sell,
                    count(*) FILTER (WHERE s.decision = 'wait') AS wait,
                    avg(CASE WHEN s.decision = 'buy'  AND e4.success IS NOT NULL
                             THEN e4.success::int END) AS sr_buy,
                    avg(CASE WHEN s.decision = 'sell' AND e4.success IS NOT NULL
                             THEN e4.success::int END) AS sr_sell,
                    avg(e4.pnl_pct)      AS avg_pnl,
                    avg(e4.drawdown_pct) AS avg_dd
                FROM signals s
                LEFT JOIN signal_evaluations e4
                       ON e4.signal_id = s.id AND e4.horizon = '4h'
                WHERE s.status = 'closed'
                  AND ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
                  AND ($2::smallint IS NULL OR s.logic_version = $2);
            """
        row = await self._pool.fetchrow(query, period_sec, logic_version)
        return dict(row) if row else {}

    async def notify_filter_counts(
        self,
        period_sec: int,
        logic_version: int | None,
    ) -> dict[str, Any]:
        """Блок 5 /stats: сколько отправлено и поглощено за период (одна версия)."""
        row = await self._pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE notified_at IS NOT NULL) AS sent,
                count(*) FILTER (WHERE notified AND notified_at IS NULL) AS absorbed
            FROM signals s
            WHERE ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
              AND ($2::smallint IS NULL OR s.logic_version = $2);
            """,
            period_sec,
            logic_version,
        )
        return dict(row) if row else {"sent": 0, "absorbed": 0}

    # --- /summary ---

    async def data_counts_24h(self) -> list[tuple[str, int]]:
        """Приток данных за 24 часа по таблицам (§5, свои запросы)."""
        result: list[tuple[str, int]] = []
        for label, table in DATA_STREAMS:
            # Имя таблицы — из фиксированного белого списка DATA_STREAMS, не из ввода.
            count = await self._pool.fetchval(
                f"SELECT count(*) FROM {table} WHERE ts > now() - interval '24 hours';"
            )
            result.append((label, int(count or 0)))
        return result

    async def signal_counts_24h(
        self,
        min_probability: float,
        primary_horizon: str,
    ) -> dict[str, Any]:
        """Сигналы за 24 часа: раскладка, отправлено/поглощено, кандидаты, закрыто."""
        grp = await self._pool.fetch(
            """
            SELECT decision, count(*) AS n FROM signals
            WHERE ts > now() - interval '24 hours'
            GROUP BY decision ORDER BY decision;
            """
        )
        by_decision = {r["decision"]: int(r["n"]) for r in grp}

        row = await self._pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE notified_at IS NOT NULL) AS sent,
                count(*) FILTER (WHERE notified AND notified_at IS NULL) AS absorbed,
                count(*) FILTER (
                    WHERE decision <> 'wait' AND probability >= $1
                ) AS candidates
            FROM signals
            WHERE ts > now() - interval '24 hours';
            """,
            float(min_probability),
        )
        closed = await self._pool.fetchval(
            """
            SELECT count(*) FROM signal_evaluations
            WHERE horizon = $1 AND evaluated_at > now() - interval '24 hours';
            """,
            primary_horizon,
        )
        return {
            "by_decision": by_decision,
            "sent": int(row["sent"]) if row else 0,
            "absorbed": int(row["absorbed"]) if row else 0,
            "candidates": int(row["candidates"]) if row else 0,
            "closed": int(closed or 0),
        }

    async def db_size(self) -> str | None:
        """Человекочитаемый размер БД (pg_size_pretty)."""
        return await self._pool.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()));"
        )
