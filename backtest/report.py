"""Сборка текстового отчёта по прогону (§10 ТЗ).

Нумерация расчётов повторяет отчёт Этапа 7.1, чтобы цифры сравнивались
напрямую. Каждое число сопровождается размером выборки; доли — доверительным
интервалом Уилсона. Вердикт §10.9 формулируется механически: «выполнено /
не выполнено» по каждому из четырёх условий предрегистрированного критерия,
без интерпретации.

Статистика считается здесь же на стандартной библиотеке: новых внешних
зависимостей этап не вводит (§12.1 ТЗ), а нужны ровно две вещи — точный
двусторонний биномиальный тест и интервал Уилсона.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backtest import db
from backtest.config import BacktestConfig

WIDTH = 78


# --- Статистика ------------------------------------------------------------

def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Доверительный интервал Уилсона для доли (по умолчанию 95%).

    Выбран вместо нормального приближения намеренно: на малых выборках и долях
    у краёв диапазона нормальный интервал даёт границы вне [0, 1].
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (
        max(0.0, (centre - spread) / denominator),
        min(1.0, (centre + spread) / denominator),
    )


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        + k * math.log(p) + (n - k) * math.log(1 - p)
    )


def binomial_test_two_sided(successes: int, n: int, p0: float) -> float:
    """Точный двусторонний биномиальный тест (метод суммирования вероятностей).

    Возвращает p-значение: сумма вероятностей всех исходов, не более вероятных,
    чем наблюдённый. Реализован через логарифмы гамма-функции, поэтому работает
    и на выборках в тысячи наблюдений без переполнения.
    """
    if n <= 0:
        return 1.0
    p0 = min(max(p0, 0.0), 1.0)
    observed = _log_binom_pmf(successes, n, p0)
    if observed == -math.inf:
        return 0.0
    # Относительный допуск гасит ошибку округления при сравнении вероятностей.
    threshold = observed + 1e-7
    total = 0.0
    for k in range(n + 1):
        value = _log_binom_pmf(k, n, p0)
        if value <= threshold:
            total += math.exp(value)
    return min(1.0, total)


def pct(value: float) -> str:
    return f"{100.0 * value:.2f}%"


# --- Вспомогательные выборки ----------------------------------------------

async def _run_row(run_id: int) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT run_id, started_at, finished_at, code_commit, agents_used, "
        "config_json::text AS config_json, period_from, period_to, status "
        "FROM backtest.runs WHERE run_id = $1;",
        run_id,
    )
    if row is None:
        raise ValueError(f"прогон {run_id} не найден")
    data = dict(row)
    data["config"] = json.loads(data["config_json"])
    return data


async def _baselines(run_id: int, horizon: int, oos_only: bool) -> dict[str, Any]:
    """Три базовые линии на тех же наблюдениях: всегда buy, всегда sell, случай.

    «Всегда buy» и «всегда sell» считаются по фактическому движению цены на тех
    же моментах и горизонтах, а не по решениям системы: иначе базовая линия
    зависела бы от того, когда система решила промолчать.
    """
    row = await db.fetchrow(
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE price_end > price_at_ts) AS up,
               count(*) FILTER (WHERE price_end < price_at_ts) AS down
        FROM backtest.outcomes o
        JOIN backtest.decisions d
          ON d.run_id = o.run_id AND d.inst_id = o.inst_id AND d.ts = o.ts
        WHERE o.run_id = $1 AND o.horizon_h = $2 AND o.is_independent
          AND ($3 = FALSE OR o.is_oos);
        """,
        run_id, horizon, oos_only,
    )
    n = int(row["n"] or 0)
    up = int(row["up"] or 0)
    down = int(row["down"] or 0)
    return {
        "n": n,
        "always_buy": up / n if n else 0.0,
        "always_sell": down / n if n else 0.0,
        "random": 0.5,
        "up": up,
        "down": down,
    }


async def _system(run_id: int, horizon: int, oos_only: bool) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE direction_hit) AS hits,
               avg(gross_pnl_pct) AS avg_gross,
               avg(net_pnl_pct)   AS avg_net,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY net_pnl_pct) AS med_net,
               count(*) FILTER (WHERE net_pnl_pct > 0) AS net_positive,
               sum(net_pnl_pct) AS sum_net
        FROM backtest.outcomes
        WHERE run_id = $1 AND horizon_h = $2 AND is_independent
          AND ($3 = FALSE OR is_oos);
        """,
        run_id, horizon, oos_only,
    )
    n = int(row["n"] or 0)
    hits = int(row["hits"] or 0)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hits / n if n else 0.0,
        "avg_gross": float(row["avg_gross"] or 0.0),
        "avg_net": float(row["avg_net"] or 0.0),
        "med_net": float(row["med_net"] or 0.0),
        "net_positive": int(row["net_positive"] or 0),
        "sum_net": float(row["sum_net"] or 0.0),
    }


def check_criterion(
    system: dict[str, Any],
    baselines: dict[str, Any],
    min_edge_pp: float,
    min_n: int,
    max_p: float,
) -> dict[str, Any]:
    """Механическая проверка предрегистрированного критерия §6. Без интерпретации."""
    best_baseline = max(baselines["always_buy"], baselines["always_sell"])
    edge_pp = 100.0 * (system["hit_rate"] - best_baseline)
    p_value = binomial_test_two_sided(system["hits"], system["n"], best_baseline)
    conditions = {
        "а) превышение лучшей тривиальной стратегии >= 5 п.п.": edge_pp >= min_edge_pp,
        "б) N >= 500": system["n"] >= min_n,
        "в) p < 0.05": p_value < max_p,
        "г) средний net_pnl_pct > 0": system["avg_net"] > 0.0,
    }
    return {
        "best_baseline": best_baseline,
        "edge_pp": edge_pp,
        "p_value": p_value,
        "conditions": conditions,
        "passed": all(conditions.values()),
    }


# --- Сборка отчёта ---------------------------------------------------------

def _head(title: str) -> str:
    return f"\n{'=' * WIDTH}\n {title}\n{'=' * WIDTH}"


def _sub(title: str) -> str:
    return f"\n--- {title} ---"


async def build_report(run_id: int, out_path: Path) -> Path:
    """Собирает отчёт по прогону и записывает его в ``out_path``."""
    run = await _run_row(run_id)
    config = run["config"]["config"]
    criterion = run["config"]["criterion"]
    cfg = BacktestConfig(
        instruments=tuple(config["instruments"]),
        bar=config["bar"],
        period_from=datetime.fromisoformat(config["period_from"]),
        period_to=datetime.fromisoformat(config["period_to"]),
        step_hours=int(config["step_hours"]),
        horizons=tuple(int(h) for h in config["horizons"]),
        fee_roundtrip_pct=_dec(config["fee_roundtrip_pct"]),
        slippage_pct=_dec(config["slippage_pct"]),
        oos_months=int(config["oos_months"]),
        request_pause_ms=int(config["request_pause_ms"]),
    )

    lines: list[str] = []
    lines.append("=" * WIDTH)
    lines.append(" AGENT TRADE — ЭТАП 7.4: ИСТОРИЧЕСКИЙ РЕПЛЕЙ ЯДРА")
    lines.append("=" * WIDTH)
    lines.append(f" Прогон:            #{run_id}, статус {run['status']}")
    lines.append(f" Коммит кода:       {run['code_commit']}")
    lines.append(f" Агенты в прогоне:  {', '.join(run['agents_used'])}")
    lines.append(f" Период:            {config['period_from']} — {config['period_to']}")
    lines.append(f" Проверочный отрезок с: {config['oos_from']}")
    lines.append(f" Открыт:            {run['started_at']}")
    lines.append(f" Закрыт:            {run['finished_at']}")
    lines.append(f" Инструменты:       {', '.join(cfg.instruments)}")
    lines.append(f" Издержки:          комиссия {config['fee_roundtrip_pct']}% + "
                 f"проскальзывание {config['slippage_pct']}%")
    lines.append("")
    lines.append(" Предрегистрированный критерий (зафиксирован ДО результатов):")
    for key, value in criterion.items():
        lines.append(f"   {key}: {value}")

    lines.append(_head("СОСТАВ ВЫБОРКИ"))
    counts = await db.fetchrow(
        """
        SELECT count(*) AS decisions,
               count(*) FILTER (WHERE direction = 'wait') AS waits,
               count(*) FILTER (WHERE direction = 'buy')  AS buys,
               count(*) FILTER (WHERE direction = 'sell') AS sells
        FROM backtest.decisions WHERE run_id = $1;
        """,
        run_id,
    )
    lines.append(f"Решений всего: {counts['decisions']}")
    lines.append(f"  buy: {counts['buys']}, sell: {counts['sells']}, wait: {counts['waits']}")
    lines.append("Решения wait в доли попадания не входят (§9.3 ТЗ), их число выше.")

    per_inst = await db.fetch(
        "SELECT inst_id, count(*) AS n, min(ts) AS ts_from, max(ts) AS ts_to "
        "FROM backtest.decisions WHERE run_id=$1 GROUP BY inst_id ORDER BY inst_id;",
        run_id,
    )
    for row in per_inst:
        lines.append(
            f"  {row['inst_id']}: {row['n']} решений, {row['ts_from']} — {row['ts_to']}"
        )

    # --- Расчёт 1 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 1: СИСТЕМА ПРОТИВ ТРЁХ БАЗОВЫХ ЛИНИЙ"))
    lines.append("Только независимые наблюдения (непересекающиеся окна горизонта).")
    for oos_only in (False, True):
        scope = "ПРОВЕРОЧНЫЙ ОТРЕЗОК (out-of-sample)" if oos_only else "ВСЯ ВЫБОРКА"
        lines.append(_sub(scope))
        lines.append(
            f"{'гориз.':>7} {'N':>7} {'система':>10} {'95% ДИ':>18} "
            f"{'всегда buy':>11} {'всегда sell':>12} {'случай':>8}"
        )
        for horizon in cfg.horizons:
            system = await _system(run_id, horizon, oos_only)
            base = await _baselines(run_id, horizon, oos_only)
            low, high = wilson_interval(system["hits"], system["n"])
            lines.append(
                f"{horizon:>6}ч {system['n']:>7} {pct(system['hit_rate']):>10} "
                f"[{pct(low)}, {pct(high)}]".ljust(46)
                + f"{pct(base['always_buy']):>11} {pct(base['always_sell']):>12} "
                f"{pct(0.5):>8}"
            )

    # --- Расчёт 2 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 2: ПОПАДАНИЕ НАПРАВЛЕНИЯ КАЖДОГО АГЕНТА"))
    lines.append("Мнение агента берётся из agents_payload решения; сравнивается")
    lines.append("с фактическим движением цены на том же горизонте.")
    for horizon in cfg.horizons:
        rows = await db.fetch(
            """
            SELECT el->>'agent' AS agent,
                   count(*) FILTER (WHERE el->>'signal' IN ('bullish','bearish')) AS n,
                   count(*) FILTER (
                       WHERE (el->>'signal' = 'bullish' AND o.price_end > d.price_at_ts)
                          OR (el->>'signal' = 'bearish' AND o.price_end < d.price_at_ts)
                   ) AS hits,
                   count(*) FILTER (WHERE el->>'signal' = 'neutral') AS neutrals
            FROM backtest.decisions d
            JOIN backtest.outcomes o
              ON o.run_id=d.run_id AND o.inst_id=d.inst_id AND o.ts=d.ts
            CROSS JOIN LATERAL jsonb_array_elements(d.agents_payload) el
            WHERE d.run_id=$1 AND o.horizon_h=$2 AND o.is_independent
            GROUP BY el->>'agent' ORDER BY el->>'agent';
            """,
            run_id, horizon,
        )
        base = await _baselines(run_id, horizon, False)
        lines.append(_sub(f"горизонт {horizon} ч (базовая линия: "
                          f"buy {pct(base['always_buy'])}, sell {pct(base['always_sell'])})"))
        if not rows:
            lines.append("  нет данных")
        for row in rows:
            n = int(row["n"] or 0)
            hits = int(row["hits"] or 0)
            rate = hits / n if n else 0.0
            low, high = wilson_interval(hits, n)
            lines.append(
                f"  {row['agent']:<10} N={n:<7} попадание {pct(rate):>7} "
                f"[{pct(low)}, {pct(high)}]  нейтральных: {row['neutrals']}"
            )

    # --- Расчёт 3 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 3: КАЛИБРОВКА ИНДЕКСА СОГЛАСИЯ"))
    lines.append("В Этапе 7.1 связь была монотонно ОБРАТНОЙ. Проверяется, сохраняется")
    lines.append("ли она на большой выборке и после правок Этапа 7.3.")
    for horizon in cfg.horizons:
        rows = await db.fetch(
            """
            SELECT least(width_bucket(d.probability, 0, 1, 5), 5) AS bucket,
                   count(*) AS n,
                   avg(d.probability) AS claimed,
                   count(*) FILTER (WHERE o.direction_hit) AS hits
            FROM backtest.decisions d
            JOIN backtest.outcomes o
              ON o.run_id=d.run_id AND o.inst_id=d.inst_id AND o.ts=d.ts
            WHERE d.run_id=$1 AND o.horizon_h=$2 AND o.is_independent
            GROUP BY bucket ORDER BY bucket;
            """,
            run_id, horizon,
        )
        lines.append(_sub(f"горизонт {horizon} ч"))
        lines.append(
            f"  {'диапазон':<12} {'N':>7} {'заявлено':>10} "
            f"{'фактически':>11} {'разрыв':>9}"
        )
        for row in rows:
            n = int(row["n"])
            hits = int(row["hits"])
            actual = hits / n if n else 0.0
            claimed = float(row["claimed"] or 0.0)
            label = ["0.0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1.0"][int(row["bucket"]) - 1]
            lines.append(
                f"  {label:<12} {n:>7} {claimed:>10.4f} {actual:>11.4f} "
                f"{claimed - actual:>9.4f}"
            )

    # --- Расчёт 4 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 4: ДОЛЯ ПОВТОРОВ ВЫВОДА АГЕНТА"))
    rows = await db.fetch(
        """
        WITH ordered AS (
            SELECT d.inst_id, d.ts, el->>'agent' AS agent, el->>'signal' AS signal,
                   round((el->>'confidence')::numeric, 4) AS confidence
            FROM backtest.decisions d
            CROSS JOIN LATERAL jsonb_array_elements(d.agents_payload) el
            WHERE d.run_id = $1
        ), lagged AS (
            SELECT *,
                   lag(signal)     OVER (PARTITION BY inst_id, agent ORDER BY ts) AS prev_signal,
                   lag(confidence) OVER (PARTITION BY inst_id, agent ORDER BY ts) AS prev_conf
            FROM ordered
        )
        SELECT inst_id, agent,
               count(*) FILTER (WHERE prev_signal IS NOT NULL) AS comparable,
               count(*) FILTER (
                   WHERE prev_signal IS NOT NULL AND signal = prev_signal
               ) AS same_signal,
               count(*) FILTER (
                   WHERE prev_conf IS NOT NULL AND confidence = prev_conf
               ) AS same_conf
        FROM lagged GROUP BY inst_id, agent ORDER BY inst_id, agent;
        """,
        run_id,
    )
    lines.append(
        f"  {'инструмент':<18} {'агент':<10} {'N':>7} "
        f"{'то же направление':>18} {'та же уверенность':>18}"
    )
    for row in rows:
        comparable = int(row["comparable"] or 0)
        s_share = (int(row["same_signal"]) / comparable) if comparable else 0.0
        c_share = (int(row["same_conf"]) / comparable) if comparable else 0.0
        lines.append(
            f"  {row['inst_id']:<18} {row['agent']:<10} {comparable:>7} "
            f"{pct(s_share):>18} {pct(c_share):>18}"
        )

    # --- Расчёт 5 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 5: СОГЛАСОВАННОСТЬ НАПРАВЛЕНИЙ МЕЖДУ АГЕНТАМИ"))
    rows = await db.fetch(
        """
        WITH piv AS (
            SELECT d.inst_id, d.ts,
                   max(CASE WHEN el->>'agent'='market'  THEN el->>'signal' END) AS m,
                   max(CASE WHEN el->>'agent'='futures' THEN el->>'signal' END) AS f
            FROM backtest.decisions d
            CROSS JOIN LATERAL jsonb_array_elements(d.agents_payload) el
            WHERE d.run_id = $1
            GROUP BY d.inst_id, d.ts
        )
        SELECT inst_id,
               count(*) FILTER (WHERE m IS NOT NULL AND f IS NOT NULL) AS both_present,
               count(*) FILTER (WHERE m IS NOT NULL AND f IS NOT NULL AND m = f) AS same,
               count(*) FILTER (
                   WHERE m IN ('bullish','bearish') AND f IN ('bullish','bearish')
               ) AS both_dir,
               count(*) FILTER (
                   WHERE m IN ('bullish','bearish')
                     AND f IN ('bullish','bearish') AND m = f
               ) AS same_dir
        FROM piv GROUP BY inst_id ORDER BY inst_id;
        """,
        run_id,
    )
    lines.append(f"  {'инструмент':<18} {'оба есть':>9} {'совпало':>9} {'доля':>8} "
                 f"{'оба направленны':>16} {'совпало':>9} {'доля':>8}")
    for row in rows:
        both = int(row["both_present"] or 0)
        same = int(row["same"] or 0)
        both_dir = int(row["both_dir"] or 0)
        same_dir = int(row["same_dir"] or 0)
        lines.append(
            f"  {row['inst_id']:<18} {both:>9} {same:>9} "
            f"{pct(same / both if both else 0):>8} {both_dir:>16} {same_dir:>9} "
            f"{pct(same_dir / both_dir if both_dir else 0):>8}"
        )

    # --- Расчёт 6 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 6: NET PNL ПО ГОРИЗОНТАМ (С ИЗДЕРЖКАМИ)"))
    total_costs = float(cfg.fee_roundtrip_pct) + float(cfg.slippage_pct)
    lines.append(f"Издержки на сделку: {total_costs:.3f}%")
    lines.append(f"  {'гориз.':>7} {'N':>7} {'ср. gross':>10} {'ср. net':>10} "
                 f"{'медиана net':>12} {'net>0':>8} {'сумма net':>11}")
    for oos_only in (False, True):
        lines.append(_sub("ПРОВЕРОЧНЫЙ ОТРЕЗОК" if oos_only else "ВСЯ ВЫБОРКА"))
        for horizon in cfg.horizons:
            system = await _system(run_id, horizon, oos_only)
            share = system["net_positive"] / system["n"] if system["n"] else 0.0
            lines.append(
                f"  {horizon:>5}ч {system['n']:>7} {system['avg_gross']:>10.4f} "
                f"{system['avg_net']:>10.4f} {system['med_net']:>12.4f} "
                f"{pct(share):>8} {system['sum_net']:>11.2f}"
            )

    # --- Расчёт 7 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 7: РАЗБИВКА ПО РЕЖИМАМ РЫНКА И ВОЛАТИЛЬНОСТИ"))
    for horizon in cfg.horizons:
        rows = await db.fetch(
            """
            SELECT regime, vol_quartile, count(*) AS n,
                   count(*) FILTER (WHERE direction_hit) AS hits,
                   avg(net_pnl_pct) AS avg_net
            FROM backtest.outcomes
            WHERE run_id=$1 AND horizon_h=$2 AND is_independent
            GROUP BY regime, vol_quartile ORDER BY regime, vol_quartile;
            """,
            run_id, horizon,
        )
        lines.append(_sub(f"горизонт {horizon} ч"))
        lines.append(f"  {'режим':<8} {'кв.вол.':>8} {'N':>7} {'попадание':>10} {'ср. net':>10}")
        for row in rows:
            n = int(row["n"])
            hits = int(row["hits"])
            lines.append(
                f"  {row['regime']:<8} {row['vol_quartile']:>8} {n:>7} "
                f"{pct(hits / n if n else 0):>10} {float(row['avg_net'] or 0):>10.4f}"
            )

    # --- Расчёт 8 ---------------------------------------------------------
    lines.append(_head("РАСЧЁТ 8: КОРРЕЛЯЦИЯ ИСХОДОВ МЕЖДУ ИНСТРУМЕНТАМИ"))
    lines.append("Три инструмента НЕ дают трёхкратного роста мощности: криптовалюты")
    lines.append("сильно коррелированы. Ниже — фактическая корреляция попаданий.")
    for horizon in cfg.horizons:
        rows = await db.fetch(
            """
            SELECT a.inst_id AS inst_a, b.inst_id AS inst_b,
                   count(*) AS n,
                   corr(CASE WHEN a.direction_hit THEN 1.0 ELSE 0.0 END,
                        CASE WHEN b.direction_hit THEN 1.0 ELSE 0.0 END) AS r
            FROM backtest.outcomes a
            JOIN backtest.outcomes b
              ON a.run_id=b.run_id AND a.ts=b.ts AND a.horizon_h=b.horizon_h
             AND a.inst_id < b.inst_id
            WHERE a.run_id=$1 AND a.horizon_h=$2 AND a.is_independent
            GROUP BY a.inst_id, b.inst_id ORDER BY a.inst_id, b.inst_id;
            """,
            run_id, horizon,
        )
        lines.append(_sub(f"горизонт {horizon} ч"))
        if not rows:
            lines.append("  пар инструментов с совпадающими моментами нет")
        for row in rows:
            r = float(row["r"] or 0.0)
            n = int(row["n"])
            # Грубая оценка эффективного размера выборки при средней корреляции r
            # между k рядами: N_eff ≈ N / (1 + (k-1)·r).
            lines.append(
                f"  {row['inst_a']} ↔ {row['inst_b']}: N={n}, корреляция попаданий r={r:.3f}, "
                f"эффективных наблюдений ≈ {int(n / (1 + max(r, 0.0))) if n else 0}"
            )

    # --- Вердикт ----------------------------------------------------------
    lines.append(_head("ВЕРДИКТ ПО ПРЕДРЕГИСТРИРОВАННОМУ КРИТЕРИЮ (§6 ТЗ)"))
    lines.append("Проверка механическая: выполнено / не выполнено, без интерпретации.")
    lines.append("Критерий проверяется ТОЛЬКО на проверочном отрезке (out-of-sample).")
    min_edge = float(criterion.get("min_edge_pp", 5.0))
    min_n = int(criterion.get("min_independent_n", 500))
    max_p = float(criterion.get("max_p_value", 0.05))
    any_passed = False
    for horizon in cfg.horizons:
        system = await _system(run_id, horizon, True)
        base = await _baselines(run_id, horizon, True)
        verdict = check_criterion(system, base, min_edge, min_n, max_p)
        any_passed = any_passed or verdict["passed"]
        lines.append(_sub(f"горизонт {horizon} ч"))
        lines.append(
            f"  N={system['n']}, попаданий {system['hits']}, доля {pct(system['hit_rate'])}, "
            f"лучшая тривиальная {pct(verdict['best_baseline'])}"
        )
        lines.append(
            f"  превышение {verdict['edge_pp']:+.2f} п.п., p={verdict['p_value']:.4g}, "
            f"средний net {system['avg_net']:+.4f}%"
        )
        for name, ok in verdict["conditions"].items():
            lines.append(f"    {'ВЫПОЛНЕНО    ' if ok else 'НЕ ВЫПОЛНЕНО '} {name}")
        lines.append(f"  ИТОГ ПО ГОРИЗОНТУ: {'ВЫПОЛНЕН' if verdict['passed'] else 'НЕ ВЫПОЛНЕН'}")

    lines.append("")
    lines.append(
        "ОБЩИЙ ВЕРДИКТ: преимущество "
        + ("НАЙДЕНО" if any_passed else "НЕ НАЙДЕНО")
        + " (все четыре условия одновременно "
        + ("выполнены хотя бы на одном горизонте)" if any_passed
           else "не выполнены ни на одном горизонте и ни в одной конфигурации)")
    )

    lines.append(_head("ОГРАНИЧЕНИЯ, ДЕЙСТВУЮЩИЕ НЕЗАВИСИМО ОТ РЕЗУЛЬТАТА (§15 ТЗ)"))
    for item in (
        "15.1 Liquidity Agent не проверен и не может быть проверен этим методом: "
        "истории стакана не существует.",
        "15.2 Реплей не воспроизводит проскальзывание, глубину стакана и реальную "
        "исполнимость входа. BT_SLIPPAGE_PCT — грубая поправка, а не измерение.",
        "15.3 Три инструмента сильно коррелированы; эффективное число независимых "
        "наблюдений меньше формального (см. Расчёт 8).",
        "15.4 Положительный результат реплея НЕ доказывает, что система заработает "
        "вживую. Он лишь даёт основание проверить гипотезу на живых данных.",
        "15.5 Отрицательный результат на большой выборке и нескольких режимах — "
        "существенно более сильное утверждение, чем отрицательный результат "
        "Этапа 7.1, и его следует принимать всерьёз.",
        "Открытый интерес: исторического ряда среди разрешённых эндпоинтов нет, "
        "поэтому подтверждения со стороны OI в реплее не бывает и уверенность "
        "Futures систематически ниже продакшновой.",
    ):
        lines.append(f"  * {item}")

    lines.append("")
    lines.append(f"Отчёт собран: {datetime.now(UTC).isoformat()}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _dec(value: str):
    from decimal import Decimal

    return Decimal(str(value))
