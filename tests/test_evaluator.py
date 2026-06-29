"""Тесты оценщика: расчёт pnl/drawdown/success для buy и sell, парсинг горизонтов."""

import pytest

from src.evaluator.evaluator import compute_evaluation, horizon_to_seconds


def _candles(rows: list[tuple[float, float, float]]) -> list[dict]:
    """Строит свечи из кортежей (high, low, close)."""
    return [{"high": h, "low": low, "close": c} for h, low, c in rows]


# --- horizon_to_seconds ---

def test_horizon_to_seconds() -> None:
    assert horizon_to_seconds("1h") == 3600
    assert horizon_to_seconds("4h") == 14400
    assert horizon_to_seconds("30m") == 1800
    assert horizon_to_seconds("90s") == 90


def test_horizon_to_seconds_invalid() -> None:
    with pytest.raises(ValueError):
        horizon_to_seconds("1d")


# --- buy ---

def test_buy_winning() -> None:
    # Цена сигнала 100, к концу окна 110, минимум опускался до 95.
    candles = _candles([(105, 98, 104), (112, 95, 110)])
    r = compute_evaluation("buy", 100.0, candles)
    assert r["price_at_close"] == 110.0
    assert r["pnl_pct"] == pytest.approx(10.0)
    assert r["drawdown_pct"] == pytest.approx(5.0)   # (100-95)/100
    assert r["success"] is True


def test_buy_losing() -> None:
    candles = _candles([(101, 92, 95), (96, 88, 90)])
    r = compute_evaluation("buy", 100.0, candles)
    assert r["pnl_pct"] == pytest.approx(-10.0)
    assert r["drawdown_pct"] == pytest.approx(12.0)  # (100-88)/100
    assert r["success"] is False


def test_buy_drawdown_clamped_to_zero() -> None:
    # Цена ни разу не опускалась ниже сигнала → просадка 0.
    candles = _candles([(112, 101, 110)])
    r = compute_evaluation("buy", 100.0, candles)
    assert r["drawdown_pct"] == 0.0


# --- sell ---

def test_sell_winning() -> None:
    # Для sell успех = падение цены. Сигнал 100, к концу 90 → pnl +10.
    candles = _candles([(103, 95, 96), (98, 88, 90)])
    r = compute_evaluation("sell", 100.0, candles)
    assert r["pnl_pct"] == pytest.approx(10.0)
    assert r["drawdown_pct"] == pytest.approx(3.0)   # (103-100)/100 ход вверх против sell
    assert r["success"] is True


def test_sell_losing() -> None:
    candles = _candles([(108, 100, 105), (112, 104, 110)])
    r = compute_evaluation("sell", 100.0, candles)
    assert r["pnl_pct"] == pytest.approx(-10.0)
    assert r["drawdown_pct"] == pytest.approx(12.0)  # (112-100)/100
    assert r["success"] is False


# --- прочее ---

def test_empty_window_raises() -> None:
    with pytest.raises(ValueError):
        compute_evaluation("buy", 100.0, [])


def test_is_deterministic() -> None:
    candles = _candles([(105, 98, 104), (112, 95, 110)])
    assert compute_evaluation("buy", 100.0, candles) == compute_evaluation(
        "buy", 100.0, candles
    )
