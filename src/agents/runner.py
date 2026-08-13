"""Планировщик агентов: запуск всех 3 агентов, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.agents.base import BaseAgent
from src.agents.futures import FuturesAgent
from src.agents.liquidity import LiquidityAgent
from src.agents.market import MarketAgent
from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis


async def run() -> None:
    """Поднимает инфраструктуру, запускает агентов и ждёт сигнала остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск сервиса агентов Agent Trade (Этап 3)",
        timeframe=settings.AGENT_TIMEFRAME,
        interval=settings.AGENT_INTERVAL,
    )

    # 1. Инфраструктура: БД и Redis.
    await db.connect()
    get_redis()  # прогрев клиента (агенты пишут heartbeat)
    # Идемпотентно гарантируем таблицу учёта сбоев (Задача B, на старом томе).
    await db.ensure_agent_failure_schema()

    tasks: list[asyncio.Task[None]] = []
    try:
        # 2. Инструменты: spot (Market/Liquidity) и swap (Futures).
        spot_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SYMBOL, "spot"
        )
        swap_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SWAP_SYMBOL, "swap"
        )
        log.info("Инструменты готовы", spot_id=spot_id, swap_id=swap_id)

        # 3. Агенты (каждый получает только свой инструмент).
        agents: list[BaseAgent] = [
            MarketAgent(
                spot_id,
                settings.AGENT_TIMEFRAME,
                settings.AGENT_MIN_CANDLES,
                settings.AGENT_INTERVAL,
            ),
            LiquidityAgent(spot_id, settings.AGENT_INTERVAL),
            FuturesAgent(swap_id, settings.AGENT_TIMEFRAME, settings.AGENT_INTERVAL),
        ]
        tasks = [asyncio.create_task(a.run(), name=a.name) for a in agents]

        # 4. Обработчики сигналов остановки.
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        # 5. Работаем до отмены задач (SIGINT/SIGTERM).
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем агентов")
    finally:
        # 6. Graceful shutdown.
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        await close_redis()
        log.info("Сервис агентов остановлен, ресурсы освобождены")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    log: structlog.types.WrappedLogger,
) -> None:
    """Вешает обработчики SIGINT/SIGTERM, отменяющие задачи агентов."""

    def _shutdown() -> None:
        log.info("Сигнал остановки получен")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass
