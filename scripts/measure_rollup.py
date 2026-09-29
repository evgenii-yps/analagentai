#!/usr/bin/env python3
"""Свёртка таблиц замеров ``trailing_outcomes`` и ``strategy_outcomes`` (Этап 9.4, блок D).

Встраивается в ``scripts/retention.py`` (отдельной ночной задачи нет). Образец —
``trade_flow_1m`` и ``agent_outputs_daily``: СНАЧАЛА СВЁРТКА, ПОТОМ УДАЛЕНИЕ.

ЧТО ХРАНИТСЯ ВЕЧНО
  * ``trailing_outcomes_daily`` — сутки (по времени сигнала, UTC) × инструмент ×
    ``logic_version`` × горизонт × вариант выхода (activation, retrace);
  * ``strategy_outcomes_daily`` — сутки (по ``entry_ts``, UTC) × инструмент ×
    ``logic_version`` × стратегия × горизонт;
  * ``trailing_pairs_compact`` — по строке на пару (сигнал, горизонт), тринадцать
    итогов в миллионных долях процента: точное воспроизведение защит от подгонки 9.1.3;
  * ``strategy_pairs_compact`` — по строке на пару, итоги четырёх стратегий,
    привязанных к сигналу: попарное сравнение с системой.

ЧТО УДАЛЯЕТСЯ (решение заказчика, поправки 1 и 2)
  * ``trailing_outcomes`` — ВСЁ (замер 9.1.3 завершён, писатель ``trailing_main``
    отключён насовсем): :data:`FROZEN_TABLES`;
  * ``strategy_outcomes`` — детали версий логики СТАРШЕ ТЕКУЩЕЙ, независимо от
    возраста. Текущая версия хранится ПОЛНОСТЬЮ: замер продолжается, вопрос
    «есть ли у системы преимущество над случайным входом» не решён.
    Переход на новую версию автоматически переводит предыдущую в свёрнутое
    состояние.
  * ``MEASURE_DETAIL_RETENTION_DAYS`` — НЕ ПРИМЕНЯЕТСЯ ни к одной таблице: граница
    хранения определяется только версией логики (решение заказчика 29.09.2026;
    поправка 1 в части календарного потолка отменена). Значение читается, печатается
    и проверяется на «целое >= 1», но никакого удаления по возрасту в коде нет и
    включающей строки нет. ИЗ ЭТОГО СЛЕДУЕТ, что рост ``strategy_outcomes`` (~26 МБ в
    сутки) ничем не ограничен по построению — это принятое решение, а не упущение;
    вопрос возвращается при закрытии вопроса о преимуществе над случайностью либо при
    срабатывании раннего порога вотчдога (25% свободного места).

ПОРЯДОК ОБЯЗАТЕЛЕН И ОБЕСПЕЧЕН КОДОМ
  1. Свёртка идёт по ЗАВЕРШЁННЫМ суткам (``день <= сегодня - SETTLE_DAYS``), каждые
     сутки — одной транзакцией: сутки либо свёрнуты целиком, либо нет. Идемпотентна
     (``ON CONFLICT DO NOTHING``).
  2. Удаляются ТОЛЬКО те (сутки, версия), где счёт сошёлся ТРИЖДЫ: число строк
     деталей == сумме ``n_rows`` свёртки == числу строк компактной таблицы, а
     сумма итогов в деньгах в деталях == сумме в компактной таблице. Любое
     расхождение — удаление этих суток пропускается и печатается.
  3. Первое удаление — только командой ``--measure-delete`` с подтверждением ЧИСЛОМ
     строк (как ``repair_9_1_strategy_settle.py``); после успеха ночная задача
     удаляет сама (флаг ``measure_rollup_state.delete_armed``).
  4. Таблицы остаются в ``PROTECTED_TABLES``: общий путь удаления ``retention.py``
     их по-прежнему отвергает. Удаление здесь — отдельный узкий путь со своими
     ограждениями.

Только стандартная библиотека; SQL выполняется переданной функцией ``psql``.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

# psql(*sql) -> stdout. Каждая строка — отдельный ``-c``; одна строка с несколькими
# командами выполняется одной транзакцией.
Psql = Callable[..., str]

TRAILING = "trailing_outcomes"
STRATEGY = "strategy_outcomes"
TRAILING_DAILY = "trailing_outcomes_daily"
STRATEGY_DAILY = "strategy_outcomes_daily"
TRAILING_COMPACT = "trailing_pairs_compact"
STRATEGY_COMPACT = "strategy_pairs_compact"
STATE_TABLE = "measure_rollup_state"
DERIVED_TABLES = (TRAILING_DAILY, STRATEGY_DAILY, TRAILING_COMPACT, STRATEGY_COMPACT)

# Сутки считаются завершёнными через три дня: максимальный горизонт 24 ч + сутки на
# ночной расчёт + запас. Неполные сутки не сворачиваются никогда: ``DO NOTHING`` не
# позволил бы потом дописать итог.
SETTLE_DAYS = 3

# Поправка 2: замер 9.1.3 завершён, таблица замораживается — сворачивается вся.
FROZEN_TABLES: frozenset[str] = frozenset({TRAILING})

# Порядок вариантов v00..v12 повторяет ``src.trailing.rule.VARIANTS`` (сверяется тестом).
ACTIVATION_RATIOS = (0.25, 0.50, 0.75, 1.00)
RETRACE_RATIOS = (0.20, 0.33, 0.50)
VARIANTS: tuple[tuple[float, float], ...] = (
    (0.00, 0.00),
    *((a, r) for a in ACTIVATION_RATIOS for r in RETRACE_RATIOS),
)
# Стратегии, привязанные к сигналу и сравниваемые с системой попарно.
PAIRED_STRATEGIES = ("system", "always_buy", "always_sell", "coin_flip")

PNL_SCALE = 1_000_000  # net_pnl_pct хранится с шестью знаками — целое без потери точности


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


class MeasureError(RuntimeError):
    """Ограждение сработало — операция остановлена, ничего не удалено."""


# ---------------------------------------------------------------------------
# SQL свёртки
# ---------------------------------------------------------------------------

def day_range(day: date) -> tuple[str, str]:
    """Границы суток UTC как SQL-литералы (``day`` — объект date, инъекции нет)."""
    start = f"'{day.isoformat()} 00:00:00+00'::timestamptz"
    end = f"'{(day + timedelta(days=1)).isoformat()} 00:00:00+00'::timestamptz"
    return start, end


def _pctl(col: str, q: str, scale: int = 6) -> str:
    return (f"round((percentile_cont({q}) WITHIN GROUP (ORDER BY {col}))::numeric, {scale})")


def trailing_daily_sql(day: date) -> str:
    """Суточная свёртка ``trailing_outcomes`` за сутки сигнала ``day``."""
    a, b = day_range(day)
    return f"""
        INSERT INTO {TRAILING_DAILY}
            (day, instrument_id, logic_version, horizon_h, activation_ratio,
             retrace_ratio, n_rows, n_target, n_stop, n_trail, n_timeout,
             n_ambiguous, n_no_data, n_res_1m, n_res_1h, n_buy, n_sell, n_pnl,
             avg_net_pnl, p25_net_pnl, p50_net_pnl, p75_net_pnl, avg_peak_pct,
             avg_mae_pct, avg_mfe_pct, p50_bars_to_hit)
        SELECT '{day.isoformat()}'::date, s.instrument_id, t.logic_version,
               t.horizon_h, t.activation_ratio, t.retrace_ratio,
               count(*),
               count(*) FILTER (WHERE t.exit_reason = 'target'),
               count(*) FILTER (WHERE t.exit_reason = 'stop'),
               count(*) FILTER (WHERE t.exit_reason = 'trail'),
               count(*) FILTER (WHERE t.exit_reason = 'timeout'),
               count(*) FILTER (WHERE t.exit_reason = 'ambiguous'),
               count(*) FILTER (WHERE t.exit_reason = 'no_data'),
               count(*) FILTER (WHERE t.resolution = '1m'),
               count(*) FILTER (WHERE t.resolution = '1h'),
               count(*) FILTER (WHERE t.direction = 'buy'),
               count(*) FILTER (WHERE t.direction = 'sell'),
               count(t.net_pnl_pct),
               round(avg(t.net_pnl_pct), 6),
               {_pctl('t.net_pnl_pct', '0.25')},
               {_pctl('t.net_pnl_pct', '0.5')},
               {_pctl('t.net_pnl_pct', '0.75')},
               round(avg(t.peak_pct), 6),
               round(avg(t.mae_pct), 6),
               round(avg(t.mfe_pct), 6),
               {_pctl('t.bars_to_hit', '0.5', 2)}
          FROM {TRAILING} t
          JOIN signals s ON s.id = t.signal_id
         WHERE s.ts >= {a} AND s.ts < {b}
         GROUP BY s.instrument_id, t.logic_version, t.horizon_h,
                  t.activation_ratio, t.retrace_ratio
        ON CONFLICT DO NOTHING;
    """


def _variant_filter(activation: float, retrace: float) -> str:
    return f"t.activation_ratio = {activation:.2f} AND t.retrace_ratio = {retrace:.2f}"


def trailing_compact_sql(day: date) -> str:
    """Компактные пары ``trailing_outcomes`` за сутки сигнала ``day``.

    ``exclude_reason`` повторяет ``scripts/trailing_stats.collect`` в том же порядке
    проверок: неполный набор → ``incomplete``; есть ``no_data`` → ``no_data``; есть
    ``ambiguous`` → ``ambiguous``; у кого-то нет итога в деньгах → ``incomplete``;
    иначе пара полная и итоги записаны. У исключённых пар итоги NULL.
    """
    a, b = day_range(day)
    n = len(VARIANTS)
    complete = (
        f"(count(*) = {n} AND NOT bool_or(t.exit_reason = 'no_data') "
        f"AND NOT bool_or(t.exit_reason = 'ambiguous') AND count(t.net_pnl_pct) = {n})"
    )
    cols = ", ".join(f"v{i:02d}" for i in range(n))
    pivots = ",\n               ".join(
        f"CASE WHEN {complete} THEN max(round(t.net_pnl_pct * {PNL_SCALE})::int) "
        f"FILTER (WHERE {_variant_filter(av, rv)}) END"
        for av, rv in VARIANTS
    )
    return f"""
        INSERT INTO {TRAILING_COMPACT}
            (day, signal_id, horizon_h, logic_version, n_rows, computed_at,
             exclude_reason, {cols})
        SELECT '{day.isoformat()}'::date, t.signal_id, t.horizon_h,
               min(t.logic_version), count(*)::smallint, min(t.computed_at),
               CASE WHEN count(*) < {n} THEN 'incomplete'
                    WHEN bool_or(t.exit_reason = 'no_data') THEN 'no_data'
                    WHEN bool_or(t.exit_reason = 'ambiguous') THEN 'ambiguous'
                    WHEN count(t.net_pnl_pct) < {n} THEN 'incomplete'
               END,
               {pivots}
          FROM {TRAILING} t
          JOIN signals s ON s.id = t.signal_id
         WHERE s.ts >= {a} AND s.ts < {b}
         GROUP BY t.signal_id, t.horizon_h
        ON CONFLICT DO NOTHING;
    """


def strategy_daily_sql(day: date) -> str:
    """Суточная свёртка ``strategy_outcomes`` за сутки входа ``day``."""
    a, b = day_range(day)
    return f"""
        INSERT INTO {STRATEGY_DAILY}
            (day, instrument_id, logic_version, strategy, horizon_h, n_rows,
             n_target, n_stop, n_timeout, n_ambiguous, n_no_data, n_res_1m,
             n_res_1h, n_buy, n_sell, n_pnl, avg_net_pnl, p25_net_pnl,
             p50_net_pnl, p75_net_pnl, avg_mae_pct, avg_mfe_pct)
        SELECT '{day.isoformat()}'::date, o.instrument_id, o.logic_version,
               o.strategy, o.horizon_h,
               count(*),
               count(*) FILTER (WHERE o.outcome = 'target'),
               count(*) FILTER (WHERE o.outcome = 'stop'),
               count(*) FILTER (WHERE o.outcome = 'timeout'),
               count(*) FILTER (WHERE o.outcome = 'ambiguous'),
               count(*) FILTER (WHERE o.outcome = 'no_data'),
               count(*) FILTER (WHERE o.resolution = '1m'),
               count(*) FILTER (WHERE o.resolution = '1h'),
               count(*) FILTER (WHERE o.direction = 'buy'),
               count(*) FILTER (WHERE o.direction = 'sell'),
               count(o.net_pnl_pct),
               round(avg(o.net_pnl_pct), 6),
               {_pctl('o.net_pnl_pct', '0.25')},
               {_pctl('o.net_pnl_pct', '0.5')},
               {_pctl('o.net_pnl_pct', '0.75')},
               round(avg(o.mae_pct), 6),
               round(avg(o.mfe_pct), 6)
          FROM {STRATEGY} o
         WHERE o.entry_ts >= {a} AND o.entry_ts < {b}
         GROUP BY o.instrument_id, o.logic_version, o.strategy, o.horizon_h
        ON CONFLICT DO NOTHING;
    """


def strategy_compact_sql(day: date) -> str:
    """Компактные пары ``strategy_outcomes`` (четыре стратегии, привязанные к сигналу)."""
    a, b = day_range(day)
    names = ", ".join(f"'{s}'" for s in PAIRED_STRATEGIES)
    pivots = ",\n               ".join(
        f"max(round(o.net_pnl_pct * {PNL_SCALE})::int) FILTER (WHERE o.strategy = '{s}')"
        for s in PAIRED_STRATEGIES
    )
    return f"""
        INSERT INTO {STRATEGY_COMPACT}
            (day, signal_id, horizon_h, logic_version, system_pnl,
             always_buy_pnl, always_sell_pnl, coin_flip_pnl)
        SELECT '{day.isoformat()}'::date, o.signal_id, o.horizon_h, o.logic_version,
               {pivots}
          FROM {STRATEGY} o
         WHERE o.entry_ts >= {a} AND o.entry_ts < {b}
           AND o.signal_id IS NOT NULL AND o.strategy IN ({names})
         GROUP BY o.signal_id, o.horizon_h, o.logic_version
        ON CONFLICT DO NOTHING;
    """


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _int(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value not in ("", "\\N") else 0


def _rows(out: str) -> list[list[str]]:
    return [line.split("|") for line in out.splitlines() if line.strip()]


def tables_exist(psql: Psql) -> bool:
    """Все ли таблицы миграции 029 и обе исходные на месте."""
    names = (TRAILING, STRATEGY, *DERIVED_TABLES, STATE_TABLE)
    sql = "SELECT " + " AND ".join(f"to_regclass('public.{n}') IS NOT NULL" for n in names)
    return psql(sql).strip() == "t"


def current_logic_version(psql: Psql) -> int | None:
    """Текущая версия логики — последнее окно ``logic_version_windows``."""
    out = psql("SELECT logic_version FROM logic_version_windows "
               "ORDER BY started_at DESC LIMIT 1;").strip()
    return int(out) if out.isdigit() else None


def settle_cutoff(today: date | None = None) -> date:
    """Последние завершённые сутки (включительно)."""
    today = today or datetime.now(UTC).date()
    return today - timedelta(days=SETTLE_DAYS)


def retention_days(value: str | None) -> int:
    """``MEASURE_DETAIL_RETENTION_DAYS``: целое >= 1, иначе остановка (не молчаливое умолчание)."""
    if value is None or not str(value).strip().isdigit() or int(value) < 1:
        raise MeasureError(
            f"MEASURE_DETAIL_RETENTION_DAYS={value!r} — нужно целое число не меньше 1")
    return int(value)


def writers_active(cron_dir: str = "/etc/cron.d") -> dict[str, bool] | None:
    """Есть ли в cron НЕзакомментированные строки писателей замеров.

    None — cron прочитать не удалось: вызывающий обязан считать это «неизвестно» и
    не удалять. Читается фактическое расписание, а не память о том, что его сняли.
    """
    files = glob.glob(os.path.join(cron_dir, "*"))
    if not files:
        return None
    found = {"trailing_main": False, "baseline_main": False}
    try:
        for path in files:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    for key in found:
                        if key in text:
                            found[key] = True
    except OSError:
        return None
    return found


# ---------------------------------------------------------------------------
# Свёртка
# ---------------------------------------------------------------------------

@dataclass
class RollupResult:
    """Итог свёртки одной таблицы."""

    days: list[date] = field(default_factory=list)
    daily_rows: int = 0
    compact_rows: int = 0


def _candidate_days(psql: Psql, table: str, cutoff: date) -> list[date]:
    """Завершённые сутки, по которым есть детали, а свёртки ещё нет."""
    if table == TRAILING:
        sql = f"""
            SELECT d FROM (
                SELECT DISTINCT (s.ts AT TIME ZONE 'UTC')::date AS d
                  FROM {TRAILING} t JOIN signals s ON s.id = t.signal_id
                 WHERE s.ts < {day_range(cutoff)[1]}
            ) x
             WHERE NOT EXISTS (SELECT 1 FROM {TRAILING_DAILY} r WHERE r.day = x.d)
             ORDER BY d;"""
    else:
        sql = f"""
            SELECT d FROM (
                SELECT DISTINCT (entry_ts AT TIME ZONE 'UTC')::date AS d
                  FROM {STRATEGY} WHERE entry_ts < {day_range(cutoff)[1]}
            ) x
             WHERE NOT EXISTS (SELECT 1 FROM {STRATEGY_DAILY} r WHERE r.day = x.d)
             ORDER BY d;"""
    return [date.fromisoformat(r[0]) for r in _rows(psql(sql))]


def rollup(psql: Psql, table: str, *, cutoff: date | None = None) -> RollupResult:
    """Сворачивает завершённые сутки таблицы ``table``. Идемпотентно.

    Каждые сутки — ОДНА транзакция из двух вставок (итоги + компактные пары): сутки
    появляются в свёртке либо целиком, либо не появляются, поэтому «сутки есть в
    итогах» означает «сутки свёрнуты полностью».
    """
    cutoff = cutoff or settle_cutoff()
    result = RollupResult()
    build = ((trailing_daily_sql, trailing_compact_sql) if table == TRAILING
             else (strategy_daily_sql, strategy_compact_sql))
    for day in _candidate_days(psql, table, cutoff):
        out = psql(build[0](day) + build[1](day))
        counts = [int(m) for m in re.findall(r"INSERT 0 (\d+)", out)]
        if len(counts) == 2:
            result.daily_rows += counts[0]
            result.compact_rows += counts[1]
        result.days.append(day)
    return result


# ---------------------------------------------------------------------------
# Сверка и удаление
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DayVersion:
    """Сутки × версия, детали которых можно удалять (счёт сошёлся)."""

    day: date
    logic_version: int
    n_rows: int


@dataclass
class Plan:
    """Что удаляется, что остаётся и почему (для отчёта «только счёт»)."""

    table: str
    eligible: list[DayVersion] = field(default_factory=list)
    mismatched: list[tuple[date, int, str]] = field(default_factory=list)
    kept_by_rule: int = 0          # строк, которые правило удалять не велит (текущая версия)
    unrolled: int = 0              # строк по несвёрнутым суткам (ещё не завершены)
    total_rows: int = 0
    # версия -> (всего строк, к удалению)
    per_version: dict[int, tuple[int, int]] = field(default_factory=dict)

    @property
    def delete_rows(self) -> int:
        return sum(e.n_rows for e in self.eligible)


def _rule_allows(table: str, version: int, current: int | None) -> bool:
    """Правило хранения: какие версии деталей вправе удаляться (возраст не учитывается).

    Замороженная таблица — все версии. Иначе — версии СТАРШЕ текущей; детали текущей
    версии хранятся полностью (решение заказчика 29.09.2026).
    """
    if table in FROZEN_TABLES:
        return True
    return current is not None and version < current


def _reconcile_trailing_sql(cutoff: date) -> str:
    end = day_range(cutoff)[1]
    total = " + ".join(f"coalesce(c.v{i:02d}, 0)" for i in range(len(VARIANTS)))
    return f"""
        WITH det AS (
            SELECT (s.ts AT TIME ZONE 'UTC')::date AS day, t.logic_version,
                   count(*) AS n,
                   sum(round(t.net_pnl_pct * {PNL_SCALE})) FILTER (
                       WHERE c.signal_id IS NOT NULL AND c.exclude_reason IS NULL) AS sm
              FROM {TRAILING} t
              JOIN signals s ON s.id = t.signal_id
              LEFT JOIN {TRAILING_COMPACT} c
                     ON c.signal_id = t.signal_id AND c.horizon_h = t.horizon_h
             WHERE s.ts < {end}
             GROUP BY 1, 2
        ), rol AS (
            SELECT day, logic_version, sum(n_rows) AS n FROM {TRAILING_DAILY} GROUP BY 1, 2
        ), com AS (
            SELECT c.day, c.logic_version, sum(c.n_rows) AS n,
                   sum({total}) AS sm
              FROM {TRAILING_COMPACT} c GROUP BY 1, 2
        )
        SELECT det.day, det.logic_version, det.n, coalesce(rol.n, 0),
               coalesce(com.n, 0), coalesce(det.sm, 0), coalesce(com.sm, 0)
          FROM det
          LEFT JOIN rol USING (day, logic_version)
          LEFT JOIN com USING (day, logic_version)
         ORDER BY 1, 2;
    """


def _reconcile_strategy_sql(cutoff: date) -> str:
    end = day_range(cutoff)[1]
    names = ", ".join(f"'{s}'" for s in PAIRED_STRATEGIES)
    total = ("coalesce(system_pnl, 0) + coalesce(always_buy_pnl, 0) + "
             "coalesce(always_sell_pnl, 0) + coalesce(coin_flip_pnl, 0)")
    return f"""
        WITH det AS (
            SELECT (entry_ts AT TIME ZONE 'UTC')::date AS day, logic_version,
                   count(*) AS n,
                   count(DISTINCT (signal_id, horizon_h))
                       FILTER (WHERE signal_id IS NOT NULL AND strategy IN ({names})) AS pairs,
                   sum(round(net_pnl_pct * {PNL_SCALE}))
                       FILTER (WHERE signal_id IS NOT NULL AND strategy IN ({names})) AS sm
              FROM {STRATEGY}
             WHERE entry_ts < {end}
             GROUP BY 1, 2
        ), rol AS (
            SELECT day, logic_version, sum(n_rows) AS n FROM {STRATEGY_DAILY} GROUP BY 1, 2
        ), com AS (
            SELECT day, logic_version, count(*) AS pairs, sum({total}) AS sm
              FROM {STRATEGY_COMPACT} GROUP BY 1, 2
        )
        SELECT det.day, det.logic_version, det.n, coalesce(rol.n, 0),
               det.pairs, coalesce(com.pairs, 0), coalesce(det.sm, 0), coalesce(com.sm, 0)
          FROM det
          LEFT JOIN rol USING (day, logic_version)
          LEFT JOIN com USING (day, logic_version)
         ORDER BY 1, 2;
    """


def _diagnose_trailing(row: list[str]) -> list[str]:
    """Какие из трёх сравнений trailing не сошлись — с числами. Пусто — всё сошлось."""
    n, rol, com = _int(row[2]), _int(row[3]), _int(row[4])
    sm_det, sm_com = _int(row[5]), _int(row[6])
    failed = []
    if n != rol:
        failed.append(f"строк деталей {n} ≠ строк в свёртке {rol}")
    if n != com:
        failed.append(f"строк деталей {n} ≠ строк в компактной таблице {com}")
    if sm_det != sm_com:
        failed.append(f"сумма итогов: в деталях {sm_det} ≠ в компактной таблице {sm_com}")
    return failed


def _diagnose_strategy(row: list[str]) -> list[str]:
    """То же для strategy: свёртка, число пар, сумма итогов."""
    n, rol = _int(row[2]), _int(row[3])
    pairs_det, pairs_com = _int(row[4]), _int(row[5])
    sm_det, sm_com = _int(row[6]), _int(row[7])
    failed = []
    if n != rol:
        failed.append(f"строк деталей {n} ≠ строк в свёртке {rol}")
    if pairs_det != pairs_com:
        failed.append(f"пар в деталях {pairs_det} ≠ пар в компактной таблице {pairs_com}")
    if sm_det != sm_com:
        failed.append(f"сумма итогов: в деталях {sm_det} ≠ в компактной таблице {sm_com}")
    return failed


def plan_deletion(psql: Psql, table: str, *, current: int | None,
                  cutoff: date | None = None, today: date | None = None) -> Plan:
    """Считает, какие (сутки, версия) деталей можно удалить, и почему остальные нельзя."""
    today = today or datetime.now(UTC).date()
    cutoff = cutoff or settle_cutoff(today)
    plan = Plan(table=table)
    sql = _reconcile_trailing_sql(cutoff) if table == TRAILING else _reconcile_strategy_sql(cutoff)
    for row in _rows(psql(sql)):
        day = date.fromisoformat(row[0])
        version = _int(row[1])
        n = _int(row[2])
        allowed = _rule_allows(table, version, current)
        total, doomed = plan.per_version.get(version, (0, 0))
        if not allowed:
            plan.kept_by_rule += n
            plan.per_version[version] = (total + n, doomed)
            continue
        failed = (_diagnose_trailing(row) if table == TRAILING
                  else _diagnose_strategy(row))
        ok = not failed
        why = "; ".join(failed)
        if ok:
            plan.eligible.append(DayVersion(day, version, n))
            plan.per_version[version] = (total + n, doomed + n)
        else:
            plan.mismatched.append((day, version, why))
            plan.per_version[version] = (total + n, doomed)
    # Строки по суткам, которые ещё не завершены (позже cutoff) — не в этом отчёте.
    plan.total_rows = _int(psql(f"SELECT count(*) FROM {table};"))
    counted = sum(t for t, _ in plan.per_version.values())
    plan.unrolled = max(plan.total_rows - counted, 0)
    return plan


def delete_details(psql: Psql, plan: Plan, *, batch: int, pause: float) -> int:
    """Удаляет детали по плану (только сошедшиеся сутки). Возвращает число строк."""
    total = 0
    for item in plan.eligible:
        a, b = day_range(item.day)
        if plan.table == TRAILING:
            sql = (f"DELETE FROM {TRAILING} WHERE ctid IN (SELECT t.ctid FROM {TRAILING} t "
                   f"JOIN signals s ON s.id = t.signal_id WHERE s.ts >= {a} AND s.ts < {b} "
                   f"AND t.logic_version = {int(item.logic_version)} LIMIT {int(batch)});")
        else:
            sql = (f"DELETE FROM {STRATEGY} WHERE ctid IN (SELECT ctid FROM {STRATEGY} "
                   f"WHERE entry_ts >= {a} AND entry_ts < {b} "
                   f"AND logic_version = {int(item.logic_version)} LIMIT {int(batch)});")
        while True:
            out = psql(sql)
            deleted = _int(out.split()[-1]) if out.startswith("DELETE") else 0
            total += deleted
            if deleted < batch:
                break
            time.sleep(pause)
    return total


def is_armed(psql: Psql) -> bool:
    out = psql(f"SELECT value FROM {STATE_TABLE} WHERE key = 'delete_armed';").strip()
    return out == "1"


def arm(psql: Psql) -> None:
    psql(f"INSERT INTO {STATE_TABLE} (key, value) VALUES ('delete_armed', '1') "
         "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = now();")


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def _plans(psql: Psql) -> tuple[int | None, dict[str, Plan]]:
    current = current_logic_version(psql)
    plans = {t: plan_deletion(psql, t, current=current)
             for t in (TRAILING, STRATEGY)}
    return current, plans


def _mb(value: str) -> str:
    return f"{int(value) / 1024 / 1024:.0f} МБ"


def _size_line(psql: Psql, table: str) -> str:
    row = _rows(psql(
        f"SELECT pg_relation_size('{table}'), pg_indexes_size('{table}'), "
        f"pg_total_relation_size('{table}'), "
        f"pg_size_pretty(pg_total_relation_size('{table}'));"))[0]
    return (f"{table}: данные {_mb(row[0])}, индексы {_mb(row[1])}, "
            f"всего {_mb(row[2])} ({row[3]})")


def report_plans(psql: Psql, plans: dict[str, Plan], current: int | None) -> None:
    for table, plan in plans.items():
        _log(f"--- {table}: строк всего {plan.total_rows}; к удалению {plan.delete_rows}; "
             f"по правилу остаются {plan.kept_by_rule}; "
             f"строк в несвёрнутых сутках {plan.unrolled} ---")
        for version in sorted(plan.per_version):
            total, doomed = plan.per_version[version]
            mark = " (текущая)" if version == current else ""
            _log(f"    версия {version}{mark}: всего {total}, к удалению {doomed}")
        for day, version, why in plan.mismatched:
            _log(f"    НЕ СОШЛОСЬ (удаление пропущено): {day} v{version}: {why}")


def dry_run(psql: Psql, *, cap_days: int | None = None) -> dict[str, Plan]:
    """Режим «только посчитать, не удалять» (D.3.3): свёртка + отчёт, удаления нет."""
    if not tables_exist(psql):
        raise MeasureError("миграция 029 не применена — нужны таблицы свёртки")
    _log("=== Замеры: прогон «только счёт» — ничего не удаляется ===")
    for table in (TRAILING, STRATEGY):
        res = rollup(psql, table)
        _log(f"{table}: свёрнуто суток {len(res.days)}; строк итогов +{res.daily_rows}; "
             f"компактных пар +{res.compact_rows}")
    current, plans = _plans(psql)
    _log(f"Текущая версия логики: {current}. {retention_days_note(cap_days)}")
    report_plans(psql, plans, current)
    for t in (TRAILING, STRATEGY, *DERIVED_TABLES):
        _log("размер: " + _size_line(psql, t))
    for t in (TRAILING_DAILY, STRATEGY_DAILY, TRAILING_COMPACT, STRATEGY_COMPACT):
        _log(f"строк в {t}: {_int(psql(f'SELECT count(*) FROM {t};'))}")
    _log("Для удаления подтвердите числами: retention.py --measure-delete "
         f"--confirm-trailing {plans[TRAILING].delete_rows} "
         f"--confirm-strategy {plans[STRATEGY].delete_rows}")
    return plans


NOT_APPLIED_NOTE = ("не применяется; граница хранения определяется версией логики, "
                    "решение 29.09.2026")


def retention_days_note(cap_days: int | None) -> str:
    """Строка журнала о ``MEASURE_DETAIL_RETENTION_DAYS``: значение и «не применяется»."""
    shown = "не задано" if cap_days is None else str(cap_days)
    return f"MEASURE_DETAIL_RETENTION_DAYS={shown} ({NOT_APPLIED_NOTE})"


def refusal_message(plans: dict[str, Plan], want: dict[str, int]) -> str:
    """Диагностика отказа: КАКОЕ сравнение не сошлось и с какими числами.

    Причин две, и они различаются. Расхождение сверки — сутки, у которых не сошлись
    строки деталей / свёртки / компактной таблицы или сумма итогов (перечень ниже).
    Иначе число к удалению просто отличается от подтверждённого: с момента прогона
    «только счёт» сменилась дата UTC или ночная свёртка добавила сутки.
    """
    lines = [f"числа подтверждения не совпали с расчётом (граница завершённых суток: "
             f"{settle_cutoff()}):"]
    for table, plan in plans.items():
        lines.append(f"  {table}: подтверждено {want[table]}, сейчас к удалению {plan.delete_rows}"
                     + ("" if plan.delete_rows != want[table] else " (совпало)"))
        for day, version, why in plan.mismatched[:10]:
            lines.append(f"    сверка не сошлась: сутки {day}, версия {version}: {why}")
        if len(plan.mismatched) > 10:
            lines.append(f"    … и ещё суток с расхождением: {len(plan.mismatched) - 10}")
    if any(plan.mismatched for plan in plans.values()):
        lines.append("Причина: расхождение сверки (сутки выше в удаление не входят). Это НЕ "
                     "штатная смена даты — сверка в базе нарушена; удаление не выполняется, "
                     "разберитесь до повторного прогона.")
    else:
        lines.append("Расхождений сверки нет: изменилось лишь множество завершённых суток "
                     "(смена даты UTC или ночная свёртка). Повторите прогон «только счёт» и "
                     "подтвердите новые числа.")
    return "\n".join(lines)


def delete_command(psql: Psql, *, confirm_trailing: int, confirm_strategy: int,
                   batch: int, pause: float, cron_dir: str = "/etc/cron.d") -> int:
    """Первое (ручное) удаление деталей. Возвращает 0 — успех.

    Ограждения: писатели сняты с cron (читается фактическое расписание); числа
    подтверждения совпали с расчётом; счёт сошёлся по каждым суткам (иначе эти сутки
    пропускаются). Свёртка здесь НЕ выполняется: между слепком «до» и удалением
    состав свёртки не должен меняться.
    """
    if not tables_exist(psql):
        raise MeasureError("миграция 029 не применена — нужны таблицы свёртки")
    active = writers_active(cron_dir)
    if active is None:
        raise MeasureError(f"расписание {cron_dir} прочитать не удалось — удаление отклонено")
    running = [k for k, v in active.items() if v]
    if running:
        raise MeasureError("в cron остались писатели замеров: " + ", ".join(running)
                           + " — снимите их (docs/STAGE_9_4_REPORT.md, блок D)")
    current, plans = _plans(psql)
    if current is None:
        raise MeasureError("текущая версия логики не определена (logic_version_windows пуста)")
    report_plans(psql, plans, current)
    want = {TRAILING: confirm_trailing, STRATEGY: confirm_strategy}
    if any(plan.delete_rows != want[table] for table, plan in plans.items()):
        raise MeasureError(refusal_message(plans, want))
    total = 0
    for table, plan in plans.items():
        n = delete_details(psql, plan, batch=batch, pause=pause)
        _log(f"{table}: удалено строк {n}")
        total += n
    arm(psql)
    _log(f"Удалено всего {total}. Ночное удаление разрешено (delete_armed=1). "
         "Место на диске ещё не возвращено — далее retention.py --measure-compact.")
    return 0


def nightly(psql: Psql, *, cron_dir: str = "/etc/cron.d",
            batch: int, pause: float) -> bool:
    """Ночной шаг: свёртка всегда; удаление — только если разрешено (флаг + писатели).

    Возвращает False при ошибке (вызывающий выставит код 1). Отсутствие таблиц
    миграции 029 — не ошибка: шаг пропускается с сообщением.
    """
    if not tables_exist(psql):
        _log("Замеры: миграция 029 не применена — свёртка замеров пропущена.")
        return True
    ok = True
    rolled: dict[str, bool] = {}
    for table in (TRAILING, STRATEGY):
        try:
            res = rollup(psql, table)
            _log(f"{table}: свёрнуто суток {len(res.days)}; строк итогов +{res.daily_rows}; "
                 f"компактных пар +{res.compact_rows}")
            rolled[table] = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            rolled[table] = False
            _log(f"ОШИБКА свёртки {table}: {(getattr(exc, 'stderr', '') or '').strip() or exc}")
    if not is_armed(psql):
        _log("Замеры: удаление деталей не разрешено (первое удаление — командой "
             "--measure-delete после подтверждения). Детали сохранены.")
        return ok
    current = current_logic_version(psql)
    active = writers_active(cron_dir)
    for table in (TRAILING, STRATEGY):
        if not rolled[table]:
            _log(f"{table}: удаление ПРОПУЩЕНО — свёртка не выполнена.")
            continue
        if table == TRAILING and (active is None or active["trailing_main"]):
            _log("trailing_outcomes: удаление ПРОПУЩЕНО — писатель trailing_main не снят "
                 "с cron (или cron не прочитан).")
            continue
        try:
            plan = plan_deletion(psql, table, current=current)
            for day, version, why in plan.mismatched:
                _log(f"{table}: сутки {day} v{version} НЕ СОШЛИСЬ, пропущены: {why}")
            deleted = delete_details(psql, plan, batch=batch, pause=pause)
            _log(f"{table}: удалено строк деталей: {deleted}")
            if deleted > 0:
                psql(f"VACUUM (ANALYZE, PARALLEL 0) {table};")
        except Exception as exc:  # noqa: BLE001
            ok = False
            _log(f"ОШИБКА удаления {table}: {(getattr(exc, 'stderr', '') or '').strip() or exc}")
    return ok


# --- Слепок, размеры, уплотнение -------------------------------------------

_PK_ORDER = {
    TRAILING_DAILY: "day, instrument_id, logic_version, horizon_h, activation_ratio, retrace_ratio",
    STRATEGY_DAILY: "day, instrument_id, logic_version, strategy, horizon_h",
    TRAILING_COMPACT: "signal_id, horizon_h",
    STRATEGY_COMPACT: "signal_id, horizon_h, logic_version",
}


def snapshot(psql: Psql, path: str, upto: date) -> dict[str, str]:
    """Слепок свёрток по сутки ``<= upto`` в файл; возвращает sha256 каждой таблицы.

    Сравнение «до» и «после» — побайтное (``cmp`` двух файлов). Верхняя граница
    ``upto`` задаётся явно: ночная свёртка между слепками вправе добавить более
    поздние сутки, и в слепок они входить не должны.
    """
    digests: dict[str, str] = {}
    lines: list[str] = [f"# слепок свёрток; сутки <= {upto.isoformat()}"]
    for table in DERIVED_TABLES:
        data = psql(f"COPY (SELECT * FROM {table} WHERE day <= '{upto.isoformat()}' "
                    f"ORDER BY {_PK_ORDER[table]}) TO STDOUT;")
        lines.append(f"## {table}")
        lines.append(data)
        digests[table] = hashlib.sha256(data.encode("utf-8")).hexdigest()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return digests


def disk_avail_bytes() -> int:
    out = subprocess.run(["df", "-P", "-B1", "/"], capture_output=True, text=True, check=True)
    return int(out.stdout.splitlines()[1].split()[3])


def disk_used_free_gb() -> tuple[float, float]:
    out = subprocess.run(["df", "-P", "-B1", "/"], capture_output=True, text=True, check=True)
    parts = out.stdout.splitlines()[1].split()
    return int(parts[2]) / 1024**3, int(parts[3]) / 1024**3


def sizes(psql: Psql, label: str, log_path: str | None = None) -> str:
    """Три точки замера места (до / после удаления / после уплотнения): числами."""
    used, free = disk_used_free_gb()
    lines = [f"[{label}] df: занято {used:.2f} ГБ, свободно {free:.2f} ГБ"]
    for table in (TRAILING, STRATEGY):
        lines.append(f"[{label}] {_size_line(psql, table)}")
    text = "\n".join(lines)
    _log(text)
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(UTC):%F %T} {text}\n")
    return text


def dependents_report(psql: Psql) -> dict[str, int]:
    """Представления, триггеры, функции и внешние ссылки на обе таблицы (должно быть 0)."""
    both = f"'{TRAILING}'::regclass, '{STRATEGY}'::regclass"
    queries = {
        "представления": (
            f"SELECT count(DISTINCT c.oid) FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid "
            f"JOIN pg_class c ON c.oid = r.ev_class WHERE d.refobjid IN ({both}) "
            "AND c.oid <> d.refobjid;"),
        "триггеры": (f"SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                     f"AND tgrelid IN ({both});"),
        "функции": ("SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
                    f"AND (prosrc ILIKE '%{TRAILING}%' OR prosrc ILIKE '%{STRATEGY}%');"),
        "внешние ссылки на таблицы": (f"SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                                      f"AND confrelid IN ({both});"),
        "активные запросы к таблицам": (
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
            "AND state <> 'idle' AND datname = current_database() "
            f"AND (query ILIKE '%{TRAILING}%' OR query ILIKE '%{STRATEGY}%');"),
    }
    return {name: _int(psql(sql)) for name, sql in queries.items()}


def compact(psql: Psql, *, cron_dir: str = "/etc/cron.d", reserve_gb: float = 1.0,
            avail_bytes: Callable[[], int] = disk_avail_bytes) -> int:
    """Уплотнение (``VACUUM FULL``) обеих таблиц: возвращает место операционной системе.

    ``DELETE`` файл таблицы не сжимает — без этого шага ``df`` останется прежним.
    Перед каждой таблицей проверяется РЕАЛЬНОЕ свободное место (нужна копия таблицы с
    индексами + запас), писатели в cron, отсутствие зависимых объектов и активных
    запросов: ``VACUUM FULL`` берёт исключительную блокировку.
    """
    active = writers_active(cron_dir)
    if active is None or any(active.values()):
        raise MeasureError("писатели замеров не сняты с cron (или cron не прочитан) — "
                           "уплотнение отклонено")
    deps = dependents_report(psql)
    _log("Зависимости и активность: " + "; ".join(f"{k}: {v}" for k, v in deps.items()))
    if any(deps.values()):
        raise MeasureError("у таблиц есть зависимые объекты или активные запросы — "
                           "исключительная блокировка небезопасна")
    for table in (TRAILING, STRATEGY):
        total = _int(psql(f"SELECT pg_total_relation_size('{table}');"))
        need = total + int(reserve_gb * 1024**3)
        have = avail_bytes()
        _log(f"{table}: нужно свободных {need / 1024**3:.2f} ГБ (таблица с индексами "
             f"{total / 1024**3:.2f} ГБ + запас {reserve_gb:.1f} ГБ), "
             f"свободно {have / 1024**3:.2f} ГБ")
        if have < need:
            raise MeasureError(f"{table}: свободного места {have} байт меньше необходимого {need}")
        _log(_size_line(psql, table) + "  ← до уплотнения")
        psql("SET lock_timeout = '60s';", f"VACUUM (FULL, ANALYZE) {table};")
        _log(_size_line(psql, table) + "  ← после уплотнения")
    return 0
