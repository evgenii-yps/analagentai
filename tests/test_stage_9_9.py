"""Этап 9.9: итог версии логики 7 — замер, только чтение (ТЗ 9.9, раздел 6).

SQL блоков проверяется на НАСТОЯЩЕЙ PostgreSQL с синтетическими данными, известный ответ
которых посчитан руками (в комментариях к сценариям). Мутации (§6, п. 7) — это копии SQL с
одной намеренной поломкой: проверка обязана на них упасть, иначе она ничего не проверяет.

Сеть, биржа и docker не нужны: ``docker compose exec ... psql`` подменяется заглушкой, которая
запускает тот же psql против тестовой базы.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.demo import ledger
from tests.demo_support import (
    HOST,
    PORT,
    ROOT,
    USER,
    build_database,
    drop_database,
    insert_buy_filled,
    insert_sell_filled,
    open_pool,
    psql_raw,
    reset_data,
    server_up,
)

pytestmark = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

SQL_DIR = ROOT / "analysis" / "sql" / "9_9"
RUN_9_9 = ROOT / "analysis" / "run_9_9.sh"
RUN_7_1 = ROOT / "analysis" / "run_7_1.sh"
LIB_RO = ROOT / "analysis" / "lib_ro.sh"
LIB_VERIFY = ROOT / "deploy" / "lib_verify.sh"
GOLDEN_7_1 = ROOT / "tests" / "golden" / "run_7_1_before_9_9.sh"

BLOCK_FILES = [
    "00_scope.sql", "01_profile_closed.sql", "02_open_mtm.sql", "03_by_token.sql",
    "04_exits_and_time.sql", "05_mae_mfe.sql", "06_time_in_market.sql",
    "07_entry_vs_random.sql", "08_weekly.sql", "09_demo_vs_virtual.sql",
]
AS_OF = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
AS_OF_TXT = "2026-10-18 12:00:00+00"
H0 = datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC)     # целый час: удобная точка отсчёта

D = Decimal


# --------------------------------------------------------------------------
# База, роль только на чтение, запуск SQL
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    """Чистая база по init.sql и миграциям + роль agenttrade_ro с ОДНИМ правом SELECT."""
    role = "SELECT {} FROM pg_roles WHERE rolname='agenttrade_ro';"
    created = psql_raw("postgres", role.format("count(*)")) == "0"
    restore_nologin = False
    if created:
        psql_raw("postgres", "CREATE ROLE agenttrade_ro LOGIN;")
    elif psql_raw("postgres", role.format("rolcanlogin")) == "f":
        psql_raw("postgres", "ALTER ROLE agenttrade_ro LOGIN;")
        restore_nologin = True
    name = build_database("s99")
    psql_raw(name, "GRANT SELECT ON ALL TABLES IN SCHEMA public TO agenttrade_ro;")
    yield name
    drop_database(name)
    if created:
        psql_raw("postgres", "DROP ROLE IF EXISTS agenttrade_ro;")
    elif restore_nologin:
        psql_raw("postgres", "ALTER ROLE agenttrade_ro NOLOGIN;")


@pytest.fixture
async def pool(db_name):
    p = await open_pool(db_name)
    await reset_data(p)       # BTC/USDT, DOGE/USDT, FOO/USDT
    yield p
    await p.close()


def run_sql(db: str, path: Path | str, *, extra=(), text: str | None = None) -> str:
    """Выполняет файл блока ролью agenttrade_ro; выходной формат — «имя|значение» (-A)."""
    sql = text if text is not None else Path(path).read_text(encoding="utf-8")
    cmd = ["psql", "-X", "-q", "-A", "-F", "|", "-h", HOST, "-p", PORT, "-U", "agenttrade_ro",
           "-d", db, "-v", "ON_ERROR_STOP=1", "-v", f"as_of={AS_OF_TXT}"]
    for item in extra:
        cmd += ["-v", item]
    res = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


def num(value: str):
    if value in ("-", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return value


def record(out: str) -> dict:
    """Одна запись расширенного вывода (-A): строки «подпись|значение»."""
    rec = {}
    for line in out.splitlines():
        if "|" in line:
            key, _, val = line.partition("|")
            rec[key.strip()] = num(val)
    return rec


def table(out: str) -> list[dict]:
    """Таблица неподвижного вывода (-A): первая строка — заголовки, дальше строки."""
    lines = [ln for ln in out.splitlines() if ln.strip() and "|" in ln]
    head = lines[0].split("|")
    return [dict(zip(head, [num(v) for v in ln.split("|")], strict=True)) for ln in lines[1:]]


def sql_of(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Сценарии данных
# --------------------------------------------------------------------------


async def mkpos(pool, symbol, opened, *, status="closed", closed=None, reason="target", pct=1.0,
                usd=None, entry=100.0, mae=-0.5, mfe=1.2, target=1.0, cost=0.22, notional=2.0,
                logic=7) -> int:
    """Позиция с заданными итогом, ходом цены и версией логики. Возвращает id."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol=$1;", symbol)
    sig = await pool.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, $2, 'buy', $3) RETURNING id;", inst["id"], opened, logic)
    c = status == "closed"
    return await pool.fetchval(
        "INSERT INTO positions (instrument_id, signal_id, logic_version, horizon_h, side, "
        "is_virtual, status, signal_ts, signal_price, opened_at, entry_price, entry_lag_sec, "
        "entry_slippage_pct, qty, notional_usd, target_pct, target_price, cost_pct, deadline_at, "
        "resolution, closed_at, exit_price, exit_reason, outcome_certain, net_pnl_pct, "
        "net_pnl_usd, mae_pct, mfe_pct) "
        "VALUES ($1, $2, $3, 24, 'buy', TRUE, $4, $5, $6, $5, $6, 5, 0, "
        "$7::numeric / $6::numeric, $7::numeric, $8, $6::numeric * 1.01, $9, "
        "$5::timestamptz + interval '48 hours', '1m', $10, $11, $12, $13, $14, $15, $16, $17) "
        "RETURNING id;",
        inst["id"], sig, logic, status, opened, entry, notional, target, cost,
        closed if c else None, entry * 1.01 if c else None, reason if c else None,
        True if c else None, pct if c else None,
        (usd if usd is not None else notional * pct / 100) if c else None,
        mae if c else None, mfe if c else None)


async def candle(pool, symbol, ts, close, tf="1m") -> None:
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol=$1;", symbol)
    await pool.execute(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $4, $4, $4, 1) ON CONFLICT DO NOTHING;",
        inst["id"], tf, ts, close)


async def seed_01(pool) -> dict:
    """§6.1: итоги +1, +1, 0, −2, −3 и закрытие data_gap (−9, вне метрик)."""
    ids = {}
    for key, day, pct in (("w1", 1, 1.0), ("w2", 1, 1.0), ("z", 2, 0.0), ("l2", 2, -2.0),
                          ("l3", 3, -3.0)):
        ids[key] = await mkpos(pool, "BTC/USDT", H0 + timedelta(days=day, hours=len(ids)),
                               closed=H0 + timedelta(days=day, hours=len(ids) + 5),
                               reason="plus_exit" if key == "z" else "target", pct=pct)
    ids["gap"] = await mkpos(pool, "DOGE/USDT", H0 + timedelta(days=4), reason="data_gap",
                             closed=H0 + timedelta(days=4, hours=9), pct=-9.0)
    # чужие выборки не должны попасть в расчёт: версия 6
    await mkpos(pool, "BTC/USDT", H0 + timedelta(days=5), closed=H0 + timedelta(days=5, hours=2),
                pct=7.0, logic=6)
    return ids


def assert_01(out: str, ids: dict) -> None:
    r = record(out)
    assert r["Закрытых сделок (n)"] == 5                       # data_gap не входит
    assert r["Различных дней открытия (n_eff)"] == 3
    assert r["Доля прибыльных, %"] == 40.0                      # 2 из 5
    assert r["Доля убыточных, %"] == 40.0                       # 2 из 5
    assert r["Доля «в ноль», %"] == 20.0                        # 1 из 5
    assert r["Средняя прибыль, %"] == 1.0 and r["Средний убыток, %"] == -2.5
    assert r["Payoff (ср. прибыль / ср. убыток)"] == 0.4        # 1 / 2,5
    assert r["Фактор прибыли (по %)"] == 0.4                    # 2 / 5
    assert r["Фактор прибыли (по $)"] == 0.4
    assert r["Средний итог на сделку, %"] == -0.6
    # sd = √3,3 = 1,8166; полуширина = 1,96 × 1,8166 / √3 = 2,0557
    assert r["интервал, нижняя граница, %"] == pytest.approx(-0.6 - 2.0557, abs=0.006)
    assert r["интервал, верхняя граница, %"] == pytest.approx(-0.6 + 2.0557, abs=0.006)
    assert r["Медиана итога, %"] == 0.0
    assert r["Лучшая сделка, %"] == 1.0 and r["Худшая сделка, %"] == -3.0
    assert r["Сумма итога, $"] == -0.06                         # (1+1+0−2−3)% от $2
    assert r["Справочно: data_gap, число (вне метрик)"] == 1
    assert r["Справочно: data_gap, средний итог, %"] == -9.0


async def seed_02(pool) -> None:
    """§6.2: вход 100, свеча 90, издержки 0,2 → −10,2 %; вторая позиция — свеча старше 10 минут."""
    a = AS_OF
    await mkpos(pool, "BTC/USDT", a - timedelta(hours=5), status="open", entry=100.0, cost=0.2)
    await mkpos(pool, "DOGE/USDT", a - timedelta(hours=6), status="open", entry=100.0, cost=0.2)
    await candle(pool, "BTC/USDT", a - timedelta(minutes=2), 90.0)
    await candle(pool, "BTC/USDT", a - timedelta(seconds=30), 200.0)   # не закрыта — не берётся
    await candle(pool, "DOGE/USDT", a - timedelta(minutes=11), 50.0)   # старше 10 минут
    await mkpos(pool, "BTC/USDT", a - timedelta(days=2), closed=a - timedelta(days=1), pct=1.0)


def assert_02(out: str) -> None:
    r = record(out)
    assert r["Открыто позиций"] == 2
    assert r["с текущей ценой"] == 1
    assert r["без цены (нет свежей свечи)"] == 1
    assert r["Худший текущий итог открытых, %"] == -10.2        # (90/100 − 1) × 100 − 0,2
    assert r["Средний текущий итог открытых, %"] == -10.2
    assert r["Открытых глубже −3 %"] == 1 and r["Открытых глубже −5 %"] == 1
    assert r["Открытых глубже −10 %"] == 1
    assert r["Закрытые + открытые с ценой (n)"] == 2             # 1 закрытая + 1 открытая с ценой
    assert r["Средний итог только закрытых, %"] == 1.0
    assert r["средний итог, %"] == -4.6                        # (1 − 10,2) / 2
    assert r["Разница «общий − закрытые», п.п."] == -5.6


async def seed_06(pool) -> None:
    """§6.3: BTC A [H, H+6) и B [H+3, H+9); DOGE [H+9, H+10:30). Замер в H+12:30."""
    H = H0
    await mkpos(pool, "BTC/USDT", H, closed=H + timedelta(hours=6), pct=1.0)
    await mkpos(pool, "BTC/USDT", H + timedelta(hours=3), closed=H + timedelta(hours=9), pct=1.0)
    await mkpos(pool, "DOGE/USDT", H + timedelta(hours=8, minutes=30),
                closed=H + timedelta(hours=10, minutes=30), pct=1.0)


AS_OF_06 = H0 + timedelta(hours=12, minutes=30)


def assert_06(out: str) -> None:
    rows = {r["Монета"]: r for r in table(out)}
    # ручной расчёт по сетке H … H+12 (13 точек), позиция открыта в t, если opened ≤ t < closed
    # BTC: 1,1,1,2,2,2,1,1,1,0,0,0,0 (12/13; 4 пустых); DOGE: открыта в H+9 и H+10 (2/13)
    # всего: 1,1,1,2,2,2,1,1,1,1,1,0,0 → максимум 2, пустых 2 из 13
    total = rows["ВСЕ МОНЕТЫ"]
    assert total["Часов в сетке"] == 13
    assert total["Максимум"] == 2
    assert total["Ср. одновременно"] == pytest.approx(14 / 13, abs=0.006)
    assert total["Часов без позиций, %"] == pytest.approx(100 * 2 / 13, abs=0.006)
    assert rows["BTC/USDT"]["Максимум"] == 2
    assert rows["BTC/USDT"]["Часов без позиций, %"] == pytest.approx(100 * 4 / 13, abs=0.006)
    assert rows["DOGE/USDT"]["Часов без позиций, %"] == pytest.approx(100 * 11 / 13, abs=0.006)
    assert rows["DOGE/USDT"]["Максимум"] == 1


def _px(k: float) -> float:
    return 100.0 * 1.01 ** (k / 24.0)        # рост на 1 % в сутки


async def seed_07(pool) -> None:
    """§6.4: цена растёт на 1 % в сутки. Вход 1 — со свечой +24 ч; вход 2 — без неё;
    вход 3 — прошло меньше суток (не участвует). Замер — H0+40 ч."""
    for k in range(0, 41):
        await candle(pool, "BTC/USDT", H0 + timedelta(hours=k), _px(k), tf="1h")
    p1 = H0 + timedelta(hours=1)
    await mkpos(pool, "BTC/USDT", p1, closed=p1 + timedelta(hours=3), entry=_px(1))
    await candle(pool, "BTC/USDT", p1 + timedelta(hours=24), _px(25))
    # «ловушка» для сдвига на 23 ч: свеча в +23 ч с другой ценой
    await candle(pool, "BTC/USDT", p1 + timedelta(hours=23), 50.0)
    p2 = H0 + timedelta(hours=2)
    await mkpos(pool, "BTC/USDT", p2, closed=p2 + timedelta(hours=3), entry=_px(2))
    await mkpos(pool, "BTC/USDT", H0 + timedelta(hours=30), status="open", entry=_px(30))


AS_OF_07 = H0 + timedelta(hours=40)


def assert_07(out: str) -> None:
    rows = {r["Монета"]: r for r in table(out.split("\n\nВывод")[0])}
    tot = rows["ВСЕ"]
    assert tot["Входов"] == 1                      # вход со свечой
    assert tot["Без св."] == 1                     # свечи через +24 ч нет — исключён и посчитан
    assert tot["Вх. ср., %"] == pytest.approx(1.0, abs=0.01)
    assert tot["Сл. ср., %"] == pytest.approx(1.0, abs=0.01)
    assert tot["Часов"] == 16                      # часы 1 … 16 (+24 ч ещё в пределах замера)
    assert abs(tot["Разн., п.п."]) < 0.01
    assert tot["Вх. в «+» ч, %"] == 100.0
    assert "Вывод без интерпретации: разница средних = 0.00 п.п., n_eff = 1" in out


async def seed_09(pool) -> list[int]:
    """§6.5: пара 1 — комиссия покупки в монете, продажи в USDT; пара 2 — покупки тоже в USDT."""
    a = await mkpos(pool, "BTC/USDT", AS_OF - timedelta(days=3), closed=AS_OF - timedelta(days=2),
                    pct=0.8, notional=50.0)
    await insert_buy_filled(pool, a, cost_usd="50.0", fee_ccy="BTC", fee="0.00000083",
                            avg_price="60060", filled_at=AS_OF - timedelta(days=3))
    await insert_sell_filled(pool, a, cost_usd="50.45", fee="0.05045", fee_ccy="USDT",
                             filled_at=AS_OF - timedelta(days=2))
    b = await mkpos(pool, "DOGE/USDT", AS_OF - timedelta(days=3), closed=AS_OF - timedelta(days=2),
                    pct=-0.4, notional=50.0)
    await insert_buy_filled(pool, b, symbol="DOGE/USDT", cost_usd="50.0", fee_ccy="USDT",
                            fee="0.05", avg_price="0.1", filled_at=AS_OF - timedelta(days=3))
    await insert_sell_filled(pool, b, symbol="DOGE/USDT", cost_usd="49.6", fee="0.0496",
                             fee_ccy="USDT", filled_at=AS_OF - timedelta(days=2))
    await pool.execute("UPDATE demo_orders SET slippage_pct=0.10, lag_sec=4 "
                       "WHERE position_id=$1 AND leg='buy';", a)
    await pool.execute("UPDATE demo_orders SET slippage_pct=0.30, lag_sec=8 "
                       "WHERE position_id=$1 AND leg='buy';", b)
    await pool.execute("UPDATE demo_orders SET slippage_pct=-0.20, lag_sec=2 "
                       "WHERE leg='sell';")
    await pool.execute("UPDATE demo_orders SET sizing_rule='mm1', requested_cost_usd=50 "
                       "WHERE leg='buy';")
    # пропущенные покупки
    for reason in ("reserve", "reserve", "token_cap"):
        pid = await mkpos(pool, "BTC/USDT", AS_OF - timedelta(hours=1), status="open")
        inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol='BTC/USDT';")
        await pool.execute(
            "INSERT INTO demo_orders (position_id, instrument_id, leg, status, skip_reason, "
            "cl_ord_id, symbol, host, virtual_ts) VALUES ($1, $2, 'buy', 'skipped', $3, $4, "
            "'BTC/USDT', 'www.okx.com', $5);", pid, inst["id"], reason, f"sk{pid}", AS_OF)
    return [a, b]


async def assert_09(pool, out: str) -> None:
    head = out.split("Пропуски покупок")[0]
    r = record(head)
    pairs = await ledger.fetch_pairs(pool)
    assert len(pairs) == 2
    profits = [ledger.pair_profit(p) for p in pairs]
    pcts = [100 * ledger.pair_profit(p) / ledger.pair_cost(p) for p in pairs]
    assert r["Пар (покупка и продажа исполнены)"] == 2
    assert r["Сумма прибыли демо, $"] == pytest.approx(float(sum(profits)), abs=1e-6)
    assert r["Средний итог демо, %"] == pytest.approx(float(sum(pcts) / 2), abs=1e-6)
    assert r["Средний итог виртуальный, %"] == pytest.approx((0.8 - 0.4) / 2, abs=1e-6)
    assert r["Разница демо − виртуальный, п.п."] == pytest.approx(
        float(sum(pcts) / 2) - 0.2, abs=1e-6)
    assert r["Проскальзывание покупки ср., %"] == pytest.approx(0.2, abs=1e-6)
    assert r["Проскальзывание покупки мед., %"] == pytest.approx(0.2, abs=1e-6)
    assert r["Проскальзывание продажи ср., %"] == pytest.approx(-0.2, abs=1e-6)
    assert r["Задержка покупки ср., с"] == pytest.approx(6.0, abs=1e-6)
    assert r["Задержка продажи ср., с"] == pytest.approx(2.0, abs=1e-6)


def run_09(db: str, text: str | None = None) -> str:
    return run_sql(db, SQL_DIR / "09_demo_vs_virtual.sql", extra=("prec=8",), text=text)


# --------------------------------------------------------------------------
# §6.1–6.5. Расчёты на синтетических данных
# --------------------------------------------------------------------------


async def test_01_profile_matches_the_hand_calculation(pool, db_name) -> None:
    ids = await seed_01(pool)
    assert_01(run_sql(db_name, SQL_DIR / "01_profile_closed.sql"), ids)


async def test_01_names_the_best_and_the_worst_trade(pool, db_name) -> None:
    ids = await seed_01(pool)
    out = run_sql(db_name, SQL_DIR / "01_profile_closed.sql")
    assert f"Лучшая сделка, %|1.00\n  позиция №|{ids['w1']}" in out
    assert f"Худшая сделка, %|-3.00\n  позиция №|{ids['l3']}" in out


async def test_02_open_positions_use_the_last_closed_minute_candle(pool, db_name) -> None:
    await seed_02(pool)
    assert_02(run_sql(db_name, SQL_DIR / "02_open_mtm.sql"))


async def test_06_time_in_market_matches_the_hand_calculation(pool, db_name) -> None:
    await seed_06(pool)
    assert_06(run_with_as_of(db_name, "06_time_in_market.sql", AS_OF_06))


def run_with_as_of(db: str, name: str, as_of: datetime, *, text: str | None = None) -> str:
    stamp = as_of.strftime("%Y-%m-%d %H:%M:%S+00")
    res = subprocess.run(
        ["psql", "-X", "-q", "-A", "-F", "|", "-h", HOST, "-p", PORT, "-U", "agenttrade_ro",
         "-d", db, "-v", "ON_ERROR_STOP=1", "-v", f"as_of={stamp}"],
        input=text if text is not None else sql_of(name), capture_output=True, text=True,
        check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


async def test_07_a_steady_one_percent_a_day_gives_zero_difference(pool, db_name) -> None:
    await seed_07(pool)
    assert_07(run_with_as_of(db_name, "07_entry_vs_random.sql", AS_OF_07))


async def test_09_demo_pairs_agree_with_the_ledger(pool, db_name) -> None:
    await seed_09(pool)
    await assert_09(pool, run_09(db_name))


async def test_09_counts_skips_and_the_mm1_starting_point(pool, db_name) -> None:
    await seed_09(pool)
    out = run_09(db_name)
    skips = out.split("Пропуски покупок за всё время (status = skipped):")[1].split("Размер")[0]
    rows = table(skips)
    assert {(r["Причина пропуска"], r["Число"]) for r in rows} == {("reserve", 2.0),
                                                                    ("token_cap", 1.0)}
    mm1 = record(out.split("точка отсчёта Этапа 9.6):")[1])
    assert mm1["Строк покупки с sizing_rule = mm1"] == 2
    assert mm1["из них исполнено"] == 2
    assert mm1["Средняя заявленная сумма, $"] == 50.0


async def test_the_other_blocks_work_on_the_basic_scenario(pool, db_name) -> None:
    await seed_01(pool)
    await seed_02(pool)
    for name in ("00_scope.sql", "03_by_token.sql", "04_exits_and_time.sql", "05_mae_mfe.sql",
                 "08_weekly.sql"):
        assert run_sql(db_name, SQL_DIR / name).strip(), name
    scope = record(run_sql(db_name, SQL_DIR / "00_scope.sql"))
    assert scope["Позиций всего"] == 9 and scope["Закрыто (в метриках)"] == 6
    assert scope["Закрыто по data_gap (вне метрик)"] == 1 and scope["Открыто сейчас"] == 2
    exits = {r["Выход"]: r for r in table(run_sql(db_name, SQL_DIR / "04_exits_and_time.sql"))}
    assert exits["target"]["Число"] == 5 and exits["plus_exit"]["Число"] == 1
    assert "data_gap" not in exits


@pytest.mark.parametrize("name", BLOCK_FILES)
async def test_every_block_survives_an_empty_selection(pool, db_name, name) -> None:
    """Нет позиций версии 7 (первый день замера): блок печатает пустую таблицу, не ошибку."""
    assert run_sql(db_name, SQL_DIR / name, extra=("prec=2",)) is not None


async def test_a_logic_version_other_than_7_never_enters_a_block(pool, db_name) -> None:
    await mkpos(pool, "BTC/USDT", H0, closed=H0 + timedelta(hours=3), pct=5.0, logic=6)
    scope = record(run_sql(db_name, SQL_DIR / "00_scope.sql"))
    assert scope["Позиций всего"] == 0


# --------------------------------------------------------------------------
# §6.7. Мутации: каждая обязана ронять проверку
# --------------------------------------------------------------------------


def mutate(name: str, old: str, new: str) -> str:
    text = sql_of(name)
    assert old in text, f"мутации нечего менять в {name}: {old!r}"
    return text.replace(old, new)


async def test_mutation_data_gap_in_the_metrics_is_caught(pool, db_name) -> None:
    ids = await seed_01(pool)
    mutated = mutate("01_profile_closed.sql", "exit_reason IS DISTINCT FROM 'data_gap'", "TRUE")
    out = run_sql(db_name, "", text=mutated)
    with pytest.raises(AssertionError):
        assert_01(out, ids)


async def test_mutation_open_positions_at_the_entry_price_is_caught(pool, db_name) -> None:
    await seed_02(pool)
    mutated = mutate("02_open_mtm.sql", "px.close::numeric / v.entry_price",
                     "v.entry_price::numeric / v.entry_price")
    out = run_sql(db_name, "", text=mutated)
    with pytest.raises(AssertionError):
        assert_02(out)


async def test_mutation_23_hours_instead_of_24_is_caught(pool, db_name) -> None:
    await seed_07(pool)
    mutated = mutate("07_entry_vs_random.sql", "v7.opened_at + interval '24 hours'",
                     "v7.opened_at + interval '23 hours'")
    out = run_with_as_of(db_name, "", AS_OF_07, text=mutated)
    with pytest.raises(AssertionError):
        assert_07(out)


async def test_mutation_buy_fee_in_usdt_not_subtracted_is_caught(pool, db_name) -> None:
    await seed_09(pool)
    mutated = mutate("09_demo_vs_virtual.sql", "(s_cost - s_fee_usdt) - (b_cost + b_fee_usdt)",
                     "(s_cost - s_fee_usdt) - b_cost")
    out = run_09(db_name, mutated)
    with pytest.raises(AssertionError):
        await assert_09(pool, out)


async def test_mutation_in_the_shared_block_of_06_is_caught(pool, db_name) -> None:
    await seed_06(pool)
    mutated = mutate("06_time_in_market.sql", "v.opened_at <= g.h", "v.opened_at < g.h")
    out = run_with_as_of(db_name, "", AS_OF_06, text=mutated)
    with pytest.raises(AssertionError):
        assert_06(out)


# --------------------------------------------------------------------------
# Свойства файлов
# --------------------------------------------------------------------------


def _common(text: str) -> str:
    start = text.split("-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ", 1)[1]
    return start.split("-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ===")[0]


def test_all_ten_block_files_exist() -> None:
    assert sorted(p.name for p in SQL_DIR.glob("*.sql")) == sorted(BLOCK_FILES)
    assert (ROOT / "analysis" / "REPORT_9_9.md").exists()


def test_the_shared_definitions_are_identical_in_every_block() -> None:
    """Выборка, «закрытые» и «открытые» определены в каждом файле и обязаны совпадать побайтно."""
    texts = {n: sql_of(n) for n in BLOCK_FILES if not n.startswith("09")}
    shared = {n: _common(t) for n, t in texts.items()}
    assert len(set(shared.values())) == 1, "определения в файлах блоков разошлись"
    only = next(iter(shared.values()))
    for must in ("logic_version = 7", "is_virtual", "IS DISTINCT FROM 'data_gap'",
                 "0.0001", "interval '10 minutes'", "interval '1 minute'"):
        assert must in only, must


def test_every_block_forces_a_read_only_session() -> None:
    for name in BLOCK_FILES:
        code = "\n".join(ln for ln in sql_of(name).splitlines() if not ln.lstrip().startswith("--"))
        first_query = re.search(r"^(WITH|SELECT) ", code, re.M).start()
        marker = code.find("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
        assert 0 <= marker < first_query, name


def test_the_blocks_contain_no_statement_that_changes_anything() -> None:
    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE|"
        r"REINDEX|REFRESH|LOCK|CALL|DO)\b", re.I)
    for name in BLOCK_FILES:
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in sql_of(name).splitlines())
        code = re.sub(r"'[^']*'", "''", code)
        code = re.sub(r"SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", "", code)
        assert not forbidden.search(code), (name, forbidden.search(code).group(0))


async def test_a_write_in_the_read_only_session_is_refused(db_name) -> None:
    """Страховка из шапки файла работает: после неё даже владелец базы не может записать."""
    res = subprocess.run(
        ["psql", "-X", "-q", "-h", HOST, "-p", PORT, "-U", USER, "-d", db_name,
         "-v", "ON_ERROR_STOP=1"],
        input="SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;\n"
              "INSERT INTO instruments (exchange,symbol,base,quote,type) "
              "VALUES ('okx','ZZZ/USDT','ZZZ','USDT','spot');\n",
        capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "read-only" in res.stderr


async def test_the_reader_role_cannot_write(db_name) -> None:
    res = subprocess.run(
        ["psql", "-X", "-q", "-h", HOST, "-p", PORT, "-U", "agenttrade_ro", "-d", db_name,
         "-c", "INSERT INTO instruments (exchange,symbol,base,quote,type) "
               "VALUES ('okx','ZZZ/USDT','ZZZ','USDT','spot');"],
        capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "permission denied" in res.stderr


# --------------------------------------------------------------------------
# Заглушка docker и запуск скриптов целиком
# --------------------------------------------------------------------------


def make_stub(tmp_path: Path, db: str, *, ro_password: str | None = None) -> dict:
    """Каталог приложения с .env и ``docker``, который запускает psql против тестовой базы."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    lines = ["POSTGRES_DB=agenttrade"]
    if ro_password:
        lines.append(f"POSTGRES_RO_PASSWORD={ro_password}")
    (app / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    docker = bindir / "docker"
    docker.write_text(f"""#!/usr/bin/env bash
if [ "$1" = compose ] && [ "$2" = version ]; then exit 0; fi
args=("$@"); i=0
while [ "${{args[$i]}}" != psql ]; do
  if [ "${{args[$i]}}" = -e ]; then export "${{args[$((i+1))]}}"; i=$((i+2)); else i=$((i+1)); fi
done
rest=("${{args[@]:$((i+1))}}"); out=(); j=0; mode=socket; has_c=0
while [ $j -lt ${{#rest[@]}} ]; do
  case "${{rest[$j]}}" in
    -d) out+=(-d "{db}"); j=$((j+2)) ;;
    -h) mode=passwd; j=$((j+2)) ;;
    -c) has_c=1; out+=("${{rest[$j]}}"); j=$((j+1)) ;;
    *) out+=("${{rest[$j]}}"); j=$((j+1)) ;;
  esac
done
if [ "$mode" = socket ] && [ -n "${{STUB_SOCKET_FAIL:-}}" ]; then
  echo "psql: socket trust disabled" >&2; exit 2
fi
if [ "$has_c" = 0 ] && [ -n "${{STUB_LEAK:-}}" ]; then
  echo "psql: warning: connection string password=$STUB_LEAK"
fi
exec psql -h "{HOST}" -p "{PORT}" "${{out[@]}}"
""", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return {"app": app, "bin": bindir}


def run_script(script: Path, stub: dict, tmp_path: Path, *, env_extra=None, args=()):
    env = {"PATH": f"{stub['bin']}:{os.environ['PATH']}", "APP_DIR": str(stub["app"]),
           "OUT_DIR": str(tmp_path / "out"), "HOME": str(tmp_path), "AS_OF": AS_OF_TXT}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True,
                          check=False)


def fingerprint(db: str) -> str:
    """Число строк и контрольная сумма таблиц, в которые скрипт мог бы записать."""
    return psql_raw(
        db, "SELECT (SELECT count(*) FROM positions) || '/' || "
            "(SELECT count(*) FROM demo_orders) || '/' || (SELECT count(*) FROM ohlcv) || '/' || "
            "(SELECT md5(string_agg(p::text, ',' ORDER BY id)) FROM positions p);")


async def bulk(pool) -> None:
    """~60 позиций, пять монет, шесть недель: типичный объём для проверки длины отчёта."""
    inst = [("ETH/USDT", "ETH"), ("SOL/USDT", "SOL"), ("XRP/USDT", "XRP")]
    for sym, base in inst:
        await pool.execute(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', $1, $2, 'USDT', 'spot');", sym, base)
    syms = ["BTC/USDT", "DOGE/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
    start = AS_OF - timedelta(days=40)
    for k in range(60):
        o = start + timedelta(hours=15 * k + 1)
        s = syms[k % 5]
        if k < 50:
            await mkpos(pool, s, o, closed=o + timedelta(hours=10 + k % 40),
                        reason=("target", "plus_exit", "timeout")[k % 3],
                        pct=(1.0, 0.0, -2.0, 0.4)[k % 4])
        elif k < 53:
            await mkpos(pool, s, o, reason="data_gap", closed=o + timedelta(hours=50), pct=-1.0)
        else:
            await mkpos(pool, s, o, status="open")
    for s in syms:
        await candle(pool, s, AS_OF - timedelta(minutes=3), 99.0)


@needs_bash
async def test_run_9_9_runs_all_ten_blocks_in_a_read_only_report(pool, db_name, tmp_path) -> None:
    await bulk(pool)
    await seed_09(pool)
    stub = make_stub(tmp_path, db_name)
    before = fingerprint(db_name)
    res = run_script(RUN_9_9, stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    report = (tmp_path / "out" / f"report_9_9_{datetime.now(UTC):%Y%m%d}.txt").read_text("utf-8")
    for head in ("00 — ОХВАТ", "01 — ПРОФИЛЬ", "02 — ОТКРЫТЫЕ", "03 — ПО МОНЕТАМ",
                 "04 — ВЫХОДЫ", "05 — ХОД ЦЕНЫ", "06 — ВРЕМЯ В РЫНКЕ",
                 "07 — ВХОД СИСТЕМЫ", "08 — ПО НЕДЕЛЯМ", "09 — ДЕМО ПРОТИВ"):
        assert f"===== {head}" in report, head
    assert "с ошибками 0" in report and "ОШИБКА" not in report
    assert len(report.splitlines()) <= 300, len(report.splitlines())
    assert "Вывод без интерпретации: разница средних" in report
    assert "блоков 10" in report
    after = fingerprint(db_name)
    assert before == after            # скрипт ничего не записал


@needs_bash
async def test_run_9_9_never_prints_the_password(pool, db_name, tmp_path) -> None:
    await seed_01(pool)
    secret = "Zq9$w\\x7!pass&word"
    stub = make_stub(tmp_path, db_name, ro_password=secret)
    # заглушка добавляет в вывод каждого блока строку с паролем — как сделал бы текст ошибки psql
    res = run_script(RUN_9_9, stub, tmp_path, env_extra={"STUB_LEAK": secret})
    report = (tmp_path / "out" / f"report_9_9_{datetime.now(UTC):%Y%m%d}.txt").read_text("utf-8")
    assert res.returncode == 0, res.stdout + res.stderr
    assert secret not in res.stdout and secret not in res.stderr and secret not in report
    assert "password=***" in report


@needs_bash
async def test_run_9_9_falls_back_to_the_password_connection(pool, db_name, tmp_path) -> None:
    await seed_01(pool)
    stub = make_stub(tmp_path, db_name, ro_password="s3cret")
    res = run_script(RUN_9_9, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "127.0.0.1 с паролем роли" in res.stdout
    assert "s3cret" not in res.stdout


@needs_bash
async def test_run_9_9_reports_a_failed_block_and_exits_nonzero(pool, db_name, tmp_path) -> None:
    sqldir = tmp_path / "analysis"
    shutil.copytree(ROOT / "analysis", sqldir)
    (sqldir / "sql" / "9_9" / "03_by_token.sql").write_text(
        sql_of("03_by_token.sql").replace("FROM closed", "FROM no_such_table", 1), "utf-8")
    stub = make_stub(tmp_path, db_name)
    res = run_script(sqldir / "run_9_9.sh", stub, tmp_path)
    assert res.returncode != 0
    assert "с ошибками 1" in res.stdout and "ОШИБКА: блок «03" in res.stdout


@needs_bash
async def test_run_9_9_without_a_connection_says_what_to_check(pool, db_name, tmp_path) -> None:
    stub = make_stub(tmp_path, db_name)
    res = run_script(RUN_9_9, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode != 0
    assert "не удалось подключиться к БД ролью agenttrade_ro" in res.stdout


# --------------------------------------------------------------------------
# run_7_1.sh после выноса lib_ro.sh даёт тот же вывод, что до
# --------------------------------------------------------------------------


def _normalize_7_1(text: str, tmp_path: Path) -> str:
    """Убирает из вывода то, что отличается между двумя запусками само по себе: даты запуска,
    путь к каталогу и единственное место, где расчёт печатает now() (блок 00, server_time_utc)."""
    out, skip = [], 0
    for line in text.splitlines():
        if skip:
            skip -= 1
            continue
        if "server_time_utc" in line:
            skip = 2                   # заголовок таблицы, разделитель и строка со временем
            continue
        if any(k in line for k in ("Дата запуска", "Время окончания", "Запросы:", "Готово. Отчёт:",
                                   "Вывод получился")):
            continue
        out.append(line.replace(str(tmp_path), "<TMP>"))
    return "\n".join(out)


@needs_bash
async def test_run_7_1_gives_the_same_output_before_and_after_the_extraction(
    pool, db_name, tmp_path
) -> None:
    await seed_01(pool)
    old_dir = tmp_path / "old" / "analysis"
    old_dir.mkdir(parents=True)
    shutil.copy(GOLDEN_7_1, old_dir / "run_7_1.sh")
    os.symlink(ROOT / "analysis" / "sql", old_dir / "sql")
    stub = make_stub(tmp_path, db_name, ro_password="old_new_pw")
    runs = {}
    for label, script in (("old", old_dir / "run_7_1.sh"), ("new", RUN_7_1)):
        out_dir = tmp_path / f"out_{label}"
        env = {"OUT_DIR": str(out_dir), "TARGET_LOGIC_VERSION": "7"}
        res = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, check=False,
            env={"PATH": f"{stub['bin']}:{os.environ['PATH']}", "APP_DIR": str(stub["app"]),
                 "HOME": str(tmp_path), **env})
        runs[label] = (res.returncode, _normalize_7_1(res.stdout + res.stderr, tmp_path))
    assert runs["old"][0] == runs["new"][0]
    assert runs["old"][1] == runs["new"][1]
    assert "РАСЧЁТ 7" in runs["new"][1]


@needs_bash
def test_run_7_1_sources_the_shared_library_and_has_no_copy_of_it() -> None:
    body = RUN_7_1.read_text("utf-8")
    assert 'source "$LIB_DIR/lib_ro.sh"' in body
    assert "redact() {" not in body and "psql_socket() {" not in body
    lib = LIB_RO.read_text("utf-8")
    for fn in ("redact()", "psql_socket()", "psql_passwd()", "detect_conn_mode()", "ro_init()"):
        assert fn in lib, fn
    assert "agenttrade_ro" in lib


@needs_bash
def test_the_new_shell_scripts_pass_bash_n() -> None:
    for path in (LIB_RO, RUN_7_1, RUN_9_9, LIB_VERIFY, ROOT / "deploy" / "verify_9_6.sh",
                 ROOT / "deploy" / "verify_9_3.sh"):
        res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True,
                             check=False)
        assert res.returncode == 0, (path.name, res.stderr)


def test_the_stage_touches_only_analysis_and_deploy_and_tests() -> None:
    """Источники продукта не меняются; версия логики прежняя."""
    from tests.conftest import CURRENT_LOGIC_VERSION

    assert CURRENT_LOGIC_VERSION == 7
    body = RUN_9_9.read_text("utf-8")
    for forbidden in ("docker compose restart", "docker compose up", "docker compose build",
                      "git pull", "pip install"):
        assert forbidden not in body


# --------------------------------------------------------------------------
# §7. Скрипты проверки находят строку запуска в обоих форматах
# --------------------------------------------------------------------------

JSON_LINE = (
    '{"enabled": true, "host": "www.okx.com", "mm_enabled": true, "mm_reserve_pct": "25.0", '
    '"mm_step_pct": "5.0", "mm_token_cap_pct": "15.0", "mm_min_order_usd": "5.0", '
    '"event": "Сервис demo запущен (демо-счёт OKX, спот)", "timestamp": "2026-10-18T09:12:44Z"}')
CONSOLE_LINE = (
    "demo-1  | 2026-10-18T09:12:44.512345Z [info     ] Сервис demo запущен (демо-счёт OKX, спот) "
    "[demo] enabled=True host=www.okx.com interval=30 mm_enabled=True mm_reserve_pct=25.0 "
    "mm_step_pct=5.0 mm_token_cap_pct=15.0 mm_min_order_usd=5.0 instruments=['BTC/USDT']")
CONSOLE_COLORED = CONSOLE_LINE.replace(
    "mm_enabled=True", "\x1b[36mmm_enabled\x1b[0m=\x1b[35mTrue\x1b[0m")


def _lib(*calls: str) -> str:
    res = subprocess.run(["bash", "-c", f'source "{LIB_VERIFY}"\n' + "\n".join(calls)],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


@needs_bash
def test_the_helper_reads_the_same_values_from_both_formats() -> None:
    keys = ("mm_enabled", "mm_reserve_pct", "mm_step_pct", "mm_token_cap_pct", "mm_min_order_usd")
    got = {}
    for label, line in (("json", JSON_LINE), ("console", CONSOLE_LINE),
                        ("colored", CONSOLE_COLORED)):
        calls = ['L="$(startup_line "$LOGS" mm_enabled)"']
        calls += [f'echo "{k}=$(startup_val "$L" {k})"' for k in keys]
        calls.append('echo "ts=$(startup_ts "$L")"')
        res = subprocess.run(
            ["bash", "-c", f'source "{LIB_VERIFY}"\n' + "\n".join(calls)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "LOGS": "старая строка\n" + line + "\nхвост журнала"})
        assert res.returncode == 0, res.stderr
        got[label] = res.stdout.splitlines()
    assert got["json"][:5] == got["console"][:5] == got["colored"][:5] == [
        "mm_enabled=true", "mm_reserve_pct=25.0", "mm_step_pct=5.0",
        "mm_token_cap_pct=15.0", "mm_min_order_usd=5.0"]
    assert got["json"][5] == "ts=2026-10-18T09:12:44Z"
    assert got["console"][5] == "ts=2026-10-18T09:12:44.512345Z"


@needs_bash
def test_the_helper_takes_the_last_line_and_compares_numbers_as_numbers() -> None:
    out = _lib(
        'LOGS=$\'mm_enabled=False mm_step_pct=1\\nmm_enabled=True mm_step_pct=5\'',
        'L="$(startup_line "$LOGS" mm_enabled)"',
        'echo "last=$(startup_val "$L" mm_step_pct)"',
        'startup_eq "$L" mm_step_pct 5.0 && echo num-equal',
        'startup_eq "$L" mm_enabled false || echo bool-differs',
        'startup_eq "$L" no_such_key 1 || echo missing-is-not-equal',
        'echo "none=[$(startup_line "ничего похожего" mm_enabled)]"')
    assert "last=5" in out and "num-equal" in out and "bool-differs" in out
    assert "missing-is-not-equal" in out and "none=[]" in out


def _verify_stub(tmp_path: Path, db: str, logs: dict[str, str]) -> dict:
    app = tmp_path / "vapp"
    app.mkdir(exist_ok=True)
    bindir = tmp_path / "vbin"
    bindir.mkdir(exist_ok=True)
    for name, text in logs.items():
        (tmp_path / f"{name}.log").write_text(text + "\n", encoding="utf-8")
    cases = "\n".join(
        f'  *"compose logs"*{name}*) cat "{tmp_path}/{name}.log" ;;' for name in logs)
    docker = bindir / "docker"
    docker.write_text(f"""#!/usr/bin/env bash
case "$*" in
  *" psql "*) sql="${{@: -1}}"
    exec psql -h "{HOST}" -p "{PORT}" -U "{USER}" -d "{db}" -tA -c "$sql" ;;
  *"ps --format"*) echo running ;;
{cases}
esac
""", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (app / ".env").write_text(
        "DEMO_MM_ENABLED=true\nDEMO_MM_RESERVE_PCT=25\nDEMO_MM_STEP_PCT=5\n"
        "DEMO_MM_TOKEN_CAP_PCT=15.0   # потолок\nDEMO_MM_MIN_ORDER_USD=5\n", encoding="utf-8")
    return {"app": app, "bin": bindir}


@needs_bash
@pytest.mark.parametrize("line", [JSON_LINE, CONSOLE_LINE, CONSOLE_COLORED],
                         ids=["json", "console", "console-colored"])
async def test_verify_9_6_item_3_is_green_on_either_log_format(
    pool, db_name, tmp_path, line
) -> None:
    stub = _verify_stub(tmp_path, db_name, {"demo": line})
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "verify_9_6.sh")], capture_output=True, text=True,
        check=False, env={"PATH": f"{stub['bin']}:{os.environ['PATH']}",
                          "APP_DIR": str(stub["app"]), "HOME": str(tmp_path)})
    assert "✅ все пять величин в журнале совпадают с .env" in res.stdout, res.stdout
    assert "НЕ ПРОВЕРЕНО: строки запуска" not in res.stdout


@needs_bash
async def test_verify_9_6_item_3_still_finds_a_real_mismatch(pool, db_name, tmp_path) -> None:
    stub = _verify_stub(tmp_path, db_name, {"demo": CONSOLE_LINE.replace(
        "mm_reserve_pct=25.0", "mm_reserve_pct=10.0")})
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "verify_9_6.sh")], capture_output=True, text=True,
        check=False, env={"PATH": f"{stub['bin']}:{os.environ['PATH']}",
                          "APP_DIR": str(stub["app"]), "HOME": str(tmp_path)})
    assert "❌ расходятся с .env: DEMO_MM_RESERVE_PCT" in res.stdout, res.stdout


POS_JSON = ('{"logic_version": 7, "max_open": 0, "slot_usd": 2, "budget_usd": 0, '
            '"min_probability": 0.8, "max_hold_hours": 48, "plus_wait_start_hours": 24, '
            '"cooldown_sec": 3600, "one_per_token": false, "event": "start"}')
POS_CONSOLE = ("2026-10-18T09:00:00Z [info     ] positions started logic_version=7 max_open=0 "
               "slot_usd=2.0 budget_usd=0.0 min_probability=0.8 max_hold_hours=48 "
               "plus_wait_start_hours=24 cooldown_sec=3600 one_per_token=False")
NOTE_JSON = ('{"notify_signals_enabled": false, "notify_trades_enabled": true, '
             '"notify_daily_summary_utc": "06:00", "notify_trades_max_per_hour": 0}')
NOTE_CONSOLE = ("2026-10-18T09:00:00Z [info     ] notify started notify_signals_enabled=False "
                "notify_trades_enabled=True notify_daily_summary_utc='06:00' "
                "notify_trades_max_per_hour=0")


@needs_bash
@pytest.mark.parametrize("pos,note", [(POS_JSON, NOTE_JSON), (POS_CONSOLE, NOTE_CONSOLE)],
                         ids=["json", "console"])
async def test_verify_9_3_startup_lines_are_found_in_either_format(
    pool, db_name, tmp_path, pos, note
) -> None:
    stub = _verify_stub(tmp_path, db_name, {"positions": pos, "notify": note})
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "verify_9_3.sh")], capture_output=True, text=True,
        check=False, env={"PATH": f"{stub['bin']}:{os.environ['PATH']}",
                          "APP_DIR": str(stub["app"]), "HOME": str(tmp_path)})
    assert "🟢 positions называет все девять величин §8" in res.stdout, res.stdout
    assert "🟢 notify называет все четыре величины §8" in res.stdout, res.stdout


@needs_bash
async def test_verify_9_3_still_flags_a_wrong_startup_value(pool, db_name, tmp_path) -> None:
    stub = _verify_stub(tmp_path, db_name, {
        "positions": POS_CONSOLE.replace("max_open=0", "max_open=5"), "notify": NOTE_CONSOLE})
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "verify_9_3.sh")], capture_output=True, text=True,
        check=False, env={"PATH": f"{stub['bin']}:{os.environ['PATH']}",
                          "APP_DIR": str(stub["app"]), "HOME": str(tmp_path)})
    assert "в строке запуска positions нет max_open=0" in res.stdout, res.stdout


def test_verify_scripts_use_the_shared_helper_not_a_json_only_pattern() -> None:
    for name in ("verify_9_6.sh", "verify_9_3.sh"):
        body = (ROOT / "deploy" / name).read_text("utf-8")
        assert "lib_verify.sh" in body and "startup_line" in body, name
    assert "grep -E '\"mm_enabled\"'" not in (ROOT / "deploy" / "verify_9_6.sh").read_text("utf-8")


# --------------------------------------------------------------------------
# Доработка 1. startup_ts на консольной строке сервера; взвешенная строка блока 07
# --------------------------------------------------------------------------

SERVER_LINE = (
    "demo-1  | 2026-10-18 20:39:35 [info     ] Сервис demo запущен (демо-счёт OKX, спот) "
    "component=demo enabled=True host=www.okx.com mm_enabled=True mm_min_order_usd=5.0 "
    "mm_reserve_pct=25.0 mm_step_pct=5.0 mm_token_cap_pct=15.0 notify_enabled=True")


@needs_bash
def test_startup_ts_reads_the_real_server_line_as_utc() -> None:
    """Консольный structlog пишет пробел между датой и временем и не пишет пояс."""
    res = subprocess.run(
        ["bash", "-c", f'source "{LIB_VERIFY}"\nstartup_ts "$L"'], capture_output=True,
        text=True, check=False, env={**os.environ, "L": SERVER_LINE})
    assert res.stdout == "2026-10-18 20:39:35+00", res.stdout
    # и PostgreSQL принимает это как момент в UTC
    got = psql_raw("postgres", f"SELECT '{res.stdout}'::timestamptz AT TIME ZONE 'UTC';")
    assert got == "2026-10-18 20:39:35"


@needs_bash
@pytest.mark.parametrize("line,want", [
    ("2026-10-18T09:12:44.512345Z [info] x", "2026-10-18T09:12:44.512345Z"),
    ("2026-10-18T09:12:44 [info] x", "2026-10-18T09:12:44+00"),
    ("2026-10-18 09:12:44.5+03:00 [info] x", "2026-10-18 09:12:44.5+03:00"),
    ('{"timestamp": "2026-10-18 09:12:44", "a": 1}', "2026-10-18 09:12:44+00"),
    ("строка без времени", ""),
], ids=["t-z", "t-naive", "space-offset", "json-space", "none"])
def test_startup_ts_forms(line, want) -> None:
    res = subprocess.run(
        ["bash", "-c", f'source "{LIB_VERIFY}"\nstartup_ts "$L"'], capture_output=True,
        text=True, check=False, env={**os.environ, "L": line})
    assert res.stdout == want


@needs_bash
async def test_verify_9_6_window_starts_at_the_service_launch(pool, db_name, tmp_path) -> None:
    """Старая покупка (до запуска, sizing_rule пуст) в окно не попадает: п. 3 ✅ и п. 4 ✅."""
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    line = SERVER_LINE.replace("2026-10-18 20:39:35", started.strftime("%Y-%m-%d %H:%M:%S"))
    old = await mkpos(pool, "BTC/USDT", started - timedelta(hours=2), status="open")
    new = await mkpos(pool, "DOGE/USDT", started + timedelta(minutes=5), status="open")
    await insert_buy_filled(pool, old, cost_usd="2.0")
    await insert_buy_filled(pool, new, symbol="DOGE/USDT", cost_usd="50.0", avg_price="0.1")
    await pool.execute("UPDATE demo_orders SET created_at=$1 WHERE position_id=$2;",
                       started - timedelta(hours=1), old)
    await pool.execute(
        "UPDATE demo_orders SET created_at=$1, sizing_rule='mm1', sizing_equity_usd=1000, "
        "sizing_exposure_usd=0, requested_cost_usd=50 WHERE position_id=$2;",
        started + timedelta(minutes=10), new)
    stub = _verify_stub(tmp_path, db_name, {"demo": line})
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "verify_9_6.sh")], capture_output=True, text=True,
        check=False, env={"PATH": f"{stub['bin']}:{os.environ['PATH']}",
                          "APP_DIR": str(stub["app"]), "HOME": str(tmp_path)})
    out = res.stdout
    assert "✅ все пять величин в журнале совпадают с .env" in out, out
    assert f"(запуск {started:%Y-%m-%d %H:%M:%S}+00)" in out, out
    assert "✅ у всех 1 новых строк sizing_rule='mm1'" in out, out
    assert "❌ у " not in out, out


def test_verify_9_3_does_not_depend_on_the_launch_time() -> None:
    """verify_9_3.sh берёт из строки запуска только величины, время ему не нужно."""
    assert "startup_ts" not in (ROOT / "deploy" / "verify_9_3.sh").read_text("utf-8")


async def seed_07_weighted(pool) -> None:
    """Две монеты: BTC — 3 входа, безусловная +1 %; DOGE — 1 вход, безусловная −1 %."""
    def down(k: float) -> float:
        return 100.0 * 0.99 ** (k / 24.0)

    for k in range(0, 41):
        await candle(pool, "BTC/USDT", H0 + timedelta(hours=k), _px(k), tf="1h")
        await candle(pool, "DOGE/USDT", H0 + timedelta(hours=k), down(k), tf="1h")
    for k in (1, 2, 3):
        o = H0 + timedelta(hours=k)
        await mkpos(pool, "BTC/USDT", o, closed=o + timedelta(hours=3), entry=_px(k) / 1.01)
        await candle(pool, "BTC/USDT", o + timedelta(hours=24), _px(k + 24))
    o = H0 + timedelta(hours=1)
    await mkpos(pool, "DOGE/USDT", o, closed=o + timedelta(hours=3), entry=down(1))
    await candle(pool, "DOGE/USDT", o + timedelta(hours=24), down(25))


async def test_07_weights_the_random_return_by_entries_per_coin(pool, db_name) -> None:
    await seed_07_weighted(pool)
    out = run_with_as_of(db_name, "07_entry_vs_random.sql", AS_OF_07)
    rows = {r["Монета"]: r for r in table(out.split("\n\nВывод")[0])}
    assert rows["BTC/USDT"]["Входов"] == 3 and rows["DOGE/USDT"]["Входов"] == 1
    assert rows["BTC/USDT"]["Сл. ср., %"] == pytest.approx(1.0, abs=0.01)
    assert rows["DOGE/USDT"]["Сл. ср., %"] == pytest.approx(-1.0, abs=0.01)
    # без весов (все часы всех монет поровну) было бы 0; с весами (3 × 1 + 1 × (−1)) / 4 = 0,5
    assert rows["ВСЕ"]["Сл. ср., %"] == pytest.approx(0.0, abs=0.01)
    line = next(ln for ln in out.splitlines() if ln.startswith("Случайная доходность"))
    m = re.fullmatch(
        r"Случайная доходность, взвешенная по числу входов в монету = (-?[\d.]+) %; "
        r"разница средних со взвешиванием = (-?[\d.]+) п\.п\.", line)
    assert m, line
    assert float(m.group(1)) == pytest.approx(0.5, abs=0.005)
    # (3 × 2,01 + 1 × (−1)) / 4 = 1,2575 — средний вход; минус 0,5
    assert float(m.group(2)) == pytest.approx(rows["ВСЕ"]["Вх. ср., %"] - 0.5, abs=0.011)
    assert "Вывод без интерпретации: разница средних = " in out        # прежняя строка осталась


async def test_07_weighted_line_without_entries_says_no_data(pool, db_name) -> None:
    out = run_with_as_of(db_name, "07_entry_vs_random.sql", AS_OF_07)
    assert ("Случайная доходность, взвешенная по числу входов в монету = нет данных %; "
            "разница средних со взвешиванием = нет данных п.п.") in out
