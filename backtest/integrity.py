"""Контроль целостности загруженных рядов (§4.3 ТЗ).

Разрывы НЕ «зашиваются» интерполяцией и ничем не заполняются: найденный разрыв
записывается в ``backtest.gaps``, а окна принятия решения, попавшие в разрыв
или в ``AGENT_MIN_CANDLES`` свечей после него, исключаются из выборки с
указанием причины. Подстановка недостающих данных запрещена (§16 ТЗ) — она
превратила бы измерение в измерение собственных допущений.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backtest import db
from backtest.loader import bar_seconds

SERIES_CANDLES = "candles"
SERIES_FUNDING = "funding"


@dataclass(frozen=True)
class ContinuityReport:
    """Отчёт о непрерывности одного ряда (§11 ТЗ)."""

    inst_id: str
    series: str
    expected_n: int
    actual_n: int
    gaps: list[tuple[datetime, datetime, int]]

    @property
    def is_continuous(self) -> bool:
        return not self.gaps

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.actual_n / self.expected_n if self.expected_n else 0.0


async def check_continuity(
    inst_id: str,
    series: str,
    since: datetime,
    until: datetime,
    *,
    bar: str = "1H",
    step_seconds: int | None = None,
) -> ContinuityReport:
    """Ищет пропуски, дубли и нарушения монотонности времени в ряде.

    Для свечей шаг известен из бара. Для funding шаг НЕ предполагается: он
    берётся как медиана фактических интервалов ряда — ТЗ прямо требует
    подтвердить интервал зондом, а не считать восьмичасовым по умолчанию.
    Дубли невозможны на уровне схемы (первичный ключ), но проверяются: если
    первичный ключ когда-нибудь ослабят, проверка это обнаружит.
    """
    if series == SERIES_CANDLES:
        rows = await db.fetch(
            "SELECT open_time AS ts FROM backtest.candles "
            "WHERE inst_id=$1 AND bar=$2 AND open_time BETWEEN $3 AND $4 "
            "ORDER BY open_time;",
            inst_id, bar, since, until,
        )
        step = timedelta(seconds=step_seconds or bar_seconds(bar))
    elif series == SERIES_FUNDING:
        rows = await db.fetch(
            "SELECT funding_time AS ts FROM backtest.funding "
            "WHERE inst_id=$1 AND funding_time BETWEEN $2 AND $3 "
            "ORDER BY funding_time;",
            inst_id, since, until,
        )
        step = timedelta(seconds=step_seconds) if step_seconds else _median_step(
            [r["ts"] for r in rows]
        )
    else:
        raise ValueError(f"неизвестный ряд: {series}")

    stamps = [r["ts"] for r in rows]
    return build_report(inst_id, series, stamps, since, until, step)


def _median_step(stamps: list[datetime]) -> timedelta:
    """Медианный фактический интервал ряда (0 записей → час как нейтральный шаг)."""
    if len(stamps) < 2:
        return timedelta(hours=1)
    deltas = sorted(
        (stamps[i + 1] - stamps[i]).total_seconds() for i in range(len(stamps) - 1)
    )
    middle = deltas[len(deltas) // 2]
    return timedelta(seconds=middle if middle > 0 else 3600)


def build_report(
    inst_id: str,
    series: str,
    stamps: list[datetime],
    since: datetime,
    until: datetime,
    step: timedelta,
) -> ContinuityReport:
    """Чистая часть проверки: по списку отметок времени строит отчёт.

    Вынесена отдельно, чтобы проверяться тестом без обращения к БД.
    """
    step_sec = step.total_seconds() or 3600.0
    expected_n = int((until - since).total_seconds() // step_sec) + 1
    gaps: list[tuple[datetime, datetime, int]] = []

    previous: datetime | None = None
    for ts in stamps:
        if previous is None:
            previous = ts
            continue
        delta = (ts - previous).total_seconds()
        if delta <= 0:
            # Немонотонность или дубль — сообщаем как разрыв нулевой длины,
            # чтобы факт не потерялся молча.
            gaps.append((previous, ts, 0))
        elif delta > step_sec * 1.5:
            missing = int(round(delta / step_sec)) - 1
            gaps.append((previous, ts, missing))
        previous = ts

    return ContinuityReport(
        inst_id=inst_id,
        series=series,
        expected_n=expected_n,
        actual_n=len(stamps),
        gaps=gaps,
    )


async def save_gaps(report: ContinuityReport) -> int:
    """Записывает найденные разрывы в backtest.gaps (идемпотентно)."""
    if not report.gaps:
        return 0
    await db.pool().executemany(
        """
        INSERT INTO backtest.gaps (inst_id, series, gap_from, gap_to, missing_n)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (inst_id, series, gap_from) DO NOTHING;
        """,
        [
            (report.inst_id, report.series, gap_from, gap_to, missing)
            for gap_from, gap_to, missing in report.gaps
        ],
    )
    return len(report.gaps)


def excluded_windows(
    gaps: list[tuple[datetime, datetime, int]],
    warmup_candles: int,
    step: timedelta,
) -> list[tuple[datetime, datetime]]:
    """Отрезки времени, из которых моменты решения исключаются.

    Исключается сам разрыв и ``warmup_candles`` свечей после него: индикаторы
    Market Agent (EMA200 и прочие) считаются по окну назад, поэтому первые
    наблюдения после разрыва опираются на неполную историю.
    """
    excluded: list[tuple[datetime, datetime]] = []
    for gap_from, gap_to, _missing in gaps:
        excluded.append((gap_from, gap_to + step * warmup_candles))
    return excluded


def is_excluded(ts: datetime, excluded: list[tuple[datetime, datetime]]) -> bool:
    """Попадает ли момент решения в исключённый отрезок."""
    return any(start < ts <= end for start, end in excluded)


def utcnow() -> datetime:
    return datetime.now(UTC)
