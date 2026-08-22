-- ЭТАП 8.1. Четыре горизонта оценки (§5) и граница версии логики (§6).
--
-- Миграция ИДЕМПОТЕНТНА: повторное применение не ломает данные и не падает.
-- Откат — 009_stage_8_1_horizons_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/009_stage_8_1_horizons.sql

BEGIN;

-- --- §5. Горизонт оценки в ЧАСАХ -------------------------------------------
--
-- Было: одна оценка на сигнал по каждому текстовому горизонту ('1h','4h').
-- Стало: горизонт — целое число часов, ключ (signal_id, horizon_h).
ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS horizon_h SMALLINT;

-- Заполнение существующих записей. ТЗ 8.1 §5 предписывает horizon_h = 4 («так
-- они и считались»). Буквальное применение этого правила ко ВСЕМ строкам
-- сломало бы данные там, где оценка велась на 1 часе: строка с horizon='1h'
-- считалась именно на часе, и объявить её четырёхчасовой — значит подменить
-- факт (а два таких ряда ещё и столкнулись бы на новом первичном ключе).
-- Поэтому горизонт берётся ИЗ УЖЕ ЗАПИСАННОГО текста, и только неразбираемое
-- значение получает 4. Досчёта несуществующих горизонтов здесь нет: ни одной
-- НОВОЙ строки миграция не создаёт.
UPDATE signal_evaluations
   SET horizon_h = CASE
        WHEN horizon ~ '^[0-9]+h$' THEN (regexp_replace(horizon, 'h$', ''))::smallint
        WHEN horizon ~ '^[0-9]+$'  THEN horizon::smallint
        WHEN horizon ~ '^[0-9]+d$' THEN ((regexp_replace(horizon, 'd$', ''))::int * 24)::smallint
        ELSE 4::smallint
       END
 WHERE horizon_h IS NULL;

ALTER TABLE signal_evaluations ALTER COLUMN horizon_h SET NOT NULL;

-- Проверка ПЕРЕД сменой ключа: не оказалось ли у одного сигнала двух оценок на
-- один и тот же горизонт в часах. Такое возможно, если в колонке horizon
-- когда-то оказалось значение, которое не разбирается (тогда оно получает 4 по
-- правилу ТЗ и может столкнуться с настоящей строкой '4h').
--
-- Молча удалить лишнюю строку нельзя: это данные об оценке, и какая из двух
-- верна — решать не миграции. Молча упасть на ошибке уникального индекса тоже
-- нельзя: сообщение «could not create unique index» ничего не объясняет
-- дежурному. Поэтому миграция останавливается САМА и называет сигналы.
DO $$
DECLARE dups TEXT;
BEGIN
    SELECT string_agg(DISTINCT signal_id::text, ', ') INTO dups
      FROM (
          SELECT signal_id FROM signal_evaluations
           GROUP BY signal_id, horizon_h HAVING count(*) > 1
      ) q;
    IF dups IS NOT NULL THEN
        RAISE EXCEPTION
            'Миграция 009 остановлена: у сигналов % есть по две оценки на один '
            'горизонт (обычно из-за неразбираемого значения в колонке horizon). '
            'Разберите эти строки вручную и повторите миграцию: удалять данные '
            'об оценках миграция не имеет права.', dups;
    END IF;
END $$;

-- Старый суррогатный первичный ключ и старое ограничение уникальности по
-- текстовому горизонту больше не нужны: ключ записи — (сигнал, горизонт в часах).
ALTER TABLE signal_evaluations DROP CONSTRAINT IF EXISTS signal_evaluations_pkey;
ALTER TABLE signal_evaluations
    DROP CONSTRAINT IF EXISTS signal_evaluations_signal_id_horizon_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'signal_evaluations'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE signal_evaluations
            ADD PRIMARY KEY (signal_id, horizon_h);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_eval_horizon
    ON signal_evaluations (horizon_h, evaluated_at);

-- --- §6. Граница версии логики ---------------------------------------------
--
-- Данные версий 4 и 5 НИКОГДА не смешиваются в анализе, поэтому момент
-- перехода обязан быть машиночитаемым, а не восстанавливаться по памяти.
-- Запись делается ОДИН РАЗ при первом старте на версии (ON CONFLICT DO NOTHING),
-- поэтому перезапуск сервиса границу не сдвигает.
CREATE TABLE IF NOT EXISTS logic_version_windows (
    logic_version SMALLINT     PRIMARY KEY,
    started_at    TIMESTAMPTZ  NOT NULL,
    note          TEXT
);

-- Версия 4 началась 16.08.2026 16:25 UTC (живое окно Этапа 7.3) — значение
-- зафиксировано отчётом 7.4 §13.2 и переносится сюда, чтобы обе границы лежали
-- в одном месте.
INSERT INTO logic_version_windows (logic_version, started_at, note)
VALUES (4, TIMESTAMPTZ '2026-08-16 16:25:00+00',
        'Этап 7.3: перцентильный Futures, калибровка, инерция входов')
ON CONFLICT (logic_version) DO NOTHING;

COMMIT;
