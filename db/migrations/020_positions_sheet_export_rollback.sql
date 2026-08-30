-- ОТКАТ миграции 020 (Этап 9.1.2 §7).
--
-- ЧТО ТЕРЯЕТСЯ, и это надо знать ДО запуска: вместе с колонками исчезает
-- признак «уже записано в лист». Повторное применение 020 и следующий прогон
-- выгрузки создадут в листе ВТОРЫЕ строки для всех уже записанных сделок:
-- очередь снова станет полной, а лист об этом ничего не знает.
--
-- Откат уместен только вместе с ручной чисткой листа — либо когда в лист ещё
-- ничего не писалось (SHEETS_TRADES_ENABLED=false, как и задано по умолчанию).
--
-- Таблица positions и все её данные откатом НЕ ЗАТРАГИВАЮТСЯ: миграции 018 и
-- 019 отдельные и своей силы не теряют.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/020_positions_sheet_export_rollback.sql

BEGIN;

DROP INDEX IF EXISTS positions_sheet_pending_idx;

ALTER TABLE positions DROP COLUMN IF EXISTS sheet_closed_at;
ALTER TABLE positions DROP COLUMN IF EXISTS sheet_opened_at;

COMMIT;
