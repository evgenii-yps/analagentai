-- ЗАМЕР 0 (шаг 1 версии 7). Хранение свечей СТАРШИХ МАСШТАБОВ (1Dutc/1Wutc/1Mutc).
--
-- ЭТА МИГРАЦИЯ НЕ СОЗДАЁТ ТАБЛИЦЫ backtest.candles_htf. ЭТО ОСОЗНАННОЕ
-- ОТКЛОНЕНИЕ ОТ §6 ТЗ, И ВОТ ПОЧЕМУ.
--
-- §6 ТЗ прямо предписывает: «Имена сверить с фактической схемой
-- (db/migrations/008_backtest_schema.sql). Если существующая таблица свечей уже
-- допускает указание масштаба — использовать её и доложить об этом решении
-- вместо создания новой». Сверка выполнена, и условие ВЫПОЛНЯЕТСЯ:
--
--   backtest.candles (миграция 008): inst_id, bar, open_time, close_time,
--   open, high, low, close, volume, volume_ccy, fetched_at,
--   PRIMARY KEY (inst_id, bar, open_time).
--
-- Колонка ``bar`` в ключе уже есть, точность ``NUMERIC(20,8)`` — та самая, что
-- требует §6, а ``fetched_at`` покрывает ``loaded_at`` из предложенного DDL.
--
-- ЧИТАТЕЛИ ПРОВЕРЕНЫ ПОИМЁННО, А НЕ «ПО ПАМЯТИ». Добавление строк со старшими
-- масштабами в общую таблицу опасно ровно одним: запросом, который читает
-- свечи БЕЗ фильтра по ``bar`` — такой запрос молча начал бы смешивать дневные
-- бары с часовыми. Проверены все восемь мест, читающих backtest.candles:
--
--   src/core/db.py:766        WHERE inst_id=$1 AND bar=$2 …   (ПРОДАКШН, risk)
--   backtest/clock.py:106     WHERE inst_id=$1 AND bar=$2 …
--   backtest/integrity.py:60  WHERE inst_id=$1 AND bar=$2 …
--   backtest/evaluate.py:129  WHERE inst_id=$1 AND bar=$2 …
--   backtest/evaluate.py:146  WHERE inst_id=$1 AND bar=$2 …
--   backtest/loader.py:384    WHERE inst_id=$1 AND bar=$2 …
--   scripts/backfill_8_2.py:60 WHERE inst_id=$1 AND bar=$2 …
--   deploy/verify_8_2.sh:283  WHERE bar='${bar}' …
--
-- Фильтр по масштабу есть ВЕЗДЕ, в том числе на единственном продакшн-пути
-- (src/risk/runner.py читает часовой ряд через get_backtest_candles с bar='1H').
-- Ни одно решение системы от появления строк с bar='1Dutc' не меняется.
--
-- ЧТО ОТДЕЛЬНАЯ ТАБЛИЦА ПОТЕРЯЛА БЫ. В предложенном §6 DDL нет ``close_time``.
-- А §8 требует проверять, что «конец периода не позже текущего момента минус
-- запас» — то есть КОНЕЦ бара. Без хранимого ``close_time`` его пришлось бы
-- каждый раз выводить из ``bar`` и ``ts`` календарной арифметикой, то есть
-- завести вторую копию формулы длины месяца — ровно то, против чего
-- предостерегает сам §8.1 («запас берётся существующей settle_seconds, а не
-- копией формулы»). Здесь конец периода ХРАНИТСЯ и сверяется с началом
-- следующего бара при загрузке.
--
-- ЧТО ДОБАВЛЯЕТСЯ ЗДЕСЬ, И ПОЧЕМУ ИМЕННО В ТАКОМ ВИДЕ:
--
--   1. Колонка ``source`` — происхождение бара. §6 принял решение «старшие ряды
--      загружаются С БИРЖИ, а не собираются из часовых», а сборка из часовых
--      остаётся КОНТРОЛЕМ. Различить две стороны контроля через год можно будет
--      только по записанному происхождению. Колонка NULLABLE: у уже
--      загруженных часовых строк происхождения не записано, и выдумывать его
--      задним числом нельзя.
--
--   2. Ограничения целостности §6 (high >= low, open/close внутри диапазона,
--      белый список масштабов) — ОБЛАСТЬЮ ТОЛЬКО СТАРШИЕ БАРЫ. Ограничение на
--      всю таблицу проверялось бы при применении на уже лежащих миллионах
--      часовых строк и могло бы не примениться вовсе, а сверх того начало бы
--      отбивать вставки часового загрузчика — то есть меняло бы поведение кода,
--      которого этот этап не касается.
--
-- ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ: проверки кратности метки суткам UTC (§3, ловушка
-- 1D против 1Dutc). Выражение ``extract(epoch from open_time)`` над timestamptz
-- в PostgreSQL помечено STABLE, а не IMMUTABLE, и в CHECK не принимается.
-- Кратность проверяется в коде — backtest/htf.py:assert_bar_alignment — и
-- закреплена тестом tests/test_zamer_0.py (контрольный опыт: подмена 1Dutc
-- на 1D роняет тест).
--
-- Миграция ИДЕМПОТЕНТНА: повторное применение ничего не меняет.
-- Откат — 026_backtest_candles_htf_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/026_backtest_candles_htf.sql

BEGIN;

ALTER TABLE backtest.candles ADD COLUMN IF NOT EXISTS source TEXT;

COMMENT ON COLUMN backtest.candles.source IS
    'Происхождение бара: okx — загружен с биржи; NULL — записан до Замера 0. '
    'Сборка из часовых в таблицу НЕ пишется: она только контроль (§6, §7).';

-- Белый список масштабов, оканчивающихся на utc. Ловит опечатку вида «1Yutc»
-- и, что важнее, попытку записать бар без суффикса рядом с ними. Строки с
-- любым другим ``bar`` (в том числе всеми часовыми) ограничением не
-- затрагиваются вовсе.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_htf_bar'
    ) THEN
        ALTER TABLE backtest.candles ADD CONSTRAINT candles_htf_bar CHECK (
            bar NOT LIKE '%utc' OR bar IN ('1Dutc','1Wutc','1Mutc')
        );
    END IF;
END $$;

-- Форма бара. Область — только старшие масштабы: см. пункт 2 заголовка.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_htf_ohlc'
    ) THEN
        ALTER TABLE backtest.candles ADD CONSTRAINT candles_htf_ohlc CHECK (
            bar NOT IN ('1Dutc','1Wutc','1Mutc')
            OR (high >= low
                AND open BETWEEN low AND high
                AND close BETWEEN low AND high)
        );
    END IF;
END $$;

-- Конец периода обязан быть позже начала. Тоже только для старших баров:
-- у часовых строк это верно по построению, но проверять их этап не брался.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'candles_htf_period'
    ) THEN
        ALTER TABLE backtest.candles ADD CONSTRAINT candles_htf_period CHECK (
            bar NOT IN ('1Dutc','1Wutc','1Mutc') OR close_time > open_time
        );
    END IF;
END $$;

-- Чтение старших рядов идёт по (inst_id, bar) с порядком по времени — это же
-- порядок первичного ключа, отдельного индекса не требуется. Индекс заводится
-- только под выборку «все инструменты одного масштаба», которой пользуется
-- проверка §8: она обходит ВСЕ сохранённые старшие бары разом.
CREATE INDEX IF NOT EXISTS ix_bt_candles_htf_close
    ON backtest.candles (bar, close_time)
    WHERE bar IN ('1Dutc','1Wutc','1Mutc');

COMMIT;
