"""Decision Agent: агрегирует выводы агентов в одно решение.

ВАЖНО: Decision Agent НЕ анализирует рынок сам. Он читает ТОЛЬКО таблицу
``agent_outputs`` (через ``db.get_latest_agent_output``) и не имеет доступа к
сырым рыночным таблицам (ohlcv/orderbook/funding) — это видно по импортам и коду.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

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
) -> tuple[str, float, list[dict[str, Any]], str]:
    """Чистая функция агрегации → (decision, probability, agents_payload, rationale).

    ``outputs`` — последние выводы агентов (могут быть None / устаревшие /
    ``insufficient_data``). Детерминирована: одинаковый ввод и ``now`` →
    одинаковый результат.
    """
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

    # 6. Вероятность: |балл| усиленный согласованностью направлений.
    directions = [_SIGNAL_VALUE[o["signal"]] for o in fresh]
    pos = sum(1 for d in directions if d > 0)
    neg = sum(1 for d in directions if d < 0)
    agreement = abs(pos - neg) / len(fresh)
    probability = round(min(abs(score) * (0.5 + 0.5 * agreement), 1.0), 4)

    # 7. Объяснение.
    parts = ", ".join(
        f"{o['agent']}={o['signal']}({float(o['confidence']):.2f})" for o in fresh
    )
    rationale = (
        f"{parts}; балл={score:+.2f}, согласованность={agreement:.2f} → {decision}."
    )
    return decision, probability, payload, rationale


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
    ) -> None:
        # instrument_id — основной инструмент, под которым пишется сигнал.
        self.instrument_id = instrument_id
        # У каждого агента может быть свой инструмент (spot/swap).
        self.agent_instruments = agent_instruments
        self.interval = interval
        self.weights = weights
        self.threshold = threshold
        self.min_agents = min_agents
        self.freshness_sec = freshness_sec
        self._log = structlog.get_logger().bind(agent="decision")

    async def decide_once(self) -> None:
        """Читает последние выводы агентов, агрегирует и сохраняет решение."""
        outputs = [
            await db.get_latest_agent_output(agent, self.agent_instruments[agent])
            for agent in AGENTS
        ]
        decision, probability, payload, rationale = make_decision(
            outputs,
            weights=self.weights,
            threshold=self.threshold,
            min_agents=self.min_agents,
            freshness_sec=self.freshness_sec,
            now=datetime.now(UTC),
        )
        await db.save_signal(
            self.instrument_id, decision, probability, payload, rationale
        )
        self._log.info(
            "Решение сохранено",
            decision=decision,
            probability=probability,
            agents=len(payload),
        )

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
        """Пишет в Redis отметку времени последнего успешного решения."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set("decision:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
