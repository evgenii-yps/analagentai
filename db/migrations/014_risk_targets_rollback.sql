-- Откат миграции 014 (Этап 8.2 §3).
--
-- ВНИМАНИЕ: удаляет ИСТОРИЮ УЖЕ ВЫДАННЫХ ЦЕЛЕЙ (signal_targets). После отката
-- нельзя ответить на вопрос «какую цель система назвала в момент сигнала и
-- сбылась ли она» ни по одному прошлому сигналу: эти данные не восстанавливаются
-- пересчётом, потому что пересчёт даёт СЕГОДНЯШНЮЮ цель, а не вчерашнюю.
--
-- Таблицы продакшна (signals, instruments) откат не затрагивает: внешние ключи
-- смотрят из удаляемых таблиц наружу, а не наоборот.
--
-- Перед откатом на работающей системе снимите копию:
--   docker compose exec -T postgres pg_dump -U agenttrade -d agenttrade \
--       -t risk_targets -t signal_targets > /tmp/risk_targets_backup.sql

BEGIN;

DROP TABLE IF EXISTS signal_targets;
DROP TABLE IF EXISTS risk_targets;

COMMIT;
