"""Разовый аналитический скрипт (Этап 6.8) — воспроизводит числовые выкладки
отчёта по агрегации Decision Agent. НЕ входит в образ и не импортируется из src/.

Запуск: python analysis/aggregation_math.py

Считает:
  * Задача C — вклад каждого агента в score при медианных уверенностях и
    чувствительность к WEIGHT_*;
  * Задача E — достижимые значения множителя вероятности при 2 и 3 свежих
    агентах и точечную массу probability=1.0 при единодушии направления.

Формула агрегации (src/decision/agent.py):
    score = Σ(direction_i · confidence_i · weight_i) / Σ(confidence_i · weight_i)
    probability = min(|score| · (0.5 + 0.5 · agreement), 1.0)
    agreement = |pos − neg| / len(fresh)
"""

from __future__ import annotations

from itertools import product

# Медианные уверенности агентов с продакшена (§2.1 ТЗ 6.8).
MED = {"market": 0.372, "liquidity": 0.057, "futures": 0.040}


def influence(confs: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    """Доля влияния каждого агента на score = c_i·w_i / Σ(c_j·w_j)."""
    prod = {a: confs[a] * weights[a] for a in confs}
    total = sum(prod.values())
    return {a: prod[a] / total for a in prod}


def task_c() -> None:
    print("=== Задача C. Вклад агентов в score (веса равны 1) ===")
    eq = {a: 1.0 for a in MED}
    infl = influence(MED, eq)
    for a in ("market", "liquidity", "futures"):
        print(f"  {a:9s}: conf={MED[a]:.3f}  влияние={infl[a] * 100:5.1f}%")
    print(f"  сумма denom Σ(c·w) = {sum(MED.values()):.3f}")

    print("\n  Чувствительность к WEIGHT_LIQUIDITY (market/futures = 1.0):")
    for wl in (1.0, 2.0, 3.0, 6.5, 10.0):
        w = {"market": 1.0, "liquidity": wl, "futures": 1.0}
        infl = influence(MED, w)
        print(
            f"    w_liq={wl:4.1f} → market {infl['market'] * 100:4.1f}%, "
            f"liquidity {infl['liquidity'] * 100:4.1f}%, futures {infl['futures'] * 100:4.1f}%"
        )
    w_eq = MED["market"] / MED["liquidity"]
    print(f"  Чтобы liquidity сравнялся с market по влиянию: w_liq ≈ {w_eq:.2f}")


def _score(dirs: list[int], confs: list[float]) -> float:
    num = sum(d * c for d, c in zip(dirs, confs, strict=True))
    den = sum(confs)
    return num / den if den > 0 else 0.0


def _agreement(dirs: list[int]) -> float:
    pos = sum(1 for d in dirs if d > 0)
    neg = sum(1 for d in dirs if d < 0)
    return abs(pos - neg) / len(dirs)


def task_e() -> None:
    print("\n=== Задача E. Достижимый множитель вероятности (0.5 + 0.5·agreement) ===")
    for n in (2, 3):
        vals = sorted({_agreement(list(d)) for d in product((-1, 0, 1), repeat=n)})
        mult = [round(0.5 + 0.5 * a, 4) for a in vals]
        print(f"  свежих агентов={n}: agreement ∈ {[round(v, 3) for v in vals]}")
        print(f"                     множитель ∈ {mult}")

    print("\n  Единодушие направления → |score|=1 независимо от весов/уверенностей:")
    for d0, name in ((1, "все bullish"), (-1, "все bearish")):
        s = _score([d0, d0, d0], [MED["market"], MED["liquidity"], MED["futures"]])
        a = _agreement([d0, d0, d0])
        p = min(abs(s) * (0.5 + 0.5 * a), 1.0)
        print(f"    {name}: score={s:+.3f}, agreement={a:.2f}, probability={p:.3f}")

    print("\n  Пример смешанного расклада (market bearish, liquidity bullish, futures neutral):")
    dirs = [-1, 1, 0]
    confs = [MED["market"], MED["liquidity"], MED["futures"]]
    s = _score(dirs, confs)
    a = _agreement(dirs)
    p = min(abs(s) * (0.5 + 0.5 * a), 1.0)
    print(f"    score={s:+.3f}, agreement={a:.2f}, probability={p:.3f}")

    print("\n  Пример 2 bullish (market+liquidity) + 1 neutral (futures):")
    dirs = [1, 1, 0]
    s = _score(dirs, confs)
    a = _agreement(dirs)
    p = min(abs(s) * (0.5 + 0.5 * a), 1.0)
    print(f"    score={s:+.3f}, agreement={a:.2f}, probability={p:.3f}")


if __name__ == "__main__":
    task_c()
    task_e()
