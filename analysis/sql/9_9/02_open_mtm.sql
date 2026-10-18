-- ЭТАП 9.9, блок 02 — открытые позиции по текущей цене и общий итог. Только чтение.
-- Что показывает: сколько «висит» в минусе прямо сейчас и насколько итог закрытых сделок
-- завышен тем, что неудачные позиции ещё не закрыты (оценка «отбора выживших»).

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
\x on
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
, o AS (
    SELECT count(*)                                  AS n_open,
           count(cur_pct)                            AS n_px,
           avg(cur_pct)                              AS mean,
           min(cur_pct)                              AS worst,
           count(*) FILTER (WHERE cur_pct < -3)      AS d3,
           count(*) FILTER (WHERE cur_pct < -5)      AS d5,
           count(*) FILTER (WHERE cur_pct < -10)     AS d10,
           sum(cur_usd)                              AS usd
      FROM opened
),
allp AS (                           -- закрытые + открытые с ценой: тот же набор, что в блоке 01
    SELECT net_pnl_pct AS pct, net_pnl_usd AS usd FROM closed
    UNION ALL
    SELECT cur_pct, cur_usd FROM opened WHERE cur_pct IS NOT NULL
),
a AS (
    SELECT count(*)                                         AS n,
           count(*) FILTER (WHERE pct >  0.0001)            AS n_win,
           count(*) FILTER (WHERE pct < -0.0001)            AS n_loss,
           count(*) FILTER (WHERE pct BETWEEN -0.0001 AND 0.0001) AS n_flat,
           avg(pct)                                         AS mean,
           sum(usd)                                         AS usd
      FROM allp
),
c AS (SELECT avg(net_pnl_pct) AS mean FROM closed)
SELECT
    o.n_open                                                    AS "Открыто позиций",
    o.n_px                                                      AS "  с текущей ценой",
    o.n_open - o.n_px                                           AS "  без цены (нет свежей свечи)",
    round(o.mean::numeric, 2)                                   AS "Средний текущий итог открытых, %",
    round(o.worst::numeric, 2)                                  AS "Худший текущий итог открытых, %",
    o.d3                                                        AS "Открытых глубже −3 %",
    o.d5                                                        AS "Открытых глубже −5 %",
    o.d10                                                       AS "Открытых глубже −10 %",
    round(o.usd::numeric, 2)                                    AS "Сумма текущего итога открытых, $",
    a.n                                                         AS "Закрытые + открытые с ценой (n)",
    round(100.0 * a.n_win  / NULLIF(a.n, 0), 2)                 AS "  доля прибыльных, %",
    round(100.0 * a.n_loss / NULLIF(a.n, 0), 2)                 AS "  доля убыточных, %",
    round(100.0 * a.n_flat / NULLIF(a.n, 0), 2)                 AS "  доля «в ноль», %",
    round(a.mean::numeric, 2)                                   AS "  средний итог, %",
    round(a.usd::numeric, 2)                                    AS "  сумма итога, $",
    round(c.mean::numeric, 2)                                   AS "Средний итог только закрытых, %",
    round((a.mean - c.mean)::numeric, 2)                        AS "Разница «общий − закрытые», п.п."
FROM o, a, c;
