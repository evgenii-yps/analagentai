"""Тесты агрегации Decision Agent на фиксированных наборах выводов.

Проверяют детерминированность и сценарии из ТЗ: единодушие → buy/sell,
конфликт/слабые → wait, меньше MIN_AGENTS → wait, устаревшие игнорируются.
"""

from datetime import UTC, datetime, timedelta

from src.decision.agent import make_decision

_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
_WEIGHTS = {"market": 1.0, "liquidity": 1.0, "futures": 1.0}


def _out(agent: str, signal: str, confidence: float, age_sec: float = 0.0) -> dict:
    """Строит вывод агента с заданным возрастом (секунд назад от _NOW)."""
    return {
        "agent": agent,
        "signal": signal,
        "confidence": confidence,
        "ts": _NOW - timedelta(seconds=age_sec),
    }


def _decide(outputs, *, threshold=0.3, min_agents=2, freshness_sec=300):
    return make_decision(
        outputs,
        weights=_WEIGHTS,
        threshold=threshold,
        min_agents=min_agents,
        freshness_sec=freshness_sec,
        now=_NOW,
    )


def test_all_bullish_is_buy() -> None:
    outputs = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bullish", 0.6),
        _out("futures", "bullish", 0.7),
    ]
    decision, probability, payload, _ = _decide(outputs)
    assert decision == "buy"
    assert probability > 0.0
    assert len(payload) == 3


def test_all_bearish_is_sell() -> None:
    outputs = [
        _out("market", "bearish", 0.8),
        _out("liquidity", "bearish", 0.6),
        _out("futures", "bearish", 0.7),
    ]
    decision, _, _, _ = _decide(outputs)
    assert decision == "sell"


def test_conflict_is_wait() -> None:
    # Сильный bull против сильного bear с равными весами → балл ≈ 0.
    outputs = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bearish", 0.8),
    ]
    decision, _, _, _ = _decide(outputs)
    assert decision == "wait"


def test_weak_signals_is_wait() -> None:
    # Один слабый bull + один neutral → балл ниже порога.
    outputs = [
        _out("market", "bullish", 0.2),
        _out("liquidity", "neutral", 0.5),
    ]
    decision, _, _, _ = _decide(outputs)
    assert decision == "wait"


def test_fewer_than_min_agents_is_wait() -> None:
    outputs = [_out("market", "bullish", 0.9)]  # только один свежий
    decision, probability, payload, _ = _decide(outputs)
    assert decision == "wait"
    assert probability == 0.0
    assert len(payload) == 1


def test_stale_outputs_ignored() -> None:
    # Два bullish, но один устаревший → остаётся один свежий → wait.
    outputs = [
        _out("market", "bullish", 0.9),
        _out("liquidity", "bullish", 0.9, age_sec=10_000),
    ]
    decision, _, payload, _ = _decide(outputs)
    assert decision == "wait"
    assert len(payload) == 1
    assert payload[0]["agent"] == "market"


def test_insufficient_data_ignored() -> None:
    outputs = [
        _out("market", "bullish", 0.9),
        _out("liquidity", "insufficient_data", 0.0),
    ]
    decision, _, payload, _ = _decide(outputs)
    assert decision == "wait"  # остаётся один валидный → меньше MIN_AGENTS
    assert len(payload) == 1


def test_none_outputs_ignored() -> None:
    outputs = [None, _out("market", "bullish", 0.9), None]
    decision, _, payload, _ = _decide(outputs)
    assert decision == "wait"
    assert len(payload) == 1


def test_payload_reflects_used_outputs() -> None:
    outputs = [
        _out("market", "bullish", 0.8),
        _out("futures", "bullish", 0.6),
    ]
    _, _, payload, _ = _decide(outputs)
    agents = {p["agent"] for p in payload}
    assert agents == {"market", "futures"}
    assert all({"agent", "signal", "confidence", "ts"} <= p.keys() for p in payload)


def test_unanimous_has_higher_probability_than_mixed() -> None:
    unanimous = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bullish", 0.8),
        _out("futures", "bullish", 0.8),
    ]
    mixed = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bullish", 0.8),
        _out("futures", "neutral", 0.8),
    ]
    _, prob_unanimous, _, _ = _decide(unanimous)
    _, prob_mixed, _, _ = _decide(mixed)
    assert prob_unanimous > prob_mixed


def test_is_deterministic() -> None:
    outputs = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bearish", 0.3),
        _out("futures", "bullish", 0.5),
    ]
    assert _decide(outputs) == _decide(outputs)


# --- Задача B1 (Этап 7.2): знаменатель согласованности = полное число агентов ---

def test_agreement_denominator_is_total_agents_signal_8205() -> None:
    # Воспроизводим сигнал #8205: Market отсутствует, Liquidity нейтрально (0.05),
    # Futures за рост (1.00). Решение принимают 2 агента из 3.
    outputs = [
        _out("liquidity", "neutral", 0.05),
        _out("futures", "bullish", 1.00),
    ]
    decision, probability, payload, _ = _decide(outputs)  # total_agents по умолч. = 3
    assert decision == "buy"
    assert len(payload) == 2
    # Было (знаменатель len(fresh)=2): agreement 0.50, probability ≈ 0.72 (уходило
    # пользователю). Стало (знаменатель 3): agreement 0.33, probability ≈ 0.64 —
    # ниже порога уведомления 0.7.
    assert abs(probability - 0.6349) < 0.01
    assert probability < 0.7


def test_dropping_agent_lowers_agreement_and_probability() -> None:
    # Тот же расклад, но сравниваем новый знаменатель (3) со старым (len(fresh)=2).
    outputs = [
        _out("liquidity", "neutral", 0.05),
        _out("futures", "bullish", 1.00),
    ]
    common = dict(
        weights=_WEIGHTS, threshold=0.3, min_agents=2, freshness_sec=300, now=_NOW
    )
    _, prob_new, _, _ = make_decision(outputs, total_agents=3, **common)
    _, prob_old, _, _ = make_decision(outputs, total_agents=2, **common)
    # Выпадение агента ПОНИЖАЕТ согласованность → новая вероятность строго ниже.
    assert prob_new < prob_old


def test_full_house_unanimous_agreement_is_one() -> None:
    # Полный состав, единодушны → agreement = |3-0|/3 = 1.0 (не меняется).
    outputs = [
        _out("market", "bullish", 0.8),
        _out("liquidity", "bullish", 0.8),
        _out("futures", "bullish", 0.8),
    ]
    _, probability, _, _ = _decide(outputs)
    assert probability == 1.0
