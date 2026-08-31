-- ЭТАП 9.1.4 §4. Пересчёт исхода закрытых позиций при ДРУГИХ уровнях предела.
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Владелец спросил: «торговля спотовая, ликвидации нет,
-- портфель разнесён по пяти токенам — нужен ли нам вообще предел убытка?»
-- Рассуждение верно в том, что на споте позицию никто не закроет
-- принудительно. Но у предела есть вторая роль: ОН ОСВОБОЖДАЕТ СЛОТ. Слотов
-- пять, и повисшая в минусе позиция стоит не только своего убытка, но и всех
-- входов по этому инструменту за время её висения. Здесь лежит замер обеих
-- цен — в процентах и в слотах — на тех сделках, которые система
-- ДЕЙСТВИТЕЛЬНО открыла.
--
-- ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ. Ни одно решение системы от таблицы не зависит:
-- LOGIC_VERSION остаётся 5, BARRIER_STOP_PCT не трогается, сервисы decision,
-- notify, evaluator, risk и positions о ней не знают. Рекомендация «убрать
-- предел» или «поставить его на X%» этим этапом ЗАПРЕЩЕНА прямо (§1 ТЗ).
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА: positions НЕ ИЗМЕНЯЕТСЯ НИ ОДНОЙ КОЛОНКОЙ. Внешний
-- ключ смотрит ИЗ новой таблицы наружу, а не наоборот. Пересчётный результат
-- не имеет права попасть туда, где лежит факт: строка positions — это то, что
-- случилось, а строка отсюда — то, что случилось бы.
--
-- ON DELETE CASCADE стоит намеренно: содержимое таблицы ПОЛНОСТЬЮ производно от
-- positions, signals и ohlcv и восстановимо повторным прогоном. Осиротевшая
-- строка после удаления позиции была бы числом ни о чём.
--
-- ПОЧЕМУ ПОЛЯ ИСХОДА БЕЗ NOT NULL. Прямое требование §4 ТЗ, и оно верное:
-- у исхода 'no_data' бара выхода не существует вовсе — ряд свечей оборвался
-- раньше, чем правило смогло что-либо решить. Требование NOT NULL заставило бы
-- подставить туда выдуманное число, неотличимое от измеренного.
--
-- ТРИ РАСХОЖДЕНИЯ С ПЕРЕЧНЯМИ §4 ТЗ, И КАЖДОЕ СВЕРЕНО С КОДОМ ПРАВИЛА, а не с
-- перечнем в тексте задания (§4 ТЗ этого прямо и требует).
--
--  1. 'data_gap' В ПЕРЕЧЕНЬ НЕ ВХОДИТ, хотя правило выхода такую причину знает
--     (src/positions/rules.EXIT_DATA_GAP). Возвращает её НЕ check_exit, а
--     check_gap_exit, и позиции с ней в выборку замера не берутся вовсе (§2 ТЗ).
--     Отсутствие значения в ограничении — это проверяемое утверждение: строка с
--     'data_gap' здесь означала бы, что в замер попало то, что замером не
--     является.
--
--  2. 'no_data' В ПЕРЕЧНЕ §4 ТЗ НЕТ, А ОН НУЖЕН. check_exit возвращает None,
--     когда бар срока не предъявлен и ни один уровень не задет: неизвестно,
--     кончилось окно или данные не подъехали. Назвать такой случай 'timeout'
--     значило бы выдать неизмеренное за измеренное. Имя взято то же, что в
--     src/barrier/outcomes.OUTCOME_NO_DATA и в миграции 021, — второго словаря
--     для одного и того же состояния не заводится.
--
--  3. 'ambiguous' ЗДЕСЬ ИЗМЕРЕН, а не пуст — в отличие от миграции 021.
--     §4 ТЗ говорит «у исходов ambiguous и no_data бара выхода не существует»;
--     для ПОДВИЖНОГО правила Этапа 8.10 это так, а для правила позиций — нет.
--     check_exit при одновременном касании выходит ПО ПРЕДЕЛУ (пункт 3 §4.4
--     rules.py): бар выхода есть, цена есть, итог есть, и помечается такой
--     случай флагом outcome_certain в positions, а не пустотой. Поэтому пустая
--     форма записи здесь разрешена ТОЛЬКО для 'no_data'.
--
-- ЗАЧЕМ held_sec, extra_held_sec И blocked_signals ОБЪЯВЛЕНЫ NOT NULL, а исход
-- нет. Это ЦЕНА ВОПРОСА В СЛОТАХ (§3 ТЗ, ЧИСЛО 3) — вторая половина ответа
-- владельцу, и у неё другое устройство: при 'no_data' она не «неизвестна», а
-- НЕ ОПРЕДЕЛЕНА, потому что определять её не от чего. Ограничение
-- position_stop_shadow_gap_chk требует в этом случае ровно нули и тем самым
-- запрещает принять их за измерение: нуль здесь стоит рядом с exit_reason =
-- 'no_data', и читается только вместе с ним. Строки 'no_data' исключаются из
-- всех средних и сумм скриптом (см. scripts/stop_counterfactual_9_1_4.py).
--
-- Миграция идемпотентна. Откат — 022_position_stop_shadow_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/022_position_stop_shadow.sql

BEGIN;

CREATE TABLE IF NOT EXISTS position_stop_shadow (
    position_id      BIGINT       NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    -- 'control', 'stop_1.5', 'stop_2.0', 'stop_3.0' либо 'no_stop'. Строка, а
    -- не число: у 'no_stop' уровня нет вовсе, а NULL в первичном ключе
    -- PostgreSQL не допускает.
    variant          TEXT         NOT NULL,
    -- Уровень предела продублирован числом, чтобы по нему можно было
    -- группировать запросом, не разбирая строку variant. У 'no_stop' он NULL —
    -- и это не пропуск, а само содержание варианта.
    stop_pct         NUMERIC(10,6),
    exit_reason      TEXT         NOT NULL,
    exit_bar_ts      TIMESTAMPTZ,
    exit_price       NUMERIC(20,8),
    net_pnl_pct      NUMERIC(12,6),
    -- Считается от positions.notional_usd ФАКТИЧЕСКОЙ позиции, а не от
    -- константы: слот мог отличаться, и подставленная константа превратила бы
    -- замер в оценку.
    net_pnl_usd      NUMERIC(12,6),
    -- ЦЕНА ВОПРОСА В СЛОТАХ. held_sec — сколько слот был бы занят от открытия
    -- позиции до пересчётного закрытия; extra_held_sec — насколько это дольше
    -- ФАКТА (у control ноль по построению, и это сверяется);
    -- blocked_signals — сколько годных входов по тому же инструменту попало бы
    -- в окно дополнительного удержания.
    --
    -- ЗАБЛОКИРОВАННЫЕ ВХОДЫ ТОЛЬКО СЧИТАЮТСЯ, НО НЕ ОЦЕНИВАЮТСЯ (§3 ТЗ). Чтобы
    -- узнать их итог, пришлось бы проиграть целиком другую историю позиций, где
    -- каждый вход меняет занятость следующих. Это другой этап, и число здесь
    -- нельзя читать как «столько-то прибыли потеряно».
    held_sec         INTEGER      NOT NULL,
    extra_held_sec   INTEGER      NOT NULL,
    blocked_signals  INTEGER      NOT NULL,
    bars_used        INTEGER      NOT NULL,
    resolution       TEXT         NOT NULL,
    logic_version    INTEGER      NOT NULL,
    computed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT position_stop_shadow_pk PRIMARY KEY (position_id, variant)
);

CREATE INDEX IF NOT EXISTS position_stop_shadow_variant_idx
    ON position_stop_shadow (variant);

-- Ограничения заводятся ОТДЕЛЬНЫМ блоком с проверкой существования по имени:
-- CREATE TABLE IF NOT EXISTS существующую таблицу не меняет, и на томе, где
-- таблица уже создана, они иначе не появились бы никогда (тот же приём, что в
-- миграциях 017, 018 и 021).
DO $$
BEGIN
    -- ПЕРЕЧЕНЬ СВЕРЕН С КОДОМ ПРАВИЛА (src/positions/rules.py), а не со
    -- списком в тексте ТЗ: четыре причины, которые возвращает check_exit, плюс
    -- 'no_data' — признание того, что исход не измерен. См. пункты 1 и 2 в
    -- заголовке.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_reason_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_reason_chk
            CHECK (exit_reason IN ('target', 'stop', 'timeout', 'ambiguous',
                                   'no_data'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_res_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_res_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    -- Имя варианта и уровень предела идут только вместе. 'no_stop' с
    -- заполненным уровнем — это утверждение, отрицающее само себя; любой
    -- другой вариант без уровня означал бы, что предел взялся неизвестно
    -- откуда.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_variant_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_variant_chk
            CHECK (
                CASE WHEN variant = 'no_stop'
                     THEN stop_pct IS NULL
                     ELSE stop_pct IS NOT NULL AND stop_pct > 0
                END
            );
    END IF;

    -- ФОРМА ЗАПИСИ. Неизмеренный исход обязан выглядеть неизмеренным, а
    -- измеренный — измеренным. 'ambiguous' здесь ИЗМЕРЕН (пункт 3 в
    -- заголовке): правило позиций при одновременном касании выходит по пределу.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_shape_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_shape_chk
            CHECK (
                CASE WHEN exit_reason = 'no_data'
                     THEN exit_bar_ts IS NULL AND exit_price IS NULL
                          AND net_pnl_pct IS NULL AND net_pnl_usd IS NULL
                     ELSE exit_bar_ts IS NOT NULL AND exit_price IS NOT NULL
                          AND net_pnl_pct IS NOT NULL AND net_pnl_usd IS NOT NULL
                END
            );
    END IF;

    -- ТРИ NOT NULL, КОТОРЫЕ НЕЛЬЗЯ ПРОЧЕСТЬ КАК ИЗМЕРЕНИЕ. При 'no_data'
    -- удержание не «равно нулю», а не определено вовсе; ограничение требует
    -- ровно нули, и читать их в отрыве от exit_reason нельзя. См. заголовок.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_gap_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_gap_chk
            CHECK (
                exit_reason <> 'no_data'
                OR (held_sec = 0 AND extra_held_sec = 0 AND blocked_signals = 0)
            );
    END IF;

    -- СЛЕДСТВИЕ ПРАВИЛА, ЗАПИСАННОЕ ОГРАНИЧЕНИЕМ, А НЕ ОБЕЩАНИЕМ. Контроль —
    -- это пересчёт ФАКТА тем же правилом и тем же уровнем предела, поэтому он
    -- обязан закрыться ровно тогда же, когда закрылась настоящая позиция.
    -- Ненулевое extra_held_sec у контроля означало бы, что сверка с фактом
    -- прошла при разошедшихся моментах закрытия.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_control_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_control_chk
            CHECK (
                variant <> 'control'
                OR (extra_held_sec = 0 AND blocked_signals = 0
                    AND exit_reason <> 'no_data')
            );
    END IF;

    -- Отрицательное удержание и отрицательное число входов невозможны;
    -- extra_held_sec отрицательным быть МОЖЕТ — при пределе уже фактического
    -- позиция закрылась бы раньше, — и запрещать его значило бы запретить
    -- половину замера.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_stop_shadow_bounds_chk') THEN
        ALTER TABLE position_stop_shadow
            ADD CONSTRAINT position_stop_shadow_bounds_chk
            CHECK (bars_used >= 0 AND held_sec >= 0 AND blocked_signals >= 0
                   AND logic_version > 0
                   AND (exit_price IS NULL OR exit_price > 0));
    END IF;
END $$;

-- Право на чтение — той же роли, что и у остальных таблиц замеров, если она
-- заведена. Отсутствие роли не ошибка: на части машин её нет.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON position_stop_shadow TO agenttrade_ro;
    END IF;
END $$;

COMMENT ON TABLE position_stop_shadow IS
    'Этап 9.1.4: пересчёт исхода закрытых позиций при пяти уровнях предела '
    '(control, 1.5%, 2.0%, 3.0%, без предела). ЗАМЕР, а не внедрение: '
    'LOGIC_VERSION остаётся 5, BARRIER_STOP_PCT не меняется, ни одно решение '
    'системы от этой таблицы не зависит.';

COMMIT;
