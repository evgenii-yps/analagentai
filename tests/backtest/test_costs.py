"""§13.4 ТЗ: издержки и знак направления.

Проверяется на трёх ручных примерах: net = gross − комиссия − проскальзывание,
знак для ``sell`` инвертирован корректно.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.evaluate import Costs, gross_pnl_pct, is_independent, net_pnl_pct

# КРУГОВАЯ комиссия = ВХОД + ВЫХОД: тейкер спота OKX (Lv1) берёт 0.10% за одну
# сделку, значит 0.20% за круг. До Этапа 8.7 здесь стояло 0.10 — половина
# издержек, поэтому все ожидаемые числа ниже пересчитаны.
COSTS = Costs(fee_roundtrip_pct=Decimal("0.20"), slippage_pct=Decimal("0.01"))


def test_costs_total() -> None:
    assert COSTS.total == pytest.approx(0.21)


def test_manual_example_buy_up() -> None:
    """Пример 1: buy, цена 100 → 102. Валовая +2%, чистая +1.79%."""
    gross = gross_pnl_pct("buy", 100.0, 102.0)
    assert gross == pytest.approx(2.0)
    assert net_pnl_pct(gross, COSTS) == pytest.approx(1.79)


def test_manual_example_sell_down() -> None:
    """Пример 2: sell, цена 100 → 98. Падение — это попадание: валовая +2%."""
    gross = gross_pnl_pct("sell", 100.0, 98.0)
    assert gross == pytest.approx(2.0)
    assert net_pnl_pct(gross, COSTS) == pytest.approx(1.79)


def test_manual_example_sell_up() -> None:
    """Пример 3: sell, цена 100 → 101. Рост против сигнала: валовая −1%."""
    gross = gross_pnl_pct("sell", 100.0, 101.0)
    assert gross == pytest.approx(-1.0)
    assert net_pnl_pct(gross, COSTS) == pytest.approx(-1.21)


def test_costs_are_charged_regardless_of_outcome() -> None:
    """Издержки вычитаются и у прибыльных, и у убыточных наблюдений."""
    profit = net_pnl_pct(gross_pnl_pct("buy", 100.0, 105.0), COSTS)
    loss = net_pnl_pct(gross_pnl_pct("buy", 100.0, 95.0), COSTS)
    assert profit == pytest.approx(4.79)
    assert loss == pytest.approx(-5.21)


def test_small_move_is_eaten_by_costs() -> None:
    """Движение меньше издержек даёт отрицательную чистую доходность.

    Это ровно та ситуация, о которой предупреждает §9.3 ТЗ: медианное
    4-часовое движение в Этапе 7.1 (0.03%) меньше издержек (0.21%).
    """
    gross = gross_pnl_pct("buy", 100.0, 100.03)
    assert gross > 0
    assert net_pnl_pct(gross, COSTS) < 0


def test_zero_price_is_rejected() -> None:
    with pytest.raises(ValueError):
        gross_pnl_pct("buy", 0.0, 100.0)


def test_independence_rule() -> None:
    """Наблюдение независимо, если час метки кратен горизонту (§9.5 ТЗ)."""
    assert is_independent(0, 4) is True
    assert is_independent(4, 4) is True
    assert is_independent(5, 4) is False
    assert is_independent(12, 12) is True
    assert is_independent(13, 24) is False
