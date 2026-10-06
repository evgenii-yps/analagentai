"""Этап 9.5.3, редакция 1: позиции до запуска демо не попадают в лист владельца."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from src.export import demo_owner_sheet as dos
from tests.demo_support import (
    add_position,
    build_database,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    reset_data,
    server_up,
    set_state,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
needs_db = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("ownerbefore")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


async def skipped_buy(pool, pid: int, reason: str) -> None:
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, 'buy', 'skipped', $2, $3, "
        "'BTC/USDT', 'h', now());", pid, reason, f"at95b{pid}")


def marked_ids(payload: dos.OwnerSheetPayload) -> list[int | None]:
    """Номера позиций по заметкам T (у ордера в пути метки нет — None)."""
    out: list[int | None] = []
    for (text,) in payload.blocks["T"]:
        match = re.search(r"\[поз\. (\d+)\]", text)
        out.append(int(match.group(1)) if match else None)
    return out


@needs_db
async def test_before_start_positions_never_reach_the_sheet(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    for n in range(5):
        pid = await add_position(pool, opened_at=utc(1, 8) + timedelta(minutes=n))
        await skipped_buy(pool, pid, "before_start")
    payload = await dos.build_owner_rows(pool, NOW, "Europe/Moscow")
    assert payload.total_positions == 0 and payload.dropped == 0
    assert all(rows == [] for rows in payload.blocks.values())
    assert payload.last_written_row == 5                     # данных нет: пишется одна A3
    writes, clears = dos.to_requests(payload)
    assert [w["range"] for w in writes] == ["A3"]
    assert clears[0]["range"] == "A6:D1000"                  # прежние строки уйдут при очистке


@needs_db
async def test_the_other_skips_stay_in_the_sheet(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    ids = {}
    for n, reason in enumerate(("no_capital", "no_balance", "stale")):
        ids[reason] = await add_position(pool, opened_at=utc(5, 8) + timedelta(minutes=n))
        await skipped_buy(pool, ids[reason], reason)
    payload = await dos.build_owner_rows(pool, NOW, "Europe/Moscow")
    assert marked_ids(payload) == [ids["no_capital"], ids["no_balance"], ids["stale"]]
    assert [row[3] for row in payload.blocks["A:D"]] == ["пропущено"] * 3
    assert payload.total_positions == 3


@needs_db
async def test_rows_after_the_exclusion_are_contiguous_from_row_6(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    keep: list[int] = []
    # before_start вперемешку с остальными, в том числе МЕЖДУ ними по времени
    plan = [("before_start", utc(1, 8)), ("filled", utc(5, 8)), ("before_start", utc(5, 9)),
            ("stale", utc(5, 10)), ("before_start", utc(5, 11)), ("no_capital", utc(5, 12)),
            ("before_start", utc(6, 8))]
    for kind, opened in plan:
        if kind == "filled":
            pid = await add_position(pool, opened_at=opened, status="closed",
                                     closed_at=opened + timedelta(hours=1))
            await insert_buy_filled(pool, pid, filled_at=opened)
            await insert_sell_filled(pool, pid, filled_at=opened + timedelta(hours=1))
            keep.append(pid)
        else:
            pid = await add_position(pool, opened_at=opened)
            await skipped_buy(pool, pid, kind)
            if kind != "before_start":
                keep.append(pid)
    payload = await dos.build_owner_rows(pool, NOW, "Europe/Moscow")
    assert marked_ids(payload) == keep                       # порядок прежний, исключённых нет
    n = len(keep)
    assert payload.first_row == 6 and payload.last_written_row == 5 + n
    assert payload.total_positions == n
    # ни одной пустой строки внутри блоков (у пропущенных F–K и M пусты по спецификации, поэтому
    # проверяются A:D и T) и диапазоны записи идут подряд с 6-й
    assert all(len(rows) == n for rows in payload.blocks.values())
    assert all(all(cell != "" for cell in row) for row in payload.blocks["A:D"])
    assert all(row[0] != "" for row in payload.blocks["T"])
    writes, clears = dos.to_requests(payload)
    assert [w["range"] for w in writes[1:]] == [
        f"A6:D{5 + n}", f"F6:K{5 + n}", f"M6:M{5 + n}", f"T6:T{5 + n}"]
    assert [c["range"] for c in clears][0] == f"A{6 + n}:D1000"
