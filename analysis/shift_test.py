#!/usr/bin/env python3
"""Этап 9.10, блок 05 — тест значимости циклическим сдвигом флагов (ТЗ 9.10, §5).

ЗАЧЕМ. Дни с одним и тем же флагом тренда идут неделями подряд, поэтому обычное перемешивание
дней завысило бы значимость. Здесь флаги сдвигаются по кругу на k дней относительно доходностей:
длительность режимов сохраняется, а связь «флаг — доходность» рвётся. p-значение — доля сдвигов
k = 30 … N−30, при которых разница средних FALSE − TRUE не меньше наблюдаемой.

ВХОД — файл фактов, который пишет run_9_10.sh (строки с другими метками пропускаются):
    @@R04|день|монета|v1|v2|v3|v4|r_%        блок 04: монета×день, дневная доходность, %
    @@R02|день|монета|v1|v2|v3|v4|pnl_%      блок 02: закрытая позиция версии 7, net_pnl_pct
Флаги: 1 — вето (TRUE), 0 — можно (FALSE), пусто — флага нет (NULL).
Отклонение от «CSV day, flag_V1..flag_V4, r_pooled» из ТЗ — одно и сознательное: у V4 флаг свой у
каждой монеты, поэтому строка — монета×день (позиция), а «pooled» делается внутри теста (среднее
по дню для группы флага); V1–V3 от этого не меняются.

Разница считается так: для каждого дня — среднее значение по строкам с флагом FALSE и отдельно
с флагом TRUE; затем среднее этих дневных средних по дням, где группа не пуста; разница
FALSE − TRUE. Для V1–V3 флаг у всех строк дня общий, для V4 — свой у каждой монеты.

ТОЛЬКО СТАНДАРТНАЯ БИБЛИОТЕКА (на хосте нет pandas/numpy): argparse, sys. Тест проверяет список
импортов.

Запуск:  python3 shift_test.py ФАЙЛ_ФАКТОВ [--append]
Печатает текст блока 05; с --append дописывает в файл фактов строки @@F|… с итогами для вердикта.
"""

from __future__ import annotations

import argparse
import sys

RULES = ("V1", "V2", "V3", "V4")
PER_COIN = frozenset({"V4"})     # у V4 флаг свой у каждой монеты; у остальных — общий (BTC)
MIN_SHIFT = 30                   # сдвиги k = 30 … N−30 (ТЗ 9.10, §5)
MIN_DAYS = 90                    # при N < 90 сдвигов недостаточно для вывода (ТЗ 9.10, §5)


def _flag(text: str) -> bool | None:
    return None if text == "" else text == "1"


def read_rows(lines, tag: str) -> list[tuple]:
    """Строки одной метки: (день, монета, (f1..f4), значение)."""
    out = []
    prefix = f"@@{tag}|"
    for line in lines:
        if not line.startswith(prefix):
            continue
        parts = line.rstrip("\n").split("|")
        if len(parts) != 8:
            raise ValueError(f"строка {tag} должна иметь 8 полей: {line!r}")
        flags = tuple(_flag(p) for p in parts[3:7])
        out.append((parts[1], parts[2], flags, float(parts[7])))
    return out


def _diff(sum_f: float, n_f: int, sum_t: float, n_t: int) -> float | None:
    if n_f == 0 or n_t == 0:
        return None
    return sum_f / n_f - sum_t / n_t


def _rotate(seq: list, k: int) -> list:
    """Циклический сдвиг вправо на k: элемент i получает значение элемента i−k."""
    k %= len(seq) or 1
    return seq[-k:] + seq[:-k] if k else list(seq)


class Panel:
    """Ряд наблюдений одного правила: дни по порядку и флаги/значения по дням."""

    def __init__(self, rows: list[tuple], rule: str) -> None:
        idx = RULES.index(rule)
        self.per_coin = rule in PER_COIN
        self.days = sorted({r[0] for r in rows})
        pos = {d: i for i, d in enumerate(self.days)}
        n = len(self.days)
        if self.per_coin:
            self.sums: dict[str, list[float]] = {}
            self.cnts: dict[str, list[int]] = {}
            self.flags: dict[str, list[bool | None]] = {}
            for day, tok, flags, val in rows:
                if tok not in self.sums:
                    self.sums[tok], self.cnts[tok], self.flags[tok] = [0.0] * n, [0] * n, [None] * n
                i = pos[day]
                self.sums[tok][i] += val
                self.cnts[tok][i] += 1
                self.flags[tok][i] = flags[idx]
        else:
            sums, cnts = [0.0] * n, [0] * n
            self.day_flag: list[bool | None] = [None] * n
            for day, _tok, flags, val in rows:
                i = pos[day]
                sums[i] += val
                cnts[i] += 1
                if flags[idx] is not None:
                    self.day_flag[i] = flags[idx]
            self.mean = [s / c if c else None for s, c in zip(sums, cnts, strict=True)]

    def diff(self, k: int = 0) -> float | None:
        """Разница FALSE − TRUE при сдвиге флагов на k дней (k = 0 — наблюдаемая)."""
        n = len(self.days)
        if n == 0:
            return None
        if not self.per_coin:
            sf = st = 0.0
            nf = nt = 0
            for x, f in zip(self.mean, _rotate(self.day_flag, k), strict=True):
                if f is None or x is None:
                    continue
                if f:
                    st += x
                    nt += 1
                else:
                    sf += x
                    nf += 1
            return _diff(sf, nf, st, nt)
        day_sf, day_cf = [0.0] * n, [0] * n
        day_st, day_ct = [0.0] * n, [0] * n
        for tok, sums in self.sums.items():
            cnts = self.cnts[tok]
            for i, f in enumerate(_rotate(self.flags[tok], k)):
                if f is None or cnts[i] == 0:
                    continue
                if f:
                    day_st[i] += sums[i]
                    day_ct[i] += cnts[i]
                else:
                    day_sf[i] += sums[i]
                    day_cf[i] += cnts[i]
        mf = [s / c for s, c in zip(day_sf, day_cf, strict=True) if c]
        mt = [s / c for s, c in zip(day_st, day_ct, strict=True) if c]
        return _diff(sum(mf), len(mf), sum(mt), len(mt))


def shift_test(rows: list[tuple], rule: str, min_shift: int = MIN_SHIFT) -> dict:
    """Наблюдаемая разница, число сдвигов и p (доля сдвигов с разницей не меньше наблюдаемой)."""
    panel = Panel(rows, rule)
    n_days = len(panel.days)
    observed = panel.diff(0)
    shifts = hits = 0
    if observed is not None:
        for k in range(min_shift, n_days - min_shift + 1):
            d = panel.diff(k)
            if d is None:
                continue
            shifts += 1
            if d >= observed:
                hits += 1
    return {"rule": rule, "days": n_days, "obs": observed, "shifts": shifts,
            "p": (hits / shifts) if shifts else None}


def _fmt(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def render(title: str, results: list[dict], note: str) -> list[str]:
    lines = [f"-- {title} --"]
    if note:
        lines.append(note)
    lines.append(" Прав. |  Дней | Набл. разница FALSE − TRUE | Сдвигов |      p")
    for r in results:
        lines.append(f" {r['rule']:<5} | {r['days']:>5} | {_fmt(r['obs'], '+.4f'):>26} | "
                     f"{r['shifts']:>7} | {_fmt(r['p'], '.4f'):>6}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Блок 05: тест сдвигом (ТЗ 9.10)")
    ap.add_argument("facts", help="файл фактов run_9_10.sh")
    ap.add_argument("--append", action="store_true", help="дописать итоги в файл фактов")
    args = ap.parse_args(argv)
    with open(args.facts, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    try:
        rows04 = read_rows(lines, "R04")
        rows02 = read_rows(lines, "R02")
    except ValueError as exc:
        print(f"ОШИБКА: {exc}")
        return 1

    print("Флаги сдвигаются по кругу на k дней относительно доходностей, k = 30 … N−30")
    print("(N — число дней). p — доля сдвигов, где разница FALSE − TRUE не меньше наблюдаемой.")
    print("Положительная разница — вето помогает. p считается, если есть хотя бы один сдвиг")
    print("(N ≥ 60); вывод допустим при N ≥ 90.")
    facts: list[str] = []
    sections = (
        ("История (блок 04), монета×день, доходность в % за день", rows04, "R04"),
        ("Живые сделки версии 7 (блок 02), net_pnl_pct, п.п.", rows02, "R02"),
    )
    for title, rows, tag in sections:
        results = [shift_test(rows, rule) for rule in RULES] if rows else []
        few = bool(rows) and len({r[0] for r in rows}) < MIN_DAYS
        note = "мало дней, справочно (N < 90 — сдвигов недостаточно для вывода)" if few else ""
        if not rows:
            print(f"-- {title} --")
            print("нет данных")
            continue
        for line in render(title, results, note):
            print(line)
        for r in results:
            facts.append(f"@@F|{r['rule']}.p{tag[1:]}|{'' if r['p'] is None else repr(r['p'])}")
            facts.append(f"@@F|{r['rule']}.shifts{tag[1:]}|{r['shifts']}")
            facts.append(f"@@F|{r['rule']}.days{tag[1:]}|{r['days']}")
    if args.append:
        with open(args.facts, "a", encoding="utf-8") as fh:
            fh.write("\n".join(facts) + ("\n" if facts else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
