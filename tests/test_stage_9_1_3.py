"""Этап 9.1.3: замер подвижного выхода на фактических виртуальных позициях.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ЭТАП ЗАМЕРНЫЙ, И ГЛАВНАЯ ОПАСНОСТЬ ЗАМЕРА — ПРАВДОПОДОБНОЕ ЧИСЛО. Неверно
посчитанный прирост выглядит ровно так же, как верный: у него нет ни исключения,
ни пустого места, ни красной строки в журнале. Поэтому проверяется не «получилось
ли число», а три вещи, каждая из которых способна сделать число ложным:

 1. ОКНО. Контроль и двенадцать подвижных вариантов обязаны считаться по ОДНОМУ
    И ТОМУ ЖЕ отрезку ряда. Съехавшее на один бар окно дало бы разницу окон,
    выданную за разницу правил. Совпадение не предполагается — оно сверяется
    (``check_window``), и здесь проверяется, что сверка ПАДАЕТ при расхождении.
 2. КОНТРОЛЬ. Пересчёт живым правилом обязан воспроизвести уже записанное в
    ``positions`` до последнего знака. Не воспроизвёл — сравнение вариантов не
    публикуется вовсе, код возврата 2. Если расчёт не умеет повторить
    случившееся, его числа по НЕ случившимся вариантам не стоят ничего.
 3. СОСТАВ ВЫБОРКИ. ``data_gap`` и открытые позиции не должны попадать в замер
    ни при каких обстоятельствах: у первых цена выхода восстановлена, у вторых
    исход ещё не наступил.

ДВОЙНИК БАЗЫ ЗДЕСЬ НЕ МЯГЧЕ НАСТОЯЩЕЙ (§11.2 ТЗ). Он ВЫПОЛНЯЕТ SQL настоящих
методов ``DB`` и сверяет каждую колонку с составом таблиц, вычитанным ИЗ ФАЙЛОВ
МИГРАЦИЙ (``tests/schema_double.py``). На Этапе 9.1.2.2 двойник подменял метод
целиком, SQL не выполнялся вовсе, и обращение к несуществующей колонке
``positions.symbol`` прошло все проверки и упало на боевой базе.

ЧЕГО ЭТИ ПРОВЕРКИ НЕ ДОКАЗЫВАЮТ. Ни одна из них не запускалась на настоящей
базе и на настоящих свечах: ряды здесь придуманы, и придуманы так, чтобы ответ
был известен заранее. Совпадение контроля с фактом на боевых данных — это то,
что покажет первый прогон на сервере, и заменить его синтетикой нельзя.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from src.barrier.outcomes import Bar
from src.shadow.trailing import (
    CONTROL_VARIANT,
    armed_scan,
    check_window,
    closing_moment,
    resolve_position,
    variant_name,
)
from src.trailing.rule import ACTIVATION_RATIOS, RETRACE_RATIOS, TRAILING_VARIANTS
from tests.schema_double import (
    SchemaPool,
    UndefinedColumn,
    check_sql_columns,
    project,
    schema,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# Позиция-образец: вход по 100, цель +2%, предел −1%, час горизонта, слот $2.
# Круглые числа выбраны затем, чтобы ответ был виден глазом, а не только
# распечатан: цель 102.00, предел 99.00, порог включения A=0.25 — 100.50.
OPENED = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
HORIZON_H = 1
DEADLINE = OPENED + timedelta(hours=HORIZON_H)
ENTRY = 100.0
TARGET_PCT = 2.0
STOP_PCT = 1.0
COST_PCT = 0.22
SLOT = 2.0
BARS_IN_WINDOW = 60


def _bars(path: list[tuple[float, float, float]]) -> list[Bar]:
    """Ряд минутных баров окна из троек ``(high, low, close)``."""
    return [
        Bar(ts=OPENED + timedelta(minutes=i), high=h, low=lo, close=c)
        for i, (h, lo, c) in enumerate(path)
    ]


def _flat(high: float, low: float, close: float, count: int
          ) -> list[tuple[float, float, float]]:
    return [(high, low, close)] * count


def _resolve(bars: list[Bar], **over: Any):
    kwargs: dict[str, Any] = {
        "opened_at": OPENED, "deadline_at": DEADLINE, "horizon_h": HORIZON_H,
        "entry_price": ENTRY, "target_pct": TARGET_PCT, "stop_pct": STOP_PCT,
        "cost_pct": COST_PCT, "notional_usd": SLOT, "resolution": "1m",
    }
    kwargs.update(over)
    return resolve_position(bars, **kwargs)


# =============================================================================
# §7.1–§7.4. Правило на придуманных рядах с заранее известным ответом
# =============================================================================

def test_a_straight_drop_to_the_stop_arms_nothing_and_changes_nothing() -> None:
    """§7.1: цена сразу идёт вниз до предела.

    ГЛАВНОЕ СВОЙСТВО МЕХАНИЗМА, И ОНО ИЗМЕРЯЕТСЯ ПРЯМО. Подвижная цель поднимает
    пол под УЖЕ полученной прибылью. Сделка, ушедшая против сигнала, до порога
    включения не доходит — значит цель не сдвинется ни разу, и прирост равен
    нулю ПО ПОСТРОЕНИЮ, а не по совпадению. Это и есть ответ владельцу на
    вопрос «не двигать ли цель вместо предела».
    """
    bars = _bars([(100.2, 98.5, 98.7)] + _flat(99.0, 98.0, 98.5, 59))
    shadow = _resolve(bars)

    assert shadow.control is not None
    assert shadow.control.exit_reason == "stop"
    assert len(shadow.variants) == 12
    assert all(not v.armed for v in shadow.variants), "механизм задет на падении"
    assert all(v.armed_at is None for v in shadow.variants)
    assert all(v.exit_reason == "stop" for v in shadow.variants)
    deltas = {
        round(v.net_pnl_pct - shadow.control.net_pnl_pct, 9)
        for v in shadow.variants
    }
    assert deltas == {0.0}, f"прирост не ноль: {deltas}"


def test_a_clean_run_to_target_never_exits_before_the_target_bar() -> None:
    """§7.2: цена доходит до цели без откатов — подвижный выход не срабатывает раньше.

    ОШИБКА В ТЗ, НАЗВАННАЯ, А НЕ ОБОЙДЁННАЯ. §7.2 требует у подвижных вариантов
    «исход target». Такого исхода у них НЕ БЫВАЕТ и быть не может: уровень
    включения A ≤ 1 лежит не дальше цели, поэтому цена, дошедшая до цели, уже
    прошла порог включения и подвижный выход к тому моменту работает. Это не
    особенность реализации 9.1.3, а документированное свойство правила Этапа
    8.10 (заголовок ``src/trailing/rule.py``, пункт 2), записанное ограничением
    БД ``trailing_outcomes_reason_variant_chk`` и повторённое в миграции 021.

    Проверяемая половина требования — «не срабатывает раньше цели» — верна, и
    проверяется здесь: ни один подвижный вариант не вышел по откату раньше того
    бара, на котором контроль взял цель.
    """
    # Ровный подъём без откатов: каждый следующий бар целиком выше предыдущего.
    path = [(100.0 + 0.05 * i, 100.0 + 0.05 * i - 0.02, 100.0 + 0.05 * i)
            for i in range(1, BARS_IN_WINDOW + 1)]
    shadow = _resolve(_bars(path))

    assert shadow.control is not None
    assert shadow.control.exit_reason == "target"
    target_bar = shadow.control.exit_bar_ts
    assert target_bar is not None

    for row in shadow.variants:
        assert row.exit_reason != "target", (
            "у подвижного варианта исход target невозможен — см. докстринг"
        )
        if row.exit_reason == "trail":
            assert row.exit_bar_ts >= target_bar, (
                f"{row.variant} вышел по откату раньше цели: "
                f"{row.exit_bar_ts} < {target_bar}"
            )


def test_a_reversal_at_sixty_percent_arms_only_the_two_lower_levels() -> None:
    """§7.3: вершина на 60% пути к цели включает A=0.25 и A=0.50, но не выше.

    Порог включения — доля A от цели: при цели +2% это +0.50%, +1.00%, +1.50% и
    +2.00%. Вершина +1.20% (цена 101.20) проходит первые два и не доходит до
    двух верхних. Числа подобраны так, что ответ проверяется в уме.
    """
    path = (
        [(100.4, 100.3, 100.35)]           # ниже первого порога
        + [(101.2, 100.5, 101.0)]          # вершина 101.20 = 60% пути
        + _flat(100.6, 99.2, 99.4, 58)     # разворот вниз
    )
    shadow = _resolve(_bars(path))

    by_activation: dict[float, set[bool]] = {}
    for row in shadow.variants:
        by_activation.setdefault(row.activation_frac, set()).add(row.armed)

    assert by_activation[0.25] == {True}, "A=0.25 не включился на 60% пути"
    assert by_activation[0.50] == {True}, "A=0.50 не включился на 60% пути"
    assert by_activation[0.75] == {False}, "A=0.75 включился, не дойдя до порога"
    assert by_activation[1.00] == {False}, "A=1.00 включился, не дойдя до порога"

    # Момент задетости — бар, на котором порог достигнут, а не первый бар окна.
    armed_at = {r.armed_at for r in shadow.variants if r.armed}
    assert armed_at == {OPENED + timedelta(minutes=1)}


def test_the_short_pullback_leaves_earlier_and_at_a_better_price() -> None:
    """§7.4: короткий откат R=0.20 выходит раньше и по лучшей цене, чем R=0.50.

    Это арифметическое следствие, а не наблюдение: уровень выхода стоит на доле
    R пройденного пути от вершины, поэтому при меньшем R он выше и достигается
    раньше при том же снижении. Проверка нужна затем, чтобы поймать
    перепутанный знак или перепутанные местами параметры — ошибку, которая на
    боевых данных выглядела бы как содержательный вывод.
    """
    path = (
        [(100.4, 100.3, 100.35)]
        + [(101.2, 100.9, 101.1)]          # вершина 101.20
        + _flat(101.0, 100.0, 100.2, 58)   # плавный откат вниз
    )
    shadow = _resolve(_bars(path))

    short = next(r for r in shadow.variants if r.variant == variant_name(0.25, 0.20))
    long = next(r for r in shadow.variants if r.variant == variant_name(0.25, 0.50))
    assert short.exit_reason == "trail" and long.exit_reason == "trail"
    assert short.exit_bar_ts <= long.exit_bar_ts, "короткий откат вышел позже"
    assert short.exit_price > long.exit_price, "короткий откат вышел хуже"
    assert short.net_pnl_pct > long.net_pnl_pct


def test_the_usd_result_comes_from_the_positions_own_slot() -> None:
    """§3.2: итог в долларах считается от ``notional_usd`` ФАКТИЧЕСКОЙ позиции.

    Константа вместо слота превратила бы замер в оценку — и сделала бы это
    молча: числа остались бы правдоподобными.
    """
    bars = _bars([(100.2, 98.5, 98.7)] + _flat(99.0, 98.0, 98.5, 59))
    for slot in (2.0, 7.5):
        shadow = _resolve(bars, notional_usd=slot)
        assert shadow.control is not None
        expected = slot * shadow.control.net_pnl_pct / 100.0
        assert shadow.control.net_pnl_usd == pytest.approx(expected)


# =============================================================================
# §7.5. Контроль воспроизводит записанный исход
# =============================================================================

def test_the_control_reproduces_the_recorded_outcome_exactly() -> None:
    """§7.5: контроль повторяет ``exit_reason``, ``exit_bar_ts``, ``exit_price``
    и ``net_pnl_pct`` в точности.

    «В точности» здесь не фигура речи: контроль — это ПРЯМОЙ вызов той же
    функции ``src.positions.rules.check_exit``, которой закрыты настоящие
    позиции, а не повторение её логики. Поэтому сравнение идёт без допуска.
    """
    bars = _bars([(100.4, 100.3, 100.35)] + _flat(102.5, 101.0, 102.2, 59))
    shadow = _resolve(bars)
    assert shadow.control is not None

    # Записанный факт такой позиции: цель взята на втором баре окна.
    assert shadow.control.exit_reason == "target"
    assert shadow.control.exit_bar_ts == OPENED + timedelta(minutes=1)
    assert shadow.control.exit_price == pytest.approx(102.0)
    assert shadow.control.net_pnl_pct == pytest.approx(TARGET_PCT - COST_PCT)
    assert shadow.control.variant == CONTROL_VARIANT
    assert shadow.control.activation_frac is None
    assert shadow.control.pullback_frac is None
    assert shadow.control.armed is False


# =============================================================================
# Правка после боевого прогона: момент закрытия, область задетости, счётчики
# =============================================================================

def test_the_closing_moment_is_the_bar_close_not_the_bar_open() -> None:
    """Момент закрытия — ЗАКРЫТИЕ бара выхода, а не его открытие.

    ЭТА ПРОВЕРКА НАПИСАНА ПОСЛЕ БОЕВОГО ПРОГОНА, КОТОРЫЙ ЕЁ ПОТРЕБОВАЛ. Все
    одиннадцать позиций разошлись ровно на 60 секунд: ``check_exit`` возвращает
    время ОТКРЫТИЯ бара, а ``runner`` пишет в ``closed_at`` время его ЗАКРЫТИЯ.
    Сверялись разные величины при нулевом допуске.

    ПОДМЕНА ОДНОГО ДРУГИМ РОНЯЕТ ЭТУ ПРОВЕРКУ — в этом её смысл.
    """
    bar_open = datetime(2026, 8, 31, 1, 5, tzinfo=UTC)
    assert closing_moment(bar_open, "1m") == bar_open + timedelta(minutes=1)
    assert closing_moment(bar_open, "1h") == bar_open + timedelta(hours=1)
    # Величины РАЗНЫЕ, и разница — ровно длина бара.
    assert closing_moment(bar_open, "1m") != bar_open


def test_the_closing_rule_still_matches_the_live_runner() -> None:
    """Правило ``runner`` не изменилось: ``closed_at = бар выхода + 60 секунд``.

    ЗДЕСЬ ДВА МЕСТА ЗНАЮТ ОДНО И ТО ЖЕ, и это признано, а не спрятано.
    ``src/positions/runner.py`` считает момент закрытия литералом 60 секунд;
    :func:`closing_moment` выводит его из разрешения бара. Сегодня они совпадают
    (``positions_resolution_chk`` допускает только ``1m``), но совпадение это не
    вечное — и потому проверяется по ТЕКСТУ живого правила. Изменится там
    формула — эта проверка упадёт, а не разойдётся молча.
    """
    source = (_ROOT / "src" / "positions" / "runner.py").read_text(encoding="utf-8")
    assert "decision.exit_bar_ts + timedelta(seconds=60)" in source, (
        "правило момента закрытия в runner изменилось — проверьте closing_moment"
    )
    # И у закрытия по пробелу момент ДРУГОЙ — ``now``; такие позиции в замер не
    # входят вовсе, поэтому вывести его из бара и не требуется.
    assert "now if by_gap else" in source


async def test_a_bar_open_instead_of_a_bar_close_is_caught_as_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Позиция с ``closed_at`` по ОТКРЫТИЮ бара распознаётся как расхождение.

    Это та самая путаница, что пришла с боевой базы, поданная как вход: если
    сверка снова начнёт сравнивать открытие бара с закрытием, она либо примет
    неверную позицию (и эта проверка упадёт), либо отвергнет верную (и упадёт
    соседняя). Обе стороны закрыты.
    """
    wrong = _position_row(closed_at=OPENED + timedelta(minutes=1))
    pool = _ShadowPool([wrong], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 2
    out = capsys.readouterr().out
    assert "closed_at" in out
    assert "КОНТРОЛЬ НЕ СОВПАЛ" in out
    assert pool.writes == []


def test_the_arming_scan_stops_at_the_variants_own_exit_bar() -> None:
    """Задетость считается ТОЛЬКО в пределах жизни варианта (боевая позиция 6).

    ВОСПРОИЗВЕДЕНИЕ НАСТОЯЩЕЙ ПРИЧИНЫ. Вариант вышел по пределу на первом баре;
    порога включения цена достигла ПОЗЖЕ — когда сделки уже не было. Просмотр
    всего окна объявлял такую сделку задетой, завышая главное число этапа, и
    ронял расчёт утверждением «выход stop при armed=true».

    Механизм эту сделку не трогал: он не успел включиться до её выхода.
    """
    path = (
        [(100.1, 98.5, 99.0)]              # предел задет сразу, порог — нет
        + _flat(101.5, 101.0, 101.2, 59)   # порог задет уже ПОСЛЕ выхода
    )
    shadow = _resolve(_bars(path))

    assert shadow.control is not None
    assert shadow.control.exit_reason == "stop"
    assert all(v.exit_reason == "stop" for v in shadow.variants)
    assert all(not v.armed for v in shadow.variants), (
        "задетость засчитана по бару, случившемуся после выхода варианта"
    )
    assert all(v.armed_at is None for v in shadow.variants)

    # Тот же ряд, но без ограничения по бару выхода, даёт ДРУГОЙ ответ — значит
    # ограничение действительно работает, а не совпало.
    prefix = _bars(path)
    unscoped, _at = armed_scan(
        prefix, entry_price=ENTRY, target_pct=TARGET_PCT,
        activation_ratio=0.25, direction="buy",
    )
    scoped, _at2 = armed_scan(
        prefix, entry_price=ENTRY, target_pct=TARGET_PCT,
        activation_ratio=0.25, direction="buy", until=OPENED,
    )
    assert unscoped is True and scoped is False


def test_a_threshold_and_a_stop_in_the_same_bar_are_ambiguous_not_stop() -> None:
    """Порог и предел В ОДНОМ баре дают ``ambiguous`` — способом живого правила.

    УТВЕРЖДЕНИЕ СУЖЕНО ДО «МЕЖДУ БАРАМИ» именно из-за этого случая: максимум
    бара может дойти до порога включения, а минимум того же бара — до предела, и
    порядок событий внутри минуты ряду свечей неизвестен. Своего способа
    разрешения здесь не изобретается: правило 8.10 возвращает ``ambiguous``, и
    выход ``stop`` до этого случая просто не доходит.

    Уровни подобраны так, что ответ виден в уме: порог A=0.25 стоит на 100.50 и
    задет максимумом 100.60, предел 99.00 задет минимумом 98.50 — тем же баром.
    """
    path = [(100.6, 98.5, 99.0)] + _flat(99.0, 98.0, 98.5, 59)
    shadow = _resolve(_bars(path))

    low = [v for v in shadow.variants if v.activation_frac == 0.25]
    high = [v for v in shadow.variants if v.activation_frac == 0.50]
    assert all(v.exit_reason == "ambiguous" for v in low), (
        "одновременное касание разрешено догадкой вместо ambiguous"
    )
    # У неизмеренного исхода нет ни бара, ни цены, ни итога.
    assert all(v.exit_bar_ts is None and v.exit_price is None for v in low)
    assert all(v.net_pnl_pct is None and v.net_pnl_usd is None for v in low)
    # Порог 101.00 этот бар не достал — обычный предел, механизм не задет.
    assert all(v.exit_reason == "stop" for v in high)
    assert all(not v.armed for v in high)


async def test_the_mismatch_count_can_never_exceed_the_compared_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Расхождений не бывает больше, чем сверено (боевой вывод «11 и 12»).

    Позиция, расчёт которой упал с исключением, СВЕРЕНА (попытка была) и
    РАЗОШЛАСЬ (совпадения не получено) — в оба счётчика она входит одинаково.
    Первая редакция вычитала её из сверенных, но засчитывала в расхождения, и
    печатала невозможное. Увидев такое число, человек начинает сомневаться во
    ВСЁМ выводе, и правильно делает.
    """
    # Позиция с несогласованным сроком: расчёт откажется считать её вовсе.
    broken = _position_row(id=1, deadline_at=DEADLINE + timedelta(minutes=7))
    pool = _ShadowPool([broken, _position_row(id=2)], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 2
    compared = int(out.split("Сверено позиций:")[1].split()[0])
    mismatched = int(out.split("Разошлось:")[1].split()[0])
    assert compared == 2, f"сверено {compared}, а позиций 2"
    assert mismatched <= compared, f"разошлось {mismatched} при {compared} сверенных"
    assert mismatched == 1
    assert pool.writes == []


# =============================================================================
# §7.8 и контрольный опыт §11.2: окно и годность бара
# =============================================================================

def test_a_window_shifted_by_one_bar_is_refused_not_silently_used() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ (§11.2 ТЗ): сверка окна ПАДАЕТ при расхождении.

    Недостаточно, чтобы проверка проходила, — надо показать, что она падает,
    если вернуть дефект. Здесь дефект вносится прямо: срок сдвигается на бар, и
    окно подвижного правила перестаёт совпадать с окном позиции. Молча
    посчитанный на съехавшем окне замер выглядел бы совершенно правдоподобно.
    """
    # Верное окно принимается.
    first, last = check_window(
        opened_at=OPENED, deadline_at=DEADLINE,
        horizon_h=HORIZON_H, resolution="1m",
    )
    assert first == OPENED
    assert last == DEADLINE - timedelta(minutes=1)

    with pytest.raises(ValueError, match="кончается не на баре перед сроком"):
        check_window(
            opened_at=OPENED, deadline_at=DEADLINE + timedelta(minutes=1),
            horizon_h=HORIZON_H, resolution="1m",
        )
    with pytest.raises(ValueError, match="начинается не с бара после входа"):
        check_window(
            opened_at=OPENED + timedelta(seconds=30), deadline_at=DEADLINE,
            horizon_h=HORIZON_H, resolution="1m",
        )


def test_an_unclosed_last_bar_never_enters_the_window() -> None:
    """§7.8: незакрытый последний бар в ряд не берётся, и запас берётся ИМПОРТОМ.

    Это то же правило, что закрыло дефект 8.10.1: ``close`` формирующейся свечи
    — цена «пока что», и коллектор перезаписывает её следующим опросом.
    Проверяется двумя способами: величина запаса берётся из ``settle_seconds``
    (а не из копии формулы — копия формулы уже была причиной дефекта), и бар,
    лежащий за верхней границей чтения, в окно не попадает.
    """
    import inspect

    from src.barrier.runner import settle_seconds

    source = (_ROOT / "scripts" / "shadow_trailing_9_1_3.py").read_text(
        encoding="utf-8"
    )
    assert "from src.barrier.runner import settle_seconds" in source
    assert "settle_seconds()" in source
    # Формула запаса НЕ переписана в скрипте: там только вызов.
    assert "BARRIER_SETTLE_MINUTES" not in source
    assert settle_seconds() > 0
    assert "settle_seconds" in inspect.getsource(settle_seconds)

    # Бар за верхней границей окна отбрасывается самим правилом: ряд длиннее
    # окна не удлиняет окно.
    path = _flat(100.4, 100.3, 100.35, BARS_IN_WINDOW)
    long_series = _bars(path) + [
        Bar(ts=DEADLINE, high=999.0, low=0.1, close=999.0)
    ]
    shadow = _resolve(long_series)
    assert shadow.control is not None
    assert shadow.control.exit_reason == "timeout"
    assert shadow.control.exit_bar_ts == DEADLINE - timedelta(minutes=1)
    assert shadow.control.exit_price == pytest.approx(100.35)
    assert all(r.bars_used == BARS_IN_WINDOW for r in shadow.variants)


# =============================================================================
# §7.11. Ограничения миграции 021 — сверяются с ТЕКСТОМ файла
# =============================================================================

def test_the_migration_forbids_armed_without_a_moment() -> None:
    """§7.11: ``armed=true`` с пустым ``armed_at`` не проходит ограничение.

    ПРОВЕРКА СВЕРЯЕТСЯ С ТЕКСТОМ ФАЙЛА МИГРАЦИИ, а не с копией списка в самом
    тесте (прямое требование §7.11 ТЗ). Копия однажды разошлась бы с миграцией и
    разошлась бы молча — ровно так, как это уже случилось на Этапе 9.1.2.2 со
    схемой ``positions``.
    """
    text = (_ROOT / "db" / "migrations" / "021_position_trailing_shadow.sql"
            ).read_text(encoding="utf-8")
    assert "position_trailing_shadow_armed_chk" in text
    block = text.split("position_trailing_shadow_armed_chk", 2)[2]
    block = block.split("END IF;", 1)[0]
    assert "armed = false AND armed_at IS NULL" in block
    assert "armed = true  AND armed_at IS NOT NULL" in block

    # Форма записи неизмеренного исхода — тоже ограничением, а не обещанием.
    assert "position_trailing_shadow_shape_chk" in text
    # Три расхождения со схемой §4 ТЗ названы прямо в файле, а не обойдены.
    assert "'trail'" in text and "'no_data'" in text
    assert "ОШИБКА ТЗ" in text

    rollback = (_ROOT / "db" / "migrations"
                / "021_position_trailing_shadow_rollback.sql").read_text(
        encoding="utf-8"
    )
    assert "DROP TABLE IF EXISTS position_trailing_shadow;" in rollback


def test_the_shadow_table_never_touches_the_tables_of_fact() -> None:
    """§6.5 ТЗ: миграция 021 не изменяет ни одной таблицы факта.

    Внешний ключ смотрит ИЗ новой таблицы наружу. ``ALTER TABLE positions`` в
    файле не должно быть ни одного: теневой результат не имеет права попасть
    туда, где лежит факт.
    """
    text = (_ROOT / "db" / "migrations" / "021_position_trailing_shadow.sql"
            ).read_text(encoding="utf-8")
    for table in ("positions", "signals", "signal_evaluations", "signal_targets",
                  "risk_targets", "signal_outcomes_barrier", "strategy_outcomes",
                  "trailing_outcomes"):
        assert f"ALTER TABLE {table} " not in text, f"миграция трогает {table}"
        assert f"DROP TABLE IF EXISTS {table};" not in text
    assert "REFERENCES positions(id) ON DELETE CASCADE" in text


def test_the_stage_changes_no_live_rule() -> None:
    """§0 и §6 ТЗ: границы этапа соблюдены — правило выхода не тронуто.

    Проверяется по факту, а не по обещанию: модуль замера ВЫЗЫВАЕТ обе живые
    функции и не содержит своего определения ни порогов, ни отката.
    """
    source = (_ROOT / "src" / "shadow" / "trailing.py").read_text(encoding="utf-8")
    assert "from src.positions import rules as position_rules" in source
    assert "from src.trailing import rule as trailing_rule" in source
    assert "trailing_rule.resolve_all(" in source
    assert "position_rules.check_exit(" in source
    # Своего определения A и R нет: уровень включения берётся у правила 8.10.
    assert "trailing_rule.activation_price(" in source
    # LOGIC_VERSION не поднимается ни в одном файле этапа.
    for name in ("scripts/shadow_trailing_9_1_3.py",
                 "scripts/trailing_resample_9_1_3.py",
                 "src/shadow/trailing.py"):
        assert "LOGIC_VERSION =" not in (_ROOT / name).read_text(encoding="utf-8")


# =============================================================================
# Двойник базы: SQL ВЫПОЛНЯЕТСЯ и сверяется со схемой из миграций (§11.2)
# =============================================================================

class _ShadowPool(SchemaPool):
    """Пул с данными: позиции, свечи и хранилище теневых строк.

    Наследуется от :class:`SchemaPool`, поэтому КАЖДЫЙ запрос сперва проходит
    сверку колонок со схемой из файлов миграций и только потом исполняется.
    """

    def __init__(
        self,
        positions: list[dict[str, Any]],
        bars: dict[int, list[dict[str, Any]]],
        *,
        table_exists: bool = True,
    ) -> None:
        super().__init__()
        self.positions = positions
        self.bars = bars
        self.table_exists = table_exists
        self.shadow: dict[tuple[int, str], dict[str, Any]] = {}
        self.write_batches: list[list[Any]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM ohlcv" in sql:
            instrument_id, _timeframe, ts_from, ts_to = args
            return [
                row for row in self.bars.get(int(instrument_id), [])
                if ts_from <= row["ts"] <= ts_to
            ]
        if "FROM positions p" in sql:
            # ДВОЙНИК ПРИМЕНЯЕТ ТЕ УСЛОВИЯ, КОТОРЫЕ РЕАЛЬНО СТОЯТ В ЗАПРОСЕ, а
            # не те, которые запрос «должен» содержать. Первая редакция
            # фильтровала data_gap сама, и контрольный опыт показал это прямо:
            # удаление условия из ``db.py`` не роняло ни одной проверки —
            # двойник продолжал отсеивать те же строки за базу. Это ровно тот
            # случай «двойник мягче настоящей системы», ради которого написан
            # §11.2 ТЗ.
            since = args[0] if args else None
            rows = list(self.positions)
            if "p.status = 'closed'" in sql:
                rows = [r for r in rows if r["status"] == "closed"]
            if "p.exit_reason IS DISTINCT FROM 'data_gap'" in sql:
                rows = [r for r in rows if r["exit_reason"] != "data_gap"]
            if "p.opened_at >= $1" in sql and since is not None:
                rows = [r for r in rows if r["opened_at"] >= since]
            return project(sql, rows)
        if "FROM trailing_outcomes" in sql:
            return []
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
        if "position_trailing_shadow" in sql:
            return self.table_exists
        return None

    async def executemany(self, sql: str, rows: list[Any]) -> None:
        self._check(sql)
        self.writes.append(sql)
        self.write_batches.append(list(rows))
        # ON CONFLICT (position_id, variant) DO UPDATE — перезапись той же
        # строки, а не второй экземпляр. Двойник обязан вести себя так же,
        # иначе проверка идемпотентности ничего не проверяла бы.
        for row in rows:
            self.shadow[(int(row[0]), str(row[1]))] = {
                "armed": row[4], "exit_reason": row[6], "net_pnl_pct": row[9],
            }


def _position_row(**over: Any) -> dict[str, Any]:
    """Строка ``positions`` со ВСЕМИ полями, которые читает запрос выборки."""
    row: dict[str, Any] = {
        "id": 1, "instrument_id": 10, "symbol": "ETH/USDT", "base": "ETH",
        "logic_version": 5, "horizon_h": HORIZON_H, "side": "buy",
        "status": "closed", "opened_at": OPENED, "deadline_at": DEADLINE,
        # ЗАКРЫТИЕ БАРА, А НЕ ЕГО ОТКРЫТИЕ. Бар выхода открывается в OPENED+1мин
        # и закрывается минутой позже; ровно так ``src/positions/runner.py``
        # и пишет ``closed_at``. Первая редакция фикстуры ставила сюда открытие
        # бара — ту же путаницу, из-за которой боевой прогон дал одиннадцать
        # расхождений по 60 секунд.
        "closed_at": OPENED + timedelta(minutes=2),
        "entry_price": ENTRY, "notional_usd": SLOT,
        "target_pct": TARGET_PCT, "target_price": 102.0,
        "stop_pct": STOP_PCT, "stop_price": 99.0,
        "cost_pct": COST_PCT, "resolution": "1m",
        "exit_price": 102.0, "exit_reason": "target",
        "net_pnl_pct": TARGET_PCT - COST_PCT,
        "net_pnl_usd": SLOT * (TARGET_PCT - COST_PCT) / 100.0,
        "bars_held": 2, "outcome_certain": True,
    }
    row.update(over)
    return row


def _bar_rows() -> dict[int, list[dict[str, Any]]]:
    """Свечи, на которых записанный исход образцовой позиции воспроизводится."""
    path = [(100.4, 100.3, 100.35)] + _flat(102.5, 101.0, 102.2, 59)
    return {
        10: [
            {"ts": OPENED + timedelta(minutes=i), "open": lo,
             "high": h, "low": lo, "close": c}
            for i, (h, lo, c) in enumerate(path)
        ]
    }


async def _run_script(monkeypatch, pool: _ShadowPool, argv: list[str]) -> int:
    """Прогоняет НАСТОЯЩИЙ скрипт части Б на двойнике пула."""
    import scripts.shadow_trailing_9_1_3 as script
    from src.core.db import db as real_db

    async def _noop() -> None:
        return None

    monkeypatch.setattr(real_db, "_pool", pool, raising=False)
    monkeypatch.setattr(real_db, "connect", _noop)
    monkeypatch.setattr(real_db, "close", _noop)
    monkeypatch.setattr("sys.argv", ["shadow", *argv])
    return await script.main()


async def test_a_broken_control_stops_the_run_before_any_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7.6: испорченная позиция даёт код 2 и НИ ОДНОЙ таблицы сравнения.

    Смысл тот же, что у контроля Этапа 8.10: если пересчёт не умеет
    воспроизвести уже случившееся, его числа по НЕ случившимся вариантам не
    стоят ничего. Поэтому останавливается ВЕСЬ вывод, а не помечается одна
    строка.
    """
    pool = _ShadowPool([_position_row(exit_price=101.0)], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 2
    assert "КОНТРОЛЬ НЕ СОВПАЛ" in out
    assert "exit_price" in out
    assert "ЧИСЛО 1" not in out, "напечатана таблица при разошедшемся контроле"
    assert "ЧИСЛО 3" not in out
    assert pool.writes == [], "при разошедшемся контроле что-то записано"


async def test_a_difference_in_the_last_stored_digit_still_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3.3: сверка идёт ДО ПОСЛЕДНЕГО ЗНАКА, и допуск расширять запрещено.

    ЭТА ПРОВЕРКА НАПИСАНА ПОСЛЕ КОНТРОЛЬНОГО ОПЫТА, КОТОРЫЙ ЕЁ ПОТРЕБОВАЛ.
    Первая редакция портила позицию грубо — цена 101.00 вместо 102.00, — и такое
    расхождение переживает любое округление. Опыт показал это прямо: замена
    ``PRICE_PLACES = 8`` на ``0`` не роняла ни одной проверки, то есть запрет
    §3.3 «расширять допуск нельзя» не был проверен ничем.

    Здесь позиция портится на ПОСЛЕДНЕМ знаке хранения: восьмом для цены
    (``NUMERIC(20,8)``) и шестом для итога (``NUMERIC(12,6)``). Такое
    расхождение переживает только сравнение без допуска — и именно оно и
    требуется.
    """
    pool = _ShadowPool([_position_row(exit_price=102.00000001)], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 2
    assert "КОНТРОЛЬ НЕ СОВПАЛ" in capsys.readouterr().out

    pool = _ShadowPool(
        [_position_row(net_pnl_pct=TARGET_PCT - COST_PCT + 0.000001)],
        _bar_rows(),
    )
    assert await _run_script(monkeypatch, pool, []) == 2
    out = capsys.readouterr().out
    assert "net_pnl_pct" in out
    assert pool.writes == []


async def test_a_matching_control_lets_the_measurement_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Обратная сторона §7.6: при совпавшем контроле замер печатается целиком.

    Без этой проверки предыдущая проходила бы и у скрипта, который не печатает
    ничего никогда.
    """
    pool = _ShadowPool([_position_row()], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 0, out
    assert "Разошлось:         0" in out
    assert "ЧИСЛО 1" in out and "ЧИСЛО 2" in out and "ЧИСЛО 3" in out
    assert "ЧИСЛО 4" in out


async def test_data_gap_and_open_positions_never_enter_the_sample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7.7: ``data_gap`` в выборку не попадает, открытая позиция — тоже.

    У ``data_gap`` цена выхода не наблюдалась, а восстановлена; у открытой
    позиции исход ещё не наступил, и теневая цифра по ней была бы прогнозом, а
    не замером. Оба числа печатаются справочно — выборка, из которой что-то
    молча выпало, неотличима от выборки, в которой этого не было.
    """
    pool = _ShadowPool(
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


async def test_without_apply_not_a_single_write_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7.10: без ``--apply`` в базу не уходит ни одного запроса на запись.

    Проверяется ФАКТ отсутствия записи на уровне пула, а не текст вывода:
    скрипт, напечатавший «ничего не записано» и всё-таки записавший, прошёл бы
    проверку по выводу.
    """
    pool = _ShadowPool([_position_row()], _bar_rows())
    code = await _run_script(monkeypatch, pool, [])
    capsys.readouterr()

    assert code == 0
    assert pool.writes == []
    assert pool.write_batches == []
    assert pool.shadow == {}


async def test_a_second_apply_changes_neither_the_count_nor_the_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7.9: повторный прогон с ``--apply`` не меняет ни числа строк, ни значений.

    Идемпотентность здесь не обещание, а свойство запроса:
    ``ON CONFLICT (position_id, variant) DO UPDATE`` перезаписывает ту же строку
    теми же числами. Двойник ведёт себя так же — иначе проверка ничего не
    проверяла бы.
    """
    pool = _ShadowPool([_position_row()], _bar_rows())

    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()
    first = dict(pool.shadow)
    assert len(first) == 13, f"строк {len(first)}, ожидалось 13"

    assert await _run_script(monkeypatch, pool, ["--apply"]) == 0
    capsys.readouterr()

    assert len(pool.shadow) == len(first), "повторный прогон добавил строки"
    assert pool.shadow == first, "повторный прогон изменил значения"
    assert "ON CONFLICT (position_id, variant) DO UPDATE" in pool.writes[0]


async def test_apply_without_the_migration_refuses_instead_of_failing_late(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Нет таблицы — внятный отказ с указанием миграции, а не ошибка Postgres.

    Схема таблицы в коде НЕ дублируется намеренно (второй экземпляр однажды
    разойдётся с файлом миграции), поэтому отсутствие таблицы обязано читаться
    как инструкция, а не как трассировка.
    """
    pool = _ShadowPool([_position_row()], _bar_rows(), table_exists=False)
    code = await _run_script(monkeypatch, pool, ["--apply"])
    out = capsys.readouterr().out

    assert code == 2
    assert "021_position_trailing_shadow.sql" in out
    assert pool.write_batches == []


async def test_an_empty_sample_returns_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§5.1: пустая выборка — код возврата 3, а не 0 и не падение."""
    pool = _ShadowPool([], {})
    code = await _run_script(monkeypatch, pool, [])
    assert code == 3
    assert "Выборка пуста" in capsys.readouterr().out


async def test_a_small_sample_forbids_the_words_better_and_worse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3.4 ЧИСЛО 4 и §8.5: при N < 30 предупреждение печатается, выводов нет.

    Слова «лучше» и «хуже» при десятке сделок были бы утверждением, которого
    данные не несут: доверительный интервал разницы шире любой из наблюдаемых
    разниц. Проверяется буквально — по тексту вывода.
    """
    pool = _ShadowPool([_position_row()], _bar_rows())
    assert await _run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out

    assert "N = 1" in out
    assert "ВЫВОД О ПРЕИМУЩЕСТВЕ" in out
    lowered = out.lower()
    assert "лучше" not in lowered, "при малой выборке напечатано слово «лучше»"
    assert "хуже" not in lowered, "при малой выборке напечатано слово «хуже»"
    # И рекомендации о внедрении нет ни в каком виде.
    assert "ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ" in out


# =============================================================================
# Часть А: скрипт пересчёта прогоняется целиком
# =============================================================================

class _ResamplePool(SchemaPool):
    """Пул со строками ``trailing_outcomes`` для части А, ПОРЦИЯМИ.

    Двойник соблюдает ключ-границу и предел порции, как настоящая база: иначе он
    был бы мягче её и «доказал» бы работоспособность чтения, которое на самом
    деле тянет всю таблицу разом.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        # Порядок тот же, что в запросе: по времени сигнала, затем по ключу пары.
        self.rows = sorted(
            rows,
            key=lambda r: (r["ts"], int(r["signal_id"]), int(r["horizon_h"]),
                           float(r["activation_ratio"]), float(r["retrace_ratio"])),
        )
        self.batches = 0

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM trailing_outcomes" not in sql:
            return []
        self.batches += 1
        # ДВОЙНИК ДЕЛАЕТ РОВНО ТО, ЧТО НАПИСАНО В ЗАПРОСЕ. Первая редакция
        # применяла ключ-границу и предел порции сама, независимо от текста SQL,
        # и контрольный опыт показал это прямо: удаление ``LIMIT`` из запроса не
        # роняло ни одной проверки — двойник продолжал резать порции за базу.
        # Это ровно тот случай «двойник мягче настоящей системы», из-за которого
        # на Этапе 9.1.2.2 боевой прогон падал на зелёном стенде.
        after, limit = (args[0], args[1], args[2]), args[3]
        rows = self.rows
        if "(s.ts, t.signal_id, t.horizon_h) > ($1, $2, $3)" in sql:
            if after[0] is not None:
                rows = [
                    r for r in rows
                    if (r["ts"], int(r["signal_id"]), int(r["horizon_h"])) > after
                ]
        if "LIMIT $4" in sql:
            rows = rows[: int(limit)]
        return project(sql, rows)


def _resample_rows(pairs: int = 60) -> list[dict[str, Any]]:
    """Полные пары: тринадцать вариантов на каждую, половина по каждую сторону границы."""
    from src.trailing.rule import VARIANTS

    boundary = datetime(2026, 8, 29, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for index in range(pairs):
        computed = boundary - timedelta(days=1) if index % 2 == 0 else (
            boundary + timedelta(days=1)
        )
        for position, (activation, retrace) in enumerate(VARIANTS):
            rows.append({
                "signal_id": index + 1, "horizon_h": 4,
                "activation_ratio": activation, "retrace_ratio": retrace,
                "logic_version": 5,
                "exit_reason": "timeout" if activation == 0 else "trail",
                # Слабый сдвиг плюс воспроизводимый «шум» без генератора:
                # проверяется, что скрипт СЧИТАЕТ, а не что он что-то находит.
                "net_pnl_pct": 0.05 - 0.30 * retrace + 0.01 * (index % 7)
                               + 0.001 * position,
                "computed_at": computed,
                "ts": datetime(2026, 8, 27, tzinfo=UTC) + timedelta(hours=index),
                # Ключ ИСХОДНОЙ колонки (``i.base``), а не имя в ответе:
                # проекция по SELECT-списку сама даст ``token``.
                "base": "ETH",
            })
    return rows


async def _run_resample(monkeypatch, pool: SchemaPool) -> int:
    import scripts.trailing_resample_9_1_3 as script
    from src.core.db import db as real_db

    async def _noop() -> None:
        return None

    monkeypatch.setattr(real_db, "_pool", pool, raising=False)
    monkeypatch.setattr(real_db, "connect", _noop)
    monkeypatch.setattr(real_db, "close", _noop)
    monkeypatch.setattr("sys.argv", ["resample"])
    return await script.main()


async def test_part_a_prints_all_four_blocks_and_both_split_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.3: часть А печатает состав, ТРИ таблицы 4×3, защиты и ОБА ответа.

    ЭТОТ ТЕСТ НАПИСАН ПОСЛЕ ТОГО, КАК РУЧНОЙ ПРОГОН НАШЁЛ ДЕФЕКТ, КОТОРОГО НЕ
    НАШЛИ ПРОВЕРКИ. Запрос отдавал момент сигнала под именем ``signal_ts``, а
    переиспользуемая функция ``scripts.trailing_stats.collect`` читает ``ts`` —
    скрипт падал с ``KeyError`` на первой же строке расчёта. Ни одна проверка
    его не запускала, и потому ни одна не падала: проверок на часть А не было
    вовсе. Теперь скрипт прогоняется целиком.
    """
    pool = _ResamplePool(_resample_rows())
    code = await _run_resample(monkeypatch, pool)
    out = capsys.readouterr().out

    assert code == 0, out
    assert "БЛОК 1. СОСТАВ ВЫБОРКИ" in out
    assert "БЛОК 2." in out and "БЛОК 3." in out and "БЛОК 4." in out
    # Три таблицы 4×3, и третья названа независимой проверкой.
    assert out.count("R \\ A") == 3, "напечатано не три таблицы 4x3"
    assert "НЕЗАВИСИМАЯ ПРОВЕРКА" in out
    assert "посчитано до 2026-08-29 UTC: 30" in out
    assert "посчитано с  2026-08-29 UTC: 30" in out
    # Три защиты от подгонки.
    assert "РАЗБРОС ПРОТИВ СЛУЧАЙНОГО" in out
    assert "ИНТЕРВАЛЫ РАЗНИЦЫ С КОНТРОЛЕМ" in out
    # ОБА ответа проверки на независимой половине (§2.3 БЛОК 4).
    assert "ОТВЕТ 1 — СОВПАДЕНИЕ МЕСТ" in out
    assert "ОТВЕТ 2 — СОХРАНЕНИЕ СТОРОНЫ" in out
    # Рекомендации о внедрении нет.
    assert "ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ" in out
    # Ни одной записи: часть А только читает.
    assert pool.writes == []


async def test_part_a_shouts_when_logic_versions_are_mixed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.3 БЛОК 1: больше одной версии логики — предупреждение отдельной строкой.

    Смешение версий — известная в проекте причина ложных выводов: разные версии
    отбирают разные сигналы, и разница между вариантами отражала бы состав
    выборки, а не правило выхода. Предупреждение обязано быть заметным, а не
    строчкой в общей разбивке.
    """
    rows = _resample_rows()
    for row in rows[:13]:
        row["logic_version"] = 4
    pool = _ResamplePool(rows)
    assert await _run_resample(monkeypatch, pool) == 0
    out = capsys.readouterr().out
    assert "БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ" in out
    assert "logic_version: 4: 13 / 5:" in out


async def test_part_a_says_so_when_the_old_half_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пустая старая часть — сказано вслух, а не выдано за независимую проверку.

    Так выглядит база после принудительного пересчёта 8.10: ``computed_at``
    сброшен у всех строк, и деление на старую и новую часть теряет смысл.
    Напечатать в этом случае третью таблицу без оговорки значило бы выдать
    пересчёт всей выборки за проверку на новых данных.
    """
    rows = _resample_rows()
    for row in rows:
        row["computed_at"] = datetime(2026, 8, 31, tzinfo=UTC)
    pool = _ResamplePool(rows)
    assert await _run_resample(monkeypatch, pool) == 0
    out = capsys.readouterr().out
    assert "старая часть выборки ПУСТА" in out
    assert "(пар нет — считать нечего)" in out


# Потолок пиковой памяти части А на боевом объёме, МБ. Взят с запасом больше
# чем вдвое над измеренным (104,6 МБ на 1 707 940 строках) и вчетверо ниже
# лимита контейнера ``mem_limit: 1g``. Не «сколько влезет», а «сколько можно
# взять, не мешая девяти службам, которые работают круглосуточно».
MEMORY_CEILING_MB = 300.0

# Боевой объём на 31.08.2026. Требование правки — проверять не меньше полутора
# миллионов строк, и здесь стоит ровно то число, на котором расчёт был убит.
PRODUCTION_ROWS = 1_707_940


def _memory_probe(rows: int, *, load_everything: bool = False) -> dict[str, Any]:
    """Замер памяти ОТДЕЛЬНЫМ процессом. Возвращает разобранный ответ.

    Отдельный процесс здесь не удобство, а условие осмысленности: ``ru_maxrss``
    не опускается, и внутри общего прогона проверок замер поймал бы пик,
    оставленный чужой проверкой.
    """
    import json

    probe = _ROOT / "tests" / "memory" / "resample_memory_probe.py"
    argv = [sys.executable, str(probe), str(rows)]
    if load_everything:
        argv.append("--load-everything")
    done = subprocess.run(
        argv, cwd=str(_ROOT), capture_output=True, text=True, timeout=900
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_measurement_fits_the_container_limit_at_production_volume() -> None:
    """§4 правки: на боевом объёме расчёт укладывается в лимит с запасом.

    Боевой прогон 31.08.2026 был убит ядром: ``anon-rss`` 811 МБ и 919 МБ в двух
    прогонах подряд при ``mem_limit: 1g``. Причина — вся таблица словарями;
    считаемые числа при этом занимают тринадцать мегабайт.

    Проверяется НЕ игрушка: 1 707 940 строк, ровно тот объём, на котором расчёт
    и умер.
    """
    measured = _memory_probe(PRODUCTION_ROWS)
    assert measured["rows"] == PRODUCTION_ROWS
    assert measured["pairs"] == PRODUCTION_ROWS // 13
    assert measured["peak_rss_mb"] < MEMORY_CEILING_MB, (
        f"пик {measured['peak_rss_mb']} МБ при потолке {MEMORY_CEILING_MB} МБ"
    )


def test_the_measurement_still_fits_when_the_sample_grows_threefold() -> None:
    """Запас на рост выборки втрое — требование правки, а не пожелание.

    Выборка растёт примерно на 50 тысяч пар за двое суток. К середине сентября
    строк будет втрое больше, и «влезло сегодня» ничего не значит, если завтра
    придётся снова поднимать лимит.
    """
    measured = _memory_probe(PRODUCTION_ROWS * 3)
    assert measured["peak_rss_mb"] < MEMORY_CEILING_MB, (
        f"при росте втрое пик {measured['peak_rss_mb']} МБ"
    )


def test_loading_the_whole_sample_again_blows_the_limit() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ (§11.2): верните дефект — и проверка упадёт.

    Зелёный замер сам по себе не отличим от отсутствия замера. Здесь прежнее
    поведение — чтение всей выборки одним списком — возвращается нарочно, и
    показывается, что оно НЕ проходит: на тех же 1 707 940 строках пик выходит
    за лимит контейнера, то есть воспроизводит боевое убийство процесса.
    """
    measured = _memory_probe(PRODUCTION_ROWS, load_everything=True)
    assert measured["peak_rss_mb"] > MEMORY_CEILING_MB, (
        "прежний способ уложился в потолок — значит потолок ничего не проверяет"
    )
    # И он же выходит за сам лимит контейнера: это и есть боевое убийство.
    assert measured["peak_rss_mb"] > 1024.0, (
        f"прежний способ дал {measured['peak_rss_mb']} МБ — "
        "воспроизвести отказ по лимиту 1 ГБ не удалось"
    )


def test_the_compose_memory_limit_was_not_quietly_raised() -> None:
    """``mem_limit`` службы backtest НЕ поднят: правка уложилась в прежний лимит.

    Поднимать лимит молча запрещено прямо. Здесь это проверяется по файлу, а не
    обещанием: на машине 3,8 ГБ и девять постоянных служб, и отдать калькулятору
    полмашины значило бы рискнуть тем, что работает круглосуточно.
    """
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mem_limit: 1g" in compose


def test_the_streaming_pass_gives_the_same_numbers_as_the_old_way() -> None:
    """§5 правки: способ счёта изменился, СЧИТАЕМОЕ — нет. До последнего знака.

    Прежний способ держал всю выборку списком словарей и звал ``collect`` один
    раз на всё; новый читает порциями и зовёт ``collect`` на каждой паре
    отдельно. Отбор пар при этом ТОТ ЖЕ (одна и та же функция), и матрица чисел
    обязана совпасть побитово — вместе с ПОРЯДКОМ строк: от порядка зависят и
    деление выборки пополам, и последовательность случайных чисел в
    перестановочной проверке.
    """
    import asyncio

    import scripts.trailing_resample_9_1_3 as script
    from scripts.trailing_stats import collect as collect_all
    from scripts.trailing_stats import matrix as matrix_all
    from src.core.db import db as real_db

    rows = _resample_rows(pairs=40)

    # СТАРЫЙ способ: всё разом.
    projected = [
        {**row, "token": row["base"]} for row in sorted(
            rows, key=lambda r: (r["ts"], int(r["signal_id"]),
                                 int(r["horizon_h"]))
        )
    ]
    old_pairs, old_dropped = collect_all(projected)
    old_values = matrix_all(old_pairs)

    # НОВЫЙ способ: порциями, ``collect`` на каждой паре.
    pool = _ResamplePool(rows)
    real_db._pool = pool  # type: ignore[assignment]
    original = type(script.db).TRAILING_RESAMPLE_BATCH
    try:
        # Порция заведомо не кратна тринадцати: границы приходятся на середину
        # пар, и склейка проверяется, а не обходится стороной.
        type(script.db).TRAILING_RESAMPLE_BATCH = 47
        new_matrix, composition, new_dropped, rows_read = asyncio.run(
            script.stream_sample()
        )
    finally:
        type(script.db).TRAILING_RESAMPLE_BATCH = original

    assert rows_read == len(rows), "прочитано не всё или что-то дважды"
    assert new_matrix.values.shape == old_values.shape
    assert np.array_equal(new_matrix.values, old_values), (
        "числа разошлись между старым и новым способом счёта"
    )
    assert composition.pairs == len(old_pairs)
    assert {k: v for k, v in new_dropped.items() if v} == {
        k: v for k, v in old_dropped.items() if v
    }
    assert pool.batches > 1, f"порций {pool.batches}: чтение не порционное"


def test_a_pair_split_across_two_batches_is_not_lost_or_halved() -> None:
    """Пара, разорванная границей порции, попадает в выборку ЦЕЛИКОМ и один раз.

    Это самое хрупкое место порционного чтения: обрезанная пара выглядит
    неполной, и посчитанная по обрезку она была бы молча выброшена из сравнения
    по причине, которой в данных нет.
    """
    import asyncio

    import scripts.trailing_resample_9_1_3 as script
    from src.core.db import db as real_db

    rows = _resample_rows(pairs=17)
    pool = _ResamplePool(rows)
    real_db._pool = pool  # type: ignore[assignment]
    original = type(script.db).TRAILING_RESAMPLE_BATCH
    try:
        # 20 строк — полторы пары: каждая порция рвёт пару посередине.
        type(script.db).TRAILING_RESAMPLE_BATCH = 20
        built, composition, _dropped, rows_read = asyncio.run(script.stream_sample())
    finally:
        type(script.db).TRAILING_RESAMPLE_BATCH = original

    assert len(built) == 17, f"собрано пар {len(built)}, ожидалось 17"
    assert composition.pairs == 17
    assert rows_read == len(rows), "строки посчитаны дважды или потеряны"
    assert pool.batches > 5, "порции оказались слишком крупными для проверки"


def test_a_cursor_that_stops_moving_raises_instead_of_looping_forever() -> None:
    """Граница чтения, не двигающаяся вперёд, роняет расчёт, а не крутит цикл.

    НАЙДЕНО КОНТРОЛЬНЫМ ОПЫТОМ. Убрав ключ-границу из запроса, я получил не
    падение проверки, а ВЕЧНЫЙ ЦИКЛ: каждая порция возвращала одно и то же
    начало таблицы. Снаружи это неотличимо от долгого счёта — тот же класс
    дефекта, что тихий обрыв, из-за которого правка и понадобилась.

    Теперь такой случай распознаётся сразу и называется словами.
    """
    import asyncio

    import scripts.trailing_resample_9_1_3 as script
    from src.core.db import db as real_db

    class _StuckPool(_ResamplePool):
        """Пул, который игнорирует ключ-границу — как запрос без неё."""

        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return await super().fetch(
                sql.replace("(s.ts, t.signal_id, t.horizon_h) > ($1, $2, $3)",
                            "TRUE"),
                *args,
            )

    pool = _StuckPool(_resample_rows(pairs=17))
    real_db._pool = pool  # type: ignore[assignment]
    original = type(script.db).TRAILING_RESAMPLE_BATCH
    try:
        type(script.db).TRAILING_RESAMPLE_BATCH = 20
        with pytest.raises(RuntimeError, match="не сдвинулась вперёд"):
            asyncio.run(script.stream_sample())
    finally:
        type(script.db).TRAILING_RESAMPLE_BATCH = original


def test_one_shared_bootstrap_would_differ_in_the_sixteenth_digit() -> None:
    """ПОЧЕМУ ОСТАВЛЕНЫ ДВЕНАДЦАТЬ ОТДЕЛЬНЫХ ПЕРЕСБОРОК, а не одна общая.

    Одним вызовом на матрицу из двенадцати столбцов те же интервалы считаются в
    13 раз быстрее (1,2 минуты против 15,6 на 131 380 парах). Соблазн взять
    ускорение велик — и здесь записано, чем за него платят: результат расходится
    в ШЕСТНАДЦАТОМ знаке, потому что умножение матрицы на матрицу и на вектор
    складывают числа в разном порядке.

    На печать в четырёх знаках это не влияет никак. И всё же взят медленный
    путь: правка была о ПАМЯТИ, и поменять числа заодно с ней значило бы лишить
    владельца возможности сверить новый вывод со старым. Проверка фиксирует
    факт расхождения — чтобы следующий, кому захочется ускорить, увидел цену
    сразу, а не искал её сам.
    """
    from scripts.trailing_stats import (
        BOOTSTRAP_SEED,
        bootstrap_diff,
        bootstrap_means,
        interval,
    )

    rng = np.random.default_rng(9_1_3)
    values = rng.normal(size=(60, 13))
    fixed = values[:, 0]

    diffs = values[:, 1:] - fixed[:, None]
    cloud = bootstrap_means(diffs, resamples=500, seed=BOOTSTRAP_SEED)
    exact = 0
    for column in range(12):
        _mean, lo, hi = bootstrap_diff(
            values[:, column + 1], fixed, resamples=500, seed=BOOTSTRAP_SEED
        )
        lo2, hi2 = interval(cloud[:, column])
        # Совпадение с точностью печати — да; побитовое — нет.
        assert lo == pytest.approx(lo2, abs=1e-12)
        assert hi == pytest.approx(hi2, abs=1e-12)
        if (lo, hi) == (lo2, hi2):
            exact += 1
    assert exact < 12, (
        "общий вызов вдруг стал побитово равен двенадцати отдельным — "
        "размен, описанный в докстринге, больше не нужен, и комментарий "
        "в scripts/trailing_resample_9_1_3.py надо переписать"
    )

    # А в самом скрипте по-прежнему стоит медленный, побитово верный путь.
    source = (_ROOT / "scripts" / "trailing_resample_9_1_3.py").read_text(
        encoding="utf-8"
    )
    assert "bootstrap_diff(" in source
    assert "bootstrap_means(" not in source


def test_the_memory_estimate_is_printed_and_refuses_when_it_will_not_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Расчёт, не помещающийся в лимит, ОТКАЗЫВАЕТСЯ с числами, а не умирает.

    Убитый ядром процесс печатает код возврата 137 и оборванный вывод,
    неотличимый от вывода расчёта, который посчитал и промолчал. Прочитав лимит
    заранее, скрипт отказывается сам — и решение о лимите принимает владелец.
    """
    import scripts.trailing_resample_9_1_3 as script

    monkeypatch.setattr(script, "cgroup_memory_limit_mb", lambda: 1024.0)
    assert script.print_memory_verdict(100) is None
    out = capsys.readouterr().out
    assert "Лимит контейнера: 1,024 МБ" in out
    assert "бюджет расчёта: 512 МБ" in out

    # Выборка, которой заведомо не хватит бюджета.
    refusal = script.print_memory_verdict(200_000_000)
    assert refusal is not None and "бюджете" in refusal

    # Лимита нет — расчёт не отказывается: отказывать не от чего.
    monkeypatch.setattr(script, "cgroup_memory_limit_mb", lambda: None)
    assert script.print_memory_verdict(10_000_000) is None
    assert "Лимит памяти контейнера не задан" in capsys.readouterr().out


async def test_part_a_prints_peak_memory_and_a_completion_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пиковая память печатается, и вывод оканчивается признаком завершения.

    Без признака оборванный вывод неотличим от полного — тот же класс дефекта,
    что измеритель, печатающий невозможное число.
    """
    import scripts.trailing_resample_9_1_3 as script

    pool = _ResamplePool(_resample_rows())
    assert await _run_resample(monkeypatch, pool) == 0
    out = capsys.readouterr().out
    assert "Пиковая память:" in out
    assert script.DONE_MARKER
    assert out.rstrip().endswith(script.DONE_MARKER)
    assert "ПРИЗНАК ЗАВЕРШЕНИЯ" in out


async def test_part_a_returns_three_on_an_empty_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пустая ``trailing_outcomes`` — код 3 и указание, чем её наполнить."""
    pool = _ResamplePool([])
    assert await _run_resample(monkeypatch, pool) == 3
    assert "python -m src.trailing_main" in capsys.readouterr().out


# =============================================================================
# Контрольный опыт §11.2: двойник схемы действительно ловит чужую колонку
# =============================================================================

def test_the_schema_double_still_rejects_a_query_to_a_missing_column() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: двойник схемы отвергает запрос к несуществующей колонке.

    Без этой проверки нельзя утверждать, что двойник вообще что-то ловит: он мог
    бы пропускать всё подряд и «доказывать» исправность любого запроса. Берётся
    тот же случай, что упал на боевой базе 31.08.2026, и добавляется новый —
    несуществующая колонка таблицы этого этапа.
    """
    tables = schema()
    assert "symbol" not in tables["positions"]
    assert len(tables["positions"]) == 37

    with pytest.raises(UndefinedColumn, match="symbol"):
        check_sql_columns(
            "SELECT id, symbol FROM positions WHERE id = $1;", tables
        )
    with pytest.raises(UndefinedColumn, match="trailing_frac"):
        check_sql_columns(
            "SELECT p.trailing_frac FROM position_trailing_shadow p;", tables
        )
    # Настоящие запросы этапа проходят: двойник не отвергает всё подряд.
    check_sql_columns(
        "SELECT p.id, i.symbol FROM positions p "
        "JOIN instruments i ON i.id = p.instrument_id WHERE p.id = $1;",
        tables,
    )


def test_every_stage_query_passes_the_schema_check() -> None:
    """Все запросы, добавленные этапом, сверены со схемой из миграций.

    Проверка идёт по НАСТОЯЩИМ методам ``DB``, а не по их пересказу: только так
    она касается того самого текста SQL, который уйдёт в боевую базу.
    """
    import asyncio

    from src.core.db import DB

    async def _exercise() -> SchemaPool:
        real = DB()
        pool = _ShadowPool([], {})
        real._pool = pool  # type: ignore[assignment]
        await real.position_trailing_shadow_exists()
        await real.get_positions_for_shadow()
        await real.count_positions_for_shadow()
        await real.fetch_trailing_resample_batch()
        await real.fetch_trailing_resample_batch(
            after=(datetime(2026, 8, 29, tzinfo=UTC), 7, 4)
        )
        await real.save_position_trailing_shadow([{
            "position_id": 1, "variant": CONTROL_VARIANT,
            "activation_frac": None, "pullback_frac": None,
            "armed": False, "armed_at": None, "exit_reason": "stop",
            "exit_bar_ts": None, "exit_price": None, "net_pnl_pct": None,
            "net_pnl_usd": None, "bars_used": 1, "resolution": "1m",
            "logic_version": 5,
        }])
        return pool

    pool = asyncio.run(_exercise())
    assert len(pool.queries) >= 5
    # Токен берётся соединением: колонки symbol в positions нет.
    assert any("JOIN instruments" in q for q in pool.queries)


def test_the_grid_is_the_same_four_by_three_as_in_stage_8_10() -> None:
    """§2.4: сетка не расширена — 4 уровня включения × 3 отката плюс контроль.

    Новые значения A и R превратили бы независимую проверку старой находки в
    новый перебор, и «победитель» нашёлся бы снова — как он находится всегда,
    когда вариантов больше одного.
    """
    assert ACTIVATION_RATIOS == (0.25, 0.50, 0.75, 1.00)
    assert RETRACE_RATIOS == (0.20, 0.33, 0.50)
    assert len(TRAILING_VARIANTS) == 12

    source = (_ROOT / "scripts" / "trailing_resample_9_1_3.py").read_text(
        encoding="utf-8"
    )
    # Сетка ИМПОРТИРУЕТСЯ, а не переобъявляется в скрипте.
    assert "ACTIVATION_RATIOS" in source and "RETRACE_RATIOS" in source
    assert "ACTIVATION_RATIOS =" not in source
    assert "RETRACE_RATIOS =" not in source
    # Защиты от подгонки берутся из 8.10 вызовом, а не переписаны.
    assert "from scripts.trailing_stats import" in source
    assert "spread_permutation" in source and "bootstrap_diff" in source
    # Оба ответа проверки на независимой половине (§2.3 БЛОК 4).
    assert "ОТВЕТ 1" in source and "ОТВЕТ 2" in source
