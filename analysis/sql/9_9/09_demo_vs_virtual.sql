-- ЭТАП 9.9, блок 09 — демо против виртуального. Только чтение.
-- Что показывает: насколько реальное исполнение на демо-счёте отличается от виртуального
-- расчёта (проскальзывание, задержка, комиссии), а также сколько покупок система пропустила
-- и с какой суммой работает правило размера mm1 (точка отсчёта Этапа 9.6).
-- Прибыль пары — как в src.demo.ledger.pair_profit: продажа − комиссия продажи в USDT −
-- (покупка + комиссия покупки в USDT); комиссия в монете в деньги не входит.

\pset pager off
\pset null '-'
\pset footer off
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;   -- страховка: сессия не может писать
SET statement_timeout = '600s';
SET TIME ZONE 'UTC';
-- Момент замера. run_9_9.sh передаёт его ключом -v as_of=..., один на все блоки;
-- при ручном запуске файла отдельно берётся текущее время.
\if :{?as_of}
\else
  \set as_of now
\endif
-- Число знаков округления денежных и процентных величин (по умолчанию 2). Тест сверки с
-- src.demo.ledger задаёт больше знаков: -v prec=8.
\if :{?prec}
\else
  \set prec 2
\endif

\x on
WITH pairs AS (                     -- позиции версии 7 с исполненной покупкой И продажей на демо
    SELECT p.id, p.net_pnl_pct,
           b.cost_usd AS b_cost,
           CASE WHEN upper(b.fee_ccy) = 'USDT' THEN abs(coalesce(b.fee_usd, 0)) ELSE 0 END AS b_fee_usdt,
           abs(coalesce(b.fee_usd, 0)) AS b_fee_all, b.slippage_pct AS b_slip, b.lag_sec AS b_lag,
           s.cost_usd AS s_cost,
           CASE WHEN upper(s.fee_ccy) = 'USDT' THEN abs(coalesce(s.fee_usd, 0)) ELSE 0 END AS s_fee_usdt,
           abs(coalesce(s.fee_usd, 0)) AS s_fee_all, s.slippage_pct AS s_slip, s.lag_sec AS s_lag
      FROM positions p
      JOIN demo_orders b ON b.position_id = p.id AND b.leg = 'buy'  AND b.status = 'filled'
      JOIN demo_orders s ON s.position_id = p.id AND s.leg = 'sell' AND s.status = 'filled'
     WHERE p.logic_version = 7 AND p.is_virtual
),
calc AS (                           -- формулы src.demo.ledger.pair_profit / pair_cost
    SELECT pairs.*,
           (s_cost - s_fee_usdt) - (b_cost + b_fee_usdt) AS profit,
           b_cost + b_fee_usdt                            AS cost
      FROM pairs
)
SELECT count(*)                                                       AS "Пар (покупка и продажа исполнены)",
       round(avg(100.0 * profit / NULLIF(cost, 0))::numeric, :prec)  AS "Средний итог демо, %",
       round(avg(net_pnl_pct)::numeric, :prec)                       AS "Средний итог виртуальный, %",
       round((avg(100.0 * profit / NULLIF(cost, 0)) - avg(net_pnl_pct))::numeric, :prec)
                                                                     AS "Разница демо − виртуальный, п.п.",
       round(sum(profit)::numeric, :prec)                            AS "Сумма прибыли демо, $",
       round(avg(b_slip)::numeric, :prec)                            AS "Проскальзывание покупки ср., %",
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY b_slip))::numeric, :prec)
                                                                     AS "Проскальзывание покупки мед., %",
       round(avg(s_slip)::numeric, :prec)                            AS "Проскальзывание продажи ср., %",
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY s_slip))::numeric, :prec)
                                                                     AS "Проскальзывание продажи мед., %",
       round(avg(b_lag)::numeric, :prec)                             AS "Задержка покупки ср., с",
       round(avg(s_lag)::numeric, :prec)                             AS "Задержка продажи ср., с",
       round(sum(b_fee_all + s_fee_all)::numeric, :prec)             AS "Комиссии, $ (обе ноги, в долларах)"
  FROM calc;

\x off
\echo
\echo Пропуски покупок за всё время (status = skipped):
SELECT coalesce(d.skip_reason, '(причина не указана)')  AS "Причина пропуска",
       count(*)                                         AS "Число"
  FROM demo_orders d
  JOIN positions p ON p.id = d.position_id AND p.logic_version = 7 AND p.is_virtual
 WHERE d.leg = 'buy' AND d.status = 'skipped'
 GROUP BY 1
 ORDER BY 2 DESC, 1;

\x on
\echo
\echo Размер покупок по правилу mm1 (точка отсчёта Этапа 9.6):
SELECT count(*)                                                         AS "Строк покупки с sizing_rule = mm1",
       count(*) FILTER (WHERE d.status = 'filled')                      AS "  из них исполнено",
       to_char(min(d.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS "  первая строка (UTC)",
       round(avg(d.requested_cost_usd)::numeric, :prec)                 AS "Средняя заявленная сумма, $",
       round(avg(d.cost_usd) FILTER (WHERE d.status = 'filled')::numeric, :prec)
                                                                        AS "Средняя исполненная сумма, $"
  FROM demo_orders d
  JOIN positions p ON p.id = d.position_id AND p.logic_version = 7 AND p.is_virtual
 WHERE d.leg = 'buy' AND d.sizing_rule = 'mm1';
