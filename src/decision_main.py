"""Точка входа сервиса решений (Decision Agent, Этап 4)."""

from __future__ import annotations

import asyncio

from src.core.logging import setup_logging
from src.decision.runner import run


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает Decision Agent."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
