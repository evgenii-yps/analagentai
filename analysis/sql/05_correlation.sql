-- ЭТАП 7.1, РАСЧЁТ 5 (раздел 9 ТЗ): попарная согласованность агентов.
-- Только чтение. Независимые 4-часовые окна, logic_version = :target_version
-- (переменная psql из TARGET_LOGIC_VERSION, по умолчанию 4), degraded = false.
--
-- Направления берутся из signals.agents_payload — то есть ровно те мнения,
-- которые участвовали в решении. Окна, где одного из пары нет в payload,
-- считаются отдельной графой и в знаменатель доли совпадений НЕ входят.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 5.1 Попарное совпадение направлений агентов (независимые окна целевой версии, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       count(*)                                                  AS windows_total,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL)    AS both_present_n,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a = b) AS same_direction_x,
       round(100.0 * count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a = b)
             / NULLIF(count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL), 0), 2) AS same_direction_pct,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a <> b)             AS differ_x,
       count(*) FILTER (WHERE a IS NULL OR b IS NULL)                                 AS one_absent_x
FROM pairs
GROUP BY pair
ORDER BY pair;

\echo
\echo '--- 5.2 Совпадение БЕЗ учёта нейтральных мнений (только bullish/bearish, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish')) AS both_directional_n,
       count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish') AND a = b) AS same_direction_x,
       round(100.0 * count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish') AND a = b)
             / NULLIF(count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish')), 0), 2) AS same_direction_pct
FROM pairs
GROUP BY pair
ORDER BY pair;

\echo
\echo '--- 5.3 Совместное распределение направлений по парам (какими именно сочетаниями набрана доля) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       COALESCE(a, 'ОТСУТСТВУЕТ') AS agent_1,
       COALESCE(b, 'ОТСУТСТВУЕТ') AS agent_2,
       count(*)                   AS windows_n
FROM pairs
GROUP BY pair, a, b
ORDER BY pair, windows_n DESC;

\echo
\echo '--- 5.4 Полный состав мнений по окнам (тройка направлений и её частота) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.decision, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id, i.decision, i.success,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id, i.decision, i.success
)
SELECT COALESCE(m, '—') AS market,
       COALESCE(l, '—') AS liquidity,
       COALESCE(f, '—') AS futures,
       decision,
       count(*)                        AS windows_n,
       count(*) FILTER (WHERE success) AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM piv
GROUP BY 1, 2, 3, 4
ORDER BY windows_n DESC, 1, 2, 3;
