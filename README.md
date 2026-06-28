# Agent Trade — Этап 1: Инфраструктура

Инфраструктурный фундамент мультиагентной системы анализа крипторынка.
Пайплайн (на будущее): `Collectors → Storage → AI-агенты → Decision Agent → Risk Manager → Notifications`.

На этом этапе реализованы: Docker-окружение, схема PostgreSQL, слой доступа к БД,
клиент Redis, типизированный конфиг, структурное логирование, health-check и CI.
Бизнес-логики (сбор данных, агенты, уведомления) здесь нет.

## Стек

- Python 3.12 (asyncio)
- PostgreSQL 16-alpine, Redis 7-alpine
- asyncpg, redis-py, pydantic-settings, structlog
- pytest + pytest-asyncio, ruff

## Структура

```
.
├── docker-compose.yml      # postgres + redis + app
├── Dockerfile              # образ приложения (python:3.12-slim)
├── .env.example            # пример переменных окружения
├── pyproject.toml          # метаданные, ruff, pytest
├── requirements.txt        # закреплённые версии для Docker/CI
├── db/init.sql             # схема БД (7 таблиц + индексы)
├── src/
│   ├── main.py             # точка входа (заглушка + health-check)
│   ├── healthcheck.py      # CLI-проверка PG и Redis
│   └── core/
│       ├── config.py       # Settings (pydantic-settings)
│       ├── db.py           # пул asyncpg + методы доступа
│       ├── redis_client.py # async-клиент Redis
│       └── logging.py      # настройка structlog
└── tests/                  # smoke-тест импорта конфига
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
   docker compose up --build
   ```

   Контейнер `postgres` при первом старте автоматически применит `db/init.sql`
   и создаст 7 таблиц с индексами. Контейнер `app` дождётся готовности
   PostgreSQL и Redis и выполнит health-check.

3. Проверьте health-check вручную (внутри контейнера приложения):

   ```bash
   docker compose run --rm app python -m src.healthcheck
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
