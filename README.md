# Agent Trade — Сбор рыночных данных

Мультиагентная система анализа крипторынка.
Пайплайн (на будущее): `Collectors → Storage → AI-агенты → Decision Agent → Risk Manager → Notifications`.

- **Этап 1 (инфраструктура):** Docker-окружение, схема PostgreSQL, слой доступа к БД,
  клиент Redis, типизированный конфиг, структурное логирование, health-check и CI.
- **Этап 2 (сбор данных):** круглосуточный сервис-коллектор на ccxt (REST-опрос Binance):
  свечи OHLCV, снимки стакана, сделки, funding rate и open interest. Аналитики нет —
  только сбор и хранение.

## Стек

- Python 3.12 (asyncio)
- PostgreSQL 16-alpine, Redis 7-alpine
- asyncpg, redis-py, pydantic-settings, structlog, ccxt
- pytest + pytest-asyncio, ruff

## Структура

```
.
├── docker-compose.yml      # postgres + redis + collector
├── Dockerfile              # образ приложения (python:3.12-slim)
├── .env.example            # пример переменных окружения
├── pyproject.toml          # метаданные, ruff, pytest
├── requirements.txt        # закреплённые версии для Docker/CI
├── db/init.sql             # схема БД (7 таблиц + индексы)
├── src/
│   ├── main.py             # точка входа — запуск сервиса-коллектора
│   ├── healthcheck.py      # CLI-проверка PG и Redis
│   ├── core/
│   │   ├── config.py       # Settings (pydantic-settings)
│   │   ├── db.py           # пул asyncpg + методы доступа/записи
│   │   ├── redis_client.py # async-клиент Redis
│   │   ├── exchange.py     # фабрика ccxt-клиента
│   │   └── logging.py      # настройка structlog
│   └── collectors/
│       ├── base.py         # базовый устойчивый цикл + heartbeat
│       ├── ohlcv.py        # свечи по таймфреймам
│       ├── orderbook.py    # снимки стакана
│       ├── trades.py       # сделки (тики)
│       ├── futures.py      # funding + open interest (swap)
│       └── runner.py       # оркестрация + graceful shutdown
└── tests/                  # smoke-тест конфига + тесты парсинга/дедупликации
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

## Пошаговый запуск

1. Скопируйте пример окружения и задайте пароль БД:

   ```bash
   cp .env.example .env
   # откройте .env и замените POSTGRES_PASSWORD на свой пароль
   ```

   > Без `POSTGRES_PASSWORD` приложение намеренно падает на старте с понятной ошибкой.

2. Поднимите всё окружение:

   ```bash
   docker compose up --build
   ```

   Контейнер `postgres` при первом старте автоматически применит `db/init.sql`
   и создаст 7 таблиц с индексами. Контейнер `collector` дождётся готовности
   PostgreSQL и Redis и начнёт собирать данные — в логах будет видна работа
   коллекторов.

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

Должны присутствовать 7 таблиц: `instruments`, `ohlcv`, `trades`, `funding`,
`open_interest`, `orderbook_snapshots`, `signals`.

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
