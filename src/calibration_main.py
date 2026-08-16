"""Построение калибровочной кривой (Этап 7.3, Блок B).

Запускается ВНУТРИ контейнера (тот же образ, что и остальные сервисы), обычно
из cron на хосте командой:

    docker compose --profile tools run --rm --no-deps calibration

Почему в контейнере, а не на хосте: скрипту нужны asyncpg/structlog/pydantic,
которых на хосте нет и не будет (правило D-3 — pip-пакеты на хост не ставятся).

Нехватка данных — это НЕ ошибка: если независимых наблюдений меньше
``CALIBRATION_MIN_SAMPLES``, кривая не строится, активная кривая остаётся
прежней, и процесс завершается кодом 0. Код 1 возвращается только при реальном
сбое (БД недоступна, ошибка записи) — тогда cron это увидит.
"""

from __future__ import annotations

import asyncio

import structlog

from src.calibration.runner import run
from src.core.logging import setup_logging


async def _run() -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — сбой)."""
    setup_logging()
    log = structlog.get_logger().bind(component="calibration")
    try:
        await run()
    except Exception as exc:  # noqa: BLE001 — код возврата важнее трассировки в cron
        log.error("Сбой построения калибровочной кривой", error=str(exc))
        return 1
    return 0


def main() -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса."""
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
