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
  цены на горизонтах 1ч/4ч (pnl%, просадка, success) и пишет в `signal_evaluations`;
  по главному горизонту (4ч) заполняет сводку в `signals` и закрывает сигнал.

## Стек

- Python 3.12 (asyncio)
- PostgreSQL 16-alpine, Redis 7-alpine
- asyncpg, redis-py, pydantic-settings, structlog, ccxt
- pytest + pytest-asyncio, ruff

## Структура

```
.
├── docker-compose.yml      # postgres + redis + collector + agents + decision + notify + evaluator
├── Dockerfile              # образ приложения (python:3.12-slim)
├── .env.example            # пример переменных окружения
├── pyproject.toml          # метаданные, ruff, pytest
├── requirements.txt        # закреплённые версии для Docker/CI
├── db/init.sql             # схема БД (9 таблиц + индексы)
├── src/
│   ├── main.py             # точка входа — сервис-коллектор
│   ├── agents_main.py      # точка входа — сервис агентов
│   ├── decision_main.py    # точка входа — Decision Agent
│   ├── notify_main.py      # точка входа — сервис уведомлений
│   ├── evaluator_main.py   # точка входа — оценщик результатов
│   ├── healthcheck.py      # CLI-проверка PG и Redis
│   ├── core/
│   │   ├── config.py       # Settings (pydantic-settings)
│   │   ├── db.py           # пул asyncpg + методы доступа/записи/чтения
│   │   ├── redis_client.py # async-клиент Redis
│   │   ├── exchange.py     # фабрика ccxt-клиента
│   │   └── logging.py      # настройка structlog
│   ├── collectors/
│   │   ├── base.py         # базовый устойчивый цикл + heartbeat
│   │   ├── ohlcv.py        # свечи по таймфреймам
│   │   ├── orderbook.py    # снимки стакана
│   │   ├── trades.py       # сделки (тики)
│   │   ├── futures.py      # funding + open interest (swap)
│   │   └── runner.py       # оркестрация + graceful shutdown
│   ├── agents/
│   │   ├── base.py         # BaseAgent + AgentOutput
│   │   ├── market.py       # теханализ по OHLCV (EMA/RSI/ATR/MACD/ADX)
│   │   ├── liquidity.py    # анализ стакана (дисбаланс, стенки)
│   │   ├── futures.py      # funding + open interest
│   │   └── runner.py       # планировщик агентов + graceful shutdown
│   ├── decision/
│   │   ├── agent.py        # DecisionAgent + чистая логика агрегации
│   │   └── runner.py       # планировщик решений + graceful shutdown
│   ├── notify/
│   │   ├── telegram.py     # отправка в Telegram (httpx, async)
│   │   ├── agent.py        # NotifyAgent + should_notify + формат сообщения
│   │   └── runner.py       # планировщик уведомлений + graceful shutdown
│   └── evaluator/
│       ├── evaluator.py    # compute_evaluation + класс Evaluator
│       └── runner.py       # планировщик оценки + graceful shutdown
└── tests/                  # тесты конфига, коллекторов, агентов, агрегации, уведомлений, оценки
```

## Сбор данных (Этап 2)

Сервис-коллектор подключается к Binance через ccxt (REST-опрос) и непрерывно
пишет данные в таблицы из Этапа 1. Спотовые данные (OHLCV, сделки, стакан) берутся
со spot-рынка, funding и open interest — с бессрочного фьючерса (swap). В таблице
`instruments` создаётся две записи на токен: `spot` и `swap`.

Каждый коллектор работает в своём цикле и не падает при ошибках сети/API
(ошибка логируется как warning, цикл продолжается). После каждой успешной итерации
пишется heartbeat в Redis: `collector:heartbeat:{name}` (TTL 300 сек).

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
направленный сигнал фактом: пошла ли цена в предсказанную сторону. Для горизонтов 1ч и 4ч
по 1m-свечам считаются `pnl_pct` (движение в сторону сигнала; для `sell` со знаком минус,
положительный = верно), `drawdown_pct` (макс. ход против сигнала, всегда ≥ 0) и
`success` (`pnl_pct > 0`). Результаты пишутся в `signal_evaluations`
(идемпотентно, `UNIQUE (signal_id, horizon)`).

Когда оценён главный горизонт (`EVAL_PRIMARY_HORIZON`, 4ч) — сводка пишется в `signals`
(`pnl_pct`/`drawdown_pct`/`success`) и статус становится `closed`. Нехватка свечей —
мягкий пропуск с повтором позже. Периодически (раз в `STATS_LOG_INTERVAL`) в лог выводится
статистика: доля `success` и средний `pnl_pct` по `decision × horizon`.
Heartbeat — `evaluator:heartbeat`.

Настройки (с дефолтами): `EVAL_INTERVAL` (300), `EVAL_HORIZONS` (1h,4h),
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

1. Поднимите всё окружение одной командой:

   ```bash
   ./scripts/start.sh
   ```

   Скрипт стартует стек в ноль ручных шагов: при первом запуске он создаёт
   `.env` со **случайными** секретами (`POSTGRES_PASSWORD`, `REDIS_PASSWORD`),
   сгенерированными криптографически (`scripts/init_env.sh`), с правами `600`,
   и поднимает `docker compose`. Скрипт идемпотентен — при перезапуске (и
   автоперезапуске на сервере) существующий `.env` не трогается, пароли не
   меняются. Чтобы задать секреты вручную (Telegram-токен и пр.) — создайте
   `.env` из `.env.example` до первого запуска.

   > **Безопасность.** Порты PostgreSQL (5432) и Redis (6379) наружу не
   > публикуются — доступ только внутри Docker-сети. Redis дополнительно защищён
   > паролем (`requirepass`). Константных паролей в коде нет.

   Контейнер `postgres` при первом старте автоматически применит `db/init.sql`
   и создаст 11 таблиц с индексами. Сервисы `collector`, `agents`, `decision`,
   `notify`, `evaluator` дождутся готовности PostgreSQL и Redis и начнут работу —
   в логах будет видна работа всех сервисов.

2. Проверьте health-check вручную (внутри контейнера коллектора):

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
