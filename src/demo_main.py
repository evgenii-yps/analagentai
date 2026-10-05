"""Точка входа сервиса демо-исполнения (Этап 9.5): зеркало позиций на демо-счёт OKX.

Сервис ПОСТОЯННЫЙ. Читает ``public.positions`` и для каждой виртуальной сделки
шлёт рыночный ордер на ДЕМО-счёт OKX (спот): позиция открылась — покупка,
закрылась — продажа купленного. Деньги не настоящие; цель — измерить, сколько
съедает реальное исполнение.

ЕДИНСТВЕННЫЙ СЕРВИС, ЧИТАЮЩИЙ ДЕМО-КЛЮЧИ (``OKX_DEMO_*``). Остальные сервисы их не
используют, коллекторы и анализ остаются на настоящем рынке.

ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ (``DEMO_ENABLED=false``): в этом режиме сервис к базе и
бирже не подключается, раз в час пишет в журнал «demo выключен» и обновляет
heartbeat, чтобы вотчдог не считал контейнер зависшим.

НЕВЕРНЫЕ НАСТРОЙКИ РОНЯЮТ ТОЛЬКО ЭТОТ СЕРВИС при старте с понятным текстом
(``Settings.demo_config_errors``); остальные сервисы с общим ``.env`` не затронуты.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from typing import Any

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.logging import setup_logging
from src.core.redis_client import close_redis, get_redis
from src.demo import runner
from src.demo.exchange import create_demo_exchange
from src.demo.report import daily_report
from src.notify.telegram import send_message

_DISABLED_LOG_EVERY_SEC = 3600


async def _disabled_loop() -> None:
    """Выключенный сервис: журнал раз в час, heartbeat на каждом шаге цикла."""
    log = structlog.get_logger().bind(component="demo")
    last_log: float | None = None
    loop = asyncio.get_running_loop()
    redis = get_redis()
    while True:
        if last_log is None or loop.time() - last_log >= _DISABLED_LOG_EVERY_SEC:
            log.info("demo выключен (DEMO_ENABLED=false)", enabled=False,
                     host=settings.OKX_DEMO_HOST)
            last_log = loop.time()
        try:
            await redis.set(
                runner.HEARTBEAT_KEY, datetime.now(UTC).isoformat(),
                ex=runner.HEARTBEAT_TTL,
            )
        except Exception as exc:  # noqa: BLE001 — Redis вернётся
            log.warning("demo_heartbeat_failed=1", error=str(exc))
        await asyncio.sleep(max(1, int(settings.DEMO_INTERVAL)))


async def _require_schema() -> None:
    """Таблицы миграции 030 обязаны существовать: сервис их не создаёт сам."""
    present = await db.pool.fetchval(
        "SELECT to_regclass('public.demo_orders') IS NOT NULL "
        "AND to_regclass('public.demo_state') IS NOT NULL;"
    )
    if not present:
        raise RuntimeError(
            "нет таблиц demo_orders / demo_state: сначала примените миграцию "
            "db/migrations/030_demo_orders.sql"
        )


async def _main() -> None:
    log = structlog.get_logger().bind(component="demo")

    errors = settings.demo_config_errors()
    if errors:
        for line in errors:
            log.error("demo_config_error=1", error=line)
        raise SystemExit("Сервис demo не запущен — неверные настройки:\n- " + "\n- ".join(errors))

    if not settings.DEMO_ENABLED:
        await _disabled_loop()
        return

    await db.connect()
    get_redis()
    exchange: Any = None
    try:
        await _require_schema()
        config = runner.DemoConfig.from_settings(settings)
        exchange = create_demo_exchange(
            settings.OKX_DEMO_API_KEY, settings.OKX_DEMO_SECRET_KEY,
            settings.OKX_DEMO_PASSPHRASE, config.host,
        )
        # mirror_since фиксируется ПОСЛЕ создания клиента: упавший старт (например,
        # из-за ключа) не должен начинать отсчёт зеркалирования.
        mirror_since = await runner.ensure_state(db.pool, config.host, datetime.now(UTC))
        ctx = runner.Context(
            pool=db.pool,
            exchange=exchange,
            redis=get_redis(),
            config=config,
            symbols=[pair.spot for pair in settings.symbol_pairs],
            secrets=(settings.OKX_DEMO_API_KEY, settings.OKX_DEMO_SECRET_KEY,
                     settings.OKX_DEMO_PASSPHRASE),
            notify=send_message,
            mirror_since=mirror_since,
        )
        if settings.EXCHANGE != "okx":
            log.warning("demo_exchange_mismatch=1", exchange=settings.EXCHANGE,
                        note="инструменты собираются не с OKX; зеркало идёт на OKX")

        task = asyncio.create_task(runner.run(ctx, report=daily_report), name="demo")
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            log.info("Сигнал остановки получен")
            task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                pass
        try:
            await task
        except asyncio.CancelledError:
            log.info("Сервис demo остановлен")
    finally:
        if exchange is not None:
            await exchange.close()
        await db.close()
        await close_redis()
        log.info("Ресурсы освобождены")


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает сервис."""
    setup_logging()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
