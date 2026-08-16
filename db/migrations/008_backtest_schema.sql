-- ЭТАП 7.4. Схема исторического реплея (backtest).
--
-- ВСЁ пишется в ОТДЕЛЬНУЮ схему backtest того же экземпляра PostgreSQL.
-- Ни одна таблица продакшна не изменяется. Откат — 008_backtest_schema_rollback.sql
-- (DROP SCHEMA backtest CASCADE), продакшн им не затрагивается.
--
-- Расположение файла: репозиторий уже хранит миграции в db/migrations/
-- (007_calibration_inertia.sql), поэтому файл положен туда же, а не в отдельный
-- каталог migrations/ из §7 ТЗ. Отклонение зафиксировано в docs/STAGE_7_4_REPORT.md.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/008_backtest_schema.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS backtest;

CREATE TABLE IF NOT EXISTS backtest.candles (
    inst_id     TEXT          NOT NULL,
    bar         TEXT          NOT NULL,
    open_time   TIMESTAMPTZ   NOT NULL,
    close_time  TIMESTAMPTZ   NOT NULL,
    open        NUMERIC(20,8) NOT NULL,
    high        NUMERIC(20,8) NOT NULL,
    low         NUMERIC(20,8) NOT NULL,
    close       NUMERIC(20,8) NOT NULL,
    volume      NUMERIC(30,8) NOT NULL,
    volume_ccy  NUMERIC(30,8),
    fetched_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (inst_id, bar, open_time)
);
CREATE INDEX IF NOT EXISTS ix_bt_candles_close
    ON backtest.candles (inst_id, bar, close_time);

CREATE TABLE IF NOT EXISTS backtest.funding (
    inst_id      TEXT           NOT NULL,
    funding_time TIMESTAMPTZ    NOT NULL,
    funding_rate NUMERIC(20,10) NOT NULL,
    fetched_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (inst_id, funding_time)
);

CREATE TABLE IF NOT EXISTS backtest.gaps (
    inst_id    TEXT        NOT NULL,
    series     TEXT        NOT NULL,   -- candles | funding
    gap_from   TIMESTAMPTZ NOT NULL,
    gap_to     TIMESTAMPTZ NOT NULL,
    missing_n  INTEGER     NOT NULL,
    PRIMARY KEY (inst_id, series, gap_from)
);

CREATE TABLE IF NOT EXISTS backtest.runs (
    run_id        BIGSERIAL   PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    code_commit   TEXT        NOT NULL,
    agents_used   TEXT[]      NOT NULL,
    config_json   JSONB       NOT NULL,   -- включает предрегистрированный критерий §6
    period_from   TIMESTAMPTZ NOT NULL,
    period_to     TIMESTAMPTZ NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','failed'))
);

CREATE TABLE IF NOT EXISTS backtest.decisions (
    run_id         BIGINT        NOT NULL
        REFERENCES backtest.runs(run_id) ON DELETE CASCADE,
    inst_id        TEXT          NOT NULL,
    ts             TIMESTAMPTZ   NOT NULL,
    direction      TEXT          NOT NULL CHECK (direction IN ('buy','sell','wait')),
    score          NUMERIC(12,8) NOT NULL,
    probability    NUMERIC(12,8) NOT NULL,
    agreement      NUMERIC(12,8),
    agents_payload JSONB         NOT NULL,
    price_at_ts    NUMERIC(20,8) NOT NULL,
    PRIMARY KEY (run_id, inst_id, ts)
);

CREATE TABLE IF NOT EXISTS backtest.outcomes (
    run_id         BIGINT        NOT NULL,
    inst_id        TEXT          NOT NULL,
    ts             TIMESTAMPTZ   NOT NULL,
    horizon_h      SMALLINT      NOT NULL,
    price_end      NUMERIC(20,8) NOT NULL,
    gross_pnl_pct  NUMERIC(12,6) NOT NULL,
    net_pnl_pct    NUMERIC(12,6) NOT NULL,
    direction_hit  BOOLEAN       NOT NULL,
    is_independent BOOLEAN       NOT NULL,
    is_oos         BOOLEAN       NOT NULL,
    regime         TEXT          NOT NULL CHECK (regime IN ('up','down','flat')),
    vol_quartile   SMALLINT      NOT NULL CHECK (vol_quartile BETWEEN 1 AND 4),
    PRIMARY KEY (run_id, inst_id, ts, horizon_h),
    FOREIGN KEY (run_id, inst_id, ts)
        REFERENCES backtest.decisions(run_id, inst_id, ts) ON DELETE CASCADE
);

-- Роль прогона: запись ТОЛЬКО в схему backtest, чтение продакшн-таблиц
-- (нужно для обязательной сверки с живой системой, §13.2 ТЗ). Роль создаётся
-- отдельно (см. deploy/verify_7_4.sh и docs/STAGE_7_4_REPORT.md) — здесь только
-- выдача прав, если она уже существует.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_bt') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA backtest TO agenttrade_bt';
        EXECUTE 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA backtest TO agenttrade_bt';
        EXECUTE 'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA backtest TO agenttrade_bt';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA backtest '
                'GRANT ALL PRIVILEGES ON TABLES TO agenttrade_bt';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA backtest '
                'GRANT ALL PRIVILEGES ON SEQUENCES TO agenttrade_bt';
        -- Продакшн: ТОЛЬКО чтение. Права на запись не выдаются нигде.
        EXECUTE 'GRANT USAGE ON SCHEMA public TO agenttrade_bt';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO agenttrade_bt';
    END IF;
END $$;

COMMIT;
