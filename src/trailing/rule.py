"""Правило подвижного выхода и расчёт всех 13 вариантов за один проход (§4 ТЗ 8.10).

ЧТО ЗДЕСЬ ИЗМЕРЯЕТСЯ. Действующий выход один: цель, предел убытка или срок,
причём цель заморожена в момент сигнала. Владелец предложил другой выход —
закрывать по откату от достигнутой вершины. Этот модуль считает ОБА выхода на
одних и тех же свечах, одними и теми же правилами касания, чтобы разницу можно
было назвать числом. Он ничего не выбирает и ничего не внедряет.

ЖЁСТКАЯ ГРАНИЦА (§2 ТЗ). ``src/barrier/outcomes.py`` НЕ ПЕРЕПИСЫВАЕТСЯ и не
копируется: 13-й, контрольный вариант считается ПРЯМЫМ ВЫЗОВОМ
``outcomes.resolve`` — той самой функции, которой посчитана таблица
``signal_outcomes_barrier``. Вторая реализация того же правила рано или поздно
разошлась бы с первой на краевом случае, и сравнение стало бы недействительным
незаметно. Уровни цели и предела берутся из ``outcomes.levels``, касание
проверяется тем же нестрогим сравнением по ``high``/``low``.

ДВЕНАДЦАТЬ СОЧЕТАНИЙ И ТРИНАДЦАТОЕ, КОНТРОЛЬНОЕ:

    A (уровень включения, доля от цели): 0.25, 0.50, 0.75, 1.00
    R (величина отката, доля пройденного пути): 0.20, 0.33, 0.50
    (0, 0) — подвижного выхода нет вовсе: фиксированная цель, правило 8.8.

ПРАВИЛО, дословно по §4:

 1. идём по свечам окна ``t+1 … t+горизонт``;
 2. следим за наибольшим ходом В ПОЛЬЗУ сигнала — «вершина»;
 3. пока вершина не достигла уровня включения A — работает обычное правило;
 4. как только достигла — закрываем при откате от вершины на долю R
    пройденного пути;
 5. предел убытка действует всё время;
 6. ничего не сработало до срока — timeout по цене на срок.

ТРИ МЕСТА, ГДЕ ПРАВИЛО ЛЕГКО ПРЕВРАТИТЬ В ВЫДУМКУ. Каждое решено явно, и
каждое решение сказано вслух, потому что в отчёте выдумка неотличима от замера.

 1. ВЕРШИНА, ПОСТАВЛЕННАЯ ВНУТРИ БАРА, УЧИТЫВАЕТСЯ СО СЛЕДУЮЩЕГО БАРА.
    Внутри одного бара порядок ``high`` и ``low`` неизвестен. Если считать
    откат от вершины, поставленной этим же баром, придётся угадать, что вершина
    была раньше падения, — а угадать это нельзя (то же основание, по которому
    §4 ТЗ 8.8 отказывается разрешать одновременное касание). Поэтому уровень
    отката всегда считается от вершины, ИЗВЕСТНОЙ ДО ТЕКУЩЕГО БАРА. Правило
    сдвигает выход не более чем на один бар и НИКОГДА не сдвигает его в
    выгодную сторону: измерение получается осторожным, а не приукрашенным.

 2. ПОСЛЕ ВКЛЮЧЕНИЯ ПОДВИЖНОГО ВЫХОДА ЦЕЛЬ БОЛЬШЕ НЕ ЗАКРЫВАЕТ СДЕЛКУ, и это
    не упущение, а прямое следствие §4.4. Уровень включения A ≤ 1 лежит НЕ
    ДАЛЬШЕ цели, поэтому, чтобы дойти до цели, цена обязана пройти через
    уровень включения — то есть подвижный выход к этому моменту уже включён.
    Отсюда следствие, которое стоит знать заранее: у двенадцати подвижных
    вариантов исход ``target`` НЕВОЗМОЖЕН в принципе, он бывает только у
    контрольного. Это проверяется ограничением БД (миграция 017).

 3. ПОСЛЕ ВКЛЮЧЕНИЯ ПРЕДЕЛ УБЫТКА ФОРМАЛЬНО ДЕЙСТВУЕТ, НО НЕ СРАБАТЫВАЕТ.
    Уровень отката при положительной вершине лежит ВЫШЕ цены входа (для
    покупки) — а предел ниже неё. Цена, дошедшая до предела, обязана была
    пройти уровень отката раньше. Поэтому после включения выход всегда
    ``trail``, и это не нарушение §4.5, а его арифметическое следствие.

ЕДИНСТВЕННОЕ МЕСТО, ГДЕ ОСТАЁТСЯ НЕИЗВЕСТНОСТЬ, и она признаётся, а не
разрешается догадкой: до включения в одном баре могут случиться и достижение
уровня включения, и касание предела. Что было раньше — неизвестно: в одном
порядке сделка закрылась бы по пределу, в другом дожила бы до подвижного
выхода. Такой случай получает исход ``ambiguous`` — ровно как в §4 ТЗ 8.8.

ЧТО ЗНАЧИТ ``peak_pct``. Это вершина, ИЗВЕСТНАЯ ПРАВИЛУ НА МОМЕНТ ВЫХОДА, в
процентах от цены входа (для timeout и no_data — по всему наблюдённому
отрезку). Она не описывает окно — окно описывает ``mfe_pct``, и он считается
ровно как в 8.8, по всему окну. Благодаря такому определению итог подвижного
выхода проверяется арифметикой: ``net_pnl_pct = (1 − R) × peak_pct − cost_pct``.

Модуль ЧИСТЫЙ: ни базы, ни сети, ни времени «сейчас».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.barrier.outcomes import (
    BUY,
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_DATA,
    OUTCOME_STOP,
    OUTCOME_TARGET,
    OUTCOME_TIMEOUT,
    SELL,
    Bar,
    contiguous_prefix,
    expected_bars,
    levels,
    net_pnl,
    resolve,
    window_bounds,
)

# --- Сетка вариантов §4. Перечень ЗАКРЫТ и повторён ограничением БД (017). ---
ACTIVATION_RATIOS: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00)
RETRACE_RATIOS: tuple[float, ...] = (0.20, 0.33, 0.50)

# Контрольный вариант — «подвижного выхода нет». Ноль выбран не произвольно:
# первичный ключ таблицы (§6 ТЗ) включает оба параметра, а NULL в первичном
# ключе PostgreSQL не допускает. Пара (0, 0) не пересекается с сеткой выше и
# читается однозначно: включать нечего и откатывать нечего.
FIXED_ACTIVATION = 0.0
FIXED_RETRACE = 0.0
FIXED_VARIANT: tuple[float, float] = (FIXED_ACTIVATION, FIXED_RETRACE)

TRAILING_VARIANTS: tuple[tuple[float, float], ...] = tuple(
    (a, r) for a in ACTIVATION_RATIOS for r in RETRACE_RATIOS
)
# Контрольный идёт ПЕРВЫМ намеренно: он же — точка отсчёта всех сравнений §5.
VARIANTS: tuple[tuple[float, float], ...] = (FIXED_VARIANT, *TRAILING_VARIANTS)

# --- Причины выхода (§6 ТЗ). ---
EXIT_TARGET = OUTCOME_TARGET
EXIT_STOP = OUTCOME_STOP
EXIT_TIMEOUT = OUTCOME_TIMEOUT
EXIT_NO_DATA = OUTCOME_NO_DATA
EXIT_TRAIL = "trail"
# ШЕСТОЕ ЗНАЧЕНИЕ, КОТОРОГО НЕТ В ПЕРЕЧНЕ §6 ТЗ, и оно там обязано быть.
# §4 ТЗ требует, чтобы контрольный вариант совпал с signal_outcomes_barrier
# ДО ПОСЛЕДНЕГО ЗНАКА. В той таблице есть строки с исходом 'ambiguous' — их
# нельзя ни выбросить (тогда совпадения не будет), ни переименовать в один из
# пяти (тогда неизвестный порядок касаний внутри бара был бы выдан за
# известный). Расхождение с ТЗ названо в отчёте.
EXIT_AMBIGUOUS = OUTCOME_AMBIGUOUS

EXIT_REASONS: tuple[str, ...] = (
    EXIT_TARGET,
    EXIT_STOP,
    EXIT_TRAIL,
    EXIT_TIMEOUT,
    EXIT_AMBIGUOUS,
    EXIT_NO_DATA,
)


@dataclass(frozen=True)
class TrailingOutcome:
    """Итог одного варианта на одной паре (сигнал, горизонт).

    Поля ``hit_at``, ``bars_to_hit``, ``net_pnl_pct``, ``mae_pct``, ``mfe_pct``,
    ``resolution``, ``bars_seen`` и ``bars_expected`` означают ровно то же, что
    в ``src.barrier.outcomes.BarrierOutcome``: иначе сравнение вариантов между
    собой и с Этапом 8.8 было бы сравнением разных величин.
    """

    activation_ratio: float
    retrace_ratio: float
    exit_reason: str
    hit_at: datetime | None
    bars_to_hit: int | None
    net_pnl_pct: float | None
    peak_pct: float
    mae_pct: float
    mfe_pct: float
    resolution: str
    bars_seen: int
    bars_expected: int


def is_fixed(activation_ratio: float, retrace_ratio: float) -> bool:
    """Контрольный ли это вариант (фиксированная цель, правило 8.8)."""
    return float(activation_ratio) == FIXED_ACTIVATION and (
        float(retrace_ratio) == FIXED_RETRACE
    )


def variant_label(activation_ratio: float, retrace_ratio: float) -> str:
    """Человекочитаемое имя варианта для отчётов и журналов."""
    if is_fixed(activation_ratio, retrace_ratio):
        return "фиксированная цель"
    return f"A={float(activation_ratio):.2f} R={float(retrace_ratio):.2f}"


def activation_price(
    price_at_signal: float, target_pct: float, activation_ratio: float, direction: str
) -> float:
    """Цена уровня включения: доля ``A`` пути до цели, в ту же сторону.

    Считается ЧЕРЕЗ ``outcomes.levels`` с уменьшенной целью, а не своей
    формулой: уровень включения обязан лежать на той же прямой, что и цель, и
    единственный способ гарантировать это — считать его тем же кодом.
    """
    level, _stop = levels(
        price_at_signal, float(target_pct) * float(activation_ratio), 1.0, direction
    )
    return level


def trail_price(
    price_at_signal: float, peak_price: float, retrace_ratio: float, direction: str
) -> float:
    """Цена выхода по откату: возврат на долю ``R`` пройденного пути к вершине."""
    price = float(price_at_signal)
    peak = float(peak_price)
    if direction == BUY:
        return peak - float(retrace_ratio) * (peak - price)
    return peak + float(retrace_ratio) * (price - peak)


def move_pct(price_at_signal: float, price: float, direction: str) -> float:
    """Ход от цены входа к ``price`` В ПОЛЬЗУ сигнала, в процентах."""
    base = float(price_at_signal)
    delta = (float(price) - base) / base * 100.0
    return delta if direction == BUY else -delta


def trail_net_pnl(peak_pct: float, retrace_ratio: float, cost_pct: float) -> float:
    """Итог подвижного выхода: ``(1 − R) × вершина − издержки``.

    Это ТОЧНОЕ равенство, а не приближение: выход стоит на уровне, отстоящем от
    вершины на долю ``R`` пройденного пути, поэтому пройденной остаётся ровно
    доля ``1 − R``. Равенство проверяется отдельно (``deploy/verify_8_10.sh``):
    если оно перестанет выполняться, значит, выход посчитан не там, где сказано.
    """
    return (1.0 - float(retrace_ratio)) * float(peak_pct) - float(cost_pct)


@dataclass
class _VariantState:
    """Состояние одного подвижного варианта на проходе по свечам."""

    activation_ratio: float
    retrace_ratio: float
    activation_level: float
    result: TrailingOutcome | None = None


def _timeout_or_no_data(
    prefix: list[Bar], total: int
) -> str:
    """Исход, когда ни одна граница не задета: ``timeout`` либо ``no_data``.

    Объявить ``timeout`` можно ТОЛЬКО если непрерывный отрезок покрыл всё окно:
    иначе выход мог случиться там, где ряда нет. Правило дословно повторяет
    Этап 8.8 — иначе доли ``no_data`` у вариантов разошлись бы не по существу,
    а по трактовке разрыва.
    """
    return EXIT_TIMEOUT if len(prefix) >= total else EXIT_NO_DATA


def _from_barrier(
    outcome: object, *, peak_pct: float
) -> TrailingOutcome:
    """Контрольный вариант: ``BarrierOutcome`` → ``TrailingOutcome`` без пересчёта.

    Ни одно число здесь не вычисляется заново — они просто переносятся. Именно
    поэтому контрольная строка обязана совпасть с ``signal_outcomes_barrier``
    до последнего знака: это ОДИН И ТОТ ЖЕ расчёт, а не повторённый.
    """
    return TrailingOutcome(
        activation_ratio=FIXED_ACTIVATION,
        retrace_ratio=FIXED_RETRACE,
        exit_reason=outcome.outcome,  # type: ignore[attr-defined]
        hit_at=outcome.hit_at,  # type: ignore[attr-defined]
        bars_to_hit=outcome.bars_to_hit,  # type: ignore[attr-defined]
        net_pnl_pct=outcome.net_pnl_pct,  # type: ignore[attr-defined]
        peak_pct=peak_pct,
        mae_pct=outcome.mae_pct,  # type: ignore[attr-defined]
        mfe_pct=outcome.mfe_pct,  # type: ignore[attr-defined]
        resolution=outcome.resolution,  # type: ignore[attr-defined]
        bars_seen=outcome.bars_seen,  # type: ignore[attr-defined]
        bars_expected=outcome.bars_expected,  # type: ignore[attr-defined]
    )


def resolve_all(
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
    variants: tuple[tuple[float, float], ...] = VARIANTS,
) -> list[TrailingOutcome]:
    """Все варианты на ОДНОМ окне свечей, за ОДИН проход по нему (§7 ТЗ).

    Окно читается вызывающим кодом один раз на пару; здесь оно обходится один
    раз на все подвижные варианты сразу. Тринадцать отдельных обходов дали бы
    тринадцатикратное чтение базы — при ~460 тысячах строк это разница между
    минутами и часами.

    Контрольный вариант считается ОТДЕЛЬНЫМ вызовом ``outcomes.resolve`` по уже
    прочитанному отрезку. Это второй проход ПО ПАМЯТИ и он намеренный: точность
    совпадения с Этапом 8.8 важнее экономии на списке из тысячи чисел, а чтения
    базы он не добавляет ни одного.
    """
    if direction not in (BUY, SELL):
        raise ValueError(f"неизвестное направление: {direction}")

    _target_price, stop_price = levels(price_at_signal, target_pct, stop_pct, direction)
    first_ts, last_ts = window_bounds(signal_ts, horizon_h, resolution)
    total = expected_bars(horizon_h, resolution)
    price = float(price_at_signal)

    # Границы окна и обрезка по разрыву — тем же кодом, что в 8.8: окно у всех
    # тринадцати вариантов обязано быть одним и тем же отрезком ряда.
    window = [b for b in bars if first_ts <= b.ts <= last_ts]
    prefix = contiguous_prefix(window, first_ts, resolution)

    # mae/mfe — по ВСЕМУ наблюдённому окну, как в 8.8: они описывают окно, а не
    # сделку, и у всех тринадцати вариантов обязаны быть одинаковыми.
    barrier = resolve(
        bars,
        signal_ts=signal_ts,
        horizon_h=horizon_h,
        price_at_signal=price,
        target_pct=target_pct,
        stop_pct=stop_pct,
        cost_pct=cost_pct,
        direction=direction,
        resolution=resolution,
    )

    selected_trailing = [v for v in variants if not is_fixed(*v)]
    states = [
        _VariantState(
            activation_ratio=a,
            retrace_ratio=r,
            activation_level=activation_price(price, target_pct, a, direction),
        )
        for a, r in selected_trailing
    ]

    # Вершина, ИЗВЕСТНАЯ ДО ТЕКУЩЕГО БАРА (см. заголовок модуля, пункт 1).
    peak_price: float | None = None
    # Вершина по каждому бару включительно — нужна контрольному варианту, чтобы
    # ``peak_pct`` у него означал то же, что у подвижных.
    running_peak: list[float] = []

    for index, bar in enumerate(prefix, start=1):
        high = float(bar.high)
        low = float(bar.low)
        peak_known = 0.0 if peak_price is None else move_pct(price, peak_price, direction)

        for state in states:
            if state.result is not None:
                continue
            activated = peak_price is not None and (
                peak_price >= state.activation_level
                if direction == BUY
                else peak_price <= state.activation_level
            )
            if activated:
                level = trail_price(price, peak_price, state.retrace_ratio, direction)
                touched = low <= level if direction == BUY else high >= level
                if touched:
                    state.result = TrailingOutcome(
                        activation_ratio=state.activation_ratio,
                        retrace_ratio=state.retrace_ratio,
                        exit_reason=EXIT_TRAIL,
                        hit_at=bar.ts,
                        bars_to_hit=index,
                        net_pnl_pct=trail_net_pnl(
                            peak_known, state.retrace_ratio, cost_pct
                        ),
                        peak_pct=peak_known,
                        mae_pct=barrier.mae_pct,
                        mfe_pct=barrier.mfe_pct,
                        resolution=resolution,
                        bars_seen=len(prefix),
                        bars_expected=total,
                    )
                continue

            # До включения работает обычное правило (§4.3). Цель здесь не
            # проверяется намеренно: дойти до неё, не пройдя уровень включения,
            # цена не может (пункт 2 заголовка модуля).
            hit_stop = low <= stop_price if direction == BUY else high >= stop_price
            reaches_activation = (
                high >= state.activation_level
                if direction == BUY
                else low <= state.activation_level
            )
            if hit_stop and reaches_activation:
                # Порядок внутри бара неизвестен — и остаётся неизвестным.
                state.result = TrailingOutcome(
                    activation_ratio=state.activation_ratio,
                    retrace_ratio=state.retrace_ratio,
                    exit_reason=EXIT_AMBIGUOUS,
                    hit_at=None,
                    bars_to_hit=None,
                    net_pnl_pct=None,
                    peak_pct=peak_known,
                    mae_pct=barrier.mae_pct,
                    mfe_pct=barrier.mfe_pct,
                    resolution=resolution,
                    bars_seen=len(prefix),
                    bars_expected=total,
                )
            elif hit_stop:
                state.result = TrailingOutcome(
                    activation_ratio=state.activation_ratio,
                    retrace_ratio=state.retrace_ratio,
                    exit_reason=EXIT_STOP,
                    hit_at=bar.ts,
                    bars_to_hit=index,
                    net_pnl_pct=net_pnl(
                        OUTCOME_STOP,
                        target_pct=target_pct, stop_pct=stop_pct, cost_pct=cost_pct,
                        price_at_signal=price, close_at_deadline=None,
                        direction=direction,
                    ),
                    peak_pct=peak_known,
                    mae_pct=barrier.mae_pct,
                    mfe_pct=barrier.mfe_pct,
                    resolution=resolution,
                    bars_seen=len(prefix),
                    bars_expected=total,
                )

        # Вершина обновляется ПОСЛЕ всех проверок бара — в этом и состоит
        # правило «вершина внутри бара учитывается со следующего бара».
        if peak_price is None:
            peak_price = high if direction == BUY else low
        elif direction == BUY:
            peak_price = max(peak_price, high)
        else:
            peak_price = min(peak_price, low)
        running_peak.append(move_pct(price, peak_price, direction))

    # Вершина на момент выхода контрольного варианта: до его бара выхода. Для
    # timeout, ambiguous и no_data выхода по бару нет — берётся весь отрезок.
    if barrier.bars_to_hit is not None and barrier.bars_to_hit >= 2:
        fixed_peak = running_peak[barrier.bars_to_hit - 2]
    elif barrier.bars_to_hit is not None:
        fixed_peak = 0.0
    else:
        fixed_peak = running_peak[-1] if running_peak else 0.0

    results: list[TrailingOutcome] = []
    if any(is_fixed(*v) for v in variants):
        results.append(_from_barrier(barrier, peak_pct=fixed_peak))

    close_at_deadline = float(prefix[-1].close) if prefix else None
    for state in states:
        if state.result is not None:
            results.append(state.result)
            continue
        reason = _timeout_or_no_data(prefix, total)
        results.append(TrailingOutcome(
            activation_ratio=state.activation_ratio,
            retrace_ratio=state.retrace_ratio,
            exit_reason=reason,
            hit_at=None,
            bars_to_hit=None,
            net_pnl_pct=(
                net_pnl(
                    OUTCOME_TIMEOUT,
                    target_pct=target_pct, stop_pct=stop_pct, cost_pct=cost_pct,
                    price_at_signal=price, close_at_deadline=close_at_deadline,
                    direction=direction,
                )
                if reason == EXIT_TIMEOUT
                else None
            ),
            peak_pct=running_peak[-1] if running_peak else 0.0,
            mae_pct=barrier.mae_pct,
            mfe_pct=barrier.mfe_pct,
            resolution=resolution,
            bars_seen=len(prefix),
            bars_expected=total,
        ))
    return results
