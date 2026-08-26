-- ЭТАП 8.8 §8: замер противоположного отклонения. Только чтение.
--
-- ЗАЧЕМ ЭТОТ ЗАПРОС СУЩЕСТВУЕТ. BARRIER_STOP_PCT = 1.0 — ПРЕДПОЛОЖЕНИЕ
-- владельца, а не измерение. Никто не мерил, насколько далеко цена уходит
-- против сигнала прежде, чем вернуться. Запрос даёт числа для пересмотра.
--
-- ПОРОГИ ПО ИТОГАМ ЭТОГО ЗАМЕРА НЕ МЕНЯЮТСЯ (§8 ТЗ): задача раздела —
-- доложить таблицу и остановиться. Изменение предела — отдельное решение
-- владельца, принимаемое после доклада, а не следствие запуска запроса.
--
-- КАК ЧИТАТЬ ГЛАВНЫЙ СТОЛБЕЦ. wasted_pct — доля сигналов, у которых предел
-- УРОВНЯ L сработал бы, НО цель всё равно была бы достигнута в том же окне.
-- Это цена ложного срабатывания: чем ниже предел, тем чаще он режет сделку,
-- которая закончилась бы прибылью.
--
-- ГРАНИЦА ТОЧНОСТИ, о которой обязан знать читатель. Расчёт идёт по
-- накопленным mae_pct и mfe_pct — крайним отклонениям ЗА ВСЁ ОКНО. Порядок
-- между ними в этих двух числах не сохранён: столбец wasted_pct отвечает на
-- вопрос «дошла ли цена до цели в том же окне», а не «дошла ли ПОСЛЕ того,
-- как задела предел». Точный порядок восстанавливается только повторным
-- проходом по свечам, и для выбора уровня предела эта оценка СВЕРХУ
-- достаточна: она завышает долю напрасных срабатываний, а не занижает.
--
-- Запуск на сервере:
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < analysis/sql/07_stop_levels.sql

\pset pager off
\pset null '·'
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\if :{?logic_version} \else \set logic_version 5 \endif

\echo
\echo '--- 8.1 Объём выборки: на чём вообще считается таблица ---'
SELECT horizon_h,
       count(*)                                            AS rows,
       count(DISTINCT signal_id)                           AS signals,
       min(computed_at)                                    AS first_computed,
       max(computed_at)                                    AS last_computed,
       round(avg(target_pct), 4)                           AS avg_target_pct,
       round(avg(mae_pct), 4)                              AS avg_mae_pct,
       round(avg(mfe_pct), 4)                              AS avg_mfe_pct
FROM signal_outcomes_barrier
WHERE logic_version = :logic_version
GROUP BY horizon_h
ORDER BY horizon_h;

\echo
\echo '--- 8.2 Уровни предела: доля срабатываний и доля напрасных ---'
\echo '    would_stop_pct — сработал бы предел этого уровня (mae >= уровень);'
\echo '    wasted_pct     — сработал бы И цель всё равно взята (mfe >= цель);'
\echo '    оба столбца — проценты от rows той же строки.'

WITH levels(stop_level) AS (
    VALUES (0.30::numeric), (0.50), (0.75), (1.00), (1.50), (2.00), (3.00)
), base AS (
    SELECT b.horizon_h, b.mae_pct, b.mfe_pct, b.target_pct
    FROM signal_outcomes_barrier b
    WHERE b.logic_version = :logic_version
      -- no_data исключён намеренно: у окна без свечей mae/mfe — заполнители
      -- NOT NULL, а не измерение, и в долях им места нет.
      AND b.outcome <> 'no_data'
)
SELECT l.stop_level,
       count(*)                                                       AS rows,
       count(*) FILTER (WHERE b.mae_pct >= l.stop_level)              AS would_stop,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level)
             / NULLIF(count(*), 0), 2)                                AS would_stop_pct,
       count(*) FILTER (WHERE b.mae_pct >= l.stop_level
                          AND b.mfe_pct >= b.target_pct)              AS wasted,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level
                                        AND b.mfe_pct >= b.target_pct)
             / NULLIF(count(*) FILTER (WHERE b.mae_pct >= l.stop_level), 0), 2)
                                                                      AS wasted_share_of_stopped_pct
FROM levels l
CROSS JOIN base b
GROUP BY l.stop_level
ORDER BY l.stop_level;

\echo
\echo '--- 8.3 Разбивка по горизонту ---'

WITH levels(stop_level) AS (
    VALUES (0.30::numeric), (0.50), (0.75), (1.00), (1.50), (2.00), (3.00)
), base AS (
    SELECT b.horizon_h, b.mae_pct, b.mfe_pct, b.target_pct
    FROM signal_outcomes_barrier b
    WHERE b.logic_version = :logic_version AND b.outcome <> 'no_data'
)
SELECT b.horizon_h,
       l.stop_level,
       count(*)                                                       AS rows,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level)
             / NULLIF(count(*), 0), 2)                                AS would_stop_pct,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level
                                        AND b.mfe_pct >= b.target_pct)
             / NULLIF(count(*) FILTER (WHERE b.mae_pct >= l.stop_level), 0), 2)
                                                                      AS wasted_share_of_stopped_pct
FROM levels l
CROSS JOIN base b
GROUP BY b.horizon_h, l.stop_level
ORDER BY b.horizon_h, l.stop_level;

\echo
\echo '--- 8.4 Разбивка по токену ---'

WITH levels(stop_level) AS (
    VALUES (0.30::numeric), (0.50), (0.75), (1.00), (1.50), (2.00), (3.00)
), base AS (
    SELECT i.symbol, b.mae_pct, b.mfe_pct, b.target_pct
    FROM signal_outcomes_barrier b
    JOIN signals s     ON s.id = b.signal_id
    JOIN instruments i ON i.id = s.instrument_id
    WHERE b.logic_version = :logic_version AND b.outcome <> 'no_data'
)
SELECT b.symbol,
       l.stop_level,
       count(*)                                                       AS rows,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level)
             / NULLIF(count(*), 0), 2)                                AS would_stop_pct,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level
                                        AND b.mfe_pct >= b.target_pct)
             / NULLIF(count(*) FILTER (WHERE b.mae_pct >= l.stop_level), 0), 2)
                                                                      AS wasted_share_of_stopped_pct
FROM levels l
CROSS JOIN base b
GROUP BY b.symbol, l.stop_level
ORDER BY b.symbol, l.stop_level;

\echo
\echo '--- 8.5 Токен x горизонт: полная разбивка §8 ---'

WITH levels(stop_level) AS (
    VALUES (0.30::numeric), (0.50), (0.75), (1.00), (1.50), (2.00), (3.00)
), base AS (
    SELECT i.symbol, b.horizon_h, b.mae_pct, b.mfe_pct, b.target_pct
    FROM signal_outcomes_barrier b
    JOIN signals s     ON s.id = b.signal_id
    JOIN instruments i ON i.id = s.instrument_id
    WHERE b.logic_version = :logic_version AND b.outcome <> 'no_data'
)
SELECT b.symbol,
       b.horizon_h,
       l.stop_level,
       count(*)                                                       AS rows,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level)
             / NULLIF(count(*), 0), 2)                                AS would_stop_pct,
       round(100.0 * count(*) FILTER (WHERE b.mae_pct >= l.stop_level
                                        AND b.mfe_pct >= b.target_pct)
             / NULLIF(count(*) FILTER (WHERE b.mae_pct >= l.stop_level), 0), 2)
                                                                      AS wasted_share_of_stopped_pct
FROM levels l
CROSS JOIN base b
GROUP BY b.symbol, b.horizon_h, l.stop_level
ORDER BY b.symbol, b.horizon_h, l.stop_level;

\echo
