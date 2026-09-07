-- Откат миграции 025 (Этап 9.2 §4, §5).
--
-- ЧТО ВОССТАНАВЛИВАЕТСЯ: обязательность stop_pct и stop_price, прежний перечень
-- причин выхода из пяти значений и прежний positions_bounds_chk; снимаются три
-- ограничения версии 6.
--
-- ОТКАТ ОТКАЗЫВАЕТСЯ РАБОТАТЬ, ЕСЛИ В ТАБЛИЦЕ УЖЕ ЕСТЬ ПОЗИЦИИ ВЕРСИИ 6, и это
-- главное в этом файле. Вернуть NOT NULL на stop_pct можно ровно двумя
-- способами: удалить такие строки или подставить в них выдуманный предел.
-- Первое — потеря факта, второе — данные, которые лгут; ни того, ни другого
-- откат делать не вправе, а сделать это МОЛЧА не вправе тем более. Поэтому он
-- падает с внятным текстом и не меняет НИ ОДНОЙ строки: решение, что делать с
-- позициями версии 6, принимает человек.
--
-- ЧТО ДЕЛАТЬ, ЕСЛИ ОТКАТ ОТКАЗАЛСЯ. Либо оставить миграцию применённой (версия
-- 6 работает, и ничего чинить не требуется), либо сначала разобраться с
-- позициями версии 6 — закрыть их, выгрузить и удалить осознанно, — и только
-- потом откатывать. Обходить проверку правкой этого файла нельзя: она и есть
-- смысл файла.
--
-- ПОЗИЦИИ ВЕРСИИ 5 ОТКАТ НЕ ТРОГАЕТ НИ ОДНОЙ СТРОКОЙ: у них stop_pct и
-- stop_price заполнены, и восстановленное NOT NULL для них ничего не меняет.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/025_positions_no_stop_rollback.sql

BEGIN;

DO $$
DECLARE
    v6_rows   BIGINT;
    plus_rows BIGINT;
BEGIN
    SELECT count(*) INTO v6_rows FROM positions
     WHERE stop_pct IS NULL OR stop_price IS NULL;
    SELECT count(*) INTO plus_rows FROM positions
     WHERE exit_reason = 'plus_exit';
    IF v6_rows > 0 OR plus_rows > 0 THEN
        RAISE EXCEPTION
            'откат 025 невозможен: в positions % строк без предела и % строк с '
            'исходом plus_exit. Вернуть NOT NULL можно только удалив их или '
            'подставив выдуманный предел — ни того, ни другого откат не делает. '
            'Разберитесь с позициями версии 6 вручную и повторите',
            v6_rows, plus_rows;
    END IF;
END $$;

ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_no_stop_chk;
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_v6_reason_chk;
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_plus_exit_chk;
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_exit_measured_chk;

-- Перечень причин выхода — тот, что оставила миграция 019 (пять значений).
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_reason_chk;
ALTER TABLE positions ADD CONSTRAINT positions_reason_chk
    CHECK (exit_reason IS NULL OR exit_reason IN
           ('target', 'stop', 'timeout', 'ambiguous', 'data_gap'));

-- Границы чисел — те, что оставила миграция 018.
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_bounds_chk;
ALTER TABLE positions ADD CONSTRAINT positions_bounds_chk
    CHECK (horizon_h > 0 AND entry_price > 0
           AND signal_price > 0 AND stop_pct > 0
           AND qty > 0 AND notional_usd > 0
           AND logic_version > 0);

ALTER TABLE positions ALTER COLUMN stop_pct   SET NOT NULL;
ALTER TABLE positions ALTER COLUMN stop_price SET NOT NULL;

COMMIT;
