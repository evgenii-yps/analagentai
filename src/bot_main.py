"""Точка входа сервиса телеграм-бота (только чтение, Этап 6.7).

Запускается ВНУТРИ контейнера (тот же образ, что и остальные сервисы):

    python -m src.bot_main

Каталог scripts/ в образ не копируется — весь код бота живёт в src/ (ТЗ §3.2).
БД внутри сети compose видна как postgres:5432, Redis — как redis:6379 (штатные
настройки из конфига).
"""

from __future__ import annotations

import asyncio

from src.bot.runner import run
from src.core.logging import setup_logging


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает сервис бота."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
