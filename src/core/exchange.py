"""Фабрика асинхронного ccxt-клиента биржи."""

from __future__ import annotations

import ccxt.async_support as ccxt


def create_exchange(exchange_id: str) -> ccxt.Exchange:
    """Создаёт экземпляр ccxt-биржи с включённым rate-limit.

    Один экземпляр на процесс. По завершении работы закрывается через
    ``await exchange.close()`` (освобождает сетевые соединения aiohttp).
    """
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"enableRateLimit": True})
