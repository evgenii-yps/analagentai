"""Разовый аналитический скрипт (Этап 7.0, Задача A) — эффект приведения шкал.

Применяет ту же функцию нормировки, что и агенты (src.agents.base.normalize_confidence),
к наблюдаемым медианам уверенности с продакшена и показывает:
  * новые медианы по трём агентам (требование: расходятся ≤ 2×, было 9×);
  * новый вклад каждого агента в решение при равных весах (требование: <50%).

Запуск: PYTHONPATH=. POSTGRES_PASSWORD=x python analysis/confidence_normalization.py
(POSTGRES_PASSWORD нужен только для импорта конфигурации; к БД скрипт не обращается).
Точные новые медианы на живых данных снимаются запросом C1 из ANALYSIS_REPORT.md.
"""

from __future__ import annotations

from src.agents.base import normalize_confidence
from src.agents.futures import CONFIDENCE_SCALE as FUT_SCALE
from src.agents.liquidity import CONFIDENCE_SCALE as LIQ_SCALE
from src.agents.market import CONFIDENCE_SCALE as MKT_SCALE

# Наблюдаемые медианы сырой уверенности (§3.1 ТЗ 7.0 / ANALYSIS_REPORT.md).
OLD_MEDIAN = {"market": 0.372, "liquidity": 0.057, "futures": 0.040}
SCALE = {"market": MKT_SCALE, "liquidity": LIQ_SCALE, "futures": FUT_SCALE}


def influence(confs: dict[str, float]) -> dict[str, float]:
    """Доля влияния агента при равных весах = c_i / Σ c_j."""
    total = sum(confs.values())
    return {a: confs[a] / total for a in confs}


def main() -> None:
    # Медиана монотонной функции = функция от медианы, поэтому новые медианы
    # получаются прямым применением нормировки к старым медианам.
    new_median = {
        a: normalize_confidence(OLD_MEDIAN[a], SCALE[a]) for a in OLD_MEDIAN
    }

    print("=== Медианы уверенности: было → стало ===")
    for a in ("market", "liquidity", "futures"):
        print(
            f"  {a:9s}: {OLD_MEDIAN[a]:.3f} → {new_median[a]:.3f}  "
            f"(масштаб {SCALE[a]})"
        )
    old_ratio = max(OLD_MEDIAN.values()) / min(OLD_MEDIAN.values())
    new_ratio = max(new_median.values()) / min(new_median.values())
    print(f"  разброс медиан: было ×{old_ratio:.1f}, стало ×{new_ratio:.2f}")

    print("\n=== Вклад в решение при равных весах: было → стало ===")
    old_infl = influence(OLD_MEDIAN)
    new_infl = influence(new_median)
    for a in ("market", "liquidity", "futures"):
        print(
            f"  {a:9s}: {old_infl[a] * 100:4.1f}% → {new_infl[a] * 100:4.1f}%"
        )
    print(f"  максимальный вклад одного агента: стало {max(new_infl.values()) * 100:.1f}%")


if __name__ == "__main__":
    main()
