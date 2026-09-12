-- ЭТАП 9.3 §7.1. Журнал ОТКАЗОВ ВО ВХОДЕ.
--
-- ЧТО ПРОВЕРЕНО ПЕРЕД ТЕМ, КАК ЗАВОДИТЬ ТАБЛИЦУ (§7.1 ТЗ прямо требует
-- проверить, а не предполагать). Хранилище отказов в проекте ЕСТЬ, но оно
-- другой природы: суточные счётчики в Redis с ключом
-- ``positions:refused:<версия>:<ГГГГ-ММ-ДД>:<причина>`` и сроком жизни неделю
-- (``rules.refusal_key``, этап 9.2 §7.2). Они отвечают на вопрос «сколько», и
-- отвечают хорошо. Они НЕ отвечают на вопросы, которые ставит этот этап:
-- по какому токену, по какому сигналу, в какую минуту — и что было неделю
-- назад. Поэтому таблица заводится, а счётчики Redis ОСТАЮТСЯ на месте: они
-- переживают недоступность базы и читаются ботом мгновенно.
--
-- ПОЧЕМУ ЭТО ТАБЛИЦА, А НЕ ТОЛЬКО СТРОКИ ЖУРНАЛА. Строка журнала
-- ``positions_skipped=1`` существует с этапа 9.1 и никуда не девается. Но
-- журнал контейнера — это не выборка: посчитать по нему долю отказов
-- ``token_pause`` среди прошедших порог (предсказание §12.2 ТЗ: не меньше 70%)
-- можно лишь разбором текста, и ровно это уже один раз сделало пункт проверки
-- неработающим (§14.5 ТЗ про подстановки в оболочке).
--
-- ЧЕГО ЗДЕСЬ НЕТ. Таблица ``positions`` не меняется НИ ОДНИМ столбцом (§7.2
-- ТЗ): число открытых позиций — это ``COUNT(*) WHERE closed_at IS NULL``, а не
-- хранимая величина. Строки версий 5 и 6 эта миграция не читает и не трогает.
--
-- ПЕРЕЧЕНЬ ПРИЧИН ЗАКРЫТ ОГРАНИЧЕНИЕМ, А НЕ СОГЛАШЕНИЕМ. Причина, названная
-- своими словами, не считается причиной — её нельзя посчитать запросом. Список
-- повторяет ``rules.REFUSAL_REASONS`` и обязан с ним совпадать; совпадение
-- проверяется тестом, а не памятью.
--
-- СЛОВАРЬ ТЗ И СЛОВАРЬ КОДА РАСХОДЯТСЯ, И ЭТО РАЗРЕШЕНО §3 ТЗ («исполнитель
-- использует существующие имена»): 'no_quorum' → degraded,
-- 'logic_version_mismatch' → wrong_logic_version, 'cooldown' → token_pause,
-- 'budget' → no_free_capital, 'stale_candle' → signal_too_old И no_fresh_bar
-- (два разных события: молчала система или молчал коллектор). Сверх словаря
-- ТЗ — no_frozen_target (цель этапа 8.2 не заморожена) и instrument_busy
-- (выключатель инварианта §4.1).
--
-- ОБЪЁМ И ПРОПОЛКА. При ~7200 решениях в сутки и отказе почти на каждом
-- таблица растёт примерно на 2,5 млн строк в год (§7.1 ТЗ). Прополка
-- обязательна и написана в этом же этапе: ``scripts/retention.py`` удаляет
-- строки старше RETENTION_POSITION_REJECTIONS_DAYS (90) суток.
--
-- Миграция идемпотентна. Откат — 028_position_rejections_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--     -f /docker-entrypoint-initdb.d/migrations/028_position_rejections.sql

BEGIN;

CREATE TABLE IF NOT EXISTS public.position_rejections (
    id              BIGSERIAL PRIMARY KEY,
    ts_utc          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    signal_id       BIGINT       NOT NULL,
    token           TEXT         NOT NULL,
    reason          TEXT         NOT NULL,
    logic_version   SMALLINT     NOT NULL,
    CONSTRAINT position_rejections_reason_chk CHECK (
        reason IN ('low_probability','not_buy','degraded','no_fresh_bar',
                   'signal_too_old','no_frozen_target','wrong_logic_version',
                   'token_pause','instrument_busy','max_open','no_free_capital')
    )
);

-- ВНЕШНЕГО КЛЮЧА НА signals ЗДЕСЬ НЕТ НАМЕРЕННО. Отказы живут 90 суток, а
-- сигналы — дольше и по своим правилам; связав их ключом, мы сделали бы срок
-- хранения сигналов заложником срока хранения отказов, и любая будущая
-- прополка signals падала бы на этой таблице.
CREATE INDEX IF NOT EXISTS position_rejections_ts_idx
    ON public.position_rejections (ts_utc);
CREATE INDEX IF NOT EXISTS position_rejections_reason_idx
    ON public.position_rejections (reason, logic_version);

COMMENT ON TABLE public.position_rejections IS
    'Отказы во входе в сделку (этап 9.3 §7.1): по одной строке на отвергнутого '
    'кандидата. Отвечает на вопрос «почему сделок мало» поимённо — по токену, '
    'сигналу и минуте. Суточные счётчики Redis (positions:refused:*) остаются '
    'и отвечают на вопрос «сколько» при недоступной базе. Прополка: строки '
    'старше RETENTION_POSITION_REJECTIONS_DAYS суток удаляет scripts/retention.py.';

COMMENT ON COLUMN public.position_rejections.token IS
    'Символ инструмента (BTC/USDT), а не instrument_id: строка обязана '
    'читаться человеком без соединения с instruments, в том числе после '
    'удаления инструмента из перечня.';

COMMIT;
