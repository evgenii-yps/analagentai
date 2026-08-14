"""Тесты логики уведомлений: should_notify и форматирование сообщения."""

from datetime import UTC, datetime, timedelta

from src.notify.agent import (
    NotifyConfig,
    SignalFormatConfig,
    should_notify,
)

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


_FMT = SignalFormatConfig(symbol="BTC/USDT", tz_name="Europe/Moscow", primary_horizon="4h")


def _payload(*agents: tuple[str, str, float]) -> list[dict]:
    """Собирает agents_payload из троек (agent, signal, confidence)."""
    return [
        {"agent": a, "signal": s, "confidence": c, "ts": _NOW.isoformat()}
        for a, s, c in agents
    ]


def _sig_full(decision: str, probability: float, payload: list[dict]) -> dict:
    sig = _sig(decision, probability)
    sig["agents_payload"] = payload
    return sig


def test_format_message_all_three_agents() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(
        ("market", "bullish", 0.70),
        ("liquidity", "neutral", 0.05),
        ("futures", "bullish", 0.60),
    )
    text = format_signal_message(_sig_full("buy", 0.78, payload), 64210.0, _FMT)
    assert "ПОКУПАТЬ BTC" in text
    assert "🟢" in text
    assert "78%" in text
    assert "Цена сейчас: 64 210 USDT" in text
    assert "Теханализ: за рост" in text
    assert "Ликвидность: нейтрально" in text
    assert "Деривативы: за рост" in text
    assert "Горизонт оценки: 4 часа" in text
    assert "Решение за вами. Система не торгует сама." in text
    # Все три агента присутствуют → строки «нет данных» быть не должно.
    assert "нет данных" not in text


def test_format_message_missing_agent_is_explicit() -> None:
    from src.notify.agent import format_signal_message

    # Только два агента из трёх — отсутствующий должен быть виден ЯВНО.
    payload = _payload(("market", "bullish", 0.70), ("liquidity", "neutral", 0.05))
    text = format_signal_message(_sig_full("buy", 0.6, payload), 64000.0, _FMT)
    assert "Деривативы: нет данных, в решении не участвовал" in text


def test_format_message_no_price_line_skipped() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bullish", 0.70), ("futures", "bullish", 0.60))
    text = format_signal_message(_sig_full("buy", 0.6, payload), None, _FMT)
    assert "Цена сейчас" not in text
    # Сообщение всё равно формируется целиком.
    assert "ПОКУПАТЬ BTC" in text
    assert "Решение за вами. Система не торгует сама." in text


def test_format_message_sell() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bearish", 0.80), ("futures", "bearish", 0.70))
    text = format_signal_message(_sig_full("sell", 0.9, payload), 64000.0, _FMT)
    assert "ПРОДАВАТЬ BTC" in text
    assert "🔴" in text
    assert "за падение" in text


def test_format_message_agreement_unanimous() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "bullish", 0.6),
        ("futures", "bullish", 0.5),
    )
    text = format_signal_message(_sig_full("buy", 0.8, payload), 64000.0, _FMT)
    assert "1.00 — агенты единодушны" in text


def test_format_message_agreement_mostly_agree() -> None:
    from src.notify.agent import format_signal_message

    # 2 bullish + 1 neutral → agreement = |2-0|/3 ≈ 0.67 → «скорее согласны».
    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "neutral", 0.1),
        ("futures", "bullish", 0.5),
    )
    text = format_signal_message(_sig_full("buy", 0.8, payload), 64000.0, _FMT)
    assert "0.67 — агенты скорее согласны" in text


def test_format_message_agreement_disagree() -> None:
    from src.notify.agent import format_signal_message

    # 1 bullish + 1 bearish → agreement = |1-1|/2 = 0.0 → «мнения расходятся».
    payload = _payload(("market", "bullish", 0.7), ("futures", "bearish", 0.6))
    text = format_signal_message(_sig_full("buy", 0.8, payload), 64000.0, _FMT)
    assert "0.00 — мнения расходятся" in text


def test_format_message_time_in_moscow() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bullish", 0.7), ("futures", "bullish", 0.6))
    text = format_signal_message(_sig_full("buy", 0.8, payload), 64000.0, _FMT)
    # _NOW = 12:00 UTC → 15:00 МСК (UTC+3).
    assert "15:00 МСК" in text
    assert "#1" in text


# --- Задача A2 (Этап 7.2): подсчёт содержательных агентов для порога отправки ---

def test_count_meaningful_agents_counts_directional_and_neutral() -> None:
    from src.notify.agent import count_meaningful_agents

    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "neutral", 0.1),
        ("futures", "bullish", 0.5),
    )
    assert count_meaningful_agents(payload) == 3


def test_count_meaningful_agents_excludes_insufficient() -> None:
    from src.notify.agent import count_meaningful_agents

    # insufficient_data содержательным НЕ считается (ТЗ A2).
    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "insufficient_data", 0.0),
    )
    assert count_meaningful_agents(payload) == 1


def test_count_meaningful_agents_empty() -> None:
    from src.notify.agent import count_meaningful_agents

    assert count_meaningful_agents([]) == 0
    assert count_meaningful_agents(None) == 0


# --- Задача B1 (Этап 7.2): согласованность в тексте — знаменатель = 3 агента ---

def test_compute_agreement_uses_total_agents_denominator() -> None:
    from src.notify.agent import compute_agreement

    # Два агента из трёх, оба bullish. Было |2-0|/2 = 1.0; стало |2-0|/3 ≈ 0.667 —
    # выпадение агента понижает согласованность (та же формула, что у Decision).
    payload = _payload(("market", "bullish", 0.7), ("futures", "bullish", 0.6))
    assert abs(compute_agreement(payload) - 2 / 3) < 1e-9
