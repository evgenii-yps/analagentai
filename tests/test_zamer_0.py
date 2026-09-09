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
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from backtest.htf import (
    HTF_BARS,
    OHLC_TOLERANCE,
    Deviation,
    HtfError,
    SourceBar,
    StoredBar,
    assemble,
    assert_bar_alignment,
    measure_boundaries,
    month_anchor_day,
    next_period_start,
    parse_htf_candles,
    unclosed_rows,
    verify_period_chain,
    week_anchor_weekday,
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
            if deviation.absolute > OHLC_TOLERANCE:
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
        assert deviation.absolute <= OHLC_TOLERANCE


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
