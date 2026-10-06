"""Демо-клиент OKX (ccxt) и защита от боевого режима (Этап 9.5, §4 ТЗ).

ДЕМО-РЕЖИМ OKX — ТОТ ЖЕ REST-ХОСТ, ЧТО И БОЕВОЙ, плюс заголовок
``x-simulated-trading: 1``. Ключи для демо отдельные. Боевой ключ с заголовком и
демо-ключ без заголовка биржа отвергает — но «отвергает» не значит «защищает»:
если заголовок однажды пропадёт, а ключ окажется боевым, ордер уйдёт на НАСТОЯЩИЙ
счёт. Поэтому защита стоит в коде, а не на доверии к бирже:

* ``create_demo_exchange`` включает ``set_sandbox_mode(True)`` — ccxt 4.4.100 для
  OKX добавляет заголовок ``x-simulated-trading: 1`` в каждый запрос (проверено
  §0.4 отчёта: заголовок присутствует в подписанных запросах);
* ``assert_demo`` проверяет это ПЕРЕД КАЖДЫМ приватным запросом;
* приватные запросы к бирже из сервиса идут ТОЛЬКО через функции этого модуля
  (``fetch_usdt_free``, ``place_market_buy``, ``place_market_sell``,
  ``fetch_order_by_cl``), и каждая первой строкой зовёт ``assert_demo``.

Только спот и только «покупка, затем продажа купленного»: фьючерсы, плечо, маржа и
продажа без покупки запрещены КОДОМ (``_assert_spot_symbol``, ``tdMode=cash``,
``place_market_sell`` требует указать, сколько было куплено).

Клиент не читает ``settings`` — всё передаётся аргументами (как ``create_exchange``).
Подпись клиента — браузерная, из ``src.core.http`` (OKX блокирует питоновский
user-agent кодом 403/1010 ещё до проверки ключа).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from src.core.http import EXCHANGE_USER_AGENT, exchange_headers

# Допустимые хосты демо-режима. Счета, зарегистрированные на my.okx.com (EEA),
# работают через eea.okx.com, остальные — через www.okx.com.
ALLOWED_HOSTS: tuple[str, ...] = ("www.okx.com", "eea.okx.com")

SIM_HEADER = "x-simulated-trading"
SIM_VALUE = "1"


class DemoGuardError(RuntimeError):
    """Сработала демо-защита: запрос к бирже НЕ отправлен.

    Наследник ``RuntimeError``: ``assert_demo`` по ТЗ бросает именно его, а
    отдельный класс позволяет сервису отличить свою защиту от прочих ошибок.
    """


def quiet_exchange_loggers() -> None:
    """Не даёт ccxt писать в журнал заголовки запросов.

    ПРИ УРОВНЕ DEBUG ccxt печатает запрос целиком, вместе с заголовками:
    ``OK-ACCESS-KEY``, ``OK-ACCESS-PASSPHRASE`` и ``OK-ACCESS-SIGN``. У collector
    запросы публичные и печатать там нечего, а у демо-клиента подписанные — одна
    строка ``LOG_LEVEL=DEBUG`` в ``.env`` выложила бы ключи в журнал контейнера.
    Порог логгера ``ccxt`` поднимается до WARNING независимо от общего уровня
    (тот же приём, что для httpx и токена Telegram, дефект D-5).
    """
    logging.getLogger("ccxt").setLevel(logging.WARNING)


def create_demo_exchange(
    api_key: str, secret: str, passphrase: str, host: str
) -> ccxt.okx:
    """Создаёт ccxt-клиент OKX в ДЕМО-режиме (спот).

    ``set_sandbox_mode(True)`` вызывается ПОСЛЕ сборки клиента — именно он
    добавляет заголовок ``x-simulated-trading``. Если после этого защита не
    проходит, клиент не возвращается вовсе.
    """
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            f"хост {host!r} недопустим: разрешены {' и '.join(ALLOWED_HOSTS)}"
        )
    config: dict[str, Any] = {
        "apiKey": api_key,
        "secret": secret,
        "password": passphrase,
        "enableRateLimit": True,
        "userAgent": EXCHANGE_USER_AGENT,
        "headers": exchange_headers(),
        "hostname": host,
        "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}},
    }
    ca_file = os.environ.get("SSL_CERT_FILE")
    if ca_file:
        config["cafile"] = ca_file

    quiet_exchange_loggers()
    exchange = ccxt.okx(config)
    exchange.set_sandbox_mode(True)
    assert_demo(exchange)
    return exchange


def assert_demo(exchange: Any) -> None:
    """Бросает ``RuntimeError``, если клиент НЕ в демо-режиме.

    Условия: включён песочный режим ccxt И заголовок ``x-simulated-trading``
    равен ``"1"``. Вызывается ПЕРЕД КАЖДЫМ приватным запросом (баланс, ордер,
    запрос ордера): проверка один раз при создании не защищала бы от того, что
    заголовок исчезнет позже.
    """
    if not getattr(exchange, "isSandboxModeEnabled", False):
        raise DemoGuardError(
            "демо-защита: песочный режим ccxt выключен — запрос к бирже не "
            "отправляется"
        )
    headers = getattr(exchange, "headers", None) or {}
    value = next(
        (v for k, v in headers.items() if str(k).lower() == SIM_HEADER), None
    )
    if value is None or str(value) != SIM_VALUE:
        raise DemoGuardError(
            f"демо-защита: заголовок {SIM_HEADER}: {SIM_VALUE} отсутствует или "
            "изменён — запрос к бирже не отправляется"
        )


def _assert_spot_symbol(symbol: str) -> None:
    """Спот-пара вида ``BTC/USDT``: контракт (``BTC/USDT:USDT``) — запрещён."""
    if ":" in symbol or symbol.count("/") != 1:
        raise DemoGuardError(
            f"демо-защита: {symbol!r} не спотовая пара — разрешён только спот"
        )


def _cash_params(cl_ord_id: str) -> dict[str, str]:
    """Параметры ордера: только наличный режим (спот, без плеча и маржи)."""
    return {"clOrdId": cl_ord_id, "tdMode": "cash"}


async def fetch_usdt_free(exchange: Any) -> Decimal:
    """Свободный остаток USDT на демо-счёте."""
    assert_demo(exchange)
    balance = await exchange.fetch_balance()
    free = (balance.get("USDT") or {}).get("free")
    return Decimal(str(free)) if free is not None else Decimal(0)


async def place_market_buy(
    exchange: Any, symbol: str, cost_usd: Decimal, cl_ord_id: str
) -> dict[str, Any]:
    """Рыночная покупка на сумму ``cost_usd`` (в долларах, а не в количестве)."""
    assert_demo(exchange)
    _assert_spot_symbol(symbol)
    if cost_usd <= 0:
        raise DemoGuardError(f"демо-защита: сумма покупки должна быть > 0, получено {cost_usd}")
    return await exchange.create_market_buy_order_with_cost(
        symbol, float(cost_usd), _cash_params(cl_ord_id)
    )


async def place_market_sell(
    exchange: Any,
    symbol: str,
    qty: Decimal,
    cl_ord_id: str,
    bought_qty: Decimal,
) -> dict[str, Any]:
    """Рыночная продажа ``qty`` базовой монеты — не больше уже купленного.

    ``bought_qty`` — сколько реально куплено этим зеркалом: продажа без покупки
    запрещена кодом, и количество сверх купленного тоже.
    """
    assert_demo(exchange)
    _assert_spot_symbol(symbol)
    if qty <= 0:
        raise DemoGuardError(f"демо-защита: количество продажи должно быть > 0, получено {qty}")
    if qty > bought_qty:
        raise DemoGuardError(
            f"демо-защита: продажа {qty} больше купленного {bought_qty} — "
            "продажа без покупки запрещена"
        )
    return await exchange.create_order(
        symbol, "market", "sell", float(qty), None, _cash_params(cl_ord_id)
    )


async def fetch_order_by_cl(
    exchange: Any, symbol: str, cl_ord_id: str
) -> dict[str, Any]:
    """Запрашивает ордер по клиентскому идентификатору (восстановление, опрос)."""
    assert_demo(exchange)
    _assert_spot_symbol(symbol)
    return await exchange.fetch_order("", symbol, {"clOrdId": cl_ord_id})


def market_limits(market: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """Шаг количества (``lotSz``) и минимальный размер ордера (``minSz``) инструмента.

    Берутся из СЫРОГО ответа биржи (``market['info']``) строками: так числа точны
    до знака. Запасной путь — унифицированные поля ccxt.
    """
    info = market.get("info") or {}
    if info.get("lotSz"):
        lot = Decimal(str(info["lotSz"]))
    else:
        lot = Decimal(str((market.get("precision") or {}).get("amount") or 0))
    if info.get("minSz"):
        min_sz = Decimal(str(info["minSz"]))
    else:
        min_sz = Decimal(str(((market.get("limits") or {}).get("amount") or {}).get("min") or 0))
    return lot, min_sz


def is_auth_error(exc: BaseException) -> bool:
    """Ошибка авторизации или код 50101 (ключ не того режима).

    OKX возвращает 50101 — «APIKey does not match current environment» — когда
    ключ боевой, а запрос демо, или наоборот. ccxt сопоставляет его
    ``AuthenticationError``; текстовая проверка — страховка от смены сопоставления.
    """
    return isinstance(exc, ccxt.AuthenticationError) or "50101" in str(exc)


def is_definite_refusal(exc: BaseException) -> bool:
    """Биржа ОТВЕТИЛА отказом (ордер не принят) — а не «ответа не было».

    ccxt делит ошибки на две ветки: ``ExchangeError`` — биржа ответила и
    отказала; ``NetworkError`` (таймаут, 5xx, обслуживание, лимит запросов) —
    ответа нет или он неоднозначен, и ордер мог дойти. Для второго строка остаётся
    ``sent`` и разбирается запросом по clOrdId, а не повторной отправкой.
    """
    return isinstance(exc, ccxt.ExchangeError)
