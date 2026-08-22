"""Базовый агент и единая схема вывода ``AgentOutput``."""

from __future__ import annotations

import abc
import asyncio
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis
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
# Служебное событие самовосстановления (Этап 7.2, Задача A1): не ошибка итерации,
# а факт того, что агент сам сбросил состояние после серии сбоев.
FAILURE_AUTO_RESET = "auto_reset"


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

    def __init__(
        self, name: str, interval: float, instrument_id: int, name_suffix: str = ""
    ) -> None:
        # ``name`` — плоское имя агента («market», «liquidity», «futures»). Оно
        # попадает в колонку agent_outputs.agent, по нему Decision Agent ищет
        # выводы и по нему же настроены веса. Токен сюда НЕ дописывается: пять
        # токенов различаются instrument_id, а не именем агента.
        self.name = name
        self.token = name_suffix
        # Ключ для Redis (heartbeat, серия сбоев): здесь токен нужен, иначе
        # сбои одного токена засчитывались бы другому.
        self.key = f"{name}:{name_suffix}" if name_suffix else name
        self.interval = interval
        self.instrument_id = instrument_id
        self._log = structlog.get_logger().bind(agent=name, token=name_suffix or None)
        # Серия сбоев подряд В ПАМЯТИ процесса (Этап 7.2). Отдельно от счётчика в
        # Redis (тот — для алерта): самовосстановление НЕ должно зависеть от Redis,
        # ведь испорченным может быть как раз внешнее состояние.
        self._consecutive_failures = 0
        # Серия ПУСТЫХ выборок подряд при живом сервисе. Это ровно симптом
        # инцидента 14.08 (полная БД, но пустой ответ) — а он НЕ вызывает
        # исключения (штатный insufficient_data), поэтому счётчик сбоев его не
        # ловит. Отдельный счётчик доводит и этот симптом до самовосстановления.
        self._empty_read_streak = 0

    @abc.abstractmethod
    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает свои данные, считает показатели и возвращает заключение."""
        raise NotImplementedError

    async def reset_state(self) -> None:
        """Сбрасывает внутреннее состояние агента (Этап 7.2, Задача A1).

        Хук самовосстановления: вызывается при серии сбоев подряд. Базовые агенты
        состояния между итерациями НЕ хранят (выборка строится от ``now()`` каждый
        раз заново), поэтому по умолчанию сбрасывать нечего — метод существует как
        явная точка расширения и как контракт «состояние можно обнулить».
        """
        return None

    async def _note_read(self, is_empty: bool) -> None:
        """Учитывает пустую/непустую выборку для самовосстановления (Этап 7.2).

        Пустой ответ при живом сервисе — это симптом инцидента 14.08 (полная БД,
        но выборка пуста). Он НЕ бросает исключения (агент штатно отдаёт
        insufficient_data), поэтому счётчик сбоев его не увидит и авто-сброс не
        сработает — агент молчал бы до внешнего вмешательства. Здесь серия пустых
        ответов подряд доводится до того же ``_auto_reset`` (проверка живости и
        переоткрытие соединения), что и серия сбоев.
        """
        if not is_empty:
            self._empty_read_streak = 0
            return
        self._empty_read_streak += 1
        threshold = settings.AGENT_AUTO_RESET_STREAK
        if threshold > 0 and self._empty_read_streak >= threshold:
            self._log.warning(
                "Пустая выборка подряд при живом сервисе — самовосстановление",
                streak=self._empty_read_streak,
            )
            await self._auto_reset(self._empty_read_streak)

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
            await self._after_failure()
            return

        # 2. Запись (ошибка здесь = временная недоступность БД).
        try:
            await db.save_agent_output(output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._record_failure(FAILURE_DB_WRITE, exc)
            await self._after_failure()
            return

        # 3. Успех: сбрасываем серию сбоев, обновляем heartbeat, логируем.
        await self._on_success()
        await self._heartbeat()
        self._log.info(
            "Заключение сохранено",
            signal=output.signal,
            confidence=output.confidence,
        )

    async def _after_failure(self) -> None:
        """Обслуживает серию сбоев подряд: при достижении порога — самосброс.

        Инцидент 14.08: система 8 часов ждала внешнего вмешательства (перезапуск
        контейнера вотчдогом). Здесь агент восстанавливается сам — при
        ``AGENT_AUTO_RESET_STREAK`` сбоях подряд сбрасывает внутреннее состояние и
        переоткрывает долгоживущие соединения (пул БД + клиент Redis), в которых и
        может «залипнуть» испорченное состояние процесса.
        """
        self._consecutive_failures += 1
        threshold = settings.AGENT_AUTO_RESET_STREAK
        if threshold > 0 and self._consecutive_failures >= threshold:
            await self._auto_reset(self._consecutive_failures)

    async def _auto_reset(self, streak: int) -> None:
        """Сбрасывает состояние агента и переоткрывает соединения (Этап 7.2).

        Записывает событие в ``agent_failures`` (``error_type='auto_reset'``),
        чтобы самовосстановление было видно в суточной сводке. Сам НЕ бросает:
        любая ошибка сброса логируется, счётчик обнуляется в любом случае — иначе
        авто-сброс срабатывал бы каждую итерацию.
        """
        self._log.warning("Самовосстановление агента: сброс состояния", streak=streak)
        # 1. Сброс внутреннего состояния агента (хук; у базовых агентов пусто).
        try:
            await self.reset_state()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Ошибка сброса состояния агента", error=str(exc))
        # 2. Переоткрытие пула БД — «мягкий перезапуск» доступа к данным.
        try:
            await db.reconnect()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось переоткрыть пул БД", error=str(exc))
        # 3. Переоткрытие клиента Redis (следующий вызов get_redis() создаст новый).
        try:
            await close_redis()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось закрыть клиент Redis", error=str(exc))
        # 4. Фиксируем факт авто-сброса в БД (после reconnect пул уже новый).
        try:
            await db.record_agent_failure(
                self.name,
                FAILURE_AUTO_RESET,
                None,
                f"[{self.key}] Автосброс после {streak} сбоев подряд "
                f"(переоткрыты пул БД и Redis).",
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось записать авто-сброс в БД", error=str(exc))
        # 5. Обнуляем счётчики: серия «погашена» попыткой восстановления.
        self._consecutive_failures = 0
        self._empty_read_streak = 0

    async def _record_failure(self, error_type: str, exc: Exception) -> None:
        """Делает сбой видимым: лог с типом, строка в БД, серия сбоев в Redis, алерт.

        Heartbeat при этом НЕ обновляется (мы сюда попали до шага 3), поэтому
        затяжной сбой рано или поздно проявится и устареванием heartbeat.
        """
        exc_type = type(exc).__name__
        message = str(exc)
        self._log.warning(
            "Сбой итерации агента",
            error_type=error_type,
            exc_type=exc_type,
            error=message[:300],
        )

        # В agent_failures.detail пишем ПОЛНУЮ ТРАССИРОВКУ (а не дубль сообщения):
        # без неё «No numeric types to aggregate» не показывает, где именно упало.
        # Берём traceback из самого исключения (exc.__traceback__), а не из
        # sys.exc_info(), чтобы вложенные try/except ниже его не «затёрли».
        detail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ) or message

        # Строка в БД — для подсчёта за период (суточная сводка). Для db_write
        # сам INSERT может не пройти (БД недоступна) — не падаем из-за этого.
        try:
            # Токен пишется в detail, а не в колонку agent: схема таблицы не
            # меняется, а различить пять экземпляров одного агента нужно.
            await db.record_agent_failure(
                self.name, error_type, exc_type,
                detail if not self.token else f"[{self.key}]\n{detail}",
            )
        except Exception as rec_exc:  # noqa: BLE001
            self._log.warning("Не удалось записать сбой в БД", error=str(rec_exc))

        # Серия сбоев подряд — в Redis, для алерта. Отсутствие Redis не роняет.
        try:
            await self._bump_failure_streak()
        except Exception as st_exc:  # noqa: BLE001
            self._log.warning("Не удалось обновить счётчик сбоев", error=str(st_exc))

    async def _bump_failure_streak(self) -> None:
        """Инкремент серии сбоев; при кратности порогу — алерт в Telegram."""
        key = f"agent:failures:streak:{self.key}"
        streak = await get_redis().incr(key)
        # TTL, чтобы «висящий» счётчик сам протух, если агент замолчал совсем.
        await get_redis().expire(key, _HEARTBEAT_TTL)
        threshold = settings.AGENT_FAILURE_ALERT_STREAK
        if threshold > 0 and streak >= threshold and streak % threshold == 0:
            await self._alert_failures(int(streak))

    async def _alert_failures(self, streak: int) -> None:
        """Шлёт алерт о серии сбоев. Не бросает (send_message сам гасит ошибки)."""
        text = (
            f"⚠️ <b>Агент {self.key}</b>: {streak} сбоев подряд — "
            f"выводы не записываются. Проверьте логи сервиса agents."
        )
        await send_message(text)

    async def _on_success(self) -> None:
        """Сбрасывает серию сбоев после успешной итерации."""
        # В памяти — для самовосстановления (Этап 7.2).
        self._consecutive_failures = 0
        # В Redis — для алерта о серии сбоев.
        try:
            await get_redis().delete(f"agent:failures:streak:{self.key}")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось сбросить счётчик сбоев", error=str(exc))

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последнего успешного анализа."""
        now_iso = datetime.now(UTC).isoformat()
        # Общий ключ читают вотчдог/бот/сводка — его обновляет любой токен;
        # ключ с токеном (ниже) показывает, какой именно экземпляр отстал.
        redis = get_redis()
        if self.token:
            await redis.set(
                f"agent:heartbeat:{self.key}", now_iso, ex=_HEARTBEAT_TTL
            )
        await redis.set(
            f"agent:heartbeat:{self.name}",
            now_iso,
            ex=_HEARTBEAT_TTL,
        )
