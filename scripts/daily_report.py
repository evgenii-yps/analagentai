#!/usr/bin/env python3
"""Суточная сводка Agent Trade (запускается на хосте, обычно из cron в 06:00 UTC).

Правка §5: строка «Отправлено уведомлений» теперь считается по фактическим
отправкам (``signals.notified_at``), а не по числу кандидатов, прошедших порог
вероятности. Кандидаты выводятся отдельной строкой — расхождение показывает,
сколько отсекает анти-спам.

Правка §13.3: возраст heartbeat не может быть отрицательным (расхождение часов
контейнера и хоста) — отрицательные значения приводятся к нулю.

Сводка печатается в stdout и, если настроен Telegram, отправляется в чат.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

import asyncpg
import redis.asyncio as aioredis
import structlog

# Корень репозитория (родитель scripts/) в sys.path — для запуска из cron.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import settings  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402
from src.health.report import clamp_age_seconds, format_daily_report  # noqa: E402
from src.notify.telegram import send_message  # noqa: E402

# Отслеживаемые heartbeat-ключи Redis: подпись → ключ.
_HEARTBEAT_KEYS = {
    "decision": "decision:heartbeat",
    "notify": "notify:heartbeat",
    "evaluator": "evaluator:heartbeat",
    "market": "agent:heartbeat:market",
    "liquidity": "agent:heartbeat:liquidity",
    "futures": "agent:heartbeat:futures",
}


async def _collect_db_stats(conn: asyncpg.Connection) -> dict[str, int]:
    """Считает показатели за последние 24 часа."""
    row = await conn.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE ts >= now() - interval '24 hours') AS decisions_total,
            count(*) FILTER (WHERE ts >= now() - interval '24 hours'
                             AND decision = 'buy')  AS buy,
            count(*) FILTER (WHERE ts >= now() - interval '24 hours'
                             AND decision = 'sell') AS sell,
            count(*) FILTER (WHERE ts >= now() - interval '24 hours'
                             AND decision = 'wait') AS wait,
            count(*) FILTER (WHERE notified_at >= now() - interval '24 hours') AS notified,
            count(*) FILTER (WHERE ts >= now() - interval '24 hours'
                             AND probability >= $1) AS candidates,
            count(*) FILTER (WHERE ts >= now() - interval '24 hours'
                             AND status = 'closed') AS closed
        FROM signals;
        """,
        float(settings.NOTIFY_MIN_PROBABILITY),
    )
    return {k: int(v or 0) for k, v in dict(row).items()}


async def _collect_heartbeats(now: datetime) -> dict[str, float | None]:
    """Читает heartbeat-отметки из Redis и считает их возраст (с клампом §13.3)."""
    ages: dict[str, float | None] = {}
    client = aioredis.Redis(
        host=settings.EXPORT_PG_HOST,  # тот же хост-loopback, что и для БД
        port=settings.REDIS_PORT,
        socket_connect_timeout=3,
    )
    try:
        for label, key in _HEARTBEAT_KEYS.items():
            raw = await client.get(key)
            if raw is None:
                ages[label] = None
                continue
            try:
                last_seen = datetime.fromisoformat(raw.decode())
            except (ValueError, AttributeError):
                ages[label] = None
                continue
            ages[label] = clamp_age_seconds(last_seen, now)
    except Exception:  # noqa: BLE001 — Redis недоступен → просто нет heartbeat
        for label in _HEARTBEAT_KEYS:
            ages.setdefault(label, None)
    finally:
        await client.aclose()
    return ages


async def _run() -> int:
    """Собирает сводку, печатает и (при настройке) отправляет в Telegram."""
    setup_logging()
    log = structlog.get_logger().bind(component="daily_report")

    try:
        conn = await asyncpg.connect(dsn=settings.host_pg_dsn)
    except Exception as exc:  # noqa: BLE001
        log.error("БД недоступна для суточной сводки", error=str(exc))
        return 1

    try:
        stats = await _collect_db_stats(conn)
    finally:
        await conn.close()

    stats["heartbeats"] = await _collect_heartbeats(datetime.now(UTC))
    report = format_daily_report(stats)
    print(report)

    if settings.telegram_configured:
        await send_message(report.replace("&", "&amp;").replace("<", "&lt;"))
    return 0


def main() -> None:
    """Точка входа суточной сводки."""
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
