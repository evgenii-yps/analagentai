#!/usr/bin/env python3
"""Воспроизведение расхождения контрольного варианта (Этап 8.10.1 §1).

ЗАЧЕМ ЭТОТ СКРИПТ СУЩЕСТВУЕТ. Разбор причины, изложенный словами, проверить
нельзя — его можно только принять на веру. Здесь причина ВОСПРОИЗВОДИТСЯ: тем же
кодом, что работает на сервере, на данных, где ответ известен заранее.

СЦЕНАРИЙ А — как расхождение возникало:
  1. сигнал, чей срок только что наступил, а последний бар окна ЕЩЁ ФОРМИРУЕТСЯ;
  2. расчёт 8.8 по СТАРОМУ правилу годности (``settle_seconds=0``) пишет исход
     ``timeout`` по закрытию «пока что»;
  3. коллектор дописывает бар (UPDATE close) — как он и делает раз в 15 секунд;
  4. расчёт 8.10 читает тот же бар уже закрытым и получает другой итог;
  5. сверка контрольного варианта показывает расхождение — ровно в одном поле.

СЦЕНАРИЙ Б — как исправление это прекращает: с запасом пара к расчёту не
допускается, пока её последний бар не закрылся, и оба расчёта дают одно число.

СКРИПТ ПИШЕТ В БАЗУ и поэтому ОТКАЗЫВАЕТСЯ работать с продакшн-подключением.
Он требует ``AT_REPRO_DSN`` — адрес ОДНОРАЗОВОЙ базы, и никогда не берёт DSN из
настроек. Причина простая: он заводит ненастоящий сигнал в ``signals``, а
поддельный сигнал в продакшне — это порча тех самых данных, ради честности
которых написан весь этап.

Запуск (внутри контейнера — на хосте нет asyncpg, правило D-3):

    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro \\
        -e AT_REPRO_DSN=postgresql://agenttrade:PASS@postgres:5432/agenttrade_test \\
        barrier python -m scripts.repro_8_10_1
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.barrier.outcomes import resolve  # noqa: E402
from src.barrier.runner import (  # noqa: E402
    build_row,  # noqa: E402
    pick_series,
    settle_seconds,
)
from src.barrier.runner import compute as barrier_compute  # noqa: E402
from src.core.db import db  # noqa: E402
from src.trailing.runner import compute as trailing_compute  # noqa: E402

PRICE = 60000.0
# Расхождение, наблюдённое на сервере 28.08.2026 (signal_id=47944, h=1).
SERVER_GAP_PCT = 0.001202
# Насколько правится закрытие бара, чтобы получить ровно это расхождение.
CLOSE_SHIFT = PRICE * SERVER_GAP_PCT / 100.0

LOGIC_VERSION = 5
SYMBOL = "REPRO8101/USDT"


async def _clean(instrument_id: int) -> None:
    """Убирает РОВНО свои строки — по инструменту, который сам и завёл."""
    ids = [
        r["id"] for r in await db.pool.fetch(
            "SELECT id FROM signals WHERE instrument_id = $1;", instrument_id
        )
    ]
    if not ids:
        return
    for table in ("trailing_outcomes", "signal_outcomes_barrier", "signal_targets"):
        await db.pool.execute(
            f"DELETE FROM {table} WHERE signal_id = ANY($1::bigint[]);", ids
        )
    await db.pool.execute("DELETE FROM signals WHERE id = ANY($1::bigint[]);", ids)


async def _instrument() -> int:
    return await db.pool.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', $1, 'REPRO', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE SET symbol = EXCLUDED.symbol "
        "RETURNING id;", SYMBOL,
    )


async def _make_pair(instrument_id: int, now: datetime) -> tuple[int, datetime]:
    """Сигнал со сроком «только что» и ровным минутным окном.

    Ряд ровный намеренно: тогда исход заведомо ``timeout``, и итог зависит
    ровно от одного числа — закрытия последнего бара.
    """
    signal_ts = now - timedelta(hours=1)
    signal_id = await db.pool.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, $2, 'buy', $3) RETURNING id;",
        instrument_id, signal_ts, LOGIC_VERSION,
    )
    await db.pool.execute(
        "INSERT INTO signal_targets (signal_id, horizon_h, direction, "
        "price_at_signal, target_pct, covers_fees, targets_version) "
        "VALUES ($1, 1, 'buy', $2, 5.0, true, 1);", signal_id, PRICE,
    )
    first = signal_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
    await db.pool.executemany(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, "
        "volume) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
        "ON CONFLICT (instrument_id, timeframe, ts) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
        "close=EXCLUDED.close;",
        [(instrument_id, "1m", first + timedelta(minutes=i),
          PRICE, PRICE, PRICE, PRICE, 1.0) for i in range(60)],
    )
    return signal_id, first + timedelta(minutes=59)


async def _finalize_bar(instrument_id: int, bar_ts: datetime) -> None:
    """Коллектор дописал формирующийся бар: закрытие сдвинулось."""
    await db.pool.execute(
        "UPDATE ohlcv SET close = close + $1 WHERE instrument_id = $2 "
        "AND timeframe = '1m' AND ts = $3;", CLOSE_SHIFT, instrument_id, bar_ts,
    )


async def _row(signal_id: int, table: str, extra: str = ""):
    return await db.pool.fetchrow(
        f"SELECT * FROM {table} WHERE signal_id = $1 {extra};", signal_id
    )


async def main() -> int:
    dsn = os.environ.get("AT_REPRO_DSN", "")
    if not dsn:
        print("AT_REPRO_DSN не задан. Скрипт пишет в базу и на продакшне не работает:")
        print("укажите адрес ОДНОРАЗОВОЙ базы, например")
        print("  AT_REPRO_DSN=postgresql://agenttrade:PASS@postgres:5432/agenttrade_test")
        return 2

    db._pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    now = datetime.now(UTC).replace(microsecond=0)
    settle = settle_seconds()
    try:
        instrument_id = await _instrument()
        await _clean(instrument_id)
        print(f"одноразовая база, инструмент {SYMBOL} = {instrument_id}, "
              f"запас правила = {settle} с")

        # ---------------- СЦЕНАРИЙ А ----------------------------------------
        print("\n=== А. СТАРОЕ ПРАВИЛО (запас 0): пара считается по формирующемуся бару")
        signal_id, last_bar = await _make_pair(instrument_id, now)
        print(f"    сигнал {signal_id}, последний бар окна {last_bar:%H:%M}, "
              f"закрывается в {last_bar + timedelta(minutes=1):%H:%M}, "
              f"сейчас {now:%H:%M:%S}")

        candidates = await db.get_barrier_candidates(
            logic_version=LOGIC_VERSION, horizon_h=1, now=now, settle_seconds=0
        )
        mine = [c for c in candidates if c["id"] == signal_id]
        print(f"    кандидатов по старому правилу: {len(mine)} (ожидается 1)")

        candidate = mine[0]
        bars, resolution = await pick_series(
            instrument_id=instrument_id, signal_ts=candidate["ts"], horizon_h=1
        )
        outcome = resolve(
            bars, signal_ts=candidate["ts"], horizon_h=1,
            price_at_signal=float(candidate["price_at_signal"]),
            target_pct=float(candidate["target_pct"]), stop_pct=1.0, cost_pct=0.22,
            direction=candidate["direction"], resolution=resolution,
        )
        await db.save_barrier_outcome(build_row(
            candidate, outcome, horizon_h=1, stop_pct=1.0, cost_pct=0.22,
            computed_at=now,
        ))
        before = await _row(signal_id, "signal_outcomes_barrier")
        print(f"    8.8 записал: outcome={before['outcome']} "
              f"net_pnl_pct={before['net_pnl_pct']}")

        await _finalize_bar(instrument_id, last_bar)
        print(f"    коллектор дописал бар: закрытие +{CLOSE_SHIFT:.4f}")

        await trailing_compute(now=now + timedelta(hours=2))
        after = await _row(signal_id, "trailing_outcomes",
                           "AND activation_ratio = 0 AND retrace_ratio = 0")
        print(f"    8.10 пересчитал: exit_reason={after['exit_reason']} "
              f"net_pnl_pct={after['net_pnl_pct']}")
        gap = float(after["net_pnl_pct"]) - float(before["net_pnl_pct"])
        print(f"    РАЗНИЦА: {gap:+.6f} (на сервере {SERVER_GAP_PCT:+.6f})")
        same = all([
            after["exit_reason"] == before["outcome"],
            after["hit_at"] == before["hit_at"],
            after["bars_to_hit"] == before["bars_to_hit"],
            after["mae_pct"] == before["mae_pct"],
            after["mfe_pct"] == before["mfe_pct"],
            after["resolution"] == before["resolution"],
            after["price_at_signal"] == before["price_at_signal"],
            after["cost_pct"] == before["cost_pct"],
        ])
        print(f"    остальные поля совпали: {same} (на сервере — да)")
        print(f"    сверка: {await db.check_trailing_control(logic_version=LOGIC_VERSION)}")

        await _clean(instrument_id)

        # ---------------- СЦЕНАРИЙ Б ----------------------------------------
        print(f"\n=== Б. НОВОЕ ПРАВИЛО (запас {settle} с): пара ждёт закрытия бара")
        signal_id, last_bar = await _make_pair(instrument_id, now)
        await barrier_compute(now=now)
        written = await _row(signal_id, "signal_outcomes_barrier")
        print(f"    8.8 при сроке «только что»: строка есть? {written is not None} "
              f"(ожидается False)")

        await _finalize_bar(instrument_id, last_bar)
        print(f"    коллектор дописал бар: закрытие +{CLOSE_SHIFT:.4f}")

        later = now + timedelta(hours=2)
        await barrier_compute(now=later)
        settled = await _row(signal_id, "signal_outcomes_barrier")
        print(f"    8.8 через 2 часа: outcome={settled['outcome']} "
              f"net_pnl_pct={settled['net_pnl_pct']}")
        await trailing_compute(now=later)
        control_row = await _row(signal_id, "trailing_outcomes",
                                 "AND activation_ratio = 0 AND retrace_ratio = 0")
        print(f"    8.10 следом:      exit_reason={control_row['exit_reason']} "
              f"net_pnl_pct={control_row['net_pnl_pct']}")
        print(f"    сверка: {await db.check_trailing_control(logic_version=LOGIC_VERSION)}")

        await _clean(instrument_id)
        print("\nсвои строки убраны, база возвращена в исходное состояние")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
