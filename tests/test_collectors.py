"""Тесты чистых функций слоя данных: метрики стакана и дедупликация сделок."""

from src.core.db import compute_orderbook_metrics, dedupe_trades


def test_compute_orderbook_metrics() -> None:
    """Spread = best_ask - best_bid; объёмы суммируются по сторонам."""
    bids = [[100.0, 1.0], [99.0, 2.0]]
    asks = [[101.0, 3.0], [102.0, 4.0]]
    spread, bid_volume, ask_volume = compute_orderbook_metrics(bids, asks)

    assert spread == 1.0          # 101 - 100
    assert bid_volume == 3.0      # 1 + 2
    assert ask_volume == 7.0      # 3 + 4


def test_compute_orderbook_metrics_empty_side() -> None:
    """Если одна из сторон пуста — spread не определён (None)."""
    spread, bid_volume, ask_volume = compute_orderbook_metrics([], [[101.0, 3.0]])

    assert spread is None
    assert bid_volume == 0.0
    assert ask_volume == 3.0


def test_dedupe_trades_removes_duplicates() -> None:
    """Дубли по id удаляются, порядок первых вхождений сохраняется."""
    trades = [
        {"id": "1", "price": 10.0},
        {"id": "2", "price": 11.0},
        {"id": "1", "price": 10.5},  # дубль id=1
        {"id": 3, "price": 12.0},    # числовой id приводится к строке
    ]
    result = dedupe_trades(trades)

    assert [t["id"] for t in result] == ["1", "2", 3]


def test_dedupe_trades_skips_missing_id() -> None:
    """Сделки без id отбрасываются (нечего дедуплицировать)."""
    trades = [{"price": 10.0}, {"id": "1", "price": 11.0}]
    result = dedupe_trades(trades)

    assert len(result) == 1
    assert result[0]["id"] == "1"
