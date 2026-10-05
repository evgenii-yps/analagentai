"""Сервис демо-исполнения: цикл зеркалирования позиций на демо-счёт OKX (§6 ТЗ 9.5).

ЧТО ДЕЛАЕТ ИТЕРАЦИЯ, по порядку (порядок ФИКСИРОВАН):

  0. восстановление — строки ``pending``/``sent`` разбираются запросом ордера по
     ``clOrdId``; повторно такой ордер НЕ отправляется никогда;
  1. продажи — закрытые позиции, у которых есть выполненная покупка и нет продажи;
  2. покупки — открытые позиции без строки покупки.

ПРОДАЖИ ИДУТ РАНЬШЕ ПОКУПОК: освободившиеся деньги и размер счёта нужны покупкам, а
не наоборот, и продажа уже купленного не должна ждать новых покупок.

ЗАЩИТА ОТ ДВОЙНОЙ ОТПРАВКИ — ПОРЯДОК ДЕЙСТВИЙ. Сначала в ``demo_orders`` вставляется
строка ``pending`` (с уникальными ``cl_ord_id`` и ``(position_id, leg)``), и только
потом уходит ордер. Строка уже есть — значит, ордер отправлять нельзя, что бы ни
случилось с процессом между этими двумя действиями. Сетевой сбой без ответа
оставляет строку ``sent`` — не «не отправлено»: ордер мог дойти.

ГРАНИЦЫ. Сервис пишет ТОЛЬКО в ``demo_orders`` и ``demo_state``. Таблицы
``positions``, ``signals``, ``risk_targets``, ``position_rejections`` он читает и
не меняет ни одной строкой. Только спот, только покупка с последующей продажей
купленного; каждый приватный запрос идёт через ``src.demo.exchange`` — с
проверкой демо-заголовка.

СЕРВИС НЕ ПАДАЕТ ПРИ ОШИБКАХ ИТЕРАЦИИ (как ``positions``): исключение ловится,
пишется в журнал, цикл продолжается. Исключение — ключ не того режима (50101) и
ошибка авторизации: тогда отправка ордеров прекращается ДО ПЕРЕЗАПУСКА, потому что
каждая следующая попытка заведомо провалится.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt
import structlog

from src.demo import exchange as demo_exchange
from src.demo import rules
from src.demo.exchange import DemoGuardError

_log = structlog.get_logger().bind(component="demo")

HEARTBEAT_KEY = "demo:heartbeat"
HEARTBEAT_TTL = 300
ALERT_KEY_PREFIX = "demo:alert:"
ALERT_TTL = 3600

# Сколько секунд после отправки ордер, которого биржа «не нашла», ещё может быть в
# пути. Не найден раньше этого срока — оставляем до следующей итерации, а не
# объявляем потерянным: ложное ``lost`` означало бы купленную монету без записи.
RECOVERY_GRACE_SEC = 20

# Шаг опроса исполнения рыночного ордера, секунд.
_FILL_POLL_SEC = 1.0

_CODE_RES = (
    re.compile(r'"sCode"\s*:\s*"(\d+)"'),
    re.compile(r'"code"\s*:\s*"(\d+)"'),
)

# Причина пропуска покупки по инструменту, которого нет на споте биржи.
SKIP_NO_MARKET = "no_market"
# Позиция закрылась, пока покупка ещё не была сделана.
SKIP_CLOSED_BEFORE_BUY = "closed_before_buy"


@dataclass(frozen=True)
class DemoConfig:
    """Параметры сервиса. Строятся из настроек, но модуль настроек не читает сам."""

    host: str
    interval_sec: int = 15
    max_entry_delay_sec: int = 180
    fill_wait_sec: int = 10
    min_usdt_balance: Decimal = Decimal(20)
    report_hour_utc: int = 6

    @classmethod
    def from_settings(cls, cfg: Any) -> DemoConfig:
        return cls(
            host=str(cfg.OKX_DEMO_HOST),
            interval_sec=int(cfg.DEMO_INTERVAL),
            max_entry_delay_sec=int(cfg.DEMO_MAX_ENTRY_DELAY_SEC),
            fill_wait_sec=int(cfg.DEMO_FILL_WAIT_SEC),
            min_usdt_balance=Decimal(str(cfg.DEMO_MIN_USDT_BALANCE)),
            report_hour_utc=int(cfg.DEMO_REPORT_HOUR_UTC),
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Context:
    """Всё, что нужно итерации: база, биржа, Redis, часы и уведомления.

    Часы, сон и отправка уведомлений — поля, а не вызовы «напрямую»: тесты
    подменяют их заглушками, и никакая итерация не ждёт настоящих секунд.
    """

    pool: Any
    exchange: Any
    redis: Any
    config: DemoConfig
    symbols: list[str]
    secrets: tuple[str, ...] = ()
    notify: Callable[[str], Awaitable[bool]] | None = None
    now: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    mirror_since: datetime | None = None
    markets_loaded: bool = False
    valid_symbols: set[str] = field(default_factory=set)
    halted: str | None = None
    # Свободный USDT, прочитанный на этой итерации (None — ещё не читали).
    usdt_free: Decimal | None = None


@dataclass
class IterStats:
    """Итог итерации — для журнала и тестов."""

    recovered: int = 0
    lost: int = 0
    sells: int = 0
    buys: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    dust: int = 0
    rejected: int = 0

    def any(self) -> bool:
        return bool(
            self.recovered or self.lost or self.sells or self.buys
            or self.skipped or self.dust or self.rejected
        )


# --------------------------------------------------------------------------
# Журнал, секреты, уведомления
# --------------------------------------------------------------------------


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Убирает значения секретов из текста: ключи в журнал и в базу не попадают."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def error_code_of(exc: BaseException) -> str | None:
    """Код ошибки OKX из текста исключения ccxt (``sCode``, иначе ``code``)."""
    text = str(exc)
    for pattern in _CODE_RES:
        match = pattern.search(text)
        if match and match.group(1) != "0":
            return match.group(1)
    return None


def _safe_error_text(ctx: Context, exc: BaseException) -> str:
    return scrub(f"{type(exc).__name__}: {exc}", ctx.secrets)[:500]


async def alert(ctx: Context, reason: str, text: str) -> None:
    """Алерт в Telegram с антидребезгом: не чаще раза в час по одной причине.

    Ключ Redis ``demo:alert:<причина>`` с TTL 3600 ставится атомарно (``SET NX``).
    Недоступный Redis алерт не глушит: лучше лишнее сообщение, чем молчание.
    """
    allowed = True
    try:
        allowed = bool(
            await ctx.redis.set(f"{ALERT_KEY_PREFIX}{reason}", "1", nx=True, ex=ALERT_TTL)
        )
    except Exception as exc:  # noqa: BLE001 — антидребезг не важнее самого алерта
        _log.warning("demo_alert_redis_failed=1", error=str(exc))
    if not allowed:
        return
    try:
        if ctx.notify is not None:
            await ctx.notify(f"⚠️ <b>demo</b>: {html.escape(text)}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("demo_alert_send_failed=1", reason=reason, error=str(exc))


def halt(ctx: Context, reason: str) -> None:
    """Прекращает отправку ордеров до перезапуска сервиса."""
    if ctx.halted is None:
        ctx.halted = reason
        _log.error("demo_halted=1", reason=reason)


# --------------------------------------------------------------------------
# Состояние и рынки
# --------------------------------------------------------------------------


async def ensure_state(pool: Any, host: str, now: datetime) -> datetime:
    """Возвращает ``mirror_since``; при первом запуске записывает его (``now``).

    Позиции, открытые ДО этого момента, не зеркалятся НИКОГДА: иначе пришлось бы
    продавать то, что не покупали.
    """
    await pool.execute(
        "INSERT INTO demo_state (id, mirror_since, host) VALUES (1, $1, $2) "
        "ON CONFLICT (id) DO NOTHING;",
        now, host,
    )
    row = await pool.fetchrow("SELECT mirror_since, host FROM demo_state WHERE id = 1;")
    if row["host"] != host:
        _log.warning(
            "demo_host_changed=1", state_host=row["host"], config_host=host,
            note="mirror_since не менялся; ордера пишут текущий хост",
        )
    return row["mirror_since"]


async def ensure_markets(ctx: Context) -> bool:
    """Загружает рынки и проверяет инструменты: спот-пара, есть на бирже (§6.1.3).

    Инструмент, не прошедший проверку, получает ошибку в журнале и алерт и
    ПРОПУСКАЕТСЯ: по его позициям покупка записывается как ``skipped`` /
    ``no_market``. Возвращает, готовы ли рынки (нет — повтор на следующей итерации).
    """
    if ctx.markets_loaded:
        return True
    try:
        await ctx.exchange.load_markets()
    except Exception as exc:  # noqa: BLE001 — сеть может вернуться
        _log.warning("demo_markets_failed=1", error=_safe_error_text(ctx, exc))
        return False
    markets = ctx.exchange.markets or {}
    valid: set[str] = set()
    for symbol in ctx.symbols:
        market = markets.get(symbol)
        if ":" in symbol or market is None or not market.get("spot") \
                or market.get("active") is False:
            _log.error("demo_instrument_unavailable=1", symbol=symbol)
            await alert(
                ctx, f"instrument:{symbol}",
                f"инструмент {symbol} не найден на споте биржи — пропускается",
            )
            continue
        valid.add(symbol)
    ctx.valid_symbols = valid
    ctx.markets_loaded = True
    return True


# --------------------------------------------------------------------------
# Запись строк demo_orders
# --------------------------------------------------------------------------


async def _insert_row(
    ctx: Context, *, pos: dict[str, Any], leg: str, status: str,
    skip_reason: str | None = None, requested_cost: Decimal | None = None,
    requested_qty: Decimal | None = None, virtual_price: Any = None,
    virtual_ts: datetime,
) -> int | None:
    """Вставляет строку ноги. ``None`` — строка уже есть (другая итерация успела)."""
    value = await ctx.pool.fetchval(
        "INSERT INTO demo_orders (position_id, instrument_id, leg, status, "
        "skip_reason, cl_ord_id, symbol, host, requested_cost_usd, requested_qty, "
        "virtual_price, virtual_ts) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
        "ON CONFLICT DO NOTHING RETURNING id;",
        int(pos["id"]), int(pos["instrument_id"]), leg, status, skip_reason,
        rules.cl_ord_id(int(pos["id"]), leg), str(pos["symbol"]),
        ctx.config.host, requested_cost, requested_qty, virtual_price, virtual_ts,
    )
    return int(value) if value is not None else None


async def _mark_sent(
    ctx: Context, row_id: int, exchange_order_id: str | None = None
) -> None:
    await ctx.pool.execute(
        "UPDATE demo_orders SET status = 'sent', "
        "exchange_order_id = COALESCE($2, exchange_order_id), "
        "sent_at = COALESCE(sent_at, $3), updated_at = now() WHERE id = $1;",
        row_id, exchange_order_id, ctx.now(),
    )


async def _mark_rejected(
    ctx: Context, row_id: int, code: str | None, text: str
) -> None:
    await ctx.pool.execute(
        "UPDATE demo_orders SET status = 'rejected', error_code = $2, "
        "error_text = $3, updated_at = now() WHERE id = $1;",
        row_id, code, text,
    )


async def _delete_row(ctx: Context, row_id: int) -> None:
    """Снимает строку, ордер по которой заведомо НЕ был принят (отказ до торгов)."""
    await ctx.pool.execute("DELETE FROM demo_orders WHERE id = $1;", row_id)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _ts_of(order: dict[str, Any], fallback: datetime) -> datetime:
    millis = order.get("lastTradeTimestamp") or order.get("timestamp")
    if millis:
        return datetime.fromtimestamp(int(millis) / 1000, UTC)
    return fallback


def classify_order(order: dict[str, Any]) -> str:
    """``filled`` / ``rejected`` / ``open`` по ответу биржи на запрос ордера."""
    status = order.get("status")
    filled = _dec(order.get("filled")) or Decimal(0)
    if status == "open":
        return "open"
    if status in ("closed", "canceled", "expired", "rejected"):
        return "filled" if filled > 0 else "rejected"
    return "open"


async def _record_fill(
    ctx: Context, row: dict[str, Any], order: dict[str, Any]
) -> None:
    """Записывает исполнение: количество, цену, сумму, комиссию, проскальзывание, задержку."""
    filled = _dec(order.get("filled")) or Decimal(0)
    avg = _dec(order.get("average"))
    cost = _dec(order.get("cost"))
    if cost is None and avg is not None:
        cost = filled * avg
    fee = order.get("fee") or {}
    fee_cost = _dec(fee.get("cost"))
    fee_ccy = fee.get("currency")
    filled_at = _ts_of(order, ctx.now())
    virtual_price = _dec(row.get("virtual_price"))
    slip = None
    if avg is not None and virtual_price is not None and virtual_price > 0:
        slip = rules.slippage_pct(row["leg"], virtual_price, avg)
    lag = rules.lag_seconds(filled_at, row["virtual_ts"])
    await ctx.pool.execute(
        "UPDATE demo_orders SET status = 'filled', "
        "exchange_order_id = COALESCE($2, exchange_order_id), filled_qty = $3, "
        "avg_price = $4, cost_usd = $5, fee = $6, fee_ccy = $7, slippage_pct = $8, "
        "filled_at = $9, lag_sec = $10, updated_at = now() WHERE id = $1;",
        int(row["id"]), str(order["id"]) if order.get("id") else None,
        filled, avg, cost, fee_cost, fee_ccy, slip, filled_at, lag,
    )
    _log.info(
        "demo_filled=1", position_id=int(row["position_id"]), leg=row["leg"],
        symbol=row["symbol"], filled_qty=str(filled),
        avg_price=str(avg), slippage_pct=None if slip is None else f"{slip:.4f}",
        lag_sec=lag,
    )


async def _settle(ctx: Context, row: dict[str, Any], order: dict[str, Any]) -> str:
    """Применяет ответ биржи к строке. Возвращает итог ``classify_order``."""
    verdict = classify_order(order)
    if verdict == "filled":
        await _record_fill(ctx, row, order)
    elif verdict == "rejected":
        await _mark_rejected(
            ctx, int(row["id"]), None,
            f"ордер завершён биржей без исполнения: status={order.get('status')}",
        )
        await alert(
            ctx, "rejected",
            f"ордер {row['leg']} по позиции {row['position_id']} ({row['symbol']}) "
            f"завершён без исполнения: {order.get('status')}",
        )
    elif order.get("id"):
        await _mark_sent(ctx, int(row["id"]), str(order["id"]))
    return verdict


async def _await_fill(
    ctx: Context, symbol: str, cl_ord_id: str
) -> dict[str, Any] | None:
    """Ждёт исполнения рыночного ордера до ``fill_wait_sec``, опрашивая его по clOrdId.

    ``None`` — исполнение не подтверждено (сеть, ордер ещё не виден): строка
    остаётся ``sent`` и разбирается на следующей итерации восстановлением.
    """
    attempts = max(1, int(ctx.config.fill_wait_sec / _FILL_POLL_SEC))
    order: dict[str, Any] | None = None
    for attempt in range(attempts):
        try:
            order = await demo_exchange.fetch_order_by_cl(ctx.exchange, symbol, cl_ord_id)
        except ccxt.OrderNotFound:
            order = None
        except (DemoGuardError, ccxt.AuthenticationError):
            raise
        except Exception as exc:  # noqa: BLE001 — разберёт восстановление
            _log.warning("demo_poll_failed=1", cl_ord_id=cl_ord_id,
                         error=_safe_error_text(ctx, exc))
            return None
        if order is not None and classify_order(order) != "open":
            return order
        if attempt + 1 < attempts:
            await ctx.sleep(_FILL_POLL_SEC)
    return order


async def _submit(
    ctx: Context, row_id: int, row: dict[str, Any],
    send: Callable[[], Awaitable[dict[str, Any]]],
) -> str:
    """Отправляет ордер по уже вставленной строке ``pending`` и разбирает исход.

    Исходы:
      * ордер принят и исполнен       -> ``filled``
      * принят, исполнение не видно   -> ``sent`` (разберёт восстановление)
      * биржа отказала                -> ``rejected``, повтор не делается
      * ключ/режим/защита (50101 ...) -> строка снимается, отправка прекращается
      * сеть без ответа               -> ``sent``: ордер мог дойти
    """
    try:
        response = await send()
    except Exception as exc:  # noqa: BLE001
        return await _handle_send_error(ctx, row_id, row, exc)
    await _mark_sent(ctx, row_id, str(response.get("id")) if response.get("id") else None)
    try:
        order = await _await_fill(ctx, str(row["symbol"]), str(row["cl_ord_id"]))
    except Exception as exc:  # noqa: BLE001
        if demo_exchange.is_auth_error(exc) or isinstance(exc, DemoGuardError):
            return await _handle_send_error(ctx, None, row, exc)
        raise
    if order is None:
        return "sent"
    return await _settle(ctx, {**row, "id": row_id}, order)


async def _handle_send_error(
    ctx: Context, row_id: int | None, row: dict[str, Any], exc: BaseException
) -> str:
    text = _safe_error_text(ctx, exc)
    if isinstance(exc, DemoGuardError) or demo_exchange.is_auth_error(exc):
        # Ордер не принят: отказ случился до торгов. Строка снимается, чтобы после
        # исправления ключа и перезапуска нога была сделана, а не числилась
        # «отклонённой» навсегда; сервис до перезапуска ордеров не шлёт.
        if row_id is not None:
            await _delete_row(ctx, row_id)
        halt(ctx, "key_mode" if not isinstance(exc, DemoGuardError) else "guard")
        await alert(
            ctx, "key_mode",
            "ключ не подходит к демо-режиму (или сработала демо-защита) — отправка "
            f"ордеров остановлена до перезапуска: {text}",
        )
        return "halted"
    if demo_exchange.is_definite_refusal(exc):
        assert row_id is not None
        await _mark_rejected(ctx, row_id, error_code_of(exc), text)
        _log.warning("demo_rejected=1", position_id=int(row["position_id"]),
                     leg=row["leg"], symbol=row["symbol"], error=text)
        await alert(
            ctx, "rejected",
            f"биржа отклонила ордер {row['leg']} по позиции {row['position_id']} "
            f"({row['symbol']}): {text}",
        )
        return "rejected"
    # Ответа не было: ордер мог дойти — строка остаётся sent.
    assert row_id is not None
    await _mark_sent(ctx, row_id)
    _log.warning("demo_send_unconfirmed=1", position_id=int(row["position_id"]),
                 leg=row["leg"], symbol=row["symbol"], error=text)
    return "sent"


# --------------------------------------------------------------------------
# Восстановление (§6.1.4, §6.3)
# --------------------------------------------------------------------------


async def recover(ctx: Context, stats: IterStats) -> None:
    """Разбирает строки ``pending``/``sent``: запрос ордера по clOrdId.

    Нашёлся — записывается исполнение. Не нашёлся и прошёл запас
    ``RECOVERY_GRACE_SEC`` — ``lost`` и алерт. ПОВТОРНО ТАКОЙ ОРДЕР НЕ ОТПРАВЛЯЕТСЯ.
    """
    rows = await ctx.pool.fetch(
        "SELECT id, position_id, leg, status, symbol, cl_ord_id, virtual_price, "
        "virtual_ts, created_at, sent_at FROM demo_orders "
        "WHERE status IN ('pending', 'sent') ORDER BY id;"
    )
    for record in rows:
        row = dict(record)
        if ctx.halted:
            return
        try:
            order = await demo_exchange.fetch_order_by_cl(
                ctx.exchange, str(row["symbol"]), str(row["cl_ord_id"])
            )
        except ccxt.OrderNotFound:
            age = (ctx.now() - (row["sent_at"] or row["created_at"])).total_seconds()
            if age < RECOVERY_GRACE_SEC:
                continue
            await ctx.pool.execute(
                "UPDATE demo_orders SET status = 'lost', "
                "error_text = 'ордер не найден на бирже по clOrdId', "
                "updated_at = now() WHERE id = $1;",
                int(row["id"]),
            )
            stats.lost += 1
            _log.error("demo_lost=1", position_id=int(row["position_id"]),
                       leg=row["leg"], cl_ord_id=row["cl_ord_id"])
            await alert(
                ctx, "lost",
                f"ордер {row['leg']} по позиции {row['position_id']} "
                f"({row['symbol']}, {row['cl_ord_id']}) не найден на бирже — lost",
            )
            continue
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DemoGuardError) or demo_exchange.is_auth_error(exc):
                await _handle_send_error(ctx, None, row, exc)
                return
            _log.warning("demo_recover_failed=1", cl_ord_id=row["cl_ord_id"],
                         error=_safe_error_text(ctx, exc))
            continue
        verdict = await _settle(ctx, row, order)
        if verdict != "open":
            stats.recovered += 1


# --------------------------------------------------------------------------
# Продажи (§6.2)
# --------------------------------------------------------------------------


_SELL_CANDIDATES_SQL = (
    "SELECT p.id, p.instrument_id, p.exit_price, p.closed_at, i.symbol, i.base, "
    "b.filled_qty AS bought_qty, b.fee AS buy_fee, b.fee_ccy AS buy_fee_ccy "
    "FROM positions p "
    "JOIN instruments i ON i.id = p.instrument_id "
    "JOIN demo_orders b ON b.position_id = p.id AND b.leg = 'buy' "
    "AND b.status = 'filled' "
    "WHERE p.status = 'closed' AND p.is_virtual AND p.closed_at IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM demo_orders s "
    "WHERE s.position_id = p.id AND s.leg = 'sell') "
    "ORDER BY p.closed_at, p.id;"
)


async def run_sells(ctx: Context, stats: IterStats) -> None:
    rows = await ctx.pool.fetch(_SELL_CANDIDATES_SQL)
    markets = ctx.exchange.markets or {}
    for record in rows:
        if ctx.halted:
            return
        pos = dict(record)
        market = markets.get(str(pos["symbol"]))
        if market is None:
            _log.error("demo_sell_no_market=1", symbol=pos["symbol"],
                       position_id=int(pos["id"]))
            await alert(ctx, f"sell_no_market:{pos['symbol']}",
                        f"нет рынка {pos['symbol']} — продажа позиции {pos['id']} невозможна")
            continue
        lot_sz, min_sz = demo_exchange.market_limits(market)
        bought = Decimal(str(pos["bought_qty"]))
        qty = rules.sell_qty(
            bought, _dec(pos["buy_fee"]), pos["buy_fee_ccy"], str(pos["base"]), lot_sz
        )
        virtual_price = pos["exit_price"]
        virtual_ts = pos["closed_at"]
        if rules.is_dust(qty, min_sz):
            row_id = await _insert_row(
                ctx, pos=pos, leg=rules.LEG_SELL, status=rules.STATUS_DUST,
                skip_reason=rules.SKIP_DUST, requested_qty=qty,
                virtual_price=virtual_price, virtual_ts=virtual_ts,
            )
            if row_id is not None:
                stats.dust += 1
                _log.info("demo_dust=1", position_id=int(pos["id"]), qty=str(qty))
            continue
        row_id = await _insert_row(
            ctx, pos=pos, leg=rules.LEG_SELL, status=rules.STATUS_PENDING,
            requested_qty=qty, virtual_price=virtual_price, virtual_ts=virtual_ts,
        )
        if row_id is None:
            continue
        cl = rules.cl_ord_id(int(pos["id"]), rules.LEG_SELL)
        row = {
            "id": row_id, "position_id": pos["id"], "leg": rules.LEG_SELL,
            "symbol": pos["symbol"], "cl_ord_id": cl,
            "virtual_price": virtual_price, "virtual_ts": virtual_ts,
        }
        verdict = await _submit(
            ctx, row_id, row,
            lambda s=str(pos["symbol"]), q=qty, c=cl, b=bought: demo_exchange.place_market_sell(
                ctx.exchange, s, q, c, b
            ),
        )
        _count(stats, rules.LEG_SELL, verdict)


# --------------------------------------------------------------------------
# Покупки (§6.2)
# --------------------------------------------------------------------------


def _buy_candidates_sql() -> str:
    """Позиции без строки покупки: открытые — все; закрытые — только открытые
    после ``mirror_since`` (закрытая история до старта не зеркалится)."""
    return (
        "SELECT p.id, p.instrument_id, p.side, p.status, p.opened_at, p.entry_price, "
        "p.notional_usd, i.symbol, i.base "
        "FROM positions p JOIN instruments i ON i.id = p.instrument_id "
        "WHERE p.is_virtual AND NOT EXISTS (SELECT 1 FROM demo_orders b "
        "WHERE b.position_id = p.id AND b.leg = 'buy') "
        "AND (p.status = 'open' OR (p.status = 'closed' AND p.opened_at >= $1)) "
        "ORDER BY p.opened_at, p.id;"
    )


async def _skip_buy(
    ctx: Context, pos: dict[str, Any], reason: str, stats: IterStats
) -> None:
    row_id = await _insert_row(
        ctx, pos=pos, leg=rules.LEG_BUY, status=rules.STATUS_SKIPPED,
        skip_reason=reason, virtual_price=pos["entry_price"],
        virtual_ts=pos["opened_at"],
    )
    if row_id is not None:
        stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
        _log.info("demo_skipped=1", position_id=int(pos["id"]), reason=reason)


async def _free_usdt(ctx: Context) -> Decimal:
    if ctx.usdt_free is None:
        ctx.usdt_free = await demo_exchange.fetch_usdt_free(ctx.exchange)
    return ctx.usdt_free


async def run_buys(ctx: Context, stats: IterStats) -> None:
    assert ctx.mirror_since is not None
    rows = await ctx.pool.fetch(_buy_candidates_sql(), ctx.mirror_since)
    for record in rows:
        pos = dict(record)
        if str(pos["symbol"]) not in ctx.valid_symbols:
            await _skip_buy(ctx, pos, SKIP_NO_MARKET, stats)
            continue
        ok, reason = rules.should_buy(
            pos, ctx.mirror_since, ctx.now(), ctx.config.max_entry_delay_sec
        )
        if not ok:
            await _skip_buy(ctx, pos, str(reason), stats)
            continue
        if pos["status"] == "closed":
            await _skip_buy(ctx, pos, SKIP_CLOSED_BEFORE_BUY, stats)
            continue
        if ctx.halted:
            return
        cost = rules.order_cost_usd(pos)
        try:
            free = await _free_usdt(ctx)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DemoGuardError) or demo_exchange.is_auth_error(exc):
                await _handle_send_error(ctx, None, {"position_id": pos["id"],
                                         "leg": "buy", "symbol": pos["symbol"]}, exc)
                return
            _log.warning("demo_balance_failed=1", error=_safe_error_text(ctx, exc))
            return
        if free < ctx.config.min_usdt_balance:
            await _skip_buy(ctx, pos, rules.SKIP_NO_BALANCE, stats)
            await alert(
                ctx, "no_balance",
                f"на демо-счёте {free} USDT — меньше порога "
                f"{ctx.config.min_usdt_balance}; покупки не делаются",
            )
            continue
        row_id = await _insert_row(
            ctx, pos=pos, leg=rules.LEG_BUY, status=rules.STATUS_PENDING,
            requested_cost=cost, virtual_price=pos["entry_price"],
            virtual_ts=pos["opened_at"],
        )
        if row_id is None:
            continue
        cl = rules.cl_ord_id(int(pos["id"]), rules.LEG_BUY)
        row = {
            "id": row_id, "position_id": pos["id"], "leg": rules.LEG_BUY,
            "symbol": pos["symbol"], "cl_ord_id": cl,
            "virtual_price": pos["entry_price"], "virtual_ts": pos["opened_at"],
        }
        verdict = await _submit(
            ctx, row_id, row,
            lambda s=str(pos["symbol"]), c=cost, k=cl: demo_exchange.place_market_buy(
                ctx.exchange, s, c, k
            ),
        )
        if verdict in ("filled", "sent"):
            ctx.usdt_free = free - cost
        _count(stats, rules.LEG_BUY, verdict)


def _count(stats: IterStats, leg: str, verdict: str) -> None:
    if verdict in ("filled", "sent"):
        if leg == rules.LEG_BUY:
            stats.buys += 1
        else:
            stats.sells += 1
    elif verdict == "rejected":
        stats.rejected += 1


# --------------------------------------------------------------------------
# Итерация и цикл
# --------------------------------------------------------------------------


async def run_once(ctx: Context) -> IterStats:
    """Одна итерация: восстановление, продажи, затем покупки."""
    stats = IterStats()
    ctx.usdt_free = None
    if ctx.halted:
        return stats
    if not await ensure_markets(ctx):
        return stats
    await recover(ctx, stats)
    if ctx.halted:
        return stats
    await run_sells(ctx, stats)
    if ctx.halted:
        return stats
    await run_buys(ctx, stats)
    return stats


async def heartbeat(ctx: Context) -> None:
    await ctx.redis.set(HEARTBEAT_KEY, ctx.now().isoformat(), ex=HEARTBEAT_TTL)


def startup_fields(ctx: Context) -> dict[str, Any]:
    """Фактические значения для строки запуска (§6.5). Ключей и их фрагментов нет."""
    return {
        "enabled": True,
        "host": ctx.config.host,
        "mirror_since": ctx.mirror_since.isoformat() if ctx.mirror_since else None,
        "interval": ctx.config.interval_sec,
        "max_entry_delay_sec": ctx.config.max_entry_delay_sec,
        "min_usdt_balance": str(ctx.config.min_usdt_balance),
        "instruments": list(ctx.symbols),
    }


async def run(
    ctx: Context,
    report: Callable[[Context], Awaitable[None]] | None = None,
) -> None:
    """Вечный цикл. Не падает при ошибках итерации; ``report`` — суточная сводка."""
    _log.info("Сервис demo запущен (демо-счёт OKX, спот)", **startup_fields(ctx))
    while True:
        try:
            stats = await run_once(ctx)
            if stats.any():
                _log.info(
                    "demo_iteration=1", recovered=stats.recovered, lost=stats.lost,
                    sells=stats.sells, buys=stats.buys, skipped=stats.skipped,
                    dust=stats.dust, rejected=stats.rejected,
                )
            if report is not None:
                await report(ctx)
            await heartbeat(ctx)
        except asyncio.CancelledError:
            _log.info("Сервис demo остановлен")
            raise
        except Exception as exc:  # noqa: BLE001 — сервис не падает
            _log.warning("demo_iteration_failed=1", error=_safe_error_text(ctx, exc),
                         error_type=type(exc).__name__)
        await ctx.sleep(ctx.config.interval_sec)
