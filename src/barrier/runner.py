"""Суточный расчёт исходов по границам (§7 ТЗ 8.8).

ЧТО ДЕЛАЕТ РАСЧЁТ, по шагам:

  1. для каждого горизонта из ``EVAL_HORIZONS`` отбирает направленные сигналы
     текущей версии логики, у которых ``t + h`` уже в прошлом И есть
     ЗАМОРОЖЕННАЯ цель в ``signal_targets``;
  2. читает свечи окна: сначала минутные, и только если они покрывают ВЕСЬ
     интервал — считает по ним (§4); иначе переходит на часовые;
  3. применяет правило §3 (чистая функция ``src.barrier.outcomes.resolve``);
  4. пишет строку в ``signal_outcomes_barrier``.

ЧЕГО РАСЧЁТ НЕ ДЕЛАЕТ. Он не трогает ни ``signals``, ни ``signal_evaluations``,
ни ``signal_targets``, ни ``risk_targets`` — ни одной строкой, ни при каких
условиях. Действующая оценка остаётся ровно такой, какой была: этап вводит
ВТОРУЮ оценку рядом, а не заменяет первую.

ПОЧЕМУ ДВА ИСТОЧНИКА СВЕЧЕЙ, А НЕ ОДИН. Порядок касаний внутри часового бара
неизвестен, и признать это честнее, чем угадать (§4). Минутный ряд снимает
неопределённость, но живёт ограниченное время: политика хранения удаляет
``ohlcv`` с ``timeframe='1m'`` старше ``RETENTION_1M_DAYS``. Поэтому
разрешение, которым посчитан исход, ХРАНИТСЯ В СТРОКЕ: без него нельзя
отличить измеренный порядок от неизвестного.

СИГНАЛЫ БЕЗ ЗАМОРОЖЕННОЙ ЦЕЛИ ПРОПУСКАЮТСЯ, и их число печатается отдельно
(§7). Подставить им сегодняшнюю цель из ``risk_targets`` нельзя: сегодняшняя
цель посчитана по сегодняшнему рынку, и её подстановка означала бы, что система
«назвала» в прошлом число, которого тогда не существовало.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.barrier.outcomes import (
    OUTCOME_AMBIGUOUS,
    RESOLUTION_1H,
    RESOLUTION_1M,
    Bar,
    BarrierOutcome,
    contiguous_prefix,
    expected_bars,
    resolve,
    window_bounds,
)
from src.core.config import settings
from src.core.db import db

_log = structlog.get_logger().bind(component="barrier_outcomes")


@dataclass
class HorizonOutcome:
    """Итог по одному горизонту — для журнала и отчёта."""

    horizon_h: int
    candidates: int = 0
    written: int = 0
    skipped_no_target: int = 0
    deleted: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    by_resolution: dict[str, int] = field(default_factory=dict)

    def count(self, outcome: str, resolution: str) -> None:
        self.by_outcome[outcome] = self.by_outcome.get(outcome, 0) + 1
        self.by_resolution[resolution] = self.by_resolution.get(resolution, 0) + 1


def _bars(rows: list[dict[str, Any]]) -> list[Bar]:
    """Строки ``ohlcv`` → бары расчёта. ``ts`` — время ОТКРЫТИЯ бара."""
    return [
        Bar(ts=r["ts"], high=float(r["high"]), low=float(r["low"]),
            close=float(r["close"]))
        for r in rows
    ]


def ambiguous_share(by_outcome: dict[str, int]) -> float | None:
    """Доля ``ambiguous`` среди всех посчитанных исходов, в процентах.

    ``None``, если исходов нет вовсе: доля от нуля — не ноль, а отсутствие
    величины, и печатать «0%» в этом случае значило бы утверждать то, чего
    никто не измерял.
    """
    total = sum(by_outcome.values())
    if total == 0:
        return None
    return by_outcome.get(OUTCOME_AMBIGUOUS, 0) / total * 100.0


async def pick_series(
    *, instrument_id: int, signal_ts: datetime, horizon_h: int
) -> tuple[list[Bar], str]:
    """Ряд свечей окна и его разрешение по правилу §4.

    Минутный ряд берётся ТОЛЬКО если он покрывает окно ЦЕЛИКОМ — столько баров,
    сколько их обязано быть, без единого пропуска. Частично покрытый минутный
    ряд не годится: он разрешил бы порядок касаний на одном участке окна и
    оставил бы его неизвестным на другом, а строка в таблице одна, и написать
    в ней пришлось бы одно разрешение на оба участка.
    """
    fine = settings.BARRIER_FINE_TIMEFRAME
    first, last = window_bounds(signal_ts, horizon_h, RESOLUTION_1M)
    rows = await db.get_ohlcv_bars(instrument_id, fine, first, last)
    fine_bars = _bars(rows)
    # Полнота проверяется ТЕМ ЖЕ правилом, которым потом пойдёт расчёт, а не
    # счётом строк: ряд нужной длины, но сдвинутый с сетки, — это не покрытие
    # окна, и переход на часовые в таком случае обязан произойти здесь, а не
    # обернуться исходом no_data на минутном ряде.
    covered = contiguous_prefix(fine_bars, first, RESOLUTION_1M)
    if len(covered) >= expected_bars(horizon_h, RESOLUTION_1M):
        return fine_bars, RESOLUTION_1M

    coarse = settings.BARRIER_COARSE_TIMEFRAME
    first, last = window_bounds(signal_ts, horizon_h, RESOLUTION_1H)
    rows = await db.get_ohlcv_bars(instrument_id, coarse, first, last)
    return _bars(rows), RESOLUTION_1H


def build_row(
    candidate: dict[str, Any],
    result: BarrierOutcome,
    *,
    horizon_h: int,
    stop_pct: float,
    cost_pct: float,
    computed_at: datetime,
) -> dict[str, Any]:
    """Строка ``signal_outcomes_barrier`` по кандидату и посчитанному исходу.

    Цена решения и цель берутся ИЗ ЗАМОРОЖЕННОЙ строки ``signal_targets``, а не
    считаются заново по свечам: уровни обязаны стоять там же, где стояли в
    момент сигнала.
    """
    return {
        "signal_id": candidate["id"],
        "horizon_h": horizon_h,
        "logic_version": candidate["logic_version"],
        "direction": candidate["direction"],
        "price_at_signal": candidate["price_at_signal"],
        "target_pct": candidate["target_pct"],
        "stop_pct": stop_pct,
        "cost_pct": cost_pct,
        "outcome": result.outcome,
        "hit_at": result.hit_at,
        "bars_to_hit": result.bars_to_hit,
        "net_pnl_pct": result.net_pnl_pct,
        "mae_pct": result.mae_pct,
        "mfe_pct": result.mfe_pct,
        "resolution": result.resolution,
        "computed_at": computed_at,
    }


async def compute_horizon(
    horizon_h: int,
    *,
    now: datetime,
    computed_at: datetime,
    recompute: bool,
) -> HorizonOutcome:
    """Расчёт исходов по одному горизонту."""
    logic_version = settings.LOGIC_VERSION
    stop_pct = settings.BARRIER_STOP_PCT
    cost_pct = settings.RISK_COST_ROUNDTRIP_PCT
    item = HorizonOutcome(horizon_h=horizon_h)

    if recompute:
        item.deleted = await db.delete_barrier_outcomes(
            logic_version=logic_version, horizon_h=horizon_h
        )

    item.skipped_no_target = await db.count_barrier_skipped(
        logic_version=logic_version, horizon_h=horizon_h, now=now
    )
    candidates = await db.get_barrier_candidates(
        logic_version=logic_version, horizon_h=horizon_h, now=now, recompute=recompute
    )
    item.candidates = len(candidates)

    for candidate in candidates:
        bars, resolution = await pick_series(
            instrument_id=candidate["instrument_id"],
            signal_ts=candidate["ts"],
            horizon_h=horizon_h,
        )
        result = resolve(
            bars,
            signal_ts=candidate["ts"],
            horizon_h=horizon_h,
            price_at_signal=float(candidate["price_at_signal"]),
            target_pct=float(candidate["target_pct"]),
            stop_pct=stop_pct,
            cost_pct=cost_pct,
            direction=candidate["direction"],
            resolution=resolution,
        )
        await db.save_barrier_outcome(
            build_row(
                candidate, result, horizon_h=horizon_h, stop_pct=stop_pct,
                cost_pct=cost_pct, computed_at=computed_at,
            )
        )
        item.written += 1
        item.count(result.outcome, result.resolution)

    _log.info(
        "barrier_horizon_done=1",
        horizon_h=horizon_h,
        candidates=item.candidates,
        written=item.written,
        deleted=item.deleted,
        skipped_no_target=item.skipped_no_target,
        **{f"outcome_{k}": v for k, v in sorted(item.by_outcome.items())},
        **{f"resolution_{k}": v for k, v in sorted(item.by_resolution.items())},
    )
    return item


async def compute(
    now: datetime | None = None, *, recompute: bool = False
) -> list[HorizonOutcome]:
    """Полный расчёт по всем горизонтам ``EVAL_HORIZONS``.

    ``computed_at`` берётся ОДИН РАЗ на весь прогон, а не по строке: строки
    одного прогона обязаны быть отличимы от строк следующего одним значением,
    иначе нельзя ответить, какой запуск их написал.
    """
    now = now or datetime.now(UTC)
    computed_at = now
    await db.ensure_barrier_schema()

    items: list[HorizonOutcome] = []
    for horizon_h in settings.eval_horizons_hours:
        items.append(await compute_horizon(
            horizon_h, now=now, computed_at=computed_at, recompute=recompute
        ))

    totals: dict[str, int] = {}
    for item in items:
        for outcome, n in item.by_outcome.items():
            totals[outcome] = totals.get(outcome, 0) + n
    share = ambiguous_share(totals)

    _log.info(
        "barrier_compute_done=1",
        horizons=len(items),
        written=sum(i.written for i in items),
        skipped_no_target=sum(i.skipped_no_target for i in items),
        ambiguous_pct=(None if share is None else round(share, 2)),
        computed_at=computed_at.isoformat(),
        **{f"outcome_{k}": v for k, v in sorted(totals.items())},
    )
    if share is not None and share > settings.BARRIER_AMBIGUOUS_MAX_PCT:
        # ОТДЕЛЬНАЯ СТРОКА, как требует §4 ТЗ. Это не сбой расчёта: расчёт
        # верен, а вот метрика при такой доле неразрешимых случаев отвечает на
        # вопрос человека хуже, чем кажется, — и молчать об этом нельзя.
        _log.warning(
            "barrier_metric_unusable=1",
            ambiguous_pct=round(share, 2),
            threshold_pct=settings.BARRIER_AMBIGUOUS_MAX_PCT,
            detail="доля ambiguous выше порога — метрика в таком виде малопригодна",
        )
    return items


async def run(*, recompute: bool = False) -> list[HorizonOutcome]:
    """Точка входа сценария: своё подключение к БД, своё закрытие."""
    await db.connect()
    try:
        return await compute(recompute=recompute)
    finally:
        await db.close()
