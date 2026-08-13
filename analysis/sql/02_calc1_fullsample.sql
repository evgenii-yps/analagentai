-- Этап 7.1 — Расчёт 1 (справочно): ПОЛНАЯ выборка (все ~5623 закрытых сигнала).
-- ВНИМАНИЕ: наблюдения ЗАВИСИМЫ (соседние сигналы описывают почти один и тот же
-- отрезок рынка). Доверительные интервалы к этим цифрам НЕПРИМЕНИМЫ — приводится
-- только для сопоставления с независимой выборкой (§5 ТЗ).
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/02_calc1_fullsample.sql

WITH ev AS (
    SELECT signal_id, pnl_pct, (pnl_pct > 0) AS success_4h
    FROM signal_evaluations WHERE horizon = '4h'
),
base AS (
    SELECT s.id, s.decision, e.pnl_pct, e.success_4h
    FROM signals s JOIN ev e ON e.signal_id = s.id
    WHERE COALESCE(s.logic_version, 1) = 1 AND s.decision IN ('buy', 'sell')
)
SELECT
    grp,
    count(*)                                                        AS n_signals,
    count(*) FILTER (WHERE success_4h)                              AS successes,
    round(avg(success_4h::int)::numeric, 4)                        AS success_rate,
    round(avg(pnl_pct)::numeric, 5)                                AS mean_pnl_4h,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct))::numeric, 5) AS median_pnl_4h
FROM (
    SELECT 'all'  AS grp, * FROM base
    UNION ALL SELECT 'buy',  * FROM base WHERE decision = 'buy'
    UNION ALL SELECT 'sell', * FROM base WHERE decision = 'sell'
) q
GROUP BY grp
ORDER BY grp;
