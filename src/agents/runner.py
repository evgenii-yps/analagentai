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
from src.core.instruments import ensure_instruments
from src.core.redis_client import close_redis, get_redis


async def run() -> None:
    """Поднимает инфраструктуру, запускает агентов и ждёт сигнала остановки."""
    log = structlog.get_logger()
    pairs = settings.symbol_pairs
    log.info(
        "Запуск сервиса агентов Agent Trade (Этап 3, состав — Этап 8.1)",
        timeframe=settings.AGENT_TIMEFRAME,
        interval=settings.AGENT_INTERVAL,
        pairs=[pair.label for pair in pairs],
        closed_bars_only=settings.MARKET_CLOSED_BARS_ONLY,
    )

    # 1. Инфраструктура: БД и Redis.
    await db.connect()
    get_redis()  # прогрев клиента (агенты пишут heartbeat)
    # Идемпотентно гарантируем таблицу учёта сбоев (Задача B, на старом томе).
    await db.ensure_agent_failure_schema()

    tasks: list[asyncio.Task[None]] = []
    try:
        # 2. Инструменты: на каждый токен пара «спот + контракт».
        instruments = await ensure_instruments(db, settings.EXCHANGE, pairs)
        for item in instruments:
            log.info(
                "Инструменты готовы", token=item.token,
                spot=item.pair.spot, spot_id=item.spot_id,
                swap=item.pair.swap, swap_id=item.swap_id,
            )

        # 3. Агенты: свой комплект на каждую пару. Market и Liquidity получают
        # СПОТ, Futures — КОНТРАКТ. Это не деталь оформления: свечи собираются
        # только по споту, а funding и открытый интерес — только по контракту
        # (замеры 22.08.2026, §1 ТЗ 8.1). Подмена одного рынка другим означала
        # бы анализ не того рынка — тест test_market_split этого не допускает.
        agents: list[BaseAgent] = []
        for item in instruments:
            agents.extend([
                MarketAgent(
                    item.spot_id,
                    settings.AGENT_TIMEFRAME,
                    settings.AGENT_MIN_CANDLES,
                    settings.AGENT_INTERVAL,
                    name_suffix=item.token,
                ),
                LiquidityAgent(
                    item.spot_id, settings.AGENT_INTERVAL, name_suffix=item.token
                ),
                FuturesAgent(
                    item.swap_id, settings.AGENT_TIMEFRAME, settings.AGENT_INTERVAL,
                    name_suffix=item.token,
                ),
            ])
        log.info("Агенты созданы", count=len(agents), pairs=len(instruments))
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
