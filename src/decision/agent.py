"""Decision Agent: агрегирует выводы агентов в одно решение.

ВАЖНО: Decision Agent НЕ анализирует рынок сам. Он читает ТОЛЬКО таблицу
``agent_outputs`` (через ``db.get_latest_agent_output``) и не имеет доступа к
сырым рыночным таблицам (ohlcv/orderbook/funding) — это видно по импортам и коду.

ЭТАП 7.3. Величина, которую агент кладёт в колонку ``probability``, переименована
по смыслу в ИНДЕКС СОГЛАСИЯ: формула не изменилась, но диагностика 7.1 показала,
что вероятностью успеха она не является (связь с исходом убывающая). Вероятность
теперь берётся из калибровочной кривой, построенной по фактическим исходам, и
пишется отдельной колонкой ``calibrated_probability`` — либо NULL, если кривой
ещё нет. Колонка ``probability`` в БД сохранена: её читают выгрузка, бот и
суточная сводка.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from src.calibration.curve import probability_for_index
from src.core.config import settings
from src.core.db import db
from src.core.redis_client import get_redis

# Агенты, выводы которых агрегируются.
AGENTS = ("market", "liquidity", "futures")

# Перевод сигнала в числовое направление.
_SIGNAL_VALUE = {"bullish": 1, "bearish": -1, "neutral": 0}

# TTL heartbeat-ключа (секунды).
_HEARTBEAT_TTL = 300

# Решения.
DECISION_BUY = "buy"
DECISION_SELL = "sell"
DECISION_WAIT = "wait"

# Версия логики агентов/агрегации (Задача D). Это свойство КОДА: инкрементируется
# при правках, делающих сигналы несравнимыми с прежними. С Этапа 7.3 значение
# читается из настроек (LOGIC_VERSION в .env), чтобы граница режимов была видна в
# конфигурации, — но менять его вручную нельзя: оно должно совпадать с логикой,
# заложенной текущим кодом.
#   v1 — исходная логика.
#   v2 (Этап 7.0) — приведение шкал уверенности, симметрия Futures, защита записи.
#   v3 (Этап 7.2) — знаменатель согласованности = полное число агентов (Задача B1):
#       выпадение агента ПОНИЖАЕТ согласованность (и вероятность). Меняет
#       probability → статистика v2 и v3 несравнима, окно наблюдения обнуляется.
#   v4 (Этап 7.3) — перцентильные границы Futures (ветка bearish стала
#       достижимой), калиброванная вероятность отдельной колонкой, учёт инерции
#       входов. Меняет распределение мнений → статистика v3 и v4 несравнима.
LOGIC_VERSION = settings.LOGIC_VERSION

# Ключ кэша активной калибровочной кривой в Redis и его TTL (секунды).
CALIBRATION_CACHE_PREFIX = "calibration:active:"
CALIBRATION_CACHE_TTL = 3600


def compute_inputs_hash(agents_payload: list[dict[str, Any]]) -> str:
    """sha256 канонической строки входных мнений (Этап 7.3, Блок C).

    Каноническая строка: агенты отсортированы по имени, для каждого берётся пара
    ``signal`` + ``confidence``, округлённая до 4 знаков, разделитель ``|``:

        futures:neutral:0.4000|liquidity:bullish:1.0000|market:bearish:0.3185

    Время (``ts``) в строку НЕ входит: одинаковые мнения, прочитанные минутой
    позже, — это тот же самый вход, и решение по ним новой информации не несёт.
    Порядок агентов в payload на хэш не влияет (сортировка), изменение
    уверенности в пятом знаке — тоже (округление до четвёртого).
    """
    parts = [
        f"{entry.get('agent')}:{entry.get('signal')}:"
        f"{float(entry.get('confidence', 0.0)):.4f}"
        for entry in sorted(agents_payload, key=lambda e: str(e.get("agent")))
    ]
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_fresh(output: dict[str, Any], freshness_sec: float, now: datetime) -> bool:
    """Свежий ли вывод (по возрасту ts)."""
    ts = output["ts"]
    return (now - ts).total_seconds() <= freshness_sec


def make_decision(
    outputs: list[dict[str, Any] | None],
    *,
    weights: dict[str, float],
    threshold: float,
    min_agents: int,
    freshness_sec: float,
    now: datetime,
    total_agents: int | None = None,
) -> tuple[str, float, list[dict[str, Any]], str]:
    """Чистая функция агрегации → (decision, conviction, agents_payload, rationale).

    Второй элемент — ИНДЕКС СОГЛАСИЯ (Этап 7.3): ``|балл| × (0.5 + 0.5 ×
    согласованность)``. Формула НЕ изменена; изменилось только название, потому
    что вероятностью успеха эта величина не является (диагностика 7.1 показала
    убывающую связь с исходом). Хранится по-прежнему в колонке ``probability``.

    ``outputs`` — последние выводы агентов (могут быть None / устаревшие /
    ``insufficient_data``). Детерминирована: одинаковый ввод и ``now`` →
    одинаковый результат.

    ``total_agents`` (Задача B1, Этап 7.2) — полное число настроенных агентов
    (по умолчанию ``len(AGENTS)`` = 3). Согласованность считается относительно
    ПОЛНОГО состава, а не только свежих: выпадение агента механически понижает
    согласованность, а не повышает её (как было при знаменателе ``len(fresh)``).
    """
    if total_agents is None:
        total_agents = len(AGENTS)
    # 1–2. Отбрасываем отсутствующие, устаревшие и insufficient_data.
    fresh: list[dict[str, Any]] = []
    for output in outputs:
        if output is None:
            continue
        if output["signal"] not in _SIGNAL_VALUE:
            continue  # insufficient_data или неизвестный сигнал
        if not _is_fresh(output, freshness_sec, now):
            continue
        fresh.append(output)

    payload = [
        {
            "agent": o["agent"],
            "signal": o["signal"],
            "confidence": round(float(o["confidence"]), 4),
            "ts": o["ts"].isoformat(),
        }
        for o in fresh
    ]

    # Нет данных — нет решения.
    if len(fresh) < min_agents:
        rationale = (
            f"Свежих выводов {len(fresh)} < MIN_AGENTS={min_agents} → wait."
        )
        return DECISION_WAIT, 0.0, payload, rationale

    # 3–4. Взвешенный балл в диапазоне [-1, 1].
    numerator = 0.0
    denominator = 0.0
    for o in fresh:
        weight = weights.get(o["agent"], 1.0)
        confidence = float(o["confidence"])
        direction = _SIGNAL_VALUE[o["signal"]]
        numerator += direction * confidence * weight
        denominator += weight * confidence
    score = numerator / denominator if denominator > 0 else 0.0

    # 5. Решение по порогу.
    if score > threshold:
        decision = DECISION_BUY
    elif score < -threshold:
        decision = DECISION_SELL
    else:
        decision = DECISION_WAIT

    # 6. Индекс согласия: |балл|, усиленный согласованностью направлений.
    # Формула Этапа 7.3 НЕ ИЗМЕНЕНА — изменилось только название величины.
    # Знаменатель согласованности — ПОЛНОЕ число агентов (Задача B1), а не число
    # свежих: иначе выпадение агента механически завышало бы согласованность
    # (напр. #8205: |1-0|/2=0.50 вместо |1-0|/3=0.33 → индекс 0.72 вместо ≈0.64).
    directions = [_SIGNAL_VALUE[o["signal"]] for o in fresh]
    pos = sum(1 for d in directions if d > 0)
    neg = sum(1 for d in directions if d < 0)
    agreement = abs(pos - neg) / total_agents if total_agents > 0 else 0.0
    conviction = round(min(abs(score) * (0.5 + 0.5 * agreement), 1.0), 4)

    # 7. Объяснение.
    parts = ", ".join(
        f"{o['agent']}={o['signal']}({float(o['confidence']):.2f})" for o in fresh
    )
    rationale = (
        f"{parts}; балл={score:+.2f}, согласованность={agreement:.2f} → {decision}."
    )
    return decision, conviction, payload, rationale


class DecisionAgent:
    """Агрегирующий агент: читает выводы агентов и пишет решение в ``signals``."""

    def __init__(
        self,
        instrument_id: int,
        agent_instruments: dict[str, int],
        interval: float,
        weights: dict[str, float],
        threshold: float,
        min_agents: int,
        freshness_sec: float,
        token: str = "",
    ) -> None:
        # instrument_id — основной инструмент, под которым пишется сигнал.
        self.instrument_id = instrument_id
        # Токен пары (Этап 8.1): различает пять одновременно работающих
        # экземпляров в логах и в heartbeat-ключе.
        self.token = token
        # У каждого агента может быть свой инструмент (spot/swap).
        self.agent_instruments = agent_instruments
        self.interval = interval
        self.weights = weights
        self.threshold = threshold
        self.min_agents = min_agents
        self.freshness_sec = freshness_sec
        self._log = structlog.get_logger().bind(
            agent="decision", token=token or None
        )

    async def decide_once(self) -> None:
        """Читает последние выводы агентов, агрегирует и сохраняет решение."""
        outputs = [
            await db.get_latest_agent_output(agent, self.agent_instruments[agent])
            for agent in AGENTS
        ]
        decision, conviction, payload, rationale = make_decision(
            outputs,
            weights=self.weights,
            threshold=self.threshold,
            min_agents=self.min_agents,
            freshness_sec=self.freshness_sec,
            now=datetime.now(UTC),
            total_agents=len(AGENTS),
        )
        # Деградация (Задача A2): в решении участвовало меньше полного состава.
        # payload содержит РОВНО свежие содержательные выводы (insufficient_data и
        # устаревшие уже отфильтрованы), поэтому его длина = число «живых» агентов.
        degraded = len(payload) < len(AGENTS)

        # Инерция входов (Блок C): решение на том же наборе мнений — повтор.
        inputs_hash = compute_inputs_hash(payload)
        previous_hash = await db.get_last_inputs_hash(self.instrument_id)
        is_repeat = previous_hash is not None and previous_hash == inputs_hash

        # Калиброванная вероятность (Блок B): только если кривая уже построена.
        # Нет кривой — NULL, и никакая «вероятность» никому не показывается.
        calibrated, calibration_id = await self._calibrate(conviction)

        await db.save_signal(
            self.instrument_id,
            decision,
            conviction,
            payload,
            rationale,
            logic_version=LOGIC_VERSION,
            degraded=degraded,
            calibrated_probability=calibrated,
            calibration_id=calibration_id,
            inputs_hash=inputs_hash,
            is_repeat=is_repeat,
        )
        self._log.info(
            "Решение сохранено",
            decision=decision,
            conviction=conviction,
            calibrated_probability=calibrated,
            agents=len(payload),
            degraded=degraded,
            is_repeat=is_repeat,
        )

    async def _calibrate(self, conviction: float) -> tuple[float | None, int | None]:
        """Вероятность по активной кривой → (значение, id кривой) или (None, None).

        Отсутствие кривой — штатное состояние первых суток после смены версии
        логики, поэтому здесь нет ни ошибок, ни исключений. Сбой Redis или БД
        тоже не должен ронять решение: в худшем случае вероятность просто не
        будет записана, а сам сигнал сохранится.
        """
        try:
            curve = await self._active_curve()
        except Exception as exc:  # noqa: BLE001 — решение важнее калибровки
            self._log.warning("Кривая калибровки недоступна", error=str(exc))
            return None, None
        if not curve:
            return None, None
        value = probability_for_index(curve.get("bins") or [], conviction)
        if value is None:
            return None, None
        return value, curve.get("id")

    async def _active_curve(self) -> dict[str, Any] | None:
        """Активная кривая с кэшированием в Redis (ключ на версию логики, TTL 1 ч)."""
        key = f"{CALIBRATION_CACHE_PREFIX}{LOGIC_VERSION}"
        try:
            cached = await get_redis().get(key)
        except Exception:  # noqa: BLE001 — Redis необязателен, читаем из БД
            cached = None
        if cached:
            try:
                return json.loads(cached)
            except (TypeError, ValueError):
                pass  # мусор в кэше — перечитаем из БД и перезапишем

        curve = await db.get_active_calibration(LOGIC_VERSION)
        if curve is None:
            return None
        bins = curve["bins"]
        if isinstance(bins, str):  # asyncpg отдаёт JSONB строкой
            bins = json.loads(bins)
        compact = {"id": int(curve["id"]), "bins": bins}
        try:
            await get_redis().set(
                key, json.dumps(compact), ex=CALIBRATION_CACHE_TTL
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось закэшировать кривую", error=str(exc))
        return compact

    async def run(self) -> None:
        """Бесконечный цикл: decide_once → heartbeat → пауза. Не падает на ошибках."""
        self._log.info("Decision Agent запущен", interval=self.interval)
        while True:
            try:
                await self.decide_once()
                await self._heartbeat()
            except asyncio.CancelledError:
                self._log.info("Decision Agent остановлен")
                raise
            except Exception as exc:
                self._log.warning("Ошибка итерации решения", error=str(exc))
            await asyncio.sleep(self.interval)

    async def _heartbeat(self) -> None:
        """Отметка времени последнего успешного решения — двумя ключами.

        Общий ключ ``decision:heartbeat`` читают вотчдог, бот и суточная сводка;
        его обновляет любой токен. Ключ с токеном добавлен Этапом 8.1, чтобы
        было видно, какой именно экземпляр отстал.
        """
        now_iso = datetime.now(UTC).isoformat()
        redis = get_redis()
        await redis.set("decision:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
        if self.token:
            await redis.set(
                f"decision:heartbeat:{self.token}", now_iso, ex=_HEARTBEAT_TTL
            )
