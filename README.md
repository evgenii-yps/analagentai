# Agent Trade — Сбор рыночных данных

Мультиагентная система анализа крипторынка.
Пайплайн (на будущее): `Collectors → Storage → AI-агенты → Decision Agent → Risk Manager → Notifications`.

- **Этап 1 (инфраструктура):** Docker-окружение, схема PostgreSQL, слой доступа к БД,
  клиент Redis, типизированный конфиг, структурное логирование, health-check и CI.
- **Этап 2 (сбор данных):** круглосуточный сервис-коллектор на ccxt (REST-опрос Binance):
  свечи OHLCV, снимки стакана, сделки, funding rate и open interest. Аналитики нет —
  только сбор и хранение.
- **Этап 3 (первые агенты):** три независимых аналитических агента (Market, Liquidity,
  Futures), каждый читает только свои данные и пишет независимое заключение в
  `agent_outputs`.
- **Этап 4 (Decision Agent):** агрегирующий агент читает ТОЛЬКО `agent_outputs`
  (не сырые данные), взвешивает свежие выводы трёх агентов и пишет итоговое решение
  (`buy`/`sell`/`wait`) с вероятностью в таблицу `signals`.
- **Этап 5 (уведомления):** сервис читает новые решения из `signals` и шлёт в Telegram
  ТОЛЬКО сильные сигналы (`decision ≠ wait`, `probability ≥ NOTIFY_MIN_PROBABILITY`,
  без повторов и спама).
- **Этап 6 (анализ результатов):** сервис-оценщик дооценивает сигналы фактом движения
  цены на горизонтах 1ч/4ч/12ч/24ч (pnl%, просадка, success) и пишет в
  `signal_evaluations`; по главному горизонту (4ч) заполняет сводку в `signals`
  и закрывает сигнал. Сигнал при этом ОДИН: горизонт влияет только на оценку
  исхода (Этап 8.1 §5).
- **Этап 6.6 (выгрузка сигналов):** суточная пакетная выгрузка закрытых сигналов
  наружу — полный поток в Google Таблицу (лист «Сигналы» + служебные «Сводка по дням»
  и «Независимые окна»), витрина сильных сигналов (с фактически отправленным
  уведомлением) — в базу «Журнал сигналов» Notion. Host-скрипт `scripts/export_signals.py`
  идемпотентен, ведёт учёт в `signal_exports`, при ошибке шлёт алерт в Telegram и не
  теряет данные. Настройка приёмников и cron — в `EXPORT_REPORT.md`.

## 🚀 Запуск установщика (развёртывание на сервере)

Система разворачивается на сервере **одной командой**. Установщик сам настроит
ОС и безопасность, установит Docker, поднимет все сервисы и включит автозапуск,
бэкапы и мониторинг. От вас нужно только вставить команду и ввести два значения
для Telegram.

> Репозиторий **публичный**, поэтому токен GitHub для установки **не нужен**.

### Шаг 1. Заранее сделайте Telegram-бота

1. В Telegram напишите **@BotFather** → команда `/newbot` → придумайте имя →
   BotFather пришлёт **токен** вида `123456789:AA...` — это `TELEGRAM_BOT_TOKEN`.
2. Напишите боту **@userinfobot** — он пришлёт ваш числовой **Id**. Это
   `TELEGRAM_CHAT_ID` (чтобы получать уведомления в личку).

### Шаг 2. Откройте консоль сервера

Hetzner Cloud → **Servers → agent-trade → Console**. Войдите под пользователем
`root` (пароль — из письма Hetzner при создании сервера).

### Шаг 3. Вставьте команду и нажмите Enter

```bash
curl -fsSL https://raw.githubusercontent.com/evgenii-yps/analagentai/claude/deployment-installer-script-k9e6t4/deploy/install.sh | sudo bash
```

### Шаг 4. Ответьте на запросы

Установщик попросит:

- `TELEGRAM_BOT_TOKEN` — вставьте токен (ввод **скрыт**, символы не отображаются);
- `TELEGRAM_CHAT_ID` — вставьте ваш Id.

Он сразу отправит тестовое сообщение в Telegram. Если сообщение не пришло —
установщик попросит ввести значения заново.

### Шаг 5. Дождитесь «ГОТОВО»

В конце появится строка **«ГОТОВО»** и **пароль PostgreSQL** — он показывается
**один раз**, обязательно сохраните его (он также записан в
`/opt/agent-trade/DEPLOY_REPORT.md`).

### Что делает установщик

Порядок шагов (все идемпотентны — повторный запуск безопасен):

1. **Предусловия** — проверка ОС (Ubuntu 24.04), прав root и доступа в интернет.
2. **Доставка кода** — клонирование репозитория в `/opt/agent-trade`.
3. **Гео-тест OKX** (блокирующий) — если OKX недоступна из локации сервера
   (коды 451/403 или нет данных по WebSocket), установка **останавливается** до
   развёртывания.
4. **Секреты** — скрытый ввод Telegram-токена и chat_id с проверкой; пароль
   PostgreSQL генерируется автоматически (32 символа).
5. **ОС и безопасность** — таймзона UTC, пользователь `agent` с sudo, firewall
   UFW (наружу открыт только `22/tcp`; порты `5432`/`6379` закрыты навсегда),
   `fail2ban`, автоматические security-обновления без автоперезагрузки, swap
   2 ГБ, Docker Engine + Compose из официального репозитория.
6. **Развёртывание стека** — сборка `.env` (в т.ч. `EXCHANGE=okx`), запуск всех
   контейнеров, проверка health-check и наличия таблиц в БД.
7. **Эксплуатация** — автозапуск (systemd), ежедневный бэкап БД, политика
   хранения данных, суточная сводка в Telegram, вотчдог (перезапуск упавших
   сервисов) — через cron под пользователем `agent`.
8. **Самопроверка и отчёт** — итоговые проверки и `DEPLOY_REPORT.md`.

Секреты нигде не логируются, не коммитятся и не попадают в отчёт (единственное
исключение — пароль БД в `DEPLOY_REPORT.md`, §13.4). Полный лог установки —
`/var/log/agent-trade-install.log`.

### Если репозиторий станет приватным (создание токена GitHub)

Установщик сам определяет тип репозитория. Для приватного он попросит
**fine-grained токен только на чтение**. Как его создать:

1. GitHub → аватар справа-вверху → **Settings**.
2. Слева внизу **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
3. **Repository access** → *Only select repositories* → выберите
   `evgenii-yps/analagentai`.
4. **Permissions** → *Repository permissions* → **Contents: Read-only**.
5. **Generate token** и скопируйте строку `github_pat_...`.
6. Вставьте её, когда установщик попросит токен (ввод скрыт). В лог и отчёт
   токен не попадает.

## Стек

- Python 3.12 (asyncio)
- PostgreSQL 16-alpine, Redis 7-alpine
- asyncpg, redis-py, pydantic-settings, structlog, ccxt
- pytest + pytest-asyncio, ruff

## Структура

```
.
├── docker-compose.yml      # postgres + redis + collector + agents + decision + notify + evaluator + bot
├── Dockerfile              # образ приложения (python:3.12-slim)
├── .env.example            # пример переменных окружения
├── pyproject.toml          # метаданные, ruff, pytest
├── requirements.txt        # закреплённые версии для Docker/CI
├── db/init.sql             # схема БД (10 таблиц + индексы)
├── deploy/                 # развёртывание и эксплуатация
│   ├── install.sh          # главный идемпотентный установщик
│   ├── bootstrap           # команда-однострочник для запуска установщика
│   ├── agent-trade.service # systemd-юнит автозапуска стека
│   ├── apps_script.gs      # код приёмника Google Таблицы (выгрузка 6.6)
│   ├── agent-trade-export.cron        # шаблон cron-записи выгрузки
│   ├── agent-trade-risk.cron          # шаблон cron-записи пересчёта целей (8.2)
│   ├── verify_8_2.sh                  # проверка развёртывания Этапа 8.2
│   └── logrotate-agent-trade-export   # ротация лога выгрузки
├── scripts/                # вспомогательные скрипты эксплуатации (хост, stdlib)
│   ├── geo_check.py        # блокирующий гео-тест OKX (REST + WebSocket, stdlib)
│   ├── backup_db.sh        # ежедневный бэкап БД с ротацией
│   ├── retention.py        # политика хранения (очистка старых сырых данных)
│   ├── precheck_8_2.sql    # предпроверка глубины и целостности свечей (Этап 8.2 §1)
│   ├── backfill_8_2.py     # разовая догрузка 95 суток часовых свечей спота (8.2 §2)
│   └── watchdog.py         # вотчдог: перезапуск упавших сервисов + алерт
├── src/
│   ├── main.py             # точка входа — сервис-коллектор
│   ├── agents_main.py      # точка входа — сервис агентов
│   ├── decision_main.py    # точка входа — Decision Agent
│   ├── notify_main.py      # точка входа — сервис уведомлений
│   ├── evaluator_main.py   # точка входа — оценщик результатов
│   ├── export_main.py      # точка входа — выгрузка сигналов (docker compose run)
│   ├── bot_main.py         # точка входа — телеграм-бот только на чтение (Этап 6.7)
│   ├── risk_main.py        # точка входа — пересчёт целей по вероятности (Этап 8.2)
│   ├── healthcheck.py      # CLI-проверка PG и Redis
│   ├── core/
│   │   ├── config.py       # Settings (pydantic-settings)
│   │   ├── db.py           # пул asyncpg + методы доступа/записи/чтения
│   │   ├── redis_client.py # async-клиент Redis
│   │   ├── exchange.py     # фабрика ccxt-клиента
│   │   └── logging.py      # настройка structlog
│   ├── collectors/         # коллекторы OHLCV/стакан/сделки/funding+OI
│   ├── agents/             # Market/Liquidity/Futures + BaseAgent
│   ├── decision/           # DecisionAgent + логика агрегации
│   ├── notify/             # NotifyAgent + should_notify + Telegram
│   ├── bot/                # телеграм-бот на чтение: poller/handlers/queries/runner
│   ├── evaluator/          # compute_evaluation + класс Evaluator
│   ├── risk/               # цели по вероятности (Этап 8.2)
│   │   ├── targets.py      # MFE по касанию, 40-й процентиль, покрытие издержек
│   │   ├── quality.py      # предпроверка ряда свечей (пороги §1)
│   │   └── runner.py       # суточный пересчёт risk_targets
│   ├── export/             # выгрузка сигналов (Этап 6.6)
│   │   ├── transform.py    # чистые функции: строки листов, окно 4ч, свойства Notion
│   │   ├── queries.py      # SQL выборки/агрегатов/учёта выгрузок
│   │   ├── sheets.py       # клиент Google Apps Script (redirect, повторы)
│   │   └── notion.py       # клиент Notion REST API
│   └── health/
│       └── daily_report.py # суточная сводка о состоянии системы (хост, stdlib)
└── tests/                  # тесты конфига, коллекторов, агентов, решений, уведомлений, оценки, выгрузки
```

## Сбор данных (Этап 2)

Сервис-коллектор подключается к Binance через ccxt (REST-опрос) и непрерывно
пишет данные в таблицы из Этапа 1. Спотовые данные (OHLCV, сделки, стакан) берутся
со spot-рынка, funding и open interest — с бессрочного фьючерса (swap). В таблице
`instruments` создаётся две записи на токен: `spot` и `swap`.

Каждый коллектор работает в своём цикле и не падает при ошибках сети/API
(ошибка логируется как warning, цикл продолжается). После каждой успешной итерации
пишется heartbeat в Redis: `collector:heartbeat:{name}` (TTL 300 сек).

Состав инструментов задаётся `SYMBOLS` — пары «спот:контракт» через запятую
(Этап 8.1 §1), например
`SYMBOLS=BTC/USDT:BTC/USDT:USDT,ETH/USDT:ETH/USDT:USDT`. Разделитель пары —
первое двоеточие: имя контракта само содержит двоеточие. Свечи, стакан и сделки
собираются по СПОТУ, funding и открытый интерес — по КОНТРАКТУ; имя контракта из
имени спота не достраивается. Пустой `SYMBOLS` = одна пара из
`SYMBOL`/`SWAP_SYMBOL`.

Расширять состав следует ПОЭТАПНО (§2 ТЗ 8.1): сначала два токена, два часа
работы и `bash scripts/measure_load.sh`, и только при ненарушенных порогах —
остальные. Проверка развёртывания: `bash deploy/verify_8_1.sh`.

Настройки сбора (с дефолтами) — см. `.env.example`: `EXCHANGE`, `SYMBOL`,
`SWAP_SYMBOL`, `TIMEFRAMES`, `OHLCV_INTERVAL`, `ORDERBOOK_INTERVAL`,
`ORDERBOOK_DEPTH`, `TRADES_INTERVAL`, `FUTURES_INTERVAL`.

## Аналитические агенты (Этап 3)

Отдельный сервис `agents` (тот же образ, команда `python -m src.agents_main`)
запускает три независимых агента, каждый со своим циклом:

- **Market** — теханализ свечей OHLCV по `AGENT_TIMEFRAME`: EMA 20/50/200, RSI 14,
  ATR 14, MACD 12/26/9, ADX 14, уровни поддержки/сопротивления.
- **Liquidity** — стакан: спред, объёмы bid/ask, дисбаланс и его устойчивость, «стенки».
- **Futures** — funding rate и open interest по swap-инструменту.

Ключевые принципы: **независимость** (агент читает только свои данные, не выводы
других — видно по коду: каждому доступны лишь свои читающие методы БД), **нет данных —
нет решения** (`insufficient_data`), **агент не решающий** (только направление
`bullish`/`bearish`/`neutral` и `confidence`), **детерминированность** (покрыта тестами).
Заключения пишутся в `agent_outputs`; heartbeat — `agent:heartbeat:{name}` (TTL 300 сек).

Настройки (с дефолтами): `AGENT_TIMEFRAME` (1h), `AGENT_INTERVAL` (60),
`AGENT_MIN_CANDLES` (200).

Проверка результатов агентов:

```bash
# последнее заключение каждого агента
docker compose exec postgres psql -U agenttrade -d agenttrade -c \
  "SELECT DISTINCT ON (agent) agent, signal, round(confidence::numeric,3) conf, ts \
   FROM agent_outputs ORDER BY agent, ts DESC;"

# heartbeat агентов
docker compose exec redis redis-cli KEYS 'agent:heartbeat:*'
```

## Decision Agent (Этап 4)

Отдельный сервис `decision` (команда `python -m src.decision_main`) агрегирует выводы
трёх агентов в одно итоговое решение и пишет его в `signals`.

Логика: берётся последний вывод каждого агента; устаревшие (старше
`AGENT_FRESHNESS_SEC`) и `insufficient_data` отбрасываются; сигнал переводится в
направление (bullish +1 / bearish −1 / neutral 0); считается взвешенный балл
`Σ(направление × confidence × вес) / Σ(вес × confidence)` в диапазоне −1..+1.
Балл > `DECISION_THRESHOLD` → `buy`, < −`DECISION_THRESHOLD` → `sell`, иначе `wait`.
Если свежих выводов меньше `MIN_AGENTS` → `wait`. Вероятность растёт с |балл| и
единодушием агентов.

Ключевой принцип: Decision Agent **не анализирует рынок сам** — ему доступны только
`db.get_latest_agent_output` и `db.save_signal`, к сырым рыночным таблицам доступа нет
(видно по коду). Heartbeat — `decision:heartbeat` (TTL 300 сек).

Настройки (с дефолтами): `DECISION_INTERVAL` (60), `DECISION_THRESHOLD` (0.3),
`AGENT_FRESHNESS_SEC` (300), `MIN_AGENTS` (2), `WEIGHT_MARKET`/`WEIGHT_LIQUIDITY`/`WEIGHT_FUTURES` (1.0).

Проверка решений:

```bash
docker compose exec postgres psql -U agenttrade -d agenttrade -c \
  "SELECT decision, round(probability::numeric,3) prob, \
   jsonb_array_length(agents_payload) n_agents, left(rationale,80) rationale \
   FROM signals ORDER BY ts DESC LIMIT 5;"

docker compose exec redis redis-cli GET decision:heartbeat
```

## Уведомления в Telegram (Этап 5)

Отдельный сервис `notify` (команда `python -m src.notify_main`) читает новые решения
из `signals` и шлёт в Telegram **только сильные сигналы**. Уведомление уходит, если
одновременно: `decision ≠ wait`; `probability ≥ NOTIFY_MIN_PROBABILITY`; сигнал ещё
не отправлен (`notified = FALSE`); решение отличается от последнего отправленного по
инструменту ИЛИ прошло больше `NOTIFY_COOLDOWN_SEC` (анти-спам). Состояние последней
отправки хранится в Redis (`notify:last:{instrument_id}`), heartbeat — `notify:heartbeat`.

Сервис не падает ни при каких ошибках сети/Telegram; при пустых
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` он просто простаивает с предупреждением.
Колонка `signals.notified` добавляется идемпотентно при старте
(`ensure_notify_schema`), так что апгрейд поверх старого тома не требует пересоздания БД.

Настройка (значения — только в локальном `.env`, не в репозитории):

```
TELEGRAM_BOT_TOKEN=<токен от @BotFather>
TELEGRAM_CHAT_ID=<id чата>
NOTIFY_MIN_PROBABILITY=0.7
NOTIFY_COOLDOWN_SEC=1800
NOTIFY_TIMEZONE=Europe/Moscow
```

Формат сообщения: 🟢/🔴 действие (ПОКУПАТЬ/ПРОДАВАТЬ), вероятность в %, причина
(из `rationale`) и время в часовом поясе `NOTIFY_TIMEZONE` (по умолчанию МСК).

## Анализ результатов (Этап 6)

Отдельный сервис `evaluator` (команда `python -m src.evaluator_main`) дооценивает каждый
направленный сигнал фактом: пошла ли цена в предсказанную сторону. Для горизонтов 1ч, 4ч, 12ч и 24ч
по 1m-свечам считаются `pnl_pct` (движение в сторону сигнала; для `sell` со знаком минус,
положительный = верно), `drawdown_pct` (макс. ход против сигнала, всегда ≥ 0) и
`success` (`pnl_pct > 0`). Результаты пишутся в `signal_evaluations`
(идемпотентно, `UNIQUE (signal_id, horizon)`).

Когда оценён главный горизонт (`EVAL_PRIMARY_HORIZON`, 4ч) — сводка пишется в `signals`
(`pnl_pct`/`drawdown_pct`/`success`) и статус становится `closed`. Нехватка свечей —
мягкий пропуск с повтором позже. Периодически (раз в `STATS_LOG_INTERVAL`) в лог выводится
статистика: доля `success` и средний `pnl_pct` по `decision × horizon`.
Heartbeat — `evaluator:heartbeat`.

Настройки (с дефолтами): `EVAL_INTERVAL` (300), `EVAL_HORIZONS` (1,4,12,24 —
в часах; прежний формат «1h,4h» тоже читается),
`EVAL_PRIMARY_HORIZON` (4h), `STATS_LOG_INTERVAL` (3600).

Проверка результатов:

```bash
docker compose exec postgres psql -U agenttrade -d agenttrade -c \
  "SELECT signal_id, horizon, round(pnl_pct::numeric,3) pnl, \
   round(drawdown_pct::numeric,3) dd, success FROM signal_evaluations \
   ORDER BY signal_id, horizon;"

# агрегированная статистика по decision × horizon
docker compose exec postgres psql -U agenttrade -d agenttrade -c \
  "SELECT s.decision, e.horizon, count(*) n, \
   round(avg(e.success::int)::numeric,3) success_rate, \
   round(avg(e.pnl_pct)::numeric,3) avg_pnl \
   FROM signal_evaluations e JOIN signals s ON s.id=e.signal_id \
   GROUP BY s.decision, e.horizon ORDER BY 1,2;"
```

## Пошаговый запуск

1. Скопируйте пример окружения и задайте пароль БД:

   ```bash
   cp .env.example .env
   # откройте .env и замените POSTGRES_PASSWORD на свой пароль
   ```

   > Без `POSTGRES_PASSWORD` приложение намеренно падает на старте с понятной ошибкой.

2. Поднимите всё окружение:

   ```bash
   docker compose up --build --remove-orphans
   ```

   > **Зачем `--remove-orphans`.** Контейнер, оставшийся от services, которого в
   > текущем `docker-compose.yml` уже нет, docker compose не удаляет сам — он
   > лишь предупреждает о нём при КАЖДОЙ команде («Found orphan containers»).
   > Так на сервере до Этапа 8.7 висел остановленный `bt_load` от закрытого
   > Этапа 7.4. Флаг убирает такие контейнеры сразу. Сервисы из невключённых
   > профилей (`tools`, `backtest`) осиротевшими НЕ считаются: они запускаются
   > через `run --rm` и постоянных контейнеров не оставляют.

   Контейнер `postgres` при первом старте автоматически применит `db/init.sql`
   и создаст 9 таблиц с индексами. Сервисы `collector`, `agents`, `decision`,
   `notify`, `evaluator` дождутся готовности PostgreSQL и Redis и начнут работу —
   в логах будет видна работа всех сервисов.

3. Проверьте health-check вручную (внутри контейнера коллектора):

   ```bash
   docker compose run --rm collector python -m src.healthcheck
   ```

   Ожидаемый вывод (exit code 0):

   ```
   PostgreSQL: OK
   Redis:      OK
   ```

## Проверка таблиц в БД

```bash
docker compose exec postgres psql -U agenttrade -d agenttrade -c '\dt'
```

Должны присутствовать 9 таблиц: `instruments`, `ohlcv`, `trades`, `funding`,
`open_interest`, `orderbook_snapshots`, `signals`, `agent_outputs`, `signal_evaluations`.

## Проверка потока данных (через несколько минут после старта)

```bash
# Строки по каждому таймфрейму
docker compose exec postgres psql -U agenttrade -d agenttrade \
  -c "SELECT timeframe, count(*) FROM ohlcv GROUP BY timeframe ORDER BY timeframe;"

# Рост числа снимков стакана, сделок, funding и open interest
docker compose exec postgres psql -U agenttrade -d agenttrade \
  -c "SELECT
        (SELECT count(*) FROM orderbook_snapshots) AS orderbook,
        (SELECT count(*) FROM trades)              AS trades,
        (SELECT count(*) FROM funding)             AS funding,
        (SELECT count(*) FROM open_interest)       AS open_interest;"
```

Heartbeat-ключи коллекторов в Redis:

```bash
docker compose exec redis redis-cli KEYS 'collector:heartbeat:*'
docker compose exec redis redis-cli GET collector:heartbeat:ohlcv
```

Корректная остановка (graceful shutdown) — `Ctrl+C` в окне `docker compose up`
либо `docker compose stop`: коллекторы получают сигнал, задачи отменяются,
соединения с биржей, БД и Redis закрываются.

## Локальная разработка (тесты и линтер)

```bash
pip install -r requirements.txt
export POSTGRES_PASSWORD=любой_пароль   # нужен для импорта конфига
ruff check .
pytest
```

## Остановка

```bash
docker compose down          # остановить контейнеры
docker compose down -v       # остановить и удалить тома (pg_data, redis_data)
```

## Осиротевшие контейнеры

Контейнер, чей сервис исчез из `docker-compose.yml`, продолжает существовать и
даёт предупреждение «Found orphan containers» при каждой команде compose. Сам
он не удаляется — это делается явно:

```bash
# Посмотреть, что вообще осталось (включая остановленные)
docker compose ps -a
docker ps -a --filter "name=bt_"

# Удалить осиротевшие вместе с обычным подъёмом стека
docker compose up -d --remove-orphans

# Либо удалить конкретный контейнер поимённо
docker rm bt_load
```

Контейнер `bt_load` остался от закрытого Этапа 7.4 (`docs/STAGE_7_4_REPORT.md`)
и удалён Этапом 8.7 §6. Данных он не хранил: история реплея лежит в схеме
`backtest` базы, а не в контейнере, — удаление ничего не теряет.
