-- ЭТАП 8.1, решение по §4.3. Поминутные итоги ленты сделок.
--
-- Сырьё (таблица trades) живёт трое суток и удаляется ежесуточной задачей:
-- при пяти токенах оно упирается в потолок коллектора и занимает больше, чем
-- есть на диске. Итоги минуты не удаляются НИКОГДА — это и есть то, что от
-- ленты сделок остаётся навсегда.
--
-- Миграция идемпотентна. Откат — 010_trade_flow_1m_rollback.sql (он удаляет
-- таблицу итогов; восстановить её из сырья старше трёх суток будет уже нечем,
-- поэтому откат делается только сразу после применения).
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/010_trade_flow_1m.sql

BEGIN;

CREATE TABLE IF NOT EXISTS trade_flow_1m (
    instrument_id INTEGER       NOT NULL REFERENCES instruments(id),
    ts            TIMESTAMPTZ   NOT NULL,   -- начало ЗАВЕРШЁННОЙ минуты
    trades_n      INTEGER       NOT NULL,   -- сделок за минуту, все
    buy_volume    NUMERIC(30,8) NOT NULL,   -- объём сделок со стороной buy
    sell_volume   NUMERIC(30,8) NOT NULL,   -- объём сделок со стороной sell
    buy_n         INTEGER       NOT NULL,
    sell_n        INTEGER       NOT NULL,
    vwap          NUMERIC(20,8) NOT NULL,   -- по всем сделкам минуты
    PRIMARY KEY (instrument_id, ts)
);

-- Выборки по времени («что было в этот час») идут по всем инструментам сразу,
-- поэтому индекс по одному ts, а не по паре: первичный ключ уже покрывает
-- обращения вида «инструмент + минута».
CREATE INDEX IF NOT EXISTS ix_trade_flow_1m_ts ON trade_flow_1m (ts);

COMMIT;
