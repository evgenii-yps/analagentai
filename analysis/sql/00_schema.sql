-- Этап 7.1 — Предварительный шаг: сверка схемы (§3 ТЗ).
-- Роль: agenttrade_ro (только SELECT). Запуск через stdin:
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/00_schema.sql
-- Ничего не изменяет: только метакоманды \dt / \d.

\echo '=== Список таблиц ==='
\dt

\echo '=== signals ==='
\d signals

\echo '=== agent_outputs ==='
\d agent_outputs

\echo '=== signal_evaluations ==='
\d signal_evaluations

\echo '=== signal_exports (для контекста выгрузки) ==='
\d signal_exports

\echo '=== agent_failures (Этап 7.0, для контекста пропусков) ==='
\d agent_failures

\echo '=== Проверка распределения logic_version и NULL (§4 ТЗ) ==='
SELECT COALESCE(logic_version::text, 'NULL') AS logic_version,
       count(*) AS rows,
       count(*) FILTER (WHERE status = 'closed') AS closed,
       min(ts) AS first_ts,
       max(ts) AS last_ts
FROM signals
GROUP BY logic_version
ORDER BY logic_version NULLS FIRST;

\echo '=== Горизонты в signal_evaluations ==='
SELECT horizon, count(*) AS rows FROM signal_evaluations GROUP BY horizon ORDER BY horizon;

\echo '=== Агенты в agent_outputs и диапазон дат ==='
SELECT agent, count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts
FROM agent_outputs GROUP BY agent ORDER BY agent;
