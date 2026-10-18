-- ЭТАП 9.6. Манименеджмент демо-покупок: чем и от какого баланса посчитана сумма покупки.
--
-- ЗАЧЕМ. Размер демо-покупки перестаёт быть фиксированным ($2): при DEMO_MM_ENABLED=true
-- это процент от текущего баланса учёта с резервом и потолком на монету (src/demo/rules.py,
-- mm_order_cost). Чтобы через месяц можно было по базе посчитать, как часто правило
-- упиралось в резерв и потолок и от какого баланса считались суммы, каждая новая строка
-- покупки хранит:
--
--   * sizing_rule         — 'mm1' (правило включено) или 'fixed' (positions.notional_usd);
--   * sizing_equity_usd   — баланс учёта, от которого посчитана сумма (только при mm1);
--   * sizing_exposure_usd — вложено в эту монету ДО покупки (только при mm1).
--
-- Старые строки и все строки продаж остаются NULL. Миграция добавляет только три колонки
-- и одно ограничение в demo_orders; другие таблицы не затрагиваются. Права роли
-- agenttrade_ro на demo_orders выданы на таблицу целиком (миграция 030) — новые колонки
-- она читает без отдельной выдачи.
--
-- Миграция идемпотентна. Откат — 031_demo_sizing_rollback.sql.
--
-- Ручное применение (каталог db/migrations в контейнер postgres НЕ смонтирован — там только
-- db/init.sql, см. docker-compose.yml; файл подаётся с хоста через stdin):
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/031_demo_sizing.sql

BEGIN;

ALTER TABLE demo_orders
    ADD COLUMN IF NOT EXISTS sizing_rule            TEXT,
    ADD COLUMN IF NOT EXISTS sizing_equity_usd      NUMERIC(14,6),
    ADD COLUMN IF NOT EXISTS sizing_exposure_usd    NUMERIC(14,6);

-- ADD CONSTRAINT не имеет IF NOT EXISTS: сначала снимаем, чтобы повторное применение прошло.
ALTER TABLE demo_orders DROP CONSTRAINT IF EXISTS demo_orders_sizing_rule_chk;
ALTER TABLE demo_orders
    ADD CONSTRAINT demo_orders_sizing_rule_chk
    CHECK (sizing_rule IS NULL OR sizing_rule IN ('fixed', 'mm1'));

COMMIT;
