-- ОТКАТ миграции 019 (Этап 9.1.1 §6): перечень причин выхода снова из четырёх
-- значений, без data_gap.
--
-- ОТКАТ ОТКАЗЫВАЕТСЯ ВЫПОЛНЯТЬСЯ, ЕСЛИ СТРОКИ С data_gap УЖЕ ЕСТЬ, и это не
-- перестраховка. Ограничение с четырьмя значениями на таблице, где пятое уже
-- записано, либо не создастся вовсе (и откат оборвётся на полпути, оставив
-- таблицу БЕЗ ограничения — то есть без закрытого перечня), либо потребовало бы
-- удалить эти строки. Удалять записи о закрытых позициях ради отката схемы
-- нельзя: это данные замера, а не служебная разметка.
--
-- Что делать, если откат нужен, а строки есть: сначала решить, что делать со
-- строками (перевести в другую причину руками или сохранить их отдельно), и
-- только потом откатывать. Решение это содержательное, и принимать его за
-- владельца скрипт не вправе.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/019_positions_data_gap_rollback.sql

BEGIN;

DO $$
DECLARE
    gap_rows BIGINT;
BEGIN
    SELECT count(*) INTO gap_rows
    FROM positions
    WHERE exit_reason = 'data_gap';

    IF gap_rows > 0 THEN
        RAISE EXCEPTION
            'Откат 019 НЕ ВЫПОЛНЕН: в positions % строк с exit_reason = '
            '''data_gap''. Ограничение из четырёх значений их не допустит, а '
            'удалять записи о закрытых позициях ради отката схемы нельзя — это '
            'данные замера. Решите, что делать с этими строками, и повторите.',
            gap_rows;
    END IF;

    ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_reason_chk;
    ALTER TABLE positions ADD CONSTRAINT positions_reason_chk
        CHECK (exit_reason IS NULL OR exit_reason IN
               ('target', 'stop', 'timeout', 'ambiguous'));
END $$;

COMMIT;
