"""Этап 9.5.2, редакция 1: запись демо-сделок в лист владельца «торговля демо апи окх».

Сети нет нигде. База — настоящая PostgreSQL (как в ``test_demo_sheets``), приёмник Apps Script —
стенд на Node (``tests/apps_script/receiver_harness.mjs``, сценарии «Этап 9.5.2»), клиент —
подмена ``httpx``.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from src import export_main
from src.core.config import Settings, settings
from src.demo import ledger, rules
from src.export import demo_owner_sheet as dos
from src.export import sheets
from tests.demo_support import (
    FakeRedis,
    add_candle,
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

D = Decimal
ROOT = Path(__file__).resolve().parent.parent
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
needs_db = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")

# Колонки владельца: что система пишет, и что принадлежит формулам.
WRITTEN_COLS = set("ABCDFGHIJKMT")
FORMULA_COLS = set("ELNOPQRSUV")


def utc(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def col_index(letter: str) -> int:
    return ord(letter) - 64


def columns_of(a1: str) -> set[str]:
    """Буквы колонок, которые задевает диапазон вида ``A6:D40`` или ``M6:M9``."""
    match = re.fullmatch(r"([A-Z]+)\d+(?::([A-Z]+)\d+)?", a1)
    assert match, a1
    first, last = match.group(1), match.group(2) or match.group(1)
    return {chr(c + 64) for c in range(col_index(first), col_index(last) + 1)}


def row_values(payload: dos.OwnerSheetPayload, i: int) -> dict[str, Any]:
    """Значения i-й строки по буквам колонок (только записываемые)."""
    out: dict[str, Any] = {}
    for key, (c1, c2) in dos.BLOCK_COLUMNS.items():
        for offset, value in enumerate(payload.blocks[key][i]):
            out[chr(col_index(c1) + offset + 64)] = value
        assert len(payload.blocks[key][i]) == col_index(c2) - col_index(c1) + 1
    return out


# --------------------------------------------------------------------------
# База
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("ownersheet")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


async def raw_order(pool, pid: int, leg: str, status: str, *, skip_reason=None,
                    error_code=None, error_text=None) -> None:
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "error_code, error_text, cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, $2, $3, "
        "$4, $5, $6, $7, 'BTC/USDT', 'h', now());",
        pid, leg, status, skip_reason, error_code, error_text, f"at95{leg[0]}{pid}")


async def set_virtual(pool, pid: int, leg: str, **cols) -> None:
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(cols))
    await pool.execute(f"UPDATE demo_orders SET {sets} WHERE position_id = $1 AND leg = $2;",
                       pid, leg, *cols.values())


async def build(pool, **kwargs) -> dos.OwnerSheetPayload:
    return await dos.build_owner_rows(pool, NOW, "Europe/Moscow", **kwargs)


# --------------------------------------------------------------------------
# 1. Только A3 и блоки A:D, F:K, M, T
# --------------------------------------------------------------------------


@needs_db
async def test_only_a3_and_the_four_blocks_are_ever_addressed(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    for hour in (8, 9, 10):
        pid = await add_position(pool, opened_at=utc(5, hour), status="closed",
                                 closed_at=utc(5, hour + 1))
        await insert_buy_filled(pool, pid, filled_at=utc(5, hour))
        await insert_sell_filled(pool, pid, filled_at=utc(5, hour + 1))
    pid = await add_position(pool, opened_at=utc(6, 9))
    await raw_order(pool, pid, "buy", "skipped", skip_reason="stale")

    payload = await build(pool)
    writes, clears = dos.to_requests(payload)

    assert writes[0] == {"range": "A3", "values": [[1000.0]]}
    touched: set[str] = set()
    for item in [*writes, *clears]:
        cols = columns_of(item["range"])
        assert cols <= WRITTEN_COLS, (item["range"], cols - WRITTEN_COLS)
        assert not cols & FORMULA_COLS, item["range"]
        touched |= cols
    # ни одной строки выше шестой, кроме A3: строки 1, 2, 4, 5 не трогаются
    for item in writes[1:]:
        assert int(re.search(r"\d+", item["range"]).group()) >= 6
    for item in clears:
        assert int(re.search(r"\d+", item["range"]).group()) >= 6
    assert touched == WRITTEN_COLS
    # блоки ровно такой формы: A:D — 4, F:K — 6, M и T — по одному значению
    assert [len(payload.blocks[k][0]) for k in ("A:D", "F:K", "M", "T")] == [4, 6, 1, 1]
    # настройки: ни одной новой в листы старого вида
    assert [item["range"] for item in writes[1:]] == ["A6:D9", "F6:K9", "M6:M9", "T6:T9"]


# --------------------------------------------------------------------------
# 2. Порядок и одна позиция — одна строка
# --------------------------------------------------------------------------


@needs_db
async def test_rows_are_ordered_old_first_and_one_per_position(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    ids = {}
    for key, opened in (("late", utc(6, 12)), ("early", utc(5, 9)),
                        ("tie_b", utc(5, 15)), ("tie_a", utc(5, 15))):
        ids[key] = await add_position(pool, opened_at=opened, status="closed",
                                      closed_at=opened + timedelta(hours=1))
        await insert_buy_filled(pool, ids[key], filled_at=opened + timedelta(seconds=30))
        await insert_sell_filled(pool, ids[key], filled_at=opened + timedelta(hours=1))
    # позиция без строки buy в demo_orders в лист не попадает
    await add_position(pool, opened_at=utc(5, 11))

    payload = await build(pool)
    order = [int(re.search(r"\[поз\. (\d+)\]", t[0]).group(1)) for t in payload.blocks["T"]]
    # «tie_b» создана раньше «tie_a» → у неё меньший id → выше
    assert order == [ids["early"], ids["tie_b"], ids["tie_a"], ids["late"]]
    assert len(payload.blocks["A:D"]) == 4 and payload.total_positions == 4
    assert payload.first_row == 6 and payload.last_written_row == 9


# --------------------------------------------------------------------------
# 3. Сверка с ledger: (K − G) − M == pair_pnl
# --------------------------------------------------------------------------


@needs_db
async def test_the_sheet_profit_formula_matches_the_ledger_to_a_millionth(pool) -> None:
    start = D("1000")
    await set_state(pool, 1000, utc(3, 0))
    specs = [
        # комиссия покупки в МОНЕТЕ, прибыльная
        dict(key="win", qty="0.00003332", fee="0.00000003", fee_ccy="BTC", buy_px="60060",
             buy_cost="2.0", sell_cost="2.04", sell_fee="0.002", sell_px="61000"),
        # комиссия покупки в МОНЕТЕ, убыточная
        dict(key="loss", qty="0.00003332", fee="0.00000004", fee_ccy="BTC", buy_px="60000",
             buy_cost="2.0", sell_cost="1.93", sell_fee="0.0019", sell_px="58000"),
        # комиссия покупки в USDT
        dict(key="usdt", qty="0.00003333", fee="0.01", fee_ccy="USDT", buy_px="60000",
             buy_cost="2.0", sell_cost="2.05", sell_fee="0.002", sell_px="61500"),
        # ещё одна в монете, с «неудобными» цифрами
        dict(key="odd", qty="0.000033333333", fee="0.000000033333", fee_ccy="BTC",
             buy_px="59999.99", buy_cost="2.000001", sell_cost="2.013579", sell_fee="0.00201358",
             sell_px="60407.31"),
    ]
    ids = {}
    for n, sp in enumerate(specs):
        opened = utc(5, 8 + n)
        ids[sp["key"]] = await add_position(pool, opened_at=opened, status="closed",
                                            closed_at=opened + timedelta(hours=2))
        await insert_buy_filled(pool, ids[sp["key"]], filled_qty=sp["qty"], fee=sp["fee"],
                                fee_ccy=sp["fee_ccy"], avg_price=sp["buy_px"],
                                cost_usd=sp["buy_cost"], filled_at=opened)
        await insert_sell_filled(pool, ids[sp["key"]], cost_usd=sp["sell_cost"],
                                 fee=sp["sell_fee"], avg_price=sp["sell_px"],
                                 filled_at=opened + timedelta(hours=2))
    # открытая
    ids["open"] = await add_position(pool, opened_at=utc(6, 9))
    await insert_buy_filled(pool, ids["open"], avg_price="60000", cost_usd="2.0",
                            fee="0.00000003", filled_at=utc(6, 9))
    await add_candle(pool, "BTC/USDT", 61000.0, utc(7, 11, 50))

    payload = await build(pool)
    pnl_total = D(0)
    for i, sp in enumerate(specs):
        row = row_values(payload, i)
        net = D(str(row["K"])) - D(str(row["G"])) - D(str(row["M"]))
        profit, _pct = rules.pair_pnl(D(sp["buy_cost"]), D(sp["sell_cost"]), D(sp["sell_fee"]))
        pair = {
            "buy_cost": D(sp["buy_cost"]), "buy_fee_usd": None, "buy_fee_ccy": sp["fee_ccy"],
            "sell_cost": D(sp["sell_cost"]), "sell_fee_usd": D(sp["sell_fee"]),
            "sell_fee_ccy": "USDT",
        }
        if sp["fee_ccy"] == "USDT":
            pair["buy_fee_usd"] = D(sp["fee"])
            # rules.pair_pnl не вычитает комиссию покупки В USDT (на споте OKX она берётся в
            # монете); ledger и cash вычитают — лист обязан совпасть с деньгами (ledger).
            expected_rules = profit - D(sp["fee"])
        else:
            expected_rules = profit
        assert abs(net - expected_rules) < D("0.000001"), (sp["key"], net, expected_rules)
        assert abs(net - ledger.pair_profit(pair)) < D("0.000001"), sp["key"]
        pnl_total += net
        # пара «покупка → продажа» заполнена целиком
        assert all(row[c] != "" for c in "ABCDFGHIJKMT"), sp["key"]

    # Σ по закрытым = изменение cash в ledger_state (открытая позиция тратит cash на покупку)
    state = await ledger.compute_state(pool, start)
    open_row = row_values(payload, 4)
    open_spent = D(str(open_row["G"]))                      # комиссия открытой — в монете
    assert abs((state.cash - start) - (pnl_total - open_spent)) < D("0.000001")

    # у открытой K и H–J пусты, M — комиссия монеты по цене ПОКУПКИ
    assert [open_row[c] for c in "HIJK"] == ["", "", "", ""]
    assert open_row["M"] == round(float(D("0.00000003") * D("60000")), 8)


# --------------------------------------------------------------------------
# 4. Статусы §3.4
# --------------------------------------------------------------------------


@needs_db
async def test_statuses_follow_the_spec(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    opened = utc(5, 20)                    # 23:00 МСК
    ids: dict[str, int] = {}

    # before_start в лист не попадает (Этап 9.5.3, tests/test_stage_9_5_3.py)
    for reason in ("no_capital", "no_balance", "stale"):
        ids[reason] = await add_position(pool, opened_at=opened)
        await raw_order(pool, ids[reason], "buy", "skipped", skip_reason=reason)
    ids["rejected"] = await add_position(pool, opened_at=opened)
    await raw_order(pool, ids["rejected"], "buy", "rejected", error_code="51008",
                    error_text='{"sCode":"51008","apiKey":"KEY-LEAK-1"}')
    ids["lost"] = await add_position(pool, opened_at=opened)
    await raw_order(pool, ids["lost"], "buy", "lost")
    ids["pending"] = await add_position(pool, opened_at=opened)
    await raw_order(pool, ids["pending"], "buy", "pending")
    ids["sent"] = await add_position(pool, opened_at=opened)
    await raw_order(pool, ids["sent"], "buy", "sent")

    ids["dust"] = await add_position(pool, opened_at=opened, status="closed",
                                     closed_at=opened + timedelta(hours=1))
    await insert_buy_filled(pool, ids["dust"], filled_at=opened)
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, 'sell', 'dust', 'below_min_size', "
        "$2, 'BTC/USDT', 'h', now());", ids["dust"], f"at95s{ids['dust']}")
    ids["sell_rej"] = await add_position(pool, opened_at=opened, status="closed",
                                         closed_at=opened + timedelta(hours=1))
    await insert_buy_filled(pool, ids["sell_rej"], filled_at=opened)
    await raw_order(pool, ids["sell_rej"], "sell", "rejected", error_code="51020")
    ids["sell_lost"] = await add_position(pool, opened_at=opened, status="closed",
                                          closed_at=opened + timedelta(hours=1))
    await insert_buy_filled(pool, ids["sell_lost"], filled_at=opened)
    await raw_order(pool, ids["sell_lost"], "sell", "lost")
    ids["open"] = await add_position(pool, opened_at=opened)
    await insert_buy_filled(pool, ids["open"], filled_at=opened)
    ids["open_noprice"] = await add_position(pool, symbol="DOGE/USDT", opened_at=opened)
    await insert_buy_filled(pool, ids["open_noprice"], symbol="DOGE/USDT", fee_ccy="DOGE",
                            fee="0.000001", filled_at=opened)
    await add_candle(pool, "BTC/USDT", 61000.0, utc(7, 11, 50))

    payload = await build(pool)
    # у ордера в пути заметка без метки — разбираем по порядку id
    order = sorted(ids.values())
    rows = {pid: row_values(payload, order.index(pid)) for pid in order}

    expected_skip = {
        "no_capital": "нет свободного капитала", "no_balance": "мало USDT на демо-счёте",
        "stale": "опоздание входа",
    }
    for reason, text in expected_skip.items():
        row = rows[ids[reason]]
        assert row["D"] == "пропущено"
        assert row["T"] == f"ПРОПУЩЕНА: {text} · [поз. {ids[reason]}]"
        assert row["C"] == "BTC"
        assert [row[c] for c in "FGHIJKM"] == [""] * 7

    rej = rows[ids["rejected"]]
    assert rej["D"] == "ошибка"
    assert rej["T"] == f"ОШИБКА 51008: не хватает средств на демо-счёте · [поз. {ids['rejected']}]"
    assert "KEY-LEAK-1" not in rej["T"]                    # сырой текст биржи в лист не идёт
    assert [rej[c] for c in "FGHIJKM"] == [""] * 7

    lost = rows[ids["lost"]]
    assert lost["D"] == "потеряна"
    assert lost["T"] == f"ПОТЕРЯНА: ордер не найден на бирже · [поз. {ids['lost']}]"

    for key in ("pending", "sent"):
        row = rows[ids[key]]
        assert row["D"] == "покупать" and row["T"] == "ОРДЕР ОТПРАВЛЕН"
        assert row["C"] == "BTC" and row["A"] != "" and row["B"] != ""
        assert [row[c] for c in "FGHIJKM"] == [""] * 7

    # время A/B у не купленных — positions.opened_at: 20:00 UTC = 23:00 МСК того же дня
    sample = rows[ids["stale"]]
    assert sample["A"] == float((datetime(2026, 10, 5).date() - dos._EPOCH).days)
    assert sample["B"] == pytest.approx(23 / 24)

    dust = rows[ids["dust"]]
    assert dust["D"] == "покупать" and dust["F"] != "" and dust["G"] != "" and dust["M"] != ""
    assert [dust[c] for c in "HIJK"] == [""] * 4
    assert dust["T"].startswith("ОСТАТОК: количество меньше минимума биржи, не продано")

    assert rows[ids["sell_rej"]]["T"].startswith("ПРОДАЖА: ОШИБКА 51020")
    assert [rows[ids["sell_rej"]][c] for c in "HIJK"] == [""] * 4
    assert rows[ids["sell_lost"]]["T"].startswith("ПРОДАЖА ПОТЕРЯНА")

    opened_row = rows[ids["open"]]
    assert opened_row["T"].startswith("ОТКРЫТА · текущая прибыль ")
    assert " по цене демо-рынка · [поз. " in opened_row["T"]
    assert rows[ids["open_noprice"]]["T"].startswith("ОТКРЫТА · текущая прибыль: нет цены · ")


def test_the_note_of_a_closed_position_carries_the_whole_story() -> None:
    pos = {"id": 71, "signal_id": 9001, "probability": D("0.83"), "target_price": D("78000"),
           "target_pct": D("1.5"), "logic_version": 7, "opened_at": utc(5, 8),
           "deadline_at": utc(5, 8) + timedelta(hours=48), "base": "BTC", "p_exit_reason": None}
    buy = {"status": "filled", "virtual_price": D("77000"), "slippage_pct": D("0.0123"),
           "lag_sec": 4}
    sell = {"status": "filled", "virtual_price": D("78000"), "slippage_pct": D("-0.05"),
            "exit_reason": "target"}
    text = dos.note_text(pos, buy, sell, None)
    assert text == (
        "[поз. 71] сигнал #9001 · вероятность 0.83 · цель 78000.00 (+1.50%) · "
        "цена сигнала 77000.00, проскальзывание входа +0.012% · задержка входа 4 с · "
        "выход: цель достигнута, цена системы 78000.00, проскальзывание выхода -0.050% · "
        "правило v7")
    # открытая — без блока «выход»
    open_text = dos.note_text(pos, {**buy, "filled_qty": D("0.00003"), "fee": D("0"),
                                    "fee_ccy": "BTC", "cost_usd": D("2"), "fee_usd": D("0")},
                              None, D("80000"))
    assert open_text.startswith("ОТКРЫТА · текущая прибыль +20.00% по цене демо-рынка · [поз. 71]")
    assert "выход:" not in open_text


# --------------------------------------------------------------------------
# 5. Даты и время числами
# --------------------------------------------------------------------------


def test_sheet_serials_are_days_since_1899_12_30() -> None:
    assert dos.to_sheet_serial(datetime(1899, 12, 30)) == 0.0
    assert dos.to_sheet_serial(datetime(1900, 1, 1)) == 2.0
    assert dos.to_sheet_serial(datetime(2026, 10, 6, 12, 0)) == 46301.5
    assert dos.split_serial(datetime(2026, 10, 6, 18, 30, 15)) == (
        46301.0, (18 * 3600 + 30 * 60 + 15) / 86400)
    # округление до секунды не уводит время за сутки
    local = dos._local(datetime(2026, 10, 6, 20, 59, 59, 700_000, tzinfo=UTC), "Europe/Moscow")
    assert local == datetime(2026, 10, 7, 0, 0, 0)


@needs_db
async def test_dates_and_times_are_numbers_and_a_long_trade_subtracts_correctly(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    opened = utc(5, 22, 30, 12)             # 01:30:12 МСК 6 октября
    closed = opened + timedelta(hours=26, minutes=5, seconds=7)
    pid = await add_position(pool, opened_at=opened, status="closed", closed_at=closed)
    await insert_buy_filled(pool, pid, filled_at=opened + timedelta(seconds=20))
    await insert_sell_filled(pool, pid, filled_at=closed)
    row = row_values(await build(pool), 0)

    for col in "ABGFHIJKM":
        assert isinstance(row[col], float), col
    for col in "CDT":
        assert isinstance(row[col], str), col
    bought = opened + timedelta(seconds=20)
    assert row["A"] == float((bought.astimezone(MSK).date() - dos._EPOCH).days)
    assert row["B"] == pytest.approx((1 * 3600 + 30 * 60 + 32) / 86400, abs=1e-12)
    held_days = (row["H"] + row["I"]) - (row["A"] + row["B"])
    expected = (closed - bought).total_seconds() / 86400
    assert held_days == pytest.approx(expected, abs=1e-9)
    assert held_days > 1.0                                  # сделка длиннее суток
    # 26 часов = сутки и ещё два часа: дата выхода на день позже, время выхода позже времени входа
    assert row["H"] - row["A"] == 1.0 and row["I"] > row["B"]


# --------------------------------------------------------------------------
# 6. Очистка хвоста
# --------------------------------------------------------------------------


def test_clears_cover_exactly_the_tail_in_the_four_blocks() -> None:
    def payload(n: int) -> dos.OwnerSheetPayload:
        return dos.OwnerSheetPayload(
            start_capital=D("1000"),
            blocks={"A:D": [[1.0, 0.5, "BTC", "покупать"]] * n, "F:K": [[""] * 6] * n,
                    "M": [[0.0]] * n, "T": [["x"]] * n},
            first_row=6, last_written_row=5 + n, total_positions=n, last_row=1000)

    # было 10 строк (6–15) — стало 8 (6–13): строки 14–15 очищаются, и только в четырёх блоках
    _writes, clears = dos.to_requests(payload(8))
    assert [c["range"] for c in clears] == ["A14:D1000", "F14:K1000", "M14:M1000", "T14:T1000"]
    writes, _ = dos.to_requests(payload(8))
    assert [w["range"] for w in writes] == ["A3", "A6:D13", "F6:K13", "M6:M13", "T6:T13"]
    # данных нет вовсе: пишется одна A3, очищается область с шестой строки
    writes0, clears0 = dos.to_requests(payload(0))
    assert [w["range"] for w in writes0] == ["A3"]
    assert [c["range"] for c in clears0] == ["A6:D1000", "F6:K1000", "M6:M1000", "T6:T1000"]
    # заполнено до последней строки — очищать нечего
    _w, clears_full = dos.to_requests(payload(995))
    assert clears_full == []


@needs_db
async def test_rebuild_is_idempotent_and_shrinks_with_the_data(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    ids = []
    for n in range(10):
        pid = await add_position(pool, opened_at=utc(5, 8) + timedelta(minutes=n))
        await raw_order(pool, pid, "buy", "skipped", skip_reason="stale")
        ids.append(pid)
    first = await build(pool)
    again = await build(pool)
    assert first == again                                    # повторный прогон — те же значения
    assert first.last_written_row == 15
    await pool.execute("DELETE FROM demo_orders WHERE position_id = ANY($1::bigint[]);",
                       ids[-2:])
    shrunk = await build(pool)
    assert shrunk.last_written_row == 13
    _w, clears = dos.to_requests(shrunk)
    assert clears[0]["range"] == "A14:D1000"


# --------------------------------------------------------------------------
# 7. Предел строк
# --------------------------------------------------------------------------


@needs_db
async def test_rows_beyond_the_formula_limit_are_dropped_and_reported(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    for n in range(18):
        pid = await add_position(pool, opened_at=utc(5, 0) + timedelta(minutes=n))
        await raw_order(pool, pid, "buy", "skipped", skip_reason="stale")
    # формулы до 20-й строки → данных можно 15 (строки 6–20)
    payload = await build(pool, last_row=20)
    assert payload.total_positions == 18 and payload.dropped == 3
    assert payload.last_written_row == 20 and len(payload.blocks["T"]) == 15
    assert [c["range"] for c in dos.to_requests(payload)[1]] == []   # очищать нечего
    first_ids = [int(re.search(r"\[поз\. (\d+)\]", t[0]).group(1)) for t in payload.blocks["T"]]
    assert first_ids == sorted(first_ids)                    # пишутся ПЕРВЫЕ (старые)


async def run_warn(payload, monkeypatch) -> tuple[list[str], FakeRedis]:
    redis = FakeRedis()
    sent: list[str] = []

    async def fake_alert(text: str, log: Any) -> None:
        sent.append(text)

    monkeypatch.setattr(export_main, "get_redis", lambda: redis)
    monkeypatch.setattr(export_main, "_alert", fake_alert)
    log = structlog.get_logger()
    await export_main._warn_owner_rows(payload, log)
    await export_main._warn_owner_rows(payload, log)         # второй раз — антидребезг
    return sent, redis


def row_payload(total: int, last_row: int = 1000) -> dos.OwnerSheetPayload:
    kept = min(total, last_row - 5)
    return dos.OwnerSheetPayload(
        start_capital=D("1000"), blocks={k: [] for k in dos.BLOCK_COLUMNS}, first_row=6,
        last_written_row=5 + kept, total_positions=total, dropped=total - kept,
        last_row=last_row)


async def test_the_limit_warning_comes_early_once_a_day(monkeypatch) -> None:
    for total, expect in ((900, 0), (985, 0), (986, 1), (995, 1), (996, 1)):
        sent, redis = await run_warn(row_payload(total), monkeypatch)
        assert len(sent) == expect, (total, sent)
        if expect:
            assert "в листе кончаются строки с формулами, протяните формулы ниже" in sent[0]
            assert [t for k, t in redis.ttl.items()] == [24 * 3600]
    overflow, _ = await run_warn(row_payload(1010), monkeypatch)
    assert "15 последних в лист не записаны" in overflow[0]


# --------------------------------------------------------------------------
# 8. Приёмник: стенд на Node
# --------------------------------------------------------------------------


def test_the_receiver_harness_passes_every_9_5_2_scenario() -> None:
    if shutil.which("node") is None:
        pytest.skip("node не установлен — стенд приёмника не выполняется")
    proc = subprocess.run(["node", str(ROOT / "tests" / "apps_script" / "receiver_harness.mjs")],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout
    scenarios = [line for line in proc.stdout.splitlines() if "9.5.2:" in line]
    assert len(scenarios) >= 12, scenarios
    for name in ("пробелом в конце имени", "sheet_not_found", "sheet_ambiguous",
                 "formula_in_target", "формулы, форматы и проверка данных"):
        assert any(name in line for line in scenarios), name


def test_the_receiver_never_creates_a_sheet_or_clears_formats() -> None:
    receiver = (ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")
    body = receiver[receiver.index("function cellsValues("):]
    assert "insertSheet" not in body and "clearFormat" not in body
    assert ".clear()" not in body and "clearContent()" in body
    assert "setDataValidation" not in body and "setNumberFormat" not in body
    assert "const RECEIVER_VERSION = '9.5.2';" in receiver


# --------------------------------------------------------------------------
# Клиент cells_values (httpx подменён)
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Http:
    """Подмена ``httpx.AsyncClient``: отдаёт ответы по очереди, запоминает запросы."""

    def __init__(self, responses: list[_Resp]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Http:
        return self

    async def __aenter__(self) -> _Http:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> _Resp:
        self.requests.append(json)
        return self.responses.pop(0)


async def _no_sleep(_s: float) -> None:
    return None


async def test_write_cells_values_sends_the_spec_request_and_parses_the_answer(monkeypatch):
    http = _Http([_Resp(200, {"ok": True, "written": 25, "cleared": 100, "version": "9.5.2"})])
    monkeypatch.setattr("src.export.sheets.httpx.AsyncClient", http)
    writes = [{"range": "A6:D6", "values": [[1.0, 0.5, "BTC", "покупать"]]}]
    clears = [{"range": "A7:D1000"}]
    res = await sheets.write_cells_values("https://x.test/exec", "s", "лист", writes, clears)
    assert res.ok and (res.written, res.cleared, res.receiver_version) == (25, 100, "9.5.2")
    assert http.requests == [{"secret": "s", "sheet": "лист", "mode": "cells_values",
                              "writes": writes, "clears": clears}]


async def test_write_cells_values_does_not_retry_a_definitive_refusal(monkeypatch):
    http = _Http([_Resp(200, {"ok": False, "error": "formula_in_target", "range": "M6:M8"})])
    monkeypatch.setattr("src.export.sheets.httpx.AsyncClient", http)
    monkeypatch.setattr("src.export.sheets.asyncio.sleep", _no_sleep)
    res = await sheets.write_cells_values("u", "s", "лист", [], [])
    assert not res.ok and res.error_code == "formula_in_target" and res.error_range == "M6:M8"
    assert len(http.requests) == 1               # повтор отказа «формула в диапазоне» бессмыслен


async def test_write_cells_values_retries_network_failures(monkeypatch):
    http = _Http([_Resp(500, {}), _Resp(200, _ValueError()), _Resp(200, {"ok": True})])
    monkeypatch.setattr("src.export.sheets.httpx.AsyncClient", http)
    monkeypatch.setattr("src.export.sheets.asyncio.sleep", _no_sleep)
    res = await sheets.write_cells_values("u", "s", "лист", [], [])
    assert res.ok and len(http.requests) == 3


def _ValueError() -> Exception:                             # noqa: N802
    return ValueError("не JSON")


# --------------------------------------------------------------------------
# 9. Версия приёмника не 9.5.2 → ни одного запроса записи, алерт
# --------------------------------------------------------------------------


@dataclass
class Harness:
    redis: FakeRedis
    alerts: list[str]
    probes: list[str]
    writes: list[tuple]
    logs: list[dict[str, Any]]


@pytest.fixture
def harness(monkeypatch) -> Harness:
    h = Harness(FakeRedis(), [], [], [], [])
    monkeypatch.setattr(export_main, "get_redis", lambda: h.redis)

    async def fake_alert(text: str, log: Any) -> None:
        h.alerts.append(text)

    monkeypatch.setattr(export_main, "_alert", fake_alert)
    for name, value in (("DEMO_OWNER_SHEET_ENABLED", True), ("SHEETS_WEBAPP_URL", "https://x.test/e"),
                        ("SHEETS_SHARED_SECRET", "SHEETS-SECRET-DO-NOT-LOG"),
                        ("DEMO_OWNER_SHEET", "торговля демо апи окх"),
                        ("DEMO_OWNER_SHEET_LAST_ROW", 1000)):
        monkeypatch.setattr(settings, name, value)
    payload = dos.OwnerSheetPayload(
        start_capital=D("1000"),
        blocks={"A:D": [[46300.0, 0.5, "BTC", "покупать"]], "F:K": [[60000.0, 2.0, "", "", "", ""]],
                "M": [[0.001]], "T": [["[поз. 1] x"]]},
        first_row=6, last_written_row=6, total_positions=1, last_row=1000)

    async def fake_build(conn, now, tz, *, last_row=None):
        return payload

    monkeypatch.setattr(dos, "build_owner_rows", fake_build)
    return h


def patch_receiver(monkeypatch, h: Harness, *, version: str | None, ok: bool = True,
                   write: sheets.SheetsResult | None = None) -> None:
    async def fake_post_rows(url, secret, sheet, mode, rows, *a, **k):
        h.probes.append(f"{sheet}|{mode}")
        return sheets.SheetsResult(ok=ok, receiver_version=version)

    async def fake_write(url, secret, sheet, writes, clears=None):
        h.writes.append((sheet, writes, clears))
        return write or sheets.SheetsResult(ok=True, written=10, cleared=20,
                                            receiver_version="9.5.2")

    monkeypatch.setattr(sheets, "post_rows", fake_post_rows)
    monkeypatch.setattr(sheets, "write_cells_values", fake_write)


async def run_export() -> tuple[bool, list[dict[str, Any]]]:
    with structlog.testing.capture_logs() as logs:
        done = await export_main._export_demo_owner_sheet(object(), structlog.get_logger())
    return done, logs


@pytest.mark.parametrize("version", ["9.2", "9.1.2", "8.4.1", None])
async def test_a_receiver_that_is_not_9_5_2_gets_no_write_and_one_alert_per_hour(
    monkeypatch, harness, version
) -> None:
    patch_receiver(monkeypatch, harness, version=version)
    done1, _ = await run_export()
    done2, _ = await run_export()
    assert done1 is False and done2 is False
    assert harness.writes == []                              # ни одного запроса записи
    assert harness.probes == ["торговля тест апи окх чтение|version"] * 2
    assert len(harness.alerts) == 1                          # анти-дребезг
    assert "приёмник Google не обновлён до 9.5.2" in harness.alerts[0]
    key = f"{export_main.OWNER_SHEET_ALERT_PREFIX}receiver_version"
    assert harness.redis.ttl[key] == 3600
    assert export_main.OWNER_SHEET_LAST_OK_KEY not in harness.redis.data


async def test_a_dead_receiver_probe_is_the_same_refusal(monkeypatch, harness) -> None:
    patch_receiver(monkeypatch, harness, version=None, ok=False)
    assert (await run_export())[0] is False
    assert harness.writes == [] and len(harness.alerts) == 1


async def test_a_9_5_2_receiver_gets_the_write_and_a_single_log_line(monkeypatch, harness):
    patch_receiver(monkeypatch, harness, version="9.5.2")
    done, logs = await run_export()
    assert done is True and len(harness.writes) == 1
    sheet, writes, clears = harness.writes[0]
    assert sheet == "торговля демо апи окх"
    assert [w["range"] for w in writes] == ["A3", "A6:D6", "F6:K6", "M6:M6", "T6:T6"]
    assert [c["range"] for c in clears] == ["A7:D1000", "F7:K1000", "M7:M1000", "T7:T1000"]
    done_lines = [r for r in logs if r["event"] == "demo_owner_sheet_done=1"]
    assert len(done_lines) == 1
    assert (done_lines[0]["rows"], done_lines[0]["written"], done_lines[0]["cleared"],
            done_lines[0]["version"]) == (1, 10, 20, "9.5.2")
    assert "seconds" in done_lines[0]
    assert harness.alerts == []
    assert export_main.OWNER_SHEET_LAST_OK_KEY in harness.redis.data


async def test_a_refused_write_alerts_once_and_never_stops_the_others(monkeypatch, harness):
    refused = sheets.SheetsResult(ok=False, error="ok=false: formula_in_target (M6:M6)",
                                  error_code="formula_in_target", error_range="M6:M6")
    patch_receiver(monkeypatch, harness, version="9.5.2", write=refused)
    for _ in range(3):
        assert (await run_export())[0] is False              # не бросает
    assert len(harness.alerts) == 1 and "formula_in_target" in harness.alerts[0]
    assert export_main.OWNER_SHEET_LAST_OK_KEY not in harness.redis.data


async def test_a_crash_inside_the_target_is_contained(monkeypatch, harness) -> None:
    async def boom(conn, now, tz, *, last_row=None):
        raise RuntimeError("всё сломалось")

    monkeypatch.setattr(dos, "build_owner_rows", boom)
    assert (await run_export())[0] is False
    assert len(harness.alerts) == 1


async def test_the_switch_off_does_nothing_at_all(monkeypatch, harness) -> None:
    monkeypatch.setattr(settings, "DEMO_OWNER_SHEET_ENABLED", False)
    patch_receiver(monkeypatch, harness, version="9.5.2")

    async def never(*a, **k):
        raise AssertionError("выключенный лист обратился к базе")

    monkeypatch.setattr(dos, "build_owner_rows", never)
    assert (await run_export())[0] is False
    assert harness.probes == [] and harness.writes == [] and harness.alerts == []


def test_defaults_and_env_example() -> None:
    cfg = Settings(POSTGRES_PASSWORD="x", _env_file=None)
    assert cfg.DEMO_OWNER_SHEET_ENABLED is False
    assert cfg.DEMO_OWNER_SHEET == "торговля демо апи окх"
    assert cfg.DEMO_OWNER_SHEET_LAST_ROW == 1000
    assert cfg.DEMO_SHEETS_ENABLED is False                  # листы ред. 2 ТЗ 9.5 не включены
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^DEMO_OWNER_SHEET_ENABLED=false", env, re.M)
    assert re.search(r"^DEMO_OWNER_SHEET=торговля демо апи окх", env, re.M)
    assert re.search(r"^DEMO_OWNER_SHEET_LAST_ROW=1000", env, re.M)
    assert re.search(r"^DEMO_SHEETS_ENABLED=false", env, re.M)


def test_the_target_runs_in_both_export_modes_and_is_independent() -> None:
    source = (ROOT / "src" / "export_main.py").read_text(encoding="utf-8")
    run = source[source.index("async def _run("):]
    call = run.index("await _export_demo_owner_sheet(conn, log)")
    assert run.index("if not positions_only:") < run.index("if settings.DEMO_SHEETS_ENABLED:")
    # вызов стоит вне ветки «if not positions_only» — то есть работает и в --positions-only
    block_start = run.rindex("\n        # ЛИСТ ВЛАДЕЛЬЦА", 0, call)
    assert run[block_start:call].count("if not positions_only") == 0
    assert "demo_sheets._export" not in source


# --------------------------------------------------------------------------
# 10. Ни одной записи в базу
# --------------------------------------------------------------------------

_WRITE_SQL = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE|ALTER\s+TABLE"
                        r"|CREATE\s+TABLE|DROP\s+TABLE)\b", re.I)


def _string_constants(source: str) -> list[str]:
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def test_the_new_code_never_writes_to_the_database() -> None:
    import inspect

    module_source = (ROOT / "src" / "export" / "demo_owner_sheet.py").read_text(encoding="utf-8")
    for text in _string_constants(module_source):
        assert not _WRITE_SQL.search(text), text
    tree = ast.parse(module_source)
    methods = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not methods & {"execute", "executemany", "executescript", "copy_records_to_table"}

    for fn in (export_main._export_demo_owner_sheet, export_main._warn_owner_rows,
               export_main._owner_alert):
        src = inspect.getsource(fn)
        for text in _string_constants(src):
            assert not _WRITE_SQL.search(text), (fn.__name__, text)
        assert "conn.execute" not in src and "executemany" not in src


@needs_db
async def test_a_full_build_leaves_the_database_untouched(pool) -> None:
    await set_state(pool, 1000, utc(3, 0))
    pid = await add_position(pool, opened_at=utc(5, 9), status="closed", closed_at=utc(5, 10))
    await insert_buy_filled(pool, pid, filled_at=utc(5, 9))
    await insert_sell_filled(pool, pid, filled_at=utc(5, 10))

    async def fingerprint() -> list:
        out = []
        for table in ("demo_orders", "demo_state", "positions", "signals", "instruments",
                      "demo_balance_daily"):
            out.append(await pool.fetchval(
                f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t;"))
        return out

    before = await fingerprint()
    await build(pool)
    await build(pool)
    assert await fingerprint() == before


# --------------------------------------------------------------------------
# 11. Ключи и подписи не попадают в журнал и в T
# --------------------------------------------------------------------------


@needs_db
async def test_secrets_reach_neither_the_note_nor_the_log(pool, monkeypatch, harness) -> None:
    leak = ("KEY-abc123-SECRET-DO-NOT-LOG", "OK-ACCESS-SIGN: deadbeef", "PASS-qwe456-SECRET")
    await set_state(pool, 1000, utc(3, 0))
    pid = await add_position(pool, opened_at=utc(5, 9))
    await raw_order(pool, pid, "buy", "rejected", error_code="50113",
                    error_text=" | ".join(leak))
    pid2 = await add_position(pool, opened_at=utc(5, 10))
    await insert_buy_filled(pool, pid2, filled_at=utc(5, 10))
    await raw_order(pool, pid2, "sell", "rejected", error_code="51020",
                    error_text=" | ".join(leak))

    real = await build(pool)
    notes = " ".join(t[0] for t in real.blocks["T"])
    for secret in leak:
        assert secret not in notes
    assert "OK-ACCESS" not in notes and "SIGN" not in notes

    # весь прогон: журнал и алерты тоже чисты
    async def fake_build(conn, now, tz, *, last_row=None):
        return real

    monkeypatch.setattr(dos, "build_owner_rows", fake_build)
    patch_receiver(monkeypatch, harness, version="9.5.2",
                   write=sheets.SheetsResult(ok=False, error="ok=false: sheet_not_found",
                                             error_code="sheet_not_found"))
    _done, logs = await run_export()
    dump = json.dumps(logs, ensure_ascii=False, default=str) + " ".join(harness.alerts)
    for secret in (*leak, "SHEETS-SECRET-DO-NOT-LOG"):
        assert secret not in dump
    assert "secret" not in {k for r in logs for k in r}
