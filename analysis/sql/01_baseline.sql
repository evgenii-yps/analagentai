-- ЭТАП 7.1, РАСЧЁТ 1 (раздел 5 ТЗ): базовая линия и общая результативность.
-- Только чтение. Ни временных таблиц, ни представлений не создаётся: сессия
-- переведена в режим read-only, каждый запрос самодостаточен (общие определения
-- повторяются в CTE, чтобы запрос можно было скопировать в отчёт целиком).
--
-- Определения (раздел 4 ТЗ):
--   * независимое окно — непересекающийся 4-часовой отрезок с границами
--     00/04/08/12/16/20 UTC (epoch кратен 14400 → границы совпадают ровно);
--     из окна берётся ПЕРВЫЙ по времени закрытый сигнал;
--   * успех — pnl_pct > 0 по горизонту 4h (знак уже учитывает направление);
--   * область данных — logic_version = 1; решения wait исключены.
--
-- ВАЖНО по схеме: в ohlcv нет таймфрейма 4h (собираются 1m,5m,15m,1h), поэтому
-- базовые линии считаются по close 1m-свечей — ровно так же, как берёт цену сам
-- оценщик (src/core/db.py: get_price_at, timeframe='1m'). Свеча ищется на/до
-- нужного момента и не старше 10 минут; иначе окно попадает в графу «нет цены».

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 1.1 Размер независимой выборки (logic_version = 1) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, s.probability,
           e.pnl_pct, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*, ps.close AS p_open, pe.close AS p_close
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT count(*)                                                   AS independent_windows,
       min(win)                                                   AS first_window_utc,
       max(win)                                                   AS last_window_utc,
       count(*) FILTER (WHERE p_open IS NULL OR p_close IS NULL)  AS windows_without_price,
       (SELECT count(*) FROM v1_eval)                             AS full_sample_closed_signals
FROM px;

\echo
\echo '--- 1.2 Доля успеха по независимой выборке: числитель и знаменатель (X из N) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.decision, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
)
SELECT 'ВСЕГО' AS bucket,
       count(*) FILTER (WHERE success) AS success_x,
       count(*)                        AS total_n,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM v1_indep
UNION ALL
SELECT decision,
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM v1_indep
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.3 pnl_4h по независимой выборке: среднее и медиана (проценты) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.decision, e.pnl_pct,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
)
SELECT 'ВСЕГО' AS bucket,
       count(*)                                                                AS n,
       round(avg(pnl_pct)::numeric, 4)                                         AS avg_pnl_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4) AS median_pnl_pct,
       round(min(pnl_pct)::numeric, 4)                                         AS min_pnl_pct,
       round(max(pnl_pct)::numeric, 4)                                         AS max_pnl_pct
FROM v1_indep
UNION ALL
SELECT decision,
       count(*),
       round(avg(pnl_pct)::numeric, 4),
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4),
       round(min(pnl_pct)::numeric, 4),
       round(max(pnl_pct)::numeric, 4)
FROM v1_indep
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.4 Три базовые линии на ТЕХ ЖЕ независимых окнах (X из N) ---'
\echo '(«всегда buy»/«всегда sell» — по ohlcv 1m: close начала окна против close через 4 часа)'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT 'всегда buy (цена выросла)' AS strategy,
       count(*) FILTER (WHERE move_pct > 0)          AS success_x,
       count(*) FILTER (WHERE move_pct IS NOT NULL)  AS total_n,
       round(100.0 * count(*) FILTER (WHERE move_pct > 0)
             / NULLIF(count(*) FILTER (WHERE move_pct IS NOT NULL), 0), 2) AS success_pct
FROM px
UNION ALL
SELECT 'всегда sell (цена упала)',
       count(*) FILTER (WHERE move_pct < 0),
       count(*) FILTER (WHERE move_pct IS NOT NULL),
       round(100.0 * count(*) FILTER (WHERE move_pct < 0)
             / NULLIF(count(*) FILTER (WHERE move_pct IS NOT NULL), 0), 2)
FROM px
UNION ALL
SELECT 'фактический результат системы',
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM px;

\echo
\echo '--- 1.5 Движение рынка в независимых окнах (справочно к базовым линиям) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT count(*)                                                                 AS windows_with_price,
       round(avg(move_pct)::numeric, 4)                                         AS avg_move_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY move_pct)::numeric, 4) AS median_move_pct,
       count(*) FILTER (WHERE move_pct > 0)                                     AS up_windows,
       count(*) FILTER (WHERE move_pct < 0)                                     AS down_windows,
       count(*) FILTER (WHERE move_pct = 0)                                     AS flat_windows
FROM px
WHERE move_pct IS NOT NULL;

\echo
\echo '--- 1.6 Независимые окна поштучно (проверяемость выборки) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, s.probability, e.pnl_pct, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT win AS window_utc,
       id  AS signal_id,
       ts  AS signal_ts_utc,
       decision,
       round(probability::numeric, 4) AS probability,
       round(pnl_pct::numeric, 4)     AS pnl_4h_pct,
       success,
       round(move_pct::numeric, 4)    AS market_move_pct
FROM px
ORDER BY win;

\echo
\echo '=== ПОЛНАЯ ВЫБОРКА (logic_version = 1): наблюдения ЗАВИСИМЫ, доверительные интервалы неприменимы ==='

\echo
\echo '--- 1.7 Доля успеха по ПОЛНОЙ выборке закрытых сигналов версии 1 (X из N) ---'
WITH v1_eval AS (
    SELECT s.decision, e.success
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
)
SELECT 'ВСЕГО' AS bucket,
       count(*) FILTER (WHERE success) AS success_x,
       count(*)                        AS total_n,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM v1_eval
UNION ALL
SELECT decision,
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM v1_eval
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.8 pnl_4h по ПОЛНОЙ выборке версии 1 (наблюдения зависимы) ---'
WITH v1_eval AS (
    SELECT s.decision, e.pnl_pct
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
)
SELECT 'ВСЕГО' AS bucket,
       count(*)                                                                AS n,
       round(avg(pnl_pct)::numeric, 4)                                         AS avg_pnl_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4) AS median_pnl_pct
FROM v1_eval
UNION ALL
SELECT decision,
       count(*),
       round(avg(pnl_pct)::numeric, 4),
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4)
FROM v1_eval
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.9 Полная выборка версии 1 по суткам (динамика; наблюдения зависимы) ---'
SELECT date_trunc('day', s.ts)::date        AS day_utc,
       count(*)                             AS n,
       count(*) FILTER (WHERE e.success)    AS success_x,
       round(100.0 * count(*) FILTER (WHERE e.success) / NULLIF(count(*), 0), 2) AS success_pct,
       round(avg(e.pnl_pct)::numeric, 4)    AS avg_pnl_pct
FROM signals s
JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
WHERE s.logic_version = 1 AND s.decision <> 'wait'
GROUP BY 1
ORDER BY 1;
