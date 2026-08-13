-- Этап 7.1 — Расчёт 4: фактическая частота обновления входных данных.
-- По agent_outputs, для каждого агента: длина серий подряд идущих одинаковых
-- confidence, доля неизменившихся циклов, число уникальных значений за сутки.
-- Домен logic_version=1: ts < 2026-08-13 15:41 UTC (у agent_outputs нет колонки
-- версии — фильтр по времени; см. отчёт §Схема).
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/07_calc4_runlengths.sql

WITH o AS (
    SELECT agent, ts, confidence,
           lag(confidence) OVER (PARTITION BY agent ORDER BY ts) AS prev
    FROM agent_outputs
    WHERE ts < TIMESTAMPTZ '2026-08-13 15:41:00+00'
),
marked AS (SELECT *, (confidence IS DISTINCT FROM prev) AS changed FROM o),
grp AS (SELECT *, sum(changed::int) OVER (PARTITION BY agent ORDER BY ts) AS run_id FROM marked),
runs AS (SELECT agent, run_id, count(*) AS run_len FROM grp GROUP BY agent, run_id)

-- 4.1 — Средняя и максимальная длина серии одинаковых confidence.
SELECT agent,
       count(*)                          AS n_runs,
       round(avg(run_len)::numeric, 2)   AS avg_run_len,
       max(run_len)                      AS max_run_len
FROM runs GROUP BY agent ORDER BY agent;

-- 4.2 — Доля циклов, в которых confidence не изменился относительно предыдущего.
WITH o AS (SELECT agent, ts, confidence, lag(confidence) OVER (PARTITION BY agent ORDER BY ts) AS prev FROM agent_outputs WHERE ts < TIMESTAMPTZ '2026-08-13 15:41:00+00'),
marked AS (SELECT *, (confidence IS DISTINCT FROM prev) AS changed FROM o)
SELECT agent,
       count(*) FILTER (WHERE prev IS NOT NULL)                                  AS n_cycles,
       round(avg((NOT changed)::int) FILTER (WHERE prev IS NOT NULL)::numeric,4) AS share_repeat
FROM marked GROUP BY agent ORDER BY agent;

-- 4.3 — Число уникальных значений confidence за сутки по агенту.
SELECT agent, date(ts) AS d,
       count(*)                    AS cycles,
       count(DISTINCT confidence)  AS uniq_conf
FROM agent_outputs
WHERE ts < TIMESTAMPTZ '2026-08-13 15:41:00+00'
GROUP BY agent, date(ts)
ORDER BY agent, d;
