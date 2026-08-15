-- ЭТАП 7.1, раздел 3 ТЗ: сверка фактической схемы БД с предположениями ТЗ.
-- Только чтение. Ничего не создаётся и не изменяется.
--
-- Задача блока: до всех расчётов зафиксировать, КАК на самом деле называются
-- таблицы и колонки, хранятся ли балл (score) и согласованность (agreement)
-- отдельными колонками, чем заполнен logic_version у старых записей и с какой
-- даты заполняется degraded. Всё, что расходится с ТЗ, попадает в отчёт явно.

\pset pager off
SET default_transaction_read_only = on;   -- страховка: сессия не может писать
SET statement_timeout = '600s';

\echo
\echo '--- 0.1 Список таблиц схемы public ---'
\dt

\echo
\echo '--- 0.2 Структура signals ---'
\d signals

\echo
\echo '--- 0.3 Структура agent_outputs ---'
\d agent_outputs

\echo
\echo '--- 0.4 Структура signal_evaluations ---'
\d signal_evaluations

\echo
\echo '--- 0.5 Структура ohlcv ---'
\d ohlcv

\echo
\echo '--- 0.6 Структура agent_failures ---'
\d agent_failures

\echo
\echo '--- 0.7 Структура instruments ---'
\d instruments

\echo
\echo '--- 0.8 Есть ли где-либо колонки score / agreement (балл и согласованность)? ---'
\echo '(ожидание по коду: НЕТ ни одной — значения восстанавливаются из agents_payload/rationale)'
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (column_name ILIKE '%score%'
       OR column_name ILIKE '%agree%'
       OR column_name ILIKE '%соглас%'
       OR column_name ILIKE '%ball%')
ORDER BY table_name, column_name;

\echo
\echo '--- 0.9 Колонки signals: тип, NOT NULL, значение по умолчанию ---'
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'signals'
ORDER BY ordinal_position;

\echo
\echo '--- 0.10 logic_version: сколько записей в каждой версии, есть ли NULL ---'
\echo '(ожидание по коду: колонка NOT NULL DEFAULT 1, NULL быть не может)'
SELECT COALESCE(logic_version::text, 'NULL') AS logic_version,
       count(*)                              AS signals_total,
       count(*) FILTER (WHERE status = 'closed')      AS closed,
       count(*) FILTER (WHERE decision <> 'wait')     AS directional,
       min(ts)                               AS ts_from,
       max(ts)                               AS ts_to
FROM signals
GROUP BY logic_version
ORDER BY logic_version NULLS FIRST;

\echo
\echo '--- 0.11 Границы версий по данным (сверить с ТЗ: v2 c 13.08 15:41 UTC, v3 c 14.08 13:39 UTC) ---'
SELECT logic_version,
       min(ts) AS first_signal_utc,
       max(ts) AS last_signal_utc,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.12 degraded: с какой даты встречается true, сколько записей ---'
SELECT degraded,
       count(*)  AS signals_total,
       min(ts)   AS first_ts_utc,
       max(ts)   AS last_ts_utc
FROM signals
GROUP BY degraded
ORDER BY degraded;

\echo
\echo '--- 0.13 degraded=true в разрезе версий логики ---'
SELECT logic_version,
       count(*)                                AS signals_total,
       count(*) FILTER (WHERE degraded)        AS degraded_true,
       round(100.0 * count(*) FILTER (WHERE degraded) / NULLIF(count(*), 0), 2) AS degraded_pct
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.14 Формат rationale: примеры по версиям (откуда разбираются балл и согласованность) ---'
SELECT logic_version, decision, probability, left(rationale, 160) AS rationale_head
FROM (
    SELECT DISTINCT ON (logic_version, decision)
           logic_version, decision, probability, rationale
    FROM signals
    ORDER BY logic_version, decision, ts DESC
) t
ORDER BY logic_version, decision;

\echo
\echo '--- 0.15 Разбирается ли rationale регулярным выражением (доля непустых совпадений) ---'
SELECT logic_version,
       count(*)                                                             AS signals_total,
       count(*) FILTER (WHERE rationale ~ 'балл=')                          AS has_score_text,
       count(*) FILTER (WHERE rationale ~ 'согласованность=')               AS has_agreement_text,
       count(*) FILTER (WHERE agents_payload IS NOT NULL)                   AS has_payload,
       count(*) FILTER (WHERE jsonb_typeof(agents_payload) = 'array')       AS payload_is_array
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.16 Состав agents_payload: какие ключи лежат внутри (первая непустая запись каждой версии) ---'
SELECT logic_version, jsonb_pretty(agents_payload) AS agents_payload
FROM (
    SELECT DISTINCT ON (logic_version) logic_version, agents_payload
    FROM signals
    WHERE jsonb_typeof(agents_payload) = 'array'
      AND jsonb_array_length(agents_payload) > 0
    ORDER BY logic_version, ts DESC
) t
ORDER BY logic_version;

\echo
\echo '--- 0.17 signal_evaluations: горизонты и объём ---'
SELECT horizon,
       count(*)                                  AS evaluations,
       count(*) FILTER (WHERE success)           AS success_true,
       min(evaluated_at)                         AS first_eval,
       max(evaluated_at)                         AS last_eval
FROM signal_evaluations
GROUP BY horizon
ORDER BY horizon;

\echo
\echo '--- 0.18 ohlcv: какие таймфреймы реально собраны (ТЗ ожидает 4h — по коду его НЕТ) ---'
SELECT o.instrument_id,
       i.exchange, i.symbol, i.type,
       o.timeframe,
       count(*)  AS candles,
       min(o.ts) AS ts_from,
       max(o.ts) AS ts_to
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
GROUP BY o.instrument_id, i.exchange, i.symbol, i.type, o.timeframe
ORDER BY o.instrument_id, o.timeframe;

\echo
\echo '--- 0.19 Инструменты и привязка сигналов/выводов агентов к ним ---'
SELECT i.id, i.exchange, i.symbol, i.type,
       (SELECT count(*) FROM signals s       WHERE s.instrument_id = i.id) AS signals,
       (SELECT count(*) FROM agent_outputs a WHERE a.instrument_id = i.id) AS agent_outputs
FROM instruments i
ORDER BY i.id;

\echo
\echo '--- 0.20 agent_outputs: состав по агентам и допустимые значения signal ---'
SELECT agent, signal, count(*) AS rows, min(ts) AS ts_from, max(ts) AS ts_to
FROM agent_outputs
GROUP BY agent, signal
ORDER BY agent, signal;

\echo
\echo '--- 0.21 agent_failures: есть ли таблица и что в ней (наблюдаемость Этапа 7.0/7.2) ---'
SELECT agent, error_type, count(*) AS rows, min(ts) AS ts_from, max(ts) AS ts_to
FROM agent_failures
GROUP BY agent, error_type
ORDER BY agent, error_type;

\echo
\echo '--- 0.22 Роль подключения и режим сессии (контроль: только чтение) ---'
SELECT current_user            AS connected_as,
       current_database()      AS database,
       current_setting('default_transaction_read_only') AS read_only,
       now()                   AS server_time_utc;
