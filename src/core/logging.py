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

    # Дефект D-5: httpx на уровне INFO печатает полный URL запроса, а у Telegram
    # Bot API токен зашит прямо в URL (…/bot<TOKEN>/sendMessage). Поднимаем порог
    # логгера httpx до WARNING, чтобы токен не утекал в логи сервисов bot/notify/
    # export (и любых других, использующих httpx). Секреты — только в .env.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

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
