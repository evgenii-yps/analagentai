-- ЭТАП 9.10, блок 00 — данные и контроли. Только чтение.
-- Что показывает: откуда взят ряд по каждой монете (история биржи H-hist, живой сборщик H-live,
-- дневные свечи биржи 1Dutc как контроль), сколько суток собрано и сколько пропущено, и три
-- блокирующих контроля ТЗ 9.10 §4.4:
--   1) дневные свечи, собранные из часовых, совпадают с 1Dutc биржи (допуск 5e-4);
--   2) контрольный опыт: при сдвиге границы суток на 1 час то же сравнение ОБЯЗАНО разойтись;
--   3) шов источников: на часах, где есть и история, и живой сборщик, close совпадает (5e-4).
-- Контроль без единого сравнения ничего не доказывает и считается непройденным.
-- Строки «@@…» — машинный вывод для вердикта; run_9_10.sh уводит их в файл фактов, в отчёт они не идут.

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

, rng AS (
    SELECT t.token,
           (SELECT min(h.open_time) FROM h_hist h WHERE h.token = t.token) AS hist_from,
           (SELECT max(h.open_time) FROM h_hist h WHERE h.token = t.token) AS hist_to,
           (SELECT min(l.open_time) FROM h_live l WHERE l.token = t.token) AS live_from,
           (SELECT max(l.open_time) FROM h_live l WHERE l.token = t.token) AS live_to,
           (SELECT max((c.open_time AT TIME ZONE 'UTC')::date) FROM backtest.candles c
             WHERE c.bar = '1Dutc' AND c.inst_id = t.token || '-USDT') AS d1_last
      FROM tok t
),
hg AS (
    SELECT token, open_time,
           lag(open_time) OVER (PARTITION BY token ORDER BY open_time) AS prev
      FROM hourly
),
gaps AS (                           -- пропущенные часы объединённого ряда, по году начала пропуска
    SELECT token, extract(year FROM prev + interval '1 hour')::int AS yr,
           sum((extract(epoch FROM open_time - prev) / 3600)::int - 1) AS miss
      FROM hg
     WHERE open_time - prev > interval '1 hour'
     GROUP BY token, extract(year FROM prev + interval '1 hour')::int
),
cnt AS (
    SELECT t.token,
           (SELECT count(*) FROM daily d WHERE d.token = t.token) AS n_days,
           (SELECT count(*) FROM daily_all d
             WHERE d.token = t.token AND d.n_h < 24       -- текущие, ещё не закончившиеся сутки не считаются
               AND d.day < (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date) AS n_partial,
           coalesce((SELECT sum(g.miss) FROM gaps g WHERE g.token = t.token), 0) AS miss_total,
           coalesce((SELECT string_agg(g.yr || ':' || g.miss, '  ' ORDER BY g.yr)
                       FROM gaps g WHERE g.token = t.token), 'нет') AS miss_years
      FROM tok t
),
cmp AS (                            -- контроль 1: сутки из часов против 1Dutc биржи
    SELECT greatest(coalesce(abs(d.open - e.open::double precision) / nullif(abs(e.open::double precision), 0), 1.0),
                    coalesce(abs(d.close - e.close::double precision) / nullif(abs(e.close::double precision), 0), 1.0)) AS dev
      FROM daily d
      JOIN backtest.candles e
        ON e.bar = '1Dutc' AND e.inst_id = d.token || '-USDT'
       AND (e.open_time AT TIME ZONE 'UTC')::date = d.day
),
daily_s AS (                        -- контрольный опыт: сутки 01:00–00:59 (граница сдвинута на час)
    SELECT token, ((open_time - interval '1 hour') AT TIME ZONE 'UTC')::date AS day,
           (array_agg(open ORDER BY open_time))[1] AS open,
           (array_agg(close ORDER BY open_time DESC))[1] AS close
      FROM hourly
     GROUP BY token, ((open_time - interval '1 hour') AT TIME ZONE 'UTC')::date
    HAVING count(*) = 24
),
cmp_s AS (
    SELECT greatest(coalesce(abs(d.open - e.open::double precision) / nullif(abs(e.open::double precision), 0), 1.0),
                    coalesce(abs(d.close - e.close::double precision) / nullif(abs(e.close::double precision), 0), 1.0)) AS dev
      FROM daily_s d
      JOIN backtest.candles e
        ON e.bar = '1Dutc' AND e.inst_id = d.token || '-USDT'
       AND (e.open_time AT TIME ZONE 'UTC')::date = d.day
),
seam AS (                           -- контроль 3: шов источников, общие часы
    SELECT coalesce(abs(h.close - l.close) / nullif(abs(h.close), 0), 1.0) AS dev
      FROM h_hist h
      JOIN h_live l ON l.token = h.token AND l.open_time = h.open_time
),
k AS (
    SELECT (SELECT count(*) FROM cmp) AS n1,
           (SELECT count(*) FROM cmp WHERE dev > 5e-4) AS b1,
           (SELECT coalesce(max(dev), 0) FROM cmp) AS m1,
           (SELECT count(*) FROM cmp_s) AS n2,
           (SELECT count(*) FROM cmp_s WHERE dev > 5e-4) AS b2,
           (SELECT coalesce(max(dev), 0) FROM cmp_s) AS m2,
           (SELECT count(*) FROM seam) AS n3,
           (SELECT count(*) FROM seam WHERE dev > 5e-4) AS b3,
           (SELECT coalesce(max(dev), 0) FROM seam) AS m3
)
SELECT (SELECT json_agg(json_build_object(
              'token', r.token,
              'hf', to_char(r.hist_from, 'YYYY-MM-DD HH24:MI'), 'ht', to_char(r.hist_to, 'YYYY-MM-DD HH24:MI'),
              'lf', to_char(r.live_from, 'YYYY-MM-DD HH24:MI'), 'lt', to_char(r.live_to, 'YYYY-MM-DD HH24:MI'),
              'd1', to_char(r.d1_last, 'YYYY-MM-DD')) ORDER BY r.token) FROM rng r) AS j_rng,
       (SELECT json_agg(json_build_object(
              'token', c.token, 'n_days', c.n_days, 'n_partial', c.n_partial,
              'miss_total', c.miss_total, 'miss_years', c.miss_years) ORDER BY c.token) FROM cnt c) AS j_cnt,
       n1, b1, to_char(m1, 'FM0.0000000') AS m1,
       n2, b2, to_char(m2, 'FM0.0000000') AS m2,
       n3, b3, to_char(m3, 'FM0.0000000') AS m3,
       (n1 > 0 AND b1 = 0)::int AS ok1,
       (n2 > 0 AND b2 > 0)::int AS ok2,
       (n3 > 0 AND b3 = 0)::int AS ok3
  FROM k
\gset

\echo -- Источники по монетам (время начала бара, UTC) --
SELECT x.token AS "Монета", x.hf AS "H-hist с", x.ht AS "H-hist по", x.lf AS "H-live с",
       x.lt AS "H-live по", x.d1 AS "1Dutc до"
  FROM json_to_recordset(:'j_rng'::json)
       AS x(token text, hf text, ht text, lf text, lt text, d1 text);

\echo -- Сутки и пропуски часов объединённого ряда (год начала пропуска:часов) --
SELECT x.token AS "Монета", x.n_days AS "Суток в daily", x.n_partial AS "Неполных суток (пропущено)",
       x.miss_total AS "Часов нет", x.miss_years AS "По годам"
  FROM json_to_recordset(:'j_cnt'::json)
       AS x(token text, n_days int, n_partial int, miss_total int, miss_years text);

\echo -- Контроли ТЗ 9.10 §4.4 (допуск относительный, 5e-4) --
SELECT v.name AS "Контроль", v.n AS "Сравнено", v.bad AS "Расхождений", v.dev AS "Макс. откл.",
       v.expect AS "Ожидание", CASE WHEN v.ok = 1 THEN '✅' ELSE '❌' END AS "Итог"
  FROM (VALUES (1, '1. сутки из часов = 1Dutc биржи', :n1, :b1, :'m1', '0 расхождений', :ok1),
               (2, '2. то же при сдвиге суток на 1 ч', :n2, :b2, :'m2', '> 0 (обязан упасть)', :ok2),
               (3, '3. шов H-hist / H-live, общие часы', :n3, :b3, :'m3', '0 расхождений', :ok3))
       AS v(ord, name, n, bad, dev, expect, ok)
 ORDER BY v.ord;

\echo @@F|ctl1|:ok1
\echo @@F|ctl2|:ok2
\echo @@F|ctl3|:ok3
\echo @@F|ctl2_mismatches|:b2
