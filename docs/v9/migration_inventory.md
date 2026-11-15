Источник: `7a022d3688e7576ccbeff9997b1a633f35d77f21`. Пути ниже относятся к этому коммиту. Утверждения о production не следуют из исходного кода.

# Инвентаризация компонентов

Решения — предложения, не утверждённая политика. KEEP = сохранить; ADAPT = адаптировать; REPLACE = заменить после доказательства; RETIRE_LATER = выводить только после проверки; UNKNOWN = не решено.

| Компонент / точка входа | Читает → пишет | Решение и риск |
|---|---|---|
| `src/main.py`, `src/collectors/{ohlcv,trades,orderbook,futures}.py` | публичная биржа → `ohlcv`, `trades`, `orderbook_snapshots`, `funding`, `open_interest`; `src/core/db.py` | ADAPT; неполнота polling, единицы и свежесть |
| `scripts/retention.py`, `db/migrations/010_trade_flow_1m.sql` | trades → `trade_flow_1m`, затем retention | ADAPT; поздние сделки и удаление сырья |
| `src/agents_main.py`, `src/agents/*` | свечи, стакан, funding/OI → `agent_outputs` | ADAPT; старые выводы и семантика окон |
| `src/decision_main.py`, `src/decision/*` | `agent_outputs` → `signals` | ADAPT; версии и свежесть |
| `src/positions_main.py`, `src/positions/*`, `src/risk_main.py`, `src/risk/*` | signals/цели → виртуальные `positions`, риск-данные | ADAPT; не считать виртуальные позиции биржевыми |
| `src/demo_main.py`, `src/demo/{runner,exchange,preflight,ledger}.py` | виртуальные позиции + OKX → `demo_orders`, демо-ордера и учёт | ADAPT после доказательства владения, частичных исполнений и recovery |
| `src/evaluator_main.py`, `src/{barrier,baseline,trailing,calibration}_main.py` | сигналы и цены → оценки и измерения | ADAPT; историческая сопоставимость |
| `src/notify_main.py`, `src/bot_main.py`, `src/notify/*`, `src/bot/*` | сигналы, позиции и состояние → Telegram | ADAPT; контроль содержимого и версия данных |
| `src/export_main.py`, `src/export/*`, `deploy/apps_script.gs` | сигналы/позиции → Sheets и Notion, метки выгрузки | ADAPT; смешение истории и формулы |
| `src/healthcheck.py`, `src/health/*`, `scripts/watchdog.py`, `scripts/backup_db.sh` | доступность, журналы и БД → health, уведомления/резервные копии | KEEP/ADAPT; реальное расписание неизвестно |
| `backtest/*`, `analysis/*` | исторические данные → исследования | UNKNOWN; покрытие истории и отсутствие look-ahead проверять отдельно |

`docker-compose.yml` объявляет postgres, redis, collector, agents, decision, notify, positions, demo, evaluator, bot; профиль tools содержит export, calibration, risk, barrier; backtest — отдельный профиль. Список развёрнутых сервисов неизвестен. `deploy/*.cron`, `deploy/install.sh` и `scripts/retention.py` описывают запланированные задания; реальный crontab неизвестен. Наличие кода не подтверждает активацию.
