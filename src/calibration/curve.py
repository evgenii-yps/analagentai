"""Построение калибровочной кривой: чистые функции без обращений к БД и сети.

Кривая — это таблица «диапазон индекса согласия → фактическая доля успеха»,
построенная по НЕЗАВИСИМЫМ наблюдениям. Все функции детерминированы: одинаковый
вход даёт одинаковый выход.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Длина независимого окна (секунды). Решения выдаются раз в минуту, а горизонт
# оценки — 4 часа, поэтому соседние сигналы описывают почти один и тот же
# отрезок рынка. Независимым наблюдением считается один сигнал на каждое
# непересекающееся 4-часовое окно. 14400 нацело делит сутки, поэтому границы
# окон ровно 00/04/08/12/16/20 UTC.
WINDOW_SEC = 14400


@dataclass(frozen=True)
class Observation:
    """Одно закрытое наблюдение: когда, с каким индексом согласия, с каким исходом."""

    ts: datetime
    index: float
    success: bool


def window_start(ts: datetime) -> int:
    """Номер 4-часового окна (целая часть epoch/14400) для отметки времени."""
    return int(ts.timestamp()) // WINDOW_SEC


def to_independent(observations: Iterable[Observation]) -> list[Observation]:
    """Прореживает выборку до независимых наблюдений: одно на 4-часовое окно.

    Из окна берётся ПЕРВЫЙ по времени сигнал. Без этого шага калибровка дала бы
    ложную уверенность: 240 подряд идущих решений внутри одного окна — это одно
    наблюдение рынка, а не 240.
    """
    first_by_window: dict[int, Observation] = {}
    for obs in sorted(observations, key=lambda o: o.ts):
        key = window_start(obs.ts)
        if key not in first_by_window:
            first_by_window[key] = obs
    return [first_by_window[k] for k in sorted(first_by_window)]


def bin_index(value: float, n_bins: int) -> int:
    """Номер корзины для значения индекса в [0, 1]. Значение 1.0 → последняя корзина."""
    if n_bins <= 0:
        raise ValueError("Число корзин должно быть положительным")
    idx = int(float(value) * n_bins)
    return max(0, min(idx, n_bins - 1))


def build_bins(
    observations: Sequence[Observation],
    n_bins: int,
    prior_weight: float,
) -> tuple[list[dict[str, Any]], float]:
    """Строит корзины кривой → (bins, base_rate).

    ``base_rate`` — общая доля успеха по всей независимой выборке.
    Вероятность корзины сглажена к базовой ставке:

        p = (successes + k · base_rate) / (n + k)

    Сглаживание обязательно: без него корзина с тремя наблюдениями и нулём
    успехов дала бы 0.00, чего данные не подтверждают. Пустая корзина получает
    ровно базовую ставку.

    МОНОТОННОСТЬ НЕ НАВЯЗЫВАЕТСЯ. Изотоническая регрессия и любые методы,
    предполагающие рост вероятности с ростом индекса, здесь сознательно не
    применяются: по измерениям Этапа 7.1 фактическая зависимость убывающая, и
    принудительное «выпрямление вверх» раздавило бы кривую в константу, скрыв
    то единственное, что данные показывают уверенно.
    """
    total = len(observations)
    successes_total = sum(1 for o in observations if o.success)
    base_rate = successes_total / total if total else 0.0

    counts = [0] * n_bins
    successes = [0] * n_bins
    for obs in observations:
        i = bin_index(obs.index, n_bins)
        counts[i] += 1
        if obs.success:
            successes[i] += 1

    width = 1.0 / n_bins
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        n = counts[i]
        s = successes[i]
        p = (s + prior_weight * base_rate) / (n + prior_weight)
        bins.append(
            {
                "lo": round(i * width, 6),
                "hi": round((i + 1) * width, 6),
                "n": n,
                "successes": s,
                "p": round(p, 6),
            }
        )
    return bins, base_rate


def probability_for_index(bins: Sequence[dict[str, Any]], index: float) -> float | None:
    """Калиброванная вероятность для значения индекса согласия.

    Возвращает None, если корзин нет или значение вне [0, 1] — в этом случае
    вероятность не показывается вовсе, вместо неё не подставляется «похожее».
    """
    if not bins:
        return None
    value = float(index)
    if value < 0.0 or value > 1.0:
        return None
    i = bin_index(value, len(bins))
    p = bins[i].get("p")
    return None if p is None else float(p)


def curve_summary(bins: Sequence[dict[str, Any]]) -> str:
    """Короткая человекочитаемая сводка кривой для логов и отчётов."""
    parts = [
        f"[{b['lo']:.2f}–{b['hi']:.2f}] n={b['n']} p={b['p']:.3f}" for b in bins
    ]
    return "; ".join(parts)
