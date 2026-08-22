"""Market Agent: технический анализ по свечам OHLCV.

Все индикаторы считаются вручную на pandas/numpy с фиксированными периодами
(EMA 20/50/200, RSI 14, ATR 14, MACD 12/26/9, ADX 14). Расчёт детерминирован:
на одних и тех же данных результат всегда одинаковый.

Агент читает ТОЛЬКО свечи своего инструмента и не обращается к выводам
других агентов.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from pandas.api.types import is_numeric_dtype

from src.agents.base import (
    SIGNAL_BEARISH,
    SIGNAL_BULLISH,
    SIGNAL_NEUTRAL,
    AgentOutput,
    BaseAgent,
    normalize_confidence,
)
from src.core.config import settings
from src.core.db import db

# Характеристический масштаб уверенности (Задача A). У Market Agent сырая
# уверенность по природе нормирована (доля голосов), её теоретический и
# практический максимум = 1.0, поэтому нормировка — тождество. Масштаб задан
# явно, чтобы правило «делим на максимум агента» было единым для трёх агентов.
CONFIDENCE_SCALE = 1.0

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

    # Деление на ноль там, где plus_di + minus_di == 0 (нет направленного
    # движения: постоянные high/low). Раньше 0 заменялся на ``pd.NA`` — и это
    # был корень инцидента: ``pd.NA`` во float-ряду делает его dtype=object, а
    # следующий ``ewm().mean()`` на object-ряде падает («No numeric types to
    # aggregate» / ``float()`` от ``NAType``). Пропуск помечаем ``np.nan`` (не
    # ``pd.NA``): ряд остаётся float, nan штатно протягивается ``ewm`` и гасится
    # финальным ``fillna(0.0)``. ``where(cond)`` без ``other`` подставляет np.nan.
    di_sum = plus_di + minus_di
    di_sum = di_sum.where(di_sum != 0.0)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)
    return adx_series, plus_di.fillna(0.0), minus_di.fillna(0.0)


def _round(value: float, digits: int = 6) -> float:
    """Округление с защитой от NaN/inf (для JSON-метрик).

    Задача B.5 (Этап 7.0): раньше ``inf`` проходил сквозь guard (``pd.isna`` его
    не ловит), попадал в metrics, и ``json.dumps`` выдавал невалидный для JSONB
    ``Infinity`` → ``INSERT`` падал, а вывод агента терялся молча. Теперь ``inf``/
    ``-inf`` приводятся к 0.0 наравне с NaN. Это защита записи, направление и
    величину «нормального» сигнала не меняет.
    """
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
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
    required = ("open", "high", "low", "close")
    if not df.empty and all(c in df.columns for c in ("open", "high", "low", "close", "volume")):
        df = df.copy()
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
    else:
        # Нет нужных колонок (пустая/битая выборка) — гарантируем пустой кадр,
        # чтобы явная проверка ниже дала insufficient_data, а не KeyError.
        df = df.iloc[0:0].copy()

    # Явная защита ПЕРЕД агрегацией (Этап 7.2, Задача A1). Именно
    # «No numeric types to aggregate» (DataError на нечисловом/пустом кадре) валил
    # Market 8 часов 14.08. Теперь пустой ИЛИ нечисловой кадр — это штатный вывод
    # insufficient_data с confidence=0 (строка в agent_outputs пишется), а НЕ
    # исключение, срывающее итерацию без записи.
    numeric_ok = not df.empty and all(is_numeric_dtype(df[c]) for c in required)
    n = len(df)
    if not numeric_ok or n < min_candles:
        reason = (
            f"Недостаточно свечей: {n} < {min_candles}."
            if numeric_ok
            else "Пустая или нечисловая выборка свечей — агрегация невозможна."
        )
        return (
            "insufficient_data",
            0.0,
            {"n_candles": n, "min_candles": min_candles},
            reason,
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

    # Защита ПОСЛЕ промежуточных шагов (Этап 7.2, доработка A1): корень инцидента
    # был не на входе (вход чистый — 250 float64-свечей), а ВНУТРИ вычислений —
    # pd.NA превращал промежуточный ряд в object. Проверяем, что каждый расчётный
    # ряд остался числовым; если нет — insufficient_data, а НЕ исключение на
    # последующих .iloc[-1]/агрегациях. Это ловит и будущие регрессии такого рода.
    _intermediate = (
        ema_fast, ema_mid, ema_slow, rsi_series, atr_series,
        macd_line, macd_signal, macd_hist, adx_series, plus_di, minus_di,
    )
    if any(not is_numeric_dtype(s) for s in _intermediate):
        return (
            "insufficient_data",
            0.0,
            {"n_candles": n, "min_candles": min_candles},
            "Промежуточный ряд расчёта перестал быть числовым — вывод отложен.",
        )

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
    # Направление сигнала НЕ меняется — нормируется только величина уверенности.
    weak_trend = adx_val < _ADX_TREND_MIN and abs(score) < 3
    if score == 0 or weak_trend:
        signal = SIGNAL_NEUTRAL
        confidence_raw = _round(agreement * 0.3 * (0.5 + 0.5 * adx_factor), 4)
    else:
        signal = SIGNAL_BULLISH if score > 0 else SIGNAL_BEARISH
        confidence_raw = _round(min(agreement * (0.4 + 0.6 * adx_factor), 1.0), 4)
    confidence = normalize_confidence(confidence_raw, CONFIDENCE_SCALE)

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
        # Сырое значение уверенности до нормировки (Задача A) — для сравнения
        # старого и нового режимов.
        "confidence_raw": confidence_raw,
    }

    rationale = (
        f"EMA-стек={votes['ema_stack']:+d}, наклон EMA50={'вверх' if slope > 0 else 'вниз'}, "
        f"ADX={adx_val:.1f}, RSI={rsi_val:.1f}, MACD-гист={hist:+.2f} → "
        f"сумма голосов {score:+d}."
    )
    return signal, confidence, metrics, rationale


# Длительность таймфрейма в секундах — для отсечения незавершённого бара.
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400,
}


def timeframe_seconds(timeframe: str) -> int | None:
    """Длительность бара в секундах или None, если таймфрейм незнаком.

    Незнакомый таймфрейм НЕ повод угадывать длительность: отсечение просто не
    выполняется, и это видно в логе. Молча «прикинуть» час означало бы выкинуть
    настоящие данные.
    """
    return _TIMEFRAME_SECONDS.get(timeframe.strip().lower())


def drop_unclosed_bar(
    df: pd.DataFrame, timeframe: str, now: datetime
) -> pd.DataFrame:
    """Убирает из выборки ПОСЛЕДНИЙ бар, если он ещё не закрылся (§8 ТЗ 8.1).

    Бар с меткой ``ts`` закрыт, когда ``ts + длительность <= now``. Коллектор
    сохраняет незавершённый бар и пересчитывает его на ходу (замер 22.08.2026:
    за 90 секунд объём 53.86 → 55.19, close 77179.2 → 77158.7 при той же метке
    17:00), поэтому в 17:01 Market видел бы бар из одной минуты торгов, а в
    17:59 — из пятидесяти девяти.

    Функция вызывается ТОЛЬКО при ``MARKET_CLOSED_BARS_ONLY=true``. При
    значении по умолчанию (false) поведение системы не меняется ни в чём.
    """
    if df.empty or "ts" not in df.columns:
        return df
    seconds = timeframe_seconds(timeframe)
    if seconds is None:
        return df
    last_ts = df["ts"].iloc[-1]
    if not isinstance(last_ts, datetime):
        return df
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=UTC)
    # Закрыт ровно в момент закрытия: бар 17:00 при часовом таймфрейме закрыт
    # в 18:00:00, а не в 18:00:01.
    if last_ts + timedelta(seconds=seconds) <= now:
        return df
    return df.iloc[:-1]


class MarketAgent(BaseAgent):
    """Агент технического анализа по свечам OHLCV."""

    def __init__(
        self,
        instrument_id: int,
        timeframe: str,
        min_candles: int,
        interval: float,
        name_suffix: str = "",
        closed_bars_only: bool | None = None,
    ) -> None:
        super().__init__(name="market", interval=interval, instrument_id=instrument_id,
                         name_suffix=name_suffix)
        self.timeframe = timeframe
        self.min_candles = min_candles
        # None — берём значение из конфигурации (по умолчанию false).
        self.closed_bars_only = (
            settings.MARKET_CLOSED_BARS_ONLY if closed_bars_only is None
            else bool(closed_bars_only)
        )

    async def analyze(self, instrument_id: int) -> AgentOutput:
        """Читает свечи и формирует заключение по техническому анализу.

        Состояние между итерациями НЕ переносится (Этап 7.2): выборка строится
        заново на каждой итерации по последним ``limit`` свечам (ORDER BY ts DESC
        LIMIT), без запомненного ``last_ts``, курсора или кэша DataFrame. Граница
        выборки не может «уйти в будущее»: она вообще не строится от какого-либо
        сохранённого значения времени — берутся просто самые свежие свечи.
        """
        # Берём с запасом, чтобы EMA200 успела «прогреться».
        limit = max(self.min_candles, _EMA_SLOW) + 50
        rows = await db.get_ohlcv(instrument_id, self.timeframe, limit)
        # Полностью пустой ответ при живом сервисе = симптом инцидента 14.08.
        await self._note_read(is_empty=(len(rows) == 0))
        df = pd.DataFrame([dict(r) for r in rows])

        # §8 ТЗ 8.1: незавершённый бар. По умолчанию (false) окно остаётся
        # ровно тем же, что и до Этапа 8.1, — включая недосчитанный последний
        # бар. Переключатель введён, чтобы эффект можно было измерить, а не
        # чтобы применить его сейчас.
        bars_dropped = 0
        if self.closed_bars_only:
            before = len(df)
            df = drop_unclosed_bar(df, self.timeframe, datetime.now(UTC))
            bars_dropped = before - len(df)

        signal, confidence, metrics, rationale = analyze_ohlcv(df, self.min_candles)
        metrics["timeframe"] = self.timeframe
        metrics["closed_bars_only"] = bool(self.closed_bars_only)
        if bars_dropped:
            metrics["unclosed_bar_dropped"] = bars_dropped
        return AgentOutput(
            agent=self.name,
            instrument_id=instrument_id,
            signal=signal,
            confidence=confidence,
            metrics=metrics,
            rationale=rationale,
        )
