-- ЭТАП 8.3 §1. Настройки пользователя: какие токены, какой горизонт показывать,
-- с какой силы уведомлять и когда молчать.
--
-- ЗАЧЕМ. Пять токенов работают в продакшне с 23.08.2026. Без отбора человек
-- получает всё подряд по всем пяти, и уведомление перестаёт быть уведомлением.
--
-- Ключ — chat_id Telegram: настройки принадлежат человеку, а не системе.
-- Записи может не быть вовсе, и это НЕ ошибка: человек мог ни разу не открыть
-- меню. В таком случае действуют значения по умолчанию (§1 ТЗ), заданные
-- кодом, — подставлять строку в базу при первом же сигнале не нужно.
--
-- instruments — массив идентификаторов инструментов, а не названий токенов:
-- имя пары может смениться (BTC/USDT → BTC/USDC), идентификатор — нет.
--
-- quiet_from / quiet_to — часы UTC; NULL в обоих означает «тишина выключена».
-- Диапазон может пересекать полночь (22 → 6), поэтому проверка на стороне
-- кода, а не ограничением quiet_from < quiet_to.
--
-- Миграция идемпотентна. Откат — 013_user_settings_rollback.sql.
--
-- Ручное применение:
--   docker compose exec -T postgres psql -U agenttrade -d agenttrade \
--       < db/migrations/013_user_settings.sql

BEGIN;

CREATE TABLE IF NOT EXISTS user_settings (
    chat_id     BIGINT       PRIMARY KEY,
    instruments INTEGER[]    NOT NULL,
    horizon_h   SMALLINT     NOT NULL DEFAULT 4,
    min_score   NUMERIC(4,3) NOT NULL DEFAULT 0.700,
    quiet_from  SMALLINT,
    quiet_to    SMALLINT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Проверки значений: неверный горизонт или час молча отсекал бы уведомления,
-- а человек видел бы тишину и считал систему сломанной.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'user_settings'::regclass
           AND conname = 'user_settings_quiet_hours_valid'
    ) THEN
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_quiet_hours_valid CHECK (
                (quiet_from IS NULL AND quiet_to IS NULL)
                OR (quiet_from BETWEEN 0 AND 23 AND quiet_to BETWEEN 0 AND 23)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'user_settings'::regclass
           AND conname = 'user_settings_horizon_positive'
    ) THEN
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_horizon_positive CHECK (horizon_h > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'user_settings'::regclass
           AND conname = 'user_settings_instruments_not_empty'
    ) THEN
        -- §1 ТЗ: токенов минимум один. Пустой массив означал бы «не слать
        -- ничего никогда», и человек не отличил бы это от поломки.
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_instruments_not_empty
            CHECK (array_length(instruments, 1) >= 1);
    END IF;
END $$;

COMMENT ON TABLE user_settings IS
    'Настройки уведомлений по чату Telegram (Этап 8.3 §1). Отсутствие строки — '
    'не ошибка: действуют значения по умолчанию из кода.';

COMMIT;
