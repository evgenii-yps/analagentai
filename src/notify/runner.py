"""Планировщик сервиса уведомлений: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis
from src.notify.agent import NotifyAgent


async def run() -> None:
    """Поднимает инфраструктуру, запускает сервис уведомлений и ждёт остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск сервиса уведомлений Agent Trade (Этап 5)",
        interval=settings.NOTIFY_INTERVAL,
        min_probability=settings.NOTIFY_MIN_PROBABILITY,
        telegram_configured=settings.telegram_configured,
    )

    await db.connect()
    get_redis()
    # Идемпотентно гарантируем наличие колонки notified (на старом томе).
    await db.ensure_notify_schema()

    if not settings.telegram_configured:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сервис простаивает")

    tasks: list[asyncio.Task[None]] = []
    try:
        agent = NotifyAgent(
            interval=settings.NOTIFY_INTERVAL,
            min_probability=settings.NOTIFY_MIN_PROBABILITY,
            cooldown_sec=settings.NOTIFY_COOLDOWN_SEC,
            symbol=settings.SYMBOL,
            tz_name=settings.NOTIFY_TIMEZONE,
        )
        tasks = [asyncio.create_task(agent.run(), name="notify")]

        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем сервис уведомлений")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        await close_redis()
        log.info("Сервис уведомлений остановлен, ресурсы освобождены")


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
