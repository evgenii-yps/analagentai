"""Правила ведения одной позиции (§4, §7.1 ТЗ 9.1). ЧИСТЫЙ модуль.

Ни базы, ни сети, ни времени «сейчас» внутри функций: всё состояние приходит
параметрами. Это условие проверяемости — правило на синтетических рядах с
заранее известным ответом, а не на том, что случайно оказалось в базе.

ЧТО ЗДЕСЬ ОПИСАНО, и чего здесь нет. Здесь — когда позицию можно открыть, по
каким уровням её вести и чем она заканчивается. Здесь НЕТ ни одного решения
системы: ни порогов агентов, ни весов, ни состава кворума. Этап добавляет
наблюдение рядом, а не вмешательство внутрь.

ЧЕТЫРЕ ПРАВИЛА, КОТОРЫЕ ЛЕГКО НАРУШИТЬ НЕЗАМЕТНО (те же, что в
``src/barrier/outcomes.py``, и совпадать они обязаны):

 1. «ЗАДЕТА» — ЭТО КАСАНИЕ ВНУТРИ СВЕЧИ, по ``high`` и ``low``, а не по цене
    закрытия. Реальный ордер срабатывает по касанию. Касание РОВНО В УРОВЕНЬ
    засчитывается: сравнение нестрогое.
 2. СВЕЧА МОМЕНТА ВХОДА В ОКНО НЕ ВХОДИТ. Позиция открывается по закрытию
    своего бара, и этот бар уже прожит — искать в нём касание значило бы
    искать событие, случившееся до входа.
 3. ОДНОВРЕМЕННОЕ КАСАНИЕ НЕ РАЗРЕШАЕТСЯ ДОГАДКОЙ. Задеты обе границы в одном
    баре — исход ``ambiguous``, ``outcome_certain = False``, а ВЫХОД СЧИТАЕТСЯ
    ПО ПРЕДЕЛУ. Порядок событий внутри минуты ряду свечей неизвестен; выбор в
    свою пользу тихо завысил бы результат системы, выбор против себя завысить
    не может.
 4. ``mae_pct`` и ``mfe_pct`` СЧИТАЮТСЯ ПО ВСЕМУ УДЕРЖАННОМУ ОКНУ — от бара
    после входа до бара выхода включительно, — а не до момента касания. Они
    описывают, что позиция пережила, и именно поэтому по ним можно будет
    ответить на вопрос «что было бы при другом уровне предела».

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ ВОВСЕ: не усредняет позицию и не докупает, не
двигает уровни за ценой, не отправляет ордера, не открывает позиции на продажу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Направление. Спот: продажа — это продажа того, чего нет, поэтому значение
# ровно одно, и оно же записано ограничением positions_side_chk.
SIDE_BUY = "buy"

# Четыре причины выхода. Перечень ЗАКРЫТ и повторён ограничением БД
# (positions_reason_chk): причина, названная своими словами, не считается
# причиной — её нельзя посчитать запросом.
EXIT_TARGET = "target"
EXIT_STOP = "stop"
EXIT_TIMEOUT = "timeout"
EXIT_AMBIGUOUS = "ambiguous"

EXIT_REASONS = (EXIT_TARGET, EXIT_STOP, EXIT_TIMEOUT, EXIT_AMBIGUOUS)

# ПРИЧИНЫ ОТКАЗА ВО ВХОДЕ — тоже закрытый перечень машиночитаемых ключей, а не
# свободный текст. Свободный текст нельзя посчитать запросом, а знать, ПОЧЕМУ
# позиций мало, придётся: «позиций нет» и «позиций нет, потому что ни один
# сигнал не прошёл порог вероятности» — разные ответы.
REASON_OK = "ok"
REASON_NOT_BUY = "not_buy"
REASON_WRONG_LOGIC_VERSION = "wrong_logic_version"
REASON_DEGRADED = "degraded"
REASON_LOW_PROBABILITY = "low_probability"
REASON_INSTRUMENT_BUSY = "instrument_busy"
REASON_SLOTS_FULL = "slots_full"
REASON_SIGNAL_TOO_OLD = "signal_too_old"
REASON_NO_FRESH_BAR = "no_fresh_bar"
REASON_NO_FROZEN_TARGET = "no_frozen_target"

REFUSAL_REASONS = (
    REASON_NOT_BUY,
    REASON_WRONG_LOGIC_VERSION,
    REASON_DEGRADED,
    REASON_LOW_PROBABILITY,
    REASON_INSTRUMENT_BUSY,
    REASON_SLOTS_FULL,
    REASON_SIGNAL_TOO_OLD,
    REASON_NO_FRESH_BAR,
    REASON_NO_FROZEN_TARGET,
)


@dataclass(frozen=True)
class Bar:
    """Свеча ряда. ``ts`` — время ОТКРЫТИЯ бара, как в ``ohlcv`` и у ccxt."""

    ts: datetime
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class OpenDecision:
    """Ответ отбора: пускать ли в позицию и, если нет, по какой причине.

    ``reason`` — ключ из закрытого перечня выше, в том числе при ``allowed``:
    строка ``ok`` позволяет считать журнал одним запросом, не разделяя случаи
    «причина есть» и «причины нет».
    """

    allowed: bool
    reason: str


@dataclass(frozen=True)
class ExitDecision:
    """Чем закончилась позиция.

    ``exit_bar_ts`` — время ОТКРЫТИЯ бара выхода, а не точный момент касания:
    точнее ряд свечей не знает, и вид точности, которой нет, здесь не создаётся.
    """

    exit_reason: str
    exit_price: float
    exit_bar_ts: datetime
    outcome_certain: bool
    bars_held: int
    mae_pct: float
    mfe_pct: float


def levels(
    entry_price: float, target_pct: float, stop_pct: float
) -> tuple[float, float]:
    """Цены цели и предела от фактической цены входа. Возвращает ``(цель, предел)``.

    Уровни считаются от ``entry_price``, а НЕ от ``signals.price``: купить по
    прошлой цене нельзя, и цель, отложенная от цены, по которой сделки не было,
    отвечала бы на вопрос о несуществующей позиции.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if stop_pct <= 0:
        raise ValueError(f"предел должен быть положительным: {stop_pct}")
    price = float(entry_price)
    return (
        price * (1.0 + float(target_pct) / 100.0),
        price * (1.0 - float(stop_pct) / 100.0),
    )


def qty_for_slot(slot_usd: float, entry_price: float) -> float:
    """Количество базовой монеты на слот.

    ОКРУГЛЕНИЯ К ШАГУ ЛОТА БИРЖИ ЗДЕСЬ НЕТ НАМЕРЕННО. Позиции виртуальные, а
    выдуманное округление к неизвестному шагу было бы точностью, которой нет:
    шаг лота у каждого инструмента свой, читается он у биржи, и подставить
    вместо него правдоподобное число значило бы записать в таблицу догадку,
    неотличимую от измерения. Шаг лота появится вместе с настоящими ордерами.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if slot_usd <= 0:
        raise ValueError(f"слот должен быть положительным: {slot_usd}")
    return float(slot_usd) / float(entry_price)


def slippage_pct(signal_price: float, entry_price: float) -> float:
    """Насколько цена входа разошлась с ценой решения, в процентах.

    Самостоятельный результат этапа: величина измеряет, сколько стоит задержка
    между решением и входом. До Этапа 9.1 это число в проекте не измерялось ни
    разу.
    """
    if signal_price <= 0:
        raise ValueError(f"цена решения должна быть положительной: {signal_price}")
    return (float(entry_price) / float(signal_price) - 1.0) * 100.0


def net_pnl(entry_price: float, exit_price: float, cost_pct: float) -> float:
    """Итог сделки в процентах, ПОСЛЕ вычета круговых издержек.

    Издержки вычитаются ровно ОДИН раз: ``cost_pct`` — уже круговая величина
    (комиссия тейкера × 2 плюс проскальзывание × 2, см. RISK_COST_ROUNDTRIP_PCT),
    и второе вычитание «за выход» посчитало бы одну и ту же комиссию дважды.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    return (float(exit_price) / float(entry_price) - 1.0) * 100.0 - float(cost_pct)


def should_open(
    *,
    decision: str,
    logic_version: int,
    expected_version: int,
    degraded: bool,
    probability: float | None,
    min_probability: float,
    has_open_position: bool,
    open_count: int,
    max_open: int,
    signal_age_sec: float,
    max_signal_age_sec: int,
    bar_age_sec: float | None,
    max_bar_age_sec: int,
    has_frozen_target: bool,
) -> OpenDecision:
    """Девять условий §4.1, все одновременно. Первый несработавший — и есть отказ.

    ПОЧЕМУ ``degraded`` ДОСТАТОЧНО ВМЕСТО ПЕРЕСЧЁТА СОСТАВА АГЕНТОВ. Признак
    ``signals.degraded = FALSE`` и означает полный кворум трёх агентов: его
    ставит сам decision в момент решения. Пересчитывать состав агентов заново
    значило бы вводить ВТОРОЕ определение кворума, которое однажды разойдётся
    с первым.

    СОЗНАТЕЛЬНОЕ ОТЛИЧИЕ ОТ УВЕДОМЛЕНИЙ. Отбор НЕ смотрит на то, ушёл ли сигнал
    в Telegram. У уведомлений своя защита от потока (выдержка NOTIFY_HOLD_MIN и
    потолок NOTIFY_MAX_PER_HOUR), она бережёт внимание человека, а не моделирует
    торговлю. Привязка позиций к отправке внесла бы в замер ограничения, не
    имеющие отношения к рынку: позиция не открылась бы потому, что человеку уже
    написали три раза за час.

    ФИЛЬТРА ПО ``is_repeat`` ЗДЕСЬ НЕТ, и это не упущение: правило «один
    инструмент — одна позиция» уже исключает повторные входы, а лишнее условие
    пришлось бы объяснять — и объяснить его было бы нечем.

    ПОРЯДОК ПРОВЕРОК СОДЕРЖАТЕЛЕН. Свойства самого сигнала идут раньше свойств
    окружения: причина «сигнал не подходил» точнее причины «не было слота»,
    когда верны обе. Иначе журнал показывал бы забитые слоты там, где на самом
    деле не было ни одного годного сигнала.
    """
    if decision != SIDE_BUY:
        return OpenDecision(False, REASON_NOT_BUY)
    if int(logic_version) != int(expected_version):
        return OpenDecision(False, REASON_WRONG_LOGIC_VERSION)
    if degraded:
        return OpenDecision(False, REASON_DEGRADED)
    if probability is None or float(probability) < float(min_probability):
        return OpenDecision(False, REASON_LOW_PROBABILITY)
    if float(signal_age_sec) > float(max_signal_age_sec):
        return OpenDecision(False, REASON_SIGNAL_TOO_OLD)
    if not has_frozen_target:
        return OpenDecision(False, REASON_NO_FROZEN_TARGET)
    if has_open_position:
        return OpenDecision(False, REASON_INSTRUMENT_BUSY)
    if int(open_count) >= int(max_open):
        return OpenDecision(False, REASON_SLOTS_FULL)
    if bar_age_sec is None or float(bar_age_sec) > float(max_bar_age_sec):
        return OpenDecision(False, REASON_NO_FRESH_BAR)
    return OpenDecision(True, REASON_OK)


def _touches(bar: Bar, target_price: float, stop_price: float) -> tuple[bool, bool]:
    """Задеты ли уровни ВНУТРИ бара. Сравнение НЕСТРОГОЕ (§4.4)."""
    return (bar.high >= target_price, bar.low <= stop_price)


def check_exit(
    *,
    bars: list[Bar],
    target_price: float,
    stop_price: float,
    entry_price: float,
    deadline_at: datetime,
    cost_pct: float,
) -> ExitDecision | None:
    """Разбор баров позиции по одному, по возрастанию времени.

    ``None`` означает «позиция ещё открыта, ни одно условие не наступило»: бары
    кончились раньше срока и ни один уровень не задет. Это НЕ исход и записью
    закрытия быть не может.

    ``bars`` — только ЗАКРЫТЫЕ бары СТРОГО ПОСЛЕ бара входа, по возрастанию
    времени открытия. Отбор баров — дело вызывающего (он один знает, что такое
    «закрытый» с учётом задержки коллектора); правило же обязано оставаться
    чистым, иначе его нельзя проверить на синтетике.

    ``cost_pct`` в подсчёте mae/mfe НЕ участвует: издержки — свойство сделки, а
    крайние отклонения описывают окно. Он принимается параметром, чтобы вызов
    выглядел одинаково с ``net_pnl`` и никто не искал, где же вычитаются
    издержки, — вычитаются они в ``net_pnl``, ровно один раз.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")

    mae_pct = 0.0
    mfe_pct = 0.0
    bars_held = 0
    last_before_deadline: Bar | None = None
    reached_deadline = False

    for bar in bars:
        # 1. Срок наступил: бары окна кончились. Итог — по закрытию последнего
        #    бара, открывшегося РАНЬШЕ срока (пункт 5 §4.4).
        if bar.ts >= deadline_at:
            reached_deadline = True
            break

        bars_held += 1
        last_before_deadline = bar
        # mae/mfe — по ВСЕМУ удержанному окну, включая бар выхода.
        mfe_pct = max(mfe_pct, (bar.high / entry_price - 1.0) * 100.0)
        mae_pct = min(mae_pct, (bar.low / entry_price - 1.0) * 100.0)

        hit_target, hit_stop = _touches(bar, target_price, stop_price)

        # 2. Задеты ОБА уровня: порядок внутри минуты неизвестен, итог берётся
        #    по пределу — пессимистично, и это помечается флагом.
        if hit_target and hit_stop:
            return ExitDecision(
                exit_reason=EXIT_AMBIGUOUS,
                exit_price=float(stop_price),
                exit_bar_ts=bar.ts,
                outcome_certain=False,
                bars_held=bars_held,
                mae_pct=mae_pct,
                mfe_pct=mfe_pct,
            )
        # 3. Только цель.
        if hit_target:
            return ExitDecision(
                exit_reason=EXIT_TARGET,
                exit_price=float(target_price),
                exit_bar_ts=bar.ts,
                outcome_certain=True,
                bars_held=bars_held,
                mae_pct=mae_pct,
                mfe_pct=mfe_pct,
            )
        # 4. Только предел.
        if hit_stop:
            return ExitDecision(
                exit_reason=EXIT_STOP,
                exit_price=float(stop_price),
                exit_bar_ts=bar.ts,
                outcome_certain=True,
                bars_held=bars_held,
                mae_pct=mae_pct,
                mfe_pct=mfe_pct,
            )

    # 5. Срок наступил, ничего не задето. Требуется, чтобы бар срока был
    #    ПРЕДЪЯВЛЕН: пока его нет, неизвестно, кончились бары или просто ещё не
    #    подъехали, а разница между «истёк срок» и «данных пока нет» — это
    #    разница между исходом и его отсутствием.
    #    Если баров окна нет вовсе (сплошной пробел в ряду), выход НЕ
    #    выдумывается: цены, по которой позиция закрылась бы, не существует, и
    #    ``None`` здесь честнее любого числа.
    if reached_deadline and last_before_deadline is not None:
        return ExitDecision(
            exit_reason=EXIT_TIMEOUT,
            exit_price=float(last_before_deadline.close),
            exit_bar_ts=last_before_deadline.ts,
            outcome_certain=True,
            bars_held=bars_held,
            mae_pct=mae_pct,
            mfe_pct=mfe_pct,
        )
    return None
