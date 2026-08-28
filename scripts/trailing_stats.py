#!/usr/bin/env python3
"""Таблица вариантов выхода и ТРИ ЗАЩИТЫ ОТ ПОДГОНКИ (§5 ТЗ 8.10).

ГЛАВНОЕ, ЧТО НАДО ПОНЯТЬ ПРО ЭТОТ СКРИПТ. Двенадцать вариантов подвижного
выхода, посчитанные на двухсуточной выборке одного падающего рынка,
ГАРАНТИРОВАННО дадут «победителя» — даже если все двенадцать правил
одинаково бессмысленны. Возьмите двенадцать монет, бросьте каждую сто раз,
и одна выпадет орлом чаще прочих; назвать её «лучшей монетой» — ровно та
ошибка, против которой написан §5 ТЗ.

Поэтому здесь считается не «кто победил», а три вопроса О САМОМ СРАВНЕНИИ:

  1. РАЗБРОС. Отличается ли лучший вариант от худшего сильнее, чем варианты
     отличались бы друг от друга ПО ЧИСТОЙ СЛУЧАЙНОСТИ? Случайный разброс
     считается перестановочной проверкой: внутри каждой пары ярлыки вариантов
     перемешиваются, и смотрится, какой разброс средних получается, когда
     правило выхода заведомо ни на что не влияет. Если наблюдённый разброс не
     выходит за 95-й процентиль этого облака — печатается дословно:
     «варианты неразличимы, победитель случаен».

  2. ИНТЕРВАЛ. Насколько лучший вариант обгоняет фиксированную цель и где
     правдоподобно лежит эта разница. Метод — парный бутстрэп, 10000
     пересборок, интервал 95%, три формулировки как в Этапе 8.9.
     ВНИМАНИЕ, И ЭТО ВАЖНЕЕ САМОГО ЧИСЛА: интервал для варианта, ВЫБРАННОГО
     ПО ЭТИМ ЖЕ ДАННЫМ, смещён в его пользу. Он напечатан не как доказательство
     преимущества, а как верхняя оценка того, насколько преимущество вообще
     может быть большим. Рядом печатаются интервалы ВСЕХ двенадцати вариантов —
     их смотреть честнее.

  3. НЕЗАВИСИМАЯ ПОЛОВИНА. Выборка делится надвое по времени. Победитель первой
     половины проверяется на второй. Не победил — печатается дословно:
     «преимущество не подтверждается на независимых данных».

ВЫБОР «ЛУЧШЕГО» ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ ЗАПРЕЩЁН (§5.4 ТЗ). Скрипт не выдаёт
рекомендаций и не называет вариант, который «надо поставить». Задача этапа —
таблица, а не решение.

ЧТО В СРАВНЕНИЕ НЕ ВХОДИТ, и это не упущение: пары, где ХОТЬ ОДИН из
тринадцати вариантов дал ``ambiguous`` или ``no_data``. Там неизвестно, что
произошло, а разность с неизвестным не определена. Сравнивать варианты на
разных наборах пар нельзя тем более: разница отражала бы разный состав выборки,
а не разные правила выхода. Число исключённых пар печатается отдельно.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА (правило D-3 — пакетов на хосте нет):

    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro barrier python -m scripts.trailing_stats

ПРО КАТАЛОГ scripts И ЗАЧЕМ ЕГО ПОДКЛЮЧАТЬ ТОМОМ. В образ копируются только
``src/`` и ``backtest/`` (см. Dockerfile), поэтому ``python -m scripts.<что-то>``
внутри контейнера БЕЗ этого тома падает с ``No module named 'scripts'``.
Проверено на стенде. Том даёт контейнеру каталог скриптов только на чтение и
ничего в образе не меняет — а образ трогать нельзя: в нём живёт горячий путь,
и пересборка ради аналитического скрипта была бы риском без причины.
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
from src.trailing.rule import (  # noqa: E402
    EXIT_AMBIGUOUS,
    EXIT_NO_DATA,
    FIXED_VARIANT,
    VARIANTS,
    variant_label,
)

# Число пересборок и перестановок (§5 ТЗ). Десять тысяч — не «побольше для
# верности», а компромисс: этого достаточно, чтобы границы интервала перестали
# заметно гулять от запуска к запуску, и мало настолько, чтобы расчёт занимал
# секунды.
RESAMPLES = 10_000
CONFIDENCE = 0.95

# Зёрна отдельные для двух разных вопросов: перестановки отвечают на вопрос «а
# бывает ли такой разброс случайно», пересборки — «где лежит разница». Общее
# зерно связало бы два независимых ответа одной случайностью.
PERMUTATION_SEED = 8_10_2026
BOOTSTRAP_SEED = 810_2026

# Три формулировки §5.2. Третья ВЗЯТА ДОСЛОВНО из Этапа 8.9 (§8): это тот же
# вопрос о том же — хватает ли данных, чтобы отличить одно от другого, — и два
# разных слова для одного состояния читались бы как два разных состояния.
VERDICT_BETTER = "подвижный выход лучше, интервал не пересекает ноль"
VERDICT_WORSE = "подвижный выход хуже, интервал не пересекает ноль"
VERDICT_UNKNOWN = "различить нельзя, выборки не хватает"

# Две формулировки §5.1 и §5.3 — печатаются ДОСЛОВНО, как требует ТЗ.
VERDICT_INDISTINGUISHABLE = "варианты неразличимы, победитель случаен"
VERDICT_NOT_CONFIRMED = "преимущество не подтверждается на независимых данных"

# Минимум пар, ниже которого интервал не считается вовсе. На трёх наблюдениях
# бутстрэп даёт интервал, но это интервал ни о чём: он описывает три числа, а не
# рынок. Печатать его значило бы придать форму измерения тому, что им не
# является.
MIN_PAIRS = 20

QUERY = """
    SELECT t.signal_id,
           t.horizon_h,
           t.activation_ratio::float8 AS activation_ratio,
           t.retrace_ratio::float8    AS retrace_ratio,
           t.exit_reason,
           t.net_pnl_pct::float8      AS net_pnl_pct,
           s.ts
    FROM trailing_outcomes t
    JOIN signals s ON s.id = t.signal_id
    WHERE t.logic_version = $1
    ORDER BY s.ts ASC, t.signal_id ASC, t.horizon_h ASC;
"""


def collect(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Пары с ПОЛНЫМ набором вариантов и определённым итогом у каждого.

    Возвращает ``(пары, причины исключения)``. Пара включается, только если у
    неё есть все тринадцать вариантов И у всех тринадцати есть результат в
    деньгах. Половинчатая пара сделала бы столбцы таблицы посчитанными на
    разных выборках — а тогда разница между вариантами отражала бы состав
    выборки, а не правило выхода.
    """
    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["signal_id"]), int(row["horizon_h"]))
        item = grouped.setdefault(key, {"ts": row["ts"], "by_variant": {}})
        item["by_variant"][
            (round(float(row["activation_ratio"]), 2),
             round(float(row["retrace_ratio"]), 2))
        ] = row

    pairs: list[dict[str, Any]] = []
    dropped = {"incomplete": 0, EXIT_AMBIGUOUS: 0, EXIT_NO_DATA: 0}
    for item in grouped.values():
        by_variant = item["by_variant"]
        if len(by_variant) < len(VARIANTS) or any(
            v not in by_variant for v in VARIANTS
        ):
            dropped["incomplete"] += 1
            continue
        reasons = {by_variant[v]["exit_reason"] for v in VARIANTS}
        if EXIT_NO_DATA in reasons:
            dropped[EXIT_NO_DATA] += 1
            continue
        if EXIT_AMBIGUOUS in reasons:
            dropped[EXIT_AMBIGUOUS] += 1
            continue
        if any(by_variant[v]["net_pnl_pct"] is None for v in VARIANTS):
            dropped["incomplete"] += 1
            continue
        pairs.append({
            "ts": item["ts"],
            "pnl": {v: float(by_variant[v]["net_pnl_pct"]) for v in VARIANTS},
        })
    pairs.sort(key=lambda p: p["ts"])
    return pairs, dropped


def matrix(pairs: list[dict[str, Any]]) -> np.ndarray:
    """Матрица ``пары × варианты`` в порядке ``VARIANTS``."""
    return np.array(
        [[pair["pnl"][v] for v in VARIANTS] for pair in pairs], dtype=float
    )


def spread_permutation(
    values: np.ndarray, *, resamples: int, seed: int
) -> tuple[float, float, np.ndarray]:
    """Разброс средних и его случайный масштаб (§5.1).

    Ярлыки вариантов перемешиваются ВНУТРИ КАЖДОЙ ПАРЫ. Это и есть точная
    формулировка вопроса «а бывает ли такой разброс случайно»: если правило
    выхода ни на что не влияет, то какое из тринадцати чисел пары какому ярлыку
    досталось — безразлично, и наблюдённый разброс обязан теряться среди
    перемешанных. Перемешивание столбцов целиком отвечало бы на другой вопрос и
    разрушило бы парность — а именно парность здесь и есть источник точности.

    ``rng.permuted(..., axis=1)`` перемешивает каждую строку НЕЗАВИСИМО и делает
    это в C: тот же результат через ``argsort`` случайной матрицы считался бы
    в разы дольше, а на десятках тысяч пар это минуты против секунд.

    Возвращает ``(наблюдённый разброс, 95-й процентиль случайного, облако)``.
    """
    observed = float(values.mean(axis=0).max() - values.mean(axis=0).min())
    rng = np.random.default_rng(seed)
    cloud = np.empty(resamples, dtype=float)
    for i in range(resamples):
        means = rng.permuted(values, axis=1).mean(axis=0)
        cloud[i] = means.max() - means.min()
    return observed, float(np.percentile(cloud, CONFIDENCE * 100.0)), cloud


def bootstrap_means(
    diffs: np.ndarray, *, resamples: int, seed: int
) -> np.ndarray:
    """Облако средних для нескольких рядов разниц СРАЗУ, по общим пересборкам.

    ``diffs`` — матрица ``пары × ряды``. Все ряды пересобираются ОДНИМИ И ТЕМИ ЖЕ
    пересборками намеренно: интервалы двенадцати вариантов тогда сравнимы между
    собой, а не отличаются на собственный шум генератора.

    ПОЧЕМУ ЧЕРЕЗ ЧИСЛА ПОВТОРОВ, А НЕ ЧЕРЕЗ ИНДЕКСЫ. Пересборка с возвращением
    полностью описывается тем, СКОЛЬКО РАЗ каждая пара в неё попала, а это
    ровно полиномиальное распределение. Матрица повторов на матрицу разниц —
    одно умножение, которое считает библиотека; выборка по индексам дала бы тот
    же ответ, но на десятках тысяч пар потребовала бы гигабайты памяти под
    массив индексов ``(пересборки × пары)``.
    """
    n = diffs.shape[0]
    if n == 0:
        raise ValueError("пустая выборка: интервал не определён")
    rng = np.random.default_rng(seed)
    out = np.empty((resamples, diffs.shape[1]), dtype=float)
    probabilities = np.full(n, 1.0 / n)
    # Размер порции подобран под память: матрица повторов занимает
    # ``порция × пары × 8`` байт, и при десятках тысяч пар это десятки мегабайт.
    chunk = max(1, 4_000_000 // max(n, 1))
    done = 0
    while done < resamples:
        size = min(chunk, resamples - done)
        counts = rng.multinomial(n, probabilities, size=size)
        out[done:done + size] = counts @ diffs / float(n)
        done += size
    return out


def bootstrap_diff(
    left: np.ndarray, right: np.ndarray, *, resamples: int, seed: int
) -> tuple[float, float, float]:
    """``(разница средних, нижняя граница, верхняя граница)`` 95% интервала.

    Пересобираются ПАРЫ, а не два ряда по отдельности: пара — это одно
    наблюдение, у которого две стороны, и оба варианта стоят на одном и том же
    сигнале, в один и тот же момент, на одном и том же рынке. Независимая
    пересборка выбросила бы это совпадение и дала бы интервал ШИРЕ настоящего —
    то есть объявила бы «различить нельзя» там, где различить можно. Метод тот
    же, что в ``scripts/baseline_bootstrap.py`` (§8 ТЗ 8.9).
    """
    if left.shape != right.shape:
        raise ValueError("стороны пары обязаны быть одной длины")
    diff = (left - right).reshape(-1, 1)
    lo, hi = interval(bootstrap_means(diff, resamples=resamples, seed=seed)[:, 0])
    return float(diff.mean()), lo, hi


def interval(cloud: np.ndarray) -> tuple[float, float]:
    """Границы 95% интервала по облаку средних."""
    lo_q = (1.0 - CONFIDENCE) / 2.0 * 100.0
    hi_q = (1.0 + CONFIDENCE) / 2.0 * 100.0
    lo, hi = np.percentile(cloud, [lo_q, hi_q])
    return float(lo), float(hi)


def verdict(lo: float, hi: float) -> str:
    """Одна из трёх формулировок §5.2. Дословно, без вариаций."""
    if lo > 0.0:
        return VERDICT_BETTER
    if hi < 0.0:
        return VERDICT_WORSE
    return VERDICT_UNKNOWN


def best_variant(values: np.ndarray) -> int:
    """Номер столбца с наибольшим средним СРЕДИ ПОДВИЖНЫХ вариантов.

    Контрольный вариант исключён намеренно: он не участник соревнования, он
    точка отсчёта. Включив его, мы получили бы «победителя», которым иногда
    оказывалась бы сама фиксированная цель, и §5.3 («победитель первой половины
    проверяется на второй») потерял бы смысл.
    """
    means = values.mean(axis=0)
    order = [i for i, v in enumerate(VARIANTS) if v != FIXED_VARIANT]
    return max(order, key=lambda i: means[i])


def _fmt_variant(index: int) -> str:
    return variant_label(*VARIANTS[index])


def print_table(values: np.ndarray, reasons: dict[tuple[float, float], dict[str, int]],
                pairs_total: int) -> None:
    """Таблица §5: тринадцать строк, по одной на вариант."""
    print()
    print(" ТАБЛИЦА ВАРИАНТОВ (по парам, где итог определён у всех тринадцати)")
    print(f" Пар в сравнении: {pairs_total}")
    print()
    header = (f"  {'вариант':<24}{'средний итог,%':>16}{'медиана,%':>12}"
              f"{'доля stop,%':>13}{'доля trail,%':>13}{'доля timeout,%':>15}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    fixed_index = VARIANTS.index(FIXED_VARIANT)
    means = values.mean(axis=0)
    medians = np.median(values, axis=0)
    for i, variant in enumerate(VARIANTS):
        by_reason = reasons.get(variant, {})
        total = sum(by_reason.values()) or 1
        share = {k: 100.0 * v / total for k, v in by_reason.items()}
        mark = " ←контроль" if i == fixed_index else ""
        print(
            f"  {_fmt_variant(i):<24}{means[i]:>16.4f}{medians[i]:>12.4f}"
            f"{share.get('stop', 0.0):>13.1f}{share.get('trail', 0.0):>13.1f}"
            f"{share.get('timeout', 0.0):>15.1f}{mark}"
        )


def print_spread(values: np.ndarray) -> None:
    """§5.1 — разброс между вариантами против случайного разброса."""
    observed, p95, cloud = spread_permutation(
        values, resamples=RESAMPLES, seed=PERMUTATION_SEED
    )
    print()
    print(" §5.1 РАЗБРОС МЕЖДУ ВАРИАНТАМИ")
    print(f"   наблюдённый разброс (лучший минус худший): {observed:+.4f}%")
    print(f"   случайный разброс, 95-й процентиль:        {p95:+.4f}%")
    print(f"   (перестановок: {RESAMPLES}, зерно {PERMUTATION_SEED}, "
          f"медиана случайного {float(np.median(cloud)):.4f}%)")
    print()
    if observed <= p95:
        print(f"   → {VERDICT_INDISTINGUISHABLE}")
    else:
        print("   → разброс выходит за случайный: варианты различаются не только")
        print("     случайностью. Это НЕ означает, что различие устойчиво —")
        print("     на это отвечают §5.2 и §5.3 ниже.")


def print_intervals(values: np.ndarray) -> None:
    """§5.2 — доверительный интервал разницы «вариант минус фиксированная цель»."""
    fixed_index = VARIANTS.index(FIXED_VARIANT)
    fixed = values[:, fixed_index]
    winner = best_variant(values)
    n = values.shape[0]

    print()
    print(" §5.2 ИНТЕРВАЛ РАЗНИЦЫ «ВАРИАНТ МИНУС ФИКСИРОВАННАЯ ЦЕЛЬ»")
    print(f"   Бутстрэп: {RESAMPLES} пересборок, интервал {int(CONFIDENCE * 100)}%, "
          f"зерно {BOOTSTRAP_SEED}, пар {n}")
    if n < MIN_PAIRS:
        print()
        print(f"   Пар меньше {MIN_PAIRS} — интервал не считается.")
        print(f"   → {VERDICT_UNKNOWN} (пар меньше {MIN_PAIRS})")
        return

    moving = [i for i, v in enumerate(VARIANTS) if v != FIXED_VARIANT]
    diffs = values[:, moving] - fixed[:, None]
    cloud = bootstrap_means(diffs, resamples=RESAMPLES, seed=BOOTSTRAP_SEED)
    observed = diffs.mean(axis=0)
    bounds = [interval(cloud[:, k]) for k in range(len(moving))]
    place = {column: k for k, column in enumerate(moving)}

    k = place[winner]
    lo, hi = bounds[k]
    print()
    print(f"   ЛУЧШИЙ ПО СРЕДНЕМУ: {_fmt_variant(winner)}")
    print(f"     разница средних: {observed[k]:+.4f}%   "
          f"интервал 95%: [{lo:+.4f}; {hi:+.4f}]")
    print(f"     → {verdict(lo, hi)}")
    print("     ВНИМАНИЕ: этот вариант ВЫБРАН ПО ЭТИМ ЖЕ ДАННЫМ, поэтому его")
    print("     интервал смещён в его пользу. Это верхняя оценка возможного")
    print("     преимущества, а не его доказательство.")

    print()
    print("   ВСЕ ДВЕНАДЦАТЬ ВАРИАНТОВ ПРОТИВ ФИКСИРОВАННОЙ ЦЕЛИ:")
    for column in moving:
        k = place[column]
        lo, hi = bounds[k]
        print(f"     {_fmt_variant(column):<24} {observed[k]:+.4f}%  "
              f"[{lo:+.4f}; {hi:+.4f}]  → {verdict(lo, hi)}")


def print_split_half(values: np.ndarray) -> None:
    """§5.3 — победитель первой половины проверяется на второй."""
    n = values.shape[0]
    print()
    print(" §5.3 ПРОВЕРКА НА НЕЗАВИСИМОЙ ПОЛОВИНЕ")
    half = n // 2
    if half < MIN_PAIRS:
        print(f"   В половине меньше {MIN_PAIRS} пар ({half}) — проверка не выполнена.")
        print("   Это НЕ «преимущество подтверждено»: проверять было нечем.")
        return

    first, second = values[:half], values[half:]
    winner = best_variant(first)
    fixed_index = VARIANTS.index(FIXED_VARIANT)
    winner_second = best_variant(second)

    gain_first = float(first[:, winner].mean() - first[:, fixed_index].mean())
    gain_second = float(second[:, winner].mean() - second[:, fixed_index].mean())

    print(f"   Деление по времени: первая половина {half} пар, "
          f"вторая {n - half} пар.")
    print(f"   Победитель первой половины: {_fmt_variant(winner)} "
          f"(преимущество {gain_first:+.4f}%)")
    print(f"   Он же на второй половине:   преимущество {gain_second:+.4f}%")
    print(f"   Победитель второй половины: {_fmt_variant(winner_second)}")
    print()
    # «Не победил» означает ровно два условия, и оба названы, чтобы вердикт
    # нельзя было получить смягчением определения.
    if winner_second != winner or gain_second <= 0.0:
        print(f"   → {VERDICT_NOT_CONFIRMED}")
    else:
        print("   → победитель первой половины остался лучшим и на второй.")
        print("     На двух сутках одного падающего рынка это НЕ доказывает")
        print("     устойчивости: половины взяты из одного и того же отрезка.")


async def main() -> int:
    await db.connect()
    try:
        rows = [dict(r) for r in await db.pool.fetch(
            QUERY, int(settings.LOGIC_VERSION)
        )]
    finally:
        await db.close()

    print("=" * 78)
    print(" ЭТАП 8.10 §5. ПОДВИЖНЫЙ ВЫХОД ПРОТИВ ФИКСИРОВАННОЙ ЦЕЛИ")
    print(f" Версия логики: {settings.LOGIC_VERSION}. Строк в таблице: {len(rows)}")
    print(" ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ ЗАПРЕЩЁН (§5.4 ТЗ): это таблица, а не")
    print(" решение.")
    print("=" * 78)

    if not rows:
        print()
        print("  Строк нет — сравнивать нечего.")
        print("  Это не результат сравнения, а его отсутствие: сначала должен")
        print("  отработать расчёт (python -m src.trailing_main).")
        return 0

    pairs, dropped = collect(rows)
    print()
    print(" ИСКЛЮЧЕНО ИЗ СРАВНЕНИЯ (и это не упущение):")
    print(f"   пар с неполным набором вариантов:            {dropped['incomplete']}")
    print(f"   пар, где хоть один вариант дал no_data:      {dropped[EXIT_NO_DATA]}")
    print(f"   пар, где хоть один вариант дал ambiguous:    {dropped[EXIT_AMBIGUOUS]}")

    if not pairs:
        print()
        print("  Пар с определённым итогом у всех тринадцати вариантов нет.")
        print("  Сравнение не выполнено — это отсутствие ответа, а не ответ.")
        return 0

    values = matrix(pairs)
    reasons: dict[tuple[float, float], dict[str, int]] = {}
    for row in rows:
        key = (round(float(row["activation_ratio"]), 2),
               round(float(row["retrace_ratio"]), 2))
        bucket = reasons.setdefault(key, {})
        bucket[row["exit_reason"]] = bucket.get(row["exit_reason"], 0) + 1

    print_table(values, reasons, len(pairs))
    print_spread(values)
    print_intervals(values)
    print_split_half(values)

    print()
    print("-" * 78)
    print(" КАК ЭТО ЧИТАТЬ. Интервал — диапазон, в котором правдоподобно лежит")
    print(" настоящая разница. Пересекает ноль — данные не позволяют сказать,")
    print(" есть разница или нет. Это НЕ означает, что разницы нет; это")
    print(" означает, что наблюдений мало.")
    print()
    print(" Выборка — двое суток одного падающего рынка. Вывода о том, какой")
    print(" выход лучше, из неё не следует ни при каком результате проверок")
    print(" выше (§10.5 ТЗ).")
    print("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
