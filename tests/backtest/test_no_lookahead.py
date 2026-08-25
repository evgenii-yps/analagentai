"""§13.1 ТЗ: реплей не заглядывает в будущее.

Две независимые проверки:

  1. для 100 случайных моментов T снимок не содержит ни одной строки со
     временем позже T;
  2. физическое удаление ВСЕХ строк с временем > T из тестовой БД не меняет
     результат реплея в момент T (совпадение до 1e-9).

Вторая проверка сильнее первой: она ловит заглядывание в будущее не только
там, где его видно в снимке, но и любое косвенное — через агрегаты, кэши или
запросы в обход ``clock``.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest
from helpers import PAIR, SPOT, SWAP, T0, make_config, requires_db, seed_candles, seed_funding

from backtest import clock, replay

pytestmark = requires_db


@pytest.fixture
async def seeded(bt_db, pool):
    await seed_candles(pool, hours=24 * 40)
    await seed_funding(pool, points=120)
    return pool


async def test_snapshot_contains_nothing_from_the_future(seeded) -> None:
    """Ни одна строка снимка не может относиться ко времени позже T."""
    cfg = make_config()
    rng = random.Random(20260816)
    moments = [
        T0 + timedelta(hours=rng.randint(24 * 21, 24 * 39))
        for _ in range(100)
    ]
    for ts in moments:
        snapshot = await clock.build_snapshot(PAIR, ts, cfg)
        if not snapshot.candles.empty:
            assert snapshot.candles["close_time"].max() <= ts
        if not snapshot.funding.empty:
            assert snapshot.funding["funding_time"].max() <= ts


async def test_candle_closing_exactly_at_t_is_included(seeded) -> None:
    """Свеча, закрывающаяся ровно в T, входит в снимок; следующая — нет."""
    cfg = make_config()
    ts = T0 + timedelta(hours=24 * 25)
    snapshot = await clock.build_snapshot(PAIR, ts, cfg)
    close_times = list(snapshot.candles["close_time"])
    assert ts in close_times
    assert all(t <= ts for t in close_times)


async def test_deleting_future_rows_does_not_change_replay(seeded, pool) -> None:
    """Удаление будущего из БД не меняет вывод агентов в момент T.

    Это и есть доказательство отсутствия заглядывания: если бы реплей хоть
    как-то опирался на данные после T, удаление их изменило бы результат.
    """
    cfg = make_config()
    ts = T0 + timedelta(hours=24 * 30)

    before = await clock.build_snapshot(PAIR, ts, cfg)
    outputs_before = replay.agent_outputs_at(before, ("market", "futures"))

    deleted_candles = await pool.execute(
        "DELETE FROM backtest.candles WHERE inst_id=$1 AND close_time > $2;", SPOT, ts
    )
    deleted_funding = await pool.execute(
        "DELETE FROM backtest.funding WHERE inst_id=$1 AND funding_time > $2;", SWAP, ts
    )
    assert deleted_candles.startswith("DELETE")
    assert deleted_funding.startswith("DELETE")

    after = await clock.build_snapshot(PAIR, ts, cfg)
    outputs_after = replay.agent_outputs_at(after, ("market", "futures"))

    for left, right in zip(outputs_before, outputs_after, strict=True):
        if left is None or right is None:
            assert left is right
            continue
        assert left["agent"] == right["agent"]
        assert left["signal"] == right["signal"]
        assert abs(left["confidence"] - right["confidence"]) < 1e-9


async def test_deleting_future_does_not_change_decision(seeded, pool) -> None:
    """То же для итогового решения Decision Agent, а не только для агентов."""
    from src.core.config import settings
    from src.decision.agent import AGENTS, make_decision

    cfg = make_config()
    ts = T0 + timedelta(hours=24 * 28)

    def decide(snapshot):
        outputs = replay.agent_outputs_at(snapshot, ("market", "futures"))
        return make_decision(
            outputs,
            weights=settings.agent_weights,
            threshold=settings.DECISION_THRESHOLD,
            min_agents=settings.MIN_AGENTS,
            freshness_sec=settings.AGENT_FRESHNESS_SEC,
            now=ts,
            total_agents=len(AGENTS),
        )

    decision_before, conviction_before, _p, _r = decide(
        await clock.build_snapshot(PAIR, ts, cfg)
    )
    await pool.execute(
        "DELETE FROM backtest.candles WHERE inst_id=$1 AND close_time > $2;", SPOT, ts
    )
    await pool.execute(
        "DELETE FROM backtest.funding WHERE inst_id=$1 AND funding_time > $2;", SWAP, ts
    )
    decision_after, conviction_after, _p2, _r2 = decide(
        await clock.build_snapshot(PAIR, ts, cfg)
    )

    assert decision_before == decision_after
    assert abs(conviction_before - conviction_after) < 1e-9
