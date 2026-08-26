"""Расчёт исходов базовых стратегий (Этап 8.9, §7 ТЗ).

Запускается ВНУТРИ контейнера (тот же образ, тот же сервис профиля tools, что и
расчёт исходов системы — нового сервиса этап не заводит):

    docker compose --profile tools run --rm --no-deps barrier \\
        python -m src.baseline_main

Почему в контейнере: нужны asyncpg/structlog/pydantic, которых на хосте нет и
не будет (правило D-3).

ЭТАП НИЧЕГО НЕ УЛУЧШАЕТ. Он измеряет. Ни одно решение системы от этого модуля
не зависит: сервисы decision, notify, evaluator и risk о нём не знают, и
LOGIC_VERSION остаётся 5.

КОДЫ ВОЗВРАТА:
    0 — расчёт выполнен. Пропуск строки из-за отсутствия исторической цели
        сбоем НЕ считается: число печатается ключом ``skipped_no_target``.
        Исход ``no_data`` — тоже не сбой, а честная запись.
    1 — реальный сбой: база недоступна, ошибка записи.

АРГУМЕНТЫ:
    --strategy  считать только одну стратегию (по умолчанию все шесть);
    --since     нижняя граница момента входа, ISO-8601 (например 2026-08-25);
    --limit     потолок числа входов — для замера производительности на части
                выборки, а не всей;
    --recompute снести строки выбранных стратегий и посчитать заново.

ЗАМЕР ПРОИЗВОДИТЕЛЬНОСТИ ВЫВОДИТСЯ ВСЕГДА (§7 ТЗ), ключами ``seconds``,
``windows_read`` и ``rows_per_second``. Он нужен не для красоты: расчёт идёт
между двумя другими задачами по расписанию, и если он перестанет укладываться в
окно, узнать об этом надо из журнала, а не по последствиям.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog

from src.baseline.runner import run
from src.baseline.strategies import STRATEGIES
from src.core.config import settings
from src.core.logging import setup_logging


def parse_since(value: str | None) -> datetime | None:
    """Разбор ``--since``. Наивное время трактуется как UTC, а не как местное.

    Молчаливая трактовка наивного времени как местного сдвинула бы границу на
    смещение пояса сервера — и выборка тихо оказалась бы другой.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m src.baseline_main",
        description="Базовые стратегии как линейка сравнения (Этап 8.9)",
    )
    parser.add_argument(
        "--strategy", choices=sorted(STRATEGIES), default=None,
        help="считать только одну стратегию (по умолчанию все шесть)",
    )
    parser.add_argument(
        "--since", default=None,
        help="нижняя граница момента входа, ISO-8601 (например 2026-08-25)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="потолок числа входов — для замера на части выборки",
    )
    parser.add_argument(
        "--recompute", action="store_true",
        help="снести строки выбранных стратегий и посчитать заново",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — сбой)."""
    setup_logging()
    log = structlog.get_logger().bind(component="baseline_strategies")
    try:
        stats = await run(
            strategy=args.strategy,
            since=parse_since(args.since),
            limit=args.limit,
            recompute=args.recompute,
        )
    except Exception as exc:  # noqa: BLE001 — код возврата важнее трассировки в cron
        log.error("baseline_compute_failed=1", error=str(exc))
        return 1

    log.info(
        "baseline_exit=0",
        strategies=len(stats.per_strategy),
        written=stats.written,
        windows_read=stats.windows_read,
        seconds=round(stats.seconds, 3),
        skipped_no_target=sum(
            s.skipped_no_target for s in stats.per_strategy.values()
        ),
        seed=settings.BASELINE_SEED,
        stop_pct=settings.BARRIER_STOP_PCT,
        cost_pct=settings.RISK_COST_ROUNDTRIP_PCT,
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса."""
    raise SystemExit(asyncio.run(_run(parse_args(argv))))


if __name__ == "__main__":
    main()
