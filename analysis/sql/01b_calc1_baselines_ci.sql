-- Этап 7.1 — Расчёт 1 (продолжение): три базовые линии + доверительный интервал.
-- Те же независимые окна, что и в 01_calc1_independent.sql.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/01b_calc1_baselines_ci.sql

WITH ev AS (
    SELECT signal_id, pnl_pct, price_at_signal, price_at_close, (pnl_pct > 0) AS success_4h
    FROM signal_evaluations WHERE horizon = '4h'
),
base AS (
    SELECT s.id, s.ts, s.decision,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win_start,
           e.success_4h,
           (e.price_at_close > e.price_at_signal) AS price_up
    FROM signals s JOIN ev e ON e.signal_id = s.id
    WHERE COALESCE(s.logic_version, 1) = 1 AND s.decision IN ('buy', 'sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts)

-- 1.4 — Три базовые линии на одних и тех же окнах:
--   always_buy  = доля окон, где цена через 4ч ВЫШЕ цены на начало (price_up),
--   always_sell = обратное,
--   system      = фактическая доля успеха системы.
SELECT
    count(*)                                          AS n_windows,
    round(avg(price_up::int)::numeric, 4)            AS always_buy_success,
    round(avg((NOT price_up)::int)::numeric, 4)      AS always_sell_success,
    round(avg(success_4h::int)::numeric, 4)          AS system_success
FROM iw;

-- 1.5 — Доля успеха и 95% доверительный интервал (Wilson score — корректен при малом N).
-- z = 1.959964 (двусторонний 95%). Wilson предпочтителен Wald'у при N≈30.
WITH ev AS (
    SELECT signal_id, (pnl_pct > 0) AS success_4h FROM signal_evaluations WHERE horizon = '4h'
),
base AS (
    SELECT s.id, s.ts, s.decision,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win_start,
           e.success_4h
    FROM signals s JOIN ev e ON e.signal_id = s.id
    WHERE COALESCE(s.logic_version, 1) = 1 AND s.decision IN ('buy', 'sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
agg AS (
    SELECT grp, count(*)::numeric AS n, avg(success_4h::int)::numeric AS p
    FROM (
        SELECT 'all' AS grp, * FROM iw
        UNION ALL SELECT 'buy', * FROM iw WHERE decision = 'buy'
        UNION ALL SELECT 'sell', * FROM iw WHERE decision = 'sell'
    ) q GROUP BY grp
),
c AS (SELECT 1.959964::numeric AS z, (1.959964 * 1.959964)::numeric AS z2)
SELECT
    grp, n::int AS n, round(p, 4) AS success_rate,
    round(((p + z2 / (2 * n) - z * sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / (1 + z2 / n)), 4) AS wilson_low,
    round(((p + z2 / (2 * n) + z * sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / (1 + z2 / n)), 4) AS wilson_high,
    round((p - z * sqrt(p * (1 - p) / n)), 4) AS wald_low,
    round((p + z * sqrt(p * (1 - p) / n)), 4) AS wald_high
FROM agg, c
ORDER BY grp;
