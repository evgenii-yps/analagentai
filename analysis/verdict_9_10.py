#!/usr/bin/env python3
"""Этап 9.10, блок 07 — вердикт по критериям ТЗ 9.10 §6 (машинная печать).

Критерии записаны архитектором ДО прогона и здесь не настраиваются. Правило V1…V4 признаётся
КАНДИДАТОМ на теневое вето, если выполнены ВСЕ ЧЕТЫРЕ:
  1) история, сводно: разница FALSE − TRUE (блок 04) > 0 в обеих половинах времени
     (граница 2024-04-01);
  2) история, значимость: p теста сдвигом (блок 05) ≤ 0,05;
  3) история, по монетам: разница > 0 минимум у 4 из 5 монет;
  4) живые сделки версии 7 (блок 02): средний net_pnl_pct под вето НИЖЕ, чем у разрешённых.
Если хоть один контроль данных ТЗ §4.4 (блок 00) не пройден — вердикт не печатается.
«Кандидат» ≠ «включаем»: следующий шаг при успехе — теневой режим отдельным ТЗ.

Читает файл фактов run_9_10.sh (строки @@F|ключ|значение). Только стандартная библиотека.
Запуск:  python3 verdict_9_10.py ФАЙЛ_ФАКТОВ
"""

from __future__ import annotations

import sys

RULES = ("V1", "V2", "V3", "V4")
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ALPHA = 0.05            # критерий 2
MIN_COINS = 4           # критерий 3: минимум 4 из 5
CONTROLS = (("ctl1", "сутки из часов = 1Dutc биржи"),
            ("ctl2", "контрольный опыт со сдвигом суток обязан разойтись"),
            ("ctl3", "шов H-hist / H-live"))


def read_facts(lines) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in lines:
        if line.startswith("@@F|"):
            _, key, value = line.rstrip("\n").split("|", 2)
            facts[key] = value
    return facts


def _num(facts: dict[str, str], key: str) -> float | None:
    raw = facts.get(key, "")
    try:
        return float(raw)
    except ValueError:
        return None


def _sign(value: float | None) -> str:
    return "нет данных" if value is None else f"{value:+.4f}"


def evaluate(facts: dict[str, str], rule: str) -> dict:
    """Четыре критерия для одного правила: (выполнен, пояснение)."""
    h1, h2 = _num(facts, f"{rule}.d_h1"), _num(facts, f"{rule}.d_h2")
    p = _num(facts, f"{rule}.p04")
    coins = {c: _num(facts, f"{rule}.c_{c}") for c in COINS}
    positive = sum(1 for v in coins.values() if v is not None and v > 0)
    ok_live, veto = _num(facts, f"{rule}.live_ok"), _num(facts, f"{rule}.live_veto")
    crit = {
        1: (h1 is not None and h2 is not None and h1 > 0 and h2 > 0,
            f"половины {_sign(h1)} / {_sign(h2)}"),
        2: (p is not None and p <= ALPHA, "p = " + ("нет данных" if p is None else f"{p:.4f}")),
        3: (positive >= MIN_COINS, f"монет с разницей > 0: {positive} из {len(COINS)}"),
        4: (ok_live is not None and veto is not None and veto < ok_live,
            f"вето {_sign(veto)} против разрешённых {_sign(ok_live)}"),
    }
    return {"rule": rule, "crit": crit, "candidate": all(v[0] for v in crit.values())}


def render(facts: dict[str, str]) -> list[str]:
    lines: list[str] = []
    bad = [name for key, name in CONTROLS if facts.get(key) != "1"]
    for key, name in CONTROLS:
        lines.append(f"  контроль данных: {name}: " + ("✅" if facts.get(key) == "1" else "❌"))
    if bad:
        lines.append("ВЕРДИКТ НЕ ПЕЧАТАЕТСЯ: данные не прошли контроль")
        return lines
    cands = []
    for rule in RULES:
        res = evaluate(facts, rule)
        marks = "  ".join(
            f"{n}) {'✅' if ok else '❌'} {why}" for n, (ok, why) in res["crit"].items())
        lines.append(f"{rule}: {marks}")
        verdict = "КАНДИДАТ на теневое вето" if res["candidate"] else "не кандидат"
        lines.append(f"{rule}: ИТОГ — {verdict}")
        if res["candidate"]:
            cands.append(rule)
    if not cands:
        lines.append("ИТОГ: ни одно правило не прошло — вето не строится (решено заранее).")
    elif "V1" in cands:
        rest = ", ".join(r for r in cands if r != "V1")
        also = f"; справочно также: {rest}." if rest else "."
        lines.append("ИТОГ: кандидат V1 — главное правило" + also)
    else:
        lines.append(f"ИТОГ: прошли {', '.join(cands)}, но V1 (главное по ТЗ) не прошло — "
                     "решает архитектор.")
    lines.append("«Кандидат» ≠ «включаем»: следующий шаг — теневой режим на живых сделках, "
                 "отдельным ТЗ.")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("использование: verdict_9_10.py ФАЙЛ_ФАКТОВ")
        return 2
    with open(args[0], encoding="utf-8") as fh:
        facts = read_facts(fh.read().splitlines())
    for line in render(facts):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
