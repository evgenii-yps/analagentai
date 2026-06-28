"""Оркестратор коллекторов: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.collectors.base import BaseCollector
from src.collectors.futures import FuturesCollector
from src.collectors.ohlcv import OHLCVCollector
from src.collectors.orderbook import OrderBookCollector
from src.collectors.trades import TradesCollector
from src.core.config import settings
from src.core.db import db
from src.core.exchange import create_exchange
from src.core.redis_client import close_redis, get_redis


async def run() -> None:
    """Поднимает инфраструктуру, запускает коллекторы и ждёт сигнала остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск коллектора Agent Trade (Этап 2)",
        exchange=settings.EXCHANGE,
        symbol=settings.SYMBOL,
        swap_symbol=settings.SWAP_SYMBOL,
    )

    # 1. Инфраструктура: БД, Redis, ccxt-клиент.
    await db.connect()
    # Прогреваем клиент Redis заранее (heartbeat-ключи будут писаться коллекторами).
    get_redis()
    exchange = create_exchange(settings.EXCHANGE)

    tasks: list[asyncio.Task[None]] = []
    try:
        await exchange.load_markets()

        # 2. Инструменты: spot и swap записи одного токена.
        spot_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SYMBOL, "spot"
        )
        swap_id = await db.get_or_create_instrument(
            settings.EXCHANGE, settings.SWAP_SYMBOL, "swap"
        )
        log.info("Инструменты готовы", spot_id=spot_id, swap_id=swap_id)

        # 3. Коллекторы.
        collectors: list[BaseCollector] = [
            OHLCVCollector(
                exchange, spot_id, settings.SYMBOL,
                settings.timeframes_list, settings.OHLCV_INTERVAL,
            ),
            OrderBookCollector(
                exchange, spot_id, settings.SYMBOL,
                settings.ORDERBOOK_DEPTH, settings.ORDERBOOK_INTERVAL,
            ),
            TradesCollector(
                exchange, spot_id, settings.SYMBOL, settings.TRADES_INTERVAL,
            ),
            FuturesCollector(
                exchange, swap_id, settings.SWAP_SYMBOL, settings.FUTURES_INTERVAL,
            ),
        ]
        tasks = [asyncio.create_task(c.run(), name=c.name) for c in collectors]

        # 4. Обработка сигналов остановки: отменяем все задачи коллекторов.
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        # 5. Работаем до отмены (SIGINT/SIGTERM) задач коллекторов.
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем коллекторы")
    finally:
        # 6. Graceful shutdown: дожидаемся завершения задач и закрываем ресурсы.
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await exchange.close()
        await db.close()
        await close_redis()
        log.info("Коллектор остановлен, ресурсы освобождены")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    log: structlog.types.WrappedLogger,
) -> None:
    """Вешает обработчики SIGINT/SIGTERM, отменяющие задачи коллекторов."""

    def _shutdown() -> None:
        log.info("Сигнал остановки получен")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # На некоторых платформах (напр. Windows) обработчики недоступны.
            pass
