-- ЭТАП 7.3. Миграция: калиброванная вероятность (Блок B) и учёт инерции
-- входов (Блок C).
--
-- Идемпотентна: повторное применение безопасно (IF NOT EXISTS везде, внешний
-- ключ проверяется по системному каталогу). Существующие данные НЕ переписываются:
-- у старых сигналов calibrated_probability и inputs_hash остаются NULL, а
-- is_repeat = false. Пересчитывать их задним числом запрещено (ТЗ §10.5).
--
-- Колонка probability СОХРАНЯЕТСЯ и продолжает хранить индекс согласия:
-- переименование сломало бы выгрузку, бота и суточную сводку (ТЗ §10.4).
--
-- Применяется автоматически при старте сервисов decision/notify
-- (db.ensure_calibration_schema / db.ensure_signals_inertia). Этот файл нужен
-- для ручного применения и как читаемая запись изменения схемы.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       -f - < db/migrations/007_calibration_inertia.sql
-- Откат: db/migrations/007_calibration_inertia_rollback.sql

BEGIN;

-- --- Блок B: калибровка -----------------------------------------------------

CREATE TABLE IF NOT EXISTS calibration_curves (
    id              BIGSERIAL PRIMARY KEY,
    logic_version   SMALLINT    NOT NULL,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    sample_size     INTEGER     NOT NULL,
    window_from     TIMESTAMPTZ NOT NULL,
    window_to       TIMESTAMPTZ NOT NULL,
    base_rate       DOUBLE PRECISION NOT NULL,
    bins            JSONB       NOT NULL,
    is_active       BOOLEAN     NOT NULL DEFAULT FALSE,
    notes           TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_active
    ON calibration_curves (logic_version) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_calibration_built
    ON calibration_curves (logic_version, built_at DESC);

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS calibrated_probability DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS calibration_id bigint;

-- ADD CONSTRAINT не поддерживает IF NOT EXISTS — проверяем по каталогу.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signals_calibration_id_fkey'
    ) THEN
        ALTER TABLE signals
            ADD CONSTRAINT signals_calibration_id_fkey
            FOREIGN KEY (calibration_id) REFERENCES calibration_curves(id);
    END IF;
END $$;

-- --- Блок C: учёт инерции входов -------------------------------------------

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS inputs_hash text,
    ADD COLUMN IF NOT EXISTS is_repeat boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_signals_inputs_hash
    ON signals (instrument_id, inputs_hash, ts DESC);

-- Роль только на чтение (бот) должна видеть новую таблицу.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON calibration_curves TO agenttrade_ro;
    END IF;
END $$;

COMMIT;
