-- ЭТАП 9.11, блок 00 — данные, цели по монетам, охват. Только чтение.
-- Что показывает: откуда взят часовой ряд (история биржи H-hist и живой сборщик H-live, как в 9.10, но
-- с high/low), сколько часов и пропусков по каждой из пяти монет; цель T_coin — медиана target_pct по
-- позициям версии 7 (закрытым и открытым, без data_gap) каждой монеты; охват позиций версии 7.
-- Строки «@@…» — машинный вывод для расчёта (mr_9_11.py); run_9_11.sh уводит их в файл фактов:
--   @@T|монета|T_coin      @@P|id|монета|opened_at_utc      @@V|день|0/1/пусто (флаг V1 из 9.10)
--   @@S|id|SOL|opened_at_utc|entry_price|exit_reason|status   (позиции SOL в окне контрольного случая)

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

-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.3: флаги тренда (одинаковы во всех файлах; сверяет tests/test_stage_9_11.py) ===
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

, v7 AS (                           -- версия 7, виртуальные; закрытые для метрик и открытые (как в 9.9/9.10)
    SELECT p.id, split_part(i.symbol, '/', 1) AS token, p.status, p.opened_at, p.target_pct,
           p.entry_price, p.exit_reason
      FROM positions p
      JOIN instruments i ON i.id = p.instrument_id
     WHERE p.logic_version = 7 AND p.is_virtual
       AND p.status IN ('open', 'closed') AND p.exit_reason IS DISTINCT FROM 'data_gap'
),
tc AS (
    SELECT t.token, count(v.id) AS n,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY v.target_pct) AS t_coin
      FROM tok t
      LEFT JOIN v7 v ON v.token = t.token
     GROUP BY t.token
),
hs AS (
    SELECT t.token,
           (SELECT count(*) FROM hourly h WHERE h.token = t.token) AS n_h,
           (SELECT min(h.open_time) FROM hourly h WHERE h.token = t.token) AS t_from,
           (SELECT max(h.open_time) FROM hourly h WHERE h.token = t.token) AS t_to,
           (SELECT count(*) FROM h_hist h WHERE h.token = t.token) AS n_hist,
           (SELECT count(*) FROM h_live l WHERE l.token = t.token) AS n_live
      FROM tok t
)
SELECT (SELECT json_agg(json_build_object(
              'token', s.token, 'n_h', s.n_h, 'n_hist', s.n_hist, 'n_live', s.n_live,
              'from', to_char(s.t_from AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI'),
              'to', to_char(s.t_to AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI'),
              'miss', CASE WHEN s.n_h = 0 THEN NULL
                           ELSE (extract(epoch FROM s.t_to - s.t_from) / 3600)::int + 1 - s.n_h END)
              ORDER BY s.token) FROM hs s) AS j_hs,
       (SELECT json_agg(json_build_object(
              'token', c.token, 'n', c.n, 't', round(c.t_coin::numeric, 4)) ORDER BY c.token) FROM tc c) AS j_tc,
       (SELECT coalesce(json_agg(json_build_object('token', c.token, 't', c.t_coin) ORDER BY c.token), '[]'::json)
          FROM tc c WHERE c.n > 0) AS j_t,
       (SELECT coalesce(json_agg(json_build_object(
              'id', v.id, 'token', v.token,
              'at', to_char(v.opened_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')) ORDER BY v.opened_at, v.id), '[]'::json)
          FROM v7 v) AS j_p,
       (SELECT coalesce(json_agg(json_build_object('day', f.day, 'v1', f.v1::int) ORDER BY f.day), '[]'::json)
          FROM flags f WHERE f.token = 'BTC') AS j_v,
       (SELECT coalesce(json_agg(json_build_object(
              'id', v.id, 'token', v.token,
              'at', to_char(v.opened_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
              'px', v.entry_price, 'reason', coalesce(v.exit_reason, ''), 'status', v.status) ORDER BY v.opened_at), '[]'::json)
          FROM v7 v
         WHERE v.token = 'SOL'
           AND v.opened_at BETWEEN timestamptz '2026-10-18 12:00:00+00' AND timestamptz '2026-10-19 06:00:00+00') AS j_s,
       (SELECT count(*) FROM v7) AS n_all,
       (SELECT count(*) FROM v7 WHERE status = 'closed') AS n_closed,
       (SELECT count(*) FROM v7 WHERE status = 'open') AS n_open,
       coalesce((SELECT to_char(min(opened_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM v7), '-') AS t_first,
       (SELECT count(*) FROM flags WHERE token = 'BTC' AND v1) AS v1_true,
       (SELECT count(*) FROM flags WHERE token = 'BTC' AND NOT v1) AS v1_false
\gset

\echo -- Часовой ряд по монетам (история биржи + живой сборщик; часов в объединённом ряду, пропуски не заполняются) --
SELECT x.token AS "Монета", x."from" AS "С (UTC)", x."to" AS "По (UTC)", x.n_h AS "Часов", x.n_hist AS "из истории",
       x.n_live AS "из сборщика", x.miss AS "Пропусков"
  FROM json_to_recordset(:'j_hs'::json)
       AS x(token text, "from" text, "to" text, n_h int, n_hist int, n_live int, miss int)
 ORDER BY x.token;

\echo -- Цель T_coin = медиана target_pct позиций версии 7 по монете (нет позиций — монета исключается) --
SELECT x.token AS "Монета", x.n AS "Позиций", x.t AS "T_coin, %",
       CASE WHEN x.n = 0 THEN 'исключена' ELSE 'в расчёте' END AS "Статус"
  FROM json_to_recordset(:'j_tc'::json) AS x(token text, n int, t numeric)
 ORDER BY x.token;

\echo -- Охват позиций версии 7 (закрытые без data_gap + открытые) --
SELECT :n_all AS "Позиций", :n_closed AS "Закрыто", :n_open AS "Открыто", :'t_first' AS "Первая (UTC)",
       :v1_true AS "Дней V1=TRUE", :v1_false AS "Дней V1=FALSE";

\pset format unaligned
\pset tuples_only on
SELECT '@@T|' || x.token || '|' || x.t FROM json_to_recordset(:'j_t'::json) AS x(token text, t float8);
SELECT '@@P|' || x.id || '|' || x.token || '|' || x.at FROM json_to_recordset(:'j_p'::json) AS x(id int, token text, at text);
SELECT '@@V|' || x.day || '|' || coalesce(x.v1::text, '') FROM json_to_recordset(:'j_v'::json) AS x(day date, v1 int);
SELECT '@@S|' || x.id || '|' || x.token || '|' || x.at || '|' || x.px || '|' || x.reason || '|' || x.status
  FROM json_to_recordset(:'j_s'::json) AS x(id int, token text, at text, px numeric, reason text, status text);
\pset tuples_only off
\pset format aligned
