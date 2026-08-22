"""Базовый коллектор: устойчивый бесконечный цикл сбора с heartbeat."""

from __future__ import annotations

import abc
import asyncio
from datetime import UTC, datetime

import structlog

from src.core.redis_client import get_redis

# TTL heartbeat-ключа в Redis (секунды).
_HEARTBEAT_TTL = 300

# Как часто печатать сводку обращений к бирже (секунды). §3 ТЗ 8.1 требует
# знать число обращений в минуту и число ответов 50011 — а успешная итерация
# логируется на уровне DEBUG, который в продакшне выключен. Без этой сводки
# счётчик обращений оказался бы нулевым при живом коллекторе.
_STATS_EVERY_SEC = 60

# Код OKX «слишком много запросов». Ловится по тексту ошибки: ccxt заворачивает
# ответ биржи в исключение, и код остаётся в сообщении.
RATE_LIMIT_CODE = "50011"


class BaseCollector(abc.ABC):
    """Абстрактный коллектор с циклом, не падающим при ошибках сети/API."""

    def __init__(self, name: str, interval: float, name_suffix: str = "") -> None:
        # ``name`` остаётся ПЛОСКИМ («ohlcv», «futures», ...) — по нему пишется
        # общий heartbeat-ключ, который читают вотчдог, бот и суточная сводка.
        # Менять его на «ohlcv:BTC» было бы тихой поломкой надзора: вотчдог
        # увидел бы пропавший ключ и начал перезапускать живой контейнер.
        # Токен пары добавляется ОТДЕЛЬНЫМ ключом (Этап 8.1).
        self.name = name
        self.token = name_suffix
        self.key = f"{name}:{name_suffix}" if name_suffix else name
        self.interval = interval
        self._log = structlog.get_logger().bind(collector=name, token=name_suffix or None)
        # Счётчики окна сводки (§3 ТЗ 8.1).
        self._window_started = datetime.now(UTC)
        self._requests = 0
        self._errors = 0
        self._rate_limited = 0

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
                self._requests += 1
                await self._heartbeat()
            except asyncio.CancelledError:
                self._log.info("Коллектор остановлен")
                raise
            except Exception as exc:
                # Ошибка сети/API/БД: фиксируем и продолжаем после паузы.
                self._errors += 1
                if RATE_LIMIT_CODE in str(exc):
                    self._rate_limited += 1
                self._log.warning("Ошибка итерации сбора", error=str(exc))
            await self._log_stats_if_due()
            await asyncio.sleep(self.interval)

    async def _log_stats_if_due(self) -> None:
        """Раз в минуту печатает сводку обращений к бирже (уровень INFO).

        Именно по этим строкам считаются «обращения в минуту» и «ответы 50011»
        в scripts/measure_load.sh: успешная итерация логируется на DEBUG,
        который в продакшне выключен, и без сводки замер §3 был бы пустым.
        """
        now = datetime.now(UTC)
        elapsed = (now - self._window_started).total_seconds()
        if elapsed < _STATS_EVERY_SEC:
            return
        self._log.info(
            "Сбор: сводка окна",
            window_sec=round(elapsed, 1),
            requests=self._requests,
            errors=self._errors,
            rate_limited=self._rate_limited,
            per_minute=round(self._requests * 60.0 / elapsed, 2) if elapsed else 0.0,
        )
        self._window_started = now
        self._requests = 0
        self._errors = 0
        self._rate_limited = 0

    async def _heartbeat(self) -> None:
        """Пишет отметку времени последнего успешного сбора — двумя ключами.

        Общий ключ ``collector:heartbeat:{name}`` обновляет ЛЮБОЙ токен: его
        читают вотчдог, бот и суточная сводка, и означает он «коллектор такого
        рода жив». Ключ с токеном добавлен Этапом 8.1: по нему видно, какой
        именно из пяти экземпляров отстал, — без этого пять токенов выглядели
        бы одним.
        """
        now_iso = datetime.now(UTC).isoformat()
        redis = get_redis()
        await redis.set(f"collector:heartbeat:{self.name}", now_iso, ex=_HEARTBEAT_TTL)
        if self.token:
            await redis.set(
                f"collector:heartbeat:{self.key}", now_iso, ex=_HEARTBEAT_TTL
            )
