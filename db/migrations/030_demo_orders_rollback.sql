-- Откат миграции 030 (этап 9.5).
--
-- ЧТО УДАЛЯЕТСЯ: все три таблицы демо-исполнения целиком, вместе со строками.
-- Таблицы positions, signals, instruments откат не трогает вовсе.
--
-- ПЕРЕД УДАЛЕНИЕМ стоит снять итог, если он ещё нужен:
--   SELECT leg, status, count(*) FROM demo_orders GROUP BY 1, 2 ORDER BY 1, 2;
--   SELECT * FROM demo_balance_daily ORDER BY day;
-- Сначала остановите сервис demo (DEMO_ENABLED=false и перезапуск), иначе он
-- будет писать в таблицу, которой больше нет.
--
-- Ручное применение:
--   cd /opt/agent-trade && docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/030_demo_orders_rollback.sql

BEGIN;

DROP TABLE IF EXISTS demo_balance_daily;
DROP TABLE IF EXISTS demo_orders;
DROP TABLE IF EXISTS demo_state;

COMMIT;
