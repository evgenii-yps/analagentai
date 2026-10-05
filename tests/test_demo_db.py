"""Этап 9.5, §9 и §12 п. 12: миграция 030 и ограничения demo_orders на настоящей PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from tests.demo_support import (
    ROOT,
    add_position,
    build_database,
    drop_database,
    open_pool,
    psql_raw,
    reset_data,
    server_up,
)

pytestmark = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")

MIGRATION = ROOT / "db" / "migrations" / "030_demo_orders.sql"
ROLLBACK = ROOT / "db" / "migrations" / "030_demo_orders_rollback.sql"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_name():
    name = build_database("demodb")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


def apply(db: str, path) -> None:
    psql_raw(db, stdin=path.read_text(encoding="utf-8"))


async def insert(pool, position_id: int, leg: str = "buy", status: str = "filled",
                 skip_reason: str | None = None, cl: str | None = None) -> None:
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) "
        "VALUES ($1, 1, $2, $3, $4, $5, 'BTC/USDT', 'www.okx.com', $6);",
        position_id, leg, status, skip_reason, cl or f"at95{leg[0]}{position_id}", NOW,
    )


# --- миграция ---------------------------------------------------------------


def test_the_migration_is_idempotent(db_name) -> None:
    apply(db_name, MIGRATION)
    apply(db_name, MIGRATION)
    assert psql_raw(db_name, "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name IN ('demo_orders', 'demo_state');") == "2"


def test_key_types_match_the_real_ddl(db_name) -> None:
    """positions.id — BIGINT, instruments.id — INT: ключи демо-таблиц того же типа."""
    types = dict(line.split("|") for line in psql_raw(
        db_name,
        "SELECT table_name || '.' || column_name || '|' || data_type "
        "FROM information_schema.columns WHERE table_schema = 'public' AND "
        "((table_name = 'positions' AND column_name = 'id') OR "
        " (table_name = 'instruments' AND column_name = 'id') OR "
        " (table_name = 'demo_orders' AND column_name IN ('position_id', 'instrument_id')));",
    ).splitlines())
    assert types["positions.id"] == "bigint" and types["instruments.id"] == "integer"
    assert types["demo_orders.position_id"] == "bigint"
    assert types["demo_orders.instrument_id"] == "integer"


def test_demo_orders_columns_follow_the_spec(db_name) -> None:
    got = {
        line.split("|")[0]: line.split("|")[1:]
        for line in psql_raw(
            db_name,
            "SELECT column_name || '|' || data_type || '|' || "
            "coalesce(numeric_precision::text, '') "
            "|| '|' || coalesce(numeric_scale::text, '') || '|' || is_nullable "
            "FROM information_schema.columns WHERE table_name = 'demo_orders';",
        ).splitlines()
    }
    expected = {
        "id": ("bigint", "NO"), "position_id": ("bigint", "NO"), "instrument_id": ("integer", "NO"),
        "leg": ("text", "NO"), "status": ("text", "NO"), "skip_reason": ("text", "YES"),
        "cl_ord_id": ("text", "NO"), "exchange_order_id": ("text", "YES"),
        "symbol": ("text", "NO"), "host": ("text", "NO"),
        "requested_cost_usd": ("numeric", "YES"), "requested_qty": ("numeric", "YES"),
        "filled_qty": ("numeric", "YES"), "avg_price": ("numeric", "YES"),
        "cost_usd": ("numeric", "YES"), "fee": ("numeric", "YES"), "fee_ccy": ("text", "YES"),
        "virtual_price": ("numeric", "YES"), "virtual_ts": ("timestamp with time zone", "NO"),
        "slippage_pct": ("numeric", "YES"), "sent_at": ("timestamp with time zone", "YES"),
        "filled_at": ("timestamp with time zone", "YES"), "lag_sec": ("integer", "YES"),
        "error_code": ("text", "YES"), "error_text": ("text", "YES"),
        "created_at": ("timestamp with time zone", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
    }
    assert set(got) == set(expected)
    for name, (kind, nullable) in expected.items():
        assert got[name][0] == kind and got[name][3] == nullable, name
    precision = {"requested_cost_usd": ("14", "6"), "requested_qty": ("28", "12"),
                 "filled_qty": ("28", "12"), "avg_price": ("20", "8"), "cost_usd": ("14", "6"),
                 "fee": ("28", "12"), "virtual_price": ("20", "8"), "slippage_pct": ("12", "6")}
    for name, (prec, scale) in precision.items():
        assert (got[name][1], got[name][2]) == (prec, scale), name


def test_indexes_exist(db_name) -> None:
    names = set(psql_raw(db_name, "SELECT indexname FROM pg_indexes "
                         "WHERE tablename = 'demo_orders';").splitlines())
    assert {"demo_orders_status_idx", "demo_orders_created_idx"} <= names


def test_the_migration_has_no_effect_on_other_tables(db_name) -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    import re

    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    assert not re.search(r"\bALTER\s+TABLE\b", code, re.I)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", code, re.I)
    assert code.strip().startswith("BEGIN;") and code.strip().endswith("COMMIT;")


# --- ограничения ------------------------------------------------------------


async def test_a_valid_row_and_a_skipped_row_are_accepted(pool) -> None:
    a = await add_position(pool, opened_at=NOW)
    b = await add_position(pool, opened_at=NOW)
    await insert(pool, a, "buy", "filled")
    await insert(pool, b, "buy", "skipped", skip_reason="stale")
    await insert(pool, a, "sell", "dust", skip_reason="below_min_size")
    assert await pool.fetchval("SELECT count(*) FROM demo_orders;") == 3


async def test_skip_reason_is_required_exactly_for_skipped_and_dust(pool) -> None:
    p = await add_position(pool, opened_at=NOW)
    with pytest.raises(asyncpg.CheckViolationError):
        await insert(pool, p, "buy", "skipped", skip_reason=None)
    with pytest.raises(asyncpg.CheckViolationError):
        await insert(pool, p, "sell", "dust", skip_reason=None)
    for status in ("pending", "sent", "filled", "rejected", "lost"):
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(pool, p, "buy", status, skip_reason="stale", cl=f"x{status}")


async def test_leg_and_status_are_closed_lists(pool) -> None:
    p = await add_position(pool, opened_at=NOW)
    with pytest.raises(asyncpg.CheckViolationError):
        await insert(pool, p, "hold", "filled", cl="a1")
    with pytest.raises(asyncpg.CheckViolationError):
        await insert(pool, p, "buy", "cancelled", cl="a2")
    for status in ("pending", "sent", "filled", "rejected", "lost"):
        q = await add_position(pool, opened_at=NOW)
        await insert(pool, q, "buy", status)


async def test_one_row_per_position_and_leg(pool) -> None:
    p = await add_position(pool, opened_at=NOW)
    await insert(pool, p, "buy", "filled")
    with pytest.raises(asyncpg.UniqueViolationError):
        await insert(pool, p, "buy", "filled", cl="other-id")
    await insert(pool, p, "sell", "filled")      # другая нога — можно


async def test_cl_ord_id_is_unique(pool) -> None:
    a = await add_position(pool, opened_at=NOW)
    b = await add_position(pool, opened_at=NOW)
    await insert(pool, a, "buy", "filled", cl="same")
    with pytest.raises(asyncpg.UniqueViolationError):
        await insert(pool, b, "buy", "filled", cl="same")


async def test_foreign_keys_hold(pool) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await insert(pool, 987654, "buy", "filled")
    p = await add_position(pool, opened_at=NOW)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
            "symbol, host, virtual_ts) VALUES ($1, 999, 'buy', 'filled', 'z', 'BTC/USDT', "
            "'www.okx.com', $2);", p, NOW)


async def test_required_columns_are_not_null(pool) -> None:
    p = await add_position(pool, opened_at=NOW)
    for column in ("symbol", "host", "virtual_ts", "cl_ord_id"):
        cols = {"symbol": "'S'", "host": "'h'", "virtual_ts": "now()", "cl_ord_id": "'c'"}
        cols[column] = "NULL"
        with pytest.raises(asyncpg.NotNullViolationError):
            await pool.execute(
                "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
                f"symbol, host, virtual_ts) VALUES ({p}, 1, 'buy', 'filled', {cols['cl_ord_id']}, "
                f"{cols['symbol']}, {cols['host']}, {cols['virtual_ts']});")


async def test_demo_state_holds_exactly_one_row(pool) -> None:
    sql = "INSERT INTO demo_state (id, mirror_since, host) VALUES ({}, {}, 'h');"
    await pool.execute(sql.format(1, "now()"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(sql.format(1, "now()"))
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(sql.format(2, "now()"))
    await pool.execute("DELETE FROM demo_state;")
    with pytest.raises(asyncpg.NotNullViolationError):
        await pool.execute(sql.format(1, "NULL"))


async def test_numeric_precision_is_enough_for_a_two_dollar_btc_order(pool) -> None:
    from decimal import Decimal

    p = await add_position(pool, opened_at=NOW)
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, symbol, "
        "host, virtual_ts, filled_qty, avg_price, cost_usd, fee, slippage_pct) "
        "VALUES ($1, 1, 'buy', 'filled', 'p1', 'BTC/USDT', 'h', $2, $3, $4, $5, $6, $7);",
        p, NOW, Decimal("0.000033300000"), Decimal("60060.12345678"), Decimal("2.000000"),
        Decimal("0.000000033300"), Decimal("0.100123"))
    row = await pool.fetchrow("SELECT * FROM demo_orders WHERE cl_ord_id = 'p1';")
    assert row["filled_qty"] == Decimal("0.000033300000")
    assert row["avg_price"] == Decimal("60060.12345678")
    assert row["slippage_pct"] == Decimal("0.100123")


# --- роль только для чтения -----------------------------------------------------


def test_the_read_only_role_gets_select_on_both_tables() -> None:
    created = False
    try:
        has_role = psql_raw(
            "postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'agenttrade_ro';")
        if has_role == "0":
            psql_raw("postgres", "CREATE ROLE agenttrade_ro NOLOGIN;")
            created = True
        name = build_database("demoro")
        try:
            for table in ("demo_orders", "demo_state"):
                for right, want in (("SELECT", "t"), ("INSERT", "f"), ("UPDATE", "f")):
                    got = psql_raw(
                        name, f"SELECT has_table_privilege('agenttrade_ro', '{table}', '{right}');")
                    assert got == want, (table, right)
        finally:
            drop_database(name)
    finally:
        if created:
            psql_raw("postgres", "DROP ROLE IF EXISTS agenttrade_ro;")


# --- откат ------------------------------------------------------------------


def test_the_rollback_drops_both_tables_and_leaves_positions_alone() -> None:
    name = build_database("demorb")
    try:
        psql_raw(name, "INSERT INTO instruments (exchange, symbol, base, quote) "
                 "VALUES ('okx', 'BTC/USDT', 'BTC', 'USDT');")
        before = psql_raw(name, "SELECT count(*) FROM information_schema.columns "
                          "WHERE table_name = 'positions';")
        apply(name, ROLLBACK)
        assert psql_raw(name, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name IN ('demo_orders', 'demo_state');") == "0"
        assert psql_raw(name, "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'positions';") == before
        apply(name, ROLLBACK)                 # откат повторяем без ошибки
        apply(name, MIGRATION)                # и миграция применяется заново
        assert psql_raw(name, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name IN ('demo_orders', 'demo_state');") == "2"
    finally:
        drop_database(name)


def test_the_migration_is_picked_up_by_the_drift_check_glob() -> None:
    """schema_drift.sh строит эталон из init.sql и всех миграций, кроме *_rollback.sql."""
    script = (ROOT / "deploy" / "schema_drift.sh").read_text(encoding="utf-8")
    assert "_rollback.sql" in script
    assert MIGRATION.name.endswith(".sql") and not MIGRATION.name.endswith("_rollback.sql")


def test_the_migration_follows_029_and_takes_the_next_free_number() -> None:
    names = {p.name for p in (ROOT / "db" / "migrations").glob("0*.sql")}
    assert "029_measure_rollups.sql" in names and "030_demo_orders.sql" in names
    assert "030_demo_orders_rollback.sql" in names
    assert len([n for n in names if n.startswith("030_") and not n.endswith("_rollback.sql")]) == 1
