-- ЭТАП 8.9 §6. Базовые стратегии как линейка сравнения.
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Этап 8.8 дал числа системы, но сравнивать их не с чем.
-- На падающем рынке выигрывает любой сигнал на продажу, включая брошенную
-- монету, — и отличить умение от погоды по одним лишь числам системы нельзя.
-- Здесь лежат исходы ЗАВЕДОМО БЕЗМОЗГЛЫХ правил, посчитанные на тех же
-- моментах, тех же ценах и той же метрикой. Это линейка, а не улучшение.
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА (§2 ТЗ): ни одно решение системы не меняется.
-- signals, signal_evaluations, signal_targets, risk_targets и
-- signal_outcomes_barrier этой миграцией НЕ ИЗМЕНЯЮТСЯ и колонками не
-- дополняются. Внешние ключи смотрят из новой таблицы наружу.
--
-- ПОЧЕМУ КЛЮЧ ИМЕННО ТАКОЙ. Первичный ключ — (стратегия, инструмент, момент
-- входа, горизонт), а НЕ (стратегия, сигнал, горизонт): стратегии §5 (grid_buy,
-- grid_sell) к сигналам не привязаны вовсе — они входят каждый час независимо
-- от системы, и signal_id у них NULL. Ключ обязан работать и для них.
--
-- Миграция идемпотентна. Откат — 016_strategy_outcomes_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/016_strategy_outcomes.sql

BEGIN;

CREATE TABLE IF NOT EXISTS strategy_outcomes (
    -- Ключ стратегии: always_buy | always_sell | coin_flip | system
    --                 | grid_buy | grid_sell
    strategy        TEXT        NOT NULL,
    instrument_id   INT         NOT NULL REFERENCES instruments(id),
    -- Момент входа. Для стратегий §4 — момент сигнала; для сетки §5 — ровно
    -- начало часа.
    entry_ts        TIMESTAMPTZ NOT NULL,
    horizon_h       SMALLINT    NOT NULL,
    -- NULL для сетки: у неё сигнала нет по построению, и это не пропуск данных,
    -- а сама суть стратегии «фон рынка без всякого участия системы».
    signal_id       BIGINT      REFERENCES signals(id),
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    price_at_entry  NUMERIC(20,8) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    -- ОТКУДА ВЗЯТА ЦЕЛЬ. 'frozen' — из signal_targets, то есть ровно та, что
    -- была названа человеку; 'risk_targets:<дата>' — историческая строка за
    -- дату входа, потому что для встречного направления замороженной цели не
    -- существует вовсе. Без этой колонки через месяц нельзя будет доказать,
    -- что цели не подменялись сегодняшними, — а это единственное, что отделяет
    -- измерение от подделки.
    target_source   TEXT        NOT NULL,
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    outcome         TEXT        NOT NULL,
    hit_at          TIMESTAMPTZ,
    net_pnl_pct     NUMERIC(12,6),
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    resolution      TEXT        NOT NULL,
    -- Зерно генератора монеты. Заполнено только у coin_flip: у остальных
    -- случайности нет, и число там означало бы, что она была.
    seed            BIGINT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, instrument_id, entry_ts, horizon_h)
);

DO $$
BEGIN
    -- Перечень стратегий ЗАКРЫТ: стратегия, названная своими словами, не
    -- считается стратегией и в линейку не попадает.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_strategy_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_strategy_chk
            CHECK (strategy IN ('always_buy', 'always_sell', 'coin_flip',
                                'system', 'grid_buy', 'grid_sell'));
    END IF;

    -- ТОТ ЖЕ перечень исходов, что в Этапе 8.8 (миграция 015). Расхождение
    -- сделало бы сравнение недействительным.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_outcome_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_outcome_chk
            CHECK (outcome IN ('target', 'stop', 'timeout', 'ambiguous', 'no_data'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_resolution_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_direction_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_bounds_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_bounds_chk
            CHECK (horizon_h > 0 AND price_at_entry > 0 AND stop_pct > 0
                   AND logic_version > 0);
    END IF;

    -- ИСТОЧНИК ЦЕЛИ НАЗВАН, А НЕ ПОДРАЗУМЕВАЕТСЯ. Либо 'frozen', либо
    -- 'risk_targets:' с датой. Пустая строка или произвольный текст означали
    -- бы, что происхождение цели неизвестно, — а тогда неизвестно и всё
    -- остальное в строке.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_target_source_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_target_source_chk
            CHECK (target_source = 'frozen'
                   OR target_source ~ '^risk_targets:\d{4}-\d{2}-\d{2}$');
    END IF;

    -- Согласованность формы — та же, что в 8.8: у ambiguous и no_data нет ни
    -- момента касания, ни результата; у target и stop момент касания есть.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_shape_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_shape_chk
            CHECK (
                CASE outcome
                    WHEN 'target'  THEN hit_at IS NOT NULL AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'    THEN hit_at IS NOT NULL AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout' THEN hit_at IS NULL AND net_pnl_pct IS NOT NULL
                    ELSE                hit_at IS NULL AND net_pnl_pct IS NULL
                END
            );
    END IF;

    -- Сетка §5 не привязана к сигналу, стратегии §4 — привязаны. Ограничение
    -- не даёт перепутать одно с другим: строка grid_* с signal_id означала бы,
    -- что «фон рынка без участия системы» этой системой всё-таки затронут.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_signal_link_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_signal_link_chk
            CHECK (
                CASE WHEN strategy IN ('grid_buy', 'grid_sell')
                     THEN signal_id IS NULL
                     ELSE signal_id IS NOT NULL
                END
            );
    END IF;

    -- Зерно есть тогда и только тогда, когда была случайность.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_seed_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_seed_chk
            CHECK ((strategy = 'coin_flip') = (seed IS NOT NULL));
    END IF;
END $$;

-- Сравнение §8 идёт по (сигнал, горизонт) между стратегиями — этому запросу
-- нужен индекс, потому что первичный ключ начинается со стратегии.
CREATE INDEX IF NOT EXISTS ix_strategy_outcomes_signal
    ON strategy_outcomes (signal_id, horizon_h, strategy);
CREATE INDEX IF NOT EXISTS ix_strategy_outcomes_strategy
    ON strategy_outcomes (strategy, horizon_h, outcome);

COMMENT ON TABLE strategy_outcomes IS
    'Этап 8.9: исходы базовых стратегий на тех же моментах и той же метрикой, '
    'что у системы. Линейка для сравнения; ни одно решение системы от неё '
    'не зависит.';

COMMIT;
