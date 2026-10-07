"""Этап 9.7, §3: единый русский формат чисел (``src.core.fmt``)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core import fmt

D = Decimal
MINUS = "−"


def test_every_function_turns_none_into_a_dash() -> None:
    for func in (fmt.money, fmt.money_signed, fmt.pct, fmt.share, fmt.price, fmt.qty,
                 fmt.duration, fmt.num):
        assert func(None) == "—", func.__name__
    assert fmt.local_dt(None, "Europe/Moscow") == "—"
    assert fmt.sign_emoji(None) == "⚪"


def test_money() -> None:
    assert fmt.money(1000) == "$1 000,00"
    assert fmt.money(0) == "$0,00"
    assert fmt.money(D("1234567.891")) == "$1 234 567,89"
    assert fmt.money(-3) == f"{MINUS}$3,00"
    assert fmt.money(0.0421, 4) == "$0,0421"
    assert fmt.money(D("0.125")) == "$0,13"                  # половина вверх, не «банковское»
    assert fmt.money(1_000_000_000) == "$1 000 000 000,00"


def test_money_signed_uses_a_real_minus_and_no_sign_on_zero() -> None:
    assert fmt.money_signed(0.12) == "+$0,12"
    assert fmt.money_signed(-0.4) == f"{MINUS}$0,40"
    assert "-" not in fmt.money_signed(-0.4)                 # не дефис
    assert fmt.money_signed(0) == "$0,00"
    assert fmt.money_signed(-0.0001) == "$0,00"              # «−$0,00» человеку ничего не говорит
    assert fmt.money_signed(D("0.0240"), 4) == "+$0,0240"


def test_pct() -> None:
    assert fmt.pct(0.16) == "+0,16%"
    assert fmt.pct(-0.19) == f"{MINUS}0,19%"
    assert fmt.pct(0) == "0,00%"
    assert fmt.pct(1.2, sign=False) == "1,20%"
    assert fmt.pct(-1.2, sign=False) == f"{MINUS}1,20%"
    assert fmt.pct(D("0.0100"), 4) == "+0,0100%"
    assert fmt.pct(1234.5) == "+1 234,50%"


def test_share() -> None:
    assert fmt.share(0.62) == "62%"
    assert fmt.share(0) == "0%"
    assert fmt.share(1) == "100%"
    assert fmt.share(0.625) == "63%"


def test_price_digits_follow_the_magnitude() -> None:
    assert fmt.price(85614.3) == "85 614,30"
    assert fmt.price(120.5) == "120,50"
    assert fmt.price(2.5) == "2,5000"
    assert fmt.price(0.21437) == "0,214370"
    assert fmt.price(1234567) == "1 234 567,00"
    assert fmt.price(100) == "100,00" and fmt.price(99.99) == "99,9900"


def test_qty_has_no_trailing_zeros_and_up_to_eight_places() -> None:
    assert fmt.qty(0.0166) == "0,0166"
    assert fmt.qty(D("0.00003332")) == "0,00003332"
    assert fmt.qty(1234.5) == "1 234,5"
    assert fmt.qty(2) == "2" and fmt.qty(0) == "0"
    assert fmt.qty(D("0.123456789")) == "0,12345679"


@pytest.mark.parametrize("sec,expected", [
    (59, "меньше минуты"), (60, "1 мин"), (12 * 60, "12 мин"), (3600, "1 ч"),
    (61 * 60, "1 ч 1 мин"), (5 * 3600 + 12 * 60, "5 ч 12 мин"),
    (25 * 3600, "1 д 1 ч"), (49 * 3600, "2 д 1 ч"), (48 * 3600, "2 д"),
    (27 * 3600, "1 д 3 ч"), (0, "меньше минуты"), (-5, "меньше минуты"),
])
def test_duration(sec, expected) -> None:
    assert fmt.duration(sec) == expected


def test_local_dt_and_the_zone_label() -> None:
    ts = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)
    assert fmt.local_dt(ts, "Europe/Moscow") == "07.10 14:05 МСК"
    assert fmt.local_dt(ts, ZoneInfo("Europe/Moscow")) == "07.10 14:05 МСК"
    assert fmt.local_dt(ts, ZoneInfo("UTC")) == "07.10 11:05 UTC"


def test_sign_emoji() -> None:
    assert fmt.sign_emoji(0.01) == "🟢" and fmt.sign_emoji(D("-0.01")) == "🔴"
    assert fmt.sign_emoji(0) == "⚪" and fmt.sign_emoji(None) == "⚪"


def test_num() -> None:
    assert fmt.num(0.7234) == "0,72"
    assert fmt.num(0.8, 2) == "0,80"
    assert fmt.num(-1.5) == f"{MINUS}1,50"


def test_garbage_input_does_not_raise() -> None:
    for func in (fmt.money, fmt.pct, fmt.price, fmt.qty, fmt.duration, fmt.share):
        assert func("не число") == "—"
        assert func(float("nan")) == "—"
