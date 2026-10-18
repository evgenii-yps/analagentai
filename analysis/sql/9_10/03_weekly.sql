-- ЭТАП 9.10, блок 03 — сделки версии 7 по неделям × V1. Только чтение.
-- Что показывает: по ISO-неделям открытия (UTC) число закрытых позиций (без data_gap), которым V1
-- разрешало вход / ставило вето / не успело поставить флаг, и средний net_pnl_pct разрешённых и
-- под вето. Пустая клетка (-) — в группе нет сделок.

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

-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §2: позиции версии 7 и флаги дня открытия (одинаковы во всех файлах; сверяет tests/test_stage_9_10.py) ===
, v7 AS (                           -- выборка как в 9.9: версия логики 7, виртуальные позиции
    SELECT p.id, p.instrument_id, i.symbol, split_part(i.symbol, '/', 1) AS token, p.status,
           p.opened_at, p.closed_at, p.entry_price, p.cost_pct, p.exit_reason,
           p.net_pnl_pct, p.net_pnl_usd
      FROM positions p
      JOIN instruments i ON i.id = p.instrument_id
     WHERE p.logic_version = 7 AND p.is_virtual
),
v7f AS (                            -- флаги дня открытия: V1–V3 по BTC, V4 по своей монете
    SELECT v.*, (v.opened_at AT TIME ZONE 'UTC')::date AS day,
           b.v1, b.v2, b.v3, o.v4
      FROM v7 v
      LEFT JOIN flags b ON b.token = 'BTC' AND b.day = (v.opened_at AT TIME ZONE 'UTC')::date
      LEFT JOIN flags o ON o.token = v.token AND o.day = (v.opened_at AT TIME ZONE 'UTC')::date
),
closed AS (                         -- закрытые для метрик: без data_gap
    SELECT * FROM v7f
     WHERE status = 'closed' AND exit_reason IS DISTINCT FROM 'data_gap'
)
-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ПОЗИЦИЙ ===

SELECT to_char(day, 'IYYY-"W"IW')                          AS "Неделя",
       min(day)                                            AS "С",
       count(*)                                            AS "Сделок",
       count(*) FILTER (WHERE NOT v1)                      AS "Разрешены, N",
       count(*) FILTER (WHERE v1)                          AS "Вето, N",
       count(*) FILTER (WHERE v1 IS NULL)                  AS "Без флага, N",
       round((avg(net_pnl_pct) FILTER (WHERE NOT v1))::numeric, 3) AS "Разрешены, ср. %",
       round((avg(net_pnl_pct) FILTER (WHERE v1))::numeric, 3)     AS "Вето, ср. %"
  FROM closed
 GROUP BY to_char(day, 'IYYY-"W"IW')
 ORDER BY min(day);
