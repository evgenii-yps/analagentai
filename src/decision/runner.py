"""Планировщик Decision Agent: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis
from src.decision.agent import DecisionAgent


async def run() -> None:
    """Поднимает инфраструктуру, запускает Decision Agent и ждёт сигнала остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск Decision Agent Agent Trade (Этап 4)",
        interval=settings.DECISION_INTERVAL,
        threshold=settings.DECISION_THRESHOLD,
        min_agents=settings.MIN_AGENTS,
    )

    await db.connect()
    get_redis()  # прогрев клиента (пишется heartbeat)
    # Идемпотентно гарантируем колонку logic_version (Задача D, на старом томе).
    await db.ensure_signals_logic_version()

    tasks: list[asyncio.Task[None]] = []
    try:
        # Инструменты: market/liquidity пишут под spot, futures — под swap.
        spot_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SYMBOL, "spot"
        )
        swap_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SWAP_SYMBOL, "swap"
        )
        log.info("Инструменты готовы", spot_id=spot_id, swap_id=swap_id)

        agent = DecisionAgent(
            instrument_id=spot_id,
            agent_instruments={
                "market": spot_id,
                "liquidity": spot_id,
                "futures": swap_id,
            },
            interval=settings.DECISION_INTERVAL,
            weights=settings.agent_weights,
            threshold=settings.DECISION_THRESHOLD,
            min_agents=settings.MIN_AGENTS,
            freshness_sec=settings.AGENT_FRESHNESS_SEC,
        )
        tasks = [asyncio.create_task(agent.run(), name="decision")]

        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем Decision Agent")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        await close_redis()
        log.info("Decision Agent остановлен, ресурсы освобождены")


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
