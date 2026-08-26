-- ЭТАП 8.9 §3. БЛОКИРУЮЩАЯ ПРЕДПРОВЕРКА ДАННЫХ. ТОЛЬКО ЧТЕНИЕ.
--
-- Скрипт не создаёт, не изменяет и не удаляет ничего. Его можно запускать на
-- продакшне в любой момент, не останавливая сервисы.
--
-- Запуск на сервере (СРЕДА: сервер):
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < scripts/precheck_8_9.sql
--
-- ВЕРДИКТА СКРИПТ НЕ ВЫНОСИТ. Он печатает числа; решение принимает исполнитель.
--
-- ГЛАВНЫЙ ВОПРОС — РАЗДЕЛ 1. Базовые стратегии считают ВСТРЕЧНОЕ направление:
-- там, где система сказала «покупать», always_sell обязан продать. Цель для
-- встречного направления в signal_targets НЕ ЗАМОРОЖЕНА — её там нет вовсе,
-- потому что замораживалась цель только выданного направления. Значит, она
-- берётся из risk_targets ЗА ДАТУ СИГНАЛА. Если risk_targets хранит лишь
-- сегодняшний срез, взять её неоткуда, и этап останавливается: подстановка
-- сегодняшней цели во вчерашний сигнал — подделка истории.

\set ON_ERROR_STOP on
\if :{?logic_version} \else \set logic_version 5 \endif

\pset null '·'

\echo ''
\echo '== 1. risk_targets: ИСТОРИЯ ПО ДАТАМ ИЛИ ТЕКУЩИЙ СРЕЗ? =================='
\echo '   Ответ по КЛЮЧУ таблицы: входит ли computed_at в первичный ключ.'
\echo '   Входит — каждый пересчёт добавляет строку, история есть.'
\echo '   Не входит — строка перезаписывается, истории нет (БЛОКИРУЮЩЕЕ).'

SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
WHERE c.conrelid = 'risk_targets'::regclass AND c.contype = 'p';

\echo ''
\echo '   И ответ ПО ДАННЫМ: сколько РАЗНЫХ computed_at фактически лежит.'
\echo '   distinct_computed_at = 1 означает, что истории пока нет НИ ОДНОЙ,'
\echo '   даже если схема её допускает.'

SELECT count(*)                              AS rows_total,
       count(DISTINCT computed_at)           AS distinct_computed_at,
       min(computed_at)                      AS first_computed,
       max(computed_at)                      AS last_computed,
       count(DISTINCT date_trunc('day', computed_at)) AS distinct_days
FROM risk_targets;

\echo ''
\echo '   Разбивка по суткам: в какие дни пересчёт отработал, сколько строк.'

SELECT date_trunc('day', computed_at)::date AS day,
       count(*)                             AS rows,
       count(DISTINCT computed_at)          AS runs,
       count(*) FILTER (WHERE target_pct IS NOT NULL) AS with_target,
       count(DISTINCT instrument_id)        AS instruments
FROM risk_targets
GROUP BY 1
ORDER BY 1;

\echo ''
\echo '== 1b. ПОКРЫТИЕ: есть ли цель НА ДАТУ КАЖДОГО СИГНАЛА ==================='
\echo '   covered — для пары (инструмент, горизонт, ВСТРЕЧНОЕ направление)'
\echo '   нашлась строка risk_targets с computed_at <= момента сигнала.'
\echo '   uncovered — не нашлась: такие пары встречной стратегии не получат.'

WITH pairs AS (
    SELECT s.id, s.instrument_id, s.ts, s.decision,
           CASE WHEN s.decision = 'buy' THEN 'sell' ELSE 'buy' END AS opposite,
           h.horizon_h
    FROM signals s
    CROSS JOIN (SELECT unnest(ARRAY[1, 4, 12, 24]) AS horizon_h) h
    WHERE s.decision <> 'wait'
      AND s.logic_version = :logic_version
      AND s.ts + make_interval(hours => h.horizon_h) <= now()
)
SELECT p.horizon_h,
       count(*) AS pairs,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM risk_targets r
           WHERE r.instrument_id = p.instrument_id
             AND r.horizon_h = p.horizon_h
             AND r.direction = p.opposite
             AND r.computed_at <= p.ts
             AND r.target_pct IS NOT NULL
       )) AS covered,
       count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM risk_targets r
           WHERE r.instrument_id = p.instrument_id
             AND r.horizon_h = p.horizon_h
             AND r.direction = p.opposite
             AND r.computed_at <= p.ts
             AND r.target_pct IS NOT NULL
       )) AS uncovered
FROM pairs p
GROUP BY p.horizon_h
ORDER BY p.horizon_h;

\echo ''
\echo '== 2. signal_targets: одно направление или оба? ========================='
\echo '   Ключ таблицы и фактическое число направлений на один (сигнал, горизонт).'
\echo '   max_directions = 1 означает: заморожено ТОЛЬКО направление сигнала,'
\echo '   и встречную цель придётся брать из risk_targets (раздел 1).'

SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
WHERE c.conrelid = 'signal_targets'::regclass AND c.contype = 'p';

SELECT max(n)    AS max_directions_per_pair,
       min(n)    AS min_directions_per_pair,
       count(*)  AS pairs
FROM (
    SELECT signal_id, horizon_h, count(DISTINCT direction) AS n
    FROM signal_targets
    GROUP BY signal_id, horizon_h
) d;

\echo ''
\echo '   Совпадает ли направление замороженной цели с решением сигнала:'

SELECT count(*)                                          AS rows,
       count(*) FILTER (WHERE t.direction = s.decision)  AS same_as_decision,
       count(*) FILTER (WHERE t.direction <> s.decision) AS differs
FROM signal_targets t
JOIN signals s ON s.id = t.signal_id;

\echo ''
\echo '== 3. МИНУТНЫЕ СВЕЧИ: с какой даты по каждому инструменту ==============='
\echo '   Ожидание ТЗ: BTC с 08.08, ETH с 22.08, остальные с 23.08.'

SELECT i.symbol,
       min(o.ts)                                              AS first_1m,
       max(o.ts)                                              AS last_1m,
       count(*)                                               AS bars,
       round(EXTRACT(epoch FROM max(o.ts) - min(o.ts)) / 86400.0, 2) AS depth_days,
       round(EXTRACT(epoch FROM now() - max(o.ts)) / 3600.0, 2)      AS age_h
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
WHERE o.timeframe = '1m'
GROUP BY i.symbol
ORDER BY i.symbol;

\echo ''
\echo '   Часовая сетка §5: сколько моментов «ровно 00 минут» покрыто рядом.'

SELECT i.symbol,
       count(*) FILTER (WHERE EXTRACT(minute FROM o.ts) = 0) AS hourly_marks
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
WHERE o.timeframe = '1m'
GROUP BY i.symbol
ORDER BY i.symbol;

\echo ''
\echo '== 4. signal_outcomes_barrier: объём и разброс computed_at =============='

SELECT count(*)                    AS rows_total,
       count(DISTINCT signal_id)   AS signals,
       count(DISTINCT computed_at) AS distinct_computed_at,
       min(computed_at)            AS first_computed,
       max(computed_at)            AS last_computed,
       min(entry.ts)               AS earliest_signal_ts,
       max(entry.ts)               AS latest_signal_ts
FROM signal_outcomes_barrier b
JOIN signals entry ON entry.id = b.signal_id;

\echo ''
\echo '   По горизонту и исходу — основа будущего сравнения §8.'

SELECT horizon_h, outcome, resolution, count(*) AS rows,
       count(*) FILTER (WHERE net_pnl_pct IS NOT NULL) AS with_pnl
FROM signal_outcomes_barrier
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
