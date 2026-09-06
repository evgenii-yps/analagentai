-- Откат миграции 024 (Этап 9.1.6 §7).
--
-- ПРОВЕРКИ НА НЕПУСТОТУ ЗДЕСЬ НЕТ НАМЕРЕННО, как и в откате 022. Таблица
-- position_plus_exit_shadow содержит ТОЛЬКО производные величины: она целиком
-- восстанавливается повторным прогоном
-- scripts/plus_exit_counterfactual_9_1_6.py --apply по тем же самым positions и
-- ohlcv. Ни одного факта, которого нет больше нигде, в ней не лежит.
--
-- positions, signals, ohlcv и signal_targets этот откат НЕ ТРОГАЕТ: внешний
-- ключ смотрит из удаляемой таблицы наружу, поэтому DROP не задевает ни одной
-- строки факта.
--
-- ОТКАТ ОБЯЗАН ПРОХОДИТЬ НА НЕПУСТОЙ ТАБЛИЦЕ И ПОЗВОЛЯТЬ ПРИМЕНИТЬ МИГРАЦИЮ
-- ЗАНОВО (§7.4 ТЗ) — проверено на настоящем PostgreSQL 16.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/024_position_plus_exit_shadow_rollback.sql

BEGIN;

DROP TABLE IF EXISTS position_plus_exit_shadow;

COMMIT;
