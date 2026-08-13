-- Этап 7.1 — Расчёт 3: информативность |балла|, согласованности, probability.
-- |балл| и согласованность извлекаются из signals.rationale, куда Decision Agent
-- пишет их в явном виде («балл=+0.86, согласованность=0.33 → buy»). probability —
-- колонка signals.probability. Только независимые окна, logic_version=1.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/05_calc3_score_consensus_prob.sql

WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (
    SELECT s.id, s.ts, s.probability, e.success_4h,
           to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start,
           (regexp_match(s.rationale, 'балл=([+-][0-9.]+)'))[1]::float           AS score,
           (regexp_match(s.rationale, 'согласованность=([0-9.]+)'))[1]::float    AS consensus
    FROM signals s JOIN ev e ON e.signal_id=s.id
    WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts)

-- 3.1 — Доля успеха по квартилям |балла|.
SELECT qt AS quartile_abs_score, count(*) AS n,
       round(min(abs(score))::numeric,3) AS lo, round(max(abs(score))::numeric,3) AS hi,
       round(avg(success_4h::int)::numeric,4) AS success_rate
FROM (SELECT *, ntile(4) OVER (ORDER BY abs(score)) AS qt FROM iw WHERE score IS NOT NULL) q
GROUP BY qt ORDER BY qt;

-- 3.2 — Доля успеха по дискретным значениям согласованности.
WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, e.success_4h, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start, (regexp_match(s.rationale,'согласованность=([0-9.]+)'))[1]::float AS consensus FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts)
SELECT round(consensus::numeric,2) AS consensus, count(*) AS n,
       round(avg(success_4h::int)::numeric,4) AS success_rate
FROM iw WHERE consensus IS NOT NULL GROUP BY round(consensus::numeric,2) ORDER BY consensus;

-- 3.3 — Доля успеха по квартилям probability.
WITH ev AS (SELECT signal_id, (pnl_pct>0) AS success_4h FROM signal_evaluations WHERE horizon='4h'),
base AS (SELECT s.id, s.ts, s.probability, e.success_4h, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start FROM signals s JOIN ev e ON e.signal_id=s.id WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts)
SELECT qt AS quartile_probability, count(*) AS n,
       round(min(probability)::numeric,3) AS lo, round(max(probability)::numeric,3) AS hi,
       round(avg(success_4h::int)::numeric,4) AS success_rate
FROM (SELECT *, ntile(4) OVER (ORDER BY probability) AS qt FROM iw WHERE probability IS NOT NULL) q
GROUP BY qt ORDER BY qt;
