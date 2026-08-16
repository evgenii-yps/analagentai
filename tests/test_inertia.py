"""Этап 7.3, Блок C: учёт инерции входов (inputs_hash / is_repeat).

Частота решений и DECISION_INTERVAL не меняются — вводится честная маркировка:
решение, принятое на том же наборе входных мнений, что и предыдущее, помечается
повтором. На отправку уведомлений это не влияет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.decision.agent import DecisionAgent, compute_inputs_hash

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _payload(
    market: float = 0.3185,
    liquidity: float = 1.0,
    futures: float = 0.4,
    ts: datetime | None = None,
) -> list[dict[str, Any]]:
    stamp = (ts or _NOW).isoformat()
    return [
        {"agent": "market", "signal": "bearish", "confidence": market, "ts": stamp},
        {"agent": "liquidity", "signal": "bullish", "confidence": liquidity, "ts": stamp},
        {"agent": "futures", "signal": "neutral", "confidence": futures, "ts": stamp},
    ]


def test_identical_inputs_give_identical_hash() -> None:
    assert compute_inputs_hash(_payload()) == compute_inputs_hash(_payload())


def test_agent_order_does_not_change_hash() -> None:
    """Разный порядок агентов в payload даёт одинаковый хэш (агенты сортируются)."""
    straight = _payload()
    reversed_order = list(reversed(straight))
    assert compute_inputs_hash(straight) == compute_inputs_hash(reversed_order)


def test_timestamp_does_not_change_hash() -> None:
    """Те же мнения, прочитанные минутой позже, — тот же вход."""
    later = _payload(ts=_NOW + timedelta(minutes=1))
    assert compute_inputs_hash(_payload()) == compute_inputs_hash(later)


def test_fifth_decimal_does_not_change_hash() -> None:
    """Изменение уверенности в ПЯТОМ знаке хэш не меняет (округление до 4)."""
    assert compute_inputs_hash(_payload(market=0.31850)) == compute_inputs_hash(
        _payload(market=0.318504)
    )


def test_fourth_decimal_changes_hash() -> None:
    """Изменение уверенности в ЧЕТВЁРТОМ знаке хэш меняет."""
    assert compute_inputs_hash(_payload(market=0.3185)) != compute_inputs_hash(
        _payload(market=0.3186)
    )


def test_direction_change_changes_hash() -> None:
    changed = _payload()
    changed[0]["signal"] = "bullish"
    assert compute_inputs_hash(_payload()) != compute_inputs_hash(changed)


def test_canonical_string_format_is_stable() -> None:
    """Хэш считается ровно от формата из ТЗ §5.2 — фиксируем его явно."""
    import hashlib

    canonical = (
        "futures:neutral:0.4000|liquidity:bullish:1.0000|market:bearish:0.3185"
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert compute_inputs_hash(_payload()) == expected


def test_empty_payload_hash_is_defined() -> None:
    """Пустой набор мнений тоже имеет хэш — иначе wait-решения выпали бы из учёта."""
    assert len(compute_inputs_hash([])) == 64


# --- Поведение Decision Agent -----------------------------------------------

class _FakeDB:
    """Замена слоя БД: отдаёт заданные выводы агентов, копит сохранённые сигналы."""

    def __init__(
        self,
        outputs: dict[str, dict[str, Any] | None],
        last_hash: str | None = None,
        curve: dict[str, Any] | None = None,
    ) -> None:
        self.outputs = outputs
        self.last_hash = last_hash
        self.curve = curve
        self.saved: list[dict[str, Any]] = []

    async def get_latest_agent_output(
        self, agent: str, instrument_id: int
    ) -> dict[str, Any] | None:
        return self.outputs.get(agent)

    async def get_last_inputs_hash(self, instrument_id: int) -> str | None:
        return self.last_hash

    async def get_active_calibration(self, logic_version: int) -> dict[str, Any] | None:
        return self.curve

    async def save_signal(self, instrument_id: int, decision: str, probability: float,
                          agents_payload: Any, rationale: str, **kwargs: Any) -> None:
        self.saved.append(
            {
                "instrument_id": instrument_id,
                "decision": decision,
                "probability": probability,
                "agents_payload": agents_payload,
                **kwargs,
            }
        )


class _FakeRedis:
    """Redis, который ничего не помнит: проверяем работу без кэша."""

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None


def _agent_output(agent: str, signal: str, confidence: float) -> dict[str, Any]:
    return {
        "agent": agent,
        "signal": signal,
        "confidence": confidence,
        "ts": _NOW,
        "instrument_id": 1,
    }


def _make_agent() -> DecisionAgent:
    return DecisionAgent(
        instrument_id=1,
        agent_instruments={"market": 1, "liquidity": 1, "futures": 2},
        interval=60,
        weights={"market": 1.0, "liquidity": 1.0, "futures": 1.0},
        threshold=0.3,
        min_agents=2,
        freshness_sec=10_000_000,  # тестовые выводы «свежие» независимо от now()
    )


@pytest.fixture
def outputs() -> dict[str, dict[str, Any]]:
    return {
        "market": _agent_output("market", "bullish", 0.8),
        "liquidity": _agent_output("liquidity", "bullish", 0.6),
        "futures": _agent_output("futures", "bullish", 0.7),
    }


async def test_first_signal_is_not_repeat(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    """Первый сигнал инструмента всегда is_repeat = false."""
    from src.decision import agent as agent_mod

    fake = _FakeDB(outputs, last_hash=None)
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    await _make_agent().decide_once()

    assert fake.saved[0]["is_repeat"] is False
    assert len(fake.saved[0]["inputs_hash"]) == 64


async def test_second_signal_with_same_inputs_is_repeat(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    """Два подряд идущих решения с идентичными мнениями: у второго is_repeat = true."""
    from src.decision import agent as agent_mod

    fake = _FakeDB(outputs, last_hash=None)
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    decision_agent = _make_agent()
    await decision_agent.decide_once()
    first_hash = fake.saved[0]["inputs_hash"]

    fake.last_hash = first_hash            # как если бы сигнал уже лежал в БД
    await decision_agent.decide_once()

    assert fake.saved[1]["inputs_hash"] == first_hash
    assert fake.saved[1]["is_repeat"] is True


async def test_changed_inputs_are_not_repeat(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    from src.decision import agent as agent_mod

    fake = _FakeDB(outputs, last_hash="0" * 64)
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    await _make_agent().decide_once()
    assert fake.saved[0]["is_repeat"] is False


async def test_decision_writes_null_calibration_without_curve(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    """Нет активной кривой → обе колонки NULL, агент работает штатно."""
    from src.decision import agent as agent_mod

    fake = _FakeDB(outputs, curve=None)
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    await _make_agent().decide_once()

    saved = fake.saved[0]
    assert saved["calibrated_probability"] is None
    assert saved["calibration_id"] is None
    assert saved["decision"] == "buy"          # решение принято как обычно
    assert saved["probability"] > 0            # индекс согласия на месте


async def test_decision_applies_active_curve(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    """Есть кривая → вероятность берётся из корзины, в которую попал индекс."""
    from src.decision import agent as agent_mod

    curve = {
        "id": 7,
        "bins": [
            {"lo": 0.0, "hi": 0.5, "n": 30, "successes": 15, "p": 0.5},
            {"lo": 0.5, "hi": 1.0, "n": 30, "successes": 6, "p": 0.2},
        ],
    }
    fake = _FakeDB(outputs, curve=curve)
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    await _make_agent().decide_once()

    saved = fake.saved[0]
    assert saved["calibration_id"] == 7
    expected = 0.5 if saved["probability"] < 0.5 else 0.2
    assert saved["calibrated_probability"] == pytest.approx(expected)


async def test_decision_survives_calibration_failure(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, Any]
) -> None:
    """Сбой чтения кривой не роняет решение: сигнал сохраняется без вероятности."""
    from src.decision import agent as agent_mod

    fake = _FakeDB(outputs)

    async def _boom(logic_version: int) -> dict[str, Any]:
        raise RuntimeError("БД недоступна")

    fake.get_active_calibration = _boom  # type: ignore[assignment]
    monkeypatch.setattr(agent_mod, "db", fake)
    monkeypatch.setattr(agent_mod, "get_redis", lambda: _FakeRedis())

    await _make_agent().decide_once()

    assert fake.saved[0]["calibrated_probability"] is None
    assert fake.saved[0]["decision"] == "buy"
