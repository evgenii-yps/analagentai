# Отчёт по Этапу 6.6.1 — Консолидация веток и доводка выгрузки

Свод всего кода в единую ветку `main`, устранение дублирования и блокирующего
дефекта запуска (D-3), доводка выгрузки до рабочего состояния на реальном сервере.

---

## 1. Ветки репозитория и итоговый состав `main`

### `git branch -r` (после `git fetch --all`)

```
origin/claude/agent-trade-infra-stage-1-7hb1p0     # старая дефолтная, Этапы 1–6, инфраструктуры нет
origin/claude/deployment-installer-script-k9e6t4   # продакшн-инфраструктура (с неё работает сервер)
origin/claude/explain-signal-script-7ms5p7         # старая, не актуальна
origin/claude/local-stack-exchange-audit-so6rjz    # Этап 6.4, не актуальна
origin/claude/signals-export-sheets-notion-ovib81  # выгрузка 6.6
```

Состояние совпало с зафиксированным в ТЗ. Ветка `main` создана **от
`claude/deployment-installer-script-k9e6t4`** (продакшн — точка отсчёта) и в неё
влита `claude/signals-export-sheets-notion-ovib81` (выгрузка).

### Итоговый состав `main` (ключевое)

Инфраструктура установщика (все 11 файлов на месте):

```
deploy/install.sh              deploy/bootstrap            deploy/agent-trade.service
src/health/daily_report.py     scripts/watchdog.py         scripts/backup_db.sh
scripts/retention.py           scripts/geo_check.py        README.md (раздел развёртывания)
docker-compose.yml (изм.)      src/health/__init__.py
```

Выгрузка сигналов (6.6, с правками 6.6.1):

```
src/export/{transform,queries,sheets,notion}.py   src/export_main.py (новая точка входа)
deploy/apps_script.gs   deploy/agent-trade-export.cron   deploy/logrotate-agent-trade-export
tests/test_export.py    EXPORT_REPORT.md
```

---

## 2. Разрешение конфликтов слияния

Конфликты возникли ровно в двух файлах, как и предсказывалось.

- **`README.md`** — обе ветки добавляли разделы. Сохранены **оба**: блок
  «🚀 Запуск установщика (развёртывание на сервере)» из ветки установщика и абзац
  по Этапу 6.6 в списке этапов. Дерево структуры проекта пересобрано в один
  непротиворечивый листинг (установщик + выгрузка), счётчик таблиц БД — 10.

- **`src/health/__init__.py`** — обе ветки создавали файл (add/add). Docstring
  объединён: «Служебные проверки состояния системы, суточная сводка и утилиты
  отчётности» (Этап 6.5 + 6.6).

---

## 3. Аудит cron-скриптов на сторонние импорты (§3)

Проверен каждый файл, запускаемый из `/etc/cron.d/agent-trade`, на импорт
пакетов, которых нет на хосте (только в Docker-образе):

| Скрипт | Импорты | Вердикт |
|--------|---------|---------|
| `src/health/daily_report.py` | json, os, re, subprocess, urllib, datetime, ssl | ✅ только stdlib |
| `scripts/retention.py` | os, subprocess, sys, time, datetime | ✅ только stdlib |
| `scripts/watchdog.py` | os, subprocess, sys, urllib, json, datetime, ssl | ✅ только stdlib |
| `scripts/geo_check.py` | base64, json, os, socket, ssl, struct, sys, urllib | ✅ только stdlib |
| `scripts/backup_db.sh` | — (bash) | ✅ |
| `scripts/export_signals.py` (было) | asyncpg, structlog, httpx, pydantic | ❌ **дефект D-3** |

Итог: дефект D-3 был **только** у выгрузки. Все остальные регламентные скрипты
корректно написаны на стандартной библиотеке и ходят к БД/сети через `docker
compose exec`/`psql`/`urllib`. Исправление — только для выгрузки (раздел ниже).

Проверка запуском чистым системным Python (без venv), см. §5, критерии 5–6.

---

## 4. Что стало с `src/health/report.py`

**Удалён.** Это был вспомогательный модуль ветки выгрузки (чистый stdlib —
`datetime`), но использовался он только из `scripts/daily_report.py`, который тоже
удалён (дубль суточной сводки, дефект D-3). Из рабочей `src/health/daily_report.py`
он не вызывался. По правилу §6 («оставить только если чисто stdlib И реально
используется из daily_report.py, иначе — встроить и удалить») — удалён вместе с
`scripts/daily_report.py` и своим тестом `tests/test_health_report.py`.

Нужная из него функция — кламп отрицательного heartbeat — реализована прямо в
`src/health/daily_report.py` (stdlib, `max(0.0, age)`), см. §8.4.

---

## 5. Результат по критериям приёмки (§10)

| Критерий | Статус | Подтверждение |
|----------|--------|---------------|
| Вывод `git branch -r` в отчёте | ✅ | §1. |
| Ветка `main` создана и запушена; инфраструктура + выгрузка | ✅ | §1; `git ls-tree main`. |
| `install.sh`, `daily_report.py`, `watchdog.py`, `backup_db.sh`, `retention.py`, `geo_check.py`, `agent-trade.service` в `main` | ✅ | §1. |
| `scripts/daily_report.py` удалён; сводка ровно одна | ✅ | `git rm`; осталась `src/health/daily_report.py`. |
| `daily_report.py` без сторонних импортов, проверено чистым Python | ✅ | §6, команда 1 (вывод ниже). |
| То же для `retention.py` и `watchdog.py` | ✅ | §6, команды 2–3. |
| Выгрузка через `docker compose --profile tools run --rm --no-deps export`; sys.path и host-DSN удалены | ✅ | `src/export_main.py`; интеграционный прогон против живого Postgres. |
| `docker compose up -d` = ровно 7 контейнеров, `export` не входит | ✅ | `docker compose config --services` → 7; с `--profile tools` появляется `export`. |
| Ошибка выгрузки → код 1 на уровне процесса + алерт | ✅ | `python -m src.export_main` при недоступном Sheets вернул `exit=1`. |
| Сводка: три строки (отправлено / поглощено / кандидатов) | ✅ | `src/health/daily_report.py` строки счётчиков. |
| Нет отрицательных задержек heartbeat | ✅ | `age = max(0.0, ...)`. |
| Проверка таймзоны принимает `Etc/UTC` | ✅ | §8.3, самопроверка `[[ tz == UTC \|\| tz == Etc/UTC ]]`. |
| `install.sh` ставит cron выгрузки и logrotate; повторный запуск не затирает `.env` | ✅ | cron-строка добавлена; `write_env` использует `${VAR:-default}` (тест merge). |
| Лист «Сводка по дням»: `wait` ненулевой; колонка `notified` трёхзначная | ✅ | Интеграционный прогон: `wait=1`; `notified` = да/поглощён/нет. |
| `ruff check .` и `pytest` зелёные | ✅ | ruff — clean; pytest — 76 passed. |
| Замороженные параметры §4 не изменены; `should_notify()` не тронута | ✅ | §7 (git diff). |

### Проверка «чистым системным Python» (критерии 5–6)

Реальный импорт каждого модуля под bare `env -i /usr/bin/python3.12` (без venv,
без `PYTHONPATH`, сторонних пакетов в окружении нет):

```
OK imported (stdlib only): src/health/daily_report.py
OK imported (stdlib only): scripts/retention.py
OK imported (stdlib only): scripts/watchdog.py
OK imported (stdlib only): scripts/geo_check.py
ALL CRON SCRIPTS IMPORT WITHOUT THIRD-PARTY PACKAGES
```

Контрольный контраст — `src/export_main.py` под тем же чистым Python **обязан**
падать (значит, ему действительно нужен контейнер):

```
$ env -i /usr/bin/python3.12 -c "import sys; sys.path.insert(0,'.'); import src.export_main"
ModuleNotFoundError: No module named 'asyncpg'
```

---

## 6. Замороженные параметры и `should_notify` (§4)

`git diff` ветки установщика → `main` по `.env.example` и `src/core/config.py` не
содержит правок `NOTIFY_MIN_PROBABILITY`, `NOTIFY_COOLDOWN_SEC`, `NOTIFY_INTERVAL`,
`DECISION_THRESHOLD`, `DECISION_INTERVAL`, `MIN_AGENTS`, `AGENT_FRESHNESS_SEC`,
`WEIGHT_*`, `EVAL_HORIZONS`, `EVAL_PRIMARY_HORIZON`.

`src/notify/agent.py` изменён ровно в одной строке — путь «поглощения» дубля
теперь зовёт `db.mark_signal_absorbed` вместо `db.mark_signal_notified` (чтобы
`notified_at` не ставился без реальной отправки). Тело `should_notify()` не
тронуто.

---

## 7. Как исправлен дефект D-3 (выгрузка не запускалась)

1. Точка входа перенесена `scripts/export_signals.py` → **`src/export_main.py`**
   (каталог `src/` копируется в образ `Dockerfile`, все зависимости и сеть там
   есть). `scripts/export_signals.py` удалён.
2. Удалён хак с `sys.path` — при `python -m src.export_main` он не нужен.
3. Удалены `EXPORT_PG_HOST`, `EXPORT_PG_PORT`, свойство `host_pg_dsn`. Внутри сети
   compose БД видна как `postgres:5432` — используется штатный `settings.pg_dsn`.
4. В `docker-compose.yml` добавлен сервис `export`: тот же образ и `env_file`,
   `profiles: ["tools"]` (не поднимается обычным `up -d`), `restart: "no"`,
   `command: python -m src.export_main`, `depends_on: [postgres, redis]`.
5. Cron-строка (06:20 UTC) переведена на контейнер:
   `docker compose --profile tools run --rm --no-deps export`. `--no-deps` не
   дёргает работающие postgres/redis; код возврата контейнера пробрасывается
   наружу (cron видит 1 при ошибке).
6. Проверено фактически: при недоступном приёмнике процесс завершается кодом 1.

---

## 8. Что делает заказчик

### 8.1. Смена ветки по умолчанию в GitHub на `main`

У исполнителя нет прав владельца репозитория, поэтому переключение делает
заказчик (после того, как убедится, что развёртывание с `main` прошло успешно):

1. GitHub → репозиторий `evgenii-yps/analagentai` → **Settings**.
2. Слева **General** (открывается по умолчанию) → секция **Default branch**.
3. Нажать значок ⇄ (переключатель веток) → выбрать **`main`** → **Update**.
4. Подтвердить во всплывающем окне (**I understand, update the default branch**).

Старые ветки не удаляются — остаются архивом. Вся дальнейшая работа — от `main`.

### 8.2. Развёртывание на действующий сервер (Hetzner, веб-консоль)

> Сервер работает непрерывно, в БД — данные пилота. **Перезапуск стека = пауза в
> сборе данных примерно на 1–3 минуты** (пересборка образа + перезапуск 7
> контейнеров). Проводить в спокойное время. Все команды — под `root` в консоли
> Hetzner (**Servers → agent-trade → Console**).

```bash
# 0. Перейти в каталог проекта.
cd /opt/agent-trade
#    Ожидается: приглашение сменится на .../agent-trade.

# 1. БЭКАП БД перед любыми действиями (данные пилота бесценны).
sudo -u agent /opt/agent-trade/scripts/backup_db.sh
#    Ожидается: строка «Бэкап сохранён: /opt/agent-trade/backups/agenttrade_...sql.gz».

# 2. Копия .env в безопасное место (потеря .env = потеря доступа к БД).
cp /opt/agent-trade/.env /root/agent-trade.env.backup && ls -l /root/agent-trade.env.backup
#    Ожидается: строка со свежесозданным файлом-копией.

# 3. Запомнить текущую ветку — понадобится для отката.
git rev-parse --abbrev-ref HEAD | tee /root/agent-trade.prevbranch
#    Ожидается: claude/deployment-installer-script-k9e6t4

# 4. Забрать свежий код и переключиться на main.
#    .env (в .gitignore) и тома Docker при этом НЕ затрагиваются.
git fetch origin && git checkout main && git pull origin main
#    Ожидается: «Switched to branch 'main'» и обновление файлов без ошибок.
#    Если git ругается на локальные изменения — сначала `git stash`, потом повторить.

# 5. Добавить недостающие ключи выгрузки в .env, НЕ трогая заполненные.
for kv in "EXPORT_ENABLED=true" "EXPORT_BATCH_SIZE=500" "SHEETS_WEBAPP_URL=" \
          "SHEETS_SHARED_SECRET=" "NOTION_API_TOKEN=" \
          "NOTION_SIGNALS_DB_ID=dacf5b37-f606-40cb-b0b9-89c51762e464" \
          "EXPORT_NOTION_ONLY_NOTIFIED=true"; do
  k="${kv%%=*}"; grep -q "^${k}=" /opt/agent-trade/.env || echo "$kv" >> /opt/agent-trade/.env
done
#    Ожидается: команда без вывода. Отсутствующие ключи добавлены, существующие целы.

# 6. Вписать РЕАЛЬНЫЕ значения приёмников (из EXPORT_REPORT.md §5.1/§5.2).
sudo -u agent nano /opt/agent-trade/.env
#    Заполнить SHEETS_WEBAPP_URL, SHEETS_SHARED_SECRET, NOTION_API_TOKEN. Ctrl+O, Enter, Ctrl+X.

# 7. Пересобрать образ (появился src/export_main.py) и перезапустить стек.
#    ВНИМАНИЕ: здесь начинается пауза в сборе данных ~1–3 минуты.
docker compose build && docker compose up -d
#    Ожидается: «Container agent-trade-...-1  Started» для 7 сервисов.

# 8. Установить cron выгрузки и ротацию лога (шаблоны в deploy/).
sudo cp deploy/agent-trade-export.cron       /etc/cron.d/agent-trade-export
sudo cp deploy/logrotate-agent-trade-export  /etc/logrotate.d/agent-trade-export
sudo chmod 644 /etc/cron.d/agent-trade-export /etc/logrotate.d/agent-trade-export
sudo -u agent mkdir -p /opt/agent-trade/logs
#    Ожидается: команды без вывода.

# 9. Первый запуск выгрузки ВРУЧНУЮ + просмотр лога.
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose --profile tools run --rm --no-deps export'
#    Ожидается: в конце «Выгрузка завершена успешно». При ошибке — алерт в Telegram
#    и код возврата 1; данные не теряются, повтор на следующем запуске.

# 10. Проверить, что 7 контейнеров Up (healthy) и сбор продолжается.
docker compose ps
#     Ожидается: postgres, redis, collector, agents, decision, notify, evaluator — все Up (healthy).
#     Сервиса export здесь быть НЕ должно (он разовый, с профилем tools).

# 11. Убедиться, что данные продолжают притекать (по желанию).
docker compose exec -T postgres psql -U agenttrade -d agenttrade -c \
  "SELECT count(*) FROM ohlcv WHERE ts > now() - interval '5 minutes';"
#     Ожидается: число > 0 (свечи за последние 5 минут появляются).
```

#### Откат, если что-то пошло не так

```bash
cd /opt/agent-trade
git checkout "$(cat /root/agent-trade.prevbranch)"     # вернуться на прежнюю ветку
cp /root/agent-trade.env.backup /opt/agent-trade/.env  # вернуть прежний .env
docker compose build && docker compose up -d           # пересобрать и перезапустить
#    БД в томе Docker не тронута ни при переключении веток, ни при откате.
#    При катастрофе БД — восстановление из бэкапа шага 1 (см. scripts/backup_db.sh).
```

> Альтернатива шагам 5–8: повторно запустить `sudo bash deploy/install.sh` —
> установщик идемпотентен, сам добавит ключи выгрузки в `.env` (не затирая
> заполненные), пропишет cron выгрузки и logrotate, пересоберёт и перезапустит
> стек. Минус — он заново прогонит блокирующий гео-тест OKX и настройку ОС.

---

## 9. Что пошло не так и как обошли

1. **Ветки разошлись, продакшн-код не был в дефолтной ветке.** Исполнитель 6.6 не
   увидел инфраструктуру и написал часть заново (host-скрипт сводки, host-cron).
   Обошли консолидацией: `main` от продакшн-ветки, слияние выгрузки, удаление
   дублей.

2. **Дефект D-3 (выгрузка падала бы `ModuleNotFoundError`).** Причина —
   host-запуск скрипта со сторонними импортами; в образ каталог `scripts/` не
   копируется. Обошли переносом в контейнер (`src/export_main.py`, сервис `export`
   с профилем `tools`). См. §7.

3. **Две суточные сводки после слияния.** Оставлена рабочая
   `src/health/daily_report.py` (stdlib), удалены `scripts/daily_report.py` и
   `src/health/report.py` (дубль + дефект D-3).

4. **Счётчик «отправлено уведомлений» был завышен (416 против 10–50).** Считался
   по флагу `notified`, который ставится и при поглощении дубля. Разнесли на три
   строки: отправлено (`notified_at`), поглощено (`notified AND notified_at IS
   NULL`), кандидаты (`decision<>'wait' AND probability>=порог`).

5. **Колонка `wait` в «Сводке по дням» была бы нулевой.** `evaluator` закрывает
   только направленные сигналы. Проверено: сводка строится по ВСЕМ сигналам суток
   (`decisions_total/buy/sell/wait/candidates`), а `notified`/`closed`/успешность —
   только по закрытым. Прогон показал `wait=1`.

6. **`notified` на листе «Сигналы» сделан трёхзначным** (`да` / `поглощён` /
   `нет`) — это заменило прежнюю логику «нет данных»/`EXPORT_NOTIFIED_SINCE`,
   которая удалена как избыточная.

7. **Тех-долг `install.sh`** (§8 ТЗ): нормализация вставленных секретов
   (bracketed-paste/`\r`/пробелы — реальный инцидент из PowerShell), явная
   поддержка `TELEGRAM_*` из окружения, приём таймзоны `Etc/UTC`, добавление cron
   выгрузки и logrotate, неразрушающее дополнение `.env`. Реализовано кодом,
   проверено локально (`bash -n`, юнит-тесты нормализации и merge).

8. **Смена дефолтной ветки в GitHub** требует прав владельца — у исполнителя их
   нет. Дана пошаговая инструкция (§8.1); переключение делает заказчик.
