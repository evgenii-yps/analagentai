-- Этап 7.1 — Расчёт 5: попарная согласованность направлений агентов.
-- По независимым окнам, из signals.agents_payload: доля окон, где направления
-- двух агентов совпали (по окнам, где оба присутствуют). logic_version=1.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/08_calc5_pairwise.sql

WITH ev AS (SELECT signal_id FROM signal_evaluations WHERE horizon='4h'),
base AS (
    SELECT s.id, s.ts, s.agents_payload,
           to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400) AS win_start
    FROM signals s JOIN ev e ON e.signal_id=s.id
    WHERE COALESCE(s.logic_version,1)=1 AND s.decision IN ('buy','sell')
),
iw AS (SELECT DISTINCT ON (win_start) * FROM base ORDER BY win_start, ts),
dir AS (
    SELECT id AS sig_id,
        max(CASE WHEN a->>'agent'='market'    THEN a->>'signal' END) AS market,
        max(CASE WHEN a->>'agent'='liquidity' THEN a->>'signal' END) AS liquidity,
        max(CASE WHEN a->>'agent'='futures'   THEN a->>'signal' END) AS futures
    FROM iw, jsonb_array_elements(iw.agents_payload) a
    GROUP BY id
)
SELECT
    count(*) FILTER (WHERE market IS NOT NULL AND liquidity IS NOT NULL)                         AS n_market_liquidity,
    round(avg((market=liquidity)::int) FILTER (WHERE market IS NOT NULL AND liquidity IS NOT NULL)::numeric,4)   AS market_liquidity_agree,
    count(*) FILTER (WHERE market IS NOT NULL AND futures IS NOT NULL)                           AS n_market_futures,
    round(avg((market=futures)::int) FILTER (WHERE market IS NOT NULL AND futures IS NOT NULL)::numeric,4)       AS market_futures_agree,
    count(*) FILTER (WHERE liquidity IS NOT NULL AND futures IS NOT NULL)                        AS n_liquidity_futures,
    round(avg((liquidity=futures)::int) FILTER (WHERE liquidity IS NOT NULL AND futures IS NOT NULL)::numeric,4) AS liquidity_futures_agree
FROM dir;
