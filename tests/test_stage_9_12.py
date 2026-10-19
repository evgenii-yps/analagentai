"""Этап 9.12: «резкое падение + удержание» — выбор на старой истории, подтверждение на новой.

Только чтение (ТЗ 9.12, §1). Расчёт (analysis/hold_9_12.py) сверяется с ОТДЕЛЬНОЙ простой
реализацией по тексту ТЗ (``ref_*``): она написана иначе — по словарю баров и пересчётом с
нуля. Мутации (ТЗ 9.12, §7.7) — копии модуля с одной намеренной поломкой: проверка обязана на
них упасть, иначе она ничего не проверяет. Список мутаций — в конце файла и в
docs/STAGE_9_12_REPORT.md.

Сети, биржи и docker нет; SQL и скрипты проверяются на настоящей PostgreSQL, ``docker compose
exec ... psql`` подменяется заглушкой. Стенд (синтетика) — не доказательство для сервера.
"""

# ruff: noqa: F811 — фикстуры pool и db_name импортированы из тестов 9.10 и используются как параметры
from __future__ import annotations

import ast
import copy
import importlib
import os
import random
import re
import resource
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.demo_support import ROOT
from tests.test_stage_9_10 import (  # noqa: F401 — фикстуры и помощники этапа 9.10
    db_name,
    fingerprint,
    make_stub,
    pool,
    run_script,
    script_env,
)
from tests.test_stage_9_11 import gen_bars, load_module, needs_bash, needs_pg, ref_r6, ref_s6

sys.path.insert(0, str(ROOT / "analysis"))      # hold_9_12.py импортирует соседний mr_9_11.py

HOLD = ROOT / "analysis" / "hold_9_12.py"
VERDICT = ROOT / "analysis" / "verdict_9_12.py"
RUN_9_12 = ROOT / "analysis" / "run_9_12.sh"
VERIFY = {s: ROOT / "deploy" / f"verify_{s}.sh" for s in ("9_10", "9_11", "9_12")}
BASE_COMMIT = "5ab283a"        # main, на котором основано ТЗ 9.12

HOUR = 3600
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
T0 = datetime(2023, 1, 1, tzinfo=UTC)           # бар 0 больших синтетических рядов
HALF = datetime(2024, 4, 1, tzinfo=UTC)         # граница половин (ТЗ §5.2)
HALF_BAR = int((HALF - T0).total_seconds() // HOUR)          # 10944
N_RUN = HALF_BAR + 2000                                      # вторая половина — 2000 баров


@pytest.fixture(scope="module")
def hold():
    return load_module(HOLD, "hold_real")


@pytest.fixture(scope="module")
def mr9():
    return importlib.import_module("mr_9_11")


# --------------------------------------------------------------------------
# Синтетические ряды
# --------------------------------------------------------------------------


def write_csv(path: Path, series: dict[str, dict], t0: datetime = T0) -> None:
    lines = ["token,open_time_utc,open,high,low,close"]
    for token in sorted(series):
        for i in sorted(series[token]):
            o, h, lo, c = series[token][i]
            ts = (t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{token},{ts},{o!r},{h!r},{lo!r},{c!r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_series(n: int = N_RUN, *, seed: int = 40) -> dict[str, dict]:
    """5 монет по n часов: шум, обвалы с отскоком, пропуск часа, плоский участок."""
    return {
        tok: gen_bars(seed + k, n, start=100.0 + k, missing={500 + 7 * k},
                      flat=[(3000 + 11 * k, 3040 + 11 * k)])
        for k, tok in enumerate(COINS)
    }


def load_coins(hold, tmp_path: Path, series: dict[str, dict], t0: datetime = T0) -> dict:
    csv_path = tmp_path / "series.csv"
    write_csv(csv_path, series, t0)
    return hold.load_series(str(csv_path))


def hand_coin(mr9, n: int = 12, t0: int | None = None):
    """Монета из рук: o = 100,5 + i; h = 102 + i; l = 99 + i; c = 101 + i."""
    t0 = mr9.HALF_EPOCH - 12 * HOUR if t0 is None else t0
    coin = mr9.Coin("X", t0, n)
    for i in range(n):
        coin.o[i], coin.h[i], coin.l[i], coin.c[i] = 100.5 + i, 102.0 + i, 99.0 + i, 101.0 + i
        coin.ok[i] = True
    coin.first_valid, coin.last_valid = 0, n - 1
    return coin


# --------------------------------------------------------------------------
# Независимая реализация правила входа по тексту ТЗ (эталон)
# --------------------------------------------------------------------------


def ref_events(bars: dict, k: float, n: int) -> list[int]:
    """События семейства M2 при пороге K: cond[t] = r6 ≤ −K × s6 (s6 > 0), перед ним cond[t−1]
    известно и ложно, пауза ≥ 12 баров, бар t+1 существует."""
    cond: dict[int, bool] = {}
    for t in range(n):
        r, s = ref_r6(bars, t), ref_s6(bars, t)
        if r is not None and s is not None and s > 0:
            cond[t] = r <= -k * s
    events, last = [], -10**9
    for t in range(1, n - 1):
        if cond.get(t) is True and cond.get(t - 1) is False and t - last >= 12 and t + 1 in bars:
            events.append(t)
            last = t
    return events


# --------------------------------------------------------------------------
# §7.1. Совпадение с 9.11
# --------------------------------------------------------------------------


def test_events_at_k_2_5_equal_the_9_11_rule_m2(hold, mr9, tmp_path) -> None:
    series = {tok: gen_bars(60 + k, 2600, start=50.0 + k, missing={900, 901 + k},
                            flat=[(1500, 1540), (2000 + 10 * k, 2030 + 10 * k)])
              for k, tok in enumerate(COINS)}
    coins = load_coins(hold, tmp_path, series)
    total = 0
    for name, coin in coins.items():
        mr9.compute_indicators(coin)
        expected = mr9.find_events(coin, "M2")
        assert hold.find_events(coin, 2.5) == expected, name
        total += len(expected)
    assert total >= 30                                       # сравнение не пустое


@pytest.mark.parametrize("k", [2.0, 2.5, 3.0])
def test_events_match_an_independent_implementation_for_every_k(hold, mr9, tmp_path, k) -> None:
    n = 3600
    series = {"BTC": gen_bars(5, n, missing={1000, 1500, 1501}, flat=[(1800, 1850)]),
              "ETH": gen_bars(6, n, start=30.0)}
    coins = load_coins(hold, tmp_path, series)
    total = 0
    for name, coin in coins.items():
        mr9.compute_indicators(coin)
        ref = ref_events(series[name], k, n)
        assert hold.find_events(coin, k) == ref, (name, k)
        total += len(ref)
    assert total >= 8


def test_a_higher_threshold_gives_fewer_or_equal_events_on_the_same_data(
    hold, mr9, tmp_path
) -> None:
    coins = load_coins(hold, tmp_path, {"BTC": gen_bars(8, 4000)})
    coin = coins["BTC"]
    mr9.compute_indicators(coin)
    counts = [len(hold.find_events(coin, k)) for k in (2.0, 2.5, 3.0)]
    assert counts[0] >= counts[1] >= counts[2] > 0


# --------------------------------------------------------------------------
# §7.2. Выход
# --------------------------------------------------------------------------


def check_exit_rules(hold, mr9) -> None:
    """Выход по close[e+H−1]; пропуск бара выхода и конец данных — не завершена; издержки раз."""
    coin = hand_coin(mr9)
    tr = hold.make_trade(coin, 2, 3)                    # вход open[2] = 102,5, выход close[4] = 105
    assert tr is not None and tr.e == 2 and tr.coin == "X"
    assert tr.gross == pytest.approx((105.0 / 102.5 - 1.0) * 100.0)
    assert tr.start == coin.time_of(2) and tr.stop == coin.time_of(4)
    assert tr.mae == pytest.approx((102.5 - 101.0) / 102.5 * 100.0)     # min low баров 2…4 = 101
    # издержки вычитаются ровно один раз
    assert hold.summary([tr], 0.22)["mean"] == pytest.approx(tr.gross - 0.22)
    assert hold.summary([tr], 0.50)["mean"] == pytest.approx(tr.gross - 0.50)
    # бара выхода нет — не завершена
    coin.ok[4] = False
    assert hold.make_trade(coin, 2, 3) is None
    coin.ok[4] = True
    # выход позже последнего бара данных — не завершена; на последнем баре — завершена
    assert hold.make_trade(coin, 10, 3) is None
    last = hold.make_trade(coin, 9, 3)
    assert last is not None and last.stop == coin.time_of(11)
    assert last.gross == pytest.approx((coin.c[11] / coin.o[9] - 1.0) * 100.0)
    # пропуск внутри удержания: сделка есть, MAE пропускает отсутствующий бар
    coin.l[3] = 90.0
    coin.ok[3] = False
    inner = hold.make_trade(coin, 2, 3)
    assert inner is not None and inner.mae == pytest.approx((102.5 - 101.0) / 102.5 * 100.0)
    coin.ok[3] = True
    deep = hold.make_trade(coin, 2, 3)
    assert deep.mae == pytest.approx((102.5 - 90.0) / 102.5 * 100.0)


def test_exit_rules(hold, mr9) -> None:
    check_exit_rules(hold, mr9)


def test_trade_is_entered_at_the_open_of_the_bar_after_the_signal(hold, mr9, tmp_path) -> None:
    """Событие на баре t даёт вход по open[t+1], а не по close[t] и не по open[t]."""
    coins = load_coins(hold, tmp_path, {"BTC": gen_bars(11, 2600)})
    coin = coins["BTC"]
    hold.compute_indicators(coin)
    events = hold.find_events(coin, 2.0)
    assert events
    for t in events[:20]:
        tr = hold.make_trade(coin, t + 1, 24)
        if tr is None:
            continue
        assert tr.gross == pytest.approx((coin.c[t + 24] / coin.o[t + 1] - 1.0) * 100.0)
        assert coin.o[t + 1] != coin.c[t]


# --------------------------------------------------------------------------
# §7.3. Половины
# --------------------------------------------------------------------------


def check_halves(hold, mr9) -> None:
    """Сделка «вход в 1-й половине, выход во 2-й» не входит в выбор; вход ровно в 00:00 — 2-я."""
    coin = hand_coin(mr9, n=40)              # бар 12 открывается ровно в 2024-04-01 00:00 UTC
    assert coin.time_of(12) == mr9.HALF_EPOCH
    events = {"X": [2, 9, 10, 11, 20]}       # бары входа 3, 10, 11, 12, 21
    coins = {"X": coin}
    trades, unfinished, cut = hold.first_half_trades(coins, events, 4)
    assert [t.e for t in trades] == [3] and cut == 2 and unfinished == 0
    trades, unfinished, cut = hold.first_half_trades(coins, events, 2)
    assert [t.e for t in trades] == [3, 10] and cut == 1        # e=11: выход на баре 12 = 00:00
    second, unfinished = hold.second_half_trades(coins, events, 4)
    assert [t.e for t in second] == [12, 21] and unfinished == 0
    assert all(t.start >= mr9.HALF_EPOCH for t in second)
    # сделка 1-й половины, не дожившая до выхода (пропуск), считается как не завершённая
    coin.ok[7] = False
    trades, unfinished, cut = hold.first_half_trades(coins, {"X": [3]}, 4)
    assert trades == [] and unfinished == 1 and cut == 0


def test_a_trade_crossing_the_boundary_is_not_used_for_selection(hold, mr9) -> None:
    check_halves(hold, mr9)


def test_half_start_bar_is_the_first_bar_opening_at_or_after_the_boundary(hold, mr9) -> None:
    t0 = mr9.HALF_EPOCH - 5 * HOUR
    assert hold.half_start_bar(t0) == 5
    assert hold.half_start_bar(t0 + 1800) == 5            # бар открывается в 19:30 — он до границы
    assert hold.half_start_bar(mr9.HALF_EPOCH + 10 * HOUR) == 0       # весь ряд во 2-й половине


# --------------------------------------------------------------------------
# §7.4. Без утечки: данные 2-й половины не меняют выбор
# --------------------------------------------------------------------------


def run_collect(hold, series_path: Path, facts: Path | None = None) -> tuple[list[str], int]:
    lines: list[str] = []
    rc = hold.run(str(series_path), str(facts) if facts else None, "as-of", lines.append)
    return lines, rc


def test_selection_does_not_change_when_second_half_data_is_replaced(hold, tmp_path) -> None:
    series = run_series()
    rnd = random.Random(7)
    altered = copy.deepcopy(series)
    for bars in altered.values():
        for i in list(bars):
            if i >= HALF_BAR:
                f = rnd.uniform(0.4, 2.5)
                bars[i] = tuple(x * f for x in bars[i])
    write_csv(tmp_path / "a.csv", series)
    write_csv(tmp_path / "b.csv", altered)
    (tmp_path / "fa.txt").write_text("", encoding="utf-8")
    (tmp_path / "fb.txt").write_text("", encoding="utf-8")
    lines_a, rc_a = run_collect(hold, tmp_path / "a.csv", tmp_path / "fa.txt")
    lines_b, rc_b = run_collect(hold, tmp_path / "b.csv", tmp_path / "fb.txt")
    assert rc_a == rc_b == 0
    sel = re.search(r"^@@SEL\|(\S+)\|(\S+)$", (tmp_path / "fa.txt").read_text("utf-8"), re.M)
    assert sel and sel.group(1) != "-"                          # выбор состоялся, не заглушка
    assert (tmp_path / "fa.txt").read_text("utf-8").split("\n")[0] == \
        (tmp_path / "fb.txt").read_text("utf-8").split("\n")[0]
    # блок 01 (всё до заголовка блока 02) побайтно один и тот же
    head_a = lines_a[:next(i for i, ln in enumerate(lines_a) if ln.startswith("===== 02"))]
    head_b = lines_b[:next(i for i, ln in enumerate(lines_b) if ln.startswith("===== 02"))]
    assert head_a == head_b and any(ln.startswith("ВЫБРАНО: K=") for ln in head_a)
    # а числа второй половины действительно другие — подмена данных не пустая
    assert lines_a != lines_b


def test_select_variant_receives_only_first_half_trades(hold) -> None:
    import inspect
    sig = inspect.signature(hold.select_variant)
    assert list(sig.parameters) == ["trades_first_half"]


def test_the_choice_is_written_to_the_facts_file_before_block_02_is_printed(
    hold, tmp_path
) -> None:
    write_csv(tmp_path / "s.csv", run_series())
    facts = tmp_path / "facts.txt"
    facts.write_text("", encoding="utf-8")
    seen: list[tuple[str, bool]] = []

    def emit(line: str) -> None:
        if line.startswith("=====") or line.startswith("ВЫБРАНО") or line.startswith("ВЫБОР НЕ"):
            seen.append((line[:12], "@@SEL|" in facts.read_text("utf-8")))

    hold.run(str(tmp_path / "s.csv"), str(facts), "x", emit)
    by_head = dict(seen)
    assert by_head["===== 02 — П"] is True                  # к началу блока 02 выбор уже записан
    assert by_head["===== 01 — В"] is False               # во время печати блока 01 ещё нет


# --------------------------------------------------------------------------
# §7.5. Правило выбора
# --------------------------------------------------------------------------


def mk_trades(hold, n: int, gross: float) -> list:
    return [hold.Trade("X", i, 0, 0, gross, 0.0) for i in range(n)]


def check_selection_rules(hold) -> None:
    pick = hold.select_variant
    # лучшее среднее среди допущенных
    assert pick({(2.0, 12): mk_trades(hold, 150, 1.0), (2.5, 24): mk_trades(hold, 150, 2.0),
                 (3.0, 48): mk_trades(hold, 150, 1.5)}) == (2.5, 24)
    # ничья по среднему → меньший K
    assert pick({(3.0, 12): mk_trades(hold, 150, 1.0), (2.0, 48): mk_trades(hold, 150, 1.0),
                 (2.5, 12): mk_trades(hold, 150, 1.0)}) == (2.0, 48)
    # ничья по K → меньший H
    assert pick({(2.5, 48): mk_trades(hold, 150, 1.0), (2.5, 12): mk_trades(hold, 150, 1.0),
                 (2.5, 24): mk_trades(hold, 150, 1.0)}) == (2.5, 12)
    # разница меньше 1e-9 — это ничья: побеждает меньший K, хотя его среднее чуть ниже
    assert pick({(2.0, 12): mk_trades(hold, 150, 1.0), (3.0, 12): mk_trades(hold, 150, 1.0 + 5e-10)
                 }) == (2.0, 12)
    # разница больше 1e-9 — ничьей нет
    assert pick({(2.0, 12): mk_trades(hold, 150, 1.0), (3.0, 12): mk_trades(hold, 150, 1.0 + 2e-9)
                 }) == (3.0, 12)
    # N < 150 исключается, каким бы хорошим ни было среднее
    assert pick({(2.0, 12): mk_trades(hold, 149, 50.0), (2.5, 12): mk_trades(hold, 150, 1.0)}) \
        == (2.5, 12)
    assert pick({(2.0, 12): mk_trades(hold, 149, 50.0)}) is None
    # среднее ≤ 0 → выбор не состоялся (ровно 0 — тоже)
    assert pick({(2.0, 12): mk_trades(hold, 150, -1.0),
                 (2.5, 12): mk_trades(hold, 150, 0.1)}) is None
    assert pick({(2.5, 12): mk_trades(hold, 150, 0.22)}) is None
    assert pick({}) is None


def test_selection_rules(hold) -> None:
    check_selection_rules(hold)


def test_rank_variants_lists_the_whole_grid_in_order(hold) -> None:
    rows = hold.rank_variants({(2.5, 24): mk_trades(hold, 150, 1.0)})
    assert [(r[0], r[1]) for r in rows] == [(k, h) for k in (2.0, 2.5, 3.0) for h in (12, 24, 48)]
    assert [r[4] for r in rows] == [False, False, False, False, True, False, False, False, False]


# --------------------------------------------------------------------------
# §7.6. Тест сдвигом
# --------------------------------------------------------------------------

I0 = 100                  # бар, с которого начинается «вторая половина» синтетики теста сдвигом
LEN = 24 * 100            # сто суток
NB = I0 + LEN


def events_before_growth(hold):
    """B0 высок (+5) только в 10 разных сутках и именно там стоят события: сдвиг их теряет."""
    rise = [I0 + 24 * d + 5 for d in range(10)]
    b0 = {"A": [0.0 if i >= I0 else None for i in range(NB)]}
    for i in rise:
        b0["A"][i] = 5.0
    events = [("A", i) for i in rise]
    obs = sum(5.0 - 0.22 for _ in rise) / len(rise)
    return events, obs, b0


def ref_shift(events, b0, i0, nb, obs):
    """Эталон: сдвиг номера внутри круга длиной nb − i0."""
    length = nb - i0
    shifts = hits = 0
    for k in range(30, length // 24 - 30 + 1):
        vals = []
        for name, e in events:
            pos = (e - i0 + 24 * k) % length
            g = b0[name][i0 + pos]
            if g is not None:
                vals.append(g - 0.22)
        if vals:
            shifts += 1
            hits += sum(vals) / len(vals) >= obs
    return shifts, (hits / shifts if shifts else None)


def test_shift_p_is_small_when_events_stand_right_before_growth(hold) -> None:
    events, obs, b0 = events_before_growth(hold)
    res = hold.shift_test(events, obs, b0, I0, NB)
    assert res["shifts"] == LEN // 24 - 59 == 41 and res["p"] == 0.0
    assert hold.significant(res["p"])


def test_shift_p_is_not_small_for_random_placement(hold) -> None:
    rnd = random.Random(3)
    b0 = {"A": [None if i < I0 else rnd.gauss(0.0, 1.0) for i in range(NB)]}
    events = [("A", rnd.randrange(I0, NB)) for _ in range(20)]
    obs = sum(b0["A"][e] - 0.22 for _, e in events) / len(events)
    res = hold.shift_test(events, obs, b0, I0, NB)
    assert res["p"] is not None and res["p"] > 0.2 and not hold.significant(res["p"])
    assert (res["shifts"], res["p"]) == ref_shift(events, b0, I0, NB, obs)


def test_shift_circle_uses_only_second_half_bars(hold) -> None:
    """Огромные значения в баре до границы не влияют на p: круг не выходит за вторую половину."""
    rnd = random.Random(9)
    b0 = {"A": [None if i < I0 else rnd.gauss(0.0, 1.0) for i in range(NB)]}
    events = [("A", rnd.randrange(I0, NB)) for _ in range(25)]
    obs = sum(b0["A"][e] - 0.22 for _, e in events) / len(events)
    clean = hold.shift_test(events, obs, b0, I0, NB)
    polluted = {"A": [1000.0 if i < I0 else v for i, v in enumerate(b0["A"])]}
    assert hold.shift_test(events, obs, polluted, I0, NB) == clean
    # а события у конца круга переходят на его начало (bar I0), а не на бары до границы
    assert (clean["shifts"], clean["p"]) == ref_shift(events, b0, I0, NB, obs)


def test_shift_step_is_a_multiple_of_24_hours(hold) -> None:
    """Если B0 высок ровно в 5-й час каждых суток и события стоят в 5-й час, сдвиг на целые
    сутки всегда попадает в такой же час: среднее в сдвинутых точках равно наблюдаемому, p = 1."""
    b0 = {"A": [None if i < I0 else (5.0 if (i - I0) % 24 == 5 else 0.0) for i in range(NB)]}
    events = [("A", I0 + 24 * d + 5) for d in (3, 17, 40, 77, 95)]
    obs = 5.0 - 0.22
    res = hold.shift_test(events, obs, b0, I0, NB)
    assert res["p"] == 1.0 and res["shifts"] == 41


def test_shift_test_reports_no_p_when_the_second_half_is_too_short(hold) -> None:
    b0 = {"A": [0.0] * (I0 + 24 * 59)}
    res = hold.shift_test([("A", I0 + 5)], 1.0, b0, I0, I0 + 24 * 59)
    assert res["shifts"] == 0 and res["p"] is None and not hold.significant(res["p"])
    assert hold.shift_test([], None, b0, I0, I0 + 24 * 59)["p"] is None


# --------------------------------------------------------------------------
# B0 с тем же H* (§5.3) и критерии на реальной цепочке
# --------------------------------------------------------------------------


def check_b0_uses_h_star(hold, mr9) -> None:
    rnd = random.Random(21)
    coins = {}
    for name in ("X", "Y"):
        coin = hand_coin(mr9, n=60, t0=mr9.HALF_EPOCH - 5 * HOUR)
        coin.name = name
        for i in range(60):
            price = 100.0 + 10.0 * rnd.random()
            coin.o[i], coin.c[i] = price, price * (1 + rnd.gauss(0, 0.01))
            top, low = max(coin.o[i], coin.c[i]), min(coin.o[i], coin.c[i])
            coin.h[i], coin.l[i] = top * 1.01, low * 0.99
        coins[name] = coin
    coins["Y"].ok[30] = False
    h_star, i0 = 6, 5
    nets = []
    for coin in coins.values():
        for e in range(i0, 60):
            j = e + h_star - 1
            if coin.ok[e] and j <= 59 and coin.ok[j]:
                nets.append((coin.c[j] / coin.o[e] - 1.0) * 100.0 - 0.22)
    res = hold.confirm(coins, [], h_star)
    assert res["b0_n"] == len(nets) and res["b0_mean"] == pytest.approx(sum(nets) / len(nets))


def test_b0_uses_the_same_hold_as_the_selected_variant(hold, mr9) -> None:
    check_b0_uses_h_star(hold, mr9)


def test_confirmation_numbers_match_a_hand_recount(hold, tmp_path) -> None:
    """Сделки выбранного варианта пересчитаны отдельным кодом из сырых баров ряда."""
    series = run_series()
    coins = load_coins(hold, tmp_path, series)
    for coin in coins.values():
        hold.compute_indicators(coin)
    k, h = 2.5, 24
    ev = {n: hold.find_events(c, k) for n, c in coins.items()}
    trades, unfinished = hold.second_half_trades(coins, ev, h)
    ref = []
    for name in COINS:
        bars = series[name]
        for t in ev[name]:
            e = t + 1
            if e < HALF_BAR:
                continue
            if e + h - 1 in bars:
                ref.append((name, e, (bars[e + h - 1][3] / bars[e][0] - 1.0) * 100.0))
    assert sorted((t.coin, t.e, round(t.gross, 9)) for t in trades) == \
        sorted((n, e, round(g, 9)) for n, e, g in ref)
    res = hold.confirm(coins, trades, h)
    assert res["main"]["n"] == len(trades) and trades
    assert res["main"]["mean"] == pytest.approx(sum(g - 0.22 for _, _, g in ref) / len(ref))
    assert res["stress"]["mean"] == pytest.approx(sum(g - 0.50 for _, _, g in ref) / len(ref))
    assert sum(res["coins"][c].get("n", 0) for c in COINS) == len(trades)
    assert sum(n for _, n, _ in res["quarters"]) == len(trades)
    assert res["shift"]["n"] == len(trades)


def test_quarters_and_their_share(hold) -> None:
    def tr(date: datetime, gross: float):
        ep = int(date.timestamp())
        return hold.Trade("X", 0, ep, ep + HOUR, gross, 0.0)

    trades = ([tr(datetime(2024, 4, 5, tzinfo=UTC), 1.0)] * 10
              + [tr(datetime(2024, 8, 5, tzinfo=UTC), -1.0)] * 10
              + [tr(datetime(2024, 12, 5, tzinfo=UTC), 1.0)] * 9            # 9 < 10 — не считается
              + [tr(datetime(2025, 1, 5, tzinfo=UTC), 1.0)] * 12)
    rows = hold.quarter_table(trades)
    assert [(q, n) for q, n, _ in rows] == [("2024Q2", 10), ("2024Q3", 10), ("2024Q4", 9),
                                            ("2025Q1", 12)]
    assert hold.quarters_positive(rows) == (3, 2)
    assert hold.quarter_of(int(datetime(2024, 3, 31, 23, tzinfo=UTC).timestamp())) == "2024Q1"


# --------------------------------------------------------------------------
# Вердикт (§6)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def verdict():
    return load_module(VERDICT, "verdict_real")


def good_facts() -> dict[str, str]:
    facts = {"mean": "1.0", "median": "0.5", "p": "0.01", "b0_mean": "-0.2", "q_total": "8",
             "q_pos": "6", "mean50": "0.7", "median50": "0.3", "orig_n": "80",
             "orig_mean": "1.5", "orig_median": "1.2", "orig_mean50": "1.2", "_sel_seen": "1"}
    for c in COINS:
        facts[f"coin_{c}_mean"] = "0.9"
    return facts


def check_verdict_criteria(verdict) -> None:
    sel = ("2.5", "24")
    out = verdict.render(sel, good_facts())
    assert out[0].startswith("ВЫБОР: K=2.5, H=24")
    assert sum(1 for ln in out if "✅" in ln) == 5 and "ИТОГ: кандидат в теневой режим." in out

    def failed(**changes) -> list[int]:
        facts = {**good_facts(), **changes}
        crit = verdict.evaluate(facts)
        return [n for n, (ok, _) in crit.items() if not ok]

    assert failed() == []
    assert failed(median="-0.01") == [1] and failed(mean="0", median="0.5", b0_mean="-1") == [1]
    assert failed(coin_BTC_mean="-0.1") == []                          # 4 из 5 — достаточно
    assert failed(coin_BTC_mean="-0.1", coin_ETH_mean="0") == [2]      # 0 — не плюс; 3 из 5
    assert failed(p="0.05") == [] and failed(p="0.0501") == [3] and failed(p="") == [3]
    assert failed(p="0.03") == [] and failed(p="0.06") == [3]
    assert failed(b0_mean="1.0") == [4] and failed(b0_mean="0.99") == []       # строго выше
    assert failed(q_total="10", q_pos="6") == []                       # ровно 60 %
    assert failed(q_total="10", q_pos="5") == [5] and failed(q_total="0", q_pos="0") == [5]


def test_verdict_criteria(verdict) -> None:
    check_verdict_criteria(verdict)


def test_verdict_prints_a_failing_rule_as_not_a_candidate_with_the_numbers(verdict) -> None:
    facts = {**good_facts(), "p": "0.2", "median": "-0.3"}
    out = verdict.render(("2.0", "12"), facts)
    text = "\n".join(out)
    assert "1) ❌" in text and "3) ❌" in text and "p = 0.2000" in text
    assert "ИТОГ: не кандидат. Не прошло: 1)" in text
    assert "исходная гипотеза K=2.5, H=24" in text and "C = 0,50" in text


def test_verdict_when_selection_failed_or_is_missing(verdict, tmp_path) -> None:
    out = verdict.render(None, {"_sel_seen": "1"})
    assert out[0].startswith("ВЫБОР НЕ СОСТОЯЛСЯ") and "ИТОГ: не кандидат." in out[1]
    assert "нет данных" in verdict.render(None, {"_sel_seen": "0"})[0]
    path = tmp_path / "f.txt"
    path.write_text("@@SEL|-|-\n@@F|mean|1\n", encoding="utf-8")
    res = subprocess.run([sys.executable, "-I", str(VERDICT), str(path)], capture_output=True,
                         text=True, check=False)
    assert res.returncode == 0 and "ВЫБОР НЕ СОСТОЯЛСЯ" in res.stdout
    path.write_text("@@F|mean|1\n", encoding="utf-8")
    res = subprocess.run([sys.executable, "-I", str(VERDICT), str(path)], capture_output=True,
                         text=True, check=False)
    assert res.returncode == 1 and "нет данных" in res.stdout


def test_verdict_reads_the_last_sel_line_and_ignores_other_facts(verdict) -> None:
    sel, facts = verdict.read_facts(["@@T|BTC|1", "@@SEL|2.0|12", "@@F|mean|1.5", "шум",
                                     "@@SEL|3.0|48", "@@F|p|0.01"])
    assert sel == ("3.0", "48") and facts["mean"] == "1.5" and facts["p"] == "0.01"


# --------------------------------------------------------------------------
# Константы и состав файлов
# --------------------------------------------------------------------------


def test_constants_are_exactly_those_of_the_spec_and_sit_in_one_block(hold, mr9, verdict) -> None:
    assert hold.K_GRID == (2.0, 2.5, 3.0) and hold.H_GRID == (12, 24, 48)
    assert hold.MIN_TRADES_SELECT == 150 and hold.TIE_EPS == 1e-9
    assert (hold.COST_MAIN, hold.COST_STRESS) == (0.22, 0.50)
    assert hold.ALPHA == 0.05 and hold.MIN_COINS_POSITIVE == 4
    assert hold.MIN_QUARTER_TRADES == 10 and hold.QUARTER_SHARE_MIN == 0.60
    assert (hold.SHIFT_MIN_DAYS, hold.SHIFT_STEP_HOURS) == (30, 24)
    assert (hold.ORIG_K, hold.ORIG_H) == (2.5, 24)
    assert hold.HALF_EPOCH == int(HALF.timestamp()) == mr9.HALF_EPOCH
    # пороги вердикта = пороги расчёта
    assert (verdict.ALPHA, verdict.MIN_COINS, verdict.QUARTER_SHARE_MIN, verdict.COINS) == \
        (hold.ALPHA, hold.MIN_COINS_POSITIVE, hold.QUARTER_SHARE_MIN, hold.COINS)
    # все числа методики объявлены одним блоком до первой функции
    tree = ast.parse(HOLD.read_text(encoding="utf-8"))
    first_def = min(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef | ast.ClassDef))
    names = {t.id for n in tree.body if isinstance(n, ast.Assign) and n.lineno < first_def
             for t in n.targets if isinstance(t, ast.Name)}
    assert {"K_GRID", "H_GRID", "MIN_TRADES_SELECT", "COST_MAIN", "COST_STRESS", "ALPHA",
            "MIN_COINS_POSITIVE", "MIN_QUARTER_TRADES", "QUARTER_SHARE_MIN", "SHIFT_MIN_DAYS",
            "SHIFT_STEP_HOURS", "ORIG_K", "ORIG_H"} <= names
    text = HOLD.read_text(encoding="utf-8")
    for old in ("0.0125", "R6_K"):
        assert old not in text


def test_scripts_do_not_change_anything_on_the_server() -> None:
    for path in (RUN_9_12, VERIFY["9_12"]):
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in path.read_text("utf-8").splitlines())
        for forbidden in ("docker compose restart", "docker compose up", "docker compose build",
                          "git pull", "git checkout", "pip install", "INSERT INTO", "DELETE FROM",
                          "DROP ", "ALTER ", "TRUNCATE TABLE"):
            assert forbidden not in code, (path.name, forbidden)
    assert 'source "$LIB_DIR/lib_ro.sh"' in RUN_9_12.read_text("utf-8")
    assert "redact" in RUN_9_12.read_text("utf-8")
    assert "lib_verify.sh" in VERIFY["9_12"].read_text("utf-8")
    for path in (RUN_9_12, *VERIFY.values()):
        assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0
    for path in (HOLD, VERDICT):
        assert "LOGIC_VERSION" not in path.read_text("utf-8")


def imported_modules(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_only_the_standard_library_is_imported() -> None:
    std = set(sys.stdlib_module_names)
    assert imported_modules(HOLD) <= std | {"mr_9_11"}
    assert imported_modules(VERDICT) <= std


def test_hold_and_verdict_run_in_isolated_mode_from_any_directory(tmp_path) -> None:
    write_csv(tmp_path / "s.csv", run_series(N_RUN))
    facts = tmp_path / "f.txt"
    facts.write_text("", encoding="utf-8")
    res = subprocess.run([sys.executable, "-I", str(HOLD), "--series", str(tmp_path / "s.csv"),
                          "--facts", str(facts), "--append"], capture_output=True, text=True,
                         check=False, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    res2 = subprocess.run([sys.executable, "-I", str(VERDICT), str(facts)], capture_output=True,
                          text=True, check=False, cwd=tmp_path)
    assert res2.returncode == 0 and "ИТОГ:" in res2.stdout


def test_the_allowed_files_only_and_nothing_else_is_touched() -> None:
    res = subprocess.run(["git", "diff", "--name-only", BASE_COMMIT], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.skip("нет основы 5ab283a в этом клоне")
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT,
                               capture_output=True, text=True, check=False).stdout.split()
    allowed = {
        "analysis/run_9_12.sh", "analysis/hold_9_12.py", "analysis/verdict_9_12.py",
        "deploy/verify_9_12.sh", "tests/test_stage_9_12.py", "docs/STAGE_9_12_REPORT.md",
        "deploy/verify_9_11.sh",           # §8: пункт 5
        "deploy/verify_9_10.sh",           # по поручению владельца: то же правило
        "tests/test_stage_9_11.py",        # из охраняемых убран verify_9_10.sh (см. выше)
    }
    touched = set(res.stdout.split()) | set(untracked)
    assert touched <= allowed, sorted(touched - allowed)
    for forbidden in ("src/", "backtest/", "docker-compose.yml", "db/migrations/",
                      "analysis/mr_9_11.py", "analysis/sql/"):
        assert not any(t.startswith(forbidden) for t in touched), forbidden


# --------------------------------------------------------------------------
# §7.9. Время и память на 5 монетах × 42 000 баров
# --------------------------------------------------------------------------


def test_full_size_synthetic_run_fits_the_time_and_memory_budget(tmp_path) -> None:
    n = 42000
    t0 = datetime(2022, 2, 1, tzinfo=UTC)
    # CSV пишется монета за монетой: ряд целиком в памяти процесса pytest не держим (иначе пик
    # памяти всего набора тестов вырастает и ломает проверки лимита контейнера этапа 9.1.3)
    with open(tmp_path / "big.csv", "w", encoding="utf-8") as fh:
        fh.write("token,open_time_utc,open,high,low,close\n")
        for k, tok in enumerate(COINS):
            part = tmp_path / f"{tok}.csv"
            write_csv(part, {tok: gen_bars(100 + k, n, start=100.0 + k)}, t0)
            fh.write("".join(part.read_text(encoding="utf-8").splitlines(keepends=True)[1:]))
            part.unlink()
    facts = tmp_path / "f.txt"
    facts.write_text("", encoding="utf-8")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.monotonic()
    res = subprocess.run([sys.executable, "-I", str(HOLD), "--series", str(tmp_path / "big.csv"),
                          "--facts", str(facts), "--append"], capture_output=True, text=True,
                         check=False)
    elapsed = time.monotonic() - started
    peak_mb = max(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, before) / 1024
    assert res.returncode == 0, res.stderr
    print(f"\n[9.12] 5 × {n} баров: {elapsed:.1f} с, пик памяти {peak_mb:.0f} МБ, "
          f"строк вывода {len(res.stdout.splitlines())}")
    assert elapsed < 15 * 60 and peak_mb < 500
    assert len(res.stdout.splitlines()) < 100
    for head in ("01 —", "02 —", "03 —", "04 —"):
        assert f"===== {head}" in res.stdout
    assert "@@SEL|" in facts.read_text("utf-8")


# --------------------------------------------------------------------------
# Выбор не состоялся
# --------------------------------------------------------------------------


def test_no_selection_when_the_first_half_has_too_few_trades(hold, tmp_path) -> None:
    short = {tok: gen_bars(70 + k, 4000, start=100.0 + k) for k, tok in enumerate(COINS)}
    write_csv(tmp_path / "s.csv", short, datetime(2024, 1, 1, tzinfo=UTC))
    facts = tmp_path / "f.txt"
    facts.write_text("", encoding="utf-8")
    lines, rc = run_collect(hold, tmp_path / "s.csv", facts)
    text = "\n".join(lines)
    assert rc == 0 and "ВЫБОР НЕ СОСТОЯЛСЯ: никто не допущен (N < 150)." in text
    assert "ВЫБРАНО" not in text and "Выбор не состоялся: подтверждение не проводится" in text
    assert "Не проводится: нет выбранного варианта." in text
    assert "<< выбран" not in text
    assert "Исходная гипотеза K=2.5, H=24" in text                 # справочно считается
    body = facts.read_text("utf-8")
    assert body.splitlines()[0] == "@@SEL|-|-" and "@@F|orig_n|" in body
    assert "@@F|p|" not in body and "@@F|mean|" not in body


def losing_bars(seed: int, n: int, start: float) -> dict:
    """Как gen_bars, но после обвала падение продолжается: вход «на откате» убыточен."""
    rnd = random.Random(seed)
    bars, price = {}, start
    for i in range(n):
        drift = rnd.gauss(0, 0.004)
        if i % 157 == 90:
            drift = -0.045
        elif i % 157 in (91, 92):
            drift = -0.012
        o = price * (1 + rnd.gauss(0, 0.0015))
        c = o * (1 + drift)
        bars[i] = (o, max(o, c) * 1.002, min(o, c) * 0.998, c)
        price = c if i % 157 != 156 else start          # к концу цикла цена возвращается
    return bars


def test_no_selection_when_the_best_variant_loses_money(hold, tmp_path) -> None:
    series = {tok: losing_bars(200 + k, N_RUN, 100.0 + k) for k, tok in enumerate(COINS)}
    write_csv(tmp_path / "s.csv", series)
    facts = tmp_path / "f.txt"
    facts.write_text("", encoding="utf-8")
    lines, rc = run_collect(hold, tmp_path / "s.csv", facts)
    text = "\n".join(lines)
    assert rc == 0 and "ВЫБОР НЕ СОСТОЯЛСЯ: у лучшего варианта среднее ≤ 0." in text
    assert facts.read_text("utf-8").splitlines()[0] == "@@SEL|-|-"
    assert "допуск" in text and "да" in text            # варианты допущены, но не прибыльны


def test_empty_series_is_an_error(hold, tmp_path) -> None:
    path = tmp_path / "e.csv"
    path.write_text("token,open_time_utc,open,high,low,close\n", encoding="utf-8")
    lines, rc = run_collect(hold, path, None)
    assert rc == 1 and "НЕТ ДАННЫХ" in lines[0]


# --------------------------------------------------------------------------
# §8. Счётчики до/после в verify_9_10, verify_9_11, verify_9_12
# --------------------------------------------------------------------------


def verify_env(tmp_path: Path) -> dict:
    app = tmp_path / "app"
    bindir = tmp_path / "bin"
    app.mkdir(exist_ok=True)
    bindir.mkdir(exist_ok=True)
    (app / ".env").write_text("POSTGRES_DB=agenttrade\n", encoding="utf-8")
    docker = bindir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "APP_DIR": str(app),
            "OUT_DIR": str(tmp_path / "out"), "HOME": str(tmp_path)}


def put_run(tmp_path: Path, stage: str, before: str, after: str, start: str | None,
            end: str | None) -> None:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    (out / f"facts_{stage}_{day}.txt").write_text(
        f"@@F|rows_before|{before}\n@@F|rows_after|{after}\n", encoding="utf-8")
    lines = []
    if start:
        lines.append(f"Запуск (UTC): {start}; момент замера: 2026-10-19 19:38:53+00")
    if end:
        lines.append(f"ИТОГ ПРОГОНА: блоков 6, с ошибками 0; окончание (UTC): {end}")
    (out / f"report_{stage}_{day}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def point5(stage: str, tmp_path: Path) -> tuple[str, int, str]:
    """Строка пункта «число строк» (ее результат и номер 5 или 6) и код возврата скрипта."""
    res = subprocess.run(["bash", str(VERIFY[stage])], env=verify_env(tmp_path),
                         capture_output=True, text=True, check=False)
    n = "6" if stage == "9_12" else "5"
    block = res.stdout.split(f"\n{n}. Число строк")[1].split("\n\n")[0]
    return block, res.returncode, res.stdout


STAGES = ("9_10", "9_11", "9_12")


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
def test_counters_the_case_from_the_server_run_is_green(stage, tmp_path) -> None:
    """Прогон 19.10.2026: signals +5 за ~1 минуту — это не ошибка."""
    put_run(tmp_path, stage, "1100/434281/297", "1100/434286/297",
            "2026-10-19 19:38:53", "2026-10-19 19:39:20")
    block, rc, _ = point5(stage, tmp_path)
    assert "✅" in block and "❌" not in block and "⚠️" not in block
    assert "positions 1100→1100, signals 434281→434286, demo_orders 297→297" in block
    assert "+5" in block and "допустимых 10 (5 × 1 мин + 5)" in block


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("seconds", "minutes"), [(0, 0), (1, 1), (60, 1), (61, 2), (125, 3)])
def test_counters_the_limit_is_5_per_started_minute_plus_5(
    stage, seconds, minutes, tmp_path
) -> None:
    limit = 5 * minutes + 5
    end = datetime(2026, 10, 19, 19, 38, 53, tzinfo=UTC) + timedelta(seconds=seconds)
    for growth, expect_ok in ((limit, True), (limit + 1, False)):
        put_run(tmp_path, stage, "10/1000/5", f"10/{1000 + growth}/5", "2026-10-19 19:38:53",
                end.strftime("%Y-%m-%d %H:%M:%S"))
        block, rc, stdout = point5(stage, tmp_path)
        if expect_ok:
            assert "✅" in block and "⚠️" not in block, block
        else:
            assert "⚠️" in block and "❌" not in block, block
            assert f"signals выросли на {growth} при допустимых {limit}" in block
            assert "signals 1000→" in block and "positions 10→10" in block
            assert "ИТОГ: ВСЕ ✅" not in stdout


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("after", "what"),
                         [("11/1000/5", "positions"), ("10/1000/6", "demo_orders")])
def test_counters_positions_and_demo_orders_must_match(stage, after, what, tmp_path) -> None:
    put_run(tmp_path, stage, "10/1000/5", after, "2026-10-19 19:38:53", "2026-10-19 19:39:00")
    block, rc, stdout = point5(stage, tmp_path)
    assert "❌" in block and "число строк разошлось" in block and what in block
    assert "→" in block


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
def test_counters_equal_values_and_a_decrease(stage, tmp_path) -> None:
    put_run(tmp_path, stage, "10/1000/5", "10/1000/5", "2026-10-19 19:38:53",
            "2026-10-19 19:39:00")
    block, rc, _ = point5(stage, tmp_path)
    assert "✅" in block and "совпало" in block
    put_run(tmp_path, stage, "10/1000/5", "10/990/5", "2026-10-19 19:38:53",
            "2026-10-19 19:39:00")
    block, rc, _ = point5(stage, tmp_path)
    assert "⚠️" in block and "signals уменьшилось" in block and "1000→990" in block


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
def test_counters_growth_without_run_times_is_not_checked(stage, tmp_path) -> None:
    put_run(tmp_path, stage, "10/1000/5", "10/1003/5", None, None)
    block, rc, stdout = point5(stage, tmp_path)
    assert "⚪" in block and "не найдено" in block and "ИТОГ: ВСЕ ✅" not in stdout
    put_run(tmp_path, stage, "10/1000/5", "10/1000/5", None, None)
    block, rc, _ = point5(stage, tmp_path)
    assert "✅" in block                                      # роста нет — длительность не нужна


@needs_bash
@pytest.mark.parametrize("stage", STAGES)
def test_a_warning_is_counted_separately_from_errors_in_the_summary(stage, tmp_path) -> None:
    put_run(tmp_path, stage, "10/1000/5", "10/1500/5", "2026-10-19 19:38:53",
            "2026-10-19 19:39:00")
    _, _, stdout = point5(stage, tmp_path)
    assert "предупреждений: 1" in stdout and "ИТОГ: ВСЕ ✅" not in stdout


# --------------------------------------------------------------------------
# §7.7. Мутации: каждая обязана ловиться
# --------------------------------------------------------------------------


def check_alpha(hold) -> None:
    assert hold.significant(0.03) and hold.significant(0.05) and hold.significant(0.0)
    assert not hold.significant(0.06) and not hold.significant(None)


MUTATIONS = {
    "а": ("вход по close[t] вместо open[t+1]", HOLD, "    entry = coin.o[e]\n",
          "    entry = coin.c[e - 1]\n"),
    "б": ("выход по close[e+H] вместо close[e+H−1]", HOLD, "    return e + h - 1\n",
          "    return e + h\n"),
    "в": ("выбор по всей истории", HOLD,
          "    return start < HALF_EPOCH and stop < HALF_EPOCH\n", "    return True\n"),
    "г": ("сделки «вход в 1-й, выход во 2-й» попадают в выбор", HOLD,
          "    return start < HALF_EPOCH and stop < HALF_EPOCH\n",
          "    return start < HALF_EPOCH\n"),
    "д1": ("порог p = 0,0125 вместо 0,05 (расчёт)", HOLD, "ALPHA = 0.05  #",
           "ALPHA = 0.0125  #"),
    "д2": ("порог p = 0,10 вместо 0,05 (расчёт)", HOLD, "ALPHA = 0.05  #", "ALPHA = 0.10  #"),
    "д3": ("порог p = 0,0125 вместо 0,05 (вердикт)", VERDICT, "ALPHA = 0.05  #",
           "ALPHA = 0.0125  #"),
    "д4": ("порог p = 0,10 вместо 0,05 (вердикт)", VERDICT, "ALPHA = 0.05  #", "ALPHA = 0.10  #"),
    "е": ("B0 с H, отличным от H*", HOLD, "b0 = b0_gross(coins, h_star, i0)",
          "b0 = b0_gross(coins, h_star + 12, i0)"),
}


def mutant(key: str):
    _title, path, old, new = MUTATIONS[key]
    return load_module(path, f"mut_{key}", replace=(old, new))


def test_mutation_a_entry_at_close_t_is_caught(mr9) -> None:
    with pytest.raises(AssertionError):
        check_exit_rules(mutant("а"), mr9)


def test_mutation_b_exit_at_close_e_plus_h_is_caught(mr9) -> None:
    with pytest.raises(AssertionError):
        check_exit_rules(mutant("б"), mr9)


@pytest.mark.parametrize("key", ["в", "г"])
def test_mutations_c_d_selection_scope_is_caught(key, mr9) -> None:
    with pytest.raises(AssertionError):
        check_halves(mutant(key), mr9)


def chosen_variant(h, mr9):
    """Выбор на данных, где его могут перетянуть чужие сделки: вариант (2.0, 12) честный и
    прибыльный (+0,78 % net), (2.5, 12) торгует во второй половине, (3.0, 48) — через границу.
    У двух последних доход огромный, поэтому любая протечка в выбор их и выберет."""
    coins = {}
    for j in range(4):
        coin = hand_coin(mr9, n=900, t0=mr9.HALF_EPOCH - 600 * HOUR)
        coin.name = f"X{j}"
        for i in range(900):
            coin.o[i] = 100.0 if i < 590 else 100.0 + 10.0 * (i - 589)
            coin.c[i] = 101.0 if i < 590 else coin.o[i] + 1.0
        coins[coin.name] = coin
    names = list(coins)
    events = {
        (2.0, 12): {"X0": list(range(10, 160))},                       # вход и выход до границы
        (2.5, 12): {"X0": list(range(600, 750))},                      # вход во второй половине
        (3.0, 48): {n: list(range(552, 599)) for n in names},          # вход до, выход после
    }
    first = {kh: h.first_half_trades(coins, {n: ev.get(n, []) for n in names}, kh[1])[0]
             for kh, ev in events.items()}
    return h.select_variant(first), {kh: len(v) for kh, v in first.items()}


def test_selection_is_made_from_first_half_trades_only(hold, mr9) -> None:
    assert chosen_variant(hold, mr9) == ((2.0, 12), {(2.0, 12): 150, (2.5, 12): 0, (3.0, 48): 0})


@pytest.mark.parametrize("key", ["в", "г"])
def test_mutations_c_d_change_the_selection_on_data_where_it_matters(key, mr9) -> None:
    got, counts = chosen_variant(mutant(key), mr9)
    assert got != (2.0, 12) and (counts[(2.5, 12)] > 0 or counts[(3.0, 48)] > 0)


@pytest.mark.parametrize("key", ["д1", "д2"])
def test_mutation_e_alpha_in_the_calculation_is_caught(key) -> None:
    with pytest.raises(AssertionError):
        check_alpha(mutant(key))


@pytest.mark.parametrize("key", ["д3", "д4"])
def test_mutation_e_alpha_in_the_verdict_is_caught(key) -> None:
    with pytest.raises(AssertionError):
        check_verdict_criteria(mutant(key))


def test_mutation_f_b0_with_another_hold_is_caught(mr9) -> None:
    with pytest.raises(AssertionError):
        check_b0_uses_h_star(mutant("е"), mr9)


def test_the_checks_themselves_pass_on_the_real_modules(hold, verdict, mr9) -> None:
    check_exit_rules(hold, mr9)
    check_halves(hold, mr9)
    check_selection_rules(hold)
    check_alpha(hold)
    check_b0_uses_h_star(hold, mr9)
    check_verdict_criteria(verdict)


def test_every_mutation_target_exists_exactly_once() -> None:
    assert set(MUTATIONS) == {"а", "б", "в", "г", "д1", "д2", "д3", "д4", "е"}
    for title, path, old, new in MUTATIONS.values():
        assert path.read_text(encoding="utf-8").count(old) == 1, title
        assert old != new


# --------------------------------------------------------------------------
# Скрипты целиком: run_9_12.sh и verify_9_12.sh на настоящей PostgreSQL
# --------------------------------------------------------------------------

AS_OF_E2E = (T0 + timedelta(hours=N_RUN + 1)).strftime("%Y-%m-%d %H:%M:%S+00")
HEADINGS = ["00 — ДАННЫЕ И ОХВАТ", "01 — ВЫБОР НА ПЕРВОЙ ПОЛОВИНЕ", "02 — ПОДТВЕРЖДЕНИЕ",
            "03 — ТЕСТ СДВИГОМ", "04 — СЕТКА ЦЕЛИКОМ", "05 — ВЕРДИКТ"]


def _d(x: float) -> Decimal:
    return Decimal(f"{x:.8f}")


async def put_hist(pool, token: str, bars: dict) -> None:
    rows = []
    for i, (o, h, lo, c) in bars.items():
        ts = T0 + timedelta(hours=i)
        rows.append((f"{token}-USDT", "1H", ts, ts + timedelta(hours=1), _d(o), _d(h), _d(lo),
                     _d(c), Decimal(1)))
    await pool.executemany(
        "INSERT INTO backtest.candles (inst_id, bar, open_time, close_time, open, high, low, "
        "close, volume) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);", rows)


async def seed_e2e(pool) -> None:
    for tok, bars in run_series().items():
        await put_hist(pool, tok, bars)


def out_file(tmp_path: Path, kind: str, ext: str) -> Path:
    return tmp_path / "out" / f"{kind}_9_12_{datetime.now(UTC):%Y%m%d}.{ext}"


def report_12(tmp_path: Path) -> str:
    return out_file(tmp_path, "report", "txt").read_text("utf-8")


def facts_12(tmp_path: Path) -> str:
    return out_file(tmp_path, "facts", "txt").read_text("utf-8")


@needs_bash
@needs_pg
async def test_run_9_12_makes_a_complete_read_only_report(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    before = fingerprint(db_name)
    started = time.monotonic()
    res = run_script(RUN_9_12, stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E})
    elapsed = time.monotonic() - started
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_12(tmp_path)
    pos = [report.index(f"===== {h}") for h in HEADINGS]
    assert pos == sorted(pos)
    assert "с ошибками 0" in report and "ОШИБКА" not in report and "блоков 6" in report
    assert len(report.splitlines()) <= 200, len(report.splitlines())
    assert "@@" not in report
    assert fingerprint(db_name) == before                       # замер ничего не записал
    assert elapsed < 15 * 60
    sel_line = report.index("ВЫБРАНО: K=")
    assert sel_line < report.index("===== 02")                  # выбор напечатан раньше блока 02
    facts = facts_12(tmp_path)
    for tag in ("@@SEL|", "@@F|rows_before|", "@@F|p|", "@@F|b0_mean|", "@@F|orig_n|"):
        assert tag in facts, tag
    assert re.search(r"@@F\|rows_before\|(\S+)", facts).group(1) == \
        re.search(r"@@F\|rows_after\|(\S+)", facts).group(1)
    csv_lines = out_file(tmp_path, "series", "csv").read_text("utf-8").splitlines()
    assert csv_lines[0] == "token,open_time_utc,open,high,low,close"
    per_coin = {c: sum(1 for ln in csv_lines[1:] if ln.startswith(c + ",")) for c in COINS}
    assert set(per_coin.values()) == {N_RUN - 1}                # в каждой монете один пропуск часа
    assert "Ряд выгружен в CSV (строк по монетам)" in report
    assert "ИТОГ:" in report.split("===== 05")[1]
    # то, что напечатал скрипт, совпадает с расчётом по тому же CSV
    lines, rc = run_collect(load_module(HOLD, "hold_e2e"), out_file(tmp_path, "series", "csv"))
    assert rc == 0
    assert [ln for ln in lines if ln.startswith("ВЫБРАНО")] == \
        [ln for ln in report.splitlines() if ln.startswith("ВЫБРАНО")]


@needs_bash
@needs_pg
async def test_run_9_12_never_prints_the_password(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    secret = "Zq9$w\\x7!pass&word"
    stub = make_stub(tmp_path, db_name, ro_password=secret)
    res = run_script(RUN_9_12, stub, tmp_path,
                     env_extra={"STUB_LEAK": secret, "AS_OF": AS_OF_E2E})
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_12(tmp_path)
    assert secret not in res.stdout and secret not in res.stderr and secret not in report
    assert secret not in facts_12(tmp_path)
    assert "password=***" in report


@needs_bash
@needs_pg
async def test_run_9_12_failure_modes_are_reported(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    res = run_script(RUN_9_12, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode != 0 and "не удалось подключиться к БД ролью agenttrade_ro" in res.stdout
    res = run_script(RUN_9_12, stub, tmp_path,
                     env_extra={"AS_OF": AS_OF_E2E, "PYTHON": "python3-no-such"})
    assert res.returncode != 0 and "с ошибками 2" in res.stdout
    assert "блоки 01–05 пропущены" in res.stdout
    sandbox = tmp_path / "analysis"
    import shutil
    shutil.copytree(ROOT / "analysis", sandbox)
    broken = sandbox / "sql" / "9_11" / "00_scope.sql"
    text = (ROOT / "analysis" / "sql" / "9_11" / "00_scope.sql").read_text("utf-8")
    broken.write_text(text.replace("FROM tc c", "FROM no_such_table c", 1), "utf-8")
    res = run_script(sandbox / "run_9_12.sh", stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E})
    assert res.returncode != 0 and "ОШИБКА: блок 00" in res.stdout
    assert "расчёт 01–04 пропущен" in res.stdout


@needs_bash
@needs_pg
async def test_run_9_12_tells_to_apply_migration_032_when_the_role_cannot_read(
    db_name, tmp_path
) -> None:
    from tests.demo_support import build_database, drop_database, psql_raw
    db = build_database("nogrant12")
    try:
        psql_raw(db, stdin=(ROOT / "db" / "migrations" / "032_ro_backtest_read_rollback.sql")
                 .read_text(encoding="utf-8"))
        res = run_script(RUN_9_12, make_stub(tmp_path, db), tmp_path)
        assert res.returncode != 0 and "нет права читать backtest.candles" in res.stdout
        assert "032_ro_backtest_read.sql" in res.stdout and "===== 01" not in res.stdout
    finally:
        drop_database(db)


def run_verify(stub: dict, tmp_path: Path, stage: str = "9_12"):
    return subprocess.run(["bash", str(VERIFY[stage])], env=script_env(stub, tmp_path),
                          capture_output=True, text=True, check=False)


@needs_bash
@needs_pg
async def test_verify_9_12_is_green_after_a_clean_run_and_catches_each_defect(
    pool, db_name, tmp_path
) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name, ro_password="verify-pw")
    assert run_script(RUN_9_12, stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E}).returncode == 0
    res = run_verify(stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(re.findall(r"^  ✅", res.stdout, re.M)) == 6 and "ИТОГ: ВСЕ ✅" in res.stdout
    assert "❌" not in res.stdout and "⚪" not in res.stdout and "⚠️" not in res.stdout
    report = out_file(tmp_path, "report", "txt")
    original = report.read_text("utf-8")
    # пароль в отчёте
    report.write_text(original + "\nпароль verify-pw попал в отчёт\n", encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "найден пароль роли" in res.stdout
    # блок пропал и есть строка ОШИБКА
    report.write_text(original.replace("===== 04 —", "===== 4x —")
                      + "\nОШИБКА: блок упал\n", encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "нет блока 04" in res.stdout and "строки ОШИБКА" in res.stdout
    report.write_text(original + "лишняя строка\n" * 200, encoding="utf-8")
    assert "допустимо 200" in run_verify(stub, tmp_path).stdout
    # строка выбора после блока 02
    moved = [ln for ln in original.splitlines() if not ln.startswith("ВЫБРАНО: K=")]
    chosen = next(ln for ln in original.splitlines() if ln.startswith("ВЫБРАНО: K="))
    idx = next(i for i, ln in enumerate(moved) if ln.startswith("===== 02"))
    report.write_text("\n".join([*moved[:idx + 1], chosen, *moved[idx + 1:]]) + "\n",
                      encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "напечатана не раньше блока 02" in res.stdout
    # в отчёте нет строки выбора совсем
    report.write_text("\n".join(moved) + "\n", encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "нет строки «ВЫБРАНО" in res.stdout
    report.write_text(original, encoding="utf-8")
    # @@SEL в файле фактов не совпадает с отчётом / отсутствует
    facts = out_file(tmp_path, "facts", "txt")
    facts_text = facts.read_text("utf-8")
    facts.write_text(re.sub(r"@@SEL\|[0-9.]+\|\d+", "@@SEL|9.9|999", facts_text), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "не совпадает с @@SEL" in res.stdout
    facts.write_text(re.sub(r"@@SEL\|[^\n]*\n", "", facts_text), encoding="utf-8")
    assert "в файле фактов нет строки @@SEL" in run_verify(stub, tmp_path).stdout
    facts.write_text(facts_text, encoding="utf-8")
    # positions / demo_orders разошлись — ❌ с числами по каждой таблице
    facts.write_text(re.sub(r"(@@F\|rows_after\|)(\d+)", lambda m: f"{m[1]}{int(m[2]) + 1}",
                            facts_text), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "число строк разошлось" in res.stdout
    assert re.search(r"positions \d+→\d+, signals \d+→\d+, demo_orders \d+→\d+", res.stdout)
    # signals выросли слишком сильно — ⚠️, код возврата 0, «ВСЕ ✅» нет
    facts.write_text(re.sub(r"(@@F\|rows_after\|\d+/)(\d+)", lambda m: f"{m[1]}{int(m[2]) + 500}",
                            facts_text), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 0 and "⚠️ signals выросли на 500" in res.stdout
    assert "ИТОГ: ⚠️" in res.stdout and "ИТОГ: ВСЕ ✅" not in res.stdout
    assert not re.search(r"^  ❌", res.stdout, re.M)             # ни один пункт не провален
    # signals выросли в пределах нормы — ✅
    facts.write_text(re.sub(r"(@@F\|rows_after\|\d+/)(\d+)", lambda m: f"{m[1]}{int(m[2]) + 5}",
                            facts_text), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 0 and "ИТОГ: ВСЕ ✅" in res.stdout and "+5" in res.stdout


@needs_bash
@needs_pg
async def test_verify_9_12_does_not_claim_all_green_without_a_run(pool, db_name, tmp_path) -> None:
    res = run_verify(make_stub(tmp_path, db_name), tmp_path)
    assert "ИТОГ: ВСЕ ✅" not in res.stdout and res.returncode == 0
    assert res.stdout.count("⚪ НЕ ПРОВЕРЕНО") == 4              # пункты 3, 4, 5, 6
    assert len(re.findall(r"^  ✅", res.stdout, re.M)) == 2     # права: 1 и 2
