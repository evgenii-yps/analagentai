"""Futures Agent: анализ funding rate и open interest по swap-инструменту.

Агент читает ТОЛЬКО funding/OI (и, при наличии, цену для контекста) своего
swap-инструмента и не обращается к выводам других агентов.

ЭТАП 7.3, БЛОК A — почему логика переписана (разбор причины, а не подгонка).
За 8 суток наблюдений (11 185 выводов, три версии логики) агент не выдал НИ
ОДНОГО ``bearish``. Причина — в старом коде обе ветки, ведущие к ``bearish``,
были закрыты АБСОЛЮТНЫМИ константами и знаком funding:

  1. ветка разворота: ``is_extreme = abs(rate) > extreme_threshold`` (0.0003),
     при ``rate > 0`` давала bearish. Наблюдаемый |funding| по BTC/USDT на OKX
     на порядок меньше порога (≈0.0001 при базовом уровне ~0.00005), поэтому
     ветка не срабатывала никогда — правка Этапа 7.0 (0.0005 → 0.0003) лишь
     уменьшила разрыв, но не устранила его;
  2. ветка продолжения тренда: ``oi_rising and rate < 0`` требовала
     ОТРИЦАТЕЛЬНОГО funding. У бессрочного фьючерса на BTC funding почти всегда
     положителен (лонги платят шортам), отрицательные значения — редкое
     событие. За период наблюдений их не было.

Оставались только ``oi_rising and rate > 0`` → bullish и ``neutral``, что в
точности совпадает с наблюдаемым распределением выводов. Асимметрия была
структурной: она следовала из кода, а не из рынка.

ИСПРАВЛЕНИЕ. Направление определяется положением текущего значения показателя
ОТНОСИТЕЛЬНО ЕГО СОБСТВЕННОГО РАСПРЕДЕЛЕНИЯ за скользящее окно, а не
относительно константы. Перцентильное правило симметрично ПО ПОСТРОЕНИЮ: при
любых данных верхние ``1 − FUTURES_PCT_HIGH`` доли окна дают bullish, нижние
``FUTURES_PCT_LOW`` — bearish, и обе ветки достижимы всегда. Знак funding и
абсолютная величина в определении направления больше не участвуют.

Способ ОБЪЕДИНЕНИЯ funding и OI не изменён (ТЗ 7.3 §3.2): направление по-прежнему
задаёт funding, а OI работает подтверждением и множителем уверенности ровно той
же формулой ``funding_conf * (0.4 + 0.6 * oi_factor)``, что и раньше
(``_trend_confidence``). Изменён только способ получения направления по каждому
показателю.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.agents.base import (
    SIGNAL_BEARISH,
    SIGNAL_BULLISH,
    SIGNAL_INSUFFICIENT,
    SIGNAL_NEUTRAL,
    AgentOutput,
    BaseAgent,
    normalize_confidence,
)
from src.core.config import settings
from src.core.db import db

# Значения по умолчанию для чистой функции при прямом вызове и в тестах.
# В рантайме приходят из .env (settings.FUTURES_*).
_LOOKBACK_HOURS_DEFAULT = 168   # неделя
_PCT_HIGH_DEFAULT = 0.80
_PCT_LOW_DEFAULT = 0.20
_MIN_POINTS_DEFAULT = 20

# Характеристический масштаб уверенности. У перцентильной логики сырая
# уверенность нормирована ПО ПОСТРОЕНИЮ (расстояние от нейтральной зоны, делённое
# на её ширину, плюс множитель OI ≤ 1), то есть её максимум равен 1.0 — как у
# Market Agent. Прежнее значение 0.10 было эмпирическим максимумом СТАРОЙ сырой
# величины (funding-к-порогу); с ним любая уверенность выше 0.1 схлопывалась бы в
# 1.0, и агент выдавал бы константу. Правило «делим на максимум агента» не
# изменилось — изменилась сама измеряемая величина.
CONFIDENCE_SCALE = 1.0


def percentile_rank(values: Sequence[float], current: float) -> float:
    """Перцентиль ``current`` в выборке ``values`` — доля выборки ниже него.

    Используется «средний ранг»: доля строго меньших плюс половина равных.
    Именно эта форма делает правило ТОЧНО антисимметричным: при зеркальном
    отражении выборки и значения относительно любого центра ``c``
    (``x → 2c − x``) перцентиль переходит в ``1 − перцентиль``. На этом
    свойстве держится симметрия направлений (тест ``test_futures_symmetry``).

    Пустая выборка → 0.5 (неопределённость, нейтральная зона).
    """
    n = len(values)
    if n == 0:
        return 0.5
    below = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return (below + 0.5 * equal) / n


def direction_from_percentile(
    pct: float,
    pct_high: float,
    pct_low: float,
) -> tuple[str, float]:
    """Направление и уверенность по перцентилю → (signal, confidence в [0, 1]).

    Уверенность — нормированное расстояние перцентиля от границы нейтральной
    зоны: 0.0 ровно на границе, 1.0 на краю распределения. Внутри нейтральной
    зоны направления нет, поэтому и расстояния от неё нет: confidence = 0.0.
    """
    if pct >= pct_high:
        span = 1.0 - pct_high
        conf = 1.0 if span <= 0 else min((pct - pct_high) / span, 1.0)
        return SIGNAL_BULLISH, round(conf, 6)
    if pct <= pct_low:
        span = pct_low
        conf = 1.0 if span <= 0 else min((pct_low - pct) / span, 1.0)
        return SIGNAL_BEARISH, round(conf, 6)
    return SIGNAL_NEUTRAL, 0.0


def analyze_futures(
    funding: list[dict[str, Any]],
    open_interest: list[dict[str, Any]],
    price: float | None = None,
    *,
    pct_high: float = _PCT_HIGH_DEFAULT,
    pct_low: float = _PCT_LOW_DEFAULT,
    min_points: int = _MIN_POINTS_DEFAULT,
    lookback_hours: int = _LOOKBACK_HOURS_DEFAULT,
) -> tuple[str, float, dict[str, Any], str]:
    """Чистая функция анализа деривативов → (signal, confidence, metrics, rationale).

    ``funding``/``open_interest`` — окна значений по возрастанию ts с ключами
    ``rate`` и ``value``. Детерминирована: одинаковый ввод → одинаковый вывод.

    Направление задаёт перцентиль ТЕКУЩЕГО funding в окне funding. OI участвует
    подтверждением: если перцентиль OI указывает в ту же сторону, уверенность
    растёт (тем же множителем, что и раньше). Окно funding короче
    ``min_points`` — это ``insufficient_data`` («данных нет»), а не ``neutral``
    («данные есть, сигнала нет»): различать их обязательно. Короткое окно OI
    сигнал не отменяет — оно лишь означает отсутствие подтверждения, и это
    видно в метриках (``oi_enough = false``).
    """
    n_funding = len(funding)
    n_oi = len(open_interest)

    if n_funding < min_points:
        return (
            SIGNAL_INSUFFICIENT,
            0.0,
            {
                "n_funding": n_funding,
                "n_oi": n_oi,
                "min_points": min_points,
                "lookback_hours": lookback_hours,
            },
            (
                f"Недостаточно точек funding для перцентиля: "
                f"{n_funding} < {min_points} за {lookback_hours} ч."
            ),
        )

    funding_values = [float(item["rate"]) for item in funding]
    rate = funding_values[-1]
    funding_pct = percentile_rank(funding_values, rate)
    signal, funding_conf = direction_from_percentile(funding_pct, pct_high, pct_low)

    # OI: то же правило, но только как подтверждение направления.
    oi_enough = n_oi >= min_points
    oi_values = [float(item["value"]) for item in open_interest]
    oi_last = oi_values[-1] if oi_values else None
    if oi_enough and oi_last is not None:
        oi_pct = percentile_rank(oi_values, oi_last)
        oi_signal, oi_conf = direction_from_percentile(oi_pct, pct_high, pct_low)
    else:
        oi_pct, oi_signal, oi_conf = None, SIGNAL_NEUTRAL, 0.0

    # Объединение НЕ изменено (см. модульную docstring): OI подтверждает
    # направление funding и усиливает уверенность, но сам направления не задаёт.
    oi_factor = oi_conf if (oi_signal == signal and signal != SIGNAL_NEUTRAL) else 0.0
    if signal == SIGNAL_NEUTRAL:
        confidence_raw = 0.0
    else:
        confidence_raw = round(min(funding_conf * (0.4 + 0.6 * oi_factor), 1.0), 6)

    confidence = normalize_confidence(confidence_raw, CONFIDENCE_SCALE)

    metrics: dict[str, Any] = {
        "n_funding": n_funding,
        "n_oi": n_oi,
        "min_points": min_points,
        "lookback_hours": lookback_hours,
        "pct_high": pct_high,
        "pct_low": pct_low,
        "funding_rate": round(rate, 10),
        "funding_pct": round(funding_pct, 6),
        "funding_conf": round(funding_conf, 6),
        "oi_enough": oi_enough,
        "oi_last": None if oi_last is None else round(oi_last, 4),
        "oi_pct": None if oi_pct is None else round(oi_pct, 6),
        "oi_signal": oi_signal,
        "oi_conf": round(oi_conf, 6),
        "oi_confirms": oi_factor > 0.0,
        "confidence_raw": confidence_raw,
    }
    if price is not None:
        metrics["price"] = round(float(price), 2)

    if signal == SIGNAL_NEUTRAL:
        direction_ru = "в середине своего распределения → сигнала нет"
    elif signal == SIGNAL_BULLISH:
        direction_ru = "в верхней части своего распределения → за рост"
    else:
        direction_ru = "в нижней части своего распределения → за падение"
    oi_ru = (
        "OI подтверждает"
        if oi_factor > 0.0
        else ("OI не подтверждает" if oi_enough else "OI без окна")
    )
    rationale = (
        f"funding={rate:+.8f} (перцентиль {funding_pct:.2f} за {lookback_hours} ч, "
        f"N={n_funding}) {direction_ru}; {oi_ru}."
    )
    return signal, confidence, metrics, rationale


class FuturesAgent(BaseAgent):
    """Агент анализа деривативов (funding + open interest)."""

    def __init__(self, instrument_id: int, timeframe: str, interval: float) -> None:
        super().__init__(
            name="futures", interval=interval, instrument_id=instrument_id
        )
        self.timeframe = timeframe

    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает окна funding/OI (и цену для контекста) и формирует заключение.

        Окна берутся ЗА ВРЕМЯ (последние ``FUTURES_LOOKBACK_HOURS`` часов), а не
        «последние N строк»: перцентиль должен считаться по фиксированному
        отрезку истории независимо от того, как часто коллектор пишет значения.
        Прореживание до одной точки в час выполняется на стороне БД — иначе при
        поминутной записи неделя дала бы более 10 000 строк на каждой итерации.
        """
        lookback = settings.FUTURES_LOOKBACK_HOURS
        funding = [
            dict(r) for r in await db.get_funding_window(instrument_id, lookback)
        ]
        oi = [
            dict(r)
            for r in await db.get_open_interest_window(instrument_id, lookback)
        ]
        # Обе выборки пусты при живом сервисе → доводим до самовосстановления (7.2).
        await self._note_read(is_empty=(not funding and not oi))

        # Цена — только для контекста в метриках (может отсутствовать у swap).
        price: float | None = None
        candles = await db.get_ohlcv(instrument_id, self.timeframe, 1)
        if candles:
            price = float(dict(candles[-1])["close"])

        signal, confidence, metrics, rationale = analyze_futures(
            funding,
            oi,
            price,
            pct_high=settings.FUTURES_PCT_HIGH,
            pct_low=settings.FUTURES_PCT_LOW,
            min_points=settings.FUTURES_MIN_POINTS,
            lookback_hours=lookback,
        )
        return AgentOutput(
            agent=self.name,
            instrument_id=instrument_id,
            signal=signal,
            confidence=confidence,
            metrics=metrics,
            rationale=rationale,
        )
