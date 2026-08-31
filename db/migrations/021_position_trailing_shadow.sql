-- ЭТАП 9.1.3 §4. Теневой подвижный выход на ФАКТИЧЕСКИХ виртуальных позициях.
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Этап 8.10 измерил подвижный выход на десятках тысяч
-- ГИПОТЕТИЧЕСКИХ пар «сигнал × горизонт». Здесь лежит другой замер: что дал бы
-- подвижный выход на тех сделках, которые система ДЕЙСТВИТЕЛЬНО открыла — с их
-- фактической ценой входа, их слотом и их сроком. Это разные вопросы, и числа
-- их не складываются: у 8.10 порога вероятности нет вовсе, здесь он 0.8.
--
-- ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ. Ни одно решение системы от таблицы не зависит:
-- LOGIC_VERSION остаётся 5, сервисы decision, notify, evaluator, risk и
-- positions о ней не знают, выбор «лучшего» варианта ЗАПРЕЩЁН прямо (§0 ТЗ).
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА (§6 ТЗ): positions НЕ ИЗМЕНЯЕТСЯ НИ ОДНОЙ КОЛОНКОЙ.
-- Внешний ключ смотрит ИЗ новой таблицы наружу, а не наоборот. Теневой
-- результат не имеет права попасть туда, где лежит факт: строка positions —
-- это то, что случилось, а строка отсюда — то, что случилось бы.
--
-- ON DELETE CASCADE стоит намеренно: содержимое таблицы ПОЛНОСТЬЮ производно от
-- positions и восстановимо повторным прогоном. Осиротевшая теневая строка после
-- удаления позиции была бы числом ни о чём.
--
-- ТРИ РАСХОЖДЕНИЯ СО СХЕМОЙ §4 ТЗ, И КАЖДОЕ — ОШИБКА ТЗ, А НЕ ВОЛЬНОСТЬ.
-- Названы здесь и в отчёте этапа; молча подогнать код под неверную схему
-- значило бы получить таблицу, в которую часть исходов просто не влезает.
--
--  1. ПРИЧИНА ВЫХОДА ПОДВИЖНОГО ВАРИАНТА В КОДЕ НАЗЫВАЕТСЯ 'trail', А НЕ
--     'trailing'. Так её пишет src/trailing/rule.py (EXIT_TRAIL) с Этапа 8.10,
--     и так она лежит в trailing_outcomes (ограничение
--     trailing_outcomes_exit_reason_chk). Принять здесь 'trailing' значило бы
--     завести ВТОРОЙ словарь для одного и того же механизма и переводить между
--     ними при каждой записи — то самое «два места, знающих одно и то же»,
--     которое в этом проекте уже расходилось.
--
--  2. В ПЕРЕЧНЕ §4 ТЗ НЕТ 'no_data', А ПРАВИЛО ЕГО ВОЗВРАЩАЕТ. Ряд свечей
--     позиции может оказаться неполным (contiguous_prefix обрезает окно по
--     первому разрыву), и тогда исход подвижного варианта НЕ ИЗМЕРЕН. Выбросить
--     такие строки значило бы потерять из виду сам факт неполноты; назвать их
--     timeout — выдать неизмеренное за измеренное.
--
--  3. exit_bar_ts, exit_price, net_pnl_pct И net_pnl_usd НЕ МОГУТ БЫТЬ
--     NOT NULL. У исходов 'ambiguous' и 'no_data' подвижного варианта бара
--     выхода не существует: в первом случае порядок событий внутри минуты
--     неизвестен, во втором ряда нет вовсе. Требование NOT NULL заставило бы
--     подставить туда выдуманное число, неотличимое от измеренного. Нужная
--     форма записана ограничением position_trailing_shadow_shape_chk — ровно
--     тем же приёмом, что в миграции 017 (trailing_outcomes_shape_chk).
--
--     У КОНТРОЛЬНОГО ВАРИАНТА ПОЛЯ ЗАПОЛНЕНЫ ВСЕГДА, включая 'ambiguous':
--     живое правило позиций (src/positions/rules.check_exit) при одновременном
--     касании выходит ПО ПРЕДЕЛУ — пессимистично и с настоящей ценой. Поэтому
--     форма зависит от варианта, а не только от причины.
--
-- Миграция идемпотентна. Откат — 021_position_trailing_shadow_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/021_position_trailing_shadow.sql

BEGIN;

CREATE TABLE IF NOT EXISTS position_trailing_shadow (
    position_id      BIGINT       NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    -- 'control' либо 'A0.25_R0.20' (два знака после точки, сперва A, затем R).
    -- Строка, а не пара чисел: у контрольного варианта параметров нет вовсе, а
    -- NULL в первичном ключе PostgreSQL не допускает.
    variant          TEXT         NOT NULL,
    -- Параметры варианта продублированы числами, чтобы по ним можно было
    -- группировать запросом, не разбирая строку variant.
    activation_frac  NUMERIC(4,2),
    pullback_frac    NUMERIC(4,2),
    -- ГЛАВНОЕ ЧИСЛО ЭТАПА (§3.4 ТЗ). Подвижная цель поднимает пол под уже
    -- полученной прибылью; сделка, ушедшая против сигнала, до неё не доживает.
    -- Поэтому «сколько позиций механизм вообще задел» отвечает на вопрос
    -- владельца прямее, чем любое среднее.
    armed            BOOLEAN      NOT NULL,
    armed_at         TIMESTAMPTZ,
    exit_reason      TEXT         NOT NULL,
    exit_bar_ts      TIMESTAMPTZ,
    exit_price       NUMERIC(20,8),
    net_pnl_pct      NUMERIC(12,6),
    -- Считается от positions.notional_usd ФАКТИЧЕСКОЙ позиции, а не от
    -- константы: слот мог отличаться, и подставленная константа превратила бы
    -- замер в оценку.
    net_pnl_usd      NUMERIC(12,6),
    bars_used        INTEGER      NOT NULL,
    resolution       TEXT         NOT NULL,
    logic_version    INTEGER      NOT NULL,
    computed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT position_trailing_shadow_pk PRIMARY KEY (position_id, variant)
);

CREATE INDEX IF NOT EXISTS position_trailing_shadow_variant_idx
    ON position_trailing_shadow (variant);

-- Ограничения заводятся ОТДЕЛЬНО: CREATE TABLE IF NOT EXISTS существующую
-- таблицу не меняет, и на томе, где таблица уже создана, они иначе не
-- появились бы никогда (тот же приём, что в миграциях 017 и 018).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_reason_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_reason_chk
            CHECK (exit_reason IN ('target', 'stop', 'timeout', 'ambiguous',
                                   'trail', 'no_data'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_res_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_res_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    -- Задетость и момент задетости идут только вместе: armed без момента — это
    -- утверждение без опоры, момент без armed — момент неслучившегося.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_armed_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_armed_chk
            CHECK ((armed = false AND armed_at IS NULL)
                OR (armed = true  AND armed_at IS NOT NULL));
    END IF;

    -- Параметры есть у подвижных вариантов и отсутствуют у контрольного.
    -- Контрольный с заполненными долями означал бы, что он посчитан не живым
    -- правилом позиций, и всё сравнение недействительно.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_variant_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_variant_chk
            CHECK (
                CASE WHEN variant = 'control'
                     THEN activation_frac IS NULL AND pullback_frac IS NULL
                          AND armed = false
                     ELSE activation_frac IS NOT NULL AND pullback_frac IS NOT NULL
                          AND activation_frac > 0 AND pullback_frac > 0
                END
            );
    END IF;

    -- ФОРМА ЗАПИСИ (см. расхождение 3 в заголовке). Неизмеренный исход обязан
    -- выглядеть неизмеренным, а не нулём.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_shape_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_shape_chk
            CHECK (
                CASE
                    WHEN variant = 'control' THEN
                        exit_bar_ts IS NOT NULL AND exit_price IS NOT NULL
                        AND net_pnl_pct IS NOT NULL AND net_pnl_usd IS NOT NULL
                    WHEN exit_reason IN ('ambiguous', 'no_data') THEN
                        exit_bar_ts IS NULL AND exit_price IS NULL
                        AND net_pnl_pct IS NULL AND net_pnl_usd IS NULL
                    ELSE
                        exit_bar_ts IS NOT NULL AND exit_price IS NOT NULL
                        AND net_pnl_pct IS NOT NULL AND net_pnl_usd IS NOT NULL
                END
            );
    END IF;

    -- ДВА СЛЕДСТВИЯ ПРАВИЛА, ЗАПИСАННЫЕ ОГРАНИЧЕНИЕМ, А НЕ ОБЕЩАНИЕМ. Те же
    -- два, что в миграции 017 (trailing_outcomes_reason_variant_chk):
    --  1. подвижный выход не может сработать там, где его нет;
    --  2. цель не может закрыть сделку у подвижного варианта — уровень
    --     включения лежит не дальше цели, поэтому цена, дошедшая до цели, уже
    --     включила подвижный выход.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_reason_variant_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_reason_variant_chk
            CHECK (
                (exit_reason <> 'trail'  OR variant <> 'control')
                AND
                (exit_reason <> 'target' OR variant =  'control')
            );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_trailing_shadow_bounds_chk') THEN
        ALTER TABLE position_trailing_shadow
            ADD CONSTRAINT position_trailing_shadow_bounds_chk
            CHECK (bars_used >= 0 AND logic_version > 0
                   AND (exit_price IS NULL OR exit_price > 0));
    END IF;
END $$;

-- Право на чтение — той же роли, что и у остальных таблиц замеров, если она
-- заведена. Отсутствие роли не ошибка: на части машин её нет.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON position_trailing_shadow TO agenttrade_ro;
    END IF;
END $$;

COMMIT;
