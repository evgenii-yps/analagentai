-- ЭТАП 7.1, РАСЧЁТ 4 (раздел 8 ТЗ): фактическая частота обновления входных данных.
-- Только чтение.
--
-- В agent_outputs НЕТ колонки logic_version, поэтому версия определяется по
-- времени: границы берутся из самой таблицы signals (min(ts) версий 2 и 3), а не
-- вписаны константами. Так расчёт остаётся верным, даже если фактические
-- границы отличаются от указанных в ТЗ (13.08 15:41 / 14.08 13:39 UTC).
--
-- Серии одинаковых значений выделяются приёмом «острова и промежутки»:
-- разность двух нумераций (по времени и по времени внутри значения) постоянна
-- ровно на непрерывном участке одинакового confidence.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 4.0 Границы версий, применённые к agent_outputs ---'
SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                (SELECT min(ts) FROM signals WHERE logic_version = 3)) AS v2_start_utc,
       (SELECT min(ts) FROM signals WHERE logic_version = 3)           AS v3_start_utc,
       (SELECT min(ts) FROM agent_outputs)                             AS agent_outputs_from,
       (SELECT max(ts) FROM agent_outputs)                             AS agent_outputs_to;

\echo
\echo '--- 4.1 Серии подряд идущих циклов с ОДИНАКОВЫМ confidence (по версиям 1, 3 и 4) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v3_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v4_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                WHEN a.ts < b.v4_start THEN 3
                ELSE 4 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), seq AS (
    SELECT agent, ver, confidence,
           row_number() OVER (PARTITION BY agent, ver ORDER BY ts)
         - row_number() OVER (PARTITION BY agent, ver, confidence ORDER BY ts) AS grp
    FROM ao
), runs AS (
    SELECT agent, ver, confidence, grp, count(*) AS run_len
    FROM seq GROUP BY agent, ver, confidence, grp
)
SELECT agent,
       ver                                  AS logic_version,
       count(*)                             AS runs_n,
       sum(run_len)                         AS cycles_n,
       round(avg(run_len)::numeric, 2)      AS avg_run_len,
       max(run_len)                         AS max_run_len,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY run_len)::numeric, 2) AS median_run_len
FROM runs
WHERE ver IN (1, 3, 4)
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 4.2 Доля циклов, где confidence НЕ изменился относительно предыдущего (по версиям 1, 3 и 4) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v3_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v4_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence, a.signal,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                WHEN a.ts < b.v4_start THEN 3
                ELSE 4 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), lagged AS (
    SELECT agent, ver, confidence, signal,
           lag(confidence) OVER (PARTITION BY agent, ver ORDER BY ts) AS prev_conf,
           lag(signal)     OVER (PARTITION BY agent, ver ORDER BY ts) AS prev_signal
    FROM ao
)
SELECT agent,
       ver                                                              AS logic_version,
       count(*) FILTER (WHERE prev_conf IS NOT NULL)                    AS comparable_cycles,
       count(*) FILTER (WHERE prev_conf IS NOT NULL AND confidence = prev_conf) AS unchanged_conf_x,
       round(100.0 * count(*) FILTER (WHERE prev_conf IS NOT NULL AND confidence = prev_conf)
             / NULLIF(count(*) FILTER (WHERE prev_conf IS NOT NULL), 0), 2) AS unchanged_conf_pct,
       count(*) FILTER (WHERE prev_signal IS NOT NULL AND signal = prev_signal) AS unchanged_signal_x,
       round(100.0 * count(*) FILTER (WHERE prev_signal IS NOT NULL AND signal = prev_signal)
             / NULLIF(count(*) FILTER (WHERE prev_signal IS NOT NULL), 0), 2) AS unchanged_signal_pct
FROM lagged
WHERE ver IN (1, 3, 4)
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 4.3 Число уникальных значений confidence за сутки (по агентам, все версии помечены) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v3_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v4_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                WHEN a.ts < b.v4_start THEN 3
                ELSE 4 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
)
SELECT date_trunc('day', ts)::date AS day_utc,
       agent,
       min(ver)                    AS ver_min,
       max(ver)                    AS ver_max,
       count(*)                    AS outputs_n,
       count(DISTINCT confidence)  AS distinct_confidence,
       round(100.0 * count(DISTINCT confidence) / NULLIF(count(*), 0), 3) AS distinct_pct
FROM ao
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 4.4 Самые длинные серии повторов поштучно (топ-15 по каждой версии) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    (SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v3_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 4),
                    'infinity'::timestamptz) AS v4_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                WHEN a.ts < b.v4_start THEN 3
                ELSE 4 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), seq AS (
    SELECT agent, ver, ts, confidence,
           row_number() OVER (PARTITION BY agent, ver ORDER BY ts)
         - row_number() OVER (PARTITION BY agent, ver, confidence ORDER BY ts) AS grp
    FROM ao
), runs AS (
    SELECT agent, ver, confidence, count(*) AS run_len, min(ts) AS ts_from, max(ts) AS ts_to
    FROM seq GROUP BY agent, ver, confidence, grp
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY ver ORDER BY run_len DESC) AS rn
    FROM runs WHERE ver IN (1, 3, 4)
)
SELECT ver AS logic_version, agent, round(confidence::numeric, 4) AS confidence,
       run_len, ts_from, ts_to,
       round((extract(epoch FROM (ts_to - ts_from)) / 3600.0)::numeric, 2) AS hours_span
FROM ranked
WHERE rn <= 15
ORDER BY ver, run_len DESC;

\echo
\echo '--- 4.5 ЭТАП 7.3: доля повторных решений и число уникальных наборов входов по суткам ---'
\echo '(is_repeat = решение принято на том же наборе мнений, что и предыдущее; отправку это не фильтрует)'
SELECT date_trunc('day', ts)::date        AS day_utc,
       logic_version,
       count(*)                           AS decisions,
       count(*) FILTER (WHERE is_repeat)  AS repeats_x,
       round(100.0 * count(*) FILTER (WHERE is_repeat) / NULLIF(count(*), 0), 2) AS repeats_pct,
       count(DISTINCT inputs_hash)        AS unique_inputs,
       count(*) FILTER (WHERE inputs_hash IS NULL) AS without_hash
FROM signals
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 4.6 ЭТАП 7.3: сколько решений приходится на один уникальный набор входов ---'
SELECT logic_version,
       count(*)                                                       AS decisions,
       count(DISTINCT inputs_hash)                                    AS unique_inputs,
       round(count(*)::numeric / NULLIF(count(DISTINCT inputs_hash), 0), 2) AS decisions_per_input
FROM signals
WHERE inputs_hash IS NOT NULL
GROUP BY logic_version
ORDER BY logic_version;
