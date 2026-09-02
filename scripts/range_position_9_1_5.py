#!/usr/bin/env python3
"""ЭТАП 9.1.5: связано ли ПОЛОЖЕНИЕ ЦЕНЫ В НЕДЕЛЬНОМ РАЗМАХЕ с успехом сигнала.

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ. Владелец заметил настоящий пробел: покупка у
верхней границы недельного размаха и покупка у нижней — разные сделки, а система
их не различает. Прежде чем строить по этому различию фильтр входа, надо
узнать, есть ли там вообще связь с исходом. Здесь она измеряется, и только.

ПОЧЕМУ ЭТО ВООБЩЕ МОЖНО ИЗМЕРИТЬ СЕЙЧАС. Положение в размахе — НЕ ПРОГНОЗ, а
факт: оно считается по уже случившимся свечам. Предсказывать границы диапазона
на неделю вперёд система не умеет и в этом этапе не учится (§11 ТЗ).

ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ, И ГРАНИЦА ЖЁСТКАЯ (§1 ТЗ). LOGIC_VERSION остаётся 5.
Ни одно правило системы не меняется, ни один агент не добавляется, фильтр входа
не проектируется и не пишется «на всякий случай». РЕКОМЕНДАЦИЯ «ВНЕДРИТЬ ФИЛЬТР
ПО ПОЛОЖЕНИЮ В ДИАПАЗОНЕ» ЭТИМ ЭТАПОМ ЗАПРЕЩЕНА ПРЯМО и скриптом не печатается
ни в каком виде. Пишется ровно одна таблица — ``signal_range_position``.

ГЛАВНАЯ ОПАСНОСТЬ ЗАМЕРА — ПОДГЛЯДЫВАНИЕ В БУДУЩЕЕ. Один бар, закрывшийся ПОСЛЕ
сигнала, превращает замер в мусор, причём в КРАСИВЫЙ: связь находится сильная и
ложная. Ограждений три, и все обязательные (§2.2 ТЗ):

 1. ГОДНОСТЬ БАРА — ДВУМЯ УСЛОВИЯМИ, И ОБА ИЗ СУЩЕСТВУЮЩЕГО КОДА, НЕ КОПИЕЙ
    ФОРМУЛЫ. Бар годится в окно, если
        ``bar_ts + BAR_SECONDS[ряд] < signal_ts``  — он ЗАКРЫЛСЯ СТРОГО ДО
        сигнала (длина бара берётся из ``src.barrier.outcomes.BAR_SECONDS``,
        той же таблицы, которой меряет весь проект), И
        ``bar_ts + settle_seconds() <= now``       — он ОКОНЧАТЕЛЕН, то есть
        коллектор его уже не перезапишет (``src.barrier.runner.settle_seconds``
        вызывается, а не переписывается).

    ПОЧЕМУ ИМЕННО ТАК, А НЕ ОДНИМ ``settle_seconds()`` ОТ МОМЕНТА СИГНАЛА.
    Такой вариант был написан первым и отвергнут, и причина стоит того, чтобы
    быть записанной. ``settle_seconds()`` построен на длине ГРУБОГО бара (час)
    плюс запас: правый край окна отошёл бы от сигнала на 65 минут. Само по себе
    это безопасно — лишний час из семи суток почти не двигает минимум и
    максимум, — но КОНТРОЛЬНЫЙ ОПЫТ §2.2 ТЗ при таком крае становится
    бессмысленным: сдвиг окна на один бар вперёд утонул бы в 65-минутном
    запасе, проверка прошла бы С ВЕРНУВШИМСЯ ДЕФЕКТОМ, и опыт «доказал» бы
    работоспособность проверки, не проверив ничего. Ровно это — проверка,
    проходящая и с дефектом, и без него, — уже случалось на Этапе 9.1.3 трижды.
    Поэтому запрет на подглядывание выражен ТОЧНО (бар закрылся строго до
    сигнала), а ``settle_seconds()`` поставлен туда, на что он и отвечает:
    «сколько ждать ПОСЛЕ момента, прежде чем считать бар окончательным», —
    то есть к сравнению с ``now``, а не с моментом сигнала.
 2. ЦЕНА СИГНАЛА — ЗАМОРОЖЕННАЯ ``signal_targets.price_at_signal``, а не цена
    входа позиции. Позиция открывается ПОСЛЕ сигнала и по другой цене.
 3. ПРОВЕРКА БЛОКИРУЮЩАЯ, ПО КАЖДОЙ СТРОКЕ. Проверяются ДВА утверждения:
    момент последнего бара окна строго меньше ``signal_ts`` (§2.2 ТЗ дословно)
    И этот бар ЗАКРЫЛСЯ строго до ``signal_ts``. Хотя бы одно нарушено —
    расчёт останавливается с кодом 2, таблицы не печатаются, в базу не уходит
    ни одной строки.

КОНТРОЛЬНЫЙ ОПЫТ К ПРОВЕРКЕ — ``WINDOW_SHIFT_BARS`` (см. ниже). Проверка,
которая не падает при возвращённом дефекте, не проверяет ничего.

ВТОРАЯ ОПАСНОСТЬ — СВЯЗАННЫЕ НАБЛЮДЕНИЯ, СЧИТАННЫЕ КАК НЕЗАВИСИМЫЕ. Сигналы
идут каждую минуту; у двух соседних по одному токену почти одинаковое окно,
почти одинаковое положение и почти одинаковый исход. Сто тридцать тысяч пар —
это не сто тридцать тысяч наблюдений. Поэтому КАЖДАЯ таблица печатается ДВАЖДЫ:
на всей выборке (справочно) и на независимой подвыборке — не более одного
сигнала на инструмент в каждом четырёхчасовом окне, окна не перекрываются,
границы фиксированы по UTC, берётся первый сигнал окна. ВЫВОД ДЕЛАЕТСЯ ТОЛЬКО
ПО ВТОРОЙ. Если на всей выборке связь есть, а на независимой нет — связи нет.

ИСХОД НЕ СЧИТАЕТСЯ ЗАНОВО. Берётся уже посчитанное правило «цель–предел–срок»
Этапа 8.8: таблица ``signal_outcomes_barrier``, колонки ``outcome`` (доля
достижения цели) и ``net_pnl_pct`` (средний чистый итог, издержки уже вычтены
теми же ``cost_pct``, что в живом расчёте). Имена сверены с кодом и миграцией
015, а не с текстом ТЗ, — на 9.1.3 и 9.1.4 придуманные имена оказывались
неверными трижды.

КОДЫ ВОЗВРАТА:
  0 — расчёт выполнен;
  2 — проверка на подглядывание не прошла (либо ``--apply`` без миграции 023):
      таблицы НЕ напечатаны, в базу НЕ записано;
  3 — выборка пуста.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА. Каталог ``scripts/`` попадает только в образ
``backtest``:

    # 1. вхолостую — ни одной записи в базу:
    docker compose --profile backtest run --rm --no-deps \\
        backtest python scripts/range_position_9_1_5.py

    # 2. с записью в signal_range_position:
    docker compose --profile backtest run --rm --no-deps \\
        backtest python scripts/range_position_9_1_5.py --apply

Отдельным cron НЕ оформляется: это разовый замер, а не ночной расчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import structlog  # noqa: E402

from scripts.trailing_resample_9_1_3 import (  # noqa: E402
    cgroup_memory_limit_mb,
    peak_rss_mb,
)
from src.barrier.outcomes import (  # noqa: E402
    BAR_SECONDS,
    OUTCOME_TARGET,
    RESOLUTION_1M,
)
from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402

_log = structlog.get_logger().bind(component="range_position")

# Строка, по которой видно, что прогон дошёл до конца. Убитый ядром процесс не
# печатает ничего: без такого признака оборванный вывод неотличим от вывода
# расчёта, который посчитал и промолчал (Этап 9.1.3, два убийства подряд).
DONE_MARKER = "РАСЧЁТ ЗАВЕРШЁН"

DEFAULT_WINDOW_DAYS = 7

# Окно прореживания независимой подвыборки: четыре часа, границы фиксированы по
# UTC (floor от эпохи), из окна берётся ПЕРВЫЙ сигнал. Ключ прореживания
# включает ИНСТРУМЕНТ — иначе пять токенов конкурировали бы за одно окно и
# четыре пятых наблюдений исчезли бы молча (то же правило, что в 8.1).
INDEPENDENT_WINDOW_SEC = 4 * 3600

# Граница «старой» и «новой» половины выборки. ТА ЖЕ, что на Этапе 9.1.3
# (§4 ТЗ, ЧИСЛО 4): две разные границы у двух замеров сделали бы их несравнимыми.
BOUNDARY = datetime(2026, 8, 29, tzinfo=UTC)

# КОРЗИНЫ ФИКСИРОВАНЫ ПО ШИРИНЕ, А НЕ ПО КВАНТИЛЯМ (§4 ТЗ, ЧИСЛО 2). Границы,
# посчитанные по данным, подстраиваются под данные — это уже подгонка, и
# «находка» в такой таблице означала бы только то, что границы легли удачно.
POS_BUCKET_COUNT = 10
POS_BELOW = 0                       # pos < 0: цена НИЖЕ недельного минимума
POS_ABOVE = POS_BUCKET_COUNT + 1    # pos > 1: цена ВЫШЕ недельного максимума
POS_BUCKET_TOTAL = POS_BUCKET_COUNT + 2

# Корзины по ширине размаха. Границы левозакрытые: [0,2), [2,4), [4,7), [7,12),
# [12,∞). Названы так же, как в §3 ТЗ.
WIDTH_EDGES: tuple[float, ...] = (2.0, 4.0, 7.0, 12.0)
WIDTH_LABELS: tuple[str, ...] = ("<2%", "2–4%", "4–7%", "7–12%", ">12%")

# Трети по положению — тоже ФИКСИРОВАННЫЕ, а не квантильные. Пробитие вниз
# попадает в нижнюю треть, пробитие вверх — в верхнюю: они и есть крайние
# случаи этих третей, а не отдельная сущность. В главной таблице §4 ЧИСЛО 2 у
# них свои корзины, и там их численность видна отдельно.
TERTILE_EDGES: tuple[float, float] = (1.0 / 3.0, 2.0 / 3.0)
TERTILE_LABELS: tuple[str, ...] = ("низ (<1/3)", "середина", "верх (≥2/3)")

# Ниже этого числа наблюдений строка корзины ПОМЕЧАЕТСЯ и в выводы не идёт
# (§4 ТЗ, ЧИСЛО 5).
MIN_BUCKET_N = 30

# Защиты от подгонки (§4 ТЗ, ЧИСЛО 4). Число повторов и зерно названы здесь, а
# не подобраны на месте: перестановочная проверка с плавающим зерном давала бы
# каждый раз новый ответ на один и тот же вопрос.
PERMUTATION_RESAMPLES = 10_000
PERMUTATION_SEED = 9_1_5_2026
# Множитель нормального приближения для 95% интервала среднего.
Z95 = 1.959963984540054

# Горизонты берутся из настроек, а не переписываются числами: перечень
# «1,4,12,24» задан EVAL_HORIZONS и меняется настройкой, а не правкой скрипта.
HORIZONS: tuple[int, ...] = tuple(settings.eval_horizons_hours)

# КОНТРОЛЬНЫЙ ОПЫТ К ПРОВЕРКЕ НА ПОДГЛЯДЫВАНИЕ (§2.2 ТЗ). Ноль в боевом
# расчёте; ненулевым его делает ТОЛЬКО контрольный опыт в проверках.
#
# «БАР» ЗДЕСЬ — БАР ТОГО САМОГО РЯДА, ИЗ КОТОРОГО СЛОЖЕНО ОКНО, то есть
# минутный. Единица ставит последний бар окна так, что он закрывается РОВНО в
# момент сигнала, — и проверка обязана упасть, а расчёт вернуть код 2. Ради
# того, чтобы этот опыт был содержательным, правый край окна и определён точно,
# а не с 65-минутным запасом (см. заголовок модуля, пункт 1).
WINDOW_SHIFT_BARS = 0

# Оценка памяти. Байты на сигнал в разборном виде: одиннадцать чисел таблицы
# ``ScanResult`` (см. её заголовок). Байты на строку исхода — шесть чисел
# таблицы ``OutcomeColumns``.
BYTES_PER_SIGNAL = 66
BYTES_PER_OUTCOME = 21
# Во сколько раз выборка может вырасти, и мы обязаны остаться в лимите (§6 ТЗ).
GROWTH_FACTOR = 3
# Доля лимита контейнера, за которую расчёт не заходит. Не «сколько влезет», а
# «сколько можно взять, не мешая тому, что работает круглосуточно».
MEMORY_BUDGET_SHARE = 0.5


# ----------------------------------------------------------------------------
# Корзины. Границы НЕ ЗАВИСЯТ ОТ ДАННЫХ — это проверяется отдельным тестом.
# ----------------------------------------------------------------------------

def pos_bucket(pos: float) -> int:
    """Номер корзины положения: 0 — «ниже размаха», 11 — «выше размаха».

    ЦЕЛОЕ ЧИСЛО ДЕСЯТЫХ БЕРЁТСЯ ЧЕРЕЗ ОКРУГЛЕНИЕ, а не через ``pos // 0.1``.
    В двоичной дроби ``0.3 / 0.1`` равно 2.9999999999999996, и деление нацело
    отправило бы ровно 0.3 в корзину [0.2, 0.3) — то есть тихо сдвинуло бы
    границу, объявленную фиксированной.
    """
    if pos < 0.0:
        return POS_BELOW
    if pos > 1.0:
        return POS_ABOVE
    tenth = int(math.floor(round(pos * 10.0, 9)))
    return 1 + min(max(tenth, 0), POS_BUCKET_COUNT - 1)


def pos_bucket_label(index: int) -> str:
    """Подпись корзины положения."""
    if index == POS_BELOW:
        return "ниже размаха"
    if index == POS_ABOVE:
        return "выше размаха"
    low = (index - 1) / 10.0
    return f"{low:.1f}–{low + 0.1:.1f}"


def width_bucket(width_pct: float) -> int:
    """Номер корзины по ширине размаха. Границы левозакрытые."""
    return bisect_right(WIDTH_EDGES, width_pct)


def pos_tertile(pos: float) -> int:
    """Треть по положению. Границы фиксированы: 1/3 и 2/3, а не квантили."""
    if pos < TERTILE_EDGES[0]:
        return 0
    if pos < TERTILE_EDGES[1]:
        return 1
    return 2


def independent_window(ts_epoch: float) -> int:
    """Номер четырёхчасового окна прореживания. Границы фиксированы по UTC."""
    return int(math.floor(ts_epoch / INDEPENDENT_WINDOW_SEC))


# ----------------------------------------------------------------------------
# Растущие столбцы чисел. Ни одного словаря на строку.
# ----------------------------------------------------------------------------

class Column:
    """Растущий одномерный массив. Ёмкость удваивается.

    ПОЧЕМУ НЕ СПИСОК И НЕ СЛОВАРЬ НА СТРОКУ. На Этапе 9.1.3 полная загрузка
    выборки словарями стоила 1,7 ГБ и была убита ядром по ``mem_limit: 1g``
    дважды подряд; те же числа массивами стоили 13 МБ. Здесь выборка того же
    порядка, и повторять ту ошибку нечем.
    """

    __slots__ = ("_values", "_size")

    def __init__(self, dtype: Any, capacity: int = 4096) -> None:
        self._values = np.empty(capacity, dtype=dtype)
        self._size = 0

    def append(self, value: Any) -> None:
        if self._size == self._values.shape[0]:
            self._values = np.resize(self._values, self._size * 2)
        self._values[self._size] = value
        self._size += 1

    @property
    def values(self) -> np.ndarray:
        return self._values[: self._size]

    def __len__(self) -> int:
        return self._size


@dataclass
class ScanResult:
    """Итог прохода по барам: одиннадцать чисел на сигнал и ничего кроме чисел.

    ``last_bar_epoch`` и ``ts_epoch`` хранятся числами, а не объектами
    ``datetime``: объект момента времени стоит 48 байт против восьми, и на
    выборке в сотни тысяч сигналов это десятки мегабайт ради удобства записи,
    которое нужно ровно один раз — при выгрузке в базу.
    """

    signal_id: Column = field(default_factory=lambda: Column(np.int64))
    ts_epoch: Column = field(default_factory=lambda: Column(np.float64))
    instrument_id: Column = field(default_factory=lambda: Column(np.int32))
    pos: Column = field(default_factory=lambda: Column(np.float64))
    width_pct: Column = field(default_factory=lambda: Column(np.float64))
    range_low: Column = field(default_factory=lambda: Column(np.float64))
    range_high: Column = field(default_factory=lambda: Column(np.float64))
    last_bar_epoch: Column = field(default_factory=lambda: Column(np.float64))
    bars_in_window: Column = field(default_factory=lambda: Column(np.int32))
    independent: Column = field(default_factory=lambda: Column(np.bool_))
    is_old: Column = field(default_factory=lambda: Column(np.bool_))

    def __len__(self) -> int:
        return len(self.signal_id)


@dataclass
class ScanCounters:
    """Счётчики состава выборки (§4 ТЗ, ЧИСЛО 1). Только числа, без строк."""

    signals_seen: int = 0
    computed: int = 0
    skipped_short_history: int = 0
    skipped_no_bars: int = 0
    skipped_flat_range: int = 0
    lookahead_violations: int = 0
    independent: int = 0
    by_token: dict[str, int] = field(default_factory=dict)
    by_version: dict[int, int] = field(default_factory=dict)
    by_direction: dict[str, int] = field(default_factory=dict)
    violation_examples: list[str] = field(default_factory=list)
    max_window_bars: int = 0

    def add(self, *, token: str, version: int, direction: str) -> None:
        self.by_token[token] = self.by_token.get(token, 0) + 1
        self.by_version[version] = self.by_version.get(version, 0) + 1
        self.by_direction[direction] = self.by_direction.get(direction, 0) + 1


# ----------------------------------------------------------------------------
# ПРОХОД ПО БАРАМ. Один раз по инструменту, с подвижными минимумом и максимумом.
# ----------------------------------------------------------------------------

class SlidingRange:
    """Минимум ``low`` и максимум ``high`` по ПОДВИЖНОМУ окну баров.

    ЗАЧЕМ ЭТО УСТРОЙСТВО. Окно замера — семь суток минутных баров, около 10 080
    штук НА КАЖДЫЙ сигнал. Пересчитывать минимум и максимум заново на каждом
    сигнале — это сотни тысяч сигналов на десять тысяч баров, то есть миллиарды
    операций: расчёт не кончился бы за разумное время, а на Этапе 9.1.3
    наивный по памяти вариант вдобавок был убит ядром. Здесь бары читаются ОДИН
    РАЗ в порядке времени, а границы размаха ведутся двумя монотонными
    очередями: каждый бар входит в очередь и выходит из неё ровно по разу.

    ПОЧЕМУ ОЧЕРЕДЬ МОЖНО ЧИСТИТЬ С ХВОСТА. В очередь минимума не нужен бар,
    который больше только что пришедшего: пока он в окне, в окне и новый, а
    новый меньше, — значит, минимумом старый уже не станет никогда. То же
    зеркально для максимума. Это и делает проход линейным.

    ПАМЯТЬ ОГРАНИЧЕНА ОКНОМ, А НЕ ВЫБОРКОЙ. В худшем случае (монотонный ряд) в
    каждой очереди лежит всё окно — 10 080 пар чисел, около мегабайта, — и
    больше ни при каком объёме данных не станет.
    """

    __slots__ = ("_ts", "_min_ts", "_min_val", "_max_ts", "_max_val", "peak_size")

    def __init__(self) -> None:
        # Моменты ВСЕХ баров окна: по ним считается bars_in_window и ведётся
        # выселение. Списки с подвижным началом заменены на deque ниже.
        from collections import deque

        self._ts: deque[float] = deque()
        self._min_ts: deque[float] = deque()
        self._min_val: deque[float] = deque()
        self._max_ts: deque[float] = deque()
        self._max_val: deque[float] = deque()
        self.peak_size = 0

    def push(self, ts_epoch: float, low: float, high: float) -> None:
        self._ts.append(ts_epoch)
        while self._min_val and self._min_val[-1] >= low:
            self._min_val.pop()
            self._min_ts.pop()
        self._min_val.append(low)
        self._min_ts.append(ts_epoch)
        while self._max_val and self._max_val[-1] <= high:
            self._max_val.pop()
            self._max_ts.pop()
        self._max_val.append(high)
        self._max_ts.append(ts_epoch)
        self.peak_size = max(self.peak_size, len(self._ts))

    def evict_before(self, window_from: float) -> None:
        """Выселить бары, начавшиеся РАНЬШЕ левой границы окна."""
        while self._ts and self._ts[0] < window_from:
            self._ts.popleft()
        while self._min_ts and self._min_ts[0] < window_from:
            self._min_ts.popleft()
            self._min_val.popleft()
        while self._max_ts and self._max_ts[0] < window_from:
            self._max_ts.popleft()
            self._max_val.popleft()

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def low(self) -> float:
        return self._min_val[0]

    @property
    def high(self) -> float:
        return self._max_val[0]

    @property
    def last_ts(self) -> float:
        return self._ts[-1]


class BarStream:
    """Поток баров одного инструмента с ОДНИМ подсмотренным баром вперёд.

    Подсмотренный бар нужен затем, чтобы решить «этот бар ещё годится в окно
    или уже нет», НЕ ЗАБРАВ его из потока. Забрать и вернуть значило бы держать
    сложность там, где её можно не держать.
    """

    def __init__(
        self, *, instrument_id: int, timeframe: str, ts_from: datetime,
        batch: int | None = None,
    ) -> None:
        self._instrument_id = int(instrument_id)
        self._timeframe = str(timeframe)
        self._ts_from = ts_from
        self._batch = batch
        self._buffer: list[dict[str, Any]] = []
        self._index = 0
        self._after: datetime | None = None
        self._exhausted = False
        self.rows_read = 0

    async def _fill(self) -> None:
        while self._index >= len(self._buffer) and not self._exhausted:
            rows = await db.fetch_range_position_bars_batch(
                instrument_id=self._instrument_id,
                timeframe=self._timeframe,
                ts_from=self._ts_from,
                after_ts=self._after,
                limit=self._batch,
            )
            if not rows:
                self._exhausted = True
                self._buffer = []
                self._index = 0
                return
            self._buffer = rows
            self._index = 0
            self._after = rows[-1]["ts"]
            self.rows_read += len(rows)

    async def peek(self) -> dict[str, Any] | None:
        await self._fill()
        if self._index >= len(self._buffer):
            return None
        return self._buffer[self._index]

    def take(self) -> dict[str, Any]:
        row = self._buffer[self._index]
        self._index += 1
        return row


def window_cutoff_epoch(
    signal_epoch: float, *, bar_seconds: float, shift_sec: float
) -> float:
    """Граница окна СПРАВА: бар годится, если его момент СТРОГО МЕНЬШЕ границы.

    Граница НЕ ВКЛЮЧАЮЩАЯ намеренно. Бар с моментом ``signal_ts − длина бара``
    закрывается РОВНО В СЕКУНДУ СИГНАЛА, и его ``high``/``low`` описывают ту же
    минуту, в которой принято решение. Пустить его в окно значило бы измерить
    размах по отрезку, кончающемуся вместе с сигналом, а не до него.

    ``shift_sec`` в боевом расчёте равен нулю; ненулевым его делает только
    контрольный опыт ``WINDOW_SHIFT_BARS``.
    """
    return signal_epoch - bar_seconds + shift_sec


async def scan_instrument(
    instrument: dict[str, Any],
    *,
    window_days: int,
    settle: int,
    shift_sec: float,
    timeframe: str,
    now: datetime,
    result: ScanResult,
    counters: ScanCounters,
) -> None:
    """Один проход по барам инструмента; сигналы приклеиваются к нему по времени.

    ПОРЯДОК ЗДЕСЬ — НЕ УДОБСТВО, А УСЛОВИЕ ПРАВИЛЬНОСТИ. Сигналы приходят строго
    по возрастанию времени, поэтому и правый край окна, и левый двигаются только
    вперёд: очередь минимума и максимума можно вести одну на весь инструмент.
    Сигнал, пришедший «назад во времени», сломал бы это молча — поэтому порядок
    задан в самом запросе, а не наводится здесь сортировкой.
    """
    instrument_id = int(instrument["instrument_id"])
    token = str(instrument["token"])
    first_bar_ts = instrument["first_bar_ts"]
    window_sec = float(window_days) * 86400.0
    fine_bar_sec = float(BAR_SECONDS[timeframe])
    # ВТОРОЕ УСЛОВИЕ ГОДНОСТИ (см. заголовок модуля): бар обязан быть
    # ОКОНЧАТЕЛЬНЫМ — коллектор его уже не перезапишет. Это и есть работа
    # ``settle_seconds()``, и она про ``now``, а не про момент сигнала.
    settled_before = now.timestamp() - float(settle)

    # Первая порция сигналов нужна ещё до чтения баров: с неё берётся левая
    # граница чтения. Читать бары от начала времён значило бы прочитать всю
    # историю инструмента ради окна первого сигнала.
    batch = await db.fetch_range_position_signals_batch(instrument_id=instrument_id)
    if not batch:
        return
    if first_bar_ts is None:
        # У инструмента нет ни одного минутного бара: окна не существует ни у
        # одного его сигнала, и это надо СОСЧИТАТЬ, а не пропустить молча.
        while batch:
            counters.signals_seen += len(batch)
            counters.skipped_short_history += len(batch)
            last = batch[-1]
            batch = await db.fetch_range_position_signals_batch(
                instrument_id=instrument_id,
                after=(last["ts"], int(last["signal_id"])),
            )
        return

    first_bar_epoch = first_bar_ts.timestamp()
    bars = BarStream(
        instrument_id=instrument_id,
        timeframe=timeframe,
        ts_from=batch[0]["ts"] - timedelta(seconds=window_sec),
    )
    window = SlidingRange()
    last_window_index: int | None = None

    while batch:
        for row in batch:
            counters.signals_seen += 1
            signal_ts = row["ts"]
            signal_epoch = signal_ts.timestamp()
            cutoff = window_cutoff_epoch(
                signal_epoch, bar_seconds=fine_bar_sec, shift_sec=shift_sec
            )
            window_from = signal_epoch - window_sec

            while True:
                head = await bars.peek()
                if head is None:
                    break
                head_epoch = head["ts"].timestamp()
                # Граница СТРОГАЯ: бар, закрывающийся ровно в момент сигнала,
                # в окно не входит. Второе условие — окончательность бара.
                if head_epoch >= cutoff or head_epoch > settled_before:
                    break
                bar = bars.take()
                window.push(
                    bar["ts"].timestamp(), float(bar["low"]), float(bar["high"])
                )
            window.evict_before(window_from)
            counters.max_window_bars = max(counters.max_window_bars, len(window))

            if not window:
                counters.skipped_no_bars += 1
                continue
            # ПОЛНОЕ ОКНО — ЭТО СЕМЬ СУТОК ИСТОРИИ, А НЕ СЕМЬ СУТОК ЗАПИСЕЙ.
            # Минутные свечи живут RETENTION_1M_DAYS суток; у сигнала, чьё окно
            # начинается раньше первого сохранившегося бара, размах семи суток
            # посчитать НЕ ИЗ ЧЕГО, и посчитать его по обрезку значило бы выдать
            # размах трёх суток за размах семи.
            if first_bar_epoch > window_from:
                counters.skipped_short_history += 1
                continue

            low = window.low
            high = window.high
            if high <= low or low <= 0.0:
                # Размах нулевой ширины — это не рынок, а деление на ноль:
                # положение в нём не определено. Ограничение миграции 023 такую
                # строку не примет, и подставлять сюда 0.5 значило бы выдумать
                # измерение.
                counters.skipped_flat_range += 1
                continue

            price = float(row["price_at_signal"])
            pos = (price - low) / (high - low)
            width_pct = (high - low) / low * 100.0
            last_bar_epoch = window.last_ts

            # ПРОВЕРКА НА ПОДГЛЯДЫВАНИЕ, ПО КАЖДОЙ СТРОКЕ. Два утверждения:
            # момент последнего бара строго меньше момента сигнала (§2.2 ТЗ
            # дословно) И этот бар ЗАКРЫЛСЯ строго до сигнала. Второе — это то
            # самое «бар, закрывшийся в ту же секунду, что сигнал, в окно не
            # попадает»: без него бар, начавшийся за минуту до сигнала, прошёл
            # бы первую проверку, а его high и low содержали бы будущее.
            if (last_bar_epoch >= signal_epoch
                    or last_bar_epoch + fine_bar_sec >= signal_epoch):
                counters.lookahead_violations += 1
                if len(counters.violation_examples) < 5:
                    counters.violation_examples.append(
                        f"сигнал {int(row['signal_id'])} в {signal_ts.isoformat()}: "
                        f"последний бар окна "
                        f"{datetime.fromtimestamp(last_bar_epoch, UTC).isoformat()}"
                        f", закрывается через {fine_bar_sec:.0f} с"
                    )
                continue

            index = independent_window(signal_epoch)
            is_independent = index != last_window_index
            if is_independent:
                last_window_index = index
                counters.independent += 1

            result.signal_id.append(int(row["signal_id"]))
            result.ts_epoch.append(signal_epoch)
            result.instrument_id.append(instrument_id)
            result.pos.append(pos)
            result.width_pct.append(width_pct)
            result.range_low.append(low)
            result.range_high.append(high)
            result.last_bar_epoch.append(last_bar_epoch)
            result.bars_in_window.append(len(window))
            result.independent.append(is_independent)
            result.is_old.append(signal_epoch < BOUNDARY.timestamp())
            counters.computed += 1
            counters.add(
                token=token,
                version=int(row["logic_version"]),
                direction=str(row["decision"]),
            )

        last = batch[-1]
        batch = await db.fetch_range_position_signals_batch(
            instrument_id=instrument_id,
            after=(last["ts"], int(last["signal_id"])),
        )


# ----------------------------------------------------------------------------
# ИСХОДЫ. Уже посчитанные Этапом 8.8, прочитанные потоком и разложенные в числа.
# ----------------------------------------------------------------------------

@dataclass
class OutcomeColumns:
    """Строки исходов одной пары «направление × горизонт», числами.

    ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Есть корзина положения, корзина ширины, треть по
    положению, признак «дошло до цели», чистый итог в процентах и два признака
    подвыборки. Нет ни одного идентификатора: он не нужен ни одной таблице §4,
    а на сотнях тысяч строк стоил бы мегабайты.
    """

    pos_bucket: Column = field(default_factory=lambda: Column(np.int8))
    width_bucket: Column = field(default_factory=lambda: Column(np.int8))
    tertile: Column = field(default_factory=lambda: Column(np.int8))
    is_target: Column = field(default_factory=lambda: Column(np.bool_))
    net: Column = field(default_factory=lambda: Column(np.float64))
    independent: Column = field(default_factory=lambda: Column(np.bool_))
    is_old: Column = field(default_factory=lambda: Column(np.bool_))

    def __len__(self) -> int:
        return len(self.net)


@dataclass
class OutcomeCounters:
    """Состав выборки исходов (§4 ТЗ, ЧИСЛО 1)."""

    rows_seen: int = 0
    matched: int = 0
    unmeasured: dict[str, int] = field(default_factory=dict)
    by_horizon: dict[int, int] = field(default_factory=dict)
    by_direction: dict[str, int] = field(default_factory=dict)
    by_version: dict[int, int] = field(default_factory=dict)
    by_token: dict[str, int] = field(default_factory=dict)
    independent_rows: int = 0


class SignalLookup:
    """Поиск номера сигнала в ``ScanResult`` БЕЗ словаря на сигнал.

    Словарь ``{signal_id: индекс}`` на сотнях тысяч сигналов стоит около
    ста байт на запись — десятки мегабайт ради операции, которую отсортированный
    массив делает двоичным поиском СРАЗУ ПО ВСЕЙ ПОРЦИИ исходов, не заходя в
    Python ни на одну строку.
    """

    def __init__(self, ids: np.ndarray) -> None:
        self._order = np.argsort(ids, kind="stable")
        self._sorted = ids[self._order]

    def find(self, wanted: np.ndarray) -> np.ndarray:
        """Номера строк для массива идентификаторов; −1 там, где сигнала нет."""
        place = np.searchsorted(self._sorted, wanted)
        place_clipped = np.clip(place, 0, max(self._sorted.size - 1, 0))
        found = (
            (place < self._sorted.size)
            & (self._sorted[place_clipped] == wanted)
        )
        out = np.full(wanted.shape, -1, dtype=np.int64)
        if self._sorted.size:
            out[found] = self._order[place_clipped[found]]
        return out


async def stream_outcomes(
    scan: ScanResult,
) -> tuple[dict[tuple[str, int], OutcomeColumns], OutcomeCounters]:
    """Один проход по ``signal_outcomes_barrier``; исходы клеятся к положению.

    ``ambiguous`` и ``no_data`` В ВЫБОРКУ НЕ ИДУТ И СЧИТАЮТСЯ ОТДЕЛЬНО. У них
    ``net_pnl_pct IS NULL``: исход НЕ ИЗМЕРЕН, а не «не цель». Записать их в
    знаменатель доли достижения цели значило бы объявить неизвестное неудачей —
    то есть посчитать то, чего никто не наблюдал.
    """
    ids = scan.signal_id.values
    lookup = SignalLookup(ids)
    pos_values = scan.pos.values
    width_values = scan.width_pct.values
    independent = scan.independent.values
    is_old = scan.is_old.values

    pos_buckets = np.fromiter(
        (pos_bucket(float(v)) for v in pos_values), dtype=np.int8,
        count=pos_values.size,
    )
    width_buckets = np.fromiter(
        (width_bucket(float(v)) for v in width_values), dtype=np.int8,
        count=width_values.size,
    )
    tertiles = np.fromiter(
        (pos_tertile(float(v)) for v in pos_values), dtype=np.int8,
        count=pos_values.size,
    )

    out: dict[tuple[str, int], OutcomeColumns] = {}
    counters = OutcomeCounters()
    after: tuple[int, int] | None = None
    while True:
        rows = await db.fetch_range_position_outcomes_batch(after=after)
        if not rows:
            break
        counters.rows_seen += len(rows)
        wanted = np.fromiter(
            (int(r["signal_id"]) for r in rows), dtype=np.int64, count=len(rows)
        )
        places = lookup.find(wanted)
        for row, place in zip(rows, places, strict=True):
            if place < 0:
                continue
            outcome = str(row["outcome"])
            net = row["net_pnl_pct"]
            if net is None:
                counters.unmeasured[outcome] = (
                    counters.unmeasured.get(outcome, 0) + 1
                )
                continue
            key = (str(row["direction"]), int(row["horizon_h"]))
            columns = out.setdefault(key, OutcomeColumns())
            columns.pos_bucket.append(pos_buckets[place])
            columns.width_bucket.append(width_buckets[place])
            columns.tertile.append(tertiles[place])
            columns.is_target.append(outcome == OUTCOME_TARGET)
            columns.net.append(float(net))
            columns.independent.append(independent[place])
            columns.is_old.append(is_old[place])
            counters.matched += 1
            counters.by_horizon[key[1]] = counters.by_horizon.get(key[1], 0) + 1
            counters.by_direction[key[0]] = (
                counters.by_direction.get(key[0], 0) + 1
            )
            version = int(row["logic_version"])
            counters.by_version[version] = counters.by_version.get(version, 0) + 1
            token = str(row["token"])
            counters.by_token[token] = counters.by_token.get(token, 0) + 1
            if independent[place]:
                counters.independent_rows += 1
        last = rows[-1]
        after = (int(last["signal_id"]), int(last["horizon_h"]))
    return out, counters


# ----------------------------------------------------------------------------
# ТАБЛИЦЫ. Каждая печатается ДВАЖДЫ: вся выборка и независимая подвыборка.
# ----------------------------------------------------------------------------

@dataclass
class Cell:
    """Одна строка таблицы: сколько наблюдений и что они показали."""

    label: str
    n: int
    target_share: float | None
    mean: float | None
    lo: float | None
    hi: float | None

    @property
    def usable(self) -> bool:
        """Годится ли строка для ВЫВОДОВ (§4 ТЗ, ЧИСЛО 5)."""
        return self.n >= MIN_BUCKET_N

    @property
    def crosses_zero(self) -> bool | None:
        if self.lo is None or self.hi is None:
            return None
        return self.lo <= 0.0 <= self.hi


def mean_interval(values: np.ndarray) -> tuple[float | None, float | None, float | None]:
    """Среднее и 95% интервал среднего по нормальному приближению.

    ПОЧЕМУ НЕ ПЕРЕСБОРКА. Интервалы здесь считаются для СОТЕН ячеек (двенадцать
    корзин × два направления × четыре горизонта × две выборки, и столько же для
    ширины и совместной таблицы). Пересборка по десять тысяч раз на ячейку
    стоила бы минуты счёта и дала бы тот же ответ: на выборках от тридцати
    наблюдений — а меньшие в выводы всё равно не идут — нормальное приближение
    и пересборка расходятся в третьем знаке. Пересборка оставлена там, где она
    отвечает на ДРУГОЙ вопрос и где приближения не хватает, — в перестановочной
    проверке §4 ЧИСЛО 4.
    """
    n = values.size
    if n == 0:
        return None, None, None
    mean = float(values.mean())
    if n < 2:
        return mean, None, None
    err = float(values.std(ddof=1)) / math.sqrt(n)
    return mean, mean - Z95 * err, mean + Z95 * err


def build_cells(
    labels: list[str],
    keys: np.ndarray,
    is_target: np.ndarray,
    net: np.ndarray,
    count: int,
) -> list[Cell]:
    """Строки таблицы по номеру корзины. Пустые корзины НЕ пропускаются.

    Корзина без наблюдений печатается строкой с нулём. Пропустить её значило бы
    показать таблицу, в которой фиксированные границы выглядят подогнанными под
    данные, — а они фиксированные именно затем, чтобы этого не было видно ни в
    каком составе выборки.
    """
    cells: list[Cell] = []
    for index in range(count):
        mask = keys == index
        n = int(mask.sum())
        if n == 0:
            cells.append(Cell(labels[index], 0, None, None, None, None))
            continue
        share = float(is_target[mask].mean()) * 100.0
        mean, lo, hi = mean_interval(net[mask])
        cells.append(Cell(labels[index], n, share, mean, lo, hi))
    return cells


def print_cells(cells: list[Cell], *, title: str, first_column: str) -> None:
    """Печать таблицы §4. Строки с N < 30 помечены и в выводы не идут."""
    print(f"    {title}")
    print(f"      {first_column:<14} {'N':>7} {'доля target':>12} "
          f"{'средний итог %':>15} {'95% интервал среднего':>26}")
    for cell in cells:
        if cell.n == 0:
            print(f"      {cell.label:<14} {0:>7} {'—':>12} {'—':>15} {'—':>26}")
            continue
        interval = (
            "—" if cell.lo is None
            else f"[{cell.lo:+.3f}; {cell.hi:+.3f}]"
                 + ("  пересекает 0" if cell.crosses_zero else "  не пересекает")
        )
        mark = "" if cell.usable else "  ← N<30, в выводы не идёт"
        print(f"      {cell.label:<14} {cell.n:>7} {cell.target_share:>11.1f}% "
              f"{cell.mean:>+15.3f} {interval:>26}{mark}")


def print_joint(
    tertiles: np.ndarray,
    widths: np.ndarray,
    is_target: np.ndarray,
    net: np.ndarray,
) -> list[dict[str, Any]]:
    """Совместная таблица «треть по pos × корзина по width_pct» (§4, ЧИСЛО 3).

    НА КАКОЙ ВОПРОС ОНА ОТВЕЧАЕТ. Ширина размаха и положение в нём связаны:
    в узком диапазоне цена почти всегда «где-то посередине», в широком — чаще у
    края. Если разница по положению исчезает ВНУТРИ корзины по ширине, значит,
    вся видимая связь с положением была связью с шириной, а положение только
    её отражало.
    """
    print(f"      {'ширина \\ треть':<14}", end="")
    for label in TERTILE_LABELS:
        print(f"{label:>26}", end="")
    print()
    rows: list[dict[str, Any]] = []
    for w_index, w_label in enumerate(WIDTH_LABELS):
        print(f"      {w_label:<14}", end="")
        for t_index in range(len(TERTILE_LABELS)):
            mask = (widths == w_index) & (tertiles == t_index)
            n = int(mask.sum())
            if n == 0:
                print(f"{'—':>26}", end="")
                rows.append({
                    "width": w_label, "tertile": TERTILE_LABELS[t_index],
                    "n": 0, "target_share": None, "mean": None,
                })
                continue
            share = float(is_target[mask].mean()) * 100.0
            mean = float(net[mask].mean())
            mark = "" if n >= MIN_BUCKET_N else "*"
            print(f"{f'{n} / {share:.1f}% / {mean:+.3f}{mark}':>26}", end="")
            rows.append({
                "width": w_label, "tertile": TERTILE_LABELS[t_index],
                "n": n, "target_share": share, "mean": mean,
            })
        print()
    print("      (в ячейке: N / доля target / средний итог %; "
          "* — N<30, в выводы не идёт)")
    return rows


# ----------------------------------------------------------------------------
# ТРИ ЗАЩИТЫ ОТ ПОДГОНКИ (§4 ТЗ, ЧИСЛО 4). Только на независимой подвыборке.
# ----------------------------------------------------------------------------

def permutation_spread(
    keys: np.ndarray,
    net: np.ndarray,
    *,
    eligible: list[int],
    resamples: int = PERMUTATION_RESAMPLES,
    seed: int = PERMUTATION_SEED,
) -> dict[str, Any] | None:
    """Перестановочная проверка: бывает ли такой разброс корзин случайно.

    ЯРЛЫКИ КОРЗИН ПЕРЕМЕШИВАЮТСЯ ОТНОСИТЕЛЬНО ИСХОДОВ. Это и есть точная
    формулировка вопроса: если положение в размахе ни на что не влияет, то
    какому исходу какая корзина досталась — безразлично, и наблюдённый разброс
    обязан теряться среди перемешанных.

    ПЕЧАТАЮТСЯ ДВА РАЗМАХА, А НЕ ОДИН. «Размах между крайними корзинами»
    читается двояко: как разница между САМОЙ ЛУЧШЕЙ и САМОЙ ХУДШЕЙ корзиной
    (так считает Этап 8.10) и как разница между КРАЙНИМИ ПО ПОЛОЖЕНИЮ — нижней
    и верхней. Это разные числа, и выбрать одно молча значило бы ответить на
    вопрос, которого не задавали. Оба сравниваются со своим 95-м процентилем.

    В счёт идут только корзины с N ≥ 30: корзина из трёх наблюдений даёт
    огромный разброс сама по себе, и он не про положение в размахе.
    """
    if len(eligible) < 2:
        return None
    mask = np.isin(keys, eligible)
    labels = keys[mask]
    values = net[mask]
    if values.size == 0:
        return None

    remap = {key: i for i, key in enumerate(sorted(eligible))}
    compact = np.fromiter(
        (remap[int(k)] for k in labels), dtype=np.int64, count=labels.size
    )
    groups = len(remap)
    counts = np.bincount(compact, minlength=groups).astype(float)
    if (counts == 0).any():
        return None

    def spreads(sample: np.ndarray) -> tuple[float, float]:
        means = np.bincount(compact, weights=sample, minlength=groups) / counts
        return float(means.max() - means.min()), float(means[-1] - means[0])

    observed_spread, observed_edges = spreads(values)
    rng = np.random.default_rng(seed)
    cloud_spread = np.empty(resamples, dtype=float)
    cloud_edges = np.empty(resamples, dtype=float)
    for i in range(resamples):
        shuffled = rng.permutation(values)
        cloud_spread[i], cloud_edges[i] = spreads(shuffled)
    return {
        "buckets": [int(k) for k in sorted(eligible)],
        "observed_spread": observed_spread,
        "random_spread_p95": float(np.percentile(cloud_spread, 95.0)),
        "observed_edges": observed_edges,
        "random_edges_p95": float(np.percentile(np.abs(cloud_edges), 95.0)),
        "resamples": resamples,
        "n": int(values.size),
    }


def split_half(
    keys: np.ndarray,
    net: np.ndarray,
    is_old: np.ndarray,
) -> dict[str, Any]:
    """Независимая половина по времени: граница ``BOUNDARY``, та же, что в 9.1.3.

    ДВА ОТВЕТА, А НЕ ОДИН (§4 ТЗ, ЧИСЛО 4). Первый: совпала ли ЛУЧШАЯ корзина.
    Второй: сохранила ли она ЗНАК своего преимущества над средним по выборке.
    Совпадение имени без сохранения знака — это не подтверждение, а совпадение;
    сохранение знака при другом имени — не находка, а шум. Печатать одно вместо
    другого значило бы выдать половину ответа за целый.
    """
    out: dict[str, Any] = {
        "old_n": int(is_old.sum()), "new_n": int((~is_old).sum()),
        "best_old": None, "best_new": None, "same_bucket": None,
        "advantage_old": None, "advantage_new": None, "same_sign": None,
    }

    def best(mask: np.ndarray) -> tuple[int | None, float | None]:
        if not mask.any():
            return None, None
        overall = float(net[mask].mean())
        winner: int | None = None
        winner_value = -math.inf
        for index in range(POS_BUCKET_TOTAL):
            cell = mask & (keys == index)
            n = int(cell.sum())
            if n < MIN_BUCKET_N:
                continue
            value = float(net[cell].mean())
            if value > winner_value:
                winner, winner_value = index, value
        if winner is None:
            return None, None
        return winner, winner_value - overall

    best_old, adv_old = best(is_old)
    best_new, adv_new = best(~is_old)
    out["best_old"] = best_old
    out["best_new"] = best_new
    out["advantage_old"] = adv_old
    out["advantage_new"] = adv_new
    if best_old is not None and best_new is not None:
        out["same_bucket"] = best_old == best_new
    if best_old is not None and adv_old is not None and best_new is not None:
        # ЗНАК СВЕРЯЕТСЯ У ТОЙ ЖЕ КОРЗИНЫ, а не у победителя второй половины:
        # вопрос §4 ТЗ — «сохранила ли ОНА знак преимущества», и подстановка
        # другого победителя ответила бы «да» почти всегда.
        cell = (~is_old) & (keys == best_old)
        if int(cell.sum()) >= MIN_BUCKET_N:
            same_cell_adv = float(net[cell].mean()) - float(net[~is_old].mean())
            out["advantage_new"] = same_cell_adv
            out["same_sign"] = (adv_old > 0) == (same_cell_adv > 0)
    return out


def detectable_difference(
    keys: np.ndarray, net: np.ndarray, eligible: list[int]
) -> float | None:
    """Наименьшая разница средних, которую эта выборка ещё различает.

    Считается для ДВУХ КРАЙНИХ ПО ПОЛОЖЕНИЮ годных корзин по обычной формуле
    полуширины интервала разности средних. Это и есть прямой ответ §4 ЧИСЛО 5
    на вопрос «что на такой выборке различимо, а что нет»: разница меньше
    напечатанной неотличима от нуля, сколько бы её ни обсуждали.
    """
    if len(eligible) < 2:
        return None
    low_key, high_key = min(eligible), max(eligible)
    low = net[keys == low_key]
    high = net[keys == high_key]
    if low.size < 2 or high.size < 2:
        return None
    err = math.sqrt(
        float(low.var(ddof=1)) / low.size + float(high.var(ddof=1)) / high.size
    )
    return Z95 * err


# ----------------------------------------------------------------------------
# ПЕЧАТЬ ЧИСЕЛ §4 ТЗ.
# ----------------------------------------------------------------------------

SAMPLE_ALL = "вся выборка (справочно)"
SAMPLE_INDEPENDENT = "независимая подвыборка (по ней и делается вывод)"


def _select(columns: OutcomeColumns, *, independent_only: bool) -> dict[str, np.ndarray]:
    """Массивы одной выборки. ``independent_only`` — прореженная подвыборка."""
    mask = (
        columns.independent.values if independent_only
        else np.ones(len(columns), dtype=bool)
    )
    return {
        "pos_bucket": columns.pos_bucket.values[mask],
        "width_bucket": columns.width_bucket.values[mask],
        "tertile": columns.tertile.values[mask],
        "is_target": columns.is_target.values[mask],
        "net": columns.net.values[mask],
        "is_old": columns.is_old.values[mask],
    }


def _eligible_buckets(keys: np.ndarray, count: int) -> list[int]:
    """Корзины, годные для выводов: N ≥ 30."""
    return [
        index for index in range(count)
        if int((keys == index).sum()) >= MIN_BUCKET_N
    ]


def print_number_one(
    signal_counts: dict[str, int],
    scan_counters: ScanCounters,
    outcome_counters: OutcomeCounters,
    *,
    window_days: int,
    settle: int,
) -> None:
    """ЧИСЛО 1: состав выборки."""
    print()
    print("─" * 78)
    print(" ЧИСЛО 1. СОСТАВ ВЫБОРКИ")
    print("─" * 78)
    def line(label: str, value: Any) -> None:
        print(f"  {label:.<44} {value}")

    line("Направленных сигналов всего", signal_counts["directional_total"])
    line("Из них с посчитанным исходом (Этап 8.8)",
         signal_counts["with_outcome"])
    line("Рассмотрено проходом по барам", scan_counters.signals_seen)
    line(f"Исключено: истории короче {window_days} сут",
         scan_counters.skipped_short_history)
    line("Исключено: в окне не оказалось баров",
         scan_counters.skipped_no_bars)
    line("Исключено: размах нулевой ширины", scan_counters.skipped_flat_range)
    line("ОСТАЛОСЬ сигналов с положением", scan_counters.computed)
    line("Из них в независимой подвыборке", scan_counters.independent)
    line("Запас закрытия бара (settle_seconds)", f"{settle} с")
    line("Окно прореживания",
         f"{INDEPENDENT_WINDOW_SEC // 3600} ч, границы по UTC, "
         f"первый сигнал окна")
    print()
    print("  Разбивка ПО СИГНАЛАМ (инструмент / версия логики / направление):")
    for token, count in sorted(scan_counters.by_token.items()):
        print(f"      токен {token:<8} {count}")
    for version, count in sorted(scan_counters.by_version.items()):
        print(f"      logic_version {version:<3} {count}")
    for direction, count in sorted(scan_counters.by_direction.items()):
        print(f"      {direction:<12} {count}")
    if len(scan_counters.by_version) > 1:
        print()
        print("  " + "!" * 70)
        print("  !! ПРЕДУПРЕЖДЕНИЕ: В ВЫБОРКЕ БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ.")
        print("  !! Версии не смешиваются (правило проекта): числа ниже считают")
        print("  !! вместе решения РАЗНЫХ систем, и сравнивать их между собой")
        print("  !! нельзя. Вывод по такой выборке недействителен.")
        print("  " + "!" * 70)
    print()
    print("  Разбивка ПО СТРОКАМ ИСХОДА (сигнал × горизонт):")
    print(f"      строк исхода прочитано:  {outcome_counters.rows_seen}")
    print(f"      из них легло в выборку:  {outcome_counters.matched}")
    print(f"      из них независимых:      {outcome_counters.independent_rows}")
    for horizon, count in sorted(outcome_counters.by_horizon.items()):
        print(f"      горизонт {horizon:>3}ч          {count}")
    for direction, count in sorted(outcome_counters.by_direction.items()):
        print(f"      {direction:<20} {count}")
    if outcome_counters.unmeasured:
        print("      НЕ ИЗМЕРЕННЫЕ исходы (в выборку не идут, "
              "в знаменатель доли цели тоже):")
        for outcome, count in sorted(outcome_counters.unmeasured.items()):
            print(f"          {outcome:<16} {count}")


def print_number_two(
    outcomes: dict[tuple[str, int], OutcomeColumns],
) -> dict[str, Any]:
    """ЧИСЛО 2: успех по десяти фиксированным корзинам pos (плюс две крайние)."""
    print()
    print("─" * 78)
    print(" ЧИСЛО 2. УСПЕХ ПО КОРЗИНАМ ПОЛОЖЕНИЯ В РАЗМАХЕ")
    print("─" * 78)
    print("  Границы корзин ФИКСИРОВАНЫ по ширине 0.1 и от данных не зависят.")
    print("  Крайние корзины «ниже размаха» (pos<0) и «выше размаха» (pos>1) —")
    print("  это ПРОБИТИЕ, штатный случай: цена сигнала не входит в своё окно.")
    print("  buy и sell НЕ СМЕШИВАЮТСЯ: ожидаемая связь с pos у них")
    print("  противоположна по знаку, и в сумме они взаимно уничтожились бы.")
    labels = [pos_bucket_label(i) for i in range(POS_BUCKET_TOTAL)]
    summary: dict[str, Any] = {}
    for direction, horizon in sorted(outcomes):
        columns = outcomes[(direction, horizon)]
        print()
        print(f"  ══ {direction.upper()} · горизонт {horizon}ч ══")
        for independent_only in (False, True):
            data = _select(columns, independent_only=independent_only)
            title = SAMPLE_INDEPENDENT if independent_only else SAMPLE_ALL
            cells = build_cells(
                labels, data["pos_bucket"], data["is_target"], data["net"],
                POS_BUCKET_TOTAL,
            )
            print_cells(cells, title=title, first_column="корзина pos")
            summary[f"{direction}|{horizon}|"
                    f"{'independent' if independent_only else 'all'}"] = [
                {
                    "bucket": cell.label, "n": cell.n,
                    "target_share": cell.target_share, "mean": cell.mean,
                    "lo": cell.lo, "hi": cell.hi, "usable": cell.usable,
                }
                for cell in cells
            ]
    return summary


def print_number_three(
    outcomes: dict[tuple[str, int], OutcomeColumns],
) -> dict[str, Any]:
    """ЧИСЛО 3: ширина размаха как соперник положения."""
    print()
    print("─" * 78)
    print(" ЧИСЛО 3. ШИРИНА РАЗМАХА КАК СОПЕРНИК")
    print("─" * 78)
    print("  В узком диапазоне разница между верхом и низом бывает меньше")
    print("  круговых издержек, и положение в нём тогда не значит ничего.")
    summary: dict[str, Any] = {}
    for direction, horizon in sorted(outcomes):
        columns = outcomes[(direction, horizon)]
        print()
        print(f"  ══ {direction.upper()} · горизонт {horizon}ч ══")
        for independent_only in (False, True):
            data = _select(columns, independent_only=independent_only)
            title = SAMPLE_INDEPENDENT if independent_only else SAMPLE_ALL
            cells = build_cells(
                list(WIDTH_LABELS), data["width_bucket"], data["is_target"],
                data["net"], len(WIDTH_LABELS),
            )
            print_cells(cells, title=title, first_column="ширина размаха")
            key = (f"{direction}|{horizon}|"
                   f"{'independent' if independent_only else 'all'}")
            summary[key] = {
                "width": [
                    {
                        "bucket": cell.label, "n": cell.n,
                        "target_share": cell.target_share, "mean": cell.mean,
                        "usable": cell.usable,
                    }
                    for cell in cells
                ]
            }
            print(f"    совместная таблица «треть по pos × ширина» — {title}")
            summary[key]["joint"] = print_joint(
                data["tertile"], data["width_bucket"], data["is_target"],
                data["net"],
            )
    return summary


def print_number_four(
    outcomes: dict[tuple[str, int], OutcomeColumns],
) -> dict[str, Any]:
    """ЧИСЛО 4: три защиты от подгонки. ТОЛЬКО на независимой подвыборке."""
    print()
    print("─" * 78)
    print(" ЧИСЛО 4. ТРИ ЗАЩИТЫ ОТ ПОДГОНКИ (независимая подвыборка)")
    print("─" * 78)
    summary: dict[str, Any] = {}
    for direction, horizon in sorted(outcomes):
        columns = outcomes[(direction, horizon)]
        data = _select(columns, independent_only=True)
        keys, net, is_old = data["pos_bucket"], data["net"], data["is_old"]
        eligible = _eligible_buckets(keys, POS_BUCKET_TOTAL)
        print()
        print(f"  ══ {direction.upper()} · горизонт {horizon}ч · "
              f"N = {net.size} ══")
        print(f"    Корзины с N ≥ {MIN_BUCKET_N}: "
              f"{', '.join(pos_bucket_label(i) for i in eligible) or 'нет ни одной'}")

        block: dict[str, Any] = {"n": int(net.size), "eligible": eligible}

        # 1. Перестановочная проверка.
        permutation = permutation_spread(keys, net, eligible=eligible)
        block["permutation"] = permutation
        if permutation is None:
            print("    1) Перестановочная проверка: не считается — годных "
                  "корзин меньше двух.")
        else:
            print(f"    1) Перестановочная проверка, {permutation['resamples']} "
                  f"повторов:")
            print(f"         разброс лучшей и худшей корзины: наблюдён "
                  f"{permutation['observed_spread']:+.4f} п.п., "
                  f"случайный 95%: {permutation['random_spread_p95']:.4f}")
            print(f"         разница крайних по положению:     наблюдена "
                  f"{permutation['observed_edges']:+.4f} п.п., "
                  f"случайная 95%: {permutation['random_edges_p95']:.4f}")
            verdict_spread = (
                "ВЫШЕ случайного" if permutation["observed_spread"]
                > permutation["random_spread_p95"] else "НЕ выше случайного"
            )
            verdict_edges = (
                "ВЫШЕ случайной" if abs(permutation["observed_edges"])
                > permutation["random_edges_p95"] else "НЕ выше случайной"
            )
            print(f"         разброс {verdict_spread}; разница крайних "
                  f"{verdict_edges}")

        # 2. Интервалы по корзинам напечатаны в ЧИСЛЕ 2; здесь — сводка.
        crossing = 0
        not_crossing: list[str] = []
        for index in eligible:
            _mean, lo, hi = mean_interval(net[keys == index])
            if lo is None or hi is None:
                continue
            if lo <= 0.0 <= hi:
                crossing += 1
            else:
                not_crossing.append(pos_bucket_label(index))
        block["intervals_crossing_zero"] = crossing
        block["intervals_not_crossing"] = not_crossing
        print(f"    2) Интервалы по годным корзинам: пересекают ноль "
              f"{crossing} из {len(eligible)}; "
              f"не пересекают: {', '.join(not_crossing) or 'ни одна'}")

        # 3. Независимая половина по времени.
        halves = split_half(keys, net, is_old)
        block["split_half"] = halves
        print(f"    3) Половины по времени (граница "
              f"{BOUNDARY.date().isoformat()}): "
              f"старая {halves['old_n']}, новая {halves['new_n']}")
        best_old = halves["best_old"]
        best_new = halves["best_new"]
        print("         лучшая корзина старой половины: "
              + (pos_bucket_label(best_old) if best_old is not None
                 else "не определена (нет корзины с N ≥ 30)"))
        print("         лучшая корзина новой половины:  "
              + (pos_bucket_label(best_new) if best_new is not None
                 else "не определена (нет корзины с N ≥ 30)"))
        print("         ОТВЕТ 1 — лучшая корзина совпала: "
              + _yes_no(halves["same_bucket"]))
        print("         ОТВЕТ 2 — знак преимущества сохранён: "
              + _yes_no(halves["same_sign"]))
        summary[f"{direction}|{horizon}"] = block
    return summary


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "определить не на чем"
    return "ДА" if value else "НЕТ"


def print_number_five(
    outcomes: dict[tuple[str, int], OutcomeColumns],
    scan_counters: ScanCounters,
) -> dict[str, Any]:
    """ЧИСЛО 5: статистическая сила. Прямая строка о том, что различимо."""
    print()
    print("─" * 78)
    print(" ЧИСЛО 5. СТАТИСТИЧЕСКАЯ СИЛА")
    print("─" * 78)
    print(f"  Независимых сигналов: {scan_counters.independent} "
          f"из {scan_counters.computed} посчитанных "
          f"({_share(scan_counters.independent, scan_counters.computed)}).")
    print("  ИМЕННО ЭТО ЧИСЛО, А НЕ ПОЛНОЕ, определяет силу замера: соседние")
    print("  сигналы одного токена делят почти одно окно и почти один исход.")
    summary: dict[str, Any] = {}
    for direction, horizon in sorted(outcomes):
        data = _select(outcomes[(direction, horizon)], independent_only=True)
        keys, net = data["pos_bucket"], data["net"]
        eligible = _eligible_buckets(keys, POS_BUCKET_TOTAL)
        small = [
            pos_bucket_label(i) for i in range(POS_BUCKET_TOTAL)
            if 0 < int((keys == i).sum()) < MIN_BUCKET_N
        ]
        mde = detectable_difference(keys, net, eligible)
        summary[f"{direction}|{horizon}"] = {
            "n": int(net.size), "eligible": len(eligible),
            "min_detectable_pp": mde, "small_buckets": small,
        }
        print()
        print(f"  ══ {direction.upper()} · горизонт {horizon}ч ══")
        print(f"    Наблюдений: {net.size}; корзин с N ≥ {MIN_BUCKET_N}: "
              f"{len(eligible)} из {POS_BUCKET_TOTAL}")
        if small:
            print(f"    Корзины с N < {MIN_BUCKET_N} (помечены в таблицах, "
                  f"в выводы не идут): {', '.join(small)}")
        if mde is None:
            print("    РАЗЛИЧИМО: ничего. Годных корзин меньше двух — сравнивать")
            print("    крайние по положению не с чем.")
        else:
            print("    РАЗЛИЧИМО: разница средних между крайними годными "
                  "корзинами")
            print(f"    от {mde:.3f} п.п. и больше. Разница МЕНЬШЕ этой на такой")
            print("    выборке неотличима от нуля, сколько бы её ни обсуждали.")
    return summary


def _share(part: int, total: int) -> str:
    return "—" if total == 0 else f"{part / total * 100.0:.1f}%"


def memory_report(scan_counters: ScanCounters, outcome_counters: OutcomeCounters,
                  window_bars: int) -> dict[str, Any]:
    """Пиковая память и оценка при росте выборки втрое (§6 ТЗ)."""
    peak = peak_rss_mb()
    limit = cgroup_memory_limit_mb()
    numbers_mb = (
        scan_counters.computed * BYTES_PER_SIGNAL
        + outcome_counters.matched * BYTES_PER_OUTCOME
    ) / 2**20
    window_mb = window_bars * 40 / 2**20
    grown_mb = numbers_mb * GROWTH_FACTOR + window_mb
    print()
    print("─" * 78)
    print(" ПАМЯТЬ")
    print("─" * 78)
    print(f"  Пиковая память процесса:              {peak:,.0f} МБ")
    print("  Лимит контейнера:                     "
          + (f"{limit:,.0f} МБ" if limit else "не задан / не читается"))
    print(f"  Из них на числа выборки:              {numbers_mb:,.1f} МБ")
    print(f"  Окно баров (не растёт с выборкой):    {window_mb:,.1f} МБ")
    print(f"  Оценка чисел при росте выборки в {GROWTH_FACTOR} раза: "
          f"{grown_mb:,.1f} МБ")
    print("  ОКНО БАРОВ ОТ ОБЪЁМА ВЫБОРКИ НЕ ЗАВИСИТ: его размер задан семью")
    print("  сутками, а не числом сигналов. Растёт только таблица чисел.")
    if limit:
        budget = limit * MEMORY_BUDGET_SHARE
        verdict = "укладывается" if grown_mb < budget else "НЕ УКЛАДЫВАЕТСЯ"
        print(f"  Бюджет ({MEMORY_BUDGET_SHARE:.0%} лимита): {budget:,.0f} МБ — "
              f"при росте втрое {verdict}")
    return {
        "peak_rss_mb": round(peak, 1),
        "limit_mb": None if limit is None else round(limit, 1),
        "numbers_mb": round(numbers_mb, 2),
        "window_mb": round(window_mb, 2),
        "grown_mb": round(grown_mb, 2),
    }


# ----------------------------------------------------------------------------
# ЗАПИСЬ. Единственная таблица, в которую этот этап пишет.
# ----------------------------------------------------------------------------

WRITE_BATCH = 5_000


async def write_rows(
    scan: ScanResult, *, window_days: int, resolution: str
) -> int:
    """Выгрузка ``ScanResult`` в ``signal_range_position`` порциями.

    СТРОКИ СОБИРАЮТСЯ ИЗ МАССИВОВ ПОРЦИЯМИ, а не списком целиком: список
    словарей на сотни тысяч сигналов стоил бы сотни мегабайт ради одного
    прохода по нему, и это ровно та ошибка, которой Этап 9.1.3 стоил двух
    убийств ядром.
    """
    total = len(scan)
    written = 0
    ids = scan.signal_id.values
    lows = scan.range_low.values
    highs = scan.range_high.values
    widths = scan.width_pct.values
    positions = scan.pos.values
    last_bars = scan.last_bar_epoch.values
    bars = scan.bars_in_window.values
    for start in range(0, total, WRITE_BATCH):
        stop = min(start + WRITE_BATCH, total)
        rows = [
            {
                "signal_id": int(ids[i]),
                "window_days": int(window_days),
                "range_low": float(lows[i]),
                "range_high": float(highs[i]),
                "range_width_pct": float(widths[i]),
                "pos": float(positions[i]),
                "last_bar_ts": datetime.fromtimestamp(float(last_bars[i]), UTC),
                "bars_in_window": int(bars[i]),
                "resolution": resolution,
            }
            for i in range(start, stop)
        ]
        written += await db.save_signal_range_position(rows)
    return written


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Замер: связано ли положение цены в недельном размахе с успехом "
            "сигнала (Этап 9.1.5). Без --apply — только печать."
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="записать положения в signal_range_position",
    )
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS, metavar="N",
        help=f"ширина окна размаха в сутках (по умолчанию {DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--json", default=None, metavar="PATH",
        help="дополнительно сохранить сводку машиночитаемо",
    )
    args = parser.parse_args()
    if args.window_days <= 0:
        parser.error(f"--window-days обязан быть положительным: {args.window_days}")

    setup_logging()
    await db.connect()
    try:
        return await _run(args)
    finally:
        await db.close()


async def _run(args: argparse.Namespace) -> int:
    window_days = int(args.window_days)
    settle = settle_seconds()
    timeframe = settings.BARRIER_FINE_TIMEFRAME
    shift_sec = float(WINDOW_SHIFT_BARS) * float(BAR_SECONDS[timeframe])
    now = datetime.now(UTC)

    print("=" * 78)
    print(" ЭТАП 9.1.5. ПОЛОЖЕНИЕ ЦЕНЫ В НЕДЕЛЬНОМ РАЗМАХЕ И ИСХОД СИГНАЛА")
    print("=" * 78)
    print(f"  Окно размаха:            {window_days} сут, ряд {timeframe}")
    print(f"  Годность бара:           закрылся строго до сигнала "
          f"(bar_ts + {BAR_SECONDS[timeframe]} с < signal_ts)")
    print(f"                           и окончателен "
          f"(bar_ts + settle_seconds()={settle} с ≤ now)")
    print("  Цена сигнала:            signal_targets.price_at_signal "
          "(замороженная)")
    print("  Исход:                   signal_outcomes_barrier.outcome / "
          "net_pnl_pct (Этап 8.8)")
    print(f"  LOGIC_VERSION системы:   {settings.LOGIC_VERSION} "
          f"(этим этапом НЕ меняется)")
    if WINDOW_SHIFT_BARS:
        print("  " + "!" * 70)
        print(f"  !! КОНТРОЛЬНЫЙ ОПЫТ: окно сдвинуто вперёд на "
              f"{WINDOW_SHIFT_BARS} грубых бара(ов).")
        print("  !! Это НЕ боевой расчёт. Проверка на подглядывание обязана "
              "упасть.")
        print("  " + "!" * 70)

    if args.apply and not await db.signal_range_position_exists():
        print()
        print("  Таблицы signal_range_position нет: примените миграцию 023")
        print("  (db/migrations/023_signal_range_position.sql) и повторите.")
        print("  Ни одной строки не записано.")
        _log.error(
            "Замер размаха: нет таблицы", rangepos_rows_written=0,
            peak_rss_mb=round(peak_rss_mb(), 1),
        )
        print()
        print(DONE_MARKER)
        return 2

    signal_counts = await db.count_range_position_signals()
    instruments = await db.get_range_position_instruments(timeframe=timeframe)

    scan = ScanResult()
    counters = ScanCounters()
    for instrument in instruments:
        await scan_instrument(
            instrument,
            window_days=window_days,
            settle=settle,
            shift_sec=shift_sec,
            timeframe=timeframe,
            now=now,
            result=scan,
            counters=counters,
        )

    # ---- БЛОКИРУЮЩАЯ ПРОВЕРКА НА ПОДГЛЯДЫВАНИЕ (§2.2 ТЗ) -------------------
    # Она стоит ДО печати таблиц и ДО записи намеренно. Замер с подглядыванием
    # даёт КРАСИВЫЙ результат: связь находится сильная и ложная. Напечатать её
    # «с оговоркой» значило бы выпустить в отчёт число, которое будет
    # процитировано без оговорки.
    if counters.lookahead_violations:
        print()
        print("  " + "!" * 70)
        print(f"  !! ПРОВЕРКА НА ПОДГЛЯДЫВАНИЕ НЕ ПРОШЛА: "
              f"{counters.lookahead_violations} нарушений.")
        print("  !! Таблицы НЕ печатаются, в базу НЕ записано ни одной строки.")
        for example in counters.violation_examples:
            print(f"  !!   {example}")
        print("  " + "!" * 70)
        _log.error(
            "Замер размаха: подглядывание в будущее",
            rangepos_signals_total=counters.signals_seen,
            rangepos_skipped_short_history=counters.skipped_short_history,
            rangepos_lookahead_violations=counters.lookahead_violations,
            rangepos_independent_n=counters.independent,
            rangepos_rows_written=0,
            peak_rss_mb=round(peak_rss_mb(), 1),
        )
        print()
        print(DONE_MARKER)
        return 2

    if not len(scan):
        print()
        print("  Выборка пуста: ни у одного сигнала нет полного окна.")
        print(f"  Рассмотрено сигналов: {counters.signals_seen}; "
              f"исключено по короткой истории: {counters.skipped_short_history}.")
        _log.info(
            "Замер размаха: выборка пуста",
            rangepos_signals_total=counters.signals_seen,
            rangepos_skipped_short_history=counters.skipped_short_history,
            rangepos_lookahead_violations=0,
            rangepos_independent_n=0,
            rangepos_rows_written=0,
            peak_rss_mb=round(peak_rss_mb(), 1),
        )
        print()
        print(DONE_MARKER)
        return 3

    outcomes, outcome_counters = await stream_outcomes(scan)
    if not outcomes:
        print()
        print("  Положения посчитаны, но ни у одного сигнала нет ИЗМЕРЕННОГО")
        print("  исхода: считать таблицы не по чему.")
        _log.info(
            "Замер размаха: исходов нет",
            rangepos_signals_total=counters.signals_seen,
            rangepos_skipped_short_history=counters.skipped_short_history,
            rangepos_lookahead_violations=0,
            rangepos_independent_n=counters.independent,
            rangepos_rows_written=0,
            peak_rss_mb=round(peak_rss_mb(), 1),
        )
        print()
        print(DONE_MARKER)
        return 3

    print_number_one(
        signal_counts, counters, outcome_counters,
        window_days=window_days, settle=settle,
    )
    table_two = print_number_two(outcomes)
    table_three = print_number_three(outcomes)
    table_four = print_number_four(outcomes)
    table_five = print_number_five(outcomes, counters)

    written = 0
    if args.apply:
        written = await write_rows(
            scan, window_days=window_days, resolution=RESOLUTION_1M
        )
        print()
        print(f"  Записано строк в signal_range_position: {written}")
    else:
        print()
        print("  Ничего не записано: без --apply скрипт только считает.")

    memory = memory_report(counters, outcome_counters, counters.max_window_bars)

    print()
    print("─" * 78)
    print(" ГРАНИЦА ЭТАПА")
    print("─" * 78)
    print("  Ни одно правило системы не изменено. LOGIC_VERSION остаётся "
          f"{settings.LOGIC_VERSION}.")
    print("  Рекомендация «внедрить фильтр по положению в диапазоне» этим")
    print("  этапом ЗАПРЕЩЕНА (§1 ТЗ) и здесь не даётся ни в каком виде.")
    print(f"  ПРИЗНАК ЗАВЕРШЕНИЯ: строка «{DONE_MARKER}» в самом конце вывода.")

    _log.info(
        "Замер размаха: расчёт завершён",
        rangepos_signals_total=counters.signals_seen,
        rangepos_skipped_short_history=counters.skipped_short_history,
        rangepos_lookahead_violations=counters.lookahead_violations,
        rangepos_independent_n=counters.independent,
        rangepos_rows_written=written,
        peak_rss_mb=memory["peak_rss_mb"],
    )

    if args.json:
        summary = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "window_days": window_days,
            "settle_seconds": settle,
            "resolution": RESOLUTION_1M,
            "logic_version": int(settings.LOGIC_VERSION),
            "independent_window_sec": INDEPENDENT_WINDOW_SEC,
            "boundary": BOUNDARY.isoformat(),
            "min_bucket_n": MIN_BUCKET_N,
            "composition": {
                "directional_total": signal_counts["directional_total"],
                "with_outcome": signal_counts["with_outcome"],
                "signals_seen": counters.signals_seen,
                "skipped_short_history": counters.skipped_short_history,
                "skipped_no_bars": counters.skipped_no_bars,
                "skipped_flat_range": counters.skipped_flat_range,
                "computed": counters.computed,
                "independent": counters.independent,
                "by_token": counters.by_token,
                "by_version": {str(k): v for k, v in counters.by_version.items()},
                "by_direction": counters.by_direction,
                "outcome_rows": outcome_counters.matched,
                "outcome_unmeasured": outcome_counters.unmeasured,
                "outcome_by_horizon": {
                    str(k): v for k, v in outcome_counters.by_horizon.items()
                },
            },
            "number_two_pos_buckets": table_two,
            "number_three_width": table_three,
            "number_four_defences": table_four,
            "number_five_power": table_five,
            "memory": memory,
            "rows_written": written,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
        print(f"  Сводка сохранена: {args.json}")

    print()
    print(DONE_MARKER)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
