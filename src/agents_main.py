"""Точка входа сервиса аналитических агентов (Этап 3)."""

from __future__ import annotations

import asyncio

from src.agents.runner import run
from src.core.logging import setup_logging


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает планировщик агентов."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
