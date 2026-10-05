"""Этап 9.5, §4.1 и §6.1: точка входа — выключенный режим, ошибки настроек, схема."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import demo_main
from src.core.config import Settings
from src.demo import runner
from tests.demo_support import SECRETS, FakeRedis, capture_demo_logs


def make_settings(**kw) -> Settings:
    return Settings(POSTGRES_PASSWORD="x", _env_file=None, DEMO_INTERVAL=1, **kw)


async def test_a_disabled_service_touches_neither_the_database_nor_the_exchange(
    monkeypatch
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(demo_main, "settings", make_settings())
    monkeypatch.setattr(demo_main, "get_redis", lambda: redis)

    async def boom(*a, **k):
        raise AssertionError("выключенный сервис обратился к базе")

    monkeypatch.setattr(demo_main.db, "connect", boom)
    monkeypatch.setattr(demo_main, "create_demo_exchange", boom)

    with capture_demo_logs(monkeypatch) as logs:
        task = asyncio.create_task(demo_main._main())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert runner.HEARTBEAT_KEY in redis.data and redis.ttl[runner.HEARTBEAT_KEY] == 300
    assert any("выключен" in str(e["event"]) for e in logs.entries)


async def test_invalid_settings_stop_only_this_service_with_a_clear_text(monkeypatch) -> None:
    monkeypatch.setattr(demo_main, "settings", make_settings(
        DEMO_ENABLED=True, OKX_DEMO_API_KEY=SECRETS[0], OKX_DEMO_HOST="okx.example.com"))
    with capture_demo_logs(monkeypatch) as logs, pytest.raises(SystemExit) as info:
        await demo_main._main()
    message = str(info.value)
    assert "OKX_DEMO_SECRET_KEY" in message and "OKX_DEMO_PASSPHRASE" in message
    assert "OKX_DEMO_HOST" in message
    assert SECRETS[0] not in message and SECRETS[0] not in logs.text()


async def test_missing_migration_stops_the_enabled_service_with_a_clear_text(monkeypatch) -> None:
    class Pool:
        async def fetchval(self, *a, **k):
            return False

    monkeypatch.setattr(demo_main, "db", SimpleNamespace(pool=Pool()))
    with pytest.raises(RuntimeError, match="030_demo_orders.sql"):
        await demo_main._require_schema()


async def test_the_exchange_is_created_in_demo_mode_with_the_configured_host(monkeypatch) -> None:
    from src.demo import exchange as dx

    ex = dx.create_demo_exchange(SECRETS[0], SECRETS[1], SECRETS[2], "eea.okx.com")
    try:
        assert ex.hostname == "eea.okx.com" and ex.isSandboxModeEnabled
        assert ex.apiKey == SECRETS[0]
    finally:
        await ex.close()


def test_the_config_for_the_service_comes_from_settings() -> None:
    cfg = runner.DemoConfig.from_settings(make_settings(
        OKX_DEMO_HOST="eea.okx.com", DEMO_MAX_ENTRY_DELAY_SEC=99, DEMO_FILL_WAIT_SEC=3,
        DEMO_MIN_USDT_BALANCE=12.5, DEMO_START_CAPITAL_USD=500, DEMO_NOTIFY_ENABLED=False,
        NOTIFY_TRADES_MAX_PER_HOUR=7, NOTIFY_TIMEZONE="Europe/Berlin"))
    assert (cfg.host, cfg.max_entry_delay_sec, cfg.fill_wait_sec) == ("eea.okx.com", 99, 3)
    assert str(cfg.min_usdt_balance) == "12.5" and cfg.interval_sec == 1
    assert cfg.start_capital_usd == 500 and cfg.notify_enabled is False
    assert cfg.trades_max_per_hour == 7 and cfg.timezone == "Europe/Berlin"
