-- ЭТАП 8.8 §6. Вторая оценка исхода — по правилу «какая граница задета первой».
--
-- НОМЕР МИГРАЦИИ. ТЗ называет файл 014_barrier_outcomes.sql, но номер 014 уже
-- занят миграцией Этапа 8.2 (014_risk_targets.sql, применена на сервере).
-- Два разных файла под одним номером сделали бы порядок применения
-- неопределённым, поэтому номер сдвинут на следующий свободный — 015.
-- Содержание миграции ТЗ соответствует полностью.
--
-- ЗАЧЕМ ВТОРАЯ ТАБЛИЦА, А НЕ КОЛОНКИ В signal_evaluations. Действующая оценка
-- отвечает на вопрос «где была цена в момент t + горизонт». Новая отвечает на
-- другой вопрос: «какая граница задета первой». Это РАЗНЫЕ величины, и
-- смешивать их в одной строке значит потерять возможность сравнить их между
-- собой (§9). Обе оценки существуют параллельно; ни одна не отменяет другую.
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА (§1 ТЗ): ни одно решение системы не меняется.
-- signals, signal_evaluations, signal_targets и risk_targets этой миграцией
-- НЕ ИЗМЕНЯЮТСЯ и колонками не дополняются. Внешний ключ смотрит из новой
-- таблицы наружу, а не наоборот.
--
-- logic_version в таблице ОБЯЗАТЕЛЕН: правило проекта о несмешивании версий.
-- Без него выборка «доля target» молча усреднила бы разные системы.
--
-- Миграция идемпотентна. Откат — 015_barrier_outcomes_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/015_barrier_outcomes.sql

BEGIN;

CREATE TABLE IF NOT EXISTS signal_outcomes_barrier (
    signal_id       BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT    NOT NULL,
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    -- Цена решения. Берётся ЗАМОРОЖЕННОЙ из signal_targets, а не считается
    -- заново: уровни обязаны стоять там же, где стояли в момент сигнала.
    price_at_signal NUMERIC(20,8) NOT NULL,
    -- Замороженная цель В ПРОЦЕНТАХ из signal_targets. Текущее значение из
    -- risk_targets брать ЗАПРЕЩЕНО (§3 ТЗ): это подделка истории.
    target_pct      NUMERIC(10,6) NOT NULL,
    -- Предел (BARRIER_STOP_PCT) и круговые издержки (RISK_COST_ROUNDTRIP_PCT)
    -- в процентах. Хранятся В СТРОКЕ, а не подразумеваются из .env: значение
    -- параметра со временем меняется, и без снимка нельзя понять, при каком
    -- пределе получен исход.
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    outcome         TEXT        NOT NULL,
    -- Момент касания и число баров до него. NULL для timeout, ambiguous
    -- (порядок неизвестен) и no_data (касания не наблюдалось).
    hit_at          TIMESTAMPTZ,
    bars_to_hit     INTEGER,
    -- Итог в деньгах за вычетом издержек (§5). NULL для ambiguous и no_data:
    -- у неизвестного исхода нет и результата.
    net_pnl_pct     NUMERIC(12,6),
    -- Максимальное отклонение ПРОТИВ сигнала (mae) и В ПОЛЬЗУ (mfe) за окно,
    -- в процентах, по касанию. Оба нужны §8: по ним строится таблица уровней
    -- предела без повторного чтения свечей.
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    -- Чем считали: '1m' — минутные свечи (порядок касаний однозначен),
    -- '1h' — часовые (одновременное касание неразрешимо, §4). Колонка нужна
    -- всегда: без неё нельзя отличить измеренный порядок от неизвестного.
    resolution      TEXT        NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h)
);

-- Ограничения значений заводятся ОТДЕЛЬНО, потому что CREATE TABLE
-- IF NOT EXISTS существующую таблицу не меняет: на томе, где таблица уже
-- создана прежним запуском, ограничения иначе не появились бы никогда.
DO $$
BEGIN
    -- Перечень исходов ЗАКРЫТ (§3 ТЗ): ровно пять значений. Исход, названный
    -- своими словами, не считается исходом — база обязана его отвергнуть.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_outcome_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_outcome_chk
            CHECK (outcome IN ('target', 'stop', 'timeout', 'ambiguous', 'no_data'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_resolution_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;

    -- Направление: та же пара значений, что в signals.decision и
    -- signal_targets.direction. 'wait' исходом не оценивается вовсе.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_direction_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;

    -- Версия логики положительна: ноль зарезервирован под признак «версия
    -- неизвестна» (миграция 012) и в этой таблице появиться не может.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_logic_version_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_logic_version_chk
            CHECK (logic_version > 0);
    END IF;

    -- Горизонт положителен, цена решения строго больше нуля: на неё делят.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_bounds_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_bounds_chk
            CHECK (horizon_h > 0 AND price_at_signal > 0 AND stop_pct > 0);
    END IF;

    -- СОГЛАСОВАННОСТЬ ИСХОДА И ПОЛЕЙ. Ограничение поймало бы ошибку расчёта
    -- раньше, чем она попадёт в отчёт: у ambiguous и no_data не бывает ни
    -- момента касания, ни результата в деньгах; у target и stop момент
    -- касания есть обязательно.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_shape_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_shape_chk
            CHECK (
                CASE outcome
                    WHEN 'target'    THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                          AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'      THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                          AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout'   THEN hit_at IS NULL AND bars_to_hit IS NULL
                                          AND net_pnl_pct IS NOT NULL
                    ELSE                  hit_at IS NULL AND bars_to_hit IS NULL
                                          AND net_pnl_pct IS NULL
                END
            );
    END IF;
END $$;

-- Выборки §8 и §9 идут по горизонту и по исходу, а не по signal_id.
CREATE INDEX IF NOT EXISTS ix_barrier_horizon_outcome
    ON signal_outcomes_barrier (logic_version, horizon_h, outcome);

COMMENT ON TABLE signal_outcomes_barrier IS
    'Этап 8.8: исход сигнала по правилу «какая граница задета первой». '
    'Вторая, НЕЗАВИСИМАЯ оценка рядом с signal_evaluations; ни одно решение '
    'системы от неё не зависит.';

COMMIT;
