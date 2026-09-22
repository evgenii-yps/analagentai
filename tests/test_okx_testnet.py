"""ТЗ «Подключение OKX testnet»: конфиг, фабрика клиента, graceful degradation.

Сетевых обращений эти тесты не делают: и создание ccxt-клиента, и включение
sandbox-режима, и разбор ошибок — чистые операции над объектом клиента/памятью,
проверяемые без реальной OKX (см. ту же логику в
``tests/test_stage_8_10_1.py::test_ccxt_client_carries_the_browser_signature``).
"""

from __future__ import annotations

import ccxt.async_support as ccxt
import pytest

import src.collectors.ohlcv as ohlcv_module
from src.collectors.ohlcv import OHLCVCollector
from src.core.config import Settings
from src.core.exchange import create_exchange

# --- Settings: TESTNET требует ключи, но ТОЛЬКО при EXCHANGE=okx -------------


def test_production_okx_without_testnet_needs_no_keys() -> None:
    """Продакшн (EXCHANGE=okx, TESTNET по умолчанию false) не требует ключей.

    Это ГЛАВНАЯ проверка обратной совместимости: установщик (deploy/install.sh)
    пишет EXCHANGE=okx и никогда не собирает OKX_API_KEY/SECRET/PASSPHRASE —
    новая проверка обязана молчать в точности в этой конфигурации.
    """
    fresh = Settings(POSTGRES_PASSWORD="x", EXCHANGE="okx")
    assert fresh.TESTNET is False
    assert fresh.OKX_API_KEY == ""


def test_binance_ignores_testnet_flag() -> None:
    """EXCHANGE=binance с TESTNET=true не падает — testnet имеет смысл только для okx."""
    fresh = Settings(POSTGRES_PASSWORD="x", EXCHANGE="binance", TESTNET=True)
    assert fresh.TESTNET is True


def test_okx_testnet_without_keys_fails_at_startup() -> None:
    """EXCHANGE=okx TESTNET=true без ключей роняет сервис на старте, а не молчит."""
    with pytest.raises(ValueError, match="OKX_API_KEY"):
        Settings(POSTGRES_PASSWORD="x", EXCHANGE="okx", TESTNET=True)


def test_okx_testnet_with_partial_keys_still_fails() -> None:
    """Частично заполненные ключи — тоже отказ: подписанный запрос нужен весь набор."""
    with pytest.raises(ValueError, match="OKX_SECRET_KEY"):
        Settings(
            POSTGRES_PASSWORD="x",
            EXCHANGE="okx",
            TESTNET=True,
            OKX_API_KEY="k",
            OKX_PASSPHRASE="p",
        )


def test_okx_testnet_with_full_keys_starts() -> None:
    """Все три ключа заданы — сервис стартует."""
    fresh = Settings(
        POSTGRES_PASSWORD="x",
        EXCHANGE="okx",
        TESTNET=True,
        OKX_API_KEY="k",
        OKX_SECRET_KEY="s",
        OKX_PASSPHRASE="p",
    )
    assert fresh.TESTNET is True


# --- create_exchange: фабрика клиента -----------------------------------------


def test_unsupported_exchange_rejected() -> None:
    with pytest.raises(ValueError, match="не поддерживается"):
        create_exchange("kraken")


def test_binance_client_unaffected_by_testnet_default() -> None:
    """Без testnet=True конфиг клиента не содержит ни sandbox, ни ключей."""
    exchange = create_exchange("binance")
    assert exchange.apiKey in (None, "")
    assert not getattr(exchange, "isSandboxModeEnabled", False)


def test_okx_production_client_has_no_sandbox_and_no_keys() -> None:
    """EXCHANGE=okx без testnet — тот же клиент, что и сейчас в продакшне:
    без ключей, без sandbox-режима, с браузерной подписью (Этап 8.10.1)."""
    exchange = create_exchange("okx")
    assert exchange.apiKey in (None, "")
    assert not getattr(exchange, "isSandboxModeEnabled", False)
    assert "x-simulated-trading" not in exchange.headers


def test_okx_testnet_without_credentials_is_rejected_defensively() -> None:
    """Фабрика проверяет ключи САМА, а не полагается только на Settings —
    её можно вызвать напрямую, в обход валидатора конфига."""
    with pytest.raises(ValueError, match="testnet требует"):
        create_exchange("okx", testnet=True)


def test_testnet_requires_okx() -> None:
    """testnet=True для любой другой биржи — явная ошибка, а не тихий no-op."""
    with pytest.raises(ValueError, match="только для okx"):
        create_exchange("binance", testnet=True, api_key="k", secret_key="s", passphrase="p")


def test_okx_testnet_client_is_sandboxed_and_signed() -> None:
    """Ключи есть → клиент в sandbox-режиме, с ключами и с той же браузерной
    подписью, что и продакшн-клиент (единое место — src.core.http)."""
    exchange = create_exchange(
        "okx", testnet=True, api_key="my-key", secret_key="my-secret", passphrase="my-pass"
    )
    assert exchange.isSandboxModeEnabled is True
    assert exchange.apiKey == "my-key"
    assert exchange.secret == "my-secret"
    assert exchange.password == "my-pass"
    # Песочница — тот же домен, ccxt лишь подставляет заголовок (§3.1 ТЗ:
    # "testnet работает в том же домене; параметр в ccxt").
    assert exchange.headers.get("x-simulated-trading") == "1"
    assert exchange.headers.get("User-Agent", "").startswith("Mozilla/")


# --- OHLCVCollector: неподдерживаемый таймфрейм не блокирует остальные -------


class _FakeExchange:
    """Минимальная подделка ccxt-клиента: одному таймфрейму выдаёт NotSupported."""

    id = "okx"

    def __init__(self, unsupported: set[str]) -> None:
        self.unsupported = unsupported
        self.requested: list[str] = []

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        self.requested.append(timeframe)
        if timeframe in self.unsupported:
            raise ccxt.NotSupported(f"{symbol} {timeframe} not supported")
        return [[1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 10.0]]


@pytest.mark.asyncio
async def test_unsupported_timeframe_does_not_block_the_rest(monkeypatch) -> None:
    """Один неподдерживаемый таймфрейм пропускается (warning), остальные — собираются.

    До правки ``fetch_ohlcv`` не был обёрнут ПО ТАЙМФРЕЙМУ: исключение на первом
    же элементе списка прерывало всю итерацию ``collect_once``, и таймфреймы
    после него не собрались бы НИ РАЗУ — ошибка повторялась бы на одном и том же
    месте до бесконечности (§3.3, §4.3 ТЗ: «пропустить с graceful degradation»).
    """
    saved: list[tuple[int, str, list]] = []

    async def fake_upsert(instrument_id: int, timeframe: str, candles: list) -> None:
        saved.append((instrument_id, timeframe, candles))

    monkeypatch.setattr(ohlcv_module.db, "upsert_ohlcv", fake_upsert)

    exchange = _FakeExchange(unsupported={"3m"})
    collector = OHLCVCollector(
        exchange, instrument_id=1, symbol="BTC/USDT",
        timeframes=["1m", "3m", "5m"], interval=30,
    )

    await collector.collect_once()

    assert exchange.requested == ["1m", "3m", "5m"]
    assert [tf for _, tf, _ in saved] == ["1m", "5m"]
