"""Старшие таймфреймы: загрузка с биржи, сборка-контроль и проверки (Замер 0).

ЭТОТ МОДУЛЬ НИЧЕГО НЕ РЕШАЕТ ПРО ТОРГОВЛЮ. Он добывает дневной, недельный и
месячный ряды и доказывает, что они правильные. Ни агента старшего таймфрейма,
ни вето, ни расширения теханализа здесь нет и в этот этап не входит.

ЛОВУШКА, РАДИ КОТОРОЙ НАПИСАНА ПОЛОВИНА ЭТОГО ФАЙЛА: ЧАСОВОЙ ПОЯС СВЕЧЕЙ OKX.
У OKX дневные, недельные и месячные свечи по умолчанию открываются по
ГОНКОНГСКОМУ времени (UTC+8), а не по UTC; для UTC существуют отдельные
значения параметра ``bar`` — ``1Dutc``, ``1Wutc``, ``1Mutc``. Часовые от этого
не зависят: границы часа совпадают в обоих случаях. Значение без суффикса не
используется НИГДЕ — ни в загрузке, ни в контроле, — и попытка его записать
отбивается :func:`assert_bar_alignment`: бар, открытый в 16:00 UTC, не является
началом суток UTC, и это видно арифметически, а не на глаз. Без этой проверки
контроль §7 не сошёлся бы никогда, а причину искали бы в арифметике сборки.

ГРАНИЦЫ НЕДЕЛИ И МЕСЯЦА НЕ ПРЕДПОЛАГАЮТСЯ. В этом файле нет ни слова
«понедельник» и ни одной единицы в смысле «первое число». День недели, с
которого биржа начинает недельный бар, и число месяца, с которого начинается
месячный, ИЗМЕРЯЮТСЯ по фактическим меткам (:func:`measure_boundaries`) и
передаются в сборку значением. Если биржа однажды начнёт неделю с воскресенья,
это будет напечатано, а не молча перемолото в неверное сравнение.

ПАМЯТЬ. Сборка старшего бара из младших — ОДИН ПРОХОД ПО ВРЕМЕНИ, потоком
(:class:`BucketAggregator`). В памяти живёт ровно одно накапливаемое ведро,
сколько бы лет истории ни шло через него. Загрузка ряда в список запрещена:
лимит контейнера 1 ГБ, и на Этапе 9.1.3 часть А была убита ядром дважды, пока
не стала считать потоком.

ТОЧНОСТЬ. Всё считается в ``Decimal`` и сравнивается в ``Decimal``. Ни одного
``float`` на пути от базы до сравнения: точность хранения — ``NUMERIC(20,8)``,
и пересчёт обязан воспроизводить именно её (урок DOGE), а не приблизительно
похожее число.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

# Масштабы Замера 0. ТОЛЬКО с суффиксом utc — см. заголовок модуля.
HTF_BARS: tuple[str, ...] = ("1Dutc", "1Wutc", "1Mutc")

# Масштабы, которых в ПРОДАКШН-таблице свечей ``public.ohlcv`` быть не может.
# Продакшн собирает только 1m/5m/15m/1h (src/core/config.py: TIMEFRAMES), и
# старший масштаб там мог записать ТОЛЬКО этот этап. Это и есть признак
# происхождения, по которому граница этапа проверяется ПО СУЩЕСТВУ, а не по
# числу строк: см. :func:`boundary_violation_reasons`.
#
# Перечень тот же, что в пункте 7 deploy/verify_z0.sh, и это не совпадение:
# два разных перечня в одном этапе однажды разошлись бы молча.
PRODUCTION_FORBIDDEN_TIMEFRAMES: tuple[str, ...] = (
    "1d", "1w", "1M", "1Dutc", "1Wutc", "1Mutc",
)

# Происхождение бара, записываемое в backtest.candles.source. Старшие ряды
# ЗАГРУЖАЮТСЯ С БИРЖИ (§6 ТЗ), сборка из младших — только контроль и в таблицу
# не пишется никогда.
SOURCE_EXCHANGE = "okx"

# Часовой масштаб, из которого собирается дневной контроль. Значение то же,
# каким уже загружен ряд Этапа 7.4.
BAR_HOURLY = "1H"

# --- Допуск сравнения O/H/L/C (§7 ТЗ; пересмотрен Замером 0.2) -------------
#
# ПРАВИЛО ОДНО, И ЧИСЛА В НЁМ НЕТ. Расхождение допустимо, если его величина не
# превышает ЕДИНИЦУ ПОСЛЕДНЕГО ЗНАЧАЩЕГО ЗНАКА той стороны, которая записана
# ГРУБЕЕ. Допуск не константа: он считается для каждого поля каждого бара по
# тому, с какой точностью это поле записано, — см. :func:`write_tolerance`.
#
# ПОЧЕМУ ПРЕЖНИЕ ДВА ПРАВИЛА ЗАМЕНЕНЫ. Прогон 11.09.2026 дал 105 расхождений.
# Разбор каждого показал: 98 из них — SOL и DOGE на недельном и месячном
# масштабах, где одна сторона записана на один-два знака грубее другой, и это
# не ошибка сборки, а разная точность записи. Ни абсолютный допуск (1e-8), ни
# относительный (5e-4) на эти данные не ложатся: абсолютный недостижим при
# цене BTC в десятки тысяч и избыточен при цене DOGE в 0,0002, а относительный
# сравнивает величину отклонения с ЦЕНОЙ, тогда как измеряемое явление —
# ТОЧНОСТЬ ЗАПИСИ, и с ценой она не связана вовсе.
#
# ПРАВИЛО ПРОВЕРЕНО НА ФАКТИЧЕСКИХ ЗНАЧЕНИЯХ, а не выведено из соображений:
#
#   пример | наша     | биржа    | грубее      | допуск | отклонение | вердикт
#   SOL    | 2,54     | 2,548    | наша (2)    | 0,01   | 0,008      | поглощено
#   ETH    | 1196,00  | 1194,60  | наша (0)    | 1      | 1,40       | расхождение
#   BTC    | 7388,50  | 7131,90  | обе (1)     | 0,1    | 256,60     | расхождение
#
# Все три воспроизводятся тестами и КОНТРОЛЬНЫМ ОПЫТОМ САМОГО ПРОГОНА
# (scripts/htf_parity_z0.py, tolerance_probe): опыт меряет то же правило,
# которым считается результат, а не его копию.
#
# ЧТО ЭТО ПРАВИЛО НЕ ЛОВИТ, И ЭТО СКАЗАНО ПРЯМО. У поля, записанного обеими
# сторонами без десятичных знаков, допуск равен единице цены. Для BTC и ETH,
# единственных инструментов с такой записью в этих рядах, единица — это доли
# сотой процента; для дешёвого инструмента такая запись означала бы слепоту,
# но её там нет: SOL и DOGE записаны с 2–6 знаками. Подгонять правило под этот
# случай отдельным допуском нельзя — правило одно.
#
# ТОЛЬКО Decimal. Ни одного float на пути сравнения: отклонение 0,008 против
# допуска 0,01 на двоичной арифметике даёт ложное срабатывание. Числа
# разбираются из СТРОК прямо в Decimal.


def numeric_text(value: Decimal) -> str:
    """Текстовое представление числа — ровно то, что вернул бы ``value::text``.

    Экспоненциальная запись здесь недопустима: по ней нельзя сосчитать знаки
    после точки, а именно они и решают. ``format(value, "f")`` даёт позиционную
    запись с сохранённым масштабом — ``NUMERIC(20,8)`` приходит из asyncpg
    как ``Decimal`` с тем же масштабом, с каким лежит в базе.
    """
    return format(value, "f")


def significant_text(text: str) -> str:
    """Текст без НЕЗНАЧАЩИХ нулей справа от десятичной точки (§4.1 ТЗ).

    ``1196.00`` -> ``1196``; ``7388.50`` -> ``7388.5``; ``2.540`` -> ``2.54``.

    Нули СЛЕВА от точки не отбрасываются: ``7130`` — это ноль знаков и допуск
    1, а не 10. Решение зафиксировано ТЗ и на усмотрение кода не оставлено.
    """
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"


def significant_decimals(text: str) -> int:
    """Число ЗНАЧАЩИХ знаков после десятичной точки в текстовой записи."""
    body = significant_text(text)
    if "." not in body:
        return 0
    return len(body.split(".", 1)[1])


def write_tolerance(*texts: str) -> Decimal:
    """Допуск сравнения: единица последнего значащего знака ГРУБОЙ стороны.

    Грубее та сторона, у которой значащих знаков МЕНЬШЕ; при равенстве берётся
    это же число знаков. Аргументов ровно столько, сколько сравниваемых сторон,
    и ни одного числа в теле: допуск выводится из САМИХ ЗАПИСЕЙ.
    """
    coarse = min(significant_decimals(text) for text in texts)
    return Decimal(10) ** -coarse


OHLC_FIELDS: tuple[str, ...] = ("open", "high", "low", "close")


class HtfError(RuntimeError):
    """Нарушение посылки работы со старшими барами. Загрузка не продолжается."""


# ---------------------------------------------------------------------------
# Календарь: длина периода и его начало
# ---------------------------------------------------------------------------


def next_period_start(bar: str, opened_at: datetime) -> datetime:
    """Начало СЛЕДУЮЩЕГО бара того же масштаба — он же конец текущего.

    Для суток и недели длина постоянна. Для месяца она непостоянна, и потому
    считается календарно: тот же день месяца и то же время в следующем месяце.
    Никакого «30 суток»: месячный бар февраля короче месячного бара марта, и
    подмена этого константой сдвинула бы весь ряд.

    Значение НЕ принимается на веру: :func:`verify_period_chain` сверяет его с
    фактическим началом следующего бара по КАЖДОЙ соседней паре загруженного
    ряда. Если биржа считает месяц иначе, это выяснится при загрузке, а не в
    расхождениях контроля через две недели.
    """
    if bar == "1Dutc":
        return opened_at + timedelta(days=1)
    if bar == "1Wutc":
        return opened_at + timedelta(days=7)
    if bar == "1Mutc":
        return _add_month(opened_at)
    raise HtfError(f"неизвестный старший масштаб {bar!r}: допустимы {list(HTF_BARS)}")


def _add_month(moment: datetime) -> datetime:
    """Тот же день месяца и то же время в следующем месяце.

    День месяца больше 28 не поддерживается и вызывает отказ, а не догадку:
    у месячного бара, начинающегося 31-го числа, в феврале начала не
    существует, и любое «подходящее» правило здесь было бы выдумкой. Фактически
    измеренное биржей число месяца печатается загрузчиком; если оно окажется
    больше 28, это факт для доклада, а не повод дописать ветку.
    """
    if moment.day > 28:
        raise HtfError(
            f"месячный бар открыт {moment.day}-го числа ({moment.isoformat()}): "
            "правило перехода к следующему месяцу для такого дня не определено. "
            "Измеренное биржей число месяца доложить, а не подгонять код"
        )
    year, month = moment.year, moment.month + 1
    if month > 12:
        year, month = year + 1, 1
    return moment.replace(year=year, month=month)


def period_seconds_hint(bar: str) -> int:
    """Грубая длина периода в секундах — ТОЛЬКО для оценок и шага пагинации.

    В сравнениях и в вычислении конца бара НЕ участвует: там работает
    календарный :func:`next_period_start`. Здесь месяц взят за 31 сутки, чтобы
    оценка «сколько баров осталось» не занижала шаг.
    """
    return {"1Dutc": 86_400, "1Wutc": 604_800, "1Mutc": 2_678_400}[bar]


# ---------------------------------------------------------------------------
# Проверки, ради которых этап и существует
# ---------------------------------------------------------------------------


def assert_bar_alignment(bar: str, opened_at: datetime) -> None:
    """Метка старшего бара обязана быть полуночью UTC. Иначе — отказ.

    ЭТО И ЕСТЬ ЗАЩИТА ОТ ПОДМЕНЫ ``1Dutc`` НА ``1D`` (§3 ТЗ). Дневной бар,
    открытый по гонконгскому времени, приходится на 16:00 UTC предыдущих суток
    — то есть на полночь он не попадает ни при каком стечении обстоятельств.
    Проверка ловит это арифметически: сравнивать «на глаз» первые метки двух
    рядов пришлось бы человеку, а человек читает отчёт раз в неделю.

    Кратность суткам проверяется в СЕКУНДАХ ОТ ЭПОХИ, а не сравнением полей
    времени: так проверка не зависит от того, в каком часовом поясе создан
    объект ``datetime``, — а именно эта разница и есть предмет проверки.
    """
    if opened_at.tzinfo is None:
        raise HtfError(f"метка {bar} без часового пояса: {opened_at!r}")
    epoch = opened_at.timestamp()
    if epoch != int(epoch) or int(epoch) % 86_400 != 0:
        raise HtfError(
            f"метка бара {bar} не кратна суткам UTC: {opened_at.isoformat()} "
            f"(остаток {int(epoch) % 86_400} с). Так выглядит ряд, снятый БЕЗ "
            "суффикса utc: у OKX сутки по умолчанию открываются по Гонконгу "
            "(UTC+8), то есть в 16:00 UTC предыдущего дня"
        )


def verify_period_chain(bar: str, opens: Iterable[datetime]) -> int:
    """Сверяет вычисленный конец бара с фактическим началом следующего.

    Доказательство, а не допущение: правило длины периода из
    :func:`next_period_start` проверяется на КАЖДОЙ соседней паре фактически
    загруженного ряда. Ряд обязан быть отсортирован по возрастанию.

    Принимает ИТЕРАТОР и держит в памяти одну метку: ряд приходит из базы
    потоком, и превращать его в список ради проверки значило бы нарушить §10
    ТЗ ровно там, где проверяется его же соблюдение. Возвращает число
    проверенных пар — «ноль пар» и «все пары сошлись» обязаны различаться.
    """
    previous: datetime | None = None
    checked = 0
    for later in opens:
        if previous is not None:
            expected = next_period_start(bar, previous)
            if expected != later:
                raise HtfError(
                    f"{bar}: правило длины периода не совпало с рядом биржи. "
                    f"После {previous.isoformat()} ожидалось "
                    f"{expected.isoformat()}, биржа отдала {later.isoformat()}"
                )
            checked += 1
        previous = later
    return checked


def measure_boundaries(bar: str, opens: Iterable[datetime]) -> dict[str, Any]:
    """ИЗМЕРЯЕТ границы ряда: день недели, число месяца, время суток.

    Ничего не утверждает и ничего не требует — только считает факты, которые
    §3 ТЗ велит снять с биржи, а не вывести рассуждением. Результат
    печатается зондом и передаётся в сборку значением.
    """
    weekdays: set[int] = set()
    month_days: set[int] = set()
    times: set[str] = set()
    count = 0
    for moment in opens:
        count += 1
        weekdays.add(moment.weekday())
        month_days.add(moment.day)
        times.add(moment.strftime("%H:%M:%S"))
    return {
        "bar": bar,
        "bars_measured": count,
        # 0 — понедельник (соглашение datetime.weekday). Множество, а не одно
        # значение: если биржа непоследовательна, это обязано быть видно.
        "weekday_set": sorted(weekdays),
        "month_day_set": sorted(month_days),
        "time_of_day_set": sorted(times),
    }


def week_anchor_weekday(measured: dict[str, Any]) -> int:
    """День недели начала недельного бара — ИЗ ИЗМЕРЕНИЯ, а не из текста ТЗ."""
    days = measured.get("weekday_set") or []
    if len(days) != 1:
        raise HtfError(
            f"недельные бары начинаются в РАЗНЫЕ дни недели: {days}. "
            "Сборка недели из дневных на таком ряде невозможна — это факт "
            "для доклада, а не повод выбрать день большинством"
        )
    return int(days[0])


def month_anchor_day(measured: dict[str, Any]) -> int:
    """Число месяца начала месячного бара — ИЗ ИЗМЕРЕНИЯ, а не из текста ТЗ."""
    days = measured.get("month_day_set") or []
    if len(days) != 1:
        raise HtfError(
            f"месячные бары начинаются с РАЗНЫХ чисел месяца: {days}. "
            "Это факт для доклада, а не повод выбрать число большинством"
        )
    return int(days[0])


# ---------------------------------------------------------------------------
# Разбор ответа биржи
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedHtf:
    """Итог разбора страницы старших свечей.

    ``dropped_unconfirmed`` считается ОТДЕЛЬНО и печатается: §8 ТЗ прямо
    требует знать, сколько баров биржа вернула незакрытыми и сколько отброшено.
    Ноль отброшенных — подозрительно и подлежит докладу, а не считается
    успехом; отличить «ноль незакрытых пришло» от «фильтр не работает» можно
    только по этому числу.
    """

    rows: list[tuple[Any, ...]]
    dropped_unconfirmed: int
    seen: int


def parse_htf_candles(
    rows: Iterable[Iterable[str]], inst_id: str, bar: str
) -> ParsedHtf:
    """Разбирает ответ OKX в строки вставки. Незакрытые бары НЕ проходят.

    Формат OKX: ``[ts, open, high, low, close, vol, volCcy, volCcyQuote,
    confirm]``. Бар с ``confirm = 0`` не сохраняется НИ ПРИ КАКИХ УСЛОВИЯХ —
    это тот самый дефект, который заблокировал Этап 7.4. Отбрасывается он
    ЗДЕСЬ, до всякой записи, а не фильтром в запросе: фильтр в запросе можно
    забыть в следующем запросе, а разбор — один.

    Конец периода не берётся из ответа (биржа его не отдаёт), а считается
    календарно и затем сверяется с началом следующего бара
    (:func:`verify_period_chain`).
    """
    parsed: list[tuple[Any, ...]] = []
    dropped = 0
    seen = 0
    for row in rows:
        item = list(row)
        seen += 1
        if len(item) < 6:
            continue
        if len(item) >= 9 and str(item[8]) != "1":
            dropped += 1
            continue
        opened_at = datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC)
        assert_bar_alignment(bar, opened_at)
        parsed.append(
            (
                inst_id,
                bar,
                opened_at,
                next_period_start(bar, opened_at),
                Decimal(str(item[1])),
                Decimal(str(item[2])),
                Decimal(str(item[3])),
                Decimal(str(item[4])),
                Decimal(str(item[5])),
                Decimal(str(item[6])) if len(item) > 6 and item[6] not in ("", None)
                else None,
                SOURCE_EXCHANGE,
            )
        )
    return ParsedHtf(rows=parsed, dropped_unconfirmed=dropped, seen=seen)


# ---------------------------------------------------------------------------
# Сборка старшего бара из младших — ПОТОКОМ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceBar:
    """Младший бар на входе сборки. Только то, из чего строится старший."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class AssembledBar:
    """Старший бар, собранный из младших. ``sources`` — сколько их сложилось."""

    bar: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    sources: int
    expected_sources: int

    @property
    def is_complete(self) -> bool:
        """Все ли младшие бары периода на месте.

        Неполный период — это НЕ расхождение (§7 ТЗ). Сутки, где нет всех 24
        часов, из сравнения исключаются и попадают в отдельный счётчик: выдать
        неполноту за расхождение значило бы объявить ошибкой пропуск, который
        этап обязан просто измерить.
        """
        return self.sources == self.expected_sources


def daily_bucket_start(moment: datetime) -> datetime:
    """Начало суток UTC, которым накрыт момент."""
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_bucket_start(moment: datetime, anchor_weekday: int) -> datetime:
    """Начало недельного периода — от ИЗМЕРЕННОГО дня недели, не от понедельника."""
    day = daily_bucket_start(moment)
    return day - timedelta(days=(day.weekday() - anchor_weekday) % 7)


def monthly_bucket_start(moment: datetime, anchor_day: int) -> datetime:
    """Начало месячного периода — от ИЗМЕРЕННОГО числа месяца, не от первого."""
    day = daily_bucket_start(moment)
    if day.day >= anchor_day:
        return day.replace(day=anchor_day)
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return day.replace(year=year, month=month, day=anchor_day)


def expected_hours(_start: datetime) -> int:
    """Сколько часов в сутках. Ровно 24: границы часа в UTC не плавают."""
    return 24


def expected_days_in_week(_start: datetime) -> int:
    return 7


def expected_days_in_month(start: datetime) -> int:
    """Сколько суток в месячном периоде — календарно, а не константой."""
    return (next_period_start("1Mutc", start) - start).days


class BucketAggregator:
    """Сборка старших баров из младших ОДНИМ ПРОХОДОМ ПО ВРЕМЕНИ.

    В памяти живёт ровно одно накапливаемое ведро независимо от длины ряда —
    это и есть требование §10 ТЗ. Готовый старший бар отдаётся в тот момент,
    когда пришёл младший бар СЛЕДУЮЩЕГО периода; последний бар отдаёт
    :meth:`flush`.

    Вход обязан идти по ВОЗРАСТАНИЮ времени. Нарушение порядка — отказ, а не
    молчаливая перестановка: по неупорядоченному ряду ``open`` и ``close``
    старшего бара оказались бы взяты не у тех младших, и заметить это было бы
    нечем.
    """

    def __init__(self, bar: str, bucket_start, expected) -> None:
        self.bar = bar
        self._bucket_start = bucket_start
        self._expected = expected
        self._start: datetime | None = None
        self._open: Decimal = Decimal(0)
        self._high: Decimal = Decimal(0)
        self._low: Decimal = Decimal(0)
        self._close: Decimal = Decimal(0)
        self._volume: Decimal = Decimal(0)
        self._count = 0
        self._last_ts: datetime | None = None

    def push(self, source: SourceBar) -> AssembledBar | None:
        """Принимает младший бар. Возвращает старший, если период закрылся."""
        if self._last_ts is not None and source.ts <= self._last_ts:
            raise HtfError(
                f"сборка {self.bar}: ряд не по возрастанию времени — "
                f"{source.ts.isoformat()} после {self._last_ts.isoformat()}"
            )
        self._last_ts = source.ts
        start = self._bucket_start(source.ts)
        done: AssembledBar | None = None
        if self._start is not None and start != self._start:
            done = self._emit()
        if self._start is None or done is not None:
            self._start = start
            self._open = source.open
            self._high = source.high
            self._low = source.low
            self._volume = Decimal(0)
            self._count = 0
        else:
            self._high = max(self._high, source.high)
            self._low = min(self._low, source.low)
        self._close = source.close
        self._volume += source.volume
        self._count += 1
        return done

    def flush(self) -> AssembledBar | None:
        """Отдаёт последнее накопленное ведро. После неё сборка пуста."""
        return self._emit() if self._start is not None else None

    def _emit(self) -> AssembledBar:
        assert self._start is not None
        done = AssembledBar(
            bar=self.bar,
            open_time=self._start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
            sources=self._count,
            expected_sources=self._expected(self._start),
        )
        self._start = None
        self._count = 0
        return done


def make_aggregator(
    bar: str,
    *,
    week_weekday: int | None = None,
    month_day: int | None = None,
) -> BucketAggregator:
    """Сборщик нужного масштаба. Границы недели и месяца — ТОЛЬКО измеренные.

    Значений по умолчанию у ``week_weekday`` и ``month_day`` нет намеренно:
    подставленный здесь понедельник или первое число превратили бы измерение
    §3 ТЗ в украшение — код всё равно считал бы по своему представлению.
    """
    if bar == "1Dutc":
        return BucketAggregator(bar, daily_bucket_start, expected_hours)
    if bar == "1Wutc":
        if week_weekday is None:
            raise HtfError(
                "сборка 1Wutc требует ИЗМЕРЕННОГО дня недели начала бара "
                "(§3 ТЗ): границы недели не предполагаются"
            )
        anchor = int(week_weekday)
        return BucketAggregator(
            bar, lambda ts: weekly_bucket_start(ts, anchor), expected_days_in_week
        )
    if bar == "1Mutc":
        if month_day is None:
            raise HtfError(
                "сборка 1Mutc требует ИЗМЕРЕННОГО числа месяца начала бара "
                "(§3 ТЗ): границы месяца не предполагаются"
            )
        anchor = int(month_day)
        return BucketAggregator(
            bar, lambda ts: monthly_bucket_start(ts, anchor), expected_days_in_month
        )
    raise HtfError(f"неизвестный старший масштаб {bar!r}: допустимы {list(HTF_BARS)}")


def assemble(
    bar: str,
    sources: Iterable[SourceBar],
    *,
    week_weekday: int | None = None,
    month_day: int | None = None,
    shift: timedelta = timedelta(0),
) -> Iterator[AssembledBar]:
    """Собирает старшие бары из потока младших. Ленивая, память O(1).

    ``shift`` — КОНТРОЛЬНЫЙ ОПЫТ §7 ТЗ и больше ничего. Сдвиг окна сборки на
    час обязан дать ненулевое число расхождений; если при сдвинутом окне
    расхождений ноль, значит сравнение ничего не сравнивает, и этап не сдан.
    Параметр живёт здесь, а не в отдельной копии сборки: контрольный опыт
    обязан проверять ТУ ЖЕ функцию, которой считается результат.
    """
    aggregator = make_aggregator(bar, week_weekday=week_weekday, month_day=month_day)
    for source in sources:
        moved = source if not shift else SourceBar(
            ts=source.ts + shift,
            open=source.open, high=source.high, low=source.low,
            close=source.close, volume=source.volume,
        )
        done = aggregator.push(moved)
        if done is not None:
            yield done
    tail = aggregator.flush()
    if tail is not None:
        yield tail


# ---------------------------------------------------------------------------
# Сравнение собранного с загруженным (§7) и проверка незакрытых (§8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deviation:
    """Отклонение по одному полю одного бара. Значения — ``Decimal``.

    ТЕКСТ СТОРОНЫ — ЧАСТЬ ДАННЫХ, А НЕ ОФОРМЛЕНИЕ. Допуск считается по числу
    ЗНАЧАЩИХ ЗНАКОВ в записи, поэтому текст обязан дожить до сравнения. По
    умолчанию он выводится из самого ``Decimal`` (``NUMERIC`` приходит из базы
    с сохранённым масштабом, и ``format(value, "f")`` равен ``value::text``);
    ``*_text`` позволяет подать запись биржи ровно в том виде, в каком она
    пришла из API или файла, когда она известна точнее.
    """

    inst_id: str
    bar: str
    open_time: datetime
    field: str
    assembled: Decimal
    stored: Decimal
    assembled_text: str | None = None
    stored_text: str | None = None

    @property
    def absolute(self) -> Decimal:
        return abs(self.assembled - self.stored)

    @property
    def relative(self) -> Decimal:
        """Отклонение в долях от биржевого значения. Ноль делится на ноль в ноль.

        В решение о расхождении НЕ ВХОДИТ и входить не будет: правило одно и
        оно в :attr:`tolerance`. Величина считается для ОБЪЁМА, порог по
        которому назначается отдельно и по измерению (§7 ТЗ).
        """
        if self.stored == 0:
            return Decimal(0) if self.assembled == 0 else Decimal(1)
        return abs(self.assembled - self.stored) / abs(self.stored)

    @property
    def text_assembled(self) -> str:
        """Запись НАШЕЙ стороны в исходном текстовом виде."""
        return self.assembled_text or numeric_text(self.assembled)

    @property
    def text_stored(self) -> str:
        """Запись БИРЖЕВОЙ стороны в исходном текстовом виде."""
        return self.stored_text or numeric_text(self.stored)

    @property
    def decimals_assembled(self) -> int:
        return significant_decimals(self.text_assembled)

    @property
    def decimals_stored(self) -> int:
        return significant_decimals(self.text_stored)

    @property
    def coarse_decimals(self) -> int:
        """Число знаков ГРУБОЙ стороны — той, у которой их меньше."""
        return min(self.decimals_assembled, self.decimals_stored)

    @property
    def tolerance(self) -> Decimal:
        """Допуск для ЭТОГО поля ЭТОГО бара. Считается, а не берётся из константы."""
        return write_tolerance(self.text_assembled, self.text_stored)

    @property
    def is_mismatch(self) -> bool:
        """Расхождение по правилу Замера 0.2.

        Сравнение СТРОГОЕ: равенство границе допустимо (§4.4 ТЗ). Отклонение
        ровно в единицу последнего знака — это и есть разная точность записи,
        и засчитывать его расхождением значило бы вернуть прежний шум.
        """
        return self.absolute > self.tolerance

    @property
    def is_precision_difference(self) -> bool:
        """Стороны записаны с РАЗНОЙ точностью.

        Признак не участвует в вердикте и нужен ровно для одного: назвать в
        сводке, сколько поглощённых отклонений объясняется точностью записи, а
        сколько — чем-то ещё. Смешать эти два числа значило бы спрятать второе.
        """
        return self.decimals_assembled != self.decimals_stored


def compare_bar(
    inst_id: str, assembled: AssembledBar, stored: dict[str, Any]
) -> tuple[list[Deviation], Deviation | None]:
    """Сверяет собранный бар с загруженным: (расхождения O/H/L/C, объём).

    Расхождение считается по правилу Замера 0.2 (:attr:`Deviation.is_mismatch`):
    отклонение превысило ЕДИНИЦУ ПОСЛЕДНЕГО ЗНАЧАЩЕГО ЗНАКА грубой стороны.
    Числа здесь нет: допуск выводится из самих записей.

    Значения передаются ``Decimal``-ами КАК ЕСТЬ, без промежуточного ``str()``:
    масштаб ``NUMERIC`` обязан дожить до сравнения, а по пути через ``str()``
    мелкие величины уходят в экспоненциальную запись, по которой знаки после
    точки уже не сосчитать.

    Объём возвращается ОТДЕЛЬНО и в перечень расхождений не попадает: на первом
    прогоне он не блокирующий (§7 ТЗ), и смешать его с O/H/L/C значило бы
    заблокировать этап округлением на стороне биржи.
    """
    mismatches: list[Deviation] = []
    for field in OHLC_FIELDS:
        deviation = Deviation(
            inst_id=inst_id, bar=assembled.bar, open_time=assembled.open_time,
            field=field,
            assembled=getattr(assembled, field),
            stored=stored[field],
        )
        if deviation.is_mismatch:
            mismatches.append(deviation)
    volume = Deviation(
        inst_id=inst_id, bar=assembled.bar, open_time=assembled.open_time,
        field="volume",
        assembled=assembled.volume,
        stored=stored["volume"],
    )
    return mismatches, volume


@dataclass(frozen=True)
class StoredBar:
    """Сохранённый бар в том виде, в каком его проверяет §8."""

    inst_id: str
    bar: str
    open_time: datetime
    close_time: datetime


def unclosed_rows(
    rows: Iterable[StoredBar], *, now: datetime, settle: int
) -> list[StoredBar]:
    """Сохранённые бары, период которых ЕЩЁ НЕ ЗАКРЫЛСЯ с запасом (§8 ТЗ).

    Правило: конец периода обязан быть не позже ``now - settle``. Запас
    ``settle`` берётся вызывающим кодом из существующей
    ``src.barrier.runner.settle_seconds`` — здесь он ПАРАМЕТР именно поэтому:
    вторая копия формулы запаса однажды разошлась бы с первой (урок Этапа
    8.10.1), и разошлась бы молча.

    Список, а не счётчик: увидеть, КАКОЙ бар не закрылся, важнее, чем узнать,
    что их сколько-то.
    """
    edge = now - timedelta(seconds=settle)
    return [row for row in rows if row.close_time > edge]


def boundary_violation_reasons(
    *,
    before: dict[str, int] | None,
    after: dict[str, int] | None,
) -> list[str]:
    """Нарушена ли граница этапа. Пустой список — не нарушена (§13.10 ТЗ).

    ПОЧЕМУ ЗДЕСЬ НЕТ НИ ОДНОГО СЧЁТЧИКА СТРОК. Прежняя проверка сравнивала
    ``count(*)`` продакшн-таблиц до и после загрузки. На стенде исполнителя
    продакшн не запущен, и она работала; на боевой машине он работает 24/7, и
    она срабатывала ВСЕГДА. Прогоны 11.09.2026 оба закончились кодом 6 с
    «продакшн-таблицы изменились», и оба раза ложно: прямой опыт с паузой
    60 секунд, когда не было запущено ничего, дал signals +5, agent_outputs
    +15, ohlcv +5, open_interest +5. За 6 мин 15 с загрузки набежало +30 и +90,
    ровно по этому темпу, — при том что загрузчик записал 75 417 строк В СВОЮ
    схему. Разница в три порядка. Проверка не отличала «загрузчик залез не
    туда» от «система живёт», то есть не означала ничего.

    ЧЕМ ЗАМЕНЕНО. Признаком ПРОИСХОЖДЕНИЯ строки: в ``public.ohlcv`` нет и не
    может быть старших масштабов (:data:`PRODUCTION_FORBIDDEN_TIMEFRAMES`) —
    продакшн их не собирает вовсе. Такую строку мог записать только этот этап.
    Образец взят из пункта 7 deploy/verify_z0.sh, где он уже работает.

    ``None`` — проверить было НЕЧЕМ (продакшн-таблицы нет). Это НЕ «нарушений
    нет», и молчать об этом нельзя: «не проверено» и «чисто» — разные исходы.
    """
    if before is None or after is None:
        return [
            "границу этапа проверить НЕЧЕМ: продакшн-таблицы ohlcv нет. "
            "Это НЕ «нарушений нет» — вывод о неприкосновенности продакшна "
            "сделать не на чем"
        ]
    reasons: list[str] = []
    appeared = {
        scale: count - before.get(scale, 0)
        for scale, count in after.items()
        if count - before.get(scale, 0) > 0
    }
    if appeared:
        reasons.append(
            f"в public.ohlcv ПОЯВИЛИСЬ строки со старшим масштабом: {appeared}. "
            "Такую строку мог записать только этот этап — это запись вне схемы "
            "backtest (§1 ТЗ)"
        )
    stale = {
        scale: count for scale, count in after.items()
        if count > 0 and scale not in appeared
    }
    if stale:
        reasons.append(
            f"в public.ohlcv УЖЕ ЛЕЖАЛИ строки со старшим масштабом: {stale}. "
            "Этот прогон их не писал, но граница этапа нарушена раньше, и "
            "умолчать об этом значило бы засчитать нарушение за норму"
        )
    return reasons
