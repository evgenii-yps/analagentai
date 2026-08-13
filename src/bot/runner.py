"""Планировщик сервиса бота: подготовка роли ro, запуск поллера, shutdown.

Сервис бота подключается к БД ТОЛЬКО ролью ``agenttrade_ro`` (SELECT). Роль
создаётся идемпотентно при старте под основным пользователем — исключительно
для этого одноразового действия; все запросы бота идут уже под ролью ro. Если
роль создать не удаётся, бот НЕ подключается основным пользователем «пока что»
(ТЗ §8), а продолжает попытки, оставаясь живым (не роняет контейнер).
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

import asyncpg
import structlog

from src.bot.poller import BotPoller
from src.bot.queries import BotQueries, ensure_readonly_role
from src.core.config import mask_secret, settings
from src.core.redis_client import close_redis, get_redis

# Пауза между попытками подготовить роль ro при недоступной БД (секунды).
_RETRY_SEC = 15

# TTL heartbeat-ключа в режиме простоя (секунды).
_HEARTBEAT_TTL = 300


async def run() -> None:
    """Поднимает инфраструктуру, запускает бота (или простой) и ждёт остановки."""
    log = structlog.get_logger()
    log.info(
        "Запуск сервиса бота Agent Trade (Этап 6.7)",
        enabled=settings.BOT_ENABLED,
        poll_timeout=settings.BOT_POLL_TIMEOUT,
        token=mask_secret(settings.TELEGRAM_BOT_TOKEN),
        allowed_chats=len(settings.bot_allowed_chat_ids),
    )

    get_redis()  # прогрев клиента Redis

    # Конфигурационные предусловия: при их отсутствии сервис не падает, а
    # простаивает с понятным предупреждением (как notify по §8 ТЗ Этапа 5).
    reason = _blocking_reason()
    if reason is not None:
        log.warning("Бот простаивает", причина=reason)
        main = _idle_loop(log)
    else:
        main = _run_bot(log)

    tasks: list[asyncio.Task[None]] = [asyncio.create_task(main, name="bot")]
    try:
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем сервис бота")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_redis()
        log.info("Сервис бота остановлен, ресурсы освобождены")


def _blocking_reason() -> str | None:
    """Причина простоя сервиса (None — можно работать)."""
    if not settings.BOT_ENABLED:
        return "сервис выключен (BOT_ENABLED=false)"
    if not settings.TELEGRAM_BOT_TOKEN:
        return "не задан TELEGRAM_BOT_TOKEN"
    if not settings.POSTGRES_RO_PASSWORD:
        return "не задан POSTGRES_RO_PASSWORD — роль только на чтение недоступна"
    return None


async def _run_bot(log: structlog.types.WrappedLogger) -> None:
    """Готовит роль ro, открывает RO-пул и крутит поллер до остановки."""
    ro_pool = await _wait_readonly(log)
    try:
        poller = BotPoller(BotQueries(ro_pool))
        await poller.run()
    finally:
        await ro_pool.close()


async def _wait_readonly(log: structlog.types.WrappedLogger) -> asyncpg.Pool:
    """Подготавливает роль ro и RO-пул, повторяя при недоступной БД.

    Никогда не подключается основным пользователем для запросов бота: основное
    соединение используется лишь для одноразового создания роли (ТЗ §8).
    """
    while True:
        try:
            await _create_readonly_role(log)
            pool = await asyncpg.create_pool(
                dsn=settings.pg_dsn_ro, min_size=1, max_size=5
            )
            log.info("RO-пул agenttrade_ro готов")
            return pool
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — БД может быть ещё не готова
            log.warning(
                "Не удалось подготовить роль ro — повтор",
                error=str(exc),
                retry_sec=_RETRY_SEC,
            )
            await _heartbeat_once()
            await asyncio.sleep(_RETRY_SEC)


async def _create_readonly_role(log: structlog.types.WrappedLogger) -> None:
    """Создаёт/обновляет роль ro под основным пользователем (одноразово)."""
    admin = await asyncpg.connect(dsn=settings.pg_dsn)
    try:
        await ensure_readonly_role(
            admin, settings.POSTGRES_RO_PASSWORD, settings.POSTGRES_DB
        )
    finally:
        await admin.close()


async def _idle_loop(log: structlog.types.WrappedLogger) -> None:
    """Простой: держим heartbeat, чтобы вотчдог видел контейнер, и ждём остановки."""
    while True:
        try:
            await _heartbeat_once()
            await asyncio.sleep(min(settings.BOT_POLL_TIMEOUT, 60))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка heartbeat в простое", error=str(exc))
            await asyncio.sleep(_RETRY_SEC)


async def _heartbeat_once() -> None:
    """Пишет bot:heartbeat (ISO, TTL 300) — единый формат со всеми сервисами."""
    await get_redis().set("bot:heartbeat", datetime.now(UTC).isoformat(), ex=_HEARTBEAT_TTL)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    log: structlog.types.WrappedLogger,
) -> None:
    """Вешает обработчики SIGINT/SIGTERM, отменяющие задачи (graceful shutdown)."""

    def _shutdown() -> None:
        log.info("Сигнал остановки получен")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass
