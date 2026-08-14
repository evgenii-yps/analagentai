"""Тесты агентов: расчёт индикаторов и логика на фиксированных данных.

Проверяют детерминированность (одинаковый ввод → одинаковый вывод),
правдоподобность метрик (RSI 0–100, ADX ≥ 0) и базовую логику сигналов.
"""

import numpy as np
import pandas as pd

from src.agents.base import normalize_confidence
from src.agents.futures import analyze_futures
from src.agents.liquidity import CONFIDENCE_SCALE as LIQ_SCALE
from src.agents.liquidity import analyze_orderbook
from src.agents.market import _round, analyze_ohlcv, ema, rsi


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    """Строит OHLCV-датафрейм из ряда цен закрытия (детерминированно)."""
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 10.0),
        }
    )


def _uptrend(n: int = 260) -> pd.DataFrame:
    idx = np.arange(n)
    return _make_ohlcv((100.0 + idx * 0.5 + np.sin(idx / 5.0) * 2.0).tolist())


def _downtrend(n: int = 260) -> pd.DataFrame:
    idx = np.arange(n)
    return _make_ohlcv((300.0 - idx * 0.5 + np.sin(idx / 5.0) * 2.0).tolist())


# --- Индикаторы ---

def test_ema_of_constant_is_constant() -> None:
    s = pd.Series([5.0] * 50)
    assert ema(s, 20).iloc[-1] == 5.0


def test_rsi_monotonic_increase_is_100() -> None:
    s = pd.Series([float(i) for i in range(1, 60)])
    assert rsi(s).iloc[-1] == 100.0


def test_rsi_within_bounds() -> None:
    df = _uptrend()
    r = rsi(df["close"])
    assert r.dropna().between(0.0, 100.0).all()


# --- Market Agent ---

def test_market_insufficient_data() -> None:
    df = _make_ohlcv([100.0] * 50)  # меньше 200 свечей
    signal, confidence, metrics, _ = analyze_ohlcv(df, min_candles=200)
    assert signal == "insufficient_data"
    assert confidence == 0.0
    assert metrics["n_candles"] == 50


def test_market_uptrend_is_bullish_and_metrics_plausible() -> None:
    signal, confidence, metrics, _ = analyze_ohlcv(_uptrend(), min_candles=200)
    assert signal == "bullish"
    assert 0.0 <= confidence <= 1.0
    assert 0.0 <= metrics["rsi14"] <= 100.0
    assert metrics["adx14"] >= 0.0
    assert metrics["plus_di"] >= 0.0 and metrics["minus_di"] >= 0.0
    assert metrics["resistance"] >= metrics["support"]


def test_market_downtrend_is_bearish() -> None:
    signal, _, _, _ = analyze_ohlcv(_downtrend(), min_candles=200)
    assert signal == "bearish"


def test_market_is_deterministic() -> None:
    df = _uptrend()
    assert analyze_ohlcv(df, 200) == analyze_ohlcv(df, 200)


# --- Liquidity Agent ---

def _snapshots(bid_vol: float, ask_vol: float, n: int = 6) -> list[dict]:
    return [
        {
            "spread": 0.1,
            "bid_volume": bid_vol,
            "ask_volume": ask_vol,
            "bids": [[100.0, 1.0], [99.0, 1.0]],
            "asks": [[100.1, 1.0], [101.0, 1.0]],
        }
        for _ in range(n)
    ]


def test_liquidity_insufficient_data() -> None:
    signal, confidence, _, _ = analyze_orderbook(_snapshots(100, 50, n=3))
    assert signal == "insufficient_data"
    assert confidence == 0.0


def test_liquidity_bid_heavy_is_bullish() -> None:
    signal, confidence, metrics, _ = analyze_orderbook(_snapshots(100, 50))
    assert signal == "bullish"
    assert metrics["imbalance"] > 0
    assert 0.0 <= confidence <= 1.0


def test_liquidity_ask_heavy_is_bearish() -> None:
    signal, _, metrics, _ = analyze_orderbook(_snapshots(50, 100))
    assert signal == "bearish"
    assert metrics["imbalance"] < 0


def test_liquidity_balanced_is_neutral() -> None:
    signal, _, _, _ = analyze_orderbook(_snapshots(100, 100))
    assert signal == "neutral"


# --- Futures Agent ---

def _oi(first: float, last: float) -> list[dict]:
    return [{"value": first}, {"value": (first + last) / 2}, {"value": last}]


def test_futures_insufficient_data() -> None:
    signal, _, _, _ = analyze_futures(funding=[], open_interest=[])
    assert signal == "insufficient_data"


def test_futures_moderate_positive_funding_rising_oi_is_bullish() -> None:
    signal, conf, _, _ = analyze_futures(
        funding=[{"rate": 0.0001}], open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "bullish"
    assert 0.0 <= conf <= 1.0


def test_futures_extreme_funding_is_reversal_bearish() -> None:
    signal, _, metrics, _ = analyze_futures(
        funding=[{"rate": 0.002}], open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "bearish"          # экстремальный + funding → разворот вниз
    assert metrics["funding_extreme"] is True


def test_futures_flat_oi_is_neutral() -> None:
    signal, _, _, _ = analyze_futures(
        funding=[{"rate": 0.0001}], open_interest=_oi(1000.0, 1000.0)
    )
    assert signal == "neutral"


def test_futures_is_deterministic() -> None:
    args = ([{"rate": 0.0001}], _oi(1000.0, 1100.0))
    assert analyze_futures(*args) == analyze_futures(*args)


# --- Задача A: приведение шкал уверенности (Этап 7.0) ---

def test_normalize_confidence_basic() -> None:
    # market: масштаб 1.0 → тождество.
    assert normalize_confidence(0.5, 1.0) == 0.5
    # liquidity: 0.057/0.15 ≈ 0.38.
    assert normalize_confidence(0.057, 0.15) == 0.38
    # насыщение на 1.0.
    assert normalize_confidence(0.2, 0.1) == 1.0
    # отрицательное/некорректный масштаб → 0.0.
    assert normalize_confidence(-1.0, 0.15) == 0.0
    assert normalize_confidence(0.5, 0.0) == 0.0


def test_market_saves_confidence_raw_and_identity_scale() -> None:
    signal, confidence, metrics, _ = analyze_ohlcv(_uptrend(), min_candles=200)
    assert signal == "bullish"
    assert "confidence_raw" in metrics
    # У market масштаб 1.0 → нормировка тождественна.
    assert confidence == metrics["confidence_raw"]


def test_liquidity_saves_confidence_raw_and_amplifies() -> None:
    signal, confidence, metrics, _ = analyze_orderbook(_snapshots(100, 50))
    assert signal == "bullish"                       # направление НЕ изменилось
    assert "confidence_raw" in metrics
    # Нормировка = сырое/scale с насыщением — усиливает мелкую сырую уверенность.
    assert confidence == normalize_confidence(metrics["confidence_raw"], LIQ_SCALE)
    assert confidence >= metrics["confidence_raw"]


def test_futures_saves_confidence_raw() -> None:
    _, _, metrics, _ = analyze_futures(
        funding=[{"rate": 0.0001}], open_interest=_oi(1000.0, 1100.0)
    )
    assert "confidence_raw" in metrics


def test_normalization_preserves_direction_on_fixed_inputs() -> None:
    # Направление не должно зависеть от нормировки уверенности (критерий приёмки).
    assert analyze_ohlcv(_uptrend(), 200)[0] == "bullish"
    assert analyze_ohlcv(_downtrend(), 200)[0] == "bearish"
    assert analyze_orderbook(_snapshots(100, 50))[0] == "bullish"
    assert analyze_orderbook(_snapshots(50, 100))[0] == "bearish"


# --- Задача B.5: _round гасит inf (иначе JSONB отвергал INSERT) ---

def test_round_clamps_inf_and_nan() -> None:
    assert _round(float("inf")) == 0.0
    assert _round(float("-inf")) == 0.0
    assert _round(float("nan")) == 0.0
    assert _round(1.23456, 2) == 1.23


# --- Задача C: симметрия Futures — медвежьи ветки достижимы ---

def test_futures_negative_funding_rising_oi_is_bearish() -> None:
    # Реалистичный отрицательный funding (распродажа) + рост OI → продолжение вниз.
    signal, _, _, _ = analyze_futures(
        funding=[{"rate": -0.0002}], open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "bearish"


def test_futures_positive_extreme_is_bearish_at_new_threshold() -> None:
    # При новом пороге 0.0003 реалистичный всплеск 0.0004 → ветка разворота (bearish).
    signal, _, metrics, _ = analyze_futures(
        funding=[{"rate": 0.0004}],
        open_interest=_oi(1000.0, 1000.0),
        extreme_threshold=0.0003,
    )
    assert signal == "bearish"
    assert metrics["funding_extreme"] is True


def test_futures_extreme_threshold_is_configurable() -> None:
    # То же значение funding при старом пороге 0.0005 экстремумом НЕ считается.
    signal, _, metrics, _ = analyze_futures(
        funding=[{"rate": 0.0004}],
        open_interest=_oi(1000.0, 1000.0),
        extreme_threshold=0.0005,
    )
    assert signal == "neutral"
    assert metrics["funding_extreme"] is False


# --- Задача A1 (Этап 7.2): пустой/нечисловой кадр → insufficient_data, НЕ DataError ---

def test_market_empty_dataframe_is_insufficient_not_exception() -> None:
    # Именно это (пустая выборка) валило Market как DataError 8 часов 14.08.
    signal, confidence, metrics, _ = analyze_ohlcv(pd.DataFrame(), min_candles=200)
    assert signal == "insufficient_data"
    assert confidence == 0.0
    assert metrics["n_candles"] == 0


def test_market_object_columns_dataframe_is_insufficient_not_exception() -> None:
    # Нечисловые (object) колонки — вторая версия причины DataError. Должно быть
    # insufficient_data без исключения (проверка перед агрегацией).
    df = pd.DataFrame({c: ["x"] * 250 for c in ("open", "high", "low", "close", "volume")})
    signal, confidence, _, _ = analyze_ohlcv(df, min_candles=200)
    assert signal == "insufficient_data"
    assert confidence == 0.0


def test_market_missing_columns_dataframe_is_insufficient_not_exception() -> None:
    # Кадр без нужных колонок (битая выборка) не должен падать KeyError/DataError.
    df = pd.DataFrame({"foo": [1.0] * 250})
    signal, confidence, _, _ = analyze_ohlcv(df, min_candles=200)
    assert signal == "insufficient_data"
    assert confidence == 0.0
