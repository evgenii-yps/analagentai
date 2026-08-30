-- ОТКАТ миграции 019 (Этап 9.1.1 §7.6).
--
-- ЧТО ТЕРЯЕТСЯ ПРИ ОТКАТЕ, и это надо знать до его запуска: вместе с колонкой
-- исчезает признак «строка уже записана в лист». Повторное применение 019 и
-- повторный прогон выгрузки запишут в лист ВТОРЫЕ строки тех же сделок, и
-- цепочка объёмов удвоит их прибыль. Откат уместен только вместе с ручной
-- чисткой листа — либо когда в лист ещё ничего не писалось
-- (POSITIONS_SHEETS_ENABLED=false, как и задано по умолчанию).
--
-- Таблица positions и все её данные откатом НЕ ЗАТРАГИВАЮТСЯ: миграция 018
-- отдельная и своей силы не теряет.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/019_positions_sheet_export_rollback.sql

BEGIN;

DROP INDEX IF EXISTS ix_positions_sheet_pending;

ALTER TABLE positions DROP COLUMN IF EXISTS sheet_exported_at;

COMMIT;
