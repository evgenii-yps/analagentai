#!/usr/bin/env python3
"""Доверительный интервал разницы «система против базовой стратегии» (§8 ТЗ 8.9).

ЗАЧЕМ ЭТОТ СКРИПТ СУЩЕСТВУЕТ, и почему одной таблицы средних недостаточно.
Разница средних всегда чем-нибудь да отличается от нуля. На выборке в двое
суток она отличается от нуля даже тогда, когда никакой разницы нет: числа
пляшут просто потому, что сигналов мало. Отчёт, в котором написано «система
лучше на 0.3%», без оценки этой пляски создаёт уверенность, которой у данных
нет, — и решения принимаются по шуму.

Поэтому здесь считается не разница, а ИНТЕРВАЛ, в котором она правдоподобно
лежит. Метод — бутстрэп: выборка пересобирается BOOTSTRAP_RESAMPLES раз с
возвращением, каждый раз считается разница средних, и берутся 2.5-й и 97.5-й
процентили полученного облака. Это 95% интервал.

ТРИ ВОЗМОЖНЫХ ОТВЕТА, И ТРЕТИЙ — САМЫЙ ЧАСТЫЙ И САМЫЙ ЧЕСТНЫЙ:

    «система лучше, интервал не пересекает ноль»
    «система хуже, интервал не пересекает ноль»
    «различить нельзя, выборки не хватает»

Третья формулировка на нынешнем объёме ОЖИДАЕМА. Она не означает, что система
плоха или хороша: она означает, что данных пока не хватает, чтобы отличить одно
от другого. Заменять её числом без интервала запрещено (§8 ТЗ).

ПОЧЕМУ БУТСТРЭП ПАРНЫЙ. Система и базовая стратегия стоят на ОДНИХ И ТЕХ ЖЕ
сигналах, в одни и те же моменты, на одном и том же рынке. Пересобирать их
независимо значило бы выбросить это совпадение и получить интервал ШИРЕ
настоящего — то есть объявить «различить нельзя» там, где различить можно.
Пересобираются ПАРЫ целиком: взяли пару — взяли обе её стороны.

ЧТО В СРАВНЕНИЕ НЕ ВХОДИТ, и это не упущение:
  * grid_buy и grid_sell — у них нет общих пар с системой по построению
    (они входят каждый час независимо от сигналов). Их числа показывает
    раздел 9.6 запроса, но вычитать их из системы нельзя;
  * пары, где хоть одна сторона дала ambiguous или no_data — там неизвестно,
    что произошло, и разность с неизвестным не определена.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА (правило D-3 — пакетов на хосте нет):
    docker compose --profile tools run --rm --no-deps barrier \\
        python -m scripts.baseline_bootstrap
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402

# Число пересборок (§8 ТЗ). Десять тысяч — не «побольше для верности», а
# компромисс: этого достаточно, чтобы границы 95% интервала перестали заметно
# гулять от запуска к запуску, и мало настолько, чтобы расчёт занимал секунды.
BOOTSTRAP_RESAMPLES = 10_000
CONFIDENCE = 0.95

# Зерно бутстрэпа. Отдельное от BASELINE_SEED: то определяет монету (часть
# ИЗМЕРЯЕМОГО), это — только пересборки выборки (часть ИЗМЕРЕНИЯ). Смешивать
# их значило бы связать результат линейки со способом её поверки.
BOOTSTRAP_SEED = 8_9_2026

VERDICT_BETTER = "система лучше, интервал не пересекает ноль"
VERDICT_WORSE = "система хуже, интервал не пересекает ноль"
VERDICT_UNKNOWN = "различить нельзя, выборки не хватает"

# Минимум пар, ниже которого интервал не считается вовсе. На трёх наблюдениях
# бутстрэп даёт интервал, но это интервал ни о чём: он описывает три числа, а
# не рынок. Печатать его значило бы придать форму измерения тому, что им
# не является.
MIN_PAIRS = 20

QUERY = """
    SELECT b.strategy,
           b.horizon_h,
           s.direction  AS sys_direction,
           (sig.notified_at IS NOT NULL) AS notified,
           b.net_pnl_pct::float8 AS base_pnl,
           s.net_pnl_pct::float8 AS sys_pnl
    FROM strategy_outcomes b
    JOIN strategy_outcomes s
      ON s.strategy = 'system'
     AND s.signal_id = b.signal_id
     AND s.horizon_h = b.horizon_h
     AND s.logic_version = b.logic_version
    JOIN signals sig ON sig.id = b.signal_id
    WHERE b.logic_version = $1
      AND b.strategy <> 'system'
      AND b.signal_id IS NOT NULL
      AND b.net_pnl_pct IS NOT NULL
      AND s.net_pnl_pct IS NOT NULL;
"""


def bootstrap_diff(
    sys_pnl: np.ndarray, base_pnl: np.ndarray, *, resamples: int, seed: int
) -> tuple[float, float, float]:
    """``(разница средних, нижняя граница, верхняя граница)`` 95% интервала.

    Пересобираются ИНДЕКСЫ ПАР, а не два ряда по отдельности: пара — это одно
    наблюдение, у которого две стороны.
    """
    if sys_pnl.shape != base_pnl.shape:
        raise ValueError("стороны пары обязаны быть одной длины")
    n = sys_pnl.shape[0]
    if n == 0:
        raise ValueError("пустая выборка: интервал не определён")
    diff = sys_pnl - base_pnl
    observed = float(diff.mean())

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    means = diff[idx].mean(axis=1)
    lo_q = (1.0 - CONFIDENCE) / 2.0 * 100.0
    hi_q = (1.0 + CONFIDENCE) / 2.0 * 100.0
    lo, hi = np.percentile(means, [lo_q, hi_q])
    return observed, float(lo), float(hi)


def verdict(lo: float, hi: float) -> str:
    """Одна из трёх формулировок §8. Дословно, без вариаций."""
    if lo > 0.0:
        return VERDICT_BETTER
    if hi < 0.0:
        return VERDICT_WORSE
    return VERDICT_UNKNOWN


def _line(label: str, rows: list[dict[str, Any]]) -> str:
    """Строка отчёта по одной группе сравнения."""
    n = len(rows)
    if n < MIN_PAIRS:
        return (
            f"  {label:<46} пар: {n:>6}  "
            f"— {VERDICT_UNKNOWN} (пар меньше {MIN_PAIRS})"
        )
    sys_pnl = np.array([r["sys_pnl"] for r in rows], dtype=float)
    base_pnl = np.array([r["base_pnl"] for r in rows], dtype=float)
    observed, lo, hi = bootstrap_diff(
        sys_pnl, base_pnl, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED
    )
    # ВЫРОЖДЕННЫЙ СЛУЧАЙ, который иначе читался бы неверно. На сигналах
    # «покупать» стратегия always_buy — это ТА ЖЕ САМАЯ сделка, что у системы:
    # то же направление, та же цена, та же замороженная цель. Разница там равна
    # нулю не потому, что данных мало, а потому, что сравнивать нечего.
    # Формулировка вердикта остаётся одной из трёх, предписанных ТЗ, но рядом
    # обязана стоять причина — иначе «различить нельзя» прочтут как «данных не
    # хватает», хотя данные здесь идеальны.
    note = ""
    if float(np.abs(sys_pnl - base_pnl).max()) == 0.0:
        note = "  (стороны совпадают по построению: это одна и та же сделка)"
    return (
        f"  {label:<46} пар: {n:>6}  "
        f"разница средних: {observed:+.4f}%  "
        f"интервал 95%: [{lo:+.4f}; {hi:+.4f}]\n"
        f"  {'':<46} → {verdict(lo, hi)}{note}"
    )


def _group(rows: list[dict[str, Any]], *keys: str) -> dict[tuple, list[dict[str, Any]]]:
    out: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(tuple(row[k] for k in keys), []).append(row)
    return out


async def main() -> int:
    await db.connect()
    try:
        rows = [dict(r) for r in await db.pool.fetch(
            QUERY, int(settings.LOGIC_VERSION)
        )]
    finally:
        await db.close()

    print("=" * 78)
    print(" ЭТАП 8.9 §8. СИСТЕМА ПРОТИВ БАЗОВЫХ СТРАТЕГИЙ")
    print(f" Бутстрэп: {BOOTSTRAP_RESAMPLES} пересборок, интервал "
          f"{int(CONFIDENCE * 100)}%, зерно {BOOTSTRAP_SEED}")
    print(f" Версия логики: {settings.LOGIC_VERSION}. Пар в сравнении: {len(rows)}")
    print("=" * 78)

    if not rows:
        print()
        print("  Общих созревших пар нет — сравнивать нечего.")
        print("  Это не результат сравнения, а его отсутствие: сначала должен")
        print("  отработать расчёт базовых стратегий (python -m src.baseline_main).")
        return 0

    print()
    print(" ВСЕ ГОРИЗОНТЫ ВМЕСТЕ")
    for (strategy,), group in sorted(_group(rows, "strategy").items()):
        print(_line(f"система против {strategy}", group))

    print()
    print(" ПО ГОРИЗОНТАМ")
    for (horizon, strategy), group in sorted(_group(rows, "horizon_h", "strategy").items()):
        print(_line(f"{horizon}ч: система против {strategy}", group))

    print()
    print(" ПО НАПРАВЛЕНИЮ СИГНАЛА СИСТЕМЫ")
    for (direction, strategy), group in sorted(
        _group(rows, "sys_direction", "strategy").items()
    ):
        print(_line(f"{direction}: система против {strategy}", group))

    print()
    print(" ПО ПРИЗНАКУ «СИГНАЛ ОТПРАВЛЕН ЧЕЛОВЕКУ»")
    for (notified, strategy), group in sorted(
        _group(rows, "notified", "strategy").items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
    ):
        label = "отправлен" if notified else "не отправлен"
        print(_line(f"{label}: система против {strategy}", group))

    print()
    print("-" * 78)
    print(" КАК ЭТО ЧИТАТЬ. Интервал — это диапазон, в котором правдоподобно")
    print(" лежит настоящая разница. Пересекает ноль — значит, данные пока не")
    print(" позволяют сказать, есть разница или нет. Это НЕ означает, что")
    print(" разницы нет; это означает, что наблюдений мало.")
    print()
    print(" Стратегии grid_buy и grid_sell здесь отсутствуют намеренно: у них")
    print(" нет общих пар с системой, и вычитать их из неё нельзя. Их числа —")
    print(" в разделе 9.6 запроса analysis/sql/09_baseline_compare.sql.")
    print("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
