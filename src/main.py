"""Точка входа приложения (Этап 1 — заглушка с health-check).

На этом этапе бизнес-логики нет: настраиваем логирование, проверяем
доступность PostgreSQL и Redis и завершаем работу.
"""

from __future__ import annotations

import asyncio

import structlog

from src.core.db import db
from src.core.logging import setup_logging
from src.core.redis_client import ping_redis


async def _run() -> None:
    """Поднимает соединения, логирует статус инфраструктуры и закрывает их."""
    log = structlog.get_logger()
    log.info("Запуск Agent Trade (Этап 1: инфраструктура)")

    await db.connect()
    try:
        pg_ok = await db.ping()
        redis_ok = await ping_redis()
        log.info("Статус инфраструктуры", postgres=pg_ok, redis=redis_ok)
    finally:
        await db.close()

    log.info("Завершение работы")


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает event loop."""
    setup_logging()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
