"""Асинхронный клиент Redis на базе ``redis.asyncio``."""

from __future__ import annotations

import redis.asyncio as redis

from src.core.config import settings

# Кэшированный синглтон клиента Redis.
_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Возвращает (создавая при первом вызове) async-клиент Redis."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            # Пароль опционален: пусто → без auth (см. config.REDIS_PASSWORD).
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
        )
    return _client


async def ping_redis() -> bool:
    """Проверяет доступность Redis командой ``PING``."""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


async def close_redis() -> None:
    """Закрывает соединение с Redis и сбрасывает синглтон."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
