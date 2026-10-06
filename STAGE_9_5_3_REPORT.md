9.5.3 редакция 1

# Этап 9.5.3 — убрать из листа владельца позиции до запуска демо

Основа — main `108ba4f` (PR #16), ветка `claude/wonderful-volta-x9b0a3` перезапущена от него (PR #16 уже влит).

## Что было не так
`src/export/demo_owner_sheet.py` писал в лист «торговля демо апи окх» и позиции со строкой buy в статусе
`skipped` / `skip_reason='before_start'` (43 позиции, открытые до `mirror_since`). Это не сделки демо-счёта.

## Что сделано
Одно условие в выборке `_POSITIONS_SQL`: из позиций, у которых есть строка buy, исключены те, у кого buy —
`status='skipped' AND skip_reason='before_start'`. Остальные пропуски (`no_capital`, `no_balance`, `stale` и др.)
остаются. Колонки, порядок, очистка, приёмник, версия 9.5.2 не менялись. Лишние строки уйдут при следующем прогоне:
область перестраивается целиком, а строки ниже последней записанной очищаются (`clearContent` четырёх блоков до
`DEMO_OWNER_SHEET_LAST_ROW`).

### Дифф условия выборки
```diff
--- a/src/export/demo_owner_sheet.py
+++ b/src/export/demo_owner_sheet.py
@@ -369,3 +369,6 @@ _POSITIONS_SQL = (
     "LEFT JOIN signals sg ON sg.id = p.signal_id "
-    "WHERE EXISTS (SELECT 1 FROM demo_orders b WHERE b.position_id = p.id AND b.leg = 'buy') "
+    "WHERE EXISTS (SELECT 1 FROM demo_orders b WHERE b.position_id = p.id AND b.leg = 'buy' "
+    # Этап 9.5.3: позиции, открытые ДО запуска демо (buy skipped / before_start), — не сделки
+    # демо-счёта, в лист владельца они не идут. Остальные пропуски остаются.
+    "AND NOT (b.status = 'skipped' AND b.skip_reason = 'before_start')) "
     "ORDER BY p.opened_at, p.id;"
```
Позиции до запуска не учитываются и в `total_positions`, поэтому предупреждение о конце строк с формулами теперь
считает только реальные демо-позиции.

## Тесты
`tests/test_stage_9_5_3.py` (3 теста, на настоящей PostgreSQL):
* (а) позиции `before_start` в лист не попадают: блоки пусты, пишется одна A3, очистка начинается с `A6:D1000`;
* (б) `no_capital`, `no_balance`, `stale` остаются, в прежнем порядке, D = «пропущено»;
* (в) `before_start` вперемешку с остальными (в том числе между ними по времени): строки идут подряд с 6-й, без
  пустых промежутков, диапазоны записи `A6:D{5+n}`, `F6:K…`, `M6:M…`, `T6:T…`.

Проверено мутацией: при откате условия падают тесты (а) и (в).

В `tests/test_stage_9_5_2.py` изменён один тест (`test_statuses_follow_the_spec`): из него убрана позиция
`before_start` и её текст «позиция до запуска демо» — теперь это поведение закрепляет `test_stage_9_5_3.py`.
Таблица `SKIP_RU` в коде не тронута (причина осталась в словаре, но до листа не доходит).

Результаты: `pytest` целиком — **1491 passed, 98 skipped**, 0 failed; стенд приёмника — все сценарии прошли;
`ruff check .` — чисто.
