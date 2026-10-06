"""Общие заглушки для тестов этапа 9.5: биржа, Redis, база, позиции.

Сети нет нигде: биржа — заглушка ``FakeExchange`` с тем же набором методов, что
использует сервис, и тем же устройством ответов, что у ccxt (проверено по ccxt
4.4.100 в §0.4 отчёта). База — НАСТОЯЩАЯ PostgreSQL: схема строится из
``db/init.sql`` и всех миграций по порядку, как это делает ``deploy/schema_drift.sh``.
Сервер берётся из PGTEST_HOST / PGTEST_PORT / PGTEST_USER (по умолчанию сокет /tmp,
порт 54329); нет сервера — тесты с базой пропускаются.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import ccxt.async_support as ccxt

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("PGTEST_HOST", "/tmp")
PORT = os.environ.get("PGTEST_PORT", "54329")
USER = os.environ.get("PGTEST_USER", "agenttrade")

SECRETS = ("KEY-abc123-SECRET-DO-NOT-LOG", "SEC-xyz789-SECRET-DO-NOT-LOG", "PASS-qwe456-SECRET")


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------


def psql_raw(db: str, *sqls: str, stdin: str | None = None) -> str:
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


def server_up() -> bool:
    try:
        psql_raw("postgres", "SELECT 1;")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def migration_files() -> list[Path]:
    return sorted(
        p for p in (ROOT / "db" / "migrations").glob("*.sql")
        if not p.name.endswith("_rollback.sql")
    )


def build_database(prefix: str = "demotest") -> str:
    """Создаёт чистую базу: ``init.sql`` и все миграции по порядку. Возвращает имя."""
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    psql_raw("postgres", f"CREATE DATABASE {name};")
    psql_raw(name, stdin=(ROOT / "db" / "init.sql").read_text(encoding="utf-8"))
    for path in migration_files():
        psql_raw(name, stdin=path.read_text(encoding="utf-8"))
    return name


def drop_database(name: str) -> None:
    psql_raw("postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE);")


async def open_pool(db: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=HOST, port=int(PORT), user=USER, database=db, min_size=1, max_size=4
    )


async def reset_data(pool: asyncpg.Pool) -> None:
    """Очищает данные теста и заводит два спотовых инструмента (BTC, DOGE)."""
    await pool.execute(
        "TRUNCATE demo_balance_daily, demo_orders, demo_state, positions, signals, "
        "instruments, ohlcv "
        "RESTART IDENTITY CASCADE;"
    )
    await pool.execute(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) VALUES "
        "('okx', 'BTC/USDT', 'BTC', 'USDT', 'spot'), "
        "('okx', 'DOGE/USDT', 'DOGE', 'USDT', 'spot'), "
        "('okx', 'FOO/USDT', 'FOO', 'USDT', 'spot');"
    )


async def add_position(
    pool: asyncpg.Pool,
    *,
    symbol: str = "BTC/USDT",
    opened_at: datetime,
    status: str = "open",
    entry_price: float = 60000.0,
    notional: float = 2.0,
    closed_at: datetime | None = None,
    exit_price: float | None = None,
    exit_reason: str = "target",
    net_pnl_usd: float | None = None,
    logic_version: int = 7,
) -> int:
    """Вставляет сигнал и позицию (версия 7: без предела убытка). Возвращает id позиции."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol = $1;", symbol)
    sig = await pool.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, $2, 'buy', $3) RETURNING id;",
        inst["id"], opened_at, logic_version,
    )
    closed = status == "closed"
    exit_px = exit_price if exit_price is not None else entry_price * 1.01
    pnl_usd = net_pnl_usd if net_pnl_usd is not None else 0.01
    return await pool.fetchval(
        "INSERT INTO positions (instrument_id, signal_id, logic_version, horizon_h, "
        "side, is_virtual, status, signal_ts, signal_price, opened_at, entry_price, "
        "entry_lag_sec, entry_slippage_pct, qty, notional_usd, target_pct, "
        "target_price, cost_pct, deadline_at, resolution, closed_at, exit_price, "
        "exit_reason, outcome_certain, net_pnl_pct, net_pnl_usd) "
        "VALUES ($1, $2, $3, 24, 'buy', TRUE, $4, $5, $6, $5, $6, 5, 0, "
        "$7::numeric / $6::numeric, $7::numeric, 1, $6::numeric * 1.01, 0.22, "
        "$5::timestamptz + interval '48 hours', '1m', $8, $9, $10, $11, $12, $13) RETURNING id;",
        inst["id"], sig, logic_version, status, opened_at, entry_price, notional,
        closed_at if closed else None,
        exit_px if closed else None,
        exit_reason if closed else None,
        True if closed else None,
        0.5 if closed else None,
        pnl_usd if closed else None,
    )


async def insert_buy_filled(
    pool: asyncpg.Pool, position_id: int, *, symbol: str = "BTC/USDT",
    filled_qty: str = "0.00003332", fee: str = "0.00000003", fee_ccy: str = "BTC",
    avg_price: str = "60060", cost_usd: str = "2.0", fee_usd: str | None = None,
    filled_at=None,
) -> None:
    """Готовая выполненная покупка — как будто сервис её уже сделал."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol = $1;", symbol)
    pos = await pool.fetchrow("SELECT opened_at, entry_price FROM positions WHERE id = $1;",
                              position_id)
    if fee_usd is None:
        fee_usd = str(Decimal(fee) * Decimal(avg_price)) if fee_ccy != "USDT" else fee
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
        "symbol, host, requested_cost_usd, filled_qty, avg_price, cost_usd, fee, "
        "fee_ccy, virtual_price, virtual_ts, filled_at, fee_usd) "
        "VALUES ($1, $2, 'buy', 'filled', $3, $4, 'www.okx.com', 2, $5, $6, $7, $8, "
        "$9, $10, $11, $12, $13)",
        position_id, inst["id"], f"at95b{position_id}", symbol,
        Decimal(filled_qty), Decimal(avg_price), Decimal(cost_usd), Decimal(fee), fee_ccy,
        pos["entry_price"], pos["opened_at"], filled_at or pos["opened_at"],
        Decimal(fee_usd),
    )


async def insert_sell_filled(
    pool: asyncpg.Pool, position_id: int, *, symbol: str = "BTC/USDT",
    filled_qty: str = "0.00003329", cost_usd: str = "2.04", fee: str = "0.002",
    fee_ccy: str = "USDT", avg_price: str = "61000", filled_at=None,
    exit_reason: str | None = "target", equity_after: str | None = None,
    virtual_price: str = "60600",
) -> None:
    """Готовая выполненная продажа (продажа на бирже: комиссия — в USDT)."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol = $1;", symbol)
    pos = await pool.fetchrow("SELECT closed_at FROM positions WHERE id = $1;", position_id)
    when = filled_at or pos["closed_at"]
    fee_usd = Decimal(fee) if fee_ccy == "USDT" else Decimal(fee) * Decimal(avg_price)
    await pool.execute(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, cl_ord_id, "
        "symbol, host, requested_qty, filled_qty, avg_price, cost_usd, fee, fee_ccy, "
        "virtual_price, virtual_ts, filled_at, fee_usd, exit_reason, equity_after_usd) "
        "VALUES ($1, $2, 'sell', 'filled', $3, $4, 'www.okx.com', $5, $5, $6, $7, $8, $9, "
        "$10, $11, $11, $12, $13, $14)",
        position_id, inst["id"], f"at95s{position_id}", symbol, Decimal(filled_qty),
        Decimal(avg_price), Decimal(cost_usd), Decimal(fee), fee_ccy, Decimal(virtual_price),
        when, fee_usd, exit_reason, Decimal(equity_after) if equity_after else None,
    )


async def add_candle(pool: asyncpg.Pool, symbol: str, close: float, ts) -> None:
    """Минутная свеча в ohlcv — запасной источник цены открытой ноги."""
    inst = await pool.fetchrow("SELECT id FROM instruments WHERE symbol = $1;", symbol)
    await pool.execute(
        "INSERT INTO ohlcv (instrument_id, timeframe, ts, open, high, low, close, volume) "
        "VALUES ($1, '1m', $2, $3, $3, $3, $3, 1) ON CONFLICT DO NOTHING;",
        inst["id"], ts, close,
    )


async def set_state(pool: asyncpg.Pool, start_capital: float, mirror_since) -> None:
    await pool.execute(
        "INSERT INTO demo_state (id, mirror_since, host, start_capital) "
        "VALUES (1, $1, 'www.okx.com', $2) ON CONFLICT (id) DO UPDATE SET "
        "mirror_since = $1, start_capital = $2;",
        mirror_since, Decimal(str(start_capital)),
    )


# --------------------------------------------------------------------------
# Redis и часы
# --------------------------------------------------------------------------


class FakeRedis:
    """Минимальный Redis: ``set`` с ``nx``/``ex``, ``get``, ``delete``."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> int:
        existed = self.data.pop(key, None) is not None or key in self.lists
        self.lists.pop(key, None)
        return 1 if existed else 0

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttl[key] = seconds
        return key in self.data or key in self.zsets or key in self.lists

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return list(self.lists.get(key, []))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zremrangebyscore(self, key: str, low: str | float, high: float) -> int:
        zset = self.zsets.get(key, {})
        dead = [m for m, score in zset.items() if score <= high]
        for member in dead:
            del zset[member]
        return len(dead)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))


@dataclass
class Clock:
    """Управляемые часы: ``now()`` отдаёт текущее значение, ``advance`` двигает его."""

    value: datetime = field(default_factory=lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


async def no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------------------
# Биржа
# --------------------------------------------------------------------------


def _market(symbol: str, lot: str, min_sz: str) -> dict[str, Any]:
    base, quote = symbol.split("/")
    return {
        "id": f"{base}-{quote}", "symbol": symbol, "base": base, "quote": quote,
        "spot": True, "swap": False, "active": True,
        "info": {"lotSz": lot, "minSz": min_sz, "instType": "SPOT"},
        "precision": {"amount": float(lot)}, "limits": {"amount": {"min": float(min_sz)}},
    }


@dataclass
class FakeExchange:
    """Заглушка биржи с устройством ответов ccxt (unified order)."""

    prices: dict[str, float] = field(default_factory=lambda: {
        "BTC/USDT": 60000.0, "DOGE/USDT": 0.1, "FOO/USDT": 1.0})
    buy_slip: float = 0.001           # покупка дороже цены на 0.1 %
    sell_slip: float = 0.001          # продажа дешевле цены на 0.1 %
    fee_rate: float = 0.001
    usdt: Decimal = Decimal(1000)
    # режим следующего ордера: normal | auth | reject | timeout_reached | timeout_lost
    mode: str = "normal"
    open_polls: int = 0               # сколько опросов ордер остаётся open
    isSandboxModeEnabled: bool = True  # noqa: N815 — имя атрибута ccxt
    headers: dict[str, str] = field(default_factory=lambda: {
        "x-simulated-trading": "1", "User-Agent": "Mozilla/5.0"})
    markets: dict[str, Any] | None = None
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    fail_markets: bool = False
    fail_balance: Exception | None = None
    fail_tickers: bool = False
    missing_tickers: tuple[str, ...] = ()
    _polls: dict[str, int] = field(default_factory=dict)
    _seq: int = 0

    def known_markets(self) -> dict[str, Any]:
        return {
            "BTC/USDT": _market("BTC/USDT", "0.00000001", "0.00001"),
            "DOGE/USDT": _market("DOGE/USDT", "0.000001", "0.01"),
            "FOO/USDT": {**_market("FOO/USDT", "0.0001", "0.01"), "spot": False},
        }

    async def load_markets(self) -> dict[str, Any]:
        self.calls.append(("load_markets",))
        if self.fail_markets:
            raise ccxt.NetworkError("сеть недоступна")
        self.markets = self.known_markets()
        return self.markets

    async def fetch_balance(self) -> dict[str, Any]:
        self.calls.append(("fetch_balance",))
        if self.fail_balance is not None:
            raise self.fail_balance
        return {"USDT": {"free": float(self.usdt)}}

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        self.calls.append(("fetch_tickers", tuple(symbols or ())))
        if self.fail_tickers:
            raise ccxt.NetworkError("тикеры недоступны")
        return {s: {"last": self.prices[s]} for s in (symbols or self.prices)
                if s not in self.missing_tickers}

    async def fetch_time(self) -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": self.prices[symbol]}

    async def close(self) -> None:
        self.calls.append(("close",))

    def _raise_for_mode(self, symbol: str, cl: str) -> bool:
        """Бросает ошибку по режиму. Возвращает, дошёл ли ордер до биржи."""
        mode = self.mode
        if mode == "auth":
            raise ccxt.AuthenticationError(
                'okx {"code":"50101","msg":"APIKey does not match current environment.",'
                f'"data":[]}} key={SECRETS[0]}')
        if mode == "reject":
            raise ccxt.InsufficientFunds(
                'okx {"code":"1","data":[{"sCode":"51008","sMsg":"Order failed. Insufficient '
                f'USDT balance"}}],"msg":"All operations failed"}} secret={SECRETS[1]}')
        if mode == "timeout_reached":
            return True   # ордер дошёл и исполнен, но ответ потерян
        if mode == "timeout_lost":
            raise ccxt.RequestTimeout("okx POST ... timed out")
        return True

    def _store(self, symbol: str, side: str, qty: float, avg: float, cl: str) -> dict[str, Any]:
        self._seq += 1
        base = symbol.split("/")[0]
        fee_in_base = side == "buy"
        fee = qty * self.fee_rate if fee_in_base else qty * avg * self.fee_rate
        order = {
            "id": f"ord{self._seq}", "clientOrderId": cl, "symbol": symbol,
            "status": "closed", "filled": qty, "average": avg, "cost": qty * avg,
            "fee": {"cost": fee, "currency": base if fee_in_base else "USDT"},
            "timestamp": int(datetime.now(UTC).timestamp() * 1000),
            "lastTradeTimestamp": int(datetime.now(UTC).timestamp() * 1000),
        }
        self.orders[cl] = order
        return order

    async def create_market_buy_order_with_cost(
        self, symbol: str, cost: float, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = params or {}
        self.calls.append(("buy", symbol, cost, dict(params)))
        cl = params["clOrdId"]
        reached = self._raise_for_mode(symbol, cl)
        avg = self.prices[symbol] * (1 + self.buy_slip)
        order = self._store(symbol, "buy", cost / avg, avg, cl)
        if self.mode == "timeout_reached" and reached:
            raise ccxt.RequestTimeout("okx POST ... timed out")
        return {"id": order["id"], "clientOrderId": cl, "info": {}}

    async def create_order(
        self, symbol: str, type: str, side: str, amount: float,  # noqa: A002
        price: float | None = None, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        self.calls.append(("sell" if side == "sell" else side, symbol, amount, dict(params)))
        cl = params["clOrdId"]
        reached = self._raise_for_mode(symbol, cl)
        avg = self.prices[symbol] * (1 - self.sell_slip)
        order = self._store(symbol, side, amount, avg, cl)
        if self.mode == "timeout_reached" and reached:
            raise ccxt.RequestTimeout("okx POST ... timed out")
        return {"id": order["id"], "clientOrderId": cl, "info": {}}

    async def fetch_order(
        self, id: str, symbol: str, params: dict[str, Any] | None = None  # noqa: A002
    ) -> dict[str, Any]:
        params = params or {}
        cl = params["clOrdId"]
        self.calls.append(("fetch_order", cl))
        if cl not in self.orders:
            raise ccxt.OrderNotFound('okx {"code":"51603","msg":"Order does not exist"}')
        polls = self._polls.get(cl, 0)
        self._polls[cl] = polls + 1
        order = dict(self.orders[cl])
        if polls < self.open_polls:
            return {**order, "status": "open", "filled": 0.0, "average": None, "cost": 0.0}
        return order

    # --- удобные выборки для проверок ---
    def sent(self, kind: str | None = None) -> list[tuple]:
        rows = [c for c in self.calls if c[0] in ("buy", "sell")]
        return [c for c in rows if kind is None or c[0] == kind]


def build_context(
    pool: asyncpg.Pool, exchange: Any, clock: Clock, *, redis: FakeRedis | None = None,
    notified: list[str] | None = None, mirror_since: datetime | None = None,
    symbols: list[str] | None = None, **config: Any,
):
    """Контекст сервиса с заглушками: часы управляемые, сна нет, уведомления в список."""
    from src.demo import runner

    sink = notified if notified is not None else []

    async def notify(text: str) -> bool:
        sink.append(text)
        return True

    start_capital = Decimal(str(config.pop("start_capital", 1000)))
    cfg = runner.DemoConfig(host=config.pop("host", "www.okx.com"), **config)
    return runner.Context(
        pool=pool, exchange=exchange, redis=redis or FakeRedis(), config=cfg,
        symbols=symbols or ["BTC/USDT", "DOGE/USDT"], secrets=SECRETS,
        notify=notify, now=clock.now, sleep=no_sleep,
        mirror_since=mirror_since or clock.now() - timedelta(minutes=10),
        start_capital=start_capital,
    )



class CapturedLogs:
    """Перехваченные записи structlog: и модульных логгеров демо, и глобальных."""

    def __init__(self) -> None:
        import structlog.testing

        self.cap = structlog.testing.LogCapture()
        self.global_entries: list[dict[str, Any]] = []

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [*self.cap.entries, *self.global_entries]

    def text(self) -> str:
        return repr(self.entries)


@contextmanager
def capture_demo_logs(monkeypatch) -> Iterator[CapturedLogs]:
    """Подменяет модульные логгеры сервиса на перехватывающие на время блока.

    Модульные логгеры создаются при импорте и держат процессоры того момента,
    поэтому глобальная перенастройка structlog до них не дотягивается — их
    подменяют явно. Логгеры, создаваемые внутри функций (``demo_main``),
    перехватываются через ``structlog.testing.capture_logs``.
    """
    import structlog
    import structlog.testing

    from src.demo import ledger, messages, report, runner

    logs = CapturedLogs()
    proxy = structlog.wrap_logger(
        None, processors=[logs.cap], wrapper_class=structlog.make_filtering_bound_logger(0)
    ).bind(component="demo")
    monkeypatch.setattr(runner, "_log", proxy)
    monkeypatch.setattr(report, "_log", proxy)
    monkeypatch.setattr(ledger, "_log", proxy)
    monkeypatch.setattr(messages, "_log", proxy)
    with structlog.testing.capture_logs() as entries:
        logs.global_entries = entries
        yield logs
