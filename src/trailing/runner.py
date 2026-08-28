"""Суточный расчёт исходов подвижного выхода (§7 ТЗ 8.10).

ЧТО ДЕЛАЕТ РАСЧЁТ, по шагам:

  1. берёт пары (сигнал, горизонт) ИЗ ``signal_outcomes_barrier`` — вместе с
     направлением, ценой решения, замороженной целью, пределом и издержками;
  2. читает минутное окно ОДИН РАЗ на пару (``barrier.runner.pick_series``);
  3. считает по нему ВСЕ ТРИНАДЦАТЬ вариантов за один проход
     (``trailing.rule.resolve_all``);
  4. пишет тринадцать строк одной пачкой в ``trailing_outcomes``;
  5. в конце СВЕРЯЕТ контрольный вариант с ``signal_outcomes_barrier``.

ПОЧЕМУ ОКНО ЧИТАЕТСЯ ОДИН РАЗ — это требование §7 ТЗ, а не оптимизация ради
красоты. Тринадцать проходов означали бы тринадцатикратное чтение минутных
свечей: при ~460 тысячах строк итога это разница между минутами и часами, и
суточная задача перестала бы укладываться в своё окно между двумя другими.

ЧЕГО РАСЧЁТ НЕ ДЕЛАЕТ. Он не трогает ни ``signals``, ни ``signal_evaluations``,
ни ``signal_targets``, ни ``risk_targets``, ни ``signal_outcomes_barrier``, ни
``strategy_outcomes`` — ни одной строкой, ни при каких условиях. Он не выбирает
«лучший» вариант и не меняет ни одного правила системы: §5.4 ТЗ запрещает выбор
параметра для внедрения прямо, а §2 запрещает трогать горячий путь.

СВЕРКА КОНТРОЛЬНОГО ВАРИАНТА — НЕ ФОРМАЛЬНОСТЬ. Все сравнения этапа строятся на
допущении, что тринадцать вариантов отличаются ТОЛЬКО правилом выхода, а правила
касания у них одни. Проверить это допущение можно ровно одним способом: посчитать
контрольный вариант тем же кодом, что и Этап 8.8, и убедиться, что строки совпали
до последнего знака. Не совпали — сравнивать нельзя, и расчёт обязан сообщить об
этом кодом возврата, а не строкой в середине журнала.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.barrier.runner import pick_series
from src.core.config import settings
from src.core.db import db
from src.trailing.rule import (
    VARIANTS,
    TrailingOutcome,
    resolve_all,
    variant_label,
)

_log = structlog.get_logger().bind(component="trailing_outcomes")


@dataclass
class VariantStat:
    """Итог по одному варианту — для журнала и отчёта."""

    activation_ratio: float
    retrace_ratio: float
    written: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def count(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    @property
    def label(self) -> str:
        return variant_label(self.activation_ratio, self.retrace_ratio)


@dataclass
class RunStats:
    """Итог прогона целиком, включая замер производительности (§7 ТЗ)."""

    pairs: int = 0
    pairs_done_before: int = 0
    windows_read: int = 0
    deleted: int = 0
    seconds: float = 0.0
    control: dict[str, int] = field(default_factory=dict)
    per_variant: dict[tuple[float, float], VariantStat] = field(default_factory=dict)

    def stat(self, activation_ratio: float, retrace_ratio: float) -> VariantStat:
        key = (activation_ratio, retrace_ratio)
        return self.per_variant.setdefault(
            key,
            VariantStat(activation_ratio=activation_ratio, retrace_ratio=retrace_ratio),
        )

    @property
    def written(self) -> int:
        return sum(s.written for s in self.per_variant.values())

    @property
    def control_ok(self) -> bool:
        """Совпал ли контрольный вариант с Этапом 8.8 (§4 ТЗ).

        Пустая сверка (нечего сравнивать) НЕ считается совпадением — она
        считается отсутствием сверки, и вывод «правила совпали» из неё не
        следует. Отвечает за это вызывающий код: ``compared`` печатается рядом.
        """
        return (
            bool(self.control)
            and self.control.get("mismatched", 0) == 0
            and self.control.get("missing", 0) == 0
        )


def build_rows(
    anchor: dict[str, Any],
    results: list[TrailingOutcome],
    *,
    computed_at: datetime,
) -> list[dict[str, Any]]:
    """Строки ``trailing_outcomes`` по одной паре и посчитанным вариантам.

    Входные числа (цена, цель, предел, издержки) переносятся ИЗ КАНДИДАТА, то
    есть из строки Этапа 8.8, а не берутся из настроек заново: сравнение
    действительно только при одинаковых входных числах.
    """
    return [
        {
            "signal_id": anchor["signal_id"],
            "horizon_h": anchor["horizon_h"],
            "activation_ratio": item.activation_ratio,
            "retrace_ratio": item.retrace_ratio,
            "logic_version": anchor["logic_version"],
            "direction": anchor["direction"],
            "price_at_signal": anchor["price_at_signal"],
            "target_pct": anchor["target_pct"],
            "stop_pct": anchor["stop_pct"],
            "cost_pct": anchor["cost_pct"],
            "exit_reason": item.exit_reason,
            "hit_at": item.hit_at,
            "bars_to_hit": item.bars_to_hit,
            "net_pnl_pct": item.net_pnl_pct,
            "peak_pct": item.peak_pct,
            "mae_pct": item.mae_pct,
            "mfe_pct": item.mfe_pct,
            "resolution": item.resolution,
            "computed_at": computed_at,
        }
        for item in results
    ]


async def compute_pair(
    anchor: dict[str, Any], *, computed_at: datetime, stats: RunStats
) -> None:
    """Одна пара: одно чтение окна, тринадцать вариантов, одна запись пачкой."""
    bars, resolution = await pick_series(
        instrument_id=anchor["instrument_id"],
        signal_ts=anchor["ts"],
        horizon_h=anchor["horizon_h"],
    )
    stats.windows_read += 1

    results = resolve_all(
        bars,
        signal_ts=anchor["ts"],
        horizon_h=anchor["horizon_h"],
        price_at_signal=float(anchor["price_at_signal"]),
        target_pct=float(anchor["target_pct"]),
        stop_pct=float(anchor["stop_pct"]),
        cost_pct=float(anchor["cost_pct"]),
        direction=anchor["direction"],
        resolution=resolution,
    )
    await db.save_trailing_outcomes(
        build_rows(anchor, results, computed_at=computed_at)
    )
    for item in results:
        stat = stats.stat(item.activation_ratio, item.retrace_ratio)
        stat.written += 1
        stat.count(item.exit_reason)


async def compute(
    *,
    since: datetime | None = None,
    limit: int | None = None,
    recompute: bool = False,
    now: datetime | None = None,
) -> RunStats:
    """Полный расчёт по всем парам Этапа 8.8."""
    now = now or datetime.now(UTC)
    computed_at = now
    await db.ensure_trailing_schema()

    logic_version = settings.LOGIC_VERSION
    stats = RunStats()
    started = time.monotonic()

    if recompute:
        stats.deleted = await db.delete_trailing_outcomes(logic_version=logic_version)

    done = await db.get_trailing_pairs_done(
        logic_version=logic_version, variants=len(VARIANTS)
    )
    anchors = await db.get_trailing_anchors(
        logic_version=logic_version, since=since, limit=limit
    )
    stats.pairs = len(anchors)

    for anchor in anchors:
        key = (int(anchor["signal_id"]), int(anchor["horizon_h"]))
        if key in done:
            stats.pairs_done_before += 1
            continue
        await compute_pair(anchor, computed_at=computed_at, stats=stats)

    stats.seconds = time.monotonic() - started
    stats.control = await db.check_trailing_control(logic_version=logic_version)

    for key in sorted(stats.per_variant):
        stat = stats.per_variant[key]
        _log.info(
            "trailing_variant_done=1",
            activation_ratio=stat.activation_ratio,
            retrace_ratio=stat.retrace_ratio,
            variant=stat.label,
            written=stat.written,
            **{f"reason_{k}": v for k, v in sorted(stat.by_reason.items())},
        )

    _log.info(
        "trailing_compute_done=1",
        pairs=stats.pairs,
        pairs_done_before=stats.pairs_done_before,
        windows_read=stats.windows_read,
        variants=len(VARIANTS),
        written=stats.written,
        deleted=stats.deleted,
        seconds=round(stats.seconds, 3),
        rows_per_second=(
            round(stats.written / stats.seconds, 1) if stats.seconds > 0 else None
        ),
        control_compared=stats.control.get("compared"),
        control_mismatched=stats.control.get("mismatched"),
        control_missing=stats.control.get("missing"),
        computed_at=computed_at.isoformat(),
    )

    if stats.control.get("compared", 0) == 0:
        # Сверять было нечего. Это НЕ «совпало»: вывод о совпадении правил из
        # пустой сверки не следует, и молчание сделало бы эти два состояния
        # неотличимыми в журнале.
        _log.warning(
            "trailing_control_unchecked=1",
            detail="контрольный вариант не с чем сверять: строк 8.8 нет",
        )
    elif not stats.control_ok:
        # БЛОКИРУЮЩЕЕ по §4 ТЗ: правила касания разошлись, и недействительно
        # ВСЁ сравнение вариантов, а не только контрольная строка.
        _log.error(
            "trailing_control_mismatch=1",
            compared=stats.control.get("compared"),
            mismatched=stats.control.get("mismatched"),
            missing=stats.control.get("missing"),
            detail=(
                "контрольный вариант разошёлся с signal_outcomes_barrier — "
                "сравнение вариантов недействительно"
            ),
        )
    return stats


async def run(**kwargs: Any) -> RunStats:
    """Точка входа сценария: своё подключение к БД, своё закрытие."""
    await db.connect()
    try:
        return await compute(**kwargs)
    finally:
        await db.close()


__all__ = ["RunStats", "VariantStat", "build_rows", "compute", "run"]
