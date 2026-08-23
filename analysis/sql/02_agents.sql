-- ЭТАП 7.1, РАСЧЁТ 2 (раздел 6 ТЗ): вклад каждого агента по отдельности.
-- Только чтение. Область данных — logic_version = :target_version (переменная
-- psql из TARGET_LOGIC_VERSION, по умолчанию 4), независимые 4-часовые окна,
-- degraded = false.
--
-- Источник мнения агента — signals.agents_payload (JSONB): в нём лежит РОВНО тот
-- набор выводов, который видел Decision Agent в момент решения (agent, signal,
-- confidence, ts). Это точнее, чем присоединять agent_outputs по времени.
-- Если агента в payload нет — он не участвовал в решении (устарел, отсутствовал
-- или выдал insufficient_data); такие случаи считаются отдельной графой.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 2.1 Распределение уверенности по агентам, НЕЗАВИСИМЫЕ ОКНА целевой версии ---'
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
), aw AS (
    SELECT g.agent, p.confidence
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT (el->>'confidence')::double precision AS confidence
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*)                                    AS windows_n,
       count(confidence)                           AS present_n,
       count(*) - count(confidence)                AS absent_n,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p25,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS median,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p75,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p99,
       count(*) FILTER (WHERE confidence = 0)      AS conf_eq_0,
       count(*) FILTER (WHERE confidence < 0.01)   AS conf_lt_001,
       count(*) FILTER (WHERE confidence = 1.0)    AS conf_eq_1
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.2 То же по ВСЕМ выводам agent_outputs за период целевой версии (наблюдения зависимы) ---'
-- Здесь «целевая версия» — первая (v1), и отбор идёт по её ВЕРХНЕЙ границе.
-- Перечисления версий тут нет и подстановки ближайшей версии тоже: всё, что
-- раньше старта v2, относится к v1 по построению — v1 была первой, раньше неё
-- версий не существовало.
WITH bounds AS (
    SELECT COALESCE(
               (SELECT min(ts) FROM signals WHERE logic_version = 2),
               (SELECT min(ts) FROM signals WHERE logic_version = 3),
               'infinity'::timestamptz) AS v1_end
), src AS (
    SELECT a.agent, a.confidence
    FROM agent_outputs a, bounds b
    WHERE a.ts < b.v1_end
)
SELECT agent,
       count(*)                                    AS outputs_n,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p25,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS median,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p75,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p99,
       count(*) FILTER (WHERE confidence = 0)      AS conf_eq_0,
       round(100.0 * count(*) FILTER (WHERE confidence = 0)    / NULLIF(count(*), 0), 2) AS conf_eq_0_pct,
       count(*) FILTER (WHERE confidence < 0.01)   AS conf_lt_001,
       round(100.0 * count(*) FILTER (WHERE confidence < 0.01) / NULLIF(count(*), 0), 2) AS conf_lt_001_pct,
       count(*) FILTER (WHERE confidence = 1.0)    AS conf_eq_1,
       round(100.0 * count(*) FILTER (WHERE confidence = 1.0)  / NULLIF(count(*), 0), 2) AS conf_eq_1_pct
FROM src
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.3 Распределение направлений агента по независимым окнам целевой версии (X из N) ---'
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
), aw AS (
    SELECT g.agent, p.signal
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*)                                                AS windows_n,
       count(*) FILTER (WHERE signal = 'bullish')              AS bullish_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'bullish') / NULLIF(count(*), 0), 2) AS bullish_pct,
       count(*) FILTER (WHERE signal = 'bearish')              AS bearish_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'bearish') / NULLIF(count(*), 0), 2) AS bearish_pct,
       count(*) FILTER (WHERE signal = 'neutral')              AS neutral_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE signal IS NULL)                  AS absent_n
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.4 Связь направления агента с ФАКТИЧЕСКИМ движением цены (X из N) ---'
\echo '(bullish → доля окон с ростом цены; bearish → доля окон с падением; сравнивать с базовой линией из Расчёта 1)'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.instrument_id, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
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
), aw AS (
    SELECT g.agent, p.signal, i.move_pct
    FROM px i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
    WHERE i.move_pct IS NOT NULL
)
SELECT agent,
       COALESCE(signal, 'ОТСУТСТВУЕТ') AS agent_signal,
       count(*)                        AS n,
       count(*) FILTER (WHERE move_pct > 0) AS price_up_x,
       round(100.0 * count(*) FILTER (WHERE move_pct > 0) / NULLIF(count(*), 0), 2) AS price_up_pct,
       count(*) FILTER (WHERE move_pct < 0) AS price_down_x,
       round(100.0 * count(*) FILTER (WHERE move_pct < 0) / NULLIF(count(*), 0), 2) AS price_down_pct,
       round(avg(move_pct)::numeric, 4)     AS avg_move_pct,
       CASE COALESCE(signal, '-')
            WHEN 'bullish' THEN round(100.0 * count(*) FILTER (WHERE move_pct > 0) / NULLIF(count(*), 0), 2)
            WHEN 'bearish' THEN round(100.0 * count(*) FILTER (WHERE move_pct < 0) / NULLIF(count(*), 0), 2)
       END AS hit_rate_pct
FROM aw
GROUP BY agent, signal
ORDER BY agent, signal NULLS LAST;

\echo
\echo '--- 2.5 Связь уверенности агента с исходом сигнала: две группы по медиане уверенности (X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.confidence, i.success
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT (el->>'confidence')::double precision AS confidence
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
    WHERE p.confidence IS NOT NULL
), med AS (
    SELECT agent, percentile_cont(0.5) WITHIN GROUP (ORDER BY confidence) AS med_conf
    FROM aw GROUP BY agent
)
SELECT a.agent,
       round(m.med_conf::numeric, 4) AS median_confidence,
       CASE WHEN a.confidence >= m.med_conf THEN 'уверенность >= медианы'
            ELSE 'уверенность < медианы' END AS conf_group,
       count(*)                              AS n,
       count(*) FILTER (WHERE a.success)     AS success_x,
       round(100.0 * count(*) FILTER (WHERE a.success) / NULLIF(count(*), 0), 2) AS success_pct
FROM aw a
JOIN med m ON m.agent = a.agent
GROUP BY a.agent, m.med_conf, 3
ORDER BY a.agent, 3;

\echo
\echo '--- 2.6 Согласие агента с итоговым решением (X из N независимых окон) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.decision, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.signal, i.decision
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*) AS windows_n,
       count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'buy')
                           OR (signal = 'bearish' AND decision = 'sell')) AS agrees_x,
       round(100.0 * count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'buy')
                                         OR (signal = 'bearish' AND decision = 'sell'))
             / NULLIF(count(*), 0), 2) AS agrees_pct,
       count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'sell')
                           OR (signal = 'bearish' AND decision = 'buy')) AS opposes_x,
       count(*) FILTER (WHERE signal = 'neutral')  AS neutral_x,
       count(*) FILTER (WHERE signal IS NULL)      AS absent_x
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.7 Пропуски Market: число циклов решения БЕЗ market в payload, по суткам (весь период) ---'
\echo '(ожидание ТЗ: после 14.08 13:39 UTC пропуски прекратились)'
SELECT date_trunc('day', s.ts)::date AS day_utc,
       s.logic_version,
       count(*) AS cycles,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "market"}]'::jsonb)) AS market_absent,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "liquidity"}]'::jsonb)) AS liquidity_absent,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "futures"}]'::jsonb)) AS futures_absent
FROM signals s
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 2.8 Число выводов agent_outputs по агентам и суткам (косвенный контроль пропусков) ---'
SELECT date_trunc('day', ts)::date AS day_utc,
       count(*) FILTER (WHERE agent = 'market')    AS market_rows,
       count(*) FILTER (WHERE agent = 'liquidity') AS liquidity_rows,
       count(*) FILTER (WHERE agent = 'futures')   AS futures_rows,
       count(*) FILTER (WHERE signal = 'insufficient_data') AS insufficient_data_rows
FROM agent_outputs
GROUP BY 1
ORDER BY 1;

\echo
\echo '--- 2.9 Сбои агентов по суткам (agent_failures) ---'
SELECT date_trunc('day', ts)::date AS day_utc,
       agent,
       error_type,
       count(*) AS failures
FROM agent_failures
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo
\echo '--- 2.10 ЭТАП 7.3: направления Futures по версиям логики (проверка симметрии агента) ---'
\echo '(ожидание после 7.3: bearish строго больше нуля — до 7.3 их было РОВНО НОЛЬ за 8 суток)'
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
    WHERE a.agent = 'futures'
)
SELECT ver AS logic_version,
       count(*)                                                  AS outputs_n,
       count(*) FILTER (WHERE signal = 'bullish')                AS bullish_x,
       count(*) FILTER (WHERE signal = 'bearish')                AS bearish_x,
       count(*) FILTER (WHERE signal = 'neutral')                AS neutral_x,
       count(*) FILTER (WHERE signal = 'insufficient_data')      AS insufficient_x,
       round(100.0 * count(*) FILTER (WHERE signal = 'bearish')
             / NULLIF(count(*), 0), 3)                           AS bearish_pct,
       CASE WHEN count(*) FILTER (WHERE signal = 'bearish') > 0
            THEN 'ДА — ветка bearish достижима'
            ELSE 'НЕТ — ни одного bearish' END                   AS bearish_reachable
FROM ao
GROUP BY ver
ORDER BY ver;

\echo
\echo '--- 2.11 ЭТАП 7.3: фактическое распределение funding rate за весь период наблюдений ---'
\echo '(этим подтверждается или опровергается причина односторонности Futures до 7.3)'
SELECT f.instrument_id,
       i.symbol,
       count(*)                                                             AS n,
       min(f.ts)                                                            AS ts_from,
       max(f.ts)                                                            AS ts_to,
       round(min(f.rate)::numeric, 8)                                       AS min_rate,
       round(percentile_cont(0.01) WITHIN GROUP (ORDER BY f.rate)::numeric, 8) AS p1,
       round(percentile_cont(0.05) WITHIN GROUP (ORDER BY f.rate)::numeric, 8) AS p5,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY f.rate)::numeric, 8) AS median,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY f.rate)::numeric, 8) AS p95,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY f.rate)::numeric, 8) AS p99,
       round(max(f.rate)::numeric, 8)                                       AS max_rate,
       count(*) FILTER (WHERE f.rate < 0)                                   AS negative_x,
       round(100.0 * count(*) FILTER (WHERE f.rate < 0) / NULLIF(count(*), 0), 3) AS negative_pct,
       count(*) FILTER (WHERE abs(f.rate) > 0.0003)                         AS above_old_threshold_x,
       round(100.0 * count(*) FILTER (WHERE abs(f.rate) > 0.0003)
             / NULLIF(count(*), 0), 3)                                      AS above_old_threshold_pct
FROM funding f
JOIN instruments i ON i.id = f.instrument_id
GROUP BY f.instrument_id, i.symbol
ORDER BY f.instrument_id;

\echo
\echo '--- 2.12 ЭТАП 7.3: распределение open interest (тот же перцентильный принцип) ---'
SELECT o.instrument_id,
       i.symbol,
       count(*)                                                              AS n,
       round(min(o.value)::numeric, 4)                                       AS min_value,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY o.value)::numeric, 4) AS median,
       round(max(o.value)::numeric, 4)                                       AS max_value
FROM open_interest o
JOIN instruments i ON i.id = o.instrument_id
GROUP BY o.instrument_id, i.symbol
ORDER BY o.instrument_id;
