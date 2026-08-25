-- Откат миграции 012.
--
-- ВНИМАНИЕ: откат снимает ограничение и индекс, но НЕ возвращает подстановку
-- минимальной известной версии вместо признака «неизвестно». Возвращать её
-- нечем и незачем: строки с logic_version = 0 говорят правду, а прежнее
-- значение было ложным. Если версия 0 мешает стороннему отбору — чините отбор.

BEGIN;

DROP INDEX IF EXISTS ix_agent_outputs_ts;

ALTER TABLE logic_version_windows
    DROP CONSTRAINT IF EXISTS logic_version_windows_version_positive;

COMMENT ON COLUMN agent_outputs_daily.logic_version IS NULL;

COMMIT;
