"""Расчёт исходов по границам (Этап 8.8, §7 ТЗ).

Запускается ВНУТРИ контейнера (тот же образ, что и остальные сервисы), обычно
из cron на хосте:

    docker compose --profile tools run --rm --no-deps barrier

Почему в контейнере, а не на хосте: нужны asyncpg/structlog/pydantic, которых
на хосте нет и не будет (правило D-3 — pip-пакеты на хост не ставятся).

РАСЧЁТА В ГОРЯЧЕМ ПУТИ НЕТ. Сервисы decision, notify и evaluator этим этапом не
изменяются и об этом модуле ничего не знают: ни одно решение системы от исхода
по границам не зависит (§1 ТЗ).

КОДЫ ВОЗВРАТА:
    0 — расчёт выполнен. Сигнал без замороженной цели сбоем НЕ считается: он
        пропускается, а их общее число печатается ключом ``skipped_no_target``.
        Исход ``no_data`` — тоже не сбой: это честная запись о том, что ряда
        свечей на интервале не было.
    1 — реальный сбой: база недоступна, ошибка записи.

ДВА РЕЖИМА:

    без флагов   — досчитать НЕПОСЧИТАННОЕ. Идемпотентно: повторный запуск на
                   тех же данных не меняет ни одной строки.
    --recompute  — снести исходы текущей версии логики и посчитать заново.
                   Требуется явно, потому что пересчёт СТАРЫХ сигналов может
                   ПОНИЗИТЬ разрешение: минутные свечи удаляются политикой
                   хранения через RETENTION_1M_DAYS суток, и то, что однажды
                   измерено по минутам, во второй раз посчитается по часам.
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from src.barrier.runner import ambiguous_share, run
from src.core.config import settings
from src.core.logging import setup_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m src.barrier_main",
        description="Исход сигнала по границам: цель, предел, срок (Этап 8.8)",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help=("снести исходы текущей версии логики и посчитать заново; "
              "без флага досчитываются только непосчитанные пары"),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — сбой)."""
    setup_logging()
    log = structlog.get_logger().bind(component="barrier_outcomes")
    try:
        items = await run(recompute=args.recompute)
    except Exception as exc:  # noqa: BLE001 — код возврата важнее трассировки в cron
        log.error("barrier_compute_failed=1", error=str(exc))
        return 1

    totals: dict[str, int] = {}
    for item in items:
        for outcome, n in item.by_outcome.items():
            totals[outcome] = totals.get(outcome, 0) + n
    share = ambiguous_share(totals)
    log.info(
        "barrier_exit=0",
        horizons=len(items),
        written=sum(i.written for i in items),
        skipped_no_target=sum(i.skipped_no_target for i in items),
        stop_pct=settings.BARRIER_STOP_PCT,
        cost_pct=settings.RISK_COST_ROUNDTRIP_PCT,
        ambiguous_pct=(None if share is None else round(share, 2)),
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса."""
    raise SystemExit(asyncio.run(_run(parse_args(argv))))


if __name__ == "__main__":
    main()
