-- Этап 7.1 — Расчёт 2 (отдельно): пропуски агента Market по суткам.
-- «Цикл» аппроксимируется 1-минутным ведром ts. Пропуск Market = ведро, в котором
-- есть вывод любого агента, но нет market. Ожидание (§6 ТЗ): после 13.08 15:41 UTC
-- пропуски прекращаются. Флаг after_fix разбивает поток по этой границе.
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/04_calc2_market_missing.sql

WITH per_min AS (
    SELECT date(ts)                                   AS d,
           date_trunc('minute', ts)                   AS m,
           (ts >= TIMESTAMPTZ '2026-08-13 15:41:00+00') AS after_fix,
           bool_or(agent = 'market')                  AS has_market
    FROM agent_outputs
    GROUP BY date(ts), date_trunc('minute', ts), (ts >= TIMESTAMPTZ '2026-08-13 15:41:00+00')
)
SELECT d,
       after_fix,
       count(*)                                   AS cycles,
       count(*) FILTER (WHERE NOT has_market)     AS market_missing,
       round(avg((NOT has_market)::int)::numeric, 4) AS missing_share
FROM per_min
GROUP BY d, after_fix
ORDER BY d, after_fix;

-- Итог за весь период до и после границы.
WITH per_min AS (
    SELECT date_trunc('minute', ts) AS m,
           (ts >= TIMESTAMPTZ '2026-08-13 15:41:00+00') AS after_fix,
           bool_or(agent = 'market') AS has_market
    FROM agent_outputs
    GROUP BY date_trunc('minute', ts), (ts >= TIMESTAMPTZ '2026-08-13 15:41:00+00')
)
SELECT after_fix,
       count(*) AS cycles,
       count(*) FILTER (WHERE NOT has_market) AS market_missing
FROM per_min GROUP BY after_fix ORDER BY after_fix;
