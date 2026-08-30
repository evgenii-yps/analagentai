"""Точка входа сервиса ведения позиций (Этап 9.1, позиции ВИРТУАЛЬНЫЕ).

Сервис ПОСТОЯННЫЙ, а не задача из cron: позиция закрывается по касанию внутри
минуты, и суточный прогон пропустил бы и цель, и предел — он увидел бы только
то положение цены, в котором она оказалась к моменту прогона.

СЕТЬ НАРУЖУ ЭТОМУ СЕРВИСУ НЕ НУЖНА. Он читает только собственные свечи из
``public.ohlcv``; ключи API биржи он не читает и читать не должен. Единственное
исходящее обращение — отправка уведомления в Telegram той же функцией, что у
сервиса ``notify``, и она отключается ключом ``POSITION_NOTIFY_ENABLED``.
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.logging import setup_logging
from src.core.redis_client import close_redis, get_redis
from src.positions.runner import run


async def _main() -> None:
    """Поднимает инфраструктуру, запускает цикл и ждёт остановки."""
    log = structlog.get_logger()

    # ВЫКЛЮЧЕННЫЙ СЕРВИС НЕ ПОДКЛЮЧАЕТСЯ К БАЗЕ. Держать соединение ради
    # ничегонеделания — значит занимать слот пула и выглядеть работающим в
    # мониторинге, ничего при этом не делая.
    if not settings.POSITIONS_ENABLED:
        log.info(
            "POSITIONS_ENABLED=false — сервис ведения позиций простаивает",
            component="positions",
        )
        await asyncio.Event().wait()
        return

    log.info(
        "Запуск сервиса ведения позиций (Этап 9.1)",
        component="positions",
        interval=settings.POSITION_INTERVAL,
        horizon_h=settings.POSITION_HORIZON_H,
        slot_usd=settings.POSITION_SLOT_USD,
        max_open=settings.POSITION_MAX_OPEN,
        virtual=True,
    )

    await db.connect()
    get_redis()
    # Схема гарантируется сервисом при старте: миграция 018 могла быть не
    # применена на уже работающем томе, и без таблицы сервис молча простаивал бы.
    await db.ensure_positions_schema()

    task = asyncio.create_task(run(), name="positions")
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        log.info("Сигнал остановки получен", component="positions")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        await task
    except asyncio.CancelledError:
        log.info("Сервис ведения позиций остановлен", component="positions")
    finally:
        await db.close()
        await close_redis()
        log.info("Ресурсы освобождены", component="positions")


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает сервис."""
    setup_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
