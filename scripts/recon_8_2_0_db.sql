-- Замерщик БД для Этапа 8.2.0 (разведка перед расчётом целей по вероятности).
--
-- ТОЛЬКО ЧТЕНИЕ. Ни одного INSERT/UPDATE/DELETE/DDL. Скрипт не меняет ни данные,
-- ни схему, ни конфигурацию.
--
-- Запуск на сервере (пользователь agent, флаг -T обязателен — ввод идёт из файла):
--
--   sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade -X -A -F "|" -f -' < scripts/recon_8_2_0_db.sql
--
-- Имена таблиц и колонок взяты из db/init.sql и db/migrations/*.sql, а не по памяти.

\echo '### 0. Момент замера и границы версий логики'
SELECT now() AT TIME ZONE 'UTC' AS measured_at_utc;
SELECT logic_version, started_at, note FROM logic_version_windows ORDER BY logic_version;

\echo '### 1. Состав инструментов (спот и контракт задаются ЯВНО, достраивание запрещено)'
SELECT id, exchange, symbol, base, quote, type, active, created_at
FROM instruments ORDER BY base, type, id;

\echo '### 2. ВОПРОС 1. Глубина истории цен: таблица ohlcv, разрез инструмент x таймфрейм'
-- Пропуски = сколько интервалов таймфрейма отсутствует между самой ранней и
-- самой поздней меткой (ожидаемое число свечей минус фактическое).
SELECT i.base                                   AS token,
       i.symbol                                 AS instrument,
       i.type                                   AS market,
       o.timeframe                              AS tf,
       min(o.ts)                                AS ts_min_utc,
       max(o.ts)                                AS ts_max_utc,
       count(*)                                 AS candles,
       (floor(extract(epoch FROM max(o.ts) - min(o.ts)) / s.sec)::bigint + 1)
                                                AS expected,
       (floor(extract(epoch FROM max(o.ts) - min(o.ts)) / s.sec)::bigint + 1 - count(*))
                                                AS gaps,
       round((extract(epoch FROM max(o.ts) - min(o.ts)) / 86400.0)::numeric, 3)
                                                AS span_days
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
JOIN LATERAL (SELECT CASE o.timeframe
                       WHEN '1m'  THEN 60    WHEN '3m'  THEN 180
                       WHEN '5m'  THEN 300   WHEN '15m' THEN 900
                       WHEN '30m' THEN 1800  WHEN '1h'  THEN 3600
                       WHEN '2h'  THEN 7200  WHEN '4h'  THEN 14400
                       WHEN '6h'  THEN 21600 WHEN '12h' THEN 43200
                       WHEN '1d'  THEN 86400 END AS sec) s ON TRUE
GROUP BY i.base, i.symbol, i.type, o.timeframe, s.sec
ORDER BY i.base, i.type, s.sec;

\echo '### 2b. Полных суток истории на инструмент (по самому длинному ряду инструмента)'
SELECT i.base AS token, i.symbol AS instrument, i.type AS market,
       min(o.ts) AS ts_min_utc, max(o.ts) AS ts_max_utc,
       floor(extract(epoch FROM max(o.ts) - min(o.ts)) / 86400.0)::int AS full_days
FROM ohlcv o JOIN instruments i ON i.id = o.instrument_id
GROUP BY i.base, i.symbol, i.type ORDER BY i.base, i.type;

\echo '### 2c. Инструменты БЕЗ единой свечи в ohlcv (проверка гипотезы «у контрактов свечей нет»)'
SELECT i.id, i.base, i.symbol, i.type
FROM instruments i
WHERE NOT EXISTS (SELECT 1 FROM ohlcv o WHERE o.instrument_id = i.id)
ORDER BY i.base, i.type;

\echo '### 2d. Второе хранилище свечей: backtest.candles (схема реплея, Этап 7.4)'
-- В базе ДВА хранилища свечей: public.ohlcv (продакшн, собирается коллектором)
-- и backtest.candles (историческая загрузка для реплея). Смешивать их нельзя:
-- ключ backtest.candles — ТЕКСТОВЫЙ inst_id биржи, а не instrument_id.
SELECT inst_id, bar, min(open_time) AS ts_min_utc, max(open_time) AS ts_max_utc,
       count(*) AS candles,
       floor(extract(epoch FROM max(open_time) - min(open_time)) / 86400.0)::int AS full_days
FROM backtest.candles GROUP BY inst_id, bar ORDER BY inst_id, bar;

\echo '### 3. ВОПРОС 2а. Независимые 4-часовые окна после 2026-08-22 22:59 UTC, по токенам'
-- Вариант A: все сигналы, включая wait (буквальное определение «одно наблюдение
-- на непересекающееся 4-часовое окно»).
\echo '--- 3A: все сигналы'
SELECT i.base AS token, count(*) AS independent_windows
FROM (SELECT DISTINCT ON (s.instrument_id,
                          to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400))
             s.id, s.instrument_id
      FROM signals s
      WHERE s.ts >= TIMESTAMPTZ '2026-08-22 22:59:00+00'
      ORDER BY s.instrument_id,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400),
               s.ts ASC) w
JOIN instruments i ON i.id = w.instrument_id
GROUP BY i.base ORDER BY i.base;

\echo '--- 3B: только направленные сигналы (decision <> wait) — это условие выгрузки'
SELECT i.base AS token, count(*) AS independent_windows
FROM (SELECT DISTINCT ON (s.instrument_id,
                          to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400))
             s.id, s.instrument_id
      FROM signals s
      WHERE s.ts >= TIMESTAMPTZ '2026-08-22 22:59:00+00' AND s.decision <> 'wait'
      ORDER BY s.instrument_id,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400),
               s.ts ASC) w
JOIN instruments i ON i.id = w.instrument_id
GROUP BY i.base ORDER BY i.base;

\echo '--- 3C: направленные И с посчитанной оценкой на 4 ч — что реально видит лист'
SELECT i.base AS token, count(*) AS independent_windows
FROM (SELECT DISTINCT ON (s.instrument_id,
                          to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400))
             s.id, s.instrument_id
      FROM signals s
      JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon_h = 4
      WHERE s.ts >= TIMESTAMPTZ '2026-08-22 22:59:00+00' AND s.decision <> 'wait'
      ORDER BY s.instrument_id,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400),
               s.ts ASC) w
JOIN instruments i ON i.id = w.instrument_id
GROUP BY i.base ORDER BY i.base;

\echo '--- 3D: точный набор строк листа «Независимые окна» (запрос выгрузки дословно)'
-- Совпадает с src/export/queries.py:158-176 (fetch_independent_by_token_horizon),
-- горизонты берутся из EVAL_HORIZONS.
SELECT horizon_h, token, count(*) AS rows_in_sheet,
       min(ts) AS ts_min_utc, max(ts) AS ts_max_utc
FROM (SELECT DISTINCT ON (e.horizon_h, i.id,
                          to_timestamp(floor(extract(epoch FROM s.ts) / (e.horizon_h * 3600))
                                       * (e.horizon_h * 3600)))
             e.horizon_h, i.base AS token, s.ts
      FROM signals s
      JOIN instruments i ON i.id = s.instrument_id
      JOIN signal_evaluations e ON e.signal_id = s.id
      WHERE s.decision <> 'wait' AND e.horizon_h = ANY(ARRAY[1,4,12,24])
      ORDER BY e.horizon_h, i.id,
               to_timestamp(floor(extract(epoch FROM s.ts) / (e.horizon_h * 3600))
                            * (e.horizon_h * 3600)),
               s.ts ASC) q
GROUP BY horizon_h, token ORDER BY horizon_h, token;

\echo '### 3e. Почему строк мало: сигналы и оценки по токенам после границы версии 5'
SELECT i.base AS token, s.logic_version, s.decision, count(*) AS n,
       min(s.ts) AS ts_min_utc, max(s.ts) AS ts_max_utc
FROM signals s JOIN instruments i ON i.id = s.instrument_id
WHERE s.ts >= TIMESTAMPTZ '2026-08-22 22:59:00+00'
GROUP BY i.base, s.logic_version, s.decision ORDER BY i.base, s.logic_version, s.decision;

SELECT i.base AS token, e.horizon_h, count(*) AS evaluations,
       min(s.ts) AS ts_min_utc, max(s.ts) AS ts_max_utc
FROM signal_evaluations e
JOIN signals s ON s.id = e.signal_id
JOIN instruments i ON i.id = s.instrument_id
WHERE s.ts >= TIMESTAMPTZ '2026-08-22 22:59:00+00'
GROUP BY i.base, e.horizon_h ORDER BY i.base, e.horizon_h;

\echo '### 4. ВОПРОС 4. Есть ли таблицы целевых уровней и заморозки целей'
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_name ~* '(target|level|barrier|touch|freeze|frozen)'
ORDER BY table_schema, table_name;

\echo '### 4b. Полный перечень таблиц базы (чтобы отсутствие было видно, а не предполагалось)'
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name;
