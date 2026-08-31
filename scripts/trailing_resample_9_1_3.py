#!/usr/bin/env python3
"""ЧАСТЬ А Этапа 9.1.3: пересчёт замера 8.10 на ВЫРОСШЕЙ выборке. ТОЛЬКО ЧТЕНИЕ.

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ. Устояла ли единственная устойчивая закономерность
Этапа 8.10 — короткий откат R=0.20 лучше длинного, монотонно по обеим осям — на
данных, КОТОРЫХ ПРИ ЕЁ ОБНАРУЖЕНИИ НЕ СУЩЕСТВОВАЛО. Закономерность найдена на
двухсуточной выборке одного падающего рынка; всё, что посчитано после, — это
единственная независимая проверка, которая у проекта есть.

ЭТО НЕ ЧАСТЬ Б И НЕ СКЛАДЫВАЕТСЯ С НЕЙ (§1 ТЗ). Здесь десятки тысяч
ГИПОТЕТИЧЕСКИХ пар «сигнал × горизонт» без порога вероятности; там десяток
реальных позиций с порогом 0.8. Это разные вопросы, и ни одной сводной цифры по
обеим частям не печатается.

СКРИПТ НИЧЕГО НЕ СЧИТАЕТ ЗАНОВО И НИЧЕГО НЕ ЗАПИСЫВАЕТ. Расчёт Этапа 8.10
(``src/trailing/*``, точка входа ``python -m src.trailing_main``, таблица
``trailing_outcomes``) НЕ ИЗМЕНЁН НИ ОДНОЙ СТРОКОЙ и запускается как есть, своим
ночным расписанием. Здесь только чтение уже посчитанного.

СЕТКА ТА ЖЕ — 4×3 ПЛЮС КОНТРОЛЬ, И РАСШИРЯТЬ ЕЁ ЗАПРЕЩЕНО (§2.4 ТЗ). Новые
значения A и R превратили бы независимую проверку старой находки в новый
перебор, и «победитель» нашёлся бы снова — как он находится всегда, когда
вариантов больше одного.

ТРИ ЗАЩИТЫ ОТ ПОДГОНКИ БЕРУТСЯ ИЗ ``scripts/trailing_stats.py`` ВЫЗОВОМ, а не
переписываются: перестановочная проверка, парный бутстрэп и деление выборки —
это тот же вопрос о том же, и второй экземпляр той же арифметики однажды
разошёлся бы с первым. Здесь добавлено только то, чего в 8.10 не было: разбиение
выборки на старую и новую часть и ВТОРОЙ ответ проверки на независимой половине.

ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ ЗАПРЕЩЁН ПРЯМО (§0 ТЗ). Рекомендация «внедрить
A=…, R=…» является нарушением ТЗ, а не полезной инициативой.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА. Каталог ``scripts/`` попадает только в образ
``backtest`` (§11.4 ТЗ):

    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/trailing_resample_9_1_3.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from scripts.trailing_stats import (  # noqa: E402
    BOOTSTRAP_SEED,
    MIN_PAIRS,
    PERMUTATION_SEED,
    RESAMPLES,
    VERDICT_INDISTINGUISHABLE,
    VERDICT_NOT_CONFIRMED,
    best_variant,
    bootstrap_diff,
    collect,
    matrix,
    spread_permutation,
    verdict,
)
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.trailing.rule import (  # noqa: E402
    ACTIVATION_RATIOS,
    FIXED_VARIANT,
    RETRACE_RATIOS,
    VARIANTS,
    variant_label,
)

# Граница «старой» и «новой» части выборки (§2.3 ТЗ). Закономерность 8.10
# обнаружена на том, что было посчитано ДО этого момента.
BOUNDARY = datetime(2026, 8, 29, tzinfo=UTC)

FIXED_INDEX = VARIANTS.index(FIXED_VARIANT)


def pair_computed_at(rows: list[dict[str, Any]]) -> dict[tuple[int, int], datetime]:
    """Момент расчёта пары — САМЫЙ РАННИЙ среди её тринадцати строк.

    ПОЧЕМУ ГРАНИЦА ПРОВОДИТСЯ ПО ``computed_at``, А НЕ ПО ВРЕМЕНИ СИГНАЛА. Вопрос
    §2.3 — «какие данные появились ПОСЛЕ того, как закономерность была найдена».
    Пара становится посчитанной не тогда, когда пришёл сигнал, а когда истёк её
    горизонт и расчёт до неё добрался: сигнал от 28.08 с горизонтом 24 часа
    посчитан 29-го или позже, и в находке 8.10 он не участвовал, хотя его
    ``signals.ts`` лежит до границы.

    Запись 8.10 идёт с ``ON CONFLICT DO NOTHING``, поэтому у уже посчитанной
    строки ``computed_at`` повторным прогоном НЕ сдвигается. Единственное, что
    может его обнулить, — принудительный пересчёт (``--recompute``), и такой
    случай виден сразу: старая часть окажется пустой. Скрипт это проверяет и
    говорит вслух, а не печатает пустую таблицу как результат.

    Обе разбивки печатаются рядом в БЛОКЕ 1, чтобы выбор границы можно было
    проверить глазами, а не принять на веру.
    """
    out: dict[tuple[int, int], datetime] = {}
    for row in rows:
        key = (int(row["signal_id"]), int(row["horizon_h"]))
        moment = row["computed_at"]
        if key not in out or moment < out[key]:
            out[key] = moment
    return out


def split_rows(
    rows: list[dict[str, Any]], moments: dict[tuple[int, int], datetime]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Строки старой и новой части. Делится ПАРА целиком, а не отдельные строки.

    Разорвать пару между половинами нельзя: у неё тринадцать вариантов, и они
    обязаны попасть в одну и ту же таблицу, иначе столбцы окажутся посчитаны на
    разных наборах пар.
    """
    old: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    for row in rows:
        key = (int(row["signal_id"]), int(row["horizon_h"]))
        (old if moments[key] < BOUNDARY else new).append(row)
    return old, new


def deltas_grid(values: np.ndarray) -> dict[tuple[float, float], float]:
    """Средний прирост каждого варианта против контроля, в п.п. на сделку."""
    means = values.mean(axis=0)
    base = means[FIXED_INDEX]
    return {
        variant: float(means[VARIANTS.index(variant)] - base)
        for variant in VARIANTS
        if variant != FIXED_VARIANT
    }


def print_grid(title: str, pairs: list[dict[str, Any]]) -> None:
    """Таблица 4×3: A по столбцам, R по строкам, в п.п. на сделку.

    Пустая часть выборки печатается словами, а не пустой сеткой: сетка из
    прочерков выглядит как посчитанный результат.
    """
    print()
    print(f"  {title}")
    if not pairs:
        print("    (пар нет — считать нечего)")
        return
    grid = deltas_grid(matrix(pairs))
    print(f"    пар: {len(pairs)}")
    header = f"    {'R \\ A':<8}"
    for activation in ACTIVATION_RATIOS:
        header += f"{activation:>12.2f}"
    print(header)
    print("    " + "-" * (8 + 12 * len(ACTIVATION_RATIOS)))
    for retrace in RETRACE_RATIOS:
        line = f"    {retrace:<8.2f}"
        for activation in ACTIVATION_RATIOS:
            line += f"{grid[(activation, retrace)]:>+12.4f}"
        print(line)


def print_composition(rows: list[dict[str, Any]],
                      moments: dict[tuple[int, int], datetime]) -> None:
    """БЛОК 1 §2.3: состав выборки.

    ``logic_version`` печатается разбивкой, и больше одного значения — ЭТО
    ПРЕДУПРЕЖДЕНИЕ ОТДЕЛЬНОЙ КРУПНОЙ СТРОКОЙ. Смешение версий логики в одном
    сравнении — известная в проекте причина ложных выводов: разные версии
    отбирают разные сигналы, и разница между вариантами отражала бы состав
    выборки, а не правило выхода.
    """
    keys = set(moments)
    old_keys = {k for k, m in moments.items() if m < BOUNDARY}
    print()
    print("-" * 78)
    print(" БЛОК 1. СОСТАВ ВЫБОРКИ")
    print("-" * 78)
    print(f"  всего пар: {len(keys)}")
    print(f"    посчитано до {BOUNDARY:%Y-%m-%d} UTC: {len(old_keys)}")
    print(f"    посчитано с  {BOUNDARY:%Y-%m-%d} UTC: {len(keys) - len(old_keys)}")

    by_signal_ts = {
        k for k in keys
        if min(r["ts"] for r in rows
               if (int(r["signal_id"]), int(r["horizon_h"])) == k) < BOUNDARY
    } if len(keys) <= 5000 else None
    if by_signal_ts is not None:
        print(f"    (для сверки, по времени СИГНАЛА до границы: {len(by_signal_ts)},")
        print(f"     с границы: {len(keys) - len(by_signal_ts)})")

    horizons: dict[int, set[tuple[int, int]]] = {}
    tokens: dict[str, set[tuple[int, int]]] = {}
    versions: dict[int, int] = {}
    for row in rows:
        key = (int(row["signal_id"]), int(row["horizon_h"]))
        horizons.setdefault(int(row["horizon_h"]), set()).add(key)
        tokens.setdefault(str(row["token"]), set()).add(key)
        versions[int(row["logic_version"])] = versions.get(
            int(row["logic_version"]), 0
        ) + 1

    print("  по горизонтам: " + " / ".join(
        f"{h}ч: {len(v)}" for h, v in sorted(horizons.items())
    ))
    print("  по инструментам: " + " / ".join(
        f"{t}: {len(v)}" for t, v in sorted(tokens.items())
    ))
    print("  logic_version: " + " / ".join(
        f"{v}: {n}" for v, n in sorted(versions.items())
    ))
    if len(versions) > 1:
        print()
        print("  " + "!" * 70)
        print("  !!  В ВЫБОРКЕ БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ. СРАВНЕНИЕ ВАРИАНТОВ")
        print("  !!  НА СМЕШАННЫХ ВЕРСИЯХ НЕДЕЙСТВИТЕЛЬНО: разные версии")
        print("  !!  отбирают разные сигналы, и разница между вариантами будет")
        print("  !!  отражать состав выборки, а не правило выхода.")
        print("  " + "!" * 70)

    if not old_keys:
        print()
        print("  ВНИМАНИЕ: старая часть выборки ПУСТА. Скорее всего был сделан")
        print("  принудительный пересчёт (--recompute), сбросивший computed_at у")
        print("  всех строк. Тогда разделение на старую и новую часть смысла не")
        print("  имеет, и третья таблица НЕ является независимой проверкой.")


def print_defences(values: np.ndarray) -> None:
    """БЛОК 3 §2.3: три защиты от подгонки, на ВСЕЙ выборке.

    Считаются теми же функциями, что в Этапе 8.10: ``spread_permutation``,
    ``bootstrap_diff`` и деление выборки. Формулировки вердиктов взяты оттуда же
    дословно — два разных слова для одного состояния читались бы как два разных
    состояния.
    """
    print()
    print("-" * 78)
    print(" БЛОК 3. ТРИ ЗАЩИТЫ ОТ ПОДГОНКИ (вся выборка)")
    print("-" * 78)

    observed, threshold, _cloud = spread_permutation(
        values, resamples=RESAMPLES, seed=PERMUTATION_SEED
    )
    print()
    print(" 1. РАЗБРОС ПРОТИВ СЛУЧАЙНОГО")
    print(f"    перестановок: {RESAMPLES}, зерно {PERMUTATION_SEED}")
    print(f"    наблюдённый разброс средних: {observed:.4f} п.п.")
    print(f"    95-й процентиль случайного:  {threshold:.4f} п.п.")
    if observed <= threshold:
        print(f"    → {VERDICT_INDISTINGUISHABLE}")
    else:
        print("    → разброс выходит за случайный: варианты различимы.")
        print("      Это НЕ означает, что различие полезно, — только что оно есть.")

    print()
    print(" 2. ИНТЕРВАЛЫ РАЗНИЦЫ С КОНТРОЛЕМ, все 12 вариантов")
    print(f"    пересборок: {RESAMPLES}, зерно {BOOTSTRAP_SEED}, "
          f"пар {values.shape[0]}")
    if values.shape[0] < MIN_PAIRS:
        print(f"    В выборке меньше {MIN_PAIRS} пар — интервал не считается:")
        print("    он описывал бы эти несколько чисел, а не рынок.")
        return
    print()
    print(f"    {'вариант':<22}{'разница,п.п.':>14}{'интервал 95%':>26}"
          f"{'пересекает 0':>15}")
    print("    " + "-" * 76)
    for variant in VARIANTS:
        if variant == FIXED_VARIANT:
            continue
        index = VARIANTS.index(variant)
        mean, lo, hi = bootstrap_diff(
            values[:, index], values[:, FIXED_INDEX],
            resamples=RESAMPLES, seed=BOOTSTRAP_SEED,
        )
        crosses = "пересекает" if lo <= 0.0 <= hi else "НЕ пересекает"
        print(
            f"    {variant_label(*variant):<22}{mean:>+14.4f}"
            f"{f'[{lo:+.4f}; {hi:+.4f}]':>26}{crosses:>15}"
        )
    print()
    print("    Интервал варианта, ВЫБРАННОГО ПО ЭТИМ ЖЕ ДАННЫМ, смещён в его")
    print("    пользу. Поэтому напечатаны все двенадцать, а не один.")


def print_split_half_two_answers(values: np.ndarray) -> None:
    """БЛОК 4 §2.3: проверка на независимой половине — ДВА ответа, а не один.

    ПОЧЕМУ ДВА. 29.08.2026 выяснилось, что формулировка ТЗ 8.10 («если
    победитель первой половины не побеждает на второй») была реализована
    буквально — сравнением МЕСТ. Победитель первой половины дал на второй
    +0.0224% против своих +0.0270%: сменилась не сторона, а порядок мест. Ответ
    о местах строгий, ответ о стороне содержательный, и полезны оба — печатать
    один вместо двух значит выдать одну проверку за другую.
    """
    n = values.shape[0]
    print()
    print("-" * 78)
    print(" БЛОК 4. ПРОВЕРКА НА НЕЗАВИСИМОЙ ПОЛОВИНЕ — ДВА ОТВЕТА")
    print("-" * 78)
    half = n // 2
    if half < MIN_PAIRS:
        print(f"  В половине меньше {MIN_PAIRS} пар ({half}) — проверка не выполнена.")
        print("  Это НЕ «преимущество подтверждено»: проверять было нечем.")
        return

    first, second = values[:half], values[half:]
    winner = best_variant(first)
    winner_second = best_variant(second)
    gain_first = float(first[:, winner].mean() - first[:, FIXED_INDEX].mean())
    gain_second = float(second[:, winner].mean() - second[:, FIXED_INDEX].mean())
    mean, lo, hi = bootstrap_diff(
        second[:, winner], second[:, FIXED_INDEX],
        resamples=RESAMPLES, seed=BOOTSTRAP_SEED,
    )

    print(f"  Деление по времени: первая половина {half} пар, вторая {n - half}.")
    print(f"  Победитель первой половины: {variant_label(*VARIANTS[winner])} "
          f"({gain_first:+.4f}%)")
    print(f"  Победитель второй половины: {variant_label(*VARIANTS[winner_second])}")
    print()
    print("  ОТВЕТ 1 — СОВПАДЕНИЕ МЕСТ (строгий):")
    if winner_second == winner:
        print("    победитель первой половины СОВПАЛ с победителем второй.")
    else:
        print("    победитель первой половины НЕ СОВПАЛ с победителем второй.")
        print(f"    → {VERDICT_NOT_CONFIRMED}")
    print()
    print("  ОТВЕТ 2 — СОХРАНЕНИЕ СТОРОНЫ (содержательный):")
    print(f"    победитель первой половины на второй даёт {gain_second:+.4f}%,")
    print(f"    интервал [{lo:+.4f}; {hi:+.4f}] — {verdict(lo, hi)}")
    if gain_second > 0.0:
        print("    → СОХРАНЯЕТ положительное преимущество (по знаку среднего).")
    else:
        print("    → НЕ СОХРАНЯЕТ положительного преимущества.")
    print(f"    (среднее разницы по бутстрэпу: {mean:+.4f}%)")
    print()
    print("  Две половины взяты из ОДНОГО отрезка рынка. Совпадение здесь не")
    print("  доказывает устойчивости — оно лишь не опровергает её.")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Пересчёт замера подвижного выхода 8.10 на выросшей выборке "
            "(Этап 9.1.3, часть А). Только чтение."
        )
    )
    parser.parse_args()

    await db.connect()
    try:
        rows = await db.get_trailing_resample_rows()
    finally:
        await db.close()

    print("=" * 78)
    print(" ЧАСТЬ А. ПЕРЕСЧЁТ ЗАМЕРА 8.10 НА ВЫРОСШЕЙ ВЫБОРКЕ (9.1.3)")
    print("=" * 78)
    print(f"  Строк trailing_outcomes прочитано: {len(rows)}")
    print(f"  LOGIC_VERSION настройки: {settings.LOGIC_VERSION}")

    if not rows:
        print()
        print("  Таблица trailing_outcomes пуста: считать нечего.")
        print("  Запустите расчёт 8.10: python -m src.trailing_main")
        return 3

    moments = pair_computed_at(rows)
    print_composition(rows, moments)

    old_rows, new_rows = split_rows(rows, moments)
    pairs_all, dropped = collect(rows)
    pairs_old, _ = collect(old_rows)
    pairs_new, _ = collect(new_rows)

    print()
    print("-" * 78)
    print(" БЛОК 2. СРЕДНИЙ ПРИРОСТ ПРОТИВ КОНТРОЛЯ, п.п. на сделку")
    print("-" * 78)
    print("  Исключено пар: " + ", ".join(
        f"{reason}: {count}" for reason, count in sorted(dropped.items())
    ))
    print("  (пара идёт в сравнение, только если итог определён у ВСЕХ")
    print("   тринадцати вариантов: иначе столбцы считались бы на разных")
    print("   выборках, и разница отражала бы состав, а не правило выхода)")
    print_grid("ВСЯ ВЫБОРКА", pairs_all)
    print_grid(f"ТОЛЬКО СТАРАЯ ЧАСТЬ (посчитано до {BOUNDARY:%d.%m.%Y})", pairs_old)
    print_grid(
        f"ТОЛЬКО НОВАЯ ЧАСТЬ (посчитано с {BOUNDARY:%d.%m.%Y}) — "
        "НЕЗАВИСИМАЯ ПРОВЕРКА", pairs_new
    )
    print()
    print("  Третья таблица — единственная независимая проверка находки 8.10:")
    print("  этих данных при её обнаружении не существовало.")

    if not pairs_all:
        print()
        print("  Ни одной полной пары — защиты от подгонки не считаются.")
        return 0

    values = matrix(pairs_all)
    print_defences(values)
    print_split_half_two_answers(values)

    print()
    print("=" * 78)
    print("  ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ (§0 ТЗ).")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
