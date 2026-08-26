"""Этап 8.9: базовые стратегии как линейка (§10 ТЗ). Синтетика с известным ответом.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Линейка бесполезна, если она гнётся.
Согнуть её можно тремя способами, и каждый из них выглядел бы правдоподобно в
отчёте:

  * монета, которую нельзя повторить — два прогона дают два разных ответа,
    и ни один нельзя проверить;
  * встречная цель, взятая сегодняшняя вместо исторической — линейка молча
    получает знание, которого в тот момент не существовало;
  * потерянное происхождение цели — через месяц нечем доказать, что подмены
    не было.

Плюс проверка, без которой сравнение недействительно: always_buy и always_sell
на одном моменте обязаны занять ПРОТИВОПОЛОЖНЫЕ направления, и обе строки
обязаны существовать.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.barrier.outcomes import BUY, SELL
from src.baseline.strategies import (
    ALWAYS_BUY,
    ALWAYS_SELL,
    COIN_FLIP,
    GRID_BUY,
    GRID_SELL,
    GRID_STRATEGIES,
    SIGNAL_STRATEGIES,
    SOURCE_FROZEN,
    STRATEGIES,
    SYSTEM,
    coin_flip_direction,
    direction_for,
    hourly_grid_entries,
    risk_target_source,
)
from src.core.config import settings

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5433/agenttrade)"
    ),
)

_T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_SEED = 20260826


# --- §4. Монета обязана быть воспроизводимой -------------------------------

def test_coin_is_reproducible_with_the_same_seed() -> None:
    """То же зерно и та же пара — то же направление. Всегда."""
    first = [coin_flip_direction(_SEED, sid, 4) for sid in range(200)]
    second = [coin_flip_direction(_SEED, sid, 4) for sid in range(200)]
    assert first == second


def test_coin_differs_with_another_seed() -> None:
    """Другое зерно — другая монета. Иначе зерно ничего не значило бы."""
    a = [coin_flip_direction(_SEED, sid, 4) for sid in range(200)]
    b = [coin_flip_direction(_SEED + 1, sid, 4) for sid in range(200)]
    assert a != b


def test_coin_does_not_depend_on_iteration_order() -> None:
    """Направление привязано к паре, а не к порядку обхода.

    Это главное отличие от ``random.seed(...)``: там достаточно вставить один
    сигнал в середину выборки, чтобы вся монета легла заново на другие сигналы,
    и прежние числа отчёта перестали воспроизводиться — молча.
    """
    forward = {sid: coin_flip_direction(_SEED, sid, 4) for sid in range(100)}
    backward = {sid: coin_flip_direction(_SEED, sid, 4) for sid in reversed(range(100))}
    assert forward == backward


def test_coin_distinguishes_horizons() -> None:
    """Один сигнал на разных горизонтах — независимые броски.

    Иначе монета на всех четырёх горизонтах одного сигнала совпадала бы, и
    выборка из 46 тысяч пар вела бы себя как выборка из 11 тысяч.
    """
    per_horizon = {h: coin_flip_direction(_SEED, 12345, h) for h in (1, 4, 12, 24)}
    assert len(set(per_horizon.values())) > 1


def test_coin_is_not_degenerate() -> None:
    """Монета обязана быть похожа на монету: примерно поровну, а не всё в одну."""
    flips = [coin_flip_direction(_SEED, sid, 4) for sid in range(2000)]
    share_buy = flips.count(BUY) / len(flips)
    assert 0.45 <= share_buy <= 0.55, share_buy


# --- §4. Направления стратегий ----------------------------------------------

def test_always_buy_and_always_sell_are_opposite() -> None:
    """На одном и том же моменте они обязаны занять встречные направления."""
    for signal_direction in (BUY, SELL):
        buy = direction_for(ALWAYS_BUY, signal_direction=signal_direction)
        sell = direction_for(ALWAYS_SELL, signal_direction=signal_direction)
        assert (buy, sell) == (BUY, SELL)
        assert buy != sell


def test_grid_directions_are_fixed_and_opposite() -> None:
    """У сетки случайности нет вовсе: направление задано именем стратегии."""
    assert direction_for(GRID_BUY) == BUY
    assert direction_for(GRID_SELL) == SELL


def test_system_copies_the_signal_direction() -> None:
    assert direction_for(SYSTEM, signal_direction=SELL) == SELL


def test_system_without_a_signal_direction_is_refused() -> None:
    """Стратегия system без решения системы бессмысленна — отвергаем явно."""
    with pytest.raises(ValueError, match="направления сигнала"):
        direction_for(SYSTEM, signal_direction=None)


def test_coin_without_seed_is_refused() -> None:
    with pytest.raises(ValueError, match="зерно"):
        direction_for(COIN_FLIP, signal_direction=BUY)


def test_strategy_vocabulary_is_closed() -> None:
    """Шесть стратегий — столько же, сколько разрешает ограничение БД."""
    assert len(STRATEGIES) == 6
    assert set(SIGNAL_STRATEGIES) == {ALWAYS_BUY, ALWAYS_SELL, COIN_FLIP, SYSTEM}
    assert set(GRID_STRATEGIES) == {GRID_BUY, GRID_SELL}


# --- §6. Подпись источника цели ----------------------------------------------

def test_target_source_names_the_day_it_came_from() -> None:
    """Подпись несёт ДАТУ строки risk_targets, а не дату расчёта."""
    computed = datetime(2026, 8, 24, 3, 40, tzinfo=UTC)
    assert risk_target_source(computed) == "risk_targets:2026-08-24"


def test_target_source_matches_the_database_constraint() -> None:
    """Формат подписи совпадает с тем, что разрешает ограничение миграции 016."""
    import re

    pattern = re.compile(r"^risk_targets:\d{4}-\d{2}-\d{2}$")
    assert pattern.match(risk_target_source(_T0))
    assert SOURCE_FROZEN == "frozen"


# --- §5. Сетка --------------------------------------------------------------

def test_grid_entries_are_hourly_on_the_hour() -> None:
    entries = hourly_grid_entries(_T0, _T0 + timedelta(hours=3))
    assert entries == [_T0 + timedelta(hours=i) for i in range(4)]
    assert all(e.minute == 0 and e.second == 0 for e in entries)


def test_grid_start_rounds_up_not_down() -> None:
    """Вход раньше начала окна наблюдения невозможен.

    Округление вниз дало бы вход в момент, для которого данных ещё нет, —
    и «фон рынка» начинался бы раньше самого рынка.
    """
    entries = hourly_grid_entries(_T0 + timedelta(minutes=17), _T0 + timedelta(hours=2))
    assert entries[0] == _T0 + timedelta(hours=1)


def test_grid_refuses_naive_time() -> None:
    with pytest.raises(ValueError, match="часовым поясом"):
        hourly_grid_entries(datetime(2026, 8, 25, 12, 0), _T0)


def test_grid_of_an_empty_window_is_empty() -> None:
    assert hourly_grid_entries(_T0 + timedelta(minutes=1), _T0 + timedelta(minutes=30)) == []


# --- §2. Жёсткая граница этапа ----------------------------------------------

def test_logic_version_is_not_raised() -> None:
    """LOGIC_VERSION остаётся 5: этап измеряет, а не меняет систему."""
    assert settings.LOGIC_VERSION == 5


def test_stop_and_cost_are_not_duplicated_for_baselines() -> None:
    """У линейки НЕТ своих предела и издержек — только общие с системой.

    Отдельные ключи позволили бы сравнить систему при одном пределе с монетой
    при другом, и разница отражала бы разные условия, а не разные правила.
    """
    fields = set(type(settings).model_fields)
    assert "BASELINE_STOP_PCT" not in fields
    assert "BASELINE_COST_ROUNDTRIP_PCT" not in fields
    assert settings.BARRIER_STOP_PCT > 0
    assert settings.RISK_COST_ROUNDTRIP_PCT > 0


def test_outcome_rule_is_not_reimplemented() -> None:
    """Правило исхода берётся из 8.8 как есть — второй реализации нет (§2 ТЗ).

    Проверка текстовая и намеренно грубая: она ловит именно то, что запрещено,
    — появление собственного правила касания в пакете линейки.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in (root / "src" / "baseline").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "def resolve(" not in text, path
        assert "def _touches(" not in text, path


def test_baseline_is_not_imported_by_the_hot_path() -> None:
    """Решение, уведомления, агенты, оценщик и цели о линейке не знают."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for package in ("agents", "decision", "notify", "evaluator", "risk"):
        for path in (root / "src" / package).rglob("*.py"):
            assert "src.baseline" not in path.read_text(encoding="utf-8"), path


# --- §8. Бутстрэп ------------------------------------------------------------

def test_bootstrap_verdicts_are_the_three_required_phrases() -> None:
    """Формулировки дословно те, что предписаны §8 ТЗ, — и других нет."""
    from scripts.baseline_bootstrap import (
        VERDICT_BETTER,
        VERDICT_UNKNOWN,
        VERDICT_WORSE,
        verdict,
    )

    assert VERDICT_BETTER == "система лучше, интервал не пересекает ноль"
    assert VERDICT_WORSE == "система хуже, интервал не пересекает ноль"
    assert VERDICT_UNKNOWN == "различить нельзя, выборки не хватает"
    assert verdict(0.1, 0.5) == VERDICT_BETTER
    assert verdict(-0.5, -0.1) == VERDICT_WORSE
    assert verdict(-0.5, 0.5) == VERDICT_UNKNOWN


def test_interval_touching_zero_is_not_called_a_difference() -> None:
    """Интервал, упирающийся в ноль, различием НЕ считается.

    Граница строгая намеренно: `lo == 0` означает, что ноль в интервал входит,
    а значит «разницы нет» остаётся правдоподобным объяснением.
    """
    from scripts.baseline_bootstrap import VERDICT_UNKNOWN, verdict

    assert verdict(0.0, 1.0) == VERDICT_UNKNOWN
    assert verdict(-1.0, 0.0) == VERDICT_UNKNOWN


def test_bootstrap_finds_a_real_difference() -> None:
    """На выборке с явной разницей интервал ноль не пересекает."""
    import numpy as np

    from scripts.baseline_bootstrap import bootstrap_diff, verdict

    rng = np.random.default_rng(1)
    base = rng.normal(0.0, 0.5, 500)
    system = base + 1.0
    observed, lo, hi = bootstrap_diff(system, base, resamples=2000, seed=7)
    assert observed == pytest.approx(1.0, abs=1e-9)
    assert verdict(lo, hi) == "система лучше, интервал не пересекает ноль"


def test_bootstrap_admits_when_it_cannot_tell() -> None:
    """На выборке без разницы интервал ноль пересекает — и это говорится прямо."""
    import numpy as np

    from scripts.baseline_bootstrap import bootstrap_diff, verdict

    rng = np.random.default_rng(2)
    base = rng.normal(0.0, 1.0, 40)
    system = rng.normal(0.0, 1.0, 40)
    _observed, lo, hi = bootstrap_diff(system, base, resamples=2000, seed=7)
    assert verdict(lo, hi) == "различить нельзя, выборки не хватает"


def test_bootstrap_is_reproducible() -> None:
    """Тот же вход и то же зерно — тот же интервал, до последнего знака."""
    import numpy as np

    from scripts.baseline_bootstrap import bootstrap_diff

    rng = np.random.default_rng(3)
    base = rng.normal(0.0, 1.0, 100)
    system = base + 0.2
    first = bootstrap_diff(system, base, resamples=1000, seed=11)
    second = bootstrap_diff(system, base, resamples=1000, seed=11)
    assert first == second


def test_bootstrap_is_paired_not_independent() -> None:
    """Пересобираются ПАРЫ, а не два ряда по отдельности.

    На идеально спаренных данных (разница постоянна) парный бутстрэп обязан
    дать интервал НУЛЕВОЙ ширины: сколько пар ни перебирай, разница та же.
    Независимая пересборка дала бы здесь широкий интервал — то есть объявила
    бы «различить нельзя» там, где различие точное.
    """
    import numpy as np

    from scripts.baseline_bootstrap import bootstrap_diff

    rng = np.random.default_rng(4)
    base = rng.normal(0.0, 5.0, 300)
    system = base + 0.5
    observed, lo, hi = bootstrap_diff(system, base, resamples=2000, seed=13)
    assert (observed, lo, hi) == pytest.approx((0.5, 0.5, 0.5), abs=1e-9)


def test_bootstrap_refuses_mismatched_sides() -> None:
    import numpy as np

    from scripts.baseline_bootstrap import bootstrap_diff

    with pytest.raises(ValueError, match="одной длины"):
        bootstrap_diff(np.zeros(3), np.zeros(4), resamples=10, seed=1)


# --- §10. Проверки, которым нужна база --------------------------------------

async def _refuse_if_table_has_data(conn) -> None:
    """Останавливает разрушительную проверку, если в таблице есть данные.

    Откат миграции УДАЛЯЕТ таблицу целиком. Тест, направленный на базу с
    посчитанной линейкой, стёр бы её молча. Проверка не «старается быть
    аккуратной», а ОТКАЗЫВАЕТСЯ выполняться.
    """
    if await conn.fetchval("SELECT to_regclass('strategy_outcomes');") is None:
        return
    rows = await conn.fetchval("SELECT count(*) FROM strategy_outcomes;")
    if rows:
        pytest.skip(
            f"в strategy_outcomes {rows} строк: откат миграции удалил бы их. "
            "AT_TEST_DSN обязан указывать на ОДНОРАЗОВУЮ базу"
        )


@needs_db
async def test_migration_is_idempotent_and_reversible() -> None:
    """Миграция применяется, применяется повторно, откатывается и снова встаёт.

    ВНИМАНИЕ: тест РАЗРУШИТЕЛЬНЫЙ — он удаляет таблицу.
    """
    import pathlib

    import asyncpg

    root = pathlib.Path(__file__).resolve().parent.parent
    forward = (root / "db/migrations/016_strategy_outcomes.sql").read_text("utf-8")
    back = (root / "db/migrations/016_strategy_outcomes_rollback.sql").read_text("utf-8")

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _refuse_if_table_has_data(conn)
        await conn.execute(forward)
        await conn.execute(forward)
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'strategy_outcomes'::regclass AND contype = 'c';"
        )
        assert count >= 9
        await conn.execute(back)
        assert await conn.fetchval("SELECT to_regclass('strategy_outcomes');") is None
        await conn.execute(forward)
        assert await conn.fetchval("SELECT to_regclass('strategy_outcomes');")
    finally:
        await conn.close()


@needs_db
async def test_opposite_strategies_coexist_on_the_same_moment() -> None:
    """always_buy и always_sell на ОДНОМ моменте: обе строки есть, направления встречные.

    Это проверка §10.5 и одновременно проверка первичного ключа: если бы ключ
    не включал стратегию, вторая строка вытеснила бы первую, и линейка молча
    состояла бы из одной половины.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id, signal_id = await _seed_signal(conn)
        entry_ts = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
        insert = (
            "INSERT INTO strategy_outcomes (strategy, instrument_id, entry_ts, "
            "horizon_h, signal_id, logic_version, direction, price_at_entry, "
            "target_pct, target_source, stop_pct, cost_pct, outcome, mae_pct, "
            "mfe_pct, resolution) VALUES "
            "($1,$2,$3,4,$4,5,$5,100,1,'frozen',1,0.22,'ambiguous',0.5,0.5,'1m');"
        )
        await conn.execute(insert, ALWAYS_BUY, instrument_id, entry_ts, signal_id, BUY)
        await conn.execute(insert, ALWAYS_SELL, instrument_id, entry_ts, signal_id, SELL)

        rows = await conn.fetch(
            "SELECT strategy, direction FROM strategy_outcomes "
            "WHERE instrument_id = $1 AND entry_ts = $2 ORDER BY strategy;",
            instrument_id, entry_ts,
        )
        assert len(rows) == 2
        directions = {r["strategy"]: r["direction"] for r in rows}
        assert directions[ALWAYS_BUY] == BUY
        assert directions[ALWAYS_SELL] == SELL
        assert directions[ALWAYS_BUY] != directions[ALWAYS_SELL]

        # И задвоить одну стратегию на том же моменте нельзя.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                insert, ALWAYS_BUY, instrument_id, entry_ts, signal_id, BUY
            )
    finally:
        # Убираем РОВНО свои строки — по инструменту и моменту, которые этот
        # тест сам и завёл. Более широкое условие (например «все сигналы buy
        # версии 5») снесло бы чужие данные той же базы: проверка обязана
        # чистить за собой, а не за всеми.
        await conn.execute(
            "DELETE FROM strategy_outcomes "
            "WHERE instrument_id = $1 AND entry_ts = $2;",
            instrument_id, entry_ts,
        )
        await conn.close()


@needs_db
async def test_database_refuses_invented_values() -> None:
    """Ограничения ловят подделку раньше, чем она попадёт в отчёт."""
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id, signal_id = await _seed_signal(conn)
        entry_ts = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
        base = (
            "INSERT INTO strategy_outcomes (strategy, instrument_id, entry_ts, "
            "horizon_h, signal_id, logic_version, direction, price_at_entry, "
            "target_pct, target_source, stop_pct, cost_pct, outcome, mae_pct, "
            "mfe_pct, resolution, seed) VALUES "
            "($1,$2,$3,4,$4,5,'buy',100,1,$5,1,0.22,$6,0.5,0.5,'1m',$7);"
        )
        # Стратегия, названная своими словами.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, "почти монета", instrument_id, entry_ts,
                               signal_id, "frozen", "timeout", None)
        # Источник цели без даты — происхождение неизвестно.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, ALWAYS_BUY, instrument_id, entry_ts,
                               signal_id, "risk_targets", "timeout", None)
        # Сетка, привязанная к сигналу, — это уже не фон рынка.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, GRID_BUY, instrument_id, entry_ts,
                               signal_id, "risk_targets:2026-08-24", "timeout", None)
        # Зерно у стратегии без случайности означало бы, что она была.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, ALWAYS_BUY, instrument_id, entry_ts,
                               signal_id, "frozen", "timeout", 20260826)
        # Монета без зерна — невоспроизводимая монета.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base, COIN_FLIP, instrument_id, entry_ts,
                               signal_id, "frozen", "timeout", None)
    finally:
        await conn.close()


@needs_db
async def test_historical_target_is_taken_as_of_the_entry_not_today() -> None:
    """Встречная цель берётся ЗА ДАТУ ВХОДА, даже когда есть свежее значение.

    Это центральная проверка этапа. В базу кладутся ДВЕ цели: старая (за день
    до входа) и новая (после входа) с заведомо разными значениями. Выбрана
    обязана быть старая: новая посчитана по рынку, которого в момент входа
    ещё не было.
    """
    import asyncpg

    from src.core.db import db as database

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id, _signal_id = await _seed_signal(conn)
        entry_ts = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        await conn.execute("DELETE FROM risk_targets WHERE instrument_id = $1;",
                           instrument_id)
        for computed_at, target in (
            (datetime(2026, 8, 24, 3, 40, tzinfo=UTC), 1.25),
            (datetime(2026, 8, 26, 3, 40, tzinfo=UTC), 9.99),
        ):
            await conn.execute(
                "INSERT INTO risk_targets (instrument_id,horizon_h,direction,"
                "computed_at,window_days,data_from,data_to,n_observations,"
                "target_pct,cost_roundtrip_pct,covers_fees,source,targets_version) "
                "VALUES ($1,4,'sell',$2,90,$3,$2,600,$4,0.22,true,'x',1);",
                instrument_id, computed_at, computed_at - timedelta(days=90), target,
            )
    finally:
        await conn.close()

    # Подключение к ТЕСТОВОЙ базе, а не к продакшн-адресу из настроек: пул
    # подставляется напрямую. Проверяется при этом ШТАТНЫЙ метод db, а не его
    # копия в тесте — иначе проверялось бы не то, что поедет на сервер.
    database._pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    try:
        row = await database.get_risk_target_asof(instrument_id, 4, "sell", entry_ts)
        assert row is not None
        assert float(row["target_pct"]) == pytest.approx(1.25)
        assert risk_target_source(row["computed_at"]) == "risk_targets:2026-08-24"

        # А до появления первой цели — ничего, и это не повод подставить 9.99.
        earlier = await database.get_risk_target_asof(
            instrument_id, 4, "sell", datetime(2026, 8, 23, tzinfo=UTC)
        )
        assert earlier is None
    finally:
        await database.close()
        database._pool = None


async def _seed_signal(conn) -> tuple[int, int]:
    """Инструмент и один направленный сигнал версии 5 для проверок схемы."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', 'TEST89/USDT', 'TEST89', 'USDT', 'spot') "
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
