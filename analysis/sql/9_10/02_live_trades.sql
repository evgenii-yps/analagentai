-- ЭТАП 9.10, блок 02 — сделки версии 7 с вето и без (главный блок по живым данным). Только чтение.
-- Что показывает: для каждого правила V1–V4 закрытые позиции (без data_gap) в группах «все»,
-- «разрешены» (флаг FALSE), «вето» (флаг TRUE), «без флага» (NULL, только N). Флаги — дня
-- открытия позиции (UTC): V1–V3 по BTC, V4 по монете позиции. n_eff — число различных дней
-- открытия. Доля прибыльных — net_pnl_pct > 0.0001. ФП — фактор прибыли в долларах
-- (сумма прибылей / |сумма убытков| по net_pnl_usd). «Ср. по дням» и «±» — среднее дневных
-- средних и 1,96·sd/√дней: единица наблюдения день, а не сделка. Разность «разрешены − все» по
-- дням — с интервалом для разности независимых средних. Открытые позиции в таблицу не входят.
-- Значимость здесь справочная: дней мало.

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

, ob AS (
    SELECT 'V1' AS rule, day, token, net_pnl_pct AS pnl, net_pnl_usd AS usd, exit_reason, v1 AS flag FROM closed
    UNION ALL SELECT 'V2', day, token, net_pnl_pct, net_pnl_usd, exit_reason, v2 FROM closed
    UNION ALL SELECT 'V3', day, token, net_pnl_pct, net_pnl_usd, exit_reason, v3 FROM closed
    UNION ALL SELECT 'V4', day, token, net_pnl_pct, net_pnl_usd, exit_reason, v4 FROM closed
),
gr AS (
    SELECT rule, 1 AS g, 'все' AS grp, day, pnl, usd, exit_reason FROM ob
    UNION ALL SELECT rule, 2, 'разрешены', day, pnl, usd, exit_reason FROM ob WHERE NOT flag
    UNION ALL SELECT rule, 3, 'вето', day, pnl, usd, exit_reason FROM ob WHERE flag
    UNION ALL SELECT rule, 4, 'без флага', day, pnl, usd, exit_reason FROM ob WHERE flag IS NULL
),
ag AS (
    SELECT rule, g, grp, count(*) AS n, count(DISTINCT day) AS n_eff,
           avg(pnl) AS m, percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl) AS med,
           100.0 * avg((pnl > 0.0001)::int) AS pos, min(pnl) AS worst,
           100.0 * avg((exit_reason = 'timeout')::int) AS to_pct,
           sum(usd) AS s_usd,
           sum(usd) FILTER (WHERE usd > 0) / nullif(-sum(usd) FILTER (WHERE usd < 0), 0) AS pf
      FROM gr
     GROUP BY rule, g, grp
),
dm AS (SELECT rule, g, day, avg(pnl) AS m FROM gr GROUP BY rule, g, day),
dsd AS (
    SELECT rule, g, count(*) AS nd, avg(m) AS md, stddev_samp(m) AS sd
      FROM dm GROUP BY rule, g
),
tbl AS (                            -- скелет «правило × группа»: пустая группа показывается с N = 0
    SELECT k.rule, k.g, k.grp, coalesce(a.n, 0) AS n,
           CASE WHEN k.g < 4 THEN a.n_eff END AS n_eff,
           CASE WHEN k.g < 4 THEN round(a.m::numeric, 3) END AS m,
           CASE WHEN k.g < 4 THEN round(a.med::numeric, 3) END AS med,
           CASE WHEN k.g < 4 THEN round(a.pos::numeric, 1) END AS pos,
           CASE WHEN k.g < 4 THEN round(a.worst::numeric, 3) END AS worst,
           CASE WHEN k.g < 4 THEN round(a.to_pct::numeric, 1) END AS to_pct,
           CASE WHEN k.g < 4 THEN round(a.s_usd::numeric, 4) END AS s_usd,
           CASE WHEN k.g < 4 THEN round(a.pf::numeric, 2) END AS pf,
           CASE WHEN k.g < 4 THEN s.nd END AS nd,
           CASE WHEN k.g < 4 THEN round(s.md::numeric, 3) END AS md,
           CASE WHEN k.g < 4 THEN round((1.96 * s.sd / sqrt(s.nd))::numeric, 3) END AS hw
      FROM (SELECT r.rule, g.g, g.grp
              FROM (VALUES ('V1'), ('V2'), ('V3'), ('V4')) r(rule)
             CROSS JOIN (VALUES (1, 'все'), (2, 'разрешены'), (3, 'вето'), (4, 'без флага')) g(g, grp)) k
      LEFT JOIN ag a ON a.rule = k.rule AND a.g = k.g
      LEFT JOIN dsd s ON s.rule = k.rule AND s.g = k.g
),
df AS (
    SELECT k.rule,
           round((ok.m - al.m)::numeric, 3) AS d_pos,
           round((so.md - sa.md)::numeric, 3) AS d_day,
           round((so.md - sa.md - 1.96 * sqrt(so.sd ^ 2 / so.nd + sa.sd ^ 2 / sa.nd))::numeric, 3) AS lo,
           round((so.md - sa.md + 1.96 * sqrt(so.sd ^ 2 / so.nd + sa.sd ^ 2 / sa.nd))::numeric, 3) AS hi
      FROM (VALUES ('V1'), ('V2'), ('V3'), ('V4')) k(rule)
      LEFT JOIN ag al ON al.rule = k.rule AND al.g = 1
      LEFT JOIN ag ok ON ok.rule = k.rule AND ok.g = 2
      LEFT JOIN dsd sa ON sa.rule = k.rule AND sa.g = 1
      LEFT JOIN dsd so ON so.rule = k.rule AND so.g = 2
),
op AS (
    SELECT 'V1' AS rule, v1 AS flag FROM v7f WHERE status = 'open'
    UNION ALL SELECT 'V2', v2 FROM v7f WHERE status = 'open'
    UNION ALL SELECT 'V3', v3 FROM v7f WHERE status = 'open'
    UNION ALL SELECT 'V4', v4 FROM v7f WHERE status = 'open'
),
opn AS (
    SELECT k.rule, count(o.rule) FILTER (WHERE o.flag) AS nt, count(o.rule) FILTER (WHERE NOT o.flag) AS nf,
           count(o.rule) FILTER (WHERE o.flag IS NULL) AS nn
      FROM (VALUES ('V1'), ('V2'), ('V3'), ('V4')) k(rule)
      LEFT JOIN op o ON o.rule = k.rule
     GROUP BY k.rule
)
SELECT (SELECT coalesce(json_agg(row_to_json(t) ORDER BY t.rule, t.g), '[]'::json) FROM tbl t) AS j_tbl,
       (SELECT coalesce(json_agg(row_to_json(d) ORDER BY d.rule), '[]'::json) FROM df d) AS j_df,
       (SELECT coalesce(json_agg(row_to_json(o) ORDER BY o.rule), '[]'::json) FROM opn o) AS j_op,
       (SELECT coalesce(json_agg(json_build_object(
              'rule', a.rule, 'g', a.g, 'n', a.n, 'm', a.m) ORDER BY a.rule, a.g), '[]'::json)
          FROM ag a WHERE a.g IN (2, 3)) AS j_fact,
       (SELECT coalesce(json_agg(json_build_object(
              'day', c.day, 'token', c.token, 'v1', c.v1::int, 'v2', c.v2::int, 'v3', c.v3::int,
              'v4', c.v4::int, 'pnl', c.net_pnl_pct) ORDER BY c.day, c.id), '[]'::json) FROM closed c) AS j_csv
\gset

\echo -- Закрытые позиции версии 7 по правилам (п.п. и % — это net_pnl_pct) --
SELECT x.rule AS "Прав.", x.grp AS "Группа", x.n AS "N", x.n_eff AS "n_eff", x.m AS "Ср., %",
       x.med AS "Мед., %", x.pos AS ">0, %", x.worst AS "Худш., %", x.to_pct AS "timeout, %",
       x.s_usd AS "Σ USD", x.pf AS "ФП", x.nd AS "Дней", x.md AS "Ср. по дням", x.hw AS "±95%"
  FROM json_to_recordset(:'j_tbl'::json)
       AS x(rule text, g int, grp text, n int, n_eff int, m numeric, med numeric, pos numeric,
            worst numeric, to_pct numeric, s_usd numeric, pf numeric, nd int, md numeric, hw numeric)
 ORDER BY x.rule, x.g;

\echo -- Разность «разрешены − все»: по сделкам и по дням (95 % — разность независимых средних) --
SELECT x.rule AS "Прав.", x.d_pos AS "По сделкам, п.п.", x.d_day AS "По дням, п.п.",
       x.lo AS "Нижняя 95%", x.hi AS "Верхняя 95%"
  FROM json_to_recordset(:'j_df'::json) AS x(rule text, d_pos numeric, d_day numeric, lo numeric, hi numeric)
 ORDER BY x.rule;

\echo -- Открытые позиции версии 7 (в таблицу не входят): флаг дня открытия --
SELECT x.rule AS "Прав.", x.nt AS "Вето", x.nf AS "Разрешены", x.nn AS "Без флага"
  FROM json_to_recordset(:'j_op'::json) AS x(rule text, nt int, nf int, nn int)
 ORDER BY x.rule;

\pset format unaligned
\pset tuples_only on
SELECT '@@F|' || x.rule || '.live_' || CASE x.g WHEN 2 THEN 'ok' ELSE 'veto' END || '|' || coalesce(x.m::text, '')
  FROM json_to_recordset(:'j_fact'::json) AS x(rule text, g int, n int, m numeric);
SELECT '@@F|' || x.rule || '.live_n_' || CASE x.g WHEN 2 THEN 'ok' ELSE 'veto' END || '|' || x.n
  FROM json_to_recordset(:'j_fact'::json) AS x(rule text, g int, n int, m numeric);
SELECT '@@R02|' || x.day || '|' || x.token || '|' || coalesce(x.v1::text, '') || '|' || coalesce(x.v2::text, '')
       || '|' || coalesce(x.v3::text, '') || '|' || coalesce(x.v4::text, '') || '|' || x.pnl
  FROM json_to_recordset(:'j_csv'::json) AS x(day date, token text, v1 int, v2 int, v3 int, v4 int, pnl numeric);
\pset tuples_only off
\pset format aligned
