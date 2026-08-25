-- ЭТАП 8.2 §3. Цели по вероятности: измеренная величина хода и её заморозка
-- в момент сигнала.
--
-- ЗАЧЕМ ДВЕ ТАБЛИЦЫ, А НЕ ОДНА. risk_targets — ЖИВАЯ величина: она
-- пересчитывается каждые сутки по последним 90 суткам рынка и меняется вместе
-- с рынком. signal_targets — то, что человеку УЖЕ БЫЛО СКАЗАНО. Если хранить
-- только первую, то через сутки нельзя ответить на единственный важный вопрос:
-- «какую цель система назвала в тот момент и сбылась ли она». Проверить
-- систему постфактум стало бы невозможно.
--
-- ПРАВИЛО НЕИЗМЕННОСТИ. Строки signal_targets НИКОГДА не обновляются. Суточный
-- пересчёт пишет НОВУЮ строку risk_targets с новым computed_at и не трогает
-- ни одного уже выданного сигнала.
--
-- targets_version — версия МЕТОДИКИ расчёта целей, НЕ logic_version. Цель не
-- участвует в принятии решения buy/sell/wait, и её появление не делает сигналы
-- версии 5 несравнимыми между собой. Старт — 1.
--
-- Существующие таблицы этой миграцией не изменяются. Обе новые таблицы не
-- удаляются политикой сроков хранения (scripts/retention.py): объём мал —
-- 5 инструментов x 4 горизонта x 2 направления = 40 строк в сутки.
--
-- Миграция идемпотентна. Откат — 014_risk_targets_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/014_risk_targets.sql

BEGIN;

CREATE TABLE IF NOT EXISTS risk_targets (
    instrument_id      INT         NOT NULL REFERENCES instruments(id),
    horizon_h          SMALLINT    NOT NULL,
    direction          TEXT        NOT NULL CHECK (direction IN ('buy','sell')),
    computed_at        TIMESTAMPTZ NOT NULL,
    window_days        SMALLINT    NOT NULL,
    data_from          TIMESTAMPTZ NOT NULL,
    data_to            TIMESTAMPTZ NOT NULL,
    n_observations     INT         NOT NULL,
    -- NULL = цель НЕ рассчитана. Причина обязана быть названа в
    -- no_target_reason: «цели нет» и «цель равна нулю» — разные утверждения.
    target_pct         NUMERIC(10,5),
    -- Доля наблюдений с MFE >= target_pct. Считается ФАКТИЧЕСКИ по выборке,
    -- а не подставляется как 0.60: расхождение с 0.60 — признак ошибки счёта.
    hit_rate           NUMERIC(6,5),
    mfe_p25            NUMERIC(10,5),
    mfe_p50            NUMERIC(10,5),
    mfe_p75            NUMERIC(10,5),
    cost_roundtrip_pct NUMERIC(6,4)  NOT NULL,
    covers_fees        BOOLEAN       NOT NULL DEFAULT FALSE,
    no_target_reason   TEXT,       -- NULL | 'few_observations'
                                   -- | 'negative_percentile' | 'data_gap'
    source             TEXT        NOT NULL,   -- 'backtest.candles'
    targets_version    SMALLINT    NOT NULL,
    PRIMARY KEY (instrument_id, horizon_h, direction, computed_at)
);

CREATE INDEX IF NOT EXISTS ix_risk_targets_latest
    ON risk_targets (instrument_id, horizon_h, direction, computed_at DESC);

CREATE TABLE IF NOT EXISTS signal_targets (
    signal_id            BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h            SMALLINT    NOT NULL,
    direction            TEXT        NOT NULL CHECK (direction IN ('buy','sell')),
    price_at_signal      DOUBLE PRECISION NOT NULL,
    target_pct           NUMERIC(10,5),
    target_price         DOUBLE PRECISION,
    hit_rate             NUMERIC(6,5),
    covers_fees          BOOLEAN     NOT NULL DEFAULT FALSE,
    no_target_reason     TEXT,
    -- Какая именно строка risk_targets была взята. Без неё нельзя восстановить,
    -- на чём стояла названная человеку цель.
    risk_target_computed_at TIMESTAMPTZ,
    targets_version      SMALLINT,
    frozen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h)
);

-- Ограничения значений. Заводятся отдельно, потому что CREATE TABLE
-- IF NOT EXISTS существующую таблицу не меняет: на томе, где таблицы уже
-- созданы прежней версией миграции, ограничения обязаны появиться тоже.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'risk_targets'::regclass
           AND conname = 'risk_targets_reason_known'
    ) THEN
        -- Перечень причин ЗАКРЫТ: неизвестная строка в no_target_reason
        -- означала бы, что расчёт молчит о причине отказа своими словами,
        -- и её нельзя было бы посчитать запросом.
        ALTER TABLE risk_targets
            ADD CONSTRAINT risk_targets_reason_known CHECK (
                no_target_reason IS NULL
                OR no_target_reason IN ('few_observations',
                                        'negative_percentile',
                                        'data_gap')
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'risk_targets'::regclass
           AND conname = 'risk_targets_reason_matches_target'
    ) THEN
        -- Цель и причина её отсутствия взаимно исключают друг друга. Строка
        -- с обоими полями сразу (или без обоих) не читается однозначно, а
        -- по этой таблице потом объясняют человеку, почему цели не было.
        ALTER TABLE risk_targets
            ADD CONSTRAINT risk_targets_reason_matches_target CHECK (
                (target_pct IS NULL) = (no_target_reason IS NOT NULL)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'risk_targets'::regclass
           AND conname = 'risk_targets_horizon_positive'
    ) THEN
        ALTER TABLE risk_targets
            ADD CONSTRAINT risk_targets_horizon_positive CHECK (horizon_h > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'signal_targets'::regclass
           AND conname = 'signal_targets_reason_known'
    ) THEN
        ALTER TABLE signal_targets
            ADD CONSTRAINT signal_targets_reason_known CHECK (
                no_target_reason IS NULL
                OR no_target_reason IN ('few_observations',
                                        'negative_percentile',
                                        'data_gap',
                                        'no_risk_target')
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'signal_targets'::regclass
           AND conname = 'signal_targets_horizon_positive'
    ) THEN
        ALTER TABLE signal_targets
            ADD CONSTRAINT signal_targets_horizon_positive CHECK (horizon_h > 0);
    END IF;
END $$;

COMMENT ON TABLE risk_targets IS
    'Цель по вероятности: 40-й процентиль максимального благоприятного хода '
    '(MFE) за 90 суток часовых свечей спота. Этап 8.2 §4. Пересчитывается '
    'ежесуточно НОВОЙ строкой; старые строки — история изменения целей.';
COMMENT ON TABLE signal_targets IS
    'Цель, ЗАМОРОЖЕННАЯ в момент выдачи сигнала (Этап 8.2 §6). Строки НИКОГДА '
    'не обновляются: без этого проверить систему постфактум невозможно.';
COMMENT ON COLUMN risk_targets.targets_version IS
    'Версия МЕТОДИКИ расчёта целей, НЕ logic_version.';
COMMENT ON COLUMN risk_targets.hit_rate IS
    'Фактическая доля наблюдений с MFE >= target_pct. Отклонение от 0.60 '
    'больше 0.02 — признак ошибки расчёта (§4.5 ТЗ 8.2).';

COMMIT;
