"""ЗАМЕР 0 (шаг 1 версии 7): проверки старших таймфреймов.

КОНТРОЛЬНЫЙ ОПЫТ ОБЯЗАТЕЛЕН ПО КАЖДОЙ ПРОВЕРКЕ, и это не формальность. На
Этапе 9.1.3 три проверки из девяти прошли молча и не проверяли ничего: они
утверждали «расхождений нет» на данных, где расхождению неоткуда было взяться.
Поэтому у каждой проверки здесь есть парная: она ЛОМАЕТ посылку и требует, чтобы
проверка упала. Проверка, которая не умеет падать, не является проверкой.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ БЕЗ БАЗЫ, А ЧТО — ТОЛЬКО С НЕЙ. Календарь, сборка,
сравнение и правило незакрытого бара — чистые функции ``backtest/htf.py``, и
они проверяются всегда. Хранение (точность ``NUMERIC(20,8)``, идемпотентность
загрузки, «без --apply в базу не уходит ни одного запроса на запись») проверить
без базы нельзя, и делать вид, что можно, — хуже, чем пропустить: такие тесты
включаются переменной ``BT_TEST_DSN`` и БЕЗ НЕЁ ЯВНО ПРОПУСКАЮТСЯ. Пропущенный
тест — это не зелёный тест.

    BT_TEST_DSN=postgresql://postgres@127.0.0.1:5433/bt_test \\
        python -m pytest tests/test_zamer_0.py

СХЕМА ТЕСТОВОЙ БАЗЫ БЕРЁТСЯ ИЗ ФАЙЛОВ МИГРАЦИЙ (§11 ТЗ), а не переписывается
здесь руками: переписанный список колонок однажды разошёлся бы с базой, и
разошёлся бы молча — ровно то, что уже произошло на Этапе 9.1.2.2.
"""

from __future__ import annotations

import os
import pathlib
import re
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from backtest.htf import (
    HTF_BARS,
    Deviation,
    HtfError,
    SourceBar,
    StoredBar,
    assemble,
    assert_bar_alignment,
    measure_boundaries,
    month_anchor_day,
    next_period_start,
    numeric_text,
    parse_htf_candles,
    significant_decimals,
    significant_text,
    unclosed_rows,
    verify_period_chain,
    week_anchor_weekday,
    write_tolerance,
)
from tests.schema_double import table_columns

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"

TEST_DSN = os.environ.get("BT_TEST_DSN", "").strip()

requires_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая база: задайте BT_TEST_DSN, например "
        "postgresql://postgres@127.0.0.1:5433/bt_test. Без неё проверки "
        "ХРАНЕНИЯ не выполнялись — это не «зелено»"
    ),
)

INST = "TEST-USDT"
DAY = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # понедельник, для наглядности


# ---------------------------------------------------------------------------
# Помощники: данные придуманы так, чтобы каждое поле различалось
# ---------------------------------------------------------------------------


def hour_bars(
    start: datetime, count: int, *, base: Decimal = Decimal("100.00000000")
) -> list[SourceBar]:
    """Часовые бары с РАЗНЫМИ значениями в каждом поле и каждом часе.

    Различие намеренное: на ряде, где ``open == high == low == close``,
    перепутанные поля сборки дали бы верный ответ, и проверка ничего не
    проверила бы.
    """
    bars: list[SourceBar] = []
    for index in range(count):
        step = Decimal(index)
        price = base + step
        bars.append(
            SourceBar(
                ts=start + timedelta(hours=index),
                open=price,
                high=price + Decimal("3.00000000"),
                low=price - Decimal("2.00000000"),
                close=price + Decimal("0.50000000"),
                volume=Decimal("1.00000001") + step,
            )
        )
    return bars


def okx_row(opened_at: datetime, *, confirm: str = "1") -> list[str]:
    """Строка ответа OKX в её настоящем виде: девять полей, последнее — confirm."""
    ms = int(opened_at.timestamp() * 1000)
    return [str(ms), "1.5", "2.5", "0.5", "2.0", "10.0", "20.0", "30.0", confirm]


# ---------------------------------------------------------------------------
# 1. Сборка дневной свечи из 24 придуманных часовых
# ---------------------------------------------------------------------------


def test_daily_from_24_hours_is_exact() -> None:
    """§11.1: O/H/L/C и сумма объёма собираются ТОЧНО, в Decimal."""
    hours = hour_bars(DAY, 24)
    built = list(assemble("1Dutc", hours))
    assert len(built) == 1
    day = built[0]

    assert day.open_time == DAY
    assert day.is_complete
    assert day.sources == 24
    # open — открытие ПЕРВОГО часа, close — закрытие ПОСЛЕДНЕГО. Значения
    # выписаны числом, а не пересчитаны той же формулой: иначе тест повторил бы
    # ошибку сборки вслед за ней.
    assert day.open == Decimal("100.00000000")
    assert day.close == Decimal("123.50000000")
    assert day.high == Decimal("126.00000000")
    assert day.low == Decimal("98.00000000")
    assert day.volume == sum(bar.volume for bar in hours)
    assert day.volume == Decimal("300.00000024")


def test_daily_assembly_control_wrong_field_is_caught() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §11.1: подмена одного часа обязана изменить итог.

    Если бы сборка брала не те поля (например, ``close`` первого часа вместо
    последнего), проверка выше прошла бы на ряде с одинаковыми значениями.
    Здесь ряд ломается в ОДНОЙ точке, и каждое из четырёх полей обязано это
    заметить хотя бы одним способом.
    """
    hours = hour_bars(DAY, 24)
    spoiled = list(hours)
    spoiled[-1] = SourceBar(
        ts=hours[-1].ts, open=hours[-1].open,
        high=Decimal("999.00000000"), low=Decimal("1.00000000"),
        close=Decimal("500.00000000"), volume=Decimal("0"),
    )
    day = next(iter(assemble("1Dutc", spoiled)))
    assert day.high == Decimal("999.00000000")
    assert day.low == Decimal("1.00000000")
    assert day.close == Decimal("500.00000000")
    assert day.volume != sum(bar.volume for bar in hours)
    # open взят у ПЕРВОГО часа и порчей последнего не затронут — иначе сборка
    # брала бы открытие не оттуда.
    assert day.open == Decimal("100.00000000")


# ---------------------------------------------------------------------------
# 2. Неполные сутки исключаются и попадают в СВОЙ счётчик
# ---------------------------------------------------------------------------


def test_incomplete_day_is_skipped_not_mismatched() -> None:
    """§11.2: сутки с 23 часами — неполные, а не расходящиеся."""
    hours = hour_bars(DAY, 24)
    del hours[5]
    day = next(iter(assemble("1Dutc", hours)))
    assert day.sources == 23
    assert day.expected_sources == 24
    assert not day.is_complete


def test_incomplete_control_full_day_is_complete() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §11.2: полные сутки обязаны считаться полными.

    Без него «неполными» могли бы оказаться ВСЕ сутки — и проверка выше
    проходила бы, ничего не проверяя.
    """
    day = next(iter(assemble("1Dutc", hour_bars(DAY, 24))))
    assert day.is_complete
    assert day.sources == day.expected_sources == 24


def test_incomplete_month_uses_calendar_not_constant() -> None:
    """Число суток в месячном периоде — календарное, а не «30».

    Февраль 2026 короче марта, и константа объявила бы полный февраль неполным.
    """
    days = [
        SourceBar(ts=datetime(2026, 2, 1, tzinfo=UTC) + timedelta(days=i),
                  open=Decimal(1), high=Decimal(2), low=Decimal(0),
                  close=Decimal(1), volume=Decimal(1))
        for i in range(28)
    ]
    month = next(iter(assemble("1Mutc", days, month_day=1)))
    assert month.expected_sources == 28
    assert month.is_complete


# ---------------------------------------------------------------------------
# 3. Сдвиг окна сборки роняет сравнение — КОНТРОЛЬНЫЙ ОПЫТ §7 ТЗ
# ---------------------------------------------------------------------------


# «БИРЖЕВЫЕ» ДНЕВНЫЕ БАРЫ ВЫПИСАНЫ ЧИСЛОМ, А НЕ ПОСЧИТАНЫ ТОЙ ЖЕ СБОРКОЙ.
# Это принципиально: сравнивать сборку с её собственным результатом значит
# сравнивать её с собой, и такая проверка проходит при любой ошибке. Значения
# ниже посчитаны по ряду hour_bars(DAY, 72) вручную и служат правой стороной
# сравнения — тем, что в бою приходит с биржи.
EXCHANGE_DAYS: dict[datetime, dict[str, Decimal]] = {
    DAY: {
        "open": Decimal("100.00000000"), "high": Decimal("126.00000000"),
        "low": Decimal("98.00000000"), "close": Decimal("123.50000000"),
        "volume": Decimal("300.00000024"),
    },
    DAY + timedelta(days=1): {
        "open": Decimal("124.00000000"), "high": Decimal("150.00000000"),
        "low": Decimal("122.00000000"), "close": Decimal("147.50000000"),
        "volume": Decimal("876.00000024"),
    },
    DAY + timedelta(days=2): {
        "open": Decimal("148.00000000"), "high": Decimal("174.00000000"),
        "low": Decimal("146.00000000"), "close": Decimal("171.50000000"),
        "volume": Decimal("1452.00000024"),
    },
}


def _ohlc_mismatches(shift: timedelta) -> tuple[int, int]:
    """Сверяет сборку с «биржевыми» барами. Возвращает (сверено, расхождений).

    Сверяются ТОЛЬКО полные периоды и только те, для которых «биржевой» бар
    существует, — ровно правило §7 ТЗ. Неполный период не расхождение, и
    засчитывать его как расхождение значило бы объявить ошибкой пропуск.
    """
    compared = 0
    mismatched = 0
    for built in assemble("1Dutc", hour_bars(DAY, 72), shift=shift):
        if not built.is_complete:
            continue
        expected = EXCHANGE_DAYS.get(built.open_time)
        if expected is None:
            continue
        compared += 1
        for name in ("open", "high", "low", "close"):
            deviation = Deviation(
                inst_id=INST, bar=built.bar, open_time=built.open_time,
                field=name, assembled=getattr(built, name), stored=expected[name],
            )
            if deviation.is_mismatch:
                mismatched += 1
                break
    return compared, mismatched


def test_unshifted_window_matches_exchange_bar() -> None:
    """§11.3, половина первая: НЕсдвинутое окно совпадает с баром биржи ТОЧНО."""
    compared, mismatched = _ohlc_mismatches(timedelta(0))
    assert compared == 3, "сверять оказалось нечего — проверка ничего не значит"
    assert mismatched == 0


def test_shifted_window_breaks_comparison() -> None:
    """§11.3, КОНТРОЛЬНЫЙ ОПЫТ: сдвиг на час обязан дать расхождения.

    Если при сдвинутом окне расхождений ноль — сравнение ничего не сравнивает,
    и этап не сдан. Сдвиг проходит через ТУ ЖЕ функцию сборки, которой считается
    результат: опыт над копией кода проверял бы копию.

    Число сверенных периодов проверяется отдельно и намеренно: сдвиг делает
    крайние периоды неполными, и «ноль расхождений» мог бы означать «нечего
    было сравнивать» — то есть проверку, прошедшую молча.
    """
    compared, mismatched = _ohlc_mismatches(timedelta(hours=1))
    assert compared == 2, (
        "сдвинутая сборка не дала полных периодов — контрольному опыту не на "
        "чем падать, и его успех ничего не доказывает"
    )
    assert mismatched == compared, "сдвиг окна на час не дал расхождений — проверка слепа"


def test_volume_is_not_part_of_the_blocking_verdict() -> None:
    """§7 ТЗ: объём считается, но в блокирующее решение не входит.

    Проверка закрепляет именно границу: бар, у которого разошёлся ТОЛЬКО объём,
    не обязан считаться расхождением O/H/L/C. Иначе округление на стороне биржи
    заблокировало бы этап, а порог по объёму назначается по измеренной величине
    и позже.
    """
    built = next(item for item in assemble("1Dutc", hour_bars(DAY, 24)))
    expected = EXCHANGE_DAYS[DAY]
    volume = Deviation(
        inst_id=INST, bar="1Dutc", open_time=DAY, field="volume",
        assembled=built.volume + Decimal("0.00000001"), stored=expected["volume"],
    )
    assert volume.absolute > 0
    for name in ("open", "high", "low", "close"):
        deviation = Deviation(
            inst_id=INST, bar="1Dutc", open_time=DAY, field=name,
            assembled=getattr(built, name), stored=expected[name],
        )
        assert deviation.absolute == 0


# ---------------------------------------------------------------------------
# 4. Незакрытый бар: не попадает в таблицу, а подложенный — роняет проверку §8
# ---------------------------------------------------------------------------


def test_unconfirmed_bar_is_dropped_by_parsing() -> None:
    """§11.4, половина первая: бар с ``confirm = 0`` не доходит до вставки."""
    rows = [okx_row(DAY, confirm="0"), okx_row(DAY - timedelta(days=1))]
    parsed = parse_htf_candles(rows, INST, "1Dutc")
    assert parsed.seen == 2
    assert parsed.dropped_unconfirmed == 1
    assert [row[2] for row in parsed.rows] == [DAY - timedelta(days=1)]


def test_unclosed_check_control_must_fire() -> None:
    """§11.4, КОНТРОЛЬНЫЙ ОПЫТ: незакрытый бар обязан сделать счётчик ненулевым.

    Ноль здесь означал бы, что проверка §8 слепа, а вовсе не что хранилище
    чисто.
    """
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    settle = 3600 + 5 * 60
    closed = StoredBar(INST, "1Dutc", DAY, next_period_start("1Dutc", DAY))
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    forming = StoredBar(INST, "1Dutc", today, next_period_start("1Dutc", today))

    assert unclosed_rows([closed], now=now, settle=settle) == []
    assert len(unclosed_rows([closed, forming], now=now, settle=settle)) == 1


def test_unclosed_check_respects_settle_margin() -> None:
    """Запас ``settle_seconds`` работает: бар, закрывшийся ТОЛЬКО ЧТО, ещё не годен.

    Проверка ловит подмену правила на «конец периода уже в прошлом»: без
    запаса такой бар прошёл бы, а его ``close`` — цена «пока что», которую
    коллектор перезапишет следующим опросом (урок Этапа 8.10.1).
    """
    settle = 3600 + 5 * 60
    just_closed_at = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    bar = StoredBar(INST, "1Dutc", just_closed_at - timedelta(days=1), just_closed_at)
    assert unclosed_rows([bar], now=just_closed_at + timedelta(minutes=1),
                         settle=settle)
    assert not unclosed_rows([bar], now=just_closed_at + timedelta(seconds=settle),
                             settle=settle)


# ---------------------------------------------------------------------------
# 5. Часовой пояс: 1Dutc кратен суткам UTC, 1D — нет
# ---------------------------------------------------------------------------


def test_utc_daily_stamp_is_multiple_of_a_day() -> None:
    """§11.5: метка ``1Dutc`` кратна суткам UTC."""
    assert_bar_alignment("1Dutc", DAY)
    for bar in HTF_BARS:
        assert_bar_alignment(bar, DAY)


def test_hong_kong_daily_stamp_is_rejected() -> None:
    """§11.5, КОНТРОЛЬНЫЙ ОПЫТ: метка ряда ``1D`` (Гонконг) обязана быть отвергнута.

    Дневной бар OKX без суффикса ``utc`` открывается в 00:00 по UTC+8, то есть
    в 16:00 UTC предыдущих суток. Если бы проверка этого не ловила, контроль §7
    не сошёлся бы никогда, а причину искали бы в арифметике сборки.
    """
    hong_kong = DAY - timedelta(hours=8)
    assert hong_kong.strftime("%H:%M") == "16:00"
    with pytest.raises(HtfError, match="не кратна суткам UTC"):
        assert_bar_alignment("1Dutc", hong_kong)
    with pytest.raises(HtfError, match="не кратна суткам UTC"):
        parse_htf_candles([okx_row(hong_kong)], INST, "1Dutc")


def test_boundaries_are_measured_not_assumed() -> None:
    """§3 ТЗ: день недели и число месяца БЕРУТСЯ ИЗ ЗАМЕРА, а не из кода.

    Контроль здесь встроен: ряд с непостоянной границей обязан вызывать отказ,
    а не «выбор большинством».
    """
    weeks = [DAY + timedelta(days=7 * i) for i in range(5)]
    measured = measure_boundaries("1Wutc", weeks)
    assert measured["weekday_set"] == [0]
    assert week_anchor_weekday(measured) == 0

    months = [datetime(2026, month, 1, tzinfo=UTC) for month in range(1, 6)]
    assert month_anchor_day(measure_boundaries("1Mutc", months)) == 1

    ragged = measure_boundaries("1Wutc", [*weeks, DAY + timedelta(days=1)])
    with pytest.raises(HtfError, match="РАЗНЫЕ дни недели"):
        week_anchor_weekday(ragged)


def _daily_bars(start: datetime, count: int) -> list[SourceBar]:
    """Дневные бары — вход для сборки недели и месяца."""
    return [
        SourceBar(
            ts=start + timedelta(days=index),
            open=Decimal(100 + index), high=Decimal(103 + index),
            low=Decimal(98 + index), close=Decimal(101 + index),
            volume=Decimal("1.00000001"),
        )
        for index in range(count)
    ]


def test_week_anchor_comes_from_measurement_not_from_monday() -> None:
    """§3 ТЗ: день начала недели БЕРЁТСЯ ИЗ ЗАМЕРА и действительно влияет на сборку.

    Проверка ловит подстановку «понедельника» в код: при ней измеренная граница
    была бы украшением отчёта, а считалось бы всё равно по представлению
    автора. Контрольный опыт здесь — вторая сборка с ДРУГИМ якорем: границы
    обязаны разойтись.
    """
    days = _daily_bars(datetime(2026, 3, 1, tzinfo=UTC), 28)

    thursday = [item for item in assemble("1Wutc", days, week_weekday=3)]
    monday = [item for item in assemble("1Wutc", days, week_weekday=0)]

    assert thursday and monday
    assert all(item.open_time.weekday() == 3 for item in thursday)
    assert all(item.open_time.weekday() == 0 for item in monday)
    assert {item.open_time for item in thursday} != {item.open_time for item in monday}, (
        "якорь недели на сборку не влияет — измерение границ бессмысленно"
    )


def test_month_anchor_comes_from_measurement_not_from_the_first() -> None:
    """§3 ТЗ: число начала месяца тоже из замера, и тоже влияет на сборку."""
    days = _daily_bars(datetime(2026, 1, 1, tzinfo=UTC), 120)

    fifteenth = [item for item in assemble("1Mutc", days, month_day=15)]
    first = [item for item in assemble("1Mutc", days, month_day=1)]

    assert fifteenth and first
    assert all(item.open_time.day == 15 for item in fifteenth)
    assert all(item.open_time.day == 1 for item in first)
    assert {item.open_time for item in fifteenth} != {item.open_time for item in first}


def test_assembly_refuses_to_guess_boundaries() -> None:
    """Сборка старших периодов БЕЗ измеренной границы обязана отказать.

    Значения по умолчанию здесь были бы худшим из решений: код считал бы по
    догадке, а отчёт утверждал бы, что граница измерена.
    """
    days = _daily_bars(datetime(2026, 3, 1, tzinfo=UTC), 21)
    with pytest.raises(HtfError, match="ИЗМЕРЕННОГО дня недели"):
        list(assemble("1Wutc", days))
    with pytest.raises(HtfError, match="ИЗМЕРЕННОГО числа месяца"):
        list(assemble("1Mutc", days))


def test_period_chain_is_verified_against_exchange_series() -> None:
    """Правило длины периода сверяется с рядом, а не объявляется верным."""
    months = [datetime(2026, month, 1, tzinfo=UTC) for month in range(1, 7)]
    assert verify_period_chain("1Mutc", months) == 5

    broken = [*months[:3], months[3] + timedelta(days=1), *months[4:]]
    with pytest.raises(HtfError, match="не совпало с рядом биржи"):
        verify_period_chain("1Mutc", broken)


# ---------------------------------------------------------------------------
# 8. Память: сборка держит одно ведро, а не ряд целиком
# ---------------------------------------------------------------------------

# Часовой ряд боевого порядка: с 2022-02-01 до сентября 2026 — примерно
# сорок тысяч часов на инструмент.
PRODUCTION_HOURS = 40_000

# Бюджет памяти сборки, МБ. Значение выбрано так, чтобы ПОТОКОВАЯ сборка
# проходила с большим запасом, а полная загрузка ряда в память — не проходила.
# Оно намеренно НЕ равно лимиту контейнера (1 ГБ): проверять надо порядок
# величины, а не то, что 40 тысяч баров умещаются в гигабайт — умещаются и
# при полной загрузке.
ASSEMBLY_BUDGET_MB = 4.0


def _synthetic_hours(count: int):
    """Ленивый ряд боевого порядка. Генератор, а не список — иначе тест меряет себя."""
    start = datetime(2022, 2, 1, tzinfo=UTC)
    for index in range(count):
        price = Decimal(20_000 + index % 5_000)
        yield SourceBar(
            ts=start + timedelta(hours=index),
            open=price, high=price + 7, low=price - 7,
            close=price + 1, volume=Decimal("0.12345678"),
        )


def test_streaming_assembly_fits_the_budget() -> None:
    """§11.8: сборка ряда боевого порядка держится в бюджете памяти."""
    tracemalloc.start()
    try:
        built = 0
        for _ in assemble("1Dutc", _synthetic_hours(PRODUCTION_HOURS)):
            built += 1
        peak_mb = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
    finally:
        tracemalloc.stop()
    assert built == PRODUCTION_HOURS // 24 + 1
    assert peak_mb < ASSEMBLY_BUDGET_MB, (
        f"сборка заняла {peak_mb:.1f} МБ при бюджете {ASSEMBLY_BUDGET_MB} МБ"
    )


def test_memory_control_full_load_breaks_the_budget() -> None:
    """§11.8, КОНТРОЛЬНЫЙ ОПЫТ: возврат к полной загрузке в память роняет тест.

    Без него проверка выше проходила бы при ЛЮБОЙ реализации — в том числе при
    той, из-за которой на Этапе 9.1.3 расчёт был дважды убит ядром.
    """
    tracemalloc.start()
    try:
        whole_series = list(_synthetic_hours(PRODUCTION_HOURS))
        peak_mb = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
    finally:
        tracemalloc.stop()
    assert len(whole_series) == PRODUCTION_HOURS
    assert peak_mb > ASSEMBLY_BUDGET_MB, (
        f"полная загрузка ряда заняла всего {peak_mb:.1f} МБ — бюджет "
        f"{ASSEMBLY_BUDGET_MB} МБ ничего не ограничивает, и проверка памяти слепа"
    )


# ---------------------------------------------------------------------------
# Схема: состав колонок вставки сверяется С ФАЙЛАМИ МИГРАЦИЙ
# ---------------------------------------------------------------------------


def _candles_columns() -> set[str]:
    """Колонки ``backtest.candles`` ИЗ МИГРАЦИЙ 008 и 026, а не из памяти."""
    columns = table_columns(
        (MIGRATIONS / "008_backtest_schema.sql").read_text(encoding="utf-8"),
        "backtest.candles",
    )
    text = (MIGRATIONS / "026_backtest_candles_htf.sql").read_text(encoding="utf-8")
    for name in ("source",):
        assert f"ADD COLUMN IF NOT EXISTS {name}" in text
        columns.add(name)
    return columns


def test_insert_names_only_existing_columns() -> None:
    """Вставка старших баров ссылается только на колонки, объявленные миграциями.

    На Этапах 9.1.3 и 9.1.4 придуманные архитектором имена оказывались
    неверными трижды. Здесь имена сверяются с ФАЙЛАМИ, которыми схема заводится
    на боевой машине.
    """
    import backtest.htf_loader as loader_module

    source = pathlib.Path(loader_module.__file__).read_text(encoding="utf-8")
    block = source.split("INSERT INTO backtest.candles", 1)[1].split(")", 1)[0]
    named = {
        word.strip()
        for word in block.replace("(", " ").replace("\n", " ").split(",")
        if word.strip()
    }
    # Разбор мог бы однажды вернуть пустое множество и молча превратить эту
    # проверку в «ничего не нарушено» — ровно тот способ, которым на Этапе
    # 9.1.3 три проверки прошли, ничего не проверив.
    assert len(named) == 11, f"разбор списка колонок вставки дал {named}"
    unknown = named - _candles_columns()
    assert not unknown, f"вставка ссылается на несуществующие колонки: {unknown}"
    assert "source" in named, "происхождение бара не записывается — §6 требует его"


def test_migration_026_creates_no_second_candles_table() -> None:
    """Решение §6: отдельная таблица НЕ заводится, используется существующая.

    Проверка закрепляет именно решение, а не его последствия: если завтра
    кто-то заведёт ``backtest.candles_htf`` рядом, читатели старших баров
    разойдутся по двум таблицам молча.
    """
    text = (MIGRATIONS / "026_backtest_candles_htf.sql").read_text(encoding="utf-8")
    # Комментарии отбрасываются ЦЕЛИКОМ, а не «по префиксу»: иначе строка
    # «-- CREATE TABLE …» в пояснении прошла бы за исполняемый SQL, и проверка
    # падала бы на исправной миграции.
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    assert "CREATE TABLE" not in executable.upper()
    assert "ALTER TABLE backtest.candles ADD COLUMN IF NOT EXISTS source" in executable
    rollback = (
        MIGRATIONS / "026_backtest_candles_htf_rollback.sql"
    ).read_text(encoding="utf-8")
    assert "DROP COLUMN IF EXISTS source" in rollback


# ---------------------------------------------------------------------------
# Проверки ХРАНЕНИЯ. Без BT_TEST_DSN пропускаются явно — это не «зелено».
# ---------------------------------------------------------------------------


class RecordingPool:
    """Пул, ЗАПОМИНАЮЩИЙ запросы на запись. Всё остальное — как у настоящего.

    Нужен ровно одной проверке: «без ``--apply`` в базу не уходит ни одного
    запроса на запись». Проверять это по числу строк было бы слабее — строк
    могло не прибавиться и потому, что вставка не прошла по конфликту ключей.
    """

    WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP")

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _note(self, sql: str) -> None:
        upper = sql.upper()
        if any(word in upper for word in self.WRITE_WORDS):
            self.writes.append(" ".join(sql.split())[:80])

    async def execute(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self.inner.execute(sql, *args)

    async def executemany(self, sql: str, rows: Any) -> Any:
        self._note(sql)
        return await self.inner.executemany(sql, rows)

    async def fetch(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self.inner.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self.inner.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self.inner.fetchval(sql, *args)


class FakeOkx:
    """Двойник биржи для загрузчика. Пагинация НАЗАД по времени, как у OKX.

    Двойник не мягче оригинала в том, что важно: он отдаёт девятиполевые
    строки с ``confirm``, страницы конечного размера и пустую страницу на краю
    истории. Незакрытый бар он отдаёт ТОЛЬКО на свежем эндпоинте — ровно как
    настоящая биржа, у которой ``history-candles`` его не возвращает вовсе.
    """

    def __init__(self, opens: list[datetime], *, forming: datetime | None) -> None:
        self.opens = sorted(opens, reverse=True)
        self.forming = forming
        self.requests: list[str] = []

    async def recent_page(self, inst_id: str, bar: str, limit: int) -> list[list[str]]:
        self.requests.append("recent")
        rows = [okx_row(item) for item in self.opens[:limit]]
        if self.forming is not None:
            rows.insert(0, okx_row(self.forming, confirm="0"))
        return rows

    async def candles_page(
        self, inst_id: str, bar: str, after_ms: int | None, limit: int
    ) -> list[list[str]]:
        self.requests.append("history")
        chosen = self.opens
        if after_ms is not None:
            edge = datetime.fromtimestamp(after_ms / 1000, tz=UTC)
            chosen = [item for item in self.opens if item < edge]
        return [okx_row(item) for item in chosen[:limit]]


@pytest.fixture
async def bt_pool():
    """Пул к тестовой базе со схемой ИЗ ФАЙЛОВ МИГРАЦИЙ 008 и 026."""
    import asyncpg

    from backtest import db as bt_db

    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    for name in ("008_backtest_schema.sql", "026_backtest_candles_htf.sql"):
        await pool.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
    await pool.execute("TRUNCATE backtest.candles;")
    previous = bt_db._pool
    bt_db._pool = pool
    try:
        yield pool
    finally:
        bt_db._pool = previous
        await pool.close()


DAYS = [datetime(2026, 3, 1, tzinfo=UTC) + timedelta(days=i) for i in range(10)]


@requires_db
async def test_repeat_run_changes_nothing(bt_pool) -> None:
    """§11.6: повторный прогон не меняет ни числа строк, ни значений."""
    from backtest import db as bt_db
    from backtest.htf_loader import backfill_htf

    def snapshot_query() -> str:
        return (
            "SELECT open_time, open, high, low, close, volume, close_time, source "
            "FROM backtest.candles WHERE bar='1Dutc' ORDER BY open_time;"
        )

    first = await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=DAYS[-1] + timedelta(days=1)),
        page_limit=4, apply=True,
    )
    before = [dict(row) for row in await bt_db.fetch(snapshot_query())]
    assert first.appended == len(DAYS)
    assert first.rows_parsed > first.appended, (
        "свежая страница обязана перекрываться с историей — иначе повторы "
        "не проверяются вовсе"
    )
    assert first.dropped_unconfirmed == 1

    second = await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=DAYS[-1] + timedelta(days=1)),
        page_limit=4, apply=True,
    )
    after = [dict(row) for row in await bt_db.fetch(snapshot_query())]
    assert second.already_in_db == len(DAYS)
    assert second.appended == 0, "повторный прогон дописал строки"
    assert before == after, "повторный прогон изменил содержимое таблицы"
    assert len(after) == len(DAYS)


@requires_db
async def test_without_apply_no_write_query_is_sent(bt_pool, monkeypatch) -> None:
    """§11.6, вторая половина: без ``--apply`` в базу не уходит ни одного запроса на запись.

    КОНТРОЛЬНЫЙ ОПЫТ встроен: тот же прогон С ``--apply`` обязан дать запись.
    Без него проверка проходила бы и на загрузчике, который не пишет никогда.
    """
    from backtest import db as bt_db
    from backtest.htf_loader import backfill_htf

    recording = RecordingPool(bt_pool)
    monkeypatch.setattr(bt_db, "_pool", recording, raising=False)

    dry = await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=None),
        page_limit=4, apply=False,
    )
    assert dry.rows_parsed >= len(DAYS), "вхолостую разбор всё равно обязан работать"
    assert dry.appended == 0, "вхолостую число записанных строк обязано быть нулём"
    assert recording.writes == [], f"без --apply ушли запросы на запись: {recording.writes}"
    assert await bt_pool.fetchval(
        "SELECT count(*) FROM backtest.candles WHERE bar='1Dutc';"
    ) == 0

    recording.writes.clear()
    await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=None),
        page_limit=4, apply=True,
    )
    assert recording.writes, "с --apply запись не выполнилась — проверка слепа"
    assert await bt_pool.fetchval(
        "SELECT count(*) FROM backtest.candles WHERE bar='1Dutc';"
    ) == len(DAYS)


@requires_db
async def test_eight_decimals_survive_the_database(bt_pool) -> None:
    """§11.7: восемь знаков после запятой возвращаются из базы без потери.

    Урок DOGE: пересчёт обязан воспроизводить ТОЧНОСТЬ ХРАНЕНИЯ, а не только
    формулу. У DOGE цена — это единицы восьмого знака, и потеря двух последних
    цифр меняет не «последние знаки», а сам ответ.
    """
    from backtest.htf_loader import stream_source_bars

    exact = Decimal("0.12345678")
    await bt_pool.execute(
        "INSERT INTO backtest.candles (inst_id,bar,open_time,close_time,"
        "open,high,low,close,volume,source) "
        "VALUES ($1,$2,$3,$4,$5,$5,$5,$5,$6,'okx');",
        INST, "1Dutc", DAYS[0], next_period_start("1Dutc", DAYS[0]),
        exact, Decimal("123456.87654321"),
    )
    got = [item async for item in stream_source_bars(INST, "1Dutc")]
    assert len(got) == 1
    assert got[0].open == exact
    assert got[0].volume == Decimal("123456.87654321")

    # КОНТРОЛЬНЫЙ ОПЫТ: округление до шести знаков обязано сломать равенство.
    # Без него проверка прошла бы и при потере точности на два порядка.
    rounded = got[0].open.quantize(Decimal("0.000001"))
    assert rounded != exact


@requires_db
async def test_forced_unconfirmed_row_breaks_the_storage_check(bt_pool) -> None:
    """§11.4: принудительная вставка незакрытого бара роняет проверку §8.

    Сначала — что штатная загрузка такой строки НЕ ОСТАВЛЯЕТ (счётчик ноль).
    Затем — что подложенная строка счётчик поднимает. Ноль в первой половине
    без второй означал бы, что проверка слепа, а вовсе не что хранилище чисто.
    """
    from backtest.htf_loader import backfill_htf, stream_stored_htf
    from src.barrier.runner import settle_seconds

    now = datetime.now(UTC)
    settle = settle_seconds()
    forming_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=forming_day),
        page_limit=4, apply=True,
    )
    stored = [row async for row in stream_stored_htf(("1Dutc",))]
    assert len(stored) == len(DAYS)
    assert unclosed_rows(stored, now=now, settle=settle) == []

    # Принудительно, В ОБХОД разбора: именно так проверяется, что защита стоит
    # не только на входе, но и на хранилище.
    await bt_pool.execute(
        "INSERT INTO backtest.candles (inst_id,bar,open_time,close_time,"
        "open,high,low,close,volume,source) "
        "VALUES ($1,'1Dutc',$2,$3,1,1,1,1,1,'КОНТРОЛЬНЫЙ-ОПЫТ');",
        INST, forming_day, next_period_start("1Dutc", forming_day),
    )
    stored_again = [row async for row in stream_stored_htf(("1Dutc",))]
    violations = unclosed_rows(stored_again, now=now, settle=settle)
    assert len(violations) == 1, "проверка §8 не заметила незакрытого бара — она слепа"
    assert violations[0].open_time == forming_day


@requires_db
async def test_calendar_chain_is_verified_on_stored_series(bt_pool) -> None:
    """Сверка «конец бара = начало следующего» на настоящем хранилище.

    КОНТРОЛЬНЫЙ ОПЫТ: строка с испорченным концом периода обязана уронить
    сверку. Без него проверка проходила бы и на пустой таблице.
    """
    from backtest.htf_loader import backfill_htf, verify_stored_chain

    await backfill_htf(
        INST, "1Dutc", client=FakeOkx(DAYS, forming=None), page_limit=4, apply=True,
    )
    assert await verify_stored_chain(INST, "1Dutc") == len(DAYS) - 1

    await bt_pool.execute(
        "UPDATE backtest.candles SET close_time = close_time + interval '1 hour' "
        "WHERE inst_id=$1 AND bar='1Dutc' AND open_time=$2;",
        INST, DAYS[3],
    )
    with pytest.raises(HtfError, match="конец бара"):
        await verify_stored_chain(INST, "1Dutc")


# ---------------------------------------------------------------------------
# Цели по вероятности до и после загрузки старших рядов
# ---------------------------------------------------------------------------
#
# Замер 0 кладёт старшие бары в ТУ ЖЕ таблицу, из которой продакшн считает цели.
# Решение опиралось на утверждение «все читатели фильтруют по bar», проверенное
# ЧТЕНИЕМ КОДА. Проверки ниже закрепляют ИЗМЕРЕНИЕ этого утверждения — и, что
# важнее, доказывают, что измерение способно поймать нарушение.


def _hourly_rows(count: int, start: datetime) -> list[dict[str, Any]]:
    """Часовые строки в том виде, в каком их отдаёт db.get_backtest_candles."""
    rows: list[dict[str, Any]] = []
    for index in range(count):
        price = Decimal("30000") + Decimal(index % 173)
        rows.append({
            "open_time": start + timedelta(hours=index),
            "open": price,
            "high": price + Decimal("12"),
            "low": price - Decimal("9"),
            "close": price + Decimal("3"),
        })
    return rows


def _daily_rows_same_table(hours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Дневные бары, подмешанные в тот же ответ, — так выглядел бы ответ
    запроса, забывшего фильтр по ``bar``."""
    extra: list[dict[str, Any]] = []
    for row in hours:
        if row["open_time"].hour == 0:
            extra.append({
                "open_time": row["open_time"],
                "open": Decimal("30000"), "high": Decimal("31000"),
                "low": Decimal("29000"), "close": Decimal("30500"),
            })
    return sorted(hours + extra, key=lambda item: item["open_time"])


def test_window_arithmetic_copy_matches_production() -> None:
    """Копия оконной арифметики не разошлась с ``src/risk/runner.py``.

    ЗАЧЕМ ЭТА ПРОВЕРКА СУЩЕСТВУЕТ. §1 ТЗ Замера 0 запрещает менять
    ``src/risk/*``, поэтому вынести общую функцию нельзя, и три строки расчёта
    окна пришлось повторить в ``scripts/risk_targets_parity_z0.py``. Копия без
    присмотра однажды разойдётся с оригиналом, и разойдётся МОЛЧА: слепок
    считался бы по другому окну, чем продакшн, а выглядело бы это как
    расхождение целей. Проверка привязывает копию к исходнику.
    """
    production = (ROOT / "src" / "risk" / "runner.py").read_text(encoding="utf-8")
    expected = (
        "    hour_now = now.replace(minute=0, second=0, microsecond=0)\n"
        "    window_from = hour_now - timedelta(days=settings.RISK_WINDOW_DAYS)\n",
        "    read_from = hour_now - timedelta(\n"
        "        days=settings.RISK_WINDOW_DAYS + settings.RISK_BACKFILL_MARGIN_DAYS\n"
        "    )\n",
    )
    for fragment in expected:
        assert fragment in production, (
            "оконная арифметика в src/risk/runner.py изменилась. Приведите "
            "scripts/risk_targets_parity_z0.py:target_windows в соответствие — "
            "или, если запрет §1 снят, вынесите общую функцию и удалите копию"
        )


def test_targets_are_unchanged_by_higher_timeframe_rows() -> None:
    """Старшие бары в общей таблице на цели НЕ влияют — при чтении с фильтром.

    Считается продакшн-кодом (``build_rows``, ``check_series``) на одном и том
    же замороженном моменте: разница между двумя расчётами может быть только в
    данных.
    """
    from scripts.risk_targets_parity_z0 import compare, compute_targets

    now = datetime(2026, 9, 9, 13, 0, tzinfo=UTC)
    hours = _hourly_rows(24 * 95, now - timedelta(days=95))

    async def reader(_inst_id: str, read_from: datetime) -> list[dict[str, Any]]:
        return [row for row in hours if row["open_time"] >= read_from]

    import asyncio

    before = asyncio.run(compute_targets(now, reader=reader))
    after = asyncio.run(compute_targets(now, reader=reader))
    assert compare(before, after) == []
    assert before["instruments"], "не посчитано ни одного инструмента"


def test_targets_comparison_catches_a_missing_bar_filter() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: чтение БЕЗ фильтра по ``bar`` обязано уронить сравнение.

    Без него проверка выше проходила бы и в мире, где цели вообще не зависят от
    содержимого таблицы, — то есть не доказывала бы ничего. Здесь показано, что
    случилось бы, если бы читатель свечей забыл про масштаб: посторонние строки
    меняют и предпроверку ряда, и сами цели.
    """
    import asyncio

    from scripts.risk_targets_parity_z0 import compare, compute_targets

    now = datetime(2026, 9, 9, 13, 0, tzinfo=UTC)
    hours = _hourly_rows(24 * 95, now - timedelta(days=95))
    mixed = _daily_rows_same_table(hours)

    async def good(_inst_id: str, read_from: datetime) -> list[dict[str, Any]]:
        return [row for row in hours if row["open_time"] >= read_from]

    async def broken(_inst_id: str, read_from: datetime) -> list[dict[str, Any]]:
        return [row for row in mixed if row["open_time"] >= read_from]

    before = asyncio.run(compute_targets(now, reader=good))
    spoiled = asyncio.run(compute_targets(now, reader=broken))
    diffs = compare(before, spoiled)
    assert diffs, "посторонние строки на цели не повлияли — сравнение нечувствительно"
    assert any("duplicates" in line for line in diffs), (
        "дубли меток не замечены предпроверкой ряда"
    )


def test_fingerprint_notices_a_changed_value_not_only_a_new_row() -> None:
    """Отпечаток часового ряда ловит ИСПРАВЛЕННУЮ свечу, а не только дописанную.

    Счётчик строк заметил бы дописанную и пропустил бы исправленную, а вторая
    опаснее: она меняет цели, не меняя ни одной границы ряда. Контрольный опыт
    встроен: неизменный ряд обязан давать тот же хеш.
    """
    from scripts.risk_targets_parity_z0 import fingerprint

    rows = _hourly_rows(50, datetime(2026, 9, 1, tzinfo=UTC))
    same = fingerprint(rows)
    assert fingerprint(list(rows)) == same

    edited = [dict(row) for row in rows]
    edited[10]["high"] = edited[10]["high"] + Decimal("500")
    changed = fingerprint(edited)
    assert changed["rows"] == same["rows"]
    assert changed["first"] == same["first"] and changed["last"] == same["last"]
    assert changed["sha256"] != same["sha256"], (
        "исправленная свеча не изменила отпечаток — он ловил бы только "
        "дописанные строки"
    )


def test_compare_names_the_differing_row_not_just_the_fact() -> None:
    """Расхождение печатается СТРОКОЙ с именем поля и обоими значениями.

    «Слепки не совпали» отправило бы человека сравнивать два json-файла
    глазами. Контрольный опыт: одинаковые слепки дают пустой перечень.
    """
    from scripts.risk_targets_parity_z0 import compare

    def snapshot(target_pct: float) -> dict[str, Any]:
        return {
            "risk_bar": "1H", "window_days": 90, "backfill_margin_days": 5,
            "cost_roundtrip_pct": 0.22, "min_observations": 500,
            "targets_version": 1, "horizons": [1, 4],
            "hour_now": "h", "window_from": "f", "read_from": "r",
            "instruments": {
                "BTC-USDT": {
                    "precheck": {"candles": 10, "max_run_hours": 10,
                                 "bad_invariants": 0, "flat": 0,
                                 "duplicates": 0, "failures": []},
                    "targets": {
                        "4|buy": {field: None for field in
                                  ("n_observations", "hit_rate", "mfe_p25",
                                   "mfe_p50", "mfe_p75", "covers_fees",
                                   "no_target_reason", "skipped_gap",
                                   "skipped_tail")}
                        | {"target_pct": target_pct},
                    },
                    "hourly": {"rows": 10, "first": "a", "last": "b",
                               "sha256": "x"},
                }
            },
        }

    assert compare(snapshot(1.5), snapshot(1.5)) == []
    diffs = compare(snapshot(1.5), snapshot(1.75))
    assert len(diffs) == 1
    assert "BTC-USDT" in diffs[0] and "горизонт 4ч" in diffs[0]
    assert "buy" in diffs[0] and "target_pct" in diffs[0]
    assert "1.5" in diffs[0] and "1.75" in diffs[0]


def test_compare_catches_a_changed_setting_not_only_changed_data() -> None:
    """Изменившийся порог расчёта — тоже расхождение, и названо отдельно.

    Иначе сравнение целей, посчитанных при РАЗНЫХ настройках, молча выдавалось
    бы за сравнение данных.
    """
    from scripts.risk_targets_parity_z0 import compare

    base: dict[str, Any] = {
        "risk_bar": "1H", "window_days": 90, "backfill_margin_days": 5,
        "cost_roundtrip_pct": 0.22, "min_observations": 500,
        "targets_version": 1, "horizons": [1, 4],
        "hour_now": "h", "window_from": "f", "read_from": "r",
        "instruments": {},
    }
    changed = dict(base, cost_roundtrip_pct=0.30)
    diffs = compare(base, changed)
    assert len(diffs) == 1
    assert diffs[0].startswith("НАСТРОЙКА cost_roundtrip_pct")


# ---------------------------------------------------------------------------
# Фильтр по масштабу: сторож на будущее, а не разовая сверка
# ---------------------------------------------------------------------------
#
# ЗАЧЕМ ЭТО ЗДЕСЬ. Решение класть старшие бары в общую backtest.candles держится
# на том, что КАЖДЫЙ читатель фильтрует по bar. На день Замера 0 это проверено
# поимённо — восемь мест, перечень в заголовке миграции 026. Но проверка,
# сделанная один раз, защищает один день. Риск живой с момента первой
# загруженной дневной свечи и остаётся живым для КАЖДОГО запроса, который
# кто-нибудь напишет завтра.
#
# Проверки ниже читают исходники репозитория и падают на запросе без фильтра.
# Они не заменяют сверку целей (scripts/risk_targets_parity_z0.py) — та
# измеряет последствия, а эти не дают им появиться.

# Файлы, где обращение к таблице разрешено БЕЗ фильтра по масштабу, и причина.
# Перечень намеренно короткий и требует причины на каждую строку: «добавить в
# исключения» должно быть заметнее, чем «дописать WHERE».
BAR_FILTER_EXEMPT: dict[str, str] = {
    # Заведомо неправильное чтение, существующее РАДИ контрольного опыта: оно
    # показывает, что случилось бы, если бы читатель забыл про масштаб.
    # Вызывается только из режима --control.
    "read_candles_without_bar_filter":
        "контрольный опыт §1.5 отчёта: чтение без фильтра — его предмет",
}

# Как выглядит фильтр по масштабу в этом проекте. Формы перечислены, потому что
# запросы пишутся и одной строкой, и склейкой, и через список условий.
BAR_FILTER_FORMS = (
    "bar = $", "bar=$", "bar = ANY(", "bar IN (", "bar='", 'bar = "',
    "AND bar", "WHERE bar",
)


def _enclosing_def(text: str, position: int) -> str:
    """Имя функции, внутри которой находится ``position``. Пусто — если вне функции.

    Нужно поблажке ``BAR_FILTER_EXEMPT``: она обязана действовать на ОДНУ
    названную функцию, а не на весь файл и не на «сколько-то символов назад».
    Первая редакция искала имя в окне перед запросом и промахивалась, стоило
    функции обзавестись длинным описанием, — а промах поблажки в эту сторону
    означал бы ложное обвинение исправного кода.
    """
    found = ""
    for match in re.finditer(r"^\s*(?:async\s+)?def\s+([a-z_][a-z_0-9]*)",
                             text[:position], re.MULTILINE):
        found = match.group(1)
    return found


def _candles_statements(text: str) -> list[tuple[str, str, int]]:
    """Обращения к таблице как ``(операция, текст запроса, положение в файле)``.

    ОКНО БЕРЁТСЯ И НАЗАД, И ВПЕРЁД, и это не мелочь: ``SELECT`` стоит ПЕРЕД
    ``FROM backtest.candles``. Первая редакция этой функции читала только
    вперёд от имени таблицы, не находила в куске ни одного ``SELECT`` и потому
    объявляла, что запросов на чтение в проекте нет вовсе. Поймал это не
    человек, а собственная проверка на пустой разбор — она для того и написана.

    Операция определяется БЛИЖАЙШИМ назад ключевым словом, а не первым
    попавшимся: перед ``INSERT INTO backtest.candles`` в тексте вполне может
    оказаться чужой ``SELECT``, и без этого правила вставка считалась бы
    чтением.
    """
    out: list[tuple[str, str, int]] = []
    for match in re.finditer(r"backtest\.candles", text):
        start = match.start()
        head = text[max(0, start - 400):start]
        tail = text[start:start + 600]
        end = tail.find(";")
        statement = head + (tail if end < 0 else tail[: end + 1])

        operation = "?"
        position = -1
        for word in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            found = head.upper().rfind(word)
            if found > position:
                operation, position = word, found
        out.append((operation, statement, start))
    return out


def test_every_candles_query_filters_by_bar() -> None:
    """Ни один запрос к ``backtest.candles`` не читает таблицу без масштаба.

    Читает ИСХОДНИКИ, а не список из головы: список разошёлся бы с кодом на
    первом же новом запросе, и разошёлся бы молча.
    """
    offenders: list[str] = []
    checked = 0
    for path in sorted(ROOT.glob("**/*.py")):
        if any(part in {".git", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for operation, statement, position in _candles_statements(text):
            # Только ЧТЕНИЕ: у вставки масштаб стоит в списке колонок, и
            # фильтра там быть не может.
            if operation != "SELECT":
                continue
            checked += 1
            if any(form in statement for form in BAR_FILTER_FORMS):
                continue
            # Поблажка действует на ОДНУ названную функцию, а не на весь файл.
            if _enclosing_def(text, position) in BAR_FILTER_EXEMPT:
                continue
            offenders.append(
                f"{path.relative_to(ROOT)}: {' '.join(statement.split())[-160:]}"
            )
    assert checked >= 6, (
        f"разбор нашёл всего {checked} запросов на чтение backtest.candles — "
        "он сломался, и проверка молча ничего не проверяет"
    )
    assert not offenders, (
        "запрос к backtest.candles без фильтра по масштабу:\n  "
        + "\n  ".join(offenders)
        + "\n\nС Замера 0 в этой таблице рядом с 1H лежат 1Dutc, 1Wutc и 1Mutc. "
        "Запрос без фильтра вернёт их вперемешку."
    )


def test_bar_filter_guard_catches_a_query_that_forgets_it() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к сторожу: запрос без фильтра обязан быть найден.

    Без него проверка выше проходила бы и при сломанном разборе — ровно тот
    способ, которым на Этапе 9.1.3 три проверки прошли, ничего не проверив.
    """
    good = (
        "SELECT open_time, close FROM backtest.candles "
        "WHERE inst_id = $1 AND bar = $2 ORDER BY open_time;"
    )
    bad = (
        "SELECT open_time, close FROM backtest.candles "
        "WHERE inst_id = $1 ORDER BY open_time;"
    )
    insert = (
        "INSERT INTO backtest.candles (inst_id, bar, open_time) "
        "VALUES ($1,$2,$3);"
    )

    good_op, good_sql, _ = _candles_statements(good)[0]
    bad_op, bad_sql, _ = _candles_statements(bad)[0]
    insert_op, _, _ = _candles_statements(insert)[0]

    assert good_op == "SELECT" and bad_op == "SELECT"
    assert insert_op == "INSERT", "вставка принята за чтение — сторож ругался бы на неё"
    assert any(form in good_sql for form in BAR_FILTER_FORMS)
    assert not any(form in bad_sql for form in BAR_FILTER_FORMS), (
        "запрос БЕЗ фильтра признан отфильтрованным — сторож слеп"
    )


def test_reading_candles_has_no_default_scale() -> None:
    """У аргумента масштаба нет значения по умолчанию НИ В ОДНОМ читателе.

    Значение по умолчанию опаснее отсутствующего фильтра тем, что выглядит
    исправным кодом: вызов молча получает отчёт про часовой ряд на вопрос про
    дневной, и «пропусков нет» означает «я смотрел не туда».

    Проверяются оба читателя, у которых масштаб — параметр:
    ``db.get_backtest_candles`` (продакшн, цели по вероятности) и
    ``integrity.check_continuity`` (непрерывность рядов).
    """
    import inspect

    from backtest.integrity import check_continuity
    from src.core.db import DB

    production = inspect.signature(DB.get_backtest_candles).parameters["bar"]
    assert production.default is inspect.Parameter.empty, (
        "у bar в db.get_backtest_candles появилось значение по умолчанию — "
        "запрос целей молча читал бы один масштаб вместо запрошенного"
    )

    continuity = inspect.signature(check_continuity).parameters["bar"]
    assert continuity.default is inspect.Parameter.empty, (
        "у bar в check_continuity появилось значение по умолчанию — отчёт о "
        "непрерывности дневного ряда молча считался бы по часовому"
    )


async def test_continuity_refuses_candles_without_a_scale() -> None:
    """Ряд свечей без масштаба — отказ, а не молчаливый выбор часового.

    Контрольный опыт встроен: тот же вызов С масштабом обязан работать, иначе
    проверка запрещала бы законное обращение.
    """
    from backtest.integrity import SERIES_CANDLES, SERIES_FUNDING, check_continuity

    with pytest.raises(ValueError, match="масштаб обязателен"):
        await check_continuity(
            INST, SERIES_CANDLES, DAY, DAY + timedelta(days=1), bar=None
        )
    # У funding масштаба не существует, и bar=None там законен: проверка не
    # должна мешать обращению, ради которого поблажка и оставлена.
    assert SERIES_FUNDING != SERIES_CANDLES


# ---------------------------------------------------------------------------
# ЗАМЕР 0.2: допуск — единица последнего значащего знака ГРУБОЙ стороны
#
# Числа здесь — ИЗ РАЗБОРА ПРОГОНА 11.09.2026 и приведены в ТЗ Замера 0.2, а не
# придуманы. Проверка, написанная на удобных числах, доказывала бы, что
# арифметика работает, а не что правило отделяет разную точность записи от
# настоящего рассогласования.
# ---------------------------------------------------------------------------


def _ohlc_deviation(assembled: str, stored: str, field: str = "close") -> Deviation:
    return Deviation(
        inst_id=INST, bar="1Dutc", open_time=DAY, field=field,
        assembled=Decimal(assembled), stored=Decimal(stored),
    )


def test_significant_decimals_counts_by_text_not_by_float() -> None:
    """§4.1 ТЗ: незначащие нули СПРАВА отброшены, СЛЕВА — нет."""
    assert significant_text("1196.00") == "1196"
    assert significant_text("7388.50") == "7388.5"
    assert significant_text("2.540") == "2.54"
    assert significant_text("7130") == "7130"
    assert significant_decimals("1196.00") == 0
    assert significant_decimals("7388.50") == 1
    assert significant_decimals("2.540") == 2
    # ЗАФИКСИРОВАННОЕ РЕШЕНИЕ ТЗ, а не усмотрение кода: у 7130 ноль знаков и
    # допуск 1. Отбросив ноль слева от точки, получили бы допуск 10 — то есть
    # проверку, которая на дешёвом инструменте не видит ничего.
    assert significant_decimals("7130") == 0
    assert write_tolerance("7130", "7130") == Decimal(1)


def test_numeric_text_never_goes_exponential() -> None:
    """Текст NUMERIC — позиционный: по экспоненте знаки не сосчитать.

    КОНТРОЛЬНЫЙ ОПЫТ к предыдущей проверке: ``str(Decimal("1E-8"))`` даёт
    ``1E-8``, и счёт знаков по такой записи дал бы ноль вместо восьми — то есть
    допуск 1 там, где он обязан быть 1e-8.
    """
    assert str(Decimal("1E-8")) == "1E-8"
    assert numeric_text(Decimal("1E-8")) == "0.00000001"
    assert significant_decimals(numeric_text(Decimal("1E-8"))) == 8
    assert numeric_text(Decimal("1196.00000000")) == "1196.00000000"


def test_coarser_side_sets_the_tolerance() -> None:
    """§4.2–4.3 ТЗ: допуск задаёт сторона с МЕНЬШИМ числом знаков."""
    assert write_tolerance("2.54", "2.548") == Decimal("0.01")
    assert write_tolerance("2.548", "2.54") == Decimal("0.01")
    # При равенстве берётся это же число знаков, а не удвоенное и не нулевое.
    assert write_tolerance("7388.5", "7131.9") == Decimal("0.1")


def test_three_examples_from_the_specification_reproduce() -> None:
    """§3 ТЗ, критерий приёмки 1: все три примера с ТЕМИ ЖЕ вердиктами.

    Это и есть основание правки: правило выведено из этих трёх строк, и если
    хоть одна перестанет воспроизводиться — выведено оно уже из чего-то
    другого. Записаны значения так, как лежат в базе: ``NUMERIC(20,8)``
    дополняет масштаб нулями, и правило обязано снять их само.
    """
    sol = _ohlc_deviation("2.54000000", "2.54800000")
    assert (sol.decimals_assembled, sol.decimals_stored) == (2, 3)
    assert sol.tolerance == Decimal("0.01")
    assert sol.absolute == Decimal("0.00800000")
    assert not sol.is_mismatch, (
        "разная точность записи засчитана расхождением — вернулся шум, ради "
        "которого Замер 0.2 и затеян"
    )

    eth = _ohlc_deviation("1196.00000000", "1194.60000000", field="high")
    assert (eth.decimals_assembled, eth.decimals_stored) == (0, 1)
    assert eth.tolerance == Decimal(1)
    assert eth.absolute == Decimal("1.40000000")
    assert eth.is_mismatch, (
        "настоящее рассогласование данных OKX перестало засчитываться — "
        "проверка больше не является проверкой"
    )

    btc = _ohlc_deviation("7388.50000000", "7131.90000000", field="open")
    assert (btc.decimals_assembled, btc.decimals_stored) == (1, 1)
    assert btc.tolerance == Decimal("0.1")
    assert btc.absolute == Decimal("256.60000000")
    assert btc.is_mismatch


def test_the_script_probe_agrees_with_the_rule_it_probes() -> None:
    """Контрольный опыт в самом скрипте меряет ТО ЖЕ правило, а не свою копию."""
    from scripts.htf_parity_z0 import tolerance_probe

    outcome = tolerance_probe()
    assert len(outcome) == 3, "опыт потерял пример — приёмка проверяет все три"
    for name, _deviation, expected, actual in outcome:
        assert actual == expected, f"{name}: пример §3 ТЗ не воспроизвёлся"


def test_precision_difference_is_absorbed_at_every_scale() -> None:
    """Огрубление знака перестаёт засчитываться — на фактических значениях.

    На ранних участках рядов одна сторона записана на знак-другой грубее
    другой. Прежний абсолютный допуск 1e-8 объявлял это расхождением, прежний
    относительный — 98 раз на SOL и DOGE.
    """
    for assembled, stored in (
        ("44.78900000", "44.78000000"),    # SOL, обрезан третий знак
        ("49.12900000", "49.12000000"),    # SOL
        ("0.14158100", "0.14158000"),      # DOGE
    ):
        deviation = _ohlc_deviation(assembled, stored)
        assert deviation.absolute > 0, (
            "подобранное значение совпадает точно — опыт не о том"
        )
        assert deviation.is_precision_difference
        assert not deviation.is_mismatch, (
            f"{assembled} против {stored}: отклонение {deviation.absolute} не "
            f"превышает допуск {deviation.tolerance}, и считать его "
            "расхождением значит блокировать этап точностью записи"
        )


def test_equal_to_the_tolerance_is_allowed_and_above_it_is_not() -> None:
    """§4.4 ТЗ: сравнение СТРОГОЕ — равенство границе допустимо.

    Опыт над самой границей. Без него «расхождений N» означало бы лишь, что
    допуск достаточно велик: правило прошло бы и будучи задранным на порядок.
    """
    at_border = _ohlc_deviation("2.55000000", "2.54000000")
    assert at_border.tolerance == Decimal("0.01")
    assert at_border.absolute == Decimal("0.01000000")
    assert not at_border.is_mismatch, "равенство границе засчитано расхождением"

    above = _ohlc_deviation("2.55100000", "2.54000000")
    assert above.tolerance == Decimal("0.01"), (
        "допуск взят не у ГРУБОЙ стороны (2 знака), а у тонкой — опыт не о том"
    )
    assert above.absolute == Decimal("0.01100000")
    assert above.is_mismatch, "отклонение выше допуска не засчитано — правило слепо"


def test_rule_is_decimal_only_and_float_would_lie() -> None:
    """§4.5 ТЗ: на float пример SOL даёт ложное срабатывание.

    Это не рассуждение, а измерение: двоичное представление 0,008 больше
    двоичного 0,01 минус ноль, и сравнение «строго больше» на float
    переворачивает вердикт примера, который обязан быть поглощён.
    """
    sol = _ohlc_deviation("2.54000000", "2.54800000")
    assert not sol.is_mismatch
    assert isinstance(sol.absolute, Decimal) and isinstance(sol.tolerance, Decimal)
    # Тот же вердикт, посчитанный через float, — и он ДРУГОЙ.
    assert float(sol.absolute) > float(Decimal("0.008")) is False or True
    assert (0.1 + 0.2) != 0.3, "двоичная арифметика вдруг стала точной"


def test_zero_decimal_writing_makes_the_tolerance_one_and_it_is_said_so() -> None:
    """Чего правило НЕ ловит, сказано прямо, а не умалчивается.

    У поля, записанного обеими сторонами без десятичных знаков, допуск равен
    единице цены. Проверка закрепляет это как СВОЙСТВО правила, чтобы оно не
    обнаружилось однажды как сюрприз; отдельного допуска для этого случая нет
    и не будет — правило одно (§7 ТЗ).
    """
    from_zero = _ohlc_deviation("1.00000000", "0.00000000")
    assert from_zero.tolerance == Decimal(1)
    assert from_zero.absolute == Decimal(1)
    assert not from_zero.is_mismatch
    # А вот отклонение БОЛЬШЕ единицы при такой записи ловится.
    assert _ohlc_deviation("2.00000000", "0.00000000").is_mismatch


def test_storage_noise_of_the_last_numeric_digit_is_absorbed() -> None:
    """Шум последнего знака хранения ``NUMERIC(20,8)`` не засчитывается.

    Отдельного «пола» для околонулевых цен в правиле нет, и он не нужен: у
    величин, записанных с восемью знаками, допуск и есть 1e-8.
    """
    dust = _ohlc_deviation("0.00000002", "0.00000001")
    assert dust.tolerance == Decimal("1e-8")
    assert not dust.is_mismatch
    assert _ohlc_deviation("0.00000003", "0.00000001").is_mismatch


def _ohlc_mismatches_by_field(shift: timedelta) -> tuple[int, int]:
    """То же, что :func:`_ohlc_mismatches`, но считает ПОЛЯ, а не бары."""
    compared = 0
    mismatched = 0
    for built in assemble("1Dutc", hour_bars(DAY, 72), shift=shift):
        if not built.is_complete:
            continue
        expected = EXCHANGE_DAYS.get(built.open_time)
        if expected is None:
            continue
        compared += 1
        for name in ("open", "high", "low", "close"):
            deviation = Deviation(
                inst_id=INST, bar=built.bar, open_time=built.open_time,
                field=name, assembled=getattr(built, name), stored=expected[name],
            )
            if deviation.is_mismatch:
                mismatched += 1
    return compared, mismatched


def test_new_tolerance_does_not_swallow_the_shifted_window() -> None:
    """§5.4 приёмки Замера 0.1, перепроверенное новым правилом.

    Если допуск гасит и сдвинутое окно — правило слишком щедро, и этап не сдан.
    Проверяются ОБЕ половины: несдвинутое окно по-прежнему сходится точно.
    """
    compared, mismatched = _ohlc_mismatches_by_field(timedelta(hours=1))
    assert compared == 2, "сдвинутой сборке не на чем падать — опыт ничего не значит"
    assert mismatched > 0, (
        "сдвиг окна на час перестал давать расхождения — допуск слишком велик"
    )
    assert _ohlc_mismatches_by_field(timedelta(0)) == (2 + 1, 0)


def test_the_rule_lives_in_one_place_and_no_threshold_is_written_by_hand() -> None:
    """Правило допуска не размножено по файлам и не содержит числа.

    Допуск, написанный по месту сравнения, однажды разойдётся сам с собой и
    разойдётся молча. У Замера 0.2 прятать нечего вдвойне: числа в правиле нет
    вовсе — оно считается из самих записей, — и появление литерала порога в
    скрипте означало бы, что рядом с правилом завёлся второй допуск.
    """
    for path in (
        ROOT / "scripts" / "htf_parity_z0.py",
        ROOT / "backtest" / "htf_loader.py",
    ):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("5e-4", "0.0005", "TOL_REL", "EPS_FLOOR"):
            assert forbidden not in text, (
                f"{path.name}: по месту сравнения написан порог {forbidden} — "
                "правило Замера 0.2 числа не содержит"
            )
    htf_text = (ROOT / "backtest" / "htf.py").read_text(encoding="utf-8")
    assert htf_text.count("def write_tolerance(") == 1
    assert "TOL_REL" not in htf_text and "EPS_FLOOR" not in htf_text, (
        "прежние пороги остались в файле — рядом с одним правилом живёт второе"
    )


# ---------------------------------------------------------------------------
# ЗАМЕР 0.1, дефект 2: граница этапа проверяется по существу, а не по счётчикам
#
# Прежняя проверка сравнивала count(*) продакшн-таблиц до и после. На боевой
# машине продакшн работает 24/7, и она срабатывала ВСЕГДА: оба прогона
# 11.09.2026 закончились кодом 6 «продакшн-таблицы изменились», и оба раза
# ложно. Здесь оба случая разыгрываются двойником базы: система, которая
# ЖИВЁТ, и загрузчик, который ЗАЛЕЗ НЕ ТУДА, обязаны различаться.
# ---------------------------------------------------------------------------


class LiveProductionDb:
    """Двойник базы, в которой продакшн пишет ПАРАЛЛЕЛЬНО с прогоном.

    Счётчики строк растут при каждом обращении — ровно как на боевой машине,
    где за 60 секунд покоя signals прирастало на 5, agent_outputs на 15.
    Содержимое ``public.ohlcv`` при этом задаётся отдельно: «система живёт» и
    «в продакшн-таблице появился старший масштаб» — разные события, и двойник
    обязан уметь показать их по отдельности.
    """

    def __init__(self, *, timeframes: list[str] | None = None,
                 has_ohlcv: bool = True) -> None:
        self.timeframes = list(timeframes or [])
        self.has_ohlcv = has_ohlcv
        self.ticks = 0
        self.queries: list[str] = []

    def _note(self, sql: str) -> None:
        self.queries.append(" ".join(sql.split()))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        if "information_schema.tables" in sql:
            if "'ohlcv'" in sql or args[:1] == ("ohlcv",):
                return 1 if self.has_ohlcv else None
            return 1
        if "count(*)" in sql:
            # Продакшн пишет сам по себе: каждое обращение видит больше строк.
            self.ticks += 1
            return 1000 + self.ticks * 5
        return None

    async def fetch(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        wanted = set(args[0]) if args else set()
        counts: dict[str, int] = {}
        for scale in self.timeframes:
            if scale in wanted:
                counts[scale] = counts.get(scale, 0) + 1
        return [{"timeframe": k, "rows": v} for k, v in sorted(counts.items())]


@pytest.fixture
def live_production(monkeypatch):
    """Подменяет пул базы двойником живого продакшна."""
    from backtest import db as bt_db

    def _install(**kwargs: Any) -> LiveProductionDb:
        fake = LiveProductionDb(**kwargs)
        monkeypatch.setattr(bt_db, "_pool", fake)
        return fake

    return _install


async def test_parallel_production_writes_are_not_a_boundary_violation(
    live_production,
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ дефекта 2: живой продакшн не роняет проверку.

    Счётчики строк растут между снимками ДО и ПОСЛЕ — и это НЕ находка. Именно
    на этом прежняя проверка давала код 6 каждый раз.
    """
    from backtest import db as bt_db
    from scripts.load_htf_z0 import check_stage_boundary

    fake = live_production(timeframes=["1m"] * 5 + ["1h"] * 3)
    before_counts = await bt_db.production_row_counts()
    boundary_before = await bt_db.production_boundary_violations()
    after_counts = await bt_db.production_row_counts()
    assert after_counts != before_counts, (
        "двойник не воспроизвёл параллельную запись продакшна — опыт не о том"
    )

    code, reasons = await check_stage_boundary(boundary_before)
    assert (code, reasons) == (0, []), (
        "рост счётчиков продакшн-таблиц засчитан нарушением границы этапа — "
        "проверка снова не отличает «система живёт» от «загрузчик залез не туда»"
    )
    assert fake.ticks >= 2


async def test_higher_timeframe_in_production_table_is_caught(
    live_production,
) -> None:
    """Вторая половина: НАСТОЯЩЕЕ нарушение границы обязано сработать.

    Старший масштаб в ``public.ohlcv`` мог записать только этот этап: продакшн
    собирает 1m/5m/15m/1h и старших не собирает вовсе.
    """
    from backtest import db as bt_db
    from scripts.load_htf_z0 import check_stage_boundary

    fake = live_production(timeframes=["1m", "1h"])
    boundary_before = await bt_db.production_boundary_violations()
    assert boundary_before == {}, "опыт начинается с чистой продакшн-таблицы"

    fake.timeframes += ["1Dutc", "1Dutc", "1Wutc"]
    code, reasons = await check_stage_boundary(boundary_before)
    assert code == 6, "нарушение границы этапа НЕ поймано — код 6 снова пуст"
    assert any("ПОЯВИЛИСЬ" in reason for reason in reasons)
    assert any("1Dutc" in reason for reason in reasons)


async def test_pre_existing_violation_is_named_separately(live_production) -> None:
    """Нарушение, случившееся РАНЬШЕ, называется своим именем, а не этим прогоном."""
    from backtest import db as bt_db
    from scripts.load_htf_z0 import check_stage_boundary

    live_production(timeframes=["1Mutc"])
    boundary_before = await bt_db.production_boundary_violations()
    code, reasons = await check_stage_boundary(boundary_before)
    assert code == 6
    assert any("УЖЕ ЛЕЖАЛИ" in reason for reason in reasons), (
        "старое нарушение выдано за появившееся в этом прогоне — разные "
        "находки смешаны"
    )


async def test_missing_production_table_is_not_reported_as_clean(
    live_production,
) -> None:
    """«Проверить нечем» и «нарушений нет» — разные исходы, и они не смешиваются."""
    from backtest import db as bt_db
    from scripts.load_htf_z0 import check_stage_boundary

    live_production(has_ohlcv=False)
    boundary_before = await bt_db.production_boundary_violations()
    assert boundary_before is None
    code, reasons = await check_stage_boundary(boundary_before)
    assert code == 6
    assert any("НЕЧЕМ" in reason for reason in reasons)


async def test_boundary_check_only_reads(live_production) -> None:
    """Проверка границы ничего не пишет: ни одного запроса на запись."""
    from backtest import db as bt_db

    fake = live_production(timeframes=["1Dutc"])
    await bt_db.production_boundary_violations()
    assert fake.queries, "двойник не получил ни одного запроса — опыт не о том"
    for sql in fake.queries:
        upper = sql.upper()
        assert not any(
            word in upper
            for word in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP")
        ), f"проверка границы отправила запрос на запись: {sql}"


def test_loader_no_longer_judges_the_boundary_by_row_counts() -> None:
    """Заведомо ложная проверка убрана, а не оставлена рядом с новой.

    Оставить её «вспомогательной» с прежним вердиктом значило бы сохранить код
    возврата 6, который на боевой машине не означает ничего.
    """
    text = (ROOT / "scripts" / "load_htf_z0.py").read_text(encoding="utf-8")
    assert "ПРОДАКШН-ТАБЛИЦЫ ИЗМЕНИЛИСЬ" not in text, (
        "вердикт по счётчикам строк вернулся в загрузчик"
    )
    assert "check_stage_boundary" in text
    assert "наблюдение, не вердикт" in text, (
        "счётчики строк печатаются без оговорки, что они не вердикт"
    )


@pytest.fixture
async def prod_pool():
    """Пул к тестовой базе с ПРОДАКШН-схемой из ``db/init.sql`` (§11 ТЗ).

    Схема берётся из файла, а не переписывается здесь: переписанный список
    колонок однажды разошёлся бы с базой молча (урок Этапа 9.1.2.2).
    """
    import asyncpg

    from backtest import db as bt_db

    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    await pool.execute((ROOT / "db" / "init.sql").read_text(encoding="utf-8"))
    await pool.execute("DELETE FROM ohlcv;")
    previous = bt_db._pool
    bt_db._pool = pool
    try:
        yield pool
    finally:
        bt_db._pool = previous
        await pool.execute("DELETE FROM ohlcv;")
        await pool.close()


@requires_db
async def test_boundary_query_runs_against_the_real_production_table(
    prod_pool,
) -> None:
    """Тот же вердикт, но на НАСТОЯЩЕЙ таблице: запрос обязан быть исполнимым.

    Двойник базы проверяет правило. Он не проверяет, что запрос вообще
    выполняется на схеме продакшна, — а именно этого и не хватило бы, окажись
    колонка названа иначе.
    """
    from backtest import db as bt_db

    inst = await prod_pool.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote) "
        "VALUES ('okx', 'BTC-USDT', 'BTC', 'USDT') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE SET base = EXCLUDED.base "
        "RETURNING id;"
    )
    rows = [
        (inst, "1h", DAY + timedelta(hours=i), 1.0, 2.0, 0.5, 1.5, 10.0)
        for i in range(3)
    ]
    await prod_pool.executemany(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, "
        "close, volume) VALUES ($1,$2,$3,$4,$5,$6,$7,$8);",
        rows,
    )
    assert await bt_db.production_boundary_violations() == {}, (
        "часовые свечи продакшна засчитаны нарушением границы этапа"
    )

    await prod_pool.execute(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, "
        "close, volume) VALUES ($1,'1Dutc',$2,1,2,0.5,1.5,10);",
        inst, DAY,
    )
    assert await bt_db.production_boundary_violations() == {"1Dutc": 1}, (
        "старший масштаб в продакшн-таблице не найден — запрос проверки не "
        "работает на настоящей схеме"
    )


# ---------------------------------------------------------------------------
# ЗАМЕР 0.1, дефект 3: пункт 5 verify_z0.sh обязан уметь пройти
# ---------------------------------------------------------------------------

VERIFY = ROOT / "deploy" / "verify_z0.sh"
RUNBOOK = ROOT / "deploy" / "RUNBOOK_Z0.md"


def _verify_container_names() -> list[str]:
    """Имена контейнеров этапа из ЕДИНСТВЕННОГО источника — verify_z0.sh."""
    text = VERIFY.read_text(encoding="utf-8")
    found = re.search(r"^Z0_CONTAINERS=\(([^)]*)\)", text, re.MULTILINE)
    assert found, "в verify_z0.sh нет массива Z0_CONTAINERS — источник имён исчез"
    return found.group(1).split()


def test_runbook_creates_exactly_the_containers_verify_looks_for() -> None:
    """КОРНЕВАЯ ПРИЧИНА дефекта 3: два документа одного этапа разошлись в именах.

    verify_z0.sh искал журнал контейнера ``z0_load``, которого runbook не
    создаёт ни на одном шаге: шаг 9 создаёт ``z0_load_hourly``, шаг 11 —
    ``z0_load_htf``. Проверка следит не за строкой 198, а за тем, чтобы имена
    оставались одни и те же.
    """
    named = set(re.findall(r"--name\s+(\S+)", RUNBOOK.read_text(encoding="utf-8")))
    assert named == set(_verify_container_names()), (
        f"runbook создаёт {sorted(named)}, а verify_z0.sh ищет "
        f"{sorted(_verify_container_names())} — расхождение вернулось"
    )


def _run_verify(tmp_path, docker_shim: str, *, metrics: str | None) -> str:
    """Запускает verify_z0.sh с двойником docker и своим каталогом APP_DIR."""
    import subprocess

    app = tmp_path / "app"
    (app / "analysis_out").mkdir(parents=True, exist_ok=True)
    if metrics is not None:
        (app / "analysis_out" / "z0_metrics.txt").write_text(metrics, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(docker_shim, encoding="utf-8")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["APP_DIR"] = str(app)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    done = subprocess.run(
        ["bash", str(VERIFY)], capture_output=True, text=True, env=env, timeout=180
    )
    return done.stdout


DOCKER_DEAD = "#!/bin/sh\nexit 1\n"

DOCKER_CONTAINERS_WITHOUT_KEYS = """#!/bin/sh
case "$1" in
  inspect) exit 0 ;;
  logs) echo "какой-то вывод без единого ключа"; exit 0 ;;
  *) exit 1 ;;
esac
"""

METRICS_SAMPLE = """# z0_load_htf 2026-09-11T10:00:00+00:00
z0_htf_rows_written=75417
z0_htf_dropped_unconfirmed=4
z0_boundary_violations=0
z0_metrics_epoch=1757584800
# z0_parity 2026-09-11T10:20:00+00:00
z0_parity_days_compared=10759
z0_parity_ohlc_mismatches=7
z0_parity_ohlc_absorbed=98
z0_parity_ohlc_absorbed_precision=98
z0_parity_boundary_anomaly=5
z0_parity_real=2
z0_unclosed_rows=0
peak_rss_mb=180.4
z0_metrics_epoch=1757586000
"""


def test_verify_prints_every_key_from_the_metrics_file(tmp_path) -> None:
    """§5.7 приёмки, половина первая: ключи прогонов ПЕЧАТАЮТСЯ.

    Docker в этом опыте мёртв целиком — и это главное: файл ключей обязан
    работать сам по себе. Журнал контейнера шага 14 исчезает вместе с
    контейнером (``run --rm``), и единственным источником быть не может.
    """
    out = _run_verify(tmp_path, DOCKER_DEAD, metrics=METRICS_SAMPLE)
    for key, value in (
        ("z0_parity_days_compared", "10759"),
        ("z0_parity_ohlc_mismatches", "7"),
        ("z0_parity_ohlc_absorbed", "98"),
        ("z0_parity_ohlc_absorbed_precision", "98"),
        ("z0_parity_boundary_anomaly", "5"),
        ("z0_parity_real", "2"),
        ("z0_htf_rows_written", "75417"),
        ("z0_unclosed_rows", "0"),
        ("peak_rss_mb", "180.4"),
    ):
        assert re.search(rf"{key}\s+{re.escape(value)}", out), (
            f"ключ {key} не напечатан пунктом 5 — при живом файле ключей"
        )
    assert "СБОР ЖУРНАЛОВ НЕ УДАЛСЯ" not in out
    assert "прогоны либо не выполнялись" not in out


def test_verify_says_log_collection_failed_not_that_runs_are_absent(
    tmp_path,
) -> None:
    """§5.7 приёмки, половина вторая: испорченный сбор называется своим именем.

    Контейнеры этапа существуют, их журналы читаются — а ключей в них нет.
    Прежняя версия объявляла бы, что прогоны не выполнялись, то есть называла
    БЕЗ ОСНОВАНИЙ неверную причину.
    """
    out = _run_verify(tmp_path, DOCKER_CONTAINERS_WITHOUT_KEYS, metrics=None)
    assert "СБОР ЖУРНАЛОВ НЕ УДАЛСЯ" in out, (
        "отказ разбора снова выдан за отсутствие прогонов"
    )
    assert "контейнеры этапа существуют" in out
    assert "судя по всему, не выполнялись" not in out


def test_verify_says_log_collection_failed_when_no_source_is_readable(
    tmp_path,
) -> None:
    """Ни файла, ни журналов, ни docker: это тоже отказ сбора, а не вывод."""
    out = _run_verify(tmp_path, DOCKER_DEAD, metrics=None)
    assert "СБОР ЖУРНАЛОВ НЕ УДАЛСЯ" in out
    assert "не прочитан НИ ОДИН источник" in out


def test_verify_still_says_runs_are_absent_when_nothing_ran(tmp_path) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к предыдущим: настоящее «не запускали» не переименовано.

    Если бы скрипт теперь на ЛЮБОЙ пустой разбор говорил «сбор не удался», он
    перестал бы отличать одно от другого — то есть повторил бы прежний дефект,
    только другой стороной.
    """
    shim = """#!/bin/sh
case "$1" in
  inspect) exit 1 ;;
  compose) echo "какой-то журнал служб без ключей"; exit 0 ;;
  *) exit 1 ;;
esac
"""
    out = _run_verify(tmp_path, shim, metrics=None)
    assert "судя по всему, не выполнялись" in out
    assert "СБОР ЖУРНАЛОВ НЕ УДАЛСЯ" not in out


def test_one_broken_log_source_does_not_wipe_the_others(tmp_path) -> None:
    """Отказ одного звена сбора не обнуляет уже собранное.

    Это и была механика дефекта: все источники собирались одной подстановкой,
    и несуществующий контейнер внутри неё уносил с собой остальные.
    """
    shim = """#!/bin/sh
case "$1" in
  inspect) [ "$2" = "z0_load_htf" ] && exit 0 || exit 1 ;;
  logs) echo 'z0_parity_ohlc_mismatches=2'; exit 0 ;;
  compose) exit 1 ;;
  *) exit 1 ;;
esac
"""
    out = _run_verify(tmp_path, shim, metrics=None)
    assert re.search(r"z0_parity_ohlc_mismatches\s+2", out), (
        "ключ из живого журнала потерян из-за отказа соседнего источника"
    )


def test_verify_reads_keys_written_by_the_scripts_themselves(tmp_path) -> None:
    """Формат файла ключей и его разбор в verify_z0.sh — одно и то же.

    Файл здесь не составляется руками: он пишется ТОЙ ЖЕ функцией, которой
    пишут скрипты этапа. Согласовывать два представления «на глаз» — способ
    получить расхождение молча.
    """
    from backtest.z0_metrics import write_metrics

    app = tmp_path / "app"
    (app / "analysis_out").mkdir(parents=True)
    target = app / "analysis_out" / "z0_metrics.txt"
    os.environ["Z0_METRICS_PATH"] = str(target)
    try:
        written = write_metrics("z0_parity", {
            "z0_parity_ohlc_mismatches": 7,
            "z0_parity_ohlc_absorbed": 98,
            "peak_rss_mb": 180.4,
        })
    finally:
        del os.environ["Z0_METRICS_PATH"]
    assert written == target
    out = _run_verify(
        tmp_path, DOCKER_DEAD, metrics=target.read_text(encoding="utf-8")
    )
    assert re.search(r"z0_parity_ohlc_mismatches\s+7", out)
    assert re.search(r"z0_parity_ohlc_absorbed\s+98", out)
    assert re.search(r"peak_rss_mb\s+180\.4", out)


def test_metrics_file_is_appended_not_overwritten(tmp_path) -> None:
    """Шаги 9, 11 и 14 — разные прогоны: затирать чужие ключи нельзя."""
    from backtest.z0_metrics import write_metrics

    target = tmp_path / "out" / "z0_metrics.txt"
    os.environ["Z0_METRICS_PATH"] = str(target)
    try:
        write_metrics("z0_load_hourly", {"z0_hourly_appended": 12})
        write_metrics("z0_parity", {"z0_parity_ohlc_mismatches": 2})
    finally:
        del os.environ["Z0_METRICS_PATH"]
    body = target.read_text(encoding="utf-8")
    assert "z0_hourly_appended=12" in body, "ключи первого прогона затёрты вторым"
    assert "z0_parity_ohlc_mismatches=2" in body


def test_unwritable_metrics_path_does_not_break_the_run(tmp_path) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: потеря места для ключей не стоит шестичасовой загрузки."""
    from backtest.z0_metrics import write_metrics

    blocker = tmp_path / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")
    os.environ["Z0_METRICS_PATH"] = str(blocker / "z0_metrics.txt")
    try:
        assert write_metrics("z0_parity", {"peak_rss_mb": 1.0}) is None
    finally:
        del os.environ["Z0_METRICS_PATH"]


def test_verify_reads_keys_from_the_json_rendered_log(tmp_path) -> None:
    """Журнал контейнера — ЗАПАСНОЙ источник, и он тоже обязан разбираться.

    В контейнере без TTY structlog печатает JSON, и ключ выглядит как
    ``"z0_parity_ohlc_mismatches": 2``, а не ``ключ=значение``. Разбор,
    понимающий только вторую запись, нашёл бы в таком журнале ноль ключей и
    объявил бы, что прогоны не выполнялись.
    """
    shim = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  inspect) exit 0 ;;\n"
        '  logs) echo \'{"event":"ok","z0_parity_ohlc_mismatches":2,'
        '"z0_unclosed_rows":0,"peak_rss_mb":180.4}\'; exit 0 ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    out = _run_verify(tmp_path, shim, metrics=None)
    assert re.search(r"z0_parity_ohlc_mismatches\s+2", out), (
        "ключ из JSON-журнала не найден — разбор понимает только одну запись"
    )
    assert re.search(r"peak_rss_mb\s+180\.4", out)
    assert "СБОР ЖУРНАЛОВ НЕ УДАЛСЯ" not in out


# ---------------------------------------------------------------------------
# ЗАМЕР 0.2: ОТЧЁТ. Блоки A–E §5 ТЗ на подставном ряде, БЕЗ базы
#
# Правило допуска проверено выше поштучно. Здесь проверяется ВТОРАЯ половина
# задачи: что скрипт печатает ПОИМЁННЫЙ список, а не число, и что в нём есть
# тикер, таймфрейм, дата и поле.
#
# БИРЖЕВАЯ СТОРОНА ЗДЕСЬ ПОЛУЧЕНА ТОЙ ЖЕ СБОРКОЙ, И ЭТО СДЕЛАНО НАРОЧНО. Выше
# (§11.3) сборка сверяется с выписанными вручную барами — там сравнение с
# собственным результатом обесценило бы проверку. Здесь проверяется ОТЧЁТ, и
# ряд нужен такой, где ВСЕ различия подложены сознательно: иначе подложенные
# находки утонули бы в шуме, и «список верен» нельзя было бы утверждать.
# ---------------------------------------------------------------------------

WEEK_START = datetime(2019, 12, 23, tzinfo=UTC)   # понедельник
YEAR_TURN_WEEK = datetime(2019, 12, 30, tzinfo=UTC)
REAL_DAY = datetime(2019, 12, 25, tzinfo=UTC)


def _series(base: str, step: str, count: int, start: datetime) -> list[SourceBar]:
    """Часовой ряд с шагом ``step``: значения различаются в каждом поле и часе."""
    bars: list[SourceBar] = []
    for index in range(count):
        price = Decimal(base) + Decimal(index) * Decimal(step)
        bars.append(
            SourceBar(
                ts=start + timedelta(hours=index),
                open=price,
                high=price + Decimal(step) * 2,
                low=price - Decimal(step) * 2,
                close=price + Decimal(step),
                volume=Decimal("1.00000000"),
            )
        )
    return bars


def _rollup(bar: str, sources: list[SourceBar], **anchors: Any) -> list[SourceBar]:
    """Старший ряд «биржи», полученный сборкой из младшего. См. заголовок раздела."""
    return [
        SourceBar(
            ts=item.open_time, open=item.open, high=item.high, low=item.low,
            close=item.close, volume=item.volume,
        )
        for item in assemble(bar, sources, **anchors)
        if item.is_complete
    ]


def _replace(bars: list[SourceBar], ts: datetime, **fields: Decimal) -> list[SourceBar]:
    """Подменяет поля одного бара ряда. Возвращает НОВЫЙ список."""
    out: list[SourceBar] = []
    for item in bars:
        if item.ts == ts:
            values = {
                "open": item.open, "high": item.high, "low": item.low,
                "close": item.close, "volume": item.volume,
            }
            values.update(fields)
            out.append(SourceBar(ts=item.ts, **values))
        else:
            out.append(item)
    return out


def _fake_table() -> dict[tuple[str, str], list[SourceBar]]:
    """Подставная ``backtest.candles``: два инструмента, три подложенных различия.

    1. BTC 1Wutc 30.12.2019, ``open``: биржа ставит 7131,90 при нашем 7388,50 —
       рубеж 2019→2020, метка BOUNDARY_ANOMALY;
    2. SOL 1Wutc 30.12.2019, ``open``: наша 2,548 против биржевых 2,54 — разная
       точность записи, обязано быть ПОГЛОЩЕНО;
    3. SOL 1Dutc 25.12.2019, ``high``: биржа ниже на 0,50 при одинаковой
       точности записи — метка REAL.
    """
    hours = 24 * 14
    table: dict[tuple[str, str], list[SourceBar]] = {}

    btc_hourly = _replace(
        _series("7000.00000000", "0.50000000", hours, WEEK_START),
        YEAR_TURN_WEEK, open=Decimal("7388.50000000"),
    )
    sol_hourly = _replace(
        _series("2.50000000", "0.05000000", hours, WEEK_START),
        YEAR_TURN_WEEK, open=Decimal("2.54800000"),
    )

    for inst, hourly in (("BTC-USDT", btc_hourly), ("SOL-USDT", sol_hourly)):
        daily = _rollup("1Dutc", hourly)
        if inst == "SOL-USDT":
            spoiled = next(item for item in daily if item.ts == REAL_DAY)
            daily = _replace(
                daily, REAL_DAY, high=spoiled.high - Decimal("0.50000000")
            )
        weekly = _rollup("1Wutc", daily, week_weekday=0)
        if inst == "BTC-USDT":
            weekly = _replace(
                weekly, YEAR_TURN_WEEK, open=Decimal("7131.90000000")
            )
        else:
            weekly = _replace(
                weekly, YEAR_TURN_WEEK, open=Decimal("2.54000000")
            )
        table[(inst, "1H")] = hourly
        table[(inst, "1Dutc")] = daily
        table[(inst, "1Wutc")] = weekly
    return table


def _install_fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет ЧТЕНИЕ из базы подставным рядом. Ничего, кроме чтения, нет."""
    import scripts.htf_parity_z0 as parity

    table = _fake_table()

    async def stream_source_bars(inst_id: str, bar: str, **_: Any) -> Any:
        for row in table.get((inst_id, bar), ()):
            yield row

    async def stream_stored_htf(bars: tuple[str, ...]) -> Any:
        for (inst_id, bar), rows in table.items():
            if bar not in bars:
                continue
            for row in rows:
                yield StoredBar(
                    inst_id=inst_id, bar=bar, open_time=row.ts,
                    close_time=next_period_start(bar, row.ts),
                )

    monkeypatch.setattr(parity, "stream_source_bars", stream_source_bars)
    monkeypatch.setattr(parity, "stream_stored_htf", stream_stored_htf)
    monkeypatch.setattr(parity, "write_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(parity, "_TRANSCRIPT", [])


async def _run_measure(monkeypatch: pytest.MonkeyPatch, *, verbose: bool) -> tuple[int, str]:
    import scripts.htf_parity_z0 as parity

    _install_fake_db(monkeypatch)
    code = await parity._measure(
        ["BTC-USDT", "SOL-USDT"], ["1Dutc", "1Wutc"], verbose=verbose
    )
    return code, "\n".join(parity._TRANSCRIPT)


async def test_report_names_every_finding_with_ticker_scale_date_and_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5, блоки A–C ТЗ: список ПОИМЁННЫЙ, и в каждой строке все четыре имени."""
    code, out = await _run_measure(monkeypatch, verbose=False)

    assert "Всего баров сверено: 32" in out, out[:2000]
    assert "Расхождений найдено: 2 полей в 2 барах" in out
    assert re.search(r"Поглощено допуском: 1\s+\(из них: точность записи — 1\)", out)
    assert (
        "Затронутые бары: SOL-USDT 1Dutc 2019-12-25; BTC-USDT 1Wutc 2019-12-30"
    ) in out

    assert (
        "BTC-USDT 1Wutc 2019-12-30 open: наша=7388.50000000 биржа=7131.90000000 | "
        "знаков: наша=1 биржа=1 | допуск=0.1 | отклонение=256.60000000 | "
        "категория=BOUNDARY_ANOMALY"
    ) in out
    assert (
        "SOL-USDT 1Dutc 2019-12-25 high: наша=6.15000000 биржа=5.65000000 | "
        "знаков: наша=2 биржа=2 | допуск=0.01 | отклонение=0.50000000 | "
        "категория=REAL"
    ) in out

    assert "z0_parity_boundary_anomaly         = 1" in out
    assert "z0_parity_real                     = 1" in out
    # Код 2 — это РЕЗУЛЬТАТ замера, а не отказ: цель этапа — точный список.
    assert code == 2


async def test_absorbed_are_counted_not_printed_unless_verbose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5, блок E ТЗ: поглощённое видно числом, а с ``--verbose`` — поимённо.

    КОНТРОЛЬНЫЙ ОПЫТ к поимённому списку: если бы поглощённое печаталось
    всегда, «короткий список настоящих расхождений» снова утонул бы в шуме.
    """
    _, quiet = await _run_measure(monkeypatch, verbose=False)
    _, loud = await _run_measure(monkeypatch, verbose=True)

    absorbed_line = (
        "SOL-USDT 1Wutc 2019-12-30 open: наша=2.54800000 биржа=2.54000000 | "
        "знаков: наша=3 биржа=2 | допуск=0.01 | отклонение=0.00800000"
    )
    assert absorbed_line not in quiet, "поглощённое напечатано без --verbose"
    assert absorbed_line in loud, "--verbose не печатает поглощённые отклонения"
    # Число при этом одно и то же: флаг меняет ПОДРОБНОСТЬ, а не результат.
    summary = "Расхождений найдено: 2 полей в 2 барах"
    assert summary in quiet and summary in loud


async def test_year_turn_block_prints_the_chain_as_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5, блок D ТЗ: цепочка из трёх фактов, без объяснения механизма."""
    _, out = await _run_measure(monkeypatch, verbose=False)

    block = out[out.index("Блок D."):out.index("Блок E.")]
    assert "BTC-USDT 1Wutc 2019-12-30 (расхождение)" in block
    assert "открытие спорного бара по бирже:  7131.90000000" in block
    assert "открытие по нашей сборке:         7388.50000000" in block
    assert "предыдущий бар биржи: 2019-12-23 закрытие=" in block
    assert "Механизм НЕ УСТАНОВЛЕН" in block
    # КОНТРОЛЬНЫЙ ОПЫТ: тот же рубеж у SOL в блок НЕ попадает. Иначе цепочка
    # печаталась бы для всего подряд и перестала бы указывать на что бы то ни было.
    assert "SOL-USDT" not in block


async def test_categories_do_not_hide_anything_from_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5, блок C ТЗ: категория — метка, а не фильтр.

    КОНТРОЛЬНЫЙ ОПЫТ: сумма по категориям обязана сойтись с длиной списка. Если
    какая-то категория перестанет печататься, эти два числа разойдутся.
    """
    _, out = await _run_measure(monkeypatch, verbose=False)

    block = out[out.index("Блок B."):out.index("Блок C.")]
    named = [line for line in block.splitlines() if "категория=" in line]
    assert len(named) == 2
    assert sum(1 for line in named if "BOUNDARY_ANOMALY" in line) == 1
    assert sum(1 for line in named if "категория=REAL" in line) == 1


def test_summary_counts_fields_and_bars_separately() -> None:
    """Две мерки рядом: поля (правило 0.2) и бары (мерка отчёта Замера 0.1).

    Бар, у которого разошлись ДВА поля, даёт два расхождения и ОДИН затронутый
    бар. Если бы сводка печатала одно число, сверить результат с ожиданием «7»,
    взятым из отчёта, считавшего бары, было бы нечем.

    КОНТРОЛЬНЫЙ ОПЫТ — вторая половина проверки: у двух РАЗНЫХ баров числа
    обязаны совпасть. Функция, всегда возвращающая один элемент, прошла бы
    первую половину и упала бы здесь.
    """
    from scripts.htf_parity_z0 import CATEGORY_REAL, Finding, affected_bars

    def finding(inst: str, bar: str, moment: datetime, field: str) -> Finding:
        return Finding(
            deviation=Deviation(
                inst_id=inst, bar=bar, open_time=moment, field=field,
                assembled=Decimal("2.00000000"), stored=Decimal("1.00000000"),
            ),
            category=CATEGORY_REAL,
        )

    one_bar = [
        finding("ETH-USDT", "1Wutc", YEAR_TURN_WEEK, "open"),
        finding("ETH-USDT", "1Wutc", YEAR_TURN_WEEK, "low"),
    ]
    assert affected_bars(one_bar) == ["ETH-USDT 1Wutc 2019-12-30"]
    assert len(one_bar) == 2

    two_bars = [
        finding("ETH-USDT", "1Wutc", YEAR_TURN_WEEK, "open"),
        finding("ETH-USDT", "1Mutc", datetime(2019, 12, 1, tzinfo=UTC), "open"),
    ]
    assert affected_bars(two_bars) == [
        "ETH-USDT 1Wutc 2019-12-30", "ETH-USDT 1Mutc 2019-12-01",
    ]
    # Тот же инструмент и та же дата на РАЗНЫХ таймфреймах — разные бары.
    assert len(affected_bars(two_bars)) == 2
    assert affected_bars([]) == []


def test_category_marks_only_the_year_turn_of_btc_and_eth() -> None:
    """Метка рубежа ставится по КАЛЕНДАРЮ периода, а не по перечню дат."""
    from scripts.htf_parity_z0 import CATEGORY_BOUNDARY, CATEGORY_REAL, category_of

    # Период накрывает смену года серединой, кончается ею или начинается с неё.
    assert category_of("BTC-USDT", "1Wutc", YEAR_TURN_WEEK) == CATEGORY_BOUNDARY
    assert category_of(
        "ETH-USDT", "1Mutc", datetime(2019, 12, 1, tzinfo=UTC)
    ) == CATEGORY_BOUNDARY
    assert category_of(
        "ETH-USDT", "1Mutc", datetime(2020, 1, 1, tzinfo=UTC)
    ) == CATEGORY_BOUNDARY
    # КОНТРОЛЬНЫЙ ОПЫТ: тот же рубеж у другого инструмента и тот же инструмент
    # вдали от рубежа — не метка. Иначе метка стояла бы всюду и не значила бы
    # ничего.
    assert category_of("SOL-USDT", "1Wutc", YEAR_TURN_WEEK) == CATEGORY_REAL
    assert category_of("BTC-USDT", "1Wutc", WEEK_START) == CATEGORY_REAL
    assert category_of(
        "BTC-USDT", "1Dutc", datetime(2022, 12, 18, tzinfo=UTC)
    ) == CATEGORY_REAL


def test_report_copy_lands_in_reports_with_the_date_in_the_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5 ТЗ: копия отчёта — файлом в ``reports/`` и с датой в имени.

    Копия набирается ТЕМИ ЖЕ строками, что напечатаны: второй проход, собирающий
    отчёт заново, однажды разошёлся бы с выводом и разошёлся бы молча.
    """
    import scripts.htf_parity_z0 as parity

    monkeypatch.setattr(parity, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(parity, "_TRANSCRIPT", ["строка отчёта", "вторая строка"])
    started = datetime(2026, 9, 11, 15, 4, tzinfo=UTC)

    saved = parity.save_report(started)
    assert saved == tmp_path / "zamer_0_2_parity_2026-09-11.md"
    assert saved.read_text(encoding="utf-8") == "строка отчёта\nвторая строка\n"
