"""§13.5 ТЗ: контроль целостности рядов.

На искусственном ряде с ИЗВЕСТНЫМ разрывом проверяется, что
``check_continuity`` находит ровно этот разрыв, а окна принятия решения,
попавшие в него (и в прогрев после него), исключаются из выборки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backtest.integrity import (
    build_report,
    excluded_windows,
    is_excluded,
)

T0 = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _series(hours: int, skip: range | None = None) -> list[datetime]:
    return [T0 + HOUR * i for i in range(hours) if not (skip and i in skip)]


def test_continuous_series_has_no_gaps() -> None:
    stamps = _series(100)
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 99, HOUR)
    assert report.is_continuous
    assert report.actual_n == 100
    assert report.expected_n == 100
    assert report.coverage_pct == pytest.approx(100.0)


def test_known_gap_is_found_exactly() -> None:
    """Ряд с вырезанными часами 50–59: находится ровно один разрыв на 10 свечей."""
    stamps = _series(100, skip=range(50, 60))
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 99, HOUR)

    assert len(report.gaps) == 1
    gap_from, gap_to, missing = report.gaps[0]
    assert gap_from == T0 + HOUR * 49
    assert gap_to == T0 + HOUR * 60
    assert missing == 10
    assert report.actual_n == 90


def test_two_gaps_are_found_separately() -> None:
    stamps = _series(120, skip=range(30, 33))
    stamps = [ts for ts in stamps if ts not in {T0 + HOUR * i for i in range(80, 90)}]
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 119, HOUR)
    assert len(report.gaps) == 2
    assert [g[2] for g in report.gaps] == [3, 10]


def test_duplicate_or_backward_time_is_reported() -> None:
    """Немонотонность и дубли не проходят молча."""
    stamps = _series(10)
    stamps.insert(5, stamps[4])          # дубль
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 9, HOUR)
    assert any(missing == 0 for _f, _t, missing in report.gaps)


def test_windows_in_gap_and_warmup_are_excluded() -> None:
    """Исключается сам разрыв и заданное число свечей прогрева после него."""
    stamps = _series(400, skip=range(100, 110))
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 399, HOUR)
    excluded = excluded_windows(report.gaps, warmup_candles=200, step=HOUR)

    assert is_excluded(T0 + HOUR * 105, excluded) is True   # внутри разрыва
    assert is_excluded(T0 + HOUR * 200, excluded) is True   # внутри прогрева
    assert is_excluded(T0 + HOUR * 90, excluded) is False   # до разрыва
    assert is_excluded(T0 + HOUR * 350, excluded) is False  # после прогрева


def test_warmup_length_matches_production_window() -> None:
    """Длина прогрева берётся из продакшн-настройки, а не из константы теста."""
    from src.core.config import settings

    stamps = _series(600, skip=range(50, 52))
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 599, HOUR)
    excluded = excluded_windows(
        report.gaps, warmup_candles=settings.AGENT_MIN_CANDLES, step=HOUR
    )
    boundary = T0 + HOUR * 52 + HOUR * settings.AGENT_MIN_CANDLES
    assert is_excluded(boundary - HOUR, excluded) is True
    assert is_excluded(boundary + HOUR, excluded) is False


def test_gap_is_not_interpolated() -> None:
    """Проверка целостности НИЧЕГО не дописывает в ряд: она только сообщает.

    Подстановка недостающих данных запрещена (§16 ТЗ); тест фиксирует, что
    функция возвращает отчёт, а не «починенный» ряд.
    """
    stamps = _series(50, skip=range(20, 25))
    report = build_report("INST", "candles", stamps, T0, T0 + HOUR * 49, HOUR)
    assert report.actual_n == 45
    assert report.expected_n == 50
    assert not hasattr(report, "filled")
