-- ЭТАП 9.4, БЛОК D. Свёртка таблиц замеров trailing_outcomes и strategy_outcomes.
--
-- ЗАЧЕМ. Две таблицы дают ~9 ГБ роста в год без верхней границы: trailing_outcomes
-- (замер 9.1.3 ЗАВЕРШЁН) и strategy_outcomes (замер 8.9 продолжается). Детали
-- сворачиваются в суточные итоги и компактные таблицы пар, после чего детали
-- удаляются по правилу scripts/retention.py (см. scripts/measure_rollup.py).
--
-- ЧТО ЗДЕСЬ СОЗДАЁТСЯ (только новые таблицы; существующие не изменяются ни
-- колонкой, ни ограничением):
--   * trailing_outcomes_daily — сутки × инструмент × logic_version × горизонт ×
--     (activation_ratio, retrace_ratio). День — по времени СИГНАЛА (signals.ts,
--     UTC), а не по computed_at.
--   * strategy_outcomes_daily — сутки × инструмент × logic_version × стратегия ×
--     горизонт. День — по entry_ts (UTC).
--   * trailing_pairs_compact — по строке на пару (сигнал, горизонт): тринадцать
--     итогов вариантов выхода в миллионных долях процента (INT4, без потери
--     точности: net_pnl_pct хранится с шестью знаками). Достаточно для
--     парного бутстрэпа, перестановочной проверки и проверки на независимой
--     половине (scripts/trailing_stats.py, scripts/trailing_resample_9_1_3.py).
--   * strategy_pairs_compact — по строке на пару (сигнал, горизонт): итоги четырёх
--     привязанных к сигналу стратегий (system, always_buy, always_sell,
--     coin_flip) для попарного сравнения (scripts/baseline_bootstrap.py).
--   * measure_rollup_state — одна служебная строка: «удаление деталей разрешено».
--     Флаг ставится ТОЛЬКО командой retention.py --measure-delete после
--     подтверждения человеком; при восстановлении базы из копии он возвращается
--     вместе с данными.
--
-- LOGIC_VERSION В КЛЮЧЕ ОБЯЗАТЕЛЕН: сутки, на которые пришлась смена версии, дают
-- две строки, а не одну смешанную.
--
-- Миграция идемпотентна. Откат — 029_measure_rollups_rollback.sql (удаляет ТОЛЬКО
-- эти таблицы; удалённые детали SQL не вернёт — они восстанавливаются из
-- резервной копии, снятой перед первым удалением).
--
-- Ручное применение. Каталог db/migrations в контейнер postgres НЕ смонтирован
-- (смонтирован только db/init.sql — дефект, выявленный на этапе 9.3), поэтому
-- путь /docker-entrypoint-initdb.d/migrations/ здесь не годится. Файл подаётся
-- с хоста через stdin — это и есть рабочий путь:
--   cd /opt/agent-trade && sudo -u agent docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/029_measure_rollups.sql

BEGIN;

CREATE TABLE IF NOT EXISTS trailing_outcomes_daily (
    day               DATE          NOT NULL,
    instrument_id     INT           NOT NULL REFERENCES instruments(id),
    logic_version     SMALLINT      NOT NULL,
    horizon_h         SMALLINT      NOT NULL,
    activation_ratio  NUMERIC(4,2)  NOT NULL,
    retrace_ratio     NUMERIC(4,2)  NOT NULL,
    n_rows            INT           NOT NULL,   -- число наблюдений (строк деталей)
    -- Счётчики по каждому значению exit_reason отдельными колонками.
    n_target          INT           NOT NULL,
    n_stop            INT           NOT NULL,
    n_trail           INT           NOT NULL,
    n_timeout         INT           NOT NULL,
    n_ambiguous       INT           NOT NULL,
    n_no_data         INT           NOT NULL,
    n_res_1m          INT           NOT NULL,   -- разбивка по resolution
    n_res_1h          INT           NOT NULL,
    n_buy             INT           NOT NULL,   -- разбивка по direction
    n_sell            INT           NOT NULL,
    n_pnl             INT           NOT NULL,   -- строк с net_pnl_pct (вес для средних)
    avg_net_pnl       NUMERIC(12,6),
    p25_net_pnl       NUMERIC(12,6),
    p50_net_pnl       NUMERIC(12,6),
    p75_net_pnl       NUMERIC(12,6),
    avg_peak_pct      NUMERIC(12,6),
    avg_mae_pct       NUMERIC(12,6),
    avg_mfe_pct       NUMERIC(12,6),
    p50_bars_to_hit   NUMERIC(12,2),
    PRIMARY KEY (day, instrument_id, logic_version, horizon_h,
                 activation_ratio, retrace_ratio)
);

CREATE TABLE IF NOT EXISTS strategy_outcomes_daily (
    day               DATE          NOT NULL,
    instrument_id     INT           NOT NULL REFERENCES instruments(id),
    logic_version     SMALLINT      NOT NULL,
    strategy          TEXT          NOT NULL,
    horizon_h         SMALLINT      NOT NULL,
    n_rows            INT           NOT NULL,
    n_target          INT           NOT NULL,   -- счётчики по outcome колонками
    n_stop            INT           NOT NULL,
    n_timeout         INT           NOT NULL,
    n_ambiguous       INT           NOT NULL,
    n_no_data         INT           NOT NULL,
    n_res_1m          INT           NOT NULL,
    n_res_1h          INT           NOT NULL,
    n_buy             INT           NOT NULL,
    n_sell            INT           NOT NULL,
    n_pnl             INT           NOT NULL,
    avg_net_pnl       NUMERIC(12,6),
    p25_net_pnl       NUMERIC(12,6),
    p50_net_pnl       NUMERIC(12,6),
    p75_net_pnl       NUMERIC(12,6),
    avg_mae_pct       NUMERIC(12,6),
    avg_mfe_pct       NUMERIC(12,6),
    PRIMARY KEY (day, instrument_id, logic_version, strategy, horizon_h)
);

-- Компактная таблица пар trailing. Порядок вариантов v00..v12 фиксирован
-- ``src.trailing.rule.VARIANTS``: (0,0), затем (0.25,0.20), (0.25,0.33),
-- (0.25,0.50), (0.50,0.20), ... (1.00,0.50). Значение — net_pnl_pct * 1 000 000,
-- целое (точное представление NUMERIC(12,6)). NULL — у пары с exclude_reason.
-- exclude_reason повторяет правило scripts/trailing_stats.collect ДОСЛОВНО и в
-- том же порядке проверок: incomplete | no_data | ambiguous | NULL (пара полная).
CREATE TABLE IF NOT EXISTS trailing_pairs_compact (
    day             DATE          NOT NULL,   -- день сигнала (UTC): сверка со свёрткой
    signal_id       BIGINT        NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT      NOT NULL,
    logic_version   SMALLINT      NOT NULL,
    n_rows          SMALLINT      NOT NULL,   -- сколько строк деталей было у пары
    computed_at     TIMESTAMPTZ   NOT NULL,   -- самый ранний computed_at пары (граница 9.1.3)
    exclude_reason  TEXT,
    v00 INT, v01 INT, v02 INT, v03 INT, v04 INT, v05 INT, v06 INT,
    v07 INT, v08 INT, v09 INT, v10 INT, v11 INT, v12 INT,
    PRIMARY KEY (signal_id, horizon_h)
);

-- Компактная таблица пар strategy: итоги в миллионных долях процента; NULL —
-- нет строки стратегии у пары или итога нет (ambiguous / no_data).
-- Сетки (grid_buy, grid_sell) к сигналам не привязаны и попарно не сравниваются —
-- их несут только суточные итоги.
CREATE TABLE IF NOT EXISTS strategy_pairs_compact (
    day             DATE          NOT NULL,   -- день входа (UTC): сверка со свёрткой
    signal_id       BIGINT        NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT      NOT NULL,
    logic_version   SMALLINT      NOT NULL,
    system_pnl      INT,
    always_buy_pnl  INT,
    always_sell_pnl INT,
    coin_flip_pnl   INT,
    PRIMARY KEY (signal_id, horizon_h, logic_version)
);

CREATE TABLE IF NOT EXISTS measure_rollup_state (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE trailing_outcomes_daily IS
    'Этап 9.4 D: суточная свёртка trailing_outcomes. Вечная; детали удаляются после свёртки.';
COMMENT ON TABLE strategy_outcomes_daily IS
    'Этап 9.4 D: суточная свёртка strategy_outcomes. Вечная.';
COMMENT ON TABLE trailing_pairs_compact IS
    'Этап 9.4 D: пары (сигнал, горизонт) с тринадцатью итогами вариантов выхода — точное воспроизведение защит от подгонки 9.1.3.';
COMMENT ON TABLE strategy_pairs_compact IS
    'Этап 9.4 D: пары (сигнал, горизонт) с итогами четырёх привязанных к сигналу стратегий — попарное сравнение с системой.';

COMMIT;
