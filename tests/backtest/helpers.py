"""Общие данные и помощники тестов реплея (Этап 7.4).

Тесты, которым нужна БД, включаются переменной окружения ``BT_TEST_DSN``:

    BT_TEST_DSN=postgresql://postgres@127.0.0.1:5433/bt_test python -m pytest tests/backtest

Без неё такие тесты ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они
не выполнялись. Тесты чистых функций (издержки, независимые окна, целостность)
работают всегда и БД не требуют.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backtest.config import BacktestConfig

TEST_DSN = os.environ.get("BT_TEST_DSN", "").strip()

requires_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте BT_TEST_DSN "
        "(например postgresql://postgres@127.0.0.1:5433/bt_test)"
    ),
)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
INST = "TEST-USDT-SWAP"


def make_config(**overrides) -> BacktestConfig:
    """Конфигурация прогона для тестов (значения подобраны под короткий ряд)."""
    base = {
        "instruments": (INST,),
        "bar": "1H",
        "period_from": T0 + timedelta(days=20),
        "period_to": T0 + timedelta(days=30),
        "step_hours": 1,
        "horizons": (1, 4, 12, 24),
        "fee_roundtrip_pct": Decimal("0.10"),
        "slippage_pct": Decimal("0.01"),
        "oos_months": 0,
        "request_pause_ms": 200,
    }
    base.update(overrides)
    # oos_months=0 запрещён валидацией, но в тестах граница задаётся напрямую.
    if base["oos_months"] == 0:
        base["oos_months"] = 1
    return BacktestConfig(**base)


async def seed_candles(
    pool,
    inst_id: str = INST,
    bar: str = "1H",
    start: datetime | None = None,
    hours: int = 24 * 40,
    base_price: float = 100.0,
) -> None:
    """Заливает непрерывный ряд часовых свечей с детерминированной формой."""
    import math

    start = start or T0
    rows = []
    for i in range(hours):
        open_time = start + timedelta(hours=i)
        price = base_price + 5.0 * math.sin(i / 7.0) + 0.02 * i
        rows.append(
            (
                inst_id, bar, open_time, open_time + timedelta(hours=1),
                Decimal(f"{price:.8f}"), Decimal(f"{price + 1:.8f}"),
                Decimal(f"{price - 1:.8f}"), Decimal(f"{price:.8f}"),
                Decimal("10"), Decimal("1000"),
            )
        )
    await pool.executemany(
        """
        INSERT INTO backtest.candles
            (inst_id, bar, open_time, close_time, open, high, low, close, volume, volume_ccy)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT DO NOTHING;
        """,
        rows,
    )


async def seed_funding(
    pool,
    inst_id: str = INST,
    start: datetime | None = None,
    points: int = 120,
    step_hours: int = 8,
) -> None:
    """Заливает ставки финансирования с шагом ``step_hours`` (как отдаёт биржа)."""
    import math

    start = start or T0
    rows = [
        (
            inst_id,
            start + timedelta(hours=step_hours * i),
            Decimal(f"{0.00005 + 0.00003 * math.sin(i / 4.0):.10f}"),
        )
        for i in range(points)
    ]
    await pool.executemany(
        "INSERT INTO backtest.funding (inst_id, funding_time, funding_rate) "
        "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING;",
        rows,
    )
