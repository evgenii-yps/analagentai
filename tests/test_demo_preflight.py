"""Этап 9.5, §7: предпроверка — на заглушке биржи, без сети и без базы."""

from __future__ import annotations

from decimal import Decimal

import ccxt.async_support as ccxt

from src.demo import preflight
from tests.demo_support import SECRETS, FakeExchange

D = Decimal


def factory(per_host: dict[str, FakeExchange]):
    def make(host: str) -> FakeExchange:
        return per_host[host]
    return make


def rejecting() -> FakeExchange:
    return FakeExchange(fail_balance=ccxt.AuthenticationError(
        f'okx {{"code":"50101","msg":"APIKey does not match current environment."}} {SECRETS[0]}'))


async def run(per_host, host="www.okx.com", symbols=("BTC/USDT", "DOGE/USDT"), **kw):
    rep = await preflight.run_preflight(
        factory(per_host), host, list(symbols), D("2"), D("20"), **kw)
    return rep, preflight.render(rep)


async def test_ready_when_the_configured_host_accepts_the_key() -> None:
    www, eea = FakeExchange(), rejecting()
    rep, text = await run({"www.okx.com": www, "eea.okx.com": eea})
    assert rep.blockers == [] and text.rstrip().endswith("ГОТОВО")
    assert "OKX_DEMO_HOST=www.okx.com — верно" in text
    assert "50101" in text                                  # eea ключ не принял — это видно
    assert "x-simulated-trading: 1" in text
    assert "расхождение" in text and "1000" in text         # время и баланс
    assert "BTC/USDT" in text and "минимальный размер" in text
    assert www.sent() == [] and eea.sent() == []            # без --test-order ордеров нет
    assert "не выполнялся" in text


async def test_names_the_other_host_when_the_key_belongs_to_eea() -> None:
    www, eea = rejecting(), FakeExchange()
    rep, text = await run({"www.okx.com": www, "eea.okx.com": eea})
    assert "впишите в .env OKX_DEMO_HOST=eea.okx.com" in text
    assert text.rstrip().splitlines()[-1].startswith("НЕ ГОТОВО")
    assert "OKX_DEMO_HOST=eea.okx.com" in text.rstrip().splitlines()[-1]


async def test_not_ready_when_no_host_accepts_the_key() -> None:
    rep, text = await run({"www.okx.com": rejecting(), "eea.okx.com": rejecting()})
    assert "ни www.okx.com, ни eea.okx.com" in text.splitlines()[-1]
    assert "ключ не подходит к демо-режиму" in text


async def test_secrets_never_appear_in_the_output() -> None:
    _rep, text = await run({"www.okx.com": rejecting(), "eea.okx.com": rejecting()})
    assert all(s not in text for s in SECRETS)


async def test_low_balance_is_a_blocker() -> None:
    _rep, text = await run({"www.okx.com": FakeExchange(usdt=D("5")), "eea.okx.com": rejecting()})
    verdict = text.splitlines()[-1]
    assert verdict.startswith("НЕ ГОТОВО") and "меньше порога" in verdict


async def test_the_dollar_minimum_is_checked_against_the_slot() -> None:
    ex = FakeExchange(prices={"BTC/USDT": 60000.0, "DOGE/USDT": 400.0, "FOO/USDT": 1.0})
    # DOGE: минимум 0.01 × 400 = $4 > $2
    rep, text = await run({"www.okx.com": ex, "eea.okx.com": rejecting()})
    assert any("DOGE/USDT" in b for b in rep.blockers)
    assert "не хватает" in text


async def test_an_instrument_missing_from_the_spot_market_is_a_blocker() -> None:
    rep, _ = await run({"www.okx.com": FakeExchange(), "eea.okx.com": rejecting()},
                       symbols=("BTC/USDT", "FOO/USDT", "NOPE/USDT"))
    assert any("NOPE/USDT" in b for b in rep.blockers)


async def test_a_big_clock_skew_is_a_blocker() -> None:
    import time

    rep, _ = await run({"www.okx.com": FakeExchange(), "eea.okx.com": rejecting()},
                       local_time=lambda: time.time() + 120)
    assert any("часы" in b for b in rep.blockers)


async def test_a_swapped_header_fails_the_first_step() -> None:
    ex = FakeExchange()
    ex.headers["x-simulated-trading"] = "0"
    rep, text = await run({"www.okx.com": ex, "eea.okx.com": FakeExchange()})
    assert text.splitlines()[-1].startswith("НЕ ГОТОВО") and "демо-режим" in text.splitlines()[-1]
    assert ex.sent() == []


async def test_the_test_order_buys_and_sells_btc_and_writes_nothing_to_a_database() -> None:
    www = FakeExchange()
    rep, text = await run({"www.okx.com": www, "eea.okx.com": rejecting()}, test_order=True)
    assert rep.blockers == [] and text.rstrip().endswith("ГОТОВО")
    kinds = [c[0] for c in www.sent()]
    assert kinds == ["buy", "sell"]
    assert www.sent("buy")[0][1:3] == ("BTC/USDT", 2.0)
    buy_cl = www.sent("buy")[0][3]["clOrdId"]
    assert buy_cl.startswith("at95pfb") and buy_cl.isalnum() and len(buy_cl) <= 32
    assert www.sent("sell")[0][3]["tdMode"] == "cash"
    assert "покупка:" in text and "продажа:" in text and "комиссия" in text
    assert "задержка" in text and "итог круга" in text
    assert "demo_orders не пишутся" in text


async def test_a_refused_test_order_is_not_ready() -> None:
    www = FakeExchange()
    www.mode = "reject"
    rep, text = await run({"www.okx.com": www, "eea.okx.com": rejecting()}, test_order=True)
    assert text.splitlines()[-1].startswith("НЕ ГОТОВО") and "пробная покупка" in text
    assert www.sent("sell") == []
