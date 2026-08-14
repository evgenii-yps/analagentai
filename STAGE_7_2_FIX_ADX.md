# Этап 7.2 (доработка) — корневая причина DataError в Market: `pd.NA` в `adx()`

Дата: 14.08.2026. Ветка: `claude/agent-resilience-aggregation-bias-m5k6ya` (от `main`).

Причина найдена **трассировкой на живом образе** (не гипотеза). Ранняя версия про
деградацию долгоживущего соединения (`STAGE_7_2_REPORT.md` §1.2) **опровергнута**:
баг воспроизводится на холодном старте — контейнер `agents` запущен 13:17:44.456,
первый `DataError` в 13:17:44.483 (через 27 мс), деградировать нечему.

## 1. Где и почему падало

**Точка падения:** `src/agents/market.py`, функция `adx()`:
`dx.ewm(alpha=1.0/period, adjust=False).mean()`.

**Механизм.** ADX по Уайлдеру:

```
plus_di  = 100 * EWMA(plus_dm)  / atr
minus_di = 100 * EWMA(minus_dm) / atr
di_sum   = plus_di + minus_di
dx       = 100 * |plus_di - minus_di| / di_sum
adx      = EWMA(dx)
```

Когда нет направленного движения (`high.diff()==0` и `low.diff()==0` —
постоянные high/low), `plus_dm=minus_dm=0` → `plus_di=minus_di=0` →
`di_sum == 0`. Прежний код гасил деление на ноль так:

```python
di_sum = (plus_di + minus_di).replace(0.0, pd.NA)   # ← корень бага
```

`pd.NA` — это nullable-скаляр pandas. Подстановка его во **float64**-ряд
поднимает `dtype` до **object** (в float-массиве `pd.NA` жить не может). Дальше
`dx = 100 * |...| / di_sum` наследует `object`, и `dx.ewm(...).mean()` на
object-ряде падает:

* локально (pandas 2.3.3) — `DataError: No numeric types to aggregate`;
* на сервере — эквивалентный `TypeError: float() argument must be a string or a
  real number, not 'NAType'`.

Это одна и та же причина; вход **чистый** (250 свечей, `open/high/low/close/volume`
все `float64`) — `pd.NA` рождается **внутри** вычислений, поэтому входная проверка
из A1 корректно не срабатывала.

## 2. Исправление в корне

`src/agents/market.py::adx()` — `pd.NA` → `np.nan`, ряд остаётся `float`:

```python
di_sum = plus_di + minus_di
di_sum = di_sum.where(di_sum != 0.0)   # other=np.nan по умолчанию; dtype float
dx = 100.0 * (plus_di - minus_di).abs() / di_sum
adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)
```

* Деление на ноль обрабатывается явно: где `di_sum == 0`, ставится `np.nan`.
* `np.nan` сохраняет `float64`; `nan` штатно протягивается через `ewm` и гасится
  финальным `fillna(0.0)` → на вырожденном участке `ADX = 0` (корректно: нет
  тренда), без NaN и без исключения.
* Проверено: на трендовых данных `adx` по-прежнему `float64` и осмысленный; на
  плоском участке — `0.0`, `dtype=float64`, без NaN.

Других использований `pd.NA` / `replace(..., NA)` в `src/` нет (проверено grep).

## 3. Защита ПОСЛЕ промежуточных шагов (а не только на входе)

В `analyze_ohlcv`, сразу после расчёта всех индикаторных рядов и до извлечения
`.iloc[-1]`/агрегаций, добавлена проверка: если любой из рядов (`ema_*`, `rsi`,
`atr`, `macd_*`, `adx`, `plus_di`, `minus_di`) перестал быть числовым — вернуть
`insufficient_data`, **а не** дать исключению улететь. Это ловит и будущие
регрессии того же класса (появление object-ряда внутри вычислений). Итог:
компьют-путь Market больше не может выбросить исключение из-за нечислового
промежуточного ряда — только штатный `insufficient_data` со строкой в
`agent_outputs`.

## 4. Тест, воспроизводящий баг (падает на старом коде)

`tests/test_agents.py`:

* `test_adx_zero_di_sum_does_not_crash` — прямой вызов `adx()` на входе с
  постоянными high/low (`plus_di + minus_di == 0`). На **старом** коде падает
  (`DataError`/`TypeError NAType`); после правки — зелёный, `adx` числовой и `= 0`.
* `test_market_flat_high_low_is_insufficient_or_neutral_not_exception` — тот же
  вырожденный вход через полный `analyze_ohlcv` (≥200 свечей): без исключения,
  результат `neutral`/`insufficient_data`.

Подтверждено: до правки оба падают именно с `DataError: No numeric types to
aggregate`; после — весь набор зелёный (143 passed), `ruff` чисто.

## 5. Версии pandas/numpy в requirements.txt

`requirements.txt` **уже** закрепляет точные версии, совпадающие с образом:

```
pandas==2.3.3
numpy==2.4.6
```

`Dockerfile` собирает образ именно из `requirements.txt` (`pip install -r
requirements.txt`) — то есть прод-образ воспроизводим и соответствует среде, где
снята трассировка (pandas 2.3.3, numpy 2.4.6, python 3.12). **Менять ничего не
потребовалось.** Замечание на будущее: `pyproject.toml` держит более широкие
диапазоны (`pandas>=2.2,<3`, `numpy>=1.26,<3`) — они для editable/dev-установок и
на прод-образ не влияют; при желании их можно сузить, но это не требуется для
воспроизводимости прод-образа.

## 6. Трассировка в `agent_failures.detail`

Коммит `60d0f87` (входит в эту же ветку): `_record_failure` пишет в `detail`
полную `traceback.format_exception(exc)` вместо дубля `str(exc)`;
`record_agent_failure` держит **хвост** до 4000 символов (нижние кадры = место
падения), а не режет до 300. Именно эта правка и позволила бы увидеть точку
падения (`adx()`), не собирая репродукцию вручную. Лог-строка осталась короткой
(сообщение). Тест: `tests/test_agent_resilience.py::test_record_failure_writes_full_traceback`.
