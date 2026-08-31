-- Откат миграции 021 (Этап 9.1.3 §4).
--
-- ПРОВЕРКИ НА НЕПУСТОТУ ЗДЕСЬ НЕТ НАМЕРЕННО, в отличие от откатов 018 и 020.
-- Таблица position_trailing_shadow содержит ТОЛЬКО производные величины: она
-- целиком восстанавливается повторным прогоном
-- scripts/shadow_trailing_9_1_3.py --apply по тем же самым positions и ohlcv.
-- Ни одного факта, которого нет больше нигде, в ней не лежит.
--
-- positions этот откат НЕ ТРОГАЕТ: внешний ключ смотрит из удаляемой таблицы
-- наружу, поэтому DROP не задевает ни одной строки факта.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/021_position_trailing_shadow_rollback.sql

BEGIN;

DROP TABLE IF EXISTS position_trailing_shadow;

COMMIT;
