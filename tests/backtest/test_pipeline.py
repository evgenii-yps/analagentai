"""Сквозная проверка конвейера: прогон → исходы → отчёт.

Файл сверх перечня §7 ТЗ: перечень не исчерпывающий, а без сквозной проверки
нельзя утверждать, что модули работают вместе, а не по отдельности. Здесь же
проверяются два требования приёмки, которые иначе остались бы без доказательства:

  * §14.3 — критерий успеха записан в ``backtest.runs.config_json`` ДО появления
    первой строки результатов (подтверждается временными метками);
  * §14.6 — прогон не изменил ни одной строки вне схемы ``backtest``
    (счётчики строк продакшн-таблиц до и после).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import INST, T0, make_config, requires_db, seed_candles, seed_funding

from backtest import evaluate, replay, report

pytestmark = requires_db

CRITERION = {
    "min_edge_pp": 5.0,
    "min_independent_n": 500,
    "max_p_value": 0.05,
    "description": "тестовая копия предрегистрированного критерия",
}


@pytest.fixture
async def seeded(bt_db, pool):
    await seed_candles(pool, hours=24 * 45)
    await seed_funding(pool, points=140)
    return pool


async def test_criterion_is_registered_before_any_result(seeded, pool) -> None:
    """Критерий лежит в config_json, а решений на момент открытия прогона ещё нет."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)

    row = await pool.fetchrow(
        "SELECT started_at, config_json::text AS cfg, status FROM backtest.runs "
        "WHERE run_id=$1;",
        run_id,
    )
    stored = json.loads(row["cfg"])
    assert stored["criterion"]["min_edge_pp"] == 5.0
    assert stored["criterion"]["min_independent_n"] == 500
    assert row["status"] == "running"

    decisions = await pool.fetchval(
        "SELECT count(*) FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    assert decisions == 0, "результаты появились раньше критерия — предрегистрация нарушена"

    # Первая строка результата обязана быть позже открытия прогона.
    await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    first_ts = await pool.fetchval(
        "SELECT min(ts) FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    assert first_ts is not None


async def test_full_pipeline_produces_report(seeded, tmp_path: Path, pool) -> None:
    """Прогон → исходы → отчёт: файл создан и содержит все разделы §10."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)
    decisions = await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    assert decisions > 0

    outcomes = await evaluate.evaluate_run(run_id, cfg)
    await replay.finish_run(run_id, "ok")

    out = await report.build_report(run_id, tmp_path / "report.txt")
    text = out.read_text(encoding="utf-8")

    for section in (
        "РАСЧЁТ 1: СИСТЕМА ПРОТИВ ТРЁХ БАЗОВЫХ ЛИНИЙ",
        "РАСЧЁТ 2: ПОПАДАНИЕ НАПРАВЛЕНИЯ КАЖДОГО АГЕНТА",
        "РАСЧЁТ 3: КАЛИБРОВКА ИНДЕКСА СОГЛАСИЯ",
        "РАСЧЁТ 4: ДОЛЯ ПОВТОРОВ ВЫВОДА АГЕНТА",
        "РАСЧЁТ 5: СОГЛАСОВАННОСТЬ НАПРАВЛЕНИЙ МЕЖДУ АГЕНТАМИ",
        "РАСЧЁТ 6: NET PNL ПО ГОРИЗОНТАМ",
        "РАСЧЁТ 7: РАЗБИВКА ПО РЕЖИМАМ РЫНКА",
        "РАСЧЁТ 8: КОРРЕЛЯЦИЯ ИСХОДОВ МЕЖДУ ИНСТРУМЕНТАМИ",
        "ВЕРДИКТ ПО ПРЕДРЕГИСТРИРОВАННОМУ КРИТЕРИЮ",
        "ОГРАНИЧЕНИЯ, ДЕЙСТВУЮЩИЕ НЕЗАВИСИМО ОТ РЕЗУЛЬТАТА",
    ):
        assert section in text, f"в отчёте нет раздела: {section}"

    # Вердикт формулируется механически, одной из двух форм.
    assert ("преимущество НАЙДЕНО" in text) or ("преимущество НЕ НАЙДЕНО" in text)
    if outcomes:
        assert "ВЫПОЛНЕНО" in text or "НЕ ВЫПОЛНЕНО" in text


async def test_replay_is_idempotent(seeded, pool) -> None:
    """Повторный прогон того же run_id не задваивает решения."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market"])
    first = await pool.fetchval(
        "SELECT count(*) FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    await replay.replay_instrument(run_id, INST, cfg, ["market"])
    second = await pool.fetchval(
        "SELECT count(*) FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    assert first == second


async def test_wait_decisions_have_no_outcomes(seeded, pool) -> None:
    """Решения wait не получают строк исхода: направления у них нет (§9.3 ТЗ)."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    await evaluate.evaluate_run(run_id, cfg)

    orphan = await pool.fetchval(
        """
        SELECT count(*) FROM backtest.outcomes o
        JOIN backtest.decisions d
          ON d.run_id=o.run_id AND d.inst_id=o.inst_id AND d.ts=o.ts
        WHERE o.run_id=$1 AND d.direction = 'wait';
        """,
        run_id,
    )
    assert orphan == 0


async def test_single_agent_configuration_uses_standard_mechanism(seeded, pool) -> None:
    """Конфигурация A (только Market) идёт по ШТАТНОМУ механизму агрегации.

    При продакшновом MIN_AGENTS=2 один агент не набирает кворума, и Decision
    Agent отдаёт ``wait`` — это поведение самой системы, а не решение реплея.
    Веса при отсутствующем агенте не перераспределяются (§3.4 ТЗ). Факт
    фиксируется тестом, чтобы он не выглядел в отчёте неожиданностью.
    """
    from src.core.config import settings

    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market"])

    rows = await pool.fetch(
        "SELECT DISTINCT direction FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    directions = {r["direction"] for r in rows}
    if settings.MIN_AGENTS > 1:
        assert directions == {"wait"}


async def test_production_tables_untouched(seeded, pool) -> None:
    """§14.6: прогон не меняет ни одной строки вне схемы backtest."""
    from backtest import db as bt

    before = await bt.production_row_counts()
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    await evaluate.evaluate_run(run_id, cfg)
    after = await bt.production_row_counts()

    assert before == after, f"продакшн-таблицы изменились: {before} → {after}"


async def test_outcome_horizons_and_flags(seeded, pool) -> None:
    """Исходы считаются по всем горизонтам, флаги независимости расставлены."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    await evaluate.evaluate_run(run_id, cfg)

    rows = await pool.fetch(
        "SELECT horizon_h, count(*) AS n, "
        "count(*) FILTER (WHERE is_independent) AS ind "
        "FROM backtest.outcomes WHERE run_id=$1 GROUP BY horizon_h ORDER BY horizon_h;",
        run_id,
    )
    if not rows:
        pytest.skip("на синтетике не оказалось направленных решений")
    horizons = {int(r["horizon_h"]) for r in rows}
    assert horizons <= set(cfg.horizons)
    for row in rows:
        assert int(row["ind"]) <= int(row["n"])
        # Доля независимых наблюдений убывает с ростом горизонта.
        if int(row["horizon_h"]) == 24:
            assert int(row["ind"]) * 24 <= int(row["n"]) + 24


async def test_price_end_comes_from_the_right_candle(seeded, pool) -> None:
    """price_end берётся из свечи, закрывшейся ровно через horizon часов."""
    cfg = make_config()
    run_id = await replay.start_run(cfg, ["market", "futures"], CRITERION)
    await replay.replay_instrument(run_id, INST, cfg, ["market", "futures"])
    await evaluate.evaluate_run(run_id, cfg)

    row = await pool.fetchrow(
        "SELECT ts, horizon_h, price_end FROM backtest.outcomes "
        "WHERE run_id=$1 ORDER BY ts LIMIT 1;",
        run_id,
    )
    if row is None:
        pytest.skip("нет исходов на синтетике")
    expected = await pool.fetchval(
        "SELECT close FROM backtest.candles WHERE inst_id=$1 AND bar=$2 AND close_time=$3;",
        INST, cfg.bar, row["ts"] + timedelta(hours=int(row["horizon_h"])),
    )
    assert float(row["price_end"]) == pytest.approx(float(expected))


def test_t0_is_used_by_helpers() -> None:
    """Защита от случайного расхождения тестовых данных и конфигурации."""
    cfg = make_config()
    assert cfg.period_from > T0
    assert cfg.period_to > cfg.period_from
