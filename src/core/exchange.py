"""Фабрика асинхронного ccxt-клиента биржи."""

from __future__ import annotations

import os

import ccxt.async_support as ccxt

from src.core.http import EXCHANGE_USER_AGENT, exchange_headers


def create_exchange(exchange_id: str) -> ccxt.Exchange:
    """Создаёт экземпляр ccxt-биржи с включённым rate-limit.

    Один экземпляр на процесс. По завершении работы закрывается через
    ``await exchange.close()`` (освобождает сетевые соединения aiohttp).

    Если задана стандартная переменная окружения ``SSL_CERT_FILE`` (например,
    в среде с корпоративным прокси и собственным CA), её значение передаётся
    ccxt как ``cafile``. По умолчанию ccxt использует certifi — поведение
    в обычной среде не меняется.

    ПОДПИСЬ КЛИЕНТА ЗАДАЁТСЯ ЯВНО (Этап 8.10.1). По умолчанию ccxt 4.4
    представляется как ``python-requests/<версия>`` — то есть питоном, а защита
    OKX с 28.08.2026 отбивает такие запросы кодом 403/1010 ещё до проверки
    ключа. Заголовки берутся из ``src.core.http``, единого места проекта: своя
    подпись здесь означала бы, что через месяц то же самое придётся чинить в
    третьем месте.

    ``userAgent`` и ``headers`` задаются ОБА намеренно. ccxt подставляет
    ``userAgent`` сам, но только если запрос идёт его собственным путём; явные
    ``headers`` покрывают и остальные случаи, а совпадающие значения друг другу
    не противоречат.
    """
    config: dict[str, object] = {
        "enableRateLimit": True,
        "userAgent": EXCHANGE_USER_AGENT,
        "headers": exchange_headers(),
    }
    ca_file = os.environ.get("SSL_CERT_FILE")
    if ca_file:
        config["cafile"] = ca_file

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class(config)
