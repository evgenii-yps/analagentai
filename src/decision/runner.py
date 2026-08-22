"""Планировщик Decision Agent: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.instruments import ensure_instruments
from src.core.redis_client import close_redis, get_redis
from src.decision.agent import DecisionAgent


async def run() -> None:
    """Поднимает инфраструктуру, запускает Decision Agent и ждёт сигнала остановки."""
    log = structlog.get_logger()
    pairs = settings.symbol_pairs
    log.info(
        "Запуск Decision Agent Agent Trade (Этап 4, состав — Этап 8.1)",
        interval=settings.DECISION_INTERVAL,
        threshold=settings.DECISION_THRESHOLD,
        min_agents=settings.MIN_AGENTS,
        logic_version=settings.LOGIC_VERSION,
        pairs=[pair.label for pair in pairs],
    )

    await db.connect()
    get_redis()  # прогрев клиента (пишется heartbeat)
    # Идемпотентно гарантируем колонки logic_version (Задача D) и degraded
    # (Задача A2, Этап 7.2) на старом томе — init.sql на нём уже не выполнится.
    await db.ensure_signals_logic_version()
    await db.ensure_signals_degraded()
    # Этап 7.3: калиброванная вероятность (Блок B) и учёт инерции входов (Блок C).
    await db.ensure_calibration_schema()
    await db.ensure_signals_inertia()

    tasks: list[asyncio.Task[None]] = []
    try:
        # Инструменты: market/liquidity пишут под spot, futures — под swap.
        instruments = await ensure_instruments(db, settings.EXCHANGE, pairs)
        for item in instruments:
            log.info(
                "Инструменты готовы", token=item.token,
                spot_id=item.spot_id, swap_id=item.swap_id,
            )

        # §6 ТЗ 8.1: момент границы версии логики фиксируется в БД ОДИН РАЗ —
        # при первом старте на этой версии. Данные версий 4 и 5 никогда не
        # смешиваются в анализе, и граница должна быть машиночитаемой, а не
        # восстанавливаться по памяти или по времени развёртывания.
        boundary = await db.record_logic_version_start(settings.LOGIC_VERSION)
        log.info(
            "Граница версии логики",
            logic_version=settings.LOGIC_VERSION,
            started_at_utc=boundary.isoformat(timespec="minutes"),
        )

        # Своё решение на каждую пару: сигналы разных токенов независимы и
        # пишутся под инструментом СПОТА (там же цена, по которой считается
        # исход). Futures-мнение берётся по КОНТРАКТУ той же пары.
        agents = [
            DecisionAgent(
                instrument_id=item.spot_id,
                agent_instruments={
                    "market": item.spot_id,
                    "liquidity": item.spot_id,
                    "futures": item.swap_id,
                },
                interval=settings.DECISION_INTERVAL,
                weights=settings.agent_weights,
                threshold=settings.DECISION_THRESHOLD,
                min_agents=settings.MIN_AGENTS,
                freshness_sec=settings.AGENT_FRESHNESS_SEC,
                token=item.token,
            )
            for item in instruments
        ]
        tasks = [
            asyncio.create_task(agent.run(), name=f"decision:{agent.token or 'single'}")
            for agent in agents
        ]

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
