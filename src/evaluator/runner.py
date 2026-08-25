"""Планировщик оценщика: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis
from src.evaluator.evaluator import Evaluator


async def run() -> None:
    """Поднимает инфраструктуру, запускает оценщик и ждёт сигнала остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск оценщика Agent Trade (Этап 6, горизонты — Этап 8.1)",
        interval=settings.EVAL_INTERVAL,
        horizons_h=settings.eval_horizons_hours,
        primary_h=settings.eval_primary_horizon_h,
    )

    await db.connect()
    get_redis()
    # Идемпотентно гарантируем схему оценок с колонкой horizon_h (Этап 8.1 §5).
    await db.ensure_evaluator_schema()
    await db.ensure_logic_version_schema()

    tasks: list[asyncio.Task[None]] = []
    try:
        # Граница версии логики (§6): сигналы, выданные ДО перехода на текущую
        # версию, новых горизонтов не получают — досчёт задним числом запрещён
        # (§12 ТЗ 8.1).
        #
        # Границу фиксирует тот сервис, который стартовал первым (запись
        # идемпотентна, первый писатель побеждает). Порядок старта контейнеров
        # не задан, и если бы оценщик просто ЧИТАЛ границу, при старте раньше
        # Decision Agent он получил бы «границы нет» — то есть работал бы вовсе
        # без отсечения и досчитал бы горизонты 12ч и 24ч старым сигналам
        # версии 4. Это ровно то, что §12 запрещает, поэтому граница здесь
        # именно ЗАПИСЫВАЕТСЯ, а не читается.
        evaluate_from = await db.record_logic_version_start(settings.LOGIC_VERSION)
        log.info(
            "Граница дооценки",
            logic_version=settings.LOGIC_VERSION,
            evaluate_from=None if evaluate_from is None
            else evaluate_from.isoformat(timespec="minutes"),
        )
        evaluator = Evaluator(
            interval=settings.EVAL_INTERVAL,
            horizons=settings.eval_horizons_hours,
            primary_horizon=settings.eval_primary_horizon_h,
            stats_log_interval=settings.STATS_LOG_INTERVAL,
            evaluate_from=evaluate_from,
        )
        tasks = [asyncio.create_task(evaluator.run(), name="evaluator")]

        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем оценщик")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        await close_redis()
        log.info("Оценщик остановлен, ресурсы освобождены")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    log: structlog.types.WrappedLogger,
) -> None:
    """Вешает обработчики SIGINT/SIGTERM, отменяющие задачи."""

    def _shutdown() -> None:
        log.info("Сигнал остановки получен")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass
