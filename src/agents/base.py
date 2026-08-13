"""Базовый агент и единая схема вывода ``AgentOutput``."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import get_redis
from src.notify.telegram import send_message

# TTL heartbeat-ключа агента в Redis (секунды).
_HEARTBEAT_TTL = 300

# Допустимые значения сигнала.
SIGNAL_BULLISH = "bullish"
SIGNAL_BEARISH = "bearish"
SIGNAL_NEUTRAL = "neutral"
SIGNAL_INSUFFICIENT = "insufficient_data"

# Типы сбоя итерации (Задача B): ошибка расчёта vs ошибка записи в БД.
FAILURE_COMPUTE = "compute"
FAILURE_DB_WRITE = "db_write"


def normalize_confidence(raw: float, scale: float) -> float:
    """Приводит «сырую» уверенность агента к сопоставимой шкале [0, 1] (Задача A).

    Делит сырое значение на характеристический масштаб агента (его максимально
    достижимую уверенность) и насыщает на 1.0. Чистая и детерминированная: одни
    и те же ``raw``/``scale`` → один и тот же результат, без состояния.

    Смысл: у market сырое значение уже нормировано его природой (доля голосов),
    его масштаб = 1.0 (тождество). У liquidity/futures сырое значение
    интринзически мало (сырой дисбаланс / funding-к-порогу), поэтому масштаб —
    их эмпирический максимум, чтобы 0.5 у одного агента значило примерно ту же
    степень уверенности, что 0.5 у другого.
    """
    if scale <= 0:
        return 0.0
    return round(min(max(raw, 0.0) / scale, 1.0), 4)


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

        Сбой НЕ роняет агента, но теперь ВИДЕН (Задача B): расчёт и запись в БД
        обёрнуты раздельно, каждый сбой фиксируется с типом. Heartbeat и
        success-лог достигаются ТОЛЬКО после успешной записи — то есть живым
        считается агент, реально выдавший вывод, а не просто крутящий цикл.
        ``CancelledError`` пробрасывается для graceful shutdown.
        """
        self._log.info("Агент запущен", interval=self.interval)
        while True:
            try:
                await self._iterate()
            except asyncio.CancelledError:
                self._log.info("Агент остановлен")
                raise
            await asyncio.sleep(self.interval)

    async def _iterate(self) -> None:
        """Одна итерация: раздельно ловит ошибку расчёта и ошибку записи."""
        # 1. Расчёт (ошибка здесь = баг в коде агента).
        try:
            output = await self.analyze(self.instrument_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фиксируем и продолжаем
            await self._record_failure(FAILURE_COMPUTE, exc)
            return

        # 2. Запись (ошибка здесь = временная недоступность БД).
        try:
            await db.save_agent_output(output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._record_failure(FAILURE_DB_WRITE, exc)
            return

        # 3. Успех: сбрасываем серию сбоев, обновляем heartbeat, логируем.
        await self._on_success()
        await self._heartbeat()
        self._log.info(
            "Заключение сохранено",
            signal=output.signal,
            confidence=output.confidence,
        )

    async def _record_failure(self, error_type: str, exc: Exception) -> None:
        """Делает сбой видимым: лог с типом, строка в БД, серия сбоев в Redis, алерт.

        Heartbeat при этом НЕ обновляется (мы сюда попали до шага 3), поэтому
        затяжной сбой рано или поздно проявится и устареванием heartbeat.
        """
        exc_type = type(exc).__name__
        detail = str(exc)
        self._log.warning(
            "Сбой итерации агента",
            error_type=error_type,
            exc_type=exc_type,
            error=detail[:300],
        )

        # Строка в БД — для подсчёта за период (суточная сводка). Для db_write
        # сам INSERT может не пройти (БД недоступна) — не падаем из-за этого.
        try:
            await db.record_agent_failure(self.name, error_type, exc_type, detail)
        except Exception as rec_exc:  # noqa: BLE001
            self._log.warning("Не удалось записать сбой в БД", error=str(rec_exc))

        # Серия сбоев подряд — в Redis, для алерта. Отсутствие Redis не роняет.
        try:
            await self._bump_failure_streak()
        except Exception as st_exc:  # noqa: BLE001
            self._log.warning("Не удалось обновить счётчик сбоев", error=str(st_exc))

    async def _bump_failure_streak(self) -> None:
        """Инкремент серии сбоев; при кратности порогу — алерт в Telegram."""
        key = f"agent:failures:streak:{self.name}"
        streak = await get_redis().incr(key)
        # TTL, чтобы «висящий» счётчик сам протух, если агент замолчал совсем.
        await get_redis().expire(key, _HEARTBEAT_TTL)
        threshold = settings.AGENT_FAILURE_ALERT_STREAK
        if threshold > 0 and streak >= threshold and streak % threshold == 0:
            await self._alert_failures(int(streak))

    async def _alert_failures(self, streak: int) -> None:
        """Шлёт алерт о серии сбоев. Не бросает (send_message сам гасит ошибки)."""
        text = (
            f"⚠️ <b>Агент {self.name}</b>: {streak} сбоев подряд — "
            f"выводы не записываются. Проверьте логи сервиса agents."
        )
        await send_message(text)

    async def _on_success(self) -> None:
        """Сбрасывает серию сбоев после успешной итерации."""
        try:
            await get_redis().delete(f"agent:failures:streak:{self.name}")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось сбросить счётчик сбоев", error=str(exc))

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последнего успешного анализа."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set(
            f"agent:heartbeat:{self.name}",
            now_iso,
            ex=_HEARTBEAT_TTL,
        )
