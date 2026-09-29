"""Этап 9.4, блок D: свёртка таблиц замеров, удаление деталей, уплотнение.

SQL проверяется на НАСТОЯЩЕМ PostgreSQL: подставная база не поймала бы ни ошибку
синтаксиса, ни расхождение округления. Сервер берётся из PGTEST_HOST / PGTEST_PORT /
PGTEST_USER (по умолчанию сокет /tmp, порт 54329); нет сервера — тесты пропускаются.
Схемы таблиц загружаются из настоящих миграций 016, 017 и 029.
"""

from __future__ import annotations

import os
import random
import statistics
import subprocess
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from scripts import measure_rollup as mr
from scripts import retention

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("PGTEST_HOST", "/tmp")
PORT = os.environ.get("PGTEST_PORT", "54329")
USER = os.environ.get("PGTEST_USER", "agenttrade")


def _psql_raw(db: str, *sqls: str, stdin: str | None = None) -> str:
    cmd = ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", db, "-t", "-A",
           "-v", "ON_ERROR_STOP=1"]
    if stdin is not None:
        cmd += ["-q", "-f", "-"]
    for sql in sqls:
        cmd += ["-c", sql]
    res = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res.stdout.strip()


def _server_up() -> bool:
    try:
        _psql_raw("postgres", "SELECT 1;")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason="нет PostgreSQL для проверки SQL")

TODAY = datetime.now(UTC).date()
CUTOFF = TODAY - timedelta(days=mr.SETTLE_DAYS)
FIRST_DAY = TODAY - timedelta(days=12)


def _version_for(day: date) -> int:
    if day <= TODAY - timedelta(days=9):
        return 5
    if day <= TODAY - timedelta(days=6):
        return 6
    return 7


def _ts(day: date, hour: int, minute: int) -> str:
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:07+00"


def _build_data(rng: random.Random) -> str:
    """SQL наполнения: сигналы, детали обеих таблиц, окна версий."""
    out: list[str] = [
        "INSERT INTO instruments (exchange, symbol, base, quote) VALUES "
        "('okx','A/USDT','A','USDT'), ('okx','B/USDT','B','USDT');",
        f"INSERT INTO logic_version_windows (logic_version, started_at) VALUES "
        f"(5, '{FIRST_DAY}'), (6, '{TODAY - timedelta(days=8)}'), "
        f"(7, '{TODAY - timedelta(days=5)}');",
    ]
    sig_id = 0
    trailing: list[str] = []
    strategy: list[str] = []
    signals: list[str] = []
    day = FIRST_DAY
    while day < TODAY:
        for k in range(8):
            sig_id += 1
            inst = 1 + (k % 2)
            version = _version_for(day)
            # Сутки смены версии: в день TODAY-9 часть сигналов уже версии 6.
            if day == TODAY - timedelta(days=9) and k >= 4:
                version = 6
            ts = _ts(day, 1 + k * 2, 1 + k)
            signals.append(f"({sig_id}, {inst}, '{ts}', {version})")
            for horizon in (1, 4):
                incomplete = rng.random() < 0.03
                special = rng.choice([None] * 12 + ["ambiguous", "no_data"])
                for vi, (act, ret) in enumerate(mr.VARIANTS):
                    if incomplete and vi == 5:
                        continue
                    reason = rng.choice(["target", "stop", "timeout"] if act == 0
                                        else ["trail", "stop", "timeout"])
                    if special and vi == 3:
                        reason = special
                    if reason in ("ambiguous", "no_data"):
                        pnl, hit, bars = "NULL", "NULL", "NULL"
                    else:
                        pnl = f"{round(rng.uniform(-2.5, 3.0), 6)}"
                        if reason == "timeout":
                            hit, bars = "NULL", "NULL"
                        else:
                            hit = f"'{ts}'::timestamptz + interval '1 hour'"
                            bars = str(rng.randint(1, 60))
                    trailing.append(
                        f"({sig_id}, {horizon}, {act}, {ret}, {version}, 'buy', 100, 2, 1.5, 0.1, "
                        f"'{reason}', {hit}, {bars}, {pnl}, {round(rng.uniform(0, 3), 6)}, "
                        f"{round(rng.uniform(-2, 0), 6)}, {round(rng.uniform(0, 3), 6)}, "
                        f"'{rng.choice(['1m', '1h'])}', "
                        f"'{ts}'::timestamptz + interval '{rng.randint(1, 40)} hours')")
                for strat in mr.PAIRED_STRATEGIES:
                    outcome = rng.choice(["target", "stop", "timeout", "ambiguous", "no_data"])
                    if outcome in ("ambiguous", "no_data"):
                        pnl, hit = "NULL", "NULL"
                    else:
                        pnl = f"{round(rng.uniform(-2.5, 3.0), 6)}"
                        hit = ("NULL" if outcome == "timeout"
                               else f"'{ts}'::timestamptz + interval '1 hour'")
                    seed = "42" if strat == "coin_flip" else "NULL"
                    strategy.append(
                        f"('{strat}', {inst}, '{ts}', {horizon}, {sig_id}, {version}, "
                        f"'{rng.choice(['buy', 'sell'])}', 100, 2, 'frozen', 1.5, 0.1, "
                        f"'{outcome}', "
                        f"{hit}, {pnl}, -1, 1, '{rng.choice(['1m', '1h'])}', {seed})")
        for hour in (3, 9):
            for inst in (1, 2):
                for grid in ("grid_buy", "grid_sell"):
                    strategy.append(
                        f"('{grid}', {inst}, '{day.isoformat()} {hour:02d}:00:00+00', 1, NULL, "
                        f"{_version_for(day)}, 'buy', 100, 2, 'frozen', 1.5, 0.1, 'timeout', NULL, "
                        f"{round(rng.uniform(-1, 1), 6)}, -1, 1, '1h', NULL)")
        day += timedelta(days=1)
    out.append("INSERT INTO signals (id, instrument_id, ts, logic_version) VALUES "
               + ", ".join(signals) + ";")
    out.append(
        "INSERT INTO trailing_outcomes (signal_id, horizon_h, activation_ratio, retrace_ratio, "
        "logic_version, direction, price_at_signal, target_pct, stop_pct, cost_pct, exit_reason, "
        "hit_at, bars_to_hit, net_pnl_pct, peak_pct, mae_pct, mfe_pct, resolution, computed_at) "
        "VALUES " + ", ".join(trailing) + ";")
    out.append(
        "INSERT INTO strategy_outcomes (strategy, instrument_id, entry_ts, horizon_h, signal_id, "
        "logic_version, direction, price_at_entry, target_pct, target_source, stop_pct, cost_pct, "
        "outcome, hit_at, net_pnl_pct, mae_pct, mfe_pct, resolution, seed) VALUES "
        + ", ".join(strategy) + ";")
    return "\n".join(out)


BASE_SCHEMA = """
CREATE TABLE instruments (id SERIAL PRIMARY KEY, exchange text, symbol text, base text,
                          quote text, type text DEFAULT 'spot');
CREATE TABLE signals (id BIGSERIAL PRIMARY KEY,
                      instrument_id INT NOT NULL REFERENCES instruments(id),
                      ts TIMESTAMPTZ NOT NULL, logic_version SMALLINT NOT NULL DEFAULT 1);
CREATE INDEX idx_signals_ts ON signals (ts DESC);
CREATE TABLE logic_version_windows (logic_version SMALLINT PRIMARY KEY CHECK (logic_version > 0),
                                    started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ);
"""


class Env:
    """База-песочница и функция ``psql`` в том же виде, что у retention._psql."""

    def __init__(self, name: str) -> None:
        self.name = name

    def psql(self, *sqls: str) -> str:
        return _psql_raw(self.name, *sqls)

    def exec_file(self, path: Path) -> None:
        _psql_raw(self.name, stdin=path.read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path):
    name = "mtest_" + uuid.uuid4().hex[:10]
    _psql_raw("postgres", f"CREATE DATABASE {name};")
    e = Env(name)
    try:
        _psql_raw(name, stdin=BASE_SCHEMA)
        for mig in ("016_strategy_outcomes", "017_trailing_outcomes", "029_measure_rollups"):
            e.exec_file(ROOT / "db" / "migrations" / f"{mig}.sql")
        _psql_raw(name, stdin=_build_data(random.Random(94)))
        yield e
    finally:
        _psql_raw("postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")


@pytest.fixture()
def cron(tmp_path):
    """Каталог cron без писателей замеров."""
    d = tmp_path / "cron.d"
    d.mkdir()
    (d / "agent-trade").write_text("# 40 4 * * * agent ... python -m src.trailing_main\n"
                                   "0 4 * * 0 agent docker_gc.sh\n", encoding="utf-8")
    return d


def _count(env: Env, sql: str) -> int:
    return int(env.psql(sql) or 0)


def _rollup_all(env: Env) -> None:
    for t in (mr.TRAILING, mr.STRATEGY):
        mr.rollup(env.psql, t)


# --- Свёртка -----------------------------------------------------------------


def test_варианты_совпадают_с_правилом_подвижного_выхода():
    from src.trailing.rule import VARIANTS
    assert tuple(VARIANTS) == mr.VARIANTS


def test_свёртка_только_завершённые_сутки_и_идемпотентна(env):
    res = mr.rollup(env.psql, mr.TRAILING)
    assert res.days and max(res.days) <= CUTOFF and min(res.days) == FIRST_DAY
    assert len(res.days) == (CUTOFF - FIRST_DAY).days + 1
    first = env.psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "
                     f"FROM {mr.TRAILING_DAILY} t;")
    again = mr.rollup(env.psql, mr.TRAILING)
    assert again.days == []                        # второй запуск ничего не делает
    assert env.psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "
                    f"FROM {mr.TRAILING_DAILY} t;") == first
    # Несвёрнутые сутки (позже границы) в итогах отсутствуют.
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING_DAILY} "
                       f"WHERE day > '{CUTOFF}'") == 0


def test_logic_version_в_ключе_сутки_смены_дают_две_строки(env):
    _rollup_all(env)
    day = TODAY - timedelta(days=9)
    versions = env.psql(f"SELECT DISTINCT logic_version FROM {mr.TRAILING_DAILY} "
                        f"WHERE day = '{day}' ORDER BY 1;").split()
    assert versions == ["5", "6"]
    versions = env.psql(f"SELECT DISTINCT logic_version FROM {mr.STRATEGY_DAILY} "
                        f"WHERE day = '{day}' ORDER BY 1;").split()
    assert versions == ["5", "6"]


def test_итоги_trailing_совпадают_с_независимым_расчётом(env):
    _rollup_all(env)
    day = TODAY - timedelta(days=10)
    rows = env.psql(
        "SELECT t.exit_reason, t.net_pnl_pct, t.peak_pct, t.bars_to_hit, t.resolution "
        f"FROM {mr.TRAILING} t JOIN signals s ON s.id = t.signal_id "
        f"WHERE s.instrument_id = 1 AND t.horizon_h = 1 AND t.activation_ratio = 0.25 "
        f"AND t.retrace_ratio = 0.20 AND s.ts >= '{day}' AND s.ts < '{day + timedelta(days=1)}';")
    parsed = [r.split("|") for r in rows.splitlines()]
    pnl = [float(r[1]) for r in parsed if r[1]]
    got = env.psql(
        f"SELECT n_rows, n_trail, n_stop, n_timeout, n_pnl, avg_net_pnl, p25_net_pnl, "
        f"p50_net_pnl, p75_net_pnl, n_res_1m, n_res_1h FROM {mr.TRAILING_DAILY} "
        f"WHERE day = '{day}' AND instrument_id = 1 AND horizon_h = 1 "
        f"AND activation_ratio = 0.25 AND retrace_ratio = 0.20;").split("|")
    assert int(got[0]) == len(parsed)
    assert int(got[1]) == sum(r[0] == "trail" for r in parsed)
    assert int(got[2]) == sum(r[0] == "stop" for r in parsed)
    assert int(got[3]) == sum(r[0] == "timeout" for r in parsed)
    assert int(got[4]) == len(pnl)
    assert float(got[5]) == pytest.approx(statistics.mean(pnl), abs=1e-6)
    if len(pnl) >= 2:
        q1, q2, q3 = statistics.quantiles(pnl, n=4, method="inclusive")
        assert float(got[6]) == pytest.approx(q1, abs=2e-6)
        assert float(got[7]) == pytest.approx(q2, abs=2e-6)
        assert float(got[8]) == pytest.approx(q3, abs=2e-6)
    assert int(got[9]) == sum(r[4] == "1m" for r in parsed)
    assert int(got[10]) == sum(r[4] == "1h" for r in parsed)


def test_компактная_trailing_воспроизводит_отбор_пар_trailing_stats(env):
    """Компактная таблица даёт ТО ЖЕ, что ``scripts.trailing_stats.collect`` по деталям."""
    from scripts.trailing_stats import collect
    _rollup_all(env)
    pairs = env.psql(f"SELECT signal_id, horizon_h FROM {mr.TRAILING_COMPACT} "
                     f"ORDER BY 1, 2;").splitlines()
    assert len(pairs) > 100
    seen_reasons: set[str] = set()
    for line in pairs:
        sid, hor = (int(x) for x in line.split("|"))
        det = env.psql(
            "SELECT t.signal_id, t.horizon_h, t.activation_ratio, t.retrace_ratio, "
            f"t.exit_reason, t.net_pnl_pct, s.ts FROM {mr.TRAILING} t JOIN signals s "
            f"ON s.id = t.signal_id WHERE t.signal_id = {sid} AND t.horizon_h = {hor};")
        rows = [{"signal_id": int(c[0]), "horizon_h": int(c[1]),
                 "activation_ratio": float(c[2]), "retrace_ratio": float(c[3]),
                 "exit_reason": c[4], "net_pnl_pct": float(c[5]) if c[5] else None,
                 "ts": c[6]} for c in (r.split("|") for r in det.splitlines())]
        kept, dropped = collect(rows)
        cols = ", ".join(f"v{i:02d}" for i in range(13))
        comp = env.psql(f"SELECT exclude_reason, n_rows, {cols} "
                        f"FROM {mr.TRAILING_COMPACT} WHERE signal_id = {sid} "
                        f"AND horizon_h = {hor};").split("|")
        reason, n_rows, values = comp[0], int(comp[1]), comp[2:]
        assert n_rows == len(rows)
        if kept:
            assert reason == ""                                   # пара полная
            expected = [round(kept[0]["pnl"][v] * mr.PNL_SCALE) for v in mr.VARIANTS]
            assert [int(x) for x in values] == expected           # до знака, без потери
        else:
            (only,) = [k for k, v in dropped.items() if v]
            assert reason == only                                 # та же причина отсева
            assert all(x == "" for x in values)
            seen_reasons.add(only)
    assert {"incomplete", "no_data", "ambiguous"} & seen_reasons   # выборка нетривиальна


def test_компактная_strategy_несёт_итоги_четырёх_стратегий(env):
    _rollup_all(env)
    row = env.psql(f"SELECT system_pnl, always_buy_pnl, always_sell_pnl, coin_flip_pnl "
                   f"FROM {mr.STRATEGY_COMPACT} WHERE signal_id = 1 AND horizon_h = 1;").split("|")
    det = dict(r.split("|") for r in env.psql(
        f"SELECT strategy, net_pnl_pct FROM {mr.STRATEGY} WHERE signal_id = 1 AND horizon_h = 1;"
    ).splitlines())
    for value, name in zip(row, mr.PAIRED_STRATEGIES, strict=True):
        if det[name] == "":
            assert value == ""
        else:
            assert int(value) == round(float(det[name]) * mr.PNL_SCALE)
    # Сетки в компактную таблицу не попадают.
    assert _count(env, f"SELECT count(*) FROM {mr.STRATEGY_COMPACT} c "
                       f"WHERE c.signal_id IS NULL") == 0


# --- План и правило поправок 1–2 ---------------------------------------------


def test_правило_trailing_всё_strategy_только_старые_версии(env):
    _rollup_all(env)
    current = mr.current_logic_version(env.psql)
    assert current == 7
    t = mr.plan_deletion(env.psql, mr.TRAILING, current=current)
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=current)
    # trailing (заморожена): под удаление все завершённые сутки ВСЕХ версий, включая текущую.
    assert {e.logic_version for e in t.eligible} == {5, 6, 7}
    assert t.mismatched == []
    assert t.kept_by_rule == 0
    # strategy: версия 7 остаётся ПОЛНОСТЬЮ, а версии 5 и 6 уходят независимо от возраста.
    assert {e.logic_version for e in s.eligible} == {5, 6}
    assert s.per_version[7][1] == 0 and s.per_version[7][0] > 0
    assert s.kept_by_rule == s.per_version[7][0]
    assert s.mismatched == []
    # Строки несвёрнутых суток (TODAY-2, TODAY-1) в удаляемое не входят.
    assert t.unrolled > 0 and s.unrolled > 0


def test_переход_на_новую_версию_переводит_предыдущую_в_удаляемые(env):
    _rollup_all(env)
    env.psql(f"INSERT INTO logic_version_windows (logic_version, started_at) "
             f"VALUES (8, '{TODAY}');")
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=mr.current_logic_version(env.psql),)
    assert {e.logic_version for e in s.eligible} == {5, 6, 7}


def test_календарный_потолок_не_применяется_нигде(env):
    """Решение 29.09.2026: граница хранения — только версия логики, потолка по возрасту нет."""
    _rollup_all(env)
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=7)
    assert 7 not in {e.logic_version for e in s.eligible}
    assert not hasattr(mr, "CAPPED_TABLES")
    assert "cap_days" not in mr.plan_deletion.__code__.co_varnames
    note = mr.retention_days_note(30)
    assert "не применяется" in note and "решение 29.09.2026" in note


def test_расхождение_счёта_блокирует_удаление_только_этих_суток(env):
    _rollup_all(env)
    day = TODAY - timedelta(days=11)
    env.psql(f"UPDATE {mr.TRAILING_DAILY} SET n_rows = n_rows + 1 WHERE day = '{day}' "
             f"AND instrument_id = 1 AND horizon_h = 1 AND activation_ratio = 0.25 "
             f"AND retrace_ratio = 0.20;")
    plan = mr.plan_deletion(env.psql, mr.TRAILING, current=7)
    assert day in {d for d, _, _ in plan.mismatched}
    assert day not in {e.day for e in plan.eligible}
    assert len(plan.eligible) > 3                                     # остальные сутки целы
    # Порча компактной таблицы (сумма итогов) — тоже расхождение.
    _rollup_all(env)
    other = TODAY - timedelta(days=10)
    env.psql(f"UPDATE {mr.TRAILING_COMPACT} SET v00 = v00 + 1 WHERE day = '{other}' "
             f"AND exclude_reason IS NULL AND ctid = (SELECT min(ctid) FROM {mr.TRAILING_COMPACT} "
             f"WHERE day = '{other}' AND exclude_reason IS NULL);")
    plan = mr.plan_deletion(env.psql, mr.TRAILING, current=7)
    assert other in {d for d, _, _ in plan.mismatched}


def test_расхождение_компактной_strategy_блокирует_сутки(env):
    _rollup_all(env)
    day = TODAY - timedelta(days=11)
    env.psql(f"DELETE FROM {mr.STRATEGY_COMPACT} WHERE day = '{day}' AND signal_id = "
             f"(SELECT min(signal_id) FROM {mr.STRATEGY_COMPACT} WHERE day = '{day}');")
    plan = mr.plan_deletion(env.psql, mr.STRATEGY, current=7)
    assert day in {d for d, _, _ in plan.mismatched}


# --- Удаление ----------------------------------------------------------------


def _delete(env, cron, plans_confirm=None, **kw):
    current = mr.current_logic_version(env.psql)
    t = mr.plan_deletion(env.psql, mr.TRAILING, current=current)
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=current)
    return mr.delete_command(
        env.psql,
        confirm_trailing=t.delete_rows if plans_confirm is None else plans_confirm[0],
        confirm_strategy=s.delete_rows if plans_confirm is None else plans_confirm[1],
        batch=50, pause=0, cron_dir=str(cron), **kw), t, s


def test_удаление_слепок_свёрток_до_и_после_совпал_побайтно(env, cron, tmp_path):
    _rollup_all(env)
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    d1 = mr.snapshot(env.psql, str(before), CUTOFF)
    rc, t, s = _delete(env, cron)
    assert rc == 0
    d2 = mr.snapshot(env.psql, str(after), CUTOFF)
    assert before.read_bytes() == after.read_bytes()
    assert d1 == d2
    assert mr.is_armed(env.psql)
    # Удалены именно посчитанные строки.
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING} t JOIN signals s ON s.id = t.signal_id "
                       f"WHERE s.ts < '{CUTOFF + timedelta(days=1)}'") == 0
    assert _count(env, f"SELECT count(*) FROM {mr.STRATEGY} WHERE logic_version < 7") == 0
    # Текущая версия strategy и несвёрнутые сутки trailing остались.
    assert _count(env, f"SELECT count(*) FROM {mr.STRATEGY} "
                       f"WHERE logic_version = 7") == s.kept_by_rule + s.unrolled
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING}") == t.unrolled


def test_удаление_отклоняется_при_неверном_подтверждении(env, cron):
    _rollup_all(env)
    before = _count(env, f"SELECT count(*) FROM {mr.TRAILING}")
    with pytest.raises(mr.MeasureError, match="подтверждено"):
        _delete(env, cron, plans_confirm=(1, 1))
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING}") == before
    assert not mr.is_armed(env.psql)


def test_удаление_отклоняется_пока_писатели_в_cron(env, tmp_path):
    _rollup_all(env)
    d = tmp_path / "cron_on"
    d.mkdir()
    (d / "agent-trade").write_text(
        "40 4 * * * agent cd /x && docker compose run barrier python -m src.trailing_main\n"
        "25 4 * * * agent cd /x && docker compose run barrier python -m src.baseline_main\n",
        encoding="utf-8")
    with pytest.raises(mr.MeasureError, match="писатели"):
        mr.delete_command(env.psql, confirm_trailing=0, confirm_strategy=0,
                          batch=50, pause=0, cron_dir=str(d))
    # Закомментированная строка писателем не считается; пустой каталог — «неизвестно».
    assert mr.writers_active(str(d)) == {"trailing_main": True, "baseline_main": True}
    assert mr.writers_active(str(tmp_path / "нет_такого")) is None


def test_удаление_без_свёртки_невозможно(env, cron):
    """Сутки, которые не свёрнуты, в план не попадают: число подтверждения будет нулевым."""
    t = mr.plan_deletion(env.psql, mr.TRAILING, current=7)
    assert t.eligible == [] and t.delete_rows == 0
    assert all("≠ строк в свёртке 0" in why for _, _, why in t.mismatched)


def test_отказ_называет_несошедшееся_сравнение_и_числа(env, cron):
    """Отказ без диагностики — тупик: должно быть видно, КАКОЕ сравнение и с какими числами."""
    _rollup_all(env)
    t = mr.plan_deletion(env.psql, mr.TRAILING, current=7)
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=7)
    day = TODAY - timedelta(days=11)
    n = int(env.psql(f"SELECT sum(n_rows) FROM {mr.TRAILING_DAILY} WHERE day = '{day}';"))
    env.psql(f"UPDATE {mr.TRAILING_DAILY} SET n_rows = n_rows + 1 WHERE day = '{day}' "
             f"AND instrument_id = 1 AND horizon_h = 1 AND activation_ratio = 0.25 "
             f"AND retrace_ratio = 0.20;")
    env.psql(f"UPDATE {mr.TRAILING_COMPACT} SET v00 = v00 + 1 WHERE ctid = (SELECT min(ctid) "
             f"FROM {mr.TRAILING_COMPACT} WHERE day = '{day}' AND exclude_reason IS NULL);")
    with pytest.raises(mr.MeasureError) as err:
        mr.delete_command(env.psql, confirm_trailing=t.delete_rows,
                          confirm_strategy=s.delete_rows, batch=50, pause=0, cron_dir=str(cron))
    text = str(err.value)
    assert f"сутки {day}" in text
    assert f"строк деталей {n} ≠ строк в свёртке {n + 1}" in text     # первое сравнение
    assert "сумма итогов: в деталях" in text and "≠ в компактной таблице" in text  # третье
    assert "сверка не сошлась" in text and "НЕ штатная смена даты" in text
    assert f"подтверждено {t.delete_rows}, сейчас к удалению" in text
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING} t JOIN signals s "
                       f"ON s.id = t.signal_id WHERE s.ts < '{CUTOFF + timedelta(days=1)}'") > 0


def test_отказ_при_сдвиге_даты_не_путает_с_расхождением_сверки(env, cron):
    _rollup_all(env)
    t = mr.plan_deletion(env.psql, mr.TRAILING, current=7)
    s = mr.plan_deletion(env.psql, mr.STRATEGY, current=7)
    with pytest.raises(mr.MeasureError) as err:
        mr.delete_command(env.psql, confirm_trailing=t.delete_rows - 1,
                          confirm_strategy=s.delete_rows, batch=50, pause=0, cron_dir=str(cron))
    text = str(err.value)
    assert "Расхождений сверки нет" in text and "Повторите прогон" in text
    assert f"подтверждено {t.delete_rows - 1}, сейчас к удалению {t.delete_rows}" in text
    assert f"подтверждено {s.delete_rows}, сейчас к удалению {s.delete_rows} (совпало)" in text


def test_несвёрнутые_сутки_диагностируются_как_нет_в_свёртке(env):
    plan = mr.plan_deletion(env.psql, mr.STRATEGY, current=7)     # свёртка не строилась
    assert plan.delete_rows == 0 and plan.mismatched
    assert all("≠ строк в свёртке 0" in why for _, _, why in plan.mismatched)


def test_ночной_прогон_без_разрешения_ничего_не_удаляет(env, cron):
    before = _count(env, f"SELECT count(*) FROM {mr.TRAILING}") + _count(
        env, f"SELECT count(*) FROM {mr.STRATEGY}")
    assert mr.nightly(env.psql, cron_dir=str(cron), batch=50, pause=0)
    after = _count(env, f"SELECT count(*) FROM {mr.TRAILING}") + _count(
        env, f"SELECT count(*) FROM {mr.STRATEGY}")
    assert before == after
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING_DAILY}") > 0   # но свёртка есть


def test_ночной_прогон_с_разрешением_удаляет_по_правилу(env, cron):
    mr.rollup(env.psql, mr.TRAILING)
    mr.rollup(env.psql, mr.STRATEGY)
    mr.arm(env.psql)
    assert mr.nightly(env.psql, cron_dir=str(cron), batch=50, pause=0)
    assert _count(env, f"SELECT count(*) FROM {mr.STRATEGY} WHERE logic_version < 7") == 0
    assert _count(env, f"SELECT count(*) FROM {mr.STRATEGY} WHERE logic_version = 7") > 0


def test_ночной_прогон_пропускает_trailing_пока_писатель_в_cron(env, tmp_path):
    d = tmp_path / "cron_on"
    d.mkdir()
    (d / "agent-trade").write_text(
        "40 4 * * * agent docker compose run barrier python -m src.trailing_main\n",
        encoding="utf-8")
    mr.arm(env.psql)
    before = _count(env, f"SELECT count(*) FROM {mr.TRAILING}")
    assert mr.nightly(env.psql, cron_dir=str(d), batch=50, pause=0)
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING}") == before


def test_ночной_прогон_без_миграции_не_ошибка(env, cron):
    env.exec_file(ROOT / "db" / "migrations" / "029_measure_rollups_rollback.sql")
    assert mr.nightly(env.psql, cron_dir=str(cron), batch=50, pause=0) is True


def test_свёртка_после_удаления_не_возрождает_детали(env, cron):
    _rollup_all(env)
    _delete(env, cron)
    daily = _count(env, f"SELECT count(*) FROM {mr.TRAILING_DAILY}")
    assert mr.rollup(env.psql, mr.TRAILING).days == []
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING_DAILY}") == daily


# --- Уплотнение и размеры ----------------------------------------------------


def test_уплотнение_отказывает_без_места_и_с_писателями(env, cron, tmp_path):
    with pytest.raises(mr.MeasureError, match="свободного места"):
        mr.compact(env.psql, cron_dir=str(cron), avail_bytes=lambda: 1)
    d = tmp_path / "cron_on"
    d.mkdir()
    (d / "x").write_text("25 4 * * * agent python -m src.baseline_main\n", encoding="utf-8")
    with pytest.raises(mr.MeasureError, match="не сняты"):
        mr.compact(env.psql, cron_dir=str(d), avail_bytes=lambda: 10**12)


def test_уплотнение_возвращает_место_после_удаления(env, cron):
    _rollup_all(env)
    _delete(env, cron)
    before = _count(env, f"SELECT pg_total_relation_size('{mr.STRATEGY}')")
    assert mr.compact(env.psql, cron_dir=str(cron), avail_bytes=lambda: 10**12) == 0
    after = _count(env, f"SELECT pg_total_relation_size('{mr.STRATEGY}')")
    assert after < before
    deps = mr.dependents_report(env.psql)
    assert set(deps.values()) == {0}


def test_размеры_печатаются_числами(env, tmp_path):
    log = tmp_path / "sizes.log"
    text = mr.sizes(env.psql, "до_удаления", str(log))
    assert "[до_удаления] df:" in text and "trailing_outcomes: данные" in text
    assert "индексы" in log.read_text(encoding="utf-8")


def test_прогон_только_счёт_ничего_не_удаляет(env, capsys):
    before = _count(env, f"SELECT count(*) FROM {mr.TRAILING}")
    plans = mr.dry_run(env.psql)
    out = capsys.readouterr().out
    assert _count(env, f"SELECT count(*) FROM {mr.TRAILING}") == before
    assert f"--confirm-trailing {plans[mr.TRAILING].delete_rows}" in out
    assert "версия 7 (текущая)" in out and "размер: trailing_pairs_compact" in out


# --- Границы и ограждения ----------------------------------------------------


def test_обе_таблицы_остаются_защищёнными_общим_путём():
    assert {"trailing_outcomes", "strategy_outcomes"} <= retention.PROTECTED_TABLES
    with pytest.raises(retention.RetentionRuleError):
        retention._check_protected("trailing_outcomes", "")


def test_потолок_из_env_обязан_быть_числом():
    assert mr.retention_days("30") == 30
    for bad in (None, "", "0", "abc", "-5"):
        with pytest.raises(mr.MeasureError):
            mr.retention_days(bad)


def test_ручные_команды_взаимоисключающи_и_ночной_режим_без_флагов():
    args = retention.parse_args([])
    assert not (args.measure_dry_run or args.measure_delete or args.measure_compact)
    with pytest.raises(SystemExit):
        retention.parse_args(["--measure-dry-run", "--measure-delete"])
    assert retention.parse_args(["--measure-delete", "--confirm-trailing", "5",
                                 "--confirm-strategy", "7"]).confirm_trailing == 5


def test_delete_без_подтверждения_отклоняется_кодом_2(capsys):
    assert retention.main(["--measure-delete"]) == 2
    assert "--confirm-trailing" in capsys.readouterr().out
