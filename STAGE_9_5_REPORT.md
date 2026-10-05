# Этап 9.5 — демо-исполнение зеркалом (OKX, спот). Отчёт исполнителя

Репозиторий `evgenii-yps/analagentai`, ветка `claude/funny-pascal-ld3rkn`, от `main` = `cf88835`
(слияние PR #13, этап 9.4). ТЗ — редакция 2 от 05.10.2026.

**Главное одной строкой.** Сервис `demo` написан, покрыт тестами и добавлен в compose; он **выключен
по умолчанию**. Реальная биржа в среде исполнителя недоступна: всё, что касается ответов OKX, проверено
на заглушке, устроенной по фактическому поведению ccxt 4.4.100 (§2). Критерии приёмки §13.3–§13.5
закрываются только на сервере владельца.

---

## 1. Предусловия §0

| Пункт | Итог |
|---|---|
| 0.1 этап 9.4 влит в main | **Да.** `db/migrations/029_measure_rollups.sql` в `main` (`cf88835`). При первом запуске 05.10 в `main` его не было — я остановился и доложил, ничего не писал; работа продолжена после слияния PR #13 владельцем. |
| 0.2 свежий main, ветки | `git fetch --all` выполнен; ветка сброшена на `origin/main`. Удалённые ветки (32): `agent-resilience-aggregation-bias-m5k6ya`, `agent-trade-core-diagnostics-pvd1b9`, `agent-trade-infra-stage-1-7hb1p0`, `ambiguous-marker-cases-fil8jx`, `balance-accounting-fixes-56bs38`, `bars-tolerance-measurement-pmnci5`, `bold-faraday-im8uy8`, `confident-thompson-yrhh0g`, `core-predictive-ability-diagnosis-jd34ht`, `deal-label-overwrite-fix-hqchha`, `deployment-installer-script-k9e6t4`, `elegant-cerf-pg1iq4`, `explain-signal-script-7ms5p7`, `funny-pascal-ld3rkn`, `gallant-lovelace-j9xs00`, `local-stack-exchange-audit-so6rjz`, `measure-outcomes-no-stop-loss-aplbe7`, `measurement-0-higher-timeframes-0suqco`, `quirky-faraday-45qjo4`, `signals-export-sheets-notion-ovib81`, `single-position-unclosed-candle-stqi6i`, `stage-7-4-market-split-s0941y`, `stage-8-1-unknown-logic-version`, `stage-8-10-1-control-mismatch`, `stage-8-10-trailing-exit-t24php`, `stage-8-3-signal-text`, `stage-9-1-6-no-loss-limit-ynu77p`, `stage-9-2-nostop-plus-48-1hksli`, `telegram-bot-signal-readability-y4d9tq`, `trailing-shadow-measurement-9-1-3`, `weekly-range-position-measurement-z4samw`, `main`. |
| 0.3 ветка 2.5 | **Не тронута и не взята за основу.** `git diff origin/main -- src/collectors src/agents src/decision src/positions src/evaluator src/risk src/notify src/export src/bot` пуст. |
| 0.4 ccxt | Остаётся `4.4.100` (`requirements.txt` не менялся). Проверка — §2 ниже. |

## 2. Проверка §0.4 в установленной ccxt 4.4.100 (до написания кода)

**(а) `set_sandbox_mode` и заголовок.** `ccxt.async_support.okx.set_sandbox_mode(True)` есть; для OKX он
ставит `headers['x-simulated-trading'] = '1'` (адрес не меняет). Проверено не по атрибуту, а на
уровне HTTP-сессии: на подставленной сессии перехвачен реальный подписанный запрос
`GET https://{host}/api/v5/account/balance` — в его заголовках `x-simulated-trading: 1`, `OK-ACCESS-KEY`,
`OK-ACCESS-SIGN`, браузерный `User-Agent`; для клиента **без** `set_sandbox_mode` заголовка в запросе
нет. Это закреплено тестом `test_the_header_really_goes_out_in_a_signed_request`.

**(б) `create_market_buy_order_with_cost`.** Есть (`has['createMarketBuyOrderWithCost'] = True`).
Фактический запрос: `POST /api/v5/trade/batch-orders`, тело
`[{"instId":"BTC-USDT","side":"buy","ordType":"market","sz":"2","tdMode":"cash","tgtCcy":"quote_ccy","clOrdId":"at95b1"}]`
— то есть `sz` — это сумма в USDT. Продажа через `create_order(..., 'market', 'sell', qty, None, params)`:
`tgtCcy: base_ccy`, `sz` — количество. Запрос ордера по клиентскому идентификатору:
`GET /api/v5/trade/order?instId=BTC-USDT&clOrdId=at95b1`, в унифицированном ответе `filled`, `average`,
`cost`, `fee{cost,currency}` (комиссия положительным числом), `lastTradeTimestamp`.

**(в) хост.** Опция `hostname` подставляется в шаблон `https://{hostname}`; проверено на
`www.okx.com` и `eea.okx.com` (URL запроса и заголовок совпадают с хостом).

Всё три пункта есть — СТОП не потребовался, самодельного кода вместо ccxt нет.

## 3. Что сделано

| Файл | Что |
|---|---|
| `db/migrations/030_demo_orders.sql` (+`_rollback`) | таблицы `demo_state`, `demo_orders` точно по §9; `GRANT SELECT` для `agenttrade_ro` на обе; откат удаляет обе |
| `src/demo/rules.py` | чистые функции §5 на `Decimal`: `order_cost_usd`, `cl_ord_id`, `should_buy`, `sell_qty`, `slippage_pct`, `pair_pnl` + вспомогательные `is_dust`, `fee_in_usdt`, `lag_seconds` |
| `src/demo/exchange.py` | `create_demo_exchange`, `assert_demo`, защищённые обёртки приватных вызовов (`fetch_usdt_free`, `place_market_buy`, `place_market_sell`, `fetch_order_by_cl`), только спот/`tdMode=cash`, продажа не больше купленного |
| `src/demo/runner.py` | цикл §6: восстановление → продажи → покупки; строка `pending` до ордера; ошибки; алерты с антидребезгом; heartbeat; строка запуска |
| `src/demo/report.py` | суточная сводка §8 |
| `src/demo/preflight.py` | предпроверка §7 (`--test-order`), без доступа к базе |
| `src/demo_main.py` | точка входа; выключенный режим не трогает ни базу, ни биржу |
| `src/core/config.py` | 10 настроек §4.1 + `demo_config_errors()` |
| `docker-compose.yml`, `.env.example`, `pyproject.toml` | сервис `demo`; параметры (ключи пусты, `DEMO_ENABLED=false`); пакет `src.demo` в сборке |
| `scripts/watchdog.py` | `("demo:heartbeat", "DEMO_INTERVAL", 15, "demo")` |
| `deploy/verify_9_5.sh` | только чтение, по-русски, ✅/❌ (§11) |
| `tests/test_demo_*.py`, `tests/demo_support.py` | 144 новых теста |

**Границы §1.2 — как они соблюдены и чем проверены**
- коллекторы, агенты, decision, positions, evaluator, export, bot — ни одной изменённой строки (diff пуст);
  `LOGIC_VERSION` не тронут (7);
- запись только в `demo_orders` и `demo_state` — тест разбирает все строковые литералы `src/demo/*.py` и
  `src/demo_main.py` через AST (ловит и склеенные литералы) и требует, чтобы цели записи были ровно эти две
  таблицы; DDL в сервисе запрещён тем же тестом;
- демо-ключи (`OKX_DEMO_*`) нигде в `src/`, кроме `config.py`, `demo_main.py` и `src/demo/`;
- размер ордера — только `rules.order_cost_usd` (тест подменяет функцию и видит другую сумму на бирже);
  `positions` сервис не пишет вовсе (тест сравнивает строку позиции до и после итерации);
- `assert_demo` вызывается перед каждым приватным запросом внутри обёрток; тест подменяет заголовок и
  проверяет, что **ни одного обращения к бирже** не произошло, включая случай, когда заголовок пропал
  уже после старта.

## 4. Схема БД: типы ключей (подтверждено по фактическому DDL)

`positions.id` — `BIGSERIAL` → `BIGINT` (`POSITIONS_DDL` в `src/core/db.py`, миграция 018);
`instruments.id` — `SERIAL` → `INT` (`db/init.sql`). Внешние ключи `demo_orders.position_id BIGINT`,
`instrument_id INT` совпадают по типам; тест сверяет типы по `information_schema` на базе, собранной из
`init.sql` и всех миграций. Номер миграции — **030** (029 — последняя в main).
`deploy/schema_drift.sh` строит эталон из `init.sql` и всех миграций кроме `*_rollback.sql`, поэтому 030
в эталон входит; сам скрипт требует docker и здесь не запускался (§7).

## 5. Проверки

- `ruff check .` (0.8.4, как в CI) — чисто.
- `pytest` на Python 3.12 с настоящей PostgreSQL 16: **1347 passed, 98 skipped** (база на чистом `main`,
  тем же способом: 1203 passed, 98 skipped; разница — ровно новые тесты, регрессий нет). Без PostgreSQL
  тесты с базой пропускаются, как у этапа 9.4; в CI сервис postgres уже подключён.
- Покрытие §12: п. 1 — `test_demo_exchange.py`; п. 2–5 — `test_demo_rules.py`; п. 6–9 и 11 —
  `test_demo_runner.py`; п. 10 — `test_demo_safety.py`; п. 12 — `test_demo_db.py` (идемпотентность миграции,
  типы и точность колонок, все CHECK/UNIQUE/FK, роль `agenttrade_ro`, откат и повторное применение).
- **Проверка самих тестов поломками.** В код по одной внесены 12 намеренных ошибок: продажа после покупок,
  игнор `before_start`, комиссия не вычитается, 50101 не останавливает сервис, пропущен `assert_demo`,
  секреты не вычищаются, `lost` без запаса, строки `sent` не разбираются, перевёрнут знак проскальзывания,
  убран `ON CONFLICT`, убрано подавление DEBUG-журнала ccxt, убран `NOT EXISTS` в отборе покупок.
  Одиннадцать ловились сразу. Двенадцатая («убран `NOT EXISTS`») сначала НЕ ловилась: двойную отправку и
  без него останавливает уникальный индекс. Это вторая линия защиты, и для неё добавлен отдельный тест
  `test_the_unique_constraints_alone_stop_a_double_send` (убери `ON CONFLICT` — он краснеет).

## 6. Найдено по ходу и исправлено (вне перечня ТЗ)

1. **Ключи могли попасть в журнал при `LOG_LEVEL=DEBUG`.** ccxt на этом уровне печатает заголовки
   запроса целиком — `OK-ACCESS-KEY`, `OK-ACCESS-PASSPHRASE`, `OK-ACCESS-SIGN` (воспроизведено). У
   collector запросы публичные, и там это безвредно; у демо-клиента — нет. Логгер `ccxt` теперь поднимается
   до WARNING внутри `create_demo_exchange` (тест + проверка поломкой). Это закрывает §12 п. 11 для
   уровня, который тест на INFO не видит.
2. Первая строка «demo выключен» не писалась, если с загрузки машины прошло меньше часа (счётчик
   стартовал с нуля, а `loop.time()` — это uptime). Исправлено, тест есть.

## 7. Отклонения от ТЗ и решения там, где ТЗ молчит

1. **Проверка настроек — методом, а не `model_validator`.** ТЗ §4.1: «сервис падает при старте с понятным
   текстом». `.env` общий, и валидатор в `Settings` при `DEMO_ENABLED=true` с пустым ключом уронил бы не
   только `demo`, но и collector, agents, decision, positions — из-за настроек, которые им не нужны.
   `Settings.demo_config_errors()` зовёт только `demo_main`; он падает с перечнем ошибок (имена ключей,
   значения не печатаются). Хост вне списка — ошибка всегда, ключи — при `DEMO_ENABLED=true`.
2. **Ошибка авторизации / 50101: строка ордера снимается, а не получает `rejected`.** ТЗ §6.3 требует
   прекратить отправку до перезапуска, а `rejected` — «повтор не делается». Эти правила сталкиваются на
   продаже: ордер отклонён ДО торгов, строка `rejected` осталась бы навсегда, и купленная монета никогда
   не была бы продана даже после исправления ключа. Поэтому при отказе авторизации (и при срабатывании
   демо-защиты) строка `pending` удаляется — это `DELETE` из `demo_orders`, то есть в рамках границы
   «только `demo_orders`». Обычные отказы биржи (`InsufficientFunds`, `InvalidOrder` и т. д.) остаются
   `rejected` без повтора, как в ТЗ.
3. **Запас 20 с перед `lost`.** ТЗ: «не нашёлся — `lost`». Ордер, отправленный меньше 20 секунд назад, ещё
   может быть в пути; ложное `lost` означало бы купленную монету без записи. Строка старше запаса и не
   найденная — `lost` сразу, повторной отправки нет. На старте после перезапуска запас обычно уже истёк.
4. **Сводка уходит в `DEMO_REPORT_HOUR_UTC` и позже в тот же день**, а не только внутри этого часа: если
   сервис в этот час был остановлен, сводка не теряется. Один раз в сутки (ключ `demo:report:<дата>`);
   при неудачной отправке — повтор через 15 минут, а не на каждой итерации.
5. **Три причины пропуска сверх перечня ТЗ** (`skip_reason` — свободный текст по схеме §9):
   `no_market` (инструмент не прошёл проверку §6.1.3), `closed_before_buy` (позиция закрылась, пока покупка
   не была сделана; ТЗ: «получает только skipped»), `below_min_size` (причина для `dust`, которой ТЗ не
   называет, а CHECK её требует).
6. **Закрытые позиции без строки покупки.** ТЗ называет только открытые. Чтобы выполнялось «позиция,
   открытая и закрытая между итерациями, получает только skipped», в отбор добавлены и закрытые позиции, но
   только открытые **после** `mirror_since` — вся прежняя закрытая история не получает ни одной строки.
7. **Таблицы сервис не создаёт сам** (в отличие от `positions`, у которой есть `ensure_*_schema`): в ТЗ
   сервису разрешены только `INSERT/UPDATE/DELETE` в две таблицы, а DDL — нет. Нет таблиц — сервис падает с
   текстом «примените миграцию 030». Порядок развёртывания обязан быть «миграция, потом образ».
8. **Сравнение пар в сводке исключает закрытия `data_gap`**: цена их выхода не наблюдалась, а
   восстановлена (так же, как их исключает сводка позиций). Пары с комиссией в валюте, которую нельзя
   пересчитать в USDT, в итог не входят (молча занижать издержки нельзя) — пишется предупреждение.
9. **Вотчдог: только heartbeat-ключ** (как в ТЗ). `demo` не добавлен ни в `CONTAINERS` вотчдога, ни в
   список контейнеров суточной сводки здоровья (`src/health/daily_report.py`) — это решение за владельцем:
   без этого упавший контейнер `demo` вотчдог по heartbeat заметит, а по состоянию контейнера — нет.
10. **`mirror_since` фиксируется после создания клиента биржи**, чтобы упавший из-за ключа старт не начинал
    отсчёт.
11. Предпроверка дополнительно требует непустые ключи и проверяет расхождение часов (порог 20 с — биржа
    отвергает подписи примерно с 30 с).

## 8. Что НЕ проверено (честно)

- **Ни одного запроса к настоящей бирже.** Ответы OKX (форма `fee`, моменты `fillTime`, коды ошибок)
  смоделированы по поведению ccxt 4.4.100; их соответствие живому демо-счёту покажет предпроверка
  владельца, особенно `--test-order` (в том числе «скрытый долларовый минимум», ТЗ §2).
- CI на GitHub не запускался (локально ровно его команды: `ruff check .`, `pytest`, Python 3.12).
- `deploy/verify_9_5.sh` проверен на синтаксис (`bash -n`) и тестом «только чтение»; на сервере не
  запускался. `deploy/schema_drift.sh` не запускался (нужен docker).
- Не делалась проверка длительной работы, нагрузки и поведения при реальных сбоях сети — только
  имитированные (`RequestTimeout`, `OrderNotFound`, `AuthenticationError`).
- Приёмка §13.3–§13.5 (предпроверка на сервере, первые 24 часа, первая сводка в Telegram) — шаги владельца.

## 9. Что нужно перед включением (для инструкции архитектора)

Порядок: миграция 030 → ключи в `.env` на сервере (демо-режим OKX, Read+Trade, без Withdraw, привязка к
IP) → сборка образа → `docker compose up -d demo` (при `DEMO_ENABLED=false` сервис простаивает) →
`python -m src.demo.preflight`, затем с `--test-order` → вписать `OKX_DEMO_HOST`, который назвала
предпроверка → `DEMO_ENABLED=true` → `deploy/verify_9_5.sh`. Откат: `DEMO_ENABLED=false` и перезапуск; при
необходимости `030_demo_orders_rollback.sql` (снимает итог по `demo_orders` заранее — в шапке файла).

## 10. Замечания для архитектора (этап 9.6)

- Размер ордера задаёт единственная функция `src.demo.rules.order_cost_usd`; этап 9.6 меняет только её.
  Тест `test_the_amount_comes_only_from_order_cost_usd` страхует, что запасных путей нет.
- `demo_orders` не внесена в `PROTECTED_TABLES` (`scripts/retention.py`): правил удаления для неё нет, и
  защита нужна, только если такое правило когда-нибудь появится.
