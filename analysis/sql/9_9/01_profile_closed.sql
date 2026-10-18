-- ЭТАП 9.9, блок 01 — профиль результата по закрытым сделкам. Только чтение.
-- Что показывает: сколько сделок в плюс и в минус, каков размер средней прибыли против
-- среднего убытка, чем кончаются худшие сделки и каков итог на сделку (с интервалом по дням).

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
, a AS (
    SELECT count(*) AS n,
           count(DISTINCT (opened_at AT TIME ZONE 'UTC')::date) AS n_eff,   -- различные дни открытия
           count(*) FILTER (WHERE cls = 'win')  AS n_win,
           count(*) FILTER (WHERE cls = 'loss') AS n_loss,
           count(*) FILTER (WHERE cls = 'flat') AS n_flat,
           avg(net_pnl_pct)                                AS mean,
           stddev_samp(net_pnl_pct)                        AS sd,
           avg(net_pnl_pct) FILTER (WHERE cls = 'win')     AS avg_win,
           avg(net_pnl_pct) FILTER (WHERE cls = 'loss')    AS avg_loss,
           sum(net_pnl_pct) FILTER (WHERE cls = 'win')     AS sum_win,
           sum(net_pnl_pct) FILTER (WHERE cls = 'loss')    AS sum_loss,
           sum(net_pnl_usd) FILTER (WHERE cls = 'win')     AS usd_win,
           sum(net_pnl_usd) FILTER (WHERE cls = 'loss')    AS usd_loss,
           sum(net_pnl_usd)                                AS usd,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY net_pnl_pct) AS med,
           percentile_cont(0.05) WITHIN GROUP (ORDER BY net_pnl_pct) AS p05,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY net_pnl_pct) AS p95,
           max(net_pnl_pct)                                AS best,
           (array_agg(id ORDER BY net_pnl_pct DESC, id))[1] AS best_id,
           min(net_pnl_pct)                                AS worst,
           (array_agg(id ORDER BY net_pnl_pct ASC, id))[1]  AS worst_id
      FROM closed
),
g AS (                              -- закрытия data_gap: отдельной строкой, в метрики не входят
    SELECT count(*) AS n, avg(net_pnl_pct) AS mean
      FROM v7
     WHERE status = 'closed' AND exit_reason = 'data_gap'
)
SELECT
    a.n                                                         AS "Закрытых сделок (n)",
    a.n_eff                                                     AS "Различных дней открытия (n_eff)",
    round(100.0 * a.n_win  / NULLIF(a.n, 0), 2)                 AS "Доля прибыльных, %",
    round(100.0 * a.n_loss / NULLIF(a.n, 0), 2)                 AS "Доля убыточных, %",
    round(100.0 * a.n_flat / NULLIF(a.n, 0), 2)                 AS "Доля «в ноль», %",
    round(a.avg_win::numeric, 2)                                AS "Средняя прибыль, %",
    round(a.avg_loss::numeric, 2)                               AS "Средний убыток, %",
    round((a.avg_win / NULLIF(abs(a.avg_loss), 0))::numeric, 2) AS "Payoff (ср. прибыль / ср. убыток)",
    round((a.sum_win / NULLIF(abs(a.sum_loss), 0))::numeric, 2) AS "Фактор прибыли (по %)",
    round((a.usd_win / NULLIF(abs(a.usd_loss), 0))::numeric, 2) AS "Фактор прибыли (по $)",
    round(a.mean::numeric, 2)                                   AS "Средний итог на сделку, %",
    round((a.mean - 1.96 * a.sd / sqrt(NULLIF(a.n_eff, 0)))::numeric, 2)
                                                                AS "  интервал, нижняя граница, %",
    round((a.mean + 1.96 * a.sd / sqrt(NULLIF(a.n_eff, 0)))::numeric, 2)
                                                                AS "  интервал, верхняя граница, %",
    round(a.med::numeric, 2)                                    AS "Медиана итога, %",
    round(a.best::numeric, 2)                                   AS "Лучшая сделка, %",
    a.best_id                                                   AS "  позиция №",
    round(a.worst::numeric, 2)                                  AS "Худшая сделка, %",
    a.worst_id                                                  AS "  позиция №",
    round(a.p05::numeric, 2)                                    AS "5-й процентиль, %",
    round(a.p95::numeric, 2)                                    AS "95-й процентиль, %",
    round(a.usd::numeric, 2)                                    AS "Сумма итога, $",
    g.n                                                         AS "Справочно: data_gap, число (вне метрик)",
    round(g.mean::numeric, 2)                                   AS "Справочно: data_gap, средний итог, %"
FROM a, g;
