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
        "Запуск оценщика Agent Trade (Этап 6)",
        interval=settings.EVAL_INTERVAL,
        horizons=settings.eval_horizons_list,
        primary=settings.EVAL_PRIMARY_HORIZON,
    )

    await db.connect()
    get_redis()
    # Идемпотентно гарантируем наличие таблицы оценок (на старом томе).
    await db.ensure_evaluator_schema()

    tasks: list[asyncio.Task[None]] = []
    try:
        evaluator = Evaluator(
            interval=settings.EVAL_INTERVAL,
            horizons=settings.eval_horizons_list,
            primary_horizon=settings.EVAL_PRIMARY_HORIZON,
            stats_log_interval=settings.STATS_LOG_INTERVAL,
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
