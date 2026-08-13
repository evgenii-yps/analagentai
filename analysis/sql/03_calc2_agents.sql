-- Этап 7.1 — Расчёт 2: вклад каждого агента (market / liquidity / futures).
-- Источник направления/уверенности агента в момент сигнала — signals.agents_payload
-- (JSONB-массив {agent, signal, confidence, ts}), сохранённый самим Decision Agent.
-- Это надёжнее, чем джойнить agent_outputs по времени: payload — ровно те выводы,
-- на которых принято ЭТО решение. Только независимые окна, logic_version=1.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/03_calc2_agents.sql

-- Разворачиваем payload независимых окон в строки «окно × агент».
WITH ev AS (
    SELECT signal_id, price_at_signal, price_at_close, (pnl_pct > 0) AS success_4h
    FROM signal_evaluations WHERE horizon = '4h'
),
base AS (
    SELECT s.id, s.ts, s.decision, s.agents_payload,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win_start,
           e.success_4h, (e.price_at_close > e.price_at_signal) AS price_up
    FROM signals s JOIN ev e ON e.signal_id = s.id
    WHERE COALESCE(s.logic_version, 1) = 1 AND s.decision IN ('buy', 'sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
ao AS (
    SELECT iw.id, iw.decision, iw.success_4h, iw.price_up,
           a->>'agent'              AS agent,
           a->>'signal'             AS asignal,
           (a->>'confidence')::float AS conf
    FROM iw, jsonb_array_elements(iw.agents_payload) a
)

-- 2.1 — Распределение уверенности по агенту.
SELECT
    agent,
    count(*)                                                                   AS n,
    round((percentile_cont(0.50) WITHIN GROUP (ORDER BY conf))::numeric, 4)    AS median,
    round((percentile_cont(0.25) WITHIN GROUP (ORDER BY conf))::numeric, 4)    AS p25,
    round((percentile_cont(0.75) WITHIN GROUP (ORDER BY conf))::numeric, 4)    AS p75,
    round((percentile_cont(0.99) WITHIN GROUP (ORDER BY conf))::numeric, 4)    AS p99,
    round(avg((conf = 0)::int)::numeric, 4)                                    AS share_zero,
    round(avg((conf < 0.01)::int)::numeric, 4)                                 AS share_lt_0_01
FROM ao GROUP BY agent ORDER BY agent;

-- 2.2 — Распределение направлений по агенту.
WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h, price_at_signal, price_at_close FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, s.decision, s.agents_payload, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
ao AS (SELECT a->>'agent' AS agent, a->>'signal' AS asignal FROM iw, jsonb_array_elements(iw.agents_payload) a)
SELECT agent, count(*) AS n,
    round(avg((asignal='bullish')::int)::numeric,4) AS bullish,
    round(avg((asignal='bearish')::int)::numeric,4) AS bearish,
    round(avg((asignal='neutral')::int)::numeric,4) AS neutral
FROM ao GROUP BY agent ORDER BY agent;

-- 2.3 — Связь направления агента с фактическим движением цены (сравнить с базовой линией §1.4).
WITH ev AS (SELECT signal_id, price_at_signal, price_at_close, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, s.agents_payload, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start, (e.price_at_close>e.price_at_signal) AS price_up FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
ao AS (SELECT iw.price_up, a->>'agent' AS agent, a->>'signal' AS asignal FROM iw, jsonb_array_elements(iw.agents_payload) a)
SELECT agent,
    count(*) FILTER (WHERE asignal='bullish')                                          AS n_bullish,
    round(avg(price_up::int) FILTER (WHERE asignal='bullish')::numeric,4)              AS when_bullish_price_up,
    count(*) FILTER (WHERE asignal='bearish')                                          AS n_bearish,
    round(avg((NOT price_up)::int) FILTER (WHERE asignal='bearish')::numeric,4)        AS when_bearish_price_down
FROM ao GROUP BY agent ORDER BY agent;

-- 2.4 — Связь уверенности с исходом: две группы по МЕДИАНЕ уверенности агента.
WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, s.agents_payload, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start, e.success_4h FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
ao AS (SELECT iw.success_4h, a->>'agent' AS agent, (a->>'confidence')::float AS conf FROM iw, jsonb_array_elements(iw.agents_payload) a),
med AS (SELECT agent, percentile_cont(0.5) WITHIN GROUP (ORDER BY conf) AS m FROM ao GROUP BY agent)
SELECT ao.agent,
    (ao.conf >= med.m) AS high_conf_group,
    count(*) AS n,
    round(avg(ao.success_4h::int)::numeric,4) AS success_rate
FROM ao JOIN med USING (agent)
GROUP BY ao.agent, high_conf_group
ORDER BY ao.agent, high_conf_group;

-- 2.5 — Согласие агента с итоговым решением (bullish↔buy, bearish↔sell).
WITH ev AS (SELECT signal_id FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, s.decision, s.agents_payload, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
ao AS (SELECT iw.decision, a->>'agent' AS agent, a->>'signal' AS asignal FROM iw, jsonb_array_elements(iw.agents_payload) a)
SELECT agent, count(*) AS n,
    round(avg(((asignal='bullish' AND decision='buy') OR (asignal='bearish' AND decision='sell'))::int)::numeric,4) AS agree_with_decision
FROM ao GROUP BY agent ORDER BY agent;
