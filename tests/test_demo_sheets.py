"""Этап 9.5, редакция 2, §6: два демо-листа Google Таблицы (на настоящей PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import asyncpg
import pytest
import structlog

from src import export_main
from src.demo import ledger
from src.export import demo_sheets, sheets
from tests.demo_support import (
    Clock,
    FakeExchange,
    add_candle,
    add_position,
    build_context,
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
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
COLS = {name: i for i, name in enumerate(demo_sheets.TRADES_HEADER)}


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_the_header_and_the_warning_follow_the_spec() -> None:
    assert demo_sheets.WARNING == [
        "Лист заполняет система, ручные правки затираются. Проскальзывание на демо-счёте — "
        "оценка, не точная копия реального рынка."]
    assert demo_sheets.TRADES_HEADER == [
        "№ позиции", "дата входа", "время входа", "токен", "сигнал", "вероятность",
        "цена сигнала", "цена покупки", "проскальзывание входа %", "количество монет",
        "сумма входа $", "комиссия входа $", "дата выхода", "время выхода", "причина выхода",
        "цена продажи", "проскальзывание выхода %", "сумма выхода $", "комиссия выхода $",
        "прибыль $", "прибыль %", "время в сделке", "баланс после закрытия $", "статус"]
    assert demo_sheets.BALANCE_HEADER == [
        "дата", "баланс на начало $", "баланс на конец $", "изменение за день $",
        "изменение за день %", "с начала $", "с начала %", "свободно на конец $",
        "в рынке на конец $", "открыто сделок", "закрыто сделок", "прибыльных", "убыточных",
        "пропущено", "комиссии за день $", "наибольшая сумма в рынке за день $"]
    assert demo_sheets.SHEET_TRADES == "Демо OKX — сделки"
    assert demo_sheets.SHEET_BALANCE == "Демо OKX — баланс по дням"
    assert demo_sheets.SHEET_TRADES != export_main._SHEET_TRADES     # журнал владельца не трогаем


def test_numbers_are_numbers_and_blanks_are_blank() -> None:
    assert demo_sheets.num(D("1.23456789"), 4) == 1.2346 and isinstance(demo_sheets.num(1), float)
    assert demo_sheets.num(None) == "" and demo_sheets.num("") == ""


def test_the_demo_sheets_flag_is_off_by_default_and_guards_the_export() -> None:
    from pathlib import Path

    from src.core.config import Settings

    assert Settings(POSTGRES_PASSWORD="x", _env_file=None).DEMO_SHEETS_ENABLED is False
    code = (Path(export_main.__file__)).read_text(encoding="utf-8")
    assert "if settings.DEMO_SHEETS_ENABLED:" in code


@pytest.fixture(scope="module")
def db_name():
    if not server_up():
        pytest.skip("нет PostgreSQL")
    name = build_database("demosheets")
    yield name
    drop_database(name)


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)
    yield p
    await p.close()


async def history(pool) -> dict[str, int]:
    await set_state(pool, 1000, utc(3, 0))
    ids: dict[str, int] = {}
    # закрытая: вход 22:30 UTC 5 октября = 01:30 МСК 6 октября
    ids["closed"] = await add_position(
        pool, opened_at=utc(5, 22, 30), status="closed", closed_at=utc(6, 3), exit_reason="target")
    await pool.execute("UPDATE signals SET probability = 0.66 WHERE id = (SELECT signal_id "
                       "FROM positions WHERE id = $1);", ids["closed"])
    await insert_buy_filled(pool, ids["closed"], filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60060", cost_usd="2.0", filled_at=utc(5, 22, 31))
    await pool.execute("UPDATE demo_orders SET slippage_pct = 0.1 WHERE position_id = $1;",
                       ids["closed"])
    await insert_sell_filled(pool, ids["closed"], cost_usd="2.04", fee="0.002",
                             avg_price="61000", filled_at=utc(6, 3, 5), equity_after="1000.038")
    await pool.execute("UPDATE demo_orders SET slippage_pct = 0.2 WHERE position_id = $1 "
                       "AND leg = 'sell';", ids["closed"])
    # открытая
    ids["open"] = await add_position(pool, opened_at=utc(7, 9))
    await insert_buy_filled(pool, ids["open"], filled_qty="0.00003333", fee="0.00000003",
                            avg_price="60000", cost_usd="2.0", filled_at=utc(7, 9, 1))
    await add_candle(pool, "BTC/USDT", 61000.0, utc(7, 11, 50))
    # пропущенная, ошибка, потеряна, остаток
    for key, status, reason, err in (("skipped", "skipped", "stale", None),
                                     ("rejected", "rejected", None, "51008"),
                                     ("lost", "lost", None, None)):
        ids[key] = await add_position(pool, opened_at=utc(7, 10))
        await pool.execute(
            "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
            "error_code, cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, 'buy', $2, $3, $4, "
            "$5, 'BTC/USDT', 'h', now());", ids[key], status, reason, err, f"at95b{ids[key]}")
    ids["dust"] = await add_position(pool, opened_at=utc(7, 8), status="closed",
                                     closed_at=utc(7, 8, 30))
    await insert_buy_filled(pool, ids["dust"], filled_qty="0.000011", fee="0.000002",
                            filled_at=utc(7, 8, 1))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, 'sell', 'dust', 'below_min_size', "
        "$2, 'BTC/USDT', 'h', now());", ids["dust"], f"at95s{ids['dust']}")
    # до mirror_since — в лист не попадает
    ids["old"] = await add_position(pool, opened_at=utc(1, 10))
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
        "cl_ord_id, symbol, host, virtual_ts) VALUES ($1, 1, 'buy', 'skipped', 'before_start', "
        "'old1', 'BTC/USDT', 'h', now());", ids["old"])
    return ids


def by_id(rows: list[list], pid: int) -> list:
    return next(r for r in rows[2:] if r[0] == pid)


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_trades_sheet_layout_and_order(pool) -> None:
    ids = await history(pool)
    rows = await demo_sheets.build_trades_sheet(pool, NOW, MSK)

    assert rows[0] == demo_sheets.WARNING                       # строка 1 — предупреждение
    assert rows[1] == demo_sheets.TRADES_HEADER                 # строка 2 — заголовок
    assert all(len(r) == 24 for r in rows[1:])
    positions = [r[0] for r in rows[2:]]
    assert ids["old"] not in positions                          # до mirror_since — не показываем
    assert len(positions) == 6
    # новые сверху: по времени входа
    entries = [(r[1][6:], r[1][3:5], r[1][:2], r[2]) for r in rows[2:]]
    assert entries == sorted(entries, reverse=True)


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_closed_trade_row(pool) -> None:
    ids = await history(pool)
    row = by_id(await demo_sheets.build_trades_sheet(pool, NOW, MSK), ids["closed"])
    c = COLS
    # МСК, время позиции (а не исполнения покупки)
    assert row[c["дата входа"]] == "06.10.2026" and row[c["время входа"]] == "01:30:00"
    assert row[c["токен"]] == "BTC" and row[c["сигнал"]] == "покупать"
    assert row[c["вероятность"]] == 0.66
    assert row[c["цена сигнала"]] == 60000.0 and row[c["цена покупки"]] == 60060.0
    assert row[c["проскальзывание входа %"]] == 0.1
    assert row[c["количество монет"]] == 0.00003333 and row[c["сумма входа $"]] == 2.0
    assert row[c["комиссия входа $"]] == 0.001802   # 0.00000003 BTC × 60060, в долларах
    assert row[c["дата выхода"]] == "06.10.2026" and row[c["время выхода"]] == "06:05:00"
    assert row[c["причина выхода"]] == "цель достигнута"
    assert row[c["цена продажи"]] == 61000.0 and row[c["проскальзывание выхода %"]] == 0.2
    assert row[c["сумма выхода $"]] == 2.04 and row[c["комиссия выхода $"]] == 0.002
    assert row[c["прибыль $"]] == 0.038 and row[c["прибыль %"]] == 1.9
    assert row[c["время в сделке"]] == "04:34"      # от покупки (22:31 UTC) до продажи
    assert row[c["баланс после закрытия $"]] == 1000.038
    assert row[c["статус"]] == "закрыта"


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_numbers_in_the_rows_are_numbers_not_text(pool) -> None:
    ids = await history(pool)
    row = by_id(await demo_sheets.build_trades_sheet(pool, NOW, MSK), ids["closed"])
    numeric = ["№ позиции", "вероятность", "цена сигнала", "цена покупки",
               "проскальзывание входа %", "количество монет", "сумма входа $",
               "комиссия входа $", "цена продажи", "проскальзывание выхода %",
               "сумма выхода $", "комиссия выхода $", "прибыль $", "прибыль %",
               "баланс после закрытия $"]
    for name in numeric:
        assert isinstance(row[COLS[name]], int | float), name
        assert not isinstance(row[COLS[name]], str | bool), name
    for name in ("дата входа", "время входа", "дата выхода", "время выхода", "время в сделке",
                 "статус"):
        assert isinstance(row[COLS[name]], str), name


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_an_open_trade_has_empty_exit_columns_and_a_current_profit(pool) -> None:
    ids = await history(pool)
    row = by_id(await demo_sheets.build_trades_sheet(pool, NOW, MSK), ids["open"])
    c = COLS
    for name in ("дата выхода", "время выхода", "причина выхода", "цена продажи",
                 "проскальзывание выхода %", "сумма выхода $", "комиссия выхода $",
                 "баланс после закрытия $"):
        assert row[c[name]] == "", name
    assert row[c["статус"]] == "открыта (текущая)"
    # текущая прибыль по цене рынка 61000: 0.0000333 × 61000 − 2.0
    assert row[c["прибыль $"]] == round(0.0000333 * 61000 - 2.0, 6) == 0.0313
    assert row[c["прибыль %"]] == round((0.0000333 * 61000 / 2.0 - 1) * 100, 4)
    assert row[c["время в сделке"]] == "02:59"          # с 09:01 до 12:00


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_statuses_in_russian(pool) -> None:
    ids = await history(pool)
    rows = await demo_sheets.build_trades_sheet(pool, NOW, MSK)
    status = {k: by_id(rows, ids[k])[COLS["статус"]] for k in ids if k != "old"}
    assert status["closed"] == "закрыта"
    assert status["open"] == "открыта (текущая)"
    assert status["skipped"] == "пропущена: вход устарел"
    assert status["rejected"] == "ошибка: 51008"
    assert status["lost"] == "потеряна (lost)"
    assert status["dust"] == "остаток (dust)"
    skipped = by_id(rows, ids["skipped"])
    assert skipped[COLS["цена покупки"]] == "" and skipped[COLS["прибыль $"]] == ""


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_balance_sheet_layout(pool) -> None:
    await history(pool)
    clock = Clock(NOW)
    ctx = build_context(pool, FakeExchange(), clock, mirror_since=utc(3, 0), start_capital=1000)
    await ledger.ensure_snapshots(ctx, NOW)
    rows = await demo_sheets.build_balance_sheet(pool, NOW, MSK)

    assert rows[0] == demo_sheets.WARNING and rows[1] == demo_sheets.BALANCE_HEADER
    now_row = rows[2]
    assert now_row[0] == "сейчас 07.10.2026 15:00"               # по Москве
    state = await ledger.compute_state(pool, D("1000"))
    h = {name: i for i, name in enumerate(demo_sheets.BALANCE_HEADER)}
    assert now_row[h["баланс на конец $"]] == float(round(state.equity, 6))
    assert now_row[h["свободно на конец $"]] == float(round(state.cash, 6))
    assert now_row[h["в рынке на конец $"]] == float(round(state.in_market, 6))
    assert now_row[h["с начала $"]] == float(round(state.equity - 1000, 6))
    days = rows[3:]
    dates = [r[0] for r in days]
    assert dates == sorted(dates, key=lambda t: (t[6:], t[3:5], t[:2]), reverse=True)
    assert len(days) == 4 and all(len(r) == 16 for r in rows[1:])      # с 3 по 6 октября
    first = days[-1]                                                   # 03.10, самый старый
    assert first[0] == "03.10.2026" and first[h["баланс на начало $"]] == 1000.0
    assert isinstance(first[h["открыто сделок"]], int)
    assert isinstance(first[h["баланс на конец $"]], float)
    day5 = next(r for r in days if r[0] == "06.10.2026")
    assert day5[h["закрыто сделок"]] == 1 and day5[h["прибыльных"]] == 1
    assert day5[h["изменение за день $"]] == float(round(
        D(str(day5[h["баланс на конец $"]])) - D(str(day5[h["баланс на начало $"]])), 6))


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_the_balance_sheet_before_the_first_start_has_only_the_now_label(pool) -> None:
    rows = await demo_sheets.build_balance_sheet(pool, NOW, MSK)
    assert len(rows) == 3 and rows[2][0].startswith("сейчас ")


# --- отправка: режим replace, оба листа целиком --------------------------------------


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_both_sheets_are_rebuilt_whole_in_replace_mode(pool, monkeypatch) -> None:
    await history(pool)
    calls: list[dict] = []

    async def fake_post(url, secret, sheet, mode, rows, header=None, **kw):
        calls.append({"sheet": sheet, "mode": mode, "rows": rows, "header": header, "kw": kw})
        return sheets.SheetsResult(ok=True, inserted=len(rows))

    monkeypatch.setattr(export_main.sheets, "post_rows", fake_post)
    monkeypatch.setattr(export_main.settings, "SHEETS_WEBAPP_URL", "https://example.test/x")
    monkeypatch.setattr(export_main.settings, "SHEETS_SHARED_SECRET", "s3cret")
    log = structlog.get_logger()

    async with pool.acquire() as conn:
        trades, days = await export_main._export_demo_sheets(conn, log)
        again = await export_main._export_demo_sheets(conn, log)

    assert (trades, days) == again
    assert [c["sheet"] for c in calls[:2]] == ["Демо OKX — сделки", "Демо OKX — баланс по дням"]
    assert len(calls) == 4                                   # каждый прогон перестраивает оба
    for call in calls:
        assert call["mode"] == "replace"
        # заголовок идёт внутри строк (строка 2), а не параметром приёмника
        assert call["header"] is None and call["kw"] == {}
        assert call["rows"][0] == demo_sheets.WARNING
        assert call["rows"][1] in (demo_sheets.TRADES_HEADER, demo_sheets.BALANCE_HEADER)
    assert "торговля тест апи окх чтение" not in {c["sheet"] for c in calls}


@pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
async def test_a_refused_sheet_raises_export_error(pool, monkeypatch) -> None:
    async def refuse(*a, **k):
        return sheets.SheetsResult(ok=False, error="HTTP 500")

    monkeypatch.setattr(export_main.sheets, "post_rows", refuse)
    monkeypatch.setattr(export_main.settings, "SHEETS_WEBAPP_URL", "https://example.test/x")
    monkeypatch.setattr(export_main.settings, "SHEETS_SHARED_SECRET", "s3cret")
    async with pool.acquire() as conn:
        with pytest.raises(export_main.ExportError, match="Демо OKX — сделки"):
            await export_main._export_demo_sheets(conn, structlog.get_logger())


async def test_missing_demo_tables_and_missing_credentials_are_clear_errors(monkeypatch) -> None:
    monkeypatch.setattr(export_main.settings, "SHEETS_WEBAPP_URL", "")
    with pytest.raises(export_main.ExportError, match="SHEETS_WEBAPP_URL"):
        await export_main._export_demo_sheets(None, structlog.get_logger())
    monkeypatch.setattr(export_main.settings, "SHEETS_WEBAPP_URL", "https://example.test/x")
    monkeypatch.setattr(export_main.settings, "SHEETS_SHARED_SECRET", "s3cret")

    class NoTables:
        async def fetch(self, *a, **k):
            raise asyncpg.UndefinedTableError('relation "demo_orders" does not exist')

    with pytest.raises(export_main.ExportError, match="миграцию 030"):
        await export_main._export_demo_sheets(NoTables(), structlog.get_logger())


def test_date_helpers_use_the_notification_timezone() -> None:
    ts = utc(5, 22, 30)
    assert demo_sheets.date_str(ts, MSK) == "06.10.2026"
    assert demo_sheets.time_str(ts, MSK) == "01:30:00"
    assert demo_sheets.date_str(None, MSK) == "" and date(2026, 10, 6).year == 2026
    assert timedelta(0) == timedelta()
