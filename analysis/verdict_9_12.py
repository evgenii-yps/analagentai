#!/usr/bin/env python3
"""Этап 9.12, блок 05 — вердикт по критериям ТЗ 9.12 §6 (машинная печать).

Критерии записаны архитектором ДО прогона и здесь не настраиваются. Выбранный на первой половине
вариант (K*, H*) — КАНДИДАТ в теневой режим, если на второй половине выполнены ВСЕ ПЯТЬ:
  1) среднее net (C = 0,22) > 0 И медиана net > 0;
  2) среднее net (C = 0,22) > 0 минимум у 4 из 5 монет;
  3) p теста сдвигом ≤ 0,05 (одна подтверждающая гипотеза; выбор сделан на другой половине);
  4) среднее net (C = 0,22) выше среднего B0 с тем же H*;
  5) среднее > 0 не менее чем в 60 % кварталов (с N ≥ 10).
Справочно, не блокирует: устойчивость к издержкам 0,50 %; исходная гипотеза K = 2,5, H = 24.
Выбор не состоялся ⇒ «не кандидат».

Читает файл фактов run_9_12.sh: строку «@@SEL|K|H» (выбор) и строки «@@F|ключ|значение», их пишет
hold_9_12.py --append. Только стандартная библиотека. Запуск:  python3 verdict_9_12.py ФАЙЛ_ФАКТОВ
"""

from __future__ import annotations

import sys

# Пороги — те же, что в блоке КОНСТАНТЫ hold_9_12.py (сверяет tests/test_stage_9_12.py).
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ALPHA = 0.05  # §6.3
MIN_COINS = 4  # §6.2: не менее 4 из 5
QUARTER_SHARE_MIN = 0.60  # §6.5
NAMES = {
    1: "ср. net > 0 и медиана net > 0",
    2: "ср. net > 0 у ≥ 4 из 5 монет",
    3: "p теста сдвигом ≤ 0,05",
    4: "ср. net выше, чем у B0 с тем же H*",
    5: "ср. net > 0 в ≥ 60 % кварталов (N ≥ 10)",
}


def read_facts(lines) -> tuple[tuple[str, str] | None, dict[str, str]]:
    """(выбор (K, H) либо None — «выбор не состоялся» / нет строки, факты)."""
    sel: tuple[str, str] | None = None
    seen = False
    facts: dict[str, str] = {}
    for line in lines:
        if line.startswith("@@SEL|"):
            parts = line.rstrip("\n").split("|")
            seen = True
            sel = (parts[1], parts[2]) if len(parts) == 3 and parts[1] != "-" else None
        elif line.startswith("@@F|"):
            _, key, value = line.rstrip("\n").split("|", 2)
            facts[key] = value
    facts["_sel_seen"] = "1" if seen else "0"
    return sel, facts


def num(facts: dict[str, str], key: str) -> float | None:
    try:
        return float(facts.get(key, ""))
    except ValueError:
        return None


def sg(x: float | None) -> str:
    return "нет" if x is None else f"{x:+.3f}"


def evaluate(facts: dict[str, str]) -> dict[int, tuple[bool, str]]:
    """Пять критериев: номер → (выполнен, пояснение с числами)."""
    mean, median = num(facts, "mean"), num(facts, "median")
    positive = [c for c in COINS if (num(facts, f"coin_{c}_mean") or 0.0) > 0.0]
    p = num(facts, "p")
    b0 = num(facts, "b0_mean")
    q_total, q_pos = num(facts, "q_total"), num(facts, "q_pos")
    share = q_pos / q_total if q_total and q_pos is not None else None
    return {
        1: (mean is not None and median is not None and mean > 0 and median > 0,
            f"ср. {sg(mean)}, мед. {sg(median)}"),
        2: (len(positive) >= MIN_COINS,
            f"монет с ср. net > 0: {len(positive)} из {len(COINS)}"
            + (f" ({', '.join(positive)})" if positive else "")),
        3: (p is not None and p <= ALPHA, "p = " + ("нет данных" if p is None else f"{p:.4f}")),
        4: (mean is not None and b0 is not None and mean > b0,
            f"{sg(mean)} против B0 {sg(b0)}"),
        5: (share is not None and share >= QUARTER_SHARE_MIN,
            "кварталов со средним > 0: "
            + ("нет данных" if share is None
               else f"{int(q_pos)} из {int(q_total)} ({100.0 * share:.0f} %)")),
    }


def reference(facts: dict[str, str]) -> list[str]:
    stable = all((num(facts, k) or 0.0) > 0 for k in ("mean50", "median50"))
    return [
        f"Справочно, C = 0,50: ср. {sg(num(facts, 'mean50'))}, мед. {sg(num(facts, 'median50'))} "
        f"({'издержки выдерживает' if stable else 'издержек 0,50 % не выдерживает'}).",
        f"Справочно, исходная гипотеза K=2.5, H=24 на второй половине: "
        f"N={facts.get('orig_n', 'нет')}, C = 0,22: ср. {sg(num(facts, 'orig_mean'))}, "
        f"мед. {sg(num(facts, 'orig_median'))}; C = 0,50: ср. {sg(num(facts, 'orig_mean50'))}.",
    ]


def render(sel: tuple[str, str] | None, facts: dict[str, str]) -> list[str]:
    if facts.get("_sel_seen") != "1":
        return ["ИТОГ: нет данных — в файле фактов нет строки @@SEL (расчёт блоков 01–04 не "
                "выполнен); не кандидат."]
    if sel is None:
        return [
            "ВЫБОР НЕ СОСТОЯЛСЯ (на первой половине никто не допущен или у лучшего среднее ≤ 0).",
            "ИТОГ: не кандидат. Вторая половина — только справочно (блок 04).",
        ]
    lines = [f"ВЫБОР: K={sel[0]}, H={sel[1]} (сделан на первой половине; на второй проверяется)."]
    crit = evaluate(facts)
    for n, (ok, why) in crit.items():
        lines.append(f"{n}) {'✅' if ok else '❌'} {NAMES[n]}: {why}")
    failed = [n for n, (ok, _) in crit.items() if not ok]
    if not failed:
        lines.append("ИТОГ: кандидат в теневой режим.")
    else:
        lines.append("ИТОГ: не кандидат. Не прошло: "
                     + ", ".join(f"{n}) {NAMES[n]}" for n in failed) + ".")
    lines.extend(reference(facts))
    lines.append("«Кандидат» ≠ «включаем»: следующий шаг — теневой режим на будущих данных, "
                 "отдельным ТЗ.")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("использование: verdict_9_12.py ФАЙЛ_ФАКТОВ")
        return 2
    with open(args[0], encoding="utf-8") as fh:
        sel, facts = read_facts(fh.read().splitlines())
    for line in render(sel, facts):
        print(line)
    return 0 if facts["_sel_seen"] == "1" else 1


if __name__ == "__main__":
    sys.exit(main())
