"""Ежесуточная сводка о состоянии системы в Telegram (§10 ТЗ 6.5).

Собирает краткий отчёт за последние сутки и отправляет его в Telegram:
жив ли каждый сервис (по heartbeat в Redis), свежесть данных, число сигналов
и агрегированная статистика успешности. Запускается раз в сутки из cron
внутри контейнера:

    docker compose exec -T evaluator python -m src.health.daily_report

Скрипт устойчив к сбоям (§14 ТЗ 6.5): любые ошибки при сборе метрик ловятся,
в отчёт попадает то, что удалось собрать; при полном отказе процесс не «падает»
с трейсбеком, а логирует проблему и завершается штатно.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.logging import setup_logging
from src.core.redis_client import close_redis, get_redis
from src.notify.telegram import send_message

_log = structlog.get_logger().bind(component="daily_report")

# Ожидаемые heartbeat-ключи сервисов (см. соответствующие runner'ы).
HEARTBEAT_KEYS = [
    "collector:heartbeat:ohlcv",
    "collector:heartbeat:orderbook",
    "collector:heartbeat:trades",
    "collector:heartbeat:futures",
    "agent:heartbeat:market",
    "agent:heartbeat:liquidity",
    "agent:heartbeat:futures",
    "decision:heartbeat",
    "notify:heartbeat",
    "evaluator:heartbeat",
]


async def _collect_heartbeats() -> list[str]:
    """Возвращает строки статуса по каждому heartbeat-ключу (жив / нет)."""
    lines: list[str] = []
    try:
        redis = get_redis()
        for key in HEARTBEAT_KEYS:
            try:
                alive = await redis.exists(key)
            except Exception:  # noqa: BLE001
                alive = 0
            mark = "🟢" if alive else "🔴"
            lines.append(f"{mark} {key}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Не удалось опросить Redis: {exc}")
    return lines


async def _collect_data_stats() -> list[str]:
    """Свежесть данных и объёмы за последние сутки."""
    lines: list[str] = []
    try:
        last_ohlcv = await db.pool.fetchval("SELECT max(ts) FROM ohlcv;")
        lines.append(f"Последняя свеча OHLCV: {last_ohlcv or 'нет данных'}")

        window = "now() - interval '24 hours'"
        row = await db.pool.fetchrow(
            f"""
            SELECT
              (SELECT count(*) FROM trades              WHERE ts > {window}) AS trades,
              (SELECT count(*) FROM orderbook_snapshots WHERE ts > {window}) AS orderbook,
              (SELECT count(*) FROM ohlcv               WHERE ts > {window}) AS ohlcv;
            """
        )
        lines.append(
            f"За 24ч — свечей: {row['ohlcv']}, сделок: {row['trades']}, "
            f"снимков стакана: {row['orderbook']}"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Не удалось получить статистику данных: {exc}")
    return lines


async def _collect_signal_stats() -> list[str]:
    """Число сигналов за сутки и разбивка по решениям."""
    lines: list[str] = []
    try:
        row = await db.pool.fetchrow(
            """
            SELECT
              count(*)                                          AS total,
              count(*) FILTER (WHERE decision = 'buy')          AS buy,
              count(*) FILTER (WHERE decision = 'sell')         AS sell,
              count(*) FILTER (WHERE decision = 'wait')         AS wait,
              count(*) FILTER (WHERE notified)                  AS notified
            FROM signals
            WHERE ts > now() - interval '24 hours';
            """
        )
        lines.append(
            f"Сигналов за 24ч: {row['total']} "
            f"(buy: {row['buy']}, sell: {row['sell']}, wait: {row['wait']}; "
            f"отправлено в Telegram: {row['notified']})"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Не удалось получить статистику сигналов: {exc}")
    return lines


async def _collect_success_stats() -> list[str]:
    """Агрегированная успешность по decision × horizon (за всё время)."""
    lines: list[str] = []
    try:
        stats = await db.get_success_stats()
        if not stats:
            lines.append("Оценённых сигналов пока нет (накопление статистики).")
            return lines
        for s in stats:
            rate = (s.get("success_rate") or 0.0) * 100
            pnl = s.get("avg_pnl_pct") or 0.0
            lines.append(
                f"{s['decision']}/{s['horizon']}: n={s['n']}, "
                f"успех {rate:.0f}%, ср. pnl {pnl:+.2f}%"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Не удалось получить статистику успешности: {exc}")
    return lines


def _build_message(sections: dict[str, list[str]]) -> str:
    """Собирает HTML-сообщение из секций."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"<b>📊 Agent Trade — суточная сводка</b>\n<i>{now}</i>"]
    for title, lines in sections.items():
        body = "\n".join(lines) if lines else "нет данных"
        parts.append(f"\n<b>{title}</b>\n{body}")
    return "\n".join(parts)


async def _run() -> None:
    """Собирает сводку и отправляет её в Telegram."""
    setup_logging()
    if not settings.telegram_configured:
        _log.warning("Telegram не настроен — суточная сводка не отправлена.")
        return

    try:
        await db.connect()
    except Exception as exc:  # noqa: BLE001
        _log.warning("Нет доступа к БД для сводки", error=str(exc))

    sections: dict[str, list[str]] = {}
    sections["Сервисы (heartbeat)"] = await _collect_heartbeats()
    sections["Данные"] = await _collect_data_stats()
    sections["Сигналы"] = await _collect_signal_stats()
    sections["Успешность (всё время)"] = await _collect_success_stats()

    message = _build_message(sections)
    sent = await send_message(message)
    if sent:
        _log.info("Суточная сводка отправлена в Telegram.")
    else:
        _log.warning("Не удалось отправить суточную сводку в Telegram.")

    await db.close()
    await close_redis()


def main() -> None:
    """Точка входа CLI. Никогда не завершается трейсбеком (устойчивость §14)."""
    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        # Даже при неожиданной ошибке не «роняем» cron-задачу трейсбеком.
        print(f"daily_report: непредвиденная ошибка: {exc}", flush=True)


if __name__ == "__main__":
    main()
