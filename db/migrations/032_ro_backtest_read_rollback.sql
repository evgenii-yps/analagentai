-- Откат миграции 032 (этап 9.10): отзывает у роли agenttrade_ro чтение backtest.candles.
--
-- ЧТО ТЕРЯЕТСЯ: только возможность запускать замер analysis/run_9_10.sh — он без этого права
-- не прочтёт историю свечей. Сами данные, схема backtest и остальные права роли не меняются.
--
-- Ручное применение:
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/032_ro_backtest_read_rollback.sql

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        REVOKE SELECT ON backtest.candles FROM agenttrade_ro;
        REVOKE USAGE ON SCHEMA backtest FROM agenttrade_ro;
    END IF;
END $$;

COMMIT;
