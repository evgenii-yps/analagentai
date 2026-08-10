"""Тесты суточной сводки: две строки счётчиков и кламп возраста heartbeat."""

from datetime import UTC, datetime, timedelta

from src.health.report import clamp_age_seconds, format_daily_report

_NOW = datetime(2026, 8, 10, 6, 0, 0, tzinfo=UTC)


# --- clamp_age_seconds (§13.3) ---

def test_clamp_positive_age() -> None:
    last = _NOW - timedelta(seconds=42)
    assert clamp_age_seconds(last, _NOW) == 42.0


def test_clamp_negative_age_to_zero() -> None:
    # Часы контейнера ушли вперёд → отметка «в будущем» даёт 0, а не -1.
    future = _NOW + timedelta(seconds=5)
    assert clamp_age_seconds(future, _NOW) == 0.0


def test_clamp_none() -> None:
    assert clamp_age_seconds(None, _NOW) is None


# --- format_daily_report (§5.4) ---

def test_report_has_two_separate_counters() -> None:
    text = format_daily_report(
        {
            "decisions_total": 1440,
            "buy": 12,
            "sell": 8,
            "wait": 1420,
            "notified": 15,     # реальные отправки (по notified_at)
            "candidates": 416,  # прошли порог вероятности
            "closed": 20,
        }
    )
    assert "Отправлено уведомлений: 15" in text
    assert "Кандидатов (вероятность ≥ порога): 416" in text


def test_report_includes_heartbeats() -> None:
    text = format_daily_report(
        {"decisions_total": 0, "heartbeats": {"decision": 12.0, "notify": None}}
    )
    assert "decision: 12" in text
    assert "notify: нет данных" in text
