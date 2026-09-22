"""Фабрика асинхронного ccxt-клиента биржи."""

from __future__ import annotations

import os

import ccxt.async_support as ccxt

from src.core.http import EXCHANGE_USER_AGENT, exchange_headers

# Биржи, которые умеет собирать проект. Список закрытый — опечатка в EXCHANGE
# обязана ронять сервис понятной ошибкой, а не падать позже на несуществующем
# методе ccxt.
SUPPORTED_EXCHANGES = ("binance", "okx")


def create_exchange(
    exchange_id: str,
    testnet: bool = False,
    api_key: str = "",
    secret_key: str = "",
    passphrase: str = "",
) -> ccxt.Exchange:
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

    ``testnet`` — ТОЛЬКО для ``exchange_id == "okx"``: включает песочницу ccxt
    (``sandbox: True`` — тот же домен ``www.okx.com``, но ccxt подставляет
    заголовок ``x-simulated-trading`` и переключает подпись запросов). Продакшн
    (``testnet=False``) НЕ меняется: клиент, как и раньше, ходит только по
    публичным эндпоинтам и без ключей. Песочница OKX требует подписанные
    запросы всегда — пустой ключ здесь обязан падать явной ошибкой, а не
    авторизационным отказом биржи на первой же итерации коллектора.
    Параметр вынесен из ``Settings`` намеренно: функция не читает глобальный
    синглтон и проверяется без него (как ``ensure_instruments``).
    """
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Биржа {exchange_id!r} не поддерживается: {', '.join(SUPPORTED_EXCHANGES)}"
        )

    config: dict[str, object] = {
        "enableRateLimit": True,
        "userAgent": EXCHANGE_USER_AGENT,
        "headers": exchange_headers(),
    }
    ca_file = os.environ.get("SSL_CERT_FILE")
    if ca_file:
        config["cafile"] = ca_file

    if testnet:
        if exchange_id != "okx":
            raise ValueError(
                f"testnet=True поддерживается только для okx, получено "
                f"exchange_id={exchange_id!r}"
            )
        if not (api_key and secret_key and passphrase):
            raise ValueError(
                "OKX testnet требует непустых api_key, secret_key, passphrase "
                "(в .env: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE)"
            )
        config["sandbox"] = True
        config["apiKey"] = api_key
        config["secret"] = secret_key
        config["password"] = passphrase

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class(config)
