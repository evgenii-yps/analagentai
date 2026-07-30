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
    notified       BOOLEAN NOT NULL DEFAULT FALSE -- отправлено ли уведомление (Этап 5)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);

-- Оценка результатов сигналов фактом движения цены (Этап 6).
CREATE TABLE IF NOT EXISTS signal_evaluations (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT NOT NULL REFERENCES signals(id),
    horizon         TEXT NOT NULL,                 -- 1h | 4h
    price_at_signal DOUBLE PRECISION NOT NULL,
    price_at_close  DOUBLE PRECISION NOT NULL,
    pnl_pct         DOUBLE PRECISION NOT NULL,
    drawdown_pct    DOUBLE PRECISION NOT NULL,
    success         BOOLEAN NOT NULL,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (signal_id, horizon)
);

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

-- Аудит доступности бирж (Этап 6.4).
-- Результат воспроизводимой проверки публичных эндпоинтов каждой биржи.
CREATE TABLE IF NOT EXISTS exchange_audit (
    id              BIGSERIAL PRIMARY KEY,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip_label TEXT        NOT NULL,        -- метка машины: local | hetzner-nbg
    exchange        TEXT        NOT NULL,
    endpoint        TEXT        NOT NULL,        -- tickers | instruments | ws
    http_status     INTEGER,
    latency_ms      INTEGER,
    geo_blocked     BOOLEAN     NOT NULL,
    ws_available    BOOLEAN,
    rate_limit_note TEXT,
    error_text      TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_exchange_audit_lookup
    ON exchange_audit (exchange, checked_at DESC);

-- Аудит торговых пар на биржах (Этап 6.4).
-- Тип NUMERIC(28,14) выбран сознательно: цена токенов вроде DENT (~0.0000277)
-- при обычной точности NUMERIC(18,8) молча округлялась бы в ноль.
CREATE TABLE IF NOT EXISTS pair_audit (
    id                 BIGSERIAL PRIMARY KEY,
    checked_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    source_ip_label    TEXT          NOT NULL,
    exchange           TEXT          NOT NULL,
    symbol             TEXT          NOT NULL,
    listed             BOOLEAN       NOT NULL,
    last_price         NUMERIC(28,14),
    bid                NUMERIC(28,14),
    ask                NUMERIC(28,14),
    spread_pct         NUMERIC(10,4),
    depth_bid_2pct_usd NUMERIC(18,2),
    depth_ask_2pct_usd NUMERIC(18,2),
    vol_24h_usd        NUMERIC(18,2),
    tick_size          NUMERIC(28,14),
    min_order_usd      NUMERIC(18,2),
    verdict            TEXT          NOT NULL,   -- not_listed | illiquid | tradable
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_pair_audit_lookup
    ON pair_audit (symbol, exchange, checked_at DESC);
