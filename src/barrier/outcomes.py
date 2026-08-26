"""Правило исхода по границам: цель, предел, срок (§3–§5 ТЗ 8.8).

ЧЕМ ЭТА ОЦЕНКА ОТЛИЧАЕТСЯ ОТ ДЕЙСТВУЮЩЕЙ. ``signal_evaluations`` фиксирует
положение цены в момент ``t + горизонт``: цена могла дойти до цели и вернуться,
могла дойти на час позже замера — обе истории записываются там одинаково.
Здесь считается другое: КАКАЯ ГРАНИЦА ЗАДЕТА ПЕРВОЙ. Обе величины существуют
параллельно и сравниваются позже (§9); ни одна не отменяет другую.

Модуль ЧИСТЫЙ: ни базы, ни сети, ни времени «сейчас» внутри функций. Всё
состояние приходит параметрами, поэтому правило проверяется на синтетических
рядах с заранее известным ответом (§10 ТЗ).

ТРИ УРОВНЯ от цены решения P:

    уровень   buy                      sell
    цель      P × (1 + target_pct/100) P × (1 − target_pct/100)
    предел    P × (1 − stop_pct/100)   P × (1 + stop_pct/100)
    срок      t + h часов              то же

ЧЕТЫРЕ ПРАВИЛА, КОТОРЫЕ ЛЕГКО НАРУШИТЬ НЕЗАМЕТНО:

 1. «ЗАДЕТА» — ЭТО КАСАНИЕ ВНУТРИ СВЕЧИ, по ``high`` и ``low``, а не по цене
    закрытия (§3). Реальный ордер срабатывает по касанию; закрытие отвечает на
    другой вопрос. Касание РОВНО В ГРАНИЦУ засчитывается: сравнение нестрогое.
 2. СВЕЧА МОМЕНТА РЕШЕНИЯ В ОКНО НЕ ВХОДИТ. Окно строго ``t+1 … t+h`` в барах
    выбранного разрешения — ровно так же, как в ``src/risk/targets.py``.
 3. ОДНОВРЕМЕННОЕ КАСАНИЕ НЕ РАЗРЕШАЕТСЯ ДОГАДКОЙ. Если внутри одного бара
    задеты обе границы, порядок неизвестен, и исход — ``ambiguous`` (§4).
    Приведение к ``target`` или ``stop`` по любому эвристическому правилу
    запрещено: это была бы выдумка, неотличимая в отчёте от измерения.
 4. ``mae_pct`` и ``mfe_pct`` СЧИТАЮТСЯ ПО ВСЕМУ ОКНУ, а не до момента касания.
    Они описывают ОКНО, а не сделку, и именно поэтому годятся для §8 — вопроса
    «что было бы при другом уровне предела». Обрезка их по факту срабатывания
    сделала бы §8 неотвечаемым: при пределе 0.3% сделка закрылась бы раньше,
    чем при 1.0%, и обрезанное значение отвечало бы на чужой вопрос.

ГРАНИЦА ПРИМЕНИМОСТИ. Величина ``bars_to_hit`` измеряется В БАРАХ ВЫБРАННОГО
РАЗРЕШЕНИЯ: 7 при ``resolution='1m'`` — это семь минут, при ``'1h'`` — семь
часов. Сравнивать эти числа между разрешениями напрямую нельзя.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Бар в секундах для двух поддерживаемых разрешений (§6: колонка resolution
# принимает ровно эти два значения).
BAR_SECONDS: dict[str, int] = {"1m": 60, "1h": 3600}

RESOLUTION_1M = "1m"
RESOLUTION_1H = "1h"

BUY = "buy"
SELL = "sell"

# Пять исходов §3. Перечень ЗАКРЫТ и повторён ограничением БД (миграция 015):
# исход, названный своими словами, не считается исходом.
OUTCOME_TARGET = "target"
OUTCOME_STOP = "stop"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_NO_DATA = "no_data"

OUTCOMES = (
    OUTCOME_TARGET,
    OUTCOME_STOP,
    OUTCOME_TIMEOUT,
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_DATA,
)


@dataclass(frozen=True)
class Bar:
    """Свеча ряда. ``ts`` — время ОТКРЫТИЯ бара, как в ``ohlcv`` и у ccxt."""

    ts: datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BarrierOutcome:
    """Итог по одной паре (сигнал, горизонт).

    ``hit_at`` — время ОТКРЫТИЯ бара, в котором произошло касание, а не точный
    момент касания: точнее ряд свечей не знает, и вид точности, которой нет,
    здесь не создаётся.
    """

    outcome: str
    hit_at: datetime | None
    bars_to_hit: int | None
    net_pnl_pct: float | None
    mae_pct: float
    mfe_pct: float
    resolution: str
    bars_seen: int
    bars_expected: int


def levels(
    price_at_signal: float, target_pct: float, stop_pct: float, direction: str
) -> tuple[float, float]:
    """Цены цели и предела (§3). Возвращает ``(target_price, stop_price)``."""
    if direction not in (BUY, SELL):
        raise ValueError(f"неизвестное направление: {direction}")
    if price_at_signal <= 0:
        raise ValueError(f"цена решения должна быть положительной: {price_at_signal}")
    if stop_pct <= 0:
        raise ValueError(f"предел должен быть положительным: {stop_pct}")
    price = float(price_at_signal)
    if direction == BUY:
        return (
            price * (1.0 + float(target_pct) / 100.0),
            price * (1.0 - float(stop_pct) / 100.0),
        )
    return (
        price * (1.0 - float(target_pct) / 100.0),
        price * (1.0 + float(stop_pct) / 100.0),
    )


def expected_bars(horizon_h: int, resolution: str) -> int:
    """Сколько баров обязано быть в окне ``t+1 … t+h`` при этом разрешении.

    Горизонт задан в ЧАСАХ, поэтому число баров зависит от разрешения: 24 часа
    это 24 часовых бара или 1440 минутных. Окно и там, и там покрывает один и
    тот же отрезок времени длиной в горизонт, начиная со следующего бара после
    бара решения.
    """
    if horizon_h <= 0:
        raise ValueError(f"горизонт должен быть положительным: {horizon_h}")
    if resolution not in BAR_SECONDS:
        raise ValueError(f"неизвестное разрешение: {resolution}")
    return horizon_h * 3600 // BAR_SECONDS[resolution]


def window_bounds(
    signal_ts: datetime, horizon_h: int, resolution: str
) -> tuple[datetime, datetime]:
    """Границы окна по времени ОТКРЫТИЯ баров: ``(первый, последний)``.

    Отсчёт ведётся от бара РЕШЕНИЯ — того, внутри которого лежит ``t``. Он
    получается усечением ``t`` вниз до сетки баров, и в окно НЕ входит (§3):
    экстремум свечи решения известен только постфактум.

    Обе границы включительные: ``[первый, последний]`` — ровно
    ``expected_bars`` баров.
    """
    if signal_ts.tzinfo is None:
        # Наивное время молча трактовалось бы как местное, и окно уехало бы на
        # смещение часового пояса. Сигналы хранятся в TIMESTAMPTZ — требуем то
        # же самое здесь, а не догадываемся.
        raise ValueError("момент сигнала обязан быть с часовым поясом")
    step = BAR_SECONDS[resolution]
    epoch_s = int(signal_ts.timestamp())
    decision_open = datetime.fromtimestamp(epoch_s - epoch_s % step, tz=signal_ts.tzinfo)
    first = decision_open + timedelta(seconds=step)
    last = decision_open + timedelta(seconds=step * expected_bars(horizon_h, resolution))
    return first, last


def contiguous_prefix(bars: list[Bar], first_ts: datetime, resolution: str) -> list[Bar]:
    """Непрерывный отрезок ряда, начинающийся ровно с ``first_ts``.

    Ряд, начатый позже положенного или разорванный, обрезается по месту
    разрыва: по неполному окну считается не тот исход, а ДРУГОЙ. При этом
    касание, случившееся ДО разрыва, остаётся фактом — его и возвращает
    ``resolve``; отброшено только то, что за разрывом.
    """
    step = timedelta(seconds=BAR_SECONDS[resolution])
    prefix: list[Bar] = []
    expected_ts = first_ts
    for bar in bars:
        if bar.ts != expected_ts:
            break
        prefix.append(bar)
        expected_ts = expected_ts + step
    return prefix


def _excursions(
    bars: list[Bar], price: float, direction: str
) -> tuple[float, float]:
    """``(mae_pct, mfe_pct)`` по всему переданному отрезку, по касанию.

    Пустой отрезок даёт ``(0.0, 0.0)``: это НЕ измерение, а заполнитель для
    колонок ``NOT NULL`` схемы §6. Отличить его от настоящего нуля позволяет
    исход — у пустого окна он всегда ``no_data``.

    ``mae`` — максимальное отклонение ПРОТИВ сигнала, ``mfe`` — В ПОЛЬЗУ. Оба
    в процентах от цены решения и оба МОГУТ БЫТЬ ОТРИЦАТЕЛЬНЫМИ: если цена ни
    разу не ушла против сигнала, ``mae`` отрицателен — это наблюдение, а не
    брак. Обрезка нулём сместила бы таблицу §8 вверх и завысила бы долю
    «сработавших» пределов.
    """
    if not bars:
        return 0.0, 0.0
    highest = max(float(b.high) for b in bars)
    lowest = min(float(b.low) for b in bars)
    if direction == BUY:
        mae = (price - lowest) / price * 100.0
        mfe = (highest - price) / price * 100.0
    else:
        mae = (highest - price) / price * 100.0
        mfe = (price - lowest) / price * 100.0
    return mae, mfe


def _touches(bar: Bar, target_price: float, stop_price: float, direction: str
             ) -> tuple[bool, bool]:
    """Задеты ли внутри бара цель и предел. Касание РОВНО В ГРАНИЦУ — да."""
    if direction == BUY:
        return float(bar.high) >= target_price, float(bar.low) <= stop_price
    return float(bar.low) <= target_price, float(bar.high) >= stop_price


def net_pnl(
    outcome: str,
    *,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    price_at_signal: float,
    close_at_deadline: float | None,
    direction: str,
) -> float | None:
    """Итог в деньгах за вычетом издержек (§5).

    ``cost_pct`` — круговые издержки, приходят ПАРАМЕТРОМ из
    ``RISK_COST_ROUNDTRIP_PCT``. Зашивать 0.22 в код запрещено (§5 ТЗ):
    тарифы биржи меняются, и зашитая константа однажды начнёт врать молча.

    У ``ambiguous`` и ``no_data`` результата нет: неизвестно, что произошло, —
    значит, неизвестно и сколько получил бы человек. Ноль здесь был бы
    утверждением «человек не заработал и не потерял», которого никто не мерил.
    """
    if outcome == OUTCOME_TARGET:
        return float(target_pct) - float(cost_pct)
    if outcome == OUTCOME_STOP:
        return -float(stop_pct) - float(cost_pct)
    if outcome == OUTCOME_TIMEOUT:
        if close_at_deadline is None:
            raise ValueError("timeout без цены на срок: окно пустым не бывает")
        move = (float(close_at_deadline) - float(price_at_signal)) / float(
            price_at_signal
        ) * 100.0
        if direction == SELL:
            move = -move
        return move - float(cost_pct)
    return None


def resolve(
    bars: list[Bar],
    *,
    signal_ts: datetime,
    horizon_h: int,
    price_at_signal: float,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    direction: str,
    resolution: str,
) -> BarrierOutcome:
    """Исход по одной паре (сигнал, горизонт). Чистая функция.

    ``bars`` — свечи выбранного разрешения по возрастанию ``ts``, БЕЗ повторов;
    лишние свечи вне окна допустимы и отбрасываются здесь же, чтобы вызывающему
    коду не приходилось повторять правило границ окна.

    ПОРЯДОК ПРОВЕРОК В КАЖДОМ БАРЕ ЗНАЧИМ: сначала выясняется, задеты ли ОБЕ
    границы (тогда исход ``ambiguous``), и только затем — какая одна. Обратный
    порядок молча превращал бы неразрешимый случай в ``target`` или ``stop``
    в зависимости от того, какую ветку написали первой.
    """
    if direction not in (BUY, SELL):
        raise ValueError(f"неизвестное направление: {direction}")
    if resolution not in BAR_SECONDS:
        raise ValueError(f"неизвестное разрешение: {resolution}")

    target_price, stop_price = levels(price_at_signal, target_pct, stop_pct, direction)
    first_ts, last_ts = window_bounds(signal_ts, horizon_h, resolution)
    total = expected_bars(horizon_h, resolution)
    price = float(price_at_signal)

    window = [b for b in bars if first_ts <= b.ts <= last_ts]
    prefix = contiguous_prefix(window, first_ts, resolution)
    # mae/mfe — ПО ВСЕМУ наблюдённому окну (правило 4 в заголовке модуля).
    mae_pct, mfe_pct = _excursions(prefix, price, direction)

    def _done(outcome: str, hit: Bar | None, index: int | None) -> BarrierOutcome:
        close_at_deadline = float(prefix[-1].close) if prefix else None
        return BarrierOutcome(
            outcome=outcome,
            hit_at=None if hit is None else hit.ts,
            bars_to_hit=index,
            net_pnl_pct=net_pnl(
                outcome,
                target_pct=target_pct, stop_pct=stop_pct, cost_pct=cost_pct,
                price_at_signal=price, close_at_deadline=close_at_deadline,
                direction=direction,
            ),
            mae_pct=mae_pct, mfe_pct=mfe_pct, resolution=resolution,
            bars_seen=len(prefix), bars_expected=total,
        )

    for index, bar in enumerate(prefix, start=1):
        hit_target, hit_stop = _touches(bar, target_price, stop_price, direction)
        if hit_target and hit_stop:
            # Порядок внутри бара неизвестен — и остаётся неизвестным (§4).
            return _done(OUTCOME_AMBIGUOUS, None, None)
        if hit_target:
            return _done(OUTCOME_TARGET, bar, index)
        if hit_stop:
            return _done(OUTCOME_STOP, bar, index)

    # Ни одна граница не задета на непрерывном отрезке. Объявить timeout можно
    # ТОЛЬКО если отрезок покрыл всё окно: иначе граница могла быть задета там,
    # где ряда нет, и «до срока не задета ни одна» было бы утверждением о
    # данных, которых мы не видели.
    if len(prefix) < total:
        return _done(OUTCOME_NO_DATA, None, None)
    return _done(OUTCOME_TIMEOUT, None, None)
