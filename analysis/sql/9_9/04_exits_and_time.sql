-- ЭТАП 9.9, блок 04 — выходы и время в сделке. Только чтение.
-- Что показывает: чем заканчиваются сделки (цель, плюс после суток, срок) и как долго держатся.

\pset pager off
\pset null '-'
\pset footer off
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;   -- страховка: сессия не может писать
SET statement_timeout = '600s';
SET TIME ZONE 'UTC';
-- Момент замера. run_9_9.sh передаёт его ключом -v as_of=..., один на все блоки;
-- при ручном запуске файла отдельно берётся текущее время.
\if :{?as_of}
\else
  \set as_of now
\endif
-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.9 §3 (одинаковы во всех файлах; сверяет tests/test_stage_9_9.py) ===
WITH v7 AS (                        -- выборка: версия логики 7, виртуальные позиции
    SELECT p.id, p.instrument_id, i.symbol, p.status, p.opened_at, p.closed_at,
           p.entry_price, p.cost_pct, p.target_pct, p.notional_usd, p.exit_reason,
           p.net_pnl_pct, p.net_pnl_usd, p.mae_pct, p.mfe_pct
      FROM positions p
      JOIN instruments i ON i.id = p.instrument_id
     WHERE p.logic_version = 7 AND p.is_virtual
),
closed AS (                         -- закрытые для метрик: без data_gap
    SELECT v7.*,
           CASE WHEN net_pnl_pct >  0.0001 THEN 'win'
                WHEN net_pnl_pct < -0.0001 THEN 'loss'
                ELSE 'flat' END AS cls,
           extract(epoch FROM closed_at - opened_at) / 3600.0 AS hours
      FROM v7
     WHERE status = 'closed' AND exit_reason IS DISTINCT FROM 'data_gap'
),
opened AS (                         -- открытые по close последней закрытой минутной свечи
    SELECT v.*, px.close AS last_close, px.ts AS last_ts,
           CASE WHEN px.ts >= :'as_of'::timestamptz - interval '10 minutes'
                THEN (px.close::numeric / v.entry_price - 1) * 100 - v.cost_pct
           END AS cur_pct,          -- NULL = «нет цены» (свечи нет или она старше 10 минут)
           CASE WHEN px.ts >= :'as_of'::timestamptz - interval '10 minutes'
                THEN v.notional_usd * ((px.close::numeric / v.entry_price - 1) * 100 - v.cost_pct) / 100
           END AS cur_usd
      FROM v7 v
      LEFT JOIN LATERAL (
            SELECT o.close, o.ts
              FROM ohlcv o
             WHERE o.instrument_id = v.instrument_id AND o.timeframe = '1m'
               AND o.ts <= :'as_of'::timestamptz - interval '1 minute'
             ORDER BY o.ts DESC
             LIMIT 1) px ON true
     WHERE v.status = 'open'
)
-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ===
SELECT lbl                                                     AS "Выход",
       n                                                       AS "Число",
       round(100.0 * n / NULLIF((SELECT count(*) FROM closed), 0), 2) AS "Доля, %",
       round(mean::numeric, 2)                                 AS "Ср. итог, %",
       round(avg_h::numeric, 2)                                AS "Ср. часов",
       round(med_h::numeric, 2)                                AS "Медиана часов"
  FROM (
        SELECT exit_reason AS lbl,
               CASE exit_reason WHEN 'target' THEN 1 WHEN 'plus_exit' THEN 2
                                WHEN 'timeout' THEN 3 ELSE 4 END AS ord,
               count(*) AS n, avg(net_pnl_pct) AS mean, avg(hours) AS avg_h,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS med_h
          FROM closed
         GROUP BY exit_reason
        UNION ALL
        SELECT 'ВСЕ', 5, count(*), avg(net_pnl_pct), avg(hours),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hours)
          FROM closed
        UNION ALL
        SELECT 'цель в первые 24 ч', 6, count(*), avg(net_pnl_pct), avg(hours),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hours)
          FROM closed
         WHERE exit_reason = 'target' AND hours <= 24
       ) t
 ORDER BY ord, lbl;
