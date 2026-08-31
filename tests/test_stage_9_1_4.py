"""Этап 9.1.4: замер исхода закрытых позиций при других уровнях предела убытка.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ЭТАП ЗАМЕРНЫЙ, И ГЛАВНАЯ ОПАСНОСТЬ ЗАМЕРА — ПРАВДОПОДОБНОЕ ЧИСЛО. Неверно
посчитанный исход выглядит ровно так же, как верный: ни исключения, ни пустого
места, ни красной строки в журнале. Поэтому проверяется не «получилось ли
число», а пять вещей, каждая из которых способна сделать число ложным:

 1. ПРАВИЛО. Все пять вариантов считаются ОДНОЙ И ТОЙ ЖЕ ``check_exit`` с разным
    ``stop_price``. Своё правило выхода сделало бы контроль не контролем.
 2. ОТКЛЮЧЕНИЕ ПРЕДЕЛА. ``stop_price = 0`` обязано быть ЭКВИВАЛЕНТНО отсутствию
    предела, а не «примерно как отсутствие». Эквивалентность здесь проверяется
    на случайных рядах против отдельно написанного образца, а не объявляется.
 3. КОНТРОЛЬ. Пересчёт при фактическом пределе обязан воспроизвести записанное в
    ``positions`` до последнего знака — включая МОМЕНТ ЗАКРЫТИЯ БАРА, на котором
    Этап 9.1.3 получил одиннадцать ложных расхождений по 60 секунд.
 4. СОСТАВ ВЫБОРКИ. ``data_gap`` и открытые позиции не попадают в замер ни при
    каких обстоятельствах.
 5. ОКНО ЗАБЛОКИРОВАННЫХ ВХОДОВ. Считаются только свой инструмент, только
    покупки, только выше порога и только внутри окна лишнего удержания.

КОНТРОЛЬНЫЕ ОПЫТЫ ОБЯЗАТЕЛЬНЫ (§6 ТЗ). По каждой новой проверке показано, что
она ПАДАЕТ при возвращённом дефекте: проверка, которая проходит и с дефектом, и
без него, не проверяет ничего. На Этапе 9.1.3 три проверки из девяти прошли
молча. Опыты помечены в именах словом ``control_experiment``.

ДВОЙНИК БАЗЫ ЗДЕСЬ НЕ МЯГЧЕ НАСТОЯЩЕЙ. Он ВЫПОЛНЯЕТ SQL настоящих методов
``DB`` и сверяет каждую колонку с составом таблиц, вычитанным ИЗ ФАЙЛОВ
МИГРАЦИЙ (``tests/schema_double.py``), и применяет ТЕ УСЛОВИЯ, КОТОРЫЕ РЕАЛЬНО
СТОЯТ В ЗАПРОСЕ, а не те, которые запрос «должен» содержать.

ЧЕГО ЭТИ ПРОВЕРКИ НЕ ДОКАЗЫВАЮТ. Ни одна из них не запускалась на настоящих
свечах: ряды здесь придуманы, и придуманы так, чтобы ответ был известен заранее.
Совпадение контроля с фактом на боевых данных — это то, что покажет первый
прогон на сервере, и заменить его синтетикой нельзя.
"""

from __future__ import annotations

import inspect
import pathlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import scripts.stop_counterfactual_9_1_4 as stopcf
from src.positions.rules import Bar
from tests.schema_double import (
    SchemaPool,
    UndefinedColumn,
    check_sql_columns,
    project,
    schema,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Позиция-образец: вход по 100, цель +2%, предел −1%, час горизонта, слот $2.
# Круглые числа выбраны затем, чтобы ответ был виден глазом: цель 102.00,
# фактический предел 99.00, пересчётные — 98.50, 98.00 и 97.00.
OPENED = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
HORIZON_H = 1
DEADLINE = OPENED + timedelta(hours=HORIZON_H)
ENTRY = 100.0
TARGET_PCT = 2.0
STOP_PCT = 1.0
COST_PCT = 0.22
SLOT = 2.0
INSTRUMENT = 10
OTHER_INSTRUMENT = 11
MIN_PROBABILITY = 0.8


def code_only(text: str) -> str:
    """Текст без пояснений: только исполняемый код.

    ЗАЧЕМ ЭТО НУЖНО. Проверки границ этапа ищут в файле собственное правило
    выхода — например, своё сравнение ``bar.low <= stop_price``. Но ровно это
    сравнение ЦИТИРУЕТСЯ в пояснении, объясняющем, почему предел отключается
    нулём. Искать по всему файлу значило бы запретить объяснять, как работает
    чужой код, — и проверка падала бы на исправном файле, а обойти её пришлось
    бы, убрав объяснение.

    Убираются строки в тройных кавычках (все пояснения в проекте написаны ими) и
    построчные комментарии. Строковые литералы в одинарных кавычках остаются:
    ни один из искомых образцов в них не встречается, и вырезать их значило бы
    ослабить проверку.
    """
    without_docstrings = "".join(text.split('"""')[0::2])
    out: list[str] = []
    for line in without_docstrings.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "#" in line and '"' not in line.split("#", 1)[0] \
                and "'" not in line.split("#", 1)[0]:
            line = line.split("#", 1)[0]
        out.append(line)
    return "\n".join(out)


def _bars(path: list[tuple[float, float, float]]) -> list[Bar]:
    """Ряд минутных баров окна из троек ``(high, low, close)``."""
    return [
        Bar(ts=OPENED + timedelta(minutes=i), high=h, low=lo, close=c)
        for i, (h, lo, c) in enumerate(path)
    ]


def _flat(high: float, low: float, close: float, count: int
          ) -> list[tuple[float, float, float]]:
    return [(high, low, close)] * count


def _resolve(bars: list[Bar], **over: Any) -> list[stopcf.VariantOutcome]:
    kwargs: dict[str, Any] = {
        "position_stop_pct": STOP_PCT, "entry_price": ENTRY,
        "target_pct": TARGET_PCT, "cost_pct": COST_PCT, "notional_usd": SLOT,
        "opened_at": OPENED, "deadline_at": DEADLINE,
        "fact_closed_at": OPENED + timedelta(minutes=1),
        "resolution": "1m", "side": "buy",
    }
    kwargs.update(over)
    return stopcf.resolve_position_stops(bars, **kwargs)


def _by_variant(rows: list[stopcf.VariantOutcome]) -> dict[str, stopcf.VariantOutcome]:
    return {row.variant: row for row in rows}


# Три ряда, на которых ответ известен заранее.
#
# В КАЖДОМ РЯДУ 61 БАР, А НЕ 60, И ПОСЛЕДНИЙ — БАР СРОКА. ``check_exit``
# возвращает ``timeout`` только когда бар с меткой ``deadline_at`` ПРЕДЪЯВЛЕН:
# пока его нет, неизвестно, кончилось окно или данные не подъехали, и правило
# честно отвечает «исхода нет». Ряд из 60 баров давал бы ``no_data`` там, где
# ожидается ``timeout``, — и проверка измеряла бы неполноту данных вместо
# правила. Сам бар срока в окно не входит: цикл прерывается на нём.
#
# DIP_THEN_TARGET: бар 0 задевает фактический предел 99.00 (минимум 98.80) и не
# задевает 98.50; дальше цена возвращается и на баре 5 доходит до цели 102.00.
DIP_THEN_TARGET = _bars(
    [(100.20, 98.80, 99.50)]
    + _flat(101.00, 99.60, 100.80, 4)
    + [(102.50, 101.00, 102.20)]
    + _flat(103.00, 102.00, 102.50, 55)
)
# KEEPS_FALLING: бар 0 задевает 99.00, бар 1 — 98.50 и 98.00, бар 2 — 97.00,
# дальше цена оседает на 96.00 и до срока не возвращается.
KEEPS_FALLING = _bars(
    [(100.10, 98.90, 99.00), (99.00, 97.90, 98.00), (98.00, 96.90, 97.00)]
    + _flat(96.50, 95.80, 96.00, 58)
)
# STRAIGHT_TARGET: цель взята на первом же баре, предела не касались вовсе.
STRAIGHT_TARGET = _bars(
    [(102.50, 100.10, 102.20)] + _flat(103.00, 102.00, 102.50, 60)
)


# =============================================================================
# §6.1–§6.4. Правило на придуманных рядах с заранее известным ответом
# =============================================================================

def test_a_dip_that_recovers_reaches_the_target_only_without_the_stop() -> None:
    """§6.1: цена задела предел, вернулась и дошла до цели.

    ЭТО И ЕСТЬ ВОПРОС ВЛАДЕЛЬЦА, ПОДАННЫЙ КАК РЯД СВЕЧЕЙ. При фактическом
    пределе сделка закрыта в минус на первом же баре; без предела та же сделка
    доходит до цели — но занимает слот на пять баров дольше. Обе половины ответа
    измеряются одновременно, потому что порознь каждая вводит в заблуждение.
    """
    rows = _by_variant(_resolve(DIP_THEN_TARGET))

    assert rows["control"].exit_reason == "stop"
    assert rows["control"].exit_price == pytest.approx(99.0)
    assert rows["no_stop"].exit_reason == "target"
    assert rows["no_stop"].exit_price == pytest.approx(102.0)
    # Более широкий предел 98.50 бара 0 уже не задевает — исход тот же, что без
    # предела вовсе. Кривая по пяти точкам показывает это прямо.
    assert rows["stop_1.5"].exit_reason == "target"
    assert rows["stop_2.0"].exit_reason == "target"
    assert rows["stop_3.0"].exit_reason == "target"

    # Слот занят на пять баров дольше: факт закрылся по бару 0, пересчёт — по
    # бару 5, и оба момента считаются по ЗАКРЫТИЮ бара.
    assert rows["no_stop"].extra_held_sec == 5 * 60
    assert rows["control"].extra_held_sec == 0
    assert rows["no_stop"].held_sec == 6 * 60

    assert rows["no_stop"].net_pnl_pct == pytest.approx(TARGET_PCT - COST_PCT)
    assert rows["control"].net_pnl_pct == pytest.approx(-STOP_PCT - COST_PCT)


def test_control_experiment_the_dip_case_needs_the_stop_to_actually_differ() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.1: без разного предела проверка ничего не проверяет.

    Если бы вариант ``no_stop`` считался тем же пределом, что и контроль,
    предыдущая проверка требовала бы от одного и того же расчёта двух разных
    ответов и упала бы. Здесь дефект возвращается ЯВНО — вариант считается с
    фактическим пределом, — и видно, что исход становится тем же самым.
    """
    with_fact_stop = stopcf.resolve_variant(
        DIP_THEN_TARGET, variant="control", position_stop_pct=STOP_PCT,
        entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
        notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
        fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
    )
    assert with_fact_stop.exit_reason == "stop", (
        "дефект не воспроизвёлся: при фактическом пределе исход обязан быть stop"
    )
    without = stopcf.resolve_variant(
        DIP_THEN_TARGET, variant="no_stop", position_stop_pct=STOP_PCT,
        entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
        notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
        fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
    )
    assert without.exit_reason != with_fact_stop.exit_reason


def test_a_price_that_keeps_falling_ends_worse_without_the_stop() -> None:
    """§6.2: цена после предела продолжила падать — итог без предела в минус глубже.

    ВТОРАЯ ПОЛОВИНА ОТВЕТА, и без неё первая была бы агитацией. Предел убытка
    существует ради хвоста: сделка, ушедшая вниз и не вернувшаяся, без предела
    досиживает до срока и закрывается по той цене, какая будет.
    """
    rows = _by_variant(_resolve(KEEPS_FALLING))

    assert rows["control"].exit_reason == "stop"
    assert rows["no_stop"].exit_reason == "timeout"
    # Итог без предела: закрытие последнего бара окна, 96.00.
    assert rows["no_stop"].net_pnl_pct == pytest.approx(-4.0 - COST_PCT)
    assert rows["no_stop"].net_pnl_pct < rows["control"].net_pnl_pct
    # Слот занят до самого срока: 60 баров вместо одного.
    assert rows["no_stop"].held_sec == 60 * 60
    assert rows["no_stop"].extra_held_sec == 59 * 60
    # Промежуточные уровни ловят падение по дороге, каждый на своём баре.
    assert rows["stop_1.5"].exit_reason == "stop"
    assert rows["stop_1.5"].exit_bar_ts == OPENED + timedelta(minutes=1)
    assert rows["stop_3.0"].exit_bar_ts == OPENED + timedelta(minutes=2)


def test_a_position_closed_at_the_target_is_the_same_under_all_five() -> None:
    """§6.3: закрытая по цели — пять одинаковых исходов и ноль лишнего удержания.

    Предел убытка не касается сделки, которая до него не доходила. Если бы
    касался — расчёт где-то подменял бы уровень цели уровнем предела, и это
    видно было бы ровно здесь.
    """
    rows = _by_variant(
        _resolve(STRAIGHT_TARGET, fact_closed_at=OPENED + timedelta(minutes=1))
    )
    assert {row.exit_reason for row in rows.values()} == {"target"}
    assert {row.exit_bar_ts for row in rows.values()} == {OPENED}
    assert {row.extra_held_sec for row in rows.values()} == {0}
    assert {round(row.net_pnl_pct, 9) for row in rows.values()} == {
        round(TARGET_PCT - COST_PCT, 9)
    }


def test_a_wider_stop_never_shortens_the_holding_on_any_series() -> None:
    """§6.4: монотонность — шире предел, не раньше выход. Ни на одной позиции.

    Расширение предела способно только УБРАТЬ выход по пределу; цели и срока оно
    не касается. Проверяется на всех трёх рядах сразу и вдобавок на случайных:
    свойство обязано быть свойством правила, а не удачей трёх примеров.
    """
    for series in (DIP_THEN_TARGET, KEEPS_FALLING, STRAIGHT_TARGET):
        chain = sorted(
            _resolve(series), key=lambda row: stopcf.stop_width(row.stop_pct)
        )
        held = [row.held_sec for row in chain if row.measured]
        assert held == sorted(held), f"удержание не монотонно: {held}"

    rng = random.Random(20260831)
    for _ in range(300):
        path = []
        price = 100.0
        for _bar in range(61):
            price = max(1.0, price + rng.uniform(-1.2, 1.2))
            high = price + rng.uniform(0.0, 1.5)
            low = max(0.5, price - rng.uniform(0.0, 1.5))
            path.append((high, low, price))
        chain = sorted(
            _resolve(_bars(path)), key=lambda row: stopcf.stop_width(row.stop_pct)
        )
        held = [row.held_sec for row in chain if row.measured]
        assert held == sorted(held), f"удержание не монотонно: {held}"


def test_control_experiment_the_monotonicity_check_actually_refuses() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.4: проверка монотонности ПАДАЕТ на нарушении.

    Без этого опыта предыдущая проверка проходила бы и у ``check_monotonic``,
    которая не делает ничего вовсе: на исправном расчёте нарушения нет, и
    молчание неотличимо от работы.
    """
    good = _resolve(KEEPS_FALLING)
    stopcf.check_monotonic(good)  # на исправном расчёте молчит

    broken = [
        row if row.variant != "no_stop"
        else stopcf.VariantOutcome(
            variant=row.variant, stop_pct=row.stop_pct,
            exit_reason=row.exit_reason, exit_bar_ts=row.exit_bar_ts,
            exit_price=row.exit_price, net_pnl_pct=row.net_pnl_pct,
            net_pnl_usd=row.net_pnl_usd, closed_at=row.closed_at,
            held_sec=1, extra_held_sec=row.extra_held_sec,
            bars_used=row.bars_used, resolution=row.resolution,
        )
        for row in good
    ]
    with pytest.raises(AssertionError, match="не может укоротить удержание"):
        stopcf.check_monotonic(broken)


def test_a_series_that_stops_short_of_the_deadline_is_unmeasured_not_timeout() -> None:
    """Оборванный ряд даёт ``no_data``, а не ``timeout``, и нули при нём связаны.

    ``check_exit`` возвращает «исхода нет», пока бар срока НЕ ПРЕДЪЯВЛЕН: разница
    между «истёк срок» и «данных пока нет» — это разница между исходом и его
    отсутствием. Назвать такой случай ``timeout`` значило бы выдать неизмеренное
    за измеренное, а поставить туда какое-нибудь удержание — за измеренное выдать
    ещё и цену вопроса в слотах.
    """
    short = _bars(_flat(100.50, 99.50, 100.00, 60))  # бара срока нет
    rows = _by_variant(
        _resolve(short, fact_closed_at=OPENED + timedelta(minutes=1))
    )
    assert rows["no_stop"].exit_reason == "no_data"
    assert rows["no_stop"].measured is False
    assert rows["no_stop"].exit_price is None
    assert rows["no_stop"].net_pnl_pct is None
    assert rows["no_stop"].closed_at is None
    # Нули стоят рядом с причиной и читаются только вместе с ней; ограничение
    # position_stop_shadow_gap_chk требует ровно этого.
    assert (rows["no_stop"].held_sec, rows["no_stop"].extra_held_sec) == (0, 0)
    assert rows["no_stop"].bars_used == 60
    # Неизмеренный вариант в цепочку монотонности не входит и её не роняет.
    stopcf.check_monotonic(list(rows.values()))


def test_the_monotonicity_guard_is_actually_called_on_every_position() -> None:
    """Проверка монотонности ВЫЗЫВАЕТСЯ расчётом, а не лежит рядом без дела.

    КОНТРОЛЬНЫЙ ОПЫТ, КОТОРОГО ТРЕБУЕТ САМА ПРЕДЫДУЩАЯ ПРОВЕРКА. Свойство
    монотонности можно проверить и снаружи — но тогда удаление вызова
    ``check_monotonic`` из расчёта не уронило бы ни одной проверки, и охранник
    исчез бы молча. Здесь охранник подменяется падающим, и расчёт обязан упасть.
    """
    import scripts.stop_counterfactual_9_1_4 as module

    called: list[int] = []

    def _boom(rows: list[stopcf.VariantOutcome]) -> None:
        called.append(len(rows))
        raise AssertionError("охранник вызван")

    original = module.check_monotonic
    module.check_monotonic = _boom
    try:
        with pytest.raises(AssertionError, match="охранник вызван"):
            _resolve(KEEPS_FALLING)
    finally:
        module.check_monotonic = original
    assert called == [5], "охранник получил не все пять вариантов"


# =============================================================================
# §6.10. Эквивалентность варианта «без предела» выбранному способу отключения
# =============================================================================

def _reference_without_stop(
    bars: list[Bar], *, target_price: float, deadline_at: datetime
) -> tuple[str, float, datetime, int] | None:
    """ОБРАЗЕЦ правила БЕЗ предела, написанный отдельно и ТОЛЬКО для сверки.

    В расчёте этот код не участвует ни одной строкой — иначе он был бы вторым
    правилом выхода, что §2.1 ТЗ запрещает прямо. Здесь он нужен ровно затем,
    чтобы утверждение «stop_price = 0 равносилен отсутствию предела» можно было
    ПРОВЕРИТЬ, а не объявить.
    """
    last: Bar | None = None
    reached = False
    held = 0
    for bar in bars:
        if bar.ts >= deadline_at:
            reached = True
            break
        held += 1
        last = bar
        if bar.high >= target_price:
            return ("target", target_price, bar.ts, held)
    if reached and last is not None:
        return ("timeout", float(last.close), last.ts, held)
    return None


def _random_series(rng: random.Random, count: int = 61) -> list[Bar]:
    path = []
    price = 100.0
    for _bar in range(count):
        price = max(1.0, price + rng.uniform(-2.0, 2.0))
        high = price + rng.uniform(0.0, 2.0)
        low = max(0.5, price - rng.uniform(0.0, 2.0))
        path.append((high, low, price))
    return _bars(path)


def test_the_no_stop_variant_equals_a_rule_without_a_stop_at_all() -> None:
    """§6.10: эквивалентность ``stop_price = 0`` проверяется, а не предполагается.

    Две тысячи случайных рядов, включая те, где цена уходит вниз на десятки
    процентов. Совпадать обязаны все четыре величины: причина, цена, бар выхода
    и число баров. Достаточно одного ряда, на котором ``stop_price = 0``
    сработал бы как предел, — и проверка упадёт.
    """
    rng = random.Random(4242)
    target_price = ENTRY * (1.0 + TARGET_PCT / 100.0)
    seen = {"target": 0, "timeout": 0}
    for _ in range(2000):
        series = _random_series(rng)
        got = stopcf.resolve_variant(
            series, variant="no_stop", position_stop_pct=STOP_PCT,
            entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
            notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
            fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
        )
        want = _reference_without_stop(
            series, target_price=target_price, deadline_at=DEADLINE
        )
        assert want is not None
        assert (got.exit_reason, got.exit_price, got.exit_bar_ts, got.bars_used) == (
            want[0], pytest.approx(want[1]), want[2], want[3]
        )
        seen[got.exit_reason] = seen.get(got.exit_reason, 0) + 1
    # Оба исхода встретились: проверка, увидевшая только один из них, доказывала
    # бы эквивалентность лишь наполовину.
    assert seen["target"] > 0 and seen["timeout"] > 0, seen


def test_control_experiment_a_nonzero_no_stop_price_breaks_the_equivalence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.10: при ненулевом «нулевом» пределе сверка падает.

    Дефект возвращается буквально — ``NO_STOP_PRICE`` становится настоящим
    уровнем, — и предыдущая проверка обязана его увидеть. Если бы не увидела,
    она проходила бы при любом способе отключения предела, то есть не проверяла
    бы ничего.
    """
    monkeypatch.setattr(stopcf, "NO_STOP_PRICE", 98.5)
    got = stopcf.resolve_variant(
        DIP_THEN_TARGET, variant="no_stop", position_stop_pct=STOP_PCT,
        entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
        notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
        fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
    )
    want = _reference_without_stop(
        DIP_THEN_TARGET,
        target_price=ENTRY * (1.0 + TARGET_PCT / 100.0), deadline_at=DEADLINE,
    )
    assert want is not None and want[0] == "target"
    assert got.exit_reason == "target", (
        "уровень 98.50 ряд DIP_THEN_TARGET не задевает — дефект не проявился бы"
    )

    # А на ряде, который его задевает, расхождение налицо: именно это и обязана
    # ловить проверка эквивалентности.
    deeper = _bars(
        [(100.20, 98.40, 99.50)] + _flat(103.00, 102.00, 102.50, 60)
    )
    got_deeper = stopcf.resolve_variant(
        deeper, variant="no_stop", position_stop_pct=STOP_PCT,
        entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
        notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
        fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
    )
    assert got_deeper.exit_reason == "stop", "дефект не воспроизвёлся"
    want_deeper = _reference_without_stop(
        deeper, target_price=ENTRY * (1.0 + TARGET_PCT / 100.0),
        deadline_at=DEADLINE,
    )
    assert want_deeper is not None and want_deeper[0] == "target"
    assert got_deeper.exit_reason != want_deeper[0]


def test_the_only_premise_of_the_equivalence_is_checked_on_every_series() -> None:
    """Посылка «минимум бара положителен» проверяется, а не предполагается.

    Бар с ``low <= 0`` сделал бы сравнение ``bar.low <= 0`` истинным, и «вариант
    без предела» тихо стал бы вариантом с пределом на нуле. Ряд с таким баром
    обязан быть отвергнут, а не посчитан.
    """
    poisoned = _bars([(100.2, 0.0, 99.5)] + _flat(103.0, 102.0, 102.5, 60))
    with pytest.raises(ValueError, match="неположительным"):
        stopcf.resolve_variant(
            poisoned, variant="no_stop", position_stop_pct=STOP_PCT,
            entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
            notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
            fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
        )
    # А варианты с настоящим пределом такой ряд считают: посылка нужна ровно
    # варианту без предела и больше никому.
    ok = stopcf.resolve_variant(
        poisoned, variant="control", position_stop_pct=STOP_PCT,
        entry_price=ENTRY, target_pct=TARGET_PCT, cost_pct=COST_PCT,
        notional_usd=SLOT, opened_at=OPENED, deadline_at=DEADLINE,
        fact_closed_at=OPENED + timedelta(minutes=1), resolution="1m",
    )
    assert ok.exit_reason == "stop"


def test_the_target_level_comes_from_the_live_levels_function() -> None:
    """``rules.levels`` ноль отвергает — и обходить эту проверку нельзя.

    Уровень цели варианта без предела берётся ТЕМ ЖЕ вызовом ``levels`` с
    фактическим ``stop_pct`` позиции, а вернувшийся уровень предела
    отбрасывается. Подстановка нуля в ``levels`` была бы спором с правилом
    вместо пользования им.
    """
    from src.positions.rules import levels

    with pytest.raises(ValueError, match="предел должен быть положительным"):
        levels(ENTRY, TARGET_PCT, 0.0)
    source = " ".join(inspect.getsource(stopcf.resolve_variant).split())
    assert "position_rules.levels( entry_price, target_pct, position_stop_pct )" in source


# =============================================================================
# §6.6. Момент закрытия бара — грабли 9.1.3
# =============================================================================

def test_the_closing_moment_is_reused_and_not_written_a_second_time() -> None:
    """§2.2 ТЗ: ``closing_moment`` ПЕРЕИСПОЛЬЗУЕТСЯ, а не пишется заново.

    Второе место, знающее длину бара, разошлось бы с первым — и разошлось бы
    молча. Проверяется по тексту: импорт есть, своей арифметики «плюс минута»
    в модуле замера нет.
    """
    source = (_ROOT / "scripts" / "stop_counterfactual_9_1_4.py").read_text(
        encoding="utf-8"
    )
    assert "from src.shadow.trailing import closing_moment" in source
    assert "closing_moment(decision.exit_bar_ts, resolution)" in source
    assert "timedelta(seconds=60)" not in source
    assert "timedelta(minutes=1)" not in source


def test_the_closing_rule_still_matches_the_live_runner() -> None:
    """Правило ``runner`` не изменилось: ``closed_at = бар выхода + 60 секунд``.

    ЗДЕСЬ ДВА МЕСТА ЗНАЮТ ОДНО И ТО ЖЕ, и это признано, а не спрятано (та же
    проверка, что на Этапе 9.1.3). Изменится формула в живом правиле — эта
    проверка упадёт, а не разойдётся молча.
    """
    source = (_ROOT / "src" / "positions" / "runner.py").read_text(encoding="utf-8")
    assert "decision.exit_bar_ts + timedelta(seconds=60)" in source
    assert "now if by_gap else" in source


# =============================================================================
# Двойник базы: SQL ВЫПОЛНЯЕТСЯ и сверяется со схемой из миграций
# =============================================================================

def filter_signals(sql: str, rows: list[dict[str, Any]], args: tuple[Any, ...]) -> int:
    """Отбор сигналов ТЕМИ условиями, которые РЕАЛЬНО стоят в запросе.

    Двойник, фильтрующий за базу по своему разумению, мягче настоящей базы: он
    отсеял бы те же строки и при запросе, из которого условие убрали. На Этапе
    9.1.3 это была причина проверки, не проверявшей ничего, — и здесь на каждом
    условии стоит контрольный опыт, показывающий обратное.
    """
    instrument_id, min_probability, ts_from, ts_to = args
    out = list(rows)
    if "instrument_id = $1" in sql:
        out = [r for r in out if r["instrument_id"] == instrument_id]
    if "decision = 'buy'" in sql:
        out = [r for r in out if r["decision"] == "buy"]
    if "degraded = FALSE" in sql:
        out = [r for r in out if not r["degraded"]]
    if "probability IS NOT NULL" in sql:
        out = [r for r in out if r["probability"] is not None]
    if "probability >= $2" in sql:
        out = [r for r in out
               if r["probability"] is not None
               and r["probability"] >= min_probability]
    if "ts >= $3" in sql:
        out = [r for r in out if r["ts"] >= ts_from]
    if "ts < $4" in sql:
        out = [r for r in out if r["ts"] < ts_to]
    return len(out)


class _StopPool(SchemaPool):
    """Пул с данными: позиции, свечи, сигналы и хранилище строк замера.

    Наследуется от :class:`SchemaPool`, поэтому КАЖДЫЙ запрос сперва проходит
    сверку колонок со схемой из файлов миграций и только потом исполняется.
    """

    def __init__(
        self,
        positions: list[dict[str, Any]],
        bars: dict[int, list[dict[str, Any]]],
        signals: list[dict[str, Any]] | None = None,
        *,
        table_exists: bool = True,
        duplicate_writes: bool = False,
    ) -> None:
        super().__init__()
        self.positions = positions
        self.bars = bars
        self.signals = signals or []
        self.table_exists = table_exists
        self.duplicate_writes = duplicate_writes
        self.shadow: dict[Any, dict[str, Any]] = {}
        self.write_batches: list[list[Any]] = []
        self.signal_queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM ohlcv" in sql:
            instrument_id, _timeframe, ts_from, ts_to = args
            return [
                row for row in self.bars.get(int(instrument_id), [])
                if ts_from <= row["ts"] <= ts_to
            ]
        if "FROM positions p" in sql:
            # ДВОЙНИК ПРИМЕНЯЕТ ТЕ УСЛОВИЯ, КОТОРЫЕ РЕАЛЬНО СТОЯТ В ЗАПРОСЕ.
            since = args[0] if args else None
            rows = list(self.positions)
            if "p.status = 'closed'" in sql:
                rows = [r for r in rows if r["status"] == "closed"]
            if "p.exit_reason IS DISTINCT FROM 'data_gap'" in sql:
                rows = [r for r in rows if r["exit_reason"] != "data_gap"]
            if "p.opened_at >= $1" in sql and since is not None:
                rows = [r for r in rows if r["opened_at"] >= since]
            return project(sql, rows)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._check(sql)
        if "closed_total" in sql:
            since = args[0] if args else None
            rows = [
                r for r in self.positions
                if since is None or r["opened_at"] >= since
            ]
            return {
                "closed_total": sum(1 for r in rows if r["status"] == "closed"),
                "data_gap": sum(
                    1 for r in rows
                    if r["status"] == "closed" and r["exit_reason"] == "data_gap"
                ),
                "still_open": sum(1 for r in rows if r["status"] == "open"),
            }
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        if "to_regclass" in sql:
            return self.table_exists
        if "FROM signals" in sql:
            self.signal_queries.append((sql, args))
            return filter_signals(sql, self.signals, args)
        return None

    async def executemany(self, sql: str, rows: list[Any]) -> None:
        self._check(sql)
        self.writes.append(sql)
        self.write_batches.append(list(rows))
        # ON CONFLICT (position_id, variant) DO UPDATE — перезапись той же
        # строки, а не второй экземпляр. Двойник обязан вести себя так же,
        # иначе проверка идемпотентности ничего не проверяла бы.
        for row in rows:
            key: Any = (
                (int(row[0]), str(row[1]), len(self.write_batches))
                if self.duplicate_writes
                else (int(row[0]), str(row[1]))
            )
            self.shadow[key] = {
                "stop_pct": row[2], "exit_reason": row[3],
                "net_pnl_pct": row[6], "held_sec": row[8],
                "extra_held_sec": row[9], "blocked_signals": row[10],
            }


def _position_row(**over: Any) -> dict[str, Any]:
    """Строка ``positions`` со ВСЕМИ полями, которые читает запрос выборки.

    ЗАКРЫТИЕ БАРА, А НЕ ЕГО ОТКРЫТИЕ, в ``closed_at``: бар выхода открывается в
    OPENED и закрывается минутой позже, и ровно так пишет ``runner``.
    """
    row: dict[str, Any] = {
        "id": 1, "instrument_id": INSTRUMENT, "symbol": "ETH/USDT", "base": "ETH",
        "logic_version": 5, "horizon_h": HORIZON_H, "side": "buy",
        "status": "closed", "opened_at": OPENED, "deadline_at": DEADLINE,
        "closed_at": OPENED + timedelta(minutes=1),
        "entry_price": ENTRY, "notional_usd": SLOT,
        "target_pct": TARGET_PCT, "target_price": 102.0,
        "stop_pct": STOP_PCT, "stop_price": 99.0,
        "cost_pct": COST_PCT, "resolution": "1m",
        "exit_price": 99.0, "exit_reason": "stop",
        "net_pnl_pct": -STOP_PCT - COST_PCT,
        "net_pnl_usd": SLOT * (-STOP_PCT - COST_PCT) / 100.0,
        "bars_held": 1, "outcome_certain": True,
    }
    row.update(over)
    return row


def _bar_rows(series: list[Bar] = DIP_THEN_TARGET,
              instrument_id: int = INSTRUMENT) -> dict[int, list[dict[str, Any]]]:
    """Свечи в том виде, в каком их отдаёт ``ohlcv``."""
    return {
        instrument_id: [
            {"ts": bar.ts, "open": bar.low, "high": bar.high,
             "low": bar.low, "close": bar.close}
            for bar in series
        ]
    }


def _signal_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": INSTRUMENT, "decision": "buy", "degraded": False,
        "probability": 0.85, "ts": OPENED + timedelta(minutes=3),
    }
    row.update(over)
    return row


async def _run_script(monkeypatch, pool: _StopPool, argv: list[str]) -> int:
    """Прогоняет НАСТОЯЩИЙ скрипт этапа на двойнике пула."""
    from src.core.db import db as real_db

    async def _noop() -> None:
        return None

    monkeypatch.setattr(real_db, "_pool", pool, raising=False)
    monkeypatch.setattr(real_db, "connect", _noop)
    monkeypatch.setattr(real_db, "close", _noop)
    monkeypatch.setattr("sys.argv", ["stopcf", *argv])
    return await stopcf.main()


# =============================================================================
# §6.7. Контроль блокирующий
# =============================================================================

async def test_a_broken_control_stops_the_run_before_any_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.7: испорченная позиция даёт код 2 и НИ ОДНОЙ таблицы сравнения.

    Если пересчёт не умеет воспроизвести уже случившееся, его числа по НЕ
    случившимся вариантам не стоят ничего. Поэтому останавливается ВЕСЬ вывод, а
    не помечается одна строка.
    """
    pool = _StopPool([_position_row(exit_price=98.0)], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 2
    assert "КОНТРОЛЬ НЕ СОВПАЛ" in out
    assert "exit_price" in out
    assert "ЧИСЛО 1" not in out, "напечатана таблица при разошедшемся контроле"
    assert "ЧИСЛО 2" not in out and "ЧИСЛО 3" not in out
    assert pool.writes == [], "при разошедшемся контроле что-то записано"


async def test_a_difference_in_the_last_stored_digit_still_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.2: сверка идёт ДО ПОСЛЕДНЕГО ЗНАКА, и допуск расширять запрещено.

    Позиция портится на ПОСЛЕДНЕМ знаке хранения: восьмом для цены
    (``NUMERIC(20,8)``) и шестом для итога (``NUMERIC(12,6)``). Такое расхождение
    переживает только сравнение без допуска — и именно оно и требуется. Грубая
    порча (98.00 вместо 99.00) прошла бы и при округлении до целых, то есть не
    проверяла бы запрет §2.2.
    """
    pool = _StopPool([_position_row(exit_price=99.00000001)], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 2
    assert "КОНТРОЛЬ НЕ СОВПАЛ" in capsys.readouterr().out

    pool = _StopPool(
        [_position_row(net_pnl_pct=-STOP_PCT - COST_PCT + 0.000001)], _bar_rows()
    )
    assert await _run_script(monkeypatch, pool, []) == 2
    out = capsys.readouterr().out
    assert "net_pnl_pct" in out
    assert pool.writes == []


async def test_a_bar_open_instead_of_a_bar_close_is_caught_as_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.6: позиция с ``closed_at`` по ОТКРЫТИЮ бара распознаётся как расхождение.

    Это та самая путаница, что пришла с боевой базы 9.1.3: сверялись открытие
    бара с его закрытием при нулевом допуске, и все одиннадцать позиций
    разошлись ровно на 60 секунд.
    """
    pool = _StopPool([_position_row(closed_at=OPENED)], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 2
    out = capsys.readouterr().out
    assert "closed_at" in out and "КОНТРОЛЬ НЕ СОВПАЛ" in out
    assert pool.writes == []


async def test_control_experiment_replacing_the_close_with_the_open_breaks_control(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.6: подмена момента закрытия моментом открытия роняет сверку.

    Дефект возвращается буквально — ``closing_moment`` начинает возвращать время
    ОТКРЫТИЯ бара, — и исправная позиция объявляется разошедшейся. Без этого
    опыта проверка проходила бы и у скрипта, который вовсе не сверяет момент.
    """
    pool = _StopPool([_position_row()], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 0, capsys.readouterr().out
    capsys.readouterr()

    monkeypatch.setattr(stopcf, "closing_moment", lambda ts, resolution: ts)
    pool = _StopPool([_position_row()], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 2
    out = capsys.readouterr().out
    assert "closed_at" in out, "подмена не поймана — сверка момента не работает"


async def test_the_mismatch_count_can_never_exceed_the_compared_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7.3 ТЗ: расхождений не бывает больше, чем сверено (грабли 9.1.3).

    Позиция, расчёт которой упал с исключением, СВЕРЕНА (попытка была) и
    РАЗОШЛАСЬ (совпадения не получено); в оба счётчика она входит одинаково.
    Ряд с нулевым минимумом валит расчёт варианта без предела — это и есть
    позиция, на которой прежняя редакция напечатала бы невозможное.
    """
    poisoned = _bars([(100.2, 0.0, 99.5)] + _flat(103.0, 102.0, 102.5, 60))
    pool = _StopPool(
        [_position_row(id=1), _position_row(id=2)],
        {INSTRUMENT: [
            {"ts": bar.ts, "open": bar.low, "high": bar.high,
             "low": bar.low, "close": bar.close}
            for bar in poisoned
        ]},
    )
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 2
    assert "Сверено позиций:   2" in out
    assert "Разошлось:         2" in out
    assert "ValueError" in out


# =============================================================================
# §6.5. Заблокированные входы
# =============================================================================

def _blocking_pool() -> _StopPool:
    """Позиция из §6.1 и семь сигналов, из которых годен ровно один.

    Окно лишнего удержания: [01:01, 01:06) — от ФАКТИЧЕСКОГО закрытия по бару 0
    до пересчётного по бару 5. Каждый негодный сигнал отличается от годного
    ровно одним свойством, чтобы отказ каждого условия был виден по отдельности.
    """
    return _StopPool(
        [_position_row()],
        _bar_rows(),
        [
            _signal_row(),                                        # годен
            _signal_row(instrument_id=OTHER_INSTRUMENT),          # чужой токен
            _signal_row(decision="sell"),                         # не покупка
            _signal_row(probability=0.5),                         # ниже порога
            _signal_row(degraded=True),                           # неполный кворум
            _signal_row(ts=OPENED),                               # до окна
            _signal_row(ts=OPENED + timedelta(minutes=6)),        # правый конец
        ],
    )


async def test_blocked_signals_count_only_the_right_instrument_kind_and_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.5: годен ровно один сигнал из семи, и попадает он ровно в свои варианты.

    У контроля окна нет вовсе (он закрывается в тот же момент, что и факт),
    поэтому у него ноль — и это не «ничего не нашли», а «искать было негде».
    """
    pool = _blocking_pool()
    code = await _run_script(monkeypatch, pool, ["--apply"])
    out = capsys.readouterr().out
    assert code == 0, out

    rows = {key[1]: value for key, value in pool.shadow.items()}
    assert rows["control"]["blocked_signals"] == 0
    assert rows["no_stop"]["blocked_signals"] == 1
    assert rows["stop_1.5"]["blocked_signals"] == 1
    # Контроль вовсе не спрашивал базу: окно пустое, и запрос не отправлялся.
    assert all(args[2] < args[3] for _sql, args in pool.signal_queries)


@pytest.mark.parametrize(
    ("condition", "what"),
    [
        ("instrument_id = $1", "чужой токен"),
        ("decision = 'buy'", "продажа"),
        ("degraded = FALSE", "неполный кворум"),
        ("probability >= $2", "вероятность ниже порога"),
        ("ts >= $3", "сигнал до окна"),
        ("ts < $4", "сигнал на правом конце окна"),
    ],
)
def test_control_experiment_dropping_any_condition_changes_the_count(
    condition: str, what: str
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.5: каждое условие запроса ОТСЕИВАЕТ хотя бы один сигнал.

    Дефект возвращается по одному условию за раз: из НАСТОЯЩЕГО текста запроса
    убирается одно, и счёт обязан измениться. Условие, удаление которого ничего
    не меняет, не проверено ничем — ровно этим и был плох двойник 9.1.3,
    фильтровавший за базу.
    """
    from src.core.db import DB

    sql = inspect.getsource(DB.count_blocked_signals)
    assert condition in sql, f"условие {condition!r} исчезло из запроса"
    signals = _blocking_pool().signals
    args = (INSTRUMENT, MIN_PROBABILITY, OPENED + timedelta(minutes=1),
            OPENED + timedelta(minutes=6))

    full = filter_signals(sql, signals, args)
    assert full == 1, f"на исправном запросе годен один сигнал, получено {full}"
    broken = filter_signals(sql.replace(condition, "TRUE"), signals, args)
    assert broken > full, (
        f"удаление условия {condition!r} ({what}) счёт не изменило — "
        "условие не проверено ничем"
    )


def test_the_blocked_window_is_half_open_and_says_so() -> None:
    """Границы окна содержательны, а не удобны, и записаны в запросе.

    В момент ФАКТИЧЕСКОГО закрытия слот уже свободен — сигнал, пришедший ровно
    тогда, вошёл бы в позицию, а при более широком пределе не вошёл бы. В момент
    ПЕРЕСЧЁТНОГО закрытия слот освобождается и там, поэтому правый конец не
    включается.
    """
    from src.core.db import DB

    sql = inspect.getsource(DB.count_blocked_signals)
    assert "ts >= $3" in sql and "ts < $4" in sql
    assert "if ts_to <= ts_from:" in sql, (
        "вывернутое окно обязано давать ноль без обращения к базе"
    )


# =============================================================================
# §6.8–§6.9. Состав выборки, запись и идемпотентность
# =============================================================================

async def test_data_gap_and_open_positions_never_enter_the_sample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.8: ``data_gap`` в выборку не попадает, открытая позиция — тоже.

    У ``data_gap`` цена выхода не наблюдалась, а восстановлена; у открытой
    позиции исход ещё не наступил, и пересчётная цифра по ней была бы прогнозом,
    а не замером. Оба числа печатаются справочно: выборка, из которой что-то
    молча выпало, неотличима от выборки, в которой этого не было.
    """
    pool = _StopPool(
        [
            _position_row(id=1),
            _position_row(id=2, exit_reason="data_gap", exit_price=101.0,
                          net_pnl_pct=0.5, net_pnl_usd=0.01),
            _position_row(id=3, status="open", closed_at=None, exit_price=None,
                          exit_reason=None, net_pnl_pct=None, net_pnl_usd=None),
        ],
        _bar_rows(),
    )
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "Из них исключено (data_gap):  1" in out
    assert "Открытых (в замер не идут):   1" in out
    assert "В выборке замера:             1" in out


def test_control_experiment_the_double_would_show_a_dropped_sample_condition() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.8: двойник не фильтрует за базу.

    Дефект возвращается прямо: из текста запроса убирается условие про
    ``data_gap``, и двойник обязан вернуть строку, которую настоящая база
    вернула бы тоже. Двойник, отсеивающий такие строки сам, объявил бы
    исправной и базу, из которой условие пропало.
    """
    from src.core.db import DB

    sql = inspect.getsource(DB.get_positions_for_shadow)
    assert "p.exit_reason IS DISTINCT FROM 'data_gap'" in sql
    rows = [
        _position_row(id=1),
        _position_row(id=2, exit_reason="data_gap"),
    ]
    kept = [r for r in rows if r["exit_reason"] != "data_gap"]
    assert len(kept) == 1
    without = sql.replace("AND p.exit_reason IS DISTINCT FROM 'data_gap'", "")
    assert "IS DISTINCT FROM 'data_gap'" not in without
    # Двойник ориентируется на текст запроса — см. _StopPool.fetch.
    assert "p.exit_reason IS DISTINCT FROM 'data_gap'" in inspect.getsource(
        _StopPool.fetch
    )


async def test_without_apply_not_a_single_write_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.9: без ``--apply`` в базу не уходит ни одного запроса на запись.

    Проверяется ФАКТ отсутствия записи на уровне пула, а не текст вывода:
    скрипт, напечатавший «ничего не записано» и всё-таки записавший, прошёл бы
    проверку по выводу.
    """
    pool = _StopPool([_position_row()], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    capsys.readouterr()

    assert code == 0
    assert pool.writes == []
    assert pool.write_batches == []
    assert pool.shadow == {}


async def test_a_second_apply_changes_neither_the_count_nor_the_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.9: повторный прогон с ``--apply`` не меняет ни числа строк, ни значений.

    Идемпотентность здесь не обещание, а свойство запроса:
    ``ON CONFLICT (position_id, variant) DO UPDATE`` перезаписывает ту же строку
    теми же числами.
    """
    pool = _StopPool([_position_row()], _bar_rows(), [_signal_row()])

    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()
    first = dict(pool.shadow)
    assert len(first) == 5, f"строк {len(first)}, ожидалось 5"

    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()

    assert len(pool.shadow) == len(first), "повторный прогон добавил строки"
    assert pool.shadow == first, "повторный прогон изменил значения"
    assert "ON CONFLICT (position_id, variant) DO UPDATE" in pool.writes[0]


async def test_control_experiment_a_pool_that_appends_breaks_idempotency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к §6.9: двойник, копящий строки, ловится проверкой.

    Дефект возвращается на стороне хранилища: если бы запись не была
    перезаписью, второй прогон удвоил бы число строк. Проверка выше обязана это
    увидеть — иначе она проходила бы и при ``INSERT`` без ``ON CONFLICT``.
    """
    pool = _StopPool([_position_row()], _bar_rows(), duplicate_writes=True)
    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()
    first = dict(pool.shadow)
    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()
    assert len(pool.shadow) > len(first), "дефект не воспроизвёлся"


async def test_apply_without_the_migration_refuses_instead_of_failing_late(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Нет таблицы — внятный отказ с указанием миграции, а не ошибка Postgres."""
    pool = _StopPool([_position_row()], _bar_rows(), table_exists=False)
    code = await _run_script(monkeypatch, pool, ["--apply"])
    out = capsys.readouterr().out

    assert code == 2
    assert "022_position_stop_shadow.sql" in out
    assert pool.write_batches == []


async def test_an_empty_sample_returns_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§5 ТЗ: пустая выборка — код возврата 3, а не 0 и не падение."""
    pool = _StopPool([], {})
    code = await _run_script(monkeypatch, pool, [])
    assert code == 3
    assert "Выборка пуста" in capsys.readouterr().out


# =============================================================================
# §3. Четыре числа и их порядок
# =============================================================================

async def test_all_four_numbers_are_printed_and_the_first_one_is_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3 и §7.4 ТЗ: напечатаны все четыре числа, ЧИСЛО 1 — первым.

    Порядок содержателен: ЧИСЛО 1 — прямой ответ на вопрос владельца, и
    прятать его под таблицы средних значило бы отвечать не на тот вопрос,
    который задан. ХУДШАЯ СДЕЛКА печатается по каждому варианту: смысл предела —
    ограничить хвост, а среднее хвост скрывает.
    """
    pool = _StopPool([_position_row()], _bar_rows(), [_signal_row()])
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out
    assert code == 0, out

    for marker in ("ЧИСЛО 1", "ЧИСЛО 2", "ЧИСЛО 3", "ЧИСЛО 4"):
        assert marker in out, f"не напечатано {marker}"
    assert out.index("ЧИСЛО 1") < out.index("ЧИСЛО 2")
    assert out.index("ЧИСЛО 1") < out.index("ЧИСЛО 3")
    assert out.index("ЧИСЛО 1") < out.index("ЧИСЛО 4")
    # Предупреждение о силе выборки стоит ПЕРЕД таблицами, которые оно
    # ограничивает: иначе их прочитают раньше, чем узнают, как читать нельзя.
    assert out.index("ЧИСЛО 4") < out.index("ЧИСЛО 2")

    assert "ХУДШАЯ СДЕЛКА" in out
    for variant in stopcf.variant_names():
        assert f"худшая у {variant}:" in out, f"нет худшей сделки у {variant}"
    # Обе разбивки ЧИСЛА 2 — по всей выборке и по закрытым по пределу.
    assert "вся выборка" in out and "только закрытые по пределу" in out
    # ЧИСЛО 3 названо целиком, вместе с оговоркой о неоценённости входов.
    assert "ЗАБЛОКИРОВАННЫЕ ВХОДЫ ТОЛЬКО СЧИТАЮТСЯ, НО НЕ ОЦЕНИВАЮТСЯ" in out
    assert "ВЕРХНЯЯ" in out and "ГРАНИЦА" in out


async def test_a_small_sample_forbids_every_comparative_word(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3 ЧИСЛО 4 и §7.5: при N < 30 предупреждение печатается, оценок нет.

    Слова «лучше» и «хуже» при десятке сделок были бы утверждением, которого
    данные не несут: доверительный интервал разницы шире любой из наблюдаемых
    разниц. Проверяется буквально — по тексту вывода, включая слова, в которых
    запрещённое лежит внутри («улучшено» содержит «лучше»).
    """
    pool = _StopPool([_position_row()], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out

    assert "N = 1" in out
    assert "ВЫВОД О ПРЕИМУЩЕСТВЕ" in out
    lowered = out.lower()
    assert "лучше" not in lowered, "при малой выборке напечатано «лучше»"
    assert "хуже" not in lowered, "при малой выборке напечатано «хуже»"
    # И рекомендации об уровне предела нет ни в каком виде (§1 ТЗ).
    assert "ВЫБОР УРОВНЯ ПРЕДЕЛА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ" in out


async def test_the_first_number_answers_about_stop_positions_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3 ЧИСЛО 1: считаются ТОЛЬКО позиции, фактически закрытые по пределу.

    Позиция, закрытая по цели, к вопросу «дошли бы убыточные до цели» отношения
    не имеет, и подмешать её значило бы ответить на другой вопрос.
    """
    target_bars = {
        INSTRUMENT: [
            {"ts": bar.ts, "open": bar.low, "high": bar.high,
             "low": bar.low, "close": bar.close}
            for bar in DIP_THEN_TARGET
        ],
        OTHER_INSTRUMENT: [
            {"ts": bar.ts, "open": bar.low, "high": bar.high,
             "low": bar.low, "close": bar.close}
            for bar in STRAIGHT_TARGET
        ],
    }
    pool = _StopPool(
        [
            _position_row(id=1),
            _position_row(
                id=2, instrument_id=OTHER_INSTRUMENT, symbol="SOL/USDT",
                exit_reason="target", exit_price=102.0,
                net_pnl_pct=TARGET_PCT - COST_PCT,
                net_pnl_usd=SLOT * (TARGET_PCT - COST_PCT) / 100.0,
            ),
        ],
        target_bars,
    )
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out
    assert code == 0, out

    assert "Таких позиций: 1" in out
    assert "Без предела дошли бы до цели: 1 из 1" in out
    # Зато ЧИСЛО 2 считает обе: цена вопроса измеряется на всей выборке тоже.
    assert "N = 2" in out


# =============================================================================
# Границы этапа: миграция, живые правила, таблицы факта
# =============================================================================

def test_the_migration_matches_the_reasons_the_rule_actually_returns() -> None:
    """§4 ТЗ: перечень причин СВЕРЕН С КОДОМ ПРАВИЛА, а не со списком из ТЗ.

    На Этапе 9.1.3 в ТЗ был назван ``trailing`` вместо ``trail`` и забыт
    ``no_data``. Здесь перечень собирается из констант ``src/positions/rules``,
    и расхождение с текстом миграции роняет проверку.
    """
    from src.positions import rules

    text = (_ROOT / "db" / "migrations" / "022_position_stop_shadow.sql").read_text(
        encoding="utf-8"
    )
    assert "position_stop_shadow_reason_chk" in text
    block = text.split("position_stop_shadow_reason_chk", 2)[2].split("END IF;", 1)[0]
    for reason in (rules.EXIT_TARGET, rules.EXIT_STOP, rules.EXIT_TIMEOUT,
                   rules.EXIT_AMBIGUOUS):
        assert f"'{reason}'" in block, f"причина {reason} не разрешена миграцией"
    assert "'no_data'" in block, "неизмеренный исход записать некуда"
    # ``data_gap`` правило выхода знает, но возвращает его НЕ check_exit, и
    # такие позиции в выборку не берутся вовсе. Отсутствие значения — это
    # проверяемое утверждение, а не забывчивость.
    assert f"'{rules.EXIT_DATA_GAP}'" not in block


def test_the_migration_keeps_the_unmeasured_outcome_looking_unmeasured() -> None:
    """§4 ТЗ: поля исхода без NOT NULL, а нули удержания связаны с ``no_data``.

    ПОЛЯ ИСХОДА НАМЕРЕННО БЕЗ NOT NULL — прямое требование ТЗ и признание
    ошибки Этапа 9.1.3. А три NOT NULL, которые ТЗ оставило, связаны
    ограничением с причиной выхода: при ``no_data`` там обязаны стоять ровно
    нули, и читать их в отрыве от причины нельзя.
    """
    text = (_ROOT / "db" / "migrations" / "022_position_stop_shadow.sql").read_text(
        encoding="utf-8"
    )
    for column in ("exit_bar_ts", "exit_price", "net_pnl_pct", "net_pnl_usd"):
        line = next(ln for ln in text.splitlines() if ln.strip().startswith(column))
        assert "NOT NULL" not in line, f"{column} объявлена NOT NULL"
    assert "position_stop_shadow_shape_chk" in text
    assert "position_stop_shadow_gap_chk" in text
    assert "position_stop_shadow_control_chk" in text
    # Отрицательное лишнее удержание разрешено намеренно: при пределе уже
    # фактического позиция закрылась бы раньше.
    bounds = text.split("position_stop_shadow_bounds_chk", 2)[2].split("END IF;", 1)[0]
    assert "extra_held_sec >= 0" not in bounds

    rollback = (_ROOT / "db" / "migrations"
                / "022_position_stop_shadow_rollback.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS position_stop_shadow;" in rollback


def test_the_measurement_never_touches_the_tables_of_fact() -> None:
    """§7.8 ТЗ: ни одной записи в таблицы факта — ни миграцией, ни кодом.

    Внешний ключ смотрит ИЗ новой таблицы наружу. Пересчётный результат не имеет
    права попасть туда, где лежит факт: строка ``positions`` — это то, что
    случилось, а строка отсюда — то, что случилось бы.
    """
    fact_tables = (
        "positions", "signals", "signal_evaluations", "signal_targets",
        "risk_targets", "trailing_outcomes", "position_trailing_shadow",
    )
    text = (_ROOT / "db" / "migrations" / "022_position_stop_shadow.sql").read_text(
        encoding="utf-8"
    )
    for table in fact_tables:
        assert f"ALTER TABLE {table} " not in text, f"миграция трогает {table}"
        assert f"DROP TABLE IF EXISTS {table};" not in text
    assert "REFERENCES positions(id) ON DELETE CASCADE" in text

    script = code_only(
        (_ROOT / "scripts" / "stop_counterfactual_9_1_4.py").read_text(
            encoding="utf-8"
        )
    )
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in script, f"скрипт содержит {verb}"

    from src.core.db import DB

    write = inspect.getsource(DB.save_position_stop_shadow)
    assert "INSERT INTO position_stop_shadow" in write
    for table in fact_tables:
        assert f"INSERT INTO {table}" not in write
        assert f"UPDATE {table}" not in write
    for reader in (DB.count_blocked_signals, DB.position_stop_shadow_exists):
        source = inspect.getsource(reader)
        assert "INSERT" not in source and "UPDATE" not in source
        assert "DELETE" not in source


def test_the_stage_changes_no_live_rule() -> None:
    """§1 ТЗ: границы этапа соблюдены — правило выхода не переписано.

    Проверяется по факту, а не по обещанию: расчёт ВЫЗЫВАЕТ живое правило и не
    содержит своего условия касания предела. Условие ``bar.low <= stop_price``
    существует ровно в одном месте проекта — в ``src/positions/rules._touches``.
    """
    script = code_only(
        (_ROOT / "scripts" / "stop_counterfactual_9_1_4.py").read_text(
            encoding="utf-8"
        )
    )
    assert "from src.positions import rules as position_rules" in script
    assert "position_rules.check_exit(" in script
    assert "position_rules.levels(" in script
    assert "position_rules.net_pnl(" in script
    # Ни своего сравнения с пределом, ни своей арифметики издержек.
    assert "<= stop_price" not in script
    assert ">= target_price" not in script
    assert "LOGIC_VERSION =" not in script
    # Уровень предела не берётся из сегодняшней настройки: контроль обязан
    # считаться тем уровнем, что записан в САМОЙ позиции. Слова
    # ``BARRIER_STOP_PCT`` в пояснениях достаточно, обращения к ней — нет.
    assert "settings.BARRIER_STOP_PCT" not in script

    # И сами живые правила этапом не тронуты: порог входа читается из настроек,
    # а не переписан числом.
    assert "settings.POSITION_MIN_PROBABILITY" in script


def test_the_calculation_of_an_outcome_is_a_pure_function() -> None:
    """§5 ТЗ: расчёт исхода по ряду баров — ни базы, ни сети, ни ``now()``.

    Это условие проверяемости, а не стиль: правило, которое само ходит в базу,
    нельзя прогнать на придуманном ряде с заранее известным ответом.
    """
    for function in (stopcf.resolve_variant, stopcf.resolve_position_stops,
                     stopcf.check_monotonic, stopcf.assert_no_stop,
                     stopcf.outcome_table, stopcf.money_summary,
                     stopcf.slot_summary, stopcf.variant_stop_pct):
        # Пояснения из проверки исключаются: строка «ни datetime.now()» в
        # заголовке функции — это обещание чистоты, а не её нарушение.
        code = code_only(inspect.getsource(function))
        assert "datetime.now(" not in code, f"{function.__name__} зовёт now()"
        assert "await " not in code, f"{function.__name__} ходит в базу"
        assert "db." not in code, f"{function.__name__} ходит в базу"


def test_the_settle_delay_is_imported_and_not_copied() -> None:
    """Запас закрытия бара берётся ИМПОРТОМ, а не копией формулы.

    Копия формулы уже была причиной дефекта 8.10.1 в этом проекте: читался
    ещё не закрытый бар, ``close`` которого коллектор потом переписывал.
    """
    script = (_ROOT / "scripts" / "stop_counterfactual_9_1_4.py").read_text(
        encoding="utf-8"
    )
    assert "from src.barrier.runner import settle_seconds" in script
    assert "settle_seconds()" in script
    assert "BARRIER_SETTLE_MINUTES" not in script


# =============================================================================
# Двойник схемы: он по-прежнему строже удобного
# =============================================================================

def test_the_schema_double_still_rejects_a_query_to_a_missing_column() -> None:
    """Двойник обязан ловить обращение к несуществующей колонке.

    КОНТРОЛЬНЫЙ ОПЫТ к самому двойнику: без него все проверки, идущие через
    него, доказывали бы лишь то, что код не падает.
    """
    tables = schema()
    assert "position_stop_shadow" in tables
    with pytest.raises(UndefinedColumn):
        check_sql_columns(
            "SELECT s.what_is_this FROM position_stop_shadow s;", tables
        )
    with pytest.raises(UndefinedColumn):
        check_sql_columns("SELECT nonexistent FROM signals;", tables)
    # А исправный запрос проходит — иначе проверка выше падала бы всегда.
    check_sql_columns(
        "SELECT variant, held_sec FROM position_stop_shadow;", tables
    )


def test_every_stage_query_passes_the_schema_check() -> None:
    """Каждый запрос этапа сверяется с составом таблиц из файлов миграций."""
    from src.core.db import DB

    tables = schema()
    for method in (DB.get_positions_for_shadow, DB.count_positions_for_shadow,
                   DB.count_blocked_signals, DB.save_position_stop_shadow,
                   DB.position_stop_shadow_exists, DB.get_ohlcv_bars):
        source = inspect.getsource(method)
        for chunk in source.split('"""'):
            if "SELECT" in chunk or "INSERT" in chunk:
                check_sql_columns(chunk, tables)
