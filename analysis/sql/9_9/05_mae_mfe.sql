-- ЭТАП 9.9, блок 05 — ход цены внутри сделки. Только чтение.
-- Что показывает: насколько цена уходила против позиции (MAE, число ≤ 0) и в её пользу
-- (MFE, число ≥ 0) и как часто убыточные сделки были в плюсе хотя бы на полцели.

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
SELECT CASE WHEN GROUPING(cls) = 1 THEN 'все'
            WHEN cls = 'win' THEN 'прибыльные'
            WHEN cls = 'loss' THEN 'убыточные'
            ELSE 'в ноль' END                                  AS "Группа",
       count(*)                                                AS "n",
       round(avg(mae_pct)::numeric, 2)                         AS "MAE ср., %",
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY mae_pct))::numeric, 2)
                                                               AS "MAE мед., %",
       round(avg(mfe_pct)::numeric, 2)                         AS "MFE ср., %",
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY mfe_pct))::numeric, 2)
                                                               AS "MFE мед., %",
       count(*) FILTER (WHERE mfe_pct >= target_pct / 2)       AS "MFE ≥ ½ цели, шт",
       round(100.0 * count(*) FILTER (WHERE mfe_pct >= target_pct / 2) / NULLIF(count(*), 0), 2)
                                                               AS "MFE ≥ ½ цели, %"
  FROM closed
 GROUP BY GROUPING SETS ((), (cls))
 ORDER BY GROUPING(cls) DESC, CASE cls WHEN 'win' THEN 1 WHEN 'loss' THEN 2 ELSE 3 END;
