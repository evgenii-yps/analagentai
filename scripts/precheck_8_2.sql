-- ЭТАП 8.2 §1. БЛОКИРУЮЩАЯ ПРЕДПРОВЕРКА ДАННЫХ. ТОЛЬКО ЧТЕНИЕ.
--
-- Скрипт не создаёт, не изменяет и не удаляет ничего: ни одной команды кроме
-- SELECT в нём нет. Его можно запускать на продакшне в любой момент.
--
-- Запуск на сервере:
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < scripts/precheck_8_2.sql
--
-- Значения по умолчанию можно переопределить:
--   ... psql -v bar=1H -v window_hours=2160 -v max_age_hours=3 -f /dev/stdin < ...
--
-- ВЕРДИКТА СКРИПТ НЕ ВЫНОСИТ. Он печатает числа; выполнены ли пороги допуска
-- (§1 ТЗ), решает исполнитель по отчёту. Строка «всё в порядке» здесь была бы
-- утверждением о данных, которых скрипт не видел в момент написания.
--
-- ПОРОГИ ДОПУСКА (§1 ТЗ), с которыми сравниваются столбцы ниже:
--   * непрерывный ряд не короче 2160 часов (90 суток) — столбец max_run_hours;
--   * последняя свеча не старше 3 часов      — столбец age_hours;
--   * P-3 = 0                                 — столбец bad_invariants;
--   * P-4 <= 5% строк                         — столбец flat_pct.

\set ON_ERROR_STOP on
\if :{?bar}          \else \set bar          '1H'  \endif
\if :{?window_hours} \else \set window_hours 2160  \endif
\if :{?max_age_hours} \else \set max_age_hours 3   \endif

\pset null '·'

-- Пять спотовых инструментов Этапа 8.1. Список ЗАДАН ЯВНО, а не выведен из
-- содержимого таблицы: инструмент, которого в backtest.candles нет вовсе,
-- обязан быть виден строкой с нулями, а не молча выпасть из отчёта.
CREATE TEMP VIEW pc_expected(inst_id) AS
    VALUES ('BTC-USDT'), ('ETH-USDT'), ('SOL-USDT'), ('XRP-USDT'), ('DOGE-USDT');

CREATE TEMP VIEW pc_rows AS
    SELECT c.inst_id, c.open_time, c.open, c.high, c.low, c.close
    FROM backtest.candles c
    WHERE c.bar = :'bar';

\echo
\echo '=== P-1. Глубина ряда: число свечей и границы (bar =' :bar ') ==='
SELECT e.inst_id,
       count(r.open_time)                                   AS candles,
       min(r.open_time)                                     AS first_open_time,
       max(r.open_time)                                     AS last_open_time,
       round(extract(epoch FROM (now() - max(r.open_time))) / 3600.0, 2)
                                                            AS age_hours,
       round(extract(epoch FROM (max(r.open_time) - min(r.open_time)))
             / 3600.0 / 24.0, 2)                            AS span_days
FROM pc_expected e
LEFT JOIN pc_rows r ON r.inst_id = e.inst_id
GROUP BY e.inst_id
ORDER BY e.inst_id;

\echo
\echo '=== P-2. Пропуски внутри ряда и самый длинный непрерывный отрезок ==='
-- missing_hours — сколько часовых свечей не хватает между первой и последней;
-- gap_events    — сколько РАЗРЫВОВ (соседние свечи дальше часа друг от друга);
-- max_run_hours — длина самого длинного непрерывного отрезка, в часах. Именно
--                 она сравнивается с порогом 2160, а не общее число свечей:
--                 ряд из 3000 свечей с дырой посередине порога не проходит.
-- off_grid      — свечи, не выровненные по началу часа (кратность нарушена).
WITH islands AS (
    SELECT inst_id,
           open_time,
           open_time - (row_number() OVER (PARTITION BY inst_id ORDER BY open_time))
                       * interval '1 hour' AS grp
    FROM pc_rows
),
runs AS (
    SELECT inst_id, grp, count(*) AS run_len,
           min(open_time) AS run_from, max(open_time) AS run_to
    FROM islands GROUP BY inst_id, grp
),
best AS (
    SELECT DISTINCT ON (inst_id) inst_id, run_len, run_from, run_to
    FROM runs ORDER BY inst_id, run_len DESC, run_from DESC
),
totals AS (
    SELECT inst_id,
           count(*) AS n,
           (extract(epoch FROM (max(open_time) - min(open_time))) / 3600)::bigint + 1
               AS expected_n,
           count(*) FILTER (
               WHERE date_trunc('hour', open_time) <> open_time
           ) AS off_grid
    FROM pc_rows GROUP BY inst_id
)
SELECT e.inst_id,
       coalesce(t.expected_n, 0) - coalesce(t.n, 0) AS missing_hours,
       coalesce(g.gap_events, 0)                    AS gap_events,
       coalesce(b.run_len, 0)                       AS max_run_hours,
       b.run_from                                   AS longest_run_from,
       b.run_to                                     AS longest_run_to,
       coalesce(t.off_grid, 0)                      AS off_grid,
       :window_hours                                AS need_hours
FROM pc_expected e
LEFT JOIN totals t ON t.inst_id = e.inst_id
LEFT JOIN best   b ON b.inst_id = e.inst_id
LEFT JOIN (
    SELECT inst_id, count(*) - 1 AS gap_events
    FROM runs GROUP BY inst_id
) g ON g.inst_id = e.inst_id
ORDER BY e.inst_id;

\echo
\echo '=== P-2b. Десять самых крупных разрывов (для отчёта) ==='
WITH islands AS (
    SELECT inst_id, open_time,
           open_time - (row_number() OVER (PARTITION BY inst_id ORDER BY open_time))
                       * interval '1 hour' AS grp
    FROM pc_rows
),
runs AS (
    SELECT inst_id, min(open_time) AS run_from, max(open_time) AS run_to
    FROM islands GROUP BY inst_id, grp
),
ordered AS (
    SELECT inst_id, run_to,
           lead(run_from) OVER (PARTITION BY inst_id ORDER BY run_from) AS next_from
    FROM runs
)
SELECT inst_id,
       run_to                                                   AS gap_from,
       next_from                                                AS gap_to,
       (extract(epoch FROM (next_from - run_to)) / 3600)::bigint - 1 AS missing_hours
FROM ordered
WHERE next_from IS NOT NULL
ORDER BY missing_hours DESC, inst_id
LIMIT 10;

\echo
\echo '=== P-3. Нарушения инвариантов свечи (порог допуска: 0) ==='
SELECT e.inst_id,
       count(r.open_time) FILTER (
           WHERE r.high < GREATEST(r.open, r.close)
              OR r.low  > LEAST(r.open, r.close)
              OR r.high < r.low
              OR r.low <= 0
       ) AS bad_invariants,
       count(r.open_time) FILTER (WHERE r.high < GREATEST(r.open, r.close)) AS high_below_body,
       count(r.open_time) FILTER (WHERE r.low  > LEAST(r.open, r.close))    AS low_above_body,
       count(r.open_time) FILTER (WHERE r.high < r.low)                     AS high_below_low,
       count(r.open_time) FILTER (WHERE r.low <= 0)                         AS low_not_positive
FROM pc_expected e
LEFT JOIN pc_rows r ON r.inst_id = e.inst_id
GROUP BY e.inst_id
ORDER BY e.inst_id;

\echo
\echo '=== P-4. Плоские свечи high = low (порог допуска: не более 5%) ==='
SELECT e.inst_id,
       count(r.open_time)                                        AS candles,
       count(r.open_time) FILTER (WHERE r.high = r.low)          AS flat,
       CASE WHEN count(r.open_time) = 0 THEN NULL
            ELSE round(100.0 * count(r.open_time) FILTER (WHERE r.high = r.low)
                       / count(r.open_time), 3)
       END                                                       AS flat_pct
FROM pc_expected e
LEFT JOIN pc_rows r ON r.inst_id = e.inst_id
GROUP BY e.inst_id
ORDER BY e.inst_id;

\echo
\echo '=== P-5. Дубли open_time (контроль самой проверки: обязан быть 0) ==='
-- Ноль здесь — не «данные хорошие», а «первичный ключ на месте и проверка
-- смотрит туда, куда нужно». Ненулевое значение означало бы, что ключа нет.
SELECT e.inst_id,
       coalesce(d.dup_times, 0)  AS duplicate_open_times,
       coalesce(d.extra_rows, 0) AS extra_rows
FROM pc_expected e
LEFT JOIN (
    SELECT inst_id, count(*) AS dup_times, sum(n) - count(*) AS extra_rows
    FROM (
        SELECT inst_id, open_time, count(*) AS n
        FROM pc_rows GROUP BY inst_id, open_time HAVING count(*) > 1
    ) q GROUP BY inst_id
) d ON d.inst_id = e.inst_id
ORDER BY e.inst_id;

\echo
\echo '=== Свод: столбцы для сравнения с порогами §1 (вердикт выносит отчёт) ==='
WITH islands AS (
    SELECT inst_id, open_time,
           open_time - (row_number() OVER (PARTITION BY inst_id ORDER BY open_time))
                       * interval '1 hour' AS grp
    FROM pc_rows
),
runs AS (
    SELECT inst_id, count(*) AS run_len FROM islands GROUP BY inst_id, grp
),
agg AS (
    SELECT inst_id,
           count(*) AS candles,
           max(open_time) AS last_open_time,
           count(*) FILTER (
               WHERE high < GREATEST(open, close) OR low > LEAST(open, close)
                  OR high < low OR low <= 0
           ) AS bad_invariants,
           count(*) FILTER (WHERE high = low) AS flat
    FROM pc_rows GROUP BY inst_id
)
SELECT e.inst_id,
       coalesce(a.candles, 0)                                AS candles,
       coalesce((SELECT max(run_len) FROM runs r WHERE r.inst_id = e.inst_id), 0)
                                                             AS max_run_hours,
       :window_hours                                         AS need_run_hours,
       round(extract(epoch FROM (now() - a.last_open_time)) / 3600.0, 2)
                                                             AS age_hours,
       :max_age_hours                                        AS max_age_hours,
       coalesce(a.bad_invariants, 0)                         AS bad_invariants,
       CASE WHEN coalesce(a.candles, 0) = 0 THEN NULL
            ELSE round(100.0 * a.flat / a.candles, 3) END    AS flat_pct
FROM pc_expected e
LEFT JOIN agg a ON a.inst_id = e.inst_id
ORDER BY e.inst_id;
