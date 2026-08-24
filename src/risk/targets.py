"""Расчёт цели по вероятности: MFE, процентиль, покрытие издержек (§4–§5 ТЗ 8.2).

Модуль ЧИСТЫЙ: ни базы, ни сети, ни времени «сейчас» внутри функций. Всё
состояние приходит параметрами, поэтому расчёт проверяется на синтетических
рядах с заранее известным ответом (§10 ТЗ).

ЧТО ТАКОЕ MFE. Максимальное благоприятное отклонение — насколько далеко цена
уходила В НУЖНУЮ СТОРОНУ за горизонт, считая ПО КАСАНИЮ (по внутрисвечным
максимуму и минимуму), а не по цене закрытия. Именно касание отвечает на вопрос
человека «дойдёт ли цена до уровня», а закрытие отвечает на другой вопрос.

    MFE_buy(t,h)  = (max(high[t+1..t+h]) − close[t]) / close[t] × 100
    MFE_sell(t,h) = (close[t] − min(low[t+1..t+h])) / close[t] × 100

ЧЕТЫРЕ ПРАВИЛА, КОТОРЫЕ ЛЕГКО НАРУШИТЬ НЕЗАМЕТНО, и потому вынесены сюда:

 1. Свеча ``t`` в собственное окно будущего НЕ ВХОДИТ (§4.1). Берутся строго
    ``t+1 .. t+h``: экстремум самой свечи решения известен только постфактум,
    и его участие завысило бы цель на величину, которой в момент решения
    не существовало.
 2. Отрицательные MFE НЕ ОБРЕЗАЮТСЯ нулём (§4.2). Если цена ни разу не
    поднялась выше цены решения, MFE отрицателен — это наблюдение, а не брак.
    Обрезка сместила бы процентиль вверх и завысила бы цель.
 3. Наблюдение, окно которого пересекает РАЗРЫВ ряда, отбрасывается (§4.3):
    максимум по неполному окну — это максимум по другому окну, а не по этому.
 4. Покупка и продажа считаются РАЗДЕЛЬНО (§4.8): падения быстрее ростов, и
    общее распределение усреднило бы разные величины.

ГРАНИЦА ПРИМЕНИМОСТИ, о которой обязаны знать и отчёт, и человек: выборка
БЕЗУСЛОВНАЯ. Берутся ВСЕ часы окна, а не только те, в которые система выдала бы
сигнал, — истории сигналов на 90 суток не существует. Поэтому цель описывает
поведение РЫНКА, а не качество системы.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

# Длина часового бара. Ряд целей считается только по часовым свечам: на них же
# работает Market Agent (AGENT_TIMEFRAME=1h), и смешивать таймфреймы нельзя.
BAR_SECONDS = 3600

# Процентиль цели (§4.4). 40-й: по построению 60% наблюдений имеют MFE не
# меньше цели, то есть «6 случаев из 10» — это не подставленное число, а
# свойство выбранного процентиля, которое затем ПРОВЕРЯЕТСЯ по выборке.
TARGET_PERCENTILE = 0.40

# Во сколько раз цель обязана превышать круговые издержки, чтобы сделка имела
# смысл (§5). Три — запас на то, что издержки известны приблизительно.
COST_MULTIPLIER = 3

# Направления.
BUY = "buy"
SELL = "sell"

# Причины отсутствия цели. Перечень ЗАКРЫТ и повторён ограничением БД
# (миграция 014): причина, названная своими словами, не считается запросом.
REASON_FEW_OBSERVATIONS = "few_observations"
REASON_NEGATIVE_PERCENTILE = "negative_percentile"
REASON_DATA_GAP = "data_gap"
# Только для signal_targets: строки risk_targets не нашлось вовсе (§6.5).
REASON_NO_RISK_TARGET = "no_risk_target"


@dataclass(frozen=True)
class Candle:
    """Часовая свеча спота из ``backtest.candles`` (закрытая)."""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class MfeSample:
    """Выборка MFE и то, что в неё НЕ вошло.

    ``skipped_gap`` — наблюдения, отброшенные из-за разрыва ряда (§4.3);
    ``skipped_tail`` — базовые свечи у правого края, для которых горизонта
    просто ещё не наступило. Оба числа идут в отчёт: «наблюдений мало» и
    «наблюдения выброшены» — разные состояния, и различать их обязательно.
    """

    values: tuple[float, ...]
    skipped_gap: int
    skipped_tail: int

    @property
    def n(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class TargetResult:
    """Итог расчёта по одной паре (инструмент, горизонт) и одному направлению.

    ``target_pct`` — НЕ округлённый: округление до пяти знаков выполняет
    колонка ``NUMERIC(10,5)`` при записи. Округлять здесь нельзя: проверка
    §10.2 сверяет процентиль с точностью 1e-9.
    """

    n_observations: int
    target_pct: float | None
    hit_rate: float | None
    mfe_p25: float | None
    mfe_p50: float | None
    mfe_p75: float | None
    covers_fees: bool
    no_target_reason: str | None
    skipped_gap: int = 0
    skipped_tail: int = 0


def percentile_cont(values: list[float] | tuple[float, ...], q: float) -> float:
    """Непрерывный процентиль с линейной интерполяцией — как ``percentile_cont``.

    Совпадает с реализацией PostgreSQL и numpy (метод ``linear``): для
    отсортированного ряда длины ``n`` берётся позиция ``q × (n − 1)``, и между
    соседними значениями идёт линейная интерполяция. Совпадение важно потому,
    что ту же величину можно перепроверить прямым запросом к базе.
    """
    if not values:
        raise ValueError("процентиль пустой выборки не определён")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"процентиль вне [0, 1]: {q}")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def contiguous_runs(candles: list[Candle]) -> list[int]:
    """Номер непрерывного отрезка для каждой свечи ряда.

    Отрезок обрывается везде, где расстояние до следующей свечи не равно ровно
    часу. Наблюдения, чьи концы лежат в разных отрезках, в выборку не попадают
    (§4.3): по неполному окну считается не тот максимум.
    """
    runs: list[int] = []
    current = 0
    for index, candle in enumerate(candles):
        if index > 0:
            delta = candle.open_time - candles[index - 1].open_time
            if delta != timedelta(seconds=BAR_SECONDS):
                current += 1
        runs.append(current)
    return runs


def mfe_sample(
    candles: list[Candle],
    horizon_h: int,
    direction: str,
) -> MfeSample:
    """Выборка MFE по всем базовым свечам ряда для одного направления.

    Ряд обязан быть отсортирован по ``open_time`` по возрастанию и не содержать
    повторов — это гарантирует первичный ключ ``backtest.candles``.
    """
    if direction not in (BUY, SELL):
        raise ValueError(f"неизвестное направление: {direction}")
    if horizon_h <= 0:
        raise ValueError(f"горизонт должен быть положительным: {horizon_h}")

    runs = contiguous_runs(candles)
    values: list[float] = []
    skipped_gap = 0
    skipped_tail = 0

    for index in range(len(candles)):
        last = index + horizon_h
        if last >= len(candles):
            # Горизонта ещё не наступило: это не пропуск данных, а правый край.
            skipped_tail += 1
            continue
        if runs[last] != runs[index]:
            skipped_gap += 1
            continue
        anchor = float(candles[index].close)
        if anchor <= 0:
            # Неположительная цена решения делает отношение бессмысленным.
            # Такие строки отсеивает предпроверка §1; здесь — страховка.
            skipped_gap += 1
            continue
        # ОКНО СТРОГО t+1 .. t+h: свеча решения в него не входит (§4.1).
        window = candles[index + 1 : last + 1]
        if direction == BUY:
            extreme = max(float(c.high) for c in window)
            value = (extreme - anchor) / anchor * 100.0
        else:
            extreme = min(float(c.low) for c in window)
            value = (anchor - extreme) / anchor * 100.0
        # Отрицательные значения НЕ обрезаются (§4.2).
        values.append(value)

    return MfeSample(
        values=tuple(values), skipped_gap=skipped_gap, skipped_tail=skipped_tail
    )


def covers_fees_flag(target_pct: float | None, cost_roundtrip_pct: float) -> bool:
    """Покрывает ли цель тройные круговые издержки (§5).

    Сравнение идёт в Decimal, а не в float, намеренно: ``3 × 0.22`` в двоичной
    арифметике равно 0.66000000000000003, и цель ровно 0.66 объявлялась бы
    непокрывающей. Граница проходит там, где её задал ТЗ, а не там, куда её
    сдвинуло представление чисел.
    """
    if target_pct is None:
        return False
    threshold = Decimal(str(cost_roundtrip_pct)) * COST_MULTIPLIER
    return Decimal(str(target_pct)) >= threshold


def compute_target(
    sample: MfeSample,
    *,
    cost_roundtrip_pct: float,
    min_observations: int,
    percentile: float = TARGET_PERCENTILE,
) -> TargetResult:
    """Цель, фактическая доля касаний и опорные процентили по выборке MFE.

    ``hit_rate`` считается ФАКТИЧЕСКИ (§4.5) — долей наблюдений с MFE не меньше
    цели, а не подставляется как 0.60. Подстановка скрыла бы ошибку расчёта:
    именно расхождение факта с ожиданием и служит проверкой.
    """
    n = sample.n
    common = {
        "n_observations": n,
        "skipped_gap": sample.skipped_gap,
        "skipped_tail": sample.skipped_tail,
    }
    if n < min_observations:
        return TargetResult(
            target_pct=None, hit_rate=None,
            mfe_p25=None, mfe_p50=None, mfe_p75=None,
            covers_fees=False, no_target_reason=REASON_FEW_OBSERVATIONS,
            **common,
        )

    values = sample.values
    target = percentile_cont(values, percentile)
    p25 = percentile_cont(values, 0.25)
    p50 = percentile_cont(values, 0.50)
    p75 = percentile_cont(values, 0.75)

    if target <= 0:
        # Уровень ниже текущей цены целью покупки быть не может (§4.7).
        # Процентили сохраняются: они описывают рынок и нужны отчёту, даже
        # когда цель не выдаётся.
        return TargetResult(
            target_pct=None, hit_rate=None,
            mfe_p25=p25, mfe_p50=p50, mfe_p75=p75,
            covers_fees=False, no_target_reason=REASON_NEGATIVE_PERCENTILE,
            **common,
        )

    hit_rate = sum(1 for v in values if v >= target) / n
    return TargetResult(
        target_pct=target, hit_rate=hit_rate,
        mfe_p25=p25, mfe_p50=p50, mfe_p75=p75,
        covers_fees=covers_fees_flag(target, cost_roundtrip_pct),
        no_target_reason=None,
        **common,
    )


def round_significant(value: float, digits: int = 6) -> float:
    """Округление до ``digits`` ЗНАЧАЩИХ цифр (§4.9).

    Шаг цены инструмента системе неизвестен: он принадлежит бирже, в базе не
    хранится и по свечам не восстанавливается. Поэтому округление ведётся по
    значащим цифрам — оно заведомо НЕ ГРУБЕЕ шага цены и одинаково годится и
    для BTC (цена порядка 65 000), и для DOGE (порядка 0.1), где округление до
    двух знаков уничтожило бы цель.
    """
    if value == 0 or not math.isfinite(value):
        return value
    exponent = math.floor(math.log10(abs(value)))
    return round(value, digits - 1 - exponent)


def target_price(
    price_at_signal: float, target_pct: float, direction: str, digits: int = 6
) -> float:
    """Цена цели: вверх для покупки, вниз для продажи (§6.3)."""
    if direction not in (BUY, SELL):
        raise ValueError(f"неизвестное направление: {direction}")
    factor = 1.0 + float(target_pct) / 100.0
    if direction == SELL:
        factor = 1.0 - float(target_pct) / 100.0
    return round_significant(float(price_at_signal) * factor, digits)
