-- Откат Замера 0 (шаг 1 версии 7): снятие того, что добавила миграция 026.
--
-- ЧТО ЭТОТ ОТКАТ ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ. Он снимает ограничения, индекс и
-- колонку происхождения. Он НЕ УДАЛЯЕТ ЗАГРУЖЕННЫЕ СТРОКИ со старшими
-- масштабами: удаление данных — это отдельное решение, а не побочный эффект
-- отката схемы. Строки лежат в схеме backtest и ни на одно решение системы не
-- влияют (все читатели фильтруют по ``bar`` — перечень в тексте миграции 026).
--
-- Если строки нужно убрать тоже, это делается ЯВНО и отдельной командой,
-- напечатанной здесь, а не спрятанной в откат:
--
--   DELETE FROM backtest.candles WHERE bar IN ('1Dutc','1Wutc','1Mutc');
--
-- Миграция ИДЕМПОТЕНТНА: повторное применение ничего не меняет.

BEGIN;

DROP INDEX IF EXISTS backtest.ix_bt_candles_htf_close;

ALTER TABLE backtest.candles DROP CONSTRAINT IF EXISTS candles_htf_period;
ALTER TABLE backtest.candles DROP CONSTRAINT IF EXISTS candles_htf_ohlc;
ALTER TABLE backtest.candles DROP CONSTRAINT IF EXISTS candles_htf_bar;

ALTER TABLE backtest.candles DROP COLUMN IF EXISTS source;

COMMIT;
