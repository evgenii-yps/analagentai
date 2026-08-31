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
import resource
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import structlog  # noqa: E402

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

# Строка, по которой видно, что прогон дошёл до конца. Убитый ядром процесс не
# печатает ничего: без такого признака оборванный вывод неотличим от вывода
# расчёта, который посчитал и промолчал.
DONE_MARKER = "РАСЧЁТ ЗАВЕРШЁН"

FIXED_INDEX = VARIANTS.index(FIXED_VARIANT)

_log = structlog.get_logger().bind(component="trailing_resample")


# Сколько байт на пару держит матрица чисел: тринадцать float64.
BYTES_PER_PAIR = 13 * 8
# Во сколько раз выборка может вырасти, и мы обязаны остаться в лимите (§ правки).
GROWTH_FACTOR = 3
# Доля лимита контейнера, за которую расчёт не заходит. Не «сколько влезет», а
# «сколько можно взять, не мешая тому, что работает круглосуточно».
MEMORY_BUDGET_SHARE = 0.5


def peak_rss_mb() -> float:
    """Пиковое потребление памяти процессом, МБ. Linux отдаёт ``ru_maxrss`` в КБ."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cgroup_memory_limit_mb() -> float | None:
    """Лимит памяти контейнера, МБ. ``None`` — лимита нет или он не читается.

    ЗАЧЕМ ЭТО ЗНАТЬ РАСЧЁТУ. Убитый ядром процесс не печатает ничего: код
    возврата 137 и оборванный вывод, неотличимый от вывода расчёта, который
    посчитал и промолчал. Прочитав лимит заранее, скрипт может ОТКАЗАТЬСЯ
    внятно — с числами — вместо того чтобы быть убитым.
    """
    for path, unlimited in (
        ("/sys/fs/cgroup/memory.max", "max"),                  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None),  # cgroup v1
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if text == unlimited:
            return None
        try:
            value = int(text)
        except ValueError:
            continue
        # cgroup v1 при отсутствии лимита пишет заведомо огромное число.
        if value <= 0 or value > 1 << 50:
            return None
        return value / 2**20
    return None


@dataclass
class Composition:
    """Состав выборки, накопленный ЗА ОДИН ПРОХОД по строкам (БЛОК 1).

    Ни один список строк здесь не хранится: только счётчики. Прежняя редакция
    держала все 1 707 940 строк словарями (около 1,7 ГБ) и ещё раз проходила по
    ним ради разбивок; счётчики стоят несколько килобайт и считаются попутно.
    """

    pairs: int = 0
    old_by_computed: int = 0
    old_by_signal_ts: int = 0
    horizons: dict[int, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    versions: dict[int, int] = field(default_factory=dict)

    def add_pair(self, *, horizon_h: int, token: str, computed_at: datetime,
                 ts: datetime) -> None:
        self.pairs += 1
        if computed_at < BOUNDARY:
            self.old_by_computed += 1
        if ts < BOUNDARY:
            self.old_by_signal_ts += 1
        self.horizons[horizon_h] = self.horizons.get(horizon_h, 0) + 1
        self.tokens[token] = self.tokens.get(token, 0) + 1

    def add_row_version(self, logic_version: int) -> None:
        self.versions[logic_version] = self.versions.get(logic_version, 0) + 1


class PairMatrix:
    """Растущая матрица «пары × варианты». Числа, и ничего кроме чисел.

    ПОЧЕМУ НЕ СПИСОК СЛОВАРЕЙ. Тринадцать чисел пары в матрице занимают 104
    байта; те же тринадцать чисел словарём с ключами-кортежами — около 1200.
    На 131 380 парах это 13 МБ против 154 МБ, и разница ровно та, из-за которой
    расчёт не доживал до блока 2.

    Ёмкость удваивается: перекладывание стоит одного лишнего экземпляра матрицы
    в пике (13 МБ на боевом объёме), а прирост по одной строке стоил бы
    квадратичного времени.
    """

    def __init__(self, width: int, capacity: int = 4096) -> None:
        self._values = np.empty((capacity, width), dtype=float)
        self._is_old = np.empty(capacity, dtype=bool)
        self._width = width
        self._size = 0

    def append(self, row: list[float], *, is_old: bool) -> None:
        if self._size == self._values.shape[0]:
            self._values = np.resize(
                self._values, (self._size * 2, self._width)
            )
            self._is_old = np.resize(self._is_old, self._size * 2)
        self._values[self._size] = row
        self._is_old[self._size] = is_old
        self._size += 1

    @property
    def values(self) -> np.ndarray:
        return self._values[: self._size]

    @property
    def is_old(self) -> np.ndarray:
        return self._is_old[: self._size]

    def __len__(self) -> int:
        return self._size


def pair_key(row: dict[str, Any]) -> tuple[Any, int, int]:
    """Ключ пары в том же порядке, в каком её отдаёт база."""
    return (row["ts"], int(row["signal_id"]), int(row["horizon_h"]))


def fold_pair(
    rows: list[dict[str, Any]],
    matrix_out: PairMatrix,
    composition: Composition,
    dropped: dict[str, int],
) -> None:
    """Свернуть строки ОДНОЙ пары в строку матрицы. Отбор — функцией Этапа 8.10.

    ``collect`` вызывается на тринадцати строках одной пары, а не на всей
    выборке: он группирует по ключу, поэтому на одной паре отвечает «эта пара
    годится» либо «эта пара отброшена, вот почему». Отбор при этом остаётся ТЕМ
    ЖЕ — полная пара, у всех тринадцати вариантов есть итог, ни одного
    ``ambiguous`` и ``no_data``. Переписать его своими словами значило бы завести
    второй экземпляр правила отбора, и он однажды разошёлся бы с первым.
    """
    pairs, pair_dropped = collect(rows)
    for reason, count in pair_dropped.items():
        dropped[reason] = dropped.get(reason, 0) + count
    if not pairs:
        return
    pair = pairs[0]
    first = rows[0]
    # Момент расчёта пары — САМЫЙ РАННИЙ среди её тринадцати строк (см. §2.3
    # отчёта: граница проводится по ``computed_at``, а не по времени сигнала).
    computed_at = min(row["computed_at"] for row in rows)
    composition.add_pair(
        horizon_h=int(first["horizon_h"]),
        token=str(first["token"]),
        computed_at=computed_at,
        ts=first["ts"],
    )
    matrix_out.append(
        [pair["pnl"][v] for v in VARIANTS], is_old=computed_at < BOUNDARY
    )


async def stream_sample() -> tuple[PairMatrix, Composition, dict[str, int], int]:
    """Один проход по ``trailing_outcomes``: матрица чисел и счётчики состава.

    Возвращает ``(матрица, состав, причины отброса, прочитано строк)``.

    ПАМЯТЬ НЕ РАСТЁТ С ЧИСЛОМ СТРОК. В каждый момент живут ровно три вещи:
    порция чтения (13 000 строк, около 13 МБ), строки одной пары и матрица
    чисел (104 байта на пару). Прежняя редакция держала всю таблицу словарями —
    1 707 940 строк, около 1,7 ГБ, — и была убита ядром по лимиту контейнера.

    ХВОСТОВАЯ ПАРА ПОРЦИИ ОТКЛАДЫВАЕТСЯ ДО СЛЕДУЮЩЕЙ ПОРЦИИ. Порция почти
    всегда обрывается посередине пары, и посчитать пару по обрезку значило бы
    объявить полную пару неполной — то есть выбросить её из сравнения по
    причине, которой в данных нет. Цена — одно перечитывание пары на порцию.
    """
    matrix_out = PairMatrix(len(VARIANTS))
    composition = Composition()
    dropped: dict[str, int] = {}
    after: tuple[Any, int, int] | None = None
    rows_read = 0

    while True:
        batch = await db.fetch_trailing_resample_batch(after=after)
        if not batch:
            break
        last_batch = len(batch) < db.TRAILING_RESAMPLE_BATCH
        tail_key = pair_key(batch[-1])
        usable = (
            batch if last_batch
            else [row for row in batch if pair_key(row) != tail_key]
        )
        if not usable:
            # Порция целиком занята одной парой. Такого не бывает при разумной
            # порции, но молча зациклиться здесь было бы хуже всего: чтение не
            # сдвинулось бы ни на строку, а скрипт выглядел бы работающим.
            raise RuntimeError(
                f"порция из {len(batch)} строк занята одной парой — "
                "увеличьте DB.TRAILING_RESAMPLE_BATCH"
            )
        rows_read += len(usable)

        buffer: list[dict[str, Any]] = []
        current: tuple[Any, int, int] | None = None
        for row in usable:
            composition.add_row_version(int(row["logic_version"]))
            key = pair_key(row)
            if current is not None and key != current:
                fold_pair(buffer, matrix_out, composition, dropped)
                buffer = []
            current = key
            buffer.append(row)
        if buffer:
            fold_pair(buffer, matrix_out, composition, dropped)

        if last_batch:
            break
        # Следующая порция начинается СТРОГО после последней разобранной пары,
        # поэтому отложенный хвост в неё попадает целиком.
        after = current

    return matrix_out, composition, dropped, rows_read


def deltas_grid(values: np.ndarray) -> dict[tuple[float, float], float]:
    """Средний прирост каждого варианта против контроля, в п.п. на сделку.

    Считается ПО МАТРИЦЕ, а не по списку пар: те же числа, но без словаря на
    каждую пару. Среднее по столбцу — это ровно «сумма делить на число
    наблюдений», то есть тот самый агрегат, ради которого выборку и не нужно
    держать целиком в виде объектов.
    """
    means = values.mean(axis=0)
    base = means[FIXED_INDEX]
    return {
        variant: float(means[VARIANTS.index(variant)] - base)
        for variant in VARIANTS
        if variant != FIXED_VARIANT
    }


def print_grid(title: str, values: np.ndarray) -> None:
    """Таблица 4×3: A по столбцам, R по строкам, в п.п. на сделку.

    Пустая часть выборки печатается словами, а не пустой сеткой: сетка из
    прочерков выглядит как посчитанный результат.
    """
    print()
    print(f"  {title}")
    if values.shape[0] == 0:
        print("    (пар нет — считать нечего)")
        return
    grid = deltas_grid(values)
    print(f"    пар: {values.shape[0]}")
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


def print_composition(composition: Composition, rows_read: int) -> None:
    """БЛОК 1 §2.3: состав выборки — из счётчиков, накопленных за один проход.

    ``logic_version`` печатается разбивкой, и больше одного значения — ЭТО
    ПРЕДУПРЕЖДЕНИЕ ОТДЕЛЬНОЙ КРУПНОЙ СТРОКОЙ. Смешение версий логики в одном
    сравнении — известная в проекте причина ложных выводов: разные версии
    отбирают разные сигналы, и разница между вариантами отражала бы состав
    выборки, а не правило выхода.
    """
    total = composition.pairs
    print()
    print("-" * 78)
    print(" БЛОК 1. СОСТАВ ВЫБОРКИ")
    print("-" * 78)
    print(f"  всего пар: {total}")
    print(f"    посчитано до {BOUNDARY:%Y-%m-%d} UTC: {composition.old_by_computed}")
    print(f"    посчитано с  {BOUNDARY:%Y-%m-%d} UTC: "
          f"{total - composition.old_by_computed}")
    print(f"    (для сверки, по времени СИГНАЛА до границы: "
          f"{composition.old_by_signal_ts},")
    print(f"     с границы: {total - composition.old_by_signal_ts})")
    print("  по горизонтам: " + " / ".join(
        f"{h}ч: {n}" for h, n in sorted(composition.horizons.items())
    ))
    print("  по инструментам: " + " / ".join(
        f"{t}: {n}" for t, n in sorted(composition.tokens.items())
    ))
    print("  logic_version: " + " / ".join(
        f"{v}: {n}" for v, n in sorted(composition.versions.items())
    ))
    versions = {v: n for v, n in composition.versions.items() if n}
    if len(versions) > 1:
        print()
        print("  " + "!" * 70)
        print("  !!  В ВЫБОРКЕ БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ. СРАВНЕНИЕ ВАРИАНТОВ")
        print("  !!  НА СМЕШАННЫХ ВЕРСИЯХ НЕДЕЙСТВИТЕЛЬНО: разные версии")
        print("  !!  отбирают разные сигналы, и разница между вариантами будет")
        print("  !!  отражать состав выборки, а не правило выхода.")
        print("  " + "!" * 70)
    if not composition.old_by_computed:
        print()
        print("  ВНИМАНИЕ: старая часть выборки ПУСТА. Скорее всего был сделан")
        print("  принудительный пересчёт (--recompute), сбросивший computed_at у")
        print("  всех строк. Тогда разделение на старую и новую часть смысла не")
        print("  имеет, и третья таблица НЕ является независимой проверкой.")
    print(f"  прочитано строк: {rows_read}")


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
    # ДВЕНАДЦАТЬ ОТДЕЛЬНЫХ ПЕРЕСБОРОК, А НЕ ОДНА ОБЩАЯ — И ЭТО ОСОЗНАННЫЙ
    # РАЗМЕН, названный вслух. Одним вызовом ``bootstrap_means`` на матрицу
    # 12 столбцов те же интервалы считаются в 13 раз быстрее (1,2 минуты против
    # 15,6 на 131 380 парах), но результат расходится в ШЕСТНАДЦАТОМ знаке:
    # умножение матрицы на матрицу и на вектор складывают числа в разном
    # порядке. На печать в четырёх знаках это не влияет НИКАК, и всё же взят
    # медленный путь: правка была о памяти, и менять числа заодно с ней —
    # значит лишить владельца возможности сверить новый вывод со старым.
    #
    # Память от этого не страдает: порция пересборок делится на число пар и с
    # ростом выборки не растёт, а вызовы идут последовательно.
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


def memory_estimate_mb(pairs: int) -> float:
    """Сколько памяти потребует расчёт при таком числе пар, МБ.

    Считаются три слагаемых, и все три известны точно, а не на глаз: матрица
    чисел, её копия при перестановке и порция пересборок бутстрэпа. Порция
    бутстрэпа подобрана в ``trailing_stats`` под память и с ростом выборки НЕ
    растёт — она делится на число пар.
    """
    matrix_mb = pairs * BYTES_PER_PAIR / 2**20
    permutation_copy_mb = matrix_mb
    bootstrap_chunk = max(1, 4_000_000 // max(pairs, 1))
    bootstrap_mb = bootstrap_chunk * pairs * 8 / 2**20
    reading_mb = db.TRAILING_RESAMPLE_BATCH * 1024 / 2**20
    return matrix_mb + permutation_copy_mb + bootstrap_mb + reading_mb


def print_memory_verdict(pairs: int) -> str | None:
    """Печатает расчёт памяти. Возвращает текст отказа либо ``None``.

    ОТКАЗ ВНЯТНЫЙ ЛУЧШЕ СМЕРТИ МОЛЧА. Процесс, убитый ядром по лимиту
    контейнера, не печатает ничего: код возврата 137 и оборванный вывод,
    неотличимый от вывода расчёта, который посчитал и промолчал. Ровно это
    случилось 31.08.2026. Прочитав лимит заранее, скрипт отказывается сам — с
    числами, — и решение принимает владелец, а не ядро.

    ЛИМИТ НЕ ПОДНИМАЕТСЯ МОЛЧА. Если оценка не помещается, здесь печатается,
    сколько нужно и сколько есть; правку ``mem_limit`` делает владелец.
    """
    limit = cgroup_memory_limit_mb()
    need = memory_estimate_mb(pairs)
    need_grown = memory_estimate_mb(pairs * GROWTH_FACTOR)
    print()
    print(f"  Оценка памяти на {pairs} пар: {need:,.0f} МБ")
    print(f"  При росте выборки втрое ({pairs * GROWTH_FACTOR} пар): "
          f"{need_grown:,.0f} МБ")
    if limit is None:
        print("  Лимит памяти контейнера не задан или не читается.")
        return None
    budget = limit * MEMORY_BUDGET_SHARE
    print(f"  Лимит контейнера: {limit:,.0f} МБ, "
          f"бюджет расчёта: {budget:,.0f} МБ "
          f"({MEMORY_BUDGET_SHARE:.0%} лимита)")
    if need <= budget:
        return None
    return (
        f"расчёту нужно около {need:,.0f} МБ при бюджете {budget:,.0f} МБ "
        f"(лимит контейнера {limit:,.0f} МБ)"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Пересчёт замера подвижного выхода 8.10 на выросшей выборке "
            "(Этап 9.1.3, часть А). Только чтение."
        )
    )
    parser.parse_args()

    print("=" * 78)
    print(" ЧАСТЬ А. ПЕРЕСЧЁТ ЗАМЕРА 8.10 НА ВЫРОСШЕЙ ВЫБОРКЕ (9.1.3)")
    print("=" * 78)
    print(f"  LOGIC_VERSION настройки: {settings.LOGIC_VERSION}")
    print(f"  ПРИЗНАК ЗАВЕРШЕНИЯ: строка «{DONE_MARKER}» в самом конце вывода.")
    print("  Её отсутствие означает, что прогон НЕ дошёл до конца, а не что")
    print("  считать было нечего: убитый ядром процесс не печатает ничего.")

    await db.connect()
    try:
        matrix_out, composition, dropped, rows_read = await stream_sample()
    finally:
        await db.close()

    if rows_read == 0:
        print()
        print("  Таблица trailing_outcomes пуста: считать нечего.")
        print("  Запустите расчёт 8.10: python -m src.trailing_main")
        print()
        print(f"  Пиковая память: {peak_rss_mb():,.0f} МБ")
        print(DONE_MARKER)
        return 3

    print_composition(composition, rows_read)

    values = matrix_out.values
    is_old = matrix_out.is_old
    _log.info(
        "Часть А: выборка прочитана",
        resample_rows_read=rows_read,
        resample_pairs=len(matrix_out),
        peak_rss_mb=round(peak_rss_mb(), 1),
    )

    refusal = print_memory_verdict(len(matrix_out))
    if refusal:
        print()
        print("=" * 78)
        print(" ОТКАЗ: РАСЧЁТ НЕ ПОМЕЩАЕТСЯ В ЛИМИТ ПАМЯТИ")
        print("=" * 78)
        print(f"  {refusal}")
        print()
        print("  Ничего не посчитано и ничего не напечатано наполовину. Поднять")
        print("  лимит контейнера — решение владельца, а не скрипта: на машине")
        print("  девять постоянных служб, и круглосуточный сбор данных важнее")
        print("  разового расчёта.")
        print()
        print(f"  Пиковая память: {peak_rss_mb():,.0f} МБ")
        print(DONE_MARKER)
        return 4

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
    print_grid("ВСЯ ВЫБОРКА", values)
    print_grid(
        f"ТОЛЬКО СТАРАЯ ЧАСТЬ (посчитано до {BOUNDARY:%d.%m.%Y})", values[is_old]
    )
    print_grid(
        f"ТОЛЬКО НОВАЯ ЧАСТЬ (посчитано с {BOUNDARY:%d.%m.%Y}) — "
        "НЕЗАВИСИМАЯ ПРОВЕРКА", values[~is_old]
    )
    print()
    print("  Третья таблица — единственная независимая проверка находки 8.10:")
    print("  этих данных при её обнаружении не существовало.")

    if len(matrix_out) == 0:
        print()
        print("  Ни одной полной пары — защиты от подгонки не считаются.")
        print()
        print(f"  Пиковая память: {peak_rss_mb():,.0f} МБ")
        print(DONE_MARKER)
        return 0

    print_defences(values)
    print_split_half_two_answers(values)

    peak = peak_rss_mb()
    _log.info("Часть А: расчёт завершён", peak_rss_mb=round(peak, 1),
              resample_pairs=len(matrix_out))
    print()
    print("=" * 78)
    print("  ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ (§0 ТЗ).")
    print("=" * 78)
    print(f"  Пиковая память: {peak:,.0f} МБ")
    print(DONE_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
