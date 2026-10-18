-- ЭТАП 9.10, блок 04 — история: дневная доходность после флага (главный блок по мощности). Только чтение.
-- Что показывает: наблюдение = монета×день, r = close(d)/open(d) − 1 (покупка на открытии дня,
-- продажа на закрытии, без комиссий). Для дня берётся среднее r по монетам с данной группой флага;
-- сравниваются средние по дням для FALSE (покупать можно) и TRUE (вето), разница = FALSE − TRUE
-- (положительная — вето помогает). Для V1–V3 флаг у всех монет дня общий (BTC); для V4 у каждой
-- монеты свой. Разрезы: сводно, две половины времени (граница 2024-04-01, записана заранее),
-- по монетам (одно наблюдение на день), по календарным годам. Доходность в % за день.
-- Тест значимости по этим данным — блок 05 (строки «@@R04» уходят в файл фактов).

\pset pager off
\pset null '-'
\pset footer off
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;   -- страховка: сессия не может писать
SET statement_timeout = '600s';
SET TIME ZONE 'UTC';
-- Момент замера. run_9_10.sh передаёт его ключом -v as_of=..., один на все блоки;
-- при ручном запуске файла отдельно берётся текущее время.
\if :{?as_of}
\else
  \set as_of now
\endif

-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.1–4.2: часовой и дневной ряды (одинаковы во всех файлах; сверяет tests/test_stage_9_10.py) ===
WITH tok(token) AS (VALUES ('BTC'), ('ETH'), ('SOL'), ('XRP'), ('DOGE')),
h_hist AS (                         -- история биржи: backtest.candles, закрытые бары
    SELECT split_part(c.inst_id, '-', 1) AS token, c.open_time,
           c.open::double precision AS open, c.close::double precision AS close
      FROM backtest.candles c
     WHERE c.bar = '1H'
       AND c.inst_id IN ('BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT', 'DOGE-USDT')
       AND c.open_time + interval '1 hour' <= :'as_of'::timestamptz
),
h_live AS (                         -- живой сборщик: public.ohlcv, закрытые бары
    SELECT split_part(i.symbol, '/', 1) AS token, o.ts AS open_time, o.open, o.close
      FROM ohlcv o
      JOIN instruments i ON i.id = o.instrument_id
     WHERE o.timeframe = '1h' AND i.type = 'spot'
       AND i.symbol IN ('BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT')
       AND o.ts + interval '1 hour' <= :'as_of'::timestamptz
),
h_last AS (                         -- последний час истории по монете
    SELECT token, max(open_time) AS t FROM h_hist GROUP BY token
),
hourly AS (                         -- слияние: H-hist везде, где он есть; H-live — только после его последнего часа
    SELECT token, open_time, open, close FROM h_hist
    UNION ALL
    SELECT l.token, l.open_time, l.open, l.close
      FROM h_live l
      LEFT JOIN h_last x ON x.token = l.token
     WHERE l.open_time > coalesce(x.t, '-infinity'::timestamptz)
),
daily_all AS (                      -- сутки UTC из часов; n_h < 24 — неполные сутки
    SELECT token, (open_time AT TIME ZONE 'UTC')::date AS day,
           (array_agg(open ORDER BY open_time))[1] AS open,
           (array_agg(close ORDER BY open_time DESC))[1] AS close,
           count(*) AS n_h
      FROM hourly
     GROUP BY token, (open_time AT TIME ZONE 'UTC')::date
),
daily AS (                          -- неполные сутки НЕ создаются (пропуск, а не синтетика)
    SELECT token, day, open, close FROM daily_all WHERE n_h = 24
)
-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ РЯДОВ ===

-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.3: флаги тренда (одинаковы во всех файлах; сверяет tests/test_stage_9_10.py) ===
-- Флаг дня d считается ТОЛЬКО по дневным барам, закрытым до начала дня d (последний — d−1):
-- окна ниже заканчиваются на «1 PRECEDING». Флаг TRUE означает «вето». Нет полного окна — NULL.
, cal AS (                          -- календарь без дыр: каждая дата для каждой монеты
    SELECT t.token, g::date AS day
      FROM tok t
     CROSS JOIN generate_series((SELECT min(day) FROM daily),
                                (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date,
                                interval '1 day') g
),
dser AS (
    SELECT c.token, c.day, d.close
      FROM cal c
      LEFT JOIN daily d ON d.token = c.token AND d.day = c.day
),
fw AS (
    SELECT token, day,
           count(close) OVER w50  AS n50,  avg(close) OVER w50  AS sma50,
           count(close) OVER w20a AS n20a, avg(close) OVER w20a AS sma20a,
           count(close) OVER w20b AS n20b, avg(close) OVER w20b AS sma20b,
           count(close) OVER w8   AS n8,
           lag(close, 1) OVER o   AS c1,
           lag(close, 8) OVER o   AS c8
      FROM dser
    WINDOW o    AS (PARTITION BY token ORDER BY day),
           w50  AS (o ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING),   -- d−50 … d−1
           w20a AS (o ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),   -- SMA20(d−1): d−20 … d−1
           w20b AS (o ROWS BETWEEN 25 PRECEDING AND 6 PRECEDING),   -- SMA20(d−6): d−25 … d−6
           w8   AS (o ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING)     -- d−8 … d−1
),
flags AS (
    SELECT token, day,
           CASE WHEN n50 = 50 THEN c1 < sma50 END                       AS v1,   -- close(d−1) < SMA50(d−1)
           CASE WHEN n20a = 20 AND n20b = 20 THEN sma20a < sma20b END   AS v2,   -- SMA20(d−1) < SMA20(d−6)
           CASE WHEN n8 = 8 THEN c1 < c8 END                            AS v3,   -- close(d−1) < close(d−8)
           CASE WHEN n50 = 50 THEN c1 < sma50 END                       AS v4    -- как V1, по своей монете
      FROM fw
)
-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ФЛАГОВ ===

, obs AS (
    SELECT d.token, d.day, d.close / d.open - 1 AS r, b.v1, b.v2, b.v3, f.v4
      FROM daily d
      JOIN flags f ON f.token = d.token AND f.day = d.day
      JOIN flags b ON b.token = 'BTC' AND b.day = d.day
     WHERE d.open > 0
),
ol AS (
    SELECT 'V1' AS rule, token, day, v1 AS flag, r FROM obs WHERE v1 IS NOT NULL
    UNION ALL SELECT 'V2', token, day, v2, r FROM obs WHERE v2 IS NOT NULL
    UNION ALL SELECT 'V3', token, day, v3, r FROM obs WHERE v3 IS NOT NULL
    UNION ALL SELECT 'V4', token, day, v4, r FROM obs WHERE v4 IS NOT NULL
),
dm AS (SELECT rule, day, flag, avg(r) AS m FROM ol GROUP BY rule, day, flag),
sm AS (
    SELECT rule, min(day) AS d0, max(day) AS d1,
           count(*) FILTER (WHERE NOT flag) AS n_f, count(*) FILTER (WHERE flag) AS n_t,
           avg(m) FILTER (WHERE NOT flag) AS m_f, avg(m) FILTER (WHERE flag) AS m_t
      FROM dm GROUP BY rule
),
hv AS (
    SELECT rule, (day >= date '2024-04-01') AS h2,
           count(*) FILTER (WHERE NOT flag) AS n_f, count(*) FILTER (WHERE flag) AS n_t,
           avg(m) FILTER (WHERE NOT flag) AS m_f, avg(m) FILTER (WHERE flag) AS m_t
      FROM dm GROUP BY rule, (day >= date '2024-04-01')
),
cn AS (
    SELECT rule, token,
           count(*) FILTER (WHERE NOT flag) AS n_f, count(*) FILTER (WHERE flag) AS n_t,
           avg(r) FILTER (WHERE NOT flag) AS m_f, avg(r) FILTER (WHERE flag) AS m_t
      FROM ol GROUP BY rule, token
),
yr AS (
    SELECT rule, extract(year FROM day)::int AS yr,
           count(*) FILTER (WHERE NOT flag) AS n_f, count(*) FILTER (WHERE flag) AS n_t,
           avg(m) FILTER (WHERE NOT flag) AS m_f, avg(m) FILTER (WHERE flag) AS m_t
      FROM dm GROUP BY rule, extract(year FROM day)::int
)
SELECT (SELECT coalesce(json_agg(json_build_object(
              'rule', s.rule, 'd0', s.d0, 'd1', s.d1, 'n_f', s.n_f, 'n_t', s.n_t,
              'm_f', round((100 * s.m_f)::numeric, 4), 'm_t', round((100 * s.m_t)::numeric, 4),
              'd', round((100 * (s.m_f - s.m_t))::numeric, 4),
              'dx', 100 * (s.m_f - s.m_t)) ORDER BY s.rule), '[]'::json) FROM sm s) AS j_sm,
       (SELECT coalesce(json_agg(json_build_object(
              'rule', h.rule, 'h2', h.h2::int, 'n_f', h.n_f, 'n_t', h.n_t,
              'd', round((100 * (h.m_f - h.m_t))::numeric, 4),
              'dx', 100 * (h.m_f - h.m_t)) ORDER BY h.rule, h.h2), '[]'::json) FROM hv h) AS j_hv,
       (SELECT coalesce(json_agg(json_build_object(
              'rule', c.rule, 'token', c.token, 'n_f', c.n_f, 'n_t', c.n_t,
              'd', round((100 * (c.m_f - c.m_t))::numeric, 4),
              'dx', 100 * (c.m_f - c.m_t)) ORDER BY c.rule, c.token), '[]'::json) FROM cn c) AS j_cn,
       (SELECT coalesce(json_agg(json_build_object(
              'rule', y.rule, 'yr', y.yr, 'n_f', y.n_f, 'n_t', y.n_t,
              'd', round((100 * (y.m_f - y.m_t))::numeric, 4)) ORDER BY y.rule, y.yr), '[]'::json) FROM yr y) AS j_yr,
       (SELECT coalesce(json_agg(json_build_object(
              'day', o.day, 'token', o.token, 'v1', o.v1::int, 'v2', o.v2::int, 'v3', o.v3::int,
              'v4', o.v4::int, 'r', 100 * o.r) ORDER BY o.day, o.token), '[]'::json) FROM obs o) AS j_csv
\gset

\echo -- Сводно по дням: средняя дневная доходность по группам флага, разница FALSE − TRUE --
SELECT x.rule AS "Прав.", x.d0 AS "С дня", x.d1 AS "По день", x.n_f AS "Дней FALSE", x.n_t AS "Дней TRUE",
       x.m_f AS "Ср. FALSE, %", x.m_t AS "Ср. TRUE, %", x.d AS "FALSE − TRUE, п.п."
  FROM json_to_recordset(:'j_sm'::json)
       AS x(rule text, d0 date, d1 date, n_f int, n_t int, m_f numeric, m_t numeric, d numeric)
 ORDER BY x.rule;

\echo -- По половинам времени (граница 2024-04-01): дней FALSE/TRUE и разница FALSE − TRUE, п.п. --
SELECT x.rule AS "Прав.",
       max(x.n_f) FILTER (WHERE x.h2 = 0) AS "До: дн.F", max(x.n_t) FILTER (WHERE x.h2 = 0) AS "дн.T",
       max(x.d) FILTER (WHERE x.h2 = 0) AS "До: разница",
       max(x.n_f) FILTER (WHERE x.h2 = 1) AS "С: дн.F", max(x.n_t) FILTER (WHERE x.h2 = 1) AS "дн.T",
       max(x.d) FILTER (WHERE x.h2 = 1) AS "С: разница"
  FROM json_to_recordset(:'j_hv'::json) AS x(rule text, h2 int, n_f int, n_t int, d numeric)
 GROUP BY x.rule ORDER BY x.rule;

\echo -- По монетам: разница FALSE − TRUE, п.п. (одно наблюдение на день) --
SELECT x.token AS "Монета",
       max(x.d) FILTER (WHERE x.rule = 'V1') AS "V1", max(x.d) FILTER (WHERE x.rule = 'V2') AS "V2",
       max(x.d) FILTER (WHERE x.rule = 'V3') AS "V3", max(x.d) FILTER (WHERE x.rule = 'V4') AS "V4"
  FROM json_to_recordset(:'j_cn'::json) AS x(rule text, token text, n_f int, n_t int, d numeric)
 GROUP BY x.token ORDER BY x.token;

\echo -- По календарным годам: разница FALSE − TRUE, п.п. --
SELECT x.yr AS "Год",
       max(x.d) FILTER (WHERE x.rule = 'V1') AS "V1", max(x.d) FILTER (WHERE x.rule = 'V2') AS "V2",
       max(x.d) FILTER (WHERE x.rule = 'V3') AS "V3", max(x.d) FILTER (WHERE x.rule = 'V4') AS "V4"
  FROM json_to_recordset(:'j_yr'::json) AS x(rule text, yr int, n_f int, n_t int, d numeric)
 GROUP BY x.yr ORDER BY x.yr;

\pset format unaligned
\pset tuples_only on
SELECT '@@F|' || x.rule || '.d_all|' || coalesce(x.dx::text, '')
  FROM json_to_recordset(:'j_sm'::json) AS x(rule text, dx numeric);
SELECT '@@F|' || x.rule || '.d_h' || (x.h2 + 1) || '|' || coalesce(x.dx::text, '')
  FROM json_to_recordset(:'j_hv'::json) AS x(rule text, h2 int, dx numeric);
SELECT '@@F|' || x.rule || '.c_' || x.token || '|' || coalesce(x.dx::text, '')
  FROM json_to_recordset(:'j_cn'::json) AS x(rule text, token text, dx numeric);
SELECT '@@R04|' || x.day || '|' || x.token || '|' || coalesce(x.v1::text, '') || '|' || coalesce(x.v2::text, '')
       || '|' || coalesce(x.v3::text, '') || '|' || coalesce(x.v4::text, '') || '|' || x.r
  FROM json_to_recordset(:'j_csv'::json) AS x(day date, token text, v1 int, v2 int, v3 int, v4 int, r numeric);
\pset tuples_only off
\pset format aligned
