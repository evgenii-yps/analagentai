# Этап 8.2.0 — разведка перед расчётом целей по вероятности

**Характер работы:** только измерение. Ни один агент, ни схема БД, ни конфигурация,
ни `.env` не изменены; контейнеры не перезапускались; данные не загружались и не
удалялись. Добавлены только два скрипта-замерщика и этот файл отчёта.

**Ветка разработки (проверена перед первым коммитом):** `claude/probability-targets-reconnaissance-ylg2co`

```
$ git branch --show-current
claude/probability-targets-reconnaissance-ylg2co
```

**Дата составления:** 2026-08-24.

---

## §0. Среда, в которой выполнялась разведка — читать первым

Задание рассчитано на продакшн-сервер Hetzner. Сессия выполнялась **не на нём**, а в
изолированном одноразовом контейнере Claude Code с чистым клоном репозитория. Это
установлено замером, а не предположением:

| Что проверено | Команда | Результат |
|---|---|---|
| Демон Docker | `docker ps` | `failed to connect to the docker API at unix:///var/run/docker.sock … no such file or directory` |
| Пользователь `agent` | `id agent` | `id: 'agent': no such user` |
| Файл `.env` | `ls -la .env` | `No such file or directory` |
| Имя хоста | `hostname` | `vm` |
| Доступ к OKX | `curl -sS --max-time 25 "https://www.okx.com/api/v5/public/time"` | `curl: (56) CONNECT tunnel failed, response 403` |
| Причина отказа | `curl -sS "$HTTPS_PROXY/__agentproxy/status"` | `{"kind":"connect_rejected","detail":"gateway answered 403 to CONNECT (policy denial or upstream failure)","host":"www.okx.com:443"}` |

Следствия, которые нельзя обойти из этой среды:

* **PostgreSQL продакшна недоступен** — нет ни демона Docker, ни пользователя `agent`,
  ни `.env` с паролем. Порт 5432 на сервере публикуется только на `127.0.0.1`
  (`docker-compose.yml:23`), то есть снаружи недостижим в принципе.
* **Публичный API OKX недоступен** — хост `www.okx.com` запрещён политикой исходящего
  трафика этой среды. Отказ политики не обходится и не повторяется.

Поэтому отчёт честно разделён на две части:

* всё, что определяется по **коду и миграциям репозитория**, измерено здесь и приведено
  с файлами и номерами строк;
* всё, что требует **сервера или биржи**, помечено **«НЕ ИЗМЕРЕНО»** с указанием
  причины, и к каждому такому пункту приложена готовая команда, дающая цифру за один
  запуск. Оценок «по памяти» и правдоподобных чисел в отчёте нет ни одного.

Замерщики (только чтение, ничего не пишут):

* `scripts/recon_8_2_0_db.sql` — все замеры по базе (вопросы 1, 2а, 4);
* `scripts/recon_8_2_0_okx.py` — зонд биржи по споту **и** по контракту (вопрос 3).

Запуск на сервере:

```bash
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres \
    psql -U agenttrade -d agenttrade -X -A -F "|" -f -' < scripts/recon_8_2_0_db.sql

sudo -u agent bash -c 'cd /opt/agent-trade && docker compose --profile backtest \
    run --rm backtest python scripts/recon_8_2_0_okx.py \
    --pairs BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP,XRP-USDT:XRP-USDT-SWAP,DOGE-USDT:DOGE-USDT-SWAP'
```

### §0.1. Замерщик проверен на работоспособность (это измерено, а не заявлено)

Отдавать неработающий скрипт вместо цифр было бы тем же, что цифры без замера. Поэтому
`scripts/recon_8_2_0_db.sql` прогнан целиком на **одноразовом локальном экземпляре
PostgreSQL 16**, поднятом из схемы самого репозитория. Продакшн при этом не
затрагивался никак: экземпляр поднят в этом контейнере на порту 55432, после проверки
остановлен и удалён.

```
$ initdb -D /home/pgtest/pgdata -U agenttrade --auth=trust      # → initdb OK
$ pg_ctl -D /home/pgtest/pgdata -o '-p 55432 -k /tmp' start     # → server started
$ psql -p 55432 -U agenttrade -d agenttrade -f db/init.sql      # → init.sql OK
$ for m in db/migrations/0*.sql; do psql … -f "$m"; done
OK  db/migrations/007_calibration_inertia.sql
OK  db/migrations/008_backtest_schema.sql
OK  db/migrations/009_stage_8_1_horizons.sql
OK  db/migrations/010_trade_flow_1m.sql
OK  db/migrations/011_agent_outputs_daily.sql
OK  db/migrations/012_unknown_logic_version.sql
OK  db/migrations/013_user_settings.sql
$ psql -p 55432 -U agenttrade -d agenttrade -X -A -F "|" -v ON_ERROR_STOP=1 -f - \
      < scripts/recon_8_2_0_db.sql
… все 12 разделов отработали …
EXIT=0
```

Мало того, что запросы выполняются, — проверена и **их арифметика**, на управляемых
данных, где правильный ответ известен заранее:

| Проверка | Что подано | Ожидалось | Получено |
|---|---|---|---|
| счёт пропусков | 10 часовых свечей подряд, две выброшены (03:00 и 07:00) | `candles=8, expected=10, gaps=2` | `candles=8, expected=10, gaps=2` |
| «инструменты без свечей» (раздел 2c) | заведены спот и контракт, свечи только у спота | в выдаче один контракт | `2|BTC|BTC/USDT:USDT|swap` |
| независимые окна 3A | 6 сигналов: 3 в окне 00–04, 2 в окне 04–08, 1 до границы версии | `BTC = 2` | `BTC = 2` |
| независимые окна 3B | те же, `wait` отброшены | `BTC = 2` | `BTC = 2` |
| независимые окна 3C | оценок в `signal_evaluations` не заведено | пусто | `(0 rows)` |

Отдельно подтверждено, что сигнал от 22.08 20:00 (версия 4, **до** границы 22:59) в
подсчёт не попадает — то есть версии логики в замере не смешиваются.

Заодно измерен состав таблиц пустой базы (раздел 4b): таблиц целевых уровней в ней нет
(см. §4), а `logic_version_windows` после миграций содержит **одну** строку — версию 4
от `2026-08-16 16:25:00+00`, её засевает `db/migrations/009_stage_8_1_horizons.sql:98-101`.
Границу версии 5 в эту таблицу пишет уже рантайм, при первом старте на новой версии
(`src/core/db.py:1082`, вызов — `src/decision/runner.py:54`), поэтому её
фактическое значение на сервере даёт раздел 0 замерщика.

Зонд биржи `scripts/recon_8_2_0_okx.py` проверен только на синтаксис
(`python3 -m py_compile` → OK): выполнить его здесь нельзя, хост биржи запрещён.

---

## §1. Вопрос 1 — фактическая глубина истории цен

### 1.1. Настоящие имена таблиц и колонок (измерено по репозиторию)

Хранилищ свечей в базе **два**, и путать их нельзя.

**1) `public.ohlcv` — продакшн, сюда пишет коллектор.** `db/init.sql:19-30`:

```sql
CREATE TABLE IF NOT EXISTS ohlcv (
    instrument_id INT NOT NULL REFERENCES instruments(id),
    timeframe     TEXT NOT NULL,                -- 1m,5m,15m,1h,4h,1d
    ts            TIMESTAMPTZ NOT NULL,
    open  DOUBLE PRECISION NOT NULL,
    high  DOUBLE PRECISION NOT NULL,
    low   DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_ts ON ohlcv (instrument_id, timeframe, ts DESC);
```

Колонка времени — **`ts`** (начало свечи, `TIMESTAMPTZ`), не `open_time`. Инструмент —
числовой **`instrument_id`** → `instruments(id)` (`db/init.sql:6-16`: `exchange`,
`symbol`, `base`, `quote`, `type` = `spot|swap|future`).

**2) `backtest.candles` — отдельная схема исторического реплея (Этап 7.4).**
`db/migrations/008_backtest_schema.sql:19-34`: ключ — **текстовый `inst_id`** (имя
инструмента на бирже), таймфрейм называется **`bar`**, время — **`open_time` /
`close_time`**. Это другое хранилище с другими именами колонок; смешивать его с `ohlcv`
в одном расчёте нельзя.

Проверка того, что других таблиц свечей нет:

```
$ grep -rniE 'CREATE TABLE' db/migrations/*.sql | grep -v rollback
db/migrations/007_calibration_inertia.sql:25:CREATE TABLE IF NOT EXISTS calibration_curves (
db/migrations/008_backtest_schema.sql:19:CREATE TABLE IF NOT EXISTS backtest.candles (
db/migrations/008_backtest_schema.sql:36:CREATE TABLE IF NOT EXISTS backtest.funding (
db/migrations/008_backtest_schema.sql:44:CREATE TABLE IF NOT EXISTS backtest.gaps (
db/migrations/008_backtest_schema.sql:53:CREATE TABLE IF NOT EXISTS backtest.runs (
db/migrations/008_backtest_schema.sql:66:CREATE TABLE IF NOT EXISTS backtest.decisions (
db/migrations/008_backtest_schema.sql:80:CREATE TABLE IF NOT EXISTS backtest.outcomes (
db/migrations/009_stage_8_1_horizons.sql:89:CREATE TABLE IF NOT EXISTS logic_version_windows (
db/migrations/010_trade_flow_1m.sql:18:CREATE TABLE IF NOT EXISTS trade_flow_1m (
db/migrations/011_agent_outputs_daily.sql:21:CREATE TABLE IF NOT EXISTS agent_outputs_daily (
db/migrations/013_user_settings.sql:27:CREATE TABLE IF NOT EXISTS user_settings (
```

### 1.2. Важное следствие для целей: у контрактов свечей в `ohlcv` нет

Коллектор свечей заводится **только на спотовом инструменте пары**.
`src/collectors/runner.py:59-77`:

```python
collectors.extend([
    OHLCVCollector(
        exchange, item.spot_id, item.pair.spot,      # <-- СПОТ
        settings.timeframes_list, settings.OHLCV_INTERVAL,
        name_suffix=token,
    ),
    OrderBookCollector(exchange, item.spot_id, item.pair.spot, ...),
    TradesCollector(exchange, item.spot_id, item.pair.spot, ...),
    FuturesCollector(exchange, item.swap_id, item.pair.swap, ...),   # funding + OI
])
```

`FuturesCollector` пишет `funding` и `open_interest`, но **не свечи**. То есть по коду
для всех пяти контрактов (`type='swap'`) в `ohlcv` не должно быть ни одной строки.
Это утверждение по коду; фактическую проверку делает запрос **2c** замерщика
(«инструменты без единой свечи»). Пока запрос не выполнен на сервере — **НЕ ИЗМЕРЕНО**.

Сопутствующее: срок хранения задан только для минутных свечей —
`RETENTION_1M_DAYS = 30` (`src/core/config.py:199`), правило
`_rule("ohlcv", "RETENTION_1M_DAYS", "30", "AND timeframe = '1m'")`
(`scripts/retention.py:355`). Часовые и любые не-`1m` свечи не удаляются никогда, то
есть глубина по ним ограничена только моментом запуска сбора.

Набор хранимых таймфреймов задаётся `TIMEFRAMES` (`src/core/config.py:48`, по умолчанию
`1m,5m,15m,1h`). Фактическое значение лежит в `.env` сервера — прочитать его отсюда
нельзя, поэтому замерщик не берёт его из конфигурации, а **обнаруживает реально
хранимые таймфреймы прямо из данных** (`GROUP BY o.timeframe`).

### 1.3. Сами цифры глубины — НЕ ИЗМЕРЕНО

Самая ранняя метка, самая поздняя метка, число свечей, число пропусков и полные сутки
истории по каждому из 5 токенов × (спот, контракт) × каждый таймфрейм —
**НЕ ИЗМЕРЕНО. Причина: PostgreSQL продакшна недоступен из среды выполнения (§0).**

Запрос, который даёт всю таблицу разом (раздел 2 файла `scripts/recon_8_2_0_db.sql`);
пропуск = «ожидаемое число интервалов между крайними метками минус фактическое число
свечей»:

```bash
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres psql -U agenttrade -d agenttrade -X -A -F "|" -c "SELECT i.base AS token, i.symbol AS instrument, i.type AS market, o.timeframe AS tf, min(o.ts) AS ts_min_utc, max(o.ts) AS ts_max_utc, count(*) AS candles, (floor(extract(epoch FROM max(o.ts)-min(o.ts))/s.sec)::bigint+1) AS expected, (floor(extract(epoch FROM max(o.ts)-min(o.ts))/s.sec)::bigint+1-count(*)) AS gaps, round((extract(epoch FROM max(o.ts)-min(o.ts))/86400.0)::numeric,3) AS span_days FROM ohlcv o JOIN instruments i ON i.id=o.instrument_id JOIN LATERAL (SELECT CASE o.timeframe WHEN '\''1m'\'' THEN 60 WHEN '\''5m'\'' THEN 300 WHEN '\''15m'\'' THEN 900 WHEN '\''1h'\'' THEN 3600 WHEN '\''4h'\'' THEN 14400 WHEN '\''1d'\'' THEN 86400 END AS sec) s ON TRUE GROUP BY i.base,i.symbol,i.type,o.timeframe,s.sec ORDER BY i.base,i.type,s.sec;"'
```

Отдельная строка «полных суток истории на инструмент» — раздел **2b** того же файла.

Ожидаемый вид результата (шаблон под заполнение, **цифры не подставлены умышленно**):

| токен | инструмент | рынок | tf | ts_min_utc | ts_max_utc | свечей | ожидалось | пропусков | суток |
|---|---|---|---|---|---|---|---|---|---|
| BTC | *(из `instruments.symbol`)* | spot | … | не измерено | не измерено | не измерено | не измерено | не измерено | не измерено |
| BTC | *(из `instruments.symbol`)* | swap | … | не измерено | не измерено | не измерено | не измерено | не измерено | не измерено |

Имена инструментов в шаблон подставляются из результата раздела 1 замерщика
(`SELECT id, exchange, symbol, base, quote, type … FROM instruments`), а **не
достраиваются** одно из другого: пара «спот + контракт» задаётся явно
(`src/core/instruments.py:51-83`, разделитель — первое двоеточие).

---

## §2. Вопрос 2 — выгрузка независимых окон

### 2а. Сколько независимых 4-часовых окон лежит в базе после 22.08.2026 22:59 UTC

**НЕ ИЗМЕРЕНО. Причина: PostgreSQL продакшна недоступен из среды выполнения (§0).**

В замерщике этот вопрос разложен на четыре запроса (разделы 3A–3D), потому что
«независимое окно» и «строка листа» — не одно и то же, и разница как раз и разделяет
две причины:

| № | Что считает | Зачем |
|---|---|---|
| 3A | все сигналы, одно наблюдение на непересекающееся 4 ч окно, по токенам | буквальное определение из ТЗ |
| 3B | то же, но только `decision <> 'wait'` | первое условие выгрузки |
| 3C | то же + наличие оценки на горизонте 4 ч | второе условие выгрузки |
| 3D | дословный запрос выгрузки (все четыре горизонта) | что лист обязан показать |

Разность 3A → 3B → 3C и есть ответ «сколько отсеивается и на каком условии».
Дополнительно раздел **3e** показывает, сколько после границы версии 5 вообще есть
сигналов и оценок по каждому токену.

Одна команда для 3A:

```bash
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres psql -U agenttrade -d agenttrade -X -A -F "|" -c "SELECT i.base AS token, count(*) AS independent_windows FROM (SELECT DISTINCT ON (s.instrument_id, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400)) s.id, s.instrument_id FROM signals s WHERE s.ts >= TIMESTAMPTZ '\''2026-08-22 22:59:00+00'\'' ORDER BY s.instrument_id, to_timestamp(floor(extract(epoch FROM s.ts)/14400)*14400), s.ts ASC) w JOIN instruments i ON i.id=w.instrument_id GROUP BY i.base ORDER BY i.base;"'
```

### 2б. Код выгрузки и дословное условие отбора строк листа

**Измерено по репозиторию.**

Лист называется `Независимые окна` — константа `_SHEET_WINDOWS`,
**`src/export_main.py:57`**.

Наполняется он в **`src/export_main.py:120-139`**:

```python
120    # Независимые окна: по одному наблюдению на ТОКЕН и ГОРИЗОНТ (§7 ТЗ 8.1).
121    horizons = settings.eval_horizons_hours
122    windows = await queries.fetch_independent_by_token_horizon(conn, horizons)
123    correlation = await queries.fetch_outcome_correlation(conn, horizons)
...
134    res = await sheets.post_rows(
135        url, secret, _SHEET_WINDOWS, "replace",
136        [INDEPENDENT_DISCLAIMER, *window_rows], header=INDEPENDENT_HEADER,
137    )
```

Отбор строк — **`src/export/queries.py:141-178`**, функция
`fetch_independent_by_token_horizon`. Условие **дословно** (строки 158-176):

```sql
SELECT DISTINCT ON (e.horizon_h, i.id, win)
       <колонки сигнала>,
       e.horizon_h        AS horizon_h,
       ...
       to_timestamp(
           floor(extract(epoch FROM s.ts) / (e.horizon_h * 3600))
           * (e.horizon_h * 3600)
       ) AS win
FROM signals s
JOIN instruments i ON i.id = s.instrument_id
LEFT JOIN signal_evaluations e1 ON e1.signal_id = s.id AND e1.horizon = '1h'
LEFT JOIN signal_evaluations e4 ON e4.signal_id = s.id AND e4.horizon = '4h'
JOIN signal_evaluations e ON e.signal_id = s.id
WHERE s.decision <> 'wait'
  AND e.horizon_h = ANY($1::int[])
ORDER BY e.horizon_h, i.id, win, s.ts ASC;
```

(строки `FROM signals s` … `LEFT JOIN … e4` подставляются из константы `_SIGNAL_JOINS`,
`src/export/queries.py:45-50`.)

Разбор по пунктам вопроса:

| Что спрашивалось | Ответ (измерено) | Где |
|---|---|---|
| **по каким токенам** | Ограничения нет. Токен входит в ключ прореживания `DISTINCT ON (…, i.id, …)`, но ни один токен не отбирается и не исключается. | `queries.py:159`, `175` |
| **по какому окну** | Окно **равно длине горизонта**, а не фиксированным 4 часам: `floor(epoch(s.ts) / (horizon_h·3600)) · horizon_h·3600`. При `EVAL_HORIZONS=1,4,12,24` это окна 1 ч, 4 ч, 12 ч и 24 ч. Границы кратны длине окна от эпохи. | `queries.py:167-170` |
| **по какой версии логики** | **Фильтра по `logic_version` НЕТ ВООБЩЕ.** `s.logic_version` только выводится колонкой (`_SIGNAL_COLUMNS`, `queries.py:37`) и никак не ограничивает выборку. Лист смешивает версии 4 и 5 в одной таблице. | `queries.py:173-174` (весь `WHERE`) |
| **ограничение на один инструмент от эпохи одного токена** | **В этом запросе — нет.** Проверено: единственная функция с фиксацией на один инструмент — `fetch_independent_windows` (`queries.py:124-138`, старый вариант «одно окно 4 ч на инструмент»), и она **мёртвый код**: `grep -rn 'fetch_independent_windows' --include=*.py .` даёт только определение и упоминание в docstring, ни одного вызова. Наследие эпохи одного токена осталось на уровне **конфигурации**, а не запроса: при пустом `SYMBOLS` система молча сворачивается к одной паре `SYMBOL`/`SWAP_SYMBOL` (`src/core/config.py:322-331`). Значение `SYMBOLS` на сервере отсюда прочитать нельзя — **НЕ ИЗМЕРЕНО**. | `queries.py:124-138`, `config.py:322-331` |
| **прочие фильтры** | Два, и оба существенные: **`s.decision <> 'wait'`** (сигналы «ждать» в лист не попадают) и **обязательный `JOIN signal_evaluations`** — без посчитанной оценки на горизонте наблюдения нет вовсе. Фильтра по времени нет, поэтому лист показывает всю историю, а не период после границы версии. | `queries.py:172-174` |

**Дефект, найденный при разборе кода выгрузки (измерено, не предположено).**
Первой строкой листа отправляется оговорка `INDEPENDENT_DISCLAIMER` — список из
**одного** элемента (`src/export/transform.py:205-212`), тогда как строки данных и
заголовок — из **пятнадцати** (`transform.py:214-230`, `build_independent_row` →
`transform.py:250-266`). Замер ширины:

```
$ python3 -c "<разбор AST src/export/transform.py>"
SIGNALS_HEADER -> 33 элементов
SUMMARY_HEADER -> 16 элементов
INDEPENDENT_DISCLAIMER -> 1 элементов
INDEPENDENT_HEADER -> 15 элементов
CORRELATION_HEADER -> 5 элементов
build_independent_row ->  15 элементов
```

Приёмник в Google Таблице берёт ширину диапазона из **первой** строки пачки —
`deploy/apps_script.gs:36`:

```javascript
sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length)
     .setValues(rows);
```

`rows[0]` — это оговорка, её длина 1. Значит диапазон получается шириной в один
столбец, а в `setValues` приходят строки по 15 значений → Apps Script бросает
исключение, `doPost` ловит его и отвечает `{ ok:false, error:… }`
(`apps_script.gs:40-42`), а выгрузка превращает это в
`ExportError("лист «Независимые окна»: …")` (`src/export_main.py:138-139`). Пока строк
данных нет (`rows` состоит из одной оговорки), запись проходит — отказ появляется
ровно тогда, когда данные появляются.

Дополнительное подтверждение, что приёмник действительно старый: оговорка добавлена в
Этапе 8.1, а `deploy/apps_script.gs` с Этапа 6.6 не менялся ни разу:

```
$ git log --oneline -- deploy/apps_script.gs
c6d2696 feat(export): суточная выгрузка сигналов в Google Таблицу и Notion (Этап 6.6)

$ git log --oneline -S 'INDEPENDENT_DISCLAIMER' -- src/export/transform.py src/export_main.py
6b97b98 feat(instruments,evaluator): пять токенов и четыре горизонта оценки (Этап 8.1)
```

Тестами это не покрыто: `grep -n 'DISCLAIMER|Независимые окна|independent' tests/test_export.py tests/test_stage_8_1.py` не даёт ни одного совпадения по листу независимых окон.

Оговорка: код приёмника в самой Google Таблице заказчик ставит руками, и совпадение
развёрнутой версии с `deploy/apps_script.gs` отсюда **не измерено**. Проверяется это
журналом выгрузки (пункт 2в) — там отказ виден дословно.

### 2в. Когда выгрузка запускалась и чем закончилась

**НЕ ИЗМЕРЕНО. Причина: журнал лежит на сервере (`/opt/agent-trade/logs/export.log`),
из среды выполнения недоступен (§0).**

Измерено по репозиторию — расписание и место журнала:

```
$ grep -n 'export' deploy/install.sh | grep cron -A0
516:20 6 * * * ${APP_USER} cd ${APP_DIR} && /usr/bin/docker compose --profile tools run --rm --no-deps export >> ${APP_DIR}/logs/export.log 2>&1
```

то же в `deploy/agent-trade-export.cron:21`. Итого: **ежесуточно в 06:20 UTC**,
пользователь `agent`, журнал `/opt/agent-trade/logs/export.log`, код возврата 1 при
ошибке пробрасывается наружу (`src/export_main.py:264-270`).

Команды, дающие ответ за один запуск:

```bash
# последний запуск и его исход
sudo -u agent tail -n 120 /opt/agent-trade/logs/export.log

# ошибки за последние двое суток
sudo -u agent bash -c "awk -v d=\"\$(date -u -d '2 days ago' +%Y-%m-%d)\" '\$0 >= d' /opt/agent-trade/logs/export.log | grep -iE 'error|ok=false|Независимые окна|ExportError|не удалось'"

# подтверждение дефекта 2б, если он сработал: ищем дословный текст отказа
sudo -u agent grep -n 'Независимые окна' /opt/agent-trade/logs/export.log | tail -n 20

# расписание, как оно реально стоит на сервере
sudo -u agent bash -c 'cat /etc/cron.d/agent-trade /etc/cron.d/agent-trade-export 2>/dev/null'
```

### Вывод по вопросу 2

Однозначный вывод «данных нет» либо «данные есть, но выгрузка их не берёт»
**сформулировать по факту замера сейчас нельзя: ни один из трёх замеров (2а, 2в и
состав `SYMBOLS` на сервере) в этой среде выполнить невозможно.** Давать вывод без
замера запрещено пунктом ТЗ «Запрещено», поэтому вместо вывода приводится решающее
правило — какая цифра какой вывод доказывает:

| Результат замера | Однозначный вывод |
|---|---|
| 3A даёт заметные числа по всем пяти токенам, а 3C — почти нули | Данные **есть**; их не берёт **условие оценки**: нет строк в `signal_evaluations`, потому что оценщик не смог набрать окно 1-минутных свечей или сигналы ещё не дозрели. |
| 3A ≈ 3B ≈ 3C, и все они малы или пусты по SOL/XRP/DOGE | Данных в базе **нет**: сигналы по этим токенам не выдаются вовсе (первый подозреваемый — `SYMBOLS` в `.env` без трёх токенов, см. `config.py:322-331`). |
| 3D даёт много строк, а в журнале выгрузки есть `лист «Независимые окна»: ok=false` | Данные **есть**, выгрузка их **не доносит**: сработал дефект ширины строки из пункта 2б. |
| 3A и 3B велики, 3C велик, журнал чист, а лист пуст или устарел | Отказ произошёл раньше по цепочке — на листе «Сводка по дням» (`src/export_main.py:114-118` бросает `ExportError` **до** листа независимых окон, и тогда лист вообще не переписывается). |

Заранее известно и не требует замера одно: даже при полностью исправной выгрузке лист
**смешивает версии логики 4 и 5**, потому что фильтра по `logic_version` в запросе нет
(пункт 2б). Для расчёта целей по вероятности брать этот лист как есть нельзя.

---

## §3. Вопрос 3 — что готова отдать биржа

**НЕ ИЗМЕРЕНО. Причина: хост `www.okx.com` запрещён политикой исходящего трафика среды
выполнения; попытка соединения отклоняется шлюзом с кодом 403 на CONNECT (§0). Отказ
политики не обходится.**

Ни одна из четырёх запрошенных величин — глубина часовых свечей по 5 спотовым
инструментам и 5 контрактам, число свечей за запрос, устройство постраничной выборки,
заявленные ограничения частоты — **не подставлена по памяти и не взята из
документации**. Вместо цифр приложен зонд, который получает их за один запуск.

Зонд: `scripts/recon_8_2_0_okx.py`. Эндпоинт —
`https://www.okx.com/api/v5/market/history-candles`
(`backtest/loader.py:55-57`). Клиент — `httpx` со штатной подписью
`python-httpx/<версия>`: проверено Этапом 7.4, что на неё OKX отвечает 200
(`backtest/loader.py:97-107`); браузерная подпись не подставляется. Свечи **никуда не
сохраняются** — зонд печатает и завершается.

Что именно он меряет:

| Величина вопроса | Как меряется | Функция |
|---|---|---|
| глубина назад, отдельно спот и **контракт** | листаем `after` назад страницами по 100 до пустого ответа или до остановки пагинации | `probe_earliest` |
| свечей за один запрос | `limit` пробуется 300 → 200 → 100, берётся первый, который биржа отдаёт целиком | `probe_limit` |
| устройство постраничной выборки | измеряется порядок строк в ответе, направление `after` и поведение `before` | `probe_pagination` |
| ограничения частоты | темп наращивается 400 → 25 мс до первого кода `50011`; печатаются и заголовки ответа, если биржа их шлёт | `probe_pace` |

**Отличие от уже существующего `scripts/probe_history_depth.py` (Этап 7.4)** — тот
зондирует свечи **только на споте**, а на контракте только `funding`
(`probe_history_depth.py:224-294`). Для целей по вероятности нужны оба рынка, и
глубина контракта из глубины спота не выводится, поэтому написан отдельный зонд.

### Внутрисвечные максимум и минимум (high/low) — обязательное требование

Это требование зонд проверяет **тремя разными замерами**, потому что «поля есть в
ответе сейчас» не означает «они есть на всю глубину»:

1. **состав строки** — печатается сырая первая строка ответа и число полей, чтобы
   список полей был измерен, а не взят из документации (`probe_row_shape`);
2. **осмысленность** — на выборке в 100 свечей проверяется `high ≥ max(open, close)` и
   `low ≤ min(open, close)`; печатается число нарушений и число вырожденных свечей с
   `high == low` (`probe_row_shape`);
3. **самый ранний край** — после того как найдена самая ранняя доступная свеча,
   делается отдельный запрос к этому краю и печатается его строка целиком
   (`probe_instrument`, блок «high/low на самом раннем крае»).

Пока зонд не выполнен, ответ на вопрос «доступны ли high/low за тот же период» —
**НЕ ИЗМЕРЕНО**. Заранее известно только то, что читается в коде: продакшн-таблица
`ohlcv` колонки `high` и `low` хранит (`db/init.sql:24-25`), и оценщик уже считает по
ним просадку — `min(low)` для `buy` и `max(high)` для `sell`
(`src/evaluator/evaluator.py:76-81`). То есть в самой системе механика «по факту
касания, а не по закрытию» уже есть; открыт только вопрос глубины на бирже.

---

## §4. Вопрос 4 — готовность схемы

**Ответ по репозиторию: таблиц для целевых уровней и для заморозки целей в момент
сигнала в схеме НЕТ.** Ни в `db/init.sql`, ни в одной из миграций `db/migrations/*.sql`
такой таблицы не создаётся — полный перечень `CREATE TABLE` приведён в §1.1, и в нём
нет ничего похожего.

Поиск по всему коду и всем миграциям тоже пуст:

```
$ grep -rniE 'price_target|targets|target_level|level_hit|barrier|signal_targets' \
      --include=*.py --include=*.sql src/ db/ backtest/ analysis/ scripts/
(совпадений по целевым уровням нет; единственные попадания слова frozen —
 @dataclass(frozen=True) в src/calibration/curve.py:23, src/core/instruments.py:34,123,
 src/core/user_settings.py:49, backtest/*.py, к целям отношения не имеют)
```

Ближайшее, что есть в схеме, — `signal_evaluations` (`db/init.sql:194-205`): она
хранит исход по горизонту (`price_at_signal`, `price_at_close`, `pnl_pct`,
`drawdown_pct`, `success`), но **уровня цели не хранит вовсе** и заморозки на момент
сигнала не делает. Для расчёта «цель достигнута по факту касания» её недостаточно.

**Число строк и фактическое наличие таблиц в самой базе — НЕ ИЗМЕРЕНО. Причина:
PostgreSQL продакшна недоступен (§0).** Схема на томе может отличаться от репозитория
(часть DDL применяется идемпотентно из кода в рантайме — например
`src/export/queries.py:58-101`), поэтому вывод «в базе нет» без запроса к базе делать
нельзя. Проверка (разделы 4 и 4b замерщика):

```bash
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres psql -U agenttrade -d agenttrade -X -A -F "|" -c "SELECT table_schema, table_name FROM information_schema.tables WHERE table_name ~* '\''(target|level|barrier|touch|freeze|frozen)'\'' ORDER BY 1,2;"'

sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres psql -U agenttrade -d agenttrade -X -A -F "|" -c "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('\''pg_catalog'\'','\''information_schema'\'') ORDER BY 1,2;"'
```

---

## §5. Сводка: что измерено, что нет

| Пункт ТЗ | Состояние | Причина, если не измерено |
|---|---|---|
| 1. Имена таблиц и колонок свечей | **измерено** (по миграциям и коду) | — |
| 1. Свечи только по споту, не по контракту | **измерено по коду**, подтверждение в базе — нет | БД недоступна |
| 1. Ранняя/поздняя метка, число свечей, пропуски, полные сутки | **не измерено** | БД недоступна |
| 2а. Независимые окна после 22.08 22:59 по токенам | **не измерено** | БД недоступна |
| 2б. Код и дословное условие отбора листа | **измерено** (файл + строки) | — |
| 2б. Отсутствие фильтра по `logic_version` | **измерено** | — |
| 2б. Отсутствие ограничения на один инструмент в запросе | **измерено** | — |
| 2б. Значение `SYMBOLS` на сервере | **не измерено** | `.env` недоступен |
| 2б. Дефект ширины строки в приёмнике Apps Script | **измерено по коду** (ширины 1 против 15), срабатывание на сервере — нет | журнал недоступен |
| 2в. Последний запуск, исход, ошибки за двое суток | **не измерено** | журнал на сервере недоступен |
| 3. Глубина, `limit`, пагинация, лимиты частоты, high/low | **не измерено** | `www.okx.com` запрещён политикой сети, 403 на CONNECT |
| 4. Таблиц целей и заморозки нет в репозитории | **измерено** | — |
| 4. Их отсутствие/наличие и число строк в самой базе | **не измерено** | БД недоступна |

## §6. Что сделать дальше, чтобы отчёт стал полным

Один прогон на сервере закрывает всё оставшееся:

```bash
cd /opt/agent-trade && git fetch origin && git checkout claude/probability-targets-reconnaissance-ylg2co

# 1. База (вопросы 1, 2а, 4)
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose exec -T postgres \
    psql -U agenttrade -d agenttrade -X -A -F "|" -f -' < scripts/recon_8_2_0_db.sql \
    | tee /tmp/recon_8_2_0_db.out

# 2. Журнал выгрузки (вопрос 2в)
sudo -u agent tail -n 200 /opt/agent-trade/logs/export.log | tee /tmp/recon_8_2_0_export.out

# 3. Биржа (вопрос 3)
sudo -u agent bash -c 'cd /opt/agent-trade && docker compose --profile backtest run --rm backtest \
    python scripts/recon_8_2_0_okx.py \
    --pairs BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP,XRP-USDT:XRP-USDT-SWAP,DOGE-USDT:DOGE-USDT-SWAP' \
    | tee /tmp/recon_8_2_0_okx.out
```

Все три команды только читают: ни одного `INSERT`, `UPDATE`, `DELETE` или DDL, ни
одного перезапуска контейнера, ни одной строки в конфигурации. Третья команда поднимает
разовый контейнер профиля `backtest` (`--rm`) и не трогает работающие сервисы.
