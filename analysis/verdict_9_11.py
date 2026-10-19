#!/usr/bin/env python3
"""Этап 9.11, блок 06 — вердикт по критериям ТЗ 9.11 §8 (машинная печать).

Критерии записаны архитектором ДО прогона и здесь не настраиваются. Правило Mi — КАНДИДАТ на новый
тип входа (сначала теневой режим), если выполнены ВСЕ ЧЕТЫРЕ:
  1) среднее net X3 (C = 0,22) > 0 в целом И в каждой половине (до/после 01.04.2024);
  2) среднее net X3 (C = 0,22) > 0 минимум у 4 из 5 монет;
  3) p теста сдвигом ≤ 0,0125 (0,05 / 4 правила — поправка на множественные сравнения);
  4) среднее net X3 (C = 0,22) выше, чем у B0 с тем же выходом.
Справочно, не блокирует: критерий 1 при C = 0,50; результат при V1 = TRUE; результат X1.
Ни одно правило не прошло ⇒ вход на откате на этих настройках не строится; печатается, какое
правило ближе всего и что у него не прошло.

Читает файл фактов run_9_11.sh (строки @@F|ключ|значение, их пишет mr_9_11.py --append).
Только стандартная библиотека. Запуск:  python3 verdict_9_11.py ФАЙЛ_ФАКТОВ
"""

from __future__ import annotations

import sys

RULES = ("M1", "M2", "M3", "M4")
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ALPHA = 0.0125           # §8, критерий 3
MIN_COINS = 4            # §8, критерий 2: не менее 4 из 5
NAMES = {
    1: "ср. net X3 > 0 в целом и в обеих половинах",
    2: "ср. net X3 > 0 у ≥ 4 из 5 монет",
    3: "p теста сдвигом ≤ 0,0125",
    4: "ср. net X3 выше, чем у B0",
}


def read_facts(lines) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in lines:
        if line.startswith("@@F|"):
            _, key, value = line.rstrip("\n").split("|", 2)
            facts[key] = value
    return facts


def num(facts: dict[str, str], key: str) -> float | None:
    try:
        return float(facts.get(key, ""))
    except ValueError:
        return None


def sg(x: float | None) -> str:
    return "нет" if x is None else f"{x:+.3f}"


def pc(x: float | None) -> str:
    return "нет" if x is None else f"{x:.1f} %"


def evaluate(facts: dict[str, str], rule: str) -> dict:
    """Четыре критерия: номер → (выполнен, пояснение с числами)."""
    allv, h1, h2 = (num(facts, f"{rule}.x3_{k}") for k in ("all", "h1", "h2"))
    positive = [c for c in COINS if (num(facts, f"{rule}.x3_c_{c}") or 0.0) > 0.0]
    p = num(facts, f"{rule}.p")
    b0 = num(facts, "B0.x3_all")
    crit = {
        1: (all(v is not None and v > 0 for v in (allv, h1, h2)),
            f"всего {sg(allv)}, до {sg(h1)}, после {sg(h2)}"),
        2: (len(positive) >= MIN_COINS, f"монет с net > 0: {len(positive)} из {len(COINS)}"),
        3: (p is not None and p <= ALPHA, "p = " + ("нет данных" if p is None else f"{p:.4f}")),
        4: (allv is not None and b0 is not None and allv > b0, f"{sg(allv)} против B0 {sg(b0)}"),
    }
    passed = sum(1 for ok, _ in crit.values() if ok)
    return {"rule": rule, "crit": crit, "passed": passed, "mean": allv,
            "candidate": passed == len(crit)}


def reference(facts: dict[str, str], rule: str) -> str:
    c50 = [num(facts, f"{rule}.x3_{k}_c50") for k in ("all", "h1", "h2")]
    stable = all(v is not None and v > 0 for v in c50)
    return (f"{rule} справочно: C = 0,50 — всего {sg(c50[0])}, до {sg(c50[1])}, после {sg(c50[2])} "
            f"({'критерий 1 держится' if stable else 'критерий 1 не держится'}); "
            f"V1 = TRUE: X3 {sg(num(facts, f'{rule}.x3_r_V1=TRUE'))}; "
            f"X1 (без стопа и срока): ср. net {sg(num(facts, f'{rule}.x1_mean'))}, "
            f"дошли до цели {pc(num(facts, f'{rule}.x1_reach'))}")


def render(facts: dict[str, str]) -> list[str]:
    lines: list[str] = []
    results = [evaluate(facts, r) for r in RULES]
    for res in results:
        marks = "; ".join(
            f"{n}) {'✅' if ok else '❌'} {why}" for n, (ok, why) in res["crit"].items())
        lines.append(f"{res['rule']}: {marks}")
        verdict = "кандидат" if res["candidate"] else "не кандидат"
        lines.append(f"{res['rule']}: ИТОГ — {verdict}")
    cands = [r["rule"] for r in results if r["candidate"]]
    if cands:
        lines.append("ИТОГ: кандидат(ы) на теневой режим: " + ", ".join(cands) + ".")
    else:
        best = max(results, key=lambda r: (r["passed"], -1e9 if r["mean"] is None else r["mean"]))
        failed = ", ".join(f"{n}) {NAMES[n]}" for n, (ok, _) in best["crit"].items() if not ok)
        lines.append("ИТОГ: ни одно правило не прошло — вход на откате на этих настройках "
                     "не строится.")
        lines.append(f"Ближе всего {best['rule']}: прошло {best['passed']} из 4; "
                     f"не прошло: {failed}.")
    for r in RULES:
        lines.append(reference(facts, r))
    lines.append("«Кандидат» ≠ «включаем»: следующий шаг — теневой режим, отдельным ТЗ.")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("использование: verdict_9_11.py ФАЙЛ_ФАКТОВ")
        return 2
    with open(args[0], encoding="utf-8") as fh:
        facts = read_facts(fh.read().splitlines())
    for line in render(facts):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
