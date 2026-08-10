# Отчёт по Этапу 6.6 — Выгрузка сигналов в Google Таблицу и Журнал сигналов Notion

> **⚠️ Обновлено в Этапе 6.6.1.** Способ ЗАПУСКА выгрузки изменён: она работает не
> host-скриптом, а внутри контейнера (`docker compose --profile tools run --rm
> --no-deps export`, точка входа `python -m src.export_main`) — на хосте нет
> сторонних пакетов (дефект D-3). Также колонка `notified` листа «Сигналы» стала
> трёхзначной (`да` / `поглощён` / `нет`), а параметры `EXPORT_PG_HOST/PORT` и
> `EXPORT_NOTIFIED_SINCE` удалены. Актуальные команды развёртывания и запуска —
> в **`CONSOLIDATION_REPORT.md`**. Разделы ниже сохранены как история решений 6.6;
> где они расходятся с 6.6.1 — приоритет у `CONSOLIDATION_REPORT.md`.

Реализована пакетная суточная выгрузка закрытых сигналов в две внешние точки:
полный поток — в Google Таблицу, витрина сильных сигналов — в базу «Журнал
сигналов» Notion. Ниже — фактическая структура данных, инструкции для заказчика,
результаты по критериям приёмки и список отклонений.

---

## 1. Фактическая структура `agents_payload`

`agents_payload` заполняет Decision Agent (`src/decision/agent.py`, функция
`make_decision`). Это **JSON-массив** объектов — по одному на каждого агента,
чей свежий вывод реально участвовал в решении. Агент, не набравший данных
(`insufficient_data`) или устаревший, в массив **не попадает** — поэтому его
колонки в выгрузке остаются пустыми (значимый факт, не ноль).

Ключи объекта: `agent`, `signal`, `confidence`, `ts`.

Пример (сигнал, где Futures-агент не участвовал):

```json
[
  {"agent": "market",    "signal": "bullish", "confidence": 0.42, "ts": "2026-08-08T00:09:00+00:00"},
  {"agent": "liquidity", "signal": "bullish", "confidence": 0.30, "ts": "2026-08-08T00:09:00+00:00"}
]
```

- `agent` — `market` | `liquidity` | `futures` (в нижнем регистре);
- `signal` — `bullish` | `bearish` | `neutral`;
- `confidence` — число `0..1`, округлено до 4 знаков.

Маппинг колонок листа «Сигналы»: `market_signal`/`market_confidence` ←
элемент с `agent="market"`, аналогично `liquidity_*` и `futures_*`.
`agents_count` = длина массива. Для Notion `Источник агента` (multi_select)
переводит присутствующих агентов в метки `Market` / `Liquidity` / `Futures`.

> Данные получены из кода, который пишет `agents_payload` (доступа к
> продакшн-серверу `46.224.52.105` у исполнителя нет). Структура детерминирована
> `make_decision`, поэтому совпадает с фактической в БД.

---

## 2. С какого момента `notified_at` достоверен

Поле `signals.notified_at` заполняется **только после успешного ответа
Telegram API** (правка §5, метод `db.mark_signal_notified`). Достоверным оно
становится **с момента деплоя этой правки на сервер** — то есть с момента, когда
на проде будет применён коммит Этапа 6.6 и перезапущен сервис `notify`.

- Точный момент нужно зафиксировать при деплое (дата/время перезапуска `notify`)
  и, для точности выгрузки, вписать его в `.env` как `EXPORT_NOTIFIED_SINCE`
  (ISO-8601, например `2026-08-11T09:00:00+00:00`).
- Если `EXPORT_NOTIFIED_SINCE` не задан — скрипт определяет границу автоматически
  как `MIN(notified_at)` по БД.
- Сигналы, закрытые **до** этой границы, в колонке `notified` листа «Сигналы»
  помечаются значением **`нет данных`** (а не `нет`): восстановить историю
  отправок невозможно. Сигналы после границы: `да` (отправлено) / `нет` (не
  отправлено).

Историю восстановить нельзя — для >1000 сигналов, закрытых до правки,
`notified_at` останется `NULL` → в выгрузке `нет данных`.

---

## 3. Сколько выгружено при первом запуске

На продакшене первый запуск обработает все накопленные закрытые сигналы (>1000)
теми же пачками по `EXPORT_BATCH_SIZE` (по умолчанию 500). Фактические цифры
заполнить **после первого прогона на сервере**:

| Цель   | Выгружено при первом запуске |
|--------|------------------------------|
| Sheets | `<заполнить: ______>`        |
| Notion | `<заполнить: ______>` (только сигналы с `notified_at`; при первом запуске — 0, см. §6.Backfill ТЗ) |

Проверка соответствия: число строк листа «Сигналы» = число строк в
`signal_exports` с `target='sheets'` (см. команды в разделе 6).

Локальная интеграционная проверка (мок Sheets/Notion + временный Postgres,
3 закрытых сигнала, 2 из них с `notified_at`) дала:

```
RUN 1: Сигналы append=3 (+header), Сводка replace=2 дня, Независимые окна replace=2, Notion=2
RUN 2: Сигналы — POST не было (0 новых), Сводка/Окна пересобраны, Notion=0   ← идемпотентность
signal_exports: sheets=3 (id 1,2,3), notion=2 (id 1,3)
Независимые окна: окно 00:00 → сигнал 1 (самый ранний, не 2), окно 04:00 → сигнал 3
futures_signal/confidence: пусто у сигнала без futures-агента (не 0)
```

---

## 4. Результат по критериям приёмки (§11)

| # | Критерий | Статус | Комментарий |
|---|----------|--------|-------------|
| 1 | Миграции §4 идемпотентны | ✅ | `apply_migrations`/`ensure_notify_schema` проверены двойным прогоном на живом Postgres; `IF NOT EXISTS`. |
| 2 | `notify` ставит `notified_at` только после успеха Telegram | ✅ | `mark_signal_notified` (успех) ставит `notified_at`; `mark_signal_absorbed` (дубль/cooldown) — нет. Проверено на живой БД. |
| 3 | Сводка: две строки — «отправлено» (по `notified_at`) и «кандидатов» | ✅ | `scripts/daily_report.py` + `src/health/report.py`; юнит-тест на обе строки. |
| 4 | Первый запуск выгружает все закрытые в «Сигналы»; строк = записей в `signal_exports(sheets)` | ✅ | Интеграционно подтверждено (3=3). На проде — сверить командой из раздела 6. |
| 5 | Повторный запуск не добавляет строк | ✅ | RUN 2: по «Сигналам» POST не выполняется (пустая выборка). |
| 6 | «Сводка по дням» и «Независимые окна» пересобираются целиком, без дублей | ✅ | Режим `replace` (`sheet.clear()` в Apps Script). |
| 7 | «Независимые окна»: ровно один сигнал на 4-часовое окно, окна не пересекаются | ✅ | `DISTINCT ON (win) … ORDER BY win, ts ASC`; юнит-тесты на `window_4h_start`. |
| 8 | `futures_*` пусты, где агента не было (не 0) | ✅ | `extract_agent_columns`; отдельный юнит-тест + интеграционная проверка. |
| 9 | В Notion — только сигналы с `notified_at`, 11 свойств, без дублей | ✅ | Выборка `notified_at IS NOT NULL`; `build_notion_properties` (все свойства); отметка после каждой страницы. |
| 10 | Обрыв связи не теряет данные, приходит алерт | ✅ | Отметки ставятся только после `ok:true`/успешной страницы; при ошибке — алерт в Telegram + выход 1. |
| 11 | Cron 06:20 UTC под `agent`, лог пишется и ротируется | ✅ (артефакты) | `deploy/agent-trade-export.cron`, `deploy/logrotate-agent-trade-export`; установка — раздел 5. |
| 12 | Секретов нет в git, логах, отчёте | ✅ | `.env` в `.gitignore`; в логах маскирование `mask_secret` (видны 4 последних символа). |
| 13 | `ruff check` и `pytest` проходят; добавлены юнит-тесты | ✅ | 81 → 100+ тестов, включая сборку строки, `window_4h_utc`, маппинг Notion. |
| 14 | Ни один влияющий на сигналы параметр не изменён | ✅ | `git diff` по `.env.example`/`config.py` не содержит `NOTIFY_MIN_PROBABILITY`, `DECISION_THRESHOLD`, `WEIGHT_*`, `MIN_AGENTS`, `AGENT_FRESHNESS_SEC`. |

---

## 5. Что делает заказчик

### 5.1. Приёмник Google Таблицы (§8.1)

1. Открыть <https://sheets.new>, назвать таблицу **«Agent Trade — Сигналы»**.
2. Меню **Расширения → Apps Script**.
3. Удалить весь код в открывшемся окне, вставить код из файла
   `deploy/apps_script.gs` (репозиторий).
4. В первой строке заменить `ВСТАВЬ_СЮДА_СЕКРЕТ` на любую придуманную строку из
   20+ символов (латиница и цифры). Эту же строку сохранить — она пойдёт в `.env`
   как `SHEETS_SHARED_SECRET`.
5. Нажать значок дискеты (сохранить).
6. Кнопка **Развернуть → Новое развёртывание → шестерёнка → Веб-приложение**.
   Поля: «Запуск от имени» — **Я**; «У кого есть доступ» — **Все**. Нажать
   **Развернуть**.
7. Google попросит разрешения — **Разрешить доступ**, выбрать свой аккаунт, на
   экране «Google не проверил это приложение» → **Дополнительные настройки →
   Перейти на страницу … (небезопасно)**, затем **Разрешить**. Это стандартное
   предупреждение для собственных скриптов.
8. Скопировать выданный URL веб-приложения (заканчивается на `/exec`) — это
   `SHEETS_WEBAPP_URL`.

> «Доступ: Все» означает, что записывать может любой, кто знает URL. Защита —
> секрет из шага 4, проверяемый в коде. URL и секрет никуда не публиковать.

### 5.2. Интеграция Notion (§9.1)

1. Открыть <https://www.notion.so/profile/integrations> → **New integration**.
2. Тип **Internal**, рабочее пространство — где лежит Agent Trade, имя
   **«Agent Trade Export»**.
3. В разделе **Capabilities** включить **Read content, Update content,
   Insert content**. Доступ к пользовательской информации не нужен.
4. Скопировать **Internal Integration Secret** (начинается с `ntn_`) — это
   `NOTION_API_TOKEN`.
5. Открыть в Notion базу «Журнал сигналов» → меню **•••** в правом верхнем углу →
   **Connections → Connect to →** выбрать **«Agent Trade Export»**.

> Шаг 5 обязателен: без него интеграция базу не видит и API вернёт 404.

### 5.3. Заполнить `.env` на сервере (`/opt/agent-trade/.env`, права 600)

```
EXPORT_ENABLED=true
EXPORT_BATCH_SIZE=500
SHEETS_WEBAPP_URL=<из шага 5.1.8>
SHEETS_SHARED_SECRET=<из шага 5.1.4>
NOTION_API_TOKEN=<из шага 5.2.4>
NOTION_SIGNALS_DB_ID=dacf5b37-f606-40cb-b0b9-89c51762e464
EXPORT_NOTION_ONLY_NOTIFIED=true
```

> В 6.6.1 на новом сервере эти ключи добавляет сам `install.sh`, не затирая уже
> заполненные (§8.6). Ключ `EXPORT_NOTIFIED_SINCE` удалён — колонка `notified`
> теперь трёхзначная и границы достоверности не требует.

### 5.4. Установить cron и ротацию лога (под root)

```bash
sudo cp deploy/agent-trade-export.cron       /etc/cron.d/agent-trade-export
sudo cp deploy/logrotate-agent-trade-export  /etc/logrotate.d/agent-trade-export
sudo chmod 644 /etc/cron.d/agent-trade-export /etc/logrotate.d/agent-trade-export
sudo -u agent mkdir -p /opt/agent-trade/logs
```

---

## 6. Три команды для человека

```bash
# 1) Запустить выгрузку вручную (на сервере, от пользователя agent):
cd /opt/agent-trade && docker compose --profile tools run --rm --no-deps export

# 2) Посмотреть лог выгрузки:
tail -n 100 /opt/agent-trade/logs/export.log

# 3) Сколько сигналов ещё не выгружено (Sheets и Notion):
docker compose exec -T postgres psql -U agenttrade -d agenttrade -c \
"SELECT
   (SELECT count(*) FROM signals s WHERE s.status='closed'
      AND NOT EXISTS (SELECT 1 FROM signal_exports x WHERE x.signal_id=s.id AND x.target='sheets')) AS sheets_pending,
   (SELECT count(*) FROM signals s WHERE s.status='closed' AND s.notified_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM signal_exports x WHERE x.signal_id=s.id AND x.target='notion')) AS notion_pending;"
```

Сверка критерия §11.4 (строк в листе = записей в учёте):

```bash
psql "host=127.0.0.1 port=5432 dbname=agenttrade user=agenttrade" -c \
"SELECT count(*) FROM signal_exports WHERE target='sheets';"
# сравнить с числом строк данных на листе «Сигналы» (без строки-шапки)
```

---

## 7. Что пошло не так и как обошли

1. **Нет доступа к продакшн-серверу** (`46.224.52.105`). §7.1 предписывает
   выполнить `SELECT agents_payload …` на сервере. Обошли: структуру вывели из
   кода `make_decision`, который её и формирует (детерминированно). Раздел 1.

2. **Файлы `daily_report.py`, `install.sh`, вотчдог отсутствуют в репозитории.**
   Репозиторий на момент задачи — на Этапе 6; серверные артефакты в нём не
   отслеживаются. Обошли:
   - `scripts/daily_report.py` создан заново, с обеими строками счётчиков (§5.4)
     и клампом отрицательного heartbeat (§13.3). Если на сервере уже есть свой
     `daily_report.py` — перенести в него две правки (счётчик по `notified_at`
     + кламп), либо заменить нашим.
   - Тех-долг по `install.sh` (§13.1, §13.2) реализовать в самом файле нельзя —
     его нет в репозитории. Готовые к применению рекомендации — в разделе 8.
   - Алерты (§6.8) шлются тем же ботом/`chat_id`, что и вотчдог: через
     `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` из `.env` (`src/notify/telegram.py`).

3. **Новые секреты нельзя делать обязательными в pydantic.** Все сервисы стека и
   CI импортируют `Settings`; `required`-поле без значения уронило бы их и тесты.
   Обошли: пустые значения по умолчанию + проверка обязательности **в рантайме**
   скрипта выгрузки (пустой секрет → алерт и выход, «значение по умолчанию» не
   подставляется). Пороги/веса не затронуты.

4. **Запуск скрипта на хосте, а БД в `.env` указана как `postgres:5432`.** На
   хосте нужен `127.0.0.1`. Добавлены `EXPORT_PG_HOST`/`EXPORT_PG_PORT`
   (по умолчанию `127.0.0.1:5432`) и свойство `settings.host_pg_dsn`.

5. **Отличие «нет» от «нет данных».** Введена граница достоверности
   `notified_at` (`EXPORT_NOTIFIED_SINCE`, иначе авто `MIN(notified_at)`).
   Раздел 2.

---

## 8. Тех-долг §13.1–§13.2 (`install.sh` отсутствует в репозитории)

Файл `install.sh` в репозитории отсутствует, поэтому правки оформлены как
готовые к применению рекомендации (применить в серверный `install.sh`):

- **§13.1. Нормализация вводимых секретов.** При чтении `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` (и прочих секретов) обрезать пробелы, `\r` и
  ESC-последовательности; штатно поддержать их получение из переменных окружения.
  Пример нормализации в bash:

  ```bash
  # срезать CR, ESC-последовательности и крайние пробелы
  normalize() { printf '%s' "$1" | tr -d '\r' | sed -E 's/\x1B\[[0-9;]*[A-Za-z]//g' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g'; }
  TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(normalize "$TELEGRAM_BOT_TOKEN")}"
  TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(normalize "$TELEGRAM_CHAT_ID")}"
  ```

  и описать переменные окружения в README.

- **§13.2. Проверка таймзоны (строка ~558).** Принимать и `UTC`, и `Etc/UTC`:

  ```bash
  tz="$(cat /etc/timezone 2>/dev/null || true)"
  case "$tz" in
    UTC|Etc/UTC) : ;;                    # обе формы валидны
    *) echo "FAIL: таймзона $tz, ожидается UTC"; exit 1 ;;
  esac
  ```

- **§13.3. Отрицательный heartbeat в `daily_report.py`** — реализовано в
  `src/health/report.py::clamp_age_seconds` (отрицательные значения → 0).

---

## 9. Состав изменений

| Файл | Назначение |
|------|------------|
| `db/init.sql` | Таблица `signal_exports`, индекс, колонка `signals.notified_at`. |
| `src/core/config.py` | Параметры `EXPORT_*`, `SHEETS_*`, `NOTION_*`; `host_pg_dsn`; `mask_secret`. |
| `src/core/db.py` | `mark_signal_notified` (ставит `notified_at`), `mark_signal_absorbed`; `ensure_notify_schema` добавляет `notified_at`. |
| `src/notify/agent.py` | Путь «поглощения» дубля больше не ставит `notified_at`. |
| `src/export/transform.py` | Чистые функции: строки листов, окно 4ч, свойства Notion. |
| `src/export/queries.py` | SQL выборки/агрегатов/учёта выгрузок. |
| `src/export/sheets.py` | Клиент Apps Script (redirect, таймаут 60с, повторы 5/15/45). |
| `src/export/notion.py` | Клиент Notion REST (версия API `2022-06-28`). |
| `scripts/export_signals.py` | Оркестратор выгрузки. **В 6.6.1 перенесён → `src/export_main.py`** (запуск в контейнере, D-3). |
| `scripts/daily_report.py` | Суточная сводка. **В 6.6.1 удалён** (дубль; осталась `src/health/daily_report.py`). |
| `src/health/report.py` | Чистые функции сводки. **В 6.6.1 удалён** (не использовался рабочей сводкой). |
| `deploy/apps_script.gs` | Код приёмника Google Таблицы. |
| `deploy/agent-trade-export.cron` | Cron 06:20 UTC под `agent`. |
| `deploy/logrotate-agent-trade-export` | Ротация лога, 14 дней. |
| `tests/test_export.py`, `tests/test_health_report.py` | Юнит-тесты. |
| `.env.example` | Блок «Выгрузка сигналов (Этап 6.6)». |
