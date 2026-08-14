#!/usr/bin/env python3
"""Перекалибровка CONFIDENCE_SCALE по факту (Этап 7.2, Задача B2). ТОЛЬКО ЧТЕНИЕ.

Скрипт НИЧЕГО НЕ МЕНЯЕТ: он читает ``agent_outputs`` и печатает предложение по
новым константам ``CONFIDENCE_SCALE`` для liquidity/futures (у market масштаб
структурно = 1.0, тождество). Решение о правке ``.env`` принимает заказчик —
скрипт лишь даёт цифры.

Что считает по каждому агенту за период (по умолчанию с выката Этапа 7.0,
13.08 15:19 UTC — граница logic_version=2, где появилось поле confidence_raw):
  * долю confidence >= 1.0 (насыщение «в потолок»);
  * перцентили сырой уверенности p50/p90/p95/p99;
  * число уникальных значений сырой уверенности;
  * длину серий повторов (макс/медиана подряд идущих одинаковых значений);
  * ПРЕДЛОЖЕНИЕ нового масштаба = p99 сырого значения и прогноз доли насыщения и
    медиан после нормировки на него.

Целевое состояние (ориентир из ТЗ): доля насыщения < 5% у каждого агента,
медианы нормированной уверенности трёх агентов различаются не более чем вдвое.

Запуск на СЕРВЕРЕ (нужен доступ к контейнеру postgres; только стандартная
библиотека Python + docker compose exec -T):

    cd /opt/agent-trade && python3 analysis/recalibrate_scales.py

Необязательные переменные окружения:
    APP_DIR           каталог со стеком (по умолчанию /opt/agent-trade)
    RECALIBRATE_SINCE нижняя граница периода UTC (по умолчанию 2026-08-13 15:19:00+00)
    POSTGRES_USER / POSTGRES_DB как в .env (читаются автоматически из .env).
"""

from __future__ import annotations

import os
import subprocess
import sys

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Граница периода = момент выката Этапа 7.0 (logic_version=2): раньше поля
# confidence_raw в metrics не было, смешивать режимы нельзя.
DEFAULT_SINCE = "2026-08-13 15:19:00+00"

# Целевые ориентиры (ТЗ §4 B2).
TARGET_CLIP_PCT = 5.0     # доля насыщения на 1.0 должна быть ниже, %
TARGET_MEDIAN_RATIO = 2.0  # медианы нормированной уверенности ≤ 2× между агентами

# Текущие константы масштаба (для сравнения «было/предложено»).
CURRENT_SCALE = {"market": 1.0, "liquidity": 0.15, "futures": 0.10}


def _env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.path.join(APP_DIR, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


ENV = _env_file()
PG_USER = os.environ.get("POSTGRES_USER", ENV.get("POSTGRES_USER", "agenttrade"))
PG_DB = os.environ.get("POSTGRES_DB", ENV.get("POSTGRES_DB", "agenttrade"))
SINCE = os.environ.get("RECALIBRATE_SINCE", DEFAULT_SINCE)


def _psql(sql: str) -> str | None:
    """Выполняет SELECT в контейнере postgres. None — если команда не удалась.

    Только чтение: команда — SELECT, флаг -T для docker compose exec (ТЗ §2.4).
    """
    try:
        r = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", PG_USER, "-d", PG_DB, "-tA", "-F", "|", "-c", sql,
            ],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка запуска psql: {exc}", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"psql вернул ошибку: {r.stderr.strip()}", file=sys.stderr)
        return None
    return r.stdout.strip()


def _fetch_aggregates() -> dict[str, dict[str, float]]:
    """Перцентили, доля насыщения и число уникальных значений по каждому агенту."""
    sql = (
        "SELECT agent, count(*) n, "
        "round(100.0*avg((confidence>=1.0)::int),2) pct_clipped, "
        "count(DISTINCT (metrics->>'confidence_raw')) uniq_raw, "
        "round((percentile_cont(0.50) WITHIN GROUP "
        "(ORDER BY (metrics->>'confidence_raw')::float))::numeric,6) p50, "
        "round((percentile_cont(0.90) WITHIN GROUP "
        "(ORDER BY (metrics->>'confidence_raw')::float))::numeric,6) p90, "
        "round((percentile_cont(0.95) WITHIN GROUP "
        "(ORDER BY (metrics->>'confidence_raw')::float))::numeric,6) p95, "
        "round((percentile_cont(0.99) WITHIN GROUP "
        "(ORDER BY (metrics->>'confidence_raw')::float))::numeric,6) p99 "
        "FROM agent_outputs "
        "WHERE metrics ? 'confidence_raw' AND signal IN ('bullish','bearish','neutral') "
        f"AND ts >= '{SINCE}'::timestamptz "
        "GROUP BY agent ORDER BY agent;"
    )
    out = _psql(sql)
    result: dict[str, dict[str, float]] = {}
    if not out:
        return result
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) < 8:
            continue
        agent = parts[0]
        try:
            result[agent] = {
                "n": float(parts[1]),
                "pct_clipped": float(parts[2]),
                "uniq_raw": float(parts[3]),
                "p50": float(parts[4]),
                "p90": float(parts[5]),
                "p95": float(parts[6]),
                "p99": float(parts[7]),
            }
        except ValueError:
            continue
    return result


def _fetch_run_lengths() -> dict[str, dict[str, float]]:
    """Макс/медиана длины серий повторов сырой уверенности (по возрастанию ts)."""
    sql = (
        "SELECT agent, (metrics->>'confidence_raw')::float raw "
        "FROM agent_outputs "
        "WHERE metrics ? 'confidence_raw' AND signal IN ('bullish','bearish','neutral') "
        f"AND ts >= '{SINCE}'::timestamptz "
        "ORDER BY agent, ts;"
    )
    out = _psql(sql)
    seqs: dict[str, list[float]] = {}
    if not out:
        return {}
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) < 2:
            continue
        try:
            seqs.setdefault(parts[0], []).append(float(parts[1]))
        except ValueError:
            continue

    result: dict[str, dict[str, float]] = {}
    for agent, seq in seqs.items():
        runs: list[int] = []
        if seq:
            cur = 1
            for prev, val in zip(seq, seq[1:], strict=False):
                if val == prev:
                    cur += 1
                else:
                    runs.append(cur)
                    cur = 1
            runs.append(cur)
        runs.sort()
        max_run = float(runs[-1]) if runs else 0.0
        median_run = float(runs[len(runs) // 2]) if runs else 0.0
        result[agent] = {"max_run": max_run, "median_run": median_run}
    return result


def main() -> int:
    print("=== Перекалибровка CONFIDENCE_SCALE (Этап 7.2, Задача B2) — ТОЛЬКО ЧТЕНИЕ ===")
    print(f"Период: ts >= {SINCE} (граница logic_version=2). БД: {PG_DB}, роль: {PG_USER}.\n")

    agg = _fetch_aggregates()
    if not agg:
        print("Нет данных с полем confidence_raw за период (или psql недоступен).")
        print("Проверьте, что стек запущен и период RECALIBRATE_SINCE выбран верно.")
        return 0
    runs = _fetch_run_lengths()

    proposed: dict[str, float] = {}
    proj_median_norm: dict[str, float] = {}
    for agent in ("market", "liquidity", "futures"):
        a = agg.get(agent)
        if not a:
            continue
        r = runs.get(agent, {"max_run": 0.0, "median_run": 0.0})
        # Предложение: масштаб = p99 сырого значения (устойчивее max к выбросам).
        scale = a["p99"] if a["p99"] > 0 else CURRENT_SCALE.get(agent, 1.0)
        proposed[agent] = scale
        # Прогноз медианы нормированной уверенности при новом масштабе.
        proj_median_norm[agent] = min(a["p50"] / scale, 1.0) if scale > 0 else 0.0

        print(f"[{agent}]  n={int(a['n'])}")
        print(f"    доля confidence>=1.0 (насыщение): {a['pct_clipped']:.2f}%  "
              f"(цель < {TARGET_CLIP_PCT:.0f}%)")
        print(f"    перцентили сырой уверенности: "
              f"p50={a['p50']:.6f}  p90={a['p90']:.6f}  p95={a['p95']:.6f}  p99={a['p99']:.6f}")
        print(f"    уникальных значений сырой уверенности: {int(a['uniq_raw'])}")
        print(f"    серии повторов подряд: макс={int(r['max_run'])}, "
              f"медиана={int(r['median_run'])}")
        cur = CURRENT_SCALE.get(agent, 1.0)
        note = " (market — тождество, масштаб оставить 1.0)" if agent == "market" else ""
        print(f"    масштаб: сейчас {cur} → предложено {scale:.6f}{note}")
        print(f"    прогноз медианы нормированной уверенности при новом масштабе: "
              f"{proj_median_norm[agent]:.3f}\n")

    # Проверка целевых ориентиров.
    print("=== Проверка целевых ориентиров (ориентир, решение — за заказчиком) ===")
    clipped_bad = [ag for ag, a in agg.items() if a["pct_clipped"] >= TARGET_CLIP_PCT]
    if clipped_bad:
        print(f"  ⚠ Насыщение ≥ {TARGET_CLIP_PCT:.0f}% у: {', '.join(sorted(clipped_bad))} "
              "— масштаб этих агентов занижен (упираются в потолок).")
    else:
        print(f"  ✓ Насыщение ниже {TARGET_CLIP_PCT:.0f}% у всех агентов.")

    if proj_median_norm:
        vals = [v for v in proj_median_norm.values() if v > 0]
        if len(vals) >= 2:
            ratio = max(vals) / min(vals)
            ok = "✓" if ratio <= TARGET_MEDIAN_RATIO else "⚠"
            print(f"  {ok} Разброс прогнозных медиан нормированной уверенности: "
                  f"×{ratio:.2f} (цель ≤ ×{TARGET_MEDIAN_RATIO:.0f}).")

    print("\nПредлагаемые масштабы (ПРИМЕНЯТЬ ТОЛЬКО ПОСЛЕ СОГЛАСОВАНИЯ):")
    if "liquidity" in proposed:
        print(f"  # liquidity CONFIDENCE_SCALE: "
              f"{CURRENT_SCALE['liquidity']} → {proposed['liquidity']:.4f}")
    if "futures" in proposed:
        print(f"  # futures CONFIDENCE_SCALE:   "
              f"{CURRENT_SCALE['futures']} → {proposed['futures']:.4f}")
    print("  (масштабы заданы константами в src/agents/*.py; при согласовании выносятся"
          " в .env отдельным шагом — скрипт их НЕ меняет.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
