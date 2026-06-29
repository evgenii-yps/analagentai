"""Точка входа сервиса уведомлений (Telegram, Этап 5)."""

from __future__ import annotations

import asyncio

from src.core.logging import setup_logging
from src.notify.runner import run


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает сервис уведомлений."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
