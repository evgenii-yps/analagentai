"""§13.3 ТЗ: выборка независимых наблюдений не содержит пересекающихся окон.

Для каждого горизонта h наблюдение помечается независимым, когда час метки
кратен h. Проверяется главное следствие: расстояние между соседними
независимыми наблюдениями не меньше горизонта, то есть окна не пересекаются.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers import make_config

from backtest.clock import decision_times
from backtest.evaluate import is_independent

HORIZONS = (1, 4, 12, 24)


def _independent_times(horizon: int) -> list[datetime]:
    cfg = make_config()
    return [ts for ts in decision_times(cfg) if is_independent(ts.hour, horizon)]


@pytest.mark.parametrize("horizon", HORIZONS)
def test_independent_windows_do_not_overlap(horizon: int) -> None:
    stamps = _independent_times(horizon)
    assert stamps, "выборка независимых наблюдений пуста"
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        assert (later - earlier) >= timedelta(hours=horizon)


@pytest.mark.parametrize("horizon", HORIZONS)
def test_independent_sample_is_a_subset(horizon: int) -> None:
    cfg = make_config()
    every = set(decision_times(cfg))
    independent = set(_independent_times(horizon))
    assert independent <= every


def test_higher_horizon_gives_fewer_observations() -> None:
    """Чем длиннее горизонт, тем меньше независимых наблюдений — иначе их
    «независимость» была бы фиктивной."""
    counts = [len(_independent_times(h)) for h in HORIZONS]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > counts[-1]


def test_daily_horizon_takes_one_observation_per_day() -> None:
    stamps = _independent_times(24)
    days = {ts.date() for ts in stamps}
    assert len(stamps) == len(days)
    assert all(ts.hour == 0 for ts in stamps)


def test_four_hour_windows_match_stage_7_1_boundaries() -> None:
    """Границы 4-часовых окон совпадают с принятыми в Этапе 7.1 (00/04/08/12/16/20)."""
    stamps = _independent_times(4)
    assert {ts.hour for ts in stamps} == {0, 4, 8, 12, 16, 20}


def test_decision_times_are_hourly_and_inside_period() -> None:
    cfg = make_config()
    stamps = decision_times(cfg)
    assert stamps[0] >= cfg.period_from
    assert stamps[-1] <= cfg.period_to
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        assert (later - earlier) == timedelta(hours=cfg.step_hours)


def test_utc_is_used_everywhere() -> None:
    cfg = make_config()
    assert all(ts.tzinfo == UTC for ts in decision_times(cfg))
