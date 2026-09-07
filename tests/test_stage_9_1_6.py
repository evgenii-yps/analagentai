"""Этап 9.1.6: замер правила «без предела убытка, ожидание возврата в плюс».

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, И ПОЧЕМУ ИМЕННО ЭТО.

ГЛАВНАЯ ОПАСНОСТЬ ЗАМЕРА — ПРАВДОПОДОБНОЕ ЧИСЛО. Неверно посчитанный исход
выглядит ровно так же, как верный: ни исключения, ни пустого места, ни красной
строки в журнале. Поэтому проверяется не «получилось ли число», а шесть вещей,
каждая из которых способна сделать число ложным:

 1. ПРАВИЛО. Все пять вариантов и обе фазы каждого считаются ОДНОЙ И ТОЙ ЖЕ
    ``rules.check_exit`` с разными уровнями и сроками. Своё правило выхода
    сделало бы контроль не контролем.
 2. ГРАНИЦА ФАЗ. Сутки — это ровно сутки. Сдвиг на один бар переносит бары из
    фазы, где условие плюса не проверяется, в фазу, где проверяется.
 3. ЦЕНА ВЫХОДА ПРИ ``plus_exit``. Это ЦЕНА БЕЗУБЫТКА, а не закрытие бара, и
    следствие проверяемое: итог такой сделки равен нулю по построению.
 4. ЧИСТЫЙ ПЛЮС ПРОТИВ ВАЛОВОГО. Вариант B обязан отличаться от D; подмена
    одного флага делает их одинаковыми и не видна ни по одному числу.
 5. ЗАПРЕТ НА ПОДГЛЯДЫВАНИЕ И ОТБРАКОВКА. Бар годен, только если закрылся
    строго раньше рассматриваемого момента; позиция годна, только если её
    48-часовой отрезок покрыт целиком.
 6. КОНТРОЛЬ. Пересчёт при фактическом пределе обязан воспроизвести записанное в
    ``positions`` — включая МОМЕНТ ЗАКРЫТИЯ БАРА, на котором Этап 9.1.3 получил
    одиннадцать ложных расхождений по 60 секунд.

КОНТРОЛЬНЫЕ ОПЫТЫ ОБЯЗАТЕЛЬНЫ (§9.3 ТЗ), И ЗДЕСЬ ОНИ — НАСТОЯЩАЯ ПРАВКА КОДА.
Каждый опыт читает файл скрипта, ТЕКСТУАЛЬНО заменяет в нём кусок на дефектный,
компилирует получившийся модуль и показывает, что защита падает. Замена, не
нашедшая своего куска, роняет опыт сама (:func:`mutated`): опыт, который ничего
не изменил, — это не пройденный опыт, а несделанный. На Этапе 9.1.3 три проверки
из девяти прошли молча; опыты помечены в именах словом ``control_experiment``.

ДВОЙНИК БАЗЫ ЗДЕСЬ НЕ МЯГЧЕ НАСТОЯЩЕЙ (§9.4 ТЗ). Он ВЫПОЛНЯЕТ SQL настоящих
методов ``DB``, сверяет каждую колонку с составом таблиц, вычитанным ИЗ ФАЙЛОВ
МИГРАЦИЙ, и отдаёт РОВНО ТЕ КОЛОНКИ, что названы в запросе. Оба греха —
фильтрация строк за базу и свои ключи в ответе — уже воспроизводились в этом
проекте.

ЧЕГО ЭТИ ПРОВЕРКИ НЕ ДОКАЗЫВАЮТ. Ни одна из них не запускалась на настоящих
свечах: ряды здесь придуманы, и придуманы так, чтобы ответ был известен заранее.
Совпадение контроля с фактом на боевых данных — это то, что покажет первый
прогон на сервере, и заменить его синтетикой нельзя.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import scripts.plus_exit_counterfactual_9_1_6 as plus
from src.core.db import DB
from src.positions import rules as position_rules
from src.positions.rules import Bar
from tests.conftest import CURRENT_LOGIC_VERSION
from tests.schema_double import (
    SchemaPool,
    UndefinedColumn,
    check_sql_columns,
    output_columns,
    project,
    schema,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "plus_exit_counterfactual_9_1_6.py"
_MIGRATION = _ROOT / "db" / "migrations" / "024_position_plus_exit_shadow.sql"
_ROLLBACK = (
    _ROOT / "db" / "migrations" / "024_position_plus_exit_shadow_rollback.sql"
)

# Позиция-образец: вход по 100, цель +2%, предел −1%, издержки 0.22%, слот $2.
# Круглые числа выбраны затем, чтобы ответ был виден глазом: цель 102.00,
# предел 99.00, цена ЧИСТОГО безубытка 100.22, ВАЛОВОГО — 100.00.
OPENED = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
DEADLINE_24 = OPENED + timedelta(hours=24)
DEADLINE_48 = OPENED + timedelta(hours=48)
ENTRY = 100.0
TARGET_PCT = 2.0
STOP_PCT = 1.0
COST_PCT = 0.22
SLOT = 2.0
INSTRUMENT = 10
MINUTES = 48 * 60 + 1


def code_only(text: str) -> str:
    """Текст без пояснений: только исполняемый код.

    ЗАЧЕМ ЭТО НУЖНО. Проверки границы этапа ищут в файле собственное правило
    выхода — например, своё сравнение ``bar.high >= target``. Но ровно такие
    сравнения ЦИТИРУЮТСЯ в пояснениях, объясняющих, как выражено ожидание плюса
    через чужую функцию. Искать по всему файлу значило бы запретить объяснять,
    как работает чужой код, — и проверка падала бы на исправном файле, а обойти
    её пришлось бы, убрав объяснение.
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


def mutated(old: str, new: str) -> types.ModuleType:
    """НАСТОЯЩАЯ ПРАВКА КОДА: файл скрипта с заменённым куском, как модуль.

    ОПЫТ, НЕ ИЗМЕНИВШИЙ НИ ОДНОГО СИМВОЛА, — ЭТО НЕ ПРОЙДЕННЫЙ ОПЫТ, А
    НЕСДЕЛАННЫЙ. Поэтому кусок обязан встретиться в файле РОВНО ОДИН раз, и
    несовпадение роняет опыт здесь же, а не превращает его в зелёную строку.
    Именно так три проверки Этапа 9.1.3 прошли молча: дефект вносился не туда,
    куда думали, и «проверка» подтверждала исправный код.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    found = text.count(old)
    if found != 1:
        raise AssertionError(
            f"контрольный опыт не нашёл своего куска ровно один раз "
            f"(нашёл {found}): {old!r}"
        )
    name = f"plus_exit_mutant_{abs(hash(old)):x}"
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(_SCRIPT)
    # МОДУЛЬ КЛАДЁТСЯ В ``sys.modules`` ДО ИСПОЛНЕНИЯ: ``dataclass`` ищет там
    # пространство имён своего класса, и без записи разбор аннотаций падает
    # раньше, чем опыт успевает что-либо проверить.
    sys.modules[name] = module
    try:
        exec(compile(text.replace(old, new), str(_SCRIPT), "exec"),
             module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def bars_from(path: list[tuple[float, float, float]]) -> list[Bar]:
    """Ряд минутных баров из троек ``(high, low, close)`` от момента входа."""
    return [
        Bar(ts=OPENED + timedelta(minutes=i), high=h, low=lo, close=c)
        for i, (h, lo, c) in enumerate(path)
    ]


def _hold(high: float, low: float, close: float, minutes: int
          ) -> list[tuple[float, float, float]]:
    return [(high, low, close)] * minutes


def stored_levels(entry: float, target_pct: float, stop_pct: float
                  ) -> tuple[float, float]:
    """Уровни ТАК, КАК ИХ ХРАНИТ БАЗА: ``rules.levels`` плюс NUMERIC(20,8).

    Ровно этот путь проходит цена при открытии позиции: живой сервис считает
    уровни формулой, а колонка округляет их до восьми знаков. Пересчёт, не
    повторивший округление, разошёлся с боевым фактом 06.09.2026 на трёх
    позициях DOGE — см. §А ТЗ.
    """
    target, stop = position_rules.levels(entry, target_pct, stop_pct)
    return (
        plus.to_storage(target, plus.PRICE_STORAGE_PLACES),
        plus.to_storage(stop, plus.PRICE_STORAGE_PLACES),
    )


def resolve_with(module: Any, bars: list[Bar], **over: Any) -> Any:
    """Расчёт указанным модулем — исправным или подделанным контрольным опытом.

    Уровни приходят ОДНИМ способом в обоих случаях: опыт обязан проверять
    ровно тот дефект, который в него внесли, а не разницу в том, как тест
    позвал функцию.
    """
    entry = float(over.get("entry_price", ENTRY))
    target_pct = float(over.get("target_pct", TARGET_PCT))
    stop_pct = float(over.get("stop_pct", STOP_PCT))
    target_price, stop_price = stored_levels(entry, target_pct, stop_pct)
    kwargs: dict[str, Any] = {
        "entry_price": entry, "target_pct": target_pct, "stop_pct": stop_pct,
        "fact_target_price": target_price, "fact_stop_price": stop_price,
        "cost_pct": COST_PCT, "notional_usd": SLOT, "side": "buy",
        "opened_at": OPENED, "fact_deadline_at": DEADLINE_24,
        "resolution": "1m",
    }
    kwargs.update(over)
    return module.resolve_position(bars, **kwargs)


def resolve(bars: list[Bar], **over: Any) -> dict[str, plus.PlusOutcome]:
    entry = float(over.get("entry_price", ENTRY))
    target_pct = float(over.get("target_pct", TARGET_PCT))
    stop_pct = float(over.get("stop_pct", STOP_PCT))
    target_price, stop_price = stored_levels(entry, target_pct, stop_pct)
    kwargs: dict[str, Any] = {
        "entry_price": ENTRY, "target_pct": TARGET_PCT, "stop_pct": STOP_PCT,
        "fact_target_price": target_price, "fact_stop_price": stop_price,
        "cost_pct": COST_PCT, "notional_usd": SLOT, "side": "buy",
        "opened_at": OPENED, "fact_deadline_at": DEADLINE_24,
        "resolution": "1m",
    }
    kwargs.update(over)
    return plus.resolve_position(bars, **kwargs)


# Четыре ряда, на которых ответ известен заранее.
#
# В КАЖДОМ РЯДУ 2881 БАР, А НЕ 2880, И ПОСЛЕДНИЙ — БАР СРОКА t0+48ч.
# ``check_exit`` возвращает ``timeout`` только когда бар с меткой ``deadline_at``
# ПРЕДЪЯВЛЕН: пока его нет, неизвестно, кончилось окно или данные не подъехали,
# и правило честно отвечает «исхода нет». Ряд из 2880 баров давал бы ``no_data``
# там, где ожидается ``timeout_48``, — и проверка измеряла бы неполноту данных
# вместо правила.

# DIP_THEN_BREAKEVEN: минимум 97.00 задевает предел 99.00 на первом же баре;
# к 24 часам позиция стоит −2.22% чистыми; на 30-м часу максимум 100.30
# перекрывает чистый безубыток 100.22, но цели 102.00 не достигает никогда.
DIP_THEN_BREAKEVEN = bars_from(
    _hold(99.00, 97.00, 98.00, 24 * 60)
    + _hold(99.50, 98.50, 99.00, 6 * 60)
    + _hold(100.30, 99.90, 100.20, 18 * 60 + 1)
)
# KEEPS_FALLING: цена оседает и не возвращается ни к безубытку, ни к цели.
KEEPS_FALLING = bars_from(_hold(99.00, 97.00, 98.00, MINUTES))
# GROSS_ONLY_THEN_TARGET: с 24-го часа максимум 100.10 перекрывает ВАЛОВОЙ
# безубыток 100.00, но не чистый 100.22; с 26-го часа берётся цель.
GROSS_ONLY_THEN_TARGET = bars_from(
    _hold(99.90, 99.50, 99.80, 24 * 60)
    + _hold(100.10, 99.90, 100.00, 2 * 60)
    + _hold(102.50, 100.00, 102.20, 22 * 60 + 1)
)
# TARGET_ON_FIRST_DAY: цель взята на первом же баре, предела не касались вовсе.
TARGET_ON_FIRST_DAY = bars_from(
    [(102.50, 100.10, 102.20)] + _hold(103.00, 102.00, 102.50, MINUTES - 1)
)


# =============================================================================
# §3 ТЗ. Правило на придуманных рядах с заранее известным ответом
# =============================================================================

def test_the_owners_rule_is_exactly_what_the_measurement_computes() -> None:
    """§3 ТЗ целиком, на одном ряде: предел снят, сутки на цель, сутки на плюс.

    ЭТО И ЕСТЬ ВОПРОС ВЛАДЕЛЬЦА, ПОДАННЫЙ КАК РЯД СВЕЧЕЙ. При фактическом
    правиле сделка закрыта в минус на первом же баре. При снятом пределе и
    закрытии по сроку 24 часа (вариант 9.1.4) она закрыта ЕЩЁ ХУЖЕ. И только
    ожидание вторых суток выводит её в ноль — ценой слота, занятого 30 часов
    вместо одной минуты. Обе половины ответа измеряются одновременно, потому что
    порознь каждая вводит в заблуждение.
    """
    rows = resolve(DIP_THEN_BREAKEVEN)

    assert rows["control"].exit_reason == "stop"
    assert rows["control"].exit_price == pytest.approx(99.0)
    assert rows["control"].net_pnl_pct == pytest.approx(-STOP_PCT - COST_PCT)

    assert rows["A_nostop_24"].exit_reason == "timeout_24"
    assert rows["A_nostop_24"].exit_price == pytest.approx(98.0)
    assert rows["A_nostop_24"].held_hours == pytest.approx(24.0)

    assert rows["B_nostop_plus_48"].exit_reason == "plus_exit"
    # ЦЕНА ВЫХОДА — ЦЕНА БЕЗУБЫТКА, А НЕ ЗАКРЫТИЕ БАРА (100.20).
    assert rows["B_nostop_plus_48"].exit_price == pytest.approx(100.22)
    assert rows["B_nostop_plus_48"].net_pnl_pct == pytest.approx(0.0, abs=1e-12)
    # Первый бар вторых суток с максимумом 100.30 — это 30-й час; момент
    # закрытия — закрытие этого бара, отсюда лишняя минута.
    assert rows["B_nostop_plus_48"].held_hours == pytest.approx(30.0 + 1 / 60)

    # C отличается от B ровно тем, что предел сработал раньше ожидания.
    assert rows["C_stop_plus_48"].exit_reason == "stop"
    assert rows["C_stop_plus_48"].held_hours == rows["control"].held_hours

    # D дошёл до ВАЛОВОГО безубытка на том же баре — цена ниже, итог ниже.
    assert rows["D_nostop_plus_48_gross"].exit_reason == "plus_exit"
    assert rows["D_nostop_plus_48_gross"].exit_price == pytest.approx(100.0)
    assert rows["D_nostop_plus_48_gross"].net_pnl_pct == pytest.approx(-COST_PCT)


def test_a_price_that_never_comes_back_pays_for_the_wait_twice() -> None:
    """Ожидание, которое не помогло: тот же убыток и вдвое дольше занятый слот."""
    rows = resolve(KEEPS_FALLING)

    assert rows["control"].exit_reason == "stop"
    assert rows["A_nostop_24"].exit_reason == "timeout_24"
    assert rows["B_nostop_plus_48"].exit_reason == "timeout_48"
    assert rows["D_nostop_plus_48_gross"].exit_reason == "timeout_48"
    # Предел у C сработал — ожидания не было вовсе (§4 ТЗ).
    assert rows["C_stop_plus_48"].exit_reason == "stop"

    assert rows["B_nostop_plus_48"].net_pnl_pct == pytest.approx(-2.0 - COST_PCT)
    assert rows["B_nostop_plus_48"].held_hours == pytest.approx(48.0)
    assert rows["A_nostop_24"].held_hours == pytest.approx(24.0)


def test_the_cost_rate_alone_decides_two_of_the_five_variants() -> None:
    """Ради чего заведён вариант D: ставка издержек разделяет сделки.

    На этом ряде B ждёт ЧИСТОГО безубытка 100.22, не получает его на 24-м часу и
    дожидается цели на 26-м. D довольствуется ВАЛОВЫМ 100.00 и выходит в минус
    ровно на издержки — то есть ставка 0.22% стоит здесь двух процентов итога.
    """
    rows = resolve(GROSS_ONLY_THEN_TARGET)

    assert rows["D_nostop_plus_48_gross"].exit_reason == "plus_exit"
    assert rows["D_nostop_plus_48_gross"].net_pnl_pct == pytest.approx(-COST_PCT)
    assert rows["B_nostop_plus_48"].exit_reason == "target"
    assert rows["B_nostop_plus_48"].net_pnl_pct == pytest.approx(
        TARGET_PCT - COST_PCT
    )
    assert (
        rows["D_nostop_plus_48_gross"].held_hours
        < rows["B_nostop_plus_48"].held_hours
    )


def test_a_position_closed_at_the_target_is_the_same_under_all_five() -> None:
    """Сделка, дошедшая до цели в первые сутки, вариантом не различается.

    Проверка кажется тривиальной, а она и есть та, что ловит самую тихую
    ошибку: если вторая фаза начнёт вмешиваться в уже закрытую сделку, числа
    останутся правдоподобными, а выборка молча поедет.
    """
    rows = resolve(TARGET_ON_FIRST_DAY)
    for variant in plus.variant_names():
        assert rows[variant].exit_reason == "target"
        assert rows[variant].exit_price == pytest.approx(102.0)
        assert rows[variant].held_hours == pytest.approx(1 / 60)


def test_the_target_stays_active_through_the_second_day() -> None:
    """§3.1 ТЗ: цель активна на ВСЁМ отрезке [t0, t0+48ч], а не только сутки."""
    path = (
        _hold(99.90, 99.50, 99.80, 24 * 60)
        + _hold(102.50, 100.00, 102.20, 24 * 60 + 1)
    )
    rows = resolve(bars_from(path))
    assert rows["B_nostop_plus_48"].exit_reason == "target"
    assert rows["B_nostop_plus_48"].exit_price == pytest.approx(102.0)


def test_when_both_are_reachable_on_one_bar_the_target_wins() -> None:
    """§3.7 ТЗ: цель и плюс на одном баре — исход ``target``, и цена целевая."""
    path = (
        _hold(99.90, 99.50, 99.80, 24 * 60)
        # Один и тот же бар перекрывает и 100.22, и 102.00.
        + _hold(102.50, 99.00, 100.10, 24 * 60 + 1)
    )
    rows = resolve(bars_from(path))
    assert rows["B_nostop_plus_48"].exit_reason == "target"
    assert rows["B_nostop_plus_48"].exit_price == pytest.approx(102.0)
    assert rows["B_nostop_plus_48"].held_hours == pytest.approx(24.0 + 1 / 60)


def test_the_plus_is_not_checked_before_the_first_day_is_over() -> None:
    """§3.3 ТЗ: на [t0, t0+24ч) выходов, кроме цели и предела, нет.

    Ряд ВЕСЬ ВРЕМЯ выше чистого безубытка, но до суток это никого не выводит:
    первый выход по плюсу возможен только с отметки 24 часов включительно.
    """
    path = _hold(100.50, 100.30, 100.40, MINUTES)
    rows = resolve(bars_from(path))
    assert rows["B_nostop_plus_48"].exit_reason == "plus_exit"
    # Первый бар, на котором условие вообще проверяется, — бар отметки 24 ч.
    assert rows["B_nostop_plus_48"].held_hours == pytest.approx(24.0 + 1 / 60)
    assert rows["A_nostop_24"].exit_reason == "timeout_24"


def test_a_series_that_stops_short_of_the_deadline_is_unmeasured() -> None:
    """Оборванный ряд даёт ``no_data``, а не ``timeout_48``.

    Назвать неполноту данных исходом значило бы выдать неизмеренное за
    измеренное — и выдать в сторону, удобную ожиданию: «сделка досидела до
    срока» звучит как измерение, а на деле бар срока никто не предъявлял.
    """
    rows = resolve(bars_from(_hold(99.00, 97.00, 98.00, 40 * 60)))
    assert rows["B_nostop_plus_48"].exit_reason == "no_data"
    assert rows["B_nostop_plus_48"].exit_price is None
    assert rows["B_nostop_plus_48"].net_pnl_pct is None
    assert rows["B_nostop_plus_48"].held_hours is None
    # А у вариантов без ожидания первые сутки покрыты целиком — исход есть.
    assert rows["A_nostop_24"].exit_reason == "timeout_24"


def test_a_series_shorter_than_the_first_day_is_unmeasured_everywhere() -> None:
    """Ряд не дотянул и до суток: исхода нет ни у одного варианта."""
    rows = resolve(bars_from(_hold(99.90, 99.50, 99.80, 10 * 60)))
    for variant in plus.variant_names():
        if variant == "control":
            continue
        assert rows[variant].exit_reason == "no_data"


# =============================================================================
# §3.6 ТЗ. Цена безубытка
# =============================================================================

def test_the_breakeven_price_is_the_one_written_in_the_assignment() -> None:
    """§3.6 ТЗ дословно: покупка ``P0×(1+C)``, продажа ``P0×(1−C)``."""
    assert plus.breakeven_price(
        100.0, 0.22, side="buy", gross=False
    ) == pytest.approx(100.22)
    assert plus.breakeven_price(
        100.0, 0.22, side="sell", gross=False
    ) == pytest.approx(99.78)
    assert plus.breakeven_price(
        100.0, 0.22, side="buy", gross=True
    ) == pytest.approx(100.0)


def test_the_breakeven_price_gives_exactly_zero_by_construction() -> None:
    """Итог сделки, закрытой по цене безубытка, равен нулю. Это и есть проверка.

    Считается ТОЙ ЖЕ ``rules.net_pnl``, которой посчитан итог настоящей позиции:
    своя формула здесь сверяла бы цену безубытка сама с собой.
    """
    for entry in (0.35, 100.0, 63_412.77):
        for cost in (0.0, 0.22, 1.5):
            price = plus.breakeven_price(
                entry, cost, side="buy", gross=False
            )
            # Цена приведена к точности хранения (§А 2.2), поэтому ноль здесь —
            # с точностью до ПОЛОВИНЫ ТИКА, и допуск считается от цены входа, а
            # не берётся константой: константа по BTC пропустила бы DOGE.
            assert position_rules.net_pnl(entry, price, cost) == pytest.approx(
                0.0, abs=plus.flat_tolerance(entry)
            )
            assert price == plus.to_storage(price, plus.PRICE_STORAGE_PLACES)


def test_the_cost_rate_comes_from_the_position_and_not_from_settings() -> None:
    """§3.6 ТЗ: ставка издержек берётся ИЗ КОДА и из самой позиции.

    Позиция велась по ЗАПИСАННОМУ ``cost_pct``; настройка сегодняшнего дня могла
    с тех пор измениться, и «плюс», посчитанный по ней, был бы плюсом другой
    сделки. Проверяется прямо: позиция с иной ставкой даёт иную цену выхода.
    """
    rows = resolve(DIP_THEN_BREAKEVEN, cost_pct=0.10)
    assert rows["B_nostop_plus_48"].exit_price == pytest.approx(100.10)
    assert rows["B_nostop_plus_48"].net_pnl_pct == pytest.approx(0.0, abs=1e-12)
    body = code_only(inspect.getsource(plus.resolve_variant))
    assert "settings." not in body


# =============================================================================
# §9.2 ТЗ. Эквивалентность снятия предела — переиспользована, а не переписана
# =============================================================================

def test_the_no_stop_equivalence_is_reused_from_stage_9_1_4() -> None:
    """§2.1 ТЗ: ``assert_no_stop`` и ``NO_STOP_PRICE`` НЕ дублируются.

    Разбор эквивалентности «предела нет» ≡ ``stop_price = 0`` сделан на Этапе
    9.1.4 вместе с его единственной посылкой. Вторая копия того же рассуждения
    однажды разошлась бы с первой — и разошлась бы молча.
    """
    import scripts.stop_counterfactual_9_1_4 as stopcf

    assert plus.assert_no_stop is stopcf.assert_no_stop
    assert plus.NO_STOP_PRICE == stopcf.NO_STOP_PRICE
    body = code_only(_SCRIPT.read_text(encoding="utf-8"))
    assert "def assert_no_stop" not in body


def test_the_premise_of_the_equivalence_is_checked_on_every_series() -> None:
    """Посылка «минимум бара положителен» роняет расчёт при нарушении (§9.2 ТЗ).

    Бар с ``low <= 0`` сделал бы сравнение ``bar.low <= 0`` истинным, и «вариант
    без предела» тихо превратился бы в вариант с пределом на нуле.
    """
    broken = bars_from(
        _hold(99.90, 99.50, 99.80, 100)
        + [(99.90, 0.0, 99.80)]
        + _hold(99.90, 99.50, 99.80, MINUTES - 101)
    )
    with pytest.raises(ValueError, match="неположительным минимумом"):
        resolve(broken)


def test_the_target_level_comes_from_the_live_levels_function() -> None:
    """§9.2 ТЗ: цель для вариантов без предела берётся тем же ``levels``.

    ``rules.levels`` отвергает ``stop_pct <= 0``, поэтому подставлять туда ноль
    ради варианта без предела нельзя: это был бы спор с правилом вместо
    пользования им. Уровень цели считается с ФАКТИЧЕСКИМ пределом позиции, а
    вернувшийся уровень предела отбрасывается.
    """
    with pytest.raises(ValueError, match="предел должен быть положительным"):
        position_rules.levels(ENTRY, TARGET_PCT, 0.0)
    # ПРАВКА §А 2.4: уровень БЕРЁТСЯ из строки позиции — это самый точный способ
    # повторить случившееся, — а ``levels`` стоит на пути расчёта ПРОВЕРКОЙ.
    body = code_only(inspect.getsource(plus.assert_levels_match_row))
    assert "position_rules.levels(" in body
    assert "assert_levels_match_row(" in code_only(
        inspect.getsource(plus.resolve_variant)
    )
    rows = resolve(TARGET_ON_FIRST_DAY)
    for variant in plus.variant_names():
        assert rows[variant].exit_price == pytest.approx(102.0)


def test_variants_without_a_stop_never_produce_a_stop_outcome() -> None:
    """Следствие снятия предела, проверенное на ряде, где предел точно задет."""
    rows = resolve(KEEPS_FALLING)
    for variant in ("A_nostop_24", "B_nostop_plus_48", "D_nostop_plus_48_gross"):
        assert rows[variant].exit_reason not in ("stop", "ambiguous")


def test_the_holding_order_is_a_consequence_and_it_is_checked() -> None:
    """Пять неравенств §``HOLDING_ORDER`` — следствие устройства вариантов."""
    for series in (DIP_THEN_BREAKEVEN, KEEPS_FALLING, GROSS_ONLY_THEN_TARGET,
                   TARGET_ON_FIRST_DAY):
        rows = resolve(series)
        for narrow, wide in plus.HOLDING_ORDER:
            assert rows[wide].held_hours >= rows[narrow].held_hours - 1e-9


def test_the_holding_order_check_actually_refuses() -> None:
    """Проверка порядка удержания обязана падать на подделанной паре."""
    rows = resolve(DIP_THEN_BREAKEVEN)
    faked = dict(rows)
    faked["B_nostop_plus_48"] = plus.PlusOutcome(
        variant="B_nostop_plus_48", exit_reason="plus_exit",
        exit_bar_ts=OPENED, closed_at=OPENED, exit_price=100.22,
        net_pnl_pct=0.0, net_pnl_usd=0.0, held_hours=0.0, bars_used=1,
        resolution="1m",
    )
    with pytest.raises(AssertionError, match="по устройству вариантов"):
        plus.check_holding_order(faked)


# =============================================================================
# §5.4 ТЗ. Никакого подглядывания вперёд
# =============================================================================

def test_a_bar_counts_as_available_only_after_it_has_closed() -> None:
    """§5.4 ТЗ: бар годен, только если ЗАКРЫЛСЯ строго раньше момента."""
    ts = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert plus.bar_is_closed(ts, ts + timedelta(seconds=61), "1m") is True
    assert plus.bar_is_closed(ts, ts + timedelta(seconds=60), "1m") is True
    assert plus.bar_is_closed(ts, ts + timedelta(seconds=59), "1m") is False
    assert plus.bar_is_closed(ts, ts + timedelta(seconds=3601), "1h") is True
    assert plus.bar_is_closed(ts, ts + timedelta(seconds=3599), "1h") is False


def test_the_window_boundary_does_not_use_the_collector_delay() -> None:
    """УРОК ЭТАПА 9.1.5, ЗАПИСАННЫЙ ПРОВЕРКОЙ (§5.4 ТЗ).

    ``settle_seconds()`` равна 3900 секундам и отвечает на вопрос «перестал ли
    коллектор перезаписывать бар», а не «закрылся ли бар». Поставленная на
    границу окна, она объявила бы незакрытым любой бар в пределах часа с лишним
    до границы — и запрет на подглядывание перестал бы что-либо запрещать.
    """
    from src.barrier.runner import settle_seconds

    assert settle_seconds() == 3900
    boundary = code_only(inspect.getsource(plus.bar_is_closed))
    assert "settle_seconds" not in boundary
    assert "settle_seconds" not in code_only(
        inspect.getsource(plus.closed_bars)
    )
    assert "settle_seconds" not in code_only(
        inspect.getsource(plus.covers_full_window)
    )
    assert "bar_step" in boundary


def test_an_unclosed_bar_is_dropped_and_the_guard_confirms_it() -> None:
    """Отбор роняет незакрытый бар, а НЕЗАВИСИМАЯ проверка это подтверждает."""
    now = OPENED + timedelta(minutes=10, seconds=30)
    bars = bars_from(_hold(99.90, 99.50, 99.80, 12))
    kept = plus.closed_bars(bars, now, "1m")
    assert [bar.ts for bar in kept] == [
        OPENED + timedelta(minutes=i) for i in range(10)
    ]
    plus.assert_closed_bars(kept, now, "1m")
    with pytest.raises(ValueError, match="НЕЗАКРЫТЫЙ бар"):
        plus.assert_closed_bars(bars, now, "1m")


def test_the_loader_both_filters_and_checks() -> None:
    """Отбор и проверка обязаны стоять В ОДНОМ месте чтения ряда.

    Проверка, живущая в файле, но не вызываемая на пути расчёта, — это дыра,
    неотличимая от её отсутствия.
    """
    body = code_only(inspect.getsource(plus._load_bars))
    assert "closed_bars(" in body
    assert "assert_closed_bars(" in body


# =============================================================================
# §5.2–5.3 ТЗ. Отбраковка insufficient_future_bars
# =============================================================================

def test_a_position_younger_than_48_hours_is_not_covered() -> None:
    """§5.3 ТЗ: главный ограничитель выборки, названный числом."""
    now = OPENED + timedelta(hours=48, seconds=61)
    assert plus.covers_full_window(OPENED, now, "1m") is True
    assert plus.covers_full_window(OPENED, now - timedelta(seconds=2), "1m") is False
    assert plus.covers_full_window(
        OPENED, OPENED + timedelta(hours=47), "1m"
    ) is False


def test_the_coverage_guard_refuses_a_position_that_has_no_future() -> None:
    """Та же величина проверкой: она и падает при снятой отбраковке."""
    plus.assert_sample_covered(OPENED, OPENED + timedelta(hours=48, seconds=61), "1m")
    with pytest.raises(ValueError, match="не покрывает"):
        plus.assert_sample_covered(OPENED, OPENED + timedelta(hours=30), "1m")


# =============================================================================
# §3.3–3.4 ТЗ. Граница фаз
# =============================================================================

def test_the_phase_boundary_is_the_positions_own_deadline() -> None:
    """Граница первых суток берётся ИЗ ПОЗИЦИИ, а не считается заново.

    Контроль обязан воспроизвести ФАКТ, а факт закрыт по
    ``positions.deadline_at``. Свой пересчёт разошёлся бы с записанным молча.
    """
    first, second = plus.phase_deadlines(OPENED, DEADLINE_24)
    assert first == DEADLINE_24
    assert second == DEADLINE_48
    body = code_only(inspect.getsource(plus.phase_deadlines))
    assert "fact_deadline_at" in body


def test_a_position_whose_horizon_is_not_a_day_stops_the_calculation() -> None:
    """Посылка §3 ТЗ: правило описано для СУТОК, и это проверяется."""
    with pytest.raises(ValueError, match="граница первых суток"):
        resolve(KEEPS_FALLING, fact_deadline_at=OPENED + timedelta(hours=12))


# =============================================================================
# §9.1 ТЗ. Контроль
# =============================================================================

def _fact(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1, "exit_reason": "stop",
        "closed_at": OPENED + timedelta(minutes=1),
        "exit_price": 99.0, "net_pnl_pct": -1.22,
    }
    row.update(over)
    return row


def test_the_control_variant_reproduces_the_recorded_fact() -> None:
    """Контроль сходится с фактом по всем четырём величинам."""
    control = resolve(DIP_THEN_BREAKEVEN)["control"]
    assert plus.compare_control(_fact(), control) == []


def test_the_recorded_timeout_is_read_as_timeout_24_and_not_as_a_mismatch() -> None:
    """Правило пишет одно ``timeout``; замер делит его на два — и сводит обратно.

    Приведение имён — не поблажка: допуск сверки остаётся нулевым, меняется
    только то, ЧТО с чем сравнивается. Ровно так же на Этапе 9.1.3 пришлось
    свести открытие бара с его закрытием.
    """
    assert plus.control_reason_of_fact("timeout") == "timeout_24"
    assert plus.control_reason_of_fact("stop") == "stop"
    control = resolve(GROSS_ONLY_THEN_TARGET)["control"]
    assert control.exit_reason == "timeout_24"
    assert plus.compare_control(
        _fact(exit_reason="timeout", closed_at=DEADLINE_24,
              exit_price=99.80, net_pnl_pct=-0.42),
        control,
    ) == []


def test_the_closing_moment_is_reused_and_not_written_a_second_time() -> None:
    """§2.1 ТЗ: приведение к ``closed_at`` — готовая функция, а не своя копия.

    ГРАБЛИ 9.1.3: правило возвращает время ОТКРЫТИЯ бара выхода, а живой сервис
    пишет время его ЗАКРЫТИЯ. Первая редакция сравнивала одно с другим напрямую
    и получила одиннадцать ложных расхождений ровно по 60 секунд.
    """
    from src.shadow.trailing import closing_moment

    assert plus.closing_moment is closing_moment
    body = code_only(_SCRIPT.read_text(encoding="utf-8"))
    assert "def closing_moment" not in body
    assert "timedelta(seconds=60)" not in body


def test_the_closing_rule_still_matches_the_live_runner() -> None:
    """Живой сервис по-прежнему пишет ``closed_at`` = закрытие бара выхода.

    Проверка смотрит в ``src/positions/runner.py``: разъедься эта договорённость
    — и контроль начал бы расходиться на 60 секунд, а причина была бы не в
    замере.
    """
    runner = code_only(
        (_ROOT / "src" / "positions" / "runner.py").read_text(encoding="utf-8")
    )
    assert "decision.exit_bar_ts + timedelta(seconds=60)" in runner


def test_a_mismatch_in_any_of_the_four_values_is_reported() -> None:
    """Расходится любая из четырёх величин — контроль обязан её назвать."""
    control = resolve(DIP_THEN_BREAKEVEN)["control"]
    assert plus.compare_control(_fact(exit_reason="target"), control)
    assert plus.compare_control(_fact(closed_at=OPENED), control)
    assert plus.compare_control(_fact(exit_price=99.5), control)
    assert plus.compare_control(_fact(net_pnl_pct=-1.0), control)


def test_an_unmeasured_control_is_a_mismatch_and_says_so_in_words() -> None:
    """Живое правило не дало исхода, а в базе исход есть — это расхождение."""
    problems = plus.compare_control(_fact(), None)
    assert problems == ["живое правило не дало исхода на прочитанном ряде свечей"]


def test_the_control_tolerance_is_eight_digits_after_storage_rounding() -> None:
    """§9.1 ТЗ: допуск 1e-8, но сравниваются величины ОДНОЙ точности.

    Колонка ``positions.net_pnl_pct`` имеет тип NUMERIC(12,6) и хранит шесть
    знаков. Сравнивать с ней пересчитанный ``float`` напрямую до восьмого знака
    значило бы объявлять расхождением ОКРУГЛЕНИЕ ХРАНЕНИЯ — тот же вид ложного
    расхождения, что стоил Этапу 9.1.3 одиннадцати несовпадений по 60 секунд.
    Поэтому пересчёт приводится к точности хранения, и уже приведённое
    сверяется с допуском восьми знаков.
    """
    assert plus.CONTROL_TOLERANCE == 1e-8
    assert plus.PCT_STORAGE_PLACES == 6
    # Разница за седьмым знаком — это округление хранения, а не расхождение.
    assert not plus._differs(-1.2200004567, -1.220000, 6)
    # Разница в шестом знаке — уже расхождение, и допуск её не прощает.
    assert plus._differs(-1.220003, -1.220000, 6)
    # И тем более в третьем.
    assert plus._differs(-1.221, -1.220000, 6)


# =============================================================================
# §6 ТЗ. Числа
# =============================================================================

def _shadow(**over: Any) -> plus.PlusOutcome:
    row: dict[str, Any] = {
        "variant": "B_nostop_plus_48", "exit_reason": "timeout_48",
        "exit_bar_ts": DEADLINE_48, "closed_at": DEADLINE_48,
        "exit_price": 98.0, "net_pnl_pct": -2.22, "net_pnl_usd": -0.0444,
        "held_hours": 48.0, "bars_used": 100, "resolution": "1m",
    }
    row.update(over)
    return plus.PlusOutcome(**row)


def test_number_six_counts_only_those_in_the_red_at_the_24_hour_mark() -> None:
    """ЧИСЛО 6 (§6 ТЗ): группа — «жива на 24 часах И в чистом минусе».

    Сделка, дошедшая до цели раньше суток, в группу не входит: спрашивать про её
    положение на 24 часах бессмысленно. Сделка, стоявшая на 24 часах в ПЛЮСЕ, не
    входит тоже — вопрос владельца был про минус.
    """
    shadows = {
        # В минусе и вернулась через 6 часов.
        1: {
            "A_nostop_24": _shadow(
                variant="A_nostop_24", exit_reason="timeout_24",
                net_pnl_pct=-2.22, closed_at=DEADLINE_24, held_hours=24.0,
            ),
            "B_nostop_plus_48": _shadow(
                exit_reason="plus_exit", net_pnl_pct=0.0,
                closed_at=DEADLINE_24 + timedelta(hours=6), held_hours=30.0,
            ),
        },
        # В минусе и не вернулась: −2.22 на 24 часах, −3.22 на 48.
        2: {
            "A_nostop_24": _shadow(
                variant="A_nostop_24", exit_reason="timeout_24",
                net_pnl_pct=-2.22, closed_at=DEADLINE_24, held_hours=24.0,
            ),
            "B_nostop_plus_48": _shadow(net_pnl_pct=-3.22),
        },
        # На 24 часах В ПЛЮСЕ — в группу не входит.
        3: {
            "A_nostop_24": _shadow(
                variant="A_nostop_24", exit_reason="timeout_24",
                net_pnl_pct=+0.30, closed_at=DEADLINE_24, held_hours=24.0,
            ),
            "B_nostop_plus_48": _shadow(net_pnl_pct=+0.10),
        },
        # Закрыта по цели в первые сутки — в группу не входит.
        4: {
            "A_nostop_24": _shadow(
                variant="A_nostop_24", exit_reason="target",
                net_pnl_pct=+1.78, closed_at=OPENED + timedelta(hours=3),
                held_hours=3.0,
            ),
            "B_nostop_plus_48": _shadow(
                exit_reason="target", net_pnl_pct=+1.78,
                closed_at=OPENED + timedelta(hours=3), held_hours=3.0,
            ),
        },
    }
    block = plus.recovery_summary(
        shadows, [1, 2, 3, 4], deadlines=dict.fromkeys((1, 2, 3, 4), DEADLINE_24)
    )
    assert block["in_minus_at_24h"] == 2
    assert block["returned"] == 1
    assert block["return_hours_median"] == pytest.approx(6.0)
    assert block["return_hours_max"] == pytest.approx(6.0)
    assert block["stayed"] == 1
    assert block["stayed_mean_at_24h"] == pytest.approx(-2.22)
    assert block["stayed_mean_at_48h"] == pytest.approx(-3.22)


def test_the_return_time_is_counted_from_the_24_hour_mark() -> None:
    """Вопрос владельца — «сколько ждать», а не «сколько всего висела сделка»."""
    shadows = {
        1: {
            "A_nostop_24": _shadow(
                variant="A_nostop_24", exit_reason="timeout_24",
                net_pnl_pct=-1.0, closed_at=DEADLINE_24, held_hours=24.0,
            ),
            "B_nostop_plus_48": _shadow(
                exit_reason="plus_exit", net_pnl_pct=0.0,
                closed_at=DEADLINE_24 + timedelta(hours=13, minutes=30),
                held_hours=37.5,
            ),
        },
    }
    block = plus.recovery_summary(shadows, [1], deadlines={1: DEADLINE_24})
    assert block["return_hours_median"] == pytest.approx(13.5)


def test_the_bootstrap_interval_is_reproducible_and_says_nothing_on_nothing() -> None:
    """§6.2 ТЗ: зерно закреплено, а интервал от нуля наблюдений НЕ выдумывается."""
    values = [-1.22, -2.22, 1.78, 0.0, -0.42, 1.78, -3.10, 0.0, 1.78, -1.22]
    first = plus.bootstrap_mean_ci(values)
    second = plus.bootstrap_mean_ci(values)
    assert first == second
    assert first is not None
    assert first[0] < sum(values) / len(values) < first[1]
    assert plus.bootstrap_mean_ci([]) is None


def test_overlapping_intervals_are_reported_as_such() -> None:
    """§6.2 ТЗ требует ЯВНОЙ строки про пересечение, а не взгляда на числа."""
    assert plus.intervals_overlap((-1.0, 1.0), (0.5, 2.0)) is True
    assert plus.intervals_overlap((-1.0, -0.5), (0.5, 2.0)) is False
    assert plus.intervals_overlap((0.5, 2.0), (-1.0, -0.5)) is False
    assert plus.intervals_overlap(None, (0.0, 1.0)) is None


def test_unmeasured_outcomes_never_enter_a_mean() -> None:
    """Подставить ``no_data`` ноль значило бы утверждать «вариант не изменил итог»."""
    shadows = {
        1: {name: _shadow(variant=name) for name in plus.variant_names()},
        2: {
            name: plus.PlusOutcome(
                variant=name, exit_reason="no_data", exit_bar_ts=None,
                closed_at=None, exit_price=None, net_pnl_pct=None,
                net_pnl_usd=None, held_hours=None, bars_used=0,
                resolution="1m",
            )
            for name in plus.variant_names()
        },
    }
    rows = plus.money_summary(shadows, [1, 2])
    for row in rows:
        assert row["n"] == 1
        assert row["unmeasured"] == 1
        assert row["mean_pct"] == pytest.approx(-2.22)


def test_the_regime_warning_is_printed_as_text(capsys: Any) -> None:
    """§6.3 ТЗ: оговорка печатается ТЕКСТОМ, а не подразумевается."""
    plus.print_regime_warning()
    printed = capsys.readouterr().out
    assert "ОДИН РЕЖИМ РЫНКА" in printed


def test_a_small_sample_says_so_out_loud(capsys: Any) -> None:
    """§5.5 ТЗ: молчаливая выдача таблицы на пяти сделках запрещена."""
    assert plus.MIN_SAMPLE == 15
    plus.print_small_sample(5)
    printed = capsys.readouterr().out
    assert "ВЫБОРКА МАЛА, РАЗЛИЧИТЬ ВАРИАНТЫ НЕЛЬЗЯ" in printed


def test_the_script_never_recommends_a_variant() -> None:
    """§1.4 и §11.10 ТЗ: рекомендации внедрить вариант в выводе НЕТ."""
    text = _SCRIPT.read_text(encoding="utf-8")
    printed = "\n".join(re.findall(r'print\((.*)', text))
    for word in ("рекомендуем", "рекомендуется", "следует внедрить",
                 "лучший вариант", "предлагаем перейти"):
        assert word not in printed.lower()
    assert "НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ" in text


# =============================================================================
# §9.3 ТЗ. КОНТРОЛЬНЫЕ ОПЫТЫ. Каждый — настоящая правка кода, и каждый обязан
# уронить свою проверку. Опыт, прошедший молча, — это дыра в проверке, а не
# успех: так было трижды на Этапе 9.1.3.
# =============================================================================

def test_control_experiment_1_shifting_the_24_hour_boundary_by_one_bar() -> None:
    """ОПЫТ 1: граница суток сдвинута на один бар вперёд.

    Сдвиг переносит бар отметки 24 часов из фазы, где условие плюса
    проверяется, в фазу, где не проверяется. Числа при этом остаются
    правдоподобными — поэтому проверка границы стоит отдельно и падает.
    """
    broken = mutated(
        "    return fact_deadline_at, opened_at + timedelta(hours=TOTAL_HOURS)",
        "    return (fact_deadline_at + bar_step('1m'),\n"
        "            opened_at + timedelta(hours=TOTAL_HOURS))",
    )
    # Исправный код на этом же ряде исхода даёт.
    assert resolve(DIP_THEN_BREAKEVEN)["B_nostop_plus_48"].exit_reason == "plus_exit"
    with pytest.raises(ValueError, match="граница первых суток"):
        resolve_with(broken, DIP_THEN_BREAKEVEN)


def test_control_experiment_2_allowing_an_unclosed_bar_into_the_calculation() -> None:
    """ОПЫТ 2: отбор перестал выбрасывать незакрытый бар.

    Проверка на подглядывание написана ОТДЕЛЬНЫМ сравнением именно затем, чтобы
    не спрашивать разрешения у испорченного отбора.
    """
    broken = mutated(
        "    return [bar for bar in bars if bar_is_closed(bar.ts, now, resolution)]",
        "    return list(bars)",
    )
    now = OPENED + timedelta(minutes=10, seconds=30)
    bars = bars_from(_hold(99.90, 99.50, 99.80, 12))
    assert len(plus.closed_bars(bars, now, "1m")) == 10
    kept = broken.closed_bars(bars, now, "1m")
    assert len(kept) == 12
    with pytest.raises(ValueError, match="НЕЗАКРЫТЫЙ бар"):
        broken.assert_closed_bars(kept, now, "1m")


def test_control_experiment_3_using_the_bar_close_instead_of_the_breakeven() -> None:
    """ОПЫТ 3: цена выхода при ``plus_exit`` заменена на цену закрытия бара.

    Подмена не видна ни по одному исходу — меняется только число. Ловит её
    следствие: выход в безубыток обязан давать РОВНО ноль.
    """
    broken = mutated(
        "            exit_reason=EXIT_PLUS,\n"
        "            exit_bar_ts=plus_ts,\n"
        "            exit_price=plus_price,",
        "            exit_reason=EXIT_PLUS,\n"
        "            exit_bar_ts=plus_ts,\n"
        "            exit_price=next(b.close for b in second if b.ts == plus_ts),",
    )
    good = resolve(DIP_THEN_BREAKEVEN)["B_nostop_plus_48"]
    assert good.net_pnl_pct == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError, match="цена выхода не равна цене безубытка"):
        resolve_with(broken, DIP_THEN_BREAKEVEN)


def test_control_experiment_4_turning_variant_b_into_variant_d() -> None:
    """ОПЫТ 4: у варианта B снята проверка «плюс чистый» — то есть B стал D.

    ЭТО САМАЯ ТИХАЯ ИЗ ШЕСТИ ПОДМЕН. Проверка «выход в безубыток даёт ноль»
    её НЕ ловит: она спрашивает про мерку у той же испорченной строки и
    подтверждает ноль по валовой мерке. Ловит договор о вариантах — второе
    изложение таблицы §4 ТЗ.
    """
    broken = mutated(
        "    VariantSpec(B_NOSTOP_PLUS_48, keeps_stop=False, waits_plus=True, "
        "gross_plus=False),",
        "    VariantSpec(B_NOSTOP_PLUS_48, keeps_stop=False, waits_plus=True, "
        "gross_plus=True),",
    )
    # Исправный код: B и D — разные правила, и на этом ряде дают разный итог.
    good = resolve(DIP_THEN_BREAKEVEN)
    assert good["B_nostop_plus_48"].exit_price != good["D_nostop_plus_48_gross"].exit_price
    with pytest.raises(ValueError, match="валовой плюс считает не тот вариант"):
        broken.assert_variant_contract()
    with pytest.raises(ValueError, match="валовой плюс считает не тот вариант"):
        resolve_with(broken, DIP_THEN_BREAKEVEN)


def test_control_experiment_5_widening_the_control_tolerance_to_two_digits() -> None:
    """ОПЫТ 5: допуск контроля расширен с восьми знаков до двух.

    Расхождение в третьем знаке — это тысячная процента на сделку; расширенный
    допуск проглатывает его молча, и контроль перестаёт быть контролем.
    Расширять допуск запрещено (§9.1 ТЗ), и запрет проверяется.
    """
    broken = mutated("CONTROL_TOLERANCE = 1e-8", "CONTROL_TOLERANCE = 1e-2")
    control = resolve(DIP_THEN_BREAKEVEN)["control"]
    doctored = _fact(net_pnl_pct=-1.221)
    assert plus.compare_control(doctored, control), (
        "исправный контроль обязан заметить расхождение в третьем знаке"
    )
    assert broken.compare_control(doctored, control) == [], (
        "опыт не удался: расширенный допуск обязан был пропустить расхождение"
    )


def test_control_experiment_6_dropping_the_insufficient_future_bars_filter() -> None:
    """ОПЫТ 6: отбраковка непокрытых позиций снята.

    Позиция, открытая двадцать часов назад, не может быть посчитана по правилу
    со сроком 48 часов: у неё попросту нет будущего. Снятая отбраковка роняет
    независимую проверку покрытия — ту, что стоит на пути расчёта каждой
    позиции.
    """
    broken = mutated(
        "    return bar_is_closed(\n"
        "        opened_at + timedelta(hours=TOTAL_HOURS), now, resolution\n"
        "    )",
        "    return True",
    )
    young = OPENED + timedelta(hours=20)
    assert plus.covers_full_window(OPENED, young, "1m") is False
    assert broken.covers_full_window(OPENED, young, "1m") is True
    with pytest.raises(ValueError, match="не покрывает"):
        broken.assert_sample_covered(OPENED, young, "1m")


# =============================================================================
# ПРАВКА §А. Округление вычисленной цены — тем же способом, каким округляет база
# =============================================================================

# ТРИ БОЕВЫЕ ПОЗИЦИИ DOGE, НА КОТОРЫХ ВСТАЛ ХОЛОСТОЙ ПРОГОН 06.09.2026.
# instrument_id=285, exit_reason='target', target_pct=1.002750, cost_pct=0.220000.
# Пересчёт давал у всех трёх РОВНО 0.782750 = target_pct − cost_pct; факт
# отличался в шестом знаке, и сторона отличия совпадала со стороной округления
# цены цели колонкой NUMERIC(20,8). Числа взяты из §А ТЗ дословно.
DOGE_TARGET_PCT = 1.002750
DOGE_CASES: tuple[tuple[float, float, float], ...] = (
    #  цена входа, цель как она лежит в базе, записанный итог
    (0.08223, 0.08305456, 0.782748),   # округление ВНИЗ
    (0.08324, 0.08407469, 0.782751),   # округление ВВЕРХ
    (0.08730, 0.08817540, 0.782749),   # округление ВНИЗ
)


def doge_bars(target_price: float) -> list[Bar]:
    """Ряд, на котором цель берётся первым же баром и держится до срока."""
    high = target_price * 1.001
    return bars_from(
        [(high, target_price * 0.999, high)]
        + _hold(high, target_price, high, MINUTES - 1)
    )


def doge_fact(entry: float, target_price: float, net: float) -> dict[str, Any]:
    return {
        "id": 1, "exit_reason": "target",
        "closed_at": OPENED + timedelta(minutes=1),
        "exit_price": target_price, "net_pnl_pct": net,
    }


def test_the_storage_rounding_is_the_one_postgres_uses_not_pythons() -> None:
    """§А 2.1: способ округления взят ИЗ БАЗЫ, а не из головы.

    PostgreSQL округляет NUMERIC половиной ОТ НУЛЯ; встроенный ``round()``
    Python — половиной К ЧЁТНОМУ. На половинах они расходятся, и подставить
    ``round()`` значило бы завести второе, чуть другое округление рядом с
    базой. Числа ниже сверены запросом к настоящему PostgreSQL 16.
    """
    assert plus.PRICE_STORAGE_PLACES == 8
    assert plus.PCT_STORAGE_PLACES == 6
    # ``0.000000005::numeric(20,8)`` на настоящей базе даёт 0.00000001.
    assert plus.to_storage(0.000000005, 8) == pytest.approx(0.00000001)
    assert plus.to_storage(-0.000000005, 8) == pytest.approx(-0.00000001)
    assert plus.to_storage(0.000000015, 8) == pytest.approx(0.00000002)
    assert plus.to_storage(0.000000025, 8) == pytest.approx(0.00000003)
    # А ``round()`` на двух из них дал бы другое — потому его здесь и нет.
    assert round(0.000000015, 8) != plus.to_storage(0.000000015, 8)
    assert round(0.000000025, 8) != plus.to_storage(0.000000025, 8)


@pytest.mark.parametrize(("entry", "target_price", "net"), DOGE_CASES)
def test_the_three_live_mismatches_of_06_09_2026_now_reproduce_exactly(
    entry: float, target_price: float, net: float
) -> None:
    """§А 4.1: три позиции, остановившие боевой прогон, сходятся до знака.

    ЭТО ПРОВЕРКА НА ВОЗВРАТ ДЕФЕКТА, А НЕ НА ПРАВИЛО. Она держит ровно то
    место, где пересчёт разошёлся с фактом: цену цели, округлённую базой.
    """
    stored_target, stored_stop = stored_levels(entry, DOGE_TARGET_PCT, STOP_PCT)
    assert stored_target == pytest.approx(target_price, abs=1e-12)

    rows = plus.resolve_position(
        doge_bars(stored_target), entry_price=entry,
        target_pct=DOGE_TARGET_PCT, stop_pct=STOP_PCT,
        fact_target_price=stored_target, fact_stop_price=stored_stop,
        cost_pct=COST_PCT, notional_usd=SLOT, side="buy", opened_at=OPENED,
        fact_deadline_at=DEADLINE_24, resolution="1m",
    )
    control = rows["control"]
    assert control.exit_reason == "target"
    assert control.exit_price == pytest.approx(target_price, abs=1e-12)
    assert plus.to_storage(control.net_pnl_pct, 6) == pytest.approx(net)
    assert plus.compare_control(doge_fact(entry, target_price, net), control) == []

    # И то, что было до правки: цель без округления даёт РОВНО
    # target_pct − cost_pct у всех трёх — тот самый признак из §А 1 ТЗ.
    raw_target, _ = position_rules.levels(entry, DOGE_TARGET_PCT, STOP_PCT)
    raw_net = position_rules.net_pnl(entry, raw_target, COST_PCT)
    assert plus.to_storage(raw_net, 6) == pytest.approx(
        DOGE_TARGET_PCT - COST_PCT
    )
    assert plus.to_storage(raw_net, 6) != pytest.approx(net)


def test_the_breakeven_price_is_rounded_like_the_column_too() -> None:
    """§А 2.2: округляются ВСЕ три вычисляемые цены, включая Pbe.

    §3.6 основного ТЗ задавал Pbe формулой и про округление молчал. Цена
    безубытка — такая же вычисленная цена, как цель и предел, и оставить её
    неокруглённой значило бы мерить итог ``plus_exit`` более точной ценой, чем
    итог ``target``.
    """
    for entry in (0.08223, 0.08730, 100.0, 63_412.77):
        price = plus.breakeven_price(entry, COST_PCT, side="buy", gross=False)
        assert price == plus.to_storage(price, plus.PRICE_STORAGE_PLACES)
    # У DOGE округление видно глазом: 0.08223 × 1.0022 = 0.082410906.
    assert plus.breakeven_price(
        0.08223, COST_PCT, side="buy", gross=False
    ) == pytest.approx(0.08241091, abs=1e-12)
    # Валовой безубыток — сама цена входа, она уже в точности хранения.
    assert plus.breakeven_price(
        0.08223, COST_PCT, side="buy", gross=True
    ) == pytest.approx(0.08223)


def test_a_level_that_no_rule_could_have_produced_stops_the_calculation() -> None:
    """§А 2.4: уровень из строки берётся, но не принимается на веру.

    Допуск — ОДИН ТИК ХРАНЕНИЯ, и он не подобран, а выведен: при открытии
    позиции ``levels`` считалась от ``ohlcv.close`` (DOUBLE PRECISION), а
    ``positions.entry_price`` — это NUMERIC(20,8), то есть та же цена,
    округлённая. Сдвиг цены входа не больше полутика, множитель близок к
    единице, значит уровень сдвинется не больше чем на тик. Больше — значит
    уровень посчитан другим правилом.
    """
    stored_target, stored_stop = stored_levels(ENTRY, TARGET_PCT, STOP_PCT)
    tick = plus.storage_tick(plus.PRICE_STORAGE_PLACES)
    # Сдвиг ровно на тик объясним округлением цены входа — расчёт идёт дальше.
    plus.assert_levels_match_row(
        entry_price=ENTRY, target_pct=TARGET_PCT, stop_pct=STOP_PCT,
        fact_target_price=stored_target + tick, fact_stop_price=stored_stop,
    )
    # Сдвиг на сто тиков объяснения не имеет.
    with pytest.raises(ValueError, match="уровень цели"):
        plus.assert_levels_match_row(
            entry_price=ENTRY, target_pct=TARGET_PCT, stop_pct=STOP_PCT,
            fact_target_price=stored_target + 100 * tick,
            fact_stop_price=stored_stop,
        )
    with pytest.raises(ValueError, match="уровень предела"):
        plus.assert_levels_match_row(
            entry_price=ENTRY, target_pct=TARGET_PCT, stop_pct=STOP_PCT,
            fact_target_price=stored_target,
            fact_stop_price=stored_stop - 100 * tick,
        )


def test_the_guard_stands_on_the_path_of_every_variant() -> None:
    """Проверка уровней вызывается ИЗ расчёта, а не лежит рядом с ним."""
    assert "assert_levels_match_row(" in code_only(
        inspect.getsource(plus.resolve_variant)
    )


def test_control_experiment_8_dropping_the_rounding_of_a_computed_price() -> None:
    """ОПЫТ 8 (§А 3): у вычисленной цены выхода снято округление.

    ЭТО ВОЗВРАТ ИМЕННО ТОГО ДЕФЕКТА, что остановил боевой прогон: уровни снова
    считаются формулой в ``float``, минуя точность хранения. Контроль обязан
    упасть.

    И ВТОРАЯ ПОЛОВИНА ОПЫТА, РАДИ КОТОРОЙ §3 ТЗ ТРЕБУЕТ ЦЕНУ ПОРЯДКА 0.08: НА
    BTC ТОТ ЖЕ ДЕФЕКТ ПРОХОДИТ МОЛЧА. У монеты по 78 000 восьмой знак цены на
    семь порядков меньше, чем у монеты по 0.08, и в шестом знаке
    ``net_pnl_pct`` его не видно. Опыт, поставленный на BTC, был бы зелёным при
    внесённом дефекте — ровно тот случай, ради которого писан §9.3 основного ТЗ.
    """
    broken = mutated(
        "    stop_price = float(fact_stop_price) if spec.keeps_stop "
        "else NO_STOP_PRICE",
        "    target_price, _own = position_rules.levels(\n"
        "        entry_price, target_pct, stop_pct\n"
        "    )\n"
        "    stop_price = _own if spec.keeps_stop else NO_STOP_PRICE",
    )

    # --- на DOGE контроль падает на всех трёх боевых позициях ---
    for entry, target_price, net in DOGE_CASES:
        stored_target, stored_stop = stored_levels(
            entry, DOGE_TARGET_PCT, STOP_PCT
        )
        fact = doge_fact(entry, target_price, net)
        common: dict[str, Any] = {
            "entry_price": entry, "target_pct": DOGE_TARGET_PCT,
            "stop_pct": STOP_PCT, "fact_target_price": stored_target,
            "fact_stop_price": stored_stop, "cost_pct": COST_PCT,
            "notional_usd": SLOT, "side": "buy", "opened_at": OPENED,
            "fact_deadline_at": DEADLINE_24, "resolution": "1m",
        }
        bars = doge_bars(stored_target)
        assert plus.compare_control(
            fact, plus.resolve_position(bars, **common)["control"]
        ) == [], "исправный код обязан сойтись с фактом"
        problems = broken.compare_control(
            fact, broken.resolve_position(bars, **common)["control"]
        )
        assert problems, f"дефект не пойман на DOGE {entry}"
        assert any("net_pnl_pct" in problem for problem in problems)

    # --- на BTC ТОТ ЖЕ дефект проходит молча ---
    btc_entry = 78_000.0
    btc_target, btc_stop = stored_levels(btc_entry, DOGE_TARGET_PCT, STOP_PCT)
    btc_common: dict[str, Any] = {
        "entry_price": btc_entry, "target_pct": DOGE_TARGET_PCT,
        "stop_pct": STOP_PCT, "fact_target_price": btc_target,
        "fact_stop_price": btc_stop, "cost_pct": COST_PCT,
        "notional_usd": SLOT, "side": "buy", "opened_at": OPENED,
        "fact_deadline_at": DEADLINE_24, "resolution": "1m",
    }
    btc_bars = doge_bars(btc_target)
    btc_control = plus.resolve_position(btc_bars, **btc_common)["control"]
    btc_fact = {
        "id": 2, "exit_reason": "target",
        "closed_at": OPENED + timedelta(minutes=1),
        "exit_price": plus.to_storage(btc_control.exit_price, 8),
        "net_pnl_pct": plus.to_storage(btc_control.net_pnl_pct, 6),
    }
    assert broken.compare_control(
        btc_fact, broken.resolve_position(btc_bars, **btc_common)["control"]
    ) == [], (
        "опыт на BTC обязан пройти молча — в этом и состоит его вторая половина"
    )


def test_control_experiment_9_computing_the_result_from_percentages() -> None:
    """ОПЫТ 9 (§А 3): возвращён короткий путь «итог из процентов, минуя цены».

    КОРОТКОГО ПУТИ В КОДЕ НИКОГДА НЕ БЫЛО — равенство ``target_pct − cost_pct``
    выходило само, потому что цель не округлялась (см. опыт 8). Здесь он
    внесён ЯВНО: итог считается из процента, приведённого к точности хранения,
    а не из цены выхода. Контроль обязан упасть.
    """
    broken = mutated(
        "    if decision.exit_reason != position_rules.EXIT_TIMEOUT:\n"
        "        return _finish(",
        "    if decision.exit_reason != position_rules.EXIT_TIMEOUT:\n"
        "        if decision.exit_reason == position_rules.EXIT_TARGET:\n"
        "            _row = _finish(\n"
        "                variant=spec.name, exit_reason=decision.exit_reason,\n"
        "                exit_bar_ts=decision.exit_bar_ts,\n"
        "                exit_price=decision.exit_price,\n"
        "                entry_price=entry_price, cost_pct=cost_pct,\n"
        "                notional_usd=notional_usd, opened_at=opened_at,\n"
        "                resolution=resolution, bars_used=decision.bars_held,\n"
        "            )\n"
        "            return PlusOutcome(**{\n"
        "                **_row.__dict__,\n"
        "                'net_pnl_pct': target_pct - float(cost_pct),\n"
        "            })\n"
        "        return _finish(",
    )
    for entry, target_price, net in DOGE_CASES:
        stored_target, stored_stop = stored_levels(
            entry, DOGE_TARGET_PCT, STOP_PCT
        )
        common: dict[str, Any] = {
            "entry_price": entry, "target_pct": DOGE_TARGET_PCT,
            "stop_pct": STOP_PCT, "fact_target_price": stored_target,
            "fact_stop_price": stored_stop, "cost_pct": COST_PCT,
            "notional_usd": SLOT, "side": "buy", "opened_at": OPENED,
            "fact_deadline_at": DEADLINE_24, "resolution": "1m",
        }
        bars = doge_bars(stored_target)
        fact = doge_fact(entry, target_price, net)
        assert plus.compare_control(
            fact, plus.resolve_position(bars, **common)["control"]
        ) == []
        problems = broken.compare_control(
            fact, broken.resolve_position(bars, **common)["control"]
        )
        assert problems, f"короткий путь не пойман на DOGE {entry}"
        assert any("net_pnl_pct" in problem for problem in problems)


def test_the_result_is_computed_from_the_exit_price_and_not_from_percentages() -> None:
    """§А 2.3: итог считается ИЗ ЦЕН. Короткого пути в файле нет.

    Проверка смотрит в исполняемый код, минуя пояснения: разбор дефекта в
    заголовке файла цитирует ``target_pct − cost_pct``, и запретить объяснять,
    что случилось, было бы неверно.
    """
    body = code_only(_SCRIPT.read_text(encoding="utf-8"))
    assert "position_rules.net_pnl(entry_price, exit_price, cost_pct)" in body
    for shortcut in ("target_pct - cost_pct", "target_pct - float(cost_pct)",
                     "target_pct / 100.0 - cost_pct"):
        assert shortcut not in body


def test_control_experiment_zero_a_mutation_that_changes_nothing_fails() -> None:
    """ОПЫТ НАД ОПЫТАМИ: замена, не нашедшая своего куска, роняет сам опыт.

    Без этого все шесть опытов выше можно было бы «пройти», подменяя текст,
    которого в файле нет. Ровно так три проверки Этапа 9.1.3 прошли молча.
    """
    with pytest.raises(AssertionError, match="не нашёл своего куска"):
        mutated("этого куска в файле нет и быть не может", "что угодно")


# =============================================================================
# §9.4 ТЗ. Двойник базы не мягче настоящей
# =============================================================================

POSITION_ROW = {
    "id": 1, "instrument_id": INSTRUMENT, "symbol": "BTC/USDT", "base": "BTC",
    "logic_version": 5, "horizon_h": 24, "side": "buy", "status": "closed",
    "opened_at": OPENED, "deadline_at": DEADLINE_24,
    "closed_at": OPENED + timedelta(minutes=1),
    "entry_price": ENTRY, "notional_usd": SLOT, "target_pct": TARGET_PCT,
    "target_price": 102.0, "stop_pct": STOP_PCT, "stop_price": 99.0,
    "cost_pct": COST_PCT, "resolution": "1m", "exit_price": 99.0,
    "exit_reason": "stop", "net_pnl_pct": -1.22, "net_pnl_usd": -0.0244,
    "bars_held": 1, "outcome_certain": True,
}


class Pool(SchemaPool):
    """Двойник пула: ВЫПОЛНЯЕТ SQL, не фильтрует за базу, отдаёт свои колонки.

    ОБА ГРЕХА §9.4 ТЗ УЖЕ ВОСПРОИЗВОДИЛИСЬ В ЭТОМ ПРОЕКТЕ. Двойник, который
    отбирает строки сам, проверяет не запрос, а собственное представление о
    нём; двойник, отдающий свои ключи, пропускает ровно ту ошибку, на которой
    Этап 9.1.3 упал в бою (запрос называл колонку ``signal_ts``, а код читал
    ``ts``).
    """

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.positions = positions if positions is not None else [POSITION_ROW]
        self.saved: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM positions p" in sql:
            rows = [
                row for row in self.positions
                if row["status"] == "closed"
                and row["exit_reason"] != "data_gap"
                and (args[0] is None or row["opened_at"] >= args[0])
            ]
            return project(sql, rows)
        if "GROUP BY logic_version" in sql:
            out: dict[int, dict[str, Any]] = {}
            for row in self.positions:
                bucket = out.setdefault(
                    int(row["logic_version"]),
                    {"logic_version": int(row["logic_version"]),
                     "closed_total": 0, "data_gap": 0, "still_open": 0},
                )
                if row["status"] == "closed":
                    bucket["closed_total"] += 1
                    if row["exit_reason"] == "data_gap":
                        bucket["data_gap"] += 1
                else:
                    bucket["still_open"] += 1
            return [out[key] for key in sorted(out)]
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        if "to_regclass" in sql:
            return True
        if "FROM signals" in sql:
            return 0
        return None

    async def executemany(self, sql: str, rows: list[Any]) -> None:
        self._check(sql)
        self.writes.append(sql)
        self.saved.extend(rows)


def test_every_stage_query_passes_the_schema_check() -> None:
    """Каждый запрос этапа сверен с составом таблиц ИЗ ФАЙЛОВ МИГРАЦИЙ."""
    tables = schema()
    for name in (
        "count_positions_by_logic_version",
        "position_plus_exit_shadow_exists",
        "save_position_plus_exit_shadow",
        "get_positions_for_shadow",
        "count_blocked_signals",
        "get_ohlcv_bars",
    ):
        source = inspect.getsource(getattr(DB, name))
        for sql in re.findall(r'"""\n(\s+(?:SELECT|INSERT).*?);\n\s+"""',
                              source, re.S):
            check_sql_columns(sql, tables)


def test_the_schema_double_still_rejects_a_query_to_a_missing_column() -> None:
    """Опыт над двойником: он обязан ловить чужую колонку, а не пропускать её."""
    with pytest.raises(UndefinedColumn):
        check_sql_columns(
            "SELECT id, symbol FROM positions WHERE status = 'closed';"
        )
    with pytest.raises(UndefinedColumn):
        check_sql_columns("SELECT p.nonsense FROM positions p;")


def test_the_double_returns_the_columns_the_query_names() -> None:
    """§9.4 ТЗ: ответ содержит РОВНО те колонки, что перечислены в запросе."""
    source = inspect.getsource(DB.get_positions_for_shadow)
    sql = re.findall(r'"""\n(\s+SELECT.*?);\n\s+"""', source, re.S)[0]
    names = {alias for _, alias in output_columns(sql)}
    rows = project(sql, [POSITION_ROW])
    assert set(rows[0]) == names
    assert "symbol" in names


async def test_the_measurement_reads_and_writes_only_what_it_may() -> None:
    """§1.3 ТЗ: этап пишет в ОДНУ таблицу и не трогает таблицы факта."""
    pool = Pool()
    db_object = DB()
    db_object._pool = pool  # type: ignore[assignment]

    await db_object.count_positions_by_logic_version()
    await db_object.get_positions_for_shadow()
    await db_object.position_plus_exit_shadow_exists()
    await db_object.save_position_plus_exit_shadow([{
        "position_id": 1, "variant": "control", "logic_version": 5,
        "exit_reason": "stop", "exit_bar_ts": OPENED, "closed_at": OPENED,
        "exit_price": 99.0, "net_pnl_pct": -1.22, "net_pnl_usd": -0.0244,
        "held_hours": 0.0167, "bars_used": 1, "resolution": "1m",
    }])

    assert len(pool.writes) == 1
    written = pool.writes[0]
    assert "INSERT INTO position_plus_exit_shadow" in written
    for forbidden in ("INSERT INTO positions", "UPDATE positions",
                      "DELETE FROM positions", "INSERT INTO signals",
                      "UPDATE signals", "INSERT INTO ohlcv", "UPDATE ohlcv",
                      "INSERT INTO signal_targets", "UPDATE signal_targets"):
        for sql in pool.queries:
            assert forbidden not in sql


async def test_a_repeated_run_changes_neither_rows_nor_values() -> None:
    """§7.5 ТЗ: повторный прогон не двигает даже ``computed_at``.

    Условие ``WHERE`` при ``DO UPDATE`` — это и есть идемпотентность. Простой
    ``DO UPDATE`` без условия переписывал бы метку времени каждым прогоном, и
    требование выполнялось бы только на словах.
    """
    source = inspect.getsource(DB.save_position_plus_exit_shadow)
    assert "ON CONFLICT (position_id, variant, logic_version) DO UPDATE" in source
    assert "IS DISTINCT FROM EXCLUDED" in source
    tail = source.split("DO UPDATE SET", 1)[1]
    assert "WHERE position_plus_exit_shadow." in tail


# =============================================================================
# §7 ТЗ. Схема результатов
# =============================================================================

def test_the_migration_number_is_free_and_the_rollback_exists() -> None:
    """§2.3 ТЗ: номер 024 не занят, откат лежит рядом."""
    migrations = _ROOT / "db" / "migrations"
    assert _MIGRATION.exists()
    assert _ROLLBACK.exists()
    same_number = [
        path.name for path in migrations.glob("024_*.sql")
    ]
    assert sorted(same_number) == [
        "024_position_plus_exit_shadow.sql",
        "024_position_plus_exit_shadow_rollback.sql",
    ]


def test_the_primary_key_includes_the_logic_version() -> None:
    """§7.3 ТЗ: смешение версий должно быть невозможно, а не запрещено словами."""
    text = _MIGRATION.read_text(encoding="utf-8")
    assert "PRIMARY KEY (position_id, variant, logic_version)" in text


def test_the_migration_lists_the_reasons_the_calculation_can_produce() -> None:
    """Перечень исходов в базе сверен с перечнем в коде, а не написан на глаз."""
    text = _MIGRATION.read_text(encoding="utf-8")
    block = text.split("_reason_chk", 1)[1].split(";", 1)[0]
    for reason in plus.SHADOW_REASONS:
        assert f"'{reason}'" in block
    # 'data_gap' сюда попасть не может: такие позиции в выборку не берутся.
    assert "'data_gap'" not in block


def test_the_migration_keeps_an_unmeasured_outcome_looking_unmeasured() -> None:
    """§7.2 ТЗ: пустая форма разрешена ТОЛЬКО для ``no_data``. Нуль — не измерение."""
    text = _MIGRATION.read_text(encoding="utf-8")
    shape = text.split("_shape_chk", 1)[1].split("END IF", 1)[0]
    assert "exit_reason = 'no_data'" in shape
    for column in ("exit_bar_ts", "closed_at", "exit_price", "net_pnl_pct",
                   "net_pnl_usd", "held_hours"):
        assert f"{column} IS NULL" in shape
        assert f"{column} IS NOT NULL" in shape


def test_the_migration_writes_down_what_each_variant_can_and_cannot_produce() -> None:
    """Следствия устройства вариантов записаны ограничением, а не обещанием."""
    text = _MIGRATION.read_text(encoding="utf-8")
    block = text.split("_shape_variant_chk", 1)[1].split("END IF", 1)[0]
    assert "'stop', 'ambiguous'" in block
    assert "'plus_exit', 'timeout_48'" in block
    assert "'timeout_24'" in block


def test_the_measurement_never_touches_the_tables_of_fact() -> None:
    """§1.3 ТЗ, записанное в самой миграции: внешний ключ смотрит НАРУЖУ."""
    text = _MIGRATION.read_text(encoding="utf-8")
    assert "REFERENCES positions(id) ON DELETE CASCADE" in text
    for forbidden in ("ALTER TABLE positions", "ALTER TABLE signals",
                      "ALTER TABLE ohlcv", "ALTER TABLE signal_targets",
                      "UPDATE positions", "DELETE FROM positions"):
        assert forbidden not in text
    rollback = _ROLLBACK.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS position_plus_exit_shadow" in rollback
    for forbidden in ("DROP TABLE IF EXISTS positions", "DELETE FROM positions",
                      "DROP TABLE IF EXISTS signals", "DROP TABLE IF EXISTS ohlcv"):
        assert forbidden not in rollback


# =============================================================================
# §1 ТЗ. Граница этапа
# =============================================================================

def test_the_stage_changes_no_live_rule() -> None:
    """§1.1 ТЗ 9.1.6: замер не трогает ни порогов, ни весов, ни своей таблицы.

    ЧТО ЗДЕСЬ ПОМЕНЯЛОСЬ И ПОЧЕМУ (Этап 9.2). Проверки «в боевом правиле нет ни
    слова plus_exit» стояли здесь по делу: ЭТАП 9.1.6 БЫЛ ЗАМЕРОМ, и попадание
    его правила в бой означало бы внедрение, которого никто не заказывал.

    Этап 9.2 — ВНЕДРЕНИЕ по решению владельца, и ровно этих слов в боевом коде
    теперь ждут. Требовать их отсутствия дальше значило бы требовать, чтобы
    принятое решение не было выполнено. Что осталось верным навсегда и потому
    проверяется здесь по-прежнему: ЗАМЕРНЫЙ СКРИПТ пишет только в свою таблицу,
    а боевой сервис о ней по-прежнему не знает — иначе результат замера начал
    бы влиять на ведение живых позиций.
    """
    from src.core.config import settings

    assert settings.LOGIC_VERSION == CURRENT_LOGIC_VERSION
    assert settings.BARRIER_STOP_PCT == 1.0
    assert settings.RISK_COST_ROUNDTRIP_PCT == 0.22
    runner_text = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "position_plus_exit_shadow" not in runner_text
    rules_text = (_ROOT / "src" / "positions" / "rules.py").read_text(
        encoding="utf-8"
    )
    assert "position_plus_exit_shadow" not in rules_text


def test_the_stage_writes_its_own_rule_nowhere() -> None:
    """§1.2 ТЗ: правило выхода НЕ переписано — только вызовы ``check_exit``."""
    body = code_only(_SCRIPT.read_text(encoding="utf-8"))
    assert "position_rules.check_exit(" in body
    # Своего сравнения касания в файле нет ни в одном виде.
    for own_rule in ("bar.high >=", "bar.low <=", ".high >= target",
                     ".low <= stop"):
        assert own_rule not in body


def test_the_calculation_of_an_outcome_is_a_pure_function() -> None:
    """Ни базы, ни сети, ни ``datetime.now()`` внутри правила (§8 ТЗ).

    Это условие проверяемости: правило проверяется на придуманных рядах с
    заранее известным ответом, а не на том, что случайно оказалось в базе.
    """
    for function in (plus.resolve_position, plus.resolve_variant,
                     plus.breakeven_price, plus.phase_deadlines,
                     plus.recovery_summary, plus.money_summary):
        body = code_only(inspect.getsource(function))
        assert "datetime.now" not in body
        assert "await " not in body
        assert "db." not in body


def test_the_exit_codes_are_the_ones_the_assignment_names() -> None:
    """§8.3 ТЗ: 0 — досчитал, 2 — сработала защита, 3 — данных не хватило."""
    body = _SCRIPT.read_text(encoding="utf-8")
    assert "return 3" in body
    assert "return 2" in body
    assert "return 0" in body
    assert "MEMORY_CAP_MB = 1024.0" in body


def test_the_peak_memory_is_measured_by_the_same_yardstick_as_9_1_4() -> None:
    """§8.4 ТЗ: пиковая память печатается, и мерка та же, не своя."""
    import scripts.stop_counterfactual_9_1_4 as stopcf

    assert plus.peak_rss_mb is stopcf.peak_rss_mb
    assert plus.peak_rss_mb() > 0.0
