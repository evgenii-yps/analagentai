"""Этап 9.5, §12 п. 2–5: чистые правила демо-исполнения (без сети и базы)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.config import Settings
from src.demo import rules

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
D = Decimal


def pos(opened_at: datetime = T0, side: str = "buy", **extra) -> dict:
    return {"opened_at": opened_at, "side": side, **extra}


# --- п. 2: should_buy -------------------------------------------------------


def test_should_buy_ok_for_a_fresh_buy_position() -> None:
    assert rules.should_buy(pos(), T0 - timedelta(minutes=5), T0 + timedelta(seconds=20), 180) \
        == (True, None)


def test_should_buy_before_start() -> None:
    since = T0 + timedelta(seconds=1)
    assert rules.should_buy(pos(), since, T0 + timedelta(seconds=5), 180) \
        == (False, "before_start")


def test_should_buy_before_start_boundary_is_strict() -> None:
    # opened_at == mirror_since — ещё годится; на секунду раньше — уже нет.
    assert rules.should_buy(pos(), T0, T0, 180)[0] is True
    assert rules.should_buy(pos(T0 - timedelta(seconds=1)), T0, T0, 180) \
        == (False, "before_start")


def test_should_buy_stale_boundary_is_strict() -> None:
    since = T0 - timedelta(hours=1)
    assert rules.should_buy(pos(), since, T0 + timedelta(seconds=180), 180) == (True, None)
    assert rules.should_buy(pos(), since, T0 + timedelta(seconds=180, microseconds=1), 180) \
        == (False, "stale")
    assert rules.should_buy(pos(), since, T0 + timedelta(seconds=181), 180) \
        == (False, "stale")


def test_should_buy_not_buy() -> None:
    assert rules.should_buy(pos(side="sell"), T0 - timedelta(hours=1), T0, 180) \
        == (False, "not_buy")


def test_should_buy_reason_order_follows_the_spec() -> None:
    # Все три причины сразу: первой называется before_start, затем stale, затем not_buy.
    since = T0 + timedelta(hours=1)
    assert rules.should_buy(pos(side="sell"), since, T0 + timedelta(days=1), 180)[1] \
        == "before_start"
    since = T0 - timedelta(hours=1)
    assert rules.should_buy(pos(side="sell"), since, T0 + timedelta(days=1), 180)[1] == "stale"


# --- п. 3: sell_qty ---------------------------------------------------------


def test_sell_qty_subtracts_the_fee_taken_in_the_base_coin() -> None:
    qty = rules.sell_qty(D("0.00002000"), D("0.00000002"), "BTC", "BTC", D("0.00000001"))
    assert qty == D("0.00001998")


def test_sell_qty_ignores_a_fee_taken_in_usdt() -> None:
    qty = rules.sell_qty(D("0.00002000"), D("0.002"), "USDT", "BTC", D("0.00000001"))
    assert qty == D("0.00002000")


def test_sell_qty_uses_the_absolute_value_of_the_fee() -> None:
    # OKX отдаёт комиссию отрицательной.
    assert rules.sell_qty(D("1"), D("-0.001"), "DOGE", "DOGE", D("0.000001")) == D("0.999")


def test_sell_qty_rounds_down_to_the_lot_step_never_up() -> None:
    assert rules.sell_qty(D("10.19999"), D("0"), "BTC", "BTC", D("0.01")) == D("10.19")
    assert rules.sell_qty(D("10.199999999"), None, None, "BTC", D("0.00001")) == D("10.19999")
    # ровно на шаге — не меняется
    assert rules.sell_qty(D("0.5"), D("0"), "BTC", "BTC", D("0.1")) == D("0.5")


def test_sell_qty_is_zero_when_the_fee_eats_everything() -> None:
    assert rules.sell_qty(D("0.001"), D("0.001"), "BTC", "BTC", D("0.0001")) == D("0")


def test_sell_qty_below_the_exchange_minimum_is_dust() -> None:
    qty = rules.sell_qty(D("0.0000099"), D("0"), "BTC", "BTC", D("0.00000001"))
    assert rules.is_dust(qty, D("0.00001")) is True
    ok = rules.sell_qty(D("0.0000100"), D("0"), "BTC", "BTC", D("0.00000001"))
    assert rules.is_dust(ok, D("0.00001")) is False   # ровно минимум — не dust
    assert rules.is_dust(D("0"), D("0.00001")) is True


def test_sell_qty_returns_decimal_and_rejects_a_bad_step() -> None:
    assert isinstance(rules.sell_qty(D("1"), D("0"), "BTC", "BTC", D("0.1")), Decimal)
    with pytest.raises(ValueError):
        rules.sell_qty(D("1"), D("0"), "BTC", "BTC", D("0"))


# --- п. 4: slippage_pct -----------------------------------------------------


def test_slippage_buy_paid_more_is_positive() -> None:
    assert rules.slippage_pct("buy", D("100"), D("101")) == D("1")


def test_slippage_buy_paid_less_is_negative() -> None:
    assert rules.slippage_pct("buy", D("100"), D("99")) == D("-1")


def test_slippage_sell_received_less_is_positive() -> None:
    assert rules.slippage_pct("sell", D("100"), D("99")) == D("1")


def test_slippage_sell_received_more_is_negative() -> None:
    assert rules.slippage_pct("sell", D("100"), D("101")) == D("-1")


def test_slippage_is_zero_at_the_virtual_price_and_decimal() -> None:
    for leg in ("buy", "sell"):
        value = rules.slippage_pct(leg, D("60000"), D("60000"))
        assert value == 0 and isinstance(value, Decimal)


def test_slippage_rejects_a_bad_leg_and_a_zero_price() -> None:
    with pytest.raises(ValueError):
        rules.slippage_pct("hold", D("1"), D("1"))
    with pytest.raises(ValueError):
        rules.slippage_pct("buy", D("0"), D("1"))


# --- п. 5: order_cost_usd ---------------------------------------------------


def test_order_cost_is_the_notional_usd_of_the_position() -> None:
    assert rules.order_cost_usd({"notional_usd": D("2.0000")}) == D("2")
    assert rules.order_cost_usd({"notional_usd": "2"}) == D("2")
    assert rules.order_cost_usd({"notional_usd": 2.5}) == D("2.5")
    assert isinstance(rules.order_cost_usd({"notional_usd": D("2")}), Decimal)


# --- cl_ord_id, pair_pnl, fee_in_usdt, lag ----------------------------------


def test_cl_ord_id_shape() -> None:
    assert rules.cl_ord_id(123, "buy") == "at95b123"
    assert rules.cl_ord_id(123, "sell") == "at95s123"
    value = rules.cl_ord_id(10**18, "sell")
    assert value.isalnum() and len(value) <= 32
    with pytest.raises(ValueError):
        rules.cl_ord_id(1, "hold")


def test_pair_pnl() -> None:
    profit, pct = rules.pair_pnl(D("2"), D("2.05"), D("0.002"))
    assert profit == D("0.048")
    assert pct == D("2.4")
    loss, loss_pct = rules.pair_pnl(D("2"), D("1.99"), D("0.002"))
    assert loss == D("-0.012") and loss_pct == D("-0.6")
    with pytest.raises(ValueError):
        rules.pair_pnl(D("0"), D("1"), D("0"))


def test_fee_in_usdt() -> None:
    assert rules.fee_in_usdt(D("0.002"), "USDT", D("60000"), "BTC") == D("0.002")
    assert rules.fee_in_usdt(D("0.00000003"), "BTC", D("60000"), "BTC") == D("0.0018")
    assert rules.fee_in_usdt(None, None, D("1"), "BTC") == D("0")
    with pytest.raises(ValueError):
        rules.fee_in_usdt(D("1"), "OKB", D("1"), "BTC")


def test_lag_seconds() -> None:
    assert rules.lag_seconds(T0 + timedelta(seconds=31, milliseconds=400), T0) == 31


# --- настройки (§4.1) -------------------------------------------------------


def _settings(**kw) -> Settings:
    return Settings(POSTGRES_PASSWORD="x", _env_file=None, **kw)


def test_defaults_match_the_spec() -> None:
    s = _settings()
    assert s.DEMO_ENABLED is False
    assert (s.OKX_DEMO_API_KEY, s.OKX_DEMO_SECRET_KEY, s.OKX_DEMO_PASSPHRASE) == ("", "", "")
    assert s.OKX_DEMO_HOST == "www.okx.com"
    assert s.DEMO_INTERVAL == 15 and s.DEMO_MAX_ENTRY_DELAY_SEC == 180
    assert s.DEMO_FILL_WAIT_SEC == 10 and s.DEMO_MIN_USDT_BALANCE == 20.0
    assert s.DEMO_START_CAPITAL_USD == 0.0
    assert s.DEMO_NOTIFY_ENABLED is True and s.DEMO_SHEETS_ENABLED is False
    assert not hasattr(s, "DEMO_REPORT_HOUR_UTC")      # сводка идёт после снимка суток
    assert s.demo_config_errors() == []


def test_enabled_requires_all_three_keys_and_names_the_missing_one() -> None:
    errors = _settings(DEMO_ENABLED=True, OKX_DEMO_API_KEY="a", OKX_DEMO_SECRET_KEY="b",
                       DEMO_START_CAPITAL_USD=100).demo_config_errors()
    assert len(errors) == 1 and "OKX_DEMO_PASSPHRASE" in errors[0]
    ok = _settings(DEMO_ENABLED=True, OKX_DEMO_API_KEY="a", OKX_DEMO_SECRET_KEY="b",
                   OKX_DEMO_PASSPHRASE="c", DEMO_START_CAPITAL_USD=100)
    assert ok.demo_config_errors() == []
    # значения ключей в сообщения не попадают
    bad = _settings(DEMO_ENABLED=True, OKX_DEMO_API_KEY="SECRETVALUE", OKX_DEMO_HOST="x.com")
    assert all("SECRETVALUE" not in e for e in bad.demo_config_errors())


def test_host_outside_the_list_is_an_error_even_when_disabled() -> None:
    assert _settings(OKX_DEMO_HOST="okx.example.com").demo_config_errors()
    assert _settings(OKX_DEMO_HOST="eea.okx.com").demo_config_errors() == []


def test_a_demo_typo_does_not_break_settings_for_other_services() -> None:
    # Опечатка в демо-настройках не должна ронять collector, agents и остальных.
    s = _settings(DEMO_ENABLED=True, OKX_DEMO_HOST="nope")
    assert s.POSTGRES_PASSWORD == "x"


def test_numeric_ranges() -> None:
    assert _settings(DEMO_INTERVAL=0).demo_config_errors()
    assert _settings(DEMO_MIN_USDT_BALANCE=-1).demo_config_errors()


def test_start_capital_must_be_positive_when_enabled() -> None:
    keys = {"OKX_DEMO_API_KEY": "a", "OKX_DEMO_SECRET_KEY": "b", "OKX_DEMO_PASSPHRASE": "c"}
    for bad in (0.0, -5.0):
        errors = _settings(DEMO_ENABLED=True, DEMO_START_CAPITAL_USD=bad,
                           **keys).demo_config_errors()
        assert len(errors) == 1 and "DEMO_START_CAPITAL_USD" in errors[0]
    good = _settings(DEMO_ENABLED=True, DEMO_START_CAPITAL_USD=1000, **keys)
    assert good.demo_config_errors() == []
    # выключенный сервис капитала не требует
    assert _settings(DEMO_ENABLED=False, DEMO_START_CAPITAL_USD=0).demo_config_errors() == []


def test_an_old_env_with_the_removed_report_hour_is_still_accepted() -> None:
    s = Settings(POSTGRES_PASSWORD="x", _env_file=None, DEMO_REPORT_HOUR_UTC=6)
    assert s.demo_config_errors() == []
