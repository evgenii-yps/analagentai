-- Этап 7.1 — Расчёт 3: калибровочная таблица.
-- Для каждого диапазона probability: заявленная вероятность (avg probability)
-- против фактической доли успеха (avg success_4h). При корректной калибровке
-- claimed ≈ actual. Только независимые окна, logic_version=1.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/06_calc3_calibration.sql

WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (
    SELECT s.id, s.ts, s.probability, e.success_4h,
           to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start
    FROM signals s JOIN ev e ON e.signal_id=s.id
    WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
binned AS (SELECT *, width_bucket(probability, 0, 1.0000001, 5) AS b FROM iw WHERE probability IS NOT NULL)
SELECT
    CASE b WHEN 1 THEN '0.0-0.2' WHEN 2 THEN '0.2-0.4' WHEN 3 THEN '0.4-0.6'
           WHEN 4 THEN '0.6-0.8' WHEN 5 THEN '0.8-1.0' ELSE '??' END AS prob_band,
    count(*)                                       AS n,
    round(avg(probability)::numeric, 4)            AS claimed_prob,
    round(avg(success_4h::int)::numeric, 4)        AS actual_success,
    round((avg(probability) - avg(success_4h::int))::numeric, 4) AS gap_claimed_minus_actual
FROM binned
GROUP BY b
ORDER BY b;
