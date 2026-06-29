"""Точка входа сервиса оценки результатов (Этап 6)."""

from __future__ import annotations

import asyncio

from src.core.logging import setup_logging
from src.evaluator.runner import run


def main() -> None:
    """Синхронная точка входа: настраивает логи и запускает оценщик."""
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
