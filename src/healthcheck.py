"""CLI-проверка доступности PostgreSQL и Redis (и OKX testnet — если включён).

Печатает статус по каждому сервису. Завершает процесс с кодом 0,
если все проверки пройдены, иначе с кодом 1. Запуск: ``python -m src.healthcheck``.
"""

from __future__ import annotations

import asyncio
import sys

import ccxt.async_support as ccxt

from src.core.config import settings
from src.core.db import db
from src.core.exchange import create_exchange
from src.core.redis_client import ping_redis

# Таймаут проверки OKX (мс): дешевле отказаться сразу, чем ждать дефолтный
# таймаут ccxt дольше, чем таймаут самого docker healthcheck.
_OKX_CHECK_TIMEOUT_MS = 8000


async def check_okx_testnet_access() -> bool:
    """Проверяет доступность OKX (той же сети, что настроена в ``.env``).

    Использует ``create_exchange`` — тот же клиент (подпись, заголовки,
    ключи), которым работают коллекторы, а не собственный запрос: иначе
    проверка могла бы отвечать «недоступно» там, где коллектор работает
    нормально (или наоборот).
    """
    exchange = create_exchange(
        "okx",
        testnet=settings.TESTNET,
        api_key=settings.OKX_API_KEY,
        secret_key=settings.OKX_SECRET_KEY,
        passphrase=settings.OKX_PASSPHRASE,
    )
    exchange.timeout = _OKX_CHECK_TIMEOUT_MS
    try:
        await exchange.fetch_time()
        return True
    except ccxt.NetworkError as exc:
        print(f"OKX{' testnet' if settings.TESTNET else ''} недоступен: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 — любая ошибка проверки — FAIL, не падение процесса
        print(f"OKX{' testnet' if settings.TESTNET else ''}: неожиданная ошибка проверки: {exc}")
        return False
    finally:
        await exchange.close()


async def _check() -> bool:
    """Проверяет PG, Redis (и OKX testnet), печатает статусы, возвращает общий результат."""
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

    healthy = pg_ok and redis_ok

    # OKX testnet намеренно проверяется (и учитывается в итоге) ТОЛЬКО при
    # EXCHANGE=okx TESTNET=true — то есть НИКОГДА в текущем продакшне
    # (EXCHANGE=okx, TESTNET=false), где эта проверка не запускается вовсе, и
    # поведение docker healthcheck для уже развёрнутого стека не меняется.
    if settings.EXCHANGE == "okx" and settings.TESTNET:
        okx_ok = await check_okx_testnet_access()
        print(f"OKX testnet: {'OK' if okx_ok else 'FAIL'}")
        healthy = healthy and okx_ok

    return healthy


def main() -> None:
    """Точка входа CLI: выставляет exit code по результату проверки."""
    healthy = asyncio.run(_check())
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
