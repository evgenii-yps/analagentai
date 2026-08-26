-- ЭТАП 8.9 §8: базовые стратегии рядом с системой. Только чтение.
--
-- ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО ЗДЕСЬ НЕТ. Здесь — описательные числа: сколько пар,
-- какая доля дошла до цели, каков средний результат. Здесь НЕТ вывода о том,
-- лучше система или хуже: разница средних без оценки неопределённости на
-- выборке в двое суток — это не измерение, а впечатление. Оценку даёт
-- scripts/baseline_bootstrap.py, и читать его вывод обязательно ВМЕСТЕ с этим.
--
--   docker compose --profile tools run --rm --no-deps barrier \
--       python -m scripts.baseline_bootstrap
--
-- ПОЧЕМУ СРАВНИВАТЬ МОЖНО НЕ ВСЁ. Стратегии always_buy, always_sell, coin_flip
-- и system стоят на ОДНИХ И ТЕХ ЖЕ моментах — их пары сравнимы попарно.
-- Стратегии grid_buy и grid_sell к сигналам не привязаны вовсе: у них нет
-- signal_id, и общих пар с системой у них НЕТ. Они отвечают на другой вопрос —
-- «каков фон рынка вообще» — и в попарное сравнение не входят. Раздел 9.5
-- показывает их отдельно и рядом, но это соседство, а не сравнение.
--
-- Запуск на сервере:
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < analysis/sql/09_baseline_compare.sql

\pset pager off
\pset null '·'
SET statement_timeout = '600s';

\if :{?logic_version} \else \set logic_version 5 \endif

-- Временное представление создаётся ДО перевода сессии в режим только чтения:
-- CREATE TEMP VIEW — команда записи. Само представление в базу ничего не
-- пишет и исчезает вместе с сессией.
CREATE TEMP VIEW bc_rows AS
SELECT o.strategy, o.signal_id, o.horizon_h, o.direction, o.outcome,
       o.net_pnl_pct, o.target_source, o.resolution, o.entry_ts,
       i.symbol,
       (s.notified_at IS NOT NULL) AS notified
FROM strategy_outcomes o
JOIN instruments i ON i.id = o.instrument_id
LEFT JOIN signals s ON s.id = o.signal_id
WHERE o.logic_version = :logic_version;

-- Общие СОЗРЕВШИЕ пары: пара учитывается, только если у обеих сторон есть
-- результат в деньгах. Пара, где одна сторона ambiguous или no_data, сравнению
-- не подлежит — там неизвестно, что произошло.
CREATE TEMP VIEW bc_paired AS
SELECT b.strategy, b.signal_id, b.horizon_h, b.direction, b.outcome,
       b.net_pnl_pct AS base_pnl, b.symbol, b.notified,
       sy.net_pnl_pct AS sys_pnl, sy.outcome AS sys_outcome,
       sy.direction AS sys_direction
FROM bc_rows b
JOIN bc_rows sy
  ON sy.strategy = 'system'
 AND sy.signal_id = b.signal_id
 AND sy.horizon_h = b.horizon_h
WHERE b.strategy <> 'system'
  AND b.signal_id IS NOT NULL
  AND b.net_pnl_pct IS NOT NULL
  AND sy.net_pnl_pct IS NOT NULL;

SET default_transaction_read_only = on;

\echo
\echo '--- 9.1 Объём: сколько строк у каждой стратегии ---'

SELECT strategy,
       count(*)                                              AS rows,
       count(*) FILTER (WHERE net_pnl_pct IS NOT NULL)       AS with_pnl,
       count(*) FILTER (WHERE outcome IN ('ambiguous','no_data')) AS no_verdict,
       count(DISTINCT signal_id)                             AS signals,
       min(entry_ts)                                         AS first_entry,
       max(entry_ts)                                         AS last_entry
FROM bc_rows
GROUP BY strategy
ORDER BY strategy;

\echo
\echo '--- 9.2 ГЛАВНАЯ ТАБЛИЦА: горизонт x стратегия ---'
\echo '    target_share_pct — доля исхода target среди строк с вердиктом;'
\echo '    avg_net_pnl_pct  — средний результат за вычетом издержек.'

SELECT horizon_h,
       strategy,
       count(*)                                                   AS rows,
       count(*) FILTER (WHERE net_pnl_pct IS NOT NULL)            AS with_pnl,
       round(100.0 * count(*) FILTER (WHERE outcome = 'target')
             / NULLIF(count(*) FILTER (
                   WHERE outcome NOT IN ('ambiguous','no_data')), 0), 2)
                                                                  AS target_share_pct,
       round(avg(net_pnl_pct), 4)                                 AS avg_net_pnl_pct,
       round(stddev_samp(net_pnl_pct), 4)                         AS sd_net_pnl_pct
FROM bc_rows
GROUP BY horizon_h, strategy
ORDER BY horizon_h, strategy;

\echo
\echo '--- 9.3 Попарно с системой: НА ОБЩИХ СОЗРЕВШИХ ПАРАХ ---'
\echo '    diff_avg = средний результат системы МИНУС средний результат базовой.'
\echo '    ВНИМАНИЕ: без доверительного интервала этот столбец ничего не решает.'
\echo '    Интервал считает scripts/baseline_bootstrap.py — читайте его вывод.'

SELECT horizon_h,
       strategy                                  AS baseline,
       count(*)                                  AS paired_rows,
       round(avg(sys_pnl), 4)                    AS avg_system,
       round(avg(base_pnl), 4)                   AS avg_baseline,
       round(avg(sys_pnl - base_pnl), 4)         AS diff_avg,
       count(*) FILTER (WHERE sys_pnl > base_pnl) AS system_better_rows,
       count(*) FILTER (WHERE sys_pnl < base_pnl) AS baseline_better_rows,
       count(*) FILTER (WHERE sys_pnl = base_pnl) AS identical_rows
FROM bc_paired
GROUP BY horizon_h, strategy
ORDER BY horizon_h, strategy;

\echo
\echo '--- 9.4 Разбивка по направлению системы ---'
\echo '    Отвечает на вопрос «не всё ли объясняется тем, что рынок падал».'

SELECT sys_direction,
       horizon_h,
       strategy                          AS baseline,
       count(*)                          AS paired_rows,
       round(avg(sys_pnl), 4)            AS avg_system,
       round(avg(base_pnl), 4)           AS avg_baseline,
       round(avg(sys_pnl - base_pnl), 4) AS diff_avg
FROM bc_paired
GROUP BY sys_direction, horizon_h, strategy
ORDER BY sys_direction, horizon_h, strategy;

\echo
\echo '--- 9.5 Разбивка по признаку «сигнал был отправлен человеку» ---'
\echo '    notified = true — сигнал прошёл пороги уведомления и дошёл до чата.'

SELECT notified,
       horizon_h,
       strategy                          AS baseline,
       count(*)                          AS paired_rows,
       round(avg(sys_pnl), 4)            AS avg_system,
       round(avg(base_pnl), 4)           AS avg_baseline,
       round(avg(sys_pnl - base_pnl), 4) AS diff_avg
FROM bc_paired
GROUP BY notified, horizon_h, strategy
ORDER BY notified DESC, horizon_h, strategy;

\echo
\echo '--- 9.6 Фон рынка: сетка §5. НЕ сравнение, а соседство ---'
\echo '    У сетки нет общих пар с системой: она входит каждый час независимо.'
\echo '    Числа стоят рядом, чтобы видеть порядок величин, а не чтобы вычитать.'

SELECT horizon_h,
       strategy,
       count(*)                                                   AS rows,
       round(100.0 * count(*) FILTER (WHERE outcome = 'target')
             / NULLIF(count(*) FILTER (
                   WHERE outcome NOT IN ('ambiguous','no_data')), 0), 2)
                                                                  AS target_share_pct,
       round(avg(net_pnl_pct), 4)                                 AS avg_net_pnl_pct
FROM bc_rows
WHERE strategy IN ('grid_buy', 'grid_sell', 'system')
GROUP BY horizon_h, strategy
ORDER BY horizon_h, strategy;

\echo
\echo '--- 9.7 Происхождение целей: доказательство, что подмены не было ---'
\echo '    frozen — цель, названная человеку; risk_targets:<дата> — историческая.'
\echo '    Дата ОБЯЗАНА быть не позже даты входа. Строк с сегодняшней целью на'
\echo '    вчерашнем входе быть не должно ни одной.'

SELECT strategy, target_source, count(*) AS rows,
       min(entry_ts)::date AS first_entry_day,
       max(entry_ts)::date AS last_entry_day
FROM bc_rows
GROUP BY strategy, target_source
ORDER BY strategy, target_source;

\echo
\echo '    Проверка фактом: сколько строк взяли цель ПОЗЖЕ момента входа.'
\echo '    Ожидается ровно 0. Любое иное число означает подделку истории.'

SELECT count(*) AS rows_with_future_target
FROM strategy_outcomes
WHERE target_source <> 'frozen'
  AND to_date(right(target_source, 10), 'YYYY-MM-DD') > entry_ts::date;

\echo
