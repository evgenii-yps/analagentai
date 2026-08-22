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
from src.core.instruments import ensure_instruments
from src.core.redis_client import close_redis, get_redis


async def run() -> None:
    """Поднимает инфраструктуру, запускает коллекторы и ждёт сигнала остановки."""
    log = structlog.get_logger()
    pairs = settings.symbol_pairs
    log.info(
        "Запуск коллектора Agent Trade (Этап 2, состав инструментов — Этап 8.1)",
        exchange=settings.EXCHANGE,
        pairs=[pair.label for pair in pairs],
        tokens=[pair.token for pair in pairs],
    )

    # 1. Инфраструктура: БД, Redis, ccxt-клиент.
    await db.connect()
    # Прогреваем клиент Redis заранее (heartbeat-ключи будут писаться коллекторами).
    get_redis()
    exchange = create_exchange(settings.EXCHANGE)

    tasks: list[asyncio.Task[None]] = []
    try:
        await exchange.load_markets()

        # 2. Инструменты: по паре «спот + контракт» на каждый токен.
        instruments = await ensure_instruments(db, settings.EXCHANGE, pairs)
        for item in instruments:
            log.info(
                "Инструменты готовы", token=item.token,
                spot=item.pair.spot, spot_id=item.spot_id,
                swap=item.pair.swap, swap_id=item.swap_id,
            )

        # 3. Коллекторы: свой комплект на каждую пару. Свечи, стакан и сделки
        # собираются по СПОТУ, funding и открытый интерес — по КОНТРАКТУ.
        # Имена коллекторов различаются токеном, иначе heartbeat-ключи пяти
        # токенов затирали бы друг друга и надзор видел бы один инструмент.
        collectors: list[BaseCollector] = []
        for item in instruments:
            token = item.token
            collectors.extend([
                OHLCVCollector(
                    exchange, item.spot_id, item.pair.spot,
                    settings.timeframes_list, settings.OHLCV_INTERVAL,
                    name_suffix=token,
                ),
                OrderBookCollector(
                    exchange, item.spot_id, item.pair.spot,
                    settings.ORDERBOOK_DEPTH, settings.ORDERBOOK_INTERVAL,
                    name_suffix=token,
                ),
                TradesCollector(
                    exchange, item.spot_id, item.pair.spot,
                    settings.TRADES_INTERVAL, name_suffix=token,
                ),
                FuturesCollector(
                    exchange, item.swap_id, item.pair.swap,
                    settings.FUTURES_INTERVAL, name_suffix=token,
                ),
            ])
        log.info("Коллекторы созданы", count=len(collectors),
                 per_pair=len(collectors) // max(len(instruments), 1))
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
