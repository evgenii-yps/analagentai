"""CLI-проверка доступности PostgreSQL и Redis.

Печатает статус по каждому сервису. Завершает процесс с кодом 0,
если оба живы, иначе с кодом 1. Запуск: ``python -m src.healthcheck``.
"""

from __future__ import annotations

import asyncio
import sys

from src.core.db import db
from src.core.redis_client import ping_redis


async def _check() -> bool:
    """Проверяет PG и Redis, печатает статусы, возвращает общий результат."""
    # PostgreSQL: для проверки требуется поднять пул.
    try:
        await db.connect()
        pg_ok = await db.ping()
    except Exception:
        pg_ok = False
    finally:
        await db.close()

    redis_ok = await ping_redis()

    print(f"PostgreSQL: {'OK' if pg_ok else 'FAIL'}")
    print(f"Redis:      {'OK' if redis_ok else 'FAIL'}")

    return pg_ok and redis_ok


def main() -> None:
    """Точка входа CLI: выставляет exit code по результату проверки."""
    healthy = asyncio.run(_check())
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
