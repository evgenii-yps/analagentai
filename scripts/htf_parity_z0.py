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

ПОРОГИ:
  * O/H/L/C — допуск ОТНОСИТЕЛЬНЫЙ (Замер 0.1): поле считается разошедшимся,
    если ``|собрано - биржа| / max(|биржа|, EPS_FLOOR) > TOL_REL`` И при этом
    ``|собрано - биржа| > EPS_FLOOR``. Оба числа живут в одном месте —
    ``backtest/htf.py``, — вместе с измерением, из которого выведены.
    БЛОКИРУЮЩЕЕ. Рядом печатается ``z0_parity_ohlc_mismatches_abs`` по
    ПРЕЖНЕМУ абсолютному правилу 1e-8: молчаливая замена метрики лишила бы
    следующего читателя возможности сопоставить прогоны;
  * объём — на первом прогоне НЕ блокирующий. Печатаются фактические
    максимальное и медианное отклонения; порог назначается по ИЗМЕРЕННОЙ
    величине в отчёте, а не угадывается здесь. Сумма многих часовых значений
    может разойтись с биржевым в последних знаках из-за округления на стороне
    биржи.

ДВА КОНТРОЛЬНЫХ ОПЫТА, И ОБА ОБЯЗАНЫ УПАСТЬ:

  §7 — окно сборки сдвигается на ОДИН ЧАС вперёд, и сравнение обязано дать
       НЕНУЛЕВОЕ число расхождений. Ноль при сдвинутом окне означает, что
       проверка ничего не проверяет, и этап не сдан;
  §8 — в проверку хранилища подаётся бар, период которого ещё не закрылся, и
       счётчик обязан стать ненулевым. Ноль здесь означает, что проверка слепая.

ТРЕТИЙ КОНТРОЛЬНЫЙ ОПЫТ — НАД САМИМ ДОПУСКОМ (Замер 0.1). Подаются два
отклонения: ровно ``TOL_REL × 1,01`` и ``TOL_REL × 0,99``. Первое обязано быть
засчитано, второе — нет. Опыт со сдвигом окна показывает лишь, что сравнение
видит разные данные, и прошёл бы при пороге, задранном на порядок.

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

ЗАПУСК (внутри собранного образа):

    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run --rm -e PYTHONUNBUFFERED=1 backtest python scripts/htf_parity_z0.py
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
    EPS_FLOOR,
    OHLC_FIELDS,
    OHLC_TOLERANCE,
    TOL_REL,
    AssembledBar,
    Deviation,
    HtfError,
    SourceBar,
    StoredBar,
    make_aggregator,
    measure_boundaries,
    month_anchor_day,
    next_period_start,
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


@dataclass
class ParityTally:
    """Итог сравнения одного масштаба одного инструмента."""

    inst_id: str
    bar: str
    compared: int = 0
    skipped_incomplete: int = 0
    missing_counterpart: int = 0
    # Расхождения по правилу Замера 0.1: относительный TOL_REL И абсолютный
    # EPS_FLOOR, оба обязаны быть превышены. Это то число, по которому этап
    # принимает решение.
    ohlc_mismatches: int = 0
    # Расхождения по ПРЕЖНЕМУ абсолютному правилу 1e-8. Ничего не решает и
    # существует ради одного: сопоставимости с прогоном 11.09.2026. Молчаливая
    # замена метрики недопустима — сравнить два прогона было бы нечем.
    ohlc_mismatches_abs: int = 0
    examples: list[Deviation] = field(default_factory=list)
    # Относительные отклонения ПО ПОЛЯМ, разошедшимся по старому правилу.
    # Накапливаются затем, чтобы распределение осталось видимым и порог можно
    # было пересмотреть позже ПО ДАННЫМ, а не заново. Их сотни, а не миллионы:
    # поле, уложившееся в 1e-8, сюда не попадает вовсе.
    rel_devs: list[float] = field(default_factory=list)
    volume_rel: list[float] = field(default_factory=list)
    volume_abs: list[float] = field(default_factory=list)


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
            try:
                self._current = await anext(self._stream)
            except StopAsyncIteration:
                self._exhausted = True
                self._current = None
        if self._current is not None and self._current.ts == moment:
            return self._current
        return None


async def compare_series(
    inst_id: str,
    bar: str,
    *,
    week_weekday: int | None,
    month_day: int | None,
    shift: timedelta = timedelta(0),
    keep_examples: int = 3,
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
        await _walk(tally, inst_id, built_stream, stored, keep_examples)
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
    keep_examples: int,
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
        _compare(inst_id, built, counterpart, tally, keep_examples)


def _compare(
    inst_id: str,
    built: AssembledBar,
    stored: SourceBar,
    tally: ParityTally,
    keep_examples: int,
) -> None:
    """Сверяет один бар по полям и наполняет счётчики ``tally``.

    СЧИТАЮТСЯ ДВА ЧИСЛА, И ЭТО НЕ ДУБЛИРОВАНИЕ. ``ohlc_mismatches`` — по
    правилу Замера 0.1 (относительный допуск), им этап и решает.
    ``ohlc_mismatches_abs`` — по прежнему абсолютному 1e-8, ради сопоставимости
    с прогоном 11.09.2026. Заменить второе первым молча значило бы лишить
    следующего читателя возможности увидеть, что именно изменилось.

    Объём считается ВСЕГДА и в решение о расхождении не входит: на первом
    прогоне он не блокирующий (§7 ТЗ), и смешать его с O/H/L/C значило бы
    заблокировать этап округлением на стороне биржи.
    """
    mismatched = False
    mismatched_abs = False
    for name in OHLC_FIELDS:
        deviation = Deviation(
            inst_id=inst_id, bar=built.bar, open_time=built.open_time, field=name,
            assembled=getattr(built, name), stored=getattr(stored, name),
        )
        if deviation.is_mismatch_abs:
            mismatched_abs = True
            tally.rel_devs.append(float(deviation.relative_floored))
        if deviation.is_mismatch:
            mismatched = True
            if len(tally.examples) < keep_examples:
                tally.examples.append(deviation)
    if mismatched:
        tally.ohlc_mismatches += 1
    if mismatched_abs:
        tally.ohlc_mismatches_abs += 1
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


def tolerance_probe() -> tuple[bool, bool]:
    """Контрольный опыт САМОГО допуска: (засчитано выше порога, засчитано ниже).

    Порог проверяется опытом над ТЕМ ЖЕ правилом, которым считается результат
    (:attr:`Deviation.is_mismatch`), а не над его копией: опыт над копией
    проверял бы копию. Подаются две пары значений, различающиеся ровно на
    ``TOL_REL × 1,01`` и ``TOL_REL × 0,99`` от биржевого. Первая обязана быть
    засчитана как расхождение, вторая — нет.

    ЗАЧЕМ ЭТО НУЖНО ОТДЕЛЬНО. Опыт со сдвигом окна (§7) показывает, что
    сравнение видит РАЗНЫЕ ДАННЫЕ. Он ничего не говорит о том, где именно
    проходит граница допуска: он прошёл бы и при TOL_REL, задранном на порядок.
    Этот опыт закрывает ровно эту дыру.

    Цена деления берётся такой, чтобы относительное отклонение считалось точно:
    ``Decimal`` и никаких ``float`` на пути (урок DOGE).
    """
    base = Decimal("100")

    def counted(factor: Decimal) -> bool:
        deviation = Deviation(
            inst_id="КОНТРОЛЬНЫЙ-ОПЫТ", bar="1Dutc",
            open_time=datetime(2026, 1, 1, tzinfo=UTC), field="close",
            assembled=base * (Decimal(1) + TOL_REL * factor), stored=base,
        )
        return deviation.is_mismatch

    return counted(Decimal("1.01")), counted(Decimal("0.99"))


def print_tally(tally: ParityTally) -> None:
    volume_max = max(tally.volume_rel) if tally.volume_rel else 0.0
    volume_med = statistics.median(tally.volume_rel) if tally.volume_rel else 0.0
    print(
        f"  {tally.inst_id:<10} {tally.bar:<7} "
        f"сверено {tally.compared:>6}  "
        f"неполных {tally.skipped_incomplete:>5}  "
        f"без пары {tally.missing_counterpart:>5}  "
        f"расхождений O/H/L/C {tally.ohlc_mismatches:>5}  "
        f"(по старому 1e-8: {tally.ohlc_mismatches_abs:>5})  "
        f"объём: макс {volume_max:.3e}, медиана {volume_med:.3e}",
        flush=True,
    )
    for example in tally.examples:
        print(f"      {example.open_time.isoformat()} {example.field}: "
              f"собрано {example.assembled}, биржа {example.stored}, "
              f"|Δ| {example.absolute}, "
              f"относительное {float(example.relative_floored):.3e}", flush=True)


async def run(args: argparse.Namespace) -> int:
    setup_logging()
    started = datetime.now(UTC)
    print("=" * 100, flush=True)
    print(" ЗАМЕР 0, шаги 4–5: контроль совпадения и контроль незакрытых баров",
          flush=True)
    print("=" * 100, flush=True)
    print(f" Время запуска: {started.isoformat()} (UTC)", flush=True)
    print(" Скрипт ТОЛЬКО читает: ни одного запроса на запись, сеть не нужна.",
          flush=True)

    try:
        settings = read_settings(Path(args.config))
    except ConfigError as exc:
        print(f"\n ОТКАЗ КОНФИГУРАЦИИ: {exc}", flush=True)
        return 5

    instruments: list[str] = settings["instruments"]
    bars: list[str] = settings["bars"]
    print(f" Инструменты:   {instruments}", flush=True)
    print(f" Масштабы:      {bars}", flush=True)

    await db.connect()
    try:
        return await _measure(instruments, bars)
    except HtfError as exc:
        # Нарушенная посылка — это отказ с названной причиной, а не трассировка
        # стека: причину читает человек, а не отладчик.
        print(f"\n  🔴 ПОСЫЛКА КОНТРОЛЯ НАРУШЕНА: {exc}", flush=True)
        print(" ЗАМЕР 0, ШАГИ 4–5 ОСТАНОВЛЕНЫ, код возврата 2.", flush=True)
        return 2
    finally:
        await db.close()


async def _measure(instruments: list[str], bars: list[str]) -> int:
    # --- границы недели и месяца: измеряются, а не предполагаются -------------
    print("\n=== §3. Фактические границы, снятые с сохранённых меток биржи ===",
          flush=True)
    week_weekday: int | None = None
    month_day: int | None = None
    if "1Wutc" in bars:
        measured = await measure_stored_boundaries(instruments[0], "1Wutc")
        if measured["bars_measured"]:
            week_weekday = week_anchor_weekday(measured)
            print(f"  1Wutc: неделя начинается в {WEEKDAY_NAMES[week_weekday]} "
                  f"(по {measured['bars_measured']} барам {instruments[0]}), "
                  f"время суток {measured['time_of_day_set']}", flush=True)
        else:
            print("  1Wutc: меток в базе НЕТ — границу измерить нечем", flush=True)
    if "1Mutc" in bars:
        measured = await measure_stored_boundaries(instruments[0], "1Mutc")
        if measured["bars_measured"]:
            month_day = month_anchor_day(measured)
            print(f"  1Mutc: месяц начинается {month_day}-го числа "
                  f"(по {measured['bars_measured']} барам {instruments[0]}), "
                  f"время суток {measured['time_of_day_set']}", flush=True)
        else:
            print("  1Mutc: меток в базе НЕТ — границу измерить нечем", flush=True)

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
        print(f"\n  ⚪ {bar}: НЕ СВЕРЯЛСЯ — границу периода измерить нечем "
              "(в базе нет ни одного бара этого масштаба). Это НЕ «сошлось».",
              flush=True)

    print("\n=== Шаг 4. Сравнение собранного с загруженным (§7 ТЗ) ===", flush=True)
    tallies: list[ParityTally] = []
    for bar in bars:
        if bar in unmeasured:
            continue
        print(f"  ── {bar}, собрано из {ASSEMBLED_FROM[bar]} "
              "──────────────────────────────", flush=True)
        for inst_id in instruments:
            tally = await compare_series(
                inst_id, bar, week_weekday=week_weekday, month_day=month_day
            )
            tallies.append(tally)
            print_tally(tally)

    compared = sum(item.compared for item in tallies)
    skipped = sum(item.skipped_incomplete for item in tallies)
    mismatches = sum(item.ohlc_mismatches for item in tallies)
    mismatches_abs = sum(item.ohlc_mismatches_abs for item in tallies)
    rel_devs = [value for item in tallies for value in item.rel_devs]
    volume_rel = [value for item in tallies for value in item.volume_rel]
    volume_abs = [value for item in tallies for value in item.volume_abs]

    # --- шаг 4: контрольный опыт (обязан упасть) -----------------------------
    print("\n=== Шаг 4. КОНТРОЛЬНЫЙ ОПЫТ: окно сборки сдвинуто на час вперёд ===",
          flush=True)
    print("  Если при сдвинутом окне расхождений НОЛЬ — проверка ничего не "
          "проверяет, и этап НЕ СДАН.", flush=True)
    control_mismatches = 0
    control_compared = 0
    for inst_id in instruments:
        shifted = await compare_series(
            inst_id, "1Dutc", week_weekday=week_weekday, month_day=month_day,
            shift=CONTROL_SHIFT, keep_examples=1,
        )
        control_mismatches += shifted.ohlc_mismatches
        control_compared += shifted.compared
        print(f"  {inst_id:<10} сверено {shifted.compared:>6}, "
              f"расхождений {shifted.ohlc_mismatches:>6}", flush=True)
    print(f"  ИТОГ контрольного опыта: расхождений {control_mismatches} "
          f"на {control_compared} сравнениях", flush=True)

    # --- шаг 4: контрольный опыт САМОГО ДОПУСКА (обязан упасть) -------------
    print("\n=== Шаг 4. КОНТРОЛЬНЫЙ ОПЫТ: чувствительность относительного "
          "допуска ===", flush=True)
    print("  Опыт над самим правилом, а не над данными: пара значений, "
          f"различающихся ровно на TOL_REL×1,01, обязана быть засчитана, "
          f"на TOL_REL×0,99 — нет. Без этого «расхождений {mismatches}» "
          "означало бы лишь, что порог достаточно велик.", flush=True)
    control_above, control_below = tolerance_probe()
    print(f"  TOL_REL = {TOL_REL} (относительный), "
          f"EPS_FLOOR = {EPS_FLOOR} (абсолютный, вторичное условие)", flush=True)
    print(f"  отклонение TOL_REL×1,01 засчитано: "
          f"{'ДА' if control_above else 'НЕТ — ДОПУСК СЛЕП'}", flush=True)
    print(f"  отклонение TOL_REL×0,99 засчитано: "
          f"{'НЕТ' if not control_below else 'ДА — ДОПУСК НЕ ДЕЙСТВУЕТ'}",
          flush=True)

    # --- шаг 5: незакрытые бары в хранилище ---------------------------------
    settle = settle_seconds()
    now = datetime.now(UTC)
    print("\n=== Шаг 5. Незакрытые бары в хранилище (§8 ТЗ) ===", flush=True)
    print(f"  запас settle_seconds() = {settle} с — взят из "
          "src/barrier/runner.py, а не переписан формулой", flush=True)
    print(f"  граница: конец периода обязан быть не позже "
          f"{(now - timedelta(seconds=settle)).isoformat()}", flush=True)
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
    print(f"  проверено сохранённых старших баров: {stored_rows}", flush=True)
    print(f"  z0_unclosed_rows = {len(violations)}", flush=True)
    for row in violations[:10]:
        print(f"      {row.inst_id} {row.bar} {row.open_time.isoformat()} — "
              f"период кончается {row.close_time.isoformat()}", flush=True)

    # --- шаг 5: контрольный опыт (обязан упасть) -----------------------------
    print("\n=== Шаг 5. КОНТРОЛЬНЫЙ ОПЫТ: незакрытый бар подан в проверку ===",
          flush=True)
    probe_open = now.replace(hour=0, minute=0, second=0, microsecond=0)
    probe = StoredBar(
        inst_id="КОНТРОЛЬНЫЙ-ОПЫТ", bar="1Dutc", open_time=probe_open,
        close_time=next_period_start("1Dutc", probe_open),
    )
    control_unclosed = len(unclosed_rows([probe], now=now, settle=settle))
    print(f"  подан бар {probe.open_time.isoformat()} с концом периода "
          f"{probe.close_time.isoformat()} (ещё не наступил)", flush=True)
    print(f"  счётчик стал: {control_unclosed} "
          f"{'— проверка ВИДИТ незакрытый бар' if control_unclosed else '— ПРОВЕРКА СЛЕПА'}",
          flush=True)
    print("  Строка в базу НЕ ПИСАЛАСЬ: скрипт только читает. Что незакрытый "
          "бар не проходит в саму таблицу, доказано тестом test_zamer_0.py, "
          "где принудительная вставка такой строки роняет проверку.", flush=True)

    # --- итог ----------------------------------------------------------------
    volume_max = max(volume_rel) if volume_rel else 0.0
    volume_med = statistics.median(volume_rel) if volume_rel else 0.0
    volume_abs_max = max(volume_abs) if volume_abs else 0.0
    # Распределение относительного отклонения СРЕДИ РАЗОШЕДШИХСЯ ПО СТАРОМУ
    # ПРАВИЛУ. Именно среди них, а не среди всех сравнений: по всем сравнениям
    # медиана была бы нулём и не говорила бы ни о чём.
    rel_dev_max = max(rel_devs) if rel_devs else 0.0
    rel_dev_p50 = statistics.median(rel_devs) if rel_devs else 0.0
    peak = peak_rss_mb()

    print("\n" + "=" * 100, flush=True)
    print(" ИТОГ ШАГОВ 4–5", flush=True)
    print("=" * 100, flush=True)
    print(f"  z0_parity_days_compared            = {compared}", flush=True)
    print(f"  z0_parity_days_skipped_incomplete  = {skipped}", flush=True)
    print(f"  z0_parity_ohlc_mismatches          = {mismatches} "
          f"(относительное правило: |Δ|/max(|биржа|,{EPS_FLOOR}) > {TOL_REL} "
          f"И |Δ| > {EPS_FLOOR})", flush=True)
    print(f"  z0_parity_ohlc_mismatches_abs      = {mismatches_abs} "
          f"(прежнее абсолютное правило {OHLC_TOLERANCE}; "
          "для сопоставимости с прогоном 11.09.2026, ничего не решает)",
          flush=True)
    print(f"  z0_parity_rel_dev_max              = {rel_dev_max:.6e} "
          "(среди разошедшихся по старому правилу)", flush=True)
    print(f"  z0_parity_rel_dev_p50              = {rel_dev_p50:.6e} "
          "(там же, медиана)", flush=True)
    print(f"  z0_parity_volume_max_dev           = {volume_max:.6e} "
          f"(относительное; в абсолютных единицах {volume_abs_max:.6e})", flush=True)
    print(f"  медианное отклонение объёма        = {volume_med:.6e} "
          "(относительное)", flush=True)
    print(f"  z0_unclosed_rows                   = {len(violations)}", flush=True)
    print(f"  контрольный опыт §7 (сдвиг окна)   = {control_mismatches} расхождений",
          flush=True)
    print(f"  контрольный опыт §8 (незакрытый)   = {control_unclosed}", flush=True)
    print(f"  peak_rss_mb                        = {peak:,.1f} "
          f"(потолок {MEMORY_CAP_MB:,.0f} МБ)", flush=True)
    print(f"  оценка памяти при выборке ВТРОЕ    ≈ {_triple_estimate(peak, compared):,.0f} МБ",
          flush=True)
    print("  Порог по объёму НЕ НАЗНАЧАЕТСЯ здесь: он выводится из напечатанных "
          "выше величин в отчёте этапа (§7 ТЗ).", flush=True)

    keys: dict[str, Any] = {
        "z0_parity_days_compared": compared,
        "z0_parity_days_skipped_incomplete": skipped,
        "z0_parity_ohlc_mismatches": mismatches,
        "z0_parity_ohlc_mismatches_abs": mismatches_abs,
        "z0_parity_rel_dev_max": rel_dev_max,
        "z0_parity_rel_dev_p50": rel_dev_p50,
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
        print(f"\n  🟡 ключи НЕ записаны в {metrics_path()}: каталог "
              "недоступен на запись. Прогон это не отменяет, но verify_z0.sh "
              "придётся читать журнал контейнера.", flush=True)
    else:
        print(f"\n  Машиночитаемые ключи дописаны в {written}", flush=True)

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
    if control_above is False or control_below is True:
        reasons.append(
            "КОНТРОЛЬНЫЙ ОПЫТ ДОПУСКА НЕ УПАЛ: правило сравнения не различает "
            f"отклонения по обе стороны от TOL_REL={TOL_REL}, и число "
            "расхождений ничего не значит"
        )
        code = code or 4
    if violations:
        reasons.append(f"в хранилище {len(violations)} незакрытых баров")
        code = code or 3
    if mismatches:
        reasons.append(
            f"расхождений O/H/L/C: {mismatches} при допуске TOL_REL={TOL_REL} "
            f"(по прежнему абсолютному {OHLC_TOLERANCE} их было бы "
            f"{mismatches_abs}). ПЕРВЫМ ДЕЛОМ проверять часовой пояс (§3), "
            "а не арифметику сборки"
        )
        code = code or 2
    if peak > MEMORY_CAP_MB:
        reasons.append(f"пиковая память {peak:,.0f} МБ превысила потолок "
                       f"{MEMORY_CAP_MB:,.0f} МБ")
        code = code or 3

    print("", flush=True)
    for reason in reasons:
        print(f"  🔴 {reason}", flush=True)
    print(f" ЗАМЕР 0, ШАГИ 4–5 ЗАВЕРШЕНЫ, код возврата {code}. "
          f"Время: {datetime.now(UTC).isoformat()}", flush=True)
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
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
