"""Фабрика асинхронного ccxt-клиента биржи."""

from __future__ import annotations

import os

import ccxt.async_support as ccxt


def create_exchange(exchange_id: str) -> ccxt.Exchange:
    """Создаёт экземпляр ccxt-биржи с включённым rate-limit.

    Один экземпляр на процесс. По завершении работы закрывается через
    ``await exchange.close()`` (освобождает сетевые соединения aiohttp).

    Если задана стандартная переменная окружения ``SSL_CERT_FILE`` (например,
    в среде с корпоративным прокси и собственным CA), её значение передаётся
    ccxt как ``cafile``. По умолчанию ccxt использует certifi — поведение
    в обычной среде не меняется.
    """
    config: dict[str, object] = {"enableRateLimit": True}
    ca_file = os.environ.get("SSL_CERT_FILE")
    if ca_file:
        config["cafile"] = ca_file

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class(config)
