# Этап 9.7 — Telegram-бот: исправления и интерфейс с кнопками (редакция 1 от 07.10.2026)

Основа — `main` `7274522` (PR #17). Ветка `claude/cool-wright-d1xdvg`.
Миграций нет, зависимостей нет (`requirements.txt` не менялся), `LOGIC_VERSION` не поднимался,
правил входа/выхода и учёта денег не трогал. Бот по-прежнему только читает; единственная запись —
`user_settings`.

## 0. Предусловия

**0.1.** Свежий `main` = `7274522` (`git fetch --all`; ветка запущена от него, `HEAD == origin/main`).
Ветки на момент начала (`git branch -r`):
`agent-resilience-aggregation-bias-m5k6ya`, `agent-trade-core-diagnostics-pvd1b9`,
`agent-trade-infra-stage-1-7hb1p0`, `ambiguous-marker-cases-fil8jx`, `balance-accounting-fixes-56bs38`,
`bars-tolerance-measurement-pmnci5`, `bold-faraday-im8uy8`, `confident-thompson-yrhh0g`,
`cool-wright-d1xdvg` (эта), `core-predictive-ability-diagnosis-jd34ht`, `deal-label-overwrite-fix-hqchha`,
`demo-spot-markets-only-rkjjr4`, `deployment-installer-script-k9e6t4`, `elegant-cerf-pg1iq4`,
`explain-signal-script-7ms5p7`, `funny-pascal-ld3rkn`, `gallant-lovelace-j9xs00`,
`local-stack-exchange-audit-so6rjz`, `measure-outcomes-no-stop-loss-aplbe7`,
`measurement-0-higher-timeframes-0suqco`, `quirky-faraday-45qjo4`, `signals-export-sheets-notion-ovib81`,
`single-position-unclosed-candle-stqi6i`, `stage-7-4-market-split-s0941y`, `stage-8-1-unknown-logic-version`,
`stage-8-10-1-control-mismatch`, `stage-8-10-trailing-exit-t24php`, `stage-8-3-signal-text`,
`stage-9-1-6-no-loss-limit-ynu77p`, `stage-9-2-nostop-plus-48-1hksli`, `telegram-bot-signal-readability-y4d9tq`,
`trailing-shadow-measurement-9-1-3`, `weekly-range-position-measurement-z4samw`, `wonderful-volta-x9b0a3`, `main`.

**0.2.** Оба режима `VIRTUAL_OUTPUT_ENABLED` и `DEMO_ENABLED` покрыты тестами (справка и главный экран — все
сочетания флагов; `/status`-подробно — `test_virtual_output.py`; команды — оба значения `VIRTUAL_OUTPUT_ENABLED`).

**0.3.** `requirements.txt` не менялся; Bot API вызывается тем же `httpx`.

**0.4.** К боевой базе из этой среды доступа нет, поэтому проверено по схеме и коду, а на сервере то же
перепроверяет `deploy/verify_9_7.sh` (пункт 5):
* `agent_outputs.instrument_id` есть (`db/init.sql:256`); индекс `idx_agent_outputs (agent, instrument_id, ts DESC)`
  есть (`db/init.sql:263`); рядом `ix_agent_outputs_ts (ts)`.
* `agenttrade_ro`: SELECT на `signals`, `instruments`, `agent_outputs` — `GRANT SELECT ON ALL TABLES` + права по
  умолчанию при каждом старте бота (`src/bot/queries.py`, `ensure_readonly_role`); на `demo_state`, `demo_orders`,
  `demo_balance_daily` — отдельный GRANT в миграции 030. Проверено исполнением: тест
  `test_a_reader_role_can_run_every_new_query` поднимает роль на локальной PostgreSQL 16 (схема = `init.sql` + все
  миграции) и выполняет под ней все новые запросы.
* Служба demo читает `user_settings`: она подключается основным пользователем БД (владельцем таблицы), прав хватает.
* **Находка, важная для Д6:** агент деривативов пишет вывод по инструменту-**контракту** (`BTC/USDT:USDT`), остальные
  агенты — по споту. Поэтому «по каждой монете» группируется по базовому активу символа, а не по `instrument_id`.

## Что сделано

### Дефекты
| № | Что | Где |
|---|---|---|
| Д1 | Справка: список монет из `instruments` (активные споты), слов «рынок BTC» нет | `screens.help_screen`, `queries.spot_instruments` (+`active`) |
| Д2 | Справка и главный экран строятся от `NOTIFY_SIGNALS_ENABLED`, `DEMO_ENABLED`, `DEMO_NOTIFY_ENABLED` | `screens.Flags`, `_notify_sentence` |
| Д3 | «Agent Trade», строка «Сделки — на демо-счёте OKX, деньги не настоящие. Реальными деньгами система не торгует» | `screens` |
| Д4 | `demo:heartbeat` в проверяемых ключах только при `DEMO_ENABLED=true` | `poller.heartbeat_keys()` |
| Д5 | Русские названия служб; машинные ключи только на «Подробно» | `screens.HEARTBEAT_RU` |
| Д6 | Агенты по каждой монете отдельно, устаревшее → 💤, решение монеты из `signals` | `queries.agents_by_token`, `latest_signal_by_token` |
| Д7 | Настройки объясняют, на что влияют монеты/горизонт/порог; строка про выключенные уведомления | `settings_menu.menu_text` |
| Д8 | Тихие часы по `NOTIFY_TIMEZONE`, хранение в UTC (23:00–08:00 МСК ↔ 20/4) | `settings_menu` |
| Д9 | Значок и слово «Продано в плюс/в минус» по результату | `demo/messages.closed_text` |
| Д10 | «Согласие агентов 62%»; «Вероятность успеха по истории NN%» — отдельной строкой, только если не NULL | `demo/messages.opened_text`, `_FILL_SQL` |
| Д11 | «Пояснение:», «покупать/продавать/ждать» вместо `rationale`/`buy/sell/wait` (карточка, /stats, суточная сводка) | `handlers` |
| Д12 | Один формат чисел везде | `src/core/fmt.py`, `handlers`, `demo/messages`, `demo/report` |
| Д13 | Неизвестная команда и текст: та же фраза + кнопка «🏠 Меню» | `screens.unknown_screen` |

### Экраны и навигация (§4–§5)
* `src/core/fmt.py` — формат чисел (§3); `src/bot/nav.py` — `parse_nav`, `callback_data` с префиксом `v1:`;
  `src/bot/screens.py` — девять экранов + «Подробно» и «Качество сигналов» (чистые функции).
* Нажатие правит то же сообщение (`editMessageText`); `not_modified` → «Данные не изменились»; `failed` → то же
  содержимое новым сообщением; каждое нажатие получает `answerCallbackQuery` (в том числе при ошибке, чужом чате,
  устаревшей кнопке: «Кнопка устарела, откройте /menu»; частое нажатие: «Секунду…»).
* `src/notify/telegram.py`: `send_message(...) -> message_id | None` (+`disable_notification`), `edit_message`,
  `set_my_commands`. Все старые вызовы проверяют результат на истинность — проверено сканом по `src`.
* Меню команд (9 штук) задаётся один раз при старте; в журнал — `bot_set_my_commands=1` или
  `bot_set_my_commands_failed=1`.
* Все старые команды живы; `/demo`→«Счёт», `/status`→«Система», `/summary`→«Подробно», `/stats`→«Качество
  сигналов»; `/positions` без изменений и вне меню/справки.

### Сообщения о сделках (§6)
Новые тексты открытия/закрытия, кнопка `[💰 Счёт]` (`v1:accn`, счёт приходит **новым** сообщением, сообщение о
сделке не правится). Тихие часы: перед отправкой сообщения о сделке, почасовой сводки придержанных и суточной
сводки служба читает `user_settings` (кэш 60 с) и шлёт с `disable_notification=true`; алерты — всегда со звуком;
ошибка чтения — со звуком и предупреждением `demo_quiet_hours_read_failed=1`. Функция попадания в окно одна —
`core.user_settings.is_quiet_hour` (она там уже была; её же использует `notify/agent`, своей копии нет).

## Тесты
`1616 passed, 98 skipped`, `ruff check .` чист. Локально поднята PostgreSQL 16 (`PGTEST_*`), поэтому тесты с базой
реально выполнялись; оставшиеся пропуски — тесты с другими переменными (`BT_TEST_DSN`, `AGENT_TEST_DSN`).
Новые файлы: `tests/test_stage_9_7_fmt.py`, `_screens.py`, `_bot.py`, `_db.py` (≈125 тестов; все пункты §9
перечислены ниже по номерам).

| §9 | Где |
|---|---|
| 1 fmt | `_fmt` |
| 2 Д1–Д3 (5/3 монеты × сигналы × демо) | `_screens::test_help_and_main_tell_the_truth` |
| 3 Д4 | `_screens::test_the_demo_heartbeat_is_watched_only_while_the_demo_is_enabled` |
| 4 Д5 | `_screens::test_the_system_screen_has_no_machine_keys_and_matches_the_sample` |
| 5 Д6 | `_screens::test_agents_are_shown_per_coin…`, `_db::test_agents_are_grouped_by_coin…` |
| 6 Д8 | `_screens::test_quiet_hours_are_chosen_in_moscow_time_and_stored_in_utc` и соседние (полный перебор 24×24) |
| 7 Д9 | `test_demo_messages::test_the_closing_mark_follows_the_result` (+, 0, −) |
| 8 Д10 | `test_demo_messages::test_the_calibrated_probability_is_a_separate_line_only_when_known` |
| 9 callback ≤64 байт, `parse_nav` | `_screens::test_every_callback_fits_in_64_bytes…`, `test_parse_nav_on_garbage_never_raises` |
| 10 ≤3900 при 40 открытых сделках | `_screens::test_every_screen_fits_in_one_message_with_forty_open_trades` |
| 11 edit/answer | `_bot::test_a_press_*`, `test_not_modified_*`, `test_a_failed_edit_*` |
| 12 тихие часы demo | `_bot::test_a_trade_message_inside_the_quiet_window…` и соседние |
| 13 совместимость | `_bot::test_every_command_answers_and_none_crashes` (оба значения `VIRTUAL_OUTPUT_ENABLED`) |
| 14 Итоги | `_screens::test_results_screen_*`, `_db::test_before_start_is_not_a_skip…` |
| 15 скан записи | `_bot::test_the_bot_writes_only_to_user_settings` |

Дополнительно: валидность Telegram-HTML на всех экранах, HTTP-уровень `send_message/edit_message/set_my_commands`
(`httpx.MockTransport`), SELECT-only сканы новых запросов, сухая проверка `verify_9_7.sh` (синтаксис, нет записи).

### Изменённые старые тесты (только те, что проверяли старые тексты/маршруты)
| Файл | Тесты | Причина |
|---|---|---|
| `test_bot.py` | `test_help_contains_mandatory_phrase`, `test_agents_stale_marked`, `test_stats_puts_the_count_next_to_every_percent` | старая фраза справки была неправдой (Д3); экран агентов теперь по монетам (Д6); `buy`→«покупать» (Д11) |
| `test_demo_bot.py` | 4 текстовых + 4 с базой | `render_demo` заменён экранами «Счёт»/«Сделки»; `_route` → `_answer_command`; формат чисел |
| `test_demo_messages.py` | 8 тестов (в т. ч. `test_timezone_label_and_conversion` заменён) | новые тексты КУПЛЕНО/ПРОДАНО (§6); `local_time` заменён `fmt.local_dt` |
| `test_demo_report.py` | 4 теста | числа через `fmt`, значки 🟢/🔴, причины пропусков по-русски |
| `test_demo_safety.py` | `test_the_sheets_and_the_bot_only_read_the_demo_tables` | блок демо-запросов бота теперь из нескольких методов (срез выравнивается) |
| `test_stage_8_3.py` | `test_menu_shows_current_state`, `test_menu_confirmation_is_short_and_specific`, `test_menu_quiet_hours_round_trip` | новый текст настроек (§4.7), запятая в числах, граница конца тишины (см. отклонение 7) |
| `test_stage_9_1_1.py` | `test_the_capital_lines_stand_apart_and_are_never_summed` | формат чисел (запятая) |
| `test_virtual_output.py` | 9 тестов | справка не показывает `/positions` ни при каком значении; `/status` теперь экран «Система», подробный текст — `_status_text` |
| `demo_support.py` | заглушка `notify` | принимает `reply_markup`/`disable_notification` (+ необязательный список `sent_kwargs`) |

## Замер времени экранов
Боевой базы под рукой нет, замер — на синтетике в локальной PostgreSQL 16: `agent_outputs` 2,2 млн строк, `signals`
778 тыс., `ohlcv` 1,4 млн (90 суток поминутно по 11 инструментам), 312 закрытых и 12 открытых демо-сделок. Теплый кэш,
лучшее из трёх, мс; `sys` без Redis (в заглушке пусто).

| Экран | до оптимизации | итог |
|---|---|---|
| Главный | 437 | 93 |
| Счёт | 104 | 95 |
| Сделки | 107 | 116 |
| Итоги (7 дн / всё время) | 100 / 103 | 101 / 100 |
| Агенты | **4628** | **3** |
| Сигналы (все / сильные) | 1 / 39 | 1 / 40 |
| Система | 148 | ≈1 + 12 чтений Redis |
| Подробно | 523 | 377 |
| Качество сигналов | 115 | 111 |
| Настройки, Справка | <1 | <1 |

Что дало ускорение: отбор выводов агентов — по окну времени (сначала был боковой запрос по всем парам
«агент × инструмент»: планировщик шёл по индексу `ts` и для пар без выводов — «futures × спот» — читал всю таблицу);
`freshness_by_instrument` переписан боковым соединением (ответы те же, покрыт тестом). Реальные числа даст
`deploy/verify_9_7.sh` (пункт 4 читает `bot_screen=1 ms=…` из журнала бота).

## Отклонения от ТЗ (с причинами)
1. **Длительность.** `fmt.duration` опускает нулевую часть («2 д», «5 ч»), поэтому образец «48 ч» из §4.3 печатается
   «2 д» (правило функции «≥24 ч → дни» важнее образца). Меньше минуты — «меньше минуты», а не «0 мин» и не «<1 мин»:
   знак «<» под `parse_mode=HTML` Telegram принял бы за тег.
2. **`src/demo/runner.py` тронут** (в ТЗ он в списке неизменных): только проводка — поле `DemoConfig.chat_id`,
   тип `Context.notify` (теперь принимает `reply_markup`/`disable_notification`), поле `Context.quiet_cache`.
   Логики входов, ордеров и денег там не менялось. Иначе сообщению нечем узнать чат для тихих часов.
3. **`agents_by_token`** — не «`DISTINCT ON` по всему журналу», а `DISTINCT ON` за окно (по умолчанию час, шире
   порога свежести); всё старше окна экран и так показывает как 💤. Дополнительный необязательный параметр
   `max_age_sec`. Причина — замер выше (4,6 с → 3 мс).
4. **`freshness_by_instrument` переписан** (боковое соединение), хотя в ТЗ он не упомянут: без этого главный экран и
   «Система» тратили бы полсекунды на полугодовой истории.
5. **`spot_instruments` теперь только активные.** Список монет в справке «из активных спот-пар» (Д1); тот же список
   видит и меню настроек.
6. **«Подробно»:** общий список heartbeat печатается один раз (из состояния системы), в сводке за сутки он опущен
   (`include_heartbeats=False`) — иначе оба текста не помещались в одно сообщение.
7. **Тихие часы — граница конца.** Кнопка конца теперь означает час, когда звук **возвращается** («до 08:00»), в
   базу пишется последний тихий час включительно (UTC). Совпадение начала и конца отклоняется («тихо все сутки» —
   не тишина). Ранее сохранённые значения показываются пересчитанными в МСК, `src/notify/agent.py` не менялся.
8. **Кнопки.** Нижний ряд — ровно как в §4 для каждого экрана: у «Настроек», карточки сигнала, «Подробно» и
   «Качества сигналов» — только перечисленные в ТЗ кнопки (без «Обновить»); «Обновлено …» там не печатается, где
   его не было в образце (настройки, карточка).
9. **Анти-флуд для нажатий** (`BOT_RATE_LIMIT_SEC`, ответ «Секунду…») действует на навигацию `v1:*`; кнопки меню
   настроек остались без ограничения, как и были (иначе три монеты подряд не переключить). Счётчик нажатий отдельный
   от счётчика текстовых команд.
10. **`/last` без `all`** по-прежнему показывает только отправленные (ТЗ: «как сейчас»), `/signals` и кнопка
    «📡 Сигналы» при выключенных сигнальных уведомлениях открываются на **всех** решениях; в пустом списке «сильных»
    есть подсказка нажать «Показать все решения».
11. **Формат чисел.** Через `fmt` переведены все тексты бота, включая `/positions` (только цифры, не поведение).
    Не тронут блок цели на карточке сигнала (`notify/wording.target_block`): он общий с уведомлениями о сигналах,
    а `src/notify/*` ТЗ не затрагивает. Даты в карточке и подробном `/stats` остались прежними.
12. **`DEMO_SKIP_RU`** лежит в `src/demo/messages.py` (его же использует суточная сводка), `screens.py` его
    импортирует; в словарь добавлены `before_start`, `below_min_size`, `no_market`, `closed_before_buy`. Из-за этого
    в суточной сводке «stale 2» теперь «опоздание 2» (текст).
13. **«Последние закрытые»** читают пары `ledger.fetch_pairs` без лимита и берут последние пять (`ledger.py` менять
    нельзя). На сотнях пар это миллисекунды.
14. **Текущий % открытой сделки** считается по последней закрытой минутной свече (у бота нет клиента биржи) — так же,
    как в прежнем `/demo`.
15. Почасовая сводка придержанных сообщений тоже получает кнопку «💰 Счёт» и уходит без звука в тихие часы.

## Не сделано / нужно от владельца
* Развёртывание на сервере и пункты 3, 6, 7 приёмки (телефон: `/menu`, восемь кнопок в одном сообщении, «🏠 Меню»,
  «🔄 Обновить», девять команд в меню слева от поля ввода; следующая сделка — ✅/❌ и кнопка «💰 Счёт»; тихие часы
  в МСК) — проверяются вручную после выкладки. `bash deploy/verify_9_7.sh` — только чтение.
* Порядок выкладки: пересобрать и перезапустить `bot` и `demo`; миграций нет.
