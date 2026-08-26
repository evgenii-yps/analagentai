-- Откат миграции 016 (Этап 8.9 §6).
--
-- ВНИМАНИЕ: удаляет все посчитанные исходы базовых стратегий. Потеря не
-- катастрофична — таблица восстанавливается полным пересчётом
-- (src/baseline_main.py) по signal_outcomes_barrier, историческим целям
-- risk_targets и свечам. Но восстановление возможно ровно до тех пор, пока
-- живы минутные свечи: политика хранения удаляет ohlcv timeframe='1m' старше
-- RETENTION_1M_DAYS, и после отката пересчитанные строки получат
-- resolution='1h' вместо '1m' — то есть станут ГРУБЕЕ прежних.
--
-- Перед откатом на работающей системе снимите копию:
--   docker compose exec -T postgres pg_dump -U agenttrade -d agenttrade \
--       -t strategy_outcomes > /tmp/strategy_outcomes_backup.sql
--
-- Таблицы продакшна откат не затрагивает: внешние ключи смотрят из удаляемой
-- таблицы наружу, а не наоборот.

BEGIN;

DROP TABLE IF EXISTS strategy_outcomes;

COMMIT;
