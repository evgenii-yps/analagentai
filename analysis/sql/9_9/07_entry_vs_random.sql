-- ЭТАП 9.9, блок 07 — вход системы против случайного момента (главный блок). Только чтение.
-- Что показывает: что было бы с ценой через 24 часа после входа системы — без правил
-- выхода и без издержек — в сравнении с тем, что цена делала через 24 часа после ЛЮБОГО
-- часа за тот же период. Если разница около нуля, преимущества у входов нет.
--
-- Вход системы: все позиции версии 7 (любого статуса), с открытия которых прошло >= 24 ч;
-- доходность = close минутной свечи, ближайшей к opened_at + 24 ч (не дальше 5 минут в
-- любую сторону), к entry_price. Нет такой свечи — позиция исключается и считается отдельно.
-- Случайный момент: для той же монеты каждый часовой бар периода версии 7,
-- close(t + 24 ч) / close(t) − 1.
-- n_eff — число различных календарных дней (UTC) открытия: сделки одного дня связаны.
-- Таблица собирается в JSON и печатается вторым запросом, чтобы строка-вывод брала числа
-- из того же расчёта (команда gset), а не из его копии.

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
, t AS (SELECT min(opened_at) AS t0 FROM v7),
elig AS (                           -- позиции, у которых прошло >= 24 часов
    SELECT v7.id, v7.instrument_id, v7.symbol, v7.opened_at, v7.entry_price,
           v7.opened_at + interval '24 hours' AS t24
      FROM v7
     WHERE v7.opened_at + interval '24 hours' <= :'as_of'::timestamptz
),
ent AS (                            -- ближайшая к t24 минутная свеча в пределах 5 минут
    SELECT e.*, c.close AS c24
      FROM elig e
      LEFT JOIN LATERAL (
            SELECT o.close
              FROM ohlcv o
             WHERE o.instrument_id = e.instrument_id AND o.timeframe = '1m'
               AND o.ts BETWEEN e.t24 - interval '5 minutes' AND e.t24 + interval '5 minutes'
             ORDER BY abs(extract(epoch FROM o.ts - e.t24)), o.ts
             LIMIT 1) c ON true
),
hrs AS (                            -- безусловная 24-часовая доходность по часовым барам, %
    SELECT o.instrument_id, i.symbol, o.ts,
           (o2.close / o.close - 1) * 100 AS r
      FROM ohlcv o
      JOIN instruments i ON i.id = o.instrument_id
      JOIN ohlcv o2 ON o2.instrument_id = o.instrument_id AND o2.timeframe = '1h'
                   AND o2.ts = o.ts + interval '24 hours'
     CROSS JOIN t
     WHERE o.timeframe = '1h'
       AND o.instrument_id IN (SELECT DISTINCT instrument_id FROM v7)
       AND o.ts >= date_trunc('hour', t.t0)
       AND o2.ts <= :'as_of'::timestamptz
       AND o.close > 0
),
pe AS (                             -- доходность входа системы, %, и безусловная в час входа
    SELECT e.symbol, e.opened_at, (e.c24 / e.entry_price - 1) * 100 AS r_e, h.r AS r_hour
      FROM ent e
      LEFT JOIN hrs h ON h.instrument_id = e.instrument_id AND h.ts = date_trunc('hour', e.opened_at)
     WHERE e.c24 IS NOT NULL
),
e_agg AS (
    SELECT symbol, count(*) AS n_e,
           count(DISTINCT (opened_at AT TIME ZONE 'UTC')::date) AS n_eff,
           avg(r_e) AS m_e,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY r_e) AS med_e,
           100.0 * avg((r_e > 0)::int) AS pos_e,
           100.0 * avg((r_hour > 0)::int) AS in_pos_hours
      FROM pe
     GROUP BY ROLLUP (symbol)
),
x_agg AS (SELECT symbol, count(*) AS n_x FROM ent WHERE c24 IS NULL GROUP BY ROLLUP (symbol)),
h_agg AS (
    SELECT symbol, count(*) AS n_h, avg(r) AS m_h,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY r) AS med_h,
           100.0 * avg((r > 0)::int) AS pos_h
      FROM hrs
     GROUP BY ROLLUP (symbol)
),
s AS (SELECT DISTINCT symbol FROM v7 UNION ALL SELECT NULL::text),
tbl AS (
    SELECT s.symbol,
           coalesce(e.n_e, 0) AS n_e, coalesce(x.n_x, 0) AS n_x, coalesce(e.n_eff, 0) AS n_eff,
           round(e.m_e::numeric, 2) AS m_e, round(e.med_e::numeric, 2) AS med_e,
           round(coalesce(h.n_h, 0)::numeric, 0) AS n_h,
           round(h.m_h::numeric, 2) AS m_h, round(h.med_h::numeric, 2) AS med_h,
           round((e.m_e - h.m_h)::numeric, 2) AS d,
           round(e.pos_e::numeric, 2) AS pos_e, round(h.pos_h::numeric, 2) AS pos_h,
           round(e.in_pos_hours::numeric, 2) AS in_pos_hours
      FROM s
      LEFT JOIN e_agg e ON e.symbol IS NOT DISTINCT FROM s.symbol
      LEFT JOIN x_agg x ON x.symbol IS NOT DISTINCT FROM s.symbol
      LEFT JOIN h_agg h ON h.symbol IS NOT DISTINCT FROM s.symbol
)
SELECT (SELECT coalesce(json_agg(json_build_object(
                'sym', coalesce(symbol, 'ВСЕ'), 'ord', CASE WHEN symbol IS NULL THEN 1 ELSE 0 END,
                'n_e', n_e, 'n_x', n_x, 'n_eff', n_eff, 'm_e', m_e, 'med_e', med_e,
                'n_h', n_h, 'm_h', m_h, 'med_h', med_h, 'd', d,
                'pos_e', pos_e, 'pos_h', pos_h, 'in_pos_hours', in_pos_hours)), '[]'::json)
          FROM tbl)                                               AS rows,
       (SELECT coalesce(d::text, 'нет данных') FROM tbl WHERE symbol IS NULL)      AS d_all,
       (SELECT n_eff::text FROM tbl WHERE symbol IS NULL)                          AS neff_all
\gset

SELECT x.sym                  AS "Монета",
       x.n_e                  AS "Входов",
       x.n_x                  AS "Без св.",
       x.n_eff                AS "n_eff",
       x.m_e                  AS "Вх. ср., %",
       x.med_e                AS "Вх. мед., %",
       x.n_h                  AS "Часов",
       x.m_h                  AS "Сл. ср., %",
       x.med_h                AS "Сл. мед., %",
       x.d                    AS "Разн., п.п.",
       x.pos_e                AS "Вх. >0, %",
       x.pos_h                AS "Сл. >0, %",
       x.in_pos_hours         AS "Вх. в «+» ч, %"
  FROM json_to_recordset(:'rows'::json)
       AS x(sym text, ord int, n_e int, n_x int, n_eff int, m_e numeric, med_e numeric,
            n_h int, m_h numeric, med_h numeric, d numeric, pos_e numeric, pos_h numeric,
            in_pos_hours numeric)
 ORDER BY x.ord, x.sym;

\echo
\echo Вывод без интерпретации: разница средних = :d_all п.п., n_eff = :neff_all
