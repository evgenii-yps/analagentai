"""Тесты агентов: расчёт индикаторов и логика на фиксированных данных.

Проверяют детерминированность (одинаковый ввод → одинаковый вывод),
правдоподобность метрик (RSI 0–100, ADX ≥ 0) и базовую логику сигналов.
"""

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from src.agents.base import normalize_confidence
from src.agents.futures import analyze_futures
from src.agents.liquidity import CONFIDENCE_SCALE as LIQ_SCALE
from src.agents.liquidity import analyze_orderbook
from src.agents.market import _round, adx, analyze_ohlcv, ema, rsi


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


# Этап 7.3: направление задаётся перцентилем текущего значения в СВОЁМ окне,
# а не сравнением с абсолютным порогом. Полный набор проверок симметрии и
# достижимости обеих веток — в tests/test_futures_symmetry.py.

def _window(values: list[float]) -> list[dict]:
    return [{"rate": v} for v in values]


def _rising(n: int = 30, start: float = 0.00002, step: float = 0.000001) -> list[float]:
    return [start + step * i for i in range(n)]


def test_futures_top_of_window_is_bullish() -> None:
    signal, conf, _, _ = analyze_futures(
        funding=_window(_rising()), open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "bullish"
    assert 0.0 <= conf <= 1.0


def test_futures_bottom_of_window_is_bearish() -> None:
    # Тот же ряд в обратном порядке: текущее значение — минимум окна.
    signal, _, metrics, _ = analyze_futures(
        funding=_window(_rising()[::-1]), open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "bearish"
    assert metrics["funding_pct"] <= 0.20


def test_futures_middle_of_window_is_neutral() -> None:
    values = _rising(n=29)
    values.append(values[len(values) // 2])   # текущее = середина распределения
    signal, _, _, _ = analyze_futures(
        funding=_window(values), open_interest=_oi(1000.0, 1000.0)
    )
    assert signal == "neutral"


def test_futures_short_window_is_insufficient_data() -> None:
    # Окно короче FUTURES_MIN_POINTS: «нет данных», а не «сигнала нет».
    signal, _, _, _ = analyze_futures(
        funding=_window(_rising(n=5)), open_interest=_oi(1000.0, 1100.0)
    )
    assert signal == "insufficient_data"


def test_futures_is_deterministic() -> None:
    args = (_window(_rising()), _oi(1000.0, 1100.0))
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
        funding=_window(_rising()), open_interest=_oi(1000.0, 1100.0)
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


# --- Симметрия Futures (Задача C Этапа 7.0 → переделана Этапом 7.3) ---
# Абсолютный порог экстремума признан причиной односторонности агента: за 8 суток
# он не сработал ни разу, а вторая ветка bearish требовала отрицательного funding,
# которого у BTC практически не бывает. Теперь границы относительные, и проверка
# симметрии выполняется зеркальным отражением ряда (tests/test_futures_symmetry.py).

def test_futures_direction_does_not_depend_on_sign() -> None:
    # Ряд одинаковой ФОРМЫ, но целиком положительный и целиком отрицательный,
    # даёт одно и то же направление: знак funding больше ничего не решает.
    shape = _rising()
    positive = _window([0.0001 + v for v in shape])
    negative = _window([-0.0005 + v for v in shape])
    assert analyze_futures(positive, _oi(1000.0, 1000.0))[0] == "bullish"
    assert analyze_futures(negative, _oi(1000.0, 1000.0))[0] == "bullish"


def test_futures_pct_thresholds_are_configurable() -> None:
    # Более узкая нейтральная зона делает то же значение направленным.
    values = _rising(n=29)
    values.append(values[len(values) // 2 + 3])
    strict = analyze_futures(_window(values), _oi(1000.0, 1000.0))[0]
    loose = analyze_futures(
        _window(values), _oi(1000.0, 1000.0), pct_high=0.55, pct_low=0.45
    )[0]
    assert strict == "neutral"
    assert loose == "bullish"


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


# --- Задача 7.2 (доработка): pd.NA внутри adx() → object → крах ewm() ---

def _flat_hl_ohlcv(n: int = 60) -> pd.DataFrame:
    """OHLCV с ПОСТОЯННЫМИ high/low (нет направленного движения), но tr>0.

    high.diff()==0 и low.diff()==0 → plus_dm=minus_dm=0 → plus_di=minus_di=0 →
    plus_di+minus_di == 0. Раньше это заменялось на pd.NA, dx становился object,
    и adx()'s ewm().mean() падал (TypeError NAType / «No numeric types to aggregate»).
    """
    close = np.full(n, 100.0)
    return pd.DataFrame(
        {
            "open": close,
            "high": np.full(n, 105.0),  # постоянный high
            "low": np.full(n, 95.0),    # постоянный low
            "close": close,
            "volume": np.full(n, 10.0),
        }
    )


def test_adx_zero_di_sum_does_not_crash() -> None:
    # Прямой вызов adx() на участке plus_di+minus_di == 0. Тест ОБЯЗАН падать на
    # текущем коде (pd.NA → object → ewm крашится) и проходить после правки.
    df = _flat_hl_ohlcv(60)
    adx_series, plus_di, minus_di = adx(df["high"], df["low"], df["close"])
    assert is_numeric_dtype(adx_series)
    assert is_numeric_dtype(plus_di)
    assert is_numeric_dtype(minus_di)
    # Нет направленного движения → ADX и оба DI должны быть 0, без NaN/исключений.
    assert float(adx_series.iloc[-1]) == 0.0


def test_market_flat_high_low_is_insufficient_or_neutral_not_exception() -> None:
    # Тот же вырожденный вход, но через полный analyze_ohlcv (≥200 свечей): не
    # должно быть исключения — либо neutral, либо insufficient_data.
    df = _flat_hl_ohlcv(260)
    signal, confidence, _, _ = analyze_ohlcv(df, min_candles=200)
    assert signal in {"neutral", "insufficient_data"}
    assert 0.0 <= confidence <= 1.0
