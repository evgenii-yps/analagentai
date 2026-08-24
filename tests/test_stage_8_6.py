"""Тесты Этапа 8.6.

  8.6-1  test_ema_votes_*        — оба EMA-голоса принимают все три значения;
  8.6-2  test_ema_votes_symmetry — зеркальный ряд даёт зеркальные голоса;
  8.6-3  test_agent_silence_*    — надзор за молчанием агентов (§6).

Голоса НЕ переписывались: замер показал, что они исправны (см.
reports/8_6_report.md). Тесты закрепляют это, чтобы утверждение о запертом
голосе нельзя было повторить без проверки, и чтобы будущая правка не сломала
симметрию молча.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.agents.market import (
    _EMA_FAST,
    _EMA_MID,
    _EMA_SLOW,
    _SLOPE_LOOKBACK,
    analyze_ohlcv,
    ema,
)
from src.health import daily_report

# Ровно столько свечей запрашивает MarketAgent.analyze:
# limit = max(AGENT_MIN_CANDLES, _EMA_SLOW) + 50.
_WINDOW = 250


def _votes(close: np.ndarray) -> tuple[int, int]:
    """Голоса ema_stack и ema_slope на последнем баре окна — как в проде."""
    series = pd.Series(close[-_WINDOW:])
    fast = ema(series, _EMA_FAST).iloc[-1]
    mid = ema(series, _EMA_MID)
    slow = ema(series, _EMA_SLOW).iloc[-1]
    last, prev = mid.iloc[-1], mid.iloc[-1 - _SLOPE_LOOKBACK]
    stack = 1 if fast > last > slow else (-1 if fast < last < slow else 0)
    diff = last - prev
    return stack, (1 if diff > 0 else (-1 if diff < 0 else 0))


def _series(drift: float, n: int = _WINDOW, seed: int = 20260826) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.006, n)))


# --- 8.6-1. Все три значения достижимы ---

def test_ema_votes_go_positive_on_a_rising_series() -> None:
    assert _votes(_series(0.0006)) == (1, 1)


def test_ema_votes_go_negative_on_a_falling_series() -> None:
    """Отрицательное значение обоих голосов достижимо — это и опровергает
    утверждение «голос заперт в плюсе по устройству».

    Проверяется на выборке рядов, а не на одном: голос смотрит на последний бар
    окна, и отдельный жребий может лечь как угодно. Утверждение о запертом
    голосе — утверждение о НЕДОСТИЖИМОСТИ, и опровергается оно хотя бы одним
    достижением, а измеряется долей.
    """
    stacks, slopes = [], []
    for seed in range(40):
        stack, slope = _votes(_series(-0.0006, seed=seed))
        stacks.append(stack)
        slopes.append(slope)
    assert -1 in stacks, "ema_stack ни разу не ушёл в минус на падающих рядах"
    assert -1 in slopes, "ema_slope ни разу не ушёл в минус на падающих рядах"
    # На падении минус обязан ПРЕОБЛАДАТЬ, а не быть редкой случайностью.
    assert stacks.count(-1) > stacks.count(1)
    assert slopes.count(-1) > slopes.count(1)


def test_ema_stack_abstains_when_averages_interleave() -> None:
    """Ветка воздержания ema_stack существует И достигается.

    Достаточно, чтобы средние перестали быть строго упорядоченными: тогда обе
    ветки со знаком ложны и голос обязан вернуть ноль.
    """
    found = [s for seed in range(60)
             if (s := _votes(_series(0.0, seed=seed))[0]) == 0]
    assert found, "ноль ema_stack не встретился ни разу — ветка недостижима"


def test_ema_slope_abstention_requires_exact_equality() -> None:
    """У ema_slope ветка воздержания есть, но практически недостижима.

    Ноль требует точного равенства двух чисел с плавающей точкой, поэтому на
    реальном ряде голос всегда ±1. Это зафиксировано осознанно: голос по
    построению направленный, «воздержания» у наклона нет — см. отчёт.
    """
    flat = np.full(_WINDOW, 100.0)
    assert _votes(flat)[1] == 0, "на строго постоянном ряде наклон обязан быть нулём"
    assert all(_votes(_series(0.0, seed=seed))[1] != 0 for seed in range(20))


# --- 8.6-2. Симметрия ---

@pytest.mark.parametrize("drift", [0.0006, 0.0, -0.0006])
def test_ema_votes_symmetry(drift: float) -> None:
    """Ряд и его зеркало дают противоположные по знаку голоса.

    Зеркало строится в логарифме цены: цена не может стать отрицательной, а
    зеркалить нужно именно доходности. Оба голоса сравнивают средние одного и
    того же ряда, поэтому обязаны просто сменить знак.
    """
    close = _series(drift)
    mirrored = 100.0 * np.exp(-np.log(close / 100.0))
    stack, slope = _votes(close)
    m_stack, m_slope = _votes(mirrored)
    assert m_stack == -stack
    assert m_slope == -slope


def test_analyze_ohlcv_symmetry_of_signal() -> None:
    """Тот же ряд через штатную analyze_ohlcv: зеркало меняет вывод на обратный."""
    def frame(close: np.ndarray) -> pd.DataFrame:
        rng = np.random.default_rng(11)
        noise = np.abs(rng.normal(0, 0.002, len(close)))
        return pd.DataFrame({
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close * (1 + noise), "low": close * (1 - noise),
            "close": close, "volume": np.full(len(close), 1000.0),
        })

    close = _series(0.0006)
    mirrored = 100.0 * np.exp(-np.log(close / 100.0))
    up_signal, _, up_metrics, _ = analyze_ohlcv(frame(close), 200)
    down_signal, _, down_metrics, _ = analyze_ohlcv(frame(mirrored), 200)
    assert up_signal == "bullish" and down_signal == "bearish"
    assert down_metrics["votes"]["ema_stack"] == -up_metrics["votes"]["ema_stack"]
    assert down_metrics["votes"]["ema_slope"] == -up_metrics["votes"]["ema_slope"]


# --- 8.6-3. Надзор за молчанием (§6) ---

def test_agent_silence_reports_nothing_when_all_agents_speak(monkeypatch) -> None:
    monkeypatch.setattr(daily_report, "_psql",
                        lambda *a, **k: "market|BTC|0|1440|0\nfutures|BTC|12|1440|1")
    lines = daily_report.section_agent_silence()
    assert any("Все агенты говорят" in line for line in lines)


def test_agent_silence_flags_high_share(monkeypatch) -> None:
    """Доля выше порога попадает в сводку замечанием."""
    monkeypatch.setattr(daily_report, "_psql",
                        lambda *a, **k: "futures|DOGE|1200|1440|5")
    lines = daily_report.section_agent_silence()
    assert any("молчит" in line and "83%" in line for line in lines)


def test_agent_silence_alerts_after_a_full_day(monkeypatch) -> None:
    """Сутки подряд без единого содержательного вывода — отдельное сообщение.

    Ровно этот случай прошёл незамеченным в замере 8.5: агент деривативов
    молчал на четырёх токенах из пяти при нуле записей в agent_failures.
    """
    monkeypatch.setattr(daily_report, "_psql",
                        lambda *a, **k: "futures|XRP|1440|1440|36")
    lines = daily_report.section_agent_silence()
    assert any("36 ч подряд" in line for line in lines)
    assert any(line.startswith("🔴") for line in lines)


def test_agent_silence_is_wired_into_the_message() -> None:
    """Секция обязана попасть в сообщение, иначе надзора нет."""
    import inspect

    assert "section_agent_silence()" in inspect.getsource(daily_report.build_message)
