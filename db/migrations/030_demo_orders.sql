-- ЭТАП 9.5. Демо-исполнение зеркалом (OKX, спот): журнал ордеров демо-счёта.
--
-- ЗАЧЕМ. Каждая виртуальная сделка дублируется настоящим рыночным ордером на
-- ДЕМО-счёте OKX (только спот, только покупка и последующая продажа). Деньги не
-- настоящие; цель — измерить, сколько съедает реальное исполнение:
-- проскальзывание, задержку и комиссию. Эта миграция заводит ДВЕ новые таблицы;
-- существующие (positions, signals, instruments ...) не изменяются ни колонкой,
-- ни ограничением.
--
--   * demo_state  — одна служебная строка: с какого момента сервис зеркалит
--     позиции (mirror_since) и на каком хосте OKX. Позиции, открытые ДО этого
--     момента, не зеркалятся никогда: иначе пришлось бы продавать то, что не
--     покупали.
--   * demo_orders — по строке на ногу сделки (покупка / продажа) на позицию.
--     Уникальность (position_id, leg) и cl_ord_id — это и есть защита от двойной
--     отправки: сначала строка, потом ордер.
--
-- ТИПЫ КЛЮЧЕЙ СВЕРЕНЫ с фактическим DDL в main: positions.id — BIGSERIAL
-- (BIGINT), instruments.id — SERIAL (INT).
--
-- Роль только для чтения agenttrade_ro получает SELECT на обе таблицы (если
-- роль существует на этом сервере — как в миграциях 018, 021–024).
--
-- Миграция идемпотентна. Откат — 030_demo_orders_rollback.sql.
--
-- Ручное применение (каталог db/migrations в контейнер postgres НЕ смонтирован,
-- файл подаётся с хоста через stdin):
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/030_demo_orders.sql

BEGIN;

CREATE TABLE IF NOT EXISTS demo_state (
    id            SMALLINT PRIMARY KEY CHECK (id = 1),
    mirror_since  TIMESTAMPTZ NOT NULL,
    host          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS demo_orders (
    id                 BIGSERIAL PRIMARY KEY,
    position_id        BIGINT        NOT NULL REFERENCES positions(id),
    instrument_id      INT           NOT NULL REFERENCES instruments(id),
    leg                TEXT          NOT NULL CHECK (leg IN ('buy','sell')),
    status             TEXT          NOT NULL CHECK (status IN
                         ('pending','sent','filled','rejected','skipped','dust','lost')),
    skip_reason        TEXT,
    cl_ord_id          TEXT          NOT NULL UNIQUE,
    exchange_order_id  TEXT,
    symbol             TEXT          NOT NULL,
    host               TEXT          NOT NULL,
    requested_cost_usd NUMERIC(14,6),
    requested_qty      NUMERIC(28,12),
    filled_qty         NUMERIC(28,12),
    avg_price          NUMERIC(20,8),
    cost_usd           NUMERIC(14,6),
    fee                NUMERIC(28,12),
    fee_ccy            TEXT,
    virtual_price      NUMERIC(20,8),
    virtual_ts         TIMESTAMPTZ   NOT NULL,
    slippage_pct       NUMERIC(12,6),
    sent_at            TIMESTAMPTZ,
    filled_at          TIMESTAMPTZ,
    lag_sec            INTEGER,
    error_code         TEXT,
    error_text         TEXT,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT demo_orders_one_leg UNIQUE (position_id, leg),
    CONSTRAINT demo_orders_skip_chk CHECK
        ((status IN ('skipped','dust')) = (skip_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS demo_orders_status_idx ON demo_orders (status);
CREATE INDEX IF NOT EXISTS demo_orders_created_idx ON demo_orders (created_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON demo_state, demo_orders TO agenttrade_ro;
    END IF;
END $$;

COMMIT;
