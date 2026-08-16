-- ЭТАП 7.3. ОБРАТНАЯ миграция к 007_calibration_inertia.sql.
--
-- Возвращает схему к состоянию Этапа 7.2. Идемпотентна (IF EXISTS везде).
--
-- ВНИМАНИЕ: удаление колонок необратимо уничтожает накопленные значения
-- calibrated_probability, inputs_hash и is_repeat, а удаление таблицы —
-- все построенные калибровочные кривые. Выполнять только вместе с откатом кода
-- (git reset --hard <коммит>) и только после резервной копии БД:
--   sudo -u agent /opt/agent-trade/scripts/backup_db.sh
--
-- Колонка probability НЕ трогается ни прямой, ни обратной миграцией.
--
-- Применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f - < db/migrations/007_calibration_inertia_rollback.sql

BEGIN;

DROP INDEX IF EXISTS idx_signals_inputs_hash;

ALTER TABLE signals
    DROP CONSTRAINT IF EXISTS signals_calibration_id_fkey;

ALTER TABLE signals
    DROP COLUMN IF EXISTS calibrated_probability,
    DROP COLUMN IF EXISTS calibration_id,
    DROP COLUMN IF EXISTS inputs_hash,
    DROP COLUMN IF EXISTS is_repeat;

DROP INDEX IF EXISTS idx_calibration_active;
DROP INDEX IF EXISTS idx_calibration_built;
DROP TABLE IF EXISTS calibration_curves;

COMMIT;
