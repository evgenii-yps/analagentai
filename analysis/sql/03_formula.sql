-- ЭТАП 7.1, РАСЧЁТ 3 (раздел 7 ТЗ): информативность балла и согласованности.
-- Только чтение.
--
-- Формула по коду (src/decision/agent.py, строки 101–128):
--     score       = Σ(direction · confidence · weight) / Σ(weight · confidence)   (стр. 104–110)
--     agreement   = |pos − neg| / total_agents                                    (стр. 127)
--     probability = round(min(|score| · (0.5 + 0.5 · agreement), 1.0), 4)          (стр. 128)
-- где direction = +1 bullish / −1 bearish / 0 neutral, total_agents = 3 (Этап 7.2).
-- ДО Этапа 7.2 знаменателем согласованности было число СВЕЖИХ агентов (len(fresh)).
--
-- Отдельных колонок score/agreement в схеме НЕТ. Здесь они восстанавливаются
-- двумя независимыми путями:
--   (а) пересчётом из signals.agents_payload по формуле кода — точное значение
--       при условии, что веса агентов равны 1.0 (значения по умолчанию
--       WEIGHT_MARKET/LIQUIDITY/FUTURES в src/core/config.py);
--   (б) разбором текста signals.rationale регулярным выражением — значение
--       округлено кодом до 2 знаков, поэтому годится только для сверки.
-- Блок 3.1 сравнивает пересчитанную вероятность с сохранённой: совпадение
-- подтверждает и формулу, и предположение о весах = 1.0.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 3.1 Проверка формулы на данных: пересчёт из agents_payload против сохранённой probability ---'
\echo '(agreement_fresh — знаменатель = число свежих агентов (логика до 7.2); agreement_total3 — знаменатель = 3 (логика с 7.2))'
WITH calc AS (
    SELECT s.id, s.logic_version, s.decision, s.probability, s.degraded,
           c.num, c.den, c.pos, c.neg, c.n_fresh
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT
            sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                * (el->>'confidence')::double precision)          AS num,
            sum((el->>'confidence')::double precision)             AS den,
            count(*) FILTER (WHERE el->>'signal' = 'bullish')      AS pos,
            count(*) FILTER (WHERE el->>'signal' = 'bearish')      AS neg,
            count(*)                                               AS n_fresh
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE s.decision <> 'wait'
), f AS (
    SELECT logic_version,
           probability,
           abs(num / NULLIF(den, 0))                                       AS abs_score,
           abs(pos - neg)::double precision / NULLIF(n_fresh, 0)           AS agr_fresh,
           abs(pos - neg)::double precision / 3.0                          AS agr_total3
    FROM calc
)
SELECT logic_version,
       count(*) AS signals_n,
       count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_fresh), 1.0))::numeric, 4)) <= 0.001)  AS match_agr_fresh,
       round(100.0 * count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_fresh), 1.0))::numeric, 4)) <= 0.001)
             / NULLIF(count(*), 0), 2) AS match_agr_fresh_pct,
       count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_total3), 1.0))::numeric, 4)) <= 0.001) AS match_agr_total3,
       round(100.0 * count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_total3), 1.0))::numeric, 4)) <= 0.001)
             / NULLIF(count(*), 0), 2) AS match_agr_total3_pct
FROM f
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 3.2 Сверка пересчёта с текстом rationale (регулярное выражение, 2 знака) ---'
WITH src AS (
    SELECT s.id, s.logic_version, s.probability, s.rationale,
           (regexp_match(s.rationale, 'балл=[[:space:]]*([+-]?[0-9]+[.,]?[0-9]*)'))[1]              AS score_txt,
           (regexp_match(s.rationale, 'согласованность=[[:space:]]*([0-9]+[.,]?[0-9]*)'))[1]        AS agr_txt
    FROM signals s
    WHERE s.decision <> 'wait'
)
SELECT logic_version,
       count(*)                                        AS signals_n,
       count(score_txt)                                AS score_parsed,
       count(agr_txt)                                  AS agreement_parsed,
       round(100.0 * count(score_txt) / NULLIF(count(*), 0), 2) AS score_parsed_pct,
       round(100.0 * count(agr_txt)   / NULLIF(count(*), 0), 2) AS agreement_parsed_pct
FROM src
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 3.3 Доля успеха по КВАРТИЛЯМ |балла| (независимые окна целевой версии, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), calc AS (
    SELECT i.id, i.success, i.probability,
           abs(c.num / NULLIF(c.den, 0))                             AS abs_score,
           abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0)     AS agreement
    FROM v1_indep i
    CROSS JOIN LATERAL (
        SELECT sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                   * (el->>'confidence')::double precision)     AS num,
               sum((el->>'confidence')::double precision)        AS den,
               count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
    ) c
), q AS (
    SELECT *, ntile(4) OVER (ORDER BY abs_score) AS quartile FROM calc WHERE abs_score IS NOT NULL
)
SELECT quartile,
       count(*)                          AS n,
       round(min(abs_score)::numeric, 4) AS abs_score_min,
       round(max(abs_score)::numeric, 4) AS abs_score_max,
       count(*) FILTER (WHERE success)   AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM q
GROUP BY quartile
ORDER BY quartile;

\echo
\echo '--- 3.4 Доля успеха по КАЖДОМУ дискретному значению согласованности (независимые окна целевой версии, X из N) ---'
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
), calc AS (
    SELECT i.success,
           round((abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0))::numeric, 2) AS agreement,
           c.n AS agents_in_payload
    FROM v1_indep i
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
    ) c
)
SELECT COALESCE(agreement::text, 'нет данных') AS agreement_value,
       agents_in_payload,
       count(*)                        AS n,
       count(*) FILTER (WHERE success) AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM calc
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 3.5 Доля успеха по КВАРТИЛЯМ probability (независимые окна целевой версии, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), q AS (
    SELECT *, ntile(4) OVER (ORDER BY probability) AS quartile
    FROM v1_indep WHERE probability IS NOT NULL
)
SELECT quartile,
       count(*)                            AS n,
       round(min(probability)::numeric, 4) AS prob_min,
       round(max(probability)::numeric, 4) AS prob_max,
       count(*) FILTER (WHERE success)     AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM q
GROUP BY quartile
ORDER BY quartile;

\echo
\echo '--- 3.6 КАЛИБРОВОЧНАЯ ТАБЛИЦА: заявленная вероятность против фактической доли успеха (независимые окна целевой версии) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
    ) q ORDER BY win, ts ASC
), b AS (
    SELECT *, least(width_bucket(probability, 0, 1, 5), 5) AS bucket
    FROM v1_indep WHERE probability IS NOT NULL
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END                                       AS probability_range,
       count(*)                                  AS n,
       round(avg(probability)::numeric, 4)       AS claimed_probability_avg,
       count(*) FILTER (WHERE success)           AS success_x,
       round((count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS actual_success_rate,
       round(avg(probability)::numeric - (count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS gap_claimed_minus_actual
FROM b
GROUP BY bucket
ORDER BY bucket;

\echo
\echo '--- 3.7 Та же калибровка по ПОЛНОЙ выборке целевой версии (наблюдения ЗАВИСИМЫ, доверительные интервалы неприменимы) ---'
WITH b AS (
    SELECT s.probability, e.success, least(width_bucket(s.probability, 0, 1, 5), 5) AS bucket
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = :target_version
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE AND s.probability IS NOT NULL
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END                                 AS probability_range,
       count(*)                            AS n,
       round(avg(probability)::numeric, 4) AS claimed_probability_avg,
       count(*) FILTER (WHERE success)     AS success_x,
       round((count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS actual_success_rate
FROM b
GROUP BY bucket
ORDER BY bucket;

\echo
\echo '--- 3.8 ЭТАП 7.3: КАЛИБРОВОЧНАЯ ТАБЛИЦА по calibrated_probability (независимые окна целевой версии) ---'
\echo '(в отличие от 3.6, здесь по горизонтали — вероятность, ВЫВЕДЕННАЯ из фактических исходов)'
WITH indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.calibrated_probability AS cp, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = :target_version
          AND s.decision <> 'wait'
          AND s.degraded = FALSE
          AND s.calibrated_probability IS NOT NULL
    ) q ORDER BY win, ts ASC
), b AS (
    SELECT *, least(width_bucket(cp, 0, 1, 5), 5) AS bucket FROM indep
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END                                 AS calibrated_range,
       count(*)                            AS n,
       round(avg(cp)::numeric, 4)          AS claimed_probability_avg,
       count(*) FILTER (WHERE success)     AS success_x,
       round((count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS actual_success_rate,
       round(avg(cp)::numeric - (count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS gap
FROM b
GROUP BY bucket
ORDER BY bucket;

\echo
\echo '--- 3.9 ЭТАП 7.3: построенные калибровочные кривые (история, активная помечена) ---'
SELECT id,
       logic_version,
       built_at,
       sample_size,
       window_from,
       window_to,
       round(base_rate::numeric, 4) AS base_rate,
       is_active,
       bins
FROM calibration_curves
ORDER BY logic_version, built_at DESC;

\echo
\echo '--- 3.10 ЭТАП 7.3: контроль — активная кривая на версию логики может быть только одна ---'
SELECT logic_version,
       count(*) FILTER (WHERE is_active) AS active_curves,
       CASE WHEN count(*) FILTER (WHERE is_active) <= 1
            THEN 'ОК' ELSE 'НАРУШЕНИЕ' END AS verdict
FROM calibration_curves
GROUP BY logic_version
ORDER BY logic_version;
