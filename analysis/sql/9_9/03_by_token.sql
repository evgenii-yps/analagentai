-- ЭТАП 9.9, блок 03 — по монетам. Только чтение.
-- Что показывает: одинаково ли ведут себя пять монет или результат держится на одной-двух.

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
, c AS (
    SELECT symbol,
           count(*)                                          AS n,
           count(*) FILTER (WHERE cls = 'win')               AS n_win,
           avg(net_pnl_pct)                                  AS mean,
           min(net_pnl_pct)                                  AS worst,
           sum(net_pnl_usd)                                  AS usd
      FROM closed
     GROUP BY ROLLUP (symbol)
),
o AS (
    SELECT symbol,
           count(*)                                          AS n_open,
           count(cur_pct)                                    AS n_px,
           avg(cur_pct)                                      AS cur
      FROM opened
     GROUP BY ROLLUP (symbol)
),
s AS (SELECT DISTINCT symbol FROM v7 UNION ALL SELECT NULL::text)
SELECT coalesce(s.symbol, 'ВСЕ')                                   AS "Монета",
       coalesce(c.n, 0)                                            AS "Закрыто",
       round(100.0 * c.n_win / NULLIF(c.n, 0), 2)                  AS "Прибыльных, %",
       round(c.mean::numeric, 2)                                   AS "Ср. итог, %",
       round(c.worst::numeric, 2)                                  AS "Худшая, %",
       round(c.usd::numeric, 2)                                    AS "Сумма, $",
       coalesce(o.n_open, 0)                                       AS "Открыто",
       coalesce(o.n_px, 0)                                         AS "С ценой",
       round(o.cur::numeric, 2)                                    AS "Тек. итог откр., %"
  FROM s
  LEFT JOIN c ON c.symbol IS NOT DISTINCT FROM s.symbol
  LEFT JOIN o ON o.symbol IS NOT DISTINCT FROM s.symbol
 ORDER BY s.symbol NULLS LAST;
