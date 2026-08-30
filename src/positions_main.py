"""Точка входа сервиса ведения позиций (Этап 9.1, позиции ВИРТУАЛЬНЫЕ).

Сервис ПОСТОЯННЫЙ, а не задача из cron: позиция закрывается по касанию внутри
минуты, и суточный прогон пропустил бы и цель, и предел — он увидел бы только
то положение цены, в котором она оказалась к моменту прогона.

К БИРЖЕ ЭТОТ СЕРВИС НЕ ОБРАЩАЕТСЯ. Он читает только собственные свечи из
``public.ohlcv``; ключи API биржи он не читает и читать не должен. Исходящих
обращений у него ровно два, и оба выключаются настройкой:

  * уведомление в Telegram той же функцией, что у сервиса ``notify``
    (``POSITION_NOTIFY_ENABLED``);
  * запись закрытой позиции в Google Таблицу владельца через приёмник Apps
    Script (``POSITIONS_SHEETS_ENABLED``, Этап 9.1.1 §7; ПО УМОЛЧАНИЮ
    ВЫКЛЮЧЕНО).

ФЛАГ ``--dry-run`` (Этап 9.1.1 §7.8) печатает СТРОКУ, КОТОРУЮ ЗАПИСАЛ БЫ в
Google Таблицу по последней закрытой позиции нужного инструмента, и НЕ ПИШЕТ
НИЧЕГО — ни в лист, ни в базу. Владелец сверяет её с листом глазами; пока
сверка не пройдена, ``POSITIONS_SHEETS_ENABLED`` остаётся выключенным. Проверка
работает и при выключенном флаге записи: в этом и её смысл — посмотреть до
того, как включать.

    docker compose run --rm --no-deps positions \\
        python -m src.positions_main --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.logging import setup_logging
from src.core.redis_client import close_redis, get_redis
from src.positions.runner import run
from src.positions.sheet import dry_run_text


async def _dry_run() -> int:
    """Печатает строку для листа по последней закрытой позиции. Ничего не пишет.

    Соединение с базой открывается на чтение и закрывается сразу; постоянный
    цикл при этом НЕ ЗАПУСКАЕТСЯ вовсе — иначе «посмотреть» означало бы
    «запустить сервис», и проверка перед первой записью сама стала бы событием.
    """
    log = structlog.get_logger()
    await db.connect()
    try:
        row = await db.get_last_position_for_sheet(
            instrument_symbol=settings.POSITIONS_SHEET_INSTRUMENT
        )
    finally:
        await db.close()
    print(dry_run_text(
        row,
        timezone_name=settings.NOTIFY_TIMEZONE,
        sheet_name=settings.POSITIONS_SHEET_NAME,
    ))
    print()
    print(
        f"  POSITIONS_SHEETS_ENABLED={settings.POSITIONS_SHEETS_ENABLED} — запись "
        + ("ВКЛЮЧЕНА" if settings.POSITIONS_SHEETS_ENABLED
           else "выключена, в лист ничего не уходит")
    )
    log.info("positions_sheet_dry_run=1", component="positions",
             instrument=settings.POSITIONS_SHEET_INSTRUMENT,
             found=row is not None)
    return 0


async def _main() -> None:
    """Поднимает инфраструктуру, запускает цикл и ждёт остановки."""
    log = structlog.get_logger()

    # ВЫКЛЮЧЕННЫЙ СЕРВИС НЕ ПОДКЛЮЧАЕТСЯ К БАЗЕ. Держать соединение ради
    # ничегонеделания — значит занимать слот пула и выглядеть работающим в
    # мониторинге, ничего при этом не делая.
    if not settings.POSITIONS_ENABLED:
        log.info(
            "POSITIONS_ENABLED=false — сервис ведения позиций простаивает",
            component="positions",
        )
        await asyncio.Event().wait()
        return

    log.info(
        "Запуск сервиса ведения позиций (Этап 9.1)",
        component="positions",
        interval=settings.POSITION_INTERVAL,
        horizon_h=settings.POSITION_HORIZON_H,
        slot_usd=settings.POSITION_SLOT_USD,
        max_open=settings.POSITION_MAX_OPEN,
        virtual=True,
    )

    await db.connect()
    get_redis()
    # Схема гарантируется сервисом при старте: миграция 018 могла быть не
    # применена на уже работающем томе, и без таблицы сервис молча простаивал бы.
    await db.ensure_positions_schema()
    # Колонка sheet_exported_at идёт ОТДЕЛЬНОЙ миграцией 019: миграция 018 уже
    # применена на сервере и не редактируется. Без колонки выгрузка в лист
    # писала бы вторую строку той же сделки после каждого перезапуска.
    await db.ensure_positions_sheet_column()

    task = asyncio.create_task(run(), name="positions")
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        log.info("Сигнал остановки получен", component="positions")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        await task
    except asyncio.CancelledError:
        log.info("Сервис ведения позиций остановлен", component="positions")
    finally:
        await db.close()
        await close_redis()
        log.info("Ресурсы освобождены", component="positions")


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает сервис."""
    parser = argparse.ArgumentParser(
        description=(
            "Сервис ведения позиций (Этап 9.1, позиции ВИРТУАЛЬНЫЕ). Без "
            "аргументов — постоянный цикл."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "напечатать строку, которую записал бы в Google Таблицу по "
            "последней закрытой позиции, и выйти. Ничего не записывает."
        ),
    )
    args = parser.parse_args()
    setup_logging()
    if args.dry_run:
        sys.exit(asyncio.run(_dry_run()))
    asyncio.run(_main())


if __name__ == "__main__":
    main()
