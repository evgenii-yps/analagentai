"""Суточный пересчёт целей по вероятности (§4, §7 ТЗ 8.2).

ЧТО ДЕЛАЕТ ПЕРЕСЧЁТ, по шагам:

  1. догружает СВЕЖИЙ КРАЙ часовых свечей спота (от последней свечи до «сейчас»)
     — без этого ряд к 03:40 UTC отстаёт на сутки и предпроверка его забракует;
  2. читает окно последних 90 суток из ``backtest.candles``;
  3. выполняет предпроверку §1 по каждому инструменту;
  4. считает MFE и 40-й процентиль по каждой паре (горизонт × направление);
  5. пишет НОВЫЕ строки ``risk_targets`` с новым ``computed_at``.

ЧЕГО ПЕРЕСЧЁТ НЕ ДЕЛАЕТ. Он не трогает ``signal_targets`` — ни одной строкой,
ни при каких условиях. Уже выданная человеку цель неизменна: без этого нельзя
проверить систему постфактум.

ЧАСТИЧНЫЙ ЗАПУСК РАЗРЕШЁН (§1 ТЗ): инструмент, не прошедший предпроверку,
получает строку с ``target_pct = NULL`` и причиной ``data_gap``, а остальные
обслуживаются как обычно. Молчание вместо строки было бы неотличимо от
«пересчёт не запускался».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.core.config import settings
from src.core.db import db
from src.risk.quality import SeriesCheck, check_series
from src.risk.targets import (
    BUY,
    REASON_DATA_GAP,
    SELL,
    Candle,
    MfeSample,
    TargetResult,
    compute_target,
    mfe_sample,
)

_log = structlog.get_logger().bind(component="risk_targets")

# Направления считаются РАЗДЕЛЬНО (§4.8): падения быстрее ростов, и общее
# распределение усреднило бы разные величины.
DIRECTIONS = (BUY, SELL)

SOURCE = "backtest.candles"


def bt_inst_id(symbol: str) -> str:
    """Имя спота в ``backtest.candles`` по символу инструмента системы.

    ``BTC/USDT`` → ``BTC-USDT``. Хвост бессрочного контракта (``:USDT``)
    отбрасывается: цели считаются по СПОТУ — на нём торгует человек и на нём
    же работает Market Agent. Контрактов эта функция не касается вовсе.
    """
    spot = symbol.split(":", 1)[0]
    return spot.replace("/", "-").upper()


@dataclass
class InstrumentOutcome:
    """Итог пересчёта по одному инструменту — для журнала и отчёта."""

    symbol: str
    inst_id: str
    instrument_id: int | None
    check: SeriesCheck | None
    rows_written: int
    ok: bool
    reason: str | None = None


def _candles(rows: list[dict[str, Any]]) -> list[Candle]:
    """Строки БД → свечи расчёта. NUMERIC приходит Decimal, приводим к float."""
    return [
        Candle(
            open_time=r["open_time"],
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )
        for r in rows
    ]


def _data_gap_row(
    *,
    instrument_id: int,
    horizon_h: int,
    direction: str,
    computed_at: datetime,
    window_from: datetime,
    window_to: datetime,
    check: SeriesCheck | None,
) -> dict[str, Any]:
    """Строка «цель не рассчитана из-за состояния данных» (§7).

    Пишется ИМЕННО СТРОКА, а не пропуск: отсутствие строки нельзя отличить от
    того, что пересчёт вообще не запускался, — а это разные состояния системы.
    """
    return {
        "instrument_id": instrument_id,
        "horizon_h": horizon_h,
        "direction": direction,
        "computed_at": computed_at,
        "window_days": settings.RISK_WINDOW_DAYS,
        "data_from": (check.first_open_time if check and check.first_open_time
                      else window_from),
        "data_to": (check.last_open_time if check and check.last_open_time
                    else window_to),
        "n_observations": 0,
        "target_pct": None,
        "hit_rate": None,
        "mfe_p25": None,
        "mfe_p50": None,
        "mfe_p75": None,
        "cost_roundtrip_pct": settings.RISK_COST_ROUNDTRIP_PCT,
        "covers_fees": False,
        "no_target_reason": REASON_DATA_GAP,
        "source": SOURCE,
        "targets_version": settings.RISK_TARGETS_VERSION,
    }


def build_rows(
    *,
    instrument_id: int,
    candles: list[Candle],
    horizons: list[int],
    computed_at: datetime,
    window_from: datetime,
    window_to: datetime,
) -> tuple[list[dict[str, Any]], dict[tuple[int, str], TargetResult]]:
    """Строки risk_targets по всем парам (горизонт × направление).

    Возвращает и сами строки, и результаты расчёта — вторые нужны журналу и
    отчёту (число выброшенных из-за разрывов наблюдений, §4.3).
    """
    rows: list[dict[str, Any]] = []
    results: dict[tuple[int, str], TargetResult] = {}
    for horizon_h in horizons:
        for direction in DIRECTIONS:
            sample: MfeSample = mfe_sample(candles, horizon_h, direction)
            result = compute_target(
                sample,
                cost_roundtrip_pct=settings.RISK_COST_ROUNDTRIP_PCT,
                min_observations=settings.RISK_MIN_OBSERVATIONS,
            )
            results[(horizon_h, direction)] = result
            rows.append({
                "instrument_id": instrument_id,
                "horizon_h": horizon_h,
                "direction": direction,
                "computed_at": computed_at,
                "window_days": settings.RISK_WINDOW_DAYS,
                "data_from": candles[0].open_time if candles else window_from,
                "data_to": candles[-1].open_time if candles else window_to,
                "n_observations": result.n_observations,
                "target_pct": result.target_pct,
                "hit_rate": result.hit_rate,
                "mfe_p25": result.mfe_p25,
                "mfe_p50": result.mfe_p50,
                "mfe_p75": result.mfe_p75,
                "cost_roundtrip_pct": settings.RISK_COST_ROUNDTRIP_PCT,
                "covers_fees": result.covers_fees,
                "no_target_reason": result.no_target_reason,
                "source": SOURCE,
                "targets_version": settings.RISK_TARGETS_VERSION,
            })
    return rows, results


async def backfill_fresh_edge(inst_ids: list[str], *, now: datetime) -> dict[str, Any]:
    """Догрузка свежего края свечей перед расчётом (§7).

    Загрузчик живёт в пакете ``backtest`` (backtest/loader.py) и переписан не
    был: он идемпотентен, пагинация идёт двумя проходами, вставка — с
    ``ON CONFLICT DO NOTHING``. Повторный запуск ничего не перекачивает заново.

    ОТКАЗ ЗАГРУЗКИ НЕ ОСТАНАВЛИВАЕТ ПЕРЕСЧЁТ. Если биржа недоступна или пакет
    загрузчика отсутствует в образе, расчёт продолжается на том, что есть, —
    и предпроверка §1 сама забракует устаревший ряд по возрасту последней
    свечи. Это лучше и честнее, чем считать цель по вчерашним данным молча.
    """
    outcome: dict[str, Any] = {"attempted": False, "inserted": 0, "errors": []}
    if not settings.RISK_BACKFILL_ENABLED:
        _log.info("risk_targets_backfill_skipped=1", reason="disabled_by_config")
        return outcome
    try:
        from backtest import db as bt_db
        from backtest.loader import OkxHistory, backfill_candles, create_http_client
    except ImportError as exc:  # noqa: BLE001 — образ без пакета backtest
        _log.warning(
            "risk_targets_backfill_unavailable=1", error=str(exc),
            detail="пакет backtest отсутствует в образе; ряд не пополняется",
        )
        outcome["errors"].append(f"import: {exc}")
        return outcome

    outcome["attempted"] = True
    since = now - timedelta(
        days=settings.RISK_WINDOW_DAYS + settings.RISK_BACKFILL_MARGIN_DAYS
    )
    await bt_db.connect()
    client = create_http_client()
    try:
        history = OkxHistory(client, pause_ms=200)
        for inst_id in inst_ids:
            try:
                inserted = await backfill_candles(
                    inst_id, settings.RISK_BAR, since, now, client=history
                )
                outcome["inserted"] += int(inserted or 0)
                _log.info(
                    "risk_targets_backfill_instrument=1",
                    inst_id=inst_id, inserted=int(inserted or 0),
                )
            except Exception as exc:  # noqa: BLE001 — один инструмент не рушит все
                outcome["errors"].append(f"{inst_id}: {exc}")
                _log.warning(
                    "risk_targets_backfill_failed=1", inst_id=inst_id, error=str(exc)
                )
    finally:
        await client.aclose()
        await bt_db.close()
    return outcome


async def recompute(now: datetime | None = None) -> list[InstrumentOutcome]:
    """Полный пересчёт целей по всем инструментам. Возвращает итог по каждому."""
    now = now or datetime.now(UTC)
    computed_at = now
    horizons = settings.eval_horizons_hours
    pairs = settings.symbol_pairs
    # Границы считаются от НАЧАЛА ТЕКУЩЕГО ЧАСА, а не от «сейчас»: свечи лежат
    # на границах часов, и окно, начатое в 20:02, теряло бы первую свечу — ряд
    # из 90 суток оказывался бы на час короче порога 2160 и не проходил
    # предпроверку НИКОГДА. Ошибка выглядела бы как нехватка данных.
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    window_from = hour_now - timedelta(days=settings.RISK_WINDOW_DAYS)
    # Читаем с запасом (§2 ТЗ: 95 суток вместо 90). Запас участвует в
    # ПРЕДПРОВЕРКЕ непрерывности, но НЕ в выборке: выборка — ровно последние
    # 90 суток (§4.3), иначе окно расчёта не совпадало бы с обещанным человеку.
    read_from = hour_now - timedelta(
        days=settings.RISK_WINDOW_DAYS + settings.RISK_BACKFILL_MARGIN_DAYS
    )

    await db.ensure_risk_targets_schema()

    inst_ids = [bt_inst_id(pair.spot) for pair in pairs]
    backfill = await backfill_fresh_edge(inst_ids, now=now)
    _log.info(
        "risk_targets_backfill_done=1",
        attempted=backfill["attempted"],
        inserted=backfill["inserted"],
        errors=len(backfill["errors"]),
    )

    outcomes: list[InstrumentOutcome] = []
    for pair in pairs:
        inst_id = bt_inst_id(pair.spot)
        instrument_id = await db.get_instrument_id(pair.spot)
        if instrument_id is None:
            # Инструмента нет в справочнике: писать строку не во что (внешний
            # ключ), поэтому единственное честное действие — сказать об этом.
            _log.warning(
                "risk_targets_instrument_missing=1", symbol=pair.spot, inst_id=inst_id
            )
            outcomes.append(InstrumentOutcome(
                symbol=pair.spot, inst_id=inst_id, instrument_id=None,
                check=None, rows_written=0, ok=False, reason="instrument_missing",
            ))
            continue

        rows_db = await db.get_backtest_candles(
            inst_id, settings.RISK_BAR, read_from
        )
        loaded = _candles(rows_db)
        # Выборка — ровно окно §4.3; предпроверка §1 смотрит на весь
        # прочитанный ряд, включая запас: непрерывность 2160 часов проверяется
        # там, где она может быть, а не на обрезанном куске.
        candles = [c for c in loaded if c.open_time >= window_from]
        check = check_series(
            loaded,
            now=now,
            min_run_hours=settings.RISK_MIN_RUN_HOURS,
            max_age_hours=settings.RISK_MAX_AGE_HOURS,
            max_flat_pct=settings.RISK_MAX_FLAT_PCT,
        )
        _log.info(
            "risk_targets_precheck=1", inst_id=inst_id,
            symbol=pair.spot, **check.as_dict(),
        )

        if not check.ok:
            rows = [
                _data_gap_row(
                    instrument_id=instrument_id, horizon_h=h, direction=d,
                    computed_at=computed_at, window_from=window_from,
                    window_to=now, check=check,
                )
                for h in horizons for d in DIRECTIONS
            ]
        else:
            rows, results = build_rows(
                instrument_id=instrument_id,
                candles=candles,
                horizons=horizons,
                computed_at=computed_at,
                window_from=window_from,
                window_to=now,
            )
            for (horizon_h, direction), result in results.items():
                _log.info(
                    "risk_targets_computed=1",
                    inst_id=inst_id, horizon_h=horizon_h, direction=direction,
                    n=result.n_observations,
                    target_pct=(None if result.target_pct is None
                                else round(result.target_pct, 5)),
                    hit_rate=(None if result.hit_rate is None
                              else round(result.hit_rate, 5)),
                    covers_fees=result.covers_fees,
                    no_target_reason=result.no_target_reason,
                    skipped_gap=result.skipped_gap,
                )

        for row in rows:
            await db.save_risk_target(row)
        outcomes.append(InstrumentOutcome(
            symbol=pair.spot, inst_id=inst_id, instrument_id=instrument_id,
            check=check, rows_written=len(rows), ok=check.ok,
            reason=None if check.ok else ",".join(check.failures),
        ))

    ok_n = sum(1 for o in outcomes if o.ok)
    _log.info(
        "risk_targets_recompute_done=1",
        instruments_ok=ok_n,
        instruments_failed=len(outcomes) - ok_n,
        rows=sum(o.rows_written for o in outcomes),
        computed_at=computed_at.isoformat(),
    )
    return outcomes


async def run() -> list[InstrumentOutcome]:
    """Точка входа сценария: своё подключение к БД, своё закрытие."""
    await db.connect()
    try:
        return await recompute()
    finally:
        await db.close()
