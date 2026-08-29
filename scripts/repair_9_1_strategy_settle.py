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

ПРИЗНАК ИСПОРЧЕННОЙ СТРОКИ — расчёт раньше закрытия последнего бара окна:

    computed_at < entry_ts + make_interval(hours => horizon_h)
                           + make_interval(secs => settle_seconds())

ЗАПАС БЕРЁТСЯ ИЗ ОДНОГО МЕСТА С ЭТАПОМ 8.10.1 (``settle_seconds()``), а не
переписывается формулой: две копии одного правила разошлись бы при следующей
правке, и разошлись бы молча.

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

from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.db import db  # noqa: E402

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


def _snapshot_text(rows: list[dict[str, Any]], settle: int, moment: str) -> str:
    """Снимок «до/после» текстом: по паре (стратегия, горизонт) — три величины."""
    lines = [
        "Снимок strategy_outcomes (Этап 9.1, Задача Б)",
        f"Момент снятия (UTC): {moment}",
        f"Запас закрытия последнего бара (settle_seconds): {settle} с",
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


async def _write_snapshot(path: str, settle: int) -> list[dict[str, Any]]:
    """Снимает и сохраняет снимок. Возвращает строки — их же печатает вызвавший."""
    from datetime import UTC, datetime

    rows = await db.get_strategy_stats_snapshot()
    text = _snapshot_text(
        rows, settle, datetime.now(UTC).isoformat(timespec="seconds")
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


async def _report(settle: int) -> list[dict[str, Any]]:
    """Считает подозрительные строки и печатает всё, что требует §2.3 ТЗ."""
    total = await db.count_strategy_outcomes_unsettled(settle_seconds=settle)
    print("=" * 78)
    print(" ПОДОЗРИТЕЛЬНЫЕ СТРОКИ strategy_outcomes (Этап 9.1, Задача Б)")
    print("=" * 78)
    print(f"  Правило: computed_at < entry_ts + horizon_h ч + {settle} с")
    print(f"  Всего подозрительных строк: {total}")

    if total == 0:
        print()
        print("  Строк, посчитанных по незакрытому бару, нет.")
        return []

    rows = await db.get_strategy_outcomes_unsettled(settle_seconds=settle)

    by_pair: dict[Any, int] = {}
    by_outcome: dict[Any, int] = {}
    by_strategy: dict[str, int] = {}
    for row in rows:
        strategy = str(row["strategy"])
        by_pair[f"{strategy} / {int(row['horizon_h'])}ч"] = (
            by_pair.get(f"{strategy} / {int(row['horizon_h'])}ч", 0) + 1
        )
        by_outcome[str(row["outcome"])] = by_outcome.get(str(row["outcome"]), 0) + 1
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1

    _print_breakdown("По стратегии и горизонту:", by_pair, total)
    _print_breakdown("По исходу:", by_outcome, total)

    print()
    print(f"  Примеры (до {_EXAMPLES} строк):")
    print(
        f"    {'стратегия':<11} {'инстр.':>6} {'entry_ts':<25} {'гор.':>4} "
        f"{'computed_at':<25} {'исход':<10} {'net_pnl_pct':>12}"
    )
    for row in rows[:_EXAMPLES]:
        print(
            f"    {str(row['strategy']):<11} {int(row['instrument_id']):>6} "
            f"{row['entry_ts'].isoformat(timespec='seconds'):<25} "
            f"{int(row['horizon_h']):>4} "
            f"{row['computed_at'].isoformat(timespec='seconds'):<25} "
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

    settle = settle_seconds()
    await db.connect()
    try:
        if args.snapshot_only:
            print("=" * 78)
            print(" СНИМОК «ПОСЛЕ» (Этап 9.1, Задача Б)")
            print("=" * 78)
            await _write_snapshot(SNAPSHOT_AFTER, settle)
            # Снимок «после» ценен ровно тем, что показывает: подозрительных
            # строк не осталось. Печатаем счёт рядом, иначе его пришлось бы
            # запрашивать отдельно.
            left = await db.count_strategy_outcomes_unsettled(settle_seconds=settle)
            print()
            print(f"  Подозрительных строк осталось: {left} (ожидается 0)")
            return 0

        rows = await _report(settle)

        if not args.apply:
            print()
            print("  Ничего не изменено: без --apply скрипт только считает.")
            return 0

        if not rows:
            print()
            print("  Удалять нечего — снимок «до» не снимается: он описывал бы")
            print("  состояние, которое ничем не отличается от текущего.")
            return 0

        print()
        print("=" * 78)
        print(" СНИМОК «ДО» (Этап 9.1, Задача Б)")
        print("=" * 78)
        await _write_snapshot(SNAPSHOT_BEFORE, settle)

        deleted = await db.delete_strategy_outcomes_unsettled(settle_seconds=settle)
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
