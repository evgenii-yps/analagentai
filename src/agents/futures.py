"""Futures Agent: анализ funding rate и open interest по swap-инструменту.

Агент читает ТОЛЬКО funding/OI (и, при наличии, цену для контекста) своего
swap-инструмента и не обращается к выводам других агентов.
"""

from __future__ import annotations

from typing import Any

from src.agents.base import (
    SIGNAL_BEARISH,
    SIGNAL_BULLISH,
    SIGNAL_NEUTRAL,
    AgentOutput,
    BaseAgent,
)
from src.core.db import db

# Сколько последних значений читать и минимумы для решения.
_FUNDING_LIMIT = 10
_OI_LIMIT = 30
_MIN_FUNDING = 1
_MIN_OI = 2

# Пороги логики.
_FUNDING_EXTREME = 0.0005   # |funding| выше — экстремум (риск разворота)
_OI_RISE_PCT = 0.1          # рост OI считается значимым с этого % изменения


def analyze_futures(
    funding: list[dict[str, Any]],
    open_interest: list[dict[str, Any]],
    price: float | None = None,
    min_funding: int = _MIN_FUNDING,
    min_oi: int = _MIN_OI,
) -> tuple[str, float, dict[str, Any], str]:
    """Чистая функция анализа деривативов → (signal, confidence, metrics, rationale).

    ``funding``/``open_interest`` — списки по возрастанию ts с ключами
    ``rate`` и ``value`` соответственно. Детерминирована.
    """
    if len(funding) < min_funding or len(open_interest) < min_oi:
        return (
            "insufficient_data",
            0.0,
            {"n_funding": len(funding), "n_oi": len(open_interest)},
            "Недостаточно данных funding/OI для анализа.",
        )

    rate = float(funding[-1]["rate"])
    oi_first = float(open_interest[0]["value"])
    oi_last = float(open_interest[-1]["value"])
    oi_change_pct = (oi_last - oi_first) / oi_first * 100.0 if oi_first > 0 else 0.0
    oi_rising = oi_change_pct > _OI_RISE_PCT
    is_extreme = abs(rate) > _FUNDING_EXTREME

    if is_extreme:
        # Перегрев плеча: экстремальный funding → ставка на разворот.
        signal = SIGNAL_BEARISH if rate > 0 else SIGNAL_BULLISH
        extreme_factor = min((abs(rate) - _FUNDING_EXTREME) / _FUNDING_EXTREME, 1.0)
        confidence = round(min(0.5 + 0.5 * extreme_factor, 1.0), 4)
        rationale_dir = "экстремальный funding → риск разворота"
    elif oi_rising and rate > 0:
        # Рост OI + умеренно положительный funding → продолжение роста.
        signal = SIGNAL_BULLISH
        confidence = _trend_confidence(rate, oi_change_pct)
        rationale_dir = "рост OI + положительный funding → продолжение"
    elif oi_rising and rate < 0:
        signal = SIGNAL_BEARISH
        confidence = _trend_confidence(rate, oi_change_pct)
        rationale_dir = "рост OI + отрицательный funding → продолжение снижения"
    else:
        # OI не растёт или funding нулевой — нет подтверждения.
        signal = SIGNAL_NEUTRAL
        confidence = round(min(abs(rate) / _FUNDING_EXTREME * 0.2, 1.0), 4)
        rationale_dir = "нет подтверждения (OI не растёт)"

    metrics: dict[str, Any] = {
        "n_funding": len(funding),
        "n_oi": len(open_interest),
        "funding_rate": round(rate, 8),
        "funding_extreme": is_extreme,
        "funding_threshold": _FUNDING_EXTREME,
        "oi_first": round(oi_first, 4),
        "oi_last": round(oi_last, 4),
        "oi_change_pct": round(oi_change_pct, 4),
        "oi_rising": oi_rising,
    }
    if price is not None:
        metrics["price"] = round(float(price), 2)

    rationale = (
        f"{rationale_dir}: funding={rate:+.6f}, ΔOI={oi_change_pct:+.2f}%."
    )
    return signal, confidence, metrics, rationale


def _trend_confidence(rate: float, oi_change_pct: float) -> float:
    """Уверенность для сценария продолжения тренда."""
    funding_strength = min(abs(rate) / _FUNDING_EXTREME, 1.0)
    oi_factor = min(abs(oi_change_pct) / 2.0, 1.0)
    return round(min(funding_strength * (0.4 + 0.6 * oi_factor), 1.0), 4)


class FuturesAgent(BaseAgent):
    """Агент анализа деривативов (funding + open interest)."""

    def __init__(self, instrument_id: int, timeframe: str, interval: float) -> None:
        super().__init__(
            name="futures", interval=interval, instrument_id=instrument_id
        )
        self.timeframe = timeframe

    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает funding/OI (и цену для контекста) и формирует заключение."""
        funding = [dict(r) for r in await db.get_recent_funding(instrument_id, _FUNDING_LIMIT)]
        oi = [
            dict(r)
            for r in await db.get_recent_open_interest(instrument_id, _OI_LIMIT)
        ]

        # Цена — только для контекста в метриках (может отсутствовать у swap).
        price: float | None = None
        candles = await db.get_ohlcv(instrument_id, self.timeframe, 1)
        if candles:
            price = float(dict(candles[-1])["close"])

        signal, confidence, metrics, rationale = analyze_futures(funding, oi, price)
        return AgentOutput(
            agent=self.name,
            instrument_id=instrument_id,
            signal=signal,
            confidence=confidence,
            metrics=metrics,
            rationale=rationale,
        )
