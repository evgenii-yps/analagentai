# Этап 9.4 — дисковая гигиена, хранение копий, внешняя выгрузка, свёртка замеров

Ветка `claude/gallant-lovelace-j9xs00` от `main` (`0e2c34b`). Отчёт исполнителя.

## 0. Главное

| блок | состояние |
|---|---|
| **A** Docker: журналы, автоочистка кэша | код готов: `deploy/docker-daemon.json`, `deploy/merge_docker_daemon.py`, `scripts/docker_gc.sh`, cron |
| **B** хранение копий | код готов: `scripts/backup_db.sh` |
| **C** внешняя выгрузка | код готов: `scripts/remote_backup.sh`, `deploy/install_rclone.sh`; **нужна авторизация Google Drive** (§4) |
| **D** свёртка замеров | **СТОП по ТЗ D.1: реализации нет.** Доклад — §3. Жду подтверждения заказчика |
| **E** вотчдог, два порога, прогноз | код готов: `scripts/watchdog.py`, `src/health/disk_forecast.py`, `src/health/daily_report.py` |
| **F** runbook | `docs/maintenance.md` |

**Чего в этом отчёте нет — и почему.** Работа велась в облачной среде без доступа
к боевому серверу (`46.224.52.105`). Поэтому ни один критерий приёмки §10
**не закрыт выводом с боевого сервера** — по правилу от 13.09 отчёт без вывода
команды не засчитывается. В репозитории проверено то, что можно проверить без
сервера (§6: `ruff` чисто, `pytest` 1165 passed на Python 3.12, скрипты прогнаны
настоящим запуском с подставными docker/curl/rclone). Порядок снятия боевых
выводов — §5, по критериям — §7.

## 1. Что сделано, по блокам

### A. Docker
* `deploy/docker-daemon.json` — эталонные значения; `deploy/merge_docker_daemon.py`
  **сливает** их с существующим `/etc/docker/daemon.json` (чужие ключи сохраняются,
  прежний файл копируется в `.bak-<время>`, повторный запуск ничего не меняет).
* **Важная поправка к ТЗ (A.1).** `log-opts` из `daemon.json` применяются только к
  контейнерам, **созданным после** правки. `systemctl restart docker` сам по себе
  не изменит ни один существующий контейнер, и критерий 10.1 (`docker inspect`
  показывает max-size 20m) остался бы невыполненным. Поэтому в окне обслуживания
  стек не «останавливается», а **удаляется** (`docker compose --profile "*" down`,
  без `-v` — тома с базой остаются), а при загрузке systemd-юнит `agent-trade.service`
  (`docker compose up -d`) создаёт контейнеры заново уже с ограничением. Порядок — в
  `docs/maintenance.md`, шаги 2 и 4.
* `scripts/docker_gc.sh`: `docker builder prune -af --filter until=24h`, затем
  `docker image prune -f` (**без `-a`** — закреплено тестом по тексту скрипта),
  печать освобождённого объёма (по `df` до/после и итоговым строкам docker) в
  `logs/docker_gc.log`. Cron: воскресенье 04:00 UTC — в `deploy/install.sh` (для
  новых установок) и `deploy/add_cron_9_4.sh` (идемпотентное дописывание на
  работающем сервере). Строка «после каждой пересборки запустить вручную» —
  в §5, шаг 8.

### B. Копии (`scripts/backup_db.sh`)
* **Файл лежит в `scripts/`, а не в `deploy/`, как написано в ТЗ** — правка внесена
  там, где он есть и откуда его вызывает cron.
* Суточные: `BACKUP_KEEP_DAILY`; месячные: копия за 1-е число получает имя
  `agenttrade_ГГГГ-ММ-01.monthly.dump.gz`, суточный счётчик её не видит,
  хранится `BACKUP_KEEP_MONTHLY`. После ротации считается объём **всего**
  каталога; больше `BACKUP_DIR_MAX_GB` — сообщение в Telegram с фактическим
  объёмом, **ничего сверх правила не удаляется**.
* Порядок ротации — по имени (в нём дата), не по mtime. Файлы вне шаблона (ручная
  копия в `backups/manual/`) ротация не трогает.
* Защита от порчи настроек: `0`, пустое значение и «abc» останавливают скрипт **до**
  создания дампа и до любого удаления (значение 0 стёрло бы и свежую копию).
* Заодно исправлено: в старой версии в Telegram уходило буквальное `%0A` вместо
  перевода строки.
* Ожидание на первом прогоне: 14 суточных файлов (16–29.09) → 7, освобождение
  ≈ 4.2 ГБ (7 самых старых файлов по ~600 МБ).

### C. Внешняя выгрузка
* `deploy/install_rclone.sh` — rclone **v1.75.1** из официального zip, sha256
  закреплён: `982b5aa7…a13ab` для `linux-amd64`. Сумма сверена с файлом
  `SHA256SUMS` релиза на github.com и с суммой фактически скачанного архива —
  совпали. Не из apt.
* `scripts/remote_backup.sh`: самый свежий файл → `rclone copy` → размер в
  хранилище сверяется с локальным по `rclone lsjson` → в хранилище остаётся
  `REMOTE_BACKUP_KEEP` самых свежих (удаляются только файлы по шаблону имени
  копий, и только после успешной сверки) → итог в `logs/remote_backup.log` и
  `logs/remote_backup.status`. Любая неудача — Telegram с причиной и код 1. Успех
  Telegram не трогает; строку «Внешняя копия: дата, файл, размер» показывает
  суточная сводка. `REMOTE_BACKUP_ENABLED=false` — код 0 и запись в журнал.
  Значения из окружения перекрывают `.env` — сбой имитируется одним запуском
  (§7, критерий 10.6).
* **Cron от root** (`30 4 * * 0 root …`): по ТЗ настройки rclone лежат в
  `/root/.config/rclone/rclone.conf` (root, 600), пользователь `agent` их прочитать
  не может. Журнал `logs/remote_backup.log` поэтому создаётся от root. Токен из
  `rclone.conf` скрипт нигде не печатает (проверено тестом).

### E. Вотчдог
* Два порога: `WATCHDOG_DISK_CRIT_PCT=15` (тревога, антиспам 1 раз в час, ключ
  `watchdog:alert:disk`) и `WATCHDOG_DISK_WARN_PCT=25` (предупреждение, TTL 24 ч,
  **отдельный** ключ `watchdog:alert:disk_warn`). Ниже CRIT предупреждение не
  дублируется. Оба порога печатаются в каждом прогоне. Прежнее имя
  `WATCHDOG_DISK_MIN_FREE_PCT` читается как запасное для CRIT.
* `logs/disk_usage.csv`: раз в сутки строка `дата;занято_ГБ;свободно_процент`
  (вотчдог бежит каждые 10 минут — пишет, если даты в последней строке ещё нет).
  Прирост — по последним 14 записям как (занято_последней − занято_первой) /
  число суток между их датами (пропущенные сутки прирост не завышают). Меньше 7
  записей — «недостаточно данных». Та же оценка — в обоих сообщениях и строкой
  «Прогноз диска» в суточной сводке (общий модуль `src/health/disk_forecast.py`,
  второй реализации расчёта нет). Если диск не растёт: «прироста нет, срок не
  определён»; если порог уже пройден — «порог 15% уже пройден».
* Суточная сводка получила также строку «Внешняя копия: …».
* **Принудительная проверка без подделки данных:** порог перекрывается окружением
  (§7, критерий 10.10).

### F. `docs/maintenance.md` — готово (порядок перезагрузки, признаки успеха/неудачи, откат).
Учтено то, чего нет в ТЗ: вотчдог **сам поднимает упавшие контейнеры**, поэтому на
время окна cron-файл `agent-trade` временно убирается; при загрузке стек
поднимает systemd-юнит.

## 2. Отклонения от ТЗ и решения, которые нужны от заказчика

| # | что | решение |
|---|---|---|
| 1 | ТЗ: «восемь новых переменных» (п. 10.11) | Фактически **десять**: B — 3, C — 4, E — 2, D — 1 (`MEASURE_DETAIL_RETENTION_DAYS`, добавляется вместе с блоком D). Все — в `.env.example`; в боевой `.env` нужны явно (§5, шаг 2) |
| 2 | `deploy/backup_db.sh` в ТЗ | файл `scripts/backup_db.sh` |
| 3 | A.1 «`systemctl restart docker`» | недостаточно, см. §1.A; используется `down` + загрузка |
| 4 | Cron внешней выгрузки | от `root` (настройки rclone в `/root`) |
| 5 | Журнал systemd 575 МБ не ограничен — в ТЗ назван причиной, но в блоках A–F **нет задачи** на него | предложение: `SystemMaxUse=200M` в `/etc/systemd/journald.conf.d/`. Не сделано — вне scope IN. Скажите, включать ли |
| 6 | Ручные дампы в `/root` (1.8 ГБ) удалены вручную, штатного правила нет | вне scope; отмечено |
| 7 | B, откат: «копия за 7-е сутки выгружается наружу вручную» | понято как **7-й по свежести файл** (`ls -1 backups \| sort -r \| sed -n 7p`), выгружается в `gdrive:AgentTrade/manual` (не ротируется). Если имелся в виду другой файл — поправьте |
| 8 | Обёртки `sudo -u agent` | rclone и правки `/etc` — через `sudo` (root); всё про docker — `sudo -u agent`. `remote_backup.sh` запускается от root |

## 3. Доклад D.1 (БЛОКИРУЮЩИЙ: реализация блока D не начата)

### 3.1. Фактический DDL

**Ограничение честности.** Доступа к боевой базе нет, поэтому ниже DDL **из
миграций репозитория** (`016_strategy_outcomes.sql`, `017_trailing_outcomes.sql`;
в `src/core/db.py` те же схемы продублированы для `CREATE IF NOT EXISTS`). Это не
то же самое, что DDL боевой базы: миграции могли быть применены с отличиями.
Боевой DDL снимается **одной командой, только чтение** (`scripts/report_9_4_d1.sql`,
§5 шаг 0а) — она же даёт размеры, число строк, состав по версиям и суткам и
признак «пишет ли кто-то сейчас». Прошу приложить её вывод к подтверждению.

`trailing_outcomes` (PK `(signal_id, horizon_h, activation_ratio, retrace_ratio)`):
`signal_id BIGINT → signals(id)`, `horizon_h SMALLINT`, `activation_ratio NUMERIC(4,2)`,
`retrace_ratio NUMERIC(4,2)`, `logic_version SMALLINT`, `direction TEXT`,
`price_at_signal NUMERIC(20,8)`, `target_pct`, `stop_pct`, `cost_pct NUMERIC(10,6)`,
`exit_reason TEXT` (`target|stop|trail|timeout|ambiguous|no_data`), `hit_at TIMESTAMPTZ`,
`bars_to_hit INT`, `net_pnl_pct NUMERIC(12,6)`, `peak_pct`, `mae_pct`, `mfe_pct
NUMERIC(12,6)`, `resolution TEXT` (`1m|1h`), `computed_at TIMESTAMPTZ`. Индексы:
`(logic_version, activation_ratio, retrace_ratio, horizon_h)`, `(exit_reason,
horizon_h)`. Тринадцать вариантов (A,R): (0,0) — контроль «фиксированная цель», и
12 подвижных 4×3. **Инструмента и времени сигнала в таблице нет** — берутся
соединением с `signals` (`instrument_id`, `ts`).

`strategy_outcomes` (PK `(strategy, instrument_id, entry_ts, horizon_h)`):
`strategy TEXT` (`always_buy|always_sell|coin_flip|system|grid_buy|grid_sell`),
`instrument_id INT`, `entry_ts TIMESTAMPTZ`, `horizon_h SMALLINT`, `signal_id BIGINT`
(NULL у сетки), `logic_version SMALLINT`, `direction TEXT`, `price_at_entry`,
`target_pct`, `target_source TEXT`, `stop_pct`, `cost_pct`, `outcome TEXT`
(`target|stop|timeout|ambiguous|no_data`), `hit_at`, `net_pnl_pct NUMERIC(12,6)`,
`mae_pct`, `mfe_pct`, `resolution TEXT`, `seed BIGINT` (только `coin_flip`),
`computed_at`. Индексы: `(signal_id, horizon_h, strategy)`, `(strategy, horizon_h,
outcome)`.

Объёмы по данным ТЗ и кода: `trailing_outcomes` 2179 МБ (1 707 940 строк на момент
отчёта 9.1.3), `strategy_outcomes` 864 МБ.

### 3.2. Ответ на вопрос 1 — кто пишет и работает ли сейчас

Писателей **два**, оба — ночные задачи cron, обе идут в контейнере `barrier`
профиля `tools`:

| таблица | писатель | cron |
|---|---|---|
| `strategy_outcomes` | `src/baseline_main.py` → `src/baseline/runner.py` → `DB.save_strategy_outcome` (`db.py:1102`) | 04:25 UTC, `deploy/agent-trade-baseline.cron`, строка в `deploy/install.sh` |
| `trailing_outcomes` | `src/trailing_main.py` → `src/trailing/runner.py` → `DB.save_trailing_outcomes` (`db.py:2839`) | 04:40 UTC, `deploy/agent-trade-trailing.cron`, строка в `deploy/install.sh` |

**Работают ли сейчас:** по ТЗ §1 таблицы «продолжают расти» — значит, работают.
Подтверждается запросом п. 3 файла `report_9_4_d1.sql` (`max(computed_at)`,
строки за 24 ч/7 дней) и `/etc/cron.d/`, `tail logs/baseline.log logs/trailing.log`.

**Предложение (отдельный пункт): отключить обоих писателей** — закомментировать две
строки cron (в `/etc/cron.d/` на сервере и в `deploy/install.sh`/`deploy/*.cron`
репозитория); код не трогается, включить обратно — раскомментировать. Это не
косметика, а **условие безопасности блока D** — установлено чтением кода:

* оба писателя идемпотентны через «уже посчитанные пары», а множество пар берётся
  **из самой таблицы** (`get_trailing_pairs_done`, `get_strategy_pairs_done`), тогда
  как список пар для расчёта — из `signal_outcomes_barrier` за **всю историю
  текущей версии** (`get_trailing_anchors`: нет ограничения по давности,
  `--since` по умолчанию пуст);
* значит, удалённые детали ночью будут сочтены «недосчитанными» и **посчитаны заново**
  — а минутных свечей старше 30 суток уже нет (`RETENTION_1M_DAYS=30`), и строка
  запишется с `resolution='1h'`/`no_data`. Детали «воскреснут» в худшем разрешении
  под тем же ключом, а таблица снова начнёт расти;
* контроль `check_trailing_control` (сверка с `signal_outcomes_barrier`) при
  отсутствии удалённых пар даёт `missing`, и ночная задача вернёт код 1.

Если заказчик не хочет отключать писателей — альтернатива требует правки
`src/trailing/runner.py` и `src/baseline/runner.py` (ограничить окно расчёта
`MEASURE_DETAIL_RETENTION_DAYS`); это код замера, но не запрещённые ТЗ каталоги.
Рекомендую отключение: замер 9.1.3 завершён.

### 3.3. Ответ на вопрос 2 — читает ли что-то, кроме скриптов анализа

Проверено грепом по исходникам (`grep -rn -E "trailing_outcomes|strategy_outcomes"`
по `*.py *.sql *.sh *.yml`):

* `src/agents/`, `src/decision/`, `src/positions/`, `src/evaluator*`, `src/export*`,
  `src/calibration*`, `backtest/`, `live_*.py` — **ни одного упоминания**.
* Читатели вне писателей:
  * `DB.fetch_trailing_resample_batch` (`db.py:2182`, `FROM trailing_outcomes t JOIN signals`)
    ← `scripts/trailing_resample_9_1_3.py` и `tests/memory/`;
  * `scripts/trailing_stats.py:116` (`FROM trailing_outcomes`) — три защиты от подгонки;
  * `analysis/sql/09_baseline_compare.sql` (`FROM strategy_outcomes`);
  * `DB.get_strategy_stats_snapshot`, `count/get/delete_strategy_outcomes_unsettled`
    ← только `scripts/repair_9_1_strategy_settle.py` (одноразовый ремонт 9.1.1);
  * `DB.check_trailing_control` (`db.py:2904`) ← `trailing/runner.compute` (контроль
    писателя, см. 3.2);
  * `deploy/verify_8_9.sh`, `verify_8_10.sh`, `verify_9_1.sh` — приёмочные проверки
    прошлых этапов; `verify_8_9/8_10` **проверяют, что таблицы входят в
    `PROTECTED_TABLES` в `scripts/retention.py`**, и при выходе из списка напишут
    «НЕ защищена — вернуть».
* Внешних читателей (Sheets/Notion-выгрузка, Telegram-бот, суточная сводка) нет.

**Вывод: горячий путь и все сервисы таблицы не читают.** Читают только скрипты
анализа замеров, ночной контроль писателя и старые приёмочные скрипты.

### 3.4. Что это значит для реализации (предупреждения)

1. **`PROTECTED_TABLES`.** Обе таблицы защищены кодом от любого удаления, и защита
   проверяется перед каждым `DELETE`. Предлагаю **оставить их в списке** (verify
   старых этапов остаются валидными) и добавить отдельный узкий путь
   `_delete_measure_details()`, который удаляет **только** после сверки: по каждым
   суткам, подлежащим удалению, `count(*)` деталей == сумме `n_rows` свёртки за эти
   сутки. Это строже, чем «свёртка не упала».
2. **Свёртка не воспроизводит вывод 9.1.3 полностью.** Вывод 9.1.3 получен
   `trailing_stats.py`: парный бутстрэп (10 000 пересборок), перестановочная
   проверка разброса и проверка на независимой половине. Всем трём нужны значения
   **по парам (сигнал, горизонт) сразу по тринадцати вариантам** — из суточных
   агрегатов «сутки × инструмент × версия × вариант» они не восстанавливаются, как
   ни выбирать набор показателей. Из свёртки получатся точно: число наблюдений,
   распределение исходов, среднее (по `sum/n`), средняя разность с контролем
   (по парным суммам), приближённые доверительные интервалы; **не получатся**:
   бутстрэп-интервалы, перестановочный разброс, вердикт «на независимых данных».
   **Решение заказчика:** (а) принять; (б) рекомендую — добавить третью, маленькую
   таблицу `trailing_pairs_compact` (по строке на пару: `signal_id, horizon_h,
   logic_version` и тринадцать `net_pnl_pct` массивом; ≈130 тыс. пар ≈ 15–20 МБ) —
   она даёт **точное** воспроизведение всех трёх защит. Это расширение сверх ТЗ,
   без подтверждения не делаю.
3. **Ключ «день».** У `trailing_outcomes` нет времени сигнала и инструмента — они
   берутся из `signals` (`ts::date`, `instrument_id`); у `strategy_outcomes` день —
   `entry_ts::date`. День — по UTC. Не по `computed_at`: строка дня D считается на
   следующую-послеследующую ночь.
4. **Завершённость суток.** Сутки D свёртываются только когда `D + 1 сутки +
   максимальный горизонт (24 ч) + запас` уже позади; свёртка `ON CONFLICT DO NOTHING`
   (образец `agent_outputs_daily`), а неполные сутки никогда не сворачиваются, чтобы
   не зафиксировать неполный итог навсегда.
5. **Гранулярность и показатели** — ниже, нужно ваше «да».

### 3.5. Предложенный состав свёртки (на подтверждение)

**`trailing_outcomes_daily`**, PK `(day, instrument_id, logic_version, horizon_h,
activation_ratio, retrace_ratio)` (ключевое измерение замера — **вариант выхода (A,R)
× горизонт**; `logic_version` в PK, сутки смены версии дают две строки):
`n_rows`; исходы `n_target, n_stop, n_trail, n_timeout, n_ambiguous, n_no_data`;
`n_resolution_1m, n_resolution_1h`; по `net_pnl_pct` (только строки с итогом):
`n_pnl, sum_pnl, sum_sq_pnl, avg_pnl, p10, p25, p50, p75, p90`; `avg_peak_pct,
avg_mae_pct, avg_mfe_pct, avg_bars_to_hit`; `avg_target_pct, avg_stop_pct,
avg_cost_pct`; парные величины к контролю (0,0) на парах с полным набором из
тринадцати итогов: `n_pairs_full, sum_diff_vs_fixed, sum_sq_diff_vs_fixed`.
Ожидаемое число строк: ≈ (число суток) × 5 × 4 × 13 на версию — порядка 10 тыс.
строк, единицы МБ (меньше ожидавшихся ТЗ ~100 МБ).

**`strategy_outcomes_daily`**, PK `(day, instrument_id, logic_version, strategy,
horizon_h)` (ключевое измерение — **стратегия × горизонт**): `n_rows`; исходы
`n_target, n_stop, n_timeout, n_ambiguous, n_no_data`; `n_resolution_1m/1h`;
`n_pnl, sum_pnl, sum_sq_pnl, avg_pnl, p10…p90`; `avg_mae_pct, avg_mfe_pct`;
`avg_target_pct, avg_stop_pct, avg_cost_pct`; парные к `system` на общих
`(signal_id, horizon_h)` (кроме сеток, у которых сигнала нет — там NULL):
`n_pairs_vs_system, sum_diff_vs_system, sum_sq_diff_vs_system`.

Оговорка: процентили посуточные и **не сливаются в процентили за период** — это
свойство любых агрегатов; точные числа за период даёт `sum/n`.

### 3.6. Что я жду от заказчика по D
1. Вывод `report_9_4_d1.sql` (боевой DDL и факты).
2. «Да/нет» по составу §3.5 и по третьей таблице `trailing_pairs_compact` (§3.4 п. 2).
3. «Да/нет» на отключение двух писателей (§3.2).
4. После этого — реализация D.2, откат `db/migrations/029_…_rollback.sql` (с
   реальным путём применения внутри контейнера — не `/docker-entrypoint-initdb.d/`),
   затем D.3: слепок «до/после», отдельная копия на 30 суток, прогон «только
   посчитать», отчёт, и удаление **отдельной командой** после подтверждения.

## 4. Инструкция заказчику: авторизация Google Drive (C.2)

Токен доступа — это ключ к вашему Google Drive. Передавайте его **только исполнителю
личным сообщением**, не публикуйте, не кладите в репозиторий и не присылайте
скриншотом в общий чат.

**Что нужно у вас:** компьютер с браузером (Windows или macOS) и вход в тот Google-аккаунт,
на диске которого будут храниться копии. ~10 минут.

**Шаг 1. Скачайте rclone.** Откройте https://rclone.org/downloads/ → раздел *Windows*
(или *macOS*) → скачайте **v1.75.1** для вашей системы (для Windows — «Intel/AMD - 64 Bit»).
Распакуйте архив в любую папку, например, на рабочий стол.

**Шаг 2. Откройте терминал в этой папке.**
* Windows: откройте папку с распакованным `rclone.exe`, нажмите в адресной строке
  проводника, впишите `powershell` и Enter. Откроется синее окно.
* macOS: Программы → Утилиты → *Терминал*; впишите `cd ` (с пробелом), перетащите в окно
  распакованную папку, Enter.

**Шаг 3. Запустите авторизацию.** Впишите и нажмите Enter:
* Windows: `.\rclone.exe authorize "drive" --drive-scope drive.file`
* macOS: `./rclone authorize "drive" --drive-scope drive.file`

(`drive.file` — доступ **только к файлам, созданным этой программой**: остальной
Drive она не увидит. Если команда пожалуется на неизвестный параметр, выполните
без `--drive-scope drive.file` и сообщите исполнителю — токен будет с более широким доступом.)

**Шаг 4. Что вы увидите.** В терминале: «If your browser doesn't open automatically go to
the following link: http://127.0.0.1:53682/auth?state=…». Сам откроется браузер со
страницей Google.

**Шаг 5. Куда нажимать в браузере.** Выберите ваш Google-аккаунт → на экране
«Rclone хочет получить доступ к вашему аккаунту Google» нажмите **Продолжить / Allow**
(если Google пишет «приложение не проверено», нажмите **Дополнительные настройки → Перейти на страницу
rclone (небезопасно)** и затем «Продолжить») → появится «Success! All done. Please go back to rclone.»

**Шаг 6. Что скопировать.** Вернитесь в терминал. Там будет:
```
Paste the following into your remote machine --->
{"access_token":"ya29....","token_type":"Bearer","refresh_token":"1//...","expiry":"..."}
<---End paste
```
Выделите **только строку в фигурных скобках `{ … }`** (без стрелок и слов «Paste…»/«End paste») и
скопируйте (Ctrl+C / Cmd+C). Отправьте её исполнителю личным сообщением.

**Шаг 7. Уберите следы.** Закройте терминал; удалите папку с rclone, если он вам больше не нужен.
Отозвать доступ можно в любой момент: https://myaccount.google.com/permissions → rclone → *Удалить доступ*.

**Что делает исполнитель на сервере** (от root; токен не попадает в историю команд и логи):
```
sudo bash /opt/agent-trade/deploy/install_rclone.sh
sudo install -d -m 700 /root/.config/rclone
sudo bash -c 'umask 077; read -r -s -p "Вставьте токен и нажмите Enter: " T; echo; printf "[gdrive]\ntype = drive\nscope = drive.file\ntoken = %s\n" "$T" > /root/.config/rclone/rclone.conf'
sudo chown root:root /root/.config/rclone/rclone.conf
sudo chmod 600 /root/.config/rclone/rclone.conf
sudo ls -l /root/.config/rclone/rclone.conf
sudo rclone about gdrive:
```
Ожидание: `-rw------- 1 root root …`; `rclone about` печатает `Total/Used/Free`. **Если `Free`
меньше 7 ГБ — остановиться и доложить**, `REMOTE_BACKUP_KEEP` самостоятельно не уменьшать.

## 5. Порядок развёртывания на боевом сервере (по одной команде, из `/opt/agent-trade`)

Выполняется после слияния PR заказчиком. Образы **пересобирать не нужно** — этап
меняет только скрипты на хосте; строка вызова `docker_gc.sh` «после каждой
пересборки» относится к будущим пересборкам.

0. **Слепок «до»** (первой командой, до слияния/`git pull`):
   `sudo -u agent bash deploy/snapshot_9_4.sh before` и `df -h /` и
   `sudo -u agent docker system df` — цифры «до» для итога по диску.
   * 0а. Доклад D.1, факты: `sudo -u agent docker compose exec -T postgres psql -U agenttrade -d agenttrade < scripts/report_9_4_d1.sql > analysis_out/report_9_4_d1.txt`
1. `sudo -u agent git pull` (ветка после слияния).
2. **Прописать 10 переменных в боевой `.env` явно** (критерий 10.11). Проверить:
   `grep -E "^(BACKUP_KEEP_DAILY|BACKUP_KEEP_MONTHLY|BACKUP_DIR_MAX_GB|REMOTE_BACKUP_ENABLED|REMOTE_BACKUP_REMOTE|REMOTE_BACKUP_PATH|REMOTE_BACKUP_KEEP|WATCHDOG_DISK_CRIT_PCT|WATCHDOG_DISK_WARN_PCT)=" .env`
   — должно быть 9 строк (десятая, `MEASURE_DETAIL_RETENTION_DAYS`, — с блоком D). Значения — из `.env.example`.
3. **Google Drive** (§4): rclone, авторизация, `rclone about`. Свободно < 7 ГБ — стоп.
4. **Копия для отката B:** `f=$(ls -1 backups | grep -E '^agenttrade_[0-9-]{10}\.dump\.gz$' | sort -r | sed -n 7p); echo $f; sudo rclone copy "backups/$f" gdrive:AgentTrade/manual`
5. **Тест выгрузки:** `sudo scripts/remote_backup.sh` → в конце «Готово: выгрузка … прошла»; `sudo rclone lsjson gdrive:AgentTrade/backups`.
6. **Имитация сбоя (10.6):** `sudo REMOTE_BACKUP_REMOTE=net_takogo scripts/remote_backup.sh; echo "код=$?"` → сообщение в Telegram и `код=1`.
7. **Cron:** `sudo bash deploy/add_cron_9_4.sh` (печатает строки docker_gc и remote_backup; критерий 10.3: `grep -E "docker_gc|remote_backup" /etc/cron.d/agent-trade`).
8. **Очистка Docker:** `sudo -u agent scripts/docker_gc.sh`, затем `tail -n 8 logs/docker_gc.log` и
   `sudo -u agent docker system df` (10.2: Build Cache < 1 ГБ). **После каждой будущей пересборки образов этот же скрипт запускается вручную.**
9. **Хранение копий (10.4):** `sudo -u agent scripts/backup_db.sh; ls -l backups; du -sh backups`.
10. **Вотчдог (10.10):** `sudo -u agent python3 scripts/watchdog.py` — видны «Пороги диска…» и создан `logs/disk_usage.csv`; принудительное предупреждение:
    `sudo -u agent env WATCHDOG_DISK_WARN_PCT=99 python3 scripts/watchdog.py` дважды подряд — первый раз сообщение уходит, второй — «подавлено антиспамом»;
    после проверки: `sudo -u agent docker compose exec -T redis redis-cli DEL watchdog:alert:disk_warn`.
11. **Окно обслуживания с перезагрузкой (A.1, 10.1):** `docs/maintenance.md`, шаги 1–6.
12. **Слепок «после» (10.13):** `sudo -u agent bash deploy/snapshot_9_4.sh after` → три `OK`.

## 6. Проверки в репозитории

* `ruff check .` — чисто.
* `pytest` — 1165 passed, 98 skipped (Python 3.12, как в проекте); новые 38 тестов —
  `tests/test_stage_9_4.py`: ротация суточных/месячных, порча настроек, превышение
  потолка, `docker_gc` без `-a`, слияние `daemon.json`, выгрузка (успех, несовпадение
  размера, неверное хранилище, `ENABLED=false`, ротация в хранилище, отсутствие токена
  в журнале), прогноз диска (меньше 7 записей, прирост, окно 14, пропуски суток,
  нулевой прирост), два порога и антиспам, строки сводки, граница этапа (нет правок в
  `src/agents/`, `src/decision/`, `src/positions/`).
* Граница ТЗ соблюдена: `LOGIC_VERSION` не менялся, ни один файл в запрещённых каталогах не тронут.

## 7. Критерии приёмки: чего не хватает

| # | критерий | состояние |
|---|---|---|
| 10.1 | `docker inspect`: max-size 20m, max-file 3 | ждёт окна (§5 шаг 11) |
| 10.2 | `docker_gc` отработал, кэш < 1 ГБ | ждёт сервера |
| 10.3 | строки в `/etc/cron.d/agent-trade` | ждёт сервера |
| 10.4 | ровно 7 суточных, объём посчитан | ждёт сервера |
| 10.5 | `rclone about`, выгрузка, размер совпал | ждёт авторизации |
| 10.6 | имитация сбоя → Telegram, код ≠ 0 | ждёт rclone на сервере (в песочнице — пройдено тестом) |
| 10.7 | доклад D.1 | **предоставлен (§3), ждёт подтверждения** |
| 10.8–10.9 | слепки свёртки, таблицы свёртки | **не начаты, ждут D.1** |
| 10.10 | оба порога, 24 ч, CSV | ждёт сервера |
| 10.11 | 10 переменных в боевом `.env` | ждёт сервера |
| 10.12 | `docs/maintenance.md` | **есть** |
| 10.13 | LOGIC_VERSION, отпечаток, позиции | скрипт слепка готов, ждёт сервера |
| 10.14 | `ruff`, `pytest` | **зелёные** |

## 8. Откат по блокам

* **A:** `sudo rm /etc/docker/daemon.json` (или вернуть `daemon.json.bak-…`) →
  `sudo systemctl restart docker` → `docker compose --profile "*" down` → `up -d`
  (пересоздать контейнеры со старой настройкой). Cron gc: убрать строку.
* **B:** вернуть значения в `.env` (например, `BACKUP_KEEP_DAILY=14`). Удалённые
  файлы не вернуть — поэтому шаг 4 (копия наружу).
* **C:** `REMOTE_BACKUP_ENABLED=false`. Полностью: убрать строку cron, `sudo rm /root/.config/rclone/rclone.conf`,
  отозвать доступ в Google (§4, шаг 7).
* **D:** реализации нет — откатывать нечего.
* **E:** вернуть прежний `scripts/watchdog.py` (`git checkout 0e2c34b -- scripts/watchdog.py`);
  `src/health/disk_forecast.py` и правки `daily_report.py` без вотчдога безвредны.

### Восстановление из внешней копии (гибель сервера)
Новый сервер по `deploy/install.sh` → `sudo bash deploy/install_rclone.sh` → настроить
`gdrive` (§4) → `sudo rclone copy gdrive:AgentTrade/backups/<файл> /opt/agent-trade/backups/` →
`gunzip -c <файл> | sudo -u agent docker compose exec -T postgres pg_restore -U agenttrade -d agenttrade --clean --if-exists`.
Проверка — как `verify` в конце `deploy/install.sh` (список таблиц и числа строк).

## 9. Итог по диску

| | занято | свободно |
|---|---|---|
| на момент выдачи ТЗ (по ТЗ, после ручной чистки) | ~21.5 ГБ | ~16 ГБ |
| после этапа (A, B, C, E, F) | **не измерено** — снимается шагом 12 | ожидается ≈ +4.2 ГБ (копии) |
| после блока D (свёртка) | после D | ожидается ещё ≈ +3 ГБ в базе и ≈ −⅓ размера суточных копий |
