-- ЭТАП 9.9, блок 06 — время в рынке. Только чтение.
-- Что показывает: сколько позиций в среднем и максимум открыто одновременно и как часто
-- система вообще не держала позиций (по всем монетам и по каждой).
-- Сетка: каждый целый час от первого часа после открытия первой позиции до последнего
-- целого часа перед моментом замера; позиция считается открытой в момент часа t, если
-- opened_at <= t и она не закрыта (closed_at > t). В сетку входят все позиции версии 7.

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
, b AS (
    SELECT date_trunc('hour', min(opened_at) - interval '1 microsecond') + interval '1 hour' AS h0,
           date_trunc('hour', :'as_of'::timestamptz)                                          AS h1
      FROM v7
),
grid AS (SELECT generate_series(b.h0, b.h1, interval '1 hour') AS h FROM b WHERE b.h0 <= b.h1),
syms AS (SELECT DISTINCT symbol FROM v7),
cnt AS (                            -- сколько позиций по каждой монете открыто в каждый час
    SELECT g.h, s.symbol, count(v.id) AS n
      FROM grid g
     CROSS JOIN syms s
      LEFT JOIN v7 v ON v.symbol = s.symbol
                    AND v.opened_at <= g.h
                    AND (v.closed_at IS NULL OR v.closed_at > g.h)
     GROUP BY g.h, s.symbol
),
tot AS (SELECT h, sum(n) AS n FROM cnt GROUP BY h)
SELECT lbl AS "Монета", hours_n AS "Часов в сетке", round(avg_n::numeric, 2) AS "Ср. одновременно",
       max_n AS "Максимум", round(100.0 * empty_n / NULLIF(hours_n, 0), 2) AS "Часов без позиций, %"
  FROM (
        SELECT 0 AS ord, 'ВСЕ МОНЕТЫ' AS lbl, count(*) AS hours_n, avg(n) AS avg_n, max(n) AS max_n,
               count(*) FILTER (WHERE n = 0) AS empty_n
          FROM tot
        UNION ALL
        SELECT 1, symbol, count(*), avg(n), max(n), count(*) FILTER (WHERE n = 0)
          FROM cnt
         GROUP BY symbol
       ) t
 ORDER BY ord, lbl;
