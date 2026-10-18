-- ЭТАП 9.10. Право чтения исторических свечей для роли agenttrade_ro.
--
-- ЗАЧЕМ. Замер «вето на покупку при нисходящем тренде BTC» (analysis/run_9_10.sh) строит
-- дневной ряд из часовых свечей: живые лежат в public.ohlcv (роль agenttrade_ro их читает),
-- историю с 2022 года хранит backtest.candles (загружена в Замере 0). До этой миграции у
-- роли нет прав на схему backtest, и замер упал бы на первом же обращении к истории.
--
-- ЧТО ВЫДАЁТСЯ. Только USAGE на схему backtest и SELECT на ОДНУ таблицу backtest.candles.
-- Больше ничего: ни другие таблицы схемы, ни права записи. Продакшн-логика, сервисы и
-- LOGIC_VERSION миграцией не затрагиваются.
--
-- Блок условный: роль agenttrade_ro создаёт сервис бота (Этап 6.7); если её ещё нет,
-- миграция ничего не делает и не падает. Выдача идемпотентна. Откат —
-- 032_ro_backtest_read_rollback.sql.
--
-- Ручное применение (каталог db/migrations в контейнер postgres НЕ смонтирован — там только
-- db/init.sql, см. docker-compose.yml; файл подаётся с хоста через stdin):
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/032_ro_backtest_read.sql

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
        GRANT USAGE ON SCHEMA backtest TO agenttrade_ro;
        GRANT SELECT ON backtest.candles TO agenttrade_ro;
    END IF;
END $$;

COMMIT;
