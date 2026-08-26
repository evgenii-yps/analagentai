-- ЭТАП 8.8 §2. БЛОКИРУЮЩАЯ ПРЕДПРОВЕРКА ДАННЫХ. ТОЛЬКО ЧТЕНИЕ.
--
-- Скрипт не создаёт, не изменяет и не удаляет ничего: ни одной команды кроме
-- SELECT и CREATE TEMP VIEW в нём нет. Его можно запускать на продакшне в
-- любой момент, не останавливая сервисы.
--
-- Запуск на сервере (СРЕДА: сервер):
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < scripts/precheck_8_8.sql
--
-- Переопределение версии логики:
--   ... psql -v logic_version=5 -f /dev/stdin < scripts/precheck_8_8.sql
--
-- ВЕРДИКТА СКРИПТ НЕ ВЫНОСИТ. Он печатает числа; решение «минутных свечей
-- достаточно / недостаточно» принимает исполнитель по §2 ТЗ. Строка «всё в
-- порядке» здесь была бы утверждением о данных, которых скрипт не видел в
-- момент написания.
--
-- ЧТО РЕШАЕТСЯ ЭТИМИ ЧИСЛАМИ. От раздела 1 зависит §4 ТЗ: разрешение
-- одновременного касания. Есть минутные свечи на всём интервале сигнала —
-- порядок касаний определяется однозначно; нет — исход ambiguous, и доля
-- таких случаев ограничена 15%.

\set ON_ERROR_STOP on
\if :{?logic_version} \else \set logic_version 5 \endif

\pset null '·'

\echo ''
\echo '== 1. ТАЙМФРЕЙМЫ В ohlcv: глубина по каждому инструменту =================='
\echo '   depth_days — от первой до последней свечи; age_h — возраст последней.'

SELECT i.symbol,
       o.timeframe,
       count(*)                                              AS bars,
       min(o.ts)                                             AS first_ts,
       max(o.ts)                                             AS last_ts,
       round(EXTRACT(epoch FROM max(o.ts) - min(o.ts)) / 86400.0, 2) AS depth_days,
       round(EXTRACT(epoch FROM now() - max(o.ts)) / 3600.0, 2)      AS age_h
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
GROUP BY i.symbol, o.timeframe
ORDER BY i.symbol, o.timeframe;

\echo ''
\echo '== 1b. ПРОПУСКИ В МИНУТНОМ РЯДЕ =========================================='
\echo '   gaps — число разрывов (шаг между соседними свечами не равен 60 c);'
\echo '   missing_bars — сколько минут суммарно отсутствует внутри разрывов;'
\echo '   longest_gap_min — самый длинный разрыв, минут.'

WITH m AS (
    SELECT o.instrument_id, o.ts,
           o.ts - lag(o.ts) OVER (PARTITION BY o.instrument_id ORDER BY o.ts) AS step
    FROM ohlcv o
    WHERE o.timeframe = '1m'
)
SELECT i.symbol,
       count(*) FILTER (WHERE step > interval '60 seconds')                   AS gaps,
       COALESCE(sum(EXTRACT(epoch FROM step - interval '60 seconds') / 60.0)
                FILTER (WHERE step > interval '60 seconds'), 0)::bigint       AS missing_bars,
       COALESCE(round(EXTRACT(epoch FROM max(step)) / 60.0, 1), 0)            AS longest_gap_min
FROM m
JOIN instruments i ON i.id = m.instrument_id
GROUP BY i.symbol
ORDER BY i.symbol;

\echo ''
\echo '== 1c. ПОКРЫТИЕ МИНУТНЫМ РЯДОМ ОКОН СИГНАЛОВ ============================='
\echo '   Главный вопрос §4: на скольких (сигнал, горизонт) минутный ряд покрывает'
\echo '   ВЕСЬ интервал t+1мин .. t+h. covered — покрыт целиком (счёт по 1m);'
\echo '   partial — ряд есть, но с дырами; none — свечей на интервале нет вовсе.'

WITH pairs AS (
    SELECT s.id, s.instrument_id, s.ts, h.horizon_h
    FROM signals s
    CROSS JOIN (SELECT unnest(ARRAY[1, 4, 12, 24]) AS horizon_h) h
    WHERE s.decision <> 'wait'
      AND s.logic_version = :logic_version
      AND s.ts + make_interval(hours => h.horizon_h) <= now()
), cov AS (
    SELECT p.id, p.horizon_h,
           p.horizon_h * 60 AS expected_bars,
           (SELECT count(*) FROM ohlcv o
             WHERE o.instrument_id = p.instrument_id AND o.timeframe = '1m'
               AND o.ts > p.ts AND o.ts <= p.ts + make_interval(hours => p.horizon_h)
           ) AS actual_bars
    FROM pairs p
)
SELECT horizon_h,
       count(*)                                                        AS pairs,
       count(*) FILTER (WHERE actual_bars >= expected_bars)             AS covered,
       count(*) FILTER (WHERE actual_bars > 0
                          AND actual_bars < expected_bars)              AS partial,
       count(*) FILTER (WHERE actual_bars = 0)                          AS none
FROM cov
GROUP BY horizon_h
ORDER BY horizon_h;

\echo ''
\echo '== 2. СИГНАЛЫ ВЕРСИИ 5 С ЗАМОРОЖЕННЫМИ ЦЕЛЯМИ ============================'
\echo '   with_target — цель заморожена И не пуста (target_pct IS NOT NULL);'
\echo '   frozen_null — строка signal_targets есть, но цели в ней нет (причина);'
\echo '   no_row      — строки signal_targets нет вовсе (§2 п.3).'

WITH directed AS (
    SELECT s.id, s.ts
    FROM signals s
    WHERE s.decision <> 'wait' AND s.logic_version = :logic_version
), cls AS (
    SELECT d.id, d.ts,
           CASE
               WHEN NOT EXISTS (SELECT 1 FROM signal_targets t WHERE t.signal_id = d.id)
                   THEN 'no_row'
               WHEN EXISTS (SELECT 1 FROM signal_targets t
                             WHERE t.signal_id = d.id AND t.target_pct IS NOT NULL)
                   THEN 'with_target'
               ELSE 'frozen_null'
           END AS state
    FROM directed d
)
SELECT state,
       count(*)   AS signals,
       min(ts)    AS first_ts,
       max(ts)    AS last_ts
FROM cls
GROUP BY state
ORDER BY state;

\echo ''
\echo '== 3. ИНТЕРВАЛ СИГНАЛОВ БЕЗ ЗАМОРОЖЕННЫХ ЦЕЛЕЙ ==========================='
\echo '   Ожидание ТЗ §2 п.3: 22.08 22:59 — 24.08 20:42 UTC. Сверяется фактом.'

SELECT count(*) AS signals_without_targets,
       min(s.ts) AS gap_from,
       max(s.ts) AS gap_to
FROM signals s
WHERE s.decision <> 'wait'
  AND s.logic_version = :logic_version
  AND NOT EXISTS (SELECT 1 FROM signal_targets t WHERE t.signal_id = s.id);

\echo ''
\echo '== 4. ХРАНЕНИЕ ЦЕЛИ: ключ и единицы ======================================'
\echo '   Ключ таблицы и типы колонок читаются из каталога, а не пересказываются.'

SELECT a.attname   AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null
FROM pg_attribute a
WHERE a.attrelid = 'signal_targets'::regclass AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
WHERE c.conrelid = 'signal_targets'::regclass AND c.contype IN ('p', 'c')
ORDER BY c.contype DESC, c.conname;

\echo ''
\echo '   Различается ли цель по горизонту и направлению — ФАКТОМ по данным:'
\echo '   distinct_target_pct > 1 внутри сигнала означает, что различается.'

SELECT t.horizon_h,
       t.direction,
       count(*)                        AS rows,
       count(DISTINCT t.target_pct)    AS distinct_target_pct,
       min(t.target_pct)               AS min_target_pct,
       max(t.target_pct)               AS max_target_pct,
       count(*) FILTER (WHERE t.target_price IS NOT NULL) AS with_price
FROM signal_targets t
GROUP BY t.horizon_h, t.direction
ORDER BY t.horizon_h, t.direction;

\echo ''
\echo '== 5. ДЕЙСТВУЮЩАЯ ОЦЕНКА (для §9) ========================================'

SELECT e.horizon_h, count(*) AS evaluations,
       count(*) FILTER (WHERE e.success) AS success_true,
       round(avg(e.pnl_pct)::numeric, 4) AS avg_pnl_pct
FROM signal_evaluations e
JOIN signals s ON s.id = e.signal_id
WHERE s.logic_version = :logic_version
GROUP BY e.horizon_h
ORDER BY e.horizon_h;

\echo ''
