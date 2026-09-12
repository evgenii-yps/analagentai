"""Планировщик сервиса уведомлений: запуск, gather и graceful shutdown."""

from __future__ import annotations

import asyncio
import signal

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.instruments import horizon_label
from src.core.redis_client import close_redis, get_redis
from src.notify import daily_trades
from src.notify.agent import NotifyAgent, signal_notifications_enabled

# ПОЛЯ СТРОКИ ЗАПУСКА СЛУЖБЫ УВЕДОМЛЕНИЙ, НАЗВАННЫЕ §8 ТЗ 9.3 ПОИМЁННО.
# Перечень вынесен константой затем, чтобы проверка приёмки сверяла его с
# требованием, а не с тем, что случайно оказалось в форматной строке.
STARTUP_FIELDS: tuple[str, ...] = (
    "notify_signals_enabled", "notify_trades_enabled",
    "notify_daily_summary_utc", "notify_trades_max_per_hour",
)


def startup_fields() -> dict[str, object]:
    """Величины строки запуска службы уведомлений (§8 ТЗ 9.3).

    ПЕЧАТАЮТСЯ ФАКТИЧЕСКИ ПРИМЕНЁННЫЕ ЗНАЧЕНИЯ, А НЕ УМОЛЧАНИЯ КОДА (§8 ТЗ):
    значения читаются из ``settings`` в момент вызова.

    ``notify_trades_enabled`` БЕРЁТСЯ ИЗ ``POSITION_NOTIFY_ENABLED``, и второй
    настройки для той же величины не заводится. §3 ТЗ прямо это запрещает
    («заводить второе имя для той же величины запрещено — это отдельный класс
    дефекта, уже случавшийся в проекте»), а выключатель сообщений о сделках в
    проекте есть с этапа 9.1 и живёт рядом с самими сообщениями — в службе
    позиций, которая их и шлёт. Соответствие имён приведено в отчёте.
    """
    return {
        "notify_signals_enabled": bool(settings.NOTIFY_SIGNALS_ENABLED),
        "notify_trades_enabled": bool(settings.POSITION_NOTIFY_ENABLED),
        "notify_daily_summary_utc": str(settings.NOTIFY_DAILY_SUMMARY_UTC),
        "notify_trades_max_per_hour": int(settings.NOTIFY_TRADES_MAX_PER_HOUR),
    }


async def run() -> None:
    """Поднимает инфраструктуру, запускает сервис уведомлений и ждёт остановки."""
    log = structlog.get_logger()
    # СТРОКА ЗАПУСКА НАЗЫВАЕТ ГЛАВНОЕ СВОЙСТВО СЕРВИСА ПРЯМО (§8 ТЗ 9.3):
    # уходят ли сообщения о сигналах вообще. Пороги и ограничения потока рядом
    # печатаются по-прежнему — они остались в коде и в настройках (§6.1), —
    # и без явного признака их присутствие в журнале читалось бы как «сигналы
    # шлются, просто с ограничениями».
    signals_to_telegram = signal_notifications_enabled()
    log.info(
        "Запуск сервиса уведомлений Agent Trade (Этап 5)",
        interval=settings.NOTIFY_INTERVAL,
        min_probability=settings.NOTIFY_MIN_PROBABILITY,
        telegram_configured=settings.telegram_configured,
        hold_min=settings.NOTIFY_HOLD_MIN,
        max_per_hour=settings.NOTIFY_MAX_PER_HOUR,
        logic_version=settings.LOGIC_VERSION,
        **startup_fields(),
    )
    if not signals_to_telegram:
        log.info(
            "notify_signal_messages_off=1",
            logic_version=int(settings.LOGIC_VERSION),
            reason=(
                "NOTIFY_SIGNALS_ENABLED=false: в Telegram уходят только "
                "события сделок; сигналы собираются, оцениваются и пишутся в "
                "базу как прежде"
            ),
        )

    await db.connect()
    get_redis()
    # Идемпотентно гарантируем наличие колонки notified (на старом томе).
    await db.ensure_notify_schema()
    # Этап 7.3: колонки калибровки читаются выборкой кандидатов, поэтому сервис
    # уведомлений тоже гарантирует их наличие (порядок старта сервисов не задан).
    await db.ensure_calibration_schema()
    await db.ensure_signals_inertia()
    # Настройки пользователя (§1 ТЗ 8.3): порядок старта сервисов не задан,
    # поэтому наличие таблицы гарантирует и тот, кто её только читает.
    await db.ensure_user_settings_schema()

    if not settings.telegram_configured:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сервис простаивает")

    tasks: list[asyncio.Task[None]] = []
    try:
        agent = NotifyAgent(
            interval=settings.NOTIFY_INTERVAL,
            min_probability=settings.NOTIFY_MIN_PROBABILITY,
            cooldown_sec=settings.NOTIFY_COOLDOWN_SEC,
            symbol=settings.SYMBOL,
            tz_name=settings.NOTIFY_TIMEZONE,
            primary_horizon=horizon_label(settings.eval_primary_horizon_h),
            min_agents=settings.NOTIFY_MIN_AGENTS,
            use_calibrated=settings.NOTIFY_USE_CALIBRATED,
            min_calibrated=settings.NOTIFY_MIN_CALIBRATED,
            hold_sec=settings.NOTIFY_HOLD_MIN * 60,
            max_per_hour=settings.NOTIFY_MAX_PER_HOUR,
            recipients=[int(chat) for chat in settings.bot_allowed_chat_ids],
        )
        tasks = [
            asyncio.create_task(agent.run(), name="notify"),
            # СУТОЧНАЯ СВОДКА ПО СДЕЛКАМ — ОТДЕЛЬНОЙ ЗАДАЧЕЙ (§6.2 ТЗ 9.3), а
            # не шагом внутри цикла уведомлений. Цикл уведомлений выходит
            # досрочно, когда сигнальные сообщения выключены (а они выключены
            # весь этап), и сводка, встроенная в него шагом, не ушла бы ни
            # разу — при совершенно честном виде журнала.
            asyncio.create_task(daily_trades.run_loop(), name="daily-trades"),
        ]

        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, tasks, log)

        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Получен сигнал остановки, завершаем сервис уведомлений")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        await close_redis()
        log.info("Сервис уведомлений остановлен, ресурсы освобождены")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    tasks: list[asyncio.Task[None]],
    log: structlog.types.WrappedLogger,
) -> None:
    """Вешает обработчики SIGINT/SIGTERM, отменяющие задачи."""

    def _shutdown() -> None:
        log.info("Сигнал остановки получен")
        for task in tasks:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass
