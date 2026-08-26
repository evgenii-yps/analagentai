-- ЭТАП 8.8 §9: две оценки исхода рядом. Только чтение.
--
-- ЧТО СРАВНИВАЕТСЯ. Действующая оценка (signal_evaluations) фиксирует
-- положение цены в момент t + горизонт: success = «цена оказалась в нужной
-- стороне К СРОКУ». Новая (signal_outcomes_barrier) отвечает на другой вопрос:
-- «какая граница задета ПЕРВОЙ». Это РАЗНЫЕ величины, и расхождение между
-- ними — не ошибка одной из них, а содержание раздела.
--
-- ВЫВОДОВ О ТОМ, КАКАЯ МЕТРИКА ЛУЧШЕ, ЭТОТ ЗАПРОС НЕ ДЕЛАЕТ И ДЕЛАТЬ НЕ ДАЁТ
-- (§9 ТЗ). Данных за первые сутки на такой вывод не хватает. Задача — свести
-- числа рядом и подготовить сравнение, а не вынести приговор.
--
-- ГДЕ ОНИ ПРИНЦИПИАЛЬНО РАСХОДЯТСЯ, и почему на это надо смотреть в первую
-- очередь:
--   old_fail_new_target — старая метрика пишет провал, новая пишет target:
--       цена ДОХОДИЛА до цели внутри окна и вернулась. Человек, поставивший
--       ордер на цель, забрал бы прибыль; замер к сроку её не увидел.
--   old_success_new_stop — обратное: цена сначала пробила предел, а к сроку
--       вернулась в плюс. Человек со стоп-ордером получил бы убыток.
--
-- Запуск на сервере:
--   sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f /dev/stdin < analysis/sql/08_metric_compare.sql

\pset pager off
\pset null '·'
SET statement_timeout = '600s';

\if :{?logic_version} \else \set logic_version 5 \endif

-- ПОРЯДОК ЗДЕСЬ ЗНАЧИМ. Временное представление создаётся ДО перевода сессии в
-- режим только чтения: CREATE TEMP VIEW — команда записи, и в read-only
-- транзакции она не выполняется. Само представление ничего не пишет в базу и
-- исчезает вместе с сессией; продакшн-таблицы запрос не трогает вовсе.
-- Общая основа: по одному сигналу и горизонту — обе оценки рядом. Соединение
-- ВНУТРЕННЕЕ намеренно: строки, где есть только одна из оценок, сравнению не
-- подлежат, и их число показано отдельно разделом 9.1.
CREATE TEMP VIEW mc_pairs AS
SELECT s.id                AS signal_id,
       i.symbol,
       s.ts,
       s.decision,
       b.horizon_h,
       e.success           AS old_success,
       e.pnl_pct           AS old_pnl_pct,
       b.outcome           AS new_outcome,
       b.net_pnl_pct       AS new_net_pnl_pct,
       b.resolution,
       b.target_pct,
       b.stop_pct,
       b.cost_pct
FROM signal_outcomes_barrier b
JOIN signals s            ON s.id = b.signal_id
JOIN instruments i        ON i.id = s.instrument_id
JOIN signal_evaluations e ON e.signal_id = b.signal_id AND e.horizon_h = b.horizon_h
WHERE b.logic_version = :logic_version;

-- С этого места сессия только на чтение: ни одна строка продакшна не может
-- быть изменена даже случайной опечаткой в запросе ниже.
SET default_transaction_read_only = on;

\echo
\echo '--- 9.1 Покрытие: у скольких пар есть обе оценки ---'
\echo '    Пара без второй оценки в сравнение не входит; её надо видеть числом.'

SELECT b.horizon_h,
       count(*)                                                     AS barrier_rows,
       count(e.signal_id)                                           AS with_old_eval,
       count(*) - count(e.signal_id)                                AS barrier_only
FROM signal_outcomes_barrier b
LEFT JOIN signal_evaluations e
       ON e.signal_id = b.signal_id AND e.horizon_h = b.horizon_h
WHERE b.logic_version = :logic_version
GROUP BY b.horizon_h
ORDER BY b.horizon_h;

\echo
\echo '--- 9.2 Совпадение вердиктов по горизонтам ---'
\echo '    Вердикты приравниваются так: старый успех  <-> новый target;'
\echo '    старый провал <-> новый stop или timeout с отрицательным итогом.'
\echo '    ambiguous и no_data вердикта не имеют и в согласие не считаются.'

SELECT horizon_h,
       count(*)                                                          AS pairs,
       count(*) FILTER (WHERE new_outcome IN ('ambiguous', 'no_data'))    AS no_verdict,
       count(*) FILTER (WHERE new_outcome NOT IN ('ambiguous', 'no_data')
                          AND old_success = (new_outcome = 'target'))     AS agree,
       round(100.0 * count(*) FILTER (WHERE new_outcome NOT IN ('ambiguous', 'no_data')
                                        AND old_success = (new_outcome = 'target'))
             / NULLIF(count(*) FILTER (
                   WHERE new_outcome NOT IN ('ambiguous', 'no_data')), 0), 2)
                                                                          AS agree_pct,
       count(*) FILTER (WHERE old_success IS FALSE
                          AND new_outcome = 'target')                     AS old_fail_new_target,
       count(*) FILTER (WHERE old_success IS TRUE
                          AND new_outcome = 'stop')                       AS old_success_new_stop,
       round(avg(old_pnl_pct)::numeric, 4)                                AS avg_old_pnl_pct,
       round(avg(new_net_pnl_pct), 4)                                     AS avg_new_net_pnl_pct
FROM mc_pairs
GROUP BY horizon_h
ORDER BY horizon_h;

\echo
\echo '--- 9.3 Итог по всем горизонтам вместе ---'

SELECT count(*)                                                          AS pairs,
       count(*) FILTER (WHERE new_outcome IN ('ambiguous', 'no_data'))    AS no_verdict,
       round(100.0 * count(*) FILTER (WHERE new_outcome NOT IN ('ambiguous', 'no_data')
                                        AND old_success = (new_outcome = 'target'))
             / NULLIF(count(*) FILTER (
                   WHERE new_outcome NOT IN ('ambiguous', 'no_data')), 0), 2)
                                                                          AS agree_pct,
       round(100.0 * count(*) FILTER (WHERE old_success IS FALSE
                                        AND new_outcome = 'target')
             / NULLIF(count(*), 0), 2)                                    AS old_fail_new_target_pct,
       round(100.0 * count(*) FILTER (WHERE old_success IS TRUE
                                        AND new_outcome = 'stop')
             / NULLIF(count(*), 0), 2)                                    AS old_success_new_stop_pct,
       round(avg(old_pnl_pct)::numeric, 4)                                AS avg_old_pnl_pct,
       round(avg(new_net_pnl_pct), 4)                                     AS avg_new_net_pnl_pct
FROM mc_pairs;

\echo
\echo '--- 9.4 Полная таблица соответствия: старый вердикт x новый исход ---'

SELECT horizon_h,
       old_success,
       new_outcome,
       count(*)                                  AS pairs,
       round(avg(old_pnl_pct)::numeric, 4)       AS avg_old_pnl_pct,
       round(avg(new_net_pnl_pct), 4)            AS avg_new_net_pnl_pct
FROM mc_pairs
GROUP BY horizon_h, old_success, new_outcome
ORDER BY horizon_h, old_success, new_outcome;

\echo
\echo '--- 9.5 Чем считали: доля минутного разрешения ---'
\echo '    resolution = 1h означает, что порядок касаний внутри бара неизвестен.'

SELECT horizon_h,
       resolution,
       count(*)                                                     AS pairs,
       count(*) FILTER (WHERE new_outcome = 'ambiguous')            AS ambiguous
FROM mc_pairs
GROUP BY horizon_h, resolution
ORDER BY horizon_h, resolution;

\echo
\echo '--- 9.6 Расхождения поимённо: первые 50 самых заметных ---'
\echo '    Сигналы, где метрики дают противоположный ответ. Для разбора руками.'

SELECT signal_id, symbol, ts, decision, horizon_h,
       old_success, round(old_pnl_pct::numeric, 4) AS old_pnl_pct,
       new_outcome, round(new_net_pnl_pct, 4)      AS new_net_pnl_pct,
       resolution
FROM mc_pairs
WHERE (old_success IS FALSE AND new_outcome = 'target')
   OR (old_success IS TRUE  AND new_outcome = 'stop')
ORDER BY abs(COALESCE(new_net_pnl_pct, 0) - COALESCE(old_pnl_pct, 0)::numeric) DESC
LIMIT 50;

\echo
