"""Точка входа исторического реплея (§11 ТЗ).

    python -m backtest.run --config backtest/.env.backtest

Этапы идут последовательно и идемпотентно: повторный запуск не перекачивает
уже загруженную историю и не задваивает решения.

    1. проверка схемы и снимок счётчиков продакшн-таблиц (§14.6);
    2. загрузка истории (§4) и контроль целостности (§4.3);
    3. ОБЯЗАТЕЛЬНАЯ сверка с живой системой (§13.2) — блокирующая;
    4. предрегистрация критерия и открытие прогона (§6);
    5. прогон конфигураций A и B (§3.4);
    6. расчёт исходов (§9.3–§9.6);
    7. отчёт (§10) и повторный снимок счётчиков продакшн-таблиц.

Шаг 3 блокирующий: если реплей не воспроизводит выводы Market живой системы,
он воспроизводит другую систему, и отчёт не строится вовсе. Пропустить шаг
можно только явным флагом ``--skip-parity``, и тогда это громко пишется в лог
и в отчёт — молчаливого обхода нет.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

from backtest import db, evaluate, integrity, loader, parity, replay, report
from backtest.config import BacktestConfig, load_config
from src.core.config import settings
from src.core.logging import setup_logging

_log = structlog.get_logger().bind(component="backtest.run")

# Предрегистрируемый критерий успеха (§6 ТЗ). Значения зашиты в код и попадают
# в backtest.runs.config_json ДО появления результатов. Менять их после
# просмотра результатов запрещено.
CRITERION = {
    "scope": "только проверочный отрезок (is_oos = true), независимые наблюдения",
    "min_edge_pp": 5.0,
    "min_independent_n": 500,
    "max_p_value": 0.05,
    "require_positive_net_pnl": True,
    "description": (
        "Преимущество найдено, если ОДНОВРЕМЕННО: (а) доля попаданий превышает "
        "лучшую тривиальную стратегию не менее чем на 5 п.п.; (б) N >= 500; "
        "(в) двусторонний биномиальный тест p < 0.05; (г) средний net_pnl_pct > 0."
    ),
    "variants_tested": (
        "перебор порогов, весов и горизонтов не проводился; проверяются ровно "
        "горизонты BT_HORIZONS в двух конфигурациях агентов"
    ),
}

# Конфигурации агентов (§3.4 ТЗ).
CONFIG_A = ["market"]
CONFIG_B = ["market", "futures"]


async def _load_history(cfg: BacktestConfig) -> None:
    """Загрузка истории и контроль целостности."""
    from src.core.exchange import create_exchange

    exchange = create_exchange(settings.EXCHANGE)
    client = loader.OkxHistory(exchange, cfg.request_pause_ms)
    try:
        for inst_id in cfg.instruments:
            await loader.backfill_candles(
                inst_id, cfg.bar, cfg.period_from, cfg.period_to, client=client
            )
            await loader.backfill_funding(
                inst_id, cfg.period_from, cfg.period_to, client=client
            )
    finally:
        await exchange.close()


async def _integrity(cfg: BacktestConfig) -> dict[str, list[tuple]]:
    """Проверяет непрерывность рядов, пишет разрывы, возвращает исключаемые окна."""
    excluded: dict[str, list[tuple]] = {}
    from datetime import timedelta

    for inst_id in cfg.instruments:
        candles = await integrity.check_continuity(
            inst_id, integrity.SERIES_CANDLES, cfg.period_from, cfg.period_to, bar=cfg.bar
        )
        await integrity.save_gaps(candles)
        funding = await integrity.check_continuity(
            inst_id, integrity.SERIES_FUNDING, cfg.period_from, cfg.period_to
        )
        await integrity.save_gaps(funding)
        _log.info(
            "Целостность рядов",
            inst_id=inst_id,
            candles_actual=candles.actual_n,
            candles_expected=candles.expected_n,
            candles_gaps=len(candles.gaps),
            funding_actual=funding.actual_n,
            funding_gaps=len(funding.gaps),
        )
        excluded[inst_id] = integrity.excluded_windows(
            candles.gaps,
            warmup_candles=settings.AGENT_MIN_CANDLES,
            step=timedelta(seconds=loader.bar_seconds(cfg.bar)),
        )
    return excluded


async def _parity_gate(cfg: BacktestConfig, skip: bool) -> dict:
    """Блокирующая сверка с живой системой (§13.2)."""
    if skip:
        _log.warning(
            "СВЕРКА С ПРОДАКШНОМ ПРОПУЩЕНА по флагу --skip-parity: "
            "результаты прогона НЕЛЬЗЯ публиковать"
        )
        return {"skipped": True, "blocking_ok": False}
    result = await parity.check_parity(cfg.instruments[0], cfg)
    payload = result.as_dict()
    payload["skipped"] = False
    if not result.blocking_ok:
        _log.error(
            "Сверка с продакшном НЕ ПРОЙДЕНА: реплей воспроизводит не ту систему",
            **{name: item.summary() for name, item in result.agents.items()},
        )
    return payload


async def _run(args: argparse.Namespace) -> int:
    setup_logging()
    cfg = load_config(Path(args.config))
    await db.connect()
    try:
        if not await db.schema_exists():
            _log.error(
                "Схема backtest отсутствует: примените db/migrations/008_backtest_schema.sql"
            )
            return 2
        if not db.using_backtest_role():
            _log.warning(
                "Подключение идёт продакшн-пользователем, а не выделенной ролью "
                "agenttrade_bt: задайте BT_DB_USER/BT_DB_PASSWORD"
            )

        before = await db.production_row_counts()
        _log.info("Счётчики продакшн-таблиц до прогона", **before)

        if not args.skip_load:
            await _load_history(cfg)
        excluded = await _integrity(cfg)

        parity_payload = await _parity_gate(cfg, args.skip_parity)
        if not parity_payload.get("blocking_ok") and not args.skip_parity:
            _log.error("Прогон остановлен: сверка §13.2 не пройдена")
            return 3

        criterion = dict(CRITERION)
        criterion["parity"] = parity_payload
        criterion["registered_at"] = datetime.now(UTC).isoformat()

        results: dict[str, int] = {}
        for name, agents in (("A", CONFIG_A), ("B", CONFIG_B)):
            run_id = await replay.start_run(cfg, agents, criterion)
            try:
                for inst_id in cfg.instruments:
                    await replay.replay_instrument(
                        run_id, inst_id, cfg, agents, excluded=excluded.get(inst_id)
                    )
                await evaluate.evaluate_run(run_id, cfg)
                await replay.finish_run(run_id, "ok")
            except Exception:
                await replay.finish_run(run_id, "failed")
                raise
            out = Path(args.out_dir) / (
                f"report_7_4_config{name}_{datetime.now(UTC):%Y%m%d}.txt"
            )
            await report.build_report(run_id, out)
            results[name] = run_id
            _log.info("Отчёт собран", config=name, run_id=run_id, path=str(out))

        after = await db.production_row_counts()
        _log.info("Счётчики продакшн-таблиц после прогона", **after)
        changed = {k: (before.get(k), after.get(k)) for k in after if before.get(k) != after.get(k)}
        if changed:
            _log.error("ПРОДАКШН-ТАБЛИЦЫ ИЗМЕНИЛИСЬ во время прогона", changed=changed)
            return 4
        _log.info("Продакшн-таблицы не изменились (счётчики строк совпали)")
        print(json.dumps({"runs": results, "production_unchanged": True}, ensure_ascii=False))
        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Исторический реплей ядра (Этап 7.4)")
    parser.add_argument("--config", default="backtest/.env.backtest")
    parser.add_argument("--out-dir", default="/opt/agent-trade/analysis_out")
    parser.add_argument("--skip-load", action="store_true",
                        help="не обращаться к бирже (история уже загружена)")
    parser.add_argument("--skip-parity", action="store_true",
                        help="пропустить блокирующую сверку §13.2 (результаты непубликуемы)")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
