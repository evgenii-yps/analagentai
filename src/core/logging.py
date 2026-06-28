"""Настройка структурного логирования через structlog.

Вывод идёт в stdout. Локально (TTY) используется читаемый формат,
в проде — JSON. Уровень берётся из ``LOG_LEVEL``.
"""

from __future__ import annotations

import logging
import sys

import structlog

from src.core.config import settings


def setup_logging() -> None:
    """Настраивает structlog и стандартный logging по ``LOG_LEVEL``."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Базовая конфигурация stdlib logging (structlog пишет через него в stdout).
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # Читаемый рендер для терминала, JSON — для прода (когда stdout не TTY).
    is_tty = sys.stdout.isatty()
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if is_tty
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
