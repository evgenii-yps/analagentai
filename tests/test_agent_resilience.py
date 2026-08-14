"""Тесты самовосстановления агентов (Этап 7.2, Задача A1).

Проверяют, что серия сбоев ИЛИ серия пустых выборок подряд доводит агента до
``_auto_reset``: сброс состояния + переоткрытие пула БД и клиента Redis + запись
события ``auto_reset`` в agent_failures. Инцидент 14.08 показал, что без этого
система 8 часов ждала внешнего перезапуска.
"""

from __future__ import annotations

import pytest

from src.agents import base
from src.agents.base import AgentOutput, BaseAgent
from src.core.config import settings


class _DummyAgent(BaseAgent):
    """Минимальный конкретный агент для проверки инфраструктуры BaseAgent."""

    def __init__(self) -> None:
        super().__init__(name="dummy", interval=60, instrument_id=1)
        self.reset_calls = 0

    async def analyze(self, instrument_id: int) -> AgentOutput:  # pragma: no cover
        raise RuntimeError("не используется в этих тестах")

    async def reset_state(self) -> None:
        self.reset_calls += 1


@pytest.fixture()
def patched(monkeypatch):
    """Подменяет внешние эффекты _auto_reset на счётчики, БД/Redis не трогаем."""
    calls = {"reconnect": 0, "close_redis": 0, "record": []}

    async def fake_reconnect() -> None:
        calls["reconnect"] += 1

    async def fake_close_redis() -> None:
        calls["close_redis"] += 1

    async def fake_record(agent, error_type, exc_type, detail) -> None:
        calls["record"].append((agent, error_type))

    monkeypatch.setattr(base.db, "reconnect", fake_reconnect)
    monkeypatch.setattr(base, "close_redis", fake_close_redis)
    monkeypatch.setattr(base.db, "record_agent_failure", fake_record)
    return calls


async def test_auto_reset_triggers_after_failure_streak(patched) -> None:
    agent = _DummyAgent()
    threshold = settings.AGENT_AUTO_RESET_STREAK

    # threshold-1 сбоев — авто-сброса ещё нет.
    for _ in range(threshold - 1):
        await agent._after_failure()
    assert patched["reconnect"] == 0
    assert agent._consecutive_failures == threshold - 1

    # threshold-й сбой — срабатывает авто-сброс.
    await agent._after_failure()
    assert patched["reconnect"] == 1
    assert patched["close_redis"] == 1
    assert agent.reset_calls == 1
    assert ("dummy", "auto_reset") in patched["record"]
    # Счётчик обнулён — иначе сброс срабатывал бы каждую следующую итерацию.
    assert agent._consecutive_failures == 0


async def test_auto_reset_triggers_after_empty_read_streak(patched) -> None:
    # Пустая выборка НЕ бросает исключения (штатный insufficient_data), но серия
    # пустых ответов при живом сервисе — это и есть симптом инцидента 14.08.
    agent = _DummyAgent()
    threshold = settings.AGENT_AUTO_RESET_STREAK

    for _ in range(threshold - 1):
        await agent._note_read(is_empty=True)
    assert patched["reconnect"] == 0

    await agent._note_read(is_empty=True)
    assert patched["reconnect"] == 1
    assert ("dummy", "auto_reset") in patched["record"]
    assert agent._empty_read_streak == 0


async def test_non_empty_read_resets_empty_streak(patched) -> None:
    agent = _DummyAgent()
    for _ in range(5):
        await agent._note_read(is_empty=True)
    assert agent._empty_read_streak == 5
    # Один непустой ответ обнуляет серию — авто-сброс не сработает по случайным.
    await agent._note_read(is_empty=False)
    assert agent._empty_read_streak == 0
    assert patched["reconnect"] == 0


async def test_record_failure_writes_full_traceback(monkeypatch) -> None:
    # В agent_failures.detail должна попасть ТРАССИРОВКА, а не дубль сообщения.
    captured: dict[str, str] = {}

    async def fake_record(agent, error_type, exc_type, detail) -> None:
        captured["exc_type"] = exc_type
        captured["detail"] = detail

    async def fake_bump() -> None:
        return None

    monkeypatch.setattr(base.db, "record_agent_failure", fake_record)
    agent = _DummyAgent()
    monkeypatch.setattr(agent, "_bump_failure_streak", fake_bump)

    try:
        raise ValueError("No numeric types to aggregate")
    except ValueError as exc:
        await agent._record_failure(base.FAILURE_COMPUTE, exc)

    assert captured["exc_type"] == "ValueError"
    # Признаки трассировки, а не просто текста сообщения.
    assert "Traceback (most recent call last)" in captured["detail"]
    assert "ValueError: No numeric types to aggregate" in captured["detail"]
    assert "test_agent_resilience.py" in captured["detail"]


async def test_success_resets_failure_streak(patched) -> None:
    agent = _DummyAgent()
    agent._consecutive_failures = 7

    async def _noop() -> None:
        return None

    # _on_success сбрасывает in-memory счётчик; Redis-часть глушим.
    import src.core.redis_client as rc

    class _FakeRedis:
        async def delete(self, *a, **k):
            return None

    orig = rc._client
    rc._client = _FakeRedis()
    try:
        await agent._on_success()
    finally:
        rc._client = orig
    assert agent._consecutive_failures == 0
