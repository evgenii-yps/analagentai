-- ЭТАП 9.10, блок 01 — доля дней под вето. Только чтение.
-- Что показывает: как часто каждое из правил V1–V4 (ТЗ 9.10 §4.3) говорит «вето» — за всю
-- историю и за период версии 7 (с открытия первой позиции версии 7 по момент замера), а для V1
-- ещё по неделям периода версии 7. День считается по календарю UTC; флаг дня d берётся по
-- барам до d−1. V1–V3 считаются по BTC (один ряд дней), V4 — по каждой из пяти монет
-- (единица — монета×день). NULL — не хватило баров для окна.

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

, fl AS (
    SELECT 'V1' AS rule, day, v1 AS flag FROM flags WHERE token = 'BTC'
    UNION ALL SELECT 'V2', day, v2 FROM flags WHERE token = 'BTC'
    UNION ALL SELECT 'V3', day, v3 FROM flags WHERE token = 'BTC'
    UNION ALL SELECT 'V4', day, v4 FROM flags
),
t0 AS (
    SELECT (min(opened_at) AT TIME ZONE 'UTC')::date AS d
      FROM positions WHERE logic_version = 7 AND is_virtual
),
per AS (
    SELECT 1 AS ord, 'вся история' AS per, (SELECT min(day) FROM daily) AS d0,
           (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date AS d1
    UNION ALL
    SELECT 2, 'версия 7', (SELECT d FROM t0), (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date
),
sh AS (
    SELECT f.rule, p.ord, p.per, count(*) AS n,
           count(*) FILTER (WHERE f.flag) AS nt,
           count(*) FILTER (WHERE NOT f.flag) AS nf,
           count(*) FILTER (WHERE f.flag IS NULL) AS nn
      FROM fl f
      JOIN per p ON f.day BETWEEN p.d0 AND p.d1
     GROUP BY f.rule, p.ord, p.per
),
wk AS (
    SELECT to_char(f.day, 'IYYY-"W"IW') AS wk, min(f.day) AS d_from, max(f.day) AS d_to,
           count(*) AS n, count(*) FILTER (WHERE f.flag) AS nt,
           count(*) FILTER (WHERE f.flag IS NULL) AS nn
      FROM fl f
     WHERE f.rule = 'V1'
       AND f.day BETWEEN (SELECT d FROM t0) AND (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date
     GROUP BY to_char(f.day, 'IYYY-"W"IW')
)
SELECT (SELECT coalesce(json_agg(json_build_object(
              'rule', s.rule, 'per', s.per, 'n', s.n, 'nt', s.nt, 'nf', s.nf, 'nn', s.nn,
              'ord', s.ord) ORDER BY s.ord, s.rule), '[]'::json) FROM sh s) AS j_sh,
       (SELECT coalesce(json_agg(json_build_object(
              'wk', w.wk, 'd_from', w.d_from, 'd_to', w.d_to, 'n', w.n, 'nt', w.nt, 'nn', w.nn)
              ORDER BY w.d_from), '[]'::json) FROM wk w) AS j_wk
\gset

\echo -- Доля дней: TRUE = вето, FALSE = покупать можно, NULL = флага нет (V4: монета×день) --
SELECT x.per AS "Период", x.rule AS "Правило", x.n AS "Дней", x.nt AS "TRUE", x.nf AS "FALSE",
       x.nn AS "NULL",
       round(100.0 * x.nt / nullif(x.n, 0), 1) AS "TRUE, %",
       round(100.0 * x.nf / nullif(x.n, 0), 1) AS "FALSE, %",
       round(100.0 * x.nn / nullif(x.n, 0), 1) AS "NULL, %"
  FROM json_to_recordset(:'j_sh'::json)
       AS x(rule text, per text, ord int, n int, nt int, nf int, nn int)
 ORDER BY x.ord, x.rule;

\echo -- V1 по неделям периода версии 7 (ISO-неделя; дней под вето из дней периода в неделе) --
SELECT x.wk AS "Неделя", x.d_from AS "С", x.d_to AS "По", x.nt AS "Дней под вето",
       x.n AS "Дней в периоде", x.nn AS "Без флага"
  FROM json_to_recordset(:'j_wk'::json)
       AS x(wk text, d_from date, d_to date, n int, nt int, nn int)
 ORDER BY x.d_from;
