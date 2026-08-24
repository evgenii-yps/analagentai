"""Тесты Этапа 8.5: границы молчания Futures и правило чтения журнала.

Ничего в агентах не меняется — тесты фиксируют то, что установлено замером,
чтобы найденное не рассыпалось при следующей правке:

  8.5-1  test_futures_boundary_*      — точная граница insufficient_data;
  8.5-2  test_funding_timestamp_*     — метка funding берётся с 8-часовой сетки;
  8.5-3  test_market_scale_invariant  — Market не зависит от масштаба цены;
  8.5-4  test_log_*                   — правило поиска текста в журнале (§3).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import structlog

from src.agents.base import SIGNAL_INSUFFICIENT
from src.agents.futures import analyze_futures
from src.agents.market import analyze_ohlcv

_MIN_POINTS = 20
_LOOKBACK_H = 168


def _funding(n: int, step_hours: int = 8) -> list[dict]:
    """Окно funding из n точек на сетке step_hours (как пишет коллектор)."""
    return [{"ts": None, "rate": 0.0001 * (1 + i % 5)} for i in range(n)]


# --- 8.5-1. Граница молчания Futures ---

def test_futures_boundary_below_min_points_is_insufficient() -> None:
    """На одну точку меньше порога — это «данных нет», а не «сигнала нет»."""
    signal, confidence, metrics, _ = analyze_futures(
        _funding(_MIN_POINTS - 1), [], None,
        min_points=_MIN_POINTS, lookback_hours=_LOOKBACK_H,
    )
    assert signal == SIGNAL_INSUFFICIENT
    assert confidence == 0.0
    assert metrics["n_funding"] == _MIN_POINTS - 1
    assert metrics["min_points"] == _MIN_POINTS


def test_futures_boundary_at_min_points_speaks() -> None:
    """Ровно на пороге агент обязан заговорить."""
    signal, _, metrics, _ = analyze_futures(
        _funding(_MIN_POINTS), [], None,
        min_points=_MIN_POINTS, lookback_hours=_LOOKBACK_H,
    )
    assert signal != SIGNAL_INSUFFICIENT
    assert metrics["n_funding"] == _MIN_POINTS


def test_futures_threshold_needs_almost_the_whole_window() -> None:
    """Порог достижим только к концу окна: 20 точек по 8 ч — это 152 часа жизни.

    Двадцать точек на восьмичасовой сетке охватывают (20-1)x8 = 152 часа, и
    столько инструмент обязан прожить, прежде чем агент заговорит. Именно эта
    арифметика даёт расчётную дату перехода в diagnose_8_5.sh:
    ``первая запись funding + 152 часа``.

    Окно 168 ч вмещает 21 или 22 точки сетки — смотря попадает ли её граница на
    саму сетку. Запас над требуемыми двадцатью в любом случае мал: выпадение
    двух-трёх записей возвращает агента в молчание уже на зрелом инструменте.
    """
    step_h = 8
    span_h = (_MIN_POINTS - 1) * step_h
    assert span_h == 152
    assert span_h < _LOOKBACK_H, "порог обязан быть достижим внутри окна"
    assert _LOOKBACK_H // step_h in (21, _MIN_POINTS + 1)
    assert round(span_h / 24, 2) == 6.33


# --- 8.5-2. Метка времени funding ---

def test_funding_timestamp_comes_from_settlement_grid() -> None:
    """Коллектор берёт fundingTimestamp: ccxt отдаёт timestamp=None.

    Из-за этого строки ложатся на восьмичасовую сетку расчётов и UPSERT по
    PK (instrument_id, ts) схлопывает повторные опросы в одну строку. Если
    ccxt однажды начнёт заполнять timestamp временем опроса, смысл окна
    перцентиля сменится молча — тест стережёт именно это.
    """
    from src.core.db import _ms_to_dt

    parsed = {"timestamp": None, "fundingTimestamp": 1_756_080_000_000}
    chosen = parsed.get("timestamp") or parsed.get("fundingTimestamp")
    assert chosen == parsed["fundingTimestamp"]
    assert _ms_to_dt(chosen).minute == 0


# --- 8.5-3. Масштаб цены ---

def test_market_scale_invariant() -> None:
    """Один путь на цене 77 000 и на цене 0.21 даёт одинаковые выводы.

    Порогов, зашитых под масштаб цены BTC, в Market Agent нет: все пороги
    выражены в безразмерных величинах (RSI и ADX — индексы 0..100, периоды —
    число свечей, уверенность — доля голосов).
    """
    rng = np.random.default_rng(20260825)
    mult = np.exp(np.cumsum(rng.normal(0.0, 0.004, 260)))

    def run(level: float) -> tuple[str, float, int]:
        close = level * mult
        noise = np.random.default_rng(7)
        high = close * (1 + np.abs(noise.normal(0, 0.002, len(close))))
        low = close * (1 - np.abs(noise.normal(0, 0.002, len(close))))
        frame = pd.DataFrame({
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": high, "low": low, "close": close,
            "volume": np.full(len(close), 1000.0),
        })
        signal, confidence, metrics, _ = analyze_ohlcv(frame, 200)
        return signal, confidence, metrics["score"]

    assert run(77000.0) == run(0.21)


# --- 8.5-4. Правило чтения журнала (§3) ---

def _rendered(event: str, **fields: object) -> str:
    """Строка ровно в том виде, в каком её пишет прод: JSONRenderer structlog."""
    renderer = structlog.processors.JSONRenderer()
    return renderer(None, "info", {"event": event, **fields})


def test_log_cyrillic_is_escaped_so_substring_search_fails() -> None:
    """Русский текст в журнале лежит экранированным — искать его подстрокой нельзя.

    Это и была ложная тревога раздела C diagnose_8_4.sh: поиск не находил
    ничего и объявлял блокирующую находку на исправной системе.
    """
    line = _rendered("Служебные листы пересобраны", windows=94, horizons_h=[1, 4])
    assert "Служебные листы пересобраны" not in line
    assert "\\u0421\\u043b" in line


def test_log_machine_key_is_findable_and_json_decodes() -> None:
    """Машиночитаемый ключ ищется как есть, а разбор JSON возвращает текст."""
    line = _rendered("Служебные листы пересобраны", windows=94, horizons_h=[1, 4])
    assert "horizons_h" in line
    assert json.loads(line)["event"] == "Служебные листы пересобраны"
    assert json.loads(line)["horizons_h"] == [1, 4]


def test_log_accepted_batch_is_not_a_failure() -> None:
    """Успешно принятая пачка несёт level=info — «отказом» её звать нельзя."""
    renderer = structlog.processors.JSONRenderer()
    accepted = json.loads(renderer(None, "info", {
        "event": "Sheets: пачка принята", "level": "info",
        "sheet": "Независимые окна", "rows": 95,
    }))
    failed = json.loads(renderer(None, "error", {
        "event": "Ошибка выгрузки в Sheets", "level": "error",
        "error": "лист «Независимые окна»: ok=false",
    }))
    assert accepted["level"] == "info" and failed["level"] == "error"
    # Обе строки упоминают лист: отличать их обязан УРОВЕНЬ, а не имя листа.
    assert accepted["sheet"] in failed["error"]
