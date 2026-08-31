-- Откат миграции 022 (Этап 9.1.4 §4).
--
-- ПРОВЕРКИ НА НЕПУСТОТУ ЗДЕСЬ НЕТ НАМЕРЕННО, в отличие от откатов 018 и 020.
-- Таблица position_stop_shadow содержит ТОЛЬКО производные величины: она
-- целиком восстанавливается повторным прогоном
-- scripts/stop_counterfactual_9_1_4.py --apply по тем же самым positions,
-- signals и ohlcv. Ни одного факта, которого нет больше нигде, в ней не лежит.
--
-- positions, signals и ohlcv этот откат НЕ ТРОГАЕТ: внешний ключ смотрит из
-- удаляемой таблицы наружу, поэтому DROP не задевает ни одной строки факта.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/022_position_stop_shadow_rollback.sql

BEGIN;

DROP TABLE IF EXISTS position_stop_shadow;

COMMIT;
