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
    success        BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);
