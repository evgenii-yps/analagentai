-- Откат миграции 029 (этап 9.4, блок D).
--
-- ЧТО УДАЛЯЕТСЯ: пять таблиц, созданных миграцией 029, вместе со строками. Таблицы
-- trailing_outcomes и strategy_outcomes откат НЕ ТРОГАЕТ.
--
-- ЧЕГО ОН НЕ ДЕЛАЕТ. Если детали уже были удалены (retention.py --measure-delete),
-- этот файл их не вернёт: SQL-пути возврата удалённых строк нет. Детали
-- восстанавливаются из резервной копии, снятой ПЕРЕД первым удалением и хранимой
-- вне ротации 30 суток (docs/STAGE_9_4_REPORT.md, «Откат блока D»).
-- ОСТОРОЖНО: удалив свёртки после удаления деталей, вы теряете и то, и другое.
-- Прежде чем применять, убедитесь, что копия цела:
--   ls -l /opt/agent-trade/backups/manual/
--
-- Ручное применение (каталог db/migrations в контейнер НЕ смонтирован — файл
-- подаётся с хоста через stdin):
--   cd /opt/agent-trade && sudo -u agent docker compose exec -T postgres \
--       psql -U agenttrade -d agenttrade < db/migrations/029_measure_rollups_rollback.sql

BEGIN;

DROP TABLE IF EXISTS public.measure_rollup_state;
DROP TABLE IF EXISTS public.strategy_pairs_compact;
DROP TABLE IF EXISTS public.trailing_pairs_compact;
DROP TABLE IF EXISTS public.strategy_outcomes_daily;
DROP TABLE IF EXISTS public.trailing_outcomes_daily;

COMMIT;
