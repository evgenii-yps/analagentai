"""Разовый аналитический скрипт (Этап 6.8) — Задача B: пространство состояний
Futures Agent и (а)симметрия его веток. НЕ входит в образ, не импортируется src/.

Воспроизводит логику src/agents/futures.py::analyze_futures построчно (только ветвление
сигнала, без расчёта уверенности) и показывает, какие сочетания (знак rate,
экстремум, рост OI) в какой сигнал переходят — и что даёт наблюдаемый на
продакшене режим funding (§2.5: rate всегда > 0, |rate| ≤ 0.0001, порог 0.0005).

Запуск: python analysis/futures_state_space.py
"""

from __future__ import annotations

_FUNDING_EXTREME = 0.0005
_OI_RISE_PCT = 0.1


def signal_of(rate: float, oi_change_pct: float) -> str:
    """Точная копия ветвления сигнала из analyze_futures (без confidence)."""
    oi_rising = oi_change_pct > _OI_RISE_PCT
    is_extreme = abs(rate) > _FUNDING_EXTREME
    if is_extreme:
        return "bearish" if rate > 0 else "bullish"
    if oi_rising and rate > 0:
        return "bullish"
    if oi_rising and rate < 0:
        return "bearish"
    return "neutral"


def _cell(rate_sign: str, extreme: bool, oi_rising: bool) -> str:
    rate = 0.0
    if rate_sign == "+":
        rate = 0.001 if extreme else 0.0001
    elif rate_sign == "-":
        rate = -0.001 if extreme else -0.0001
    oi = 1.0 if oi_rising else 0.0
    return signal_of(rate, oi)


def full_state_space() -> None:
    print("=== Полное пространство состояний Futures Agent ===")
    print(f"{'rate':>8} {'|rate|>порог':>12} {'OI растёт':>10} -> сигнал")
    for rate_sign in ("+", "-", "0"):
        for extreme in (False, True):
            if rate_sign == "0" and extreme:
                continue
            for oi_rising in (False, True):
                sig = _cell(rate_sign, extreme, oi_rising)
                print(
                    f"{rate_sign:>8} {str(extreme):>12} {str(oi_rising):>10} -> {sig}"
                )


def reachable_under_observed_regime() -> None:
    print("\n=== Достижимые сигналы при наблюдаемом режиме (rate>0, |rate|≤0.0001) ===")
    # На продакшене rate строго > 0 и |rate| ≤ 0.0001 < 0.0005 → is_extreme = False всегда.
    reach = set()
    for oi_rising in (False, True):
        reach.add(_cell("+", False, oi_rising))
    print(f"  достижимо: {sorted(reach)}")
    print("  bearish недостижим: обе его ветки требуют либо rate>0 И |rate|>0.0005")
    print("  (экстремум, ниже наблюдаемого впятеро), либо rate<0 (не наблюдался ни разу).")
    print("  → фактически агент бинарный: OI растёт → bullish, иначе → neutral.")


def bias_estimate() -> None:
    print("\n=== Оценка систематического сдвига (связь с Задачей D) ===")
    # §2.2: futures neutral 2764, bullish 1558, bearish 0 → доля bullish.
    bullish, neutral = 1558, 2764
    share_bull = bullish / (bullish + neutral)
    mean_dir = share_bull * (+1) + (1 - share_bull) * 0  # bearish=0 никогда
    print(f"  доля bullish = {share_bull * 100:.1f}%, средний вклад направления = {mean_dir:+.3f}")
    print(f"  при влиянии ~8.5% (Задача C): вклад в средний score ≈ {mean_dir * 0.085:+.3f}")
    print("  → лёгкий, но систематический бычий крен из-за мёртвой медвежьей ветки.")


if __name__ == "__main__":
    full_state_space()
    reachable_under_observed_regime()
    bias_estimate()
