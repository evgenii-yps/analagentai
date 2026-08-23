-- ЭТАП 8.1. Признак «версия логики неизвестна» и исправление уже записанного.
--
-- ЧТО БЫЛО СЛОМАНО. Суточная свёртка восстанавливала logic_version по окнам
-- logic_version_windows, а выводам РАНЬШЕ самой ранней записанной границы
-- подставляла минимальную известную версию. На сервере 22.08.2026 это дало
-- 33 895 выводов версий 1-3, записанных как версия 4: 42 строки в вечной
-- таблице agent_outputs_daily за 2026-08-08 … 2026-08-21 при единственной
-- известной границе версии 4 от 2026-08-16 16:25 UTC.
--
-- ПОЧЕМУ ЭТО НЕ МЕЛОЧЬ. agent_outputs_daily не удаляется никогда, а сырьё
-- agent_outputs живёт 90 суток. После удаления сырья проверить утверждение
-- стало бы нечем, и в проекте навсегда осталась бы правдоподобная ложь.
-- Подстановка ближайшей версии — это подстановка суррогатных данных вместо
-- честного «неизвестно», и она нарушает основное правило проекта: версии
-- логики НИКОГДА не смешиваются в анализе.
--
-- ЧТО ДЕЛАЕТ МИГРАЦИЯ:
--   1. Запрещает нулевую версию в logic_version_windows. С этого момента
--      logic_version = 0 не может означать реальную версию НИКОГДА — признак
--      «неизвестно» отличим от любой версии по построению, а не по соглашению.
--   2. Переводит в «неизвестно» суточные итоги за сутки, целиком лежащие
--      раньше самой ранней границы. Сырьё для этого не нужно: вся суточная
--      строка относится к неизвестному периоду целиком.
--   3. Удаляет итоги за сутки ГРАНИЦЫ, если они записаны одной строкой вместо
--      двух. Пересчитает их ближайшая свёртка (scripts/retention.py):
--      она начинает счёт с самых ранних суток, по которым сырьё есть, а итогов
--      нет. Второй реализации расчёта в проекте нет и быть не должно.
--   4. Добавляет индекс по agent_outputs (ts): по нему идёт и поиск дыры в
--      итогах, и удаление журнала старше 90 суток — до сих пор оба шли
--      последовательным чтением всей таблицы.
--
-- ГРАНИЦЫ ПРИМЕНИМОСТИ. Шаг 3 требует сырья за сутки границы. Если сырьё уже
-- удалено, миграция строки НЕ ТРОГАЕТ и печатает предупреждение: удалить их
-- значило бы потерять данные безвозвратно, а восстановить верное разделение
-- уже невозможно. Такие строки перечисляются поимённо — решение по ним
-- принимает человек.
--
-- Миграция ИДЕМПОТЕНТНА: повторный запуск ничего не меняет.
-- Откат — 012_unknown_logic_version_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/012_unknown_logic_version.sql

BEGIN;

-- 1. Ноль не может быть реальной версией -------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM logic_version_windows WHERE logic_version <= 0) THEN
        RAISE EXCEPTION
            'в logic_version_windows есть версия <= 0 — ноль зарезервирован '
            'под признак «версия неизвестна», исправьте данные вручную';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'logic_version_windows'::regclass
           AND conname = 'logic_version_windows_version_positive'
    ) THEN
        ALTER TABLE logic_version_windows
            ADD CONSTRAINT logic_version_windows_version_positive
            CHECK (logic_version > 0);
    END IF;
END $$;

COMMENT ON COLUMN agent_outputs_daily.logic_version IS
    '0 — версия логики НЕИЗВЕСТНА (вывод сделан раньше самой ранней записанной '
    'границы версий). Ближайшая известная версия не подставляется: это была бы '
    'ложная запись в таблице, которая не удаляется никогда. Реальные версии '
    'строго положительны (ограничение на logic_version_windows).';

-- 2. Сутки целиком раньше самой ранней границы → «неизвестно» -----------------
-- Сырьё не требуется: вся строка относится к неизвестному периоду целиком.
UPDATE agent_outputs_daily d
   SET logic_version = 0
 WHERE d.logic_version > 0
   AND d.day < (SELECT min(started_at)::date FROM logic_version_windows);

-- 3. Сутки, слитые через границу: разделить на две строки ---------------------
-- Датой такие сутки не выявляются: строка за сутки границы лежит НЕ раньше
-- начала своей версии, а внутри них. Признак другой: строка помечена версией,
-- а среди её выводов есть сделанные РАНЬШЕ начала этой версии — значит, в ней
-- смешаны два периода.
--
-- Условие «нет строки с меньшей версией по той же группе» делает шаг
-- идемпотентным: после верного пересчёта ранняя часть суток лежит отдельной
-- строкой (0 или меньшая версия), и удалять становится нечего.
--
-- Удаляются ВСЕ строки таких суток, а не только смешанные: свёртка
-- пересчитывает сутки целиком, а начинает счёт с суток, по которым итогов нет
-- вовсе. Значения остальных строк от пересчёта не меняются.
DELETE FROM agent_outputs_daily d
 WHERE EXISTS (
           SELECT 1
             FROM agent_outputs_daily w
             JOIN logic_version_windows v ON v.logic_version = w.logic_version
            WHERE w.day = d.day
              AND w.logic_version > 0
              AND EXISTS (
                      SELECT 1 FROM agent_outputs a
                       WHERE a.agent = w.agent
                         AND a.instrument_id = w.instrument_id
                         AND a.ts >= w.day::timestamptz
                         AND a.ts < w.day::timestamptz + interval '1 day'
                         AND a.ts < v.started_at
                  )
              AND NOT EXISTS (
                      SELECT 1 FROM agent_outputs_daily z
                       WHERE z.day = w.day AND z.agent = w.agent
                         AND z.instrument_id = w.instrument_id
                         AND z.logic_version < w.logic_version
                  )
       )
   -- Пересчитать можно только при живом сырье. Нет сырья — не удаляем.
   AND EXISTS (
           SELECT 1 FROM agent_outputs a
            WHERE a.ts >= d.day::timestamptz
              AND a.ts < d.day::timestamptz + interval '1 day'
       );

-- Строки суток границы, которые исправить уже нельзя: сырьё удалено.
DO $$
DECLARE
    boundary DATE := (SELECT min(started_at)::date FROM logic_version_windows);
    stuck INTEGER;
BEGIN
    IF boundary IS NULL THEN
        RAISE NOTICE 'logic_version_windows пуста — исправлять нечего';
        RETURN;
    END IF;
    SELECT count(*) INTO stuck
      FROM agent_outputs_daily d
     WHERE d.day <= boundary
       AND d.logic_version > 0
       AND NOT EXISTS (
               SELECT 1 FROM agent_outputs a
                WHERE a.ts >= d.day::timestamptz
                  AND a.ts < d.day::timestamptz + interval '1 day'
           );
    IF stuck > 0 THEN
        RAISE WARNING
            'сутки до границы % включительно: % строк проверить нечем — сырьё '
            'за эти сутки удалено. Строки НЕ УДАЛЕНЫ: потерять их хуже, чем '
            'оставить неточными. Отметьте это в отчёте.',
            boundary, stuck;
    END IF;
END $$;

-- 4. Индекс по времени журнала выводов ----------------------------------------
-- Нужен и поиску дыры в итогах, и правилу удаления «старше 90 суток»:
-- существующий idx_agent_outputs (agent, instrument_id, ts DESC) отбор
-- только по ts не покрывает.
CREATE INDEX IF NOT EXISTS ix_agent_outputs_ts ON agent_outputs (ts);

COMMIT;
