#!/usr/bin/env python3
"""Этап 9.12 — «падение + удержание»: выбор на старой истории, подтверждение на новой (ТЗ 9.12).

ТОЛЬКО ЧТЕНИЕ и только расчёт: ничего не пишет в базу, ни одно решение системы не меняется.
Только стандартная библиотека (на хосте нет pandas/numpy — правило проекта D-3) и
analysis/mr_9_11.py (ряд, индикаторы r6/s6, издержки — без правок).

ВХОД
  --series  CSV часового ряда (analysis/sql/9_11/01_series.sql):
              token,open_time_utc,open,high,low,close
  --facts   файл фактов run_9_12.sh; с --append сюда дописываются строки «@@SEL|K|H» (выбор —
            СРАЗУ после выбора, до любого расчёта по второй половине) и «@@F|ключ|значение»
            (итоги для verdict_9_12.py)
  --as-of   момент замера (только для подписи)

ВЫХОД — блоки 01–04 отчёта (ТЗ §9) в стандартный вывод; каждая строка печатается сразу.

Методика (правило §3, выход §4, выбор и подтверждение §5, критерии §6) реализована буква в
букву; все числа методики — в блоке КОНСТАНТЫ ниже, с ссылкой на пункт ТЗ. Подбор сверх сетки
§5.1 запрещён (§11).

ТОЛКОВАНИЯ там, где ТЗ допускает два прочтения (все перечислены в docs/STAGE_9_12_REPORT.md):
  * половина сделки определяется по времени ОТКРЫТИЯ бара: бар с открытием ≥ 2024-04-01 00:00 UTC
    — вторая половина; сделка идёт в выбор, только если и бар входа, и бар выхода e+H−1 — в первой;
  * сделка «вход в 1-й, выход во 2-й» не входит ни в выбор, ни в подтверждение (её число — в
    колонке «через границу» блока 01);
  * не завершена: бара выхода нет (пропуск часа) или он позже последнего бара монеты;
  * MAE — глубина просадки по low баров e…e+H−1 в % от цены входа (положительное число;
    отсутствующие бары пропускаются);
  * круг теста сдвигом — бары второй половины общей часовой сетки, от первого бара с открытием
    ≥ 2024-04-01 00:00 UTC до последнего бара данных; B0 в точке круга — сделка «вход по open этого
    часа, выход через H* часов» (нет бара входа/выхода — точка пропускается);
  * квартал — календарный квартал UTC по времени входа; в долю кварталов идут кварталы с N ≥ 10;
  * s6 = 0 — условие не определено (как в mr_9_11).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

try:
    from mr_9_11 import (
        COINS,
        HALF_EPOCH,
        HALF_SPLIT,
        LOCKOUT_BARS,
        R6_LAG,
        R6_SD_WINDOW,
        Coin,
        compute_indicators,
        epoch_to_ts,
        load_series,
        net_of,
        percentile,
    )
except ImportError:  # запуск не из каталога analysis/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mr_9_11 import (
        COINS,
        HALF_EPOCH,
        HALF_SPLIT,
        LOCKOUT_BARS,
        R6_LAG,
        R6_SD_WINDOW,
        Coin,
        compute_indicators,
        epoch_to_ts,
        load_series,
        net_of,
        percentile,
    )

# ==============================================================================================
# КОНСТАНТЫ МЕТОДИКИ — зафиксированы ДО прогона (ТЗ 9.12), менять нельзя
# ==============================================================================================
K_GRID = (2.0, 2.5, 3.0)  # §5.1: порог K — падение за 6 ч в «обычных размахах» s6
H_GRID = (12, 24, 48)  # §5.1: удержание H, часов
MIN_TRADES_SELECT = 150  # §5.2: допуск к выбору N ≥ 150
TIE_EPS = 1e-9  # §5.2: ничья по среднему net — разница до 1e-9
COST_MAIN = 0.22  # §4: главная ставка издержек, % за круг
COST_STRESS = 0.50  # §4: стресс-ставка
# §5.2: граница половин — mr_9_11.HALF_EPOCH = 2024-04-01 00:00 UTC (HALF_SPLIT)
SHIFT_MIN_DAYS = 30  # §5.3: сдвиги k × 24 ч, k = 30 … N_дней − 30
SHIFT_STEP_HOURS = 24  # §5.3: шаг сдвига — сутки
ALPHA = 0.05  # §6.3: порог p теста сдвигом (одна подтверждающая гипотеза)
MIN_COINS_POSITIVE = 4  # §6.2: среднее net > 0 не менее чем у 4 из 5 монет
MIN_QUARTER_TRADES = 10  # §6.5: в долю кварталов идут кварталы с N ≥ 10
QUARTER_SHARE_MIN = 0.60  # §6.5: среднее > 0 не менее чем в 60 % таких кварталов
ORIG_K = 2.5  # §5.3: исходная гипотеза (справочно)
ORIG_H = 24
# ==============================================================================================

HOUR = 3600
DAY = 86400
FIRST, SECOND = "1-я", "2-я"
VARIANTS = tuple((k, h) for k in K_GRID for h in H_GRID)


class Trade:
    """Завершённая сделка: монета, бар входа, время входа/выхода (эпоха), итог без издержек, MAE."""

    __slots__ = ("coin", "e", "start", "stop", "gross", "mae")

    def __init__(self, coin: str, e: int, start: int, stop: int, gross: float, mae: float) -> None:
        self.coin, self.e, self.start, self.stop = coin, e, start, stop
        self.gross, self.mae = gross, mae


# ----------------------------------------------------------------------------------------------
# Правило входа (§3) и выход (§4)
# ----------------------------------------------------------------------------------------------


def cond_k(coin: Coin, k: float) -> list[bool | None]:
    """cond_K[t] = (r6[t] ≤ −K × s6[t]) при известных r6, s6 > 0; иначе значения нет (§3)."""
    out: list[bool | None] = [None] * coin.n
    for t in range(coin.n):
        r6, s6 = coin.r6[t], coin.s6[t]
        if r6 is not None and s6 is not None and s6 > 0.0:
            out[t] = r6 <= -k * s6
    return out


def find_events(coin: Coin, k: float) -> list[int]:
    """Бары сигнала t — ровно как mr_9_11.find_events: условие истинно на t и ИЗВЕСТНО ложно на
    t−1; ≥ 12 баров от прошлого события; бар входа t+1 существует."""
    cond = cond_k(coin, k)
    events: list[int] = []
    last = -(10**9)
    for t in range(1, coin.n - 1):
        if cond[t] is True and cond[t - 1] is False:
            if t - last >= LOCKOUT_BARS:
                if coin.ok[t + 1]:
                    events.append(t)
                    last = t
    return events


def exit_bar(e: int, h: int) -> int:
    """Бар выхода: close бара e + H − 1, то есть ровно H часов после открытия бара входа (§4)."""
    return e + h - 1


def make_trade(coin: Coin, e: int, h: int) -> Trade | None:
    """Сделка со входом по open бара e и выходом по close бара e+H−1; None — не завершена."""
    j = exit_bar(e, h)
    if j > coin.last_valid or not coin.ok[e] or not coin.ok[j]:
        return None
    entry = coin.o[e]
    gross = (coin.c[j] / entry - 1.0) * 100.0
    low = min(coin.l[i] for i in range(e, j + 1) if coin.ok[i])
    mae = max(0.0, (entry - low) / entry * 100.0)
    return Trade(coin.name, e, coin.time_of(e), coin.time_of(j), gross, mae)


# ----------------------------------------------------------------------------------------------
# Половины (§5.2, §5.3)
# ----------------------------------------------------------------------------------------------


def counts_for_selection(start: int, stop: int) -> bool:
    """§5.2: в выбор идёт сделка, у которой И вход, И бар выхода — в первой половине."""
    return start < HALF_EPOCH and stop < HALF_EPOCH


def counts_for_confirmation(start: int) -> bool:
    """§5.3: подтверждение — вход во второй половине."""
    return start >= HALF_EPOCH


def first_half_trades(
    coins: dict[str, Coin], events: dict[str, list[int]], h: int
) -> tuple[list[Trade], int, int]:
    """Сделки для выбора: (сделки, не завершены, через границу). Цены читаются только у тех
    сделок, что уже прошли проверку по времени."""
    trades: list[Trade] = []
    unfinished = cut = 0
    for name, coin in coins.items():
        for t in events[name]:
            e = t + 1
            start, stop = coin.time_of(e), coin.time_of(exit_bar(e, h))
            if not counts_for_selection(start, stop):
                if start < HALF_EPOCH:
                    cut += 1
                continue
            tr = make_trade(coin, e, h)
            if tr is None:
                unfinished += 1
            else:
                trades.append(tr)
    return trades, unfinished, cut


def second_half_trades(
    coins: dict[str, Coin], events: dict[str, list[int]], h: int
) -> tuple[list[Trade], int]:
    """Сделки подтверждения: (сделки, не завершены)."""
    trades: list[Trade] = []
    unfinished = 0
    for name, coin in coins.items():
        for t in events[name]:
            e = t + 1
            if not counts_for_confirmation(coin.time_of(e)):
                continue
            tr = make_trade(coin, e, h)
            if tr is None:
                unfinished += 1
            else:
                trades.append(tr)
    return trades, unfinished


# ----------------------------------------------------------------------------------------------
# Выбор варианта — ТОЛЬКО по сделкам первой половины (§5.2)
# ----------------------------------------------------------------------------------------------


def mean_net(trades: list[Trade], cost: float = COST_MAIN) -> float | None:
    return sum(net_of(t.gross, cost) for t in trades) / len(trades) if trades else None


def rank_variants(
    trades_first_half: dict[tuple[float, int], list[Trade]],
) -> list[tuple[float, int, int, float | None, bool]]:
    """(K, H, N, среднее net, допущен) по каждому варианту сетки."""
    rows = []
    for k, h in VARIANTS:
        trs = trades_first_half.get((k, h), [])
        rows.append((k, h, len(trs), mean_net(trs), len(trs) >= MIN_TRADES_SELECT))
    return rows


def select_variant(
    trades_first_half: dict[tuple[float, int], list[Trade]],
) -> tuple[float, int] | None:
    """(K, H) с наибольшим средним net среди допущенных (N ≥ 150); ничья до 1e-9 — меньший K,
    затем меньший H. None — «выбор не состоялся»: никто не допущен или у выбранного среднее ≤ 0.
    Получает ТОЛЬКО сделки первой половины."""
    admitted = [r for r in rank_variants(trades_first_half) if r[4] and r[3] is not None]
    if not admitted:
        return None
    best = max(r[3] for r in admitted)
    k, h = min((r[0], r[1]) for r in admitted if r[3] >= best - TIE_EPS)
    chosen = next(r[3] for r in admitted if (r[0], r[1]) == (k, h))
    return (k, h) if chosen > 0 else None


# ----------------------------------------------------------------------------------------------
# Статистика и подтверждение (§5.3)
# ----------------------------------------------------------------------------------------------


def summary(trades: list[Trade], cost: float) -> dict:
    nets = [net_of(t.gross, cost) for t in trades]
    n = len(nets)
    if not n:
        return {"n": 0}
    srt = sorted(nets)
    return {
        "n": n,
        "n_eff": len({t.start // DAY for t in trades}),
        "pos": 100.0 * sum(1 for x in nets if x > 0) / n,
        "mean": sum(nets) / n,
        "median": percentile(srt, 0.5),
        "worst": srt[0],
        "p5": percentile(srt, 0.05),
    }


def quarter_of(epoch: int) -> str:
    d = datetime.fromtimestamp(epoch, UTC)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def quarter_table(trades: list[Trade]) -> list[tuple[str, int, float]]:
    """(квартал, N, среднее net при C = 0,22) в хронологическом порядке."""
    buckets: dict[str, list[Trade]] = {}
    for t in trades:
        buckets.setdefault(quarter_of(t.start), []).append(t)
    return [(q, len(v), mean_net(v) or 0.0) for q, v in sorted(buckets.items())]


def quarters_positive(rows: list[tuple[str, int, float]]) -> tuple[int, int]:
    """(кварталов с N ≥ 10, из них со средним > 0)."""
    big = [r for r in rows if r[1] >= MIN_QUARTER_TRADES]
    return len(big), sum(1 for r in big if r[2] > 0)


def half_start_bar(t0: int) -> int:
    """Номер первого бара общей сетки с открытием ≥ границы половин."""
    return max(0, -((t0 - HALF_EPOCH) // HOUR))


def b0_gross(coins: dict[str, Coin], h: int, i0: int) -> dict[str, list[float | None]]:
    """B0: вход в каждый час каждой монеты, начиная с бара i0, по open; выход по close через H
    часов. gross по номеру бара входа (нет бара входа/выхода — None)."""
    out: dict[str, list[float | None]] = {}
    for name, coin in coins.items():
        arr: list[float | None] = [None] * coin.n
        for e in range(max(i0, coin.first_valid), coin.last_valid + 1):
            if coin.ok[e]:
                tr = make_trade(coin, e, h)
                if tr is not None:
                    arr[e] = tr.gross
        out[name] = arr
    return out


def b0_mean(b0: dict[str, list[float | None]]) -> tuple[int, float | None]:
    vals = [net_of(g, COST_MAIN) for arr in b0.values() for g in arr if g is not None]
    return len(vals), (sum(vals) / len(vals) if vals else None)


def significant(p: float | None) -> bool:
    """§6.3: p теста сдвигом ≤ 0,05."""
    return p is not None and p <= ALPHA


def shift_test(
    events: list[tuple[str, int]],
    obs: float | None,
    b0: dict[str, list[float | None]],
    i0: int,
    n_bars: int,
) -> dict:
    """p теста циклическим сдвигом (§5.3). events — (монета, бар входа ≥ i0) выбранного варианта;
    obs — их среднее net; круг — только бары i0…n_bars−1 (вторая половина); сдвиг k × 24 ч,
    k = 30 … N_дней − 30; статистика — среднее net B0(H*) в сдвинутых точках;
    p = доля сдвигов со средним ≥ наблюдаемого. Без случайных чисел."""
    length = n_bars - i0
    n_days = length // 24
    shifts = hits = 0
    if obs is not None and length > 0:
        for k in range(SHIFT_MIN_DAYS, n_days - SHIFT_MIN_DAYS + 1):
            s = SHIFT_STEP_HOURS * k
            total, cnt = 0.0, 0
            for name, e in events:
                g = b0[name][i0 + (e - i0 + s) % length]
                if g is not None:
                    total += net_of(g, COST_MAIN)
                    cnt += 1
            if not cnt:
                continue
            shifts += 1
            if total / cnt >= obs:
                hits += 1
    return {
        "n": len(events),
        "obs": obs,
        "shifts": shifts,
        "p": (hits / shifts) if shifts else None,
    }


def confirm(coins: dict[str, Coin], trades: list[Trade], h_star: int) -> dict:
    """Всё, что требуют критерии §6, для выбранного варианта на второй половине."""
    i0 = half_start_bar(next(iter(coins.values())).t0)
    n_bars = next(iter(coins.values())).n
    main = summary(trades, COST_MAIN)
    res: dict = {"main": main, "stress": summary(trades, COST_STRESS)}
    res["mae"] = sorted(t.mae for t in trades)
    res["coins"] = {c: summary([t for t in trades if t.coin == c], COST_MAIN) for c in coins}
    res["quarters"] = quarter_table(trades)
    res["q_total"], res["q_pos"] = quarters_positive(res["quarters"])
    b0 = b0_gross(coins, h_star, i0)
    res["b0_n"], res["b0_mean"] = b0_mean(b0)
    res["shift"] = shift_test(
        [(t.coin, t.e) for t in trades], main.get("mean"), b0, i0, n_bars
    )
    return res


# ----------------------------------------------------------------------------------------------
# Печать
# ----------------------------------------------------------------------------------------------


def f2(x: float | None, digits: int = 2, sign: bool = False) -> str:
    if x is None:
        return "-"
    return format(x, f"{'+' if sign else ''}.{digits}f")


def kh(k: float, h: int) -> str:
    return f"K={k:.1f}, H={h}"


def table(emit, header: list[str], rows: list[list[str]]) -> None:
    rows = [r + ["-"] * (len(header) - len(r)) for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    emit(" " + " | ".join(h.rjust(w) for h, w in zip(header, widths, strict=True)))
    for r in rows:
        emit(" " + " | ".join(c.rjust(w) for c, w in zip(r, widths, strict=True)))


def _stat_rows(trades: list[Trade]) -> list[list[str]]:
    rows = []
    for cost in (COST_MAIN, COST_STRESS):
        s = summary(trades, cost)
        if not s["n"]:
            rows.append([f"{cost:.2f}", "0"])
            continue
        rows.append([
            f"{cost:.2f}", str(s["n"]), str(s["n_eff"]), f2(s["pos"], 1), f2(s["mean"], 3, True),
            f2(s["median"], 3, True), f2(s["worst"]), f2(s["p5"]),
        ])
    return rows


STAT_HEAD = ["C, %", "N", "n_eff", "+%", "Ср.", "Мед.", "Худш.", "p5"]


def append_facts(path: str | None, lines: list[str]) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(f"{ln}\n" for ln in lines))
            fh.flush()


def run(series: str, facts_path: str | None, as_of: str, emit=print) -> int:
    coins = {k: v for k, v in load_series(series).items() if k in COINS}
    coins = {k: coins[k] for k in COINS if k in coins}
    half_date = HALF_SPLIT
    if not coins:
        emit("===== 01 — НЕТ ДАННЫХ ===== нет ряда: расчёт невозможен")
        return 1
    for coin in coins.values():
        compute_indicators(coin)
    events = {k: {name: find_events(c, k) for name, c in coins.items()} for k in K_GRID}

    # --- 01 — выбор на первой половине ------------------------------------------------------
    emit(f"===== 01 — ВЫБОР НА ПЕРВОЙ ПОЛОВИНЕ (до {half_date} 00:00 UTC) =====")
    first_coin = next(iter(coins.values()))
    t_first, t_last = first_coin.t0, first_coin.time_of(first_coin.n - 1)
    emit(f"Данные: {epoch_to_ts(t_first)} … {epoch_to_ts(t_last)} "
         f"UTC, монеты {', '.join(coins)}; момент замера {as_of or '-'}.")
    emit(f"Вход: r6 ≤ −K × s6 "
         f"(r6 — за {R6_LAG} ч, s6 — разброс r6 за {R6_SD_WINDOW} ч), пауза между событиями "
         f"≥ {LOCKOUT_BARS} баров, цена — открытие следующего бара.")
    emit(f"Выход — close бара e+H−1; издержки C = {COST_MAIN} %. В выбор идут сделки с входом и "
         "выходом в первой половине;")
    emit(f"допуск N ≥ {MIN_TRADES_SELECT}; побеждает наибольшее среднее net, ничья → меньший K, "
         "затем меньший H.")
    first: dict[tuple[float, int], list[Trade]] = {}
    side: dict[tuple[float, int], tuple[int, int]] = {}
    for k, h in VARIANTS:
        trs, unfinished, cut = first_half_trades(coins, events[k], h)
        first[(k, h)] = trs
        side[(k, h)] = (unfinished, cut)
    ranked = rank_variants(first)
    table(emit, ["K", "H", "N", "не завершено", "через границу", "ср. net, %", "допуск"], [
        [f"{k:.1f}", str(h), str(n), str(side[(k, h)][0]), str(side[(k, h)][1]),
         f2(m, 3, True), "да" if adm else "нет"]
        for k, h, n, m, adm in ranked
    ])
    chosen = select_variant(first)
    facts: dict[str, str] = {}
    if chosen is None:
        adm = [r for r in ranked if r[4] and r[3] is not None]
        why = "никто не допущен (N < 150)" if not adm else "у лучшего варианта среднее ≤ 0"
        emit(f"ВЫБОР НЕ СОСТОЯЛСЯ: {why}.")
        append_facts(facts_path, ["@@SEL|-|-"])
    else:
        k_star, h_star = chosen
        sel_n = len(first[chosen])
        emit(f"ВЫБРАНО: {kh(k_star, h_star)} (1-я половина: N={sel_n}, "
             f"ср. net {f2(mean_net(first[chosen]), 3, True)} %)")
        facts["sel_n"] = str(sel_n)
        facts["sel_mean"] = repr(mean_net(first[chosen]))
        append_facts(facts_path, [f"@@SEL|{k_star:.1f}|{h_star}"])    # ДО расчёта по 2-й половине

    # --- 02 — подтверждение на второй половине ---------------------------------------------
    emit("")
    emit(f"===== 02 — ПОДТВЕРЖДЕНИЕ НА ВТОРОЙ ПОЛОВИНЕ (с {half_date} 00:00 UTC) =====")
    res = None
    if chosen is None:
        emit("Выбор не состоялся: подтверждение не проводится, вердикт «не кандидат».")
        emit("Вторая половина — только справочно (блок 04).")
        trades2 = unfinished2 = None
    else:
        k_star, h_star = chosen
        trades2, unfinished2 = second_half_trades(coins, events[k_star], h_star)
        res = confirm(coins, trades2, h_star)
        emit(f"Выбранный вариант: {kh(k_star, h_star)}. Не завершено (нет бара выхода или конец "
             f"данных): {unfinished2}.")
        table(emit, STAT_HEAD, _stat_rows(trades2))
        mae = res["mae"]
        emit("MAE (глубина просадки по low за удержание, %): "
             f"медиана {f2(percentile(mae, 0.5), 2)}, "
             f"p90 {f2(percentile(mae, 0.9), 2)}, максимум {f2(mae[-1] if mae else None, 2)}")
        emit(f"-- По монетам (C = {COST_MAIN}) --")
        table(emit, ["Монета", "N", "ср. net, %"], [
            [c, str(res["coins"][c].get("n", 0)), f2(res["coins"][c].get("mean"), 3, True)]
            for c in coins
        ])
        emit(f"-- По кварталам (C = {COST_MAIN}) --")
        table(emit, ["Квартал", "N", "ср. net, %"],
              [[q, str(n), f2(m, 3, True)] for q, n, m in res["quarters"]])
        share = res["q_pos"] / res["q_total"] if res["q_total"] else None
        emit(f"Кварталов с N ≥ {MIN_QUARTER_TRADES}: {res['q_total']}, со средним > 0: "
             f"{res['q_pos']}" + ("" if share is None else f" ({100.0 * share:.0f} %)"))
        emit(f"-- B0: вход в каждый час второй половины, выход через H* = {h_star} ч, "
             f"C = {COST_MAIN} --")
        emit(f"B0: N={res['b0_n']}, ср. net {f2(res['b0_mean'], 3, True)} %")
        main, stress = res["main"], res["stress"]
        facts.update({
            "n": str(main.get("n", 0)), "n_eff": str(main.get("n_eff", 0)),
            "unfinished": str(unfinished2),
            "mean": repr(main.get("mean")), "median": repr(main.get("median")),
            "mean50": repr(stress.get("mean")), "median50": repr(stress.get("median")),
            "b0_mean": repr(res["b0_mean"]), "q_total": str(res["q_total"]),
            "q_pos": str(res["q_pos"]),
        })
        for c in COINS:
            s = res["coins"].get(c, {"n": 0})
            facts[f"coin_{c}_n"] = str(s.get("n", 0))
            facts[f"coin_{c}_mean"] = repr(s.get("mean"))

    # --- 03 — тест сдвигом -----------------------------------------------------------------
    emit("")
    emit("===== 03 — ТЕСТ СДВИГОМ =====")
    if res is None:
        emit("Не проводится: нет выбранного варианта.")
    else:
        sh = res["shift"]
        emit(f"Набор событий второй половины сдвигается по кругу (только бары второй половины) на "
             f"k × {SHIFT_STEP_HOURS} ч, k = {SHIFT_MIN_DAYS} … N_дней − {SHIFT_MIN_DAYS};")
        emit(f"статистика — ср. net B0 (H* = {chosen[1]}) в сдвинутых точках, C = {COST_MAIN}; "
             "p = доля сдвигов со средним ≥ наблюдаемого. Случайных чисел нет.")
        table(emit, ["Событий", "Набл. ср. net", "Сдвигов", "p", f"p ≤ {ALPHA}"], [[
            str(sh["n"]), f2(sh["obs"], 3, True), str(sh["shifts"]), f2(sh["p"], 4),
            "да" if significant(sh["p"]) else "нет",
        ]])
        facts["p"] = "" if sh["p"] is None else repr(sh["p"])
        facts["shifts"] = str(sh["shifts"])

    # --- 04 — сетка целиком и исходная гипотеза --------------------------------------------
    emit("")
    emit("===== 04 — СЕТКА ЦЕЛИКОМ (1-я | 2-я половина) И ИСХОДНАЯ ГИПОТЕЗА K=2.5, H=24 =====")
    emit(f"Ср. net, % при C = {COST_MAIN}; N — завершённых сделок. Вторая половина считается "
         "после выбора и на него не влияет.")
    rows = []
    orig_trades: list[Trade] = []
    for (k, h), (_k, _h, n1, m1, _adm) in zip(VARIANTS, ranked, strict=True):
        if chosen is not None and (k, h) == chosen and trades2 is not None:
            t2 = trades2
        else:
            t2, _u = second_half_trades(coins, events[k], h)
        if (k, h) == (ORIG_K, ORIG_H):
            orig_trades = t2
        rows.append([f"{k:.1f}", str(h), str(n1), f2(m1, 3, True), str(len(t2)),
                     f2(mean_net(t2), 3, True), "<< выбран" if (k, h) == chosen else ""])
    table(emit, ["K", "H", "N 1-я", "ср. net 1-я", "N 2-я", "ср. net 2-я", ""], rows)
    emit(f"-- Исходная гипотеза {kh(ORIG_K, ORIG_H)} на второй половине (справочно, на критерии "
         "не влияет) --")
    table(emit, STAT_HEAD, _stat_rows(orig_trades))
    for cost, tag in ((COST_MAIN, ""), (COST_STRESS, "50")):
        s = summary(orig_trades, cost)
        facts[f"orig_n{tag}"] = str(s["n"])
        facts[f"orig_mean{tag}"] = repr(s.get("mean"))
        facts[f"orig_median{tag}"] = repr(s.get("median"))
    append_facts(facts_path, [f"@@F|{k}|{v}" for k, v in sorted(facts.items())])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Этап 9.12: падение + удержание (ТЗ 9.12)")
    ap.add_argument("--series", required=True)
    ap.add_argument("--facts", default=None)
    ap.add_argument("--as-of", default="")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args(argv)

    def emit(line: str) -> None:
        print(line, flush=True)

    return run(args.series, args.facts if args.append else None, args.as_of, emit)


if __name__ == "__main__":
    sys.exit(main())
