#!/usr/bin/env python3
"""ЗАМЕР 0, шаги 4 и 5: контроль совпадения и контроль незакрытых баров. БЛОКИРУЮЩИЕ.

ТОЛЬКО ЧТЕНИЕ И ПЕЧАТЬ. Сеть не нужна: обе сравниваемые стороны уже в базе.
Ни одного запроса на запись этот скрипт не делает вообще — ни с ключом, ни без.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ (§7 ТЗ):

  дневная свеча, собранная из НАШИХ часовых по границам UTC, совпадает с
  дневной свечой БИРЖИ ``1Dutc``; недельная, собранная из дневных, — с
  ``1Wutc``; месячная, собранная из дневных, — с ``1Mutc``.

  ``open``  — открытие первого младшего бара периода;
  ``high``  — максимум по периоду;
  ``low``   — минимум по периоду;
  ``close`` — закрытие последнего младшего бара периода;
  ``volume``— сумма.

СУТКИ С НЕПОЛНЫМ ПОКРЫТИЕМ — НЕ РАСХОЖДЕНИЕ. Считаются только периоды, где
присутствуют ВСЕ младшие бары (24 часа в сутках, 7 суток в неделе, календарное
число суток в месяце). Неполные исключаются и попадают в ОТДЕЛЬНЫЙ счётчик:
выдать пропуск за расхождение значило бы объявить ошибкой то, что этап обязан
просто измерить.

ДОПУСК (Замер 0.2). Расхождение допустимо, если его величина не превышает
ЕДИНИЦУ ПОСЛЕДНЕГО ЗНАЧАЩЕГО ЗНАКА той стороны, которая записана ГРУБЕЕ.
Числа в правиле нет: допуск считается для КАЖДОГО ПОЛЯ КАЖДОГО БАРА по тому, с
какой точностью это поле записано (``backtest.htf.write_tolerance``). Знаки
считаются по ТЕКСТОВОЙ записи, а не по float: биржевая сторона — строка, в
каком виде она лежит, наша — ``value::text`` значения ``NUMERIC``; незначащие
нули справа от точки отбрасываются, слева — нет (``7130`` — ноль знаков и
допуск 1, а не 10). Сравнение СТРОГОЕ: равенство границе допустимо.

  * O/H/L/C — БЛОКИРУЮЩЕЕ, по правилу выше, каждое поле отдельно: число знаков
    у разных полей одного бара различается;
  * объём — на первом прогоне НЕ блокирующий. Печатаются фактические
    максимальное и медианное отклонения; порог назначается по ИЗМЕРЕННОЙ
    величине в отчёте, а не угадывается здесь. Сумма многих часовых значений
    может разойтись с биржевым в последних знаках из-за округления на стороне
    биржи.

ПОЧЕМУ ПРЕЖНИЕ ПРАВИЛА ЗАМЕНЕНЫ. Прогон 11.09.2026 с относительным допуском
дал 105 расхождений, и 98 из них — разная точность записи у SOL и DOGE на
недельном и месячном масштабах. Ни абсолютная мерка, ни относительная на эти
данные не ложатся: измеряемое явление — точность ЗАПИСИ, и с величиной цены
она не связана. Разбор — ZAMER_0_2_REPORT.md.

ОТЧЁТ печатается в stdout И сохраняется копией в ``reports/`` с датой в имени.
Поглощённые допуском отклонения построчно НЕ печатаются — только количеством в
сводке; ``--verbose`` печатает и их, для ручной проверки правила.

ДВА КОНТРОЛЬНЫХ ОПЫТА, И ОБА ОБЯЗАНЫ УПАСТЬ:

  §7 — окно сборки сдвигается на ОДИН ЧАС вперёд, и сравнение обязано дать
       НЕНУЛЕВОЕ число расхождений. Ноль при сдвинутом окне означает, что
       проверка ничего не проверяет, и этап не сдан;
  §8 — в проверку хранилища подаётся бар, период которого ещё не закрылся, и
       счётчик обязан стать ненулевым. Ноль здесь означает, что проверка слепая.

ТРЕТИЙ КОНТРОЛЬНЫЙ ОПЫТ — НАД САМИМ ДОПУСКОМ (Замер 0.2). Подаются ТРИ
фактических примера из §3 ТЗ — SOL 2,54 против 2,548 (обязан быть поглощён),
ETH 1196,00 против 1194,60 и BTC 7388,50 против 7131,90 (обязаны быть
засчитаны расхождением) — и прогоняются через ТО ЖЕ правило, которым считается
результат. Опыт со сдвигом окна показывает лишь, что сравнение видит разные
данные, и прошёл бы при допуске, задранном на порядок.

ПОЧЕМУ КОНТРОЛЬНЫЙ ОПЫТ §8 НЕ ПИШЕТ СТРОКУ В БАЗУ. Этот скрипт только читает;
запись ради проверки означала бы, что «только чтение» — не свойство, а
намерение. Незакрытый бар подаётся в ТУ ЖЕ функцию :func:`unclosed_rows`,
которой проверено хранилище, вместе с настоящими строками. Что незакрытый бар
не проходит В САМУ ТАБЛИЦУ, доказывается отдельно и на настоящей базе —
tests/test_zamer_0.py, где принудительная вставка такой строки роняет проверку.

ПАМЯТЬ (§10 ТЗ). Часовой ряд идёт ПОТОКОМ, одним проходом по времени на
инструмент: в памяти живёт одно накапливаемое ведро. Накапливаются только
отклонения по СТАРШИМ барам — их тысячи, а не миллионы, и эта граница названа
явно, а не подразумевается.

КОДЫ ВОЗВРАТА (§10 ТЗ), в порядке старшинства:
  5 — выборка пуста: сравнивать нечего, и остальные числа смысла не имеют;
  4 — контрольный опыт НЕ упал: проверка слепая, и её ноль ничего не значит;
  3 — незакрытые бары в хранилище;
  2 — расхождение O/H/L/C;
  0 — всё сошлось.

ЗАПУСК (внутри собранного образа), одной командой:

    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run --rm -e PYTHONUNBUFFERED=1 backtest python scripts/htf_parity_z0.py

``--verbose`` последним аргументом печатает и поглощённые допуском отклонения.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog  # noqa: E402

from backtest import db  # noqa: E402
from backtest.config import ConfigError  # noqa: E402
from backtest.htf import (  # noqa: E402
    BAR_HOURLY,
    OHLC_FIELDS,
    AssembledBar,
    Deviation,
    HtfError,
    SourceBar,
    StoredBar,
    make_aggregator,
    measure_boundaries,
    month_anchor_day,
    next_period_start,
    numeric_text,
    unclosed_rows,
    week_anchor_weekday,
)
from backtest.htf_loader import stream_source_bars, stream_stored_htf  # noqa: E402
from backtest.z0_metrics import metrics_path, write_metrics  # noqa: E402
from scripts.load_htf_z0 import MEMORY_CAP_MB, read_settings  # noqa: E402
from scripts.stop_counterfactual_9_1_4 import peak_rss_mb  # noqa: E402
from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402

_log = structlog.get_logger().bind(component="z0_parity")

CONFIG_PATH = Path("backtest/.env.backtest")

# Из чего собирается контроль каждого масштаба. Дневной — из ЧАСОВЫХ (это и
# есть главная сверка §7). Недельный и месячный — из ДНЕВНЫХ, как прямо
# сказано в §7 ТЗ: собирать их из часовых значило бы проверять дневную сборку
# трижды и ни разу — недельную границу.
ASSEMBLED_FROM: dict[str, str] = {
    "1Dutc": BAR_HOURLY,
    "1Wutc": "1Dutc",
    "1Mutc": "1Dutc",
}

# Сдвиг окна для контрольного опыта §7. Один час — наименьший сдвиг, который
# вообще различим на часовом ряде; больший сдвиг сделал бы опыт легче.
CONTROL_SHIFT = timedelta(hours=1)

WEEKDAY_NAMES = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)

# Каталог, куда кладётся копия отчёта. Дата в имени — чтобы прогоны не
# затирали друг друга: сравнить сегодняшний список с прошлым иначе будет нечем.
REPORTS_DIR = Path("reports")

# --- Рубеж 2019→2020 (§5, блок C ТЗ) ---------------------------------------
#
# Момент, о который спотыкается биржевой ряд: у BTC и ETH открытие бара,
# накрывающего смену года, не сходится с закрытием предыдущего бара — при том
# что НАШ ряд стыкуется сам с собой и здесь. Механизм НЕ УСТАНОВЛЕН, и отчёт
# его не постулирует: метка нужна затем, чтобы эти находки было видно отдельно
# от прочих, а не затем, чтобы их объяснить.
#
# Бар считается попавшим на рубеж, если момент смены года лежит В ЕГО ПЕРИОДЕ,
# включая оба конца: месячный бар декабря 2019 кончается ровно в этот момент,
# недельный бар с 30.12.2019 накрывает его серединой, январский начинается с
# него. Условие названо через КАЛЕНДАРЬ периода, а не перечнем дат: перечень
# пришлось бы править при каждом новом инструменте.
YEAR_TURN = datetime(2020, 1, 1, tzinfo=UTC)
YEAR_TURN_INSTRUMENTS: tuple[str, ...] = ("BTC", "ETH")

CATEGORY_BOUNDARY = "BOUNDARY_ANOMALY"
CATEGORY_REAL = "REAL"


# --- Печать: одновременно в stdout и в копию отчёта -------------------------
#
# Копия набирается ТЕМИ ЖЕ строками, что печатаются. Второй проход, собирающий
# отчёт заново, однажды разошёлся бы с выводом — и разошёлся бы молча.
_TRANSCRIPT: list[str] = []


def say(text: str = "") -> None:
    """Печатает строку и запоминает её для файла отчёта."""
    print(text, flush=True)
    _TRANSCRIPT.append(text)


def report_path(started: datetime) -> Path:
    """Имя файла копии отчёта: с датой прогона, без времени."""
    return REPORTS_DIR / f"zamer_0_2_parity_{started:%Y-%m-%d}.md"


def save_report(started: datetime) -> Path | None:
    """Кладёт копию напечатанного в ``reports/``. Неудача прогон не роняет."""
    path = report_path(started)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(_TRANSCRIPT) + "\n", encoding="utf-8")
    except OSError as exc:
        say(f"\n  🟡 копия отчёта НЕ записана в {path}: {exc}. Прогон это не "
            "отменяет — вывод выше полон.")
        return None
    return path


def category_of(inst_id: str, bar: str, open_time: datetime) -> str:
    """Метка находки (§5, блок C ТЗ). Метка, а не фильтр: не прячет ничего."""
    ticker = inst_id.split("-")[0].upper()
    if ticker not in YEAR_TURN_INSTRUMENTS:
        return CATEGORY_REAL
    if open_time <= YEAR_TURN <= next_period_start(bar, open_time):
        return CATEGORY_BOUNDARY
    return CATEGORY_REAL


@dataclass(frozen=True)
class Finding:
    """Одно расхождение ПО ОДНОМУ ПОЛЮ. Единица поимённого списка (§5, блок B).

    Хранится вместе с категорией и текстами сторон: строка отчёта обязана
    собираться из того же, чем посчитан вердикт, а не из пересчитанного заново.
    """

    deviation: Deviation
    category: str

    @property
    def line(self) -> str:
        item = self.deviation
        return (
            f"{item.inst_id} {item.bar} {item.open_time:%Y-%m-%d} {item.field}: "
            f"наша={item.text_assembled} биржа={item.text_stored} | "
            f"знаков: наша={item.decimals_assembled} биржа={item.decimals_stored} | "
            f"допуск={item.tolerance} | отклонение={item.absolute} | "
            f"категория={self.category}"
        )


def affected_bars(findings: list[Finding]) -> list[str]:
    """Уникальные бары (тикер + таймфрейм + дата), в которых есть расхождение.

    ДВЕ МЕРКИ РЯДОМ, И ЭТО НЕ ДУБЛИРОВАНИЕ. Правило Замера 0.2 считает ПОЛЯ
    (§4.6 ТЗ), а ожидание приёмки взято из отчёта Замера 0.1, который считал
    БАРЫ. Печатая обе величины, сводка позволяет сверить результат со старой
    меркой, не трогая новую. Бар с двумя разошедшимися полями даёт два
    расхождения и один затронутый бар — и оба числа названы прямо, вместо того
    чтобы читатель гадал, какое из них перед ним.

    Порядок — тот же, в каком бары встретились в списке блока B; повторы
    убраны. Сортировка здесь была бы вторым порядком рядом с первым, и списки
    двух блоков перестали бы читаться друг за другом.
    """
    seen: dict[str, None] = {}
    for finding in findings:
        item = finding.deviation
        seen.setdefault(
            f"{item.inst_id} {item.bar} {item.open_time:%Y-%m-%d}", None
        )
    return list(seen)


@dataclass(frozen=True)
class YearTurnLink:
    """Звено цепочки блока D: предыдущий бар, спорный бар, наша сборка.

    Только факты и ни одного вывода: механизм рассогласования не установлен, и
    объяснять его в отчёте нечем.
    """

    inst_id: str
    bar: str
    open_time: datetime
    previous_open_time: datetime | None
    previous_close_stored: str | None
    open_stored: str
    open_assembled: str
    # Дал ли ЭТОТ бар расхождение. Цепочка печатается для ВСЕХ баров рубежа, и
    # сошедшийся бар рядом с разошедшимся — тоже факт: без него не видно, что
    # спотыкается не весь участок, а именно эти точки.
    has_mismatch: bool


@dataclass
class ParityTally:
    """Итог сравнения одного масштаба одного инструмента.

    СЧИТАЮТСЯ ПОЛЯ, А НЕ БАРЫ. Правило Замера 0.2 применяется к каждому полю
    отдельно (§4.6 ТЗ): число знаков у ``open`` и у ``low`` одного бара
    различается, и бар, засчитанный целиком, скрыл бы, какое именно поле
    разошлось. Поимённый список отчёта — тоже по полям.
    """

    inst_id: str
    bar: str
    compared: int = 0
    skipped_incomplete: int = 0
    missing_counterpart: int = 0
    # Расхождения по правилу Замера 0.2, по одному на ПОЛЕ. Это то число, по
    # которому этап принимает решение, и оно же — длина списка блока B.
    findings: list[Finding] = field(default_factory=list)
    # Отклонения НЕНУЛЕВЫЕ, но уложившиеся в допуск. Построчно не печатаются
    # (§5, блок E ТЗ) — только количеством; ``--verbose`` печатает и их.
    absorbed: list[Deviation] = field(default_factory=list)
    # Из поглощённых — те, у которых стороны записаны с РАЗНОЙ точностью.
    # Отдельным числом намеренно: «поглощено» и «поглощено из-за точности
    # записи» — разные утверждения, и смешать их значило бы спрятать второе.
    absorbed_precision: int = 0
    year_turn: list[YearTurnLink] = field(default_factory=list)
    volume_rel: list[float] = field(default_factory=list)
    volume_abs: list[float] = field(default_factory=list)

    @property
    def mismatches(self) -> int:
        return len(self.findings)


class StoredCursor:
    """Слияние двух упорядоченных рядов: собранного и загруженного.

    Один запрос на весь ряд вместо одного запроса на бар. Оба ряда идут по
    возрастанию времени, поэтому загруженный достаточно ПРОМАТЫВАТЬ вперёд до
    искомой метки — это O(1) по памяти и один проход по времени, как требует
    §10 ТЗ. Восемь тысяч отдельных запросов дали бы тот же ответ ценой
    восьми тысяч обращений к базе.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._current: SourceBar | None = None
        self._previous: SourceBar | None = None
        self._exhausted = False

    async def close(self) -> None:
        """Закрывает курсор базы, даже если ряд не досмотрен до конца.

        Загруженный ряд читается слиянием и почти никогда не досматривается:
        сборка кончается раньше, и генератор остаётся с открытым курсором и
        занятым соединением пула (``max_size=4``).

        ЧЕСТНО О ТОМ, ЧЕГО ЭТО НЕ ЧИНИТ. Прогон на шести инструментах без
        явного закрытия ПРОШЁЛ (замер на стенде 09.09.2026): CPython считает
        ссылки и освобождает генератор сразу по выходу из ``compare_series``,
        так что соединение возвращается в пул само. То есть наблюдаемой утечки
        не было, и утверждать обратное было бы преувеличением.

        Закрытие всё равно явное — по двум причинам. Освобождение по счётчику
        ссылок есть свойство реализации, а не языка, и оно перестаёт быть
        мгновенным, как только на генератор сошлётся что-нибудь ещё
        (исключение с трассировкой, отладчик, задача сборки). А цена ошибки
        несимметрична: три строки в ``finally`` против прогона, вставшего на
        выдаче соединения без единого сообщения о причине.
        """
        await self._stream.aclose()

    async def at(self, moment: datetime) -> SourceBar | None:
        """Загруженный бар с меткой ``moment`` либо ``None``, если его нет."""
        while not self._exhausted and (
            self._current is None or self._current.ts < moment
        ):
            if self._current is not None:
                self._previous = self._current
            try:
                self._current = await anext(self._stream)
            except StopAsyncIteration:
                self._exhausted = True
                self._current = None
        if self._current is not None and self._current.ts == moment:
            return self._current
        return None

    @property
    def previous(self) -> SourceBar | None:
        """Загруженный бар, НЕПОСРЕДСТВЕННО предшествующий последнему искомому.

        Нужен ровно для блока D отчёта: цепочка «закрытие предыдущего бара →
        открытие спорного» читается только из соседних баров ТОГО ЖЕ ряда.
        Отдельного запроса за ним нет — слияние уже прошло через него.
        """
        return self._previous


async def compare_series(
    inst_id: str,
    bar: str,
    *,
    week_weekday: int | None,
    month_day: int | None,
    shift: timedelta = timedelta(0),
) -> ParityTally:
    """Сверяет собранный ряд с загруженным. Один проход по времени, память O(1).

    ``shift`` — контрольный опыт §7 и ничто иное. Он проходит через ТУ ЖЕ
    сборку, которой считается результат: контрольный опыт над копией кода
    проверял бы копию.
    """
    tally = ParityTally(inst_id=inst_id, bar=bar)
    stored = StoredCursor(stream_source_bars(inst_id, bar))
    sources = stream_source_bars(inst_id, ASSEMBLED_FROM[bar])
    built_stream = assemble_stream(
        bar, sources, week_weekday=week_weekday, month_day=month_day, shift=shift
    )
    try:
        await _walk(tally, inst_id, built_stream, stored)
    finally:
        # Оба курсора закрываются ЯВНО: см. StoredCursor.close.
        await stored.close()
        await built_stream.aclose()
        await sources.aclose()
    return tally


async def _walk(
    tally: ParityTally,
    inst_id: str,
    built_stream: Any,
    stored: StoredCursor,
) -> None:
    """Слияние двух упорядоченных рядов: тело сравнения без управления курсорами."""
    async for built in built_stream:
        if not built.is_complete:
            # Неполный период — НЕ расхождение (§7 ТЗ): это пропуск в младшем
            # ряде, который этап обязан измерить, а не объявить ошибкой.
            tally.skipped_incomplete += 1
            continue
        counterpart = await stored.at(built.open_time)
        if counterpart is None:
            # У биржи такого бара нет вовсе — тоже не расхождение, но и не
            # сравнение. Отдельный счётчик: смешать его с «сверено» значило бы
            # завысить объём проверенного.
            tally.missing_counterpart += 1
            continue
        tally.compared += 1
        _compare(inst_id, built, counterpart, stored.previous, tally)


def _compare(
    inst_id: str,
    built: AssembledBar,
    stored: SourceBar,
    previous: SourceBar | None,
    tally: ParityTally,
) -> None:
    """Сверяет один бар ПО ПОЛЯМ и наполняет счётчики ``tally``.

    Правило одно и живёт в :attr:`Deviation.is_mismatch`: отклонение больше
    единицы последнего значащего знака ГРУБОЙ стороны. Ни числа, ни второго
    правила здесь нет, и появиться им неоткуда.

    Каждое поле считается ОТДЕЛЬНО (§4.6 ТЗ). Ненулевое отклонение, уложившееся
    в допуск, не теряется: оно попадает в ``absorbed`` и живёт в сводке числом.

    Объём считается ВСЕГДА и в решение о расхождении не входит: на первом
    прогоне он не блокирующий (§7 ТЗ), и смешать его с O/H/L/C значило бы
    заблокировать этап округлением на стороне биржи.
    """
    category = category_of(inst_id, built.bar, built.open_time)
    for name in OHLC_FIELDS:
        deviation = Deviation(
            inst_id=inst_id, bar=built.bar, open_time=built.open_time, field=name,
            assembled=getattr(built, name), stored=getattr(stored, name),
        )
        if deviation.is_mismatch:
            tally.findings.append(Finding(deviation=deviation, category=category))
        elif deviation.absolute > 0:
            tally.absorbed.append(deviation)
            if deviation.is_precision_difference:
                tally.absorbed_precision += 1
    if category == CATEGORY_BOUNDARY:
        touched = any(
            item.deviation.open_time == built.open_time for item in tally.findings
        )
        tally.year_turn.append(
            YearTurnLink(
                inst_id=inst_id, bar=built.bar, open_time=built.open_time,
                previous_open_time=previous.ts if previous is not None else None,
                previous_close_stored=(
                    numeric_text(previous.close) if previous is not None else None
                ),
                open_stored=numeric_text(stored.open),
                open_assembled=numeric_text(built.open),
                has_mismatch=touched,
            )
        )
    volume = Deviation(
        inst_id=inst_id, bar=built.bar, open_time=built.open_time, field="volume",
        assembled=built.volume, stored=stored.volume,
    )
    tally.volume_abs.append(float(volume.absolute))
    tally.volume_rel.append(float(volume.relative))


async def assemble_stream(
    bar: str,
    sources: Any,
    *,
    week_weekday: int | None,
    month_day: int | None,
    shift: timedelta,
):
    """Асинхронная подача младших баров в сборку. В памяти — одно ведро.

    Сама сборка живёт в ``backtest/htf.py`` и о базе с asyncio не знает: она
    обязана проверяться тестами без стенда. Здесь только подача — ни один
    список не накапливается.
    """
    aggregator = make_aggregator(bar, week_weekday=week_weekday, month_day=month_day)
    async for item in sources:
        moved = item if not shift else SourceBar(
            ts=item.ts + shift, open=item.open, high=item.high,
            low=item.low, close=item.close, volume=item.volume,
        )
        done = aggregator.push(moved)
        if done is not None:
            yield done
    tail = aggregator.flush()
    if tail is not None:
        yield tail


async def measure_stored_boundaries(inst_id: str, bar: str) -> dict[str, Any]:
    """Границы недели и месяца — из ФАКТИЧЕСКИХ меток биржи, лежащих в базе.

    Сеть для этого не нужна: сохранённые бары ``1Wutc`` и ``1Mutc`` и есть
    метки биржи. Ни «понедельник», ни «первое число» в коде не написаны — §3 ТЗ
    требует брать их из замера.
    """
    stamps: list[datetime] = []
    async for item in stream_source_bars(inst_id, bar):
        stamps.append(item.ts)
    return measure_boundaries(bar, stamps)


# ТРИ ФАКТИЧЕСКИХ ПРИМЕРА ИЗ §3 ТЗ. Это не придуманные величины: каждая пара
# взята из разбора прогона 11.09.2026 и приведена в ТЗ вместе с вердиктом,
# который правило ОБЯЗАНО воспроизвести. Записаны они текстом ровно так, как
# лежат в базе — ``NUMERIC(20,8)`` дополняет масштаб нулями, и именно на этих
# записях правило и работает: незначащие нули отбрасываются им самим.
TOLERANCE_EXAMPLES: tuple[tuple[str, str, str, bool], ...] = (
    ("SOL", "2.54000000", "2.54800000", False),
    ("ETH", "1196.00000000", "1194.60000000", True),
    ("BTC", "7388.50000000", "7131.90000000", True),
)


def tolerance_probe() -> list[tuple[str, Deviation, bool, bool]]:
    """Контрольный опыт САМОГО допуска на трёх примерах §3 ТЗ.

    Возвращает по одной записи на пример: (имя, отклонение, ожидание, факт).

    Правило проверяется опытом над ТЕМ ЖЕ предикатом, которым считается
    результат (:attr:`Deviation.is_mismatch`), а не над его копией: опыт над
    копией проверял бы копию.

    ЗАЧЕМ ЭТО НУЖНО ОТДЕЛЬНО. Опыт со сдвигом окна (§7) показывает, что
    сравнение видит РАЗНЫЕ ДАННЫЕ. Он ничего не говорит о том, где проходит
    граница допуска: он прошёл бы и при допуске, задранном на порядок. Этот
    опыт закрывает ровно эту дыру — и закрывает её на фактических числах, а не
    на подобранных под правило.

    Числа разбираются из СТРОК прямо в ``Decimal``: на float отклонение 0,008
    против допуска 0,01 даёт ложное срабатывание двоичным округлением.
    """
    outcome: list[tuple[str, Deviation, bool, bool]] = []
    for name, ours, theirs, expected in TOLERANCE_EXAMPLES:
        deviation = Deviation(
            inst_id=f"КОНТРОЛЬНЫЙ-ОПЫТ-{name}", bar="1Dutc",
            open_time=datetime(2026, 1, 1, tzinfo=UTC), field="close",
            assembled=Decimal(ours), stored=Decimal(theirs),
        )
        outcome.append((name, deviation, expected, deviation.is_mismatch))
    return outcome


def print_tally(tally: ParityTally) -> None:
    volume_max = max(tally.volume_rel) if tally.volume_rel else 0.0
    volume_med = statistics.median(tally.volume_rel) if tally.volume_rel else 0.0
    say(
        f"  {tally.inst_id:<10} {tally.bar:<7} "
        f"сверено {tally.compared:>6}  "
        f"неполных {tally.skipped_incomplete:>5}  "
        f"без пары {tally.missing_counterpart:>5}  "
        f"расхождений O/H/L/C {tally.mismatches:>4}  "
        f"поглощено {len(tally.absorbed):>5}  "
        f"объём: макс {volume_max:.3e}, медиана {volume_med:.3e}"
    )


async def run(args: argparse.Namespace) -> int:
    """Прогон целиком. Копия отчёта кладётся в ``reports/`` при ЛЮБОМ исходе.

    Отчёт о прогоне, остановленном отказом конфигурации или нарушенной
    посылкой, нужен ровно так же, как об удавшемся: без него о неудачном
    прогоне не остаётся вообще ничего, кроме исчезнувшего журнала контейнера.
    """
    setup_logging()
    started = datetime.now(UTC)
    try:
        return await _run(args, started)
    finally:
        saved = save_report(started)
        if saved is not None:
            # Печатается ПОСЛЕ записи и мимо say(): путь к копии внутри самой
            # копии был бы обещанием, данным до его выполнения.
            print(f"\n  Копия отчёта: {saved}", flush=True)


async def _run(args: argparse.Namespace, started: datetime) -> int:
    say("=" * 100)
    say(" ЗАМЕР 0.2, шаги 4–5: контроль совпадения и контроль незакрытых баров")
    say("=" * 100)
    say(f" Время запуска: {started.isoformat()} (UTC)")
    say(" Скрипт ТОЛЬКО читает: ни одного запроса на запись, сеть не нужна.")

    try:
        settings = read_settings(Path(args.config))
    except ConfigError as exc:
        say(f"\n ОТКАЗ КОНФИГУРАЦИИ: {exc}")
        return 5

    instruments: list[str] = settings["instruments"]
    bars: list[str] = settings["bars"]
    say(f" Инструменты:   {instruments}")
    say(f" Масштабы:      {bars}")

    say(f" Подробный вывод: {'ДА' if args.verbose else 'нет'} "
        "(--verbose печатает и поглощённые допуском отклонения)")

    await db.connect()
    try:
        return await _measure(instruments, bars, verbose=args.verbose)
    except HtfError as exc:
        # Нарушенная посылка — это отказ с названной причиной, а не трассировка
        # стека: причину читает человек, а не отладчик.
        say(f"\n  🔴 ПОСЫЛКА КОНТРОЛЯ НАРУШЕНА: {exc}")
        say(" ЗАМЕР 0, ШАГИ 4–5 ОСТАНОВЛЕНЫ, код возврата 2.")
        return 2
    finally:
        await db.close()


async def _measure(
    instruments: list[str], bars: list[str], *, verbose: bool = False
) -> int:
    # --- границы недели и месяца: измеряются, а не предполагаются -------------
    say("\n=== §3. Фактические границы, снятые с сохранённых меток биржи ===")
    week_weekday: int | None = None
    month_day: int | None = None
    if "1Wutc" in bars:
        measured = await measure_stored_boundaries(instruments[0], "1Wutc")
        if measured["bars_measured"]:
            week_weekday = week_anchor_weekday(measured)
            say(f"  1Wutc: неделя начинается в {WEEKDAY_NAMES[week_weekday]} "
                  f"(по {measured['bars_measured']} барам {instruments[0]}), "
                  f"время суток {measured['time_of_day_set']}")
        else:
            say("  1Wutc: меток в базе НЕТ — границу измерить нечем")
    if "1Mutc" in bars:
        measured = await measure_stored_boundaries(instruments[0], "1Mutc")
        if measured["bars_measured"]:
            month_day = month_anchor_day(measured)
            say(f"  1Mutc: месяц начинается {month_day}-го числа "
                  f"(по {measured['bars_measured']} барам {instruments[0]}), "
                  f"время суток {measured['time_of_day_set']}")
        else:
            say("  1Mutc: меток в базе НЕТ — границу измерить нечем")

    # --- шаг 4: сравнение ----------------------------------------------------
    # Масштаб, у которого границу измерить нечем, СРАВНИВАТЬ НЕЛЬЗЯ, и это не
    # повод для догадки: без измеренного якоря сборка недели или месяца была бы
    # сборкой по представлению автора. Такой масштаб пропускается ЯВНО и
    # называется в отчёте — «пропущено» и «сошлось» обязаны различаться.
    unmeasured = [
        bar for bar in bars
        if (bar == "1Wutc" and week_weekday is None)
        or (bar == "1Mutc" and month_day is None)
    ]
    for bar in unmeasured:
        say(f"\n  ⚪ {bar}: НЕ СВЕРЯЛСЯ — границу периода измерить нечем "
              "(в базе нет ни одного бара этого масштаба). Это НЕ «сошлось».")

    say("\n=== Шаг 4. Сравнение собранного с загруженным (§7 ТЗ) ===")
    tallies: list[ParityTally] = []
    for bar in bars:
        if bar in unmeasured:
            continue
        say(f"  ── {bar}, собрано из {ASSEMBLED_FROM[bar]} "
              "──────────────────────────────")
        for inst_id in instruments:
            tally = await compare_series(
                inst_id, bar, week_weekday=week_weekday, month_day=month_day
            )
            tallies.append(tally)
            print_tally(tally)

    compared = sum(item.compared for item in tallies)
    skipped = sum(item.skipped_incomplete for item in tallies)
    findings = [finding for item in tallies for finding in item.findings]
    absorbed = [value for item in tallies for value in item.absorbed]
    absorbed_precision = sum(item.absorbed_precision for item in tallies)
    year_turn = [link for item in tallies for link in item.year_turn]
    mismatches = len(findings)
    volume_rel = [value for item in tallies for value in item.volume_rel]
    volume_abs = [value for item in tallies for value in item.volume_abs]

    # --- шаг 4: контрольный опыт (обязан упасть) -----------------------------
    say("\n=== Шаг 4. КОНТРОЛЬНЫЙ ОПЫТ: окно сборки сдвинуто на час вперёд ===")
    say("  Если при сдвинутом окне расхождений НОЛЬ — проверка ничего не "
          "проверяет, и этап НЕ СДАН.")
    control_mismatches = 0
    control_compared = 0
    for inst_id in instruments:
        shifted = await compare_series(
            inst_id, "1Dutc", week_weekday=week_weekday, month_day=month_day,
            shift=CONTROL_SHIFT,
        )
        control_mismatches += shifted.mismatches
        control_compared += shifted.compared
        say(f"  {inst_id:<10} сверено {shifted.compared:>6}, "
              f"расхождений {shifted.mismatches:>6}")
    say(f"  ИТОГ контрольного опыта: расхождений {control_mismatches} "
          f"на {control_compared} сравнениях")

    # --- шаг 4: контрольный опыт САМОГО ДОПУСКА (обязан упасть) -------------
    say("\n=== Шаг 4. КОНТРОЛЬНЫЙ ОПЫТ: правило допуска на трёх примерах §3 ТЗ ===")
    say("  Опыт над самим правилом, а не над данными: три фактические пары из "
        "ТЗ прогоняются через ТОТ ЖЕ предикат, которым посчитан результат. "
        f"Без этого «расхождений {mismatches}» означало бы лишь, что допуск "
        "достаточно велик.")
    examples = tolerance_probe()
    for name, deviation, expected, actual in examples:
        verdict = "расхождение" if actual else "поглощается"
        wanted = "расхождение" if expected else "поглощается"
        mark = "✅" if actual == expected else "🔴 НЕ ВОСПРОИЗВЕЛОСЬ"
        say(f"  {name:<4} наша={deviation.text_assembled} "
            f"биржа={deviation.text_stored} | "
            f"знаков: наша={deviation.decimals_assembled} "
            f"биржа={deviation.decimals_stored} | "
            f"допуск={deviation.tolerance} | отклонение={deviation.absolute} | "
            f"вердикт={verdict} (ожидалось: {wanted}) {mark}")
    probe_ok = all(actual == expected for _, _, expected, actual in examples)

    # --- шаг 5: незакрытые бары в хранилище ---------------------------------
    settle = settle_seconds()
    now = datetime.now(UTC)
    say("\n=== Шаг 5. Незакрытые бары в хранилище (§8 ТЗ) ===")
    say(f"  запас settle_seconds() = {settle} с — взят из "
          "src/barrier/runner.py, а не переписан формулой")
    say(f"  граница: конец периода обязан быть не позже "
          f"{(now - timedelta(seconds=settle)).isoformat()}")
    stored_rows = 0
    violations: list[StoredBar] = []
    batch: list[StoredBar] = []
    async for row in stream_stored_htf(tuple(bars)):
        stored_rows += 1
        batch.append(row)
        if len(batch) >= 1000:
            violations.extend(unclosed_rows(batch, now=now, settle=settle))
            batch.clear()
    violations.extend(unclosed_rows(batch, now=now, settle=settle))
    say(f"  проверено сохранённых старших баров: {stored_rows}")
    say(f"  z0_unclosed_rows = {len(violations)}")
    for row in violations[:10]:
        say(f"      {row.inst_id} {row.bar} {row.open_time.isoformat()} — "
              f"период кончается {row.close_time.isoformat()}")

    # --- шаг 5: контрольный опыт (обязан упасть) -----------------------------
    say("\n=== Шаг 5. КОНТРОЛЬНЫЙ ОПЫТ: незакрытый бар подан в проверку ===")
    probe_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
    probe = StoredBar(
        inst_id="КОНТРОЛЬНЫЙ-ОПЫТ", bar="1Dutc", open_time=probe_open,
        close_time=next_period_start("1Dutc", probe_open),
    )
    control_unclosed = len(unclosed_rows([probe], now=now, settle=settle))
    say(f"  подан бар {probe.open_time.isoformat()} с концом периода "
          f"{probe.close_time.isoformat()} (ещё не наступил)")
    say(f"  счётчик стал: {control_unclosed} "
          f"{'— проверка ВИДИТ незакрытый бар' if control_unclosed else '— ПРОВЕРКА СЛЕПА'}")
    say("  Строка в базу НЕ ПИСАЛАСЬ: скрипт только читает. Что незакрытый "
          "бар не проходит в саму таблицу, доказано тестом test_zamer_0.py, "
          "где принудительная вставка такой строки роняет проверку.")

    # --- отчёт: блоки A–E §5 ТЗ ---------------------------------------------
    volume_max = max(volume_rel) if volume_rel else 0.0
    volume_med = statistics.median(volume_rel) if volume_rel else 0.0
    volume_abs_max = max(volume_abs) if volume_abs else 0.0
    peak = peak_rss_mb()

    say("\n" + "=" * 100)
    say(" ОТЧЁТ ЗАМЕРА 0.2: СВЕРКА НАШЕЙ СБОРКИ С БИРЖЕВЫМ РЯДОМ")
    say("=" * 100)
    say(" Правило допуска: расхождение допустимо, если его величина НЕ ПРЕВЫШАЕТ")
    say(" единицу последнего значащего знака той стороны, которая записана ГРУБЕЕ.")
    say(" Знаки считаются по ТЕКСТОВОЙ записи (для NUMERIC — value::text), "
        "незначащие")
    say(" нули справа от точки отброшены, слева — нет. Арифметика — только Decimal.")

    affected = affected_bars(findings)
    say("\n--- Блок A. Сводка --------------------------------------------------")
    say(f"  Всего баров сверено: {compared}")
    say(f"  Расхождений найдено: {mismatches} полей в {len(affected)} барах")
    say(f"  Поглощено допуском: {len(absorbed)}  "
        f"(из них: точность записи — {absorbed_precision})")
    say(f"  Периодов пропущено как неполные: {skipped} — это НЕ расхождения и "
        "не поглощённые")
    say(f"  Затронутые бары: {'; '.join(affected) if affected else 'нет'}")

    say("\n--- Блок B. Поимённый список расхождений ----------------------------")
    if not findings:
        say("  расхождений нет — ни одного поля, где отклонение превысило допуск")
    for finding in findings:
        say(f"  {finding.line}")

    say("\n--- Блок C. Категории -----------------------------------------------")
    say(f"  {CATEGORY_BOUNDARY} — бар попадает на рубеж 2019→2020 у BTC или ETH;")
    say(f"  {CATEGORY_REAL} — всё остальное.")
    say("  Категория — МЕТКА, а не фильтр: из списка блока B не спрятано ничего.")
    for category in (CATEGORY_BOUNDARY, CATEGORY_REAL):
        count = sum(1 for finding in findings if finding.category == category)
        say(f"  {category:<18} = {count}")

    say("\n--- Блок D. Аномалия рубежа 2019→2020 -------------------------------")
    say("  Только факты. Механизм НЕ УСТАНОВЛЕН и здесь не постулируется.")
    boundary_links = [
        link for link in year_turn if link.bar in ("1Wutc", "1Mutc")
    ]
    if not boundary_links:
        say("  ни одного бара BTC или ETH на рубеже 2019→2020 сверено не было — "
            "цепочку строить не из чего. Это НЕ «сошлось».")
    for link in boundary_links:
        previous = (
            f"{link.previous_open_time:%Y-%m-%d} закрытие={link.previous_close_stored}"
            if link.previous_open_time is not None
            else "предыдущего бара в ряде нет"
        )
        say(f"  {link.inst_id} {link.bar} {link.open_time:%Y-%m-%d} "
            f"{'(расхождение)' if link.has_mismatch else '(сошёлся)'}")
        say(f"      предыдущий бар биржи: {previous}")
        say(f"      открытие спорного бара по бирже:  {link.open_stored}")
        say(f"      открытие по нашей сборке:         {link.open_assembled}")

    say("\n--- Блок E. Поглощённые допуском ------------------------------------")
    say(f"  {len(absorbed)} отклонений уложились в допуск и построчно не "
        "печатаются (§5, блок E ТЗ).")
    if verbose:
        say("  --verbose: печатаются все, для ручной проверки правила.")
        for deviation in absorbed:
            say(f"      {deviation.inst_id} {deviation.bar} "
                f"{deviation.open_time:%Y-%m-%d} {deviation.field}: "
                f"наша={deviation.text_assembled} биржа={deviation.text_stored} | "
                f"знаков: наша={deviation.decimals_assembled} "
                f"биржа={deviation.decimals_stored} | "
                f"допуск={deviation.tolerance} | отклонение={deviation.absolute}")
    else:
        say("  Запустите с --verbose, чтобы увидеть их поимённо.")

    say("\n" + "=" * 100)
    say(" МАШИНОЧИТАЕМЫЕ КЛЮЧИ ШАГОВ 4–5")
    say("=" * 100)
    say(f"  z0_parity_days_compared            = {compared}")
    say(f"  z0_parity_days_skipped_incomplete  = {skipped}")
    say(f"  z0_parity_ohlc_mismatches          = {mismatches} "
        "(по правилу Замера 0.2, ПО ПОЛЯМ: единица последнего значащего знака "
        "грубой стороны)")
    say(f"  z0_parity_ohlc_absorbed            = {len(absorbed)} "
        "(отклонение ненулевое, но в допуск уложилось)")
    say(f"  z0_parity_ohlc_absorbed_precision  = {absorbed_precision} "
        "(из них: стороны записаны с разной точностью)")
    say(f"  z0_parity_boundary_anomaly         = "
        f"{sum(1 for f in findings if f.category == CATEGORY_BOUNDARY)}")
    say(f"  z0_parity_real                     = "
        f"{sum(1 for f in findings if f.category == CATEGORY_REAL)}")
    say(f"  z0_parity_volume_max_dev           = {volume_max:.6e} "
        f"(относительное; в абсолютных единицах {volume_abs_max:.6e})")
    say(f"  медианное отклонение объёма        = {volume_med:.6e} "
        "(относительное)")
    say(f"  z0_unclosed_rows                   = {len(violations)}")
    say(f"  контрольный опыт §7 (сдвиг окна)   = {control_mismatches} расхождений")
    say(f"  контрольный опыт §8 (незакрытый)   = {control_unclosed}")
    say(f"  контрольный опыт допуска (§3 ТЗ)   = "
        f"{'все три примера воспроизвелись' if probe_ok else 'НЕ ВОСПРОИЗВЁЛСЯ'}")
    say(f"  peak_rss_mb                        = {peak:,.1f} "
        f"(потолок {MEMORY_CAP_MB:,.0f} МБ)")
    say(f"  оценка памяти при выборке ВТРОЕ    ≈ {_triple_estimate(peak, compared):,.0f} МБ")
    say("  Порог по объёму НЕ НАЗНАЧАЕТСЯ здесь: он выводится из напечатанных "
        "выше величин в отчёте этапа (§7 ТЗ).")

    keys: dict[str, Any] = {
        "z0_parity_days_compared": compared,
        "z0_parity_days_skipped_incomplete": skipped,
        "z0_parity_ohlc_mismatches": mismatches,
        "z0_parity_ohlc_absorbed": len(absorbed),
        "z0_parity_ohlc_absorbed_precision": absorbed_precision,
        "z0_parity_boundary_anomaly": sum(
            1 for finding in findings if finding.category == CATEGORY_BOUNDARY
        ),
        "z0_parity_real": sum(
            1 for finding in findings if finding.category == CATEGORY_REAL
        ),
        "z0_parity_volume_max_dev": volume_max,
        "z0_unclosed_rows": len(violations),
        "peak_rss_mb": round(peak, 1),
    }
    _log.info("Замер 0: контроль завершён", **keys)

    # Ключи пишутся ЕЩЁ И В ФАЙЛ. Журнал контейнера, запущенного с --rm,
    # исчезает вместе с контейнером, и пункт 5 verify_z0.sh не находил в нём
    # ничего — при том что ключи там были (прогон 11.09.2026).
    written = write_metrics("z0_parity", keys)
    if written is None:
        say(f"\n  🟡 ключи НЕ записаны в {metrics_path()}: каталог "
              "недоступен на запись. Прогон это не отменяет, но verify_z0.sh "
              "придётся читать журнал контейнера.")
    else:
        say(f"\n  Машиночитаемые ключи дописаны в {written}")

    # СТАРШИНСТВО КОДОВ, а не первый попавшийся отказ. Пустая выборка старше
    # всего: при ней остальные числа не значат ничего. Следом — слепой
    # контрольный опыт: без него ноль расхождений ничего не доказывает. И
    # только потом сами находки. Печатаются при этом ВСЕ, а не одна: код
    # возврата — сигнал, а не отчёт.
    code = 0
    reasons: list[str] = []
    if compared == 0:
        reasons.append(
            "выборка пуста: не сверено ни одного бара. Остальные числа смысла не имеют"
        )
        code = 5
    if control_mismatches == 0 or control_unclosed == 0:
        reasons.append(
            "КОНТРОЛЬНЫЙ ОПЫТ НЕ УПАЛ: проверка слепа, и её ноль ничего не значит"
        )
        code = code or 4
    if not probe_ok:
        reasons.append(
            "КОНТРОЛЬНЫЙ ОПЫТ ДОПУСКА НЕ УПАЛ: правило не воспроизвело вердикты "
            "трёх фактических примеров §3 ТЗ, и число расхождений ничего не значит"
        )
        code = code or 4
    if violations:
        reasons.append(f"в хранилище {len(violations)} незакрытых баров")
        code = code or 3
    if mismatches:
        reasons.append(
            f"расхождений O/H/L/C: {mismatches} (поглощено допуском "
            f"{len(absorbed)}). ЭТО РЕЗУЛЬТАТ ЗАМЕРА, А НЕ ОТКАЗ: цель — точный "
            "поимённый список, а не его отсутствие. Список — блок B выше; "
            "допуск под «ровно ноль» не подкручивается"
        )
        code = code or 2
    if peak > MEMORY_CAP_MB:
        reasons.append(f"пиковая память {peak:,.0f} МБ превысила потолок "
                       f"{MEMORY_CAP_MB:,.0f} МБ")
        code = code or 3

    say("")
    for reason in reasons:
        say(f"  🔴 {reason}")
    say(f" ЗАМЕР 0, ШАГИ 4–5 ЗАВЕРШЕНЫ, код возврата {code}. "
          f"Время: {datetime.now(UTC).isoformat()}")
    return code


def _triple_estimate(peak: float, compared: int) -> float:
    """Оценка памяти при росте выборки ВТРОЕ (§10 ТЗ).

    Растёт линейно ТОЛЬКО та часть, что накапливается, — отклонения по старшим
    барам, по два числа с плавающей точкой на бар. Часовой ряд идёт потоком, и
    от его длины пиковая память не зависит вовсе; именно поэтому оценка — не
    «пик × 3», что было бы честной, но заведомо завышенной страховкой.
    """
    accumulated_mb = compared * 2 * 8 / (1024 * 1024)
    return peak + 2 * accumulated_mb


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Замер 0, шаги 4–5: контроль совпадения и незакрытых баров. "
                    "Только чтение."
    )
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--verbose", action="store_true",
        help="печатать и ПОГЛОЩЁННЫЕ допуском отклонения — для ручной проверки "
             "правила. По умолчанию они видны только числом в сводке (§5 ТЗ).",
    )
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
