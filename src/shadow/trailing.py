"""Теневой подвижный выход на ОДНОЙ фактической позиции. ЧИСТЫЙ модуль (§5.2 ТЗ).

Ни базы, ни сети, ни ``datetime.now()`` внутри: всё состояние приходит
параметрами. Это условие проверяемости — правило проверяется на придуманных
рядах с заранее известным ответом, а не на том, что случайно оказалось в базе.

ЧТО ЗДЕСЬ СЧИТАЕТСЯ И ЧЕГО ЗДЕСЬ НЕТ

Считается тринадцать исходов ОДНОЙ позиции на ОДНОМ и том же ряде свечей:
контрольный (живое правило) и двенадцать подвижных (сетка A × R Этапа 8.10).
Ни одно правило здесь не написано заново — оба берутся вызовом:

  * КОНТРОЛЬ — ``src.positions.rules.check_exit``, ровно та функция, которой
    закрыты настоящие позиции. Именно поэтому её результат ОБЯЗАН совпасть с
    тем, что записано в ``positions``: это один и тот же расчёт, а не
    повторённый. Не совпал — сравнение вариантов недействительно целиком
    (§3.3 ТЗ), и это то же блокирующее правило, что поймало дефект чтения
    незакрытой свечи 28.08.2026.
  * ДВЕНАДЦАТЬ ПОДВИЖНЫХ — ``src.trailing.rule.resolve_all``, та функция,
    которой посчитана ``trailing_outcomes`` Этапа 8.10. Своё определение A и R
    написать было нельзя: тогда часть А и часть Б замера перестали бы быть об
    одном и том же механизме.

ПОЧЕМУ ``resolve_all`` УДАЛОСЬ ВЫЗВАТЬ БЕЗ ПРАВКИ ``src/trailing``. §3.2 ТЗ
допускает вынос функции наружу, если её сигнатура не позволяет прогнать
произвольный ряд. Выноса не потребовалось: ``resolve_all`` уже принимает ряд
баров параметром и уже чистая. Ни одна строка ``src/trailing/*`` этим этапом не
изменена.

ЕДИНСТВЕННОЕ, ЧТО ПРИШЛОСЬ СОГЛАСОВАТЬ, — ГРАНИЦЫ ОКНА, и они не подгоняются, а
ПРОВЕРЯЮТСЯ. ``resolve_all`` строит окно от бара РЕШЕНИЯ: усекает ``signal_ts``
вниз до сетки баров и берёт следующие ``horizon_h`` часов. Позиция же ведётся от
бара ПОСЛЕ входа до бара срока, причём ``opened_at`` — это время закрытия бара
входа, оно же время открытия первого бара окна. Значит эквивалентом «момента
решения» для позиции служит ``opened_at`` минус один бар. Совпадение границ
после этого не предполагается, а сверяется в :func:`check_window`, и расхождение
роняет расчёт. Молчаливый сдвиг окна на один бар — это разные сделки в двух
половинах сравнения, и заметить его по числам было бы невозможно.

ЧТО ТАКОЕ ``armed`` И ПОЧЕМУ ОН СЧИТАЕТСЯ ЗДЕСЬ, А НЕ БЕРЁТСЯ ИЗ ПРАВИЛА

Главное число части Б — не средний прирост, а сколько позиций механизм ВООБЩЕ
ЗАДЕЛ (§3.4 ТЗ). Подвижная цель поднимает пол под уже полученной прибылью;
сделка, ушедшая против сигнала до предела убытка, до неё не доживает, и цель ни
разу не сдвинется. Если механизм не задел ни одной позиции, средний прирост
равен нулю ПО ПОСТРОЕНИЮ — и это и есть ответ владельцу, а не отсутствие
ответа.

``TrailingOutcome`` признака задетости не несёт, а выводить его из причины
выхода нельзя: позиция может включить подвижный выход и всё равно досидеть до
срока, так и не откатившись. Поэтому задетость считается здесь отдельным
проходом — но уровень включения берётся ВСЁ ТОЙ ЖЕ
``rule.activation_price``, а не своей формулой. Согласованность двух проходов
не декларируется, а проверяется двумя инвариантами в :func:`_check_invariants`:

  * выход ``trail`` невозможен без задетости;
  * выход ``stop`` невозможен при задетости — уровень отката при положительной
    вершине лежит выше цены входа, а предел ниже, и цена, дошедшая до предела,
    обязана была пройти уровень отката раньше.

Нарушение любого из них означает, что два прохода разошлись, и расчёт падает,
а не печатает правдоподобные числа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.barrier.outcomes import (
    BAR_SECONDS,
    BUY,
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_DATA,
    OUTCOME_TIMEOUT,
    Bar,
    contiguous_prefix,
    window_bounds,
)
from src.positions import rules as position_rules
from src.trailing import rule as trailing_rule

# Имя контрольного варианта в таблице. Строка, а не пара нулей, как в 8.10:
# первичный ключ здесь (position_id, variant), и параметров у контроля нет.
CONTROL_VARIANT = "control"

# Исходы, у которых бара выхода НЕ СУЩЕСТВУЕТ. У 'ambiguous' порядок событий
# внутри минуты неизвестен, у 'no_data' ряда нет вовсе. Подставить сюда число
# значило бы выдать невычисленное за вычисленное.
UNMEASURED_REASONS: frozenset[str] = frozenset(
    {OUTCOME_AMBIGUOUS, OUTCOME_NO_DATA}
)


@dataclass(frozen=True)
class ShadowOutcome:
    """Один вариант выхода на одной позиции — строка ``position_trailing_shadow``."""

    variant: str
    activation_frac: float | None
    pullback_frac: float | None
    armed: bool
    armed_at: datetime | None
    exit_reason: str
    exit_bar_ts: datetime | None
    exit_price: float | None
    net_pnl_pct: float | None
    net_pnl_usd: float | None
    bars_used: int
    resolution: str


@dataclass(frozen=True)
class PositionShadow:
    """Все варианты одной позиции.

    ``control`` равен ``None``, когда живое правило на этом ряде исхода НЕ ДАЁТ
    (ряд оборвался раньше срока). Это не ноль и не «как записано»: воспроизвести
    записанный исход не удалось, и вызывающий обязан считать такую позицию
    расхождением контроля, а не пропустить её молча.
    """

    control: ShadowOutcome | None
    variants: list[ShadowOutcome]

    @property
    def rows(self) -> list[ShadowOutcome]:
        """Все строки к записи: контроль первым, если он есть."""
        return ([] if self.control is None else [self.control]) + list(self.variants)


def variant_name(activation_ratio: float, retrace_ratio: float) -> str:
    """Имя варианта: ``A0.25_R0.20`` — сперва A, затем R, по два знака (§4 ТЗ)."""
    return f"A{float(activation_ratio):.2f}_R{float(retrace_ratio):.2f}"


def bar_step(resolution: str) -> timedelta:
    """Длина бара. Разрешение неизвестно — ошибка, а не молчаливая минута."""
    if resolution not in BAR_SECONDS:
        raise ValueError(f"неизвестное разрешение: {resolution}")
    return timedelta(seconds=BAR_SECONDS[resolution])


def decision_ts_for_position(opened_at: datetime, resolution: str) -> datetime:
    """Эквивалент «момента решения» для позиции: ``opened_at`` минус один бар.

    ``opened_at`` — время ЗАКРЫТИЯ бара входа, оно же время ОТКРЫТИЯ первого
    бара окна (так его ставит ``src/positions/runner.py``). ``resolve_all``
    строит окно от бара решения и первым берёт СЛЕДУЮЩИЙ за ним. Значит бар
    решения — тот, что открылся на один шаг раньше ``opened_at``.

    Число это не подгоняется под ответ: :func:`check_window` сверяет полученное
    окно с окном позиции и роняет расчёт при расхождении.
    """
    return opened_at - bar_step(resolution)


def check_window(
    *,
    opened_at: datetime,
    deadline_at: datetime,
    horizon_h: int,
    resolution: str,
) -> tuple[datetime, datetime]:
    """Сверяет окно ``resolve_all`` с окном позиции. Возвращает ``(первый, последний)``.

    ОКНО ПОЗИЦИИ — от ``opened_at`` включительно до бара, открывшегося СТРОГО
    раньше ``deadline_at`` (``check_exit`` прерывается на ``bar.ts >=
    deadline_at``). Окно ``resolve_all`` считается от бара решения. Совпасть они
    обязаны бар в бар, иначе контроль и подвижные варианты считались бы по
    разным отрезкам ряда — и разница между ними была бы разницей окон, а не
    правил.

    Расхождение — ``ValueError``, а не предупреждение: замер, посчитанный по
    съехавшему окну, выглядит совершенно правдоподобно.
    """
    step = bar_step(resolution)
    first, last = window_bounds(
        decision_ts_for_position(opened_at, resolution), horizon_h, resolution
    )
    if first != opened_at:
        raise ValueError(
            "окно подвижного правила начинается не с бара после входа: "
            f"{first.isoformat()} вместо {opened_at.isoformat()}"
        )
    expected_last = deadline_at - step
    if last != expected_last:
        raise ValueError(
            "окно подвижного правила кончается не на баре перед сроком: "
            f"{last.isoformat()} вместо {expected_last.isoformat()} "
            f"(deadline_at={deadline_at.isoformat()}, horizon_h={horizon_h})"
        )
    return first, last


def window_prefix(
    bars: list[Bar], *, opened_at: datetime, deadline_at: datetime, resolution: str
) -> list[Bar]:
    """Непрерывный отрезок окна позиции — ТОЙ ЖЕ функцией, что в 8.8 и 8.10.

    ``contiguous_prefix`` обрезает ряд по первому разрыву: по неполному окну
    считается не тот исход, а ДРУГОЙ. Правило позиций разрывов не обрезает —
    оно идёт по тем барам, что ему дали, — и это НЕ приведено к общему виду
    намеренно: приводить пришлось бы, изменив одно из двух живых правил.
    Расхождение проявляется только на позициях с дырой в ряду, и такие позиции
    получают у подвижных вариантов исход ``no_data``, то есть остаются видны.
    """
    step = bar_step(resolution)
    window = [b for b in bars if opened_at <= b.ts <= deadline_at - step]
    return contiguous_prefix(window, opened_at, resolution)


def armed_scan(
    prefix: list[Bar],
    *,
    entry_price: float,
    target_pct: float,
    activation_ratio: float,
    direction: str = BUY,
) -> tuple[bool, datetime | None]:
    """Задет ли механизм и на каком баре впервые (§3.4 ТЗ, ЧИСЛО 1).

    ЗАДЕТОСТЬ — ЭТО «ВЕРШИНА ДОШЛА ДО ПОРОГА ВКЛЮЧЕНИЯ», по касанию внутри бара,
    нестрого — тем же сравнением, каким ``resolve_all`` проверяет
    ``reaches_activation``. Уровень берётся ``rule.activation_price``, а не
    своей формулой: он обязан лежать на той же прямой, что и цель.

    ``armed_at`` — бар, на котором порог ДОСТИГНУТ. Действовать на это правило
    начинает со СЛЕДУЮЩЕГО бара (внутри одного бара порядок ``high`` и ``low``
    неизвестен, см. заголовок ``src/trailing/rule.py``), и разница в один бар
    здесь намеренная: вопрос владельца — «задел ли механизм сделку», а не «с
    какого бара он ею управлял».
    """
    level = trailing_rule.activation_price(
        entry_price, target_pct, activation_ratio, direction
    )
    for bar in prefix:
        reached = (
            float(bar.high) >= level if direction == BUY else float(bar.low) <= level
        )
        if reached:
            return True, bar.ts
    return False, None


def exit_price_from_pnl(
    entry_price: float, net_pnl_pct: float, cost_pct: float
) -> float:
    """Цена выхода из итога — обращение ``rules.net_pnl``, а не своя арифметика.

    ``net_pnl = (exit / entry − 1) × 100 − cost`` обращается однозначно, и одна
    формула на все причины выхода лучше трёх частных случаев: для ``stop`` она
    даёт ровно уровень предела, для ``timeout`` — цену закрытия последнего бара,
    для ``trail`` — уровень отката. Частные случаи разошлись бы поодиночке.
    """
    return float(entry_price) * (
        1.0 + (float(net_pnl_pct) + float(cost_pct)) / 100.0
    )


def _usd(net_pnl_pct: float | None, notional_usd: float) -> float | None:
    """Итог в долларах от ФАКТИЧЕСКОГО слота позиции (§3.2 ТЗ)."""
    if net_pnl_pct is None:
        return None
    return float(notional_usd) * float(net_pnl_pct) / 100.0


def _check_invariants(outcome: ShadowOutcome) -> None:
    """Два следствия правила, проверяемые на каждой строке (см. заголовок).

    Их нарушение означает, что проход задетости разошёлся с самим правилом.
    Тогда падаем: правдоподобные числа, посчитанные разошедшимися проходами,
    хуже отсутствия чисел.
    """
    if outcome.exit_reason == trailing_rule.EXIT_TRAIL and not outcome.armed:
        raise AssertionError(
            f"{outcome.variant}: выход trail при armed=false — "
            "подсчёт задетости разошёлся с правилом"
        )
    if outcome.exit_reason == trailing_rule.EXIT_STOP and outcome.armed:
        raise AssertionError(
            f"{outcome.variant}: выход stop при armed=true — после включения "
            "подвижного выхода предел сработать не может"
        )


def resolve_position(
    bars: list[Bar],
    *,
    opened_at: datetime,
    deadline_at: datetime,
    horizon_h: int,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    notional_usd: float,
    resolution: str,
    direction: str = BUY,
    variants: tuple[tuple[float, float], ...] = trailing_rule.TRAILING_VARIANTS,
) -> PositionShadow:
    """Тринадцать исходов одной позиции на одном ряде свечей.

    ``bars`` — закрытые бары по возрастанию ``ts``, начиная не позже
    ``opened_at``. Отбор закрытых баров — дело вызывающего: только он знает,
    что такое «закрытый» с учётом задержки коллектора (``settle_seconds``).

    ИЗДЕРЖКИ ВЫЧИТАЮТСЯ РОВНО ОДИН РАЗ И НЕ ЗДЕСЬ: ``cost_pct`` уходит
    параметром в оба правила, и каждое вычитает его своим штатным способом.
    Своей арифметики издержек в этом модуле нет ни одной строки.
    """
    if direction != BUY:
        # Спот: позиции только на покупку (positions_side_chk). Продажа сюда
        # попасть не может, и молча посчитать её «как-нибудь» нельзя.
        raise ValueError(f"позиции ведутся только на покупку, получено: {direction}")

    check_window(
        opened_at=opened_at, deadline_at=deadline_at,
        horizon_h=horizon_h, resolution=resolution,
    )
    prefix = window_prefix(
        bars, opened_at=opened_at, deadline_at=deadline_at, resolution=resolution
    )

    control = _resolve_control(
        bars,
        opened_at=opened_at, deadline_at=deadline_at, entry_price=entry_price,
        target_pct=target_pct, stop_pct=stop_pct, cost_pct=cost_pct,
        notional_usd=notional_usd, resolution=resolution,
    )

    outcomes = trailing_rule.resolve_all(
        bars,
        signal_ts=decision_ts_for_position(opened_at, resolution),
        horizon_h=horizon_h,
        price_at_signal=entry_price,
        target_pct=target_pct,
        stop_pct=stop_pct,
        cost_pct=cost_pct,
        direction=direction,
        resolution=resolution,
        variants=variants,
    )

    last_bar_ts = prefix[-1].ts if prefix else None
    rows: list[ShadowOutcome] = []
    for item in outcomes:
        armed, armed_at = armed_scan(
            prefix,
            entry_price=entry_price, target_pct=target_pct,
            activation_ratio=item.activation_ratio, direction=direction,
        )
        # У timeout бара касания нет — есть бар, чьё закрытие стало ценой
        # выхода. Это последний бар непрерывного отрезка, тот же, что берёт
        # живое правило позиций.
        exit_bar_ts = item.hit_at
        if item.exit_reason == OUTCOME_TIMEOUT:
            exit_bar_ts = last_bar_ts
        net_pct = item.net_pnl_pct
        row = ShadowOutcome(
            variant=variant_name(item.activation_ratio, item.retrace_ratio),
            activation_frac=float(item.activation_ratio),
            pullback_frac=float(item.retrace_ratio),
            armed=armed,
            armed_at=armed_at,
            exit_reason=item.exit_reason,
            exit_bar_ts=None if net_pct is None else exit_bar_ts,
            exit_price=(
                None if net_pct is None
                else exit_price_from_pnl(entry_price, net_pct, cost_pct)
            ),
            net_pnl_pct=net_pct,
            net_pnl_usd=_usd(net_pct, notional_usd),
            bars_used=item.bars_seen,
            resolution=resolution,
        )
        _check_invariants(row)
        rows.append(row)

    return PositionShadow(control=control, variants=rows)


def _resolve_control(
    bars: list[Bar],
    *,
    opened_at: datetime,
    deadline_at: datetime,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    notional_usd: float,
    resolution: str,
) -> ShadowOutcome | None:
    """Контроль — ПРЯМОЙ вызов живого правила позиций, без единого пересчёта.

    Ряд подаётся ровно тот же, что подаёт ``src/positions/runner.py``: от
    ``opened_at`` включительно до бара срока. Уровни считаются
    ``rules.levels`` от фактической цены входа — той же функцией, что считала
    их при открытии.
    """
    target_price, stop_price = position_rules.levels(
        entry_price, target_pct, stop_pct
    )
    window = [
        position_rules.Bar(
            ts=b.ts, high=float(b.high), low=float(b.low), close=float(b.close)
        )
        for b in bars
        if b.ts >= opened_at
    ]
    decision = position_rules.check_exit(
        bars=window,
        target_price=target_price,
        stop_price=stop_price,
        entry_price=entry_price,
        deadline_at=deadline_at,
        cost_pct=cost_pct,
    )
    if decision is None:
        return None
    net_pct = position_rules.net_pnl(entry_price, decision.exit_price, cost_pct)
    return ShadowOutcome(
        variant=CONTROL_VARIANT,
        activation_frac=None,
        pullback_frac=None,
        # У контроля подвижного выхода нет вовсе, поэтому задетости быть не
        # может по определению — это записано и ограничением БД.
        armed=False,
        armed_at=None,
        exit_reason=decision.exit_reason,
        exit_bar_ts=decision.exit_bar_ts,
        exit_price=float(decision.exit_price),
        net_pnl_pct=net_pct,
        net_pnl_usd=_usd(net_pct, notional_usd),
        bars_used=decision.bars_held,
        resolution=resolution,
    )
