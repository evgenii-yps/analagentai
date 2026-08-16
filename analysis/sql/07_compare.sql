-- ЭТАП 7.3, РАСЧЁТ 7 (Блок D): сравнение целевой версии логики с версией 1
-- по одним и тем же метрикам, двумя колонками рядом.
-- Только чтение.
--
-- Целевая версия задаётся переменной psql :target_version (из переменной
-- окружения TARGET_LOGIC_VERSION, по умолчанию 4). Версия 1 — эталон, по
-- которому выполнялась диагностика Этапа 7.1.
--
-- ВАЖНО ПРИ ЧТЕНИИ. Версии сравниваются по РАЗНЫМ отрезкам рынка: версия 1 —
-- это 08–13.08.2026, целевая версия — период после развёртывания. Разница в
-- доле успеха может объясняться сменой рыночного режима, а не изменением
-- логики. Именно поэтому рядом приводятся тривиальные базовые линии на тех же
-- окнах: если система и базовая линия сдвинулись одинаково, изменилась не
-- система, а рынок.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 7.1 Объём и период сравниваемых версий ---'
SELECT logic_version,
       count(*)                                                       AS decisions_total,
       count(*) FILTER (WHERE decision <> 'wait')                     AS directional,
       count(*) FILTER (WHERE status = 'closed')                      AS closed,
       count(*) FILTER (WHERE degraded)                               AS degraded_x,
       min(ts)                                                        AS ts_from,
       max(ts)                                                        AS ts_to,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span
FROM signals
WHERE logic_version IN (1, :target_version)
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 7.2 Доля успеха системы и три базовые линии: версия 1 против целевой (X из N) ---'
WITH src AS (
    SELECT DISTINCT ON (lv, win) *
    FROM (
        SELECT s.logic_version AS lv, s.id, s.ts, s.instrument_id, s.decision,
               e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version IN (1, :target_version)
          AND s.decision <> 'wait'
          AND s.degraded = FALSE
    ) q ORDER BY lv, win, ts ASC
), px AS (
    SELECT src.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM src
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = src.instrument_id AND o.timeframe = '1m'
          AND o.ts <= src.win AND o.ts > src.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = src.instrument_id AND o.timeframe = '1m'
          AND o.ts <= src.win + interval '4 hours'
          AND o.ts >  src.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
), agg AS (
    SELECT lv,
           count(*)                                   AS windows_n,
           count(*) FILTER (WHERE success)            AS system_x,
           count(*) FILTER (WHERE move_pct IS NOT NULL) AS priced_n,
           count(*) FILTER (WHERE move_pct > 0)       AS up_x,
           count(*) FILTER (WHERE move_pct < 0)       AS down_x
    FROM px GROUP BY lv
), rows_ AS (
    SELECT 1 AS ord, 'независимых окон, N'            AS metric, lv, windows_n::numeric AS val FROM agg
    UNION ALL SELECT 2, 'система: успехов X',          lv, system_x FROM agg
    UNION ALL SELECT 3, 'система: доля успеха, %',     lv,
        round(100.0 * system_x / NULLIF(windows_n, 0), 2) FROM agg
    UNION ALL SELECT 4, 'всегда buy: успехов X',       lv, up_x FROM agg
    UNION ALL SELECT 5, 'всегда buy: доля, %',         lv,
        round(100.0 * up_x / NULLIF(priced_n, 0), 2) FROM agg
    UNION ALL SELECT 6, 'всегда sell: успехов X',      lv, down_x FROM agg
    UNION ALL SELECT 7, 'всегда sell: доля, %',        lv,
        round(100.0 * down_x / NULLIF(priced_n, 0), 2) FROM agg
    UNION ALL SELECT 8, 'окон с ценой, N',             lv, priced_n FROM agg
)
SELECT metric,
       max(val) FILTER (WHERE lv = 1)                AS version_1,
       max(val) FILTER (WHERE lv = :target_version)  AS version_target
FROM rows_
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 7.3 pnl 4h на независимых окнах: версия 1 против целевой ---'
WITH src AS (
    SELECT DISTINCT ON (lv, win) *
    FROM (
        SELECT s.logic_version AS lv, s.id, s.ts, e.pnl_pct,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version IN (1, :target_version)
          AND s.decision <> 'wait'
          AND s.degraded = FALSE
    ) q ORDER BY lv, win, ts ASC
)
SELECT lv AS logic_version,
       count(*)                                                                AS n,
       round(avg(pnl_pct)::numeric, 4)                                         AS avg_pnl_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4) AS median_pnl_pct,
       round(min(pnl_pct)::numeric, 4)                                         AS min_pnl_pct,
       round(max(pnl_pct)::numeric, 4)                                         AS max_pnl_pct
FROM src
GROUP BY lv
ORDER BY lv;

\echo
\echo '--- 7.4 Калибровочная таблица по ИНДЕКСУ СОГЛАСИЯ: версия 1 против целевой ---'
\echo '(в обеих версиях это одна и та же величина — формула индекса Этапом 7.3 не менялась)'
WITH src AS (
    SELECT DISTINCT ON (lv, win) *
    FROM (
        SELECT s.logic_version AS lv, s.id, s.ts, s.probability, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version IN (1, :target_version)
          AND s.decision <> 'wait'
          AND s.degraded = FALSE
          AND s.probability IS NOT NULL
    ) q ORDER BY lv, win, ts ASC
), b AS (
    SELECT *, least(width_bucket(probability, 0, 1, 5), 5) AS bucket FROM src
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END AS conviction_range,
       count(*) FILTER (WHERE lv = 1)                                   AS v1_n,
       count(*) FILTER (WHERE lv = 1 AND success)                       AS v1_success_x,
       round((count(*) FILTER (WHERE lv = 1 AND success))::numeric
             / NULLIF(count(*) FILTER (WHERE lv = 1), 0), 4)            AS v1_rate,
       count(*) FILTER (WHERE lv = :target_version)                     AS target_n,
       count(*) FILTER (WHERE lv = :target_version AND success)         AS target_success_x,
       round((count(*) FILTER (WHERE lv = :target_version AND success))::numeric
             / NULLIF(count(*) FILTER (WHERE lv = :target_version), 0), 4) AS target_rate
FROM b
GROUP BY bucket
ORDER BY bucket;

\echo
\echo '--- 7.5 Состав мнений агентов: версия 1 против целевой (доли направлений) ---'
SELECT el->>'agent' AS agent,
       s.logic_version,
       count(*)                                                        AS opinions_n,
       round(100.0 * count(*) FILTER (WHERE el->>'signal' = 'bullish')
             / NULLIF(count(*), 0), 2)                                 AS bullish_pct,
       round(100.0 * count(*) FILTER (WHERE el->>'signal' = 'bearish')
             / NULLIF(count(*), 0), 2)                                 AS bearish_pct,
       round(100.0 * count(*) FILTER (WHERE el->>'signal' = 'neutral')
             / NULLIF(count(*), 0), 2)                                 AS neutral_pct,
       round(avg((el->>'confidence')::double precision)::numeric, 4)   AS avg_confidence
FROM signals s
CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
              THEN s.agents_payload ELSE '[]'::jsonb END) el
WHERE s.logic_version IN (1, :target_version)
GROUP BY el->>'agent', s.logic_version
ORDER BY el->>'agent', s.logic_version;

\echo
\echo '--- 7.6 Кандидаты на уведомление: версия 1 против целевой (порог не менялся) ---'
SELECT logic_version,
       count(*) FILTER (WHERE decision <> 'wait')                         AS directional_n,
       count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7)  AS candidates_ge_07,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span,
       round((count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7)
             / GREATEST(extract(epoch FROM (max(ts) - min(ts))) / 86400.0, 0.0001))::numeric, 1)
           AS candidates_per_day
FROM signals
WHERE logic_version IN (1, :target_version)
  AND NOT (logic_version >= 3 AND degraded)
GROUP BY logic_version
ORDER BY logic_version;
