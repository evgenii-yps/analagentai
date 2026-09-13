"""ЭТАП 9.2: снятие предела убытка и выход в плюс после 24 часов. ВНЕДРЕНИЕ.

ЧЕМ ЭТОТ НАБОР ОТЛИЧАЕТСЯ ОТ НАБОРА 9.1.6. Тот проверял ЗАМЕР: считает ли
скрипт то, что обещал. Здесь проверяется ВНЕДРЕНИЕ: делает ли БОЕВОЙ сервис
ровно то, что насчитал вариант ``B_nostop_plus_48`` замера, и не задел ли он при
этом ни одной строки версии 5.

ГЛАВНЫЙ ТЕСТ ФАЙЛА — :func:`test_the_live_rule_matches_variant_b_bar_for_bar`
(§2.3 ТЗ). Он прогоняет по ОДНИМ И ТЕМ ЖЕ свечам боевой путь закрытия и
вариант B счётного скрипта и сравнивает исход, момент и цену выхода. Расхождение
— падение теста: боевое правило обязано соответствовать проверенной реализации,
а не «примерно ей».

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ, а не выдумывают ответ. ``AT_TEST_DSN`` обязан указывать на
ОДНОРАЗОВУЮ базу: тесты пишут и удаляют строки.
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from src.core.config import Settings, settings
from src.export.transform import (
    build_position_close_note_tail,
    build_position_note,
    position_hold_hours,
)
from src.positions import messages
from src.positions.rules import (
    EXIT_PLUS,
    EXIT_REASONS,
    EXIT_TARGET,
    EXIT_TIMEOUT,
    NO_STOP_PRICE,
    PLUS_WAIT_MIN_LOGIC_VERSION,
    PRICE_STORAGE_PLACES,
    Bar,
    breakeven_price,
    check_exit_plus_wait,
    levels,
    net_pnl,
    refusal_key,
    target_price_of,
    to_storage,
)
from src.positions.runner import hold_hours, plus_start_of, without_stop
from tests.conftest import CURRENT_LOGIC_VERSION

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "db" / "migrations"

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(ОДНОРАЗОВАЯ база, тест пишет и удаляет строки)"
    ),
)

# --- Синтетика: круглые числа, чтобы ответ был виден глазом -----------------
_ENTRY = 100.0
_COST_PCT = 0.22
_TARGET_PCT = 1.0
_OPENED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_PLUS_START = _OPENED + timedelta(hours=24)
_DEADLINE = _OPENED + timedelta(hours=48)


def _bar(minute: int, high: float, low: float, close: float) -> Bar:
    """Свеча на ``minute`` минут позже входа."""
    return Bar(ts=_OPENED + timedelta(minutes=minute), high=high, low=low,
               close=close)


def _series(*bars: Bar) -> list[Bar]:
    return sorted(bars, key=lambda item: item.ts)


def _flat_window(start_min: int, end_min: int, price: float) -> list[Bar]:
    """Ряд «ничего не происходит» по одному бару в час: цена стоит на месте."""
    return [
        _bar(minute, price, price, price)
        for minute in range(start_min, end_min, 60)
    ]


# =============================================================================
# §3 ТЗ. ПАРАМЕТРЫ: значения, имена и СВЯЗАННОСТЬ
# =============================================================================

def test_the_two_new_settings_carry_the_values_named_in_the_specification() -> None:
    """§3.1 ТЗ: ровно эти имена и ровно эти значения по умолчанию."""
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_MAX_HOLD_HOURS == 48
    assert fresh.POSITION_PLUS_WAIT_START_HOURS == 24


def test_the_signal_horizon_is_not_touched() -> None:
    """§3.3 ТЗ: ``horizon_h`` — горизонт СИГНАЛА, а не срок жизни позиции.

    До версии 6 два числа совпадали, и оттого выглядели одним параметром. Ровно
    поэтому их легко было бы «привести в соответствие» — и тем самым молча
    изменить горизонт, на котором система оценивает сигналы.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_HORIZON_H == 24
    assert fresh.POSITION_MAX_HOLD_HOURS != fresh.POSITION_HORIZON_H


def test_the_wait_must_start_strictly_before_the_deadline() -> None:
    """§3.4 ТЗ: связь двух настроек закреплена ПРОВЕРКОЙ НА СТАРТЕ.

    Сравняй начало ожидания со сроком — и вторая фаза схлопнется в точку: исход
    ``plus_exit`` не случился бы НИ РАЗУ, а журнал показывал бы законный
    ``timeout`` и выглядел бы совершенно нормально. Это тот же вид молчаливой
    поломки, что и связка Задачи А Этапа 9.1, и лечится он так же — отказом
    запуска, а не комментарием.
    """
    with pytest.raises(ValueError, match="фазы ожидания плюса НЕ СУЩЕСТВУЕТ"):
        Settings(POSTGRES_PASSWORD="x", POSITION_PLUS_WAIT_START_HOURS=48)
    with pytest.raises(ValueError, match="фазы ожидания плюса НЕ СУЩЕСТВУЕТ"):
        Settings(POSTGRES_PASSWORD="x", POSITION_PLUS_WAIT_START_HOURS=72)
    with pytest.raises(ValueError, match="больше нуля"):
        Settings(POSTGRES_PASSWORD="x", POSITION_PLUS_WAIT_START_HOURS=0)
    # Законное сочетание проходит: проверка не запрещает менять числа вовсе.
    assert Settings(
        POSTGRES_PASSWORD="x",
        POSITION_PLUS_WAIT_START_HOURS=12,
        POSITION_MAX_HOLD_HOURS=36,
    ).POSITION_PLUS_WAIT_START_HOURS == 12


def test_the_logic_version_is_raised_to_six() -> None:
    """§1.1 ТЗ: версия логики поднята 5 → 6, и это решение владельца.

    ВЕРСИЯ С ТЕХ ПОР УШЛА ДАЛЬШЕ (действующая — в ``CURRENT_LOGIC_VERSION``), и
    проверять здесь равенство шести значило бы запрещать любой следующий
    законный подъём. Проверяется то, что этот этап действительно утверждал:
    версия НЕ НИЖЕ шести, а граница правила выхода — ровно шесть, потому что
    именно с неё предела убытка не стало.
    """
    assert settings.LOGIC_VERSION >= 6
    assert settings.LOGIC_VERSION == CURRENT_LOGIC_VERSION
    assert PLUS_WAIT_MIN_LOGIC_VERSION == 6


def test_the_version_boundary_lives_in_the_pure_rules_module() -> None:
    """Граница версий — свойство КОДА, а не настройки.

    Настройку можно поменять в ``.env``; «с какой версии не стало предела» —
    нельзя: это то же по природе утверждение, что и перечень исходов.
    """
    assert without_stop(6) and without_stop(7)
    assert not without_stop(5) and not without_stop(1)
    assert hold_hours(6) == settings.POSITION_MAX_HOLD_HOURS == 48
    assert hold_hours(5) == settings.POSITION_HORIZON_H == 24


# =============================================================================
# §4.3 ТЗ. Цель без предела — НЕ ослабляя проверку levels
# =============================================================================

def test_levels_still_refuses_a_non_positive_stop() -> None:
    """§9.4 ТЗ: проверка ``stop_pct <= 0`` в ``levels`` не ослаблена.

    Она бережёт версию 5, у которой предел есть всегда. Обойти её подстановкой
    правдоподобного ``stop_pct`` было бы худшим из решений: вычисленный, но
    никогда не срабатывающий предел — это данные, которые лгут (§4.1 ТЗ).
    """
    for bad in (0.0, -0.0, -1.0):
        with pytest.raises(ValueError, match="предел должен быть положительным"):
            levels(_ENTRY, _TARGET_PCT, bad)


def test_the_target_of_version_six_uses_the_very_same_formula() -> None:
    """§4.3 ТЗ: цель считается той же формулой, только другой функцией.

    Разойдись эти два числа — и позиции двух версий получали бы разные цели от
    одной и той же цены входа, оставаясь при этом одинаково правдоподобными.
    """
    for entry in (100.0, 77602.7, 0.214370, 1.4161):
        for pct in (0.19, 0.5, 1.0, 2.75):
            with_stop, _ = levels(entry, pct, 1.0)
            assert target_price_of(entry, pct) == with_stop
    with pytest.raises(ValueError, match="цена входа"):
        target_price_of(0.0, 1.0)


# =============================================================================
# §2.2 ТЗ. Цена безубытка: та же формула и ТО ЖЕ округление
# =============================================================================

def test_the_breakeven_price_is_rounded_the_way_the_database_rounds() -> None:
    """§2.2 и §9.5 ТЗ: ``Decimal`` с ``ROUND_HALF_UP``, а не ``round()``.

    PostgreSQL округляет NUMERIC ПОЛОВИНОЙ ОТ НУЛЯ, встроенный ``round()``
    Python — половиной к чётному. На половинах они расходятся, и расходятся
    молча: оба числа выглядят правдоподобно.
    """
    # ПОЛОВИНА БЕРЁТСЯ ТОЧНО ПРЕДСТАВИМАЯ В ДВОИЧНОЙ ДРОБИ, и это не придирка:
    # десятичный литерал вроде 0.000000005 в double лежит чуть ВЫШЕ настоящей
    # половины, и на нём оба способа округляют вверх — разницы не видно, и тест
    # доказывал бы, что её нет. У 0.125 половина настоящая, и способы расходятся.
    assert to_storage(0.125, 2) == 0.13          # половина ОТ НУЛЯ, как в базе
    assert to_storage(-0.125, 2) == -0.13
    assert round(0.125, 2) == 0.12               # половина К ЧЁТНОМУ — не как в базе
    assert to_storage(0.375, 2) == 0.38 and round(0.375, 2) == 0.38

    # Значение приведено к точности ХРАНЕНИЯ колонки NUMERIC(20,8).
    price = breakeven_price(0.21437, _COST_PCT)
    quantum = Decimal(1).scaleb(-PRICE_STORAGE_PLACES)
    assert Decimal(str(price)) == Decimal(str(price)).quantize(
        quantum, rounding=ROUND_HALF_UP
    )


def test_the_breakeven_price_makes_the_trade_flat_by_construction() -> None:
    """Итог сделки, закрытой по цене безубытка, равен нулю. Проверяемое следствие.

    Оно и есть определение «плюса»: не «цена выросла», а «после круговых
    издержек не осталось убытка».
    """
    for entry in (100.0, 77602.7, 1.4161):
        price = breakeven_price(entry, _COST_PCT)
        assert net_pnl(entry, price, _COST_PCT) == pytest.approx(0.0, abs=1e-6)


def test_the_measuring_script_and_the_live_code_share_one_function() -> None:
    """§2.2 ТЗ: у формулы и округления ОДНО определение, а не два похожих.

    Вторая копия однажды разошлась бы с первой — и разошлась бы молча.
    """
    import scripts.plus_exit_counterfactual_9_1_6 as plus

    assert plus.breakeven_price is breakeven_price
    assert plus.to_storage is to_storage
    assert plus.PRICE_STORAGE_PLACES == PRICE_STORAGE_PLACES


# =============================================================================
# §2.3 ТЗ. ГЛАВНЫЙ ТЕСТ: боевой путь ≡ вариант B счётного скрипта
# =============================================================================

def _variant_b(bars: list[Bar]):
    """Исход варианта ``B_nostop_plus_48`` замерного скрипта на этих свечах."""
    import scripts.plus_exit_counterfactual_9_1_6 as plus

    return plus.resolve_variant(
        bars,
        spec=plus.VARIANTS_BY_NAME[plus.B_NOSTOP_PLUS_48],
        entry_price=_ENTRY,
        target_pct=_TARGET_PCT,
        stop_pct=1.0,
        target_price=to_storage(target_price_of(_ENTRY, _TARGET_PCT)),
        fact_stop_price=to_storage(_ENTRY * 0.99),
        cost_pct=_COST_PCT,
        notional_usd=2.0,
        side="buy",
        opened_at=_OPENED,
        deadline_24=_PLUS_START,
        deadline_48=_DEADLINE,
        resolution="1m",
    )


def _live(bars: list[Bar]):
    """Исход БОЕВОГО пути закрытия на тех же свечах."""
    return check_exit_plus_wait(
        bars=bars,
        target_price=to_storage(target_price_of(_ENTRY, _TARGET_PCT)),
        plus_price=breakeven_price(_ENTRY, _COST_PCT),
        entry_price=_ENTRY,
        plus_start_at=_PLUS_START,
        deadline_at=_DEADLINE,
        cost_pct=_COST_PCT,
    )


# Пять рядов, покрывающих все исходы правила. Числа круглые: цель 101.0, цена
# безубытка 100.22, срок на 48 часах, граница фаз на 24-м часу.
_PARITY_SERIES: dict[str, list[Bar]] = {
    # 1. Цель задета на первых сутках — вторых суток не будет вовсе.
    "цель на первых сутках": _series(
        *_flat_window(60, 300, 100.0),
        _bar(360, 101.5, 99.5, 101.0),
        *_flat_window(420, 2941, 101.0),
    ),
    # 2. Первые сутки в минусе, на вторых цена возвращается в ПЛЮС, но до цели
    #    не доходит: исход plus_exit по цене безубытка.
    "возврат в плюс на вторых сутках": _series(
        *_flat_window(60, 1441, 98.0),
        _bar(1500, 100.30, 97.9, 100.25),
        *_flat_window(1560, 2941, 100.25),
    ),
    # 3. Первые сутки в минусе, на вторых цена дотягивает до ЦЕЛИ: цель
    #    выигрывает у плюса, потому что лежит выше него и достигается через него.
    "цель на вторых сутках": _series(
        *_flat_window(60, 1441, 98.0),
        _bar(1500, 101.20, 97.9, 101.0),
        *_flat_window(1560, 2941, 101.0),
    ),
    # 4. Ни цель, ни плюс за 48 часов: закрытие по рынку на сроке.
    "истёк срок 48 часов": _series(
        *_flat_window(60, 2941, 96.5),
    ),
    # 5. Глубокий минус, которого предел версии 5 не пережил бы: −8% на первых
    #    сутках, возврат в плюс на вторых.
    "глубокий минус и возврат": _series(
        *_flat_window(60, 720, 92.0),
        *_flat_window(720, 1441, 95.0),
        _bar(2000, 100.40, 94.0, 100.35),
        *_flat_window(2060, 2941, 100.35),
    ),
}


@pytest.mark.parametrize("name", sorted(_PARITY_SERIES))
def test_the_live_rule_matches_variant_b_bar_for_bar(name: str) -> None:
    """§2.3 ТЗ: боевой путь и вариант B дают ОДИН исход, момент и цену.

    ЭТО ГЛАВНАЯ ПРОВЕРКА ЭТАПА. Вариант ``B_nostop_plus_48`` в
    ``scripts/plus_exit_counterfactual_9_1_6.py`` — уже проверенная реализация
    ровно того правила, которое внедряется; владелец принял решение ПО ЕГО
    ЧИСЛАМ. Разойдись с ним боевой код хотя бы на одном баре — и в бою работало
    бы не то правило, по числам которого решение принималось, а похожее.

    Сравниваются все три величины сразу. Одного исхода мало: «вышли в плюс»
    в другой момент и по другой цене — это другая сделка с другим итогом.
    """
    bars = _PARITY_SERIES[name]
    live = _live(bars)
    shadow = _variant_b(bars)
    assert live is not None, "боевое правило не дало исхода"

    # Имена исходов у замера и у боя совпадают везде, кроме срока: замер
    # РАЗДЕЛЯЛ 'timeout_24' и 'timeout_48', потому что считал сразу пять
    # вариантов с разными сроками в одной таблице. В бою вариант один, а срок
    # записан в самой строке позиции (§5.2 ТЗ), и второе имя для того же
    # события завело бы словарь, который каждому запросу пришлось бы знать.
    import scripts.plus_exit_counterfactual_9_1_6 as plus

    expected = {
        plus.EXIT_TIMEOUT_48: EXIT_TIMEOUT,
        plus.EXIT_PLUS: EXIT_PLUS,
        EXIT_TARGET: EXIT_TARGET,
    }[shadow.exit_reason]
    assert live.exit_reason == expected, (
        f"{name}: бой дал {live.exit_reason}, замер — {shadow.exit_reason}"
    )
    assert live.exit_bar_ts == shadow.exit_bar_ts, f"{name}: разошёлся момент"
    assert live.exit_price == pytest.approx(shadow.exit_price, abs=1e-12), (
        f"{name}: разошлась цена выхода"
    )


def test_the_parity_series_cover_every_outcome_of_the_new_rule() -> None:
    """Ряды §2.3 покрывают ВСЕ три исхода, а не один и тот же трижды.

    Проверка не про полноту ради полноты: набор из пяти рядов, дающих один и
    тот же ``timeout``, доказывал бы совпадение правил ровно в одной точке.
    """
    got = {
        _live(bars).exit_reason  # type: ignore[union-attr]
        for bars in _PARITY_SERIES.values()
    }
    assert got == {EXIT_TARGET, EXIT_PLUS, EXIT_TIMEOUT}


def test_the_plus_exit_closes_the_trade_at_exactly_zero() -> None:
    """Цена выхода при ``plus_exit`` — цена БЕЗУБЫТКА, а не закрытие бара.

    Разница не косметическая: закрытие бара, на котором цена задела безубыток,
    может быть и выше, и НИЖЕ его, и подстановка закрытия превратила бы «выход
    в ноль» в лотерею. Следствие проверяемое — итог равен нулю.
    """
    decision = _live(_PARITY_SERIES["возврат в плюс на вторых сутках"])
    assert decision is not None and decision.exit_reason == EXIT_PLUS
    assert decision.exit_price == breakeven_price(_ENTRY, _COST_PCT)
    assert net_pnl(_ENTRY, decision.exit_price, _COST_PCT) == pytest.approx(
        0.0, abs=1e-6
    )


def test_the_rule_waits_and_does_not_close_at_twenty_four_hours() -> None:
    """Позиция версии 6 на 24-часовой отметке НЕ закрывается, а ждёт.

    Это и есть вторая перемена варианта B: правило версии 5 закрыло бы её здесь
    по рынку с убытком −2%, версия 6 — ждёт возврата в плюс.
    """
    bars = _PARITY_SERIES["возврат в плюс на вторых сутках"]
    # Свечей ровно до границы фаз: исхода ещё нет, потому что бар срока (48 ч)
    # не предъявлен — а на 24 часах правило версии 6 не закрывает ничего.
    up_to_24 = [bar for bar in bars if bar.ts <= _PLUS_START]
    assert check_exit_plus_wait(
        bars=up_to_24,
        target_price=to_storage(target_price_of(_ENTRY, _TARGET_PCT)),
        plus_price=breakeven_price(_ENTRY, _COST_PCT),
        entry_price=_ENTRY,
        plus_start_at=_PLUS_START,
        deadline_at=_DEADLINE,
        cost_pct=_COST_PCT,
    ) is None


def test_the_rule_never_returns_stop_or_ambiguous() -> None:
    """§4.4 ТЗ: у версии 6 предела нет, и исход ``stop`` недостижим.

    Проверяется на ряде, который предел версии 5 задел бы дважды: цена уходит
    на −8% и возвращается. Ни ``stop``, ни ``ambiguous`` правило вернуть не
    может — сравнивать минимум бара не с чем.
    """
    for bars in _PARITY_SERIES.values():
        decision = _live(bars)
        assert decision is not None
        assert decision.exit_reason in (EXIT_TARGET, EXIT_PLUS, EXIT_TIMEOUT)
        assert decision.outcome_certain is True


def test_the_rule_refuses_a_series_with_a_non_positive_low() -> None:
    """Посылка «предел на нуле не срабатывает» ПРОВЕРЯЕТСЯ, а не предполагается.

    Бар с неположительным минимумом сделал бы сравнение ``bar.low <= 0``
    истинным, и «правило без предела» тихо превратилось бы в правило с пределом
    на нуле — то есть в закрытие по цене ноль.
    """
    bars = [*_flat_window(60, 600, 100.0), _bar(660, 100.0, 0.0, 100.0)]
    with pytest.raises(ValueError, match="неположительным"):
        _live(bars)
    assert NO_STOP_PRICE == 0.0


def test_the_no_stop_price_is_the_same_number_as_in_the_measuring_scripts() -> None:
    """Одно и то же число для одного и того же смысла, а не два похожих."""
    from scripts.stop_counterfactual_9_1_4 import NO_STOP_PRICE as SHADOW_NO_STOP

    assert NO_STOP_PRICE == SHADOW_NO_STOP


def test_the_extremes_cover_the_whole_window_including_both_phases() -> None:
    """``mae``/``mfe`` считаются по ВСЕМУ удержанному окну, а не по одной фазе.

    Минимум окна не равен минимуму из двух минимумов, взятых от разных начал
    отсчёта, — и сложить их почленно нельзя. Здесь глубокий минус лежит на
    ПЕРВЫХ сутках, а выход случается на вторых: посчитай правило крайние
    отклонения по фазе выхода — и −8% исчезли бы из строки позиции.
    """
    decision = _live(_PARITY_SERIES["глубокий минус и возврат"])
    assert decision is not None
    assert decision.mae_pct == pytest.approx(-8.0, abs=1e-9)
    assert decision.mfe_pct == pytest.approx(0.40, abs=1e-9)
    assert decision.bars_held > 24


# =============================================================================
# §5, §11.5 ТЗ. Исходы и то, что база физически не даст записать
# =============================================================================

def test_plus_exit_joined_the_closed_list_of_outcomes() -> None:
    """§5.1 ТЗ: шестая причина выхода — и перечень по-прежнему ЗАКРЫТ."""
    assert EXIT_PLUS == "plus_exit"
    assert EXIT_PLUS in EXIT_REASONS
    assert len(EXIT_REASONS) == len(set(EXIT_REASONS)) == 6


def test_the_timeout_keeps_its_old_name_and_the_deadline_says_the_rest() -> None:
    """§5.2 ТЗ: решение названо — имя исхода по сроку ПРЕЖНЕЕ, ``timeout``.

    Событие одно и то же — «срок вышел, закрываем по рынку», — а отличается
    только сам срок, и он записан в строке колонкой ``deadline_at``. Второе имя
    заставило бы каждый будущий запрос знать оба, а строки версии 5
    переименованию не подлежат — старое имя из базы всё равно никуда не делось
    бы.
    """
    assert "timeout_48" not in EXIT_REASONS
    # Читается ПЕРЕЧЕНЬ ограничения, а не весь текст файла: в пояснении к
    # миграции имя 'timeout_48' названо ровно затем, чтобы сказать, почему его
    # не завели, и запрещать его упоминание значило бы запретить объяснение.
    m025 = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(encoding="utf-8")
    block = m025.split("positions_reason_chk", 1)[1].split("exit_reason IN", 1)[1]
    values = set(re.findall(r"'([a-z_]+)'", block.split(")", 1)[0]))
    assert values == set(EXIT_REASONS)
    assert "timeout_48" not in values
    # Срок при этом ВИДЕН человеку — и в сообщении, и в листе.
    assert messages.exit_ru("timeout", 48) == "истёк срок 48 ч"
    assert messages.exit_ru("timeout", 24) == "истёк срок 24 ч"
    assert messages.exit_ru("timeout") == "истёк срок"


def test_the_migration_forbids_the_stop_outcome_for_version_six() -> None:
    """§4.4 ТЗ: невозможность исхода ``stop`` записана ОГРАНИЧЕНИЕМ БАЗЫ.

    Проверка в коде переживает ровно до первой правки, которую забыли прогнать
    через тесты.
    """
    m025 = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(encoding="utf-8")
    assert "positions_v6_reason_chk" in m025
    assert "positions_no_stop_chk" in m025
    # Та же защита стоит и в схеме, которую сервис гарантирует при старте: на
    # чистом томе миграций могло не быть.
    db_text = (_ROOT / "src" / "core" / "db.py").read_text(encoding="utf-8")
    assert "positions_v6_reason_chk" in db_text
    assert "positions_no_stop_chk" in db_text


def test_the_migration_contains_no_update_of_existing_rows() -> None:
    """§1.2 и §9.2 ТЗ: ни одна строка версии 5 не переписывается.

    Проверяется по ТЕКСТУ миграции: ни UPDATE, ни DELETE, ни INSERT. Миграция,
    которая «заодно проставила» предел старым строкам, выглядела бы аккуратной
    и уничтожила бы 88 измерений.
    """
    m025 = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in m025.splitlines() if not line.strip().startswith("--")
    ).upper()
    for forbidden in ("UPDATE POSITIONS", "DELETE FROM POSITIONS",
                      "INSERT INTO POSITIONS", "TRUNCATE", "DROP TABLE"):
        assert forbidden not in body, forbidden


def test_the_rollback_refuses_to_falsify_version_six_rows() -> None:
    """Откат 025 не удаляет строк и не выдумывает предел — он ОТКАЗЫВАЕТСЯ.

    Вернуть NOT NULL можно ровно двумя способами: удалить строки версии 6 или
    подставить в них выдуманный предел. Первое — потеря факта, второе — данные,
    которые лгут; и сделать это МОЛЧА нельзя тем более.
    """
    back = (_MIGRATIONS / "025_positions_no_stop_rollback.sql").read_text(
        encoding="utf-8"
    )
    assert "RAISE EXCEPTION" in back
    body = "\n".join(
        line for line in back.splitlines() if not line.strip().startswith("--")
    ).upper()
    assert "DELETE FROM POSITIONS" not in body
    assert "UPDATE POSITIONS" not in body


# =============================================================================
# §8 ТЗ. Уведомления
# =============================================================================

def test_the_closing_message_names_the_new_outcome_in_plain_words() -> None:
    """§8.1 ТЗ 9.2: человеческий язык, а не машиночитаемый ключ.

    ФОРМА СООБЩЕНИЯ ПЕРЕПИСАНА ЭТАПОМ 9.3 (§6.2 ТЗ 9.3 выписывает её
    построчно), требование этапа 9.2 при этом осталось в силе и проверяется
    здесь: исход назван словами, ключа в тексте нет, предел убытка не упомянут.
    """
    text = messages.closed_text(
        position_id=12,
        symbol="BTC/USDT", exit_reason="plus_exit", entry_price=100.0,
        exit_price=100.22, net_pnl_pct=0.0, net_pnl_usd=0.0,
        cost_pct=_COST_PCT, held_sec=30 * 3600, hold_hours=48,
    )
    assert "Причина: вышли в плюс после суток" in text
    assert "plus_exit" not in text
    assert "предел" not in text.lower()
    # §6.2 ТЗ 9.3: время в сделке в формате ЧЧ:ММ.
    assert "Время в сделке 30:00" in text

    timeout = messages.closed_text(
        position_id=13,
        symbol="BTC/USDT", exit_reason="timeout", entry_price=100.0,
        exit_price=96.5, net_pnl_pct=-3.72, net_pnl_usd=-0.074,
        cost_pct=_COST_PCT, held_sec=48 * 3600, hold_hours=48,
    )
    assert "истёк срок 48 ч" in timeout
    assert "предел" not in timeout.lower()


def test_the_opening_message_of_version_six_never_mentions_a_stop() -> None:
    """§8.2 ТЗ 9.2: предела нет — писать о нём означало бы вводить в заблуждение.

    ЧТО ИЗМЕНИЛ ЭТАП 9.3. §6.2 ТЗ 9.3 выписал сообщение об открытии построчно,
    и строки о цене безубытка в образце нет — её место заняла строка правила
    «Правило v7 · без предела убытка · срок 48 ч». Требование 9.2 при этом
    выполнено сильнее прежнего: об отсутствии предела сказано прямо, а не
    выведено из молчания.
    """
    text = messages.opened_text(
        position_id=42,
        symbol="BTC/USDT", entry_price=100.0,
        target_price=101.0, target_pct=1.0, stop_pct=None,
        probability=0.83, logic_version=6, hold_hours=48,
    )
    assert "Правило v6 · без предела убытка · срок 48 ч" in text
    assert "предел убытка −" not in text

    # У версии 5 предел есть, и сообщение его называет.
    old = messages.opened_text(
        position_id=42,
        symbol="BTC/USDT", entry_price=100.0,
        target_price=101.0, target_pct=1.0, stop_pct=1.0,
        probability=0.83, logic_version=5, hold_hours=24,
    )
    assert "Правило v5 · предел убытка −1.00% · срок 24 ч" in old


def test_the_sheet_note_of_version_six_names_the_rule_instead_of_the_stop() -> None:
    """§8.2 и §1.4 ТЗ: в листе лежат строки обеих версий вперемешку.

    Человек, читающий журнал сверху вниз, обязан видеть у каждой сделки, какое
    правило её вело: иначе он сложит их итоги, а складывать их нельзя.
    """
    row = {
        "id": 91, "logic_version": 6, "target_price": 101.0, "target_pct": 1.0,
        "stop_price": None, "stop_pct": None, "signal_id": 7,
        "probability": 0.83, "entry_lag_sec": 95,
    }
    note = build_position_note(row)
    assert note.startswith("[поз. 91]")
    assert "без предела · правило v6" in note
    assert "предел " not in note

    old = build_position_note({**row, "logic_version": 5,
                               "stop_price": 99.0, "stop_pct": 1.0})
    assert "предел 99.0000 (−1.00%)" in old


def test_the_sheet_tail_names_the_deadline_taken_from_the_row() -> None:
    """Срок в заметке считается по СТРОКЕ, а не берётся из настройки.

    Настройка отвечает на вопрос «сколько живут позиции, которые откроются
    сегодня»; здесь спрашивается «сколько жила ВОТ ЭТА». Правка настройки не
    имеет права задним числом переписать журнал.
    """
    row = {
        "exit_reason": "timeout", "net_pnl_pct": -3.72, "net_pnl_usd": -0.074,
        "cost_pct": _COST_PCT, "stop_price": None,
        "opened_at": _OPENED, "deadline_at": _DEADLINE,
    }
    assert position_hold_hours(row) == 48
    assert "истёк срок 48 ч" in build_position_close_note_tail(row)

    old = {**row, "stop_price": 99.0, "deadline_at": _OPENED + timedelta(hours=24)}
    assert "истёк срок 24 ч" in build_position_close_note_tail(old)

    # Срок не в целых часах — числа не выдумываем.
    odd = {**row, "deadline_at": _OPENED + timedelta(hours=24, minutes=30)}
    assert position_hold_hours(odd) is None
    assert build_position_close_note_tail(odd).startswith(" · истёк срок ·")

    plus = {**row, "exit_reason": "plus_exit", "net_pnl_pct": 0.0,
            "net_pnl_usd": 0.0}
    assert "вышли в плюс после суток ожидания" in (
        build_position_close_note_tail(plus)
    )


# =============================================================================
# §6.2 ТЗ. Адресация строк в выгрузке — прямой ответ
# =============================================================================

def test_the_export_addresses_rows_by_position_id_and_never_by_row_number() -> None:
    """§6.2 ТЗ: ОТВЕТ ПРЯМОЙ — адресация уже безопасна, и менять нечего.

    Дозапись закрытия адресуется МЕТКОЙ ``[поз. <id>]`` в столбце заметок, а не
    номером строки и не дописыванием в конец. Приёмник ищет метку по всему
    столбцу, при отсутствии — не угадывает строку (``notFound``), при двух и
    более — не пишет ничего (``ambiguous``). Удаление всех строк листа
    07.09.2026 поэтому не могло испортить ни одной сделки: не найдя метки,
    выгрузка кладёт сделку новой полной строкой.

    ТЕСТ ЧИТАЕТ КОД, А НЕ ПОВТОРЯЕТ УТВЕРЖДЕНИЕ. В запросе дозаписи не должно
    быть ни номера строки, ни слова о положении.
    """
    client = (_ROOT / "src" / "export_main.py").read_text(encoding="utf-8")
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")

    # Пачка дозаписи собирается из меток, и ни одного номера строки в ней нет.
    updates = client.split('"marker": position_marker(row["id"])', 1)
    assert len(updates) == 2, "дозапись перестала адресоваться меткой позиции"
    assert '"row"' not in client and '"rowIndex"' not in client

    # Приёмник ищет строку по метке и при неоднозначности НЕ ПИШЕТ.
    assert "markerRows(noteValues, item.marker)" in receiver
    assert "notFound.push(item.marker)" in receiver
    assert "ambiguous.push({ marker: String(item.marker), rows: found });" in (
        receiver
    )


# =============================================================================
# §7.2 ТЗ. Отказ по занятому слоту виден владельцу
# =============================================================================

def test_the_refusal_counter_is_split_by_version_and_by_day() -> None:
    """§7.2 и §1.4 ТЗ: счётчик несёт версию логики и сутки.

    Без версии счётчики версий 5 и 6 сложились бы в одно число; без суток
    счётчик рос бы вечно и отвечал бы на вопрос, который никому не нужен.
    """
    key = refusal_key(6, "2026-09-07", "slots_full")
    assert key == "positions:refused:6:2026-09-07:slots_full"
    assert refusal_key(5, "2026-09-07", "slots_full") != key


def test_the_refusal_key_has_exactly_one_definition() -> None:
    """Имя ключа собирают ДВОЕ: сервис пишет, бот читает.

    Две одинаковые строки в двух файлах однажды разошлись бы, и счётчики просто
    перестали бы находиться — молча, без единой ошибки, показывая честный ноль.
    """
    for name in ("src/positions/runner.py", "src/bot/poller.py"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "positions:refused:" not in text, (
            f"{name} собирает имя ключа сам, в обход refusal_key"
        )
        assert "refusal_key(" in text


def test_the_bot_shows_every_refusal_reason_in_plain_words() -> None:
    """Владелец должен видеть, СКОЛЬКО сигналов система пропускает.

    Перечень причин закрыт (``rules.REFUSAL_REASONS``), и у каждой обязан быть
    перевод: непереведённый ключ в сообщении бота читается как сбой.
    """
    from src.bot.handlers import REFUSAL_RU
    from src.positions.rules import REFUSAL_REASONS

    assert set(REFUSAL_RU) == set(REFUSAL_REASONS)


def test_the_slot_count_is_not_changed_by_this_stage() -> None:
    """§7.1 и §7.3 ТЗ 9.2: слоты и бюджет ЭТИМ ЭТАПОМ не менялись.

    По замеру 9.1.6 слот оказывался занят в среднем 22.5 часа вместо 3.4, и
    соблазн «компенсировать» занятость был очевиден. Этап 9.2 такого решения не
    принимал — его принял владелец ОТДЕЛЬНО, в ТЗ «Версия логики 7»: слоты и
    бюджет сняты целиком, и вместо них поставлена пауза по токену.

    ЧТО ОСТАЛОСЬ НЕИЗМЕННЫМ ЧЕРЕЗ ОБА РЕШЕНИЯ — размер слота и порог входа. Эти
    два числа и делают проценты версий 5, 6 и 7 сопоставимыми между собой, и
    проверка их стоит здесь по-прежнему.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_SLOT_USD == 2.0
    assert fresh.POSITION_MIN_PROBABILITY == 0.8


# =============================================================================
# §1.4 ТЗ. Версии не смешиваются ни в одном расчёте
# =============================================================================

def test_every_position_metric_can_split_by_logic_version() -> None:
    """§1.4 ТЗ: смешивать версии запрещено — и запрос обязан уметь их делить."""
    text = (_ROOT / "src" / "bot" / "queries.py").read_text(encoding="utf-8")
    block = text.split("async def positions_summary", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "logic_version = $2" in block, (
        "итог по позициям считается по всем версиям сразу"
    )
    capital = text.split("async def positions_capital", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "GROUP BY logic_version" in capital


def test_the_service_asks_the_rule_of_the_row_and_not_of_the_setting() -> None:
    """Недозакрытые позиции версии 5 дочитываются ПРАВИЛОМ ВЕРСИИ 5.

    Спроси сервис правило у настройки — и после подъёма версии он закрыл бы
    оставшиеся позиции версии 5 новым правилом, то есть пересчитал бы их
    (§1.2 и §9.2 ТЗ запрещают прямо).
    """
    runner = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    sync = runner.split("async def sync_open_positions", 1)[1].split(
        "\nasync def ", 1
    )[0]
    assert 'without_stop(row["logic_version"])' in sync
    assert "without_stop(settings.LOGIC_VERSION)" not in sync


def test_the_plus_wait_start_is_measured_from_the_position_itself() -> None:
    """Отметка ожидания отсчитывается от ``opened_at`` строки, а не от «сейчас»."""
    row = {"opened_at": _OPENED}
    assert plus_start_of(row) == _OPENED + timedelta(
        hours=settings.POSITION_PLUS_WAIT_START_HOURS
    )


# =============================================================================
# §11 ТЗ. Приёмка на НАСТОЯЩЕМ PostgreSQL 16
# =============================================================================

_PLANTED_SYMBOL = "TEST92/USDT"


async def _plant(conn, *, logic_version: int, closed: bool, **over):
    """Кладёт одну позицию заданной версии и возвращает её id."""
    ts = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', $1, 'TEST92', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE "
        "SET symbol = EXCLUDED.symbol RETURNING id;",
        _PLANTED_SYMBOL,
    )
    signal_id = await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, $2, 'buy', $3) RETURNING id;",
        instrument_id, ts, logic_version,
    )
    stop_pct = None if logic_version >= 6 else Decimal("1")
    stop_price = None if logic_version >= 6 else Decimal("99")
    fields = {
        "exit_reason": "timeout", "exit_price": Decimal("100"),
        "net_pnl_pct": Decimal("-0.22"), "net_pnl_usd": Decimal("-0.0044"),
        "outcome_certain": True, "bars_held": 1440,
    }
    fields.update(over)
    hours = 48 if logic_version >= 6 else 24
    return await conn.fetchval(
        """
        INSERT INTO positions
            (instrument_id, signal_id, logic_version, horizon_h, side,
             is_virtual, status, signal_ts, signal_price, opened_at,
             entry_price, entry_lag_sec, entry_slippage_pct, qty, notional_usd,
             target_pct, target_price, stop_pct, stop_price, cost_pct,
             deadline_at, resolution, closed_at, exit_price, exit_reason,
             outcome_certain, net_pnl_pct, net_pnl_usd, bars_held,
             mae_pct, mfe_pct)
        VALUES ($1, $2, $3, 24, 'buy', TRUE, $4, $5, 100, $5, 100, 25, 0,
                0.02, 2.0, 1, 101, $6, $7, 0.22, $8, '1m',
                $9, $10, $11, $12, $13, $14, $15, 0, 0)
        RETURNING id;
        """,
        instrument_id, signal_id, logic_version,
        "closed" if closed else "open", ts, stop_pct, stop_price,
        ts + timedelta(hours=hours),
        (ts + timedelta(hours=hours)) if closed else None,
        fields["exit_price"] if closed else None,
        fields["exit_reason"] if closed else None,
        fields["outcome_certain"] if closed else None,
        fields["net_pnl_pct"] if closed else None,
        fields["net_pnl_usd"] if closed else None,
        fields["bars_held"] if closed else None,
    )


async def _drop_planted(conn) -> None:
    """Убирает всё, что подложил этот файл. Чужих строк не трогает."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol = $1);", _PLANTED_SYMBOL
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol = $1);", _PLANTED_SYMBOL
    )


async def _checksum(conn, logic_version: int) -> str:
    """Отпечаток ВСЕХ строк заданной версии: любое изменение его сдвинет.

    Считается базой, а не Python: числа не проходят через float и не теряют
    знаков по дороге.
    """
    return await conn.fetchval(
        """
        SELECT COALESCE(md5(string_agg(t.line, '|' ORDER BY t.line)), 'пусто')
        FROM (
            SELECT p.id::text || ':' || p.status || ':' ||
                   COALESCE(p.exit_reason, '-') || ':' ||
                   COALESCE(p.stop_pct::text, '-') || ':' ||
                   COALESCE(p.stop_price::text, '-') || ':' ||
                   COALESCE(p.net_pnl_pct::text, '-') || ':' ||
                   COALESCE(p.net_pnl_usd::text, '-') || ':' ||
                   COALESCE(p.closed_at::text, '-') AS line
            FROM positions p WHERE p.logic_version = $1
        ) t;
        """,
        logic_version,
    )


@needs_db
async def test_migration_025_applies_rolls_back_and_applies_again() -> None:
    """§11.1 ТЗ: применяется, откатывается и применяется заново — НА НЕПУСТОЙ.

    Пустая таблица ничего не доказывает: и ослабление NOT NULL, и восстановление
    его обратно на ней проходят при любом содержимом ограничений. Здесь в
    таблице лежит строка ВЕРСИИ 5 — та самая, обязательность предела у которой
    откат и восстанавливает.
    """
    import asyncpg

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    backward = (_MIGRATIONS / "025_positions_no_stop_rollback.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _drop_planted(conn)
        await _plant(conn, logic_version=5, closed=True)
        assert await conn.fetchval("SELECT count(*) FROM positions;") > 0

        # Применяется дважды подряд — миграция идемпотентна.
        await conn.execute(forward)
        await conn.execute(forward)
        assert not await _is_not_null(conn, "stop_pct")
        assert await _constraint_exists(conn, "positions_no_stop_chk")

        # Откатывается на НЕПУСТОЙ таблице: строк версии 6 в ней нет, значит
        # ни одну строку фальсифицировать не придётся.
        await conn.execute(backward)
        assert await _is_not_null(conn, "stop_pct")
        assert await _is_not_null(conn, "stop_price")
        assert not await _constraint_exists(conn, "positions_no_stop_chk")

        # И применяется заново.
        await conn.execute(forward)
        assert not await _is_not_null(conn, "stop_pct")
        assert await _constraint_exists(conn, "positions_no_stop_chk")
    finally:
        await _drop_planted(conn)
        # База обязана остаться в том состоянии, в каком её взяли.
        await conn.execute(forward)
        await conn.close()


@needs_db
async def test_the_rollback_refuses_while_version_six_rows_exist() -> None:
    """Откат ОТКАЗЫВАЕТСЯ работать, пока в таблице живут позиции версии 6.

    Иначе он либо удалил бы их, либо потребовал подставить выдуманный предел.
    Первое — потеря факта, второе — данные, которые лгут.
    """
    import asyncpg

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    backward = (_MIGRATIONS / "025_positions_no_stop_rollback.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _drop_planted(conn)
        await conn.execute(forward)
        planted = await _plant(conn, logic_version=6, closed=True)
        with pytest.raises(asyncpg.exceptions.RaiseError, match="откат 025"):
            await conn.execute(backward)
        # Сценарий отката — один скрипт с BEGIN/COMMIT, и упавший RAISE
        # оставляет соединение в оборванной сделке. Снимаем её явно: иначе
        # следующий же запрос ответил бы «current transaction is aborted», и
        # тест провалился бы на уборке, а не на проверке.
        await conn.execute("ROLLBACK;")
        # Строка на месте, и предел у неё по-прежнему пуст.
        row = await conn.fetchrow(
            "SELECT stop_pct, stop_price FROM positions WHERE id = $1;", planted
        )
        assert row["stop_pct"] is None and row["stop_price"] is None
    finally:
        await _drop_planted(conn)
        await conn.execute(forward)
        await conn.close()


@needs_db
async def test_not_a_single_version_five_row_changes() -> None:
    """§11.2 ТЗ: отпечаток строк версии 5 до и после — один и тот же.

    Считается ДО применения миграции и ПОСЛЕ неё, вместе с записью позиций
    версии 6 рядом. Совпадение отпечатка и есть доказательство: изменись хоть
    один знак хоть в одной колонке — md5 разъехался бы.
    """
    import asyncpg

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _drop_planted(conn)
        await conn.execute(forward)
        for _ in range(3):
            await _plant(conn, logic_version=5, closed=True)
        before = await _checksum(conn, 5)
        total_before = await conn.fetchval(
            "SELECT sum(net_pnl_usd) FROM positions WHERE logic_version = 5 "
            "AND status = 'closed' AND exit_reason <> 'data_gap';"
        )

        # Рядом появляются позиции версии 6 — и закрытые, и открытая.
        await _plant(conn, logic_version=6, closed=True, exit_reason="plus_exit",
                     net_pnl_pct=Decimal("0"), net_pnl_usd=Decimal("0"))
        await _plant(conn, logic_version=6, closed=False)
        await conn.execute(forward)

        assert await _checksum(conn, 5) == before, (
            "строки версии 5 изменились после внедрения версии 6"
        )
        total_after = await conn.fetchval(
            "SELECT sum(net_pnl_usd) FROM positions WHERE logic_version = 5 "
            "AND status = 'closed' AND exit_reason <> 'data_gap';"
        )
        assert total_after == total_before, "накопленный итог версии 5 изменился"
    finally:
        await _drop_planted(conn)
        await conn.execute(forward)
        await conn.close()


@needs_db
async def test_the_database_physically_refuses_a_stop_for_version_six() -> None:
    """§11.5 ТЗ: строка версии 6 с исходом ``stop`` или с непустым пределом.

    Ни та, ни другая физически невозможны — и невозможны БАЗОЙ, а не кодом.
    """
    import asyncpg

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _drop_planted(conn)
        await conn.execute(forward)

        # 1. Исход stop у версии 6.
        with pytest.raises(asyncpg.exceptions.CheckViolationError,
                           match="positions_v6_reason_chk"):
            await _plant(conn, logic_version=6, closed=True,
                         exit_reason="stop", exit_price=Decimal("99"))
        await _drop_planted(conn)

        # 2. Непустой предел у версии 6.
        planted = await _plant(conn, logic_version=6, closed=True)
        with pytest.raises(asyncpg.exceptions.CheckViolationError,
                           match="positions_no_stop_chk"):
            await conn.execute(
                "UPDATE positions SET stop_price = 99, stop_pct = 1 "
                "WHERE id = $1;", planted
            )
        await _drop_planted(conn)

        # 3. Обратная сторона: у версии 5 предел ОБЯЗАТЕЛЕН, как и был.
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', $1, 'TEST92', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;", _PLANTED_SYMBOL,
        )
        ts = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        signal_id = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
            "VALUES ($1, $2, 'buy', 5) RETURNING id;", instrument_id, ts,
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError,
                           match="positions_no_stop_chk"):
            await conn.execute(
                """
                INSERT INTO positions
                    (instrument_id, signal_id, logic_version, horizon_h, side,
                     is_virtual, status, signal_ts, signal_price, opened_at,
                     entry_price, entry_lag_sec, entry_slippage_pct, qty,
                     notional_usd, target_pct, target_price, stop_pct,
                     stop_price, cost_pct, deadline_at, resolution)
                VALUES ($1, $2, 5, 24, 'buy', TRUE, 'open', $3, 100, $3, 100,
                        25, 0, 0.02, 2.0, 1, 101, NULL, NULL, 0.22, $4, '1m');
                """,
                instrument_id, signal_id, ts, ts + timedelta(hours=24),
            )

        # 4. И исход plus_exit невозможен у версии 5.
        with pytest.raises(asyncpg.exceptions.CheckViolationError,
                           match="positions_plus_exit_chk"):
            await _plant(conn, logic_version=5, closed=True,
                         exit_reason="plus_exit")
    finally:
        await _drop_planted(conn)
        await conn.execute(forward)
        await conn.close()


@needs_db
async def test_a_version_six_position_is_written_and_read_back_without_a_stop() -> None:
    """Сквозная проверка: сервис открывает позицию версии 6 в НАСТОЯЩЕЙ базе.

    Проверяется не форма SQL, а результат: строка записана, предел в ней пуст,
    срок стоит на 48 часах, а горизонт сигнала остался прежним.
    """
    import asyncpg

    from src.core.db import db as real_db

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    original = getattr(real_db, "_pool", None)
    try:
        real_db._pool = pool  # noqa: SLF001 — подмена пула на тестовый
        async with pool.acquire() as conn:
            await conn.execute(forward)
            await _drop_planted(conn)
            ts = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
            instrument_id = await conn.fetchval(
                "INSERT INTO instruments (exchange, symbol, base, quote, type) "
                "VALUES ('okx', $1, 'TEST92', 'USDT', 'spot') "
                "ON CONFLICT (exchange, symbol, type) DO UPDATE "
                "SET symbol = EXCLUDED.symbol RETURNING id;", _PLANTED_SYMBOL,
            )
            signal_id = await conn.fetchval(
                "INSERT INTO signals (instrument_id, ts, decision, logic_version)"
                " VALUES ($1, $2, 'buy', 6) RETURNING id;", instrument_id, ts,
            )

        # Схема, которую сервис гарантирует при старте, обязана быть согласна
        # с миграцией: на чистом томе 025 могла не применяться.
        await real_db.ensure_positions_schema()

        position_id = await real_db.open_position({
            "instrument_id": instrument_id, "signal_id": signal_id,
            "logic_version": 6, "horizon_h": settings.POSITION_HORIZON_H,
            "side": "buy", "signal_ts": ts, "signal_price": 100.0,
            "opened_at": ts, "entry_price": 100.0, "entry_lag_sec": 25,
            "entry_slippage_pct": 0.0, "qty": 0.02, "notional_usd": 2.0,
            "target_pct": 1.0, "target_price": 101.0,
            "stop_pct": None, "stop_price": None, "cost_pct": _COST_PCT,
            "deadline_at": ts + timedelta(hours=48),
            "last_checked_ts": ts, "resolution": "1m",
        })
        assert position_id is not None

        rows = [r for r in await real_db.get_open_positions()
                if int(r["id"]) == int(position_id)]
        assert len(rows) == 1
        row = rows[0]
        assert row["stop_pct"] is None and row["stop_price"] is None
        assert without_stop(row["logic_version"])
        assert (row["deadline_at"] - row["opened_at"]) == timedelta(hours=48)
        # ГОРИЗОНТ СИГНАЛА НЕ ТРОНУТ (§3.3 ТЗ).
        assert int(row["horizon_h"]) == 24

        # И закрывается она исходом, которого у версии 5 быть не могло.
        assert await real_db.close_position(
            int(position_id), closed_at=ts + timedelta(hours=30),
            exit_price=breakeven_price(100.0, _COST_PCT),
            exit_reason=EXIT_PLUS, outcome_certain=True,
            net_pnl_pct=0.0, net_pnl_usd=0.0, bars_held=1800,
            mae_pct=-2.0, mfe_pct=0.3, last_checked_ts=ts + timedelta(hours=30),
        )
    finally:
        async with pool.acquire() as conn:
            await _drop_planted(conn)
            await conn.execute(forward)
        real_db._pool = original  # noqa: SLF001
        await pool.close()


async def _is_not_null(conn, column: str) -> bool:
    """Стоит ли NOT NULL на колонке ``positions.<column>`` прямо сейчас."""
    return bool(await conn.fetchval(
        "SELECT attnotnull FROM pg_attribute "
        "WHERE attrelid = 'positions'::regclass AND attname = $1;", column
    ))


async def _constraint_exists(conn, name: str) -> bool:
    """Есть ли ограничение с таким именем на ``positions``."""
    return bool(await conn.fetchval(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conname = $1 AND conrelid = 'positions'::regclass;", name
    ))


def test_the_service_waits_for_the_NET_plus_and_not_the_gross_one() -> None:
    """Плюс в бою — ЧИСТЫЙ. Это разница между вариантами B и D замера 9.1.6.

    НАЙДЕНО ПРОВЕРКОЙ ТЕСТОВ НА ПОДМЕНУ. Замените в сервисе вызов
    ``breakeven_price(...)`` на ``breakeven_price(..., gross=True)`` — и правило
    начнёт выходить по ЦЕНЕ ВХОДА вместо цены безубытка, то есть закрывать
    сделку ровно в минус издержкам и называть это плюсом. Все прочие проверки
    этого файла остались бы зелёными: главный тест §2.3 зовёт правило напрямую и
    передаёт цену сам, а проводку сервиса не трогает.

    Вариант D замера отличался от варианта B ровно этим, и заведён он был именно
    затем, чтобы показать, сколько сделок разделяет ставка круговых издержек.
    Проверка читает код: в проводке сервиса ``gross`` не должен появляться ни
    разу.
    """
    runner_text = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    assert "gross" not in runner_text, (
        "сервис зовёт breakeven_price с валовым плюсом: он закрывал бы сделку "
        "в минус издержкам и называл это плюсом"
    )
    # И цена, которую сервис получает, действительно ВЫШЕ цены входа: валовой
    # безубыток равен ей, чистый — строго больше.
    net = breakeven_price(_ENTRY, _COST_PCT)
    gross = breakeven_price(_ENTRY, _COST_PCT, gross=True)
    assert gross == _ENTRY
    assert net > gross
    assert net_pnl(_ENTRY, gross, _COST_PCT) == pytest.approx(-_COST_PCT)


@needs_db
async def test_the_service_closes_a_version_six_position_end_to_end() -> None:
    """Сквозной прогон боевого сервиса на НАСТОЯЩЕЙ базе: от свечей до строки.

    ЗАЧЕМ ОН НУЖЕН ПОВЕРХ ТЕСТА §2.3. Тот сверяет ПРАВИЛО с вариантом B, зовя
    его напрямую и передавая цены сам. Здесь проверяется ПРОВОДКА: те ли цены
    сервис передаёт правилу, тот ли срок берёт из строки, ту ли отметку начала
    ожидания считает и то ли записывает в базу. Подмена чистого плюса валовым
    живёт именно здесь — и тест §2.3 её не увидел бы.

    Ряд построен так, что версия 5 закрыла бы позицию по пределу в первый же
    час: цена уходит на −3% и стоит там сутки, а на вторых сутках один бар
    поднимается выше цены безубытка.
    """
    import asyncpg

    from src.core.db import db as real_db
    from src.positions import runner as positions_runner

    forward = (_MIGRATIONS / "025_positions_no_stop.sql").read_text(
        encoding="utf-8"
    )
    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    original = getattr(real_db, "_pool", None)
    notify = settings.POSITION_NOTIFY_ENABLED
    try:
        real_db._pool = pool  # noqa: SLF001 — подмена пула на тестовый
        settings.POSITION_NOTIFY_ENABLED = False
        t0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        async with pool.acquire() as conn:
            await conn.execute(forward)
            await _drop_planted(conn)
            await conn.execute(
                "DELETE FROM ohlcv WHERE instrument_id IN "
                "(SELECT id FROM instruments WHERE symbol = $1);", _PLANTED_SYMBOL
            )
            instrument_id = await conn.fetchval(
                "INSERT INTO instruments (exchange, symbol, base, quote, type) "
                "VALUES ('okx', $1, 'TEST92', 'USDT', 'spot') "
                "ON CONFLICT (exchange, symbol, type) DO UPDATE "
                "SET symbol = EXCLUDED.symbol RETURNING id;", _PLANTED_SYMBOL,
            )
            signal_id = await conn.fetchval(
                "INSERT INTO signals (instrument_id, ts, decision, logic_version)"
                " VALUES ($1, $2, 'buy', 6) RETURNING id;", instrument_id, t0,
            )
            # Первые сутки на −3%, на вторых один бар выше цены безубытка.
            plus_minute = 30 * 60
            bars = [
                (instrument_id, "1m", t0 + timedelta(minutes=m), 97.0,
                 100.30 if m == plus_minute else 97.0, 97.0, 97.0, 1.0)
                for m in range(1, 48 * 60 + 2)
            ]
            await conn.executemany(
                "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, "
                "low, close, volume) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
                "ON CONFLICT DO NOTHING;", bars,
            )

        position_id = await real_db.open_position({
            "instrument_id": instrument_id, "signal_id": signal_id,
            "logic_version": 6, "horizon_h": settings.POSITION_HORIZON_H,
            "side": "buy", "signal_ts": t0, "signal_price": _ENTRY,
            "opened_at": t0, "entry_price": _ENTRY, "entry_lag_sec": 20,
            "entry_slippage_pct": 0.0, "qty": 0.02, "notional_usd": 2.0,
            "target_pct": _TARGET_PCT,
            "target_price": to_storage(target_price_of(_ENTRY, _TARGET_PCT)),
            "stop_pct": None, "stop_price": None, "cost_pct": _COST_PCT,
            "deadline_at": t0 + timedelta(hours=48),
            "last_checked_ts": t0, "resolution": "1m",
        })
        assert position_id is not None

        stats = await positions_runner.sync_open_positions(
            t0 + timedelta(hours=50)
        )
        assert stats.closed == 1, f"позиция не закрыта: {stats.by_reason}"
        assert stats.by_reason == {EXIT_PLUS: 1}

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT exit_reason, exit_price, closed_at, net_pnl_pct, "
                "       stop_price, stop_pct, bars_held, mae_pct "
                "FROM positions WHERE id = $1;", position_id
            )
        # ЦЕНА ВЫХОДА — ЧИСТЫЙ безубыток, и итог равен РОВНО нулю. Подставь
        # сюда валовой — вышло бы −0.22%, то есть «плюс», равный убытку.
        assert float(row["exit_price"]) == breakeven_price(_ENTRY, _COST_PCT)
        assert float(row["net_pnl_pct"]) == 0.0
        assert row["stop_price"] is None and row["stop_pct"] is None
        # Закрыта НА ВТОРЫХ сутках, по бару плюса, а не на 24-часовой отметке.
        assert row["closed_at"] == t0 + timedelta(hours=30, minutes=1)
        # И крайнее отклонение помнит весь путь, включая первые сутки.
        assert float(row["mae_pct"]) == pytest.approx(-3.0, abs=1e-6)
    finally:
        settings.POSITION_NOTIFY_ENABLED = notify
        async with pool.acquire() as conn:
            await _drop_planted(conn)
            await conn.execute(
                "DELETE FROM ohlcv WHERE instrument_id IN "
                "(SELECT id FROM instruments WHERE symbol = $1);", _PLANTED_SYMBOL
            )
            await conn.execute(forward)
        real_db._pool = original  # noqa: SLF001
        await pool.close()


# =============================================================================
# §11.11 ТЗ. Имена затронутых объектов — фактические, а не придуманные
# =============================================================================

def test_every_name_the_report_promises_actually_exists() -> None:
    """Придуманные имена в этом проекте оказывались неверными шесть раз подряд.

    Поэтому перечень §11.11 отчёта проверяется КОДОМ: каждое имя ищется там,
    где отчёт обещает его найти. Тест дороже перечитывания отчёта глазами и
    ловит ровно тот вид ошибки, что уже случался.
    """
    checks = {
        "db/migrations/025_positions_no_stop.sql": [
            "positions_no_stop_chk", "positions_v6_reason_chk",
            "positions_plus_exit_chk", "positions_exit_measured_chk",
            "positions_reason_chk", "positions_bounds_chk",
        ],
        "db/migrations/025_positions_no_stop_rollback.sql": ["RAISE EXCEPTION"],
        "src/core/config.py": [
            "POSITION_MAX_HOLD_HOURS", "POSITION_PLUS_WAIT_START_HOURS",
            "LOGIC_VERSION: int =",
        ],
        "src/positions/rules.py": [
            "EXIT_PLUS", "check_exit_plus_wait", "breakeven_price",
            "target_price_of", "to_storage", "NO_STOP_PRICE",
            "PLUS_WAIT_MIN_LOGIC_VERSION", "refusal_key", "assert_no_stop",
        ],
        "src/positions/runner.py": [
            "without_stop", "hold_hours", "plus_start_of", "_count_refusals",
        ],
        "src/positions/messages.py": ["exit_ru", "EXIT_RU_WITH_HOURS"],
        "src/export/transform.py": ["position_hold_hours", "POSITION_EXIT_RU"],
        "src/bot/handlers.py": ["REFUSAL_RU", "render_positions"],
        "deploy/apps_script.gs": ["elapsedTimeFormat", "fixElapsedFormats"],
    }
    for path, names in checks.items():
        text = (_ROOT / path).read_text(encoding="utf-8")
        for name in names:
            assert name in text, f"{path}: имени {name} в файле нет"


def test_the_env_example_names_the_new_settings() -> None:
    """Настройка, которой нет в ``.env.example``, для владельца не существует."""
    env = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^LOGIC_VERSION=\d+\b", env, re.M)
    assert re.search(r"^POSITION_MAX_HOLD_HOURS=48\b", env, re.M)
    assert re.search(r"^POSITION_PLUS_WAIT_START_HOURS=24\b", env, re.M)
