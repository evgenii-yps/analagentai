-- ЭТАП 8.10 §6. Подвижный выход: замер на истории.
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Действующий выход один: цель, предел убытка или срок,
-- причём цель заморожена в момент сигнала. Владелец предложил другой выход —
-- по откату от достигнутой вершины. Здесь лежат исходы ТРИНАДЦАТИ правил
-- выхода на ОДНИХ И ТЕХ ЖЕ сигналах, ценах и свечах: двенадцать сочетаний
-- «уровень включения × величина отката» и тринадцатое, контрольное —
-- фиксированная цель, ровно правило Этапа 8.8.
--
-- ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ. Ни одно решение системы от таблицы не зависит:
-- LOGIC_VERSION остаётся 5, сервисы decision, notify, evaluator и risk о ней
-- не знают, выбор «лучшего» варианта ЗАПРЕЩЁН прямо (§5.4 ТЗ).
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА (§2 ТЗ): signals, signal_evaluations, signal_targets,
-- risk_targets, signal_outcomes_barrier и strategy_outcomes этой миграцией
-- НЕ ИЗМЕНЯЮТСЯ и колонками не дополняются. Внешний ключ смотрит из новой
-- таблицы наружу, а не наоборот.
--
-- ПОЧЕМУ ПАРАМЕТРЫ ВАРИАНТА ВХОДЯТ В ПЕРВИЧНЫЙ КЛЮЧ. На одной паре
-- (сигнал, горизонт) существует тринадцать разных исходов — по одному на
-- правило выхода. Ключ (signal_id, horizon_h) вытеснил бы двенадцать из них,
-- и таблица молча хранила бы один вариант вместо тринадцати.
--
-- КОНТРОЛЬНЫЙ ВАРИАНТ ЗАПИСАН ПАРОЙ (0, 0), а не NULL: NULL в первичном ключе
-- PostgreSQL не допускает. Ноль здесь читается однозначно — включать нечего и
-- откатывать нечего, — и с сеткой §4 (0.25…1.00 × 0.20…0.50) не пересекается.
--
-- Миграция идемпотентна. Откат — 017_trailing_outcomes_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/017_trailing_outcomes.sql

BEGIN;

CREATE TABLE IF NOT EXISTS trailing_outcomes (
    signal_id       BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT    NOT NULL,
    -- Уровень включения подвижного выхода — доля от цели: 0.25, 0.50, 0.75,
    -- 1.00; 0 — подвижного выхода нет (контрольный вариант).
    activation_ratio NUMERIC(4,2) NOT NULL,
    -- Величина отката от вершины — доля пройденного пути: 0.20, 0.33, 0.50;
    -- 0 — у контрольного варианта.
    retrace_ratio   NUMERIC(4,2) NOT NULL,
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    -- Цена решения, замороженная цель, предел и издержки переносятся ИЗ
    -- signal_outcomes_barrier как есть. Пересчитывать их заново запрещено:
    -- сравнение с Этапом 8.8 действительно только при тех же входных числах.
    price_at_signal NUMERIC(20,8) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    -- ЧЕМ ЗАКОНЧИЛОСЬ. Шесть значений, и шестое требует объяснения:
    --   target    — задета цель (бывает ТОЛЬКО у контрольного варианта);
    --   stop      — задет предел убытка;
    --   trail     — сработал подвижный выход (только у подвижных вариантов);
    --   timeout   — до срока не сработало ничего, итог по цене на срок;
    --   no_data   — ряд свечей окна неполон, исход неизвестен;
    --   ambiguous — в одном баре случились два события, порядок которых
    --               неизвестен. §6 ТЗ этого значения не называет, но §4 ТЗ
    --               требует, чтобы контрольный вариант совпал с
    --               signal_outcomes_barrier ДО ПОСЛЕДНЕГО ЗНАКА, а там такие
    --               строки есть. Выбросить их значило бы не совпасть;
    --               переименовать — выдать неизвестный порядок за известный.
    exit_reason     TEXT        NOT NULL,
    hit_at          TIMESTAMPTZ,
    bars_to_hit     INTEGER,
    net_pnl_pct     NUMERIC(12,6),
    -- ВЕРШИНА, ИЗВЕСТНАЯ ПРАВИЛУ НА МОМЕНТ ВЫХОДА, в процентах от цены входа.
    -- Это не то же самое, что mfe_pct: тот описывает ВСЁ окно и одинаков у
    -- всех тринадцати вариантов, а вершина — то, что правило видело, когда
    -- принимало решение. Для строк trail отсюда проверяется арифметика итога:
    --   net_pnl_pct = (1 − retrace_ratio) × peak_pct − cost_pct.
    peak_pct        NUMERIC(12,6) NOT NULL,
    -- mae/mfe/resolution — те же величины и то же значение, что в 8.8.
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    resolution      TEXT        NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h, activation_ratio, retrace_ratio)
);

-- Ограничения заводятся ОТДЕЛЬНО: CREATE TABLE IF NOT EXISTS существующую
-- таблицу не меняет, и на томе, где таблица уже создана, они иначе не
-- появились бы никогда.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_exit_reason_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_exit_reason_chk
            CHECK (exit_reason IN ('target', 'stop', 'trail', 'timeout',
                                   'ambiguous', 'no_data'));
    END IF;

    -- СЕТКА ВАРИАНТОВ ЗАКРЫТА (§4 ТЗ): ровно тринадцать сочетаний. Четырнадцатое
    -- означало бы, что кто-то подобрал параметр вне заявленной сетки, — а это и
    -- есть та самая подгонка, против которой написан §5.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_variant_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_variant_chk
            CHECK ((activation_ratio, retrace_ratio) IN (
                (0.00, 0.00),
                (0.25, 0.20), (0.25, 0.33), (0.25, 0.50),
                (0.50, 0.20), (0.50, 0.33), (0.50, 0.50),
                (0.75, 0.20), (0.75, 0.33), (0.75, 0.50),
                (1.00, 0.20), (1.00, 0.33), (1.00, 0.50)
            ));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_resolution_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_direction_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_bounds_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_bounds_chk
            CHECK (horizon_h > 0 AND price_at_signal > 0 AND stop_pct > 0
                   AND logic_version > 0);
    END IF;

    -- СОГЛАСОВАННОСТЬ ИСХОДА И ПОЛЕЙ — та же, что в 8.8, плюс trail: у него
    -- момент выхода есть, потому что откат наблюдался на конкретном баре.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_shape_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_shape_chk
            CHECK (
                CASE exit_reason
                    WHEN 'target'  THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'    THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'trail'   THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout' THEN hit_at IS NULL AND bars_to_hit IS NULL
                                        AND net_pnl_pct IS NOT NULL
                    ELSE                hit_at IS NULL AND bars_to_hit IS NULL
                                        AND net_pnl_pct IS NULL
                END
            );
    END IF;

    -- ДВА СЛЕДСТВИЯ ПРАВИЛА §4, ЗАПИСАННЫЕ ОГРАНИЧЕНИЕМ, А НЕ ОБЕЩАНИЕМ.
    --
    -- 1. Подвижный выход не может сработать там, где его нет: строка trail с
    --    activation_ratio = 0 означала бы, что контрольный вариант посчитан не
    --    правилом 8.8, и всё сравнение недействительно.
    -- 2. Цель не может закрыть сделку у подвижного варианта: уровень включения
    --    лежит не дальше цели, поэтому цена, дошедшая до цели, уже включила
    --    подвижный выход. Строка target с activation_ratio > 0 означала бы, что
    --    правило реализовано иначе, чем описано.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_reason_variant_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_reason_variant_chk
            CHECK (
                (exit_reason <> 'trail'  OR activation_ratio > 0)
                AND
                (exit_reason <> 'target' OR activation_ratio = 0)
            );
    END IF;
END $$;

-- Сравнение §5 идёт по варианту и горизонту; сверка контрольного варианта с
-- signal_outcomes_barrier — по (сигнал, горизонт). Первичный ключ обслуживает
-- второй запрос, но не первый.
CREATE INDEX IF NOT EXISTS ix_trailing_outcomes_variant
    ON trailing_outcomes (logic_version, activation_ratio, retrace_ratio, horizon_h);
CREATE INDEX IF NOT EXISTS ix_trailing_outcomes_reason
    ON trailing_outcomes (exit_reason, horizon_h);

COMMENT ON TABLE trailing_outcomes IS
    'Этап 8.10: исходы тринадцати правил выхода на одних и тех же сигналах. '
    'Замер задним числом; ни одно решение системы от таблицы не зависит, '
    'выбор «лучшего» варианта для внедрения запрещён §5.4 ТЗ.';

COMMIT;
