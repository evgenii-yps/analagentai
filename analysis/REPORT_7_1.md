# Этап 7.1 — диагностика предсказательной способности ядра

**Проект:** Agent Trade **Дата подготовки пакета:** 15.08.2026
**Тип работы:** аналитическая. Код в `src/` не изменялся, параметры `.env` не изменялись,
контейнеры не перезапускались, запись в БД не выполнялась.

---

> **Обновление Этапа 7.3 (Блок D).** Пакет `analysis/` теперь параметризован
> целевой версией логики. Переменная окружения `TARGET_LOGIC_VERSION` (по
> умолчанию **4**) подставляется в расчёты 1–5 и 7 как `:target_version`;
> расчёт 6 по-прежнему идёт по всем версиям сразу. Повторить измерение
> Этапа 7.1 в точности: `TARGET_LOGIC_VERSION=1 sudo bash analysis/run_7_1.sh`.
> Имя файла отчёта содержит версию: `report_7_1_v<версия>_<дата>.txt`.
>
> Добавлены блоки: **2.10–2.12** (направления Futures по версиям с явной
> проверкой «bearish > 0» и фактическое распределение funding/OI), **3.8–3.10**
> (калибровочная таблица по `calibrated_probability`, история кривых, контроль
> единственной активной кривой), **4.5–4.6** (доля повторных решений и число
> уникальных наборов входов), **Расчёт 7** — сравнение целевой версии с версией 1
> двумя колонками рядом (`analysis/sql/07_compare.sql`).
>
> Новые блоки требуют миграции Этапа 7.3; запросы, использующие только прежние
> поля, работают на версиях 1–3 без изменений.

## Как заполняется этот отчёт

Числовые таблицы ниже **намеренно оставлены пустыми**: расчёты выполняются на сервере,
у автора пакета доступа к серверу нет. Порядок работы:

1. На сервере выполняется `analysis/run_7_1.sh` (команда — в конце ответа исполнителя).
2. Скрипт печатает вывод на экран и пишет его в `/opt/agent-trade/analysis_out/report_7_1_<дата>.txt`.
3. Аналитик переносит числа из блоков вывода в таблицы этого файла: нумерация блоков
   вывода (`1.1`, `2.4`, `6.7` …) совпадает с нумерацией таблиц здесь.
4. Каждая строка каждой таблицы содержит размер выборки. Строки без размера выборки
   не заполняются и не используются в выводах.

Условные обозначения: **X из N** — числитель (успехи) и знаменатель (наблюдения);
«независимая выборка» — по одному сигналу на непересекающееся 4-часовое окно;
«полная выборка» — все закрытые сигналы (наблюдения зависимы).

---

## 0. Расхождения фактической схемы БД с ТЗ

Раздел заполнен по исходному коду (`db/init.sql`, `src/core/db.py`, `src/decision/agent.py`)
**до** запуска на сервере. Блок вывода `СВЕРКА СХЕМЫ БД` (запросы 0.1–0.22) подтверждает
или опровергает каждый пункт фактическими данными; расхождения, обнаруженные при запуске,
дописываются в таблицу 0.2.

### 0.1 Что расходится с формулировками ТЗ

| Как в ТЗ | Как на самом деле | Следствие для расчётов |
|---|---|---|
| колонка со значением «балл» (score) | **отсутствует**: отдельной колонки нет ни в одной таблице | балл восстанавливается пересчётом из `signals.agents_payload` по формуле кода; вторым путём — разбором текста `signals.rationale` (там он округлён до 2 знаков) |
| колонка «согласованность» (agreement) | **отсутствует** | то же самое; в тексте `rationale` присутствует с Этапа 4 (коммит `a834194`), то есть и у версии 1 |
| поле `pnl_4h` | фактически `signal_evaluations.pnl_pct` при `horizon = '4h'`; сводка того же значения дублируется в `signals.pnl_pct` и `signals.success` при закрытии сигнала | во всех расчётах исход берётся из `signal_evaluations` (горизонт указан явно) |
| `logic_version` у старых записей может быть NULL | колонка объявлена `SMALLINT NOT NULL DEFAULT 1` (`db/init.sql:89`, миграция `ensure_signals_logic_version`) — **NULL невозможен**, старые записи получили 1 | разделение по версиям выполняется прямо по колонке, догадки не нужны |
| колонка `degraded` | есть: `BOOLEAN NOT NULL DEFAULT FALSE`, добавлена Этапом 7.2 (`ensure_signals_degraded`); старые записи не пересчитывались | флаг содержателен только для версии 3; в версиях 1–2 он всегда `false` и ничего не означает |
| — | в `agent_outputs` **нет** колонок `logic_version` и `degraded` | версии для Расчёта 4 и блоков 6.9 определяются по времени, а границы берутся из `signals` (не вписаны константами) |
| — | в `ohlcv` **нет** таймфрейма `4h`: собираются `1m,5m,15m,1h` (`TIMEFRAMES` в `src/core/config.py:38`) | базовые линии Расчёта 1 считаются по `close` 1m-свечей — так же, как берёт цену оценщик (`get_price_at`, `timeframe='1m'`) |
| — | у агентов разные инструменты: Futures работает по swap, остальные по spot; сигнал пишется под один инструмент | мнения агентов берутся из `agents_payload` сигнала, а не джойном `agent_outputs` по времени |

### 0.2 Расхождения, обнаруженные при запуске на сервере

_(заполняется по блоку «СВЕРКА СХЕМЫ БД»; если расхождений нет — так и записать)_

| Что проверялось | Ожидание | Факт по выводу | Влияние на расчёты |
|---|---|---|---|
| наличие колонок `score` / `agreement` (0.8) | отсутствуют | | |
| `logic_version`: NULL, состав версий (0.10, 0.11) | NULL нет; v1 ≈ 5623 закрытых | | |
| границы версий (0.11) | v2 с 13.08 15:41 UTC, v3 с 14.08 13:39 UTC | | |
| `degraded`: первая дата true (0.12, 0.13) | не ранее 14.08 | | |
| разбор `rationale` регуляркой (0.15) | 100 % строк | | |
| таймфреймы `ohlcv` (0.18) | 4h отсутствует | | |
| прочее | | | |

---

## 1. Расчёт 1 — базовая линия и общая результативность

Область: `logic_version = 1`, решения `wait` исключены, исход по горизонту 4h.

### 1.1 Размер выборки (блок 1.1)

| Показатель | Значение |
|---|---|
| независимых окон | |
| первое окно (UTC) | |
| последнее окно (UTC) | |
| окон без цены в `ohlcv` | |
| закрытых сигналов в полной выборке | |

### 1.2 Доля успеха, независимая выборка (блок 1.2)

| Группа | Успехов X | Наблюдений N | Доля, % |
|---|---|---|---|
| всего | | | |
| buy | | | |
| sell | | | |

### 1.3 pnl 4h, независимая выборка (блок 1.3)

| Группа | N | Среднее, % | Медиана, % | Мин., % | Макс., % |
|---|---|---|---|---|---|
| всего | | | | | |
| buy | | | | | |
| sell | | | | | |

### 1.4 Три базовые линии на тех же окнах (блок 1.4) — **ключевая таблица этапа**

| Стратегия | Успехов X | Наблюдений N | Доля, % |
|---|---|---|---|
| всегда buy (цена выросла) | | | |
| всегда sell (цена упала) | | | |
| фактический результат системы | | | |

### 1.5 Полная выборка версии 1 (блоки 1.7–1.9)

> Наблюдения зависимы: сигналы выдаются раз в минуту, соседние описывают почти один
> и тот же отрезок рынка. Доверительные интервалы к этим числам неприменимы.

| Группа | Успехов X | Наблюдений N | Доля, % | Среднее pnl, % | Медиана pnl, % |
|---|---|---|---|---|---|
| всего | | | | | |
| buy | | | | | |
| sell | | | | | |

**Интерпретация (2–3 предложения):**
_(отличается ли результат системы от лучшей из тривиальных стратегий и на скольких
наблюдениях; во сколько раз независимая выборка меньше полной)_

---

## 2. Расчёт 2 — вклад каждого агента по отдельности

Мнение агента берётся из `signals.agents_payload` — ровно то, что видел Decision Agent.

### 2.1 Распределение уверенности, независимые окна (блок 2.1)

| Агент | Окон N | Есть мнение | Нет мнения | p25 | Медиана | p75 | p99 | conf = 0 | conf < 0.01 | conf = 1.0 |
|---|---|---|---|---|---|---|---|---|---|---|
| market | | | | | | | | | | |
| liquidity | | | | | | | | | | |
| futures | | | | | | | | | | |

### 2.2 То же по всем выводам `agent_outputs` версии 1 (блок 2.2, наблюдения зависимы)

| Агент | Выводов N | p25 | Медиана | p75 | p99 | conf = 0, % | conf < 0.01, % | conf = 1.0, % |
|---|---|---|---|---|---|---|---|---|
| market | | | | | | | | |
| liquidity | | | | | | | | |
| futures | | | | | | | | |

### 2.3 Распределение направлений, независимые окна (блок 2.3)

| Агент | Окон N | bullish X (%) | bearish X (%) | neutral X (%) | нет мнения X |
|---|---|---|---|---|---|
| market | | | | | |
| liquidity | | | | | |
| futures | | | | | |

### 2.4 Направление агента против фактического движения цены (блок 2.4)

Сравнивать с базовой линией из таблицы 1.4.

| Агент | Направление | N | Цена вверх X (%) | Цена вниз X (%) | Попадание, % |
|---|---|---|---|---|---|
| market | bullish | | | | |
| market | bearish | | | | |
| liquidity | bullish | | | | |
| liquidity | bearish | | | | |
| futures | bullish | | | | |
| futures | bearish | | | | |

### 2.5 Уверенность против исхода: две группы по медиане (блок 2.5)

| Агент | Медиана уверенности | Группа | N | Успехов X | Доля, % |
|---|---|---|---|---|---|
| market | | ≥ медианы | | | |
| market | | < медианы | | | |
| liquidity | | ≥ медианы | | | |
| liquidity | | < медианы | | | |
| futures | | ≥ медианы | | | |
| futures | | < медианы | | | |

### 2.6 Согласие агента с итоговым решением (блок 2.6)

| Агент | Окон N | Совпало X (%) | Противоположно X | neutral X | нет мнения X |
|---|---|---|---|---|---|
| market | | | | | |
| liquidity | | | | | |
| futures | | | | | |

### 2.7 Пропуски Market по суткам (блоки 2.7–2.9)

| Сутки (UTC) | Версия | Циклов | Market отсутствует | Liquidity отсутствует | Futures отсутствует | Сбоев в `agent_failures` |
|---|---|---|---|---|---|---|
| | | | | | | |

**Интерпретация (2–3 предложения):**
_(есть ли агент, чьё направление предсказывает движение лучше базовой линии, и на каком N;
прекратились ли пропуски Market после 14.08 13:39 UTC)_

---

## 3. Расчёт 3 — информативность балла и согласованности

### 3.1 Формула вероятности: проверка по исходному коду — **выполнена**

Файл `src/decision/agent.py`, функция `make_decision`:

| Величина | Строки | Код дословно |
|---|---|---|
| балл | 104–110 | `numerator += direction * confidence * weight` / `denominator += weight * confidence` / `score = numerator / denominator if denominator > 0 else 0.0` |
| согласованность | 124–127 | `directions = [_SIGNAL_VALUE[o["signal"]] for o in fresh]` / `pos = sum(1 for d in directions if d > 0)` / `neg = sum(1 for d in directions if d < 0)` / `agreement = abs(pos - neg) / total_agents if total_agents > 0 else 0.0` |
| вероятность | 128 | `probability = round(min(abs(score) * (0.5 + 0.5 * agreement), 1.0), 4)` |

Где `direction` = +1 для `bullish`, −1 для `bearish`, 0 для `neutral` (строка 23),
а `weight` — вес агента из `.env` (`WEIGHT_MARKET` / `WEIGHT_LIQUIDITY` / `WEIGHT_FUTURES`,
значения по умолчанию 1.0, `src/core/config.py:77-79`).

**Вывод: формула из ТЗ `вероятность = |балл| × (0.5 + 0.5 × согласованность)` подтверждается
дословно.** Уточнение касается знаменателя согласованности:

* до Этапа 7.2 знаменателем было число **свежих** агентов (`len(fresh)`);
* с Этапа 7.2 (Задача B1) знаменатель — **полное** число агентов, `total_agents = 3`
  (строки 38–40, 71–72, 121–127): выпадение агента теперь понижает согласованность,
  а не повышает её. Это изменение и делает статистику версий 2 и 3 несравнимой.

Балл — не среднее направлений, а взвешенное направление с делением на сумму уверенностей:
нейтральный агент **не** влияет на числитель, но увеличивает знаменатель, то есть гасит балл.

Блок вывода 3.1 проверяет ту же формулу на фактических данных: пересчитанная из
`agents_payload` вероятность сравнивается с сохранённой в `signals.probability`.
Совпадение подтверждает одновременно и формулу, и предположение о весах, равных 1.0.

| Версия | Сигналов N | Совпало при знаменателе «свежие» X (%) | Совпало при знаменателе 3 X (%) |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |

### 3.2 Доля успеха по квартилям |балла| (блок 3.3)

| Квартиль | N | \|балл\| от | \|балл\| до | Успехов X | Доля, % |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |

### 3.3 Доля успеха по значениям согласованности (блок 3.4)

| Согласованность | Агентов в payload | N | Успехов X | Доля, % |
|---|---|---|---|---|
| 0.00 | | | | |
| 0.33 | | | | |
| 0.50 | | | | |
| 0.67 | | | | |
| 1.00 | | | | |

### 3.4 Доля успеха по квартилям probability (блок 3.5)

| Квартиль | N | prob от | prob до | Успехов X | Доля, % |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |

### 3.5 Калибровочная таблица: заявленная вероятность против фактической (блок 3.6)

| Диапазон | N | Заявленная (среднее) | Успехов X | Фактическая доля | Разрыв |
|---|---|---|---|---|---|
| 0.0 – 0.2 | | | | | |
| 0.2 – 0.4 | | | | | |
| 0.4 – 0.6 | | | | | |
| 0.6 – 0.8 | | | | | |
| 0.8 – 1.0 | | | | | |

Та же таблица по полной выборке версии 1 — блок 3.7 (наблюдения зависимы, заполнять
отдельно и не смешивать с независимой).

**Интерпретация (2–3 предложения):**
_(растёт ли доля успеха с ростом заявленной вероятности; монотонна ли зависимость;
на каких N держатся крайние диапазоны)_

---

## 4. Расчёт 4 — фактическая частота обновления входных данных

Версия для `agent_outputs` определяется по времени: границы взяты из `signals` (блок 4.0).

### 4.1 Серии подряд идущих циклов с одинаковой уверенностью (блок 4.1)

| Агент | Версия | Серий | Циклов | Средняя длина | Максимальная длина | Медиана |
|---|---|---|---|---|---|---|
| market | 1 | | | | | |
| market | 3 | | | | | |
| liquidity | 1 | | | | | |
| liquidity | 3 | | | | | |
| futures | 1 | | | | | |
| futures | 3 | | | | | |

### 4.2 Доля циклов без изменения значения (блок 4.2)

| Агент | Версия | Сравнимых циклов | confidence не изменился X (%) | signal не изменился X (%) |
|---|---|---|---|---|
| market | 1 | | | |
| market | 3 | | | |
| liquidity | 1 | | | |
| liquidity | 3 | | | |
| futures | 1 | | | |
| futures | 3 | | | |

### 4.3 Число уникальных значений уверенности за сутки (блок 4.3)

| Сутки (UTC) | Агент | Выводов | Уникальных значений | Доля уникальных, % |
|---|---|---|---|---|
| | | | | |

**Интерпретация (2–3 предложения):**
_(сколько в системе реально независимой информации; сопоставить длину серий
с интервалом решения в одну минуту и с горизонтом 4 часа)_

---

## 5. Расчёт 5 — корреляция между агентами

### 5.1 Попарное совпадение направлений, независимые окна версии 1 (блок 5.1)

| Пара | Окон всего | Оба присутствуют N | Совпало X | Доля, % | Разошлись X | Один отсутствует X |
|---|---|---|---|---|---|---|
| market / liquidity | | | | | | |
| market / futures | | | | | | |
| liquidity / futures | | | | | | |

### 5.2 То же без учёта нейтральных мнений (блок 5.2)

| Пара | Оба направленны N | Совпало X | Доля, % |
|---|---|---|---|
| market / liquidity | | | |
| market / futures | | | |
| liquidity / futures | | | |

**Интерпретация (2–3 предложения):**
_(выполняется ли принцип 3 проекта о низкой согласованности агентов; нет ли пары,
дублирующей друг друга, чей совместный вес фактически удвоен)_

---

## 6. Расчёт 6 — почему система замолчала

Выполняется по каждой версии отдельно. Из версии 3 исключены записи `degraded = true`
(кроме таблицы 6.7, где они подсчитываются).

### 6.1 Объём данных по версиям (блок 6.0)

| Версия | Решений всего | Направленных | С (UTC) | По (UTC) | Суток |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

### 6.2 Распределение probability (блоки 6.1 — все решения, 6.2 — только buy/sell)

| Показатель | v1 | v2 | v3 |
|---|---|---|---|
| медиана | | | |
| p75 | | | |
| p90 | | | |
| p95 | | | |
| p99 | | | |
| максимум | | | |
| N решений | | | |

### 6.3 Распределение |балла| (блок 6.3)

| Показатель | v1 | v2 | v3 |
|---|---|---|---|
| медиана | | | |
| p75 | | | |
| p90 | | | |
| p95 | | | |
| максимум | | | |
| N решений | | | |

### 6.4 Распределение согласованности (блок 6.4)

| Значение | v1 X (%) | v2 X (%) | v3 X (%) |
|---|---|---|---|
| 0.00 | | | |
| 0.33 | | | |
| 0.50 | | | |
| 0.67 | | | |
| 1.00 | | | |

### 6.5 Решения с probability ≥ 0.7 (блок 6.5)

| Версия | Решений всего | ≥ 0.7 X | Доля, % | Направленных N | Направленных ≥ 0.7 X | Доля, % |
|---|---|---|---|---|---|---|
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |

### 6.6 Разбивка buy / sell / wait (блок 6.6)

| Решение | v1 X (%) | v2 X (%) | v3 X (%) |
|---|---|---|---|
| buy | | | |
| sell | | | |
| wait | | | |

### 6.7 Версия 3: число кандидатов при семи порогах (блок 6.7)

Таблица только измеряет. Решение о пороге принимается отдельно, после обсуждения;
`.env` этим этапом не изменяется.

| Порог | Кандидатов X | Направленных N | Доля, % | Кандидатов в сутки |
|---|---|---|---|---|
| 0.70 | | | | |
| 0.65 | | | | |
| 0.60 | | | | |
| 0.55 | | | | |
| 0.50 | | | | |
| 0.45 | | | | |
| 0.40 | | | | |

Для сопоставления (блок 6.7б) — сколько кандидатов в сутки давала версия 1 при пороге 0.7:
| Направленных N | Кандидатов ≥ 0.7 | Суток | Кандидатов в сутки |
|---|---|---|---|
| | | | |

### 6.8 Деградированные решения версии 3 (блок 6.8)

| Версия | Решений всего | `degraded = true` X | Доля, % | Первое (UTC) | Последнее (UTC) |
|---|---|---|---|---|---|
| 3 | | | | | |

### 6.9 Проверка альтернативного объяснения: доля neutral по версиям (блоки 6.9, 6.10)

| Агент | Версия | Выводов N | neutral X | Доля, % | bullish X | bearish X |
|---|---|---|---|---|---|---|
| market | 1 | | | | | |
| market | 3 | | | | | |
| liquidity | 1 | | | | | |
| liquidity | 3 | | | | | |
| futures | 1 | | | | | |
| futures | 3 | | | | | |

**Интерпретация (2–3 предложения):**
_(объясняется ли падение числа кандидатов снижением |балла| и согласованности, или
доля neutral выросла и причина в другом; что именно изменилось между версиями 1 и 3)_

---

## 7. Чего эти данные не показывают

Ограничения, которые нельзя обойти обработкой уже собранных данных. Они одинаково
действуют на все шесть расчётов и должны сопровождать любой вывод по ним.

1. **Малый размер независимой выборки.** Независимых 4-часовых окон за период версии 1
   порядка тридцати. На такой выборке разница в 10–15 процентных пунктов между стратегиями
   статистически неотличима от случайности. Числа X и N приведены отдельно именно для того,
   чтобы аналитик мог посчитать доверительный интервал и убедиться в этом сам.
2. **Полная выборка не спасает.** Пять с лишним тысяч закрытых сигналов версии 1 — это не
   пять тысяч независимых наблюдений: решения выдаются раз в минуту, а горизонт оценки —
   четыре часа, поэтому соседние сигналы описывают почти один и тот же отрезок рынка.
   Все таблицы по полной выборке помечены как зависимые.
3. **Единственный рыночный режим.** Период наблюдения — около недели одного состояния рынка.
   Результат ничего не говорит о поведении ядра в другом режиме (сильный тренд, обвал,
   низкая волатильность).
4. **Единственный токен и единственная биржа.** Все данные — по BTC/USDT на одной бирже
   (spot и swap одного инструмента). Перенос выводов на другие инструменты не обоснован.
5. **Только три агента из запланированных.** News и OnChain не развёрнуты. Отсутствие
   предсказательной способности у текущей тройки не означает её отсутствия у полного состава.
6. **Балл и согласованность восстановлены, а не прочитаны.** Отдельных колонок в БД нет;
   значения пересчитываются из `agents_payload` (точно, при весах 1.0) и сверяются с текстом
   `rationale` (округление до 2 знаков). Блок 3.1 показывает, насколько пересчёт совпадает
   с сохранённой вероятностью; при неполном совпадении расчёты 3 и 6 теряют точность,
   и это нужно отметить в выводе.
7. **Базовые линии считаются по 1m-свечам.** Таймфрейма 4h в `ohlcv` нет, поэтому «цена
   через 4 часа» — это close ближайшей 1m-свечи. Окна, где свеча отсутствует, исключены
   и подсчитаны отдельно (блок 1.1).
8. **Версия 2 непригодна как объект выводов.** Это период восьмичасового сбоя Market и
   деградированного режима; она приводится в Расчёте 6 только для сопоставления.
9. **Данные не отвечают на вопрос «почему».** Расчёты показывают, есть ли связь между
   мнением агента и движением цены, но не показывают, какие именно индикаторы внутри
   агента её создают или разрушают.

---

## 8. Итог: выбор пути

Формулируется **после** заполнения таблиц. Ровно один вариант из трёх, с обоснованием
ссылками на конкретные таблицы и размеры выборок.

| Путь | Когда выбирается | Что подтверждает выбор |
|---|---|---|
| **A.** Преимущество есть, но собрано неправильно | доля успеха системы не превосходит базовую линию (1.4), **но** у отдельных агентов направление предсказывает движение лучше базовой линии (2.4), либо доля успеха растёт с ростом \|балла\|/вероятности (3.2, 3.5) | переделка агрегатора и формулы вероятности |
| **B.** Преимущество есть у части агентов | таблица 2.4 показывает превосходство над базовой линией у одного-двух агентов при приемлемом N, у остальных — нет; корреляция (5.1) не объясняет это дублированием | починка конкретных агентов |
| **C.** Преимущества нет ни у кого | ни один агент в 2.4 не превосходит базовую линию, калибровка (3.5) плоская или обратная, инерция (4.1–4.2) показывает, что новых суждений почти не выносится | пересмотр индикаторов и/или подключение новых источников (News, OnChain) |

**Выбранный путь:** _(A / B / C)_

**Обоснование:**
_(3–6 предложений со ссылками на номера таблиц и парами X из N; отдельно отметить,
позволяет ли размер выборки вообще различить варианты, или требуется дополнительный
период наблюдения)_

---

## 9. Приложение: полные тексты использованных SQL-запросов

Ниже приведены целиком все семь файлов, которые исполняет `analysis/run_7_1.sh`.
Тексты совпадают с файлами `analysis/sql/*.sql` в этой же ветке.

### 9.1 `analysis/sql/00_schema.sql` — сверка схемы

```sql
-- ЭТАП 7.1, раздел 3 ТЗ: сверка фактической схемы БД с предположениями ТЗ.
-- Только чтение. Ничего не создаётся и не изменяется.
--
-- Задача блока: до всех расчётов зафиксировать, КАК на самом деле называются
-- таблицы и колонки, хранятся ли балл (score) и согласованность (agreement)
-- отдельными колонками, чем заполнен logic_version у старых записей и с какой
-- даты заполняется degraded. Всё, что расходится с ТЗ, попадает в отчёт явно.

\pset pager off
SET default_transaction_read_only = on;   -- страховка: сессия не может писать
SET statement_timeout = '600s';

\echo
\echo '--- 0.1 Список таблиц схемы public ---'
\dt

\echo
\echo '--- 0.2 Структура signals ---'
\d signals

\echo
\echo '--- 0.3 Структура agent_outputs ---'
\d agent_outputs

\echo
\echo '--- 0.4 Структура signal_evaluations ---'
\d signal_evaluations

\echo
\echo '--- 0.5 Структура ohlcv ---'
\d ohlcv

\echo
\echo '--- 0.6 Структура agent_failures ---'
\d agent_failures

\echo
\echo '--- 0.7 Структура instruments ---'
\d instruments

\echo
\echo '--- 0.8 Есть ли где-либо колонки score / agreement (балл и согласованность)? ---'
\echo '(ожидание по коду: НЕТ ни одной — значения восстанавливаются из agents_payload/rationale)'
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (column_name ILIKE '%score%'
       OR column_name ILIKE '%agree%'
       OR column_name ILIKE '%соглас%'
       OR column_name ILIKE '%ball%')
ORDER BY table_name, column_name;

\echo
\echo '--- 0.9 Колонки signals: тип, NOT NULL, значение по умолчанию ---'
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'signals'
ORDER BY ordinal_position;

\echo
\echo '--- 0.10 logic_version: сколько записей в каждой версии, есть ли NULL ---'
\echo '(ожидание по коду: колонка NOT NULL DEFAULT 1, NULL быть не может)'
SELECT COALESCE(logic_version::text, 'NULL') AS logic_version,
       count(*)                              AS signals_total,
       count(*) FILTER (WHERE status = 'closed')      AS closed,
       count(*) FILTER (WHERE decision <> 'wait')     AS directional,
       min(ts)                               AS ts_from,
       max(ts)                               AS ts_to
FROM signals
GROUP BY logic_version
ORDER BY logic_version NULLS FIRST;

\echo
\echo '--- 0.11 Границы версий по данным (сверить с ТЗ: v2 c 13.08 15:41 UTC, v3 c 14.08 13:39 UTC) ---'
SELECT logic_version,
       min(ts) AS first_signal_utc,
       max(ts) AS last_signal_utc,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.12 degraded: с какой даты встречается true, сколько записей ---'
SELECT degraded,
       count(*)  AS signals_total,
       min(ts)   AS first_ts_utc,
       max(ts)   AS last_ts_utc
FROM signals
GROUP BY degraded
ORDER BY degraded;

\echo
\echo '--- 0.13 degraded=true в разрезе версий логики ---'
SELECT logic_version,
       count(*)                                AS signals_total,
       count(*) FILTER (WHERE degraded)        AS degraded_true,
       round(100.0 * count(*) FILTER (WHERE degraded) / NULLIF(count(*), 0), 2) AS degraded_pct
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.14 Формат rationale: примеры по версиям (откуда разбираются балл и согласованность) ---'
SELECT logic_version, decision, probability, left(rationale, 160) AS rationale_head
FROM (
    SELECT DISTINCT ON (logic_version, decision)
           logic_version, decision, probability, rationale
    FROM signals
    ORDER BY logic_version, decision, ts DESC
) t
ORDER BY logic_version, decision;

\echo
\echo '--- 0.15 Разбирается ли rationale регулярным выражением (доля непустых совпадений) ---'
SELECT logic_version,
       count(*)                                                             AS signals_total,
       count(*) FILTER (WHERE rationale ~ 'балл=')                          AS has_score_text,
       count(*) FILTER (WHERE rationale ~ 'согласованность=')               AS has_agreement_text,
       count(*) FILTER (WHERE agents_payload IS NOT NULL)                   AS has_payload,
       count(*) FILTER (WHERE jsonb_typeof(agents_payload) = 'array')       AS payload_is_array
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 0.16 Состав agents_payload: какие ключи лежат внутри (первая непустая запись каждой версии) ---'
SELECT logic_version, jsonb_pretty(agents_payload) AS agents_payload
FROM (
    SELECT DISTINCT ON (logic_version) logic_version, agents_payload
    FROM signals
    WHERE jsonb_typeof(agents_payload) = 'array'
      AND jsonb_array_length(agents_payload) > 0
    ORDER BY logic_version, ts DESC
) t
ORDER BY logic_version;

\echo
\echo '--- 0.17 signal_evaluations: горизонты и объём ---'
SELECT horizon,
       count(*)                                  AS evaluations,
       count(*) FILTER (WHERE success)           AS success_true,
       min(evaluated_at)                         AS first_eval,
       max(evaluated_at)                         AS last_eval
FROM signal_evaluations
GROUP BY horizon
ORDER BY horizon;

\echo
\echo '--- 0.18 ohlcv: какие таймфреймы реально собраны (ТЗ ожидает 4h — по коду его НЕТ) ---'
SELECT o.instrument_id,
       i.exchange, i.symbol, i.type,
       o.timeframe,
       count(*)  AS candles,
       min(o.ts) AS ts_from,
       max(o.ts) AS ts_to
FROM ohlcv o
JOIN instruments i ON i.id = o.instrument_id
GROUP BY o.instrument_id, i.exchange, i.symbol, i.type, o.timeframe
ORDER BY o.instrument_id, o.timeframe;

\echo
\echo '--- 0.19 Инструменты и привязка сигналов/выводов агентов к ним ---'
SELECT i.id, i.exchange, i.symbol, i.type,
       (SELECT count(*) FROM signals s       WHERE s.instrument_id = i.id) AS signals,
       (SELECT count(*) FROM agent_outputs a WHERE a.instrument_id = i.id) AS agent_outputs
FROM instruments i
ORDER BY i.id;

\echo
\echo '--- 0.20 agent_outputs: состав по агентам и допустимые значения signal ---'
SELECT agent, signal, count(*) AS rows, min(ts) AS ts_from, max(ts) AS ts_to
FROM agent_outputs
GROUP BY agent, signal
ORDER BY agent, signal;

\echo
\echo '--- 0.21 agent_failures: есть ли таблица и что в ней (наблюдаемость Этапа 7.0/7.2) ---'
SELECT agent, error_type, count(*) AS rows, min(ts) AS ts_from, max(ts) AS ts_to
FROM agent_failures
GROUP BY agent, error_type
ORDER BY agent, error_type;

\echo
\echo '--- 0.22 Роль подключения и режим сессии (контроль: только чтение) ---'
SELECT current_user            AS connected_as,
       current_database()      AS database,
       current_setting('default_transaction_read_only') AS read_only,
       now()                   AS server_time_utc;
```

### 9.2 `analysis/sql/01_baseline.sql` — Расчёт 1

```sql
-- ЭТАП 7.1, РАСЧЁТ 1 (раздел 5 ТЗ): базовая линия и общая результативность.
-- Только чтение. Ни временных таблиц, ни представлений не создаётся: сессия
-- переведена в режим read-only, каждый запрос самодостаточен (общие определения
-- повторяются в CTE, чтобы запрос можно было скопировать в отчёт целиком).
--
-- Определения (раздел 4 ТЗ):
--   * независимое окно — непересекающийся 4-часовой отрезок с границами
--     00/04/08/12/16/20 UTC (epoch кратен 14400 → границы совпадают ровно);
--     из окна берётся ПЕРВЫЙ по времени закрытый сигнал;
--   * успех — pnl_pct > 0 по горизонту 4h (знак уже учитывает направление);
--   * область данных — logic_version = 1; решения wait исключены.
--
-- ВАЖНО по схеме: в ohlcv нет таймфрейма 4h (собираются 1m,5m,15m,1h), поэтому
-- базовые линии считаются по close 1m-свечей — ровно так же, как берёт цену сам
-- оценщик (src/core/db.py: get_price_at, timeframe='1m'). Свеча ищется на/до
-- нужного момента и не старше 10 минут; иначе окно попадает в графу «нет цены».

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 1.1 Размер независимой выборки (logic_version = 1) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, s.probability,
           e.pnl_pct, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*, ps.close AS p_open, pe.close AS p_close
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT count(*)                                                   AS independent_windows,
       min(win)                                                   AS first_window_utc,
       max(win)                                                   AS last_window_utc,
       count(*) FILTER (WHERE p_open IS NULL OR p_close IS NULL)  AS windows_without_price,
       (SELECT count(*) FROM v1_eval)                             AS full_sample_closed_signals
FROM px;

\echo
\echo '--- 1.2 Доля успеха по независимой выборке: числитель и знаменатель (X из N) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.decision, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
)
SELECT 'ВСЕГО' AS bucket,
       count(*) FILTER (WHERE success) AS success_x,
       count(*)                        AS total_n,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM v1_indep
UNION ALL
SELECT decision,
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM v1_indep
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.3 pnl_4h по независимой выборке: среднее и медиана (проценты) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.decision, e.pnl_pct,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
)
SELECT 'ВСЕГО' AS bucket,
       count(*)                                                                AS n,
       round(avg(pnl_pct)::numeric, 4)                                         AS avg_pnl_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4) AS median_pnl_pct,
       round(min(pnl_pct)::numeric, 4)                                         AS min_pnl_pct,
       round(max(pnl_pct)::numeric, 4)                                         AS max_pnl_pct
FROM v1_indep
UNION ALL
SELECT decision,
       count(*),
       round(avg(pnl_pct)::numeric, 4),
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4),
       round(min(pnl_pct)::numeric, 4),
       round(max(pnl_pct)::numeric, 4)
FROM v1_indep
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.4 Три базовые линии на ТЕХ ЖЕ независимых окнах (X из N) ---'
\echo '(«всегда buy»/«всегда sell» — по ohlcv 1m: close начала окна против close через 4 часа)'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT 'всегда buy (цена выросла)' AS strategy,
       count(*) FILTER (WHERE move_pct > 0)          AS success_x,
       count(*) FILTER (WHERE move_pct IS NOT NULL)  AS total_n,
       round(100.0 * count(*) FILTER (WHERE move_pct > 0)
             / NULLIF(count(*) FILTER (WHERE move_pct IS NOT NULL), 0), 2) AS success_pct
FROM px
UNION ALL
SELECT 'всегда sell (цена упала)',
       count(*) FILTER (WHERE move_pct < 0),
       count(*) FILTER (WHERE move_pct IS NOT NULL),
       round(100.0 * count(*) FILTER (WHERE move_pct < 0)
             / NULLIF(count(*) FILTER (WHERE move_pct IS NOT NULL), 0), 2)
FROM px
UNION ALL
SELECT 'фактический результат системы',
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM px;

\echo
\echo '--- 1.5 Движение рынка в независимых окнах (справочно к базовым линиям) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT count(*)                                                                 AS windows_with_price,
       round(avg(move_pct)::numeric, 4)                                         AS avg_move_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY move_pct)::numeric, 4) AS median_move_pct,
       count(*) FILTER (WHERE move_pct > 0)                                     AS up_windows,
       count(*) FILTER (WHERE move_pct < 0)                                     AS down_windows,
       count(*) FILTER (WHERE move_pct = 0)                                     AS flat_windows
FROM px
WHERE move_pct IS NOT NULL;

\echo
\echo '--- 1.6 Независимые окна поштучно (проверяемость выборки) ---'
WITH v1_eval AS (
    SELECT s.id, s.ts, s.instrument_id, s.decision, s.probability, e.pnl_pct, e.success,
           to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
), v1_indep AS (
    SELECT DISTINCT ON (win) * FROM v1_eval ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
)
SELECT win AS window_utc,
       id  AS signal_id,
       ts  AS signal_ts_utc,
       decision,
       round(probability::numeric, 4) AS probability,
       round(pnl_pct::numeric, 4)     AS pnl_4h_pct,
       success,
       round(move_pct::numeric, 4)    AS market_move_pct
FROM px
ORDER BY win;

\echo
\echo '=== ПОЛНАЯ ВЫБОРКА (logic_version = 1): наблюдения ЗАВИСИМЫ, доверительные интервалы неприменимы ==='

\echo
\echo '--- 1.7 Доля успеха по ПОЛНОЙ выборке закрытых сигналов версии 1 (X из N) ---'
WITH v1_eval AS (
    SELECT s.decision, e.success
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
)
SELECT 'ВСЕГО' AS bucket,
       count(*) FILTER (WHERE success) AS success_x,
       count(*)                        AS total_n,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM v1_eval
UNION ALL
SELECT decision,
       count(*) FILTER (WHERE success),
       count(*),
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2)
FROM v1_eval
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.8 pnl_4h по ПОЛНОЙ выборке версии 1 (наблюдения зависимы) ---'
WITH v1_eval AS (
    SELECT s.decision, e.pnl_pct
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait'
)
SELECT 'ВСЕГО' AS bucket,
       count(*)                                                                AS n,
       round(avg(pnl_pct)::numeric, 4)                                         AS avg_pnl_pct,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4) AS median_pnl_pct
FROM v1_eval
UNION ALL
SELECT decision,
       count(*),
       round(avg(pnl_pct)::numeric, 4),
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl_pct)::numeric, 4)
FROM v1_eval
GROUP BY decision
ORDER BY 1;

\echo
\echo '--- 1.9 Полная выборка версии 1 по суткам (динамика; наблюдения зависимы) ---'
SELECT date_trunc('day', s.ts)::date        AS day_utc,
       count(*)                             AS n,
       count(*) FILTER (WHERE e.success)    AS success_x,
       round(100.0 * count(*) FILTER (WHERE e.success) / NULLIF(count(*), 0), 2) AS success_pct,
       round(avg(e.pnl_pct)::numeric, 4)    AS avg_pnl_pct
FROM signals s
JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
WHERE s.logic_version = 1 AND s.decision <> 'wait'
GROUP BY 1
ORDER BY 1;
```

### 9.3 `analysis/sql/02_agents.sql` — Расчёт 2

```sql
-- ЭТАП 7.1, РАСЧЁТ 2 (раздел 6 ТЗ): вклад каждого агента по отдельности.
-- Только чтение. Область данных — logic_version = 1, независимые 4-часовые окна.
--
-- Источник мнения агента — signals.agents_payload (JSONB): в нём лежит РОВНО тот
-- набор выводов, который видел Decision Agent в момент решения (agent, signal,
-- confidence, ts). Это точнее, чем присоединять agent_outputs по времени.
-- Если агента в payload нет — он не участвовал в решении (устарел, отсутствовал
-- или выдал insufficient_data); такие случаи считаются отдельной графой.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 2.1 Распределение уверенности по агентам, НЕЗАВИСИМЫЕ ОКНА версии 1 ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.confidence
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT (el->>'confidence')::double precision AS confidence
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*)                                    AS windows_n,
       count(confidence)                           AS present_n,
       count(*) - count(confidence)                AS absent_n,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p25,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS median,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p75,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p99,
       count(*) FILTER (WHERE confidence = 0)      AS conf_eq_0,
       count(*) FILTER (WHERE confidence < 0.01)   AS conf_lt_001,
       count(*) FILTER (WHERE confidence = 1.0)    AS conf_eq_1
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.2 То же по ВСЕМ выводам agent_outputs за период версии 1 (наблюдения зависимы) ---'
WITH bounds AS (
    SELECT COALESCE(
               (SELECT min(ts) FROM signals WHERE logic_version = 2),
               (SELECT min(ts) FROM signals WHERE logic_version = 3),
               'infinity'::timestamptz) AS v1_end
), src AS (
    SELECT a.agent, a.confidence
    FROM agent_outputs a, bounds b
    WHERE a.ts < b.v1_end
)
SELECT agent,
       count(*)                                    AS outputs_n,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p25,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS median,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p75,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY confidence)::numeric, 4) AS p99,
       count(*) FILTER (WHERE confidence = 0)      AS conf_eq_0,
       round(100.0 * count(*) FILTER (WHERE confidence = 0)    / NULLIF(count(*), 0), 2) AS conf_eq_0_pct,
       count(*) FILTER (WHERE confidence < 0.01)   AS conf_lt_001,
       round(100.0 * count(*) FILTER (WHERE confidence < 0.01) / NULLIF(count(*), 0), 2) AS conf_lt_001_pct,
       count(*) FILTER (WHERE confidence = 1.0)    AS conf_eq_1,
       round(100.0 * count(*) FILTER (WHERE confidence = 1.0)  / NULLIF(count(*), 0), 2) AS conf_eq_1_pct
FROM src
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.3 Распределение направлений агента по независимым окнам версии 1 (X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.signal
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*)                                                AS windows_n,
       count(*) FILTER (WHERE signal = 'bullish')              AS bullish_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'bullish') / NULLIF(count(*), 0), 2) AS bullish_pct,
       count(*) FILTER (WHERE signal = 'bearish')              AS bearish_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'bearish') / NULLIF(count(*), 0), 2) AS bearish_pct,
       count(*) FILTER (WHERE signal = 'neutral')              AS neutral_n,
       round(100.0 * count(*) FILTER (WHERE signal = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE signal IS NULL)                  AS absent_n
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.4 Связь направления агента с ФАКТИЧЕСКИМ движением цены (X из N) ---'
\echo '(bullish → доля окон с ростом цены; bearish → доля окон с падением; сравнивать с базовой линией из Расчёта 1)'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.instrument_id, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), px AS (
    SELECT i.*,
           CASE WHEN ps.close IS NOT NULL AND pe.close IS NOT NULL AND ps.close > 0
                THEN (pe.close - ps.close) / ps.close * 100.0 END AS move_pct
    FROM v1_indep i
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win AND o.ts > i.win - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) ps ON TRUE
    LEFT JOIN LATERAL (
        SELECT o.close FROM ohlcv o
        WHERE o.instrument_id = i.instrument_id AND o.timeframe = '1m'
          AND o.ts <= i.win + interval '4 hours'
          AND o.ts >  i.win + interval '4 hours' - interval '10 minutes'
        ORDER BY o.ts DESC LIMIT 1) pe ON TRUE
), aw AS (
    SELECT g.agent, p.signal, i.move_pct
    FROM px i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
    WHERE i.move_pct IS NOT NULL
)
SELECT agent,
       COALESCE(signal, 'ОТСУТСТВУЕТ') AS agent_signal,
       count(*)                        AS n,
       count(*) FILTER (WHERE move_pct > 0) AS price_up_x,
       round(100.0 * count(*) FILTER (WHERE move_pct > 0) / NULLIF(count(*), 0), 2) AS price_up_pct,
       count(*) FILTER (WHERE move_pct < 0) AS price_down_x,
       round(100.0 * count(*) FILTER (WHERE move_pct < 0) / NULLIF(count(*), 0), 2) AS price_down_pct,
       round(avg(move_pct)::numeric, 4)     AS avg_move_pct,
       CASE COALESCE(signal, '-')
            WHEN 'bullish' THEN round(100.0 * count(*) FILTER (WHERE move_pct > 0) / NULLIF(count(*), 0), 2)
            WHEN 'bearish' THEN round(100.0 * count(*) FILTER (WHERE move_pct < 0) / NULLIF(count(*), 0), 2)
       END AS hit_rate_pct
FROM aw
GROUP BY agent, signal
ORDER BY agent, signal NULLS LAST;

\echo
\echo '--- 2.5 Связь уверенности агента с исходом сигнала: две группы по медиане уверенности (X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.confidence, i.success
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT (el->>'confidence')::double precision AS confidence
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
    WHERE p.confidence IS NOT NULL
), med AS (
    SELECT agent, percentile_cont(0.5) WITHIN GROUP (ORDER BY confidence) AS med_conf
    FROM aw GROUP BY agent
)
SELECT a.agent,
       round(m.med_conf::numeric, 4) AS median_confidence,
       CASE WHEN a.confidence >= m.med_conf THEN 'уверенность >= медианы'
            ELSE 'уверенность < медианы' END AS conf_group,
       count(*)                              AS n,
       count(*) FILTER (WHERE a.success)     AS success_x,
       round(100.0 * count(*) FILTER (WHERE a.success) / NULLIF(count(*), 0), 2) AS success_pct
FROM aw a
JOIN med m ON m.agent = a.agent
GROUP BY a.agent, m.med_conf, 3
ORDER BY a.agent, 3;

\echo
\echo '--- 2.6 Согласие агента с итоговым решением (X из N независимых окон) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.decision, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), aw AS (
    SELECT g.agent, p.signal, i.decision
    FROM v1_indep i
    CROSS JOIN (VALUES ('market'), ('liquidity'), ('futures')) AS g(agent)
    LEFT JOIN LATERAL (
        SELECT el->>'signal' AS signal
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
        WHERE el->>'agent' = g.agent
        LIMIT 1) p ON TRUE
)
SELECT agent,
       count(*) AS windows_n,
       count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'buy')
                           OR (signal = 'bearish' AND decision = 'sell')) AS agrees_x,
       round(100.0 * count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'buy')
                                         OR (signal = 'bearish' AND decision = 'sell'))
             / NULLIF(count(*), 0), 2) AS agrees_pct,
       count(*) FILTER (WHERE (signal = 'bullish' AND decision = 'sell')
                           OR (signal = 'bearish' AND decision = 'buy')) AS opposes_x,
       count(*) FILTER (WHERE signal = 'neutral')  AS neutral_x,
       count(*) FILTER (WHERE signal IS NULL)      AS absent_x
FROM aw
GROUP BY agent
ORDER BY agent;

\echo
\echo '--- 2.7 Пропуски Market: число циклов решения БЕЗ market в payload, по суткам (весь период) ---'
\echo '(ожидание ТЗ: после 14.08 13:39 UTC пропуски прекратились)'
SELECT date_trunc('day', s.ts)::date AS day_utc,
       s.logic_version,
       count(*) AS cycles,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "market"}]'::jsonb)) AS market_absent,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "liquidity"}]'::jsonb)) AS liquidity_absent,
       count(*) FILTER (WHERE NOT (
           CASE WHEN jsonb_typeof(s.agents_payload) = 'array' THEN s.agents_payload ELSE '[]'::jsonb END
           @> '[{"agent": "futures"}]'::jsonb)) AS futures_absent
FROM signals s
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 2.8 Число выводов agent_outputs по агентам и суткам (косвенный контроль пропусков) ---'
SELECT date_trunc('day', ts)::date AS day_utc,
       count(*) FILTER (WHERE agent = 'market')    AS market_rows,
       count(*) FILTER (WHERE agent = 'liquidity') AS liquidity_rows,
       count(*) FILTER (WHERE agent = 'futures')   AS futures_rows,
       count(*) FILTER (WHERE signal = 'insufficient_data') AS insufficient_data_rows
FROM agent_outputs
GROUP BY 1
ORDER BY 1;

\echo
\echo '--- 2.9 Сбои агентов по суткам (agent_failures) ---'
SELECT date_trunc('day', ts)::date AS day_utc,
       agent,
       error_type,
       count(*) AS failures
FROM agent_failures
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

### 9.4 `analysis/sql/03_formula.sql` — Расчёт 3

```sql
-- ЭТАП 7.1, РАСЧЁТ 3 (раздел 7 ТЗ): информативность балла и согласованности.
-- Только чтение.
--
-- Формула по коду (src/decision/agent.py, строки 101–128):
--     score       = Σ(direction · confidence · weight) / Σ(weight · confidence)   (стр. 104–110)
--     agreement   = |pos − neg| / total_agents                                    (стр. 127)
--     probability = round(min(|score| · (0.5 + 0.5 · agreement), 1.0), 4)          (стр. 128)
-- где direction = +1 bullish / −1 bearish / 0 neutral, total_agents = 3 (Этап 7.2).
-- ДО Этапа 7.2 знаменателем согласованности было число СВЕЖИХ агентов (len(fresh)).
--
-- Отдельных колонок score/agreement в схеме НЕТ. Здесь они восстанавливаются
-- двумя независимыми путями:
--   (а) пересчётом из signals.agents_payload по формуле кода — точное значение
--       при условии, что веса агентов равны 1.0 (значения по умолчанию
--       WEIGHT_MARKET/LIQUIDITY/FUTURES в src/core/config.py);
--   (б) разбором текста signals.rationale регулярным выражением — значение
--       округлено кодом до 2 знаков, поэтому годится только для сверки.
-- Блок 3.1 сравнивает пересчитанную вероятность с сохранённой: совпадение
-- подтверждает и формулу, и предположение о весах = 1.0.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 3.1 Проверка формулы на данных: пересчёт из agents_payload против сохранённой probability ---'
\echo '(agreement_fresh — знаменатель = число свежих агентов (логика до 7.2); agreement_total3 — знаменатель = 3 (логика с 7.2))'
WITH calc AS (
    SELECT s.id, s.logic_version, s.decision, s.probability, s.degraded,
           c.num, c.den, c.pos, c.neg, c.n_fresh
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT
            sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                * (el->>'confidence')::double precision)          AS num,
            sum((el->>'confidence')::double precision)             AS den,
            count(*) FILTER (WHERE el->>'signal' = 'bullish')      AS pos,
            count(*) FILTER (WHERE el->>'signal' = 'bearish')      AS neg,
            count(*)                                               AS n_fresh
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE s.decision <> 'wait'
), f AS (
    SELECT logic_version,
           probability,
           abs(num / NULLIF(den, 0))                                       AS abs_score,
           abs(pos - neg)::double precision / NULLIF(n_fresh, 0)           AS agr_fresh,
           abs(pos - neg)::double precision / 3.0                          AS agr_total3
    FROM calc
)
SELECT logic_version,
       count(*) AS signals_n,
       count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_fresh), 1.0))::numeric, 4)) <= 0.001)  AS match_agr_fresh,
       round(100.0 * count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_fresh), 1.0))::numeric, 4)) <= 0.001)
             / NULLIF(count(*), 0), 2) AS match_agr_fresh_pct,
       count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_total3), 1.0))::numeric, 4)) <= 0.001) AS match_agr_total3,
       round(100.0 * count(*) FILTER (WHERE abs(probability
             - round((least(abs_score * (0.5 + 0.5 * agr_total3), 1.0))::numeric, 4)) <= 0.001)
             / NULLIF(count(*), 0), 2) AS match_agr_total3_pct
FROM f
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 3.2 Сверка пересчёта с текстом rationale (регулярное выражение, 2 знака) ---'
WITH src AS (
    SELECT s.id, s.logic_version, s.probability, s.rationale,
           (regexp_match(s.rationale, 'балл=[[:space:]]*([+-]?[0-9]+[.,]?[0-9]*)'))[1]              AS score_txt,
           (regexp_match(s.rationale, 'согласованность=[[:space:]]*([0-9]+[.,]?[0-9]*)'))[1]        AS agr_txt
    FROM signals s
    WHERE s.decision <> 'wait'
)
SELECT logic_version,
       count(*)                                        AS signals_n,
       count(score_txt)                                AS score_parsed,
       count(agr_txt)                                  AS agreement_parsed,
       round(100.0 * count(score_txt) / NULLIF(count(*), 0), 2) AS score_parsed_pct,
       round(100.0 * count(agr_txt)   / NULLIF(count(*), 0), 2) AS agreement_parsed_pct
FROM src
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 3.3 Доля успеха по КВАРТИЛЯМ |балла| (независимые окна версии 1, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), calc AS (
    SELECT i.id, i.success, i.probability,
           abs(c.num / NULLIF(c.den, 0))                             AS abs_score,
           abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0)     AS agreement
    FROM v1_indep i
    CROSS JOIN LATERAL (
        SELECT sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                   * (el->>'confidence')::double precision)     AS num,
               sum((el->>'confidence')::double precision)        AS den,
               count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
    ) c
), q AS (
    SELECT *, ntile(4) OVER (ORDER BY abs_score) AS quartile FROM calc WHERE abs_score IS NOT NULL
)
SELECT quartile,
       count(*)                          AS n,
       round(min(abs_score)::numeric, 4) AS abs_score_min,
       round(max(abs_score)::numeric, 4) AS abs_score_max,
       count(*) FILTER (WHERE success)   AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM q
GROUP BY quartile
ORDER BY quartile;

\echo
\echo '--- 3.4 Доля успеха по КАЖДОМУ дискретному значению согласованности (независимые окна версии 1, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), calc AS (
    SELECT i.success,
           round((abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0))::numeric, 2) AS agreement,
           c.n AS agents_in_payload
    FROM v1_indep i
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                      THEN i.agents_payload ELSE '[]'::jsonb END) el
    ) c
)
SELECT COALESCE(agreement::text, 'нет данных') AS agreement_value,
       agents_in_payload,
       count(*)                        AS n,
       count(*) FILTER (WHERE success) AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM calc
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 3.5 Доля успеха по КВАРТИЛЯМ probability (независимые окна версии 1, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), q AS (
    SELECT *, ntile(4) OVER (ORDER BY probability) AS quartile
    FROM v1_indep WHERE probability IS NOT NULL
)
SELECT quartile,
       count(*)                            AS n,
       round(min(probability)::numeric, 4) AS prob_min,
       round(max(probability)::numeric, 4) AS prob_max,
       count(*) FILTER (WHERE success)     AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM q
GROUP BY quartile
ORDER BY quartile;

\echo
\echo '--- 3.6 КАЛИБРОВОЧНАЯ ТАБЛИЦА: заявленная вероятность против фактической доли успеха (независимые окна версии 1) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.probability, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), b AS (
    SELECT *, least(width_bucket(probability, 0, 1, 5), 5) AS bucket
    FROM v1_indep WHERE probability IS NOT NULL
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END                                       AS probability_range,
       count(*)                                  AS n,
       round(avg(probability)::numeric, 4)       AS claimed_probability_avg,
       count(*) FILTER (WHERE success)           AS success_x,
       round((count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS actual_success_rate,
       round(avg(probability)::numeric - (count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS gap_claimed_minus_actual
FROM b
GROUP BY bucket
ORDER BY bucket;

\echo
\echo '--- 3.7 Та же калибровка по ПОЛНОЙ выборке версии 1 (наблюдения ЗАВИСИМЫ, доверительные интервалы неприменимы) ---'
WITH b AS (
    SELECT s.probability, e.success, least(width_bucket(s.probability, 0, 1, 5), 5) AS bucket
    FROM signals s
    JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
    WHERE s.logic_version = 1 AND s.decision <> 'wait' AND s.probability IS NOT NULL
)
SELECT CASE bucket
            WHEN 1 THEN '0.0 – 0.2'
            WHEN 2 THEN '0.2 – 0.4'
            WHEN 3 THEN '0.4 – 0.6'
            WHEN 4 THEN '0.6 – 0.8'
            WHEN 5 THEN '0.8 – 1.0'
       END                                 AS probability_range,
       count(*)                            AS n,
       round(avg(probability)::numeric, 4) AS claimed_probability_avg,
       count(*) FILTER (WHERE success)     AS success_x,
       round((count(*) FILTER (WHERE success))::numeric / NULLIF(count(*), 0), 4) AS actual_success_rate
FROM b
GROUP BY bucket
ORDER BY bucket;
```

### 9.5 `analysis/sql/04_inertia.sql` — Расчёт 4

```sql
-- ЭТАП 7.1, РАСЧЁТ 4 (раздел 8 ТЗ): фактическая частота обновления входных данных.
-- Только чтение.
--
-- В agent_outputs НЕТ колонки logic_version, поэтому версия определяется по
-- времени: границы берутся из самой таблицы signals (min(ts) версий 2 и 3), а не
-- вписаны константами. Так расчёт остаётся верным, даже если фактические
-- границы отличаются от указанных в ТЗ (13.08 15:41 / 14.08 13:39 UTC).
--
-- Серии одинаковых значений выделяются приёмом «острова и промежутки»:
-- разность двух нумераций (по времени и по времени внутри значения) постоянна
-- ровно на непрерывном участке одинакового confidence.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 4.0 Границы версий, применённые к agent_outputs ---'
SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                (SELECT min(ts) FROM signals WHERE logic_version = 3)) AS v2_start_utc,
       (SELECT min(ts) FROM signals WHERE logic_version = 3)           AS v3_start_utc,
       (SELECT min(ts) FROM agent_outputs)                             AS agent_outputs_from,
       (SELECT max(ts) FROM agent_outputs)                             AS agent_outputs_to;

\echo
\echo '--- 4.1 Серии подряд идущих циклов с ОДИНАКОВЫМ confidence (по версиям 1 и 3) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v3_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                ELSE 3 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), seq AS (
    SELECT agent, ver, confidence,
           row_number() OVER (PARTITION BY agent, ver ORDER BY ts)
         - row_number() OVER (PARTITION BY agent, ver, confidence ORDER BY ts) AS grp
    FROM ao
), runs AS (
    SELECT agent, ver, confidence, grp, count(*) AS run_len
    FROM seq GROUP BY agent, ver, confidence, grp
)
SELECT agent,
       ver                                  AS logic_version,
       count(*)                             AS runs_n,
       sum(run_len)                         AS cycles_n,
       round(avg(run_len)::numeric, 2)      AS avg_run_len,
       max(run_len)                         AS max_run_len,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY run_len)::numeric, 2) AS median_run_len
FROM runs
WHERE ver IN (1, 3)
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 4.2 Доля циклов, где confidence НЕ изменился относительно предыдущего (по версиям 1 и 3) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v3_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence, a.signal,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                ELSE 3 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), lagged AS (
    SELECT agent, ver, confidence, signal,
           lag(confidence) OVER (PARTITION BY agent, ver ORDER BY ts) AS prev_conf,
           lag(signal)     OVER (PARTITION BY agent, ver ORDER BY ts) AS prev_signal
    FROM ao
)
SELECT agent,
       ver                                                              AS logic_version,
       count(*) FILTER (WHERE prev_conf IS NOT NULL)                    AS comparable_cycles,
       count(*) FILTER (WHERE prev_conf IS NOT NULL AND confidence = prev_conf) AS unchanged_conf_x,
       round(100.0 * count(*) FILTER (WHERE prev_conf IS NOT NULL AND confidence = prev_conf)
             / NULLIF(count(*) FILTER (WHERE prev_conf IS NOT NULL), 0), 2) AS unchanged_conf_pct,
       count(*) FILTER (WHERE prev_signal IS NOT NULL AND signal = prev_signal) AS unchanged_signal_x,
       round(100.0 * count(*) FILTER (WHERE prev_signal IS NOT NULL AND signal = prev_signal)
             / NULLIF(count(*) FILTER (WHERE prev_signal IS NOT NULL), 0), 2) AS unchanged_signal_pct
FROM lagged
WHERE ver IN (1, 3)
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 4.3 Число уникальных значений confidence за сутки (по агентам, все версии помечены) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v3_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                ELSE 3 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
)
SELECT date_trunc('day', ts)::date AS day_utc,
       agent,
       min(ver)                    AS ver_min,
       max(ver)                    AS ver_max,
       count(*)                    AS outputs_n,
       count(DISTINCT confidence)  AS distinct_confidence,
       round(100.0 * count(DISTINCT confidence) / NULLIF(count(*), 0), 3) AS distinct_pct
FROM ao
GROUP BY 1, 2
ORDER BY 1, 2;

\echo
\echo '--- 4.4 Самые длинные серии повторов поштучно (топ-15 по каждой версии) ---'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v3_start
), ao AS (
    SELECT a.agent, a.ts, a.confidence,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                ELSE 3 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
), seq AS (
    SELECT agent, ver, ts, confidence,
           row_number() OVER (PARTITION BY agent, ver ORDER BY ts)
         - row_number() OVER (PARTITION BY agent, ver, confidence ORDER BY ts) AS grp
    FROM ao
), runs AS (
    SELECT agent, ver, confidence, count(*) AS run_len, min(ts) AS ts_from, max(ts) AS ts_to
    FROM seq GROUP BY agent, ver, confidence, grp
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY ver ORDER BY run_len DESC) AS rn
    FROM runs WHERE ver IN (1, 3)
)
SELECT ver AS logic_version, agent, round(confidence::numeric, 4) AS confidence,
       run_len, ts_from, ts_to,
       round((extract(epoch FROM (ts_to - ts_from)) / 3600.0)::numeric, 2) AS hours_span
FROM ranked
WHERE rn <= 15
ORDER BY ver, run_len DESC;
```

### 9.6 `analysis/sql/05_correlation.sql` — Расчёт 5

```sql
-- ЭТАП 7.1, РАСЧЁТ 5 (раздел 9 ТЗ): попарная согласованность агентов.
-- Только чтение. Независимые 4-часовые окна, logic_version = 1.
--
-- Направления берутся из signals.agents_payload — то есть ровно те мнения,
-- которые участвовали в решении. Окна, где одного из пары нет в payload,
-- считаются отдельной графой и в знаменатель доли совпадений НЕ входят.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 5.1 Попарное совпадение направлений агентов (независимые окна версии 1, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       count(*)                                                  AS windows_total,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL)    AS both_present_n,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a = b) AS same_direction_x,
       round(100.0 * count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a = b)
             / NULLIF(count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL), 0), 2) AS same_direction_pct,
       count(*) FILTER (WHERE a IS NOT NULL AND b IS NOT NULL AND a <> b)             AS differ_x,
       count(*) FILTER (WHERE a IS NULL OR b IS NULL)                                 AS one_absent_x
FROM pairs
GROUP BY pair
ORDER BY pair;

\echo
\echo '--- 5.2 Совпадение БЕЗ учёта нейтральных мнений (только bullish/bearish, X из N) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish')) AS both_directional_n,
       count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish') AND a = b) AS same_direction_x,
       round(100.0 * count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish') AND a = b)
             / NULLIF(count(*) FILTER (WHERE a IN ('bullish','bearish') AND b IN ('bullish','bearish')), 0), 2) AS same_direction_pct
FROM pairs
GROUP BY pair
ORDER BY pair;

\echo
\echo '--- 5.3 Совместное распределение направлений по парам (какими именно сочетаниями набрана доля) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.agents_payload,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id
), pairs AS (
    SELECT 'market / liquidity' AS pair, m AS a, l AS b FROM piv
    UNION ALL
    SELECT 'market / futures',          m,      f      FROM piv
    UNION ALL
    SELECT 'liquidity / futures',       l,      f      FROM piv
)
SELECT pair,
       COALESCE(a, 'ОТСУТСТВУЕТ') AS agent_1,
       COALESCE(b, 'ОТСУТСТВУЕТ') AS agent_2,
       count(*)                   AS windows_n
FROM pairs
GROUP BY pair, a, b
ORDER BY pair, windows_n DESC;

\echo
\echo '--- 5.4 Полный состав мнений по окнам (тройка направлений и её частота) ---'
WITH v1_indep AS (
    SELECT DISTINCT ON (win) *
    FROM (
        SELECT s.id, s.ts, s.decision, s.agents_payload, e.success,
               to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400) AS win
        FROM signals s
        JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon = '4h'
        WHERE s.logic_version = 1 AND s.decision <> 'wait'
    ) q ORDER BY win, ts ASC
), piv AS (
    SELECT i.id, i.decision, i.success,
           max(CASE WHEN el->>'agent' = 'market'    THEN el->>'signal' END) AS m,
           max(CASE WHEN el->>'agent' = 'liquidity' THEN el->>'signal' END) AS l,
           max(CASE WHEN el->>'agent' = 'futures'   THEN el->>'signal' END) AS f
    FROM v1_indep i
    LEFT JOIN LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(i.agents_payload) = 'array'
                  THEN i.agents_payload ELSE '[]'::jsonb END) el ON TRUE
    GROUP BY i.id, i.decision, i.success
)
SELECT COALESCE(m, '—') AS market,
       COALESCE(l, '—') AS liquidity,
       COALESCE(f, '—') AS futures,
       decision,
       count(*)                        AS windows_n,
       count(*) FILTER (WHERE success) AS success_x,
       round(100.0 * count(*) FILTER (WHERE success) / NULLIF(count(*), 0), 2) AS success_pct
FROM piv
GROUP BY 1, 2, 3, 4
ORDER BY windows_n DESC, 1, 2, 3;
```

### 9.7 `analysis/sql/06_silence.sql` — Расчёт 6

```sql
-- ЭТАП 7.1, РАСЧЁТ 6 (раздел 10 ТЗ): почему система замолчала.
-- Только чтение. Выполняется по КАЖДОЙ версии логики отдельно; версии сведены
-- в три колонки одной таблицы, но нигде не смешиваются в одном показателе.
--
-- Из версии 3 везде исключены записи с degraded = true (кроме блока 6.8, где
-- они и подсчитываются). Балл (|score|) и согласованность восстанавливаются
-- пересчётом из signals.agents_payload по формуле кода (см. 03_formula.sql).
-- Этот расчёт НИЧЕГО не предлагает: он только измеряет.

\pset pager off
SET default_transaction_read_only = on;
SET statement_timeout = '600s';

\echo
\echo '--- 6.0 Объём данных по версиям (после исключения degraded из версии 3) ---'
SELECT logic_version,
       count(*)                                    AS decisions_total,
       count(*) FILTER (WHERE decision <> 'wait')  AS directional,
       min(ts)                                     AS ts_from,
       max(ts)                                     AS ts_to,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span
FROM signals
WHERE NOT (logic_version = 3 AND degraded)
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.1 Распределение probability по версиям (ВСЕ решения, включая wait) ---'
WITH base AS (
    SELECT logic_version AS ver, probability
    FROM signals
    WHERE NOT (logic_version = 3 AND degraded) AND probability IS NOT NULL
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY probability) AS val FROM base GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 5, 'p99',      ver, percentile_cont(0.99) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 6, 'максимум', ver, max(probability)                                          FROM base GROUP BY ver
    UNION ALL SELECT 7, 'N решений', ver, count(*)::double precision                                FROM base GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.2 Распределение probability по версиям (ТОЛЬКО направленные решения buy/sell) ---'
WITH base AS (
    SELECT logic_version AS ver, probability
    FROM signals
    WHERE NOT (logic_version = 3 AND degraded) AND probability IS NOT NULL AND decision <> 'wait'
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY probability) AS val FROM base GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 5, 'p99',      ver, percentile_cont(0.99) WITHIN GROUP (ORDER BY probability) FROM base GROUP BY ver
    UNION ALL SELECT 6, 'максимум', ver, max(probability)                                          FROM base GROUP BY ver
    UNION ALL SELECT 7, 'N решений', ver, count(*)::double precision                                FROM base GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.3 Распределение |балла| по версиям (пересчёт из agents_payload; направленные решения) ---'
WITH base AS (
    SELECT s.logic_version AS ver,
           abs(c.num / NULLIF(c.den, 0)) AS abs_score
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT sum(CASE el->>'signal' WHEN 'bullish' THEN 1 WHEN 'bearish' THEN -1 ELSE 0 END
                   * (el->>'confidence')::double precision) AS num,
               sum((el->>'confidence')::double precision)    AS den
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE NOT (s.logic_version = 3 AND s.degraded) AND s.decision <> 'wait'
), f AS (SELECT ver, abs_score FROM base WHERE abs_score IS NOT NULL
), stats AS (
    SELECT 1 AS ord, 'медиана (p50)' AS metric, ver, percentile_cont(0.50) WITHIN GROUP (ORDER BY abs_score) AS val FROM f GROUP BY ver
    UNION ALL SELECT 2, 'p75',      ver, percentile_cont(0.75) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 3, 'p90',      ver, percentile_cont(0.90) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 4, 'p95',      ver, percentile_cont(0.95) WITHIN GROUP (ORDER BY abs_score) FROM f GROUP BY ver
    UNION ALL SELECT 5, 'максимум', ver, max(abs_score)                                          FROM f GROUP BY ver
    UNION ALL SELECT 6, 'N решений', ver, count(*)::double precision                              FROM f GROUP BY ver
)
SELECT metric,
       round(max(val) FILTER (WHERE ver = 1)::numeric, 4) AS v1,
       round(max(val) FILTER (WHERE ver = 2)::numeric, 4) AS v2,
       round(max(val) FILTER (WHERE ver = 3)::numeric, 4) AS v3
FROM stats
GROUP BY ord, metric
ORDER BY ord;

\echo
\echo '--- 6.4 Распределение согласованности по версиям: доля каждого дискретного значения, % ---'
\echo '(согласованность пересчитана так, как её считал КОД соответствующей версии: v1/v2 — знаменатель = число свежих агентов, v3 — знаменатель = 3)'
WITH base AS (
    SELECT s.logic_version AS ver,
           CASE WHEN s.logic_version >= 3
                THEN round((abs(c.pos - c.neg)::double precision / 3.0)::numeric, 2)
                ELSE round((abs(c.pos - c.neg)::double precision / NULLIF(c.n, 0))::numeric, 2)
           END AS agreement
    FROM signals s
    CROSS JOIN LATERAL (
        SELECT count(*) FILTER (WHERE el->>'signal' = 'bullish') AS pos,
               count(*) FILTER (WHERE el->>'signal' = 'bearish') AS neg,
               count(*)                                          AS n
        FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
                      THEN s.agents_payload ELSE '[]'::jsonb END) el
    ) c
    WHERE NOT (s.logic_version = 3 AND s.degraded) AND s.decision <> 'wait'
), tot AS (SELECT ver, count(*) AS n FROM base GROUP BY ver)
SELECT COALESCE(b.agreement::text, 'нет данных') AS agreement_value,
       count(*) FILTER (WHERE b.ver = 1) AS v1_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 1)
             / NULLIF((SELECT n FROM tot WHERE ver = 1), 0), 2) AS v1_pct,
       count(*) FILTER (WHERE b.ver = 2) AS v2_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 2)
             / NULLIF((SELECT n FROM tot WHERE ver = 2), 0), 2) AS v2_pct,
       count(*) FILTER (WHERE b.ver = 3) AS v3_n,
       round(100.0 * count(*) FILTER (WHERE b.ver = 3)
             / NULLIF((SELECT n FROM tot WHERE ver = 3), 0), 2) AS v3_pct
FROM base b
GROUP BY 1
ORDER BY 1;

\echo
\echo '--- 6.5 Решения с probability >= 0.7: абсолютное число и доля (X из N) ---'
SELECT logic_version,
       count(*)                                                        AS decisions_total,
       count(*) FILTER (WHERE probability >= 0.7)                      AS prob_ge_07_x,
       round(100.0 * count(*) FILTER (WHERE probability >= 0.7) / NULLIF(count(*), 0), 3) AS prob_ge_07_pct,
       count(*) FILTER (WHERE decision <> 'wait')                      AS directional_n,
       count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7) AS directional_ge_07_x,
       round(100.0 * count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7)
             / NULLIF(count(*) FILTER (WHERE decision <> 'wait'), 0), 3) AS directional_ge_07_pct
FROM signals
WHERE NOT (logic_version = 3 AND degraded)
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.6 Разбивка решений buy / sell / wait по версиям (X из N) ---'
WITH tot AS (
    SELECT logic_version AS ver, count(*) AS n
    FROM signals WHERE NOT (logic_version = 3 AND degraded) GROUP BY logic_version
)
SELECT s.decision,
       count(*) FILTER (WHERE s.logic_version = 1) AS v1_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 1)
             / NULLIF((SELECT n FROM tot WHERE ver = 1), 0), 2) AS v1_pct,
       count(*) FILTER (WHERE s.logic_version = 2) AS v2_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 2)
             / NULLIF((SELECT n FROM tot WHERE ver = 2), 0), 2) AS v2_pct,
       count(*) FILTER (WHERE s.logic_version = 3) AS v3_n,
       round(100.0 * count(*) FILTER (WHERE s.logic_version = 3)
             / NULLIF((SELECT n FROM tot WHERE ver = 3), 0), 2) AS v3_pct
FROM signals s
WHERE NOT (s.logic_version = 3 AND s.degraded)
GROUP BY s.decision
ORDER BY s.decision;

\echo
\echo '--- 6.7 ВЕРСИЯ 3: сколько кандидатов дал бы каждый порог probability (только измерение) ---'
\echo '(кандидат = decision <> wait, degraded = false, probability >= порога; «в сутки» — деление на длительность периода версии 3)'
WITH v3 AS (
    SELECT probability, ts
    FROM signals
    WHERE logic_version = 3 AND degraded = false AND decision <> 'wait' AND probability IS NOT NULL
), span AS (
    SELECT GREATEST(extract(epoch FROM (max(ts) - min(ts))) / 86400.0, 0.0001) AS days,
           count(*) AS directional_total
    FROM v3
), t(threshold) AS (
    VALUES (0.70), (0.65), (0.60), (0.55), (0.50), (0.45), (0.40)
)
SELECT t.threshold,
       (SELECT count(*) FROM v3 WHERE probability >= t.threshold)                      AS candidates_x,
       (SELECT directional_total FROM span)                                            AS directional_n,
       round(100.0 * (SELECT count(*) FROM v3 WHERE probability >= t.threshold)
             / NULLIF((SELECT directional_total FROM span), 0), 3)                     AS candidates_pct,
       round(((SELECT count(*) FROM v3 WHERE probability >= t.threshold)
             / (SELECT days FROM span))::numeric, 1)                                   AS candidates_per_day
FROM t
ORDER BY t.threshold DESC;

\echo
\echo '--- 6.7б Для сравнения: сколько кандидатов в сутки давала версия 1 при пороге 0.7 ---'
WITH v1 AS (
    SELECT probability, ts FROM signals
    WHERE logic_version = 1 AND decision <> 'wait' AND probability IS NOT NULL
)
SELECT count(*)                                        AS directional_n,
       count(*) FILTER (WHERE probability >= 0.7)      AS candidates_ge_07,
       round((extract(epoch FROM (max(ts) - min(ts))) / 86400.0)::numeric, 3) AS days_span,
       round((count(*) FILTER (WHERE probability >= 0.7)
             / GREATEST(extract(epoch FROM (max(ts) - min(ts))) / 86400.0, 0.0001))::numeric, 1) AS candidates_per_day
FROM v1;

\echo
\echo '--- 6.8 Версия 3: число и доля решений с degraded = true ---'
SELECT logic_version,
       count(*)                                 AS decisions_total,
       count(*) FILTER (WHERE degraded)         AS degraded_x,
       round(100.0 * count(*) FILTER (WHERE degraded) / NULLIF(count(*), 0), 2) AS degraded_pct,
       min(ts) FILTER (WHERE degraded)          AS first_degraded_ts,
       max(ts) FILTER (WHERE degraded)          AS last_degraded_ts
FROM signals
GROUP BY logic_version
ORDER BY logic_version;

\echo
\echo '--- 6.9 Проверка альтернативного объяснения: изменилась ли доля neutral у каждого агента между версиями ---'
\echo '(источник — agent_outputs; границы версий взяты из signals)'
WITH bounds AS (
    SELECT COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 2),
                    (SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v2_start,
           COALESCE((SELECT min(ts) FROM signals WHERE logic_version = 3),
                    'infinity'::timestamptz) AS v3_start
), ao AS (
    SELECT a.agent, a.signal,
           CASE WHEN a.ts < b.v2_start THEN 1
                WHEN a.ts < b.v3_start THEN 2
                ELSE 3 END AS ver
    FROM agent_outputs a CROSS JOIN bounds b
)
SELECT agent,
       ver AS logic_version,
       count(*)                                                AS outputs_n,
       count(*) FILTER (WHERE signal = 'neutral')              AS neutral_x,
       round(100.0 * count(*) FILTER (WHERE signal = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE signal = 'bullish')              AS bullish_x,
       count(*) FILTER (WHERE signal = 'bearish')              AS bearish_x,
       count(*) FILTER (WHERE signal = 'insufficient_data')    AS insufficient_x
FROM ao
GROUP BY agent, ver
ORDER BY agent, ver;

\echo
\echo '--- 6.10 То же по мнениям, реально участвовавшим в решениях (agents_payload) ---'
SELECT s.logic_version,
       el->>'agent'                                                    AS agent,
       count(*)                                                        AS opinions_n,
       count(*) FILTER (WHERE el->>'signal' = 'neutral')               AS neutral_x,
       round(100.0 * count(*) FILTER (WHERE el->>'signal' = 'neutral') / NULLIF(count(*), 0), 2) AS neutral_pct,
       count(*) FILTER (WHERE el->>'signal' = 'bullish')               AS bullish_x,
       count(*) FILTER (WHERE el->>'signal' = 'bearish')               AS bearish_x,
       round(avg((el->>'confidence')::double precision)::numeric, 4)   AS avg_confidence
FROM signals s
CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(s.agents_payload) = 'array'
              THEN s.agents_payload ELSE '[]'::jsonb END) el
WHERE NOT (s.logic_version = 3 AND s.degraded)
GROUP BY s.logic_version, el->>'agent'
ORDER BY el->>'agent', s.logic_version;

\echo
\echo '--- 6.11 Число решений и доля probability >= 0.7 по суткам (весь период, версии помечены) ---'
SELECT date_trunc('day', ts)::date         AS day_utc,
       logic_version,
       count(*)                            AS decisions,
       count(*) FILTER (WHERE degraded)    AS degraded_x,
       count(*) FILTER (WHERE decision <> 'wait') AS directional,
       count(*) FILTER (WHERE decision <> 'wait' AND probability >= 0.7) AS candidates_ge_07,
       round(max(probability)::numeric, 4) AS max_probability
FROM signals
GROUP BY 1, 2
ORDER BY 1, 2;
```
