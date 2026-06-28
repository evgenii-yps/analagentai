"""Точка входа приложения (Этап 2 — запуск сервиса-коллектора)."""

from __future__ import annotations

import asyncio

from src.collectors.runner import run
from src.core.logging import setup_logging


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает оркестратор."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
