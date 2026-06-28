"""Базовый агент и единая схема вывода ``AgentOutput``."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.core.db import db
from src.core.redis_client import get_redis

# TTL heartbeat-ключа агента в Redis (секунды).
_HEARTBEAT_TTL = 300

# Допустимые значения сигнала.
SIGNAL_BULLISH = "bullish"
SIGNAL_BEARISH = "bearish"
SIGNAL_NEUTRAL = "neutral"
SIGNAL_INSUFFICIENT = "insufficient_data"


@dataclass
class AgentOutput:
    """Единое заключение агента.

    ``signal`` — только направление (bullish/bearish/neutral) или
    ``insufficient_data``; решение «покупать/продавать» НЕ здесь, его примет
    Decision Agent (Этап 4). Для ``insufficient_data`` ``confidence`` = 0.
    """

    agent: str
    instrument_id: int
    signal: str
    confidence: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @classmethod
    def insufficient(
        cls,
        agent: str,
        instrument_id: int,
        rationale: str,
        metrics: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Удобный конструктор результата «недостаточно данных»."""
        return cls(
            agent=agent,
            instrument_id=instrument_id,
            signal=SIGNAL_INSUFFICIENT,
            confidence=0.0,
            metrics=metrics or {},
            rationale=rationale,
        )


class BaseAgent(abc.ABC):
    """Абстрактный агент с устойчивым циклом анализа.

    Каждый агент читает ТОЛЬКО свои входные данные (свечи / стакан / funding+OI)
    и не обращается к выводам других агентов — независимость заложена в коде.
    """

    def __init__(self, name: str, interval: float, instrument_id: int) -> None:
        self.name = name
        self.interval = interval
        self.instrument_id = instrument_id
        self._log = structlog.get_logger().bind(agent=name)

    @abc.abstractmethod
    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает свои данные, считает показатели и возвращает заключение."""
        raise NotImplementedError

    async def run(self) -> None:
        """Бесконечный цикл: analyze → сохранить → heartbeat → пауза.

        Любые исключения (кроме отмены задачи) логируются как warning и
        НЕ роняют агента. ``CancelledError`` пробрасывается для graceful shutdown.
        """
        self._log.info("Агент запущен", interval=self.interval)
        while True:
            try:
                output = await self.analyze(self.instrument_id)
                await db.save_agent_output(output)
                await self._heartbeat()
                self._log.info(
                    "Заключение сохранено",
                    signal=output.signal,
                    confidence=output.confidence,
                )
            except asyncio.CancelledError:
                self._log.info("Агент остановлен")
                raise
            except Exception as exc:
                # Ошибка БД/расчёта: фиксируем и продолжаем после паузы.
                self._log.warning("Ошибка итерации анализа", error=str(exc))
            await asyncio.sleep(self.interval)

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последнего успешного анализа."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set(
            f"agent:heartbeat:{self.name}",
            now_iso,
            ex=_HEARTBEAT_TTL,
        )
