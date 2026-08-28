-- ЭТАП 8.10 §3. ПРЕДПРОВЕРКА ДАННЫХ. ТОЛЬКО ЧТЕНИЕ.
--
-- Скрипт не создаёт, не изменяет и не удаляет ничего. Его можно запускать на
-- продакшне в любой момент, не останавливая сервисы.
--
-- Запуск на сервере (СРЕДА: сервер):
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < scripts/precheck_8_10.sql
--
-- ВЕРДИКТА СКРИПТ НЕ ВЫНОСИТ. Он печатает числа; решение принимает исполнитель.
--
-- ГЛАВНЫЙ ВОПРОС — РАЗДЕЛ 2. Подвижный выход измеряется по ВЕРШИНЕ внутри
-- окна, а вершина внутри ЧАСОВОГО бара неизвестна: часовая свеча говорит, что
-- максимум был, но не говорит, когда именно и что было до него. Поэтому пара,
-- посчитанная с resolution='1h', для этого этапа даёт исход куда более грубый,
-- чем минутная, — и доля таких пар обязана быть названа числом ДО того, как
-- кто-нибудь начнёт читать таблицу §5.

\set ON_ERROR_STOP on
\if :{?logic_version} \else \set logic_version 5 \endif

\pset null '·'

\echo ''
\echo '== 1. СКОЛЬКО ПАР В signal_outcomes_barrier ============================='
\echo '   Именно эти пары и станут входом расчёта: подвижный выход считается'
\echo '   на тех же сигналах, ценах и целях, что и исход по границам.'

SELECT count(*)                    AS pairs_total,
       count(DISTINCT signal_id)   AS signals,
       count(DISTINCT horizon_h)   AS horizons,
       min(computed_at)            AS first_computed,
       max(computed_at)            AS last_computed
FROM signal_outcomes_barrier
WHERE logic_version = :logic_version;

\echo ''
\echo '   Ожидаемый объём таблицы этапа: пар × 13 вариантов (§7 ТЗ ~460 тысяч).'

SELECT count(*)      AS pairs_total,
       count(*) * 13 AS trailing_rows_expected
FROM signal_outcomes_barrier
WHERE logic_version = :logic_version;

\echo ''
\echo '== 2. СКОЛЬКО ПАР ПОСЧИТАНО ПО МИНУТНЫМ СВЕЧАМ =========================='
\echo '   resolution=1m — вершина внутри окна известна поминутно.'
\echo '   resolution=1h — вершина известна только как максимум часа: подвижный'
\echo '   выход на таких парах измеряется заведомо грубее.'

SELECT resolution,
       count(*) AS pairs,
       round(100.0 * count(*) / NULLIF(sum(count(*)) OVER (), 0), 2) AS share_pct
FROM signal_outcomes_barrier
WHERE logic_version = :logic_version
GROUP BY resolution
ORDER BY resolution;

\echo ''
\echo '   То же по горизонтам: длинные горизонты чаще не покрыты минутным рядом.'

SELECT horizon_h, resolution, count(*) AS pairs,
       count(*) FILTER (WHERE outcome = 'target')    AS outcome_target,
       count(*) FILTER (WHERE outcome = 'stop')      AS outcome_stop,
       count(*) FILTER (WHERE outcome = 'timeout')   AS outcome_timeout,
       count(*) FILTER (WHERE outcome = 'ambiguous') AS outcome_ambiguous,
       count(*) FILTER (WHERE outcome = 'no_data')   AS outcome_no_data
FROM signal_outcomes_barrier
WHERE logic_version = :logic_version
GROUP BY horizon_h, resolution
ORDER BY horizon_h, resolution;

\echo ''
\echo '== 3. ГЛУБИНА МИНУТНОГО РЯДА ПО КАЖДОМУ ИНСТРУМЕНТУ ====================='
\echo '   Глубина ограничивает этап дважды: сверху — сколько истории вообще'
\echo '   можно измерить, снизу — сколько её останется после политики хранения'
\echo '   (RETENTION_1M_DAYS). Пересчитать подвижный выход по удалённым минутам'
\echo '   невозможно.'

SELECT i.symbol,
       min(o.ts)                                                     AS first_1m,
       max(o.ts)                                                     AS last_1m,
       count(*)                                                      AS bars,
       round(EXTRACT(epoch FROM max(o.ts) - min(o.ts)) / 86400.0, 2) AS depth_days,
       round(EXTRACT(epoch FROM now() - max(o.ts)) / 3600.0, 2)      AS age_h,
       -- Пропуски: сколько минут ДОЛЖНО быть между первой и последней свечой
       -- против того, сколько их есть. Разрыв ряда даёт исход no_data.
       (EXTRACT(epoch FROM max(o.ts) - min(o.ts))::bigint / 60 + 1) - count(*)
                                                                     AS missing_bars
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
WHERE o.timeframe = '1m'
GROUP BY i.symbol
ORDER BY i.symbol;

\echo ''
\echo '== 4. ЕСТЬ ЛИ УЖЕ ТАБЛИЦА ЭТАПА ========================================'
\echo '   Пусто — миграция 017 ещё не применена, и это ожидаемо до развёртывания.'

SELECT to_regclass('trailing_outcomes') AS trailing_outcomes;

\echo ''
