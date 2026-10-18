-- ЭТАП 9.9, блок 00 — охват замера. Только чтение.
-- Что показывает: за какой период и по скольким позициям версии 7 посчитан весь отчёт.

\pset pager off
\pset null '-'
\pset footer off
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;   -- страховка: сессия не может писать
SET statement_timeout = '600s';
SET TIME ZONE 'UTC';
-- Момент замера. run_9_9.sh передаёт его ключом -v as_of=..., один на все блоки;
-- при ручном запуске файла отдельно берётся текущее время.
\if :{?as_of}
\else
  \set as_of now
\endif
\x on
-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.9 §3 (одинаковы во всех файлах; сверяет tests/test_stage_9_9.py) ===
WITH v7 AS (                        -- выборка: версия логики 7, виртуальные позиции
    SELECT p.id, p.instrument_id, i.symbol, p.status, p.opened_at, p.closed_at,
           p.entry_price, p.cost_pct, p.target_pct, p.notional_usd, p.exit_reason,
           p.net_pnl_pct, p.net_pnl_usd, p.mae_pct, p.mfe_pct
      FROM positions p
      JOIN instruments i ON i.id = p.instrument_id
     WHERE p.logic_version = 7 AND p.is_virtual
),
closed AS (                         -- закрытые для метрик: без data_gap
    SELECT v7.*,
           CASE WHEN net_pnl_pct >  0.0001 THEN 'win'
                WHEN net_pnl_pct < -0.0001 THEN 'loss'
                ELSE 'flat' END AS cls,
           extract(epoch FROM closed_at - opened_at) / 3600.0 AS hours
      FROM v7
     WHERE status = 'closed' AND exit_reason IS DISTINCT FROM 'data_gap'
),
opened AS (                         -- открытые по close последней закрытой минутной свечи
    SELECT v.*, px.close AS last_close, px.ts AS last_ts,
           CASE WHEN px.ts >= :'as_of'::timestamptz - interval '10 minutes'
                THEN (px.close::numeric / v.entry_price - 1) * 100 - v.cost_pct
           END AS cur_pct,          -- NULL = «нет цены» (свечи нет или она старше 10 минут)
           CASE WHEN px.ts >= :'as_of'::timestamptz - interval '10 minutes'
                THEN v.notional_usd * ((px.close::numeric / v.entry_price - 1) * 100 - v.cost_pct) / 100
           END AS cur_usd
      FROM v7 v
      LEFT JOIN LATERAL (
            SELECT o.close, o.ts
              FROM ohlcv o
             WHERE o.instrument_id = v.instrument_id AND o.timeframe = '1m'
               AND o.ts <= :'as_of'::timestamptz - interval '1 minute'
             ORDER BY o.ts DESC
             LIMIT 1) px ON true
     WHERE v.status = 'open'
)
-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ===
, mir AS (                       -- зеркалена ли позиция на демо: есть исполненная покупка
    SELECT v7.id, v7.status, v7.exit_reason,
           EXISTS (SELECT 1 FROM demo_orders d
                    WHERE d.position_id = v7.id AND d.leg = 'buy' AND d.status = 'filled') AS m
      FROM v7
)
SELECT
    to_char((SELECT min(opened_at) FROM v7) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
        AS "Начало версии 7 (UTC)",
    to_char(:'as_of'::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
        AS "Момент замера (UTC)",
    round(extract(epoch FROM :'as_of'::timestamptz - (SELECT min(opened_at) FROM v7)) / 86400.0, 1)
        AS "Дней с начала версии",
    (SELECT count(*) FROM v7)                                   AS "Позиций всего",
    (SELECT count(*) FROM closed)                               AS "Закрыто (в метриках)",
    (SELECT count(*) FROM v7
      WHERE status = 'closed' AND exit_reason = 'data_gap')     AS "Закрыто по data_gap (вне метрик)",
    (SELECT count(*) FROM v7 WHERE status = 'open')             AS "Открыто сейчас",
    (SELECT count(*) FROM mir WHERE m)                          AS "Зеркалено на демо (всего)",
    (SELECT count(*) FROM mir WHERE m AND status = 'closed' AND exit_reason IS DISTINCT FROM 'data_gap')
                                                                AS "  из них закрыто (в метриках)",
    (SELECT count(*) FROM mir WHERE m AND status = 'open')      AS "  из них открыто сейчас";
