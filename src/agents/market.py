"""Market Agent: технический анализ по свечам OHLCV.

Все индикаторы считаются вручную на pandas/numpy с фиксированными периодами
(EMA 20/50/200, RSI 14, ATR 14, MACD 12/26/9, ADX 14). Расчёт детерминирован:
на одних и тех же данных результат всегда одинаковый.

Агент читает ТОЛЬКО свечи своего инструмента и не обращается к выводам
других агентов.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.agents.base import (
    SIGNAL_BEARISH,
    SIGNAL_BULLISH,
    SIGNAL_NEUTRAL,
    AgentOutput,
    BaseAgent,
)
from src.core.db import db

# Периоды индикаторов (фиксированы по ТЗ).
_EMA_FAST, _EMA_MID, _EMA_SLOW = 20, 50, 200
_RSI_PERIOD = 14
_ATR_PERIOD = 14
_MACD_FAST, _MACD_SLOW, _MACD_SIGNAL = 12, 26, 9
_ADX_PERIOD = 14

# Окно для уровней поддержки/сопротивления и наклона EMA.
_SR_LOOKBACK = 50
_SLOPE_LOOKBACK = 10

# Пороги логики.
_ADX_TREND_MIN = 20.0   # ниже — тренд считаем слабым
_RSI_BULL = 55.0
_RSI_BEAR = 45.0


def ema(series: pd.Series, period: int) -> pd.Series:
    """Экспоненциальная скользящая средняя."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = _RSI_PERIOD) -> pd.Series:
    """RSI по методу Уайлдера (значения в диапазоне 0–100)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    result = 100.0 - 100.0 / (1.0 + rs)
    # Нет убытков → RSI = 100; нет движения вовсе → 50 (нейтрально).
    result = result.where(avg_loss != 0, 100.0)
    result = result.where((avg_gain != 0) | (avg_loss != 0), 50.0)
    return result


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Истинный диапазон (True Range)."""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = _ATR_PERIOD,
) -> pd.Series:
    """Average True Range по методу Уайлдера."""
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def macd(
    close: pd.Series,
    fast: int = _MACD_FAST,
    slow: int = _MACD_SLOW,
    signal: int = _MACD_SIGNAL,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD: возвращает (линия MACD, сигнальная линия, гистограмма)."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = _ADX_PERIOD,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX по методу Уайлдера. Возвращает (ADX, +DI, -DI), все ≥ 0."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0.0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0.0)

    tr = _true_range(high, low, close)
    atr_w = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w

    di_sum = (plus_di + minus_di).replace(0.0, pd.NA)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)
    return adx_series, plus_di.fillna(0.0), minus_di.fillna(0.0)


def _round(value: float, digits: int = 6) -> float:
    """Округление с защитой от NaN/inf (для JSON-метрик)."""
    if value is None or pd.isna(value):
        return 0.0
    return round(float(value), digits)


def analyze_ohlcv(
    df: pd.DataFrame,
    min_candles: int,
) -> tuple[str, float, dict[str, Any], str]:
    """Чистая функция анализа свечей → (signal, confidence, metrics, rationale).

    ``df`` — свечи по возрастанию ts со столбцами open/high/low/close/volume.
    Детерминирована: одинаковый ввод → одинаковый вывод.
    """
    # Приводим столбцы к числам и отбрасываем «грязные» строки (NULL/NaN),
    # чтобы расчёт был корректным, а не падал на object-типах.
    if not df.empty:
        df = df.copy()
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])

    n = len(df)
    if n < min_candles:
        return (
            "insufficient_data",
            0.0,
            {"n_candles": n, "min_candles": min_candles},
            f"Недостаточно свечей: {n} < {min_candles}.",
        )

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    ema_fast = ema(close, _EMA_FAST)
    ema_mid = ema(close, _EMA_MID)
    ema_slow = ema(close, _EMA_SLOW)
    rsi_series = rsi(close)
    atr_series = atr(high, low, close)
    macd_line, macd_signal, macd_hist = macd(close)
    adx_series, plus_di, minus_di = adx(high, low, close)

    support = float(low.rolling(_SR_LOOKBACK).min().iloc[-1])
    resistance = float(high.rolling(_SR_LOOKBACK).max().iloc[-1])

    # Последние значения индикаторов.
    e_fast = float(ema_fast.iloc[-1])
    e_mid = float(ema_mid.iloc[-1])
    e_slow = float(ema_slow.iloc[-1])
    rsi_val = float(rsi_series.iloc[-1])
    adx_val = float(adx_series.iloc[-1])
    pdi = float(plus_di.iloc[-1])
    mdi = float(minus_di.iloc[-1])
    hist = float(macd_hist.iloc[-1])
    slope = float(ema_mid.iloc[-1] - ema_mid.iloc[-1 - _SLOPE_LOOKBACK])

    # Набор направленных голосов в {-1, 0, +1}.
    votes: dict[str, int] = {}
    if e_fast > e_mid > e_slow:
        votes["ema_stack"] = 1
    elif e_fast < e_mid < e_slow:
        votes["ema_stack"] = -1
    else:
        votes["ema_stack"] = 0
    votes["ema_slope"] = 1 if slope > 0 else (-1 if slope < 0 else 0)
    votes["macd"] = 1 if hist > 0 else (-1 if hist < 0 else 0)
    votes["rsi"] = 1 if rsi_val > _RSI_BULL else (-1 if rsi_val < _RSI_BEAR else 0)
    votes["di"] = 1 if pdi > mdi else (-1 if pdi < mdi else 0)

    score = sum(votes.values())
    n_votes = len(votes)
    agreement = abs(score) / n_votes
    adx_factor = min(adx_val / 40.0, 1.0)

    # Слабый тренд (низкий ADX) и нет сильного перевеса голосов → нейтрально.
    weak_trend = adx_val < _ADX_TREND_MIN and abs(score) < 3
    if score == 0 or weak_trend:
        signal = SIGNAL_NEUTRAL
        confidence = _round(agreement * 0.3 * (0.5 + 0.5 * adx_factor), 4)
    else:
        signal = SIGNAL_BULLISH if score > 0 else SIGNAL_BEARISH
        confidence = _round(
            min(agreement * (0.4 + 0.6 * adx_factor), 1.0), 4
        )

    metrics: dict[str, Any] = {
        "n_candles": n,
        "close": _round(float(close.iloc[-1]), 2),
        "ema20": _round(e_fast, 2),
        "ema50": _round(e_mid, 2),
        "ema200": _round(e_slow, 2),
        "ema50_slope": _round(slope, 4),
        "rsi14": _round(rsi_val, 2),
        "atr14": _round(float(atr_series.iloc[-1]), 4),
        "macd": _round(float(macd_line.iloc[-1]), 4),
        "macd_signal": _round(float(macd_signal.iloc[-1]), 4),
        "macd_hist": _round(hist, 4),
        "adx14": _round(adx_val, 2),
        "plus_di": _round(pdi, 2),
        "minus_di": _round(mdi, 2),
        "support": _round(support, 2),
        "resistance": _round(resistance, 2),
        "votes": votes,
        "score": score,
    }

    rationale = (
        f"EMA-стек={votes['ema_stack']:+d}, наклон EMA50={'вверх' if slope > 0 else 'вниз'}, "
        f"ADX={adx_val:.1f}, RSI={rsi_val:.1f}, MACD-гист={hist:+.2f} → "
        f"сумма голосов {score:+d}."
    )
    return signal, confidence, metrics, rationale


class MarketAgent(BaseAgent):
    """Агент технического анализа по свечам OHLCV."""

    def __init__(
        self,
        instrument_id: int,
        timeframe: str,
        min_candles: int,
        interval: float,
    ) -> None:
        super().__init__(name="market", interval=interval, instrument_id=instrument_id)
        self.timeframe = timeframe
        self.min_candles = min_candles

    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает свечи и формирует заключение по техническому анализу."""
        # Берём с запасом, чтобы EMA200 успела «прогреться».
        limit = max(self.min_candles, _EMA_SLOW) + 50
        rows = await db.get_ohlcv(instrument_id, self.timeframe, limit)
        df = pd.DataFrame([dict(r) for r in rows])

        signal, confidence, metrics, rationale = analyze_ohlcv(df, self.min_candles)
        metrics["timeframe"] = self.timeframe
        return AgentOutput(
            agent=self.name,
            instrument_id=instrument_id,
            signal=signal,
            confidence=confidence,
            metrics=metrics,
            rationale=rationale,
        )
