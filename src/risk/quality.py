"""Предпроверка ряда свечей перед расчётом цели (§1 ТЗ 8.2, повтор в §7).

Те же пять проверок, что делает ``scripts/precheck_8_2.sql`` перед началом
работ, но выполняемые расчётом на каждом суточном пересчёте. SQL-скрипт нужен
человеку и отчёту, этот модуль — коду: пересчёт обязан САМ отказываться считать
цель по дырявому ряду, а не полагаться на то, что кто-то запустил проверку
руками.

ПОЧЕМУ ОТКАЗ, А НЕ «СЧИТАЕМ ПО ТОМУ, ЧТО ЕСТЬ». Цель по ряду с пропусками — это
цель по другому ряду: пропущенные часы выбрасывают из выборки как раз те
движения, которые в них произошли. Такое число выглядит как измерение, но
измерением не является, и человек по нему принимает решение о деньгах.

Модуль чистый: ``now`` приходит параметром, обращений к базе и сети нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.risk.targets import BAR_SECONDS, Candle, contiguous_runs

# Машиночитаемые ключи отказов. Кириллица в них не используется намеренно:
# журнал хранит её экранированной, и поиск по русским словам на исправной
# системе даёт ноль (§11 ТЗ).
FAIL_NO_CANDLES = "no_candles"
FAIL_SHORT_SERIES = "short_series"
FAIL_STALE_LAST_CANDLE = "stale_last_candle"
FAIL_BAD_INVARIANTS = "bad_invariants"
FAIL_TOO_MANY_FLAT = "too_many_flat"
FAIL_DUPLICATE_OPEN_TIME = "duplicate_open_time"


@dataclass(frozen=True)
class SeriesCheck:
    """Числа предпроверки одного инструмента и перечень неисполненных порогов."""

    candles: int
    max_run_hours: int
    age_hours: float | None
    bad_invariants: int
    flat: int
    flat_pct: float | None
    duplicates: int
    first_open_time: datetime | None
    last_open_time: datetime | None
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        """Для журнала: только числа и машиночитаемые ключи."""
        return {
            "candles": self.candles,
            "max_run_hours": self.max_run_hours,
            "age_hours": None if self.age_hours is None else round(self.age_hours, 2),
            "bad_invariants": self.bad_invariants,
            "flat_pct": None if self.flat_pct is None else round(self.flat_pct, 3),
            "duplicates": self.duplicates,
            "failures": list(self.failures),
        }


def longest_run_hours(candles: list[Candle]) -> int:
    """Длина самого длинного НЕПРЕРЫВНОГО отрезка ряда, в часах.

    Именно она сравнивается с порогом «не меньше 2160 часов», а не общее число
    свечей: ряд из трёх тысяч свечей с дырой посередине порога не проходит,
    потому что окно наблюдения должно быть непрерывным.
    """
    if not candles:
        return 0
    runs = contiguous_runs(candles)
    best = 1
    current = 1
    for index in range(1, len(runs)):
        if runs[index] == runs[index - 1]:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def check_series(
    candles: list[Candle],
    *,
    now: datetime,
    min_run_hours: int,
    max_age_hours: float,
    max_flat_pct: float,
) -> SeriesCheck:
    """Пять проверок §1 по загруженному ряду. Вердикта словами не выносит."""
    n = len(candles)
    if n == 0:
        return SeriesCheck(
            candles=0, max_run_hours=0, age_hours=None, bad_invariants=0,
            flat=0, flat_pct=None, duplicates=0,
            first_open_time=None, last_open_time=None,
            failures=(FAIL_NO_CANDLES, FAIL_SHORT_SERIES),
        )

    times = [c.open_time for c in candles]
    duplicates = len(times) - len(set(times))

    bad = sum(
        1
        for c in candles
        if c.high < max(c.open, c.close)
        or c.low > min(c.open, c.close)
        or c.high < c.low
        or c.low <= 0
    )
    flat = sum(1 for c in candles if c.high == c.low)
    flat_pct = 100.0 * flat / n

    last = max(times)
    age_hours = (now - last).total_seconds() / 3600.0
    run_hours = longest_run_hours(candles)

    failures: list[str] = []
    if run_hours < min_run_hours:
        failures.append(FAIL_SHORT_SERIES)
    if age_hours > max_age_hours:
        failures.append(FAIL_STALE_LAST_CANDLE)
    if bad > 0:
        failures.append(FAIL_BAD_INVARIANTS)
    if flat_pct > max_flat_pct:
        failures.append(FAIL_TOO_MANY_FLAT)
    if duplicates > 0:
        # Дубли невозможны по первичному ключу; ненулевое значение означает,
        # что ряд собран не из той таблицы, а не что данные плохие.
        failures.append(FAIL_DUPLICATE_OPEN_TIME)

    return SeriesCheck(
        candles=n,
        max_run_hours=run_hours,
        age_hours=age_hours,
        bad_invariants=bad,
        flat=flat,
        flat_pct=flat_pct,
        duplicates=duplicates,
        first_open_time=min(times),
        last_open_time=last,
        failures=tuple(failures),
    )


def hours_between(start: datetime, end: datetime) -> float:
    """Часы между метками (для журнала и отчёта)."""
    return (end - start).total_seconds() / BAR_SECONDS
