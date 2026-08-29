-- ЭТАП 9.1 §5. Ведение одной позиции (ВИРТУАЛЬНО).
--
-- ЗАЧЕМ ЭТА ТАБЛИЦА. Система оценивает КАЖДЫЙ сигнал по отдельности, независимо
-- от остальных. Это отвечает на вопрос «хороши ли сигналы», но не отвечает на
-- вопрос «что было бы с деньгами». Второй вопрос отличается тем, что деньги
-- конечны: пока они в одной сделке, во вторую войти нечем. Здесь ведётся учёт
-- позиций с правилом «один инструмент — одна позиция»: пять инструментов, пять
-- слотов по 2 доллара.
--
-- ПОЗИЦИИ ВИРТУАЛЬНЫЕ. Ордера на биржу не отправляются, ключи API не читаются,
-- сетевых обращений к бирже код этого этапа не делает вовсе. Столбец
-- is_virtual существует не как задел на будущее, а как проверяемое утверждение:
-- строка с FALSE на этом этапе означала бы, что произошло то, чего этап не
-- обещал, и её ловит deploy/verify_9_1.sh как блокирующую находку.
--
-- ЖЁСТКАЯ ГРАНИЦА ЭТАПА (§0 ТЗ): ни одно решение системы не меняется.
-- LOGIC_VERSION остаётся 5. Таблицы signals, signal_evaluations,
-- signal_targets, risk_targets, signal_outcomes_barrier и trailing_outcomes
-- этой миграцией НЕ ИЗМЕНЯЮТСЯ и колонками не дополняются: внешние ключи
-- смотрят из новой таблицы наружу, а не наоборот.
--
-- ПОЧЕМУ ТОЛЬКО ПОКУПКИ. Спот. Продажа на споте — это продажа того, чего нет.
--
-- Миграция идемпотентна. Откат — 018_positions_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/018_positions.sql

BEGIN;

CREATE TABLE IF NOT EXISTS positions (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INT           NOT NULL REFERENCES instruments(id),
    signal_id           BIGINT        NOT NULL REFERENCES signals(id),
    logic_version       SMALLINT      NOT NULL,
    horizon_h           SMALLINT      NOT NULL,
    side                TEXT          NOT NULL,
    is_virtual          BOOLEAN       NOT NULL DEFAULT TRUE,
    status              TEXT          NOT NULL,
    -- ДВЕ ЦЕНЫ И РАЗНИЦА МЕЖДУ НИМИ — самостоятельный результат этапа.
    -- signal_price — цена в момент решения; entry_price — фактическая цена
    -- входа, то есть close ПОСЛЕДНЕЙ ЗАКРЫТОЙ минутной свечи на момент
    -- открытия. Купить по прошлой цене нельзя, и величина entry_slippage_pct
    -- измеряет, сколько стоит задержка между решением и входом. До сих пор это
    -- число в проекте не измерялось ни разу.
    signal_ts           TIMESTAMPTZ   NOT NULL,
    signal_price        NUMERIC(20,8) NOT NULL,
    opened_at           TIMESTAMPTZ   NOT NULL,
    entry_price         NUMERIC(20,8) NOT NULL,
    entry_lag_sec       INTEGER       NOT NULL,
    entry_slippage_pct  NUMERIC(12,6) NOT NULL,
    qty                 NUMERIC(28,12) NOT NULL,
    notional_usd        NUMERIC(12,4) NOT NULL,
    -- target_pct берётся ЗАМОРОЖЕННЫМ из signal_targets для этого сигнала и
    -- горизонта. Текущее значение из risk_targets брать запрещено: это была бы
    -- подделка истории — сегодняшняя цель посчитана по сегодняшнему рынку.
    target_pct          NUMERIC(10,6) NOT NULL,
    target_price        NUMERIC(20,8) NOT NULL,
    -- stop_pct = BARRIER_STOP_PCT, cost_pct = RISK_COST_ROUNDTRIP_PCT. Своих
    -- ключей этап не заводит: отдельный ключ означал бы возможность сравнить
    -- позиции при одном пределе с исходами Этапа 8.8 при другом, то есть
    -- сравнить несравнимое.
    stop_pct            NUMERIC(10,6) NOT NULL,
    stop_price          NUMERIC(20,8) NOT NULL,
    cost_pct            NUMERIC(10,6) NOT NULL,
    deadline_at         TIMESTAMPTZ   NOT NULL,
    -- Докуда позиция уже разобрана по барам. Без этой отметки каждая итерация
    -- перечитывала бы окно с самого начала.
    last_checked_ts     TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    exit_price          NUMERIC(20,8),
    exit_reason         TEXT,
    -- ПОЧЕМУ У AMBIGUOUS ПЕССИМИСТИЧНЫЙ ИТОГ. Порядок событий внутри минуты
    -- ряду свечей неизвестен. Выбор в свою пользу тихо завысил бы результат
    -- системы; выбор против себя завысить не может. Флаг outcome_certain
    -- позволяет посчитать такие строки отдельно и увидеть, велика ли их доля.
    outcome_certain     BOOLEAN,
    net_pnl_pct         NUMERIC(12,6),
    net_pnl_usd         NUMERIC(14,6),
    bars_held           INTEGER,
    mae_pct             NUMERIC(12,6),
    mfe_pct             NUMERIC(12,6),
    resolution          TEXT          NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Ограничения заводятся ОТДЕЛЬНЫМ блоком с проверкой существования по имени:
-- CREATE TABLE IF NOT EXISTS существующую таблицу не меняет, и на томе, где
-- таблица уже создана, они иначе не появились бы никогда.
DO $$
BEGIN
    -- Этап покупает и только покупает. Ограничение, а не обещание: спот,
    -- продавать нечего.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_side_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_side_chk CHECK (side = 'buy');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_status_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_status_chk
            CHECK (status IN ('open', 'closed'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_reason_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_reason_chk
            CHECK (exit_reason IS NULL OR exit_reason IN
                   ('target', 'stop', 'timeout', 'ambiguous'));
    END IF;

    -- Позиция ведётся только по минутному ряду. Часовой не позволяет
    -- определить порядок касаний, и вести по нему живую позицию значило бы
    -- выдавать догадку за измерение.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_resolution_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_resolution_chk CHECK (resolution = '1m');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_bounds_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_bounds_chk
            CHECK (horizon_h > 0 AND entry_price > 0
                   AND signal_price > 0 AND stop_pct > 0
                   AND qty > 0 AND notional_usd > 0
                   AND logic_version > 0);
    END IF;

    -- Открытая позиция с итогом и закрытая без итога — оба состояния
    -- бессмысленны. Ограничение ловит ошибку расчёта раньше, чем она попадёт
    -- в отчёт.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_shape_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_shape_chk
            CHECK (
                CASE status
                    WHEN 'open'   THEN closed_at IS NULL AND exit_price IS NULL
                                       AND exit_reason IS NULL
                                       AND net_pnl_pct IS NULL
                                       AND outcome_certain IS NULL
                    ELSE               closed_at IS NOT NULL
                                       AND exit_price IS NOT NULL
                                       AND exit_reason IS NOT NULL
                                       AND net_pnl_pct IS NOT NULL
                                       AND outcome_certain IS NOT NULL
                END
            );
    END IF;
END $$;

-- ЭТО ГЛАВНОЕ ПРАВИЛО ЭТАПА, ЗАПИСАННОЕ БАЗОЙ, А НЕ КОДОМ. Проверка в коде
-- переживает ровно до первой гонки: сервис перезапустили, две итерации
-- наложились — и позиций стало две. База такого не допустит вовсе.
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_one_open_per_instrument
    ON positions (instrument_id) WHERE status = 'open';

-- Один сигнал — не более одной позиции, даже после перезапуска и повторного
-- чтения того же сигнала.
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_signal
    ON positions (signal_id);

CREATE INDEX IF NOT EXISTS ix_positions_status_opened
    ON positions (status, opened_at DESC);

-- Право выдаётся ЯВНО, хотя ALTER DEFAULT PRIVILEGES в проекте уже стоит:
-- умолчание действует не для всякого владельца, а бот без права молча
-- перестал бы отвечать на команду /positions.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT SELECT ON positions TO agenttrade_ro;
    END IF;
END $$;

COMMENT ON TABLE positions IS
    'Этап 9.1: ведение одной позиции на инструмент. Позиции ВИРТУАЛЬНЫЕ — '
    'ордера на биржу не отправляются, ключи API не читаются. Ни одно решение '
    'системы от этой таблицы не зависит: LOGIC_VERSION остаётся 5, сервисы '
    'agents, decision, notify, evaluator и risk о ней не знают.';

COMMIT;
