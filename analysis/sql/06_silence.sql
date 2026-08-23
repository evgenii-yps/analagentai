-- ЭТАП 7.1, РАСЧЁТ 6 (раздел 10 ТЗ): почему система замолчала.
-- Только чтение. Выполняется по КАЖДОЙ версии логики отдельно; версии сведены
-- в колонки одной таблицы (по одной на версию), но нигде не смешиваются в одном показателе.
--
-- Из версии 3 везде исключены записи с degraded = true (кроме блока 6.8, где
-- они и подсчитываются). Балл (|score|) и согласованность восстанавливаются
-- пересчётом из signals.agents_payload по формуле кода (см. 03_formula.sql).
-- Этот расчёт НИЧЕГО не предлагает: он только измеряет.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 6.0 Объём данных по версиям (после исключения degraded из версии 3) ---'
SELECT logic_version,
       count(*)                                    AS decisions_total,
       count(*) FILTER (WHERE decision <> 'wait')  AS directional,
       min(ts)                                     AS ts_from,
       max(ts)                                     AS ts_to,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span
FROM signals
WHERE NOT (logic_version >= 3 AND degraded)
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.1 Распределение probability по версиям (ВСЕ решения, включая wait) ---'
WITH base AS (
    SELECT logic_version AS ver, probability
    FROM signals
    WHERE NOT (logic_version >= 3 AND degraded) AND probability IS NOT NULL
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY probability) AS val FROM base GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 5, 'p99',      ver, percentile_cont(0.99) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 6, 'максимум', ver, max(probability)                                          FROM base GROUP BY ver
    UNION ALL SELECT 7, 'N решений', ver, count(*)::double precision                                FROM base GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3,
       round(max(val) FILTER (WHERE ver = 4)::numeric, 4) AS v4
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.2 Распределение probability по версиям (ТОЛЬКО направленные решения buy/sell) ---'
WITH base AS (
    SELECT logic_version AS ver, probability
    FROM signals
    WHERE NOT (logic_version >= 3 AND degraded) AND probability IS NOT NULL AND decision <> 'wait'
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY probability) AS val FROM base GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 5, 'p99',      ver, percentile_cont(0.99) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 6, 'максимум', ver, max(probability)                                          FROM base GROUP BY ver
    UNION ALL SELECT 7, 'N решений', ver, count(*)::double precision                                FROM base GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3,
       round(max(val) FILTER (WHERE ver = 4)::numeric, 4) AS v4
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.3 Распределение |балла| по версиям (пересчёт из agents_payload; направленные решения) ---'
WITH base AS (
    SELECT s.logic_version AS ver,
           abs(c.num / NULLIF(c.den, 0)) AS abs_score
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                   * (el->>'confidence')::double precision) AS num,
               sum((el->>'confidence')::double precision)    AS den
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE NOT (s.logic_version >= 3 AND s.degraded) AND s.decision <> 'wait'
), f AS (SELECT ver, abs_score FROM base WHERE abs_score IS NOT NULL
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY abs_score) AS val FROM f GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 5, 'максимум', ver, max(abs_score)                                          FROM f GROUP BY ver
    UNION ALL SELECT 6, 'N решений', ver, count(*)::double precision                              FROM f GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3,
       round(max(val) FILTER (WHERE ver = 4)::numeric, 4) AS v4
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.4 Распределение согласованности по версиям: доля каждого дискретного значения, % ---'
\echo '(согласованность пересчитана так, как её считал КОД соответствующей версии: v1/v2 — знаменатель = число свежих агентов, v3 — знаменатель = 3)'
WITH base AS (
    SELECT s.logic_version AS ver,
           CASE WHEN s.logic_version >= 3
                THEN round((abs(c.pos - c.neg)::double precision / 3.0)::numeric, 2)
                ELSE round((abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0))::numeric, 2)
           END AS agreement
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE NOT (s.logic_version >= 3 AND s.degraded) AND s.decision <> 'wait'
), tot AS (SELECT ver, count(*) AS n FROM base GROUP BY ver)
SELECT COALESCE(b.agreement::text, 'нет данных') AS agreement_value,
       count(*) FILTER (WHERE b.ver = 1) AS v1_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 1)
             / NULLIF((SELECT n FROM tot WHERE ver = 1), 0), 2) AS v1_pct,
       count(*) FILTER (WHERE b.ver = 2) AS v2_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 2)
             / NULLIF((SELECT n FROM tot WHERE ver = 2), 0), 2) AS v2_pct,
       count(*) FILTER (WHERE b.ver = 3) AS v3_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 3)
             / NULLIF((SELECT n FROM tot WHERE ver = 3), 0), 2) AS v3_pct,
       count(*) FILTER (WHERE b.ver = 4) AS v4_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 4)
             / NULLIF((SELECT n FROM tot WHERE ver = 4), 0), 2) AS v4_pct
FROM base b
GROUP BY 1
ORDER BY 1;

\echo
\echo '--- 6.5 Решения с probability >= 0.7: абсолютное число и доля (X из N) ---'
SELECT logic_version,
       count(*)                                                        AS decisions_total,
       count(*) FILTER (WHERE probability >= 0.7)                      AS prob_ge_07_x,
       round(100.0 * count(*) FILTER (WHERE probability >= 0.7) / NULLIF(count(*), 0), 3) AS prob_ge_07_pct,
       count(*) FILTER (WHERE decision <> 'wait')                      AS directional_n,
       count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7) AS directional_ge_07_x,
       round(100.0 * count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7)
             / NULLIF(count(*) FILTER (WHERE decision <> 'wait'), 0), 3) AS directional_ge_07_pct
FROM signals
WHERE NOT (logic_version >= 3 AND degraded)
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.6 Разбивка решений buy / sell / wait по версиям (X из N) ---'
WITH tot AS (
    SELECT logic_version AS ver, count(*) AS n
    FROM signals WHERE NOT (logic_version >= 3 AND degraded) GROUP BY logic_version
)
SELECT s.decision,
       count(*) FILTER (WHERE s.logic_version = 1) AS v1_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 1)
             / NULLIF((SELECT n FROM tot WHERE ver = 1), 0), 2) AS v1_pct,
       count(*) FILTER (WHERE s.logic_version = 2) AS v2_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 2)
             / NULLIF((SELECT n FROM tot WHERE ver = 2), 0), 2) AS v2_pct,
       count(*) FILTER (WHERE s.logic_version = 3) AS v3_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 3)
             / NULLIF((SELECT n FROM tot WHERE ver = 3), 0), 2) AS v3_pct,
       count(*) FILTER (WHERE s.logic_version = 4) AS v4_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 4)
             / NULLIF((SELECT n FROM tot WHERE ver = 4), 0), 2) AS v4_pct
FROM signals s
WHERE NOT (s.logic_version >= 3 AND s.degraded)
GROUP BY s.decision
ORDER BY s.decision;

\echo
\echo '--- 6.7 ВЕРСИЯ 3: сколько кандидатов дал бы каждый порог probability (только измерение) ---'
\echo '(кандидат = decision <> wait, degraded = false, probability >= порога; «в сутки» — деление на длительность периода версии 3)'
WITH v3 AS (
    SELECT probability, ts
    FROM signals
    WHERE logic_version = 3 AND degraded = false AND decision <> 'wait' AND probability IS NOT NULL
), span AS (
    SELECT GREATEST(extract(epoch FROM (max(ts) - min(ts))) / 86400.0, 0.0001) AS days,
           count(*) AS directional_total
    FROM v3
), t(threshold) AS (
    VALUES (0.70), (0.65), (0.60), (0.55), (0.50), (0.45), (0.40)
)
SELECT t.threshold,
       (SELECT count(*) FROM v3 WHERE probability >= t.threshold)                      AS candidates_x,
       (SELECT directional_total FROM span)                                            AS directional_n,
       round(100.0 * (SELECT count(*) FROM v3 WHERE probability >= t.threshold)
             / NULLIF((SELECT directional_total FROM span), 0), 3)                     AS candidates_pct,
       round(((SELECT count(*) FROM v3 WHERE probability >= t.threshold)
             / (SELECT days FROM span))::numeric, 1)                                   AS candidates_per_day
FROM t
ORDER BY t.threshold DESC;

\echo
\echo '--- 6.7б Для сравнения: сколько кандидатов в сутки давала версия 1 при пороге 0.7 ---'
WITH v1 AS (
    SELECT probability, ts FROM signals
    WHERE logic_version = 1 AND decision <> 'wait' AND probability IS NOT NULL
)
SELECT count(*)                                        AS directional_n,
       count(*) FILTER (WHERE probability >= 0.7)      AS candidates_ge_07,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span,
       round((count(*) FILTER (WHERE probability >= 0.7)
             / GREATEST(extract(epoch FROM (max(ts) - min(ts))) / 86400.0, 0.0001))::numeric, 1) AS candidates_per_day
FROM v1;

\echo
\echo '--- 6.8 Версия 3: число и доля решений с degraded = true ---'
SELECT logic_version,
       count(*)                                 AS decisions_total,
       count(*) FILTER (WHERE degraded)         AS degraded_x,
       round(100.0 * count(*) FILTER (WHERE degraded) / NULLIF(count(*), 0), 2) AS degraded_pct,
       min(ts) FILTER (WHERE degraded)          AS first_degraded_ts,
       max(ts) FILTER (WHERE degraded)          AS last_degraded_ts
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.9 Проверка альтернативного объяснения: изменилась ли доля neutral у каждого агента между версиями ---'
\echo '(источник — agent_outputs; границы версий взяты из signals)'
WITH ver_windows AS (
    -- Границы версий берутся из самих данных: в agent_outputs колонки версии
    -- нет. Перечисление версий 1-4 убрано: оно помечало ЧЕТВЁРКОЙ выводы любой
    -- более новой версии, а с Этапа 8.1 таких выводов большинство.
    SELECT logic_version, min(ts) AS started_at FROM signals GROUP BY logic_version
), ao AS (
    SELECT a.agent, a.signal,
           -- 0 — версия НЕИЗВЕСТНА: вывод сделан раньше первого сигнала
           -- вообще, и определить её нечем. Тот же признак, что в
           -- agent_outputs_daily: подставлять ближайшую версию запрещено.
           coalesce((SELECT v.logic_version FROM ver_windows v
                      WHERE v.started_at <= a.ts
                      ORDER BY v.started_at DESC LIMIT 1), 0) AS ver
    FROM agent_outputs a
)
SELECT agent,
       ver AS logic_version,
       count(*)                                                AS outputs_n,
       count(*) FILTER (WHERE signal = 'neutral')              AS neutral_x,
       round(100.0 * count(*) FILTER (WHERE signal = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE signal = 'bullish')              AS bullish_x,
       count(*) FILTER (WHERE signal = 'bearish')              AS bearish_x,
       count(*) FILTER (WHERE signal = 'insufficient_data')    AS insufficient_x
FROM ao
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 6.10 То же по мнениям, реально участвовавшим в решениях (agents_payload) ---'
SELECT s.logic_version,
       el->>'agent'                                                    AS agent,
       count(*)                                                        AS opinions_n,
       count(*) FILTER (WHERE el->>'signal' = 'neutral')               AS neutral_x,
       round(100.0 * count(*) FILTER (WHERE el->>'signal' = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE el->>'signal' = 'bullish')               AS bullish_x,
       count(*) FILTER (WHERE el->>'signal' = 'bearish')               AS bearish_x,
       round(avg((el->>'confidence')::double precision)::numeric, 4)   AS avg_confidence
FROM signals s
CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
              THEN s.agents_payload ELSE '[]'::jsonb END) el
WHERE NOT (s.logic_version >= 3 AND s.degraded)
GROUP BY s.logic_version, el->>'agent'
ORDER BY el->>'agent', s.logic_version;

\echo
\echo '--- 6.11 Число решений и доля probability >= 0.7 по суткам (весь период, версии помечены) ---'
SELECT date_trunc('day', ts)::date         AS day_utc,
       logic_version,
       count(*)                            AS decisions,
       count(*) FILTER (WHERE degraded)    AS degraded_x,
       count(*) FILTER (WHERE decision <> 'wait') AS directional,
       count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7) AS candidates_ge_07,
       round(max(probability)::numeric, 4) AS max_probability
FROM signals
GROUP BY 1, 2
ORDER BY 1, 2;
