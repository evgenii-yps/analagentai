"""Liquidity Agent: анализ стакана заявок (orderbook_snapshots).

Агент читает ТОЛЬКО снимки стакана своего инструмента и не обращается
к выводам других агентов.
"""

from __future__ import annotations

import json
from statistics import fmean, pstdev
from typing import Any

from src.agents.base import (
    SIGNAL_BEARISH,
    SIGNAL_BULLISH,
    SIGNAL_NEUTRAL,
    AgentOutput,
    BaseAgent,
    normalize_confidence,
)
from src.core.db import db

# Характеристический масштаб уверенности (Задача A). Сырой дисбаланс стакана BTC
# у топа книги интринзически мал: теоретический максимум strength = 1.0
# недостижим, эмпирический максимум ≈ 0.15 (ANALYSIS_REPORT.md §3.1). Нормируем
# на него, чтобы полный диапазон [0,1] реально использовался и шкала стала
# сопоставима с market. Значение — калибровка по наблюдениям, уточняется
# запросом C1 из ANALYSIS_REPORT.md; см. STAGE_7_0_REPORT.md.
CONFIDENCE_SCALE = 0.15

# Сколько последних снимков анализировать и минимум для решения.
_SNAPSHOTS_LIMIT = 20
_MIN_SNAPSHOTS = 5

# Пороги логики.
_IMBALANCE_THRESHOLD = 0.15   # перевес стороны для направленного сигнала
_WIDE_SPREAD_REL = 0.005      # относительный спред (0.5%) — рынок «неликвиден»


def _imbalance(bid_vol: float, ask_vol: float) -> float:
    """Дисбаланс объёмов в диапазоне [-1, 1]: >0 перевес бидов, <0 асков."""
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def _wall_ratio(levels: list[list[float]]) -> float:
    """Отношение крупнейшей заявки к среднему уровню (наличие «стенок»)."""
    amounts = [float(level[1]) for level in levels if len(level) >= 2]
    if not amounts:
        return 0.0
    avg = fmean(amounts)
    if avg <= 0:
        return 0.0
    return max(amounts) / avg


def analyze_orderbook(
    snapshots: list[dict[str, Any]],
    min_snapshots: int = _MIN_SNAPSHOTS,
) -> tuple[str, float, dict[str, Any], str]:
    """Чистая функция анализа стакана → (signal, confidence, metrics, rationale).

    ``snapshots`` — список снимков по возрастанию ts; у каждого ключи
    ``bids``/``asks`` (списки пар [price, amount]), ``spread``,
    ``bid_volume``, ``ask_volume``. Детерминирована.
    """
    n = len(snapshots)
    if n < min_snapshots:
        return (
            "insufficient_data",
            0.0,
            {"n_snapshots": n, "min_snapshots": min_snapshots},
            f"Недостаточно снимков стакана: {n} < {min_snapshots}.",
        )

    imbalances = [
        _imbalance(float(s["bid_volume"]), float(s["ask_volume"])) for s in snapshots
    ]
    current = imbalances[-1]
    avg_imbalance = fmean(imbalances)
    imbalance_std = pstdev(imbalances) if n > 1 else 0.0

    latest = snapshots[-1]
    spread = float(latest.get("spread") or 0.0)
    bids = latest.get("bids") or []
    asks = latest.get("asks") or []
    best_bid = float(bids[0][0]) if bids else 0.0
    rel_spread = spread / best_bid if best_bid > 0 else 0.0
    bid_wall = _wall_ratio(bids)
    ask_wall = _wall_ratio(asks)

    # Логика направления.
    if rel_spread > _WIDE_SPREAD_REL:
        signal = SIGNAL_NEUTRAL
        rationale_dir = "широкий спред (неликвидно)"
    elif current > _IMBALANCE_THRESHOLD and avg_imbalance > 0:
        signal = SIGNAL_BULLISH
        rationale_dir = "перевес бидов"
    elif current < -_IMBALANCE_THRESHOLD and avg_imbalance < 0:
        signal = SIGNAL_BEARISH
        rationale_dir = "перевес асков"
    else:
        signal = SIGNAL_NEUTRAL
        rationale_dir = "баланс сторон"

    # Уверенность: сила дисбаланса × его устойчивость (низкий разброс).
    # Направление сигнала НЕ меняется — нормируется только величина уверенности.
    strength = (abs(current) + abs(avg_imbalance)) / 2.0
    consistency = max(0.0, 1.0 - imbalance_std)
    if signal == SIGNAL_NEUTRAL:
        confidence_raw = round(min(strength * 0.3 * consistency, 1.0), 4)
    else:
        confidence_raw = round(min(strength * (0.5 + 0.5 * consistency), 1.0), 4)
    confidence = normalize_confidence(confidence_raw, CONFIDENCE_SCALE)

    metrics: dict[str, Any] = {
        "n_snapshots": n,
        "spread": round(spread, 4),
        "rel_spread": round(rel_spread, 6),
        "bid_volume": round(float(latest["bid_volume"]), 4),
        "ask_volume": round(float(latest["ask_volume"]), 4),
        "imbalance": round(current, 4),
        "avg_imbalance": round(avg_imbalance, 4),
        "imbalance_std": round(imbalance_std, 4),
        "bid_wall_ratio": round(bid_wall, 2),
        "ask_wall_ratio": round(ask_wall, 2),
        "confidence_raw": confidence_raw,
    }
    rationale = (
        f"{rationale_dir}: дисбаланс={current:+.2f} (средн. {avg_imbalance:+.2f}), "
        f"отн. спред={rel_spread:.4f}."
    )
    return signal, confidence, metrics, rationale


class LiquidityAgent(BaseAgent):
    """Агент анализа стакана заявок."""

    def __init__(self, instrument_id: int, interval: float) -> None:
        super().__init__(
            name="liquidity", interval=interval, instrument_id=instrument_id
        )

    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает снимки стакана и формирует заключение по ликвидности."""
        rows = await db.get_recent_orderbook(instrument_id, _SNAPSHOTS_LIMIT)
        snapshots: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # bids/asks приходят из JSONB как строки — разбираем в списки.
            d["bids"] = json.loads(d["bids"]) if isinstance(d["bids"], str) else d["bids"]
            d["asks"] = json.loads(d["asks"]) if isinstance(d["asks"], str) else d["asks"]
            snapshots.append(d)

        signal, confidence, metrics, rationale = analyze_orderbook(snapshots)
        return AgentOutput(
            agent=self.name,
            instrument_id=instrument_id,
            signal=signal,
            confidence=confidence,
            metrics=metrics,
            rationale=rationale,
        )
