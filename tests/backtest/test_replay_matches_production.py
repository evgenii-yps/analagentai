"""§13.2 ТЗ — ГЛАВНЫЙ ТЕСТ ЭТАПА: реплей воспроизводит продакшн.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ЧТО НЕТ. Настоящая сверка требует продакшн-БД: моменты
берутся из живого окна ``logic_version = 4``, а сравниваются с тем, что система
записала в реальном времени. В среде разработки продакшн-БД нет, поэтому:

  * здесь проверяется МАШИНЕРИЯ сверки — что расхождение обнаруживается, а
    совпадение признаётся, и что Liquidity из сравнения исключён;
  * сама сверка выполняется на сервере ШАГОМ КОНВЕЙЕРА ``python -m backtest.run``
    (модуль ``backtest.parity``), и она блокирующая: при непройденной сверке
    прогон останавливается и отчёт не строится.

Считать эти тесты заменой сверке на живых данных нельзя, и в отчёте это
записано прямым текстом.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from helpers import INST, T0, make_config, requires_db, seed_candles, seed_funding

from backtest import parity
from backtest.parity import AgentParity, ParityResult


def test_excluded_fragment_boundaries_match_the_specification() -> None:
    """Границы живого окна и исключённого фрагмента — ровно из §13.2 ТЗ."""
    assert parity.LIVE_WINDOW_FROM == datetime(2026, 8, 16, 16, 25, tzinfo=UTC)
    assert parity.EXCLUDED_FROM == datetime(2026, 8, 16, 16, 11, tzinfo=UTC)
    assert parity.EXCLUDED_TO == datetime(2026, 8, 16, 16, 17, tzinfo=UTC)


def test_confidence_tolerance_is_one_millionth() -> None:
    assert parity.CONFIDENCE_TOLERANCE == 1e-6


def test_match_is_recognised() -> None:
    item = AgentParity(agent="market", compared=10, direction_match=10, confidence_match=10)
    assert item.ok is True
    result = ParityResult(agents={"market": item}, moments=10)
    assert result.blocking_ok is True


def test_direction_mismatch_fails_the_gate() -> None:
    """Расхождение направления хотя бы в одном моменте валит блокирующую сверку."""
    item = AgentParity(agent="market", compared=10, direction_match=9, confidence_match=10)
    assert item.ok is False
    assert ParityResult(agents={"market": item}, moments=10).blocking_ok is False


def test_confidence_mismatch_fails_the_gate() -> None:
    item = AgentParity(agent="market", compared=10, direction_match=10, confidence_match=9)
    assert ParityResult(agents={"market": item}, moments=10).blocking_ok is False


def test_empty_comparison_is_not_a_pass() -> None:
    """«Сравнивать было нечего» — это НЕ пройденная сверка."""
    item = AgentParity(agent="market")
    assert item.ok is False
    assert ParityResult(agents={"market": item}, moments=0).blocking_ok is False


def test_futures_mismatch_does_not_block_but_is_recorded() -> None:
    """Futures не блокирует прогон целиком, но его расхождение видно в результате.

    Входы Futures в реплее заведомо беднее: истории открытого интереса нет, а
    история funding даёт расчётную ставку вместо текущей. Поэтому расхождение
    по Futures означает недостоверность конфигурации B, а не дефект реплея
    Market.
    """
    market = AgentParity(agent="market", compared=5, direction_match=5, confidence_match=5)
    futures = AgentParity(agent="futures", compared=5, direction_match=3, confidence_match=2)
    result = ParityResult(agents={"market": market, "futures": futures}, moments=5)
    assert result.blocking_ok is True
    assert result.futures_ok is False
    assert result.as_dict()["agents"]["futures"]["ok"] is False


def test_result_dict_is_serialisable_for_config_json() -> None:
    """Результат сверки кладётся в backtest.runs.config_json — он обязан быть JSON."""
    item = AgentParity(
        agent="market", compared=2, direction_match=1, confidence_match=1,
        mismatches=[{"ts": "2026-08-16T16:30:00+00:00",
                     "live": {"signal": "bullish", "confidence": 0.5},
                     "replay": {"signal": "bearish", "confidence": 0.4}}],
    )
    payload = ParityResult(agents={"market": item}, moments=2).as_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["agents"]["market"]["ok"] is False


def test_note_states_that_decision_is_not_compared() -> None:
    """Отчёт обязан явно говорить, почему итоговое решение не сравнивается."""
    result = ParityResult(agents={}, moments=0, note="")
    assert result.blocking_ok is False
    # Текст пометки формируется в check_parity; проверяем сам факт её наличия там.
    import inspect

    source = inspect.getsource(parity.check_parity)
    assert "Liquidity исключён из сравнения" in source
    assert "не сравнивается" in source


@requires_db
async def test_replayed_agent_values_are_deterministic(bt_db, pool) -> None:
    """Один и тот же снимок даёт один и тот же вывод агентов — байт в байт.

    Без этого свойства сверка с продакшном не имела бы смысла: расхождение
    нельзя было бы отличить от собственной недетерминированности реплея.
    """
    await seed_candles(pool, hours=24 * 40)
    await seed_funding(pool, points=120)
    from backtest.clock import build_snapshot

    cfg = make_config()
    ts = T0 + timedelta(hours=24 * 30)
    snapshot = await build_snapshot(INST, ts, cfg)

    first = parity._replay_agent_values(snapshot)
    second = parity._replay_agent_values(snapshot)
    assert first == second
    assert set(first) == {"market", "futures"}


@requires_db
async def test_replay_uses_production_functions(bt_db, pool, monkeypatch) -> None:
    """Реплей обязан вызывать именно продакшн-функции агентов, а не свои копии.

    Подменяем продакшн-функцию — результат реплея обязан измениться. Если бы в
    реплее была собственная реализация индикаторов, подмена ничего бы не дала,
    и тест это поймал бы.
    """
    await seed_candles(pool, hours=24 * 40)
    await seed_funding(pool, points=120)
    from backtest import replay as replay_mod
    from backtest.clock import build_snapshot

    cfg = make_config()
    ts = T0 + timedelta(hours=24 * 30)
    snapshot = await build_snapshot(INST, ts, cfg)
    original = replay_mod.agent_outputs_at(snapshot, ("market",))[0]

    def fake_analyze(df, min_candles):
        return "bearish", 0.4242, {}, "подмена"

    monkeypatch.setattr(replay_mod, "analyze_ohlcv", fake_analyze)
    patched = replay_mod.agent_outputs_at(snapshot, ("market",))[0]

    assert patched["signal"] == "bearish"
    assert patched["confidence"] == pytest.approx(0.4242)
    assert (original["signal"], original["confidence"]) != (
        patched["signal"], patched["confidence"]
    )
