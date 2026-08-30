"""Суточный расчёт исходов базовых стратегий (§4–§5, §7 ТЗ 8.9).

ЧТО ДЕЛАЕТ РАСЧЁТ, по шагам:

  1. берёт моменты входа ИЗ ``signal_outcomes_barrier`` — те же сигналы, те же
     цены, те же горизонты, что считала система (§4);
  2. на каждом таком моменте определяет направление по правилу каждой из
     четырёх стратегий §4 и цель к нему;
  3. читает окно свечей ОДИН РАЗ на момент и прогоняет по нему все стратегии;
  4. отдельно строит сетку §5 — вход каждый час независимо от системы;
  5. пишет строки в ``strategy_outcomes``.

ПРАВИЛО ИСХОДА ЗДЕСЬ НЕ ПОВТОРЯЕТСЯ. Оно берётся из ``src.barrier.outcomes``
как есть (§2 ТЗ), вместе с выбором разрешения ряда (``pick_series``). Вторая
реализация того же правила рано или поздно разошлась бы с первой на краевом
случае, и сравнение стало бы недействительным — причём незаметно.

ОДНО ОКНО НА ЧЕТЫРЕ СТРАТЕГИИ — не оптимизация ради красоты, а условие
выполнимости §7. Четыре стратегии на одном моменте смотрят на ОДИН И ТОТ ЖЕ
отрезок свечей: он зависит от инструмента, момента и горизонта, но не от того,
покупаем мы или продаём. Читать его четырежды значило бы учетверить самую
дорогую часть работы ради одного и того же ответа.

ОТКУДА БЕРЁТСЯ ЦЕЛЬ, и почему по-разному:

  * направление стратегии СОВПАЛО с направлением сигнала → цель ЗАМОРОЖЕННАЯ,
    та самая, что была названа человеку (``target_source='frozen'``);
  * направление ВСТРЕЧНОЕ → замороженной цели не существует, её никто не
    замораживал; берётся историческая строка ``risk_targets`` за дату входа
    (``target_source='risk_targets:<дата>'``).

Это различение обязательно, и вот почему. Возьми мы историческую цель ВЕЗДЕ,
``always_buy`` расходился бы с ``system`` на сигналах «покупать» — при
одинаковом направлении, одинаковой цене и одинаковом окне, только из-за разного
источника цели. Сравнение показало бы разницу там, где её нет.

СИГНАЛЫ БЕЗ ИСТОРИЧЕСКОЙ ЦЕЛИ ПРОПУСКАЮТСЯ, и их число печатается отдельно.
Подставить сегодняшнюю цель нельзя: она посчитана по сегодняшнему рынку.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.barrier.outcomes import resolve
from src.barrier.runner import pick_series, settle_seconds
from src.baseline.strategies import (
    COIN_FLIP,
    GRID_STRATEGIES,
    SIGNAL_STRATEGIES,
    SOURCE_FROZEN,
    STRATEGIES,
    direction_for,
    hourly_grid_entries,
    risk_target_source,
)
from src.core.config import settings
from src.core.db import db

_log = structlog.get_logger().bind(component="baseline_strategies")


@dataclass
class StrategyStat:
    """Итог по одной стратегии — для журнала и отчёта."""

    strategy: str
    written: int = 0
    skipped_no_target: int = 0
    deleted: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)

    def count(self, outcome: str) -> None:
        self.by_outcome[outcome] = self.by_outcome.get(outcome, 0) + 1


@dataclass
class RunStats:
    """Итог прогона целиком, включая замер производительности (§7 ТЗ)."""

    anchors: int = 0
    grid_entries: int = 0
    windows_read: int = 0
    seconds: float = 0.0
    per_strategy: dict[str, StrategyStat] = field(default_factory=dict)

    def stat(self, strategy: str) -> StrategyStat:
        return self.per_strategy.setdefault(strategy, StrategyStat(strategy=strategy))

    @property
    def written(self) -> int:
        return sum(s.written for s in self.per_strategy.values())


async def target_for(
    strategy: str,
    direction: str,
    *,
    instrument_id: int,
    entry_ts: datetime,
    horizon_h: int,
    signal_direction: str | None,
    frozen_target_pct: float | None,
) -> tuple[float, str] | None:
    """Цель стратегии и подпись её источника, либо ``None``, если цели нет.

    ``None`` означает «посчитать нечем» и ведёт к пропуску строки, а НЕ к
    подстановке какого-нибудь правдоподобного числа: пропущенная строка видна
    в счётчике, подставленная — неотличима от измеренной.
    """
    if (
        signal_direction is not None
        and direction == signal_direction
        and frozen_target_pct is not None
    ):
        return float(frozen_target_pct), SOURCE_FROZEN

    row = await db.get_risk_target_asof(instrument_id, horizon_h, direction, entry_ts)
    if row is None or row.get("target_pct") is None:
        return None
    return float(row["target_pct"]), risk_target_source(row["computed_at"])


def _row(
    *,
    strategy: str,
    instrument_id: int,
    entry_ts: datetime,
    horizon_h: int,
    signal_id: int | None,
    logic_version: int,
    direction: str,
    price: float,
    target_pct: float,
    target_source: str,
    stop_pct: float,
    cost_pct: float,
    result: Any,
    seed: int | None,
    computed_at: datetime,
) -> dict[str, Any]:
    """Строка ``strategy_outcomes`` по посчитанному исходу."""
    return {
        "strategy": strategy,
        "instrument_id": instrument_id,
        "entry_ts": entry_ts,
        "horizon_h": horizon_h,
        "signal_id": signal_id,
        "logic_version": logic_version,
        "direction": direction,
        "price_at_entry": price,
        "target_pct": target_pct,
        "target_source": target_source,
        "stop_pct": stop_pct,
        "cost_pct": cost_pct,
        "outcome": result.outcome,
        "hit_at": result.hit_at,
        "net_pnl_pct": result.net_pnl_pct,
        "mae_pct": result.mae_pct,
        "mfe_pct": result.mfe_pct,
        "resolution": result.resolution,
        "seed": seed,
        "computed_at": computed_at,
    }


async def _evaluate_at(
    *,
    strategies: tuple[str, ...],
    instrument_id: int,
    entry_ts: datetime,
    horizon_h: int,
    price: float,
    signal_id: int | None,
    signal_direction: str | None,
    frozen_target_pct: float | None,
    logic_version: int,
    stats: RunStats,
    done: set[tuple[str, int, datetime, int]],
    computed_at: datetime,
) -> None:
    """Считает все переданные стратегии на ОДНОМ моменте и ОДНОМ окне свечей."""
    seed = settings.BASELINE_SEED
    stop_pct = settings.BARRIER_STOP_PCT
    cost_pct = settings.RISK_COST_ROUNDTRIP_PCT

    pending: list[tuple[str, str, float, str]] = []
    for strategy in strategies:
        if (strategy, instrument_id, entry_ts, horizon_h) in done:
            continue
        direction = direction_for(
            strategy,
            signal_direction=signal_direction,
            seed=seed,
            signal_id=signal_id,
            horizon_h=horizon_h,
        )
        target = await target_for(
            strategy, direction,
            instrument_id=instrument_id, entry_ts=entry_ts, horizon_h=horizon_h,
            signal_direction=signal_direction, frozen_target_pct=frozen_target_pct,
        )
        if target is None:
            stats.stat(strategy).skipped_no_target += 1
            continue
        target_pct, source = target
        pending.append((strategy, direction, target_pct, source))

    if not pending:
        # Ни одной строки к записи — окно свечей читать незачем. Это и есть
        # причина, по которой повторный прогон дёшев.
        return

    bars, resolution = await pick_series(
        instrument_id=instrument_id, signal_ts=entry_ts, horizon_h=horizon_h
    )
    stats.windows_read += 1

    for strategy, direction, target_pct, source in pending:
        result = resolve(
            bars,
            signal_ts=entry_ts,
            horizon_h=horizon_h,
            price_at_signal=price,
            target_pct=target_pct,
            stop_pct=stop_pct,
            cost_pct=cost_pct,
            direction=direction,
            resolution=resolution,
        )
        await db.save_strategy_outcome(_row(
            strategy=strategy, instrument_id=instrument_id, entry_ts=entry_ts,
            horizon_h=horizon_h, signal_id=signal_id, logic_version=logic_version,
            direction=direction, price=price, target_pct=target_pct,
            target_source=source, stop_pct=stop_pct, cost_pct=cost_pct,
            result=result, seed=(seed if strategy == COIN_FLIP else None),
            computed_at=computed_at,
        ))
        stat = stats.stat(strategy)
        stat.written += 1
        stat.count(result.outcome)


async def compute_signal_strategies(
    *,
    strategies: tuple[str, ...],
    since: datetime | None,
    limit: int | None,
    stats: RunStats,
    done: set[tuple[str, int, datetime, int]],
    computed_at: datetime,
) -> None:
    """Стратегии §4 — на моментах, где система выдала сигнал."""
    anchors = await db.get_strategy_anchors(
        logic_version=settings.LOGIC_VERSION, since=since, limit=limit
    )
    stats.anchors = len(anchors)
    for anchor in anchors:
        await _evaluate_at(
            strategies=strategies,
            instrument_id=anchor["instrument_id"],
            entry_ts=anchor["ts"],
            horizon_h=anchor["horizon_h"],
            price=float(anchor["price_at_signal"]),
            signal_id=anchor["signal_id"],
            signal_direction=anchor["direction"],
            frozen_target_pct=float(anchor["target_pct"]),
            logic_version=anchor["logic_version"],
            stats=stats, done=done, computed_at=computed_at,
        )


async def compute_grid_strategies(
    *,
    strategies: tuple[str, ...],
    since: datetime | None,
    limit: int | None,
    now: datetime,
    stats: RunStats,
    done: set[tuple[str, int, datetime, int]],
    computed_at: datetime,
) -> None:
    """Стратегии §5 — вход каждый час независимо от системы.

    Границы сетки берутся не из настроек, а из ФАКТИЧЕСКОГО окна наблюдения:
    от первого момента, на котором считалась система, до последнего. Сетка,
    уходящая за пределы наблюдения, сравнивала бы фон одного отрезка рынка с
    системой на другом — и разница отражала бы смену отрезка, а не разницу
    правил.
    """
    window = await db.get_barrier_window(logic_version=settings.LOGIC_VERSION)
    if window is None or window["ts_from"] is None:
        _log.info(
            "baseline_grid_skipped=1",
            reason="исходов системы нет — окно наблюдения не определено",
        )
        return

    start = since or window["ts_from"]
    entries = hourly_grid_entries(start, window["ts_to"])
    if not entries:
        _log.info("baseline_grid_skipped=1", reason="в окне наблюдения нет целых часов")
        return

    for pair in settings.symbol_pairs:
        instrument_id = await db.get_instrument_id(pair.spot)
        if instrument_id is None:
            continue
        # Цены входа читаются ОДИН раз на инструмент: они не зависят от
        # горизонта, и повторное чтение на каждый горизонт было бы четырёхкратной
        # платой за один и тот же ответ.
        prices = {
            row["ts"]: float(row["close"])
            for row in await db.get_grid_prices(
                instrument_id, settings.BASELINE_GRID_TIMEFRAME,
                start, window["ts_to"],
            )
        }
        for horizon_h in settings.eval_horizons_hours:
            used = 0
            for entry_ts in entries:
                if limit is not None and used >= limit:
                    break
                price = prices.get(entry_ts)
                if price is None or price <= 0:
                    # Свечи ровно на этот час нет — входить не по чему.
                    continue
                # ОКНО КОНЧАЕТСЯ БАРОМ, КОТОРЫЙ ОТКРЫВАЕТСЯ В МОМЕНТ СРОКА, а
                # закрывается через целый бар после него. Пока он формируется,
                # его close — цена «пока что», и коллектор перезапишет её
                # следующим опросом (UPSERT с DO UPDATE); исход timeout берёт
                # итог именно из этого close. Запас берётся из ОДНОГО места с
                # Этапом 8.10.1 (settle_seconds), а не переписывается формулой:
                # две копии одного правила разошлись бы при следующей правке, и
                # разошлись бы молча.
                if entry_ts + timedelta(
                    hours=horizon_h, seconds=settle_seconds()
                ) > now:
                    # Горизонт ещё не наступил ЛИБО последний бар окна не закрыт.
                    continue
                used += 1
                stats.grid_entries += 1
                await _evaluate_at(
                    strategies=strategies,
                    instrument_id=instrument_id,
                    entry_ts=entry_ts,
                    horizon_h=horizon_h,
                    price=price,
                    signal_id=None,
                    signal_direction=None,
                    frozen_target_pct=None,
                    logic_version=settings.LOGIC_VERSION,
                    stats=stats, done=done, computed_at=computed_at,
                )


async def compute(
    *,
    strategy: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    recompute: bool = False,
    now: datetime | None = None,
) -> RunStats:
    """Полный расчёт. ``strategy=None`` — все шесть."""
    now = now or datetime.now(UTC)
    computed_at = now
    await db.ensure_strategy_schema()

    selected = STRATEGIES if strategy is None else (strategy,)
    unknown = [s for s in selected if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"неизвестные стратегии: {unknown}")

    stats = RunStats()
    started = time.monotonic()

    if recompute:
        for item in selected:
            stats.stat(item).deleted = await db.delete_strategy_outcomes(
                strategy=item, logic_version=settings.LOGIC_VERSION
            )

    done = await db.get_strategy_pairs_done(logic_version=settings.LOGIC_VERSION)

    signal_side = tuple(s for s in selected if s in SIGNAL_STRATEGIES)
    grid_side = tuple(s for s in selected if s in GRID_STRATEGIES)

    if signal_side:
        await compute_signal_strategies(
            strategies=signal_side, since=since, limit=limit,
            stats=stats, done=done, computed_at=computed_at,
        )
    if grid_side:
        await compute_grid_strategies(
            strategies=grid_side, since=since, limit=limit, now=now,
            stats=stats, done=done, computed_at=computed_at,
        )

    stats.seconds = time.monotonic() - started
    for item in sorted(stats.per_strategy):
        stat = stats.per_strategy[item]
        _log.info(
            "baseline_strategy_done=1",
            strategy=item, written=stat.written, deleted=stat.deleted,
            skipped_no_target=stat.skipped_no_target,
            **{f"outcome_{k}": v for k, v in sorted(stat.by_outcome.items())},
        )
    _log.info(
        "baseline_compute_done=1",
        anchors=stats.anchors,
        grid_entries=stats.grid_entries,
        windows_read=stats.windows_read,
        written=stats.written,
        seconds=round(stats.seconds, 3),
        rows_per_second=(round(stats.written / stats.seconds, 1)
                         if stats.seconds > 0 else None),
        seed=settings.BASELINE_SEED,
        computed_at=computed_at.isoformat(),
    )
    return stats


async def run(**kwargs: Any) -> RunStats:
    """Точка входа сценария: своё подключение к БД, своё закрытие."""
    await db.connect()
    try:
        return await compute(**kwargs)
    finally:
        await db.close()
