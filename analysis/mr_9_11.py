#!/usr/bin/env python3
"""Этап 9.11 — замер входа «на откате» (mean reversion) по истории часовых свечей (ТЗ 9.11).

ТОЛЬКО ЧТЕНИЕ и только расчёт: ничего не пишет в базу, ни одно решение системы не меняется.
Только стандартная библиотека (на хосте нет pandas/numpy — правило проекта D-3).

ВХОД
  --series  CSV часового ряда (analysis/sql/9_11/01_series.sql):
              token,open_time_utc,open,high,low,close
  --facts   файл фактов run_9_11.sh (строки «@@…» блока 00_scope.sql):
              @@T|монета|T_coin          цель по монете, % (ТЗ §3.2)
              @@P|id|монета|opened_at    входы системы версии 7 (ТЗ §3.3), UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС»
              @@V|день|0/1/пусто         флаг V1 из 9.10 (ТЗ §3.4)
              @@S|id|SOL|opened_at|цена|exit_reason|status   позиции SOL в окне контрольного случая
  --as-of   момент замера (только для подписи)
  --append  дописать в файл фактов итоги для verdict_9_11.py (строки «@@F|ключ|значение»)
  --details путь CSV с полными разбивками (в отчёт не идёт)

ВЫХОД — разделы 02–05 отчёта (ТЗ §10) в стандартный вывод.

Методика (правила входа §4, выхода §5, контроли §6, критерии §8) реализована буква в букву;
все числа методики — в блоке КОНСТАНТЫ ниже, с ссылкой на пункт ТЗ. Подбор порогов запрещён (§12).

ТОЛКОВАНИЯ там, где ТЗ допускает два прочтения (все перечислены в docs/STAGE_9_11_REPORT.md):
  * s6 и sd24 — стандартное отклонение по генеральной совокупности (делитель n);
    s6 = 0 — условие M2 ложно; окно из одинаковых закрытий — sd24 ровно 0 (не шум округления);
    sd24 = 0 — z не определён, условие M1 ложно;
  * Уайлдер на рядах с пропусками: рекурсия (RSI14, ATR14) перезапускается после пропуска часа,
    первое значение — через 14 шагов после него (окно «задевает пропуск» ⇒ нет значения);
  * «ложно на t−1» — это ИЗВЕСТНО ложно: если на t−1 значения нет, события нет;
  * M4: неизвестное (из-за пропуска) среди баров t−6…t−1 без найденного M1 ⇒ значения нет;
  * бар входа t+1 отсутствует ⇒ события нет; сделка, не дожившая до срока/цели к концу
    данных (X2–X4) —
    не завершена и в статистику не входит (число печатается); у X1 такая сделка — net_mtm;
  * «доходность 24 ч»: close бара e+23 (ровно 24 ч после открытия бара входа e) к цене входа;
  * MAE печатается как глубина просадки в % (положительное число).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sys
from datetime import UTC, date, datetime

# ==============================================================================================
# КОНСТАНТЫ МЕТОДИКИ — зафиксированы ДО прогона (ТЗ 9.11), менять нельзя
# ==============================================================================================
Z_WINDOW = 24  # §4 M1: окно mean24/sd24 — закрытия баров t−23…t
Z_THRESHOLD = -2.0  # §4 M1: z ≤ −2,0
R6_LAG = 6  # §4 M2: r6 = close_t / close_{t−6} − 1
R6_SD_WINDOW = 720  # §4 M2: s6 по барам t−720…t−1 (30 суток, текущий не входит)
R6_K = 2.5  # §4 M2: r6 ≤ −2,5 × s6
RSI_PERIOD = 14  # §4 M3: RSI14 по Уайлдеру
RSI_LEVEL = 25.0  # §4 M3: RSI14 ≤ 25
M4_LOOKBACK = 6  # §4 M4: M1 хотя бы на одном из баров t−6…t−1
LOCKOUT_BARS = 12  # §4: между событиями одного правила по одной монете ≥ 12 баров
ATR_PERIOD = 14  # §5 X3: ATR14 по Уайлдеру на бар сигнала t
ATR_MULT = 3.0  # §5 X3/X4: стоп = E − 3 × ATR14_t
HORIZON_X23 = 48  # §5 X2/X3: срок, часов
HORIZON_X4 = 24  # §5 X4: срок, часов
COST_MAIN = 0.22  # §5: главная ставка издержек, % за круг (RISK_COST_ROUNDTRIP_PCT)
COST_STRESS = 0.50  # §5: стресс-ставка
HALF_SPLIT = "2024-04-01"  # §7: граница половин, 00:00 UTC
ALPHA_FAMILY = 0.0125  # §8: порог p (0,05 / 4 правила)
SHIFT_MIN_DAYS = 30  # §7: сдвиги k × 24 ч, k = 30 … N_days − 30
SHIFT_STEP_HOURS = 24  # §7: шаг сдвига — сутки (сохраняет время суток)
B0_X1_LIMIT_HOURS = 365 * 24  # §6 B0: скан X1 не дольше 365 суток
B0_X1_ENTRY_HOUR = 0  # §6 B0: для X1 — входы только в 00:00 UTC
RET_HOURS = 24  # §6: доходность через 24 ч после входа
SYSTEM_MATCH_HOURS = 3  # §7: пересечение с входом системы ±3 ч
CASE_FROM = "2026-10-18 12:00:00"  # §7: контрольный случай владельца (SOL)
CASE_TO = "2026-10-19 06:00:00"
CASE_COIN = "SOL"
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
RULES = ("M1", "M2", "M3", "M4")
EXITS = ("X1", "X2", "X3", "X4")
# ==============================================================================================

HOUR = 3600
DAY = 86400
NAN = float("nan")


def ts_to_epoch(text: str) -> int:
    """«ГГГГ-ММ-ДД ЧЧ:ММ:СС» (UTC) → секунды с начала эпохи."""
    d = date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    return (
        (d.toordinal() - 719163) * DAY
        + int(text[11:13]) * HOUR
        + int(text[14:16]) * 60
        + int(text[17:19])
    )


def epoch_to_ts(ep: int) -> str:
    return datetime.fromtimestamp(ep, UTC).strftime("%Y-%m-%d %H:%M")


HALF_EPOCH = ts_to_epoch(HALF_SPLIT + " 00:00:00")


def year_of(ep: int) -> int:
    return date.fromordinal(ep // DAY + 719163).year


# ----------------------------------------------------------------------------------------------
# Данные
# ----------------------------------------------------------------------------------------------


class Coin:
    """Часовой ряд одной монеты на общей сетке; нет бара — ok[i] = False."""

    def __init__(self, name: str, t0: int, n: int) -> None:
        self.name = name
        self.t0 = t0  # время открытия бара 0 (эпоха)
        self.n = n
        self.o = [NAN] * n
        self.h = [NAN] * n
        self.l = [NAN] * n  # noqa: E741 — low
        self.c = [NAN] * n
        self.ok = [False] * n
        self.last_valid = -1
        self.first_valid = -1
        self.mean24: list[float | None] = []
        self.sd24: list[float | None] = []
        self.z: list[float | None] = []
        self.r6: list[float | None] = []
        self.s6: list[float | None] = []
        self.rsi: list[float | None] = []
        self.atr: list[float | None] = []
        self.cond: dict[str, list[bool | None]] = {}

    def time_of(self, i: int) -> int:
        return self.t0 + i * HOUR


def load_series(path: str) -> dict[str, Coin]:
    """Читает CSV ряда. Часы без бара остаются пропусками (не заполняются)."""
    rows: dict[str, list[tuple[int, float, float, float, float]]] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            rows.setdefault(rec["token"], []).append(
                (
                    ts_to_epoch(rec["open_time_utc"]),
                    float(rec["open"]),
                    float(rec["high"]),
                    float(rec["low"]),
                    float(rec["close"]),
                )
            )
    if not rows:
        return {}
    t0 = min(r[0][0] for r in rows.values())
    t1 = max(r[-1][0] for r in rows.values())
    n = (t1 - t0) // HOUR + 1
    coins = {}
    for name, recs in rows.items():
        coin = Coin(name, t0, n)
        for ep, o, h, lo, cl in recs:
            i = (ep - t0) // HOUR
            coin.o[i], coin.h[i], coin.l[i], coin.c[i] = o, h, lo, cl
            coin.ok[i] = True
        valid = [i for i in range(n) if coin.ok[i]]
        coin.first_valid, coin.last_valid = valid[0], valid[-1]
        coins[name] = coin
    return coins


# ----------------------------------------------------------------------------------------------
# Индикаторы (ТЗ §4); пропуск часа в окне ⇒ значения нет
# ----------------------------------------------------------------------------------------------


def _win24(t: int) -> tuple[int, int]:
    """Окно mean24/sd24 для бара t: индексы [lo, hi) = закрытия баров t−23…t."""
    return t - Z_WINDOW + 1, t + 1


def _win_s6(t: int) -> tuple[int, int]:
    """Окно s6 для бара t: индексы [lo, hi) значений r6 на барах t−R6_SD_WINDOW…t−1."""
    return t - R6_SD_WINDOW, t


def _prefix_bad(valid: list[bool]) -> list[int]:
    out = [0]
    for v in valid:
        out.append(out[-1] + (0 if v else 1))
    return out


def _wilder(coin: Coin) -> None:
    """RSI14 и ATR14 по Уайлдеру; рекурсия перезапускается после пропуска часа."""
    n, c, h, lo, ok = coin.n, coin.c, coin.h, coin.l, coin.ok
    coin.rsi, coin.atr = [None] * n, [None] * n
    prev = None
    g_sum = l_sum = tr_sum = 0.0
    k = 0  # число шагов (изменений) после перезапуска
    avg_g = avg_l = avg_tr = 0.0
    for i in range(n):
        if not ok[i]:
            prev = None
            continue
        if prev is None:
            prev, k, g_sum, l_sum, tr_sum = c[i], 0, 0.0, 0.0, 0.0
            continue
        d = c[i] - prev
        gain, loss = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)
        tr = max(h[i] - lo[i], abs(h[i] - prev), abs(lo[i] - prev))
        prev = c[i]
        k += 1
        if k < RSI_PERIOD:
            g_sum, l_sum, tr_sum = g_sum + gain, l_sum + loss, tr_sum + tr
            continue
        if k == RSI_PERIOD:
            avg_g, avg_l = (g_sum + gain) / RSI_PERIOD, (l_sum + loss) / RSI_PERIOD
            avg_tr = (tr_sum + tr) / ATR_PERIOD
        else:
            avg_g = (avg_g * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
            avg_l = (avg_l * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
            avg_tr = (avg_tr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        if avg_l == 0.0:
            coin.rsi[i] = 100.0 if avg_g > 0.0 else 50.0
        else:
            coin.rsi[i] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        coin.atr[i] = avg_tr


def compute_indicators(coin: Coin) -> None:
    n, c, ok = coin.n, coin.c, coin.ok
    bad = _prefix_bad(ok)
    coin.mean24, coin.sd24, coin.z = [None] * n, [None] * n, [None] * n
    for t in range(n):
        lo, hi = _win24(t)
        if lo < 0 or hi > n or bad[hi] - bad[lo]:
            continue
        w = c[lo:hi]
        if max(w) == min(w):
            m, sd = w[0], 0.0           # плоский участок: sd ровно 0 (без шума округления)
        else:
            m = sum(w) / Z_WINDOW
            sd = math.sqrt(sum((x - m) ** 2 for x in w) / Z_WINDOW)
        coin.mean24[t], coin.sd24[t] = m, sd
        if sd > 0.0:
            coin.z[t] = (c[t] - m) / sd
    coin.r6 = [None] * n
    for t in range(R6_LAG, n):
        if ok[t] and ok[t - R6_LAG]:
            coin.r6[t] = c[t] / c[t - R6_LAG] - 1.0
    r_ok = [x is not None for x in coin.r6]
    bad6 = _prefix_bad(r_ok)
    s1, s2 = [0.0], [0.0]
    for x in coin.r6:
        v = x if x is not None else 0.0
        s1.append(s1[-1] + v)
        s2.append(s2[-1] + v * v)
    coin.s6 = [None] * n
    for t in range(n):
        lo, hi = _win_s6(t)
        if lo < 0 or hi > n or bad6[hi] - bad6[lo]:
            continue
        m = (s1[hi] - s1[lo]) / R6_SD_WINDOW
        coin.s6[t] = math.sqrt(max((s2[hi] - s2[lo]) / R6_SD_WINDOW - m * m, 0.0))
    _wilder(coin)
    _conditions(coin, bad)


def _conditions(coin: Coin, bad: list[int]) -> None:
    n, o, c, lo = coin.n, coin.o, coin.c, coin.l
    c1: list[bool | None] = [None] * n
    c2: list[bool | None] = [None] * n
    c3: list[bool | None] = [None] * n
    c4: list[bool | None] = [None] * n
    for t in range(n):
        if coin.z[t] is not None:
            c1[t] = coin.z[t] <= Z_THRESHOLD
        r6, s6 = coin.r6[t], coin.s6[t]
        if r6 is not None and s6 is not None and s6 > 0.0:
            c2[t] = r6 <= -R6_K * s6
        if coin.rsi[t] is not None:
            c3[t] = coin.rsi[t] <= RSI_LEVEL
    for t in range(M4_LOOKBACK, n):
        a, b = t - M4_LOOKBACK, t + 1
        if bad[b] - bad[a]:
            continue  # пропуск в окне t−6…t — значения нет
        window = c1[a:t]
        if any(v is True for v in window):
            known = True
        elif any(v is None for v in window):
            continue  # M1 на барах окна неизвестно — значения нет
        else:
            known = False
        c4[t] = known and c[t] > o[t] and c[t] > min(lo[a:t])
    coin.cond = {"M1": c1, "M2": c2, "M3": c3, "M4": c4}


def find_events(coin: Coin, rule: str) -> list[int]:
    """Бары сигнала t: условие истинно на t и ИЗВЕСТНО ложно на t−1; ≥ 12 баров от прошлого."""
    cond = coin.cond[rule]
    events: list[int] = []
    last = -(10**9)
    for t in range(1, coin.n - 1):
        if cond[t] is True and cond[t - 1] is False:
            if t - last >= LOCKOUT_BARS:
                if coin.ok[t + 1]:  # без бара входа события нет
                    events.append(t)
                    last = t
    return events


def entry_price(coin: Coin, t: int) -> float:
    """Цена входа сигнала на баре t — открытие бара t+1 (ТЗ §4)."""
    return coin.o[t + 1]


# ----------------------------------------------------------------------------------------------
# Выходы (ТЗ §5)
# ----------------------------------------------------------------------------------------------


def simulate(
    coin: Coin,
    e: int,
    price: float,
    kind: str,
    target_pct: float,
    atr: float | None,
    x1_limit: int | None = None,
):
    """Исход сделки со входом по `price` на баре e. Возвращает
    (gross_%, часов, gap, оба_в_баре, MAE_%, статус) либо None (сделка не завершена / нет ATR).

    gross — доходность без издержек, % = (цена_выхода / E − 1) × 100; издержки — :func:`net_of`.
    Статусы: target | stop | time | mean | mtm (только X1: цели нет, оценка по последнему close).
    """
    pt = price * (1.0 + target_pct / 100.0)
    if kind in ("X3", "X4"):
        if atr is None:
            return None
        ps = price - ATR_MULT * atr
    else:
        ps = None
    last = coin.last_valid
    if kind == "X1":
        end = last if x1_limit is None else min(last, e + x1_limit - 1)
        exp = None
    else:
        exp = e + (HORIZON_X4 if kind == "X4" else HORIZON_X23) - 1
        if exp > last:
            end = last  # до срока данных не хватает
        else:
            end = exp
    h, lo, c, ok = coin.h, coin.l, coin.c, coin.ok
    gap = both = 0
    min_low = price
    last_close = price
    last_j = e
    for j in range(e, end + 1):
        if not ok[j]:
            gap = 1
            continue
        last_close, last_j = c[j], j
        if lo[j] < min_low:
            min_low = lo[j]
        xp = None
        status = ""
        if kind in ("X3", "X4"):
            hit_s = lo[j] <= ps
            hit_t = kind == "X3" and h[j] >= pt
            if hit_s and hit_t:
                both = 1
                xp = ps  # ТЗ §5: оба в одном баре → стоп (пессимистично, как EXIT_AMBIGUOUS)
                status = "stop"
            elif hit_s:
                xp, status = ps, "stop"
            elif hit_t:
                xp, status = pt, "target"
            elif kind == "X4" and coin.mean24[j] is not None and c[j] >= coin.mean24[j]:
                xp, status = c[j], "mean"
        elif h[j] >= pt:
            xp, status = pt, "target"
        if xp is not None:
            return (
                (xp / price - 1.0) * 100.0,
                j - e + 1,
                gap,
                both,
                max(0.0, (price - min_low) / price * 100.0),
                status,
            )
    if kind == "X1":
        return (
            (last_close / price - 1.0) * 100.0,
            last_j - e + 1,
            gap,
            0,
            max(0.0, (price - min_low) / price * 100.0),
            "mtm",
        )
    if exp > last:
        return None  # срок ещё не истёк на момент данных
    if not ok[exp]:
        gap = 1
    return (
        (last_close / price - 1.0) * 100.0,
        exp - e + 1,
        gap,
        both,
        max(0.0, (price - min_low) / price * 100.0),
        "time",
    )


def net_of(gross: float, cost: float) -> float:
    """Итог сделки, % = (exit/E − 1) × 100 − C (ТЗ §5)."""
    return gross - cost


# ----------------------------------------------------------------------------------------------
# Статистика
# ----------------------------------------------------------------------------------------------


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """Процентиль с линейной интерполяцией (q в 0…1)."""
    if not sorted_vals:
        return None
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class Trade:
    """Сделка: монета, бар входа, итог без издержек и признаки."""

    __slots__ = ("coin", "e", "gross", "hold", "gap", "both", "mae", "status")

    def __init__(self, coin: str, e: int, res) -> None:
        self.coin, self.e = coin, e
        self.gross, self.hold, self.gap, self.both, self.mae, self.status = res


def stats(trades: list[Trade], cost: float, entry_epoch) -> dict:
    """Показатели ТЗ §7 по набору сделок при ставке издержек `cost`."""
    nets = [net_of(t.gross, cost) for t in trades]
    n = len(nets)
    out: dict = {"n": n}
    if not n:
        return out
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    srt = sorted(nets)
    out.update(
        n_eff=len({entry_epoch(t) // DAY for t in trades}),
        pos=100.0 * len(wins) / n,
        mean=sum(nets) / n,
        median=percentile(srt, 0.5),
        payoff=(sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
        if wins and losses
        else None,
        pf=sum(wins) / abs(sum(losses)) if wins and losses else None,
        worst=srt[0],
        p5=percentile(srt, 0.05),
        hold=sum(t.hold for t in trades) / n,
        gap=100.0 * sum(t.gap for t in trades) / n,
        both=100.0 * sum(t.both for t in trades) / n,
    )
    return out


def mean_net(trades: list[Trade], cost: float) -> float | None:
    return sum(net_of(t.gross, cost) for t in trades) / len(trades) if trades else None


# ----------------------------------------------------------------------------------------------
# Расчёт: события правил, базовые линии B0 и B1
# ----------------------------------------------------------------------------------------------


class Study:
    def __init__(self, coins: dict[str, Coin], targets: dict[str, float]) -> None:
        self.coins = {k: v for k, v in coins.items() if k in targets}
        self.targets = targets
        self.excluded = sorted(set(coins) - set(self.coins))
        self.t0 = next(iter(self.coins.values())).t0 if self.coins else 0
        self.n = next(iter(self.coins.values())).n if self.coins else 0
        self.events: dict[str, dict[str, list[int]]] = {r: {} for r in RULES}
        self.trades: dict[tuple[str, str], list[Trade]] = {}  # (группа, выход) → сделки
        self.ret24: dict[str, list[float]] = {}
        self.unfinished: dict[tuple[str, str], int] = {}
        self.no_atr: dict[tuple[str, str], int] = {}
        self.b0_x3: dict[str, list[float | None]] = {}  # монета → gross X3 на каждый час
        self.event_epoch = lambda tr: self.t0 + tr.e * HOUR

    # -- сделки ----------------------------------------------------------------------------
    def _add(
        self,
        group: str,
        kind: str,
        coin: Coin,
        e: int,
        price: float,
        sig: int,
        x1_limit: int | None = None,
    ) -> None:
        atr = coin.atr[sig] if sig >= 0 else None
        res = simulate(coin, e, price, kind, self.targets[coin.name], atr, x1_limit)
        if res is None:
            key = (group, kind)
            if kind in ("X3", "X4") and atr is None:
                self.no_atr[key] = self.no_atr.get(key, 0) + 1
            else:
                self.unfinished[key] = self.unfinished.get(key, 0) + 1
            return
        self.trades.setdefault((group, kind), []).append(Trade(coin.name, e, res))

    def _ret(self, group: str, coin: Coin, e: int, price: float) -> None:
        j = e + RET_HOURS - 1
        if j <= coin.last_valid and coin.ok[j]:
            self.ret24.setdefault(group, []).append((coin.c[j] / price - 1.0) * 100.0)

    def run_rules(self) -> None:
        for coin in self.coins.values():
            compute_indicators(coin)
        for rule in RULES:
            for coin in self.coins.values():
                ts = find_events(coin, rule)
                self.events[rule][coin.name] = ts
                for t in ts:
                    e, px = t + 1, entry_price(coin, t)
                    for kind in EXITS:
                        self._add(rule, kind, coin, e, px, t)
                    self._ret(rule, coin, e, px)

    def run_b0(self) -> None:
        """B0: каждый час каждой монеты (X1 — только в 00:00 UTC, скан ≤ 365 суток)."""
        for coin in self.coins.values():
            x3: list[float | None] = [None] * coin.n
            for e in range(max(coin.first_valid, 1), coin.last_valid + 1):
                if not coin.ok[e]:
                    continue
                px = coin.o[e]
                for kind in ("X2", "X3", "X4"):
                    self._add("B0", kind, coin, e, px, e - 1)
                self._ret("B0", coin, e, px)
                if (coin.time_of(e) // HOUR) % 24 == B0_X1_ENTRY_HOUR:
                    self._add("B0", "X1", coin, e, px, e - 1, B0_X1_LIMIT_HOURS)
            self.b0_x3[coin.name] = x3
        for tr in self.trades.get(("B0", "X3"), []):
            self.b0_x3[tr.coin][tr.e] = tr.gross

    def run_b1(self, positions: list[tuple[str, int]]) -> int:
        """B1: входы системы версии 7 — открытие первого бара, начавшегося не раньше opened_at."""
        skipped = 0
        for name, opened in positions:
            coin = self.coins.get(name)
            if coin is None:
                skipped += 1
                continue
            e = -((coin.t0 - opened) // HOUR)  # потолок (opened − t0) / 1 ч
            e0 = e
            while e <= coin.last_valid and e - e0 < 24 and (e < 0 or not coin.ok[e]):
                e += 1
            if e < 1 or e > coin.last_valid or not coin.ok[e]:
                skipped += 1
                continue
            px = coin.o[e]
            for kind in EXITS:
                self._add("B1", kind, coin, e, px, e - 1)
            self._ret("B1", coin, e, px)
        return skipped


# ----------------------------------------------------------------------------------------------
# Тест значимости циклическим сдвигом (ТЗ §7)
# ----------------------------------------------------------------------------------------------


def shift_test(
    events: list[tuple[str, int]],
    own_x3: list[float],
    b0_x3: dict[str, list[float | None]],
    n_total: int,
    cost: float = COST_MAIN,
) -> dict:
    """Наблюдаемое среднее net X3 событий и p сдвига набора событий на k × 24 ч по барам B0.

    events — (монета, бар входа); own_x3 — gross X3 самих событий (наблюдаемое среднее);
    b0_x3[монета][e] — gross X3 при входе в бар e. Без случайных чисел.
    """
    if not own_x3:
        return {"n": 0, "obs": None, "shifts": 0, "p": None}
    obs = sum(net_of(g, cost) for g in own_x3) / len(own_x3)
    n_days = n_total // 24
    shifts = hits = 0
    for k in range(SHIFT_MIN_DAYS, n_days - SHIFT_MIN_DAYS + 1):
        s = SHIFT_STEP_HOURS * k
        total, cnt = 0.0, 0
        for name, e in events:
            g = b0_x3[name][(e + s) % n_total]
            if g is not None:
                total += net_of(g, cost)
                cnt += 1
        if not cnt:
            continue
        shifts += 1
        if total / cnt >= obs:
            hits += 1
    return {
        "n": len(own_x3),
        "obs": obs,
        "shifts": shifts,
        "p": (hits / shifts) if shifts else None,
    }


# ----------------------------------------------------------------------------------------------
# Факты и печать
# ----------------------------------------------------------------------------------------------


def read_facts(path: str) -> dict:
    targets: dict[str, float] = {}
    positions: list[tuple[str, int, int]] = []
    regime: dict[int, bool] = {}
    sol: list[list[str]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if parts[0] == "@@T" and len(parts) == 3:
                targets[parts[1]] = float(parts[2])
            elif parts[0] == "@@P" and len(parts) == 4:
                positions.append((parts[2], ts_to_epoch(parts[3]), int(parts[1])))
            elif parts[0] == "@@V" and len(parts) == 3 and parts[2] != "":
                regime[(ts_to_epoch(parts[1] + " 00:00:00")) // DAY] = parts[2] == "1"
            elif parts[0] == "@@S" and len(parts) == 7:
                sol.append(parts[1:])
    return {"targets": targets, "positions": positions, "regime": regime, "sol": sol}


def f2(x: float | None, digits: int = 2, sign: bool = False) -> str:
    if x is None:
        return "-"
    return format(x, f"{'+' if sign else ''}.{digits}f")


def table(out: list[str], header: list[str], rows: list[list[str]]) -> None:
    """Таблица с выравниванием колонок по ширине; короткие строки добиваются прочерками."""
    rows = [r + ["-"] * (len(header) - len(r)) for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    out.append(" " + " | ".join(h.rjust(w) for h, w in zip(header, widths, strict=True)))
    for r in rows:
        out.append(" " + " | ".join(c.rjust(w) for c, w in zip(r, widths, strict=True)))


GROUPS = (*RULES, "B0", "B1")


def _line(s: dict) -> list[str]:
    if not s["n"]:
        return ["0"] + ["-"] * 11
    return [
        str(s["n"]), str(s["n_eff"]), f2(s["pos"], 1), f2(s["mean"], 3, True),
        f2(s["median"], 3, True), f2(s["payoff"]), f2(s["pf"]), f2(s["worst"]), f2(s["p5"]),
        f2(s["hold"], 1), f2(s["gap"], 1), f2(s["both"], 1),
    ]


def section_outcomes(st: Study, out: list[str], facts: dict[str, str]) -> None:
    out.append("===== 02 — СОБЫТИЯ И ИСХОДЫ M1–M4, B0, B1 (X1–X4) =====")
    out.append("N — сделок; n_eff — суток UTC входа; +% — доля net>0; payoff — средний плюс /")
    out.append("|средний минус|; ФП — фактор прибыли; Часов — время в сделке; gap, оба — % сделок.")
    out.append(f"-- Итог net, % при издержках C = {COST_MAIN} (главная ставка) --")
    head = ["Гр.", "Вых.", "N", "n_eff", "+%", "Ср.", "Мед.", "payoff", "ФП", "Худш.", "p5",
            "Часов", "gap%", "оба%"]
    rows = []
    for g in GROUPS:
        for kind in EXITS:
            s = stats(st.trades.get((g, kind), []), COST_MAIN, st.event_epoch)
            rows.append([g, kind, *_line(s)])
    table(out, head, rows)
    out.append(f"-- То же при стрессе C = {COST_STRESS}: ср. net, % / доля плюсовых, % / ФП --")
    rows = []
    for g in GROUPS:
        cells = [g]
        for kind in EXITS:
            s = stats(st.trades.get((g, kind), []), COST_STRESS, st.event_epoch)
            if s["n"]:
                cells.append(f"{f2(s['mean'], 3, True)} / {f2(s['pos'], 1)} / {f2(s['pf'])}")
            else:
                cells.append("-")
        rows.append(cells)
    table(out, ["Гр.", *EXITS], rows)
    b0 = mean_net(st.trades.get(("B0", "X3"), []), COST_MAIN)
    out.append(f"-- Разница «правило − B0» по среднему net X3, C = {COST_MAIN} --")
    rows = []
    for r in RULES:
        m = mean_net(st.trades.get((r, "X3"), []), COST_MAIN)
        diff = None if m is None or b0 is None else m - b0
        rows.append([r, f2(m, 3, True), f2(b0, 3, True), f2(diff, 3, True)])
        if m is not None:
            facts[f"{r}.x3_all"] = repr(m)
            facts[f"{r}.x3_n"] = str(len(st.trades[(r, "X3")]))
    table(out, ["Прав.", "ср. net X3", "B0 X3", "разница"], rows)
    if b0 is not None:
        facts["B0.x3_all"] = repr(b0)
    _section_x1(st, out, facts)
    out.append("-- Доходность через 24 ч после входа, без издержек (close бара e+23 / E − 1), % --")
    rows = []
    for g in GROUPS:
        r24 = sorted(st.ret24.get(g, []))
        if r24:
            pos = 100.0 * sum(1 for x in r24 if x > 0) / len(r24)
            rows.append([g, str(len(r24)), f2(sum(r24) / len(r24), 3, True),
                         f2(percentile(r24, 0.5), 3, True), f2(pos, 1)])
        else:
            rows.append([g, "0"])
    table(out, ["Гр.", "N", "Ср.", "Мед.", ">0, %"], rows)
    unfinished = [f"{k[0]}/{k[1]}: {v}" for k, v in sorted(st.unfinished.items())]
    out.append("Не завершены к концу данных, в статистику не входят: "
               + (", ".join(unfinished) if unfinished else "нет"))
    no_atr = [f"{k[0]}/{k[1]}: {v}" for k, v in sorted(st.no_atr.items())]
    out.append("Без ATR14 на баре сигнала (X3/X4 не считаются): "
               + (", ".join(no_atr) if no_atr else "нет"))
    if st.excluded:
        out.append("Монеты без позиций версии 7 исключены: " + ", ".join(st.excluded))


def _section_x1(st: Study, out: list[str], facts: dict[str, str]) -> None:
    out.append("-- X1 (только цель, без стопа и срока): доля дошедших до цели; время до цели; "
               "MAE = глубина просадки, % --")
    head = ["Гр.", "N", "24 ч,%", "7 сут,%", "30 сут,%", "вообще,%", "мед. до цели, ч",
            "MAE мед.", "p90", "p99", "макс", "не дошли", "ср. net_mtm", "ср. дней висят"]
    rows = []
    for g in GROUPS:
        trs = st.trades.get((g, "X1"), [])
        if not trs:
            rows.append([g, "0"])
            continue
        n = len(trs)
        hit = [t for t in trs if t.status == "target"]
        mae = sorted(t.mae for t in trs)
        pend = [t for t in trs if t.status == "mtm"]
        times = sorted(float(t.hold) for t in hit)

        def share(hours: int, hit=hit, n=n) -> str:
            return f2(100.0 * sum(1 for t in hit if t.hold <= hours) / n, 1)

        mtm = sum(net_of(t.gross, COST_MAIN) for t in pend) / len(pend) if pend else None
        days = sum(t.hold for t in pend) / len(pend) / 24.0 if pend else None
        rows.append([
            g, str(n), share(24), share(7 * 24), share(30 * 24), f2(100.0 * len(hit) / n, 1),
            f2(percentile(times, 0.5), 1), f2(percentile(mae, 0.5)), f2(percentile(mae, 0.9), 1),
            f2(percentile(mae, 0.99), 1), f2(mae[-1], 1), str(len(pend)), f2(mtm, 2, True),
            f2(days, 1),
        ])
        if g in RULES:
            facts[f"{g}.x1_mean"] = repr(sum(net_of(t.gross, COST_MAIN) for t in trs) / n)
            facts[f"{g}.x1_reach"] = repr(100.0 * len(hit) / n)
    table(out, head, rows)


def breakdown_keys(st: Study, regime: dict[int, bool]) -> dict:
    """Ключи разбивок сделки: монета, половина, год, режим V1 дня входа."""

    def reg(t: Trade) -> str | None:
        v = regime.get(st.event_epoch(t) // DAY)
        return None if v is None else ("V1=TRUE" if v else "V1=FALSE")

    return {
        "coin": lambda t: t.coin,
        "half": lambda t: "до" if st.event_epoch(t) < HALF_EPOCH else "после",
        "year": lambda t: str(year_of(st.event_epoch(t))),
        "regime": reg,
    }


FACT_PREFIX = {"coin": "c_", "half": "", "year": "y_", "regime": "r_"}
FACT_TAG = {"до": "h1", "после": "h2"}


def section_breakdowns(st: Study, regime: dict[int, bool], out: list[str],
                       facts: dict[str, str], details: list[list]) -> None:
    out.append("===== 03 — РАЗБИВКИ: монеты, половины, годы, режим V1 =====")
    out.append(f"Выход X3, C = {COST_MAIN}: ячейка = ср. net, % (N сделок). Все выходы и ставки —")
    out.append("в файле деталей breakdown_9_11_ГГГГММДД.csv.")
    titles = {
        "coin": "По монетам",
        "half": f"По половинам (граница {HALF_SPLIT} 00:00 UTC)",
        "year": "По годам",
        "regime": "По режиму V1 дня входа (BTC ниже 50-дневной средней = TRUE)",
    }
    for dim, fn in breakdown_keys(st, regime).items():
        cols: list[str] = []
        cells: dict[tuple[str, str], tuple[float | None, int]] = {}
        for g in GROUPS:
            for kind in EXITS:
                buckets: dict[str, list[Trade]] = {}
                for t in st.trades.get((g, kind), []):
                    k = fn(t)
                    if k is not None:
                        buckets.setdefault(k, []).append(t)
                for k, trs in sorted(buckets.items()):
                    for cost in (COST_MAIN, COST_STRESS):
                        s = stats(trs, cost, st.event_epoch)
                        details.append([g, kind, cost, dim, k, s["n"], s["mean"], s["median"],
                                        s["pos"]])
                    if kind == "X3":
                        cells[(g, k)] = (mean_net(trs, COST_MAIN), len(trs))
                        if k not in cols:
                            cols.append(k)
        cols = ["до", "после"] if dim == "half" else sorted(cols)
        if dim == "coin":
            cols = [c for c in COINS if c in cols]
        out.append(f"-- {titles[dim]} --")
        rows = []
        for g in GROUPS:
            row = [g]
            for c in cols:
                m, n = cells.get((g, c), (None, 0))
                row.append("-" if m is None else f"{m:+.3f} ({n})")
                tag = FACT_TAG.get(c, c) if dim == "half" else c
                facts[f"{g}.x3_{FACT_PREFIX[dim]}{tag}"] = "" if m is None else repr(m)
            rows.append(row)
        table(out, ["Гр.", *cols], rows)
    for g in (*RULES, "B0"):
        trs = st.trades.get((g, "X3"), [])
        stress = mean_net(trs, COST_STRESS)
        facts[f"{g}.x3_all_c50"] = "" if stress is None else repr(stress)
        for name, first in (("h1", True), ("h2", False)):
            part = [t for t in trs if (st.event_epoch(t) < HALF_EPOCH) == first]
            m = mean_net(part, COST_STRESS)
            facts[f"{g}.x3_{name}_c50"] = "" if m is None else repr(m)


def section_shift(st: Study, out: list[str], facts: dict[str, str]) -> None:
    out.append("===== 04 — ТЕСТ СДВИГОМ =====")
    out.append(f"Правила M1–M4, выход X3, издержки C = {COST_MAIN}.")
    out.append(f"Набор событий сдвигается по кругу на k × {SHIFT_STEP_HOURS} ч, "
               f"k = {SHIFT_MIN_DAYS} … N_дней − {SHIFT_MIN_DAYS}; статистика — ср. net X3")
    out.append("по барам B0 в сдвинутых точках; p = доля сдвигов, где среднее ≥ наблюдаемого.")
    out.append(f"Порог p ≤ {ALPHA_FAMILY} (0,05 / 4 правила). Случайных чисел нет.")
    rows = []
    for r in RULES:
        evs = [(tr.coin, tr.e) for tr in st.trades.get((r, "X3"), [])]
        own = [tr.gross for tr in st.trades.get((r, "X3"), [])]
        res = shift_test(evs, own, st.b0_x3, st.n)
        passed = res["p"] is not None and res["p"] <= ALPHA_FAMILY
        rows.append([r, str(res["n"]), f2(res["obs"], 3, True), str(res["shifts"]),
                     f2(res["p"], 4), "да" if passed else "нет"])
        facts[f"{r}.p"] = "" if res["p"] is None else repr(res["p"])
        facts[f"{r}.shifts"] = str(res["shifts"])
    head = ["Прав.", "Событий X3", "Набл. ср. net", "Сдвигов", "p", f"p ≤ {ALPHA_FAMILY}"]
    table(out, head, rows)


def _case_events(st: Study, out: list[str], coin: Coin, a: int, b: int) -> bool:
    found = []
    for r in RULES:
        for t in st.events[r].get(CASE_COIN, []):
            ep = coin.time_of(t + 1)
            if a <= ep <= b:
                found.append((ep, r, t))
    found.sort()
    for ep, r, t in found[:25]:
        px = entry_price(coin, t)
        cells = []
        for kind in EXITS:
            res = simulate(coin, t + 1, px, kind, st.targets[CASE_COIN], coin.atr[t])
            if res is None:
                cells.append(f"{kind}: не завершён")
            elif res[5] == "mtm":
                cells.append(f"{kind}: не завершён (mtm {net_of(res[0], COST_MAIN):+.2f}%)")
            else:
                cells.append(f"{kind}: {net_of(res[0], COST_MAIN):+.2f}% ({res[5]}, {res[1]} ч)")
        out.append(f" {r} {epoch_to_ts(ep)} E={px:.4g}  " + "; ".join(cells))
    return bool(found)


def section_case(st: Study, data: dict, out: list[str]) -> None:
    out.append("===== 05 — КОНТРОЛЬНЫЙ СЛУЧАЙ SOL 18–19.10.2026 И ПЕРЕСЕЧЕНИЕ С СИСТЕМОЙ =====")
    a, b = ts_to_epoch(CASE_FROM), ts_to_epoch(CASE_TO)
    coin = st.coins.get(CASE_COIN)
    out.append(f"-- События правил по {CASE_COIN}: вход (открытие бара t+1) "
               f"с {CASE_FROM[:16]} до {CASE_TO[:16]} UTC; исход при C = {COST_MAIN} --")
    if coin is None:
        out.append(f"{CASE_COIN} исключена из расчёта (нет позиций версии 7).")
    elif not _case_events(st, out, coin, a, b):
        out.append("Ни одно из правил M1–M4 по SOL в этом окне не сработало.")
        zs = [coin.z[t] for t in range(coin.n)
              if a <= coin.time_of(t) <= b and coin.z[t] is not None]
        ratios = [coin.r6[t] / coin.s6[t] for t in range(coin.n)
                  if a <= coin.time_of(t) <= b and coin.r6[t] is not None and coin.s6[t]]
        rsis = [coin.rsi[t] for t in range(coin.n)
                if a <= coin.time_of(t) <= b and coin.rsi[t] is not None]
        out.append(f"Минимумы за окно (бары сигнала): z = {f2(min(zs) if zs else None)} "
                   f"(порог {Z_THRESHOLD}), r6/s6 = {f2(min(ratios) if ratios else None)} "
                   f"(порог {-R6_K}), RSI14 = {f2(min(rsis) if rsis else None, 1)} "
                   f"(порог {RSI_LEVEL}).")
    out.append(f"-- Позиции системы версии 7 по {CASE_COIN} в том же окне --")
    for pid, _coin, at, px, reason, status in data["sol"][:25]:
        out.append(f" #{pid} {at[:16]} E={px} {status} exit_reason={reason or '-'}")
    if not data["sol"]:
        out.append("позиций нет.")
    out.append("-- Пересечение с входами системы: события правил в периоде версии 7 и из них "
               f"в пределах ±{SYSTEM_MATCH_HOURS} ч от входа системы по той же монете --")
    by_coin: dict[str, list[int]] = {}
    for name, ep, _id in data["positions"]:
        by_coin.setdefault(name, []).append(ep)
    for v in by_coin.values():
        v.sort()
    first = min((ep for _n, ep, _i in data["positions"]), default=None)
    rows = []
    for r in RULES:
        total = match = 0
        for name, ts in st.events[r].items():
            cn, eps = st.coins[name], by_coin.get(name, [])
            for t in ts:
                ep = cn.time_of(t + 1)
                if first is None or ep < first:
                    continue
                total += 1
                k = bisect.bisect_left(eps, ep - SYSTEM_MATCH_HOURS * HOUR)
                if k < len(eps) and eps[k] <= ep + SYSTEM_MATCH_HOURS * HOUR:
                    match += 1
        rows.append([r, str(total), str(match), f2(100.0 * match / total if total else 0.0, 1)])
    table(out, ["Прав.", "Событий в периоде", "Совпало с системой", "Доля, %"], rows)


def run(
    series: str, facts_path: str, as_of: str, append: bool, details_path: str | None
) -> list[str]:
    data = read_facts(facts_path)
    coins = load_series(series)
    st = Study(coins, data["targets"])
    out: list[str] = []
    if not st.coins:
        out.append("===== 02 — НЕТ ДАННЫХ ===== нет ряда или целей по монетам: расчёт невозможен")
        return out
    st.run_rules()
    st.run_b0()
    skipped = st.run_b1([(n, ep) for n, ep, _i in data["positions"]])
    facts: dict[str, str] = {}
    section_outcomes(st, out, facts)
    n_pos = len(data["positions"])
    out.append(f"B1: входов системы v7 в расчёте {n_pos - skipped} из {n_pos} (нет бара входа "
               f"или монеты: {skipped}).")
    out.append("Расхождение с 9.9 ожидаемо: вход по открытию часа, а не минуты.")
    details: list[list] = []
    section_breakdowns(st, data["regime"], out, facts, details)
    section_shift(st, out, facts)
    section_case(st, data, out)
    if details_path:
        with open(details_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                ["group", "exit", "cost", "dimension", "key", "n", "mean", "median", "pos_pct"]
            )
            w.writerows(details)
    if append:
        with open(facts_path, "a", encoding="utf-8") as fh:
            for k, v in sorted(facts.items()):
                fh.write(f"@@F|{k}|{v}\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Этап 9.11: вход на откате по истории (ТЗ 9.11)")
    ap.add_argument("--series", required=True)
    ap.add_argument("--facts", required=True)
    ap.add_argument("--as-of", default="")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--details", default=None)
    args = ap.parse_args(argv)
    for line in run(args.series, args.facts, args.as_of, args.append, args.details):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
