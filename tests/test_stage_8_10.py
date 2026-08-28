"""Этап 8.10: подвижный выход (§9.5 ТЗ). Синтетика с заранее известным ответом.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Замер бесполезен, если правило
выхода можно незаметно согнуть. Согнуть его можно четырьмя способами, и каждый
выглядел бы в отчёте правдоподобно:

  * посчитать откат от вершины, поставленной ЭТИМ ЖЕ баром, — то есть угадать,
    что вершина была раньше падения;
  * дать цели закрывать сделку после включения подвижного выхода — тогда
    подвижный вариант окажется смесью двух правил, а не отдельным правилом;
  * посчитать контрольный вариант «почти как 8.8» — и тогда всё сравнение
    недействительно, причём молча;
  * объявить timeout там, где ряд свечей разорван, — то есть сказать «ничего не
    сработало» о минутах, которых никто не видел.

Плюс проверки трёх защит §5: они обязаны печатать предписанные формулировки
ДОСЛОВНО и признавать неразличимость там, где её нет оснований отрицать.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.barrier.outcomes import BUY, SELL, Bar, resolve
from src.core.config import settings
from src.trailing.rule import (
    ACTIVATION_RATIOS,
    EXIT_AMBIGUOUS,
    EXIT_NO_DATA,
    EXIT_STOP,
    EXIT_TARGET,
    EXIT_TIMEOUT,
    EXIT_TRAIL,
    FIXED_VARIANT,
    RETRACE_RATIOS,
    TRAILING_VARIANTS,
    VARIANTS,
    activation_price,
    is_fixed,
    resolve_all,
    trail_net_pnl,
    trail_price,
    variant_label,
)

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5433/agenttrade)"
    ),
)

_T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_PRICE = 100.0
_TARGET_PCT = 2.0
_STOP_PCT = 1.0
_COST_PCT = 0.22


def _bars(rows: list[tuple[float, float, float]], *, start: datetime | None = None,
          step_minutes: int = 1, skip_after: int | None = None) -> list[Bar]:
    """Свечи ``(high, low, close)`` с минутным шагом, начиная с ``t+1``.

    ``skip_after`` пропускает одну минуту после указанного бара — так делается
    РАЗРЫВ РЯДА, и ответ на него обязан быть ``no_data``, а не «ничего не
    сработало».
    """
    first = (start or _T0) + timedelta(minutes=1)
    out: list[Bar] = []
    shift = 0
    for index, (high, low, close) in enumerate(rows):
        if skip_after is not None and index == skip_after:
            shift += 1
        out.append(Bar(
            ts=first + timedelta(minutes=(index + shift) * step_minutes),
            high=high, low=low, close=close,
        ))
    return out


def _flat(minutes: int, price: float = _PRICE) -> list[tuple[float, float, float]]:
    """Ровный ряд без движения — заполнитель хвоста окна."""
    return [(price, price, price)] * minutes


def _resolve(bars: list[Bar], *, horizon_h: int = 1, direction: str = BUY,
             target_pct: float = _TARGET_PCT):
    """Все варианты по одному ряду — с общими для тестов условиями."""
    return {
        (item.activation_ratio, item.retrace_ratio): item
        for item in resolve_all(
            bars,
            signal_ts=_T0,
            horizon_h=horizon_h,
            price_at_signal=_PRICE,
            target_pct=target_pct,
            stop_pct=_STOP_PCT,
            cost_pct=_COST_PCT,
            direction=direction,
            resolution="1m",
        )
    }


# --- §4. Сетка вариантов -----------------------------------------------------

def test_thirteen_variants_and_not_one_more() -> None:
    """Двенадцать сочетаний плюс контрольное. Перечень закрыт (§4 ТЗ)."""
    assert len(ACTIVATION_RATIOS) == 4
    assert len(RETRACE_RATIOS) == 3
    assert len(TRAILING_VARIANTS) == 12
    assert len(VARIANTS) == 13
    assert len(set(VARIANTS)) == 13


def test_fixed_variant_is_marked_by_zeros_and_is_first() -> None:
    """Контрольный вариант — (0, 0), и он же точка отсчёта всех сравнений."""
    assert VARIANTS[0] == FIXED_VARIANT == (0.0, 0.0)
    assert is_fixed(*FIXED_VARIANT)
    assert not any(is_fixed(a, r) for a, r in TRAILING_VARIANTS)
    assert variant_label(*FIXED_VARIANT) == "фиксированная цель"


def test_activation_level_lies_on_the_way_to_the_target() -> None:
    """Уровень включения — доля пути до цели, в ту же сторону, что и цель."""
    for ratio in ACTIVATION_RATIOS:
        buy = activation_price(_PRICE, _TARGET_PCT, ratio, BUY)
        sell = activation_price(_PRICE, _TARGET_PCT, ratio, SELL)
        assert buy == pytest.approx(_PRICE * (1 + _TARGET_PCT * ratio / 100.0))
        assert sell == pytest.approx(_PRICE * (1 - _TARGET_PCT * ratio / 100.0))
        assert _PRICE < buy <= _PRICE * (1 + _TARGET_PCT / 100.0)


def test_trail_level_lies_between_entry_and_peak() -> None:
    """Уровень отката всегда между ценой входа и вершиной — и НИКОГДА не ниже предела.

    Отсюда следует §4.5 в его настоящем виде: предел формально действует всегда,
    но после включения подвижного выхода не срабатывает, потому что цена дошла
    бы до отката раньше.
    """
    peak = 102.0
    stop_price = _PRICE * (1 - _STOP_PCT / 100.0)
    for ratio in RETRACE_RATIOS:
        level = trail_price(_PRICE, peak, ratio, BUY)
        assert _PRICE < level < peak
        assert level > stop_price


# --- §9.5. Вершина достигнута и откат сработал -------------------------------

def test_peak_reached_and_retrace_fires() -> None:
    """Цена дошла до половины цели, вершина зафиксирована, откат сработал.

    Ряд построен так, что ответ известен заранее: вершина 101.0 (ход +1.0%),
    вариант A=0.25 включается на +0.5%, откат R=0.50 сажает выход на 100.5.
    Падение до 100.4 обязано закрыть сделку по ``trail``.
    """
    bars = _bars([
        (100.2, 100.0, 100.2),   # 1: вершина 100.2 — включения ещё нет
        (101.0, 100.2, 101.0),   # 2: вершина 101.0 (+1.0%) — включение A=0.25
        (100.6, 100.4, 100.4),   # 3: откат до 100.4 — выход по trail
    ] + _flat(57, 100.4))
    result = _resolve(bars)

    item = result[(0.25, 0.50)]
    assert item.exit_reason == EXIT_TRAIL
    # Вершина, по которой посчитан выход, — та, что известна ДО бара выхода.
    assert item.peak_pct == pytest.approx(1.0)
    assert item.bars_to_hit == 3
    assert item.net_pnl_pct == pytest.approx(
        trail_net_pnl(1.0, 0.50, _COST_PCT)
    )
    # ...и это ровно «половина пройденного пути минус издержки».
    assert item.net_pnl_pct == pytest.approx(0.5 * 1.0 - _COST_PCT)


def test_peak_inside_a_bar_counts_from_the_next_bar() -> None:
    """Откат от вершины, поставленной ЭТИМ ЖЕ баром, не засчитывается.

    В баре 2 и вершина 101.0, и падение до 100.4. Порядок внутри бара неизвестен:
    в одном сделка закрылась бы по откату, в другом ещё нет. Угадывать нельзя,
    поэтому уровень отката считается от вершины ПРЕДЫДУЩИХ баров — и выход
    случается на баре 3, а не на баре 2.
    """
    bars = _bars([
        (100.2, 100.0, 100.2),
        (101.0, 100.4, 100.5),   # вершина и падение в одном баре
        (100.5, 100.4, 100.4),   # выход здесь
    ] + _flat(57, 100.4))
    item = _resolve(bars)[(0.25, 0.50)]
    assert item.exit_reason == EXIT_TRAIL
    assert item.bars_to_hit == 3


def test_sell_side_is_symmetric() -> None:
    """Для продажи всё то же самое, зеркально: вершина — минимум цены."""
    bars = _bars([
        (100.0, 99.8, 99.8),
        (99.8, 99.0, 99.0),      # вершина 99.0 (+1.0% в пользу продажи)
        (99.6, 99.4, 99.6),      # откат до 99.5 при R=0.50
    ] + _flat(57, 99.6))
    item = _resolve(bars, direction=SELL)[(0.25, 0.50)]
    assert item.exit_reason == EXIT_TRAIL
    assert item.peak_pct == pytest.approx(1.0)
    assert item.net_pnl_pct == pytest.approx(0.5 * 1.0 - _COST_PCT)


# --- §9.5. Вершина не достигнута и сработала цель ----------------------------

def test_target_closes_the_fixed_variant_and_never_a_moving_one() -> None:
    """Цель закрывает сделку у контрольного варианта — и только у него.

    Это не упрощение, а прямое следствие §4.4: уровень включения лежит не
    дальше цели, поэтому цена, дошедшая до цели, уже прошла включение, и
    подвижный выход к этому моменту работает. У двенадцати подвижных вариантов
    исход ``target`` невозможен в принципе — это же утверждение записано
    ограничением БД в миграции 017.
    """
    bars = _bars([
        (100.5, 100.0, 100.5),
        (102.0, 100.5, 102.0),   # цель +2% задета
    ] + _flat(58, 102.0))
    result = _resolve(bars)

    assert result[FIXED_VARIANT].exit_reason == EXIT_TARGET
    assert result[FIXED_VARIANT].net_pnl_pct == pytest.approx(_TARGET_PCT - _COST_PCT)
    assert all(
        result[v].exit_reason != EXIT_TARGET for v in TRAILING_VARIANTS
    )


def test_below_activation_the_ordinary_rule_works() -> None:
    """Пока вершина не дошла до уровня включения, работает обычное правило.

    Ход +0.4% не включает даже самый ранний вариант (A=0.25 требует +0.5%),
    поэтому подвижный выход не срабатывает ни у кого, и все двенадцать доживают
    до срока — ровно как контрольный.
    """
    bars = _bars([(100.4, 100.0, 100.1)] + _flat(59, 100.1))
    result = _resolve(bars)
    assert result[FIXED_VARIANT].exit_reason == EXIT_TIMEOUT
    for variant in TRAILING_VARIANTS:
        assert result[variant].exit_reason == EXIT_TIMEOUT
        # Итог у всех один и тот же: сделка дожила до срока и закрыта по цене.
        assert result[variant].net_pnl_pct == pytest.approx(
            result[FIXED_VARIANT].net_pnl_pct
        )


# --- §9.5. Предел раньше включения -------------------------------------------

def test_stop_before_activation() -> None:
    """Предел убытка задет до включения — исход ``stop`` у всех вариантов."""
    bars = _bars([
        (100.1, 100.0, 100.0),
        (100.1, 98.9, 99.0),     # предел −1% задет
    ] + _flat(58, 99.0))
    result = _resolve(bars)
    assert result[FIXED_VARIANT].exit_reason == EXIT_STOP
    for variant in TRAILING_VARIANTS:
        item = result[variant]
        assert item.exit_reason == EXIT_STOP
        assert item.bars_to_hit == 2
        assert item.net_pnl_pct == pytest.approx(-_STOP_PCT - _COST_PCT)


def test_stop_and_activation_in_one_bar_is_ambiguous() -> None:
    """Включение и предел в одном баре — порядок неизвестен, и он не угадывается.

    В одном порядке сделка закрылась бы по пределу, в другом дожила бы до
    подвижного выхода. Признать неизвестность честнее, чем выбрать ветку,
    написанную первой, — то же основание, что у ``ambiguous`` в Этапе 8.8.
    """
    bars = _bars([
        (101.0, 98.9, 99.0),     # и ход +1%, и предел −1% в одном баре
    ] + _flat(59, 99.0))
    result = _resolve(bars)

    # Ход +1% достигает уровня включения у A=0.25 (+0.5%) и A=0.50 (+1.0%):
    # для них порядок событий внутри бара решает исход, и он неизвестен.
    for activation in (0.25, 0.50):
        for retrace in RETRACE_RATIOS:
            item = result[(activation, retrace)]
            assert item.exit_reason == EXIT_AMBIGUOUS
            assert item.net_pnl_pct is None
            assert item.hit_at is None

    # А у A=0.75 (+1.5%) и A=1.00 (+2.0%) включения не случилось бы ни при каком
    # порядке — неизвестности нет, и выдумывать ambiguous там не за чем.
    for activation in (0.75, 1.00):
        for retrace in RETRACE_RATIOS:
            assert result[(activation, retrace)].exit_reason == EXIT_STOP

    # Контрольный вариант видит своё: цель +2% не задета, предел задет.
    assert result[FIXED_VARIANT].exit_reason == EXIT_STOP


def test_stop_never_fires_after_activation() -> None:
    """После включения предел не срабатывает: откат стоит выше него.

    Проверяется не обещанием, а рядом, где цена после включения падает СРАЗУ
    ниже предела: сделка обязана закрыться по откату, потому что уровень отката
    цена прошла раньше.
    """
    bars = _bars([
        (102.0, 100.0, 102.0),   # включение всех вариантов
        (102.0, 98.0, 98.0),     # провал ниже предела
    ] + _flat(58, 98.0))
    result = _resolve(bars)
    for variant in TRAILING_VARIANTS:
        assert result[variant].exit_reason == EXIT_TRAIL
        assert result[variant].net_pnl_pct > 0


# --- §9.5. Разрыв ряда -------------------------------------------------------

def test_a_gap_in_the_series_gives_no_data() -> None:
    """Пропущенная минута — ``no_data`` у всех вариантов, а не «ничего не сработало».

    Объявить timeout на неполном окне значило бы сказать «ни одна граница не
    задета» о минутах, которых никто не видел.
    """
    bars = _bars(_flat(60), skip_after=10)
    result = _resolve(bars)
    for variant in VARIANTS:
        item = result[variant]
        assert item.exit_reason == EXIT_NO_DATA
        assert item.net_pnl_pct is None
        assert item.bars_seen < item.bars_expected


def test_an_exit_before_the_gap_stays_a_fact() -> None:
    """Выход, случившийся ДО разрыва, остаётся фактом — как и в Этапе 8.8."""
    rows = [
        (101.0, 100.0, 101.0),
        (101.0, 100.4, 100.4),   # выход по откату при A=0.25, R=0.50
    ] + _flat(58, 100.4)
    bars = _bars(rows, skip_after=20)
    item = _resolve(bars)[(0.25, 0.50)]
    assert item.exit_reason == EXIT_TRAIL
    assert item.bars_to_hit == 2


def test_an_empty_window_is_no_data() -> None:
    result = _resolve([])
    for variant in VARIANTS:
        assert result[variant].exit_reason == EXIT_NO_DATA


# --- §4. Контрольный вариант обязан совпасть с Этапом 8.8 --------------------

def _random_series(seed: int, minutes: int = 60) -> list[Bar]:
    """Случайное блуждание — ряд без заранее известного ответа.

    Именно поэтому он и нужен: совпадение контрольного варианта с 8.8 надо
    проверять на рядах, которые никто не подбирал под ответ.
    """
    import random

    rng = random.Random(seed)
    price = _PRICE
    rows: list[tuple[float, float, float]] = []
    for _ in range(minutes):
        step = rng.uniform(-0.35, 0.35)
        close = price + step
        high = max(price, close) + abs(rng.uniform(0.0, 0.25))
        low = min(price, close) - abs(rng.uniform(0.0, 0.25))
        rows.append((high, low, close))
        price = close
    return _bars(rows)


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("direction", [BUY, SELL])
def test_control_variant_equals_stage_8_8_exactly(seed: int, direction: str) -> None:
    """Тринадцатый вариант обязан совпасть с правилом 8.8 ДО ПОСЛЕДНЕГО ЗНАКА.

    Не совпал — значит, правила касания разошлись, и сравнение вариантов
    недействительно целиком (§4 ТЗ). Проверяется на 50 случайных рядах, где
    встречаются все пять исходов 8.8.
    """
    bars = _random_series(seed)
    expected = resolve(
        bars, signal_ts=_T0, horizon_h=1, price_at_signal=_PRICE,
        target_pct=_TARGET_PCT, stop_pct=_STOP_PCT, cost_pct=_COST_PCT,
        direction=direction, resolution="1m",
    )
    item = _resolve(bars, direction=direction)[FIXED_VARIANT]
    assert item.exit_reason == expected.outcome
    assert item.hit_at == expected.hit_at
    assert item.bars_to_hit == expected.bars_to_hit
    assert item.net_pnl_pct == expected.net_pnl_pct
    assert item.mae_pct == expected.mae_pct
    assert item.mfe_pct == expected.mfe_pct
    assert item.resolution == expected.resolution
    assert item.bars_seen == expected.bars_seen
    assert item.bars_expected == expected.bars_expected


def test_window_bounds_are_the_same_for_every_variant() -> None:
    """Окно, mae и mfe у всех тринадцати вариантов одни и те же.

    Иначе варианты сравнивались бы на разных отрезках ряда, и разница отражала
    бы длину окна, а не правило выхода.
    """
    result = _resolve(_random_series(7))
    seen = {(i.mae_pct, i.mfe_pct, i.bars_seen, i.bars_expected)
            for i in result.values()}
    assert len(seen) == 1


def test_the_decision_bar_is_not_in_the_window() -> None:
    """Свеча момента решения в окно не входит — правило то же, что в 8.8.

    Ряд, начатый С бара решения, а не со следующего, обрезается по началу окна,
    и первая же свеча оказывается не на месте — значит, покрытия нет.
    """
    rows = [(105.0, 95.0, 100.0)] + _flat(60)
    bars = [Bar(ts=_T0 + timedelta(minutes=i), high=h, low=lo, close=c)
            for i, (h, lo, c) in enumerate(rows)]
    result = _resolve(bars)
    # Экстремумы бара решения не влияют: ни цель, ни предел по нему не задеты.
    assert result[FIXED_VARIANT].exit_reason == EXIT_TIMEOUT


# --- §4. Арифметика итога ----------------------------------------------------

def test_trail_result_is_exactly_the_remaining_path() -> None:
    """``net_pnl = (1 − R) × вершина − издержки`` — точное равенство, не приближение."""
    assert trail_net_pnl(2.0, 0.20, 0.22) == pytest.approx(0.8 * 2.0 - 0.22)
    assert trail_net_pnl(2.0, 0.50, 0.22) == pytest.approx(0.5 * 2.0 - 0.22)


def test_every_trail_row_satisfies_the_arithmetic() -> None:
    """То же равенство — на посчитанных рядах, а не только в формуле.

    Это же равенство проверяет ``deploy/verify_8_10.sh`` на живой таблице: если
    оно перестанет выполняться, значит, выход посчитан не на том уровне, о
    котором написано в отчёте.
    """
    for seed in range(20):
        for direction in (BUY, SELL):
            for item in _resolve(_random_series(seed), direction=direction).values():
                if item.exit_reason != EXIT_TRAIL:
                    continue
                assert item.net_pnl_pct == pytest.approx(
                    (1.0 - item.retrace_ratio) * item.peak_pct - _COST_PCT
                )
                # Вершина к моменту включения обязана быть не меньше уровня A.
                assert item.peak_pct >= item.activation_ratio * _TARGET_PCT - 1e-12


def test_later_activation_never_exits_earlier() -> None:
    """Чем позже включается подвижный выход, тем позже он может сработать.

    Свойство самого правила: уровень включения выше — значит, включение
    наступает не раньше, а выход не может случиться до включения. Нарушение
    означало бы, что варианты перепутаны местами.
    """
    for seed in range(15):
        result = _resolve(_random_series(seed))
        for retrace in RETRACE_RATIOS:
            bars_to_hit = []
            for activation in ACTIVATION_RATIOS:
                item = result[(activation, retrace)]
                bars_to_hit.append(
                    item.bars_to_hit if item.exit_reason == EXIT_TRAIL else 10**9
                )
            assert bars_to_hit == sorted(bars_to_hit)


# --- §2. Жёсткая граница этапа ----------------------------------------------

def test_logic_version_is_not_raised() -> None:
    """LOGIC_VERSION остаётся 5: этап измеряет, а не меняет систему."""
    assert settings.LOGIC_VERSION == 5


def test_touch_rule_is_not_reimplemented() -> None:
    """Правило касания берётся из 8.8 как есть — второй реализации нет (§2 ТЗ).

    Проверка текстовая и намеренно грубая: она ловит именно то, что запрещено,
    — появление собственного правила касания в пакете подвижного выхода.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in (root / "src" / "trailing").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "def resolve(" not in text, path
        assert "def _touches(" not in text, path
        assert "def _excursions(" not in text, path
        assert "def net_pnl(" not in text, path


def test_trailing_is_not_imported_by_the_hot_path() -> None:
    """Решение, уведомления, агенты, оценщик и цели о подвижном выходе не знают."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for package in ("agents", "decision", "notify", "evaluator", "risk"):
        for path in (root / "src" / package).rglob("*.py"):
            assert "src.trailing" not in path.read_text(encoding="utf-8"), path


def test_barrier_outcomes_module_is_untouched_by_this_stage() -> None:
    """``src/barrier/outcomes.py`` не знает ни о каком подвижном выходе.

    §2 ТЗ запрещает его переписывать: сравнение действительно только при
    одинаковых правилах касания, а править файл, которым посчитана таблица 8.8,
    значит менять эталон под измерение.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "src" / "barrier" / "outcomes.py").read_text(encoding="utf-8")
    assert "trail" not in text
    assert "activation" not in text


def test_trailing_outcomes_is_protected_from_retention() -> None:
    """Таблицу этапа политика хранения не удаляет никогда (§6 ТЗ)."""
    from scripts import retention

    assert "trailing_outcomes" in retention.PROTECTED_TABLES
    assert "signal_outcomes_barrier" in retention.PROTECTED_TABLES


def test_no_new_settings_for_stop_or_cost() -> None:
    """У этапа НЕТ своих предела и издержек — только общие с системой.

    Отдельные ключи позволили бы сравнить подвижный выход при одном пределе с
    фиксированной целью при другом, и разница отражала бы разные условия, а не
    разные правила.
    """
    fields = set(type(settings).model_fields)
    assert "TRAILING_STOP_PCT" not in fields
    assert "TRAILING_COST_ROUNDTRIP_PCT" not in fields


# --- §5. Три защиты от подгонки ---------------------------------------------

def test_the_five_required_phrases_are_verbatim() -> None:
    """Формулировки §5 ТЗ — дословно, и других нет.

    Третья формулировка интервала взята ДОСЛОВНО из Этапа 8.9: это тот же
    вопрос о том же, и два разных слова для одного состояния читались бы как
    два разных состояния.
    """
    from scripts.baseline_bootstrap import VERDICT_UNKNOWN as STAGE_8_9_UNKNOWN
    from scripts.trailing_stats import (
        VERDICT_BETTER,
        VERDICT_INDISTINGUISHABLE,
        VERDICT_NOT_CONFIRMED,
        VERDICT_UNKNOWN,
        VERDICT_WORSE,
    )

    assert VERDICT_INDISTINGUISHABLE == "варианты неразличимы, победитель случаен"
    assert VERDICT_NOT_CONFIRMED == (
        "преимущество не подтверждается на независимых данных"
    )
    assert VERDICT_UNKNOWN == "различить нельзя, выборки не хватает"
    assert VERDICT_UNKNOWN == STAGE_8_9_UNKNOWN
    assert VERDICT_BETTER == "подвижный выход лучше, интервал не пересекает ноль"
    assert VERDICT_WORSE == "подвижный выход хуже, интервал не пересекает ноль"


def test_identical_variants_are_declared_indistinguishable() -> None:
    """Тринадцать одинаковых столбцов — разброс ноль, победителя нет."""
    import numpy as np

    from scripts.trailing_stats import spread_permutation

    rng = np.random.default_rng(3)
    column = rng.normal(0.0, 1.0, 200)
    values = np.repeat(column[:, None], len(VARIANTS), axis=1)
    observed, p95, _cloud = spread_permutation(values, resamples=500, seed=1)
    assert observed == pytest.approx(0.0)
    assert observed <= p95


def test_pure_noise_variants_are_declared_indistinguishable() -> None:
    """Двенадцать бессмысленных правил на шуме: разброс не выходит за случайный.

    Это и есть главный страх §5 ТЗ — «победитель» находится всегда. Здесь
    наблюдённый разброс заведомо больше нуля, но он обязан теряться среди
    перестановочного облака.
    """
    import numpy as np

    from scripts.trailing_stats import spread_permutation

    rng = np.random.default_rng(11)
    values = rng.normal(0.0, 1.0, (300, len(VARIANTS)))
    observed, p95, _cloud = spread_permutation(values, resamples=500, seed=2)
    assert observed > 0.0
    assert observed <= p95


def test_a_real_difference_is_not_hidden_by_the_permutation_test() -> None:
    """Настоящее различие проверка обязана увидеть, иначе она бесполезна."""
    import numpy as np

    from scripts.trailing_stats import spread_permutation

    rng = np.random.default_rng(5)
    values = rng.normal(0.0, 1.0, (300, len(VARIANTS)))
    values[:, 3] += 1.0
    observed, p95, _cloud = spread_permutation(values, resamples=500, seed=4)
    assert observed > p95


def test_bootstrap_finds_a_real_difference() -> None:
    import numpy as np

    from scripts.trailing_stats import bootstrap_diff, verdict

    rng = np.random.default_rng(7)
    fixed = rng.normal(0.0, 1.0, 400)
    moving = fixed + 0.8
    observed, lo, hi = bootstrap_diff(moving, fixed, resamples=2000, seed=7)
    assert observed == pytest.approx(0.8, abs=1e-9)
    assert verdict(lo, hi) == "подвижный выход лучше, интервал не пересекает ноль"


def test_bootstrap_admits_when_it_cannot_tell() -> None:
    """Признание «данных не хватает» — самый частый и самый честный ответ."""
    import numpy as np

    from scripts.trailing_stats import VERDICT_UNKNOWN, bootstrap_diff, verdict

    rng = np.random.default_rng(9)
    fixed = rng.normal(0.0, 5.0, 40)
    moving = fixed + rng.normal(0.0, 5.0, 40)
    _observed, lo, hi = bootstrap_diff(moving, fixed, resamples=2000, seed=9)
    assert verdict(lo, hi) == VERDICT_UNKNOWN


def test_bootstrap_is_paired_not_independent() -> None:
    """На идеально спаренных данных интервал обязан быть нулевой ширины.

    Независимая пересборка дала бы здесь широкий интервал — то есть объявила бы
    «различить нельзя» там, где различие точное.
    """
    import numpy as np

    from scripts.trailing_stats import bootstrap_diff

    rng = np.random.default_rng(13)
    fixed = rng.normal(0.0, 5.0, 300)
    moving = fixed + 0.5
    observed, lo, hi = bootstrap_diff(moving, fixed, resamples=2000, seed=13)
    assert (observed, lo, hi) == pytest.approx((0.5, 0.5, 0.5), abs=1e-9)


def test_bootstrap_is_reproducible() -> None:
    """Тот же вход и то же зерно — те же границы. Иначе интервал не проверить."""
    import numpy as np

    from scripts.trailing_stats import bootstrap_diff

    rng = np.random.default_rng(17)
    fixed = rng.normal(0.0, 2.0, 250)
    moving = fixed + rng.normal(0.1, 2.0, 250)
    first = bootstrap_diff(moving, fixed, resamples=1000, seed=21)
    second = bootstrap_diff(moving, fixed, resamples=1000, seed=21)
    assert first == second


def test_bootstrap_refuses_mismatched_sides() -> None:
    import numpy as np

    from scripts.trailing_stats import bootstrap_diff

    with pytest.raises(ValueError, match="одной длины"):
        bootstrap_diff(np.zeros(3), np.zeros(4), resamples=10, seed=1)


def test_best_variant_never_selects_the_control() -> None:
    """«Победитель» ищется среди подвижных: контроль — точка отсчёта, не участник."""
    import numpy as np

    from scripts.trailing_stats import best_variant

    values = np.zeros((50, len(VARIANTS)))
    values[:, VARIANTS.index(FIXED_VARIANT)] = 100.0
    assert VARIANTS[best_variant(values)] != FIXED_VARIANT


def test_split_half_reports_when_the_winner_does_not_repeat() -> None:
    """Победитель первой половины, проигравший на второй, обязан быть назван.

    Данные построены так: на первой половине выигрывает один вариант, на второй
    — другой, и оба выигрыша чисто случайны по построению.
    """
    import numpy as np

    from scripts.trailing_stats import best_variant

    values = np.zeros((200, len(VARIANTS)))
    values[:100, 5] = 1.0
    values[100:, 7] = 1.0
    first_winner = best_variant(values[:100])
    second_winner = best_variant(values[100:])
    assert first_winner != second_winner
    # Именно это условие и печатает вердикт §5.3.
    assert float(values[100:, first_winner].mean()) <= 0.0


# --- §9.5. Проверки, которым нужна база --------------------------------------

async def _refuse_if_table_has_data(conn) -> None:
    """Останавливает разрушительную проверку, если в таблице есть данные.

    Откат миграции УДАЛЯЕТ таблицу целиком. Тест, направленный на базу с
    посчитанными вариантами, стёр бы их молча. Проверка не «старается быть
    аккуратной», а ОТКАЗЫВАЕТСЯ выполняться.
    """
    if await conn.fetchval("SELECT to_regclass('trailing_outcomes');") is None:
        return
    rows = await conn.fetchval("SELECT count(*) FROM trailing_outcomes;")
    if rows:
        pytest.skip(
            f"в trailing_outcomes {rows} строк: откат миграции удалил бы их. "
            "AT_TEST_DSN обязан указывать на ОДНОРАЗОВУЮ базу"
        )


async def _seed_signal(conn) -> tuple[int, int]:
    """Инструмент и один направленный сигнал версии 5 для проверок схемы."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', 'TEST810/USDT', 'TEST810', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE SET symbol = EXCLUDED.symbol "
        "RETURNING id;"
    )
    signal_id = await conn.fetchval(
        "SELECT id FROM signals WHERE instrument_id = $1 LIMIT 1;", instrument_id
    )
    if signal_id is None:
        signal_id = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
            "VALUES ($1, $2, 'buy', 5) RETURNING id;",
            instrument_id, datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        )
    return instrument_id, signal_id


@needs_db
async def test_migration_is_idempotent_and_reversible() -> None:
    """Миграция применяется, применяется повторно, откатывается и снова встаёт.

    ВНИМАНИЕ: тест РАЗРУШИТЕЛЬНЫЙ — он удаляет таблицу.
    """
    import pathlib

    import asyncpg

    root = pathlib.Path(__file__).resolve().parent.parent
    forward = (root / "db/migrations/017_trailing_outcomes.sql").read_text("utf-8")
    back = (root / "db/migrations/017_trailing_outcomes_rollback.sql").read_text("utf-8")

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _refuse_if_table_has_data(conn)
        await conn.execute(forward)
        await conn.execute(forward)
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'trailing_outcomes'::regclass AND contype = 'c';"
        )
        assert count >= 7
        await conn.execute(back)
        assert await conn.fetchval("SELECT to_regclass('trailing_outcomes');") is None
        await conn.execute(forward)
        assert await conn.fetchval("SELECT to_regclass('trailing_outcomes');")
    finally:
        await conn.close()


@needs_db
async def test_thirteen_variants_coexist_on_the_same_pair() -> None:
    """Тринадцать строк на одной паре: ключ включает параметры варианта.

    Если бы ключ был (signal_id, horizon_h), двенадцать вариантов вытеснили бы
    друг друга, и таблица молча хранила бы один вариант вместо тринадцати.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        _instrument_id, signal_id = await _seed_signal(conn)
        insert = (
            "INSERT INTO trailing_outcomes (signal_id, horizon_h, activation_ratio,"
            " retrace_ratio, logic_version, direction, price_at_signal, target_pct,"
            " stop_pct, cost_pct, exit_reason, peak_pct, mae_pct, mfe_pct,"
            " resolution) VALUES ($1, 4, $2, $3, 5, 'buy', 100, 2, 1, 0.22,"
            " 'no_data', 0, 0.5, 0.5, '1m');"
        )
        for activation, retrace in VARIANTS:
            await conn.execute(insert, signal_id, activation, retrace)
        n = await conn.fetchval(
            "SELECT count(*) FROM trailing_outcomes WHERE signal_id = $1;", signal_id
        )
        assert n == 13
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, signal_id, *VARIANTS[0])
    finally:
        await conn.execute(
            "DELETE FROM trailing_outcomes WHERE signal_id = $1;", signal_id
        )
        await conn.close()


@needs_db
async def test_database_refuses_invented_values() -> None:
    """Ограничения ловят подделку раньше, чем она попадёт в отчёт."""
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        _instrument_id, signal_id = await _seed_signal(conn)
        base = (
            "INSERT INTO trailing_outcomes (signal_id, horizon_h, activation_ratio,"
            " retrace_ratio, logic_version, direction, price_at_signal, target_pct,"
            " stop_pct, cost_pct, exit_reason, hit_at, bars_to_hit, net_pnl_pct,"
            " peak_pct, mae_pct, mfe_pct, resolution) VALUES ($1, 4, $2, $3, 5,"
            " 'buy', 100, 2, 1, 0.22, $4, $5, $6, $7, 0, 0.5, 0.5, '1m');"
        )
        moment = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
        # Вариант вне сетки §4 — подобранный параметр.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.40, 0.25, "timeout",
                               None, None, 1.0)
        # Причина выхода, названная своими словами.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.25, 0.20, "почти откат",
                               moment, 5, 1.0)
        # Откат у варианта, где подвижного выхода нет.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.00, 0.00, "trail",
                               moment, 5, 1.0)
        # Цель, закрывшая подвижный вариант, — правило реализовано не так.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.50, 0.33, "target",
                               moment, 5, 1.0)
        # Выход по откату без момента выхода.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.25, 0.20, "trail",
                               None, None, 1.0)
        # Неизвестный исход с результатом в деньгах.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, signal_id, 0.25, 0.20, "no_data",
                               None, None, 1.0)
    finally:
        await conn.execute(
            "DELETE FROM trailing_outcomes WHERE signal_id = $1;", signal_id
        )
        await conn.close()
