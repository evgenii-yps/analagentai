-- ЭТАП 9.4, ДОКЛАД D.1 — ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL.
--
-- Снимает с БОЕВОЙ базы факты, которых нет в репозитории: фактический DDL двух
-- таблиц замеров (миграции 016/017 могли разойтись с базой), размеры, состав
-- по версиям логики и суткам, признак того, пишет ли в таблицы кто-то сейчас.
--
-- Запуск (с сервера, из /opt/agent-trade):
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < scripts/report_9_4_d1.sql > analysis_out/report_9_4_d1.txt
--
-- Транзакция READ ONLY — записать что-либо этот файл технически не может.

BEGIN READ ONLY;
\pset pager off
\timing off

\echo '=== 1. DDL: trailing_outcomes ==='
\d+ trailing_outcomes
\echo
\echo '=== 1. DDL: strategy_outcomes ==='
\d+ strategy_outcomes

\echo
\echo '=== 2. Размеры (данные / индексы / всего) и число строк ==='
SELECT c.relname AS table,
       pg_size_pretty(pg_relation_size(c.oid))       AS data,
       pg_size_pretty(pg_indexes_size(c.oid))        AS indexes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       s.n_live_tup                                  AS approx_rows
  FROM pg_class c
  JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE c.relname IN ('trailing_outcomes', 'strategy_outcomes');

SELECT 'trailing_outcomes' AS table, count(*) AS exact_rows FROM trailing_outcomes
UNION ALL
SELECT 'strategy_outcomes', count(*) FROM strategy_outcomes;

\echo
\echo '=== 3. Пишет ли кто-то сейчас: свежесть computed_at ==='
SELECT 'trailing_outcomes' AS table,
       max(computed_at) AS last_computed_at,
       count(*) FILTER (WHERE computed_at > now() - interval '24 hours') AS rows_24h,
       count(*) FILTER (WHERE computed_at > now() - interval '7 days')   AS rows_7d
  FROM trailing_outcomes
UNION ALL
SELECT 'strategy_outcomes', max(computed_at),
       count(*) FILTER (WHERE computed_at > now() - interval '24 hours'),
       count(*) FILTER (WHERE computed_at > now() - interval '7 days')
  FROM strategy_outcomes;

\echo
\echo '=== 3a. Строки по суткам ВРЕМЕНИ ВЫЧИСЛЕНИЯ (последние 14) ==='
SELECT 'trailing' AS tbl, computed_at::date AS day, count(*) AS rows
  FROM trailing_outcomes GROUP BY 2 ORDER BY 2 DESC LIMIT 14;
SELECT 'strategy' AS tbl, computed_at::date AS day, count(*) AS rows
  FROM strategy_outcomes GROUP BY 2 ORDER BY 2 DESC LIMIT 14;

\echo
\echo '=== 4. Состав по версиям логики ==='
SELECT 'trailing' AS tbl, logic_version, count(*) AS rows,
       min(computed_at)::date AS first_computed, max(computed_at)::date AS last_computed
  FROM trailing_outcomes GROUP BY 2 ORDER BY 2;
SELECT 'strategy' AS tbl, logic_version, count(*) AS rows,
       min(entry_ts)::date AS first_entry, max(entry_ts)::date AS last_entry
  FROM strategy_outcomes GROUP BY 2 ORDER BY 2;

\echo
\echo '=== 5. Диапазон времени сигнала (день для свёртки trailing = signals.ts::date) ==='
SELECT min(s.ts)::date AS first_signal_day, max(s.ts)::date AS last_signal_day,
       count(DISTINCT s.ts::date) AS days
  FROM trailing_outcomes t JOIN signals s ON s.id = t.signal_id;

\echo
\echo '=== 6. Строки, которые попадут под удаление при сроке 30 суток (по дню сигнала/входа) ==='
SELECT 'trailing_outcomes' AS tbl, count(*) AS rows_older_than_30d
  FROM trailing_outcomes t JOIN signals s ON s.id = t.signal_id
 WHERE s.ts < now() - interval '30 days'
UNION ALL
SELECT 'strategy_outcomes', count(*) FROM strategy_outcomes WHERE entry_ts < now() - interval '30 days';

\echo
\echo '=== 7. Что ещё в БД зависит от таблиц (представления, правила, триггеры, функции) ==='
SELECT DISTINCT d.refobjid::regclass AS depends_on, c.relname AS dependent_view
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
 WHERE d.refobjid IN ('trailing_outcomes'::regclass, 'strategy_outcomes'::regclass)
   AND c.oid <> d.refobjid;
SELECT tgrelid::regclass AS table, tgname FROM pg_trigger
 WHERE NOT tgisinternal AND tgrelid IN ('trailing_outcomes'::regclass, 'strategy_outcomes'::regclass);
SELECT p.proname FROM pg_proc p
 WHERE p.pronamespace = 'public'::regnamespace
   AND (p.prosrc ILIKE '%trailing_outcomes%' OR p.prosrc ILIKE '%strategy_outcomes%');
SELECT conrelid::regclass AS referencing_table, conname
  FROM pg_constraint
 WHERE contype = 'f' AND confrelid IN ('trailing_outcomes'::regclass, 'strategy_outcomes'::regclass);

\echo
\echo '=== 8. Обращались ли к таблицам чтением (счётчики с последнего сброса статистики) ==='
SELECT relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del
  FROM pg_stat_user_tables WHERE relname IN ('trailing_outcomes', 'strategy_outcomes');
SELECT stats_reset FROM pg_stat_database WHERE datname = current_database();

ROLLBACK;
