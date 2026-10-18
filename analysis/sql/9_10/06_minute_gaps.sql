-- ЭТАП 9.10, блок 06 — полнота минутных свечей (вопрос из 9.9: у 112 из 959 позиций не нашлось
-- минутной свечи через 24 ч после входа). Только чтение.
-- Что показывает:
--   1) позиции версии 7 без минутной свечи в ±5 мин от opened_at + 24 ч (определение блока 07
--      пакета 9.9), разбитые на «удалены хранением» (opened_at + 24 ч старше as_of минус
--      RETENTION_1M_DAYS суток — свеча уже вычищена retention.py) и «настоящий пропуск»;
--   2) за последние RETENTION_1M_DAYS − 1 полных суток UTC — по каждой из пяти монет сутки, где
--      минутных свечей меньше 1440;
--   3) пропуски часовых свечей ohlcv 1h с 2026-08-08 (часовые не удаляются никогда): от первого
--      часа монеты в этом периоде до последнего закрытого часа (с запасом 5 минут на запись).

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
-- Срок хранения минутных свечей (RETENTION_1M_DAYS из .env; run_9_10.sh передаёт ключом
-- -v retention_days=…, при ручном запуске файла — 30, как в коде).
\if :{?retention_days}
\else
  \set retention_days 30
\endif

WITH v7 AS (
    SELECT p.id, p.instrument_id, i.symbol, p.opened_at
      FROM positions p
      JOIN instruments i ON i.id = p.instrument_id
     WHERE p.logic_version = 7 AND p.is_virtual
),
elig AS (                           -- позиции, у которых прошло >= 24 часов
    SELECT v7.id, v7.instrument_id, v7.symbol, v7.opened_at,
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
miss AS (
    SELECT ent.*,
           ent.t24 < :'as_of'::timestamptz - make_interval(days => :retention_days) AS purged
      FROM ent
     WHERE c24 IS NULL
),
tok(token) AS (VALUES ('BTC'), ('ETH'), ('SOL'), ('XRP'), ('DOGE')),
days AS (                           -- последние RETENTION_1M_DAYS − 1 полных суток UTC
    SELECT g::date AS day
      FROM generate_series(((:'as_of'::timestamptz AT TIME ZONE 'UTC')::date - (:retention_days - 1)),
                           ((:'as_of'::timestamptz AT TIME ZONE 'UTC')::date - 1),
                           interval '1 day') g
),
mc AS (                             -- минутных свечей за сутки
    SELECT split_part(i.symbol, '/', 1) AS token, (o.ts AT TIME ZONE 'UTC')::date AS day, count(*) AS n
      FROM ohlcv o
      JOIN instruments i ON i.id = o.instrument_id
     WHERE o.timeframe = '1m' AND i.type = 'spot'
       AND i.symbol IN ('BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT')
       AND (o.ts AT TIME ZONE 'UTC')::date IN (SELECT day FROM days)
     GROUP BY 1, 2
),
grid AS (
    SELECT t.token, d.day, 1440 - coalesce(mc.n, 0) AS absent
      FROM tok t CROSS JOIN days d
      LEFT JOIN mc ON mc.token = t.token AND mc.day = d.day
),
hc AS (                             -- часовые свечи с 2026-08-08
    SELECT split_part(i.symbol, '/', 1) AS token, count(*) AS n, min(o.ts) AS t_from, max(o.ts) AS t_to
      FROM ohlcv o
      JOIN instruments i ON i.id = o.instrument_id
     WHERE o.timeframe = '1h' AND i.type = 'spot'
       AND i.symbol IN ('BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT')
       AND o.ts >= timestamptz '2026-08-08 00:00:00+00'
       AND o.ts <= date_trunc('hour', :'as_of'::timestamptz - interval '5 minutes') - interval '1 hour'
     GROUP BY 1
)
SELECT (SELECT count(*) FROM elig)                                       AS n_elig,
       (SELECT count(*) FROM ent WHERE c24 IS NOT NULL)                  AS n_with,
       (SELECT count(*) FROM miss)                                       AS n_miss,
       (SELECT count(*) FROM miss WHERE purged)                          AS n_purged,
       (SELECT count(*) FROM miss WHERE NOT purged)                      AS n_real,
       ((SELECT count(*) FROM miss WHERE NOT purged) > 5)                AS defect,
       (SELECT coalesce(json_agg(json_build_object(
              'id', m.id, 'symbol', m.symbol, 't24', to_char(m.t24, 'YYYY-MM-DD HH24:MI')) ORDER BY m.t24), '[]'::json)
          FROM (SELECT * FROM miss WHERE NOT purged ORDER BY t24 LIMIT 10) m) AS j_real,
       (SELECT coalesce(json_agg(json_build_object(
              'token', t.token,
              'bad', (SELECT count(*) FROM grid g WHERE g.token = t.token AND g.absent > 0),
              'total', (SELECT count(*) FROM days),
              'absent', (SELECT coalesce(sum(g.absent) FILTER (WHERE g.absent > 0), 0) FROM grid g WHERE g.token = t.token))
              ORDER BY t.token), '[]'::json) FROM tok t) AS j_sum,
       (SELECT coalesce(json_agg(json_build_object(
              'token', g.token, 'day', g.day, 'absent', g.absent) ORDER BY g.absent DESC, g.token, g.day), '[]'::json)
          FROM (SELECT * FROM grid WHERE absent > 0 ORDER BY absent DESC, token, day LIMIT 30) g) AS j_days,
       (SELECT (SELECT count(*) FROM grid WHERE absent > 0) || '/' || (SELECT count(*) FROM grid)) AS bad_total,
       (SELECT coalesce(json_agg(json_build_object(
              'token', t.token,
              'n', coalesce(h.n, 0),
              't_from', to_char(h.t_from, 'YYYY-MM-DD HH24:MI'), 't_to', to_char(h.t_to, 'YYYY-MM-DD HH24:MI'),
              'absent', CASE WHEN h.n IS NULL THEN NULL ELSE
                  (extract(epoch FROM date_trunc('hour', :'as_of'::timestamptz - interval '5 minutes')
                                      - interval '1 hour' - h.t_from) / 3600)::int + 1 - h.n END)
              ORDER BY t.token), '[]'::json)
          FROM tok t LEFT JOIN hc h ON h.token = t.token) AS j_hour
\gset

\echo -- 1. Позиции версии 7 без минутной свечи через 24 ч после входа (±5 мин) --
SELECT :n_elig AS "С прошедшими 24 ч", :n_with AS "Со свечой", :n_miss AS "Без свечи",
       :n_purged AS "Удалены хранением", :n_real AS "Настоящий пропуск",
       :retention_days AS "RETENTION_1M_DAYS";
\if :defect
\echo -- ДЕФЕКТ СБОРА: настоящих пропусков больше 5 — минутные свечи терялись внутри срока хранения --
\endif
\echo -- Настоящие пропуски (самые ранние, не больше 10) --
SELECT x.id AS "Позиция", x.symbol AS "Монета", x.t24 AS "Вход + 24 ч (UTC)"
  FROM json_to_recordset(:'j_real'::json) AS x(id int, symbol text, t24 text);

\echo -- 2. Минутные свечи за последние (RETENTION_1M_DAYS − 1) полных суток: суток с пропусками --
SELECT x.token AS "Монета", x.bad AS "Суток с пропусками", x.total AS "Суток всего",
       x.absent AS "Свечей нет"
  FROM json_to_recordset(:'j_sum'::json) AS x(token text, bad int, total int, absent int)
 ORDER BY x.token;
\echo -- Итог: суток-монет с пропусками / всего = :bad_total --
\echo -- Сутки с наибольшим числом пропусков (не больше 30 строк) --
SELECT x.token AS "Монета", x.day AS "Сутки (UTC)", x.absent AS "Нет свечей"
  FROM json_to_recordset(:'j_days'::json) AS x(token text, day date, absent int);

\echo -- 3. Часовые свечи ohlcv 1h с 2026-08-08: отсутствующие часы --
SELECT x.token AS "Монета", x.t_from AS "Первый час", x.t_to AS "Последний час",
       x.n AS "Есть", x.absent AS "Нет часов"
  FROM json_to_recordset(:'j_hour'::json) AS x(token text, n int, t_from text, t_to text, absent int)
 ORDER BY x.token;

\pset format unaligned
\pset tuples_only on
SELECT '@@F|c06.real_gaps|' || :n_real;
SELECT '@@F|c06.purged|' || :n_purged;
\pset tuples_only off
\pset format aligned
