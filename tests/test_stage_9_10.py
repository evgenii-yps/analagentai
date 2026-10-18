"""Этап 9.10: замер «вето на покупку при нисходящем тренде BTC» и полнота минутных свечей.

SQL блоков проверяется на НАСТОЯЩЕЙ PostgreSQL с синтетикой, ответ которой известен заранее:
либо посчитан руками (в комментариях к сценариям), либо независимой реализацией правил ТЗ на
чистом Python (``ref_*``) — она написана по тексту ТЗ, а не по SQL. Мутации (ТЗ 9.10, §8) —
копии SQL или python-модуля с одной намеренной поломкой: проверка обязана на них упасть, иначе
она ничего не проверяет. Список мутаций — в конце файла и в docs/STAGE_9_10_REPORT.md.

Сети, биржи и docker нет: ``docker compose exec ... psql`` подменяется заглушкой, которая
запускает тот же psql против тестовой базы. Стенд (синтетика) — не доказательство для сервера.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import random
import re
import shutil
import stat
import statistics
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.demo_support import (
    HOST,
    PORT,
    ROOT,
    USER,
    build_database,
    drop_database,
    open_pool,
    psql_raw,
    reset_data,
    server_up,
)

pytestmark = pytest.mark.skipif(not server_up(), reason="нет PostgreSQL для проверки SQL")
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="нужен bash")

SQL_DIR = ROOT / "analysis" / "sql" / "9_10"
RUN_9_10 = ROOT / "analysis" / "run_9_10.sh"
SHIFT_PY = ROOT / "analysis" / "shift_test.py"
VERDICT_PY = ROOT / "analysis" / "verdict_9_10.py"
VERIFY_9_10 = ROOT / "deploy" / "verify_9_10.sh"
MIGRATION = ROOT / "db" / "migrations" / "032_ro_backtest_read.sql"
ROLLBACK = ROOT / "db" / "migrations" / "032_ro_backtest_read_rollback.sql"
BASE_COMMIT = "1c30e8b"      # main, на котором основано ТЗ 9.10

BLOCK_FILES = ["00_data.sql", "01_veto_share.sql", "02_live_trades.sql", "03_weekly.sql",
               "04_history_returns.sql", "06_minute_gaps.sql"]
AS_OF = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
AS_OF_TXT = "2026-10-18 12:00:00+00"
TOKENS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


# --------------------------------------------------------------------------
# База, роль только на чтение, запуск SQL
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_name():
    """Чистая база по init.sql и миграциям (в т. ч. 032) + роль agenttrade_ro с правом SELECT."""
    role = "SELECT {} FROM pg_roles WHERE rolname='agenttrade_ro';"
    created = psql_raw("postgres", role.format("count(*)")) == "0"
    restore_nologin = False
    if created:
        psql_raw("postgres", "CREATE ROLE agenttrade_ro LOGIN;")
    elif psql_raw("postgres", role.format("rolcanlogin")) == "f":
        psql_raw("postgres", "ALTER ROLE agenttrade_ro LOGIN;")
        restore_nologin = True
    name = build_database("s910")       # миграция 032 применяется здесь: роль уже существует
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
    await reset_data(p)                       # BTC/USDT, DOGE/USDT, FOO/USDT
    await p.execute("TRUNCATE backtest.candles;")
    yield p
    await p.close()


def run_sql(db: str, path: Path | str, *, extra=(), text: str | None = None,
            as_of: str = AS_OF_TXT) -> str:
    """Выполняет файл блока ролью agenttrade_ro; формат вывода — «имя|значение» (-A)."""
    sql = text if text is not None else Path(path).read_text(encoding="utf-8")
    cmd = ["psql", "-X", "-q", "-A", "-F", "|", "-h", HOST, "-p", PORT, "-U", "agenttrade_ro",
           "-d", db, "-v", "ON_ERROR_STOP=1", "-v", f"as_of={as_of}"]
    for item in extra:
        cmd += ["-v", item]
    res = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout


def sql_of(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def mutate(name: str, old: str, new: str) -> str:
    text = sql_of(name)
    assert old in text, f"мутации нечего менять в {name}: {old!r}"
    return text.replace(old, new)


def num(value: str):
    if value in ("-", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return value


class Out:
    """Разобранный вывод блока: таблицы по заголовкам «-- …», факты «@@F», строки «@@R0x»."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.titles: list[str] = []
        self.raw: dict[str, list[str]] = {}
        self.facts: dict[str, str] = {}
        self.rows: dict[str, list[list[str]]] = {"R02": [], "R04": []}
        cur = None
        for line in text.splitlines():
            if line.startswith("@@F|"):
                _, key, val = line.split("|", 2)
                self.facts[key] = val
            elif line.startswith("@@R"):
                tag, *rest = line[2:].split("|")
                self.rows[tag].append(rest)
            elif line.startswith("-- "):
                cur = line
                self.titles.append(line)
                self.raw[cur] = []
            elif line.strip() and cur is not None:
                self.raw[cur].append(line)

    def table(self, title_part: str) -> list[dict]:
        title = next(t for t in self.titles if title_part in t)
        lines = self.raw[title]
        head = lines[0].split("|")
        return [dict(zip(head, [num(v) for v in ln.split("|")], strict=True)) for ln in lines[1:]]

    def row(self, title_part: str, **match) -> dict:
        found = [r for r in self.table(title_part) if all(r[k] == v for k, v in match.items())]
        assert len(found) == 1, (title_part, match, found)
        return found[0]


def run_block(db: str, name: str, **kw) -> Out:
    return Out(run_sql(db, SQL_DIR / name, **kw))


# --------------------------------------------------------------------------
# Синтетические ряды
# --------------------------------------------------------------------------

S0 = date(2026, 6, 1)               # начало основных рядов (задолго до as_of)


def dts(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def make_closes(seed: int, n: int, start: float = 100.0, vol: float = 0.03) -> list[float]:
    """Случайное блуждание с округлением до 8 знаков: ровно то, что хранит NUMERIC(20,8)."""
    rnd = random.Random(seed)
    out, price = [], start
    for _ in range(n):
        price *= 1 + rnd.gauss(0, vol)
        out.append(round(price, 8))
    return out


def day_bars(day: date, o: float, c: float, *,
             skip_hours=()) -> list[tuple[datetime, float, float]]:
    """24 часовых бара суток: open первого = o, close последнего = c, середина — прямая."""
    closes = [round(o + (c - o) * (h + 1) / 24, 8) for h in range(24)]
    closes[23] = c
    opens = [o, *closes[:-1]]
    return [(dts(day, h), opens[h], closes[h]) for h in range(24) if h not in skip_hours]


async def add_inst(pool, *tokens: str) -> dict[str, int]:
    for t in tokens:
        await pool.execute(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', $1, $2, 'USDT', 'spot') ON CONFLICT DO NOTHING;", f"{t}/USDT", t)
    return {r["symbol"].split("/")[0]: r["id"]
            for r in await pool.fetch("SELECT id, symbol FROM instruments;")}


def _dec(x: float) -> Decimal:
    return Decimal(repr(x))


async def put_hist(pool, token: str, start: date, closes: list[float], *, first_open=None,
                   skip: dict[int, tuple[int, ...]] | None = None) -> None:
    """Часовая история в backtest.candles. skip: {номер суток: часы, которых нет}."""
    rows = []
    prev = closes[0] if first_open is None else first_open
    for i, c in enumerate(closes):
        for ts, o, cl in day_bars(start + timedelta(days=i), prev, c,
                                  skip_hours=(skip or {}).get(i, ())):
            rows.append((f"{token}-USDT", "1H", ts, ts + timedelta(hours=1), _dec(o),
                         _dec(max(o, cl)), _dec(min(o, cl)), _dec(cl), Decimal(1)))
        prev = c
    await pool.executemany(
        "INSERT INTO backtest.candles (inst_id, bar, open_time, close_time, open, high, low, "
        "close, volume) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);", rows)


async def put_live(pool, token: str, start: date, closes: list[float], *, first_open=None,
                   skip: dict[int, tuple[int, ...]] | None = None) -> None:
    """Живые часовые свечи в public.ohlcv (timeframe '1h')."""
    ids = await add_inst(pool, token)
    rows = []
    prev = closes[0] if first_open is None else first_open
    for i, c in enumerate(closes):
        for ts, o, cl in day_bars(start + timedelta(days=i), prev, c,
                                  skip_hours=(skip or {}).get(i, ())):
            rows.append((ids[token], "1h", ts, o, max(o, cl), min(o, cl), cl, 1.0))
        prev = c
    await pool.executemany(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING;", rows)


async def put_1dutc(pool, token: str, start: date, closes: list[float], *, first_open=None) -> None:
    """Дневные свечи биржи (контроль §4.4): open = прежний close, close = close суток."""
    rows = []
    prev = closes[0] if first_open is None else first_open
    for i, c in enumerate(closes):
        ts = dts(start + timedelta(days=i))
        rows.append((f"{token}-USDT", "1Dutc", ts, ts + timedelta(days=1), _dec(prev),
                     _dec(max(prev, c)), _dec(min(prev, c)), _dec(c), Decimal(1)))
        prev = c
    await pool.executemany(
        "INSERT INTO backtest.candles (inst_id, bar, open_time, close_time, open, high, low, "
        "close, volume) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);", rows)


# --------------------------------------------------------------------------
# Независимая реализация правил ТЗ §4.2–4.3 на чистом Python (эталон для проверки SQL)
# --------------------------------------------------------------------------


def ref_daily(start: date, closes: list[float], first_open: float | None = None,
              incomplete: set[int] = frozenset()) -> dict[date, tuple[float, float]]:
    """{сутки: (open, close)} для полных суток; неполные не создаются."""
    out, prev = {}, closes[0] if first_open is None else first_open
    for i, c in enumerate(closes):
        if i not in incomplete:
            out[start + timedelta(days=i)] = (prev, c)
        prev = c
    return out


def ref_flags(daily: dict[date, tuple[float, float]], d: date) -> tuple:
    """(V1, V2, V3) монеты `daily` для дня d по барам до d−1; нет окна — None (ТЗ §4.3)."""

    def window(far: int, near: int):
        vals = [daily.get(d - timedelta(days=k)) for k in range(near, far + 1)]
        return None if any(v is None for v in vals) else [v[1] for v in vals]

    w50, a, b, w8 = window(50, 1), window(20, 1), window(25, 6), window(8, 1)
    prev = d - timedelta(days=1)
    v1 = (daily[prev][1] < sum(w50) / 50) if w50 else None
    v2 = (sum(a) / 20 < sum(b) / 20) if a and b else None
    v3 = (daily[prev][1] < daily[d - timedelta(days=8)][1]) if w8 else None
    return v1, v2, v3


def flag_text(v) -> str:
    return "" if v is None else str(int(v))


def r04_by_key(out: Out) -> dict[tuple[str, str], list]:
    """{(день, монета): [v1, v2, v3, v4, r]} из строк @@R04."""
    return {(r[0], r[1]): r[2:] for r in out.rows["R04"]}


async def mkpos(pool, symbol, opened, *, status="closed", pct=1.0, reason="target", usd=None,
                notional=2.0, logic=7, hours=30) -> int:
    """Позиция версии 7 (по умолчанию закрыта через `hours` часов) с заданным итогом."""
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
        "VALUES ($1, $2, $3, 24, 'buy', TRUE, $4, $5, 100, $5, 100, 5, 0, "
        "$6::numeric / 100, $6::numeric, 1, 101, 0.22, $5::timestamptz + interval '48 hours', "
        "'1m', $7, $8, $9, $10, $11, $12, -0.5, 1.2) RETURNING id;",
        inst["id"], sig, logic, status, opened, notional,
        opened + timedelta(hours=hours) if c else None, 100.0 if c else None,
        reason if c else None, True if c else None, pct if c else None,
        (usd if usd is not None else notional * pct / 100) if c else None)


async def candle(pool, symbol, ts, close, tf="1m") -> None:
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol=$1;", symbol)
    await pool.execute(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $4, $4, $4, 1) ON CONFLICT DO NOTHING;",
        inst["id"], tf, ts, close)


# --------------------------------------------------------------------------
# §8.1. Дневная свеча из 24 часов; неполные сутки не создаются
# --------------------------------------------------------------------------


def assert_23h_day_skipped(out: Out) -> None:
    rows = r04_by_key(out)
    assert (str(S0 + timedelta(days=1)), "BTC") not in rows          # синтетики нет
    assert float(rows[(str(S0), "BTC")][4]) == pytest.approx(10.0)
    assert float(rows[(str(S0 + timedelta(days=2)), "BTC")][4]) == pytest.approx(-10.0)


async def test_day_candle_of_24_hours_is_exact_and_a_23_hour_day_is_skipped(pool, db_name) -> None:
    # сутки 0: 100 → 110 (+10 %); сутки 1: нет часа 07; сутки 2: 121 → 108,9 (−10 %)
    await put_hist(pool, "BTC", S0, [110.0, 121.0, 108.9], first_open=100.0, skip={1: (7,)})
    cnt = run_block(db_name, "00_data.sql").row("Сутки и пропуски", Монета="BTC")
    assert cnt["Суток в daily"] == 2
    assert cnt["Неполных суток (пропущено)"] == 1
    assert cnt["Часов нет"] == 1
    assert cnt["По годам"] == "2026:1"
    assert_23h_day_skipped(run_block(db_name, "04_history_returns.sql"))


async def test_the_current_unfinished_day_is_not_counted_as_a_gap(pool, db_name) -> None:
    # живые часы 2026-10-18 00:00 … 10:00 (11 баров закрыты к 12:00): сутки не закончились
    await put_live(pool, "BTC", date(2026, 10, 17), [100.0])
    inst = (await add_inst(pool, "BTC"))["BTC"]
    for h in range(11):
        await pool.execute(
            "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
            "VALUES ($1, '1h', $2, 100, 100, 100, 100, 1);", inst, dts(date(2026, 10, 18), h))
    cnt = run_block(db_name, "00_data.sql").row("Сутки и пропуски", Монета="BTC")
    assert cnt["Суток в daily"] == 1 and cnt["Неполных суток (пропущено)"] == 0


# --------------------------------------------------------------------------
# §8.2. Слияние H-hist и H-live
# --------------------------------------------------------------------------


async def seed_merge(pool) -> None:
    """История — сутки 0…4 (+1 % в сутки); живой сборщик — сутки 3…6 (−1 % в сутки)."""
    hist = [round(100 * 1.01 ** (i + 1), 8) for i in range(5)]
    await put_hist(pool, "BTC", S0, hist, first_open=100.0)
    live, p = [], hist[2]
    for _ in range(4):
        p = round(p * 0.99, 8)
        live.append(p)
    await put_live(pool, "BTC", S0 + timedelta(days=3), live, first_open=hist[2])


def assert_merge(out: Out) -> None:
    rows = r04_by_key(out)
    for i in (3, 4, 5, 6):
        assert (str(S0 + timedelta(days=i)), "BTC") in rows, i
    for i in (3, 4):                                 # перекрытие: берётся история (+1 %)
        assert float(rows[(str(S0 + timedelta(days=i)), "BTC")][4]) == pytest.approx(1.0, abs=1e-6)
    for i in (5, 6):                                 # после истории — живые (−1 %)
        assert float(rows[(str(S0 + timedelta(days=i)), "BTC")][4]) == pytest.approx(-1.0, abs=1e-6)


async def test_merge_takes_history_on_overlap_and_live_after_it(pool, db_name) -> None:
    await seed_merge(pool)
    assert_merge(run_block(db_name, "04_history_returns.sql"))


async def test_the_seam_control_sees_a_disagreeing_live_collector(pool, db_name) -> None:
    await seed_merge(pool)            # общие часы (48) в истории и у сборщика расходятся на 2 %
    out = run_block(db_name, "00_data.sql")
    seam = out.row("Контроли", **{"Контроль": "3. шов H-hist / H-live, общие часы"})
    assert seam["Сравнено"] == 48 and seam["Расхождений"] == 48
    assert out.facts["ctl3"] == "0"


# --------------------------------------------------------------------------
# §8.3. Подглядывание: флаг дня d зависит только от баров ≤ d−1
# --------------------------------------------------------------------------


async def seed_lookahead(pool) -> None:
    """Сутки 0…58 — 100, сутки 59 — 99, сутки 60 — обвал до 1, сутки 61 — отскок до 100."""
    closes = [100.0] * 59 + [99.0, 1.0, 100.0]
    await put_hist(pool, "BTC", S0, closes, first_open=100.0)


def assert_lookahead(out: Out) -> None:
    rows = r04_by_key(out)
    day = {k: str(S0 + timedelta(days=k)) for k in (59, 60, 61)}
    # день 59 видит бары до 58 (все по 100, равенство — не «ниже»): вето НЕТ, хотя бар 59 — 99
    assert rows[(day[59], "BTC")][:4] == ["0", "0", "0", "0"]
    # день 60 видит бар 59 (99 ниже средней 99,98): вето по всем четырём; обвал бара 60 не виден
    assert rows[(day[60], "BTC")][:4] == ["1", "1", "1", "1"]
    # день 61 видит обвал бара 60: вето; отскок бара 61 до 100 ему не виден
    assert rows[(day[61], "BTC")][:4] == ["1", "1", "1", "1"]


async def test_the_flag_of_a_day_does_not_see_that_days_bar(pool, db_name) -> None:
    await seed_lookahead(pool)
    assert_lookahead(run_block(db_name, "04_history_returns.sql"))


async def test_mutation_flag_by_bar_d_instead_of_d_minus_1_is_caught(pool, db_name) -> None:
    await seed_lookahead(pool)
    mutated = mutate("04_history_returns.sql", "lag(close, 1) OVER o   AS c1",
                     "close                  AS c1")
    with pytest.raises(AssertionError):
        assert_lookahead(Out(run_sql(db_name, "", text=mutated)))


async def test_mutation_window_that_includes_bar_d_is_caught(pool, db_name) -> None:
    await seed_lookahead(pool)
    mutated = mutate("04_history_returns.sql",
                     "w50  AS (o ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING)",
                     "w50  AS (o ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)")
    with pytest.raises(AssertionError):
        assert_lookahead(Out(run_sql(db_name, "", text=mutated)))


# --------------------------------------------------------------------------
# §8.4. V1–V4 на случайном ряду против независимого эталона; NULL при нехватке баров
# --------------------------------------------------------------------------

N_REF = 140
START_REF = date(2026, 3, 1)
GAPS = {"BTC": {40}, "ETH": {100}}


async def seed_ref(pool) -> dict[str, dict[date, tuple[float, float]]]:
    series = {}
    for seed, tok in ((11, "BTC"), (12, "ETH")):
        closes = make_closes(seed, N_REF)
        skip = {i: (5,) for i in GAPS[tok]}
        await put_hist(pool, tok, START_REF, closes, first_open=100.0, skip=skip)
        series[tok] = ref_daily(START_REF, closes, 100.0, incomplete=GAPS[tok])
    return series


def assert_flags_match_reference(out: Out, series) -> None:
    rows = r04_by_key(out)
    expected_keys = {(str(d), tok) for tok, daily in series.items() for d in daily}
    assert set(rows) == expected_keys
    seen = {None: 0, True: 0, False: 0}
    for (day, tok), got in rows.items():
        d = date.fromisoformat(day)
        want = [*ref_flags(series["BTC"], d), ref_flags(series[tok], d)[0]]
        assert got[:4] == [flag_text(v) for v in want], (day, tok)
        for v in want:
            seen[v] += 1
    assert min(seen.values()) > 20, seen          # оба значения и NULL реально встречаются


async def test_v1_to_v4_match_the_independent_reference_including_null(pool, db_name) -> None:
    series = await seed_ref(pool)
    assert_flags_match_reference(run_block(db_name, "04_history_returns.sql"), series)


async def test_a_missing_bar_makes_flags_null_until_the_window_clears_it(pool, db_name) -> None:
    await seed_ref(pool)
    rows = r04_by_key(run_block(db_name, "04_history_returns.sql"))
    gap_day = START_REF + timedelta(days=40)                      # у BTC неполные сутки 40
    for k in (1, 8, 25, 50):                                     # день d видит бар d−k
        d = gap_day + timedelta(days=k)
        got = rows[(str(d), "ETH")][:3]
        if k <= 8:
            assert got[2] == "", (k, got)                         # V3: окно 8 баров содержит дыру
        if k <= 25:
            assert got[1] == "", (k, got)                         # V2: окно 25 баров
        if k <= 50:
            assert got[0] == "", (k, got)                         # V1: окно 50 баров
    after = rows[(str(gap_day + timedelta(days=51)), "ETH")][:3]
    assert all(v != "" for v in after)


async def test_mutation_v3_with_a_7_day_lookback_is_caught(pool, db_name) -> None:
    series = await seed_ref(pool)
    mutated = mutate("04_history_returns.sql", "lag(close, 8) OVER o   AS c8",
                     "lag(close, 7) OVER o   AS c8")
    with pytest.raises(AssertionError):
        assert_flags_match_reference(Out(run_sql(db_name, "", text=mutated)), series)


# --------------------------------------------------------------------------
# Блок 04: сводка, половины, монеты — против эталона
# --------------------------------------------------------------------------

HALF = date(2024, 4, 1)


def ref_block04(series: dict[str, dict[date, tuple[float, float]]]) -> dict[str, float | None]:
    btc = series["BTC"]
    obs = []
    for tok, daily in series.items():
        for d, (o, c) in daily.items():
            f1, f2, f3 = ref_flags(btc, d)
            obs.append((tok, d, c / o - 1, (f1, f2, f3, ref_flags(daily, d)[0])))
    res: dict[str, float | None] = {}

    def diff(rows, keep) -> float | None:
        by_day: dict[tuple, list] = {}
        for _tok, d, r, f in rows:
            if keep(d):
                by_day.setdefault((d, f), []).append(r)
        dm = {k: sum(v) / len(v) for k, v in by_day.items()}
        mf = [v for (_, f), v in dm.items() if not f]
        mt = [v for (_, f), v in dm.items() if f]
        return 100 * (sum(mf) / len(mf) - sum(mt) / len(mt)) if mf and mt else None

    for i, rule in enumerate(("V1", "V2", "V3", "V4")):
        rows = [(t, d, r, f[i]) for t, d, r, f in obs if f[i] is not None]
        res[f"{rule}.d_all"] = diff(rows, lambda d: True)
        res[f"{rule}.d_h1"] = diff(rows, lambda d: d < HALF)
        res[f"{rule}.d_h2"] = diff(rows, lambda d: d >= HALF)
        for tok in series:
            mine = [x for x in rows if x[0] == tok]
            mf = [r for _, _, r, f in mine if not f]
            mt = [r for _, _, r, f in mine if f]
            res[f"{rule}.c_{tok}"] = (100 * (sum(mf) / len(mf) - sum(mt) / len(mt))
                                      if mf and mt else None)
    return res


async def seed_halves(pool) -> dict[str, dict[date, tuple[float, float]]]:
    """Три монеты, 150 суток с 2024-01-10 — граница половин (2024-04-01) внутри ряда."""
    start, series = date(2024, 1, 10), {}
    for seed, tok in ((21, "BTC"), (22, "ETH"), (23, "SOL")):
        closes = make_closes(seed, 150)
        skip = {70: (3,)} if tok == "BTC" else {}
        await put_hist(pool, tok, start, closes, first_open=100.0, skip=skip)
        series[tok] = ref_daily(start, closes, 100.0, incomplete={70} if tok == "BTC" else set())
    return series


async def test_block_04_matches_the_reference_overall_by_half_and_by_coin(pool, db_name) -> None:
    series = await seed_halves(pool)
    out = run_block(db_name, "04_history_returns.sql")
    want = ref_block04(series)
    assert sum(v is not None for v in want.values()) >= 20
    for key, value in want.items():
        got = out.facts.get(key)
        if value is None:
            assert got in (None, ""), key
        else:
            assert float(got) == pytest.approx(value, rel=1e-9, abs=1e-9), key
    summary = out.row("Сводно по дням", **{"Прав.": "V1"})
    assert summary["FALSE − TRUE, п.п."] == pytest.approx(round(want["V1.d_all"], 4))
    assert out.rows["R04"], "строки для теста сдвигом должны печататься"


# --------------------------------------------------------------------------
# §8.5. Контроль данных §4.4: сутки из часов = 1Dutc биржи; контрольный опыт обязан упасть
# --------------------------------------------------------------------------


async def seed_controls(pool, *, corrupt: bool = False) -> None:
    """Две монеты по 60 суток; 1Dutc биржи совпадает с часовыми; сборщик — последние 5 суток."""
    for seed, tok in ((31, "BTC"), (32, "ETH")):
        closes = make_closes(seed, 60)
        await put_hist(pool, tok, S0, closes, first_open=100.0)
        daily = list(closes)
        if corrupt and tok == "BTC":
            daily[10] = round(daily[10] * 1.01, 8)          # биржа расходится с часовыми на 1 %
        await put_1dutc(pool, tok, S0, daily, first_open=100.0)
        if tok == "BTC":
            await put_live(pool, "BTC", S0 + timedelta(days=55), closes[55:], first_open=closes[54])


def assert_controls_ok(out: Out) -> None:
    ctl = {r["Контроль"][:2]: r for r in out.table("Контроли")}
    assert ctl["1."]["Сравнено"] == 120 and ctl["1."]["Расхождений"] == 0
    assert ctl["2."]["Сравнено"] == 118 and ctl["2."]["Расхождений"] > 0     # опыт обязан упасть
    assert ctl["3."]["Сравнено"] == 120 and ctl["3."]["Расхождений"] == 0
    assert out.facts["ctl1"] == out.facts["ctl2"] == out.facts["ctl3"] == "1"
    assert all(r["Итог"] == "✅" for r in ctl.values())


async def test_controls_pass_on_consistent_data_and_the_shifted_day_diverges(pool, db_name) -> None:
    await seed_controls(pool)
    assert_controls_ok(run_block(db_name, "00_data.sql"))


async def test_a_disagreeing_exchange_candle_fails_control_1(pool, db_name) -> None:
    await seed_controls(pool, corrupt=True)
    out = run_block(db_name, "00_data.sql")
    first = next(r for r in out.table("Контроли") if r["Контроль"].startswith("1."))
    assert first["Расхождений"] >= 1 and first["Итог"] == "❌"
    assert out.facts["ctl1"] == "0"


async def test_a_control_that_compared_nothing_is_not_passed(pool, db_name) -> None:
    await put_hist(pool, "BTC", S0, make_closes(1, 5), first_open=100.0)      # 1Dutc нет вовсе
    out = run_block(db_name, "00_data.sql")
    assert out.facts["ctl1"] == "0" and out.facts["ctl2"] == "0" and out.facts["ctl3"] == "0"


async def test_mutation_tolerance_1_0_kills_the_control_experiment(pool, db_name) -> None:
    await seed_controls(pool)
    mutated = mutate("00_data.sql", "5e-4", "1.0")
    out = Out(run_sql(db_name, "", text=mutated))
    assert out.facts["ctl2"] == "0"            # сдвинутые сутки «совпали» — опыт ничего не ловит
    with pytest.raises(AssertionError):
        assert_controls_ok(out)


# --------------------------------------------------------------------------
# §8.6. Блок 02: известные средние, n_eff, фактор прибыли, разности, открытые
# --------------------------------------------------------------------------

S2 = date(2026, 5, 1)       # сутки 0 = пятница 2026-05-01


def day_of(k: int) -> date:
    return S2 + timedelta(days=k)


async def seed_02(pool) -> None:
    """BTC падает 60 суток (200 → 141) и растёт 60 (141 → 259), ETH растёт всё время.

    Вето V1–V3 по BTC: сутки 56–58 (падение, окна заполнены); разрешено: сутки 101–103 (рост).
    У ETH (V4 по своей монете, растёт) вето нет никогда. Сутки 3 — окон ещё нет, флаг NULL.
    """
    btc = [200.0 - i for i in range(60)] + [141.0 + 2 * (i - 59) for i in range(60, 120)]
    eth = [50.0 + i for i in range(120)]
    await put_hist(pool, "BTC", S2, btc, first_open=201.0)
    await put_hist(pool, "ETH", S2, eth, first_open=49.0)
    await add_inst(pool, "ETH")


# (монета, сутки, итог %, причина выхода)
POS_02 = [
    ("BTC", 56, -3.0, "timeout"), ("BTC", 57, -5.0, "timeout"), ("BTC", 57, 1.0, "target"),
    ("BTC", 101, 1.0, "target"), ("BTC", 101, 2.0, "target"), ("BTC", 102, -1.0, "timeout"),
    ("BTC", 103, 0.0, "plus_exit"), ("BTC", 3, 0.5, "target"),
    ("ETH", 57, 4.0, "target"), ("ETH", 102, -2.0, "timeout"),
]


async def put_positions_02(pool) -> None:
    for tok, k, pct, reason in POS_02:
        await mkpos(pool, f"{tok}/USDT", dts(day_of(k), 12), pct=pct, reason=reason)
    await mkpos(pool, "BTC/USDT", dts(day_of(57), 15), status="open")        # открытая под вето
    await mkpos(pool, "BTC/USDT", dts(day_of(3), 15), status="open")         # открытая без флага
    await mkpos(pool, "BTC/USDT", dts(day_of(102), 15), pct=-9.0, reason="data_gap")  # вне метрик
    await mkpos(pool, "BTC/USDT", dts(day_of(57), 9), pct=7.0, logic=6)       # чужая версия


def ref_group_stats(rows: list[tuple[int, float, str]]) -> dict:
    pnl = [r[1] for r in rows]
    by_day: dict[int, list[float]] = {}
    for k, p, _ in rows:
        by_day.setdefault(k, []).append(p)
    usd = [p * 0.02 for p in pnl]
    losses = -sum(u for u in usd if u < 0)
    return {"n": len(pnl), "n_eff": len(by_day), "m": sum(pnl) / len(pnl),
            "med": statistics.median(pnl), "pos": 100 * sum(p > 0.0001 for p in pnl) / len(pnl),
            "worst": min(pnl), "to": 100 * sum(r[2] == "timeout" for r in rows) / len(rows),
            "usd": sum(usd), "pf": sum(u for u in usd if u > 0) / losses if losses else None,
            "day_means": [sum(v) / len(v) for v in by_day.values()]}


async def test_02_premises_of_the_scenario_hold(pool, db_name) -> None:
    """Страховка сценария: эталонные флаги дней открытия именно такие, как задумано."""
    btc = btc_02()
    for k in (56, 57, 58):
        assert ref_flags(btc, day_of(k)) == (True, True, True)
    for k in (101, 102, 103):
        assert ref_flags(btc, day_of(k)) == (False, False, False)
    assert ref_flags(btc, day_of(3)) == (None, None, None)


def assert_02_v1_groups(out: Out) -> None:
    veto = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "вето"})
    ok = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "разрешены"})
    allp = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "все"})
    nof = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "без флага"})
    # вето: BTC −3, −5, +1 (сутки 56–57) и ETH +4 (сутки 57, флаг BTC)
    assert (veto["N"], veto["n_eff"]) == (4, 2)
    assert veto["Ср., %"] == -0.75 and veto["Мед., %"] == -1.0 and veto[">0, %"] == 50.0
    assert veto["Худш., %"] == -5.0 and veto["timeout, %"] == 50.0
    assert veto["Σ USD"] == -0.06 and veto["ФП"] == 0.63                 # 0,10 / 0,16 = 0,625
    # разрешены: +1, +2, −1, 0 (BTC) и −2 (ETH, сутки 102)
    assert (ok["N"], ok["n_eff"]) == (5, 3)
    assert ok["Ср., %"] == 0.0 and ok["Мед., %"] == 0.0 and ok[">0, %"] == 40.0
    assert ok["Худш., %"] == -2.0 and ok["timeout, %"] == 40.0
    assert ok["Σ USD"] == 0.0 and ok["ФП"] == 1.0                        # 0,06 / 0,06
    assert (allp["N"], allp["n_eff"], allp["Ср., %"]) == (10, 6, -0.25)  # data_gap и версия 6 — вне
    assert nof["N"] == 1


async def test_02_v1_groups_match_the_hand_calculation(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    assert_02_v1_groups(run_block(db_name, "02_live_trades.sql"))


async def test_02_difference_and_interval_by_days(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "02_live_trades.sql")
    ok_days = [1.5, -1.5, 0.0]                  # сутки 101, 102, 103 (разрешены)
    all_days = [0.5, -3.0, 0.0, 1.5, -1.5, 0.0]  # сутки 3, 56, 57, 101, 102, 103
    ok_row = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "разрешены"})
    all_row = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "все"})
    assert ok_row["Дней"] == 3 and all_row["Дней"] == 6
    assert ok_row["Ср. по дням"] == 0.0
    assert all_row["Ср. по дням"] == pytest.approx(-0.417, abs=1e-3)
    assert ok_row["±95%"] == pytest.approx(1.96 * statistics.stdev(ok_days) / 3 ** 0.5, abs=1e-3)
    d = out.row("Разность", **{"Прав.": "V1"})
    assert d["По сделкам, п.п."] == 0.25                                   # 0,0 − (−0,25)
    want = statistics.mean(ok_days) - statistics.mean(all_days)
    se = (statistics.variance(ok_days) / 3 + statistics.variance(all_days) / 6) ** 0.5
    assert d["По дням, п.п."] == pytest.approx(want, abs=1e-3)
    assert d["Нижняя 95%"] == pytest.approx(want - 1.96 * se, abs=1e-3)
    assert d["Верхняя 95%"] == pytest.approx(want + 1.96 * se, abs=1e-3)


async def test_02_v4_uses_the_own_coin_and_v2_v3_agree_with_v1_here(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "02_live_trades.sql")
    for rule in ("V1", "V2", "V3"):
        assert out.row("Закрытые позиции", **{"Прав.": rule, "Группа": "вето"})["N"] == 4
        assert out.row("Закрытые позиции", **{"Прав.": rule, "Группа": "разрешены"})["N"] == 5
    # V4: вето только у BTC (3 сделки: −3, −5, +1), ETH растёт — вето нет ни разу
    v4_veto = out.row("Закрытые позиции", **{"Прав.": "V4", "Группа": "вето"})
    v4_ok = out.row("Закрытые позиции", **{"Прав.": "V4", "Группа": "разрешены"})
    assert v4_veto["N"] == 3 and v4_veto["Ср., %"] == pytest.approx(-2.333, abs=1e-3)
    assert v4_ok["N"] == 6 and v4_ok["Ср., %"] == pytest.approx(0.667, abs=1e-3)
    assert out.row("Закрытые позиции", **{"Прав.": "V4", "Группа": "без флага"})["N"] == 1


async def test_02_open_positions_are_only_counted_not_averaged(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "02_live_trades.sql")
    op = out.row("Открытые позиции", **{"Прав.": "V1"})
    assert (op["Вето"], op["Разрешены"], op["Без флага"]) == (1, 0, 1)
    assert out.facts["V1.live_veto"] == "-0.7500" or float(out.facts["V1.live_veto"]) == -0.75
    assert float(out.facts["V1.live_ok"]) == 0.0
    assert len(out.rows["R02"]) == 10                   # в файл фактов — только закрытые в метриках


async def test_02_an_empty_group_is_shown_with_zero_rows(pool, db_name) -> None:
    await seed_02(pool)
    await mkpos(pool, "BTC/USDT", dts(day_of(101), 12), pct=1.0)         # ни одного вето
    out = run_block(db_name, "02_live_trades.sql")
    veto = out.row("Закрытые позиции", **{"Прав.": "V1", "Группа": "вето"})
    assert veto["N"] == 0 and veto["Ср., %"] is None


async def test_mutation_data_gap_in_block_02_is_caught(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    mutated = mutate("02_live_trades.sql", "exit_reason IS DISTINCT FROM 'data_gap'", "TRUE")
    with pytest.raises(AssertionError):
        assert_02_v1_groups(Out(run_sql(db_name, "", text=mutated)))


async def test_mutation_flag_of_the_report_day_instead_of_the_opening_day_is_caught(
    pool, db_name
) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    mutated = mutate("02_live_trades.sql", "b.day = (v.opened_at AT TIME ZONE 'UTC')::date",
                     "b.day = (:'as_of'::timestamptz AT TIME ZONE 'UTC')::date")
    with pytest.raises(AssertionError):
        assert_02_v1_groups(Out(run_sql(db_name, "", text=mutated)))


# --------------------------------------------------------------------------
# Блок 03 (недели × V1) и блок 01 (доля дней)
# --------------------------------------------------------------------------


def btc_02() -> dict[date, tuple[float, float]]:
    closes = [200.0 - i for i in range(60)] + [141.0 + 2 * (i - 59) for i in range(60, 120)]
    return ref_daily(S2, closes, 201.0)


async def test_03_weeks_split_by_the_flag_of_the_opening_day(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "03_weekly.sql")
    got = {ln.split("|")[0]: ln.split("|")
           for ln in out.text.splitlines() if re.match(r"^\d{4}-W\d\d\|", ln)}
    btc = btc_02()
    want: dict[str, dict] = {}
    for _tok, k, pct, _reason in POS_02:
        iso = day_of(k).isocalendar()
        w = want.setdefault(f"{iso.year}-W{iso.week:02d}", {True: [], False: [], None: []})
        w[ref_flags(btc, day_of(k))[0]].append(pct)
    assert set(got) == set(want) and len(want) >= 3
    for wk, groups in want.items():
        row = got[wk]
        assert [int(row[2]), int(row[3]), int(row[4]), int(row[5])] == [
            sum(map(len, groups.values())), len(groups[False]), len(groups[True]),
            len(groups[None])]
        for idx, key in ((6, False), (7, True)):
            if groups[key]:
                mean = sum(groups[key]) / len(groups[key])
                assert float(row[idx]) == pytest.approx(mean, abs=1e-3)
            else:
                assert row[idx] == "-"


async def test_01_shares_of_days_match_the_reference(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "01_veto_share.sql")
    btc = btc_02()
    for per, first in (("вся история", S2), ("версия 7", day_of(3))):
        days = [first + timedelta(days=i) for i in range((AS_OF.date() - first).days + 1)]
        want = [ref_flags(btc, d)[0] for d in days]
        row = out.row("Доля дней", **{"Правило": "V1", "Период": per})
        assert (row["Дней"], row["TRUE"], row["FALSE"], row["NULL"]) == (
            len(days), want.count(True), want.count(False), want.count(None))
    assert out.row("Доля дней", **{"Правило": "V1", "Период": "версия 7"})["TRUE"] > 0


async def test_01_v1_weeks_list_the_days_under_veto(pool, db_name) -> None:
    await seed_02(pool)
    await put_positions_02(pool)
    out = run_block(db_name, "01_veto_share.sql")
    table = [ln.split("|") for ln in out.text.splitlines() if re.match(r"^\d{4}-W\d\d\|", ln)]
    first_day, last_day = day_of(3), AS_OF.date()
    weeks = {}
    btc = btc_02()
    for i in range((last_day - first_day).days + 1):
        d = first_day + timedelta(days=i)
        iso = d.isocalendar()
        w = weeks.setdefault(f"{iso.year}-W{iso.week:02d}", [0, 0])
        w[1] += 1
        w[0] += ref_flags(btc, d)[0] is True
    assert {r[0]: [int(r[3]), int(r[4])] for r in table} == weeks


async def test_mutation_live_added_on_top_of_history_is_caught(pool, db_name) -> None:
    await seed_merge(pool)
    mutated = mutate("04_history_returns.sql",
                     "WHERE l.open_time > coalesce(x.t, '-infinity'::timestamptz)", "WHERE true")
    with pytest.raises(AssertionError):
        assert_merge(Out(run_sql(db_name, "", text=mutated)))


async def test_mutation_a_23_hour_day_is_accepted_is_caught(pool, db_name) -> None:
    await put_hist(pool, "BTC", S0, [110.0, 121.0, 108.9], first_open=100.0, skip={1: (7,)})
    mutated = mutate("04_history_returns.sql", "WHERE n_h = 24", "WHERE n_h >= 23")
    with pytest.raises(AssertionError):
        assert_23h_day_skipped(Out(run_sql(db_name, "", text=mutated)))


# --------------------------------------------------------------------------
# §8.7. shift_test.py и verdict_9_10.py: только стандартная библиотека; смысл теста сдвигом
# --------------------------------------------------------------------------


def load_module(path: Path, name: str, *, replace: tuple[str, str] | None = None):
    source = path.read_text(encoding="utf-8")
    if replace:
        assert replace[0] in source, replace[0]
        source = source.replace(*replace)
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(path), "exec"), module.__dict__)    # noqa: S102 — свой же файл
    return module


def regimes(seed: int, n: int) -> list[bool]:
    """Флаги по режимам случайной длины 5…20 суток (как тренды в жизни)."""
    rnd = random.Random(seed)
    out: list[bool] = []
    flag = False
    while len(out) < n:
        out += [flag] * rnd.randint(5, 20)
        flag = not flag
    return out[:n]


def panel_rows(flags: dict[str, list[bool]], values: dict[str, list[float]], n: int,
               rule_idx: int = 0):
    days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    rows = []
    for tok in flags:
        for i, day in enumerate(days):
            f = [None, None, None, None]
            f[rule_idx] = flags[tok][i]
            rows.append((day, tok, tuple(f), values[tok][i]))
    return rows


def test_shift_test_is_minimal_when_the_return_equals_the_flag() -> None:
    mod = load_module(SHIFT_PY, "shift_ok")
    n = 300
    flags = regimes(1, n)
    values = [-1.0 if f else 1.0 for f in flags]           # вето ⇒ доходность −1, иначе +1
    rows = panel_rows({"BTC": flags}, {"BTC": values}, n)
    res = mod.shift_test(rows, "V1")
    assert res["obs"] == pytest.approx(2.0) and res["days"] == n
    assert res["shifts"] == n - 30 - 30 + 1 and res["p"] == 0.0


def test_shift_test_is_not_small_for_an_independent_series() -> None:
    mod = load_module(SHIFT_PY, "shift_rnd")
    n = 300
    rnd = random.Random(7)
    flags = regimes(2, n)
    values = [rnd.gauss(0, 1) for _ in range(n)]
    res = mod.shift_test(panel_rows({"BTC": flags}, {"BTC": values}, n), "V1")
    assert res["p"] is not None and res["p"] > 0.05, res


def test_shift_test_per_coin_rule_v4_uses_each_coins_own_flags() -> None:
    mod = load_module(SHIFT_PY, "shift_v4")
    n = 300
    flags = {"BTC": regimes(3, n), "ETH": regimes(4, n)}
    tied = {t: [-1.0 if f else 1.0 for f in fl] for t, fl in flags.items()}
    res = mod.shift_test(panel_rows(flags, tied, n, rule_idx=3), "V4")
    assert res["obs"] == pytest.approx(2.0) and res["p"] == 0.0
    rnd = random.Random(5)
    noise = {t: [rnd.gauss(0, 1) for _ in range(n)] for t in flags}
    res = mod.shift_test(panel_rows(flags, noise, n, rule_idx=3), "V4")
    assert res["p"] > 0.05, res


def test_shift_test_with_too_few_days_has_no_shifts_and_says_so(tmp_path) -> None:
    mod = load_module(SHIFT_PY, "shift_few")
    n = 40
    flags = regimes(6, n)
    rows = panel_rows({"BTC": flags}, {"BTC": [float(i % 3) for i in range(n)]}, n)
    res = mod.shift_test(rows, "V1")
    assert res["shifts"] == 0 and res["p"] is None and res["obs"] is not None
    facts = tmp_path / "facts.txt"
    lines = [f"@@R02|{d}|{t}|{flag_text(f[0])}|{flag_text(f[1])}|{flag_text(f[2])}|"
             f"{flag_text(f[3])}|{v}" for d, t, f, v in rows]
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res_run = subprocess.run([sys.executable, "-I", str(SHIFT_PY), str(facts), "--append"],
                             capture_output=True, text=True, check=False)
    assert res_run.returncode == 0, res_run.stderr
    assert "мало дней, справочно" in res_run.stdout and "нет данных" in res_run.stdout
    assert "@@F|V1.shifts02|0" in facts.read_text(encoding="utf-8")


def test_shift_test_main_prints_both_sections_and_appends_p_values(tmp_path) -> None:
    n = 200
    flags = regimes(8, n)
    lines = []
    for tag in ("R04", "R02"):
        for i in range(n):
            day = (date(2024, 1, 1) + timedelta(days=i)).isoformat()
            lines.append(f"@@{tag}|{day}|BTC|{int(flags[i])}|{int(flags[i])}|{int(flags[i])}|"
                         f"{int(flags[i])}|{-1.0 if flags[i] else 1.0}")
    facts = tmp_path / "facts.txt"
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = subprocess.run([sys.executable, "-I", str(SHIFT_PY), str(facts), "--append"],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert "История (блок 04)" in res.stdout and "Живые сделки версии 7 (блок 02)" in res.stdout
    assert "мало дней" not in res.stdout                            # 200 дней — не «мало»
    appended = facts.read_text(encoding="utf-8")
    for rule in ("V1", "V2", "V3", "V4"):
        assert f"@@F|{rule}.p04|0.0" in appended and f"@@F|{rule}.p02|0.0" in appended


@pytest.mark.parametrize("path", [SHIFT_PY, VERDICT_PY], ids=lambda p: p.name)
def test_python_modules_import_only_the_standard_library(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported, path.name
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert imported <= {"__future__", "argparse", "sys"}, imported


def test_mutation_p_counts_the_wrong_side_of_the_distribution_is_caught() -> None:
    n = 300
    flags = regimes(1, n)
    rows = panel_rows({"BTC": flags}, {"BTC": [-1.0 if f else 1.0 for f in flags]}, n)
    mod = load_module(SHIFT_PY, "shift_mut", replace=("if d >= observed:", "if d <= observed:"))
    with pytest.raises(AssertionError):
        assert mod.shift_test(rows, "V1")["p"] == 0.0


def test_mutation_shifts_start_at_zero_instead_of_30_is_caught() -> None:
    n = 300
    flags = regimes(1, n)
    rows = panel_rows({"BTC": flags}, {"BTC": [-1.0 if f else 1.0 for f in flags]}, n)
    mod = load_module(SHIFT_PY, "shift_mut2", replace=("MIN_SHIFT = 30 ", "MIN_SHIFT = 0  "))
    with pytest.raises(AssertionError):            # сдвиг 0 — это сама наблюдаемая разница
        assert mod.shift_test(rows, "V1")["p"] == 0.0


# --------------------------------------------------------------------------
# Вердикт §6: критерии как записано ДО прогона
# --------------------------------------------------------------------------

COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def good_facts(rule: str = "V1") -> dict[str, str]:
    facts = {"ctl1": "1", "ctl2": "1", "ctl3": "1"}
    for r in ("V1", "V2", "V3", "V4"):
        ok = r == rule
        facts |= {f"{r}.d_h1": "0.2" if ok else "-0.1", f"{r}.d_h2": "0.1" if ok else "-0.1",
                  f"{r}.p04": "0.03" if ok else "0.5",
                  f"{r}.live_ok": "0.5", f"{r}.live_veto": "-1.5" if ok else "0.7"}
        for c in COINS:
            facts[f"{r}.c_{c}"] = "0.1" if ok else "-0.1"
    return facts


def verdict_lines(mod, facts: dict[str, str]) -> list[str]:
    return mod.render(facts)


def test_verdict_makes_v1_the_candidate_when_all_four_criteria_hold() -> None:
    mod = load_module(VERDICT_PY, "verdict_ok")
    lines = verdict_lines(mod, good_facts("V1"))
    assert "V1: ИТОГ — КАНДИДАТ на теневое вето" in lines
    assert all(f"{r}: ИТОГ — не кандидат" in lines for r in ("V2", "V3", "V4"))
    assert any(ln.startswith("ИТОГ: кандидат V1 — главное правило") for ln in lines)


@pytest.mark.parametrize("change,which", [
    ({"V1.d_h1": "-0.01"}, 1), ({"V1.d_h2": "0"}, 1), ({"V1.d_h2": ""}, 1),
    ({"V1.p04": "0.0501"}, 2), ({"V1.p04": ""}, 2),
    ({"V1.c_SOL": "-0.1", "V1.c_XRP": "-0.1"}, 3),
    ({"V1.live_veto": "0.5"}, 4), ({"V1.live_veto": "0.6"}, 4), ({"V1.live_ok": ""}, 4),
])
def test_verdict_one_failed_criterion_is_enough_to_refuse(change, which) -> None:
    mod = load_module(VERDICT_PY, "verdict_one")
    facts = good_facts("V1") | change
    res = mod.evaluate(facts, "V1")
    assert res["candidate"] is False
    assert [n for n, (ok, _) in res["crit"].items() if not ok] == [which]


def test_verdict_four_of_five_coins_is_enough_and_p_exactly_0_05_passes() -> None:
    mod = load_module(VERDICT_PY, "verdict_edge")
    facts = good_facts("V1") | {"V1.c_DOGE": "-0.2", "V1.p04": "0.05"}
    assert mod.evaluate(facts, "V1")["candidate"] is True


def test_verdict_is_not_printed_when_a_data_control_fails() -> None:
    mod = load_module(VERDICT_PY, "verdict_ctl")
    for key in ("ctl1", "ctl2", "ctl3"):
        lines = verdict_lines(mod, good_facts("V1") | {key: "0"})
        assert lines[-1] == "ВЕРДИКТ НЕ ПЕЧАТАЕТСЯ: данные не прошли контроль"
        assert not any("КАНДИДАТ" in ln or "кандидат" in ln for ln in lines)
    lines = verdict_lines(mod, {})                      # блок 00 не отработал вовсе
    assert lines[-1].startswith("ВЕРДИКТ НЕ ПЕЧАТАЕТСЯ")


def test_verdict_when_no_rule_passes_the_veto_is_not_built() -> None:
    mod = load_module(VERDICT_PY, "verdict_none")
    facts = good_facts("V1") | {"V1.p04": "0.9"}
    assert any("вето не строится" in ln for ln in verdict_lines(mod, facts))


def test_verdict_when_only_another_rule_passes_v1_stays_the_main_one() -> None:
    mod = load_module(VERDICT_PY, "verdict_v2")
    lines = verdict_lines(mod, good_facts("V2"))
    assert any("V1 (главное по ТЗ) не прошло" in ln for ln in lines)


def test_verdict_with_two_passing_rules_marks_the_second_as_reference() -> None:
    mod = load_module(VERDICT_PY, "verdict_two")
    facts = good_facts("V1")
    for key, value in good_facts("V3").items():
        if key.startswith("V3."):
            facts[key] = value
    lines = verdict_lines(mod, facts)
    assert any("кандидат V1 — главное правило; справочно также: V3" in ln for ln in lines)


def test_mutation_significance_threshold_of_1_0_is_caught() -> None:
    mod = load_module(VERDICT_PY, "verdict_mut", replace=("ALPHA = 0.05 ", "ALPHA = 1.0  "))
    facts = good_facts("V1") | {"V1.p04": "0.9"}
    with pytest.raises(AssertionError):
        assert mod.evaluate(facts, "V1")["candidate"] is False


# --------------------------------------------------------------------------
# §8.8. Блок 06: «удалены хранением» и «настоящий пропуск»
# --------------------------------------------------------------------------


async def seed_06(pool) -> None:
    """Пять позиций BTC (as_of 2026-10-18 12:00, хранение 30 суток → граница 2026-09-18 12:00)."""
    # A: t24 = 09-11 10:00 — раньше границы, свечи нет → удалены хранением
    await mkpos(pool, "BTC/USDT", datetime(2026, 9, 10, 10, tzinfo=UTC))
    # B: t24 = 10-02 10:00 — позже границы, свечи нет → настоящий пропуск
    await mkpos(pool, "BTC/USDT", datetime(2026, 10, 1, 10, tzinfo=UTC))
    # C: t24 = 10-03 10:00 — свеча в 10:03 (в пределах ±5 мин) → со свечой
    await mkpos(pool, "BTC/USDT", datetime(2026, 10, 2, 10, tzinfo=UTC))
    await candle(pool, "BTC/USDT", datetime(2026, 10, 3, 10, 3, tzinfo=UTC), 100.0)
    # D: t24 = 10-18 20:00 — позже момента замера → в расчёт не входит
    await mkpos(pool, "BTC/USDT", datetime(2026, 10, 17, 20, tzinfo=UTC), status="open")
    # E: t24 = 10-06 09:00 — ближайшая свеча в 09:07 (вне ±5 мин) → настоящий пропуск
    await mkpos(pool, "BTC/USDT", datetime(2026, 10, 5, 9, tzinfo=UTC))
    await candle(pool, "BTC/USDT", datetime(2026, 10, 6, 9, 7, tzinfo=UTC), 100.0)
    # чужая версия не входит
    await mkpos(pool, "BTC/USDT", datetime(2026, 9, 10, 10, tzinfo=UTC), logic=6)


def part1(out: Out) -> dict:
    return out.table("1. Позиции версии 7")[0]


def assert_06_split(out: Out) -> None:
    row = part1(out)
    assert row["С прошедшими 24 ч"] == 4 and row["Со свечой"] == 1 and row["Без свечи"] == 3
    assert row["Удалены хранением"] == 1 and row["Настоящий пропуск"] == 2
    assert row["RETENTION_1M_DAYS"] == 30
    assert out.facts["c06.purged"] == "1" and out.facts["c06.real_gaps"] == "2"


async def test_06_splits_positions_into_purged_and_real_gaps(pool, db_name) -> None:
    await seed_06(pool)
    out = run_block(db_name, "06_minute_gaps.sql")
    assert_06_split(out)
    assert "ДЕФЕКТ СБОРА" not in out.text                       # 2 пропуска — не больше пяти


async def test_06_the_boundary_follows_retention_days_from_the_env(pool, db_name) -> None:
    await seed_06(pool)
    out = run_block(db_name, "06_minute_gaps.sql", extra=("retention_days=10",))
    row = part1(out)              # граница 10-08 12:00: B (10-02) и E (10-06) уже вычищены
    assert row["Удалены хранением"] == 3 and row["Настоящий пропуск"] == 0
    assert row["RETENTION_1M_DAYS"] == 10


async def test_06_without_the_variable_the_default_is_30(pool, db_name) -> None:
    await seed_06(pool)
    assert_06_split(run_block(db_name, "06_minute_gaps.sql"))


async def test_06_more_than_five_real_gaps_is_a_collection_defect(pool, db_name) -> None:
    for k in range(6):
        await mkpos(pool, "BTC/USDT", datetime(2026, 10, 1, 10, tzinfo=UTC) + timedelta(hours=k))
    out = run_block(db_name, "06_minute_gaps.sql")
    assert part1(out)["Настоящий пропуск"] == 6 and "ДЕФЕКТ СБОРА" in out.text


async def test_06_lists_days_with_missing_minute_candles_and_missing_hours(pool, db_name) -> None:
    ids = await add_inst(pool, "BTC")
    # хранение 3 суток → смотрим 2 полных суток: 2026-10-16 и 2026-10-17
    rows = []
    for k in range(1440):
        if k >= 15:                                         # 16-го не хватает первых 15 минут
            rows.append((ids["BTC"], "1m", dts(date(2026, 10, 16)) + timedelta(minutes=k), 1.0))
        rows.append((ids["BTC"], "1m", dts(date(2026, 10, 17)) + timedelta(minutes=k), 1.0))
    await pool.executemany(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $4, $4, $4, 1);", rows)
    # часовые BTC с 2026-08-08 до 10:00 18 октября без двух часов; бар 11:00 в расчёт не входит
    hours, t = [], datetime(2026, 8, 8, tzinfo=UTC)
    gone = {datetime(2026, 9, 1, 5, tzinfo=UTC), datetime(2026, 9, 1, 6, tzinfo=UTC)}
    while t <= datetime(2026, 10, 18, 11, tzinfo=UTC):
        if t not in gone:
            hours.append((ids["BTC"], "1h", t, 1.0))
        t += timedelta(hours=1)
    await pool.executemany(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, $2, $3, $4, $4, $4, $4, 1);", hours)
    out = run_block(db_name, "06_minute_gaps.sql", extra=("retention_days=3",))
    btc = out.row("2. Минутные свечи", Монета="BTC")
    assert (btc["Суток с пропусками"], btc["Суток всего"], btc["Свечей нет"]) == (1, 2, 15)
    eth = out.row("2. Минутные свечи", Монета="ETH")
    assert (eth["Суток с пропусками"], eth["Свечей нет"]) == (2, 2880)    # данных нет вовсе
    assert "суток-монет с пропусками / всего = 9/10" in out.text
    assert len(out.table("2. Минутные свечи")) == 5
    days = out.table("Сутки с наибольшим числом")
    assert [d for d in days if d["Монета"] == "BTC"] == [
        {"Монета": "BTC", "Сутки (UTC)": "2026-10-16", "Нет свечей": 15.0}]
    assert days[0]["Нет свечей"] == 1440 and len(days) == 9
    hour = out.row("3. Часовые свечи", Монета="BTC")
    assert hour["Первый час"] == "2026-08-08 00:00" and hour["Последний час"] == "2026-10-18 10:00"
    assert hour["Нет часов"] == 2
    assert out.row("3. Часовые свечи", Монета="DOGE")["Есть"] == 0


async def test_06_the_list_of_days_is_capped_at_30_rows(pool, db_name) -> None:
    out = run_block(db_name, "06_minute_gaps.sql")      # 29 суток × 5 монет — свечей нет вовсе
    assert len(out.table("Сутки с наибольшим числом")) == 30
    assert "суток-монет с пропусками / всего = 145/145" in out.text


async def test_mutation_retention_boundary_inverted_is_caught(pool, db_name) -> None:
    await seed_06(pool)
    mutated = mutate("06_minute_gaps.sql", "ent.t24 < :'as_of'::timestamptz - make_interval",
                     "ent.t24 > :'as_of'::timestamptz - make_interval")
    with pytest.raises(AssertionError):
        assert_06_split(Out(run_sql(db_name, "", text=mutated)))


async def test_mutation_candle_window_of_30_minutes_is_caught(pool, db_name) -> None:
    await seed_06(pool)
    mutated = mutate("06_minute_gaps.sql",
                     "o.ts BETWEEN e.t24 - interval '5 minutes' AND e.t24 + interval '5 minutes'",
                     "o.ts BETWEEN e.t24 - interval '30 minutes' AND e.t24 + interval '30 minutes'")
    with pytest.raises(AssertionError):
        assert_06_split(Out(run_sql(db_name, "", text=mutated)))


# --------------------------------------------------------------------------
# §8.9. Миграция 032: применение → откат → повтор; чтение есть, записи нет
# --------------------------------------------------------------------------


def can(db: str, priv: str, table: str = "backtest.candles") -> bool:
    return psql_raw(db, f"SELECT has_table_privilege('agenttrade_ro', '{table}', '{priv}');") == "t"


def can_use_schema(db: str) -> bool:
    return psql_raw(db, "SELECT has_schema_privilege('agenttrade_ro', 'backtest', 'USAGE');") == "t"


def ro_psql(db: str, sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", "-X", "-q", "-h", HOST, "-p", PORT, "-U", "agenttrade_ro",
                           "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True, check=False)


def test_migration_032_applies_rolls_back_and_applies_again(db_name) -> None:
    db = build_database("mig032")            # роль уже есть (фикстура db_name): 032 выдаёт права
    try:
        assert can(db, "SELECT") and can_use_schema(db)
        psql_raw(db, stdin=MIGRATION.read_text(encoding="utf-8"))            # идемпотентна
        assert can(db, "SELECT") and can_use_schema(db)
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))
        assert not can(db, "SELECT") and not can_use_schema(db)
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))            # откат повторим
        psql_raw(db, stdin=MIGRATION.read_text(encoding="utf-8"))
        assert can(db, "SELECT") and can_use_schema(db)
    finally:
        drop_database(db)


def test_after_032_the_role_reads_candles_but_cannot_write_or_read_other_backtest_tables(
    db_name
) -> None:
    assert ro_psql(db_name, "SELECT count(*) FROM backtest.candles;").returncode == 0
    for sql in ("INSERT INTO backtest.candles (inst_id, bar, open_time, close_time, open, high, "
                "low, close, volume) VALUES ('X', '1H', now(), now(), 1, 1, 1, 1, 1);",
                "UPDATE backtest.candles SET volume = 0;", "DELETE FROM backtest.candles;",
                "TRUNCATE backtest.candles;"):
        res = ro_psql(db_name, sql)
        assert res.returncode != 0 and "permission denied" in res.stderr, sql
    for table in ("runs", "decisions", "funding", "gaps"):
        assert not can(db_name, "SELECT", f"backtest.{table}"), table
    assert not any(can(db_name, p) for p in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))


def test_migration_032_grants_nothing_but_usage_and_select_on_one_table() -> None:
    text = "\n".join(re.sub(r"--.*$", "", ln) for ln in MIGRATION.read_text("utf-8").splitlines())
    grants = re.findall(r"GRANT\s+[^;]+;", text, re.I)
    assert [g.upper().replace("\n", " ") for g in grants] == [
        "GRANT USAGE ON SCHEMA BACKTEST TO AGENTTRADE_RO;",
        "GRANT SELECT ON BACKTEST.CANDLES TO AGENTTRADE_RO;"]
    assert "pg_roles" in text and "agenttrade_ro" in text
    rollback = "\n".join(re.sub(r"--.*$", "", ln)
                         for ln in ROLLBACK.read_text("utf-8").splitlines())
    revokes = re.findall(r"REVOKE\s+[^;]+;", rollback, re.I)
    assert [r.upper().replace("\n", " ") for r in revokes] == [
        "REVOKE SELECT ON BACKTEST.CANDLES FROM AGENTTRADE_RO;",
        "REVOKE USAGE ON SCHEMA BACKTEST FROM AGENTTRADE_RO;"]


# --------------------------------------------------------------------------
# Заглушка docker и запуск скриптов целиком
# --------------------------------------------------------------------------


def make_stub(tmp_path: Path, db: str, *, ro_password: str | None = None,
              env_lines: tuple[str, ...] = ()) -> dict:
    """Каталог приложения с .env и ``docker``, который запускает psql против тестовой базы."""
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    lines = ["POSTGRES_DB=agenttrade", *env_lines]
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


def script_env(stub: dict, tmp_path: Path, extra=None) -> dict:
    env = {"PATH": f"{stub['bin']}:{os.environ['PATH']}", "APP_DIR": str(stub["app"]),
           "OUT_DIR": str(tmp_path / "out"), "HOME": str(tmp_path), "AS_OF": AS_OF_TXT}
    env.update(extra or {})
    return env


def run_script(script: Path, stub: dict, tmp_path: Path, *, env_extra=None, args=()):
    return subprocess.run(["bash", str(script), *args], env=script_env(stub, tmp_path, env_extra),
                          capture_output=True, text=True, check=False)


def fingerprint(db: str) -> str:
    """Число строк и контрольная сумма таблиц, в которые замер мог бы записать."""
    return psql_raw(
        db, "SELECT (SELECT count(*) FROM positions) || '/' || (SELECT count(*) FROM signals) "
            "|| '/' || (SELECT count(*) FROM demo_orders) || '/' || (SELECT count(*) FROM ohlcv) "
            "|| '/' || (SELECT count(*) FROM backtest.candles) || '/' || "
            "(SELECT md5(string_agg(p::text, ',' ORDER BY id)) FROM positions p);")


def report_of(tmp_path: Path) -> str:
    return (tmp_path / "out" / f"report_9_10_{datetime.now(UTC):%Y%m%d}.txt").read_text("utf-8")


def facts_of(tmp_path: Path) -> str:
    return (tmp_path / "out" / f"facts_9_10_{datetime.now(UTC):%Y%m%d}.txt").read_text("utf-8")


async def seed_e2e(pool, *, corrupt: bool = False) -> None:
    """Два года-подобных ряда в миниатюре: контроли проходят, позиции на сутках 50…58."""
    await seed_controls(pool, corrupt=corrupt)
    await add_inst(pool, "ETH")
    rnd = random.Random(3)
    for k in range(40):
        tok = ("BTC", "ETH")[k % 2]
        opened = dts(S0 + timedelta(days=50 + k % 9), 1 + k % 20)
        pct = (1.0, -2.0, 0.4, -0.5)[k % 4] + rnd.random() / 10
        await mkpos(pool, f"{tok}/USDT", opened, pct=pct, reason=("target", "timeout")[k % 2])
    await mkpos(pool, "BTC/USDT", dts(S0 + timedelta(days=58), 14), status="open")
    await mkpos(pool, "BTC/USDT", dts(S0 + timedelta(days=52), 14), reason="data_gap", pct=-9.0)
    await candle(pool, "BTC/USDT", AS_OF - timedelta(minutes=3), 99.0)


@needs_bash
async def test_run_9_10_makes_a_complete_read_only_report(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    before = fingerprint(db_name)
    res = run_script(RUN_9_10, stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_of(tmp_path)
    heads = ["00 — ДАННЫЕ И КОНТРОЛИ", "01 — ДОЛЯ ДНЕЙ ПОД ВЕТО", "02 — СДЕЛКИ ВЕРСИИ 7",
             "03 — СДЕЛКИ ВЕРСИИ 7 ПО НЕДЕЛЯМ", "04 — ИСТОРИЯ", "05 — ТЕСТ СДВИГОМ",
             "06 — ПОЛНОТА МИНУТНЫХ", "07 — ВЕРДИКТ"]
    pos = [report.index(f"===== {h}") for h in heads]
    assert pos == sorted(pos)                                   # блоки в порядке 00…07
    assert "с ошибками 0" in report and "ОШИБКА" not in report and "блоков 8" in report
    assert len(report.splitlines()) <= 300, len(report.splitlines())
    assert "@@" not in report                                   # машинные факты в отчёт не идут
    assert fingerprint(db_name) == before                       # замер ничего не записал
    facts = facts_of(tmp_path)
    assert "@@R04|" in facts and "@@R02|" in facts and "@@F|ctl1|1" in facts
    assert "@@F|V1.p04|" in facts
    rows_before = re.search(r"@@F\|rows_before\|(\S+)", facts).group(1)
    assert rows_before == re.search(r"@@F\|rows_after\|(\S+)", facts).group(1)
    assert "1. сутки из часов = 1Dutc биржи" in report and "✅" in report
    for rule in ("V1", "V2", "V3", "V4"):
        assert f"{rule}: ИТОГ —" in report                      # вердикт напечатан
    assert "RETENTION_1M_DAYS=30" in report


@needs_bash
async def test_run_9_10_does_not_print_a_verdict_when_the_data_control_fails(
    pool, db_name, tmp_path
) -> None:
    await seed_e2e(pool, corrupt=True)
    res = run_script(RUN_9_10, make_stub(tmp_path, db_name), tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_of(tmp_path)
    assert "ВЕРДИКТ НЕ ПЕЧАТАЕТСЯ: данные не прошли контроль" in report
    assert "КАНДИДАТ" not in report and "ИТОГ —" not in report


@needs_bash
async def test_run_9_10_never_prints_the_password(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    secret = "Zq9$w\\x7!pass&word"
    stub = make_stub(tmp_path, db_name, ro_password=secret)
    # заглушка добавляет в вывод каждого блока строку с паролем — как сделал бы текст ошибки psql
    res = run_script(RUN_9_10, stub, tmp_path, env_extra={"STUB_LEAK": secret})
    assert res.returncode == 0, res.stdout + res.stderr
    report = report_of(tmp_path)
    assert secret not in res.stdout and secret not in res.stderr and secret not in report
    assert secret not in facts_of(tmp_path)
    assert "password=***" in report


@needs_bash
async def test_run_9_10_falls_back_to_the_password_connection(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name, ro_password="s3cret")
    res = run_script(RUN_9_10, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "127.0.0.1 с паролем роли" in res.stdout and "s3cret" not in res.stdout


@needs_bash
async def test_run_9_10_without_a_connection_says_what_to_check(pool, db_name, tmp_path) -> None:
    stub = make_stub(tmp_path, db_name)
    res = run_script(RUN_9_10, stub, tmp_path, env_extra={"STUB_SOCKET_FAIL": "1"})
    assert res.returncode != 0
    assert "не удалось подключиться к БД ролью agenttrade_ro" in res.stdout


@needs_bash
async def test_run_9_10_reports_a_failed_block_and_exits_nonzero(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    sandbox = tmp_path / "analysis"
    shutil.copytree(ROOT / "analysis", sandbox)
    broken = sandbox / "sql" / "9_10" / "03_weekly.sql"
    broken.write_text(
        sql_of("03_weekly.sql").replace("FROM closed", "FROM no_such_table", 1), "utf-8")
    res = run_script(sandbox / "run_9_10.sh", make_stub(tmp_path, db_name), tmp_path)
    assert res.returncode != 0
    assert "с ошибками 1" in res.stdout and "ОШИБКА: блок «03" in res.stdout


@needs_bash
async def test_run_9_10_tells_to_apply_migration_032_when_the_role_cannot_read(
    db_name, tmp_path
) -> None:
    db = build_database("nogrant")
    try:
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))
        res = run_script(RUN_9_10, make_stub(tmp_path, db), tmp_path)
        assert res.returncode != 0
        assert "нет права читать backtest.candles" in res.stdout
        assert "032_ro_backtest_read.sql" in res.stdout and "===== 00" not in res.stdout
    finally:
        drop_database(db)


@needs_bash
async def test_run_9_10_reads_retention_from_env_and_ignores_garbage(
    pool, db_name, tmp_path
) -> None:
    await seed_06(pool)
    stub = make_stub(tmp_path, db_name, env_lines=("RETENTION_1M_DAYS=10        # минутные",))
    res = run_script(RUN_9_10, stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "RETENTION_1M_DAYS=10" in report_of(tmp_path)
    assert re.search(r"\|\s+10\s*\n", report_of(tmp_path)) or " 10 " in report_of(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    stub = make_stub(bad, db_name, env_lines=("RETENTION_1M_DAYS=много",))
    res = run_script(RUN_9_10, stub, bad)
    assert "RETENTION_1M_DAYS=30" in report_of(bad) and "не подходит — взято 30" in res.stdout


@needs_bash
async def test_run_9_10_without_python_marks_blocks_05_and_07_as_failed(
    pool, db_name, tmp_path
) -> None:
    await seed_e2e(pool)
    res = run_script(RUN_9_10, make_stub(tmp_path, db_name), tmp_path,
                     env_extra={"PYTHON": "python3-no-such"})
    assert res.returncode != 0 and "с ошибками 2" in res.stdout


# --------------------------------------------------------------------------
# deploy/verify_9_10.sh
# --------------------------------------------------------------------------


def run_verify(stub: dict, tmp_path: Path):
    return subprocess.run(["bash", str(VERIFY_9_10)], env=script_env(stub, tmp_path),
                          capture_output=True, text=True, check=False)


@needs_bash
async def test_verify_9_10_is_all_green_after_a_clean_run(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name, ro_password="verify-pw")
    assert run_script(RUN_9_10, stub, tmp_path).returncode == 0
    res = run_verify(stub, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    for n in "12345":
        assert f"\n{n}. " in res.stdout
    assert len(re.findall(r"^  ✅", res.stdout, re.M)) == 5   # по пункту на каждую из пяти проверок
    assert "❌" not in res.stdout and "⚪" not in res.stdout
    assert "ИТОГ: ВСЕ ✅" in res.stdout


@needs_bash
async def test_verify_9_10_does_not_claim_all_green_without_a_report(
    pool, db_name, tmp_path
) -> None:
    res = run_verify(make_stub(tmp_path, db_name), tmp_path)
    assert "ИТОГ: ВСЕ ✅" not in res.stdout and not re.search(r"^  ✅.*отчёт", res.stdout, re.M)
    assert res.stdout.count("⚪ НЕ ПРОВЕРЕНО") == 3          # пункты 3, 4, 5
    assert "ИТОГ: ⚪" in res.stdout and res.returncode == 0


@needs_bash
async def test_verify_9_10_item_1_and_2_see_a_missing_or_excessive_grant(db_name, tmp_path) -> None:
    db = build_database("vgrant")
    try:
        stub = make_stub(tmp_path, db)
        psql_raw(db, stdin=ROLLBACK.read_text(encoding="utf-8"))
        res = run_verify(stub, tmp_path)
        assert res.returncode == 1 and "❌ у роли agenttrade_ro нет права читать" in res.stdout
        psql_raw(db, stdin=MIGRATION.read_text(encoding="utf-8"))
        psql_raw(db, "GRANT INSERT ON backtest.candles TO agenttrade_ro;")
        res = run_verify(stub, tmp_path)
        assert res.returncode == 1 and "есть права записи на backtest.candles: INSERT" in res.stdout
    finally:
        drop_database(db)


@needs_bash
async def test_verify_9_10_flags_a_password_in_the_report_and_a_dead_control(
    pool, db_name, tmp_path
) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name, ro_password="verify-pw")
    assert run_script(RUN_9_10, stub, tmp_path).returncode == 0
    report = next((tmp_path / "out").glob("report_9_10_*.txt"))
    original = report.read_text("utf-8")
    report.write_text(original + "\nверный пароль verify-pw попал в отчёт\n", encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "найден пароль роли" in res.stdout
    dead = re.sub(r"(2\. то же при сдвиге суток на 1 ч\s+\|\s+\d+\s+\|\s+)\d+", r"\g<1>0", original)
    assert dead != original
    report.write_text(dead, encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "контрольный опыт не дал расхождений" in res.stdout


@needs_bash
async def test_verify_9_10_flags_a_changed_row_count(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    assert run_script(RUN_9_10, stub, tmp_path).returncode == 0
    facts = next((tmp_path / "out").glob("facts_9_10_*.txt"))
    facts.write_text(re.sub(r"(@@F\|rows_after\|)(\d+)", lambda m: f"{m[1]}{int(m[2]) + 1}",
                            facts.read_text("utf-8")), encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "число строк разошлось" in res.stdout


@needs_bash
async def test_verify_9_10_flags_a_report_that_is_too_long(pool, db_name, tmp_path) -> None:
    await seed_e2e(pool)
    stub = make_stub(tmp_path, db_name)
    assert run_script(RUN_9_10, stub, tmp_path).returncode == 0
    report = next((tmp_path / "out").glob("report_9_10_*.txt"))
    report.write_text(report.read_text("utf-8") + "лишняя строка\n" * 300, encoding="utf-8")
    res = run_verify(stub, tmp_path)
    assert res.returncode == 1 and "допустимо 300" in res.stdout


# --------------------------------------------------------------------------
# Свойства файлов
# --------------------------------------------------------------------------

FRAGMENTS = {
    "ряды": ("-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.1–4.2",
              "-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ РЯДОВ ==="),
    "флаги": ("-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §4.3",
               "-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ФЛАГОВ ==="),
    "позиции": ("-- === ОБЩИЕ ОПРЕДЕЛЕНИЯ ТЗ 9.10 §2",
                 "-- === КОНЕЦ ОБЩИХ ОПРЕДЕЛЕНИЙ ПОЗИЦИЙ ==="),
}
USES = {"ряды": BLOCK_FILES[:5], "флаги": BLOCK_FILES[1:5], "позиции": BLOCK_FILES[2:4]}


def fragment(text: str, name: str) -> str:
    start, end = FRAGMENTS[name]
    assert start in text and end in text, name
    return text.split(start, 1)[1].split(end)[0]


def test_the_block_files_are_exactly_the_planned_ones() -> None:
    assert sorted(p.name for p in SQL_DIR.glob("*.sql")) == sorted(BLOCK_FILES)


@pytest.mark.parametrize("name", FRAGMENTS)
def test_the_shared_definitions_are_identical_in_every_block(name: str) -> None:
    texts = {n: fragment(sql_of(n), name) for n in USES[name]}
    assert len(set(texts.values())) == 1, f"определения «{name}» в файлах блоков разошлись"
    body = next(iter(texts.values()))
    must = {"ряды": ("bar = '1H'", "o.timeframe = '1h'", "interval '1 hour'", "n_h = 24",
                      "'-infinity'", "('BTC'), ('ETH'), ('SOL'), ('XRP'), ('DOGE')"),
            "флаги": ("50 PRECEDING AND 1 PRECEDING", "20 PRECEDING AND 1 PRECEDING",
                       "25 PRECEDING AND 6 PRECEDING", "8 PRECEDING AND 1 PRECEDING",
                       "n50 = 50", "n20a = 20", "n20b = 20", "n8 = 8", "lag(close, 8)"),
            "позиции": ("logic_version = 7", "is_virtual", "IS DISTINCT FROM 'data_gap'",
                         "b.token = 'BTC'", "o.token = v.token")}[name]
    for item in must:
        assert item in body, item
    if name == "флаги":
        assert "CURRENT ROW" not in body and "FOLLOWING" not in body      # будущего окна нет
    other = [n for n in BLOCK_FILES if n not in USES[name]]
    for n in other:
        assert FRAGMENTS[name][0] not in sql_of(n), (n, name)


def test_blocks_01_to_04_and_00_use_the_same_series_but_06_has_its_own_selection() -> None:
    assert "backtest.candles" not in sql_of("06_minute_gaps.sql")
    for n in BLOCK_FILES[:5]:
        assert "backtest.candles" in sql_of(n), n


def test_every_block_forces_a_read_only_session_before_the_first_query() -> None:
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
        code = "\n".join(re.sub(r"--.*$", "", ln) for ln in sql_of(name).splitlines()
                         if not ln.startswith("\\echo"))
        code = re.sub(r"'[^']*'", "''", code)
        code = re.sub(r"SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", "", code)
        assert not forbidden.search(code), (name, forbidden.search(code).group(0))


async def test_a_write_in_the_read_only_session_is_refused(db_name) -> None:
    """Страховка из шапки файла: после неё даже владелец базы не может записать."""
    header = sql_of("00_data.sql").split("\\if :{?as_of}")[0]
    res = subprocess.run(
        ["psql", "-X", "-q", "-h", HOST, "-p", PORT, "-U", USER, "-d", db_name,
         "-v", "ON_ERROR_STOP=1"],
        input=header + "INSERT INTO instruments (exchange,symbol,base,quote,type) "
                       "VALUES ('okx','ZZZ/USDT','ZZZ','USDT','spot');\n",
        capture_output=True, text=True, check=False)
    assert res.returncode != 0 and "read-only" in res.stderr


def test_constants_of_the_tz_are_written_literally() -> None:
    code = "\n".join(re.sub(r"--.*$", "", ln) for ln in sql_of("00_data.sql").splitlines())
    assert code.count("dev > 5e-4") == 3              # допуск §4.4 — в каждом из трёх контролей
    assert "5e-4" not in sql_of("04_history_returns.sql")
    half = sql_of("04_history_returns.sql")
    assert half.count("date '2024-04-01'") == 2               # граница половин — только эта дата
    assert "0.0001" in sql_of("02_live_trades.sql")           # «прибыльная» — net_pnl_pct > 0.0001
    assert "1.96" in sql_of("02_live_trades.sql")
    assert "make_interval(days => :retention_days)" in sql_of("06_minute_gaps.sql")
    for name in BLOCK_FILES:
        assert "retention_days" not in sql_of(name) or name.startswith("06")


def test_only_the_rules_v1_to_v4_exist() -> None:
    for name in BLOCK_FILES[1:5]:
        text = sql_of(name)
        assert not re.search(r"\bV[05-9]\b", text), name


def test_the_run_script_and_the_helpers_pass_bash_n_and_python_compile() -> None:
    for path in (RUN_9_10, VERIFY_9_10):
        res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, (path.name, res.stderr)
    for path in (SHIFT_PY, VERDICT_PY):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_the_run_script_changes_nothing_on_the_server() -> None:
    body = RUN_9_10.read_text("utf-8")
    code_only = "\n".join(re.sub(r"#.*$", "", ln) for ln in body.splitlines())
    for forbidden in ("docker compose restart", "docker compose up", "docker compose build",
                      "git pull", "pip install", "INSERT", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
        assert forbidden not in code_only, forbidden
    assert 'source "$LIB_DIR/lib_ro.sh"' in body and "agenttrade_ro" in body
    assert "redact" in body and "RETENTION_1M_DAYS" in body
    verify = VERIFY_9_10.read_text("utf-8")
    assert 'source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_verify.sh"' in verify
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in verify.splitlines())
    for forbidden in ("INSERT INTO", "DELETE FROM", "DROP ", "ALTER ", "TRUNCATE TABLE",
                      "docker compose restart"):
        assert forbidden not in code, forbidden


# --------------------------------------------------------------------------
# §8.11. Существующие замеры не тронуты
# --------------------------------------------------------------------------

UNTOUCHED = ["analysis/run_7_1.sh", "analysis/run_9_9.sh", "analysis/lib_ro.sh", "analysis/sql/9_9",
             "analysis/sql/00_schema.sql", "analysis/sql/01_baseline.sql",
             "tests/golden/run_7_1_before_9_9.sh"]


def test_run_7_1_run_9_9_and_the_earlier_packages_are_unchanged() -> None:
    res = subprocess.run(["git", "diff", "--stat", BASE_COMMIT, "--", *UNTOUCHED], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.skip("нет git или базового коммита — сверка дифференциала невозможна")
    assert res.stdout.strip() == "", res.stdout


def test_existing_migrations_and_the_source_tree_are_unchanged() -> None:
    res = subprocess.run(["git", "diff", "--stat", BASE_COMMIT, "--", "src", "backtest", "scripts",
                          "docker-compose.yml", ".env.example", "Dockerfile", "requirements.txt",
                          *[f"db/migrations/0{n}_*" for n in range(7, 32)]], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.skip("нет git или базового коммита — сверка дифференциала невозможна")
    assert res.stdout.strip() == "", res.stdout


def test_logic_version_is_untouched() -> None:
    from tests.conftest import CURRENT_LOGIC_VERSION

    assert CURRENT_LOGIC_VERSION == 7
    assert "LOGIC_VERSION" not in RUN_9_10.read_text("utf-8")
