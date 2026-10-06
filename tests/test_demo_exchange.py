"""Этап 9.5, §0.4 и §12 п. 1: демо-клиент и защита от боевого режима."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from src.core.http import EXCHANGE_USER_AGENT
from src.demo import exchange as dx
from tests.demo_support import FakeExchange


def make(host: str = "www.okx.com"):
    return dx.create_demo_exchange("k", "s", "p", host)


# --- §0.4: поведение ccxt 4.4.100, на которое опирается сервис -----------------


def test_ccxt_version_is_pinned() -> None:
    import ccxt

    assert ccxt.__version__ == "4.4.100"


@pytest.mark.parametrize("host", ["www.okx.com", "eea.okx.com"])
def test_client_is_in_sandbox_mode_with_the_header_and_host(host: str) -> None:
    ex = make(host)
    try:
        assert ex.isSandboxModeEnabled is True
        assert ex.headers["x-simulated-trading"] == "1"
        assert ex.hostname == host
        assert ex.userAgent == EXCHANGE_USER_AGENT
        assert ex.options["defaultType"] == "spot"
        assert ex.enableRateLimit is True
        dx.assert_demo(ex)
    finally:
        asyncio.run(ex.close())


def test_the_header_really_goes_out_in_a_signed_request() -> None:
    """§0.4 (а): заголовок уходит в подписанном запросе (проверка на уровне HTTP-сессии)."""

    class Resp:
        status, reason, headers = 200, "OK", {"Content-Type": "application/json"}

        async def text(self, errors: Any = None) -> str:
            return '{"code":"0","msg":"","data":[{"details":[]}]}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    seen: dict[str, Any] = {}

    class Session:
        headers: dict[str, str] = {}

        def _m(self, method: str):
            def call(url, **kw):
                seen.update(method=method, url=str(url), headers=kw["headers"])
                return Resp()
            return call

        get = property(lambda self: self._m("GET"))
        post = property(lambda self: self._m("POST"))

        async def close(self) -> None:
            return None

    async def go(host: str, sandbox: bool) -> dict[str, Any]:
        import ccxt.async_support as ccxt

        ex = make(host) if sandbox else ccxt.okx(
            {"apiKey": "k", "secret": "s", "password": "p", "hostname": host})
        ex.session = Session()

        async def noop(*a, **k):
            return {}
        ex.load_markets = noop
        try:
            await ex.fetch_balance()
            return dict(seen)
        finally:
            await ex.close()

    for host in ("www.okx.com", "eea.okx.com"):
        demo = asyncio.run(go(host, True))
        assert demo["url"] == f"https://{host}/api/v5/account/balance"
        assert demo["headers"]["x-simulated-trading"] == "1"
        assert "OK-ACCESS-SIGN" in demo["headers"] and demo["headers"]["OK-ACCESS-KEY"] == "k"
        assert demo["headers"]["User-Agent"] == EXCHANGE_USER_AGENT
        live = asyncio.run(go(host, False))
        assert "x-simulated-trading" not in live["headers"]   # без демо-режима заголовка нет


def test_demo_client_loads_only_spot_markets_option() -> None:
    """9.5.1: по умолчанию ccxt грузит spot/future/swap/option; демо-клиенту нужен только спот."""
    ex = make()
    try:
        assert ex.options["fetchMarkets"] == {"types": ["spot"]}
    finally:
        asyncio.run(ex.close())


def _instrument(inst_type: str, inst_id: str) -> dict[str, str]:
    spot = inst_type == "SPOT"
    return {
        "instType": inst_type, "instId": inst_id, "uly": "", "category": "1",
        "baseCcy": "BTC" if spot else "", "quoteCcy": "USDT" if spot else "",
        "settleCcy": "", "ctVal": "", "ctMult": "", "ctValCcy": "", "optType": "", "stk": "",
        "listTime": "1", "expTime": "", "lever": "", "tickSz": "0.1", "lotSz": "0.00000001",
        "minSz": "0.00001", "ctType": "", "alias": "", "state": "live", "maxLmtSz": "9999",
        "maxMktSz": "9999", "maxTwapSz": "", "maxIcebergSz": "", "maxTriggerSz": "",
        "maxStopSz": "", "contTdSwTime": "", "openType": "",
    }


def _load_markets_with_stub(*, drop_option: bool = False) -> tuple[list[str], dict[str, Any]]:
    """load_markets() демо-клиента на заглушке HTTP; FUTURES содержит запись с пустым instId."""
    import json

    urls: list[str] = []

    class Resp:
        status, reason, headers = 200, "OK", {"Content-Type": "application/json"}

        def __init__(self, body: str) -> None:
            self._body = body

        async def text(self, errors: Any = None) -> str:
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    def reply(url: str) -> str:
        if "/public/instruments" in url:
            kind = url.split("instType=")[1].split("&")[0]
            rows = {
                "SPOT": [_instrument("SPOT", "BTC-USDT")],
                "FUTURES": [_instrument("FUTURES", ""), _instrument("FUTURES", "BTC-USD-261231")],
                "SWAP": [_instrument("SWAP", "BTC-USDT-SWAP")],
                "OPTION": [_instrument("OPTION", "BTC-USD-261231-60000-C")],
            }[kind]
            return json.dumps({"code": "0", "msg": "", "data": rows})
        return json.dumps({"code": "0", "msg": "", "data": []})

    class Session:
        headers: dict[str, str] = {}

        def get(self, url, **kw):
            urls.append(str(url))
            return Resp(reply(str(url)))

        post = get

        async def close(self) -> None:
            return None

    async def go() -> dict[str, Any]:
        ex = make()
        if drop_option:
            del ex.options["fetchMarkets"]
        ex.session = Session()
        try:
            return await ex.load_markets()
        finally:
            await ex.close()

    markets = asyncio.run(go())
    return urls, markets


def test_demo_load_markets_survives_empty_instid_and_asks_spot_only() -> None:
    """9.5.1: пустой instId в FUTURES не ломает load_markets; других типов не запрашиваем."""
    urls, markets = _load_markets_with_stub()
    types = {u.split("instType=")[1].split("&")[0] for u in urls if "/public/instruments" in u}
    assert types == {"SPOT"}
    assert "BTC/USDT" in markets


def test_without_the_option_the_empty_instid_breaks_load_markets() -> None:
    """9.5.1, мутация: без опции ccxt грузит все типы и падает на пустом instId."""
    with pytest.raises(TypeError):
        _load_markets_with_stub(drop_option=True)


def test_create_market_buy_order_with_cost_exists_and_sends_the_cost_as_quote() -> None:
    import ccxt.async_support as ccxt

    ex = ccxt.okx()
    try:
        assert hasattr(ex, "create_market_buy_order_with_cost")
        assert ex.has["createMarketBuyOrderWithCost"] is True
    finally:
        asyncio.run(ex.close())


def test_hostname_option_overrides_the_host() -> None:
    assert make("eea.okx.com").hostname == "eea.okx.com"
    assert make("www.okx.com").hostname == "www.okx.com"


def test_host_outside_the_list_is_refused() -> None:
    with pytest.raises(ValueError):
        dx.create_demo_exchange("k", "s", "p", "api.example.com")


# --- §12 п. 1: assert_demo ---------------------------------------------------


def test_assert_demo_passes_for_a_demo_client() -> None:
    dx.assert_demo(FakeExchange())


def test_assert_demo_fails_without_the_header() -> None:
    ex = FakeExchange()
    del ex.headers["x-simulated-trading"]
    with pytest.raises(RuntimeError):
        dx.assert_demo(ex)


def test_assert_demo_fails_when_the_header_is_not_one() -> None:
    ex = FakeExchange()
    ex.headers["x-simulated-trading"] = "0"
    with pytest.raises(RuntimeError):
        dx.assert_demo(ex)


def test_assert_demo_fails_when_sandbox_mode_is_off() -> None:
    ex = FakeExchange()
    ex.isSandboxModeEnabled = False
    with pytest.raises(RuntimeError):
        dx.assert_demo(ex)


def test_a_swapped_header_stops_every_private_call_and_no_order_leaves() -> None:
    ex = FakeExchange()
    ex.headers["x-simulated-trading"] = "0"

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await dx.place_market_buy(ex, "BTC/USDT", Decimal(2), "at95b1")
        with pytest.raises(RuntimeError):
            await dx.place_market_sell(ex, "BTC/USDT", Decimal("0.00001"), "at95s1",
                                       Decimal("0.00001"))
        with pytest.raises(RuntimeError):
            await dx.fetch_usdt_free(ex)
        with pytest.raises(RuntimeError):
            await dx.fetch_order_by_cl(ex, "BTC/USDT", "at95b1")

    asyncio.run(go())
    assert ex.calls == []   # ни одного обращения к бирже


def test_the_header_is_checked_before_every_call_not_only_once() -> None:
    ex = FakeExchange()

    async def go() -> None:
        await dx.fetch_usdt_free(ex)
        ex.headers["x-simulated-trading"] = "0"   # пропал уже после старта
        with pytest.raises(RuntimeError):
            await dx.place_market_buy(ex, "BTC/USDT", Decimal(2), "at95b1")

    asyncio.run(go())
    assert ex.sent() == []


# --- границы кодом: только спот, только «купил — продал» ----------------------


def test_only_spot_pairs_are_allowed() -> None:
    ex = FakeExchange()

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await dx.place_market_buy(ex, "BTC/USDT:USDT", Decimal(2), "at95b1")
        with pytest.raises(RuntimeError):
            await dx.place_market_sell(ex, "BTC/USDT:USDT", Decimal("1"), "at95s1", Decimal("1"))

    asyncio.run(go())
    assert ex.sent() == []


def test_a_sale_larger_than_the_purchase_is_forbidden() -> None:
    ex = FakeExchange()

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await dx.place_market_sell(ex, "BTC/USDT", Decimal("0.2"), "at95s1", Decimal("0.1"))
        with pytest.raises(RuntimeError):   # продажа без покупки
            await dx.place_market_sell(ex, "BTC/USDT", Decimal("0.1"), "at95s1", Decimal("0"))

    asyncio.run(go())
    assert ex.sent() == []


def test_orders_are_cash_only_no_margin_no_leverage() -> None:
    ex = FakeExchange()

    async def go() -> None:
        await dx.place_market_buy(ex, "BTC/USDT", Decimal(2), "at95b1")
        await dx.place_market_sell(ex, "BTC/USDT", Decimal("0.00003"), "at95s1",
                                   Decimal("0.00003"))

    asyncio.run(go())
    for call in ex.sent():
        assert call[3] == {"clOrdId": call[3]["clOrdId"], "tdMode": "cash"}


def test_error_classification() -> None:
    import ccxt.async_support as ccxt

    assert dx.is_auth_error(ccxt.AuthenticationError("x"))
    assert dx.is_auth_error(ccxt.ExchangeError('okx {"code":"50101"}'))
    assert not dx.is_auth_error(ccxt.InsufficientFunds("x"))
    assert dx.is_definite_refusal(ccxt.InsufficientFunds("x"))
    assert not dx.is_definite_refusal(ccxt.RequestTimeout("x"))
    assert not dx.is_definite_refusal(ccxt.ExchangeNotAvailable("x"))


def test_market_limits_read_the_raw_exchange_strings() -> None:
    lot, min_sz = dx.market_limits({"info": {"lotSz": "0.00000001", "minSz": "0.00001"}})
    assert (lot, min_sz) == (Decimal("0.00000001"), Decimal("0.00001"))
    lot, min_sz = dx.market_limits({"precision": {"amount": 0.001},
                                    "limits": {"amount": {"min": 0.01}}})
    assert (lot, min_sz) == (Decimal("0.001"), Decimal("0.01"))


def test_ccxt_debug_logging_cannot_leak_the_signed_headers() -> None:
    """При LOG_LEVEL=DEBUG ccxt печатает заголовки запроса: ключ, passphrase, подпись."""
    import io
    import logging

    class Resp:
        status, reason, headers = 200, "OK", {"Content-Type": "application/json"}

        async def text(self, errors: Any = None) -> str:
            return '{"code":"0","msg":"","data":[{"details":[]}]}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class Session:
        headers: dict[str, str] = {}
        get = property(lambda self: (lambda url, **kw: Resp()))

        async def close(self) -> None:
            return None

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root, ccxt_logger = logging.getLogger(), logging.getLogger("ccxt")
    saved = (root.level, ccxt_logger.level)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    ccxt_logger.setLevel(logging.NOTSET)       # как до создания демо-клиента

    async def go() -> None:
        ex = dx.create_demo_exchange("MYAPIKEY123", "MYSECRET456", "MYPASS789", "www.okx.com")
        ex.session = Session()

        async def noop(*a, **k):
            return {}
        ex.load_markets = noop
        try:
            await ex.fetch_balance()
        finally:
            await ex.close()

    try:
        asyncio.run(go())
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        ccxt_logger.setLevel(saved[1])
    out = buf.getvalue()
    for secret in ("MYAPIKEY123", "MYSECRET456", "MYPASS789", "OK-ACCESS-SIGN"):
        assert secret not in out, secret
