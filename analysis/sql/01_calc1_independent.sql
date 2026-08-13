-- Этап 7.1 — Расчёт 1: базовая линия и общая результативность.
-- ВЫБОРКА: независимые 4-часовые окна (границы 00/04/08/12/16/20 UTC),
-- из каждого окна — ПЕРВЫЙ по времени закрытый сигнал. Только logic_version=1,
-- только decision IN (buy, sell). success_4h := (signal_evaluations.pnl_pct > 0)
-- при horizon='4h' (в signals НЕТ колонки pnl_4h — см. отчёт §Схема).
-- Роль: agenttrade_ro. Запуск:
--   docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade < analysis/sql/01_calc1_independent.sql

-- Базовые CTE (повторяются в каждом файле Расчётов 1–3 и 5).
WITH ev AS (
    SELECT signal_id, pnl_pct, price_at_signal, price_at_close,
           (pnl_pct > 0) AS success_4h
    FROM signal_evaluations
    WHERE horizon = '4h'
),
base AS (
    SELECT
        s.id, s.ts, s.decision, s.probability,
        to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win_start,
        e.pnl_pct, e.price_at_signal, e.price_at_close, e.success_4h,
        (e.price_at_close > e.price_at_signal) AS price_up
    FROM signals s
    JOIN ev e ON e.signal_id = s.id
    WHERE COALESCE(s.logic_version, 1) = 1
      AND s.decision IN ('buy', 'sell')
),
iw AS (   -- одно независимое наблюдение на окно
    SELECT DISTINCT ON (win_start) *
    FROM base
    ORDER BY win_start, ts
)

-- 1.1–1.3 — N окон, доля успеха, средний/медианный pnl_4h — всего и по направлениям.
SELECT
    grp,
    count(*)                                                        AS n_windows,
    count(*) FILTER (WHERE success_4h)                              AS successes,
    round(avg(success_4h::int)::numeric, 4)                        AS success_rate,
    round(avg(pnl_pct)::numeric, 5)                                AS mean_pnl_4h,
    round((percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct))::numeric, 5) AS median_pnl_4h
FROM (
    SELECT 'all'  AS grp, * FROM iw
    UNION ALL SELECT 'buy',  * FROM iw WHERE decision = 'buy'
    UNION ALL SELECT 'sell', * FROM iw WHERE decision = 'sell'
) q
GROUP BY grp
ORDER BY grp;
