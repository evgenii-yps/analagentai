"""Этап 9.11: замер входа «на откате» (mean reversion) по истории — только чтение (ТЗ 9.11, §9).

Расчёт (analysis/mr_9_11.py) сверяется с ОТДЕЛЬНОЙ простой реализацией по тексту ТЗ (``ref_*``):
она написана иначе — по словарю баров и пересчётом с нуля на каждом баре, без накопительных
сумм и без общего кода. Мутации (§9.4) — копии модуля с одной намеренной поломкой: проверка
обязана на них упасть, иначе она ничего не проверяет.

Сети, биржи и docker нет; SQL и скрипты проверяются на настоящей PostgreSQL, ``docker compose
exec ... psql`` подменяется заглушкой. Стенд (синтетика) — не доказательство для сервера.
"""

# ruff: noqa: F811 — фикстуры pool и db_name импортированы из тестов 9.10 и используются как параметры
from __future__ import annotations

import ast
import importlib.util
import math
import os
import random
import re
import resource
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.demo_support import ROOT, build_database, drop_database, psql_raw, server_up
from tests.test_stage_9_10 import (  # noqa: F401 — фикстуры и помощники этапа 9.10
    add_inst,
    db_name,
    fingerprint,
    make_stub,
    mkpos,
    pool,
    run_script,
    script_env,
)

MR = ROOT / "analysis" / "mr_9_11.py"
VERDICT = ROOT / "analysis" / "verdict_9_11.py"
RUN_9_11 = ROOT / "analysis" / "run_9_11.sh"
VERIFY_9_11 = ROOT / "deploy" / "verify_9_11.sh"
SQL_11 = ROOT / "analysis" / "sql" / "9_11"
SQL_10 = ROOT / "analysis" / "sql" / "9_10"
BASE_COMMIT = "2324791"        # main, на котором основано ТЗ 9.11

needs_pg = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")
AS_OF_TXT = "2026-10-18 12:00:00+00"

HOUR = 3600
T0 = datetime(2026, 8, 1, tzinfo=UTC)           # время бара 0 синтетических рядов


def load_module(path: Path, name: str, *, replace: tuple[str, str] | None = None):
    source = path.read_text(encoding="utf-8")
    if replace:
        assert source.count(replace[0]) == 1, replace[0]
        source = source.replace(*replace)
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)    # noqa: S102 — свой же файл
    return module


@pytest.fixture(scope="module")
def mr():
    return load_module(MR, "mr_real")


# --------------------------------------------------------------------------
# Синтетические ряды
# --------------------------------------------------------------------------


def gen_bars(seed: int, n: int, *, missing=(), flat=(), start: float = 100.0) -> dict:
    """{час: (open, high, low, close)}: шум, обвалы с отскоком, плоские участки, широкие бары.

    `missing` — часы без бара (пропуски не заполняются); `flat` — отрезки (a, b) с ценой-константой.
    """
    rnd = random.Random(seed)
    bars, price = {}, start
    for i in range(n):
        if i in missing:
            continue
        if any(a <= i <= b for a, b in flat):
            bars[i] = (price, price, price, price)
            continue
        drift = rnd.gauss(0, 0.004)
        if i % 157 == 90:
            drift = -0.045                       # резкий обвал
        elif i % 157 in (91, 92):
            drift = 0.012                        # частичный отскок
        o = price * (1 + rnd.gauss(0, 0.0015))          # открытие не равно прошлому закрытию
        c = o * (1 + drift)
        wide = 0.03 if rnd.random() < 0.03 else 0.0
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.002)) + wide)
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.002)) - wide)
        bars[i] = (o, h, lo, c)
        price = c
    return bars


def write_csv(path: Path, series: dict[str, dict]) -> None:
    lines = ["token,open_time_utc,open,high,low,close"]
    for token in sorted(series):
        for i in sorted(series[token]):
            o, h, lo, c = series[token][i]
            ts = (T0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{token},{ts},{o!r},{h!r},{lo!r},{c!r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_coins(mr, tmp_path: Path, series: dict[str, dict]) -> dict:
    csv_path = tmp_path / "series.csv"
    write_csv(csv_path, series)
    return mr.load_series(str(csv_path))


# --------------------------------------------------------------------------
# Независимая реализация по тексту ТЗ (эталон)
# --------------------------------------------------------------------------


def ref_window(bars: dict, a: int, b: int, field: int = 3):
    """Значения поля баров a…b включительно либо None, если хоть одного бара нет."""
    if a < 0 or any(i not in bars for i in range(a, b + 1)):
        return None
    return [bars[i][field] for i in range(a, b + 1)]


def ref_mean_sd(bars: dict, t: int):
    w = ref_window(bars, t - 23, t)
    if w is None:
        return None, None
    if len(set(w)) == 1:                       # плоский участок: sd ровно 0
        return w[0], 0.0
    m = sum(w) / 24
    return m, math.sqrt(sum((x - m) ** 2 for x in w) / 24)


def ref_z(bars: dict, t: int):
    m, sd = ref_mean_sd(bars, t)
    if sd is None or sd == 0:
        return None
    return (bars[t][3] - m) / sd


def ref_r6(bars: dict, t: int):
    if t in bars and t - 6 in bars:
        return bars[t][3] / bars[t - 6][3] - 1
    return None


def ref_s6(bars: dict, t: int):
    vals = [ref_r6(bars, i) for i in range(t - 720, t)]
    if t - 720 < 0 or any(v is None for v in vals):
        return None
    m = sum(vals) / 720
    return math.sqrt(sum((v - m) ** 2 for v in vals) / 720)


def ref_segment(bars: dict, t: int):
    """Закрытия подряд идущих баров, заканчивающихся на t (с последнего пропуска)."""
    if t not in bars:
        return None
    s = t
    while s - 1 in bars:
        s -= 1
    return s


def ref_rsi(bars: dict, t: int):
    s = ref_segment(bars, t)
    if s is None or t - s < 14:
        return None
    cl = [bars[i][3] for i in range(s, t + 1)]
    gains = [max(cl[i] - cl[i - 1], 0) for i in range(1, len(cl))]
    losses = [max(cl[i - 1] - cl[i], 0) for i in range(1, len(cl))]
    ag, al = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    for g, lo in zip(gains[14:], losses[14:], strict=True):
        ag, al = (ag * 13 + g) / 14, (al * 13 + lo) / 14
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def ref_atr(bars: dict, t: int):
    s = ref_segment(bars, t)
    if s is None or t - s < 14:
        return None
    trs = []
    for i in range(s + 1, t + 1):
        _, h, lo, _ = bars[i]
        pc = bars[i - 1][3]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[:14]) / 14
    for tr in trs[14:]:
        a = (a * 13 + tr) / 14
    return a


def ref_cond(bars: dict, rule: str, t: int):
    if rule == "M1":
        z = ref_z(bars, t)
        return None if z is None else z <= -2.0
    if rule == "M2":
        r, s = ref_r6(bars, t), ref_s6(bars, t)
        return None if r is None or s is None or s <= 0 else r <= -2.5 * s
    if rule == "M3":
        v = ref_rsi(bars, t)
        return None if v is None else v <= 25
    if any(i not in bars for i in range(t - 6, t + 1)):
        return None
    prev = [ref_cond(bars, "M1", i) for i in range(t - 6, t)]
    if True in prev:
        seen = True
    elif None in prev:
        return None
    else:
        seen = False
    o, _, _, c = bars[t]
    return seen and c > o and c > min(bars[i][2] for i in range(t - 6, t))


def ref_events(bars: dict, rule: str, n: int) -> list[int]:
    out, last = [], None
    for t in range(1, n - 1):
        if ref_cond(bars, rule, t) is True and ref_cond(bars, rule, t - 1) is False:
            if (last is None or t - last >= 12) and t + 1 in bars:
                out.append(t)
                last = t
    return out


def ref_exit(bars: dict, e: int, kind: str, target: float, atr, last: int):
    """(gross %, часов, gap, оба, статус) либо None — по тексту ТЗ §5."""
    price = bars[e][0]
    pt = price * (1 + target / 100)
    if kind in ("X3", "X4"):
        if atr is None:
            return None
        ps = price - 3 * atr
    span = {"X2": 48, "X3": 48, "X4": 24}.get(kind)
    gap = both = False
    last_close = price
    j = e
    while j <= last:
        if span is not None and j > e + span - 1:
            break
        if j not in bars:
            gap = True
            j += 1
            continue
        o, h, lo, c = bars[j]
        last_close = c
        if kind in ("X3", "X4"):
            if lo <= ps:
                both = both or (kind == "X3" and h >= pt)
                return ((ps / price - 1) * 100, j - e + 1, gap, both, "stop")
            if kind == "X3" and h >= pt:
                return ((pt / price - 1) * 100, j - e + 1, gap, both, "target")
            if kind == "X4":
                m, _ = ref_mean_sd(bars, j)
                if m is not None and c >= m:
                    return ((c / price - 1) * 100, j - e + 1, gap, both, "mean")
        elif h >= pt:
            return ((pt / price - 1) * 100, j - e + 1, gap, both, "target")
        j += 1
    if kind == "X1":
        return ((last_close / price - 1) * 100, j - e, gap, False, "mtm")
    if e + span - 1 > last:
        return None
    if e + span - 1 not in bars:
        gap = True
    return ((last_close / price - 1) * 100, span, gap, both, "time")


def close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


# --------------------------------------------------------------------------
# §9.1. Эталон: индикаторы, условия, события, исходы
# --------------------------------------------------------------------------

N_REF = 2600
GAP_REF = {900}
FLAT_REF = [(300, 340), (1900, 1950)]


def ref_series():
    return {"BTC": gen_bars(11, N_REF, missing=GAP_REF, flat=FLAT_REF),
            "ETH": gen_bars(12, N_REF, start=50.0)}


def check_indicators(mod, coin, bars: dict) -> dict:
    """Сверяет с эталоном все индикаторы и условия; возвращает число непустых значений."""
    seen = {"z": 0, "s6": 0, "rsi": 0, "atr": 0, "M1": 0, "M2": 0, "M3": 0, "M4": 0}
    for t in range(coin.n):
        m, sd = ref_mean_sd(bars, t)
        assert close(coin.mean24[t], m), ("mean24", t)
        assert close(coin.sd24[t], sd), ("sd24", t)
        assert close(coin.z[t], ref_z(bars, t)), ("z", t)
        assert close(coin.r6[t], ref_r6(bars, t)), ("r6", t)
        assert close(coin.s6[t], ref_s6(bars, t)), ("s6", t)
        assert close(coin.rsi[t], ref_rsi(bars, t)), ("rsi", t)
        assert close(coin.atr[t], ref_atr(bars, t)), ("atr", t)
        for rule in ("M1", "M2", "M3", "M4"):
            assert coin.cond[rule][t] == ref_cond(bars, rule, t), (rule, t)
            seen[rule] += coin.cond[rule][t] is True
        seen["z"] += coin.z[t] is not None
        seen["s6"] += coin.s6[t] is not None
        seen["rsi"] += coin.rsi[t] is not None
        seen["atr"] += coin.atr[t] is not None
    return seen


def check_events_and_exits(mod, coins: dict, series: dict) -> dict:
    """События M1–M4 и исходы X1–X4 (правила и B0) совпадают с эталоном."""
    targets = {"BTC": 1.0, "ETH": 1.5}
    st = mod.Study(coins, targets)
    st.run_rules()
    st.run_b0()
    counts = {}
    for rule in ("M1", "M2", "M3", "M4"):
        for name, bars in series.items():
            coin = st.coins[name]
            want = ref_events(bars, rule, N_REF)
            assert st.events[rule][name] == want, (rule, name)
            counts[rule] = counts.get(rule, 0) + len(want)
            for kind in ("X1", "X2", "X3", "X4"):
                got = {tr.e: tr for tr in st.trades.get((rule, kind), []) if tr.coin == name}
                for t in want:
                    exp = ref_exit(bars, t + 1, kind, targets[name], ref_atr(bars, t),
                                   coin.last_valid)
                    # цена входа — открытие бара t+1; эталон берёт её сам
                    if exp is None:
                        assert t + 1 not in got, (rule, kind, t)
                        continue
                    tr = got[t + 1]
                    assert close(tr.gross, exp[0]) and tr.hold == exp[1], (rule, kind, t, exp)
                    assert bool(tr.gap) == exp[2] and bool(tr.both) == exp[3], (rule, kind, t)
                    assert tr.status == exp[4], (rule, kind, t)
    for kind in ("X2", "X3", "X4"):
        for name, bars in series.items():
            coin = st.coins[name]
            got = {tr.e: tr for tr in st.trades.get(("B0", kind), []) if tr.coin == name}
            for e in sorted(bars):
                if e < 1:
                    continue
                exp = ref_exit(bars, e, kind, targets[name], ref_atr(bars, e - 1), coin.last_valid)
                if exp is None:
                    assert e not in got, (kind, e)
                else:
                    assert e in got and close(got[e].gross, exp[0]), (kind, e)
    return counts


def test_indicators_match_the_reference_on_a_2600_bar_series(mr, tmp_path) -> None:
    series = ref_series()
    coins = load_coins(mr, tmp_path, series)
    for name, bars in series.items():
        mr.compute_indicators(coins[name])
        seen = check_indicators(mr, coins[name], bars)
        assert min(seen.values()) > 0, (name, seen)        # всё реально встречается


def test_events_and_outcomes_match_the_reference(mr, tmp_path) -> None:
    series = ref_series()
    coins = load_coins(mr, tmp_path, series)
    counts = check_events_and_exits(mr, coins, series)
    assert min(counts.values()) >= 5, counts


def test_flat_stretches_give_no_z_and_no_event(mr, tmp_path) -> None:
    series = ref_series()
    coin = load_coins(mr, tmp_path, series)["BTC"]
    mr.compute_indicators(coin)
    for t in range(323, 341):                  # окно 24 баров целиком в плоском участке
        assert coin.sd24[t] == 0.0 and coin.z[t] is None and coin.cond["M1"][t] is None
    for t in range(1930, 1951):
        assert coin.sd24[t] == 0.0 and coin.z[t] is None


# --------------------------------------------------------------------------
# §9.2. Пропуск часа: индикатор, окно которого задевает пропуск, — нет значения
# --------------------------------------------------------------------------


def test_a_missing_hour_blanks_every_indicator_whose_window_touches_it(mr, tmp_path) -> None:
    g = 900
    series = ref_series()
    coin = load_coins(mr, tmp_path, series)["BTC"]
    mr.compute_indicators(coin)
    assert not coin.ok[g]
    assert all(coin.mean24[t] is None for t in range(g, g + 24))      # окно 24 баров
    assert coin.mean24[g - 1] is not None and coin.mean24[g + 24] is not None
    assert coin.r6[g] is None and coin.r6[g + 6] is None             # нет close_t / нет close_{t−6}
    assert all(coin.r6[t] is not None for t in (*range(g + 1, g + 6), g + 7))
    assert all(coin.s6[t] is None for t in range(g + 1, g + 727))     # 720 баров r6 до t−1
    assert coin.s6[g + 727] is not None
    assert all(coin.rsi[t] is None and coin.atr[t] is None for t in range(g, g + 15))
    assert coin.rsi[g + 15] is not None and coin.atr[g + 15] is not None   # перезапуск Уайлдера
    assert all(coin.cond["M4"][t] is None for t in range(g, g + 7))   # окно t−6…t задевает пропуск


def crash_series(*, with_gap: bool) -> dict:
    """Плоско-шумовой ряд с обвалом на баре 105; пропуск часа 100 делает окно 24 баров неполным."""
    bars = gen_bars(5, 300, missing={100} if with_gap else set())
    o, h, lo, c = bars[105]
    crash = bars[104][3] * 0.90
    bars[105] = (bars[104][3], bars[104][3], crash * 0.999, crash)
    price = crash
    for i in range(106, 300):
        if i in bars:
            o2, h2, l2, c2 = bars[i]
            k = price / o2
            bars[i] = (price, h2 * k, l2 * k, c2 * k)
            price = c2 * k
    return bars


def test_no_event_is_created_when_the_signal_window_touches_a_gap(mr, tmp_path) -> None:
    free = crash_series(with_gap=False)
    gapped = crash_series(with_gap=True)
    coins = load_coins(mr, tmp_path, {"BTC": free})
    mr.compute_indicators(coins["BTC"])
    assert 105 in mr.find_events(coins["BTC"], "M1")                # без пропуска обвал — событие
    assert coins["BTC"].cond["M1"][105] is True
    coins = load_coins(mr, tmp_path, {"BTC": gapped})
    mr.compute_indicators(coins["BTC"])
    assert coins["BTC"].cond["M1"][105] is None                      # окно 82…105 задевает час 100
    assert 105 not in mr.find_events(coins["BTC"], "M1")


# --------------------------------------------------------------------------
# §9.3. Выходы: ручные ряды
# --------------------------------------------------------------------------


def mk_coin(mr, bars: list, *, mean24: float | None = None):
    """Coin из списка баров (o, h, l, c) либо None (пропуск); mean24 — одно значение на все бары."""
    coin = mr.Coin("BTC", 0, len(bars))
    for i, b in enumerate(bars):
        if b is not None:
            coin.o[i], coin.h[i], coin.l[i], coin.c[i] = b
            coin.ok[i] = True
    valid = [i for i, b in enumerate(bars) if b is not None]
    coin.first_valid, coin.last_valid = valid[0], valid[-1]
    coin.mean24 = [mean24] * len(bars)
    return coin


def calm(n: int, price: float = 100.0) -> list:
    return [(price, price + 0.2, price - 0.2, price)] * n


def check_exits(mr) -> None:
    T, E, ATR = 1.0, 100.0, 0.5            # цель 101, стоп 98,5

    def run(bars, kind, atr=ATR, mean24=None, limit=None):
        coin = mk_coin(mr, bars, mean24=mean24)
        return mr.simulate(coin, 0, E, kind, T, atr, limit)

    # цель в баре входа (бар входа участвует целиком)
    res = run([(100, 101.5, 99.9, 101), *calm(60)], "X3")
    assert res[0] == pytest.approx(1.0) and res[1] == 1 and res[5] == "target"
    # цель через три бара
    res = run([*calm(2), (100, 101.2, 99.5, 101), *calm(60)], "X2")
    assert res[0] == pytest.approx(1.0) and res[1] == 3 and res[5] == "target"
    # стоп (цена выхода = P_S)
    res = run([*calm(1), (100, 100.4, 98.0, 98.2), *calm(60)], "X3")
    assert res[0] == pytest.approx(-1.5) and res[1] == 2 and res[5] == "stop" and res[3] == 0
    # оба в одном баре → стоп, признак «оба» поднят
    res = run([(100, 102.0, 98.0, 100.5), *calm(60)], "X3")
    assert res[0] == pytest.approx(-1.5) and res[3] == 1 and res[5] == "stop"
    # срок X2/X3: закрытие бара e+47 (48 ч); X4: бара e+23 (24 ч)
    path = [*calm(47), (100, 100.3, 99.7, 100.4), *calm(10)]
    for kind in ("X2", "X3"):
        res = run(path, kind)
        assert res[0] == pytest.approx(0.4) and res[1] == 48 and res[5] == "time"
    path4 = [*calm(23), (100, 100.3, 99.7, 100.2), *calm(10)]
    res = run(path4, "X4", mean24=102.0)
    assert res[0] == pytest.approx(0.2) and res[1] == 24 and res[5] == "time"
    # X4: выход по close первого бара, где close ≥ mean24 этого бара
    res = run([*calm(2), (99, 100.5, 98.9, 100.3), *calm(40)], "X4", mean24=100.2)
    assert res[0] == pytest.approx(0.3) and res[1] == 3 and res[5] == "mean"
    # X4 не имеет цели: high ≥ P_T сам по себе выход не даёт
    res = run([(100, 105.0, 99.9, 100.1), *calm(30)], "X4", mean24=102.0)
    assert res[5] == "time" and res[1] == 24
    # X1: без конца данных → net_mtm по последнему close
    res = run(calm(10), "X1")
    assert res[5] == "mtm" and res[0] == pytest.approx(0.0) and res[1] == 10
    # X1: цель через тысячу баров — стопа и срока нет
    res = run([*calm(1000), (100, 101.3, 100, 101)], "X1")
    assert res[5] == "target" and res[1] == 1001
    # X1: просадка до цели учтена в MAE (глубина, %)
    res = run([(100, 100.2, 90.0, 95), (95, 101.5, 94, 101), *calm(5)], "X1")
    assert res[4] == pytest.approx(10.0) and res[5] == "target"
    # срок ещё не истёк к концу данных → не завершена (None)
    assert run(calm(30), "X2") is None and run(calm(30), "X3") is None
    # X3/X4 без ATR сделка не считается
    assert run(calm(60), "X3", atr=None) is None
    # пропуск на пути: считается по имеющимся барам, gap = 1
    res = run([*calm(2), None, (100, 101.4, 99.9, 101), *calm(60)], "X3")
    assert res[2] == 1 and res[5] == "target" and res[1] == 4
    # срок в пропущенном баре: закрытие по последнему имеющемуся бару
    bars = [*calm(47), None, *calm(10)]
    res = run(bars, "X2")
    assert res[2] == 1 and res[5] == "time" and res[0] == pytest.approx(0.0)


def test_exits_follow_the_written_rules(mr) -> None:
    check_exits(mr)


def test_net_is_gross_minus_cost_once(mr) -> None:
    assert mr.net_of(1.0, mr.COST_MAIN) == pytest.approx(0.78)
    assert mr.net_of(-1.5, mr.COST_STRESS) == pytest.approx(-2.0)
    trades = [mr.Trade("BTC", 1, (1.0, 5, 0, 0, 0.0, "target")),
              mr.Trade("BTC", 2, (-1.5, 3, 1, 1, 0.0, "stop"))]
    s = mr.stats(trades, mr.COST_MAIN, lambda t: t.e * 3600)
    assert s["mean"] == pytest.approx((0.78 - 1.72) / 2)        # (1,0 − 0,22) и (−1,5 − 0,22)
    assert s["worst"] == pytest.approx(-1.72) and s["gap"] == 50.0 and s["both"] == 50.0


def test_stats_on_hand_calculated_trades(mr) -> None:
    gross = [2.0, 1.0, -1.0, -3.0, 0.22]            # при C = 0,22: 1,78; 0,78; −1,22; −3,22; 0
    trades = [mr.Trade("BTC", i * 24 + 1, (g, i + 1, 0, 0, 0.0, "x")) for i, g in enumerate(gross)]
    trades.append(mr.Trade("ETH", 1, (0.5, 10, 0, 0, 0.0, "x")))       # те же сутки, что и первая
    s = mr.stats(trades, 0.22, lambda t: t.e * 3600)
    nets = [1.78, 0.78, -1.22, -3.22, 0.0, 0.28]
    assert s["n"] == 6 and s["n_eff"] == 5
    assert s["pos"] == pytest.approx(100 * 3 / 6)               # >0: 1,78; 0,78; 0,28
    assert s["mean"] == pytest.approx(sum(nets) / 6)
    assert s["median"] == pytest.approx(0.14)                   # между 0 и 0,28
    assert s["payoff"] == pytest.approx(((1.78 + 0.78 + 0.28) / 3) / ((1.22 + 3.22) / 2))
    assert s["pf"] == pytest.approx((1.78 + 0.78 + 0.28) / (1.22 + 3.22))
    assert s["worst"] == pytest.approx(-3.22)
    assert s["p5"] == pytest.approx(-3.22 + 0.25 * (-1.22 + 3.22))      # линейная интерполяция
    assert s["hold"] == pytest.approx((1 + 2 + 3 + 4 + 5 + 10) / 6)


def test_percentile_is_linear_interpolation(mr) -> None:
    assert mr.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert mr.percentile([1.0, 2.0, 3.0, 4.0], 0.9) == pytest.approx(3.7)
    assert mr.percentile([7.0], 0.99) == 7.0 and mr.percentile([], 0.5) is None


# --------------------------------------------------------------------------
# Событие: фронт, пауза 12 баров, бар входа
# --------------------------------------------------------------------------


def coin_with_cond(mr, cond: list, n: int | None = None):
    n = len(cond) if n is None else n
    coin = mk_coin(mr, calm(n))
    coin.cond = {"M1": cond, "M2": cond, "M3": cond, "M4": cond}
    return coin


def check_lockout(mr) -> None:
    # фронт на 3, 8 (5 баров после предыдущего → подавлен), 20 (17 баров → событие)
    pattern = [False] * 40
    for i in (3, 8, 20):
        pattern[i] = True
    assert mr.find_events(coin_with_cond(mr, pattern), "M1") == [3, 20]
    # ровно 12 баров — событие
    pattern = [False] * 40
    pattern[3] = pattern[15] = True
    assert mr.find_events(coin_with_cond(mr, pattern), "M1") == [3, 15]


def test_events_are_rising_edges_with_a_12_bar_pause(mr) -> None:
    check_lockout(mr)
    # условие держится несколько баров — событие одно (на первом)
    held = [False, False, True, True, True, False, False, False]
    assert mr.find_events(coin_with_cond(mr, held), "M1") == [2]
    # на t−1 значения нет — события нет (ложно должно быть ИЗВЕСТНО)
    unknown = [None, True, False, False]
    assert mr.find_events(coin_with_cond(mr, unknown), "M1") == []
    # последний бар данных не даёт события: бара входа t+1 нет
    last = [False, False, False, True]
    assert mr.find_events(coin_with_cond(mr, last), "M1") == []


def test_entry_is_the_open_of_the_next_bar(mr) -> None:
    coin = mk_coin(mr, [(1, 1, 1, 1), (2, 2, 2, 2), (3, 3, 3, 3)])
    assert mr.entry_price(coin, 0) == 2 and mr.entry_price(coin, 1) == 3


# --------------------------------------------------------------------------
# B1: вход по открытию первого бара, начавшегося не раньше opened_at
# --------------------------------------------------------------------------


def test_b1_enters_at_the_first_bar_that_started_not_earlier_than_opened_at(mr) -> None:
    bars = [(100 + i, 100 + i + 0.2, 100 + i - 0.2, 100 + i + 0.1) for i in range(120)]
    bars[5] = None                                              # пропущенный бар
    coin = mk_coin(mr, bars)
    coin.atr = [0.5] * len(bars)
    st = mr.Study({"BTC": coin}, {"BTC": 1.0})
    t0 = coin.t0
    # 10:20 → бар 11:00 (индекс 11); ровно 10:00 → бар 10:00; бар 5 пропущен → берётся бар 6
    skipped = st.run_b1([("BTC", t0 + 10 * 3600 + 1200), ("BTC", t0 + 10 * 3600),
                         ("BTC", t0 + 5 * 3600), ("BTC", t0 + 9999 * 3600)])
    entries = sorted(tr.e for tr in st.trades[("B1", "X1")])
    assert entries == [6, 10, 11] and skipped == 1


# --------------------------------------------------------------------------
# Тест сдвигом
# --------------------------------------------------------------------------


def hour_of_day_b0(n: int, f) -> list:
    return [f(i % 24) for i in range(n)]


def check_shift(mr) -> None:
    n = 24 * 120                                       # 120 суток
    # B0 зависит только от часа суток; события — все в 05:00 ⇒ любой сдвиг на k × 24 ч даёт то же
    # среднее, p = 1 (сдвиг не кратный 24 ч сместил бы час и снизил бы среднее)
    b0 = {"BTC": hour_of_day_b0(n, lambda h: 1.0 if h == 5 else -1.0)}
    events = [("BTC", 24 * d + 5) for d in range(10, 20)]
    res = mr.shift_test(events, [1.0] * len(events), b0, n)
    assert res["shifts"] == 120 - 2 * 30 + 1 and res["p"] == 1.0
    assert res["obs"] == pytest.approx(1.0 - 0.22)


def test_shift_keeps_time_of_day_and_is_deterministic(mr) -> None:
    check_shift(mr)
    n = 24 * 120
    b0 = {"BTC": hour_of_day_b0(n, lambda h: 1.0 if h == 5 else -1.0)}
    events = [("BTC", 24 * d + 5) for d in range(10, 20)]
    a = mr.shift_test(events, [1.0] * 10, b0, n)
    b = mr.shift_test(events, [1.0] * 10, b0, n)
    assert a == b


def test_shift_p_is_small_when_the_events_sit_on_the_good_hours(mr) -> None:
    n = 24 * 200
    rnd = random.Random(4)
    day_effect = [rnd.gauss(0, 1) for _ in range(n // 24)]
    b0 = {"BTC": [day_effect[i // 24] for i in range(n)]}
    best = sorted(range(len(day_effect)), key=lambda d: -day_effect[d])[:5]
    events = [("BTC", 24 * d) for d in best if 30 <= d < 170]
    own = [b0["BTC"][e] for _, e in events]
    res = mr.shift_test(events, own, b0, n, cost=0.0)
    assert res["p"] is not None and res["p"] <= 0.0125, res
    none = mr.shift_test([], [], b0, n)
    assert none["p"] is None and none["shifts"] == 0


def test_shift_with_too_few_days_has_no_shifts(mr) -> None:
    n = 24 * 50
    b0 = {"BTC": [0.0] * n}
    res = mr.shift_test([("BTC", 100)], [0.0], b0, n)
    assert res["shifts"] == 0 and res["p"] is None


# --------------------------------------------------------------------------
# Разбивки: половины, годы, режим
# --------------------------------------------------------------------------


def test_breakdown_keys_split_halves_exactly_at_2024_04_01(mr) -> None:
    ep = mr.ts_to_epoch("2024-04-01 00:00:00")
    coin = mr.Coin("BTC", ep - 5 * 3600, 20)
    st = mr.Study({"BTC": coin}, {"BTC": 1.0})
    keys = mr.breakdown_keys(st, {ep // 86400: True, ep // 86400 - 1: False})
    before = mr.Trade("BTC", 4, (0, 1, 0, 0, 0, "x"))       # 2024-03-31 23:00
    after = mr.Trade("BTC", 5, (0, 1, 0, 0, 0, "x"))        # 2024-04-01 00:00
    assert keys["half"](before) == "до" and keys["half"](after) == "после"
    assert keys["year"](before) == "2024" and keys["coin"](after) == "BTC"
    assert keys["regime"](before) == "V1=FALSE" and keys["regime"](after) == "V1=TRUE"
    far = mr.Trade("BTC", 19, (0, 1, 0, 0, 0, "x"))
    assert keys["regime"](far) == "V1=TRUE"                  # тот же день 2024-04-01


def test_facts_are_parsed_as_specified(mr, tmp_path) -> None:
    facts = tmp_path / "f.txt"
    facts.write_text(
        "@@T|BTC|1.0\n@@T|SOL|1.25\n@@P|7|SOL|2026-10-18 12:15:00\n@@V|2026-10-18|1\n"
        "@@V|2026-10-19|\n@@S|7|SOL|2026-10-18 12:15:00|100.5|target|closed\n@@F|x|1\n",
        encoding="utf-8")
    data = mr.read_facts(str(facts))
    assert data["targets"] == {"BTC": 1.0, "SOL": 1.25}
    assert data["positions"] == [("SOL", mr.ts_to_epoch("2026-10-18 12:15:00"), 7)]
    assert data["regime"] == {mr.ts_to_epoch("2026-10-18 00:00:00") // 86400: True}
    assert data["sol"][0][4:] == ["target", "closed"]


# --------------------------------------------------------------------------
# §9.4. Мутации: каждая обязана ловиться тестом
# --------------------------------------------------------------------------

MUTATIONS = {
    "а": ("return coin.o[t + 1]", "return coin.c[t]"),
    "б": ("return t - Z_WINDOW + 1, t + 1", "return t - Z_WINDOW + 2, t + 2"),
    "в": ("return t - R6_SD_WINDOW, t", "return t - R6_SD_WINDOW + 1, t + 1"),
    "г": ("xp = ps  # ТЗ §5: оба в одном баре", "xp = pt  # ТЗ §5: оба в одном баре"),
    "д": ("if t - last >= LOCKOUT_BARS:", "if True:"),
    "е": ("return gross - cost", "return gross - 2 * cost"),
    "ж": ("s = SHIFT_STEP_HOURS * k", "s = SHIFT_STEP_HOURS * k + 1"),
}


def mutated(key: str):
    return load_module(MR, f"mr_mut_{key}", replace=MUTATIONS[key])


def test_mutation_a_entry_at_close_t_instead_of_open_t_plus_1_is_caught(tmp_path) -> None:
    mod = mutated("а")
    series = ref_series()
    coins = load_coins(mod, tmp_path, series)
    with pytest.raises(AssertionError):
        check_events_and_exits(mod, coins, series)


def test_mutation_b_window_with_bar_t_plus_1_is_caught(tmp_path) -> None:
    mod = mutated("б")
    series = ref_series()
    coins = load_coins(mod, tmp_path, series)
    mod.compute_indicators(coins["ETH"])
    with pytest.raises(AssertionError):
        check_indicators(mod, coins["ETH"], series["ETH"])


def test_mutation_c_s6_with_the_current_bar_is_caught(tmp_path) -> None:
    mod = mutated("в")
    series = ref_series()
    coins = load_coins(mod, tmp_path, series)
    mod.compute_indicators(coins["ETH"])
    with pytest.raises(AssertionError):
        check_indicators(mod, coins["ETH"], series["ETH"])


def test_mutation_d_both_in_a_bar_counted_as_target_is_caught(tmp_path) -> None:
    mod = mutated("г")
    with pytest.raises(AssertionError):
        check_exits(mod)
    series = ref_series()
    coins = load_coins(mod, tmp_path, series)
    with pytest.raises(AssertionError):                      # и на случайном ряду с широкими барами
        check_events_and_exits(mod, coins, series)


def test_mutation_e_no_pause_between_events_is_caught(tmp_path) -> None:
    mod = mutated("д")
    with pytest.raises(AssertionError):
        check_lockout(mod)
    series = ref_series()
    coins = load_coins(mod, tmp_path, series)
    with pytest.raises(AssertionError):
        check_events_and_exits(mod, coins, series)


def test_mutation_f_cost_subtracted_twice_is_caught() -> None:
    mod = mutated("е")
    with pytest.raises(AssertionError):
        assert mod.net_of(1.0, mod.COST_MAIN) == pytest.approx(0.78)
    trades = [mod.Trade("BTC", 1, (1.0, 5, 0, 0, 0.0, "target"))]
    with pytest.raises(AssertionError):
        assert mod.stats(trades, 0.22, lambda t: 0)["mean"] == pytest.approx(0.78)


def test_mutation_g_shift_not_a_multiple_of_24_hours_is_caught() -> None:
    mod = mutated("ж")
    with pytest.raises(AssertionError):
        check_shift(mod)


def test_every_mutation_target_exists_exactly_once() -> None:
    source = MR.read_text(encoding="utf-8")
    assert set(MUTATIONS) == set("абвгдеж")
    for old, new in MUTATIONS.values():
        assert source.count(old) == 1 and old != new


# --------------------------------------------------------------------------
# §9.5. Только стандартная библиотека; константы в одном блоке
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [MR, VERDICT], ids=lambda p: p.name)
def test_python_modules_import_only_the_standard_library(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported and imported <= set(sys.stdlib_module_names), imported


def test_all_constants_of_the_method_are_in_one_block_with_tz_references() -> None:
    text = MR.read_text(encoding="utf-8")
    block = text.split("# КОНСТАНТЫ МЕТОДИКИ", 1)[1].split("# ======", 2)[1]
    expect = {"Z_WINDOW = 24": "§4", "Z_THRESHOLD = -2.0": "§4", "R6_LAG = 6": "§4",
              "R6_SD_WINDOW = 720": "§4", "R6_K = 2.5": "§4", "RSI_PERIOD = 14": "§4",
              "RSI_LEVEL = 25.0": "§4", "M4_LOOKBACK = 6": "§4", "LOCKOUT_BARS = 12": "§4",
              "ATR_PERIOD = 14": "§5", "ATR_MULT = 3.0": "§5", "HORIZON_X23 = 48": "§5",
              "HORIZON_X4 = 24": "§5", "COST_MAIN = 0.22": "§5", "COST_STRESS = 0.50": "§5",
              'HALF_SPLIT = "2024-04-01"': "§7", "ALPHA_FAMILY = 0.0125": "§8",
              "SHIFT_MIN_DAYS = 30": "§7", "SHIFT_STEP_HOURS = 24": "§7"}
    for const, ref in expect.items():
        line = next((ln for ln in block.splitlines() if ln.startswith(const)), None)
        assert line is not None, const
        assert ref in line, (const, line)


def test_the_numbers_of_the_method_are_not_repeated_outside_the_constants() -> None:
    text = MR.read_text(encoding="utf-8")
    code = text.split("# КОНСТАНТЫ МЕТОДИКИ", 1)[1].split("# ======", 2)[2]
    code = "\n".join(ln.split("#")[0] for ln in code.splitlines())
    for literal in ("720", "0.0125", "2.5", "0.22", "0.50", "-2.0"):
        assert not re.search(rf"(?<![\w.]){re.escape(literal)}(?![\w.])", code), literal


# --------------------------------------------------------------------------
# Вердикт §8
# --------------------------------------------------------------------------

COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def good_facts(rule: str = "M1") -> dict[str, str]:
    facts = {"B0.x3_all": "-0.2"}
    for r in ("M1", "M2", "M3", "M4"):
        ok = r == rule
        facts |= {f"{r}.x3_all": "0.1" if ok else "-0.3", f"{r}.x3_h1": "0.05" if ok else "-0.3",
                  f"{r}.x3_h2": "0.2" if ok else "-0.3", f"{r}.p": "0.01" if ok else "0.5",
                  f"{r}.x3_all_c50": "0.05", f"{r}.x3_h1_c50": "0.01", f"{r}.x3_h2_c50": "0.1",
                  f"{r}.x3_r_V1=TRUE": "0.4", f"{r}.x1_mean": "0.2", f"{r}.x1_reach": "97.5"}
        for c in COINS:
            facts[f"{r}.x3_c_{c}"] = "0.1" if ok else "-0.1"
    return facts


@pytest.fixture(scope="module")
def vd():
    return load_module(VERDICT, "verdict_real")


def test_verdict_candidate_when_all_four_criteria_hold(vd) -> None:
    lines = vd.render(good_facts("M2"))
    assert "M2: ИТОГ — кандидат" in lines
    assert all(f"{r}: ИТОГ — не кандидат" in lines for r in ("M1", "M3", "M4"))
    assert any(ln.startswith("ИТОГ: кандидат(ы) на теневой режим: M2") for ln in lines)


@pytest.mark.parametrize("change,which", [
    ({"M1.x3_all": "-0.01"}, (1,)), ({"M1.x3_h1": "0"}, (1,)), ({"M1.x3_h2": ""}, (1,)),
    ({"M1.x3_c_SOL": "-0.1", "M1.x3_c_XRP": "0"}, (2,)),
    ({"M1.p": "0.0126"}, (3,)), ({"M1.p": ""}, (3,)),
    ({"B0.x3_all": "0.1"}, (4,)), ({"B0.x3_all": "0.2"}, (4,)),
])
def test_verdict_one_failed_criterion_is_enough_to_refuse(vd, change, which) -> None:
    facts = good_facts("M1") | change
    res = vd.evaluate(facts, "M1")
    assert res["candidate"] is False
    assert tuple(n for n, (ok, _) in res["crit"].items() if not ok) == which


def test_verdict_edges_four_of_five_coins_and_p_equal_to_the_threshold(vd) -> None:
    facts = good_facts("M1") | {"M1.x3_c_DOGE": "-0.2", "M1.p": "0.0125"}
    assert vd.evaluate(facts, "M1")["candidate"] is True
    facts = good_facts("M1") | {"M1.x3_c_DOGE": "-0.2", "M1.x3_c_XRP": "0.0"}
    assert vd.evaluate(facts, "M1")["crit"][2][0] is False      # ноль — не «больше нуля»


def test_verdict_when_nothing_passes_names_the_closest_rule(vd) -> None:
    facts = good_facts("M3") | {"M3.p": "0.2", "M3.x3_c_BTC": "-0.1", "M3.x3_c_ETH": "-0.1"}
    lines = vd.render(facts)
    assert any("ни одно правило не прошло" in ln for ln in lines)
    near = next(ln for ln in lines if ln.startswith("Ближе всего"))
    assert near.startswith("Ближе всего M3: прошло 2 из 4") and "p теста сдвигом" in near
    assert not any("кандидат(ы)" in ln for ln in lines)


def test_verdict_prints_the_reference_lines(vd) -> None:
    lines = vd.render(good_facts("M1"))
    ref = next(ln for ln in lines if ln.startswith("M1 справочно"))
    assert "C = 0,50" in ref and "критерий 1 держится" in ref and "V1 = TRUE" in ref
    facts = good_facts("M1") | {"M1.x3_h1_c50": "-0.1"}
    assert "не держится" in next(ln for ln in vd.render(facts) if ln.startswith("M1 справочно"))


def test_mutation_significance_threshold_of_0_05_is_caught() -> None:
    mod = load_module(VERDICT, "verdict_mut", replace=("ALPHA = 0.0125", "ALPHA = 0.05"))
    facts = good_facts("M1") | {"M1.p": "0.03"}
    with pytest.raises(AssertionError):
        assert mod.evaluate(facts, "M1")["candidate"] is False


# --------------------------------------------------------------------------
# §9.6–9.7. SQL: общие определения как в 9.10 + high/low; только чтение
# --------------------------------------------------------------------------

SQL_FILES = ["00_scope.sql", "01_series.sql"]
SERIES_START = "-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.1"
SERIES_END = "-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ РЯДОВ ==="
FLAGS_START = "-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.3"
FLAGS_END = "-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ФЛАГОВ ==="
HIGH_LOW = [
    ("c.open::double precision AS open, c.close::double precision AS close",
     "c.open::double precision AS open, c.high::double precision AS high,\n"
     "           c.low::double precision AS low, c.close::double precision AS close"),
    ("SELECT split_part(i.symbol, '/', 1) AS token, o.ts AS open_time, o.open, o.close",
     "SELECT split_part(i.symbol, '/', 1) AS token, o.ts AS open_time, o.open, o.high, o.low, "
     "o.close"),
    ("SELECT token, open_time, open, close FROM h_hist",
     "SELECT token, open_time, open, high, low, close FROM h_hist"),
    ("SELECT l.token, l.open_time, l.open, l.close",
     "SELECT l.token, l.open_time, l.open, l.high, l.low, l.close"),
]


def fragment(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end) + len(end)
    return text[i:j]


def sql11(name: str) -> str:
    return (SQL_11 / name).read_text(encoding="utf-8")


def normalised_series_fragment(text: str, from_9_10: bool) -> str:
    """Фрагмент «ряды» без строки-заголовка (в ней разные ссылки на ТЗ и на файл теста)."""
    body = fragment(text, SERIES_START, SERIES_END).split("\n", 1)[1]
    if from_9_10:
        for old, new in HIGH_LOW:
            assert body.count(old) == 1, old
            body = body.replace(old, new)
    return body


def test_series_definitions_equal_9_10_except_high_and_low() -> None:
    ref = normalised_series_fragment((SQL_10 / "00_data.sql").read_text(encoding="utf-8"), True)
    for name in SQL_FILES:
        assert normalised_series_fragment(sql11(name), False) == ref, name
    for name in (p.name for p in SQL_10.glob("0[0-4]_*.sql")):          # 9.10 внутри согласована
        text = (SQL_10 / name).read_text(encoding="utf-8")
        assert normalised_series_fragment(text, True) == ref, name


def test_flag_definitions_are_byte_identical_to_9_10_04() -> None:
    ref = fragment((SQL_10 / "04_history_returns.sql").read_text(encoding="utf-8"),
                   FLAGS_START, FLAGS_END)
    ours = fragment(sql11("00_scope.sql"), FLAGS_START, FLAGS_END)
    assert ours.replace("test_stage_9_11", "test_stage_9_10") == ref
    assert "V1" in ours and "n50 = 50" in ours and "1 PRECEDING" in ours


def test_the_only_difference_in_the_series_is_high_and_low_columns() -> None:
    text = sql11("01_series.sql")
    for must in ("c.high::double precision AS high", "o.high, o.low", "open, high, low, close"):
        assert must in text
    assert "WITH tok(token)" in text and "h_last AS" in text and "n_h = 24" in text


def test_sql_files_are_read_only() -> None:
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|"
                           r"VACUUM|ANALYZE|REINDEX|REFRESH|LOCK|CALL|DO)\b", re.I)
    for name in SQL_FILES:
        text = sql11(name)
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in text.splitlines()
                         if not ln.startswith("\\echo"))
        code = re.sub(r"'[^']*'", "''", code)
        code = code.replace("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", "")
        assert not forbidden.search(code), (name, forbidden.search(code).group(0))
        assert not re.search(r"\bCOPY\b[^;]*\bFROM\b\s+(STDIN|PROGRAM|'|\w+\s*;)", code,
                             re.I | re.S), name
        no_comments = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
        marker = no_comments.find("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
        first = re.search(r"^(WITH|SELECT|COPY) ", no_comments, re.M).start()
        assert 0 <= marker < first, name
    assert "TO STDOUT WITH CSV HEADER" in sql11("01_series.sql")


# --------------------------------------------------------------------------
# Блоки 00 и 01 на настоящей PostgreSQL
# --------------------------------------------------------------------------

START = datetime(2026, 8, 1, tzinfo=UTC)


def _d(x: float) -> Decimal:
    return Decimal(f"{x:.8f}")


async def put_bars(pool, token: str, bars: dict, *, source: str = "hist",
                   shift: float = 1.0) -> None:
    """Часовые бары {час: (o, h, l, c)} в backtest.candles (hist) либо public.ohlcv (live)."""
    rows = []
    ids = await add_inst(pool, token) if source == "live" else {}
    for i, (o, h, lo, c) in bars.items():
        ts = START + timedelta(hours=i)
        o, h, lo, c = o * shift, h * shift, lo * shift, c * shift
        if source == "hist":
            rows.append((f"{token}-USDT", "1H", ts, ts + timedelta(hours=1), _d(o), _d(h), _d(lo),
                         _d(c), Decimal(1)))
        else:
            rows.append((ids[token], "1h", ts, o, h, lo, c, 1.0))
    if source == "hist":
        await pool.executemany(
            "INSERT INTO backtest.candles (inst_id, bar, open_time, close_time, open, high, low, "
            "close, volume) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);", rows)
    else:
        await pool.executemany(
            "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING;", rows)


def run_sql(db: str, name: str, *, as_of: str = AS_OF_TXT) -> str:
    sql = sql11(name)
    cmd = ["psql", "-X", "-q", "-A", "-F", "|", "-h", os.environ.get("PGTEST_HOST", "/tmp"),
           "-p", os.environ.get("PGTEST_PORT", "54329"), "-U", "agenttrade_ro", "-d", db,
           "-v", "ON_ERROR_STOP=1", "-v", f"as_of={as_of}"]
    res = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


def fact_lines(out: str, tag: str) -> list[list[str]]:
    return [ln.split("|")[1:] for ln in out.splitlines() if ln.startswith(f"@@{tag}|")]


@needs_pg
async def test_series_csv_has_the_specified_columns_order_and_the_merge_rule(pool, db_name) -> None:
    hist = gen_bars(1, 60)
    await put_bars(pool, "BTC", hist)
    await put_bars(pool, "ETH", gen_bars(2, 60, start=50.0))
    live = {i: b for i, b in gen_bars(3, 80).items() if i >= 40}        # 40…79: перекрытие 40…59
    await put_bars(pool, "BTC", live, source="live", shift=2.0)
    out = run_sql(db_name, "01_series.sql", as_of="2026-10-18 12:00:00+00")
    lines = out.splitlines()
    assert lines[0] == "token,open_time_utc,open,high,low,close"
    rows = [ln.split(",") for ln in lines[1:]]
    keys = [(r[0], r[1]) for r in rows]
    assert keys == sorted(keys)                                          # token, open_time
    assert {r[0] for r in rows} == {"BTC", "ETH"}
    btc = {r[1]: r for r in rows if r[0] == "BTC"}
    assert len(btc) == 80                                                # 60 истории + 20 после неё
    for i in (45, 59):                                                   # перекрытие — история
        r = btc[(START + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")]
        assert math.isclose(float(r[2]), hist[i][0], rel_tol=1e-7)
        assert float(r[3]) >= float(r[2]) >= float(r[4])                 # high ≥ open ≥ low
    r = btc[(START + timedelta(hours=75)).strftime("%Y-%m-%d %H:%M:%S")]
    assert math.isclose(float(r[2]), live[75][0] * 2.0, rel_tol=1e-9)    # дальше — живой сборщик
    assert math.isclose(float(r[3]), live[75][1] * 2.0, rel_tol=1e-9)    # high и low из ohlcv


@needs_pg
async def test_series_csv_contains_only_closed_bars(pool, db_name) -> None:
    await put_bars(pool, "BTC", gen_bars(1, 10))
    as_of = (START + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S+00")
    out = run_sql(db_name, "01_series.sql", as_of=as_of)
    assert len(out.splitlines()) == 1 + 5          # бары 0…4 закрыты; бар 5 закрывается в 06:00


@needs_pg
async def test_scope_block_gives_targets_positions_regime_and_the_sol_window(pool, db_name) -> None:
    await add_inst(pool, "BTC", "SOL", "ETH")
    await put_bars(pool, "BTC", gen_bars(7, 24 * 70))               # 70 суток: флаг V1 с 51-го дня
    for tok in ("SOL", "ETH"):
        await put_bars(pool, tok, gen_bars(8, 24 * 70))
    opened = [START + timedelta(days=60, hours=h) for h in (1, 2, 3)]
    for k, t in enumerate(opened):                                        # SOL: цели 1, 2, 3
        pid = await mkpos(pool, "SOL/USDT", t, pct=0.5)
        await pool.execute("UPDATE positions SET target_pct = $1 WHERE id = $2;", float(k + 1), pid)
    inside = await mkpos(pool, "SOL/USDT", datetime(2026, 10, 18, 12, 30, tzinfo=UTC), pct=0.4)
    outside = await mkpos(pool, "SOL/USDT", datetime(2026, 10, 19, 6, 30, tzinfo=UTC), pct=0.4)
    await pool.execute("UPDATE positions SET target_pct = 2 WHERE id = $1;", inside)
    await pool.execute("UPDATE positions SET target_pct = 3 WHERE id = $1;", outside)
    await mkpos(pool, "BTC/USDT", opened[0], status="open")
    await mkpos(pool, "BTC/USDT", opened[1], reason="data_gap", pct=-5.0)    # вне метрик
    await mkpos(pool, "BTC/USDT", opened[2], logic=6)                         # чужая версия
    out = run_sql(db_name, "00_scope.sql", as_of="2026-10-19 12:00:00+00")
    targets = {r[0]: float(r[1]) for r in fact_lines(out, "T")}
    assert targets == {"BTC": 1.0, "SOL": 2.0}      # SOL: 1, 2, 2, 3, 3 → медиана 2; ETH исключена
    pos = fact_lines(out, "P")
    assert len(pos) == 6 and all(len(r) == 3 for r in pos)    # 5 SOL + открытая BTC; без gap, v6
    assert sorted({r[1] for r in pos}) == ["BTC", "SOL"]
    sol = fact_lines(out, "S")
    assert [r[2] for r in sol] == ["2026-10-18 12:30:00"] and sol[0][1] == "SOL"
    assert sol[0][4] == "target" and sol[0][5] == "closed"       # id|SOL|время|цена|причина|статус
    days = fact_lines(out, "V")
    assert days and all(r[1] in ("0", "1", "") for r in days)
    assert any(r[1] in ("0", "1") for r in days) and any(r[1] == "" for r in days)
    assert "ETH" in out and "исключена" in out and "Цель T_coin" in out


# --------------------------------------------------------------------------
# §9.8. Время прогона и память: 5 монет × 40 000 баров
# --------------------------------------------------------------------------


def test_five_coins_by_40000_bars_fit_in_the_time_and_memory_budget(tmp_path, capsys) -> None:
    # CSV пишется монета за монетой: ряд целиком в памяти процесса pytest не держим (иначе
    # пик памяти всего набора тестов вырастает и ломает проверки лимита контейнера этапа 9.1.3)
    with open(tmp_path / "series.csv", "w", encoding="utf-8") as fh:
        fh.write("token,open_time_utc,open,high,low,close\n")
        for k, name in enumerate(COINS):
            part = tmp_path / f"{name}.csv"
            write_csv(part, {name: gen_bars(100 + k, 40_000, start=100.0 + k)})
            fh.write("".join(part.read_text(encoding="utf-8").splitlines(keepends=True)[1:]))
    rnd = random.Random(1)
    lines = [f"@@T|{name}|1.0" for name in COINS]
    for pid in range(300):
        ep = int((T0 + timedelta(hours=rnd.randrange(2_000, 39_000))).timestamp())
        when = datetime.fromtimestamp(ep, UTC).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"@@P|{pid}|{COINS[pid % 5]}|{when}")
    day0 = (T0 + timedelta(days=0)).date()
    lines += [f"@@V|{day0 + timedelta(days=k)}|{k % 2}" for k in range(1600)]
    facts = tmp_path / "facts.txt"
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.monotonic()
    res = subprocess.run([sys.executable, "-I", str(MR), "--series", str(tmp_path / "series.csv"),
                          "--facts", str(facts), "--append", "--details", str(tmp_path / "d.csv")],
                         capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - started
    peak_mb = max(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, before) / 1024
    assert res.returncode == 0, res.stderr
    with capsys.disabled():
        print(f"\n[стенд, синтетика 5 × 40 000 баров] время {elapsed:.0f} с, "
              f"пик памяти {peak_mb:.0f} МБ, строк вывода {len(res.stdout.splitlines())}")
    assert elapsed < 30 * 60 and peak_mb < 500
    assert len(res.stdout.splitlines()) < 220            # раздел 02–05 помещается в лимит отчёта
    for heading in ("02 —", "03 —", "04 —", "05 —"):
        assert f"===== {heading}" in res.stdout
    assert "@@F|M1.p|" in facts.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Скрипты целиком: run_9_11.sh и verify_9_11.sh
# --------------------------------------------------------------------------

N_E2E = 1904                                   # бары 0…1903 → последний открыт 2026-10-19 07:00
AS_OF_E2E = "2026-10-19 08:00:00+00"
HEADINGS = ["00 — ДАННЫЕ, ЦЕЛИ ПО МОНЕТАМ, ОХВАТ", "02 — СОБЫТИЯ И ИСХОДЫ", "03 — РАЗБИВКИ",
            "04 — ТЕСТ СДВИГОМ", "05 — КОНТРОЛЬНЫЙ СЛУЧАЙ SOL", "06 — ВЕРДИКТ"]


async def seed_e2e(pool) -> None:
    """5 монет по 1904 часа: история до бара 1700 + сборщик с перекрытием; позиции версии 7."""
    for k, tok in enumerate(COINS):
        bars = gen_bars(40 + k, N_E2E, start=100.0 + k)
        await put_bars(pool, tok, {i: b for i, b in bars.items() if i < 1700})
        await put_bars(pool, tok, {i: b for i, b in bars.items() if i >= 1690}, source="live")
        for j in range(4):
            await mkpos(pool, f"{tok}/USDT", START + timedelta(days=40 + 3 * j, hours=5 + k),
                        pct=(1.0, -2.0, 0.4, -0.5)[j])
    await mkpos(pool, "SOL/USDT", datetime(2026, 10, 18, 13, 15, tzinfo=UTC), status="open")
    await mkpos(pool, "BTC/USDT", START + timedelta(days=45), reason="data_gap", pct=-9.0)


def out_file(tmp_path: Path, kind: str, ext: str) -> Path:
    return tmp_path / "out" / f"{kind}_9_11_{datetime.now(UTC):%Y%m%d}.{ext}"


def report_11(tmp_path: Path) -> str:
    return out_file(tmp_path, "report", "txt").read_text("utf-8")


def facts_11(tmp_path: Path) -> str:
    return out_file(tmp_path, "facts", "txt").read_text("utf-8")


@needs_bash
@needs_pg
async def test_run_9_11_makes_a_complete_read_only_report(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    before = fingerprint(db_name)
    started = time.monotonic()
    res = run_script(RUN_9_11, stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E})
    elapsed = time.monotonic() - started
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_11(tmp_path)
    pos = [report.index(f"===== {h}") for h in HEADINGS]
    assert pos == sorted(pos)
    assert "с ошибками 0" in report and "ОШИБКА" not in report
    assert len(report.splitlines()) <= 300, len(report.splitlines())
    assert "@@" not in report
    assert fingerprint(db_name) == before                       # замер ничего не записал
    assert elapsed < 30 * 60
    facts = facts_11(tmp_path)
    for tag in ("@@T|BTC|1", "@@P|", "@@V|", "@@S|", "@@F|rows_before|", "@@F|M1.p|",
                "@@F|B0.x3_all|"):
        assert tag in facts, tag
    assert re.search(r"@@F\|rows_before\|(\S+)", facts).group(1) == \
        re.search(r"@@F\|rows_after\|(\S+)", facts).group(1)
    csv_lines = out_file(tmp_path, "series", "csv").read_text("utf-8").splitlines()
    assert csv_lines[0] == "token,open_time_utc,open,high,low,close"
    per_coin = {c: sum(1 for ln in csv_lines[1:] if ln.startswith(c + ",")) for c in COINS}
    assert set(per_coin.values()) == {N_E2E}                    # история и сборщик слиты без дублей
    assert "Ряд выгружен в CSV (строк по монетам)" in report
    details = out_file(tmp_path, "breakdown", "csv").read_text("utf-8").splitlines()
    assert details[0] == "group,exit,cost,dimension,key,n,mean,median,pos_pct" and len(details) > 50
    for rule in ("M1", "M2", "M3", "M4"):
        assert f"{rule}: ИТОГ —" in report
    assert "Позиции системы версии 7 по SOL" in report and "#" in report.split("Позиции системы")[1]


@needs_bash
@needs_pg
async def test_run_9_11_never_prints_the_password(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    secret = "Zq9$w\\x7!pass&word"
    stub = make_stub(tmp_path, db_name, ro_password=secret)
    res = run_script(RUN_9_11, stub, tmp_path,
                     env_extra={"STUB_LEAK": secret, "AS_OF": AS_OF_E2E})
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_11(tmp_path)
    assert secret not in res.stdout and secret not in res.stderr and secret not in report
    assert secret not in facts_11(tmp_path)
    assert "password=***" in report


@needs_bash
@needs_pg
async def test_run_9_11_failure_modes_are_reported(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    # нет соединения
    res = run_script(RUN_9_11, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode != 0 and "не удалось подключиться к БД ролью agenttrade_ro" in res.stdout
    # нет python3: блоки 02–05 и 06 не считаются, код возврата ненулевой
    res = run_script(RUN_9_11, stub, tmp_path,
                     env_extra={"AS_OF": AS_OF_E2E, "PYTHON": "python3-no-such"})
    assert res.returncode != 0 and "с ошибками 2" in res.stdout
    # сломанный SQL-блок
    sandbox = tmp_path / "analysis"
    shutil.copytree(ROOT / "analysis", sandbox)
    broken = sandbox / "sql" / "9_11" / "00_scope.sql"
    text = sql11("00_scope.sql").replace("FROM tc c", "FROM no_such_table c", 1)
    broken.write_text(text, "utf-8")
    res = run_script(sandbox / "run_9_11.sh", stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E})
    assert res.returncode != 0 and "ОШИБКА: блок 00" in res.stdout
    assert "расчёт 02–05 пропущен" in res.stdout


@needs_bash
@needs_pg
async def test_run_9_11_tells_to_apply_migration_032_when_the_role_cannot_read(
    db_name, tmp_path
) -> None:
    db = build_database("nogrant11")
    try:
        psql_raw(db, stdin=(ROOT / "db" / "migrations" / "032_ro_backtest_read_rollback.sql")
                 .read_text(encoding="utf-8"))
        res = run_script(RUN_9_11, make_stub(tmp_path, db), tmp_path)
        assert res.returncode != 0 and "нет права читать backtest.candles" in res.stdout
        assert "032_ro_backtest_read.sql" in res.stdout and "===== 02" not in res.stdout
    finally:
        drop_database(db)


def run_verify(stub: dict, tmp_path: Path):
    return subprocess.run(["bash", str(VERIFY_9_11)], env=script_env(stub, tmp_path),
                          capture_output=True, text=True, check=False)


@needs_bash
@needs_pg
async def test_verify_9_11_is_green_after_a_clean_run_and_catches_each_defect(
    pool, db_name, tmp_path
) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name, ro_password="verify-pw")
    assert run_script(RUN_9_11, stub, tmp_path, env_extra={"AS_OF": AS_OF_E2E}).returncode == 0
    res = run_verify(stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(re.findall(r"^  ✅", res.stdout, re.M)) == 6 and "ИТОГ: ВСЕ ✅" in res.stdout
    assert "❌" not in res.stdout and "⚪" not in res.stdout
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
    report.write_text(original + "лишняя строка\n" * 300, encoding="utf-8")
    assert "допустимо 300" in run_verify(stub, tmp_path).stdout
    report.write_text(original, encoding="utf-8")
    # счётчики строк разошлись
    facts = out_file(tmp_path, "facts", "txt")
    facts_text = facts.read_text("utf-8")
    facts.write_text(re.sub(r"(@@F\|rows_after\|)(\d+)", lambda m: f"{m[1]}{int(m[2]) + 1}",
                            facts_text), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "число строк разошлось" in res.stdout
    facts.write_text(facts_text, encoding="utf-8")
    # в CSV пропала монета
    csv_file = out_file(tmp_path, "series", "csv")
    csv_text = csv_file.read_text("utf-8")
    csv_file.write_text("\n".join(ln for ln in csv_text.splitlines()
                                  if not ln.startswith("DOGE,")) + "\n", encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "в ряде нет монет: DOGE" in res.stdout


@needs_bash
@needs_pg
async def test_verify_9_11_does_not_claim_all_green_without_a_run(pool, db_name, tmp_path) -> None:
    res = run_verify(make_stub(tmp_path, db_name), tmp_path)
    assert "ИТОГ: ВСЕ ✅" not in res.stdout and res.returncode == 0
    assert res.stdout.count("⚪ НЕ ПРОВЕРЕНО") == 4              # пункты 3, 4, 5, 6
    assert len(re.findall(r"^  ✅", res.stdout, re.M)) == 2     # права: 1 и 2


# --------------------------------------------------------------------------
# Рамки этапа
# --------------------------------------------------------------------------

UNTOUCHED = ["src", "backtest", "docker-compose.yml", ".env.example", "Dockerfile",
             "requirements.txt", "db", "analysis/sql/9_9", "analysis/sql/9_10",
             "analysis/run_7_1.sh", "analysis/run_9_9.sh", "analysis/run_9_10.sh",
             "analysis/lib_ro.sh", "analysis/shift_test.py", "analysis/verdict_9_10.py",
             # deploy/verify_9_10.sh правит этап 9.12 (§8, по поручению владельца)
             "deploy/lib_verify.sh", "scripts", "tests/test_stage_9_10.py"]


def test_the_stage_touches_only_its_own_files() -> None:
    res = subprocess.run(["git", "diff", "--stat", BASE_COMMIT, "--", *UNTOUCHED], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.skip("нет git или базового коммита — сверка дифференциала невозможна")
    assert res.stdout.strip() == "", res.stdout
    migrations = sorted(p.name for p in (ROOT / "db" / "migrations").glob("*.sql"))
    assert not [m for m in migrations if m[:3] > "032"]          # новых миграций нет


def test_scripts_do_not_change_anything_on_the_server() -> None:
    for path in (RUN_9_11, VERIFY_9_11):
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in path.read_text("utf-8").splitlines())
        for forbidden in ("docker compose restart", "docker compose up", "docker compose build",
                          "git pull", "pip install", "INSERT INTO", "DELETE FROM", "DROP ",
                          "ALTER ", "TRUNCATE TABLE"):
            assert forbidden not in code, (path.name, forbidden)
    body = RUN_9_11.read_text("utf-8")
    assert 'source "$LIB_DIR/lib_ro.sh"' in body and "redact" in body and "lib_verify.sh" in \
        VERIFY_9_11.read_text("utf-8")
    for path in (RUN_9_11, VERIFY_9_11):
        assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0


def test_logic_version_is_untouched() -> None:
    from tests.conftest import CURRENT_LOGIC_VERSION

    assert CURRENT_LOGIC_VERSION == 7
    for path in (MR, VERDICT, RUN_9_11):
        assert "LOGIC_VERSION" not in path.read_text("utf-8")
