-- Откат миграции 031 (этап 9.6): снимает ограничение и три колонки учёта размера покупки.
--
-- ЧТО ТЕРЯЕТСЯ: значения sizing_rule / sizing_equity_usd / sizing_exposure_usd в уже
-- записанных строках demo_orders. Сами ордера, суммы покупок и учёт капитала не меняются.
-- Перед откатом стоит снять итог, если он нужен:
--   SELECT sizing_rule, skip_reason, count(*) FROM demo_orders
--    WHERE leg = 'buy' GROUP BY 1, 2 ORDER BY 1, 2;
-- Сначала остановите сервис demo или выключите DEMO_MM_ENABLED и перезапустите его: код
-- этапа 9.6 пишет эти колонки в каждой новой строке покупки и без них упадёт.
--
-- Ручное применение:
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/031_demo_sizing_rollback.sql

BEGIN;

ALTER TABLE demo_orders DROP CONSTRAINT IF EXISTS demo_orders_sizing_rule_chk;
ALTER TABLE demo_orders
    DROP COLUMN IF EXISTS sizing_exposure_usd,
    DROP COLUMN IF EXISTS sizing_equity_usd,
    DROP COLUMN IF EXISTS sizing_rule;

COMMIT;
