"""Этап 7.3, Блок A: симметрия агентов Futures и Market.

Тесты достижимости веток и симметрии ПАДАЮТ на коде до Этапа 7.3: там
направление задавалось абсолютным порогом funding и его знаком, из-за чего
ветка ``bearish`` была структурно недостижима на реальных данных (0 значений
за 8 суток наблюдений, 11 185 выводов).
"""

from __future__ import annotations

import csv
import os

import numpy as np
import pandas as pd
import pytest

from src.agents.futures import (
    analyze_futures,
    direction_from_percentile,
    percentile_rank,
)
from src.agents.market import analyze_ohlcv

# Фикстура реального среза funding: путь до CSV с колонкой rate (см. модуль ниже).
_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "funding_okx_btc_2026_08.csv"
)


def _funding(rates: list[float]) -> list[dict[str, float]]:
    """Окно funding в формате, который возвращает БД (по возрастанию ts)."""
    return [{"rate": r} for r in rates]


def _oi(values: list[float]) -> list[dict[str, float]]:
    return [{"value": v} for v in values]


def _rising_series(n: int = 40, start: float = 0.00002, step: float = 0.000001) -> list[float]:
    """Строго возрастающий ряд: последнее значение — максимум окна."""
    return [start + step * i for i in range(n)]


# --- Перцентиль: свойства, на которых держится симметрия --------------------

def test_percentile_rank_is_mid_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile_rank(values, 1.0) == pytest.approx(0.125)
    assert percentile_rank(values, 4.0) == pytest.approx(0.875)


def test_percentile_rank_is_antisymmetric() -> None:
    """Отражение относительно любого центра переводит перцентиль в 1 − перцентиль."""
    values = [0.1, 0.4, 0.4, 0.9, 1.5, 2.2]
    center = 0.65
    for current in values:
        mirrored = [2 * center - v for v in values]
        assert percentile_rank(mirrored, 2 * center - current) == pytest.approx(
            1.0 - percentile_rank(values, current), abs=1e-12
        )


def test_direction_thresholds_are_mirror_images() -> None:
    """При границах 0.20/0.80 уверенность bullish и bearish зеркальны."""
    bull_signal, bull_conf = direction_from_percentile(0.95, 0.80, 0.20)
    bear_signal, bear_conf = direction_from_percentile(0.05, 0.80, 0.20)
    assert bull_signal == "bullish"
    assert bear_signal == "bearish"
    assert bull_conf == pytest.approx(bear_conf, abs=1e-12)


# --- Достижимость обеих веток (падают на старом коде) -----------------------

def test_futures_bearish_reachable() -> None:
    """Текущее значение ниже 20-го перцентиля окна → bearish.

    Все значения ряда ПОЛОЖИТЕЛЬНЫ и по модулю меньше прежнего абсолютного
    порога 0.0003 — то есть ровно тот случай, в котором старый код не мог
    выдать bearish ни при каких условиях.
    """
    rates = _rising_series()[::-1]  # убывающий ряд: последнее значение — минимум
    assert all(r > 0 for r in rates)
    assert max(abs(r) for r in rates) < 0.0003
    signal, confidence, metrics, _ = analyze_futures(_funding(rates), _oi([]))
    assert signal == "bearish"
    assert 0.0 <= confidence <= 1.0
    assert metrics["funding_pct"] <= 0.20


def test_futures_bullish_reachable() -> None:
    """Симметричный случай: значение выше 80-го перцентиля → bullish."""
    rates = _rising_series()
    signal, confidence, metrics, _ = analyze_futures(_funding(rates), _oi([]))
    assert signal == "bullish"
    assert 0.0 <= confidence <= 1.0
    assert metrics["funding_pct"] >= 0.80


def test_futures_middle_of_distribution_is_neutral() -> None:
    """Значение в середине своего распределения → neutral (а не «нет данных»)."""
    rates = _rising_series(n=41)
    rates = rates + [rates[len(rates) // 2]]  # текущее = медиана окна
    signal, _, _, _ = analyze_futures(_funding(rates), _oi([]))
    assert signal == "neutral"


def test_futures_symmetry() -> None:
    """Зеркальное отражение ряда относительно медианы меняет направление.

    Уверенность при этом обязана совпасть с точностью до 1e-9: направление —
    единственное, что зависит от стороны распределения.
    """
    # Неровный ряд (не монотонный), у которого ТЕКУЩЕЕ значение — в верхнем
    # хвосте: иначе обе стороны попали бы в нейтральную зону и сравнивать было
    # бы нечего.
    rates = [0.00001 * (i % 7) + 0.000002 * i for i in range(29)] + [0.00021]
    median = float(np.median(rates))
    mirrored = [2 * median - r for r in rates]

    signal, confidence, _, _ = analyze_futures(_funding(rates), _oi([]))
    m_signal, m_confidence, _, _ = analyze_futures(_funding(mirrored), _oi([]))

    assert {signal, m_signal} == {"bullish", "bearish"}
    assert confidence == pytest.approx(m_confidence, abs=1e-9)


def test_futures_symmetry_with_oi_confirmation() -> None:
    """Симметрия сохраняется и когда OI подтверждает направление."""
    rates = _rising_series(n=30)
    oi = [1000.0 + 5.0 * i for i in range(30)]
    median_r = float(np.median(rates))
    median_o = float(np.median(oi))

    signal, confidence, _, _ = analyze_futures(_funding(rates), _oi(oi))
    m_signal, m_confidence, _, _ = analyze_futures(
        _funding([2 * median_r - r for r in rates]),
        _oi([2 * median_o - v for v in oi]),
    )
    assert {signal, m_signal} == {"bullish", "bearish"}
    assert confidence == pytest.approx(m_confidence, abs=1e-9)


def test_futures_oi_confirmation_raises_confidence() -> None:
    """Подтверждение со стороны OI усиливает уверенность (объединение не изменено)."""
    rates = _rising_series(n=30)
    oi_up = [1000.0 + 5.0 * i for i in range(30)]
    _, conf_without, _, _ = analyze_futures(_funding(rates), _oi([]))
    _, conf_with, metrics, _ = analyze_futures(_funding(rates), _oi(oi_up))
    assert metrics["oi_confirms"] is True
    assert conf_with > conf_without


def test_futures_insufficient_data() -> None:
    """Окно короче FUTURES_MIN_POINTS → insufficient_data, а НЕ neutral."""
    signal, confidence, metrics, _ = analyze_futures(
        _funding(_rising_series(n=19)), _oi([]), min_points=20
    )
    assert signal == "insufficient_data"
    assert confidence == 0.0
    assert metrics["n_funding"] == 19


def test_futures_empty_window_is_insufficient_data() -> None:
    signal, _, _, _ = analyze_futures([], [])
    assert signal == "insufficient_data"


def test_futures_is_deterministic() -> None:
    args = (_funding(_rising_series()), _oi([1000.0 + i for i in range(30)]))
    assert analyze_futures(*args) == analyze_futures(*args)


def test_futures_direction_ignores_sign_of_funding() -> None:
    """Знак funding больше не определяет направление — только положение в окне.

    Два ряда с одинаковой ФОРМОЙ, но разным уровнем (весь положительный против
    всего отрицательного) дают одно и то же направление. На старом коде знак был
    определяющим, и это была вторая причина отсутствия bearish.
    """
    shape = _rising_series(n=30)
    positive = [0.0001 + s for s in shape]
    negative = [-0.0005 + s for s in shape]
    assert all(r > 0 for r in positive)
    assert all(r < 0 for r in negative)
    assert analyze_futures(_funding(positive), _oi([]))[0] == (
        analyze_futures(_funding(negative), _oi([]))[0]
    )


@pytest.mark.skipif(
    not os.path.isfile(_FIXTURE),
    reason=(
        "Фикстура реального funding не приложена: у среды разработки нет доступа "
        "к БД сервера. Команда выгрузки среза приведена в STAGE_7_3_REPORT.md; "
        "после её выполнения файл кладётся в tests/fixtures/ и тест включается."
    ),
)
def test_futures_bearish_on_real_funding_slice() -> None:
    """На реальном срезе funding новая логика даёт ненулевое число bearish."""
    with open(_FIXTURE, encoding="utf-8") as fh:
        rates = [float(row["rate"]) for row in csv.DictReader(fh)]
    assert len(rates) >= 20, "срез слишком короткий для перцентиля"

    bearish = 0
    for end in range(20, len(rates) + 1):
        window = rates[max(0, end - 168):end]
        signal, _, _, _ = analyze_futures(_funding(window), _oi([]))
        if signal == "bearish":
            bearish += 1
    assert bearish > 0


# --- Market: проверка симметрии (ТЗ §3.3) -----------------------------------

def _ohlcv(closes: list[float]) -> pd.DataFrame:
    """OHLCV-кадр из ряда закрытий (свеча симметрична относительно close)."""
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 5.0,
            "low": close - 5.0,
            "close": close,
            "volume": np.full(close.shape, 10.0),
        }
    )


def _mirror(df: pd.DataFrame, center: float) -> pd.DataFrame:
    """Отражает кадр относительно уровня ``center``: p → 2·center − p.

    High и low при отражении меняются местами — иначе получился бы кадр
    с low выше high, то есть не рынок, а артефакт преобразования.
    """
    return pd.DataFrame(
        {
            "open": 2 * center - df["open"],
            "high": 2 * center - df["low"],
            "low": 2 * center - df["high"],
            "close": 2 * center - df["close"],
            "volume": df["volume"],
        }
    )


def test_market_is_structurally_symmetric() -> None:
    """Зеркальный рынок даёт зеркальное решение Market Agent при той же уверенности.

    Это ПРОВЕРКА, а не исправление: голоса Market (EMA-стек, наклон, MACD, RSI
    относительно 50, +DI/−DI) уже симметричны по построению, поэтому код агента
    Этапом 7.3 НЕ изменяется. Тест фиксирует это фактом, чтобы будущая правка,
    вносящая перекос, была видна сразу.
    """
    closes = [60000.0 + 40.0 * i + 300.0 * np.sin(i / 9.0) for i in range(260)]
    df = _ohlcv(closes)
    center = float(np.mean(closes))

    signal, confidence, _, _ = analyze_ohlcv(df, min_candles=200)
    m_signal, m_confidence, _, _ = analyze_ohlcv(_mirror(df, center), min_candles=200)

    assert {signal, m_signal} == {"bullish", "bearish"}
    assert confidence == pytest.approx(m_confidence, abs=1e-9)


def test_market_symmetric_on_falling_series() -> None:
    """Тот же вывод на падающем ряде: сторона рынка направления голосов не ломает."""
    closes = [60000.0 - 35.0 * i + 250.0 * np.sin(i / 7.0) for i in range(260)]
    df = _ohlcv(closes)
    center = float(np.mean(closes))

    signal, confidence, _, _ = analyze_ohlcv(df, min_candles=200)
    m_signal, m_confidence, _, _ = analyze_ohlcv(_mirror(df, center), min_candles=200)

    assert {signal, m_signal} == {"bullish", "bearish"}
    assert confidence == pytest.approx(m_confidence, abs=1e-9)
