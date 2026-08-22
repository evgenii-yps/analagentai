-- ЭТАП 8.1. Суточная свёртка выводов агентов (решение заказчика, пункт 1).
--
-- Сырой журнал (agent_outputs) живёт 90 суток: logic_version меняется почти
-- каждый этап (версия 4 прожила шесть дней), смешивать версии в анализе
-- запрещено, поэтому выводы старше 90 суток почти всегда относятся к
-- устаревшей версии логики и аналитически непригодны. Суточные итоги
-- не удаляются НИКОГДА — по ним видно поведение агентов на всей истории.
--
-- LOGIC_VERSION В КЛЮЧЕ ОБЯЗАТЕЛЕН: сутки, на которые пришлась граница версий,
-- дают ДВЕ строки, а не одну смешанную. Иначе в одной ячейке оказались бы
-- выводы двух разных систем.
--
-- Миграция идемпотентна. Откат — 011_agent_outputs_daily_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/011_agent_outputs_daily.sql

BEGIN;

CREATE TABLE IF NOT EXISTS agent_outputs_daily (
    day            DATE          NOT NULL,
    agent          TEXT          NOT NULL,
    instrument_id  INTEGER       NOT NULL REFERENCES instruments(id),
    logic_version  SMALLINT      NOT NULL,
    n_total        INTEGER       NOT NULL,   -- все выводы суток, включая insufficient_data
    n_bullish      INTEGER       NOT NULL,
    n_bearish      INTEGER       NOT NULL,
    n_neutral      INTEGER       NOT NULL,
    conf_avg       NUMERIC(10,6) NOT NULL,
    conf_p50       NUMERIC(10,6) NOT NULL,
    conf_p90       NUMERIC(10,6) NOT NULL,
    repeat_rate    NUMERIC(5,4)  NOT NULL,   -- доля полных повторов предыдущего вывода
    PRIMARY KEY (day, agent, instrument_id, logic_version)
);

-- Выборки «как вёл себя агент за период» идут по агенту и дню сразу по всем
-- инструментам; первичный ключ такой запрос не покрывает.
CREATE INDEX IF NOT EXISTS ix_agent_outputs_daily_agent
    ON agent_outputs_daily (agent, day);

COMMIT;
