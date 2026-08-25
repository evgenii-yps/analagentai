# Этап 7.4 — поправка: разделение рынков (спот ↔ контракт)

> **НЕАКТУАЛЬНО С 22.08.2026.** Этап 7.4 закрыт и не выполняется (решение
> заказчика, ТЗ Этапа 8.1). Документ сохранён как описание метода и сделанных
> замеров; код в `backtest/` не удалён, но прогоны не запускаются, а его
> результаты не поддерживаются. Разделение рынков «спот → контракт», найденное
> здесь, перенесено в саму систему Этапом 8.1 (§1 ТЗ 8.1).

Отчёт о выполнении поправки к ТЗ Этапа 7.4 (ошибка §5.2). Дополняет
`docs/STAGE_7_4_REPORT.md`, не заменяет его.

## 0. Коротко

| Требование поправки | Состояние |
|---|---|
| 1. Развести рынки в конфигурации (пары «спот → контракт») | сделано |
| 2. `BT_AGENTS=market` \| `market,futures`; при `market` funding не запрашивается вовсе | сделано |
| 3. Сверка §13.2 сравнивает каждого агента на ЕГО рынке; market даёт 200/200 | сделано, вывод приложен |
| 4. Загруженное сохранить, повторной закачки не требуется | сделано (загрузчик докачивает только недостающее) |
| D-6 `PYTHONPATH` в Dockerfile | сделано |
| D-9 пересборка с `--no-cache`, каталог вместо `.env.backtest` | сделано: правило в Dockerfile/compose, `CODE_REV`, явная ошибка про каталог |
| D-10 загрузчик, реплей и зонд печатают ход работы | сделано |
| §5.3 граница периода при догруженных свечах (найдено по вопросу заказчика) | утечка в расчёте исходов закрыта, см. §4.8 |

## 1. Причина: два рынка, а не один

Замеры на сервере 22.08.2026, приведённые в поправке, подтверждают: продакшн
работает на ДВУХ инструментах одновременно.

```
.env:            SYMBOL=BTC/USDT (спот), SWAP_SYMBOL=BTC/USDT:USDT (контракт)
runner.py:47-54  MarketAgent получает spot_id, FuturesAgent — swap_id
таблица ohlcv:   свечи ТОЛЬКО по инструменту 1 (спот)
таблица funding: пишется по инструменту 2 (контракт)
```

Прежняя редакция кода считала инструмент одним идентификатором на оба ряда.
Отсюда обе поломки:

* прогон на `BTC-USDT-SWAP`: сверка §13.2 дала `market 0/200` — реплей считал
  Market по свечам КОНТРАКТА, а продакшн писал вывод, посчитанный по свечам
  СПОТА. Сверка отработала правильно: она остановила прогон;
* прогон на `BTC-USDT`: `funding-rate-history` вернул HTTP 400,
  `{"code":"51000","msg":"Parameter instId error"}` — у спота истории funding
  не существует.

### 1.1 Вторая причина `0/200`, найденная при исправлении

Только смены рынка мало. Моменты сверки лежат в ЖИВОМ ОКНЕ (с 16.08.2026), то
есть ПОЗЖЕ `BT_PERIOD_TO=2026-08-01`. Загрузчик качал ряд ровно до
`BT_PERIOD_TO`, поэтому в момент сверки самая свежая свеча в базе была от
1 августа, а продакшн считал вывод по свечам того же дня, что и момент. Даже на
верном рынке сверка не могла сойтись.

Хуже: прежний загрузчик был ОДНОПРОХОДНЫМ — он начинал пагинацию от самой ранней
уже загруженной свечи и шёл назад. Поэтому уже загруженный ряд НИКОГДА не
пополнялся свежими свечами, сколько раз его ни запускай.

Исправлено обоими способами:

* `backtest/loader.py` делает ДВА прохода: «свежий хвост» (от конца периода
  назад до самой поздней загруженной точки) и «недостающее начало» (от самой
  ранней загруженной точки назад до `BT_PERIOD_FROM`). Уже загруженное не
  перекачивается — требование 4 поправки выполняется механикой, а не обещанием;
* `backtest/run.py::parity_load_until` догружает ряды ПЕРВОЙ пары (той, на
  которой идёт сверка) до текущего момента. На выборку прогона это не влияет:
  моменты решения по-прежнему берутся из `[BT_PERIOD_FROM, BT_PERIOD_TO]`.

## 2. Что изменено в коде

### 2.1 Конфигурация (`backtest/config.py`)

`BT_INSTRUMENTS` — список ПАР «спот:контракт»:

```
BT_INSTRUMENTS=BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP
```

Свечи грузятся и читаются по левой части, funding — по правой. Достраивание
имени контракта из имени спота ЗАПРЕЩЕНО и не выполняется нигде: имена
инструментов принадлежат бирже, а не нашим соглашениям. Одиночное имя без
разделителя остаётся СПОТОМ и допускается только при `BT_AGENTS=market`; с
Futures отсутствие контракта — ошибка конфигурации до первого запроса к бирже.

`BT_AGENTS` — состав агентов: `market` либо `market,futures`. Market
обязателен (на нём держится блокирующая сверка), `liquidity` отклоняется с
указанием причины (истории стакана не существует), неизвестные имена —
ошибка. Порядок нормализуется, повторы запрещены.

### 2.2 При `BT_AGENTS=market` funding не запрашивается ВООБЩЕ

Не «запрашивается и игнорируется», а не запрашивается:

* `backtest/run.py::_load_history` не вызывает `backfill_funding`;
* `backtest/run.py::_integrity` не проверяет непрерывность ряда funding;
* `backtest/clock.py::build_snapshot` не выполняет SQL-запрос к
  `backtest.funding` (это ещё и снимает по запросу с каждого из десятков тысяч
  моментов решения);
* прогоняется только конфигурация A.

Доказательство — журнал запросов биржи за приёмочный прогон, §4.2 ниже: ноль
обращений к `funding-rate-history`.

### 2.3 Сверка §13.2 сравнивает каждого агента на ЕГО рынке

`backtest/parity.py::check_parity` принимает пару и состав агентов:
Market считается по свечам спота, Futures — по ставкам контракта и только если
Futures включён в `BT_AGENTS`. Рынок каждого агента печатается рядом с его
счётом и попадает в `backtest.runs.config_json` — чтобы «market 0/200» нельзя
было спутать со сравнением разных рынков.

Цена для Futures теперь `None`, а не цена спота: в продакшне по swap-инструменту
в таблице `ohlcv` ноль строк, значит и `FuturesAgent` получает `price=None`.
На направление и уверенность величина не влияет (`analyze_futures` кладёт её
только в метрики), но подстановка цены спота смешивала два рынка.

### 2.4 Дефекты D-6, D-9, D-10

* **D-6.** `ENV PYTHONPATH=/app` добавлен в `backtest/Dockerfile` и в корневой
  `Dockerfile`. Проверка в собранном образе — §4.1.
* **D-9.** Правило «после любого изменения кода — пересборка с `--no-cache`»
  записано в `backtest/Dockerfile` и в `docker-compose.yml`. Дополнительно
  введён `--build-arg CODE_REV`: слой `COPY` со старым кодом перестаёт
  подхватываться из кэша, если ревизия изменилась. Отдельно закрыта причина
  «каталог вместо `backtest/.env.backtest`»: docker создаёт каталог, когда
  указанного в `volumes` файла на хосте нет, — теперь конфигуратор говорит это
  прямым текстом вместо «файл не читается».
* **D-10.** Ход работы печатают: загрузчик (страниц, строк, докуда дошла
  пагинация, сколько периода пройдено), реплей (обработано моментов из
  скольких, процент, решений, пропусков) и зонд (страница, достигнутая дата).

## 3. Как выполнена приёмка и чего эта среда доказать не может

Требование поправки — прогнать полный конвейер ВНУТРИ СОБРАННОГО ОБРАЗА и
приложить фактический вывод. Это сделано. Две оговорки, которые нельзя обойти
молчанием, потому что они меняют смысл цифр:

1. **Биржа недоступна.** Сетевая политика среды закрывает `www.okx.com`
   (`CONNECT` → HTTP 403). Конвейер прогонялся против локального сервера,
   отвечающего теми же двумя эндпоинтами §4 в том же формате, с РАЗНЫМИ
   ценовыми рядами у спота и контракта и с тем же отказом на funding спота
   (`HTTP 400, 51000 Parameter instId error`). Код обращается к нему как к
   бирже — по `https://www.okx.com`, без единой правки в загрузчике.
2. **Продакшн-БД нет.** «Живая сторона» сверки §13.2 (`public.signals`,
   `logic_version = 4`) построена оснасткой, которая идёт ДРУГИМ путём, чем
   реплей: `public.ohlcv` (спот) и `public.funding` (контракт) → запросы формы
   `src/core/db.py` → продакшн-функции агентов. Реплей идёт своим:
   `backtest.candles` / `backtest.funding` → `backtest/clock.py` → те же
   функции. Совпадение возможно только при верной разводке рынков — что и
   проверяется. Допущения оснастки: живая сторона видит только ЗАКРЫТЫЕ свечи,
   `public.funding` заполнен почасовой протяжкой расчётных ставок,
   `open_interest` пуст.

Поэтому «200/200» ниже доказывает РАЗВОДКУ РЫНКОВ и работоспособность
конвейера целиком, но НЕ заменяет сверку на живых данных сервера. Её по-прежнему
надо выполнить на сервере — там и биржа, и настоящие `public.signals`.

## 4. Фактический вывод

### 4.1 Сборка образа и проверка D-6

```
$ docker build --no-cache --build-arg CODE_REV=$(git rev-parse --short HEAD) \
      -f backtest/Dockerfile -t agenttrade-backtest .
$ docker run --rm agenttrade-backtest python -c "import backtest, os; \
      print('PYTHONPATH=', os.environ.get('PYTHONPATH')); print(backtest.__file__)"
PYTHONPATH= /app
backtest импортируется из /app/backtest/__init__.py
```

Ключ `-e PYTHONPATH=/app` больше не нужен.

### 4.2 Полный конвейер, `BT_AGENTS=market` (главный вопрос этапа)

Конфигурация прогона:

```
BT_INSTRUMENTS=BTC-USDT:BTC-USDT-SWAP
BT_AGENTS=market
BT_BAR=1H
BT_PERIOD_FROM=2026-02-01T00:00:00Z
BT_PERIOD_TO=2026-08-01T00:00:00Z
BT_STEP_HOURS=1
BT_HORIZONS=1,4,12,24
BT_FEE_ROUNDTRIP_PCT=0.10
BT_SLIPPAGE_PCT=0.01
BT_OOS_MONTHS=1
BT_REQUEST_PAUSE_MS=20
```

Запуск и ПОЛНЫЙ вывод (лог не сокращён):

```
$ docker compose --profile backtest run --rm backtest
2026-08-22 15:55:26 [warning  ] Подключение идёт продакшн-пользователем, а не выделенной ролью agenttrade_bt: задайте BT_DB_USER/BT_DB_PASSWORD component=backtest.run
2026-08-22 15:55:26 [info     ] Конфигурация прогона           agents=['market'] bar=1H candles_from=['BTC-USDT'] component=backtest.run configurations=['A'] funding_from=не читается instruments=['BTC-USDT:BTC-USDT-SWAP'] period=2026-02-01T00:00:00+00:00 — 2026-08-01T00:00:00+00:00
2026-08-22 15:55:26 [info     ] Счётчики продакшн-таблиц до прогона agent_outputs=0 calibration_curves=0 component=backtest.run funding=2224 ohlcv=1300 open_interest=0 signal_evaluations=0 signals=200
2026-08-22 15:55:26 [info     ] Загрузка истории пары          candles_from=BTC-USDT component=backtest.run extended_for_parity=True funding_from=None pair=BTC-USDT:BTC-USDT-SWAP until=2026-08-22T15:55:26.499005+00:00
2026-08-22 15:55:26 [info     ] Загрузка свечей: начало        already_in_db=0 bar=1H component=backtest.loader in_db_from=None in_db_to=None inst_id=BTC-USDT since=2026-02-01T00:00:00+00:00 until=2026-08-22T15:55:26.499005+00:00
2026-08-22 15:55:26 [info     ] Загрузка свечей: идёт          boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT pages=10 period_done_pct=20.6 phase=весь период reached=2026-07-11T21:00:00+00:00 rows_written=1000 series=свечи
2026-08-22 15:55:27 [info     ] Загрузка свечей: идёт          boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT pages=20 period_done_pct=41.2 phase=весь период reached=2026-05-31T05:00:00+00:00 rows_written=2000 series=свечи
2026-08-22 15:55:27 [info     ] Загрузка свечей: идёт          boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT pages=30 period_done_pct=61.7 phase=весь период reached=2026-04-19T13:00:00+00:00 rows_written=3000 series=свечи
2026-08-22 15:55:28 [info     ] Загрузка свечей: идёт          boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT pages=40 period_done_pct=82.3 phase=весь период reached=2026-03-08T21:00:00+00:00 rows_written=4000 series=свечи
2026-08-22 15:55:28 [info     ] Загрузка свечей: готово        boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT pages=49 period_done_pct=100.0 reached=2026-01-30T09:00:00+00:00 rows_written=4861 series=свечи
2026-08-22 15:55:28 [info     ] Funding НЕ запрашивается: BT_AGENTS=market component=backtest.run pair=BTC-USDT:BTC-USDT-SWAP reason=Futures в прогоне не участвует; у спота истории funding не существует (код 51000)
2026-08-22 15:55:28 [info     ] Целостность рядов              candles_actual=4345 candles_expected=4345 candles_gaps=0 candles_inst=BTC-USDT component=backtest.run funding_actual=None funding_gaps=None funding_inst=None pair=BTC-USDT:BTC-USDT-SWAP
2026-08-22 15:55:31 [info     ] Сверка с продакшном            component=backtest.parity market=BTC-USDT summary=market: направление 200/200, уверенность (±1e-06) 200/200
2026-08-22 15:55:31 [info     ] Прогон открыт, критерий зафиксирован agents=['market'] component=backtest.replay run_id=1
2026-08-22 15:55:31 [info     ] Прогон пары: начало            agents=['market'] candles_from=BTC-USDT component=backtest.replay funding_from=не читается (нет futures) moments=4345 pair=BTC-USDT:BTC-USDT-SWAP run_id=1
2026-08-22 15:55:57 [info     ] Прогон пары: идёт              at=2026-04-25T07:00:00+00:00 component=backtest.replay decisions=1998 of=4345 pair=BTC-USDT:BTC-USDT-SWAP percent=46.0 processed=2000 run_id=1 skipped_gap=0 skipped_no_data=1
2026-08-22 15:56:25 [info     ] Прогон пары: идёт              at=2026-07-17T15:00:00+00:00 component=backtest.replay decisions=3998 of=4345 pair=BTC-USDT:BTC-USDT-SWAP percent=92.1 processed=4000 run_id=1 skipped_gap=0 skipped_no_data=1
2026-08-22 15:56:30 [info     ] Прогон пары: готово            component=backtest.replay decisions=4344 inst_id=BTC-USDT pair=BTC-USDT:BTC-USDT-SWAP run_id=1 skipped_gap=0 skipped_no_data=1
2026-08-22 15:56:30 [info     ] Нет направленных решений       component=backtest.evaluate inst_id=BTC-USDT run_id=1
2026-08-22 15:56:30 [info     ] Прогон закрыт                  component=backtest.replay run_id=1 status=ok
2026-08-22 15:56:30 [info     ] Отчёт собран                   component=backtest.run config=A path=/opt/agent-trade/analysis_out/report_7_4_configA_20260822.txt run_id=1
2026-08-22 15:56:30 [info     ] Счётчики продакшн-таблиц после прогона agent_outputs=0 calibration_curves=0 component=backtest.run funding=2224 ohlcv=1300 open_interest=0 signal_evaluations=0 signals=200
2026-08-22 15:56:30 [info     ] Продакшн-таблицы не изменились (счётчики строк совпали) component=backtest.run
{"runs": {"A": 1}, "production_unchanged": true}
```

Строка сверки §13.2 — та, ради которой делалась поправка:

```
2026-08-22 15:55:31 [info     ] Сверка с продакшном            component=backtest.parity market=BTC-USDT summary=market: направление 200/200, уверенность (±1e-06) 200/200
```

Было `market 0/200`, стало `200/200` по направлению и по уверенности
(допуск 1e-6), на рынке `BTC-USDT` — то есть на СПОТЕ, где его считает
продакшн. Требование 3 поправки выполнено.

Замечание к строке `Нет направленных решений`: при продакшновом
`MIN_AGENTS=2` один Market не набирает кворума, и Decision Agent отдаёт
`wait`. Это поведение самой системы, а не решение реплея; веса при
отсутствующем агенте не перераспределяются (§3.4 ТЗ). Оно же зафиксировано
тестом `test_single_agent_configuration_uses_standard_mechanism`.

### 4.3 Ноль обращений к funding при `BT_AGENTS=market`

Журнал запросов, полученных биржей за весь прогон §4.2:

```
      1 /api/v5/market/history-candles instId=BTC-USDT
      4 /api/v5/public/funding-rate-history instId=BTC-USDT-SWAP
```

(журнал очищался перед прогоном; за прогон §4.2 к
`funding-rate-history` не было НИ ОДНОГО обращения — требование 2 поправки)

### 4.4 Контрольный прогон: старая конфигурация обязана падать

Тот же образ, та же БД, отличается одна строка: `BT_INSTRUMENTS=BTC-USDT-SWAP`
(как в первой редакции ТЗ — один идентификатор на оба ряда).

```
2026-08-22 15:56:54 [info     ] Сверка с продакшном            component=backtest.parity market=BTC-USDT-SWAP summary=market: направление 109/200, уверенность (±1e-06) 0/200
2026-08-22 15:56:54 [error    ] Сверка с продакшном НЕ ПРОЙДЕНА: реплей воспроизводит не ту систему component=backtest.run market=market: направление 109/200, уверенность (±1e-06) 0/200
2026-08-22 15:56:54 [error    ] Прогон остановлен: сверка §13.2 не пройдена component=backtest.run
$ echo $?
3
```

Сверка не проходит, конвейер останавливается ДО построения отчёта, код
возврата 3. Совпадение направления 109/200 и уверенности 0/200 — ровно тот
признак сравнения разных рынков, который на сервере выглядел как 0/200.

### 4.5 Полный конвейер, `BT_AGENTS=market,futures`

```
2026-08-22 15:57:10 [info     ] Конфигурация прогона           agents=['market', 'futures'] bar=1H candles_from=['BTC-USDT'] component=backtest.run configurations=['A', 'B'] funding_from=['BTC-USDT-SWAP'] instruments=['BTC-USDT:BTC-USDT-SWAP'] period=2026-02-01T00:00:00+00:00 — 2026-08-01T00:00:00+00:00
2026-08-22 15:57:10 [info     ] Загрузка истории пары          candles_from=BTC-USDT component=backtest.run extended_for_parity=True funding_from=BTC-USDT-SWAP pair=BTC-USDT:BTC-USDT-SWAP until=2026-08-22T15:57:10.729208+00:00
2026-08-22 15:57:10 [info     ] Загрузка funding: готово       boundary=2026-02-01T00:00:00+00:00 component=backtest.loader inst_id=BTC-USDT-SWAP pages=4 period_done_pct=45.7 reached=2026-05-22T00:00:00+00:00 rows_written=278 series=funding
2026-08-22 15:57:10 [info     ] Целостность рядов              candles_actual=4345 candles_expected=4345 candles_gaps=0 candles_inst=BTC-USDT component=backtest.run funding_actual=214 funding_gaps=0 funding_inst=BTC-USDT-SWAP pair=BTC-USDT:BTC-USDT-SWAP
2026-08-22 15:57:14 [info     ] Сверка с продакшном            component=backtest.parity market=BTC-USDT summary=market: направление 200/200, уверенность (±1e-06) 200/200
2026-08-22 15:57:14 [info     ] Сверка с продакшном            component=backtest.parity market=BTC-USDT-SWAP summary=futures: направление 200/200, уверенность (±1e-06) 200/200
2026-08-22 15:57:14 [info     ] Прогон открыт, критерий зафиксирован agents=['market'] component=backtest.replay run_id=2
2026-08-22 15:58:13 [info     ] Прогон пары: готово            component=backtest.replay decisions=4344 inst_id=BTC-USDT pair=BTC-USDT:BTC-USDT-SWAP run_id=2 skipped_gap=0 skipped_no_data=1
2026-08-22 15:58:13 [info     ] Отчёт собран                   component=backtest.run config=A path=/opt/agent-trade/analysis_out/report_7_4_configA_20260822.txt run_id=2
2026-08-22 15:58:13 [info     ] Прогон открыт, критерий зафиксирован agents=['market', 'futures'] component=backtest.replay run_id=3
2026-08-22 15:59:17 [info     ] Прогон пары: готово            component=backtest.replay decisions=4344 inst_id=BTC-USDT pair=BTC-USDT:BTC-USDT-SWAP run_id=3 skipped_gap=0 skipped_no_data=1
2026-08-22 15:59:18 [info     ] Исходы посчитаны               component=backtest.evaluate inst_id=BTC-USDT rows=5368 run_id=3
2026-08-22 15:59:18 [info     ] Отчёт собран                   component=backtest.run config=B path=/opt/agent-trade/analysis_out/report_7_4_configB_20260822.txt run_id=3
2026-08-22 15:59:18 [info     ] Продакшн-таблицы не изменились (счётчики строк совпали) component=backtest.run
{"runs": {"A": 2, "B": 3}, "production_unchanged": true}
```

Каждый агент сверяется на своём рынке и совпадает полностью: Market на
`BTC-USDT`, Futures на `BTC-USDT-SWAP`. Прогнаны обе конфигурации (A и B),
исходы посчитаны, отчёты собраны, продакшн-таблицы не изменились (§14.6).

Лог выше снят до правки границы периода (§4.8). После неё тот же прогон даёт
`rows=5291 skipped_horizon_after_period_to=41`: наблюдения, чей горизонт
выходит за `BT_PERIOD_TO`, исключаются, а не досчитываются свежими свечами.

Журнал запросов биржи за этот прогон:

```
      1 /api/v5/market/history-candles instId=BTC-USDT
      4 /api/v5/public/funding-rate-history instId=BTC-USDT-SWAP
```

Свечей запрошена ОДНА страница вместо 49: период уже был загружен прогоном
§4.2, и загрузчик докачал только свежий хвост. Это требование 4 поправки —
загруженное сохраняется, повторной закачки нет.

### 4.6 Зонд глубины истории — ход работы теперь виден (D-10)

```
$ docker compose --profile backtest run --rm backtest \
      python scripts/probe_history_depth.py \
      --instruments BTC-USDT:BTC-USDT-SWAP,ETH-USDT

 Пары «спот → контракт»: свечи зондируются на споте, funding — на контракте.
   BTC-USDT → BTC-USDT-SWAP
   ETH-USDT → контракт не задан, funding не зондируется

=== Проверка доступа ===
  подпись клиента (User-Agent): python-httpx/0.28.1
  ответ OKX на пробный запрос: HTTP 200

=== BTC-USDT:BTC-USDT-SWAP === (свечи: BTC-USDT, funding: BTC-USDT-SWAP)
    [свечи BTC-USDT] страница 10: дошли до 2026-07-11T21:00:00+00:00 (≈1.4 мес назад)
    [свечи BTC-USDT] страница 20: дошли до 2026-05-31T05:00:00+00:00 (≈2.8 мес назад)
    [свечи BTC-USDT] страница 30: дошли до 2026-04-19T13:00:00+00:00 (≈4.2 мес назад)
    [свечи BTC-USDT] страница 40: дошли до 2026-03-08T21:00:00+00:00 (≈5.5 мес назад)
    [свечи BTC-USDT] страница 50: дошли до 2026-01-26T05:00:00+00:00 (≈6.9 мес назад)
    [свечи BTC-USDT] страница 58: пусто — край истории
    [funding BTC-USDT-SWAP] страница 4: пусто — край истории
  свечи (BTC-USDT): пагинация — after = записи РАНЬШЕ указанной метки (движение назад по времени)
  funding (BTC-USDT-SWAP): интервалы, ч: [8]; записей в сутки ≈ 3.0
  funding (BTC-USDT-SWAP): самая ранняя точка 2026-05-22T00:00:00+00:00

=== ETH-USDT === (свечи: ETH-USDT, funding: —)
  funding: НЕ ЗОНДИРУЕТСЯ — контракт для этой пары не задан. У спота истории
  funding не существует (код 51000)
```

Даты и глубины здесь принадлежат заменителю биржи, а не OKX: смысл вывода — в
том, что ход работы печатается, пары разводятся и отказ по споту назван прямо.
Настоящие глубины даст зонд на сервере.

### 4.7 Тесты и линтер (в том же образе)

```
$ docker run --rm ... -e BT_TEST_DSN=postgresql://.../bt_test agenttrade-backtest \
      python -m pytest tests -q
291 passed, 1 skipped in 53.60s

$ docker run --rm ... agenttrade-backtest ruff check backtest scripts tests
All checks passed!
```

Это НЕ приёмочное доказательство — оно в §4.2–4.5. Тесты здесь означают лишь,
что правка не сломала остального.

### 4.8 Граница периода при загруженных «свежих» свечах (§5.3 ТЗ)

Догрузка ряда пары сверки означает, что в `backtest.candles` ЕСТЬ свечи позже
`BT_PERIOD_TO`. Разбор, что с ними происходит:

**1. Решения — только внутри периода.** `backtest/clock.py::decision_times`
строит сетку от `BT_PERIOD_FROM` до `BT_PERIOD_TO` включительно
(`while cursor <= cfg.period_to`), а `replay_instrument` идёт ровно по ней.
Загруженное содержимое таблицы на этот список не влияет никак.

**2. Исходы — граница жёсткая, наблюдение исключается.** Здесь была УТЕЧКА, и
она найдена по этому вопросу: `_context_series` выбирал свечи до
`period_to + max(horizons) + 1ч`, а `_price_at_close` вовсе не имел верхней
границы. Пока загрузка обрывалась на `BT_PERIOD_TO`, это ничего не значило —
свечей за границей просто не было. После введения догрузки те же строки начали
молча досчитывать исходы у конца периода свежими данными. Исправлено
(`backtest/evaluate.py`): верхняя граница выборки равна `BT_PERIOD_TO`,
у `_price_at_close` граница стоит в SQL, а наблюдение с горизонтом за границей
ИСКЛЮЧАЕТСЯ и считается отдельным счётчиком в логе.

**3. Сверка §13.2 — единственное место, где чтение за границей допускается.**
`parity.check_parity` строит снимки на моментах живого окна; `candles_at`
ограничивает выборку `close_time <= ts` момента сверки. Ни решений, ни исходов
эти чтения не порождают. Остальные потребители ограничены: `integrity` —
`BETWEEN period_from AND period_to`, реплей — `ts <= period_to`, отчёт читает
только `backtest.decisions` и `backtest.outcomes`.

Проверка на фактической базе после прогона §4.5:

```
 candles_max_close_time |    decisions_max_ts    | outcomes_max_horizon_end |      bt_period_to
------------------------+------------------------+--------------------------+------------------------
 2026-08-22 13:00:00+00 | 2026-08-01 00:00:00+00 | 2026-08-01 00:00:00+00   | 2026-08-01 00:00:00+00

 outcomes_past_period_to   0        (исходов за границей нет)
 candles_past_period_to  517        (свечи за границей загружены — и не используются)
```

В логе прогона число отброшенных наблюдений видно явно:

```
Исходы посчитаны  rows=5291 skipped_horizon_after_period_to=41
```

Утечка стережётся тремя тестами (`tests/backtest/test_market_split.py`):
`test_decision_times_never_leave_the_period`,
`test_replay_and_outcomes_stop_at_period_to`,
`test_outcome_count_does_not_depend_on_how_much_is_loaded`. Последние два
ПАДАЮТ на коде до правки — проверено подстановкой прежнего `evaluate.py` в тот
же образ:

```
E  AssertionError: исход посчитан по свечам позже BT_PERIOD_TO
E  AssertionError: число исходов изменилось от догрузки свежих свечей — граница периода протекает
   assert 868 == 904
2 failed, 2 passed
```

Третий тест — про воспроизводимость: если бы наблюдения у конца периода
досчитывались свежими данными, число исходов зависело бы от дня запуска, и два
прогона одной конфигурации перестали бы сравниваться между собой.

## 5. Что делать на сервере

```bash
git pull                                   # ветка claude/stage-7-4-market-split-s0941y

cp backtest/.env.backtest.example backtest/.env.backtest   # ФАЙЛ, не каталог (D-9)

# Пересборка ОБЯЗАТЕЛЬНО с --no-cache после любого изменения кода (D-9):
docker compose --profile backtest build --no-cache \
    --build-arg CODE_REV=$(git rev-parse --short HEAD) backtest

# Шаг 1. Зонд — по парам, свечи со спота, funding с контракта:
docker compose --profile backtest run --rm backtest \
    python scripts/probe_history_depth.py \
    --instruments BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP

# Шаг 2. Заполнить BT_PERIOD_FROM и BT_REQUEST_PAUSE_MS по выводу зонда,
#        оставить BT_AGENTS=market (главный вопрос этапа).

# Шаг 3. Прогон:
docker compose --profile backtest run --rm backtest
```

Ключ `-e PYTHONPATH=/app` больше не нужен (D-6). Уже загруженные 39 408 свечей
`BTC-USDT` за 01.02.2022–31.07.2026 останутся на месте: загрузчик докачает
только свежий хвост до момента сверки, старое не тронет.

Ожидаемая строка сверки на сервере:

```
Сверка с продакшном  market=BTC-USDT summary=market: направление 200/200, уверенность (±1e-06) 200/200
```

Если она снова покажет расхождение — теперь в логе и в `runs.config_json` видно,
НА КАКОМ РЫНКЕ считался каждый агент, и расхождение можно будет обсуждать по
существу, а не гадать о причине.

## 6. Ограничения, не снятые этой поправкой

1. Сверка на живых данных сервера НЕ выполнена: в этой среде нет ни биржи, ни
   продакшн-БД (см. §3). Приложенный вывод получен на заменителе биржи и
   оснастке живой стороны.
2. Глубина истории funding — около трёх месяцев, поэтому конфигурация B
   остаётся разведочной. Именно поэтому `BT_AGENTS=market` — значение по
   умолчанию.
3. История открытого интереса среди разрешённых §4 эндпоинтов отсутствует:
   подтверждения со стороны OI в реплее не бывает, уверенность Futures
   систематически ниже продакшновой.
4. Продакшн пишет ТЕКУЩУЮ ставку, история отдаёт РАСЧЁТНУЮ. Величины близки,
   но не тождественны; на живых данных это может дать расхождение по Futures —
   оно не блокирует прогон, но делает конфигурацию B непубликуемой.
5. Liquidity не проверяется и проверен быть не может: истории стакана не
   существует.
