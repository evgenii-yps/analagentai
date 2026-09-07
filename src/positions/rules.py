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

ПЯТЫЙ ИСХОД, ``data_gap`` (Этап 9.1.1 §6), стоит особняком от остальных
четырёх: те описывают, ЧЕМ КОНЧИЛАСЬ позиция, а он — то, что кончить её
измерением не удалось. Ряд свечей прервался, срок истёк, и позиция закрыта по
последней известной цене, чтобы не занимать слот вечно. В статистику точности и
прибыльности такие закрытия не идут: выдуманная цена не должна входить в
результат системы наравне с измеренными.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ ВОВСЕ: не усредняет позицию и не докупает, не
двигает уровни за ценой, не отправляет ордера, не открывает позиции на продажу.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

# Направление. Спот: продажа — это продажа того, чего нет, поэтому значение
# ровно одно, и оно же записано ограничением positions_side_chk.
SIDE_BUY = "buy"

# Пять причин выхода. Перечень ЗАКРЫТ и повторён ограничением БД
# (positions_reason_chk, миграции 018 и 019): причина, названная своими
# словами, не считается причиной — её нельзя посчитать запросом.
EXIT_TARGET = "target"
EXIT_STOP = "stop"
EXIT_TIMEOUT = "timeout"
EXIT_AMBIGUOUS = "ambiguous"
# ПРОБЕЛ В ДАННЫХ (Этап 9.1.1 §6). Не исход рынка, а признание того, что исход
# НЕ ИЗМЕРЕН: ряд свечей прервался, срок истёк, и позиция закрыта по последней
# известной цене, чтобы не занимать слот вечно. В статистику точности и
# прибыльности такие закрытия НЕ ИДУТ — см. bot/queries.positions_summary.
EXIT_DATA_GAP = "data_gap"
# ВЫХОД В ЧИСТЫЙ ПЛЮС ПОСЛЕ СУТОК ОЖИДАНИЯ (Этап 9.2 §5.1). Появляется вместе с
# версией логики 6 и только у неё: правило версии 5 такого выхода не знает и
# знать не может — у него предел убытка есть, а вторых суток нет.
EXIT_PLUS = "plus_exit"

EXIT_REASONS = (
    EXIT_TARGET, EXIT_STOP, EXIT_TIMEOUT, EXIT_AMBIGUOUS, EXIT_DATA_GAP,
    EXIT_PLUS,
)

# --- Версия 6: без предела убытка, ожидание плюса до 48 часов (Этап 9.2) -----
#
# ВЕРСИЯ, С КОТОРОЙ ПРАВИЛО ВЫХОДА МЕНЯЕТСЯ. Число стоит ЗДЕСЬ, в чистом
# модуле правил, а не в настройках, и это не оплошность: настройку можно
# поменять в ``.env``, а граница версий — свойство КОДА. Версии 5 и 6 ведутся
# разными правилами, и «с какой версии не стало предела» обязано быть таким же
# неизменяемым фактом, как и сам перечень исходов. То же число повторено
# ограничением БД ``positions_no_stop_chk`` (миграция 025).
PLUS_WAIT_MIN_LOGIC_VERSION = 6

# ЦЕНА ПРЕДЕЛА, КОТОРАЯ НЕ СРАБАТЫВАЕТ НИКОГДА. Отключить предел в
# ``check_exit`` можно ровно одним способом — ценой, ниже которой минимум бара
# не опускается. Ноль годится при ЕДИНСТВЕННОЙ посылке: ни один бар ряда не
# имеет неположительного минимума. Посылка проверяется
# :func:`assert_no_stop`, а не предполагается.
#
# ЗНАЧЕНИЕ ТО ЖЕ, ЧТО У ЗАМЕРА 9.1.4 И 9.1.6 (``NO_STOP_PRICE`` в
# ``scripts/stop_counterfactual_9_1_4.py``), и совпадение это проверяется
# тестом: два числа для одного и того же смысла однажды разошлись бы молча.
NO_STOP_PRICE = 0.0

# ТОЧНОСТЬ ХРАНЕНИЯ ЦЕНЫ: ``positions.entry_price``, ``target_price``,
# ``stop_price`` и ``exit_price`` — NUMERIC(20,8).
PRICE_STORAGE_PLACES = 8

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
# Этап 9.1.1 §5: свободных денег меньше размера слота.
#
# ПРИ ДЕЙСТВУЮЩИХ ЗНАЧЕНИЯХ (бюджет 10, слот 2, слотов 5) ЭТА ПРИЧИНА НЕ
# СРАБОТАЕТ НИКОГДА: деньги и слоты кончаются одновременно. Это не повод её не
# ставить. Ограждение ставится не ради сегодняшних чисел, а ради того дня,
# когда слот или бюджет изменят — и разница станет содержательной раньше, чем
# кто-нибудь вспомнит, что её надо было предусмотреть.
REASON_NO_FREE_CAPITAL = "no_free_capital"

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
    REASON_NO_FREE_CAPITAL,
)


# --- СЧЁТЧИК ОТКАЗОВ ВО ВХОДЕ (§7.2 ТЗ 9.2) ---------------------------------
#
# Ключ несёт ВЕРСИЮ ЛОГИКИ и СУТКИ: без версии счётчики версий 5 и 6 сложились
# бы в одно число, а сравнивать их запрещено (§1.4 ТЗ); без суток счётчик рос
# бы вечно и отвечал бы на вопрос «сколько всего за всё время», который никому
# не нужен.
#
# ИМЯ КЛЮЧА ЖИВЁТ В ЧИСТОМ МОДУЛЕ, а не в сервисе, потому что собирают его
# ДВОЕ: сервис позиций (пишет) и бот (читает). Две одинаковые строки в двух
# файлах однажды разошлись бы, и счётчики просто перестали бы находиться —
# молча, без единой ошибки, показывая честный ноль.
REFUSAL_KEY_TEMPLATE = "positions:refused:{version}:{day}:{reason}"
# Счётчики живут неделю — столько же, сколько окно, за которое бот показывает
# итог по позициям. Вечный ключ в Redis это мусор, который однажды придётся
# объяснять.
REFUSAL_TTL_SEC = 7 * 24 * 3600


def refusal_key(version: int, day: str, reason: str) -> str:
    """Имя ключа счётчика отказов. Одно место, а не литерал в двух сервисах."""
    return REFUSAL_KEY_TEMPLATE.format(
        version=int(version), day=day, reason=reason
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


def to_storage(value: float, places: int = PRICE_STORAGE_PLACES) -> float:
    """Приведение числа к точности колонки ТЕМ ЖЕ СПОСОБОМ, КАКИМ ЭТО ДЕЛАЕТ БАЗА.

    ФУНКЦИЯ ПЕРЕЕХАЛА СЮДА ИЗ ЗАМЕРНОГО СКРИПТА 9.1.6 (§2.2 ТЗ 9.2), и переехала
    целиком, а не была переписана: ``scripts/plus_exit_counterfactual_9_1_6.py``
    теперь ввозит её отсюда. Вторая копия одного и того же округления однажды
    разошлась бы с первой — и разошлась бы молча, потому что оба числа выглядят
    одинаково правдоподобно.

    СПОСОБ ОКРУГЛЕНИЯ ВЗЯТ ИЗ БАЗЫ, А НЕ ИЗ ГОЛОВЫ. PostgreSQL округляет NUMERIC
    **половиной ОТ НУЛЯ**: ``0.000000005::numeric(20,8)`` даёт ``0.00000001``, а
    ``-0.000000005`` — ``-0.00000001``. Встроенный ``round()`` Python округляет
    ПОЛОВИНОЙ К ЧЁТНОМУ и на том же числе даёт ``0.0``, то есть расходится с
    базой ровно на половинах (§9.5 ТЗ 9.2 запрещает его прямо). Поэтому здесь
    ``Decimal`` с ``ROUND_HALF_UP``.

    Путь значения повторён целиком: ``_num`` в ``src/core/db.py`` делает
    ``Decimal(str(float(value)))``, и уже это число округляет колонка.
    """
    quantum = Decimal(1).scaleb(-int(places))
    return float(
        Decimal(str(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)
    )


def storage_tick(places: int = PRICE_STORAGE_PLACES) -> float:
    """Единица последнего знака хранения: 1e-8 для цены, 1e-6 для процентов."""
    return float(Decimal(1).scaleb(-int(places)))


def breakeven_price(
    entry_price: float, cost_pct: float, *, side: str = SIDE_BUY,
    gross: bool = False,
) -> float:
    """ЦЕНА БЕЗУБЫТКА: цена, при которой итог сделки равен нулю (§3.6 ТЗ 9.1.6).

    Для покупки ``Pbe = P0 × (1 + C)``, для продажи ``Pbe = P0 × (1 − C)``, где
    ``C`` — ставка КРУГОВЫХ издержек той же позиции. Формула для продажи
    записана здесь не про запас: она сразу говорит, что «плюс» у противоположного
    направления лежит по другую сторону цены входа, и молча посчитать продажу по
    формуле покупки было бы нельзя.

    ``gross=True`` — «плюс» ВАЛОВОЙ, ``Pbe = P0``. В бою не используется (§0 ТЗ
    9.2: внедряется вариант B, у которого плюс ЧИСТЫЙ); параметр сохранён,
    потому что замерный скрипт 9.1.6 считает этой же функцией и вариант D, и
    убрать его значило бы переписать замер.

    ОКРУГЛЕНИЕ ОБЯЗАТЕЛЬНО, И ОНО ЖЕ — ОКРУГЛЕНИЕ ХРАНЕНИЯ. Цена выхода обязана
    лежать в той же точности, что и все прочие цены позиции, иначе итог
    ``plus_exit`` считался бы по более точной цене, чем итог ``target``, и два
    исхода мерились бы разными линейками.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if cost_pct < 0:
        raise ValueError(f"издержки не могут быть отрицательными: {cost_pct}")
    if gross:
        # Валовой безубыток — сама цена входа. Она ПРИШЛА ИЗ КОЛОНКИ
        # NUMERIC(20,8) и уже лежит в точности хранения; округлять её нечего.
        return float(entry_price)
    rate = float(cost_pct) / 100.0
    if side == SIDE_BUY:
        raw = float(entry_price) * (1.0 + rate)
    elif side == "sell":
        raw = float(entry_price) * (1.0 - rate)
    else:
        raise ValueError(f"неизвестное направление: {side!r}")
    return to_storage(raw, PRICE_STORAGE_PLACES)


def target_price_of(entry_price: float, target_pct: float) -> float:
    """Цена цели от фактической цены входа. БЕЗ предела и без упоминания о нём.

    ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ, КОГДА ЕСТЬ :func:`levels` (§4.3 ТЗ 9.2). У версии 6
    предела убытка нет вовсе, а ``levels`` отвергает ``stop_pct <= 0`` — и
    отвергать обязана: эта проверка бережёт версию 5, у которой предел есть
    всегда, и ослабить её значило бы разрешить записать «предел 0%» как
    настоящий уровень (§9.4 ТЗ прямо это запрещает).

    Обойти проверку подстановкой правдоподобного ``stop_pct`` было бы худшим из
    решений: вычисленный, но никогда не срабатывающий предел — это данные,
    которые лгут (§4.1 ТЗ). Поэтому цель версии 6 считается ЗДЕСЬ, а ``levels``
    остаётся нетронутой и по-прежнему считает ОБА уровня сразу для версии 5 —
    ТОЙ ЖЕ формулой, что и здесь (это сверяется тестом).
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    return float(entry_price) * (1.0 + float(target_pct) / 100.0)


def levels(
    entry_price: float, target_pct: float, stop_pct: float
) -> tuple[float, float]:
    """Цены цели и предела от фактической цены входа. Возвращает ``(цель, предел)``.

    Уровни считаются от ``entry_price``, а НЕ от ``signals.price``: купить по
    прошлой цене нельзя, и цель, отложенная от цены, по которой сделки не было,
    отвечала бы на вопрос о несуществующей позиции.

    ПРОВЕРКА ``stop_pct <= 0`` ОСТАЁТСЯ И ОСЛАБЛЕНИЮ НЕ ПОДЛЕЖИТ (§9.4 ТЗ 9.2).
    Версия 6 эту функцию просто НЕ ЗОВЁТ: её цель считает
    :func:`target_price_of`, а предела у неё нет.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if stop_pct <= 0:
        raise ValueError(f"предел должен быть положительным: {stop_pct}")
    price = float(entry_price)
    return (
        target_price_of(price, target_pct),
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
    free_capital_usd: float,
    slot_usd: float,
) -> OpenDecision:
    """Десять условий, все одновременно. Первый несработавший — и есть отказ.

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

    ПРОВЕРКА ДЕНЕГ (Этап 9.1.1 §5) СТОИТ ПОСЛЕ СЛОТОВ И ДО СВЕЖЕСТИ БАРА, и
    порядок этот содержателен. После слотов — потому что «слоты заняты» более
    точная причина, когда верны обе: занятые слоты и есть то, во что ушли
    деньги. До свежести бара — потому что свежесть свечи есть свойство ДАННЫХ,
    и проверять её раньше денег значило бы объяснять отсутствие позиции
    состоянием рынка там, где на самом деле кончились деньги.

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
    # Сравнение НЕСТРОГОЕ в пользу входа: свободных денег ровно на слот —
    # значит, слот оплачен целиком, и отказ здесь был бы отказом при
    # достаточных деньгах.
    if float(free_capital_usd) < float(slot_usd):
        return OpenDecision(False, REASON_NO_FREE_CAPITAL)
    if bar_age_sec is None or float(bar_age_sec) > float(max_bar_age_sec):
        return OpenDecision(False, REASON_NO_FRESH_BAR)
    return OpenDecision(True, REASON_OK)


def check_gap_exit(
    *,
    bars: list[Bar],
    entry_price: float,
    deadline_at: datetime,
    now: datetime,
    grace_sec: int,
) -> ExitDecision | None:
    """Закрытие позиции при ПРОБЕЛЕ В ДАННЫХ (§6 ТЗ 9.1.1). ЧИСТАЯ функция.

    ЗАЧЕМ ОНА ВООБЩЕ ЕСТЬ. ``check_exit`` возвращает ``None`` в двух случаях:
    бар срока не предъявлен (неизвестно, кончилось окно или данные ещё не
    подъехали) и бар срока есть, а баров окна до срока нет вовсе. Оба поведения
    правильные — правило не выдумывает цену, которой не было. Но выхода из
    этого состояния не существовало ни одного: прервись ряд свечей по
    инструменту, позиция осталась бы открытой НАВСЕГДА и заняла бы слот
    навсегда.

    ЧТО ЗДЕСЬ ПРОИСХОДИТ И ЧЕГО ЗДЕСЬ НЕ ПРОИСХОДИТ. Здесь позиция закрывается
    по ПОСЛЕДНЕЙ ИЗВЕСТНОЙ цене — и это НЕ ИЗМЕРЕНИЕ ИСХОДА, а признание того,
    что исход измерить не удалось. Поэтому ``outcome_certain`` равен ``False``
    ВСЕГДА, без исключений: цена выхода не наблюдалась, она восстановлена.
    Такие закрытия исключаются из средних и сумм (см.
    ``bot/queries.positions_summary``), иначе выдуманная цена вошла бы в
    результат системы наравне с измеренными.

    ПОЧЕМУ ПРИ ПОЛНОМ ОТСУТСТВИИ БАРОВ ЦЕНА ВЫХОДА РАВНА ЦЕНЕ ВХОДА. Другой
    цены попросту нет: взять её неоткуда, а выдумать — значит записать в
    таблицу догадку, неотличимую от измерения. Итог сделки при этом равен ровно
    минус издержкам, и это честное описание сделки, которую невозможно оценить.

    ``None`` означает «ещё рано»: срок не истёк или запас не выдержан. Звать эту
    функцию раньше срока нельзя и незачем.

    Ни базы, ни ``datetime.now()`` внутри: «сейчас» приходит параметром — иначе
    правило нельзя было бы проверить на заранее известном ответе.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if grace_sec < 0:
        raise ValueError(f"запас не может быть отрицательным: {grace_sec}")

    # Срок плюс запас ещё не наступил — ждём. Нестрогое сравнение в пользу
    # ожидания: ровно на границе данные ещё могут подъехать.
    if now < deadline_at + timedelta(seconds=int(grace_sec)):
        return None

    # Бары окна — только те, что открылись СТРОГО РАНЬШЕ срока. Бар с меткой
    # ровно deadline_at принадлежит уже следующему окну: он открывается в
    # момент срока, а закрывается после него.
    seen = [bar for bar in bars if bar.ts < deadline_at]

    if not seen:
        return ExitDecision(
            exit_reason=EXIT_DATA_GAP,
            exit_price=float(entry_price),
            exit_bar_ts=deadline_at,
            outcome_certain=False,
            bars_held=0,
            mae_pct=0.0,
            mfe_pct=0.0,
        )

    mfe_pct = 0.0
    mae_pct = 0.0
    for bar in seen:
        mfe_pct = max(mfe_pct, (bar.high / entry_price - 1.0) * 100.0)
        mae_pct = min(mae_pct, (bar.low / entry_price - 1.0) * 100.0)
    last = seen[-1]
    return ExitDecision(
        exit_reason=EXIT_DATA_GAP,
        exit_price=float(last.close),
        exit_bar_ts=last.ts,
        outcome_certain=False,
        bars_held=len(seen),
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
    )


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


def assert_no_stop(bars: list[Bar]) -> None:
    """Единственная посылка эквивалентности «предела нет» ≡ ``stop_price = 0``.

    Проверяется, а не предполагается: бар с неположительным минимумом сделал бы
    сравнение ``bar.low <= 0`` истинным, и «правило без предела» тихо
    превратилось бы в правило с пределом на нуле.

    Та же проверка и то же рассуждение, что в ``scripts/stop_counterfactual_9_1_4``
    (Этап 9.1.4); здесь она стоит на БОЕВОМ пути, а не только на замерном.
    """
    for bar in bars:
        if float(bar.low) <= 0.0:
            raise ValueError(
                "правило без предела неприменимо к ряду с неположительным "
                f"минимумом бара: ts={bar.ts.isoformat()}, low={bar.low}"
            )


def _extremes(
    bars: list[Bar], entry_price: float, until_ts: datetime
) -> tuple[int, float, float]:
    """``(число баров, mae_pct, mfe_pct)`` по окну от входа до бара ``until_ts``.

    Считается по ВСЕМУ удержанному окну, включая бар выхода (правило 4 в шапке
    модуля), — тем же способом, каким это делает :func:`check_exit`, только за
    два прохода правила сразу: у версии 6 их два, и сложить mae/mfe двух вызовов
    почленно нельзя — минимум окна не равен минимуму из двух минимумов, взятых
    от разных начал отсчёта. От одного и того же ``entry_price`` — равен, и
    именно поэтому окно пересчитывается здесь одним проходом, а не собирается
    из кусков.
    """
    held = 0
    mae_pct = 0.0
    mfe_pct = 0.0
    for bar in bars:
        if bar.ts > until_ts:
            break
        held += 1
        mfe_pct = max(mfe_pct, (bar.high / entry_price - 1.0) * 100.0)
        mae_pct = min(mae_pct, (bar.low / entry_price - 1.0) * 100.0)
    return held, mae_pct, mfe_pct


def check_exit_plus_wait(
    *,
    bars: list[Bar],
    target_price: float,
    plus_price: float,
    entry_price: float,
    plus_start_at: datetime,
    deadline_at: datetime,
    cost_pct: float,
) -> ExitDecision | None:
    """ПРАВИЛО ВЫХОДА ВЕРСИИ 6: предела нет, цель все 48 часов, плюс — с 24-го.

    ЭТО НЕ НОВОЕ ПРАВИЛО, А ТРИ ВЫЗОВА СТАРОГО (§2.1 ТЗ 9.2). Каждая ветка ниже
    — это ``check_exit`` с другими параметрами; своего разбора баров здесь нет
    ни одной строки. Написать собственный обход значило бы завести ВТОРОЕ
    определение того, что такое «задета», и однажды они разошлись бы.

    ЧТО ИМЕННО СЧИТАЕТСЯ, по фазам:

     1. ``[t0, plus_start_at)`` — только цель, предела нет. Всё, что не
        ``timeout``, — окончательный исход. ``timeout`` здесь означает лишь
        «первые сутки прожиты», а не выход: он открывает вторую фазу.
     2. ``[plus_start_at, deadline_at]`` — ДВА вызова того же ``check_exit`` по
        тому же ряду: один с уровнем цели, другой с ценой безубытка, оба без
        предела. ``high >= X`` — это ровно условие касания ЦЕЛИ в ``check_exit``,
        поэтому «дошла до плюса» выражается вызовом с ``target_price = plus_price``.
        Первый вызов называет бар цели, второй — бар плюса; при совпадении
        побеждает ЦЕЛЬ (нестрогое сравнение меток).
     3. Ни цель, ни плюс не достигнуты — ``timeout`` по закрытию последнего
        бара, открывшегося раньше срока; цену берёт тот же ``check_exit``.

    ЦЕНА ВЫХОДА ПРИ ``plus_exit`` — ЦЕНА БЕЗУБЫТКА, А НЕ ЗАКРЫТИЕ БАРА. Разница
    не косметическая: закрытие бара, на котором цена задела безубыток, может
    быть и выше, и НИЖЕ его, и подстановка закрытия превратила бы «выход в ноль»
    в лотерею. Следствие проверяемое: ``net_pnl`` такой сделки равен нулю по
    построению.

    ``None`` означает «исхода нет»: бар границы фаз или бар срока не предъявлен.
    Это НЕ исход и записью закрытия быть не может — из этого состояния выводит
    :func:`check_gap_exit`, как и у версии 5.

    Ни базы, ни ``datetime.now()``: всё состояние приходит параметрами.
    """
    if entry_price <= 0:
        raise ValueError(f"цена входа должна быть положительной: {entry_price}")
    if plus_start_at >= deadline_at:
        raise ValueError(
            "ожидание плюса обязано начинаться РАНЬШЕ срока: "
            f"{plus_start_at.isoformat()} не раньше {deadline_at.isoformat()}"
        )
    assert_no_stop(bars)

    def _finish(reason: str, price: float, bar_ts: datetime) -> ExitDecision:
        held, mae_pct, mfe_pct = _extremes(bars, entry_price, bar_ts)
        return ExitDecision(
            exit_reason=reason,
            exit_price=float(price),
            exit_bar_ts=bar_ts,
            # ИСХОД ИЗМЕРЕН. Неопределённого порядка касаний у версии 6 не
            # бывает вовсе: предела нет, а цель и плюс на одном баре разводятся
            # правилом «побеждает цель», а не догадкой.
            outcome_certain=True,
            bars_held=held,
            mae_pct=mae_pct,
            mfe_pct=mfe_pct,
        )

    # --- Фаза 1: первые сутки. Бар границы фаз ПРЕДЪЯВЛЯЕТСЯ правилу ---------
    first = [bar for bar in bars if bar.ts <= plus_start_at]
    decision = check_exit(
        bars=first,
        target_price=target_price,
        stop_price=NO_STOP_PRICE,
        entry_price=entry_price,
        deadline_at=plus_start_at,
        cost_pct=cost_pct,
    )
    if decision is None:
        return None
    if decision.exit_reason != EXIT_TIMEOUT:
        # Цель на первых сутках. Иных исходов у ряда без предела нет: ни
        # ``stop``, ни ``ambiguous`` при stop_price = 0 недостижимы.
        return _finish(
            decision.exit_reason, decision.exit_price, decision.exit_bar_ts
        )

    # --- Фаза 2: ожидание возврата в плюс -----------------------------------
    second = [bar for bar in bars if bar.ts >= plus_start_at]

    def _run(level: float) -> ExitDecision | None:
        return check_exit(
            bars=second,
            target_price=level,
            stop_price=NO_STOP_PRICE,
            entry_price=entry_price,
            deadline_at=deadline_at,
            cost_pct=cost_pct,
        )

    target_run = _run(target_price)
    plus_run = _run(plus_price)

    def _hit(run: ExitDecision | None) -> datetime | None:
        if run is None or run.exit_reason != EXIT_TARGET:
            return None
        return run.exit_bar_ts

    target_ts = _hit(target_run)
    plus_ts = _hit(plus_run)

    # На одном баре выполнимы и цель, и плюс — исход ``target``: он выгоднее, и
    # выбирать между ними догадкой не приходится, потому что цель достигается
    # только ЧЕРЕЗ плюс (она лежит выше него).
    if target_ts is not None and (plus_ts is None or target_ts <= plus_ts):
        return _finish(EXIT_TARGET, target_price, target_ts)
    if plus_ts is not None:
        return _finish(EXIT_PLUS, plus_price, plus_ts)

    # Срок наступил, ничего не задето. Цена — по закрытию последнего бара,
    # открывшегося раньше срока, и берёт её тот же ``check_exit``.
    if target_run is None or target_run.exit_reason != EXIT_TIMEOUT:
        # Бар срока не предъявлен: неизвестно, кончились бары или ещё не
        # подъехали. Разница между «истёк срок» и «данных пока нет» — это
        # разница между исходом и его отсутствием.
        return None
    return _finish(
        EXIT_TIMEOUT, target_run.exit_price, target_run.exit_bar_ts
    )
