-- Схема БД Agent Trade (Этап 1).
-- Применяется автоматически контейнером postgres через /docker-entrypoint-initdb.d.
-- Все DDL идемпотентны (IF NOT EXISTS). Время хранится в TIMESTAMPTZ (UTC).

-- Торговые инструменты (биржа + символ + тип).
CREATE TABLE IF NOT EXISTS instruments (
    id         SERIAL PRIMARY KEY,
    exchange   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    base       TEXT NOT NULL,
    quote      TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'spot',   -- spot | swap | future
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exchange, symbol, type)
);

-- Свечи OHLCV по инструменту и таймфрейму.
CREATE TABLE IF NOT EXISTS ohlcv (
    instrument_id INT NOT NULL REFERENCES instruments(id),
    timeframe     TEXT NOT NULL,                -- 1m,5m,15m,1h,4h,1d
    ts            TIMESTAMPTZ NOT NULL,
    open  DOUBLE PRECISION NOT NULL,
    high  DOUBLE PRECISION NOT NULL,
    low   DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_ts ON ohlcv (instrument_id, timeframe, ts DESC);

-- Лента сделок.
CREATE TABLE IF NOT EXISTS trades (
    id            BIGSERIAL PRIMARY KEY,
    instrument_id INT NOT NULL REFERENCES instruments(id),
    trade_id      TEXT,
    ts            TIMESTAMPTZ NOT NULL,
    price         DOUBLE PRECISION NOT NULL,
    amount        DOUBLE PRECISION NOT NULL,
    side          TEXT,                          -- buy | sell
    UNIQUE (instrument_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades (instrument_id, ts DESC);

-- Ставки финансирования (funding rate) для деривативов.
CREATE TABLE IF NOT EXISTS funding (
    instrument_id INT NOT NULL REFERENCES instruments(id),
    ts   TIMESTAMPTZ NOT NULL,
    rate DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (instrument_id, ts)
);

-- Открытый интерес (open interest).
CREATE TABLE IF NOT EXISTS open_interest (
    instrument_id INT NOT NULL REFERENCES instruments(id),
    ts    TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (instrument_id, ts)
);

-- Снимки стакана заявок.
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    instrument_id INT NOT NULL REFERENCES instruments(id),
    ts            TIMESTAMPTZ NOT NULL,
    bids          JSONB NOT NULL,                -- [[price, amount], ...]
    asks          JSONB NOT NULL,
    spread        DOUBLE PRECISION,
    bid_volume    DOUBLE PRECISION,
    ask_volume    DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_ob_ts ON orderbook_snapshots (instrument_id, ts DESC);

-- Торговые сигналы, сформированные системой.
CREATE TABLE IF NOT EXISTS signals (
    id             BIGSERIAL PRIMARY KEY,
    instrument_id  INT NOT NULL REFERENCES instruments(id),
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision       TEXT NOT NULL,                -- buy | sell | wait
    probability    DOUBLE PRECISION,
    agents_payload JSONB,
    rationale      TEXT,
    status         TEXT NOT NULL DEFAULT 'open', -- open | closed
    pnl_pct        DOUBLE PRECISION,
    drawdown_pct   DOUBLE PRECISION,
    success        BOOLEAN,
    notified       BOOLEAN NOT NULL DEFAULT FALSE, -- признак «обработан» notify (Этап 5)
    notified_at    TIMESTAMPTZ,                    -- факт реальной отправки в Telegram (Этап 6.6)
    logic_version  SMALLINT NOT NULL DEFAULT 1,    -- версия логики агентов/агрегации (Этап 7.0/7.2)
    degraded       BOOLEAN NOT NULL DEFAULT FALSE, -- решение при неполном составе агентов (<3), Этап 7.2
    -- Этап 7.3. probability выше хранит ИНДЕКС СОГЛАСИЯ (формула не изменилась);
    -- вероятность успеха — только здесь и только если построена по фактическим
    -- исходам, иначе NULL. Колонка probability НЕ переименовывается: её читают
    -- выгрузка, бот и суточная сводка.
    calibrated_probability DOUBLE PRECISION,
    calibration_id BIGINT,
    inputs_hash    TEXT,                           -- sha256 канонической строки мнений
    is_repeat      BOOLEAN NOT NULL DEFAULT FALSE  -- тот же набор мнений, что и у предыдущего
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_inputs_hash
    ON signals (instrument_id, inputs_hash, ts DESC);

-- Калибровочные кривые (Этап 7.3): таблица соответствия «диапазон индекса
-- согласия → фактическая доля успеха», построенная по независимым наблюдениям.
CREATE TABLE IF NOT EXISTS calibration_curves (
    id              BIGSERIAL PRIMARY KEY,
    logic_version   SMALLINT    NOT NULL,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    sample_size     INTEGER     NOT NULL,   -- число НЕЗАВИСИМЫХ наблюдений
    window_from     TIMESTAMPTZ NOT NULL,
    window_to       TIMESTAMPTZ NOT NULL,
    base_rate       DOUBLE PRECISION NOT NULL,
    bins            JSONB       NOT NULL,   -- [{"lo","hi","n","successes","p"}, ...]
    is_active       BOOLEAN     NOT NULL DEFAULT FALSE,
    notes           TEXT
);
-- Активная кривая на версию логики может быть только одна.
CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_active
    ON calibration_curves (logic_version) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_calibration_built
    ON calibration_curves (logic_version, built_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'signals_calibration_id_fkey'
    ) THEN
        ALTER TABLE signals
            ADD CONSTRAINT signals_calibration_id_fkey
            FOREIGN KEY (calibration_id) REFERENCES calibration_curves(id);
    END IF;
END $$;

-- Учёт выгрузок сигналов наружу (Этап 6.6). Отдельная строка на каждую цель:
-- сбой выгрузки в Notion не блокирует повтор в Sheets и наоборот.
CREATE TABLE IF NOT EXISTS signal_exports (
    id          BIGSERIAL PRIMARY KEY,
    signal_id   BIGINT NOT NULL REFERENCES signals(id),
    target      TEXT NOT NULL,            -- 'sheets' | 'notion'
    exported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (signal_id, target)
);
CREATE INDEX IF NOT EXISTS idx_signal_exports_target ON signal_exports (target, signal_id);

-- Оценка результатов сигналов фактом движения цены (Этап 6).
-- Оценка результата сигнала. Этап 8.1 §5: горизонтов четыре (1, 4, 12, 24 ч),
-- каждый считается независимо, а сигнал остаётся ОДИН. Ключ записи —
-- (сигнал, горизонт в часах). Текстовая колонка horizon сохранена и несёт
-- подпись того же значения ('4h'): её читают выгрузка, бот и суточная сводка.
CREATE TABLE IF NOT EXISTS signal_evaluations (
    signal_id       BIGINT NOT NULL REFERENCES signals(id),
    horizon         TEXT NOT NULL,                 -- 1h | 4h | 12h | 24h
    horizon_h       SMALLINT NOT NULL,             -- тот же горизонт в часах
    price_at_signal DOUBLE PRECISION NOT NULL,
    price_at_close  DOUBLE PRECISION NOT NULL,
    pnl_pct         DOUBLE PRECISION NOT NULL,
    drawdown_pct    DOUBLE PRECISION NOT NULL,
    success         BOOLEAN NOT NULL,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h)
);
CREATE INDEX IF NOT EXISTS ix_eval_horizon
    ON signal_evaluations (horizon_h, evaluated_at);

-- Границы версий логики (Этап 8.1 §6). Данные версий не смешиваются в анализе,
-- поэтому момент перехода хранится машиночитаемо, с точностью до минуты.
CREATE TABLE IF NOT EXISTS logic_version_windows (
    logic_version SMALLINT    PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL,
    note          TEXT
);

-- Учёт сбоев итераций агентов (Этап 7.0). Раньше сбой терялся молча — теперь
-- каждый фиксируется строкой, чтобы его было видно в суточной сводке.
CREATE TABLE IF NOT EXISTS agent_failures (
    id         BIGSERIAL PRIMARY KEY,
    agent      TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_type TEXT NOT NULL,                 -- compute | db_write | auto_reset (Этап 7.2)
    exc_type   TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_failures ON agent_failures (agent, ts DESC);

-- Заключения аналитических агентов (Этап 3).
CREATE TABLE IF NOT EXISTS agent_outputs (
    id            BIGSERIAL PRIMARY KEY,
    agent         TEXT NOT NULL,                 -- market | liquidity | futures
    instrument_id INT NOT NULL REFERENCES instruments(id),
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal        TEXT NOT NULL,                 -- bullish | bearish | neutral | insufficient_data
    confidence    DOUBLE PRECISION NOT NULL DEFAULT 0,
    metrics       JSONB,
    rationale     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_outputs ON agent_outputs (agent, instrument_id, ts DESC);
