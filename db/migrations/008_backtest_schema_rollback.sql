-- ЭТАП 7.4. ОБРАТНАЯ миграция к 008_backtest_schema.sql.
--
-- Удаляет схему реплея целиком. Продакшн-схема public НЕ затрагивается:
-- ни одна таблица, колонка или строка вне backtest этой командой не меняется.
--
-- Применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/008_backtest_schema_rollback.sql

BEGIN;

DROP SCHEMA IF EXISTS backtest CASCADE;

COMMIT;
