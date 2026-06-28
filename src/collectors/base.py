"""Базовый коллектор: устойчивый бесконечный цикл сбора с heartbeat."""

from __future__ import annotations

import abc
import asyncio
from datetime import UTC, datetime

import structlog

from src.core.redis_client import get_redis

# TTL heartbeat-ключа в Redis (секунды).
_HEARTBEAT_TTL = 300


class BaseCollector(abc.ABC):
    """Абстрактный коллектор с циклом, не падающим при ошибках сети/API."""

    def __init__(self, name: str, interval: float) -> None:
        self.name = name
        self.interval = interval
        self._log = structlog.get_logger().bind(collector=name)

    @abc.abstractmethod
    async def collect_once(self) -> None:
        """Одна итерация сбора данных. Реализуется наследниками."""
        raise NotImplementedError

    async def run(self) -> None:
        """Бесконечный цикл: сбор → heartbeat → пауза.

        Любые исключения (кроме отмены задачи) логируются как warning и
        НЕ роняют коллектор. ``CancelledError`` пробрасывается дальше для
        корректного graceful shutdown.
        """
        self._log.info("Коллектор запущен", interval=self.interval)
        while True:
            try:
                await self.collect_once()
                await self._heartbeat()
            except asyncio.CancelledError:
                self._log.info("Коллектор остановлен")
                raise
            except Exception as exc:
                # Ошибка сети/API/БД: фиксируем и продолжаем после паузы.
                self._log.warning("Ошибка итерации сбора", error=str(exc))
            await asyncio.sleep(self.interval)

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последнего успешного сбора."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set(
            f"collector:heartbeat:{self.name}",
            now_iso,
            ex=_HEARTBEAT_TTL,
        )
