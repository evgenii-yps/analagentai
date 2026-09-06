-- ЭТАП 9.1.6 §7. Замер правила «без предела убытка, ожидание возврата в плюс
-- до 48 часов».
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Владелец 06.09.2026 предложил заменить правило выхода:
-- предела нет; сутки на достижение цели; если к суткам позиция в минусе — ждать
-- возврата в плюс ещё до суток; вернулась — закрыть в плюс; не вернулась за 48
-- часов — закрыть по рынку. Этап 9.1.4 измерил БЛИЗКИЙ, НО НЕ ТОТ вариант
-- («предел снят, срок 24 часа»); вариант с ОЖИДАНИЕМ ВОЗВРАТА не измерялся ни
-- разу. Здесь лежит его замер — и замер трёх соседних правил, без которых он
-- не читается.
--
-- ПЯТЬ ВАРИАНТОВ, А НЕ ДВА, и каждый отвечает на свой вопрос:
--   control                — фактическое правило: предел −1%, срок 24 часа;
--   A_nostop_24            — повторение варианта no_stop Этапа 9.1.4 на новой,
--                            большей выборке;
--   B_nostop_plus_48       — правило, предложенное владельцем;
--   C_stop_plus_48         — предел ОСТАВЛЕН, ожидание плюса есть. Владелец
--                            просит две перемены сразу; вариант C отделяет
--                            вторую от первой, иначе разницу B и control
--                            нельзя приписать ни одной из них;
--   D_nostop_plus_48_gross — то же, что B, но «плюс» ВАЛОВОЙ (Pbe = P0).
--                            Нужен ровно для одного: показать, сколько сделок
--                            разделяет ставка круговых издержек.
--
-- ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ. Ни одно решение системы от таблицы не зависит:
-- LOGIC_VERSION не поднимается, BARRIER_STOP_PCT не трогается, боевое правило
-- выхода остаётся прежним, сервисы decision, notify, evaluator, risk и
-- positions о ней не знают. РЕКОМЕНДАЦИЯ ВНЕДРИТЬ ВАРИАНТ ЭТИМ ЭТАПОМ
-- ЗАПРЕЩЕНА ПРЯМО (§1.4 ТЗ): выбор делает владелец по числам.
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА: positions, signals, ohlcv и signal_targets НЕ
-- ИЗМЕНЯЮТСЯ НИ ОДНОЙ КОЛОНКОЙ И НИ ОДНОЙ СТРОКОЙ. Внешний ключ смотрит ИЗ
-- новой таблицы наружу, а не наоборот: строка positions — это то, что
-- случилось, а строка отсюда — то, что случилось бы.
--
-- ON DELETE CASCADE стоит намеренно, как в миграции 022: содержимое таблицы
-- целиком производно от positions и ohlcv и восстановимо повторным прогоном
-- scripts/plus_exit_counterfactual_9_1_6.py --apply. Осиротевшая строка после
-- удаления позиции была бы числом ни о чём.
--
-- ЧЕТЫРЕ РАСХОЖДЕНИЯ С ПЕРЕЧНЕМ ИСХОДОВ §6 ТЗ, И КАЖДОЕ СВЕРЕНО С КОДОМ
-- ПРАВИЛА (src/positions/rules.py), а не с перечнем в тексте задания.
--
--  1. 'timeout_24' И 'timeout_48' — ЭТО ДВА РАЗНЫХ ИСХОДА, а правило возвращает
--     для обоих одно имя 'timeout' (rules.EXIT_TIMEOUT). Разделение сделано
--     здесь, в замере, и оно содержательно: «закрыт по рынку через сутки» и
--     «прождал вторые сутки и всё равно закрыт по рынку» — разные сделки с
--     разной занятостью слота. Сверка контроля с фактом при этом сводит
--     'timeout_24' обратно к записанному 'timeout'
--     (см. скрипт, control_reason_of_fact).
--
--  2. 'plus_exit' ПРАВИЛУ НЕ ИЗВЕСТЕН ВОВСЕ. Это исход НОВОГО правила, которое
--     измеряется: первый бар начиная с t0+24ч, на котором достижима цена
--     безубытка. Цена выхода у него равна ЦЕНЕ БЕЗУБЫТКА, а не цене закрытия
--     бара, и поэтому итог такой сделки равен НУЛЮ по построению (у варианта D
--     — ровно минус издержки: там «плюс» валовой). Это проверяемое
--     утверждение, и скрипт роняет расчёт при его нарушении.
--
--  3. 'ambiguous' ЗДЕСЬ ИЗМЕРЕН, а не пуст, — ровно как в миграции 022.
--     check_exit при одновременном касании цели и предела выходит ПО ПРЕДЕЛУ
--     (пункт 2 §4.4 rules.py): бар выхода есть, цена есть, итог есть. В
--     перечень §6 ТЗ этот исход не входит, а в данных он встречается, и
--     запретить его значило бы уронить запись на первой же такой позиции.
--     Достижим он только у вариантов с пределом — control и C_stop_plus_48.
--
--  4. 'data_gap' В ПЕРЕЧЕНЬ НЕ ВХОДИТ, хотя правило такую причину знает
--     (rules.EXIT_DATA_GAP). Возвращает её НЕ check_exit, а check_gap_exit, и
--     позиции с ней в выборку замера не берутся вовсе (§5.2 ТЗ). Отсутствие
--     значения в ограничении — проверяемое утверждение: строка с 'data_gap'
--     здесь означала бы, что в замер попало то, что замером не является.
--
-- ПОЧЕМУ ПОЛЯ ВЫХОДА БЕЗ NOT NULL, А ФОРМА ЗАПИСИ — ОГРАНИЧЕНИЕМ (§7.2 ТЗ).
-- У исхода 'no_data' бара выхода не существует: ряд свечей оборвался внутри
-- отрезка, и правило честно не даёт исхода. NOT NULL заставил бы подставить
-- туда выдуманное число, неотличимое от измеренного. Нуль в held_hours при
-- 'no_data' был бы таким же выдуманным числом — поэтому там NULL, а не нуль:
-- удержание в этом случае не «равно нулю», а НЕ ОПРЕДЕЛЕНО.
--
-- ВЕРСИЯ ЛОГИКИ ВХОДИТ В ПЕРВИЧНЫЙ КЛЮЧ (§7.3 ТЗ). Смешивать версии в одном
-- замере запрещено (§5.1), и ключ (position_id, variant) молча позволил бы
-- повторному прогону с другой версией ЗАТЕРЕТЬ строку предыдущей — то есть
-- сделать ровно то смешение, которое запрещено, и не оставить следа.
--
-- Миграция идемпотентна. Откат — 024_position_plus_exit_shadow_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/024_position_plus_exit_shadow.sql

BEGIN;

CREATE TABLE IF NOT EXISTS position_plus_exit_shadow (
    position_id      BIGINT       NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    -- Имя варианта из закрытого перечня §4 ТЗ. Строка, а не набор флагов:
    -- вариант — это ТРИ свойства сразу (предел, срок, ожидание плюса), и
    -- разложить их по колонкам значило бы разрешить сочетания, которых этап
    -- не считал.
    variant          TEXT         NOT NULL,
    logic_version    SMALLINT     NOT NULL,
    exit_reason      TEXT         NOT NULL,
    -- ДВА МОМЕНТА, А НЕ ОДИН, И ОНИ РАЗНЫЕ. exit_bar_ts — время ОТКРЫТИЯ бара
    -- выхода (его возвращает правило: точнее ряд свечей не знает), closed_at —
    -- время его ЗАКРЫТИЯ, то есть ровно то, что живой сервис пишет в
    -- positions.closed_at. Сверка контроля идёт по closed_at; на Этапе 9.1.3
    -- сравнение открытия с закрытием стоило одиннадцати ложных расхождений
    -- ровно по 60 секунд.
    exit_bar_ts      TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    exit_price       NUMERIC(20,8),
    net_pnl_pct      NUMERIC(12,6),
    -- Считается от positions.notional_usd ФАКТИЧЕСКОЙ позиции, а не от
    -- константы слота: слот мог отличаться, и подставленная константа
    -- превратила бы замер в оценку.
    net_pnl_usd      NUMERIC(14,6),
    -- ЗАНЯТОСТЬ СЛОТА (§6 ТЗ, ЧИСЛО 4) — вторая половина ответа владельцу.
    -- Слотов пять, и позиция, ждущая вторые сутки, стоит не только своего
    -- итога, но и всех входов по этому инструменту за время ожидания.
    held_hours       NUMERIC(10,4),
    bars_used        INTEGER      NOT NULL,
    resolution       TEXT         NOT NULL,
    computed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT position_plus_exit_shadow_pk
        PRIMARY KEY (position_id, variant, logic_version)
);

CREATE INDEX IF NOT EXISTS position_plus_exit_shadow_variant_idx
    ON position_plus_exit_shadow (variant);

-- Ограничения заводятся ОТДЕЛЬНЫМ блоком с проверкой существования по имени:
-- CREATE TABLE IF NOT EXISTS существующую таблицу не меняет, и на томе, где
-- таблица уже создана, они иначе не появились бы никогда (тот же приём, что в
-- миграциях 017, 018, 021 и 022).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_variant_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_variant_chk
            CHECK (variant IN ('control', 'A_nostop_24', 'B_nostop_plus_48',
                               'C_stop_plus_48', 'D_nostop_plus_48_gross'));
    END IF;

    -- ПЕРЕЧЕНЬ СВЕРЕН С КОДОМ ПРАВИЛА, а не со списком в тексте ТЗ. См.
    -- пункты 1–4 в заголовке.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_reason_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_reason_chk
            CHECK (exit_reason IN ('target', 'stop', 'ambiguous', 'plus_exit',
                                   'timeout_24', 'timeout_48', 'no_data'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_res_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_res_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    -- ФОРМА ЗАПИСИ (§7.2 ТЗ). Неизмеренный исход обязан выглядеть
    -- неизмеренным, а измеренный — измеренным. Пустая форма разрешена ТОЛЬКО
    -- для 'no_data'; для 'plus_exit', 'target', 'stop', 'ambiguous',
    -- 'timeout_24' и 'timeout_48' момент выхода и цена выхода обязательны.
    -- НУЛЬ НЕЛЬЗЯ ПРОЧЕСТЬ КАК ИЗМЕРЕНИЕ, поэтому у 'no_data' там NULL, а не
    -- нули: удержание в этом случае не «равно нулю», а не определено.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_shape_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_shape_chk
            CHECK (
                CASE WHEN exit_reason = 'no_data'
                     THEN exit_bar_ts IS NULL AND closed_at IS NULL
                          AND exit_price IS NULL AND net_pnl_pct IS NULL
                          AND net_pnl_usd IS NULL AND held_hours IS NULL
                     ELSE exit_bar_ts IS NOT NULL AND closed_at IS NOT NULL
                          AND exit_price IS NOT NULL AND net_pnl_pct IS NOT NULL
                          AND net_pnl_usd IS NOT NULL AND held_hours IS NOT NULL
                END
            );
    END IF;

    -- СЛЕДСТВИЯ УСТРОЙСТВА ВАРИАНТОВ, ЗАПИСАННЫЕ ОГРАНИЧЕНИЕМ, А НЕ ОБЕЩАНИЕМ.
    --
    --  * у вариантов БЕЗ ПРЕДЕЛА (A, B, D) исходов 'stop' и 'ambiguous' не
    --    бывает: обе ветки выхода по пределу в check_exit недостижимы, когда
    --    цена предела не может быть задета (см. §9.2 ТЗ и разбор в скрипте);
    --  * у вариантов БЕЗ ОЖИДАНИЯ (control, A) не бывает 'plus_exit' и
    --    'timeout_48': их окно кончается на 24 часах;
    --  * у вариантов С ОЖИДАНИЕМ (B, C, D) не бывает 'timeout_24': дойдя до
    --    суток, они не закрываются, а ждут.
    --
    -- Строка, нарушившая любое из трёх, означала бы, что расчёт посчитал не тот
    -- вариант, имя которого записал, — и заметить это по числам было бы нечем.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_shape_variant_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_shape_variant_chk
            CHECK (
                (variant NOT IN ('A_nostop_24', 'B_nostop_plus_48',
                                 'D_nostop_plus_48_gross')
                 OR exit_reason NOT IN ('stop', 'ambiguous'))
                AND (variant NOT IN ('control', 'A_nostop_24')
                     OR exit_reason NOT IN ('plus_exit', 'timeout_48'))
                AND (variant NOT IN ('B_nostop_plus_48', 'C_stop_plus_48',
                                     'D_nostop_plus_48_gross')
                     OR exit_reason <> 'timeout_24')
            );
    END IF;

    -- КОНТРОЛЬ — ЭТО ПЕРЕСЧЁТ ФАКТА, И НЕИЗМЕРЕННЫМ ОН БЫТЬ НЕ МОЖЕТ. Строка
    -- control с 'no_data' означала бы, что таблица записана при несовпавшем
    -- контроле, — а при несовпавшем контроле не записывается ни одна строка
    -- (§9.1 ТЗ). Ограничение делает это утверждение проверяемым базой, а не
    -- обещанием скрипта.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_control_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_control_chk
            CHECK (variant <> 'control' OR exit_reason <> 'no_data');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'position_plus_exit_shadow_bounds_chk') THEN
        ALTER TABLE position_plus_exit_shadow
            ADD CONSTRAINT position_plus_exit_shadow_bounds_chk
            CHECK (bars_used >= 0 AND logic_version > 0
                   AND (exit_price IS NULL OR exit_price > 0)
                   AND (held_hours IS NULL OR held_hours >= 0));
    END IF;
END $$;

-- Право на чтение — той же роли, что и у остальных таблиц замеров, если она
-- заведена. Отсутствие роли не ошибка: на части машин её нет.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON position_plus_exit_shadow TO agenttrade_ro;
    END IF;
END $$;

COMMENT ON TABLE position_plus_exit_shadow IS
    'Этап 9.1.6: замер правила «без предела убытка, ожидание возврата в плюс '
    'до 48 часов» и трёх соседних правил (control, A_nostop_24, C_stop_plus_48, '
    'D_nostop_plus_48_gross). ЗАМЕР, а не внедрение: LOGIC_VERSION не '
    'поднимается, боевое правило выхода не меняется, ни одно решение системы от '
    'этой таблицы не зависит.';

COMMIT;
