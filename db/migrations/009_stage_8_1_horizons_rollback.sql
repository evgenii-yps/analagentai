-- ОТКАТ Этапа 8.1 (§5, §6). Возвращает прежний ключ таблицы оценок.
--
-- ВНИМАНИЕ: строки оценок на горизонтах 12ч и 24ч при откате УДАЛЯЮТСЯ —
-- в прежней схеме им нет места (уникальность по текстовому горизонту их
-- допускает, но код версии 4 их не создавал и не читает). Строки горизонтов
-- 1ч и 4ч сохраняются.

BEGIN;

DELETE FROM signal_evaluations WHERE horizon_h NOT IN (1, 4);

DROP INDEX IF EXISTS ix_eval_horizon;
ALTER TABLE signal_evaluations DROP CONSTRAINT IF EXISTS signal_evaluations_pkey;
ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS horizon_h;

-- Прежний суррогатный ключ и уникальность по текстовому горизонту.
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE signal_evaluations ADD PRIMARY KEY (id);
ALTER TABLE signal_evaluations
    ADD CONSTRAINT signal_evaluations_signal_id_horizon_key UNIQUE (signal_id, horizon);

DROP TABLE IF EXISTS logic_version_windows;

COMMIT;
