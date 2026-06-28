"""Тесты агентов: расчёт индикаторов и логика на фиксированных данных.

Проверяют детерминированность (одинаковый ввод → одинаковый вывод),
правдоподобность метрик (RSI 0–100, ADX ≥ 0) и базовую логику сигналов.
"""

import numpy as np
import pandas as pd

from src.agents.futures import analyze_futures
from src.agents.liquidity import analyze_orderbook
from src.agents.market import analyze_ohlcv, ema, rsi


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
