-- ЭТАП 9.11, блок 01 — выгрузка часового ряда в CSV (ТЗ 9.11 §3.1). Только чтение.
-- В отчёт не идёт: run_9_11.sh направляет стандартный вывод в $OUT_DIR/series_9_11_ГГГГММДД.csv.
-- Колонки строго: token,open_time_utc,open,high,low,close; сортировка token, open_time.
-- COPY … TO STDOUT, а не \copy: \copy не принимает многострочный запрос, а общие определения рядов
-- (одинаковые с 9.10 + high/low) — многострочные.

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


COPY (
-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.11 §3.1 (= 9.10 §4.1–4.2 + high/low): часовой и дневной ряды (одинаковы во всех файлах; сверяет tests/test_stage_9_11.py) ===
WITH tok(token) AS (VALUES ('BTC'), ('ETH'), ('SOL'), ('XRP'), ('DOGE')),
h_hist AS (                         -- история биржи: backtest.candles, закрытые бары
    SELECT split_part(c.inst_id, '-', 1) AS token, c.open_time,
           c.open::double precision AS open, c.high::double precision AS high,
           c.low::double precision AS low, c.close::double precision AS close
      FROM backtest.candles c
     WHERE c.bar = '1H'
       AND c.inst_id IN ('BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT', 'DOGE-USDT')
       AND c.open_time + interval '1 hour' <= :'as_of'::timestamptz
),
h_live AS (                         -- живой сборщик: public.ohlcv, закрытые бары
    SELECT split_part(i.symbol, '/', 1) AS token, o.ts AS open_time, o.open, o.high, o.low, o.close
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
    SELECT token, open_time, open, high, low, close FROM h_hist
    UNION ALL
    SELECT l.token, l.open_time, l.open, l.high, l.low, l.close
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
SELECT token, to_char(open_time AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS open_time_utc,
       open, high, low, close
  FROM hourly
 ORDER BY token, open_time
) TO STDOUT WITH CSV HEADER;
