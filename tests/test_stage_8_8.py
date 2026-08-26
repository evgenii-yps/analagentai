"""Этап 8.8: исход по границам (§10 ТЗ). Синтетические ряды с известным ответом.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Правило «какая граница задета
первой» выглядит очевидным, и ровно поэтому его легко реализовать неверно так,
что ошибка не видна: она даёт правдоподобное распределение исходов, по которому
человек делает вывод о системе. Проверяются те шесть мест, где ошибка была бы
незаметной:

  * порядок касаний определяется по КАСАНИЮ (high/low), а не по закрытию;
  * свеча момента решения не попадает в собственное окно;
  * одновременное касание внутри одного бара НЕ приводится к target или stop;
  * неполный ряд не выдаётся за timeout («ни одна не задета» — утверждение о
    данных, которых мы не видели);
  * касание РОВНО В ГРАНИЦУ засчитывается;
  * издержки приходят параметром, а не зашиты числом.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.

``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу: проверка отката миграции
удаляет таблицу ``signal_outcomes_barrier`` целиком. Если в таблице есть строки,
проверка себя не выполняет — она пропускается с объяснением, а не «старается
быть аккуратной».
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.barrier.outcomes import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_DATA,
    OUTCOME_STOP,
    OUTCOME_TARGET,
    OUTCOME_TIMEOUT,
    OUTCOMES,
    RESOLUTION_1H,
    RESOLUTION_1M,
    Bar,
    contiguous_prefix,
    expected_bars,
    levels,
    net_pnl,
    resolve,
    window_bounds,
)
from src.barrier.runner import ambiguous_share, build_row
from src.core.config import settings

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5433/agenttrade)"
    ),
)

# Момент решения — ровно на границе часа, чтобы ожидаемые времена читались
# глазами. Смещение внутрь часа проверяется отдельным тестом.
_T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
_PRICE = 100.0
_TARGET_PCT = 1.0     # цель  buy = 101.0, sell = 99.0
_STOP_PCT = 1.0       # предел buy =  99.0, sell = 101.0
_COST = 0.22          # круговые издержки; в коде это значение НЕ зашито


def _bars(
    rows: list[tuple[float, float]],
    *,
    start: datetime | None = None,
    step_h: int = 1,
    closes: list[float] | None = None,
) -> list[Bar]:
    """Ряд из пар ``(high, low)``. Закрытие — середина, если не задано явно."""
    first = _T0 + timedelta(hours=step_h) if start is None else start
    out: list[Bar] = []
    for index, (high, low) in enumerate(rows):
        close = (high + low) / 2 if closes is None else closes[index]
        out.append(Bar(
            ts=first + timedelta(hours=step_h * index),
            high=high, low=low, close=close,
        ))
    return out


def _resolve(bars: list[Bar], *, direction: str = "buy", horizon_h: int = 4,
             resolution: str = RESOLUTION_1H, signal_ts: datetime = _T0,
             target_pct: float = _TARGET_PCT, stop_pct: float = _STOP_PCT):
    return resolve(
        bars, signal_ts=signal_ts, horizon_h=horizon_h, price_at_signal=_PRICE,
        target_pct=target_pct, stop_pct=stop_pct, cost_pct=_COST,
        direction=direction, resolution=resolution,
    )


# --- §3. Пять исходов на рядах с заранее известным ответом -----------------

def test_target_before_stop() -> None:
    """Цель задета раньше предела — исход target, и он датирован."""
    bars = _bars([
        (100.5, 99.5),    # ничего
        (101.2, 99.8),    # ЦЕЛЬ (101.0) — второй бар окна
        (99.0, 98.0),     # предел, но он уже неважен
        (100.0, 99.9),
    ])
    result = _resolve(bars)
    assert result.outcome == OUTCOME_TARGET
    assert result.bars_to_hit == 2
    assert result.hit_at == _T0 + timedelta(hours=2)
    # Итог в деньгах: цель минус издержки, и ничего кроме.
    assert result.net_pnl_pct == pytest.approx(_TARGET_PCT - _COST)


def test_stop_before_target() -> None:
    """Предел задет раньше цели — исход stop с отрицательным итогом."""
    bars = _bars([
        (100.4, 98.9),    # ПРЕДЕЛ (99.0) — первый же бар окна
        (101.5, 100.0),   # цель, но позже
        (100.0, 99.5),
        (100.0, 99.5),
    ])
    result = _resolve(bars)
    assert result.outcome == OUTCOME_STOP
    assert result.bars_to_hit == 1
    assert result.net_pnl_pct == pytest.approx(-_STOP_PCT - _COST)


def test_both_in_one_bar_is_ambiguous() -> None:
    """Обе границы внутри одного бара — порядок неизвестен, и он таким остаётся.

    Это главный тест раздела: любое приведение к target или stop здесь было бы
    выдумкой, неотличимой в отчёте от измерения.
    """
    bars = _bars([
        (100.2, 99.8),
        (101.5, 98.5),    # и цель, и предел в ОДНОМ баре
        (100.0, 99.9),
        (100.0, 99.9),
    ])
    result = _resolve(bars)
    assert result.outcome == OUTCOME_AMBIGUOUS
    assert result.hit_at is None
    assert result.bars_to_hit is None
    # Итога в деньгах у неизвестного исхода нет. Ноль был бы утверждением
    # «человек не заработал и не потерял», которого никто не мерил.
    assert result.net_pnl_pct is None


def test_neither_touched_is_timeout() -> None:
    """Ни одна граница не задета за полное окно — timeout по факту движения."""
    bars = _bars(
        [(100.4, 99.6), (100.3, 99.7), (100.2, 99.8), (100.5, 99.5)],
        closes=[100.0, 100.1, 100.2, 100.4],
    )
    result = _resolve(bars)
    assert result.outcome == OUTCOME_TIMEOUT
    assert result.hit_at is None
    # Движение к сроку: закрытие последнего бара окна (100.4) против 100.0.
    assert result.net_pnl_pct == pytest.approx(0.4 - _COST)


def test_gap_in_series_is_no_data() -> None:
    """Разрыв ряда до разрешения — no_data, а НЕ timeout.

    «До срока не задета ни одна» — утверждение обо всём окне. Сделать его по
    ряду с дырой значит утверждать то, чего не наблюдали.
    """
    bars = _bars([(100.4, 99.6), (100.3, 99.7)])          # два бара из четырёх
    result = _resolve(bars)
    assert result.outcome == OUTCOME_NO_DATA
    assert result.net_pnl_pct is None
    assert result.bars_seen == 2
    assert result.bars_expected == 4


def test_gap_after_hit_keeps_the_hit() -> None:
    """Касание ДО разрыва остаётся фактом: дыра за ним ничего не отменяет."""
    bars = _bars([(100.4, 99.6), (101.4, 100.0)])         # цель во втором баре
    result = _resolve(bars)
    assert result.outcome == OUTCOME_TARGET
    assert result.bars_to_hit == 2


def test_empty_series_is_no_data() -> None:
    """Ряда нет вовсе — no_data, и mae/mfe в строке заполнители, а не измерение."""
    result = _resolve([])
    assert result.outcome == OUTCOME_NO_DATA
    assert (result.mae_pct, result.mfe_pct) == (0.0, 0.0)
    assert result.bars_seen == 0


# --- §3. Касание ровно в границу -------------------------------------------

def test_exact_touch_of_target_counts() -> None:
    """high РОВНО в цель — засчитывается. Сравнение нестрогое."""
    bars = _bars([(101.0, 99.5), (100.0, 99.5), (100.0, 99.5), (100.0, 99.5)])
    assert _resolve(bars).outcome == OUTCOME_TARGET


def test_exact_touch_of_stop_counts() -> None:
    """low РОВНО в предел — засчитывается."""
    bars = _bars([(100.5, 99.0), (100.0, 99.5), (100.0, 99.5), (100.0, 99.5)])
    assert _resolve(bars).outcome == OUTCOME_STOP


def test_one_tick_short_of_target_does_not_count() -> None:
    """На тик НЕ достав до цели — исход другой. Граница именно там, где сказано."""
    bars = _bars([(100.999, 99.5), (100.0, 99.5), (100.0, 99.5), (100.0, 99.5)])
    assert _resolve(bars).outcome == OUTCOME_TIMEOUT


# --- §3. Направление sell симметрично --------------------------------------

def test_sell_target_is_below_price() -> None:
    """Для продажи цель ниже цены решения, предел выше."""
    target_price, stop_price = levels(_PRICE, _TARGET_PCT, _STOP_PCT, "sell")
    assert (target_price, stop_price) == (99.0, 101.0)
    bars = _bars([(100.4, 99.6), (100.2, 98.9), (100.0, 99.5), (100.0, 99.5)])
    result = _resolve(bars, direction="sell")
    assert result.outcome == OUTCOME_TARGET
    assert result.bars_to_hit == 2


def test_sell_timeout_pnl_has_inverted_sign() -> None:
    """Для продажи падение цены — это ПЛЮС, а не минус."""
    bars = _bars(
        [(100.4, 99.6), (100.3, 99.7), (100.2, 99.8), (100.4, 99.5)],
        closes=[100.0, 99.9, 99.8, 99.6],
    )
    result = _resolve(bars, direction="sell")
    assert result.outcome == OUTCOME_TIMEOUT
    assert result.net_pnl_pct == pytest.approx(0.4 - _COST)


# --- §3. Свеча решения в собственное окно не входит ------------------------

def test_decision_bar_is_excluded_from_its_own_window() -> None:
    """Экстремум бара решения не участвует: он известен только постфактум.

    Ряд содержит бар, открытый РОВНО в момент сигнала, и он пробивает цель.
    Если бы он попал в окно, исход был бы target на первом баре.
    """
    loud = Bar(ts=_T0, high=105.0, low=95.0, close=100.0)
    quiet = _bars([(100.4, 99.6), (100.3, 99.7), (100.2, 99.8), (100.1, 99.9)])
    result = _resolve([loud, *quiet])
    assert result.outcome == OUTCOME_TIMEOUT


def test_window_bounds_start_after_the_decision_bar() -> None:
    """Границы окна: первый бар — следующий за баром решения, всего h баров."""
    first, last = window_bounds(_T0, 4, RESOLUTION_1H)
    assert first == _T0 + timedelta(hours=1)
    assert last == _T0 + timedelta(hours=4)
    assert expected_bars(4, RESOLUTION_1H) == 4
    assert expected_bars(4, RESOLUTION_1M) == 240


def test_window_bounds_snap_to_the_grid_inside_the_hour() -> None:
    """Сигнал в 12:37 принадлежит бару 12:00 — окно начинается с 13:00."""
    first, last = window_bounds(_T0 + timedelta(minutes=37), 2, RESOLUTION_1H)
    assert first == _T0 + timedelta(hours=1)
    assert last == _T0 + timedelta(hours=2)


def test_naive_timestamp_is_refused() -> None:
    """Наивное время молча уехало бы на смещение пояса — отвергаем явно."""
    with pytest.raises(ValueError, match="часовым поясом"):
        window_bounds(datetime(2026, 8, 20, 12, 0), 1, RESOLUTION_1H)


# --- §4. Минутное разрешение снимает неопределённость -----------------------

def test_minute_resolution_orders_touches_inside_the_hour() -> None:
    """Тот же час, но по минутам: порядок касаний известен, ambiguous не нужен.

    Первая минута задевает предел, третья — цель. На часовом баре это был бы
    ambiguous; на минутном — stop, потому что порядок ВИДЕН.
    """
    start = _T0 + timedelta(minutes=1)
    minutes = [
        Bar(ts=start, high=100.2, low=98.9, close=99.0),                    # предел
        Bar(ts=start + timedelta(minutes=1), high=100.5, low=99.5, close=100.0),
        Bar(ts=start + timedelta(minutes=2), high=101.4, low=100.0, close=101.0),  # цель
    ]
    minutes += [
        Bar(ts=start + timedelta(minutes=i), high=100.1, low=99.9, close=100.0)
        for i in range(3, 60)
    ]
    result = _resolve(minutes, horizon_h=1, resolution=RESOLUTION_1M)
    assert result.outcome == OUTCOME_STOP
    assert result.bars_to_hit == 1
    assert result.resolution == RESOLUTION_1M


def test_ambiguous_survives_even_at_minute_resolution() -> None:
    """Обе границы внутри ОДНОЙ минуты — порядок по-прежнему неизвестен."""
    start = _T0 + timedelta(minutes=1)
    minutes = [Bar(ts=start, high=101.5, low=98.5, close=100.0)]
    minutes += [
        Bar(ts=start + timedelta(minutes=i), high=100.1, low=99.9, close=100.0)
        for i in range(1, 60)
    ]
    result = _resolve(minutes, horizon_h=1, resolution=RESOLUTION_1M)
    assert result.outcome == OUTCOME_AMBIGUOUS


def test_contiguous_prefix_cuts_at_the_first_hole() -> None:
    """Отрезок обрывается на первом же пропуске, а не «сшивается» через него."""
    bars = [
        Bar(ts=_T0 + timedelta(hours=1), high=1, low=1, close=1),
        Bar(ts=_T0 + timedelta(hours=2), high=1, low=1, close=1),
        Bar(ts=_T0 + timedelta(hours=4), high=1, low=1, close=1),   # дыра в 3
    ]
    prefix = contiguous_prefix(bars, _T0 + timedelta(hours=1), RESOLUTION_1H)
    assert len(prefix) == 2


def test_series_starting_late_gives_no_prefix() -> None:
    """Ряд, начатый позже первого бара окна, покрытием окна не является."""
    bars = [Bar(ts=_T0 + timedelta(hours=2), high=1, low=1, close=1)]
    assert contiguous_prefix(bars, _T0 + timedelta(hours=1), RESOLUTION_1H) == []


# --- §4. Доля ambiguous ------------------------------------------------------

def test_ambiguous_share_is_a_share_of_all_outcomes() -> None:
    assert ambiguous_share({OUTCOME_TARGET: 3, OUTCOME_AMBIGUOUS: 1}) == 25.0


def test_ambiguous_share_of_nothing_is_not_zero() -> None:
    """Доля от нуля исходов — отсутствие величины, а не ноль процентов."""
    assert ambiguous_share({}) is None


# --- §5. Итог в деньгах ------------------------------------------------------

def test_cost_is_a_parameter_not_a_literal() -> None:
    """Издержки приходят параметром: другая комиссия — другой итог.

    Зашитое 0.22 сделало бы этот тест невозможным, а предупреждение о
    непокрытых издержках однажды начало бы врать молча.
    """
    common = dict(target_pct=1.0, stop_pct=1.0, price_at_signal=_PRICE,
                  close_at_deadline=None, direction="buy")
    assert net_pnl(OUTCOME_TARGET, cost_pct=0.22, **common) == pytest.approx(0.78)
    assert net_pnl(OUTCOME_TARGET, cost_pct=0.50, **common) == pytest.approx(0.50)


def test_unknown_outcomes_have_no_money_result() -> None:
    common = dict(target_pct=1.0, stop_pct=1.0, cost_pct=_COST,
                  price_at_signal=_PRICE, close_at_deadline=100.0, direction="buy")
    assert net_pnl(OUTCOME_AMBIGUOUS, **common) is None
    assert net_pnl(OUTCOME_NO_DATA, **common) is None


def test_target_pnl_uses_frozen_target_not_the_actual_move() -> None:
    """Итог по цели считается ПО ЦЕЛИ, а не по тому, куда цена ушла дальше.

    Человек, поставивший ордер на цель, получил бы ровно цель: то, что цена
    потом улетела вдвое дальше, ему не досталось бы.
    """
    bars = _bars([(100.2, 99.8), (105.0, 100.0), (110.0, 104.0), (110.0, 104.0)])
    result = _resolve(bars, target_pct=1.0)
    assert result.outcome == OUTCOME_TARGET
    assert result.net_pnl_pct == pytest.approx(1.0 - _COST)


# --- §8. mae/mfe считаются по всему окну ------------------------------------

def test_excursions_cover_the_whole_window_not_just_up_to_the_hit() -> None:
    """mae/mfe описывают ОКНО, а не сделку, — иначе §8 неотвечаем.

    Вопрос §8 «сработал бы предел 0.3%?» — про другой уровень предела, при
    котором сделка закрылась бы в другой момент. Обрезка mae по факту
    срабатывания текущего предела отвечала бы на чужой вопрос.
    """
    bars = _bars([
        (101.2, 99.9),    # цель на первом баре — исход решён здесь
        (100.0, 97.0),    # но окно продолжается, и оно уходило на 3% вниз
        (104.0, 100.0),   # и на 4% вверх
        (100.0, 99.0),
    ])
    result = _resolve(bars)
    assert result.outcome == OUTCOME_TARGET
    assert result.bars_to_hit == 1
    assert result.mae_pct == pytest.approx(3.0)
    assert result.mfe_pct == pytest.approx(4.0)


def test_mae_may_be_negative_and_is_not_clipped() -> None:
    """Цена не ходила против сигнала — mae отрицателен. Это наблюдение."""
    bars = _bars([(100.5, 100.2), (100.6, 100.3), (100.7, 100.4), (100.8, 100.5)])
    result = _resolve(bars)
    assert result.mae_pct < 0


def test_sell_excursions_are_mirrored() -> None:
    """Для продажи «против» — это вверх, «в пользу» — вниз."""
    bars = _bars([(102.0, 99.0), (100.5, 99.5), (100.5, 99.5), (100.5, 99.5)])
    result = _resolve(bars, direction="sell", stop_pct=5.0, target_pct=5.0)
    assert result.mae_pct == pytest.approx(2.0)
    assert result.mfe_pct == pytest.approx(1.0)


# --- §6. Строка таблицы ------------------------------------------------------

def test_build_row_carries_the_frozen_levels_and_the_snapshot_of_settings() -> None:
    """В строку идут ЗАМОРОЖЕННАЯ цена и цель, а также снимок предела и издержек."""
    bars = _bars([(101.2, 99.9), (100.0, 99.5), (100.0, 99.5), (100.0, 99.5)])
    result = _resolve(bars)
    candidate = {
        "id": 77, "instrument_id": 1, "ts": _T0, "decision": "buy",
        "logic_version": 5, "direction": "buy",
        "price_at_signal": _PRICE, "target_pct": _TARGET_PCT,
    }
    row = build_row(candidate, result, horizon_h=4, stop_pct=_STOP_PCT,
                    cost_pct=_COST, computed_at=_T0)
    assert row["signal_id"] == 77
    assert row["logic_version"] == 5
    assert row["price_at_signal"] == _PRICE
    assert row["target_pct"] == _TARGET_PCT
    assert row["stop_pct"] == _STOP_PCT
    assert row["cost_pct"] == _COST
    assert row["outcome"] in OUTCOMES
    assert row["resolution"] == RESOLUTION_1H


def test_outcome_vocabulary_is_closed() -> None:
    """Ровно пять исходов §3 — столько же, сколько разрешает ограничение БД."""
    assert len(OUTCOMES) == 5
    assert set(OUTCOMES) == {
        OUTCOME_TARGET, OUTCOME_STOP, OUTCOME_TIMEOUT,
        OUTCOME_AMBIGUOUS, OUTCOME_NO_DATA,
    }


# --- §1. Жёсткая граница этапа ----------------------------------------------

def test_logic_version_is_not_raised() -> None:
    """LOGIC_VERSION остаётся 5: этап не меняет ни одного решения системы."""
    assert settings.LOGIC_VERSION == 5


def test_stop_default_is_the_owner_assumption() -> None:
    """Умолчание предела — 1.0%, и это ПРЕДПОЛОЖЕНИЕ, а не измерение (§8)."""
    assert settings.BARRIER_STOP_PCT == 1.0


def test_barrier_is_not_imported_by_the_hot_path() -> None:
    """Каталоги решения, агентов и уведомлений об этом пакете не знают.

    Проверка текстовая и намеренно грубая: она ловит именно то, что запрещено
    §1 ТЗ, — появление расчёта исхода в горячем пути.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for package in ("agents", "decision", "notify", "evaluator"):
        for path in (root / "src" / package).rglob("*.py"):
            assert "src.barrier" not in path.read_text(encoding="utf-8"), path


# --- §10. Проверки, которым нужна база --------------------------------------

async def _refuse_if_table_has_data(conn) -> None:
    """Останавливает разрушительную проверку, если в таблице есть данные.

    Откат миграции УДАЛЯЕТ таблицу целиком. Тест, направленный на базу с
    посчитанными исходами (а тем более на продакшн), стёр бы их молча. Поэтому
    проверка не «старается быть аккуратной», а ОТКАЗЫВАЕТСЯ выполняться:
    AT_TEST_DSN обязан указывать на одноразовую базу.
    """
    if await conn.fetchval("SELECT to_regclass('signal_outcomes_barrier');") is None:
        return
    rows = await conn.fetchval("SELECT count(*) FROM signal_outcomes_barrier;")
    if rows:
        pytest.skip(
            f"в signal_outcomes_barrier {rows} строк: откат миграции удалил бы их. "
            "AT_TEST_DSN обязан указывать на ОДНОРАЗОВУЮ базу"
        )


@needs_db
async def test_migration_is_idempotent_and_reversible() -> None:
    """Миграция применяется, применяется повторно, откатывается и снова встаёт.

    Проверяется НА ЖИВОЙ БАЗЕ, а не чтением файла: идемпотентность — свойство
    выполнения, и «в тексте написано IF NOT EXISTS» её не доказывает.

    ВНИМАНИЕ: тест РАЗРУШИТЕЛЬНЫЙ — он удаляет таблицу. Направлять AT_TEST_DSN
    на базу с данными нельзя; страховка ниже такой запуск не выполняет.
    """
    import pathlib

    import asyncpg

    root = pathlib.Path(__file__).resolve().parent.parent
    forward = (root / "db/migrations/015_barrier_outcomes.sql").read_text("utf-8")
    back = (root / "db/migrations/015_barrier_outcomes_rollback.sql").read_text("utf-8")

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _refuse_if_table_has_data(conn)
        await conn.execute(forward)
        await conn.execute(forward)          # повтор не ломается
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'signal_outcomes_barrier'::regclass;"
        )
        assert count >= 6
        await conn.execute(back)
        assert await conn.fetchval("SELECT to_regclass('signal_outcomes_barrier');") is None
        await conn.execute(forward)          # встаёт после отката
        assert await conn.fetchval("SELECT to_regclass('signal_outcomes_barrier');")
    finally:
        await conn.close()


@needs_db
async def test_primary_key_refuses_a_duplicate_row() -> None:
    """PRIMARY KEY (signal_id, horizon_h) не даёт задвоить исход."""
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        signal_id = await _seed_signal(conn)
        row = (signal_id, 4, 5, "buy", 100, 1, 1, 0.22, "timeout", None, None,
               0.1, 0.5, 0.5, "1h")
        insert = (
            "INSERT INTO signal_outcomes_barrier (signal_id, horizon_h, "
            "logic_version, direction, price_at_signal, target_pct, stop_pct, "
            "cost_pct, outcome, hit_at, bars_to_hit, net_pnl_pct, mae_pct, "
            "mfe_pct, resolution) VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15);"
        )
        await conn.execute(insert, *row)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, *row)
    finally:
        # За собой убираем всегда: иначе следующая проверка (откат миграции)
        # увидит строки, откажется выполняться и молча станет «пропущенной».
        await conn.execute(
            "DELETE FROM signal_outcomes_barrier WHERE signal_id = $1;", signal_id
        )
        await conn.close()


@needs_db
async def test_check_constraints_refuse_invented_values() -> None:
    """Исход, названный своими словами, не считается исходом — база откажет."""
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        signal_id = await _seed_signal(conn)
        insert = (
            "INSERT INTO signal_outcomes_barrier (signal_id, horizon_h, "
            "logic_version, direction, price_at_signal, target_pct, stop_pct, "
            "cost_pct, outcome, hit_at, bars_to_hit, net_pnl_pct, mae_pct, "
            "mfe_pct, resolution) VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15);"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, signal_id, 12, 5, "buy", 100, 1, 1, 0.22,
                               "почти дошло", None, None, 0.1, 0.5, 0.5, "1h")
        # ambiguous с итогом в деньгах — тоже отказ: у неизвестного исхода
        # результата не бывает.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, signal_id, 12, 5, "buy", 100, 1, 1, 0.22,
                               "ambiguous", None, None, 0.5, 0.5, 0.5, "1h")
        # target без момента касания — отказ: касание обязано быть датировано.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(insert, signal_id, 12, 5, "buy", 100, 1, 1, 0.22,
                               "target", None, None, 0.78, 0.5, 0.5, "1h")
    finally:
        await conn.close()


async def _seed_signal(conn) -> int:
    """Инструмент и один направленный сигнал версии 5 для проверок схемы."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', 'TEST/USDT', 'TEST', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE SET symbol = EXCLUDED.symbol "
        "RETURNING id;"
    )
    return await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, now() - interval '2 days', 'buy', 5) RETURNING id;",
        instrument_id,
    )
