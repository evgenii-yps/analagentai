#!/usr/bin/env python3
"""Поиск и починка строк ``strategy_outcomes``, посчитанных по НЕЗАКРЫТОМУ бару.

ЧТО ЗДЕСЬ ЧИНИТСЯ (Этап 9.1, Задача Б). До этого этапа сеточные стратегии
(``grid_buy``, ``grid_sell``) считали годность входа условием «срок наступил»:

    if entry_ts + timedelta(hours=horizon_h) > now: continue

Ожидания закрытия последнего бара окна в нём не было. Окно ``t+1 … t+h``
кончается баром, который ОТКРЫВАЕТСЯ в момент срока, а закрывается через целый
бар после него. Коллектор перезаписывает формирующуюся свечу (UPSERT с
DO UPDATE), и исход ``timeout``, берущий итог из её ``close``, получал цену
«пока что», а не цену на срок. Ровно эта причина была найдена Этапом 8.10.1 и
устранена там в двух местах — ``src/barrier/runner.settle_seconds()`` и
``db.get_barrier_candidates()``, — но сеточных стратегий она не касалась: они
входят каждый час независимо от системы и проверяют годность СВОИМ условием.

ПОЧЕМУ ЭТО НЕЛЬЗЯ ОСТАВИТЬ. Запись идёт через ``ON CONFLICT DO NOTHING``, а
ключи уже посчитанных строк отсеиваются до чтения окна
(``get_strategy_pairs_done``). Значит, испорченная строка не будет переписана
НИКОГДА, ни одним последующим прогоном. Величина искажения мала (на 8.10 она
составляла около 1.2e-5 от цены входа), но именно на сеточных стратегиях стоит
вывод проекта «выбор момента входа не даёт ничего».

ПРИЗНАК ИСПОРЧЕННОЙ СТРОКИ — расчёт раньше закрытия последнего бара окна, и
запас берётся ПО ФАКТИЧЕСКОМУ РАЗРЕШЕНИЮ СТРОКИ (Этап 9.1.1, §3 ТЗ):

    computed_at < entry_ts + make_interval(hours => horizon_h)
                           + make_interval(secs => CASE resolution
                                                     WHEN '1m' THEN 60
                                                     ELSE 3600 END)
                           + make_interval(mins => BARRIER_SETTLE_MINUTES)

ПОЧЕМУ НЕ ``settle_seconds()``, КАК БЫЛО В ПЕРВОЙ РЕДАКЦИИ. ``settle_seconds()``
закладывает длину ГРУБОГО бара (час) всем строкам подряд, и для ОТБОРА
кандидатов это верно: разрешение выясняется уже ПОСЛЕ отбора, по факту покрытия
окна минутным рядом, а ждать надо до него — то есть по худшему случаю. Но у
УЖЕ ПОСЧИТАННОЙ строки разрешение известно и записано в колонке ``resolution``.
Проверять её худшим случаем — значит объявлять исправное подозрительным.

ФОРМУЛА ЛЕЖИТ В ОДНОМ МЕСТЕ: ``DB.STRATEGY_UNSETTLED_PREDICATE``
(``src/core/db.py``). Оба режима этого скрипта — счёт и ``--apply`` — спрашивают
её, а не свою копию: две копии одного правила разошлись бы при следующей правке,
и разошлись бы молча.

ЧТО ПОКАЗАЛ ПЕРВЫЙ ЗАПУСК НА БОЕВЫХ ДАННЫХ (30.08.2026, ШИРОКИЙ КРИТЕРИЙ)

    всего строк strategy_outcomes:            449 764
    подозрительных по ШИРОКОМУ критерию:        7 618
    посчитанных по НЕЗАКРЫТОМУ бару:                0
    минимальный запас, стратегии по сигналам:  15 мин 12 с
    минимальный запас, сеточные:               25 мин 02 с

Все 7618 строк измерены по МИНУТНОМУ ряду (``resolution = '1m'``), где последний
бар окна закрывается через 60 секунд после срока, а не через час. Требуемый им
запас — 60 с + BARRIER_SETTLE_MINUTES (5 мин) = 360 с; фактический минимум по
всей базе — 912 с. То есть ДЕФЕКТ В КОДЕ БЫЛ (сеточные стратегии действительно
не ждали закрытия последнего бара), а ИСПОРЧЕННЫХ ДАННЫХ ОН НЕ ОСТАВИЛ: ночная
задача 04:25 UTC приходит к паре сильно позже её срока.

СЛЕДСТВИЕ: НИ ОДНА СТРОКА ``strategy_outcomes`` НЕ УДАЛЯЕТСЯ. Удаление 7618
исправных измерений было бы не починкой, а потерей данных — тем более что
пересчёт понизил бы уже снятое МИНУТНОЕ разрешение до ЧАСОВОГО там, где
минутные свечи успела удалить политика хранения (RETENTION_1M_DAYS = 30 суток).
Ровно это и защищает ``--apply`` (см. :func:`_forbid_deleting_fine_rows`).

ОЖИДАНИЕ, ЗАПИСАННОЕ ДО ЗАПУСКА (правило проекта: предсказание фиксируется
раньше расчёта). Подозрительными окажутся ТОЛЬКО строки ``grid_buy`` и
``grid_sell``. У четырёх стратегий, привязанных к сигналам (``system``,
``always_buy``, ``always_sell``, ``coin_flip``), счёт будет НОЛЬ: моменты входа
они берут из ``signal_outcomes_barrier`` (``db.get_strategy_anchors``), то есть
наследуют защиту 8.10.1 по построению. Если счёт не ноль — это отдельная
находка, о ней надо ДОЛОЖИТЬ, а не чинить молча.

ПЕРЕСЧЁТ ЭТОТ СКРИПТ НЕ ВЫПОЛНЯЕТ. С ``--apply`` он снимает снимок «до» и
УДАЛЯЕТ подозрительные строки — и на этом всё. Удалённые ключи исчезли из
множества «уже посчитано», и очередной штатный прогон базовых стратегий
(ночная задача 04:25 UTC) посчитает их заново — уже исправленным правилом.
Отдельный путь пересчёта означал бы второй код, считающий то же самое, и
однажды он разошёлся бы со штатным.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА (правило проекта: пакетов на хосте нет). В образ
копируются только ``src/`` и ``backtest/`` (см. Dockerfile), поэтому каталог
скриптов подключается томом только на чтение:

    # 1. только посчитать (ничего не меняет):
    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro -v ./reports:/app/reports \\
        barrier python -m scripts.repair_9_1_strategy_settle

    # 2. снимок «до» + удаление подозрительных строк:
    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro -v ./reports:/app/reports \\
        barrier python -m scripts.repair_9_1_strategy_settle --apply

    # 3. после очередного прогона базовых стратегий — снимок «после»:
    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro -v ./reports:/app/reports \\
        barrier python -m scripts.repair_9_1_strategy_settle --snapshot-only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402

# Длина последнего бара окна по разрешению строки, секунд. Те же числа, что в
# ``src.barrier.outcomes.BAR_SECONDS`` и в SQL-предикате
# ``DB.STRATEGY_UNSETTLED_PREDICATE``; здесь они нужны только защите ``--apply``,
# которая обязана уметь объяснить своё решение человеку числом.
BAR_SECONDS_BY_RESOLUTION = {"1m": 60, "1h": 3600}

# Куда кладутся снимки. Каталог тот же, что у остальных отчётов проекта.
_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports"
)
SNAPSHOT_BEFORE = os.path.join(_REPORTS_DIR, "strategy_stats_before_9_1.txt")
SNAPSHOT_AFTER = os.path.join(_REPORTS_DIR, "strategy_stats_after_9_1.txt")

# Стратегии, у которых подозрительные строки ОЖИДАЮТСЯ (§2.3 ТЗ). Перечень
# нужен не для фильтрации — удаляется всё подозрительное, — а для того, чтобы
# скрипт САМ назвал находку, если счёт окажется не нулевым там, где ожидался
# ноль.
GRID_STRATEGIES = ("grid_buy", "grid_sell")

# Десять примеров: больше в глаза не помещается, меньше не даёт увидеть,
# однороден ли набор.
_EXAMPLES = 10


def _fmt(value: Any, digits: int = 6) -> str:
    """Число с фиксированной точностью либо прочерк для отсутствующего."""
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _rule_text(settle_min: int) -> str:
    """Критерий одной строкой — тем же текстом, каким он записан в SQL.

    Печатается ВЕЗДЕ, где скрипт называет строку подозрительной: следующий
    человек не должен полдня выяснять, врёт измеритель или измеряемое.
    """
    return (
        "computed_at < entry_ts + horizon_h ч + (60 с для '1m' / 3600 с для "
        f"'1h') + {settle_min} мин (BARRIER_SETTLE_MINUTES)"
    )


def _snapshot_text(rows: list[dict[str, Any]], settle_min: int, moment: str) -> str:
    """Снимок «до/после» текстом: по паре (стратегия, горизонт) — три величины."""
    lines = [
        "Снимок strategy_outcomes (Этап 9.1, Задача Б)",
        f"Момент снятия (UTC): {moment}",
        f"Критерий подозрительности: {_rule_text(settle_min)}",
        "",
        "Строки без итога (no_data, ambiguous) входят в «строк», но не в среднее:",
        "среднее по неизвестному не определено.",
        "",
        f"{'стратегия':<14}{'гор.':>6}{'строк':>10}{'доля target':>14}"
        f"{'средний net_pnl_pct':>22}",
        "-" * 66,
    ]
    for row in rows:
        rows_n = int(row["rows"])
        share = 100.0 * int(row["targets"]) / rows_n if rows_n else None
        lines.append(
            f"{row['strategy']:<14}{int(row['horizon_h']):>6}{rows_n:>10}"
            f"{('—' if share is None else f'{share:.2f}%'):>14}"
            f"{_fmt(row['avg_net_pnl_pct']):>22}"
        )
    if not rows:
        lines.append("(строк нет)")
    return "\n".join(lines) + "\n"


async def _write_snapshot(path: str, settle_min: int) -> list[dict[str, Any]]:
    """Снимает и сохраняет снимок. Возвращает строки — их же печатает вызвавший."""
    from datetime import UTC, datetime

    rows = await db.get_strategy_stats_snapshot()
    text = _snapshot_text(
        rows, settle_min, datetime.now(UTC).isoformat(timespec="seconds")
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)
    print(f"  Снимок сохранён: {path}")
    return rows


def _print_breakdown(title: str, counter: dict[Any, int], total: int) -> None:
    """Разбивка с долями. Ноль строк — так и печатается, а не пропускается."""
    print()
    print(f"  {title}")
    if not counter:
        print("    (нет)")
        return
    for key in sorted(counter, key=lambda k: (-counter[k], str(k))):
        share = 100.0 * counter[key] / total if total else 0.0
        print(f"    {str(key):<28} {counter[key]:>8}  ({share:.2f}%)")


def _required_margin_sec(resolution: str, settle_min: int) -> int | None:
    """Сколько секунд строке этого разрешения полагалось ждать после срока.

    ``None`` для разрешения, которого нет в перечне: выдумывать длину бара для
    неизвестного ряда нельзя — это была бы точность, которой нет.
    """
    bar = BAR_SECONDS_BY_RESOLUTION.get(resolution)
    return None if bar is None else bar + settle_min * 60


def _fine_rows_with_enough_margin(
    rows: list[dict[str, Any]], settle_min: int
) -> list[dict[str, Any]]:
    """Строки МИНУТНОГО ряда, чей запас БОЛЬШЕ положенных 60 с + settle.

    Такая строка исправна: её последний бар закрылся за 60 секунд до расчёта, и
    ещё BARRIER_SETTLE_MINUTES прошло сверх того. Попасть под удаление она может
    только одним способом — если критерий подозрительности снова расширили до
    худшего случая. Проверка стоит здесь именно как ловушка на этот случай.
    """
    guilty: list[dict[str, Any]] = []
    for row in rows:
        resolution = str(row["resolution"])
        if resolution != "1m":
            continue
        need = _required_margin_sec(resolution, settle_min)
        margin = row.get("margin_sec")
        if need is None or margin is None:
            continue
        if float(margin) > float(need):
            guilty.append(row)
    return guilty


def _forbid_deleting_fine_rows(
    rows: list[dict[str, Any]], settle_min: int
) -> int:
    """Защита ``--apply`` (§3.2 ТЗ 9.1.1). 0 — можно продолжать, 1 — отказ.

    ПОЧЕМУ ОТКАЗ ЦЕЛИКОМ, А НЕ ПРОПУСК ТАКИХ СТРОК. Удаление измерения
    необратимо. Минутные свечи старше RETENTION_1M_DAYS уже удалены политикой
    хранения, поэтому «удалим и пересчитаем» вернуло бы не то же самое число, а
    ЧАСОВОЕ разрешение вместо минутного — незаметную подмену измерения. Скрипт,
    который в такой ситуации сделал бы часть работы, оставил бы базу в
    состоянии, про которое нельзя сказать, что в ней померено.
    """
    guilty = _fine_rows_with_enough_margin(rows, settle_min)
    if not guilty:
        return 0
    need = _required_margin_sec("1m", settle_min)
    margins = sorted(float(r["margin_sec"]) for r in guilty)
    print()
    print("=" * 78)
    print(" ОТКАЗ: под удаление попали ИСПРАВНЫЕ строки минутного ряда")
    print("=" * 78)
    print(f"  Таких строк: {len(guilty)}")
    print(f"  Запас у них: от {margins[0]:.0f} до {margins[-1]:.0f} с "
          f"при требуемых {need} с (60 с бар + {settle_min} мин)")
    print()
    print("  ПОЧЕМУ ЭТО ОТКАЗ, А НЕ ПРЕДУПРЕЖДЕНИЕ. Строка с resolution='1m' и")
    print("  запасом больше 60 с + BARRIER_SETTLE_MINUTES посчитана по ЗАКРЫТОМУ")
    print("  бару — она исправна. Её удаление необратимо: минутные свечи старше")
    print(f"  RETENTION_1M_DAYS ({settings.RETENTION_1M_DAYS} суток) уже удалены")
    print("  политикой хранения, и пересчёт вернул бы ЧАСОВОЕ разрешение вместо")
    print("  минутного — то есть другое измерение под тем же ключом.")
    print()
    print("  Появление таких строк здесь означает, что критерий подозрительности")
    print("  снова расширен до худшего случая. Чинить надо критерий")
    print("  (DB.STRATEGY_UNSETTLED_PREDICATE), а не данные.")
    print()
    print("  Ничего не удалено. Код возврата 1.")
    return 1


async def _report(settle_min: int) -> list[dict[str, Any]]:
    """Считает подозрительные строки и печатает всё, что требует §2.3 ТЗ."""
    total = await db.count_strategy_outcomes_unsettled(settle_minutes=settle_min)
    print("=" * 78)
    print(" ПОДОЗРИТЕЛЬНЫЕ СТРОКИ strategy_outcomes (Этап 9.1, Задача Б)")
    print("=" * 78)
    print(f"  Правило: {_rule_text(settle_min)}")
    print(f"  Всего подозрительных строк: {total}")

    if total == 0:
        print()
        print("  Строк, посчитанных по незакрытому бару, нет.")
        return []

    rows = await db.get_strategy_outcomes_unsettled(settle_minutes=settle_min)

    by_pair: dict[Any, int] = {}
    by_outcome: dict[Any, int] = {}
    by_strategy: dict[str, int] = {}
    by_resolution: dict[Any, int] = {}
    for row in rows:
        strategy = str(row["strategy"])
        by_pair[f"{strategy} / {int(row['horizon_h'])}ч"] = (
            by_pair.get(f"{strategy} / {int(row['horizon_h'])}ч", 0) + 1
        )
        by_outcome[str(row["outcome"])] = by_outcome.get(str(row["outcome"]), 0) + 1
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        by_resolution[str(row["resolution"])] = (
            by_resolution.get(str(row["resolution"]), 0) + 1
        )

    _print_breakdown("По стратегии и горизонту:", by_pair, total)
    _print_breakdown("По исходу:", by_outcome, total)
    # РАЗБИВКА ПО РАЗРЕШЕНИЮ ОБЯЗАТЕЛЬНА (§2 ТЗ 9.1.1): именно разрешение
    # задаёт, сколько строке полагалось ждать, и без него разбор находки
    # начинался бы с выяснения, чем эти строки вообще меряли.
    _print_breakdown("По разрешению ряда:", by_resolution, total)

    print()
    print(f"  Примеры (до {_EXAMPLES} строк):")
    print(
        f"    {'стратегия':<11} {'инстр.':>6} {'entry_ts':<25} {'гор.':>4} "
        f"{'computed_at':<25} {'разр.':>6} {'запас, с':>10} "
        f"{'нужно, с':>10} {'исход':<10} {'net_pnl_pct':>12}"
    )
    for row in rows[:_EXAMPLES]:
        need = _required_margin_sec(str(row["resolution"]), settle_min)
        print(
            f"    {str(row['strategy']):<11} {int(row['instrument_id']):>6} "
            f"{row['entry_ts'].isoformat(timespec='seconds'):<25} "
            f"{int(row['horizon_h']):>4} "
            f"{row['computed_at'].isoformat(timespec='seconds'):<25} "
            f"{str(row['resolution']):>6} "
            f"{_fmt(row['margin_sec'], 0):>10} "
            f"{('—' if need is None else str(need)):>10} "
            f"{str(row['outcome']):<10} {_fmt(row['net_pnl_pct']):>12}"
        )

    # ПРОВЕРКА ЗАПИСАННОГО ЗАРАНЕЕ ОЖИДАНИЯ. Скрипт обязан назвать находку сам:
    # человек, читающий вывод, не должен вспоминать, чего он ждал.
    unexpected = {
        name: count
        for name, count in by_strategy.items()
        if name not in GRID_STRATEGIES
    }
    print()
    if unexpected:
        print("  ⚠ НАХОДКА, ВЫХОДЯЩАЯ ЗА ОЖИДАНИЕ §2.3 ТЗ.")
        print("    Ожидалось: подозрительны ТОЛЬКО grid_buy и grid_sell, потому")
        print("    что стратегии, привязанные к сигналам, берут моменты входа из")
        print("    signal_outcomes_barrier и наследуют защиту Этапа 8.10.1.")
        print("    Фактически подозрительны и эти стратегии:")
        for name, count in sorted(unexpected.items()):
            print(f"      {name:<16}{count:>8}")
        print("    Это отдельная находка: доложить, а не чинить молча.")
    else:
        print("  Ожидание §2.3 подтвердилось: подозрительны только сеточные")
        print("  стратегии; у привязанных к сигналам счёт ноль.")
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Строки strategy_outcomes, посчитанные по незакрытому последнему "
            "бару окна (Этап 9.1, Задача Б). Без аргументов — только счёт."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "снять снимок «до» и УДАЛИТЬ подозрительные строки. Пересчёт "
            "выполняет очередной штатный прогон базовых стратегий."
        ),
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="снять снимок «после» и выйти, ничего не удаляя",
    )
    args = parser.parse_args()

    if args.apply and args.snapshot_only:
        parser.error("--apply и --snapshot-only несовместимы: одно снимает "
                     "снимок «после», другое удаляет строки")

    settle_min = int(settings.BARRIER_SETTLE_MINUTES)
    await db.connect()
    try:
        if args.snapshot_only:
            print("=" * 78)
            print(" СНИМОК «ПОСЛЕ» (Этап 9.1, Задача Б)")
            print("=" * 78)
            await _write_snapshot(SNAPSHOT_AFTER, settle_min)
            # Снимок «после» ценен ровно тем, что показывает: подозрительных
            # строк не осталось. Печатаем счёт рядом, иначе его пришлось бы
            # запрашивать отдельно.
            left = await db.count_strategy_outcomes_unsettled(
                settle_minutes=settle_min
            )
            print()
            print(f"  Подозрительных строк осталось: {left} (ожидается 0)")
            return 0

        rows = await _report(settle_min)

        if not args.apply:
            print()
            print("  Ничего не изменено: без --apply скрипт только считает.")
            if rows:
                # ПОДСКАЗКИ «УДАЛИТЬ ИХ КОМАНДОЙ …» ЗДЕСЬ БОЛЬШЕ НЕТ, и это не
                # упущение. Первый запуск на боевых данных (см. шапку) показал,
                # что прежняя подсказка предлагала снести 7618 ИСПРАВНЫХ строк.
                # Скрипт, предлагающий необратимое действие раньше, чем находка
                # разобрана, приучает выполнять его не глядя.
                print("  Прежде чем удалять, разберите находку: под критерий")
                print(f"  «{_rule_text(settle_min)}»")
                print("  исправная строка попасть не может. Если строки есть —")
                print("  это НАСТОЯЩАЯ находка, и её надо доложить с разбивкой")
                print("  выше, а не удалять. Удаление необратимо и понижает")
                print("  разрешение уже снятых измерений (см. шапку скрипта).")
            return 0

        if not rows:
            print()
            print("  Удалять нечего — снимок «до» не снимается: он описывал бы")
            print("  состояние, которое ничем не отличается от текущего.")
            return 0

        # ЗАЩИТА ИДЁТ ДО СНИМКА «ДО» И ДО УДАЛЕНИЯ. Снимок — уже действие: он
        # переписывает файл в reports/, и делать его перед отказом значило бы
        # оставить след работы, которая не состоялась.
        refused = _forbid_deleting_fine_rows(rows, settle_min)
        if refused:
            return refused

        print()
        print("=" * 78)
        print(" СНИМОК «ДО» (Этап 9.1, Задача Б)")
        print("=" * 78)
        await _write_snapshot(SNAPSHOT_BEFORE, settle_min)

        deleted = await db.delete_strategy_outcomes_unsettled(
            settle_minutes=settle_min
        )
        print()
        print(f"  Удалено подозрительных строк: {deleted}")
        print()
        print("  Пересчёт выполнит ОЧЕРЕДНОЙ штатный прогон базовых стратегий:")
        print("  удалённые ключи исчезли из множества «уже посчитано», и ночная")
        print("  задача 04:25 UTC посчитает их заново исправленным правилом.")
        print("  Вручную — той же командой, что в §14 ТЗ:")
        print("    docker compose --profile tools run --rm --no-deps \\")
        print("        barrier python -m src.baseline_main")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
