"""Пересчёт целей по вероятности (Этап 8.2, §4 и §7).

Запускается ВНУТРИ контейнера (тот же образ, что и остальные сервисы), обычно
из cron на хосте:

    docker compose --profile tools run --rm --no-deps risk

Почему в контейнере, а не на хосте: нужны asyncpg/structlog/pydantic/httpx,
которых на хосте нет и не будет (правило D-3 — pip-пакеты на хост не ставятся).

КОДЫ ВОЗВРАТА:
    0 — пересчёт выполнен. Инструмент, не прошедший предпроверку §1, кодом
        возврата НЕ считается сбоем: он получает строку с ``target_pct = NULL``
        и причиной ``data_gap``, а остальные обслуживаются (частичный запуск
        разрешён §1 ТЗ). Число таких инструментов печатается в журнал ключом
        ``instruments_failed``.
    1 — реальный сбой: база недоступна, ошибка записи. Его увидит cron.

Флаг ``--recompute`` требуется явно: запуск без аргументов ничего не считает и
печатает подсказку. Пересчёт переписывает цели на сутки вперёд для всех
инструментов, и случайный запуск «просто посмотреть» такой командой невозможен.
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from src.core.logging import setup_logging
from src.risk.runner import run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m src.risk_main",
        description="Пересчёт целей по вероятности (Этап 8.2)",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="выполнить пересчёт risk_targets по всем инструментам",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — сбой)."""
    setup_logging()
    log = structlog.get_logger().bind(component="risk_targets")
    if not args.recompute:
        log.info(
            "risk_targets_noop=1",
            hint="запустите с --recompute: без флага пересчёт не выполняется",
        )
        return 0
    try:
        outcomes = await run()
    except Exception as exc:  # noqa: BLE001 — код возврата важнее трассировки в cron
        log.error("risk_targets_recompute_failed=1", error=str(exc))
        return 1
    ok = sum(1 for item in outcomes if item.ok)
    log.info(
        "risk_targets_exit=0",
        instruments_ok=ok,
        instruments_failed=len(outcomes) - ok,
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса."""
    raise SystemExit(asyncio.run(_run(parse_args(argv))))


if __name__ == "__main__":
    main()
