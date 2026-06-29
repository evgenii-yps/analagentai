"""Тесты логики уведомлений: should_notify и форматирование сообщения."""

from datetime import UTC, datetime, timedelta

from src.notify.agent import NotifyConfig, format_signal, should_notify

_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
_CFG = NotifyConfig(min_probability=0.7, cooldown_sec=1800)


def _sig(decision: str, probability: float) -> dict:
    return {
        "id": 1,
        "instrument_id": 1,
        "ts": _NOW,
        "decision": decision,
        "probability": probability,
        "rationale": "тест",
    }


def test_wait_is_not_notified() -> None:
    assert should_notify(_sig("wait", 0.9), None, None, _NOW, _CFG) is False


def test_low_probability_is_not_notified() -> None:
    assert should_notify(_sig("buy", 0.5), None, None, _NOW, _CFG) is False


def test_first_strong_signal_is_notified() -> None:
    assert should_notify(_sig("buy", 0.8), None, None, _NOW, _CFG) is True


def test_repeat_same_decision_within_cooldown_is_not_notified() -> None:
    last_sent = _NOW - timedelta(seconds=600)  # 10 мин < 30 мин cooldown
    assert should_notify(_sig("buy", 0.8), "buy", last_sent, _NOW, _CFG) is False


def test_same_decision_after_cooldown_is_notified() -> None:
    last_sent = _NOW - timedelta(seconds=2000)  # > 1800 cooldown
    assert should_notify(_sig("buy", 0.8), "buy", last_sent, _NOW, _CFG) is True


def test_decision_change_is_notified_even_within_cooldown() -> None:
    last_sent = _NOW - timedelta(seconds=60)  # только что отправляли buy
    assert should_notify(_sig("sell", 0.8), "buy", last_sent, _NOW, _CFG) is True


def test_probability_at_threshold_is_notified() -> None:
    assert should_notify(_sig("buy", 0.7), None, None, _NOW, _CFG) is True


def test_format_signal_buy() -> None:
    text = format_signal(_sig("buy", 0.82), "BTC/USDT")
    assert "ПОКУПАТЬ BTC" in text
    assert "🟢" in text
    assert "82%" in text
    assert "тест" in text


def test_format_signal_sell() -> None:
    text = format_signal(_sig("sell", 0.91), "BTC/USDT")
    assert "ПРОДАВАТЬ BTC" in text
    assert "🔴" in text
    assert "91%" in text
