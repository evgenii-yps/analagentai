"""Суточная сводка демо-исполнения в Telegram (Этап 9.5, §8 ТЗ).

Раз в сутки, в ``DEMO_REPORT_HOUR_UTC`` и позже в тот же день (если сервис в этот
час не работал — первая итерация после него), сообщение на русском:

  * за 24 часа: покупок, продаж, пропусков по причинам, dust, lost, rejected;
  * медиана проскальзывания при входе и при выходе, %;
  * медиана задержки исполнения, секунд;
  * по парам, закрытым за сутки: демо-прибыль в $ и % против виртуальной прибыли
    тех же позиций (``positions.net_pnl_usd``) и разница;
  * то же нарастающим итогом с ``mirror_since``;
  * баланс USDT.

При ``DEMO_ENABLED=false`` сводка не шлётся: сервис в этом режиме до отправки не
доходит. Сбор данных и форматирование разделены — форматирование чистое и
проверяется без базы.
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from src.demo import exchange as demo_exchange
from src.demo import rules
from src.demo.runner import Context, _safe_error_text

_log = structlog.get_logger().bind(component="demo")

REPORT_KEY_PREFIX = "demo:report:"
_REPORT_KEY_TTL = 3 * 86400
# Пауза перед повтором, если отправка не удалась: ключ дня остаётся, но короткий.
_RETRY_TTL = 900


@dataclass
class PairTotals:
    """Итог по парам «покупка + продажа» за период."""

    pairs: int = 0
    demo_usd: Decimal = Decimal(0)
    demo_cost_usd: Decimal = Decimal(0)
    virtual_usd: Decimal = Decimal(0)
    virtual_notional_usd: Decimal = Decimal(0)

    @property
    def demo_pct(self) -> Decimal | None:
        if self.demo_cost_usd <= 0:
            return None
        return self.demo_usd / self.demo_cost_usd * 100

    @property
    def virtual_pct(self) -> Decimal | None:
        if self.virtual_notional_usd <= 0:
            return None
        return self.virtual_usd / self.virtual_notional_usd * 100


def _median(values: list[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


def pair_totals(rows: list[dict[str, Any]]) -> PairTotals:
    """Складывает пары: демо-прибыль (``rules.pair_pnl``) и виртуальная прибыль позиций.

    Пара с комиссией в неизвестной валюте не пересчитывается и в итог не входит
    (``fee_in_usdt`` бросает ``ValueError``): занижать издержки молча нельзя.
    """
    total = PairTotals()
    for row in rows:
        base = str(row["base"])
        try:
            buy_cost = Decimal(str(row["buy_cost"]))
            if str(row["buy_fee_ccy"] or "").upper() in ("USDT", "USD"):
                buy_cost += rules.fee_in_usdt(
                    row["buy_fee"], row["buy_fee_ccy"], row["buy_px"], base
                )
            sell_fee = rules.fee_in_usdt(
                row["sell_fee"], row["sell_fee_ccy"], row["sell_px"], base
            )
            profit, _pct = rules.pair_pnl(buy_cost, row["sell_cost"], sell_fee)
        except (ValueError, TypeError) as exc:
            _log.warning("demo_report_pair_skipped=1", position_id=row.get("id"),
                         error=str(exc))
            continue
        total.pairs += 1
        total.demo_usd += profit
        total.demo_cost_usd += buy_cost
        total.virtual_usd += Decimal(str(row["net_pnl_usd"]))
        total.virtual_notional_usd += Decimal(str(row["notional_usd"]))
    return total


_PAIRS_SQL = (
    "SELECT p.id, p.net_pnl_usd, p.notional_usd, i.base, "
    "b.cost_usd AS buy_cost, b.fee AS buy_fee, b.fee_ccy AS buy_fee_ccy, "
    "b.avg_price AS buy_px, s.cost_usd AS sell_cost, s.fee AS sell_fee, "
    "s.fee_ccy AS sell_fee_ccy, s.avg_price AS sell_px "
    "FROM positions p JOIN instruments i ON i.id = p.instrument_id "
    "JOIN demo_orders b ON b.position_id = p.id AND b.leg = 'buy' AND b.status = 'filled' "
    "JOIN demo_orders s ON s.position_id = p.id AND s.leg = 'sell' AND s.status = 'filled' "
    "WHERE p.status = 'closed' AND p.closed_at >= $1 "
    "AND p.exit_reason IS DISTINCT FROM 'data_gap' AND p.net_pnl_usd IS NOT NULL "
    "AND b.cost_usd > 0;"
)


async def collect(ctx: Context, now: datetime) -> dict[str, Any]:
    """Читает из базы всё, что нужно сводке (только SELECT)."""
    since = now - timedelta(hours=24)
    counts = await ctx.pool.fetch(
        "SELECT leg, status, skip_reason, count(*) AS n FROM demo_orders "
        "WHERE created_at >= $1 GROUP BY leg, status, skip_reason;",
        since,
    )
    filled = await ctx.pool.fetch(
        "SELECT leg, slippage_pct, lag_sec FROM demo_orders "
        "WHERE status = 'filled' AND filled_at >= $1;",
        since,
    )
    day_pairs = await ctx.pool.fetch(_PAIRS_SQL, since)
    all_pairs = await ctx.pool.fetch(_PAIRS_SQL, ctx.mirror_since)
    try:
        balance: Decimal | None = await demo_exchange.fetch_usdt_free(ctx.exchange)
    except Exception as exc:  # noqa: BLE001 — сводка важнее баланса
        _log.warning("demo_report_balance_failed=1", error=_safe_error_text(ctx, exc))
        balance = None
    return {
        "now": now,
        "mirror_since": ctx.mirror_since,
        "counts": [dict(r) for r in counts],
        "filled": [dict(r) for r in filled],
        "day": pair_totals([dict(r) for r in day_pairs]),
        "total": pair_totals([dict(r) for r in all_pairs]),
        "balance": balance,
    }


def _signed(value: Decimal, places: int, unit: str = "") -> str:
    return f"{value:+.{places}f}{unit}"


def _pct_or_dash(value: Decimal | None) -> str:
    return "—" if value is None else _signed(value, 2, "%")


def _pairs_block(title: str, totals: PairTotals) -> list[str]:
    if totals.pairs == 0:
        return [f"<b>{title}</b>: пар нет"]
    diff_usd = totals.demo_usd - totals.virtual_usd
    demo_pct, virtual_pct = totals.demo_pct, totals.virtual_pct
    diff_pct = None if demo_pct is None or virtual_pct is None else demo_pct - virtual_pct
    return [
        f"<b>{title}</b>: пар {totals.pairs}",
        f"  демо: {_signed(totals.demo_usd, 4, ' $')} ({_pct_or_dash(demo_pct)})",
        f"  виртуально: {_signed(totals.virtual_usd, 4, ' $')} "
        f"({_pct_or_dash(virtual_pct)})",
        f"  разница: {_signed(diff_usd, 4, ' $')} "
        f"({'—' if diff_pct is None else _signed(diff_pct, 2, ' п.п.')})",
    ]


def format_report(data: dict[str, Any]) -> str:
    """Собирает текст сводки. Чистая функция: без базы, сети и часов."""
    counts: list[dict[str, Any]] = data["counts"]

    def n(leg: str, status: str) -> int:
        return sum(int(c["n"]) for c in counts if c["leg"] == leg and c["status"] == status)

    skipped: dict[str, int] = {}
    for c in counts:
        if c["status"] == "skipped":
            reason = str(c["skip_reason"])
            skipped[reason] = skipped.get(reason, 0) + int(c["n"])
    skipped_text = (
        ", ".join(f"{html.escape(r)} {k}" for r, k in sorted(skipped.items()))
        if skipped else "нет"
    )
    dust = sum(int(c["n"]) for c in counts if c["status"] == "dust")
    lost = sum(int(c["n"]) for c in counts if c["status"] == "lost")
    rejected = sum(int(c["n"]) for c in counts if c["status"] == "rejected")

    def med(leg: str, field: str) -> Decimal | None:
        return _median([
            Decimal(str(f[field])) for f in data["filled"]
            if f["leg"] == leg and f[field] is not None
        ])

    slip_in, slip_out = med("buy", "slippage_pct"), med("sell", "slippage_pct")
    lag = _median([Decimal(str(f["lag_sec"])) for f in data["filled"]
                   if f["lag_sec"] is not None])

    def fmt(value: Decimal | None, places: int, unit: str) -> str:
        return "нет данных" if value is None else _signed(value, places, unit)

    since = data["mirror_since"]
    balance = data["balance"]
    lines = [
        "<b>Демо-исполнение OKX — сводка за 24 часа</b>",
        f"Покупок исполнено: {n('buy', 'filled')} · продаж исполнено: {n('sell', 'filled')}",
        f"Пропуски покупок: {skipped_text}",
        f"dust: {dust} · lost: {lost} · rejected: {rejected}",
        f"Проскальзывание, медиана: вход {fmt(slip_in, 4, '%')} · "
        f"выход {fmt(slip_out, 4, '%')}",
        "Задержка исполнения, медиана: "
        + ("нет данных" if lag is None else f"{lag:.0f} с"),
        "",
        *_pairs_block("Пары, закрытые за сутки", data["day"]),
        "",
        *_pairs_block(
            f"Нарастающим итогом с {since:%Y-%m-%d %H:%M} UTC", data["total"]
        ),
        "",
        "Баланс USDT на демо-счёте: "
        + ("недоступен" if balance is None else f"{balance:.2f}"),
    ]
    return "\n".join(lines)


async def daily_report(ctx: Context) -> bool:
    """Шлёт сводку, если час настал и за сегодня она ещё не отправлена.

    Ключ ``demo:report:<дата>`` ставится атомарно до отправки. Не удалась отправка —
    ключ перезаписывается на короткий срок, и повтор будет через 15 минут, а не на
    каждой итерации.
    """
    now = ctx.now()
    if now.hour < ctx.config.report_hour_utc or ctx.notify is None:
        return False
    key = f"{REPORT_KEY_PREFIX}{now.date().isoformat()}"
    if not await ctx.redis.set(key, "sending", nx=True, ex=_REPORT_KEY_TTL):
        return False
    ok = False
    try:
        text = format_report(await collect(ctx, now))
        ok = bool(await ctx.notify(text))
    except Exception as exc:  # noqa: BLE001 — сводка не должна ронять цикл
        _log.warning("demo_report_failed=1", error=_safe_error_text(ctx, exc))
    if ok:
        await ctx.redis.set(key, "sent", ex=_REPORT_KEY_TTL)
        _log.info("demo_report_sent=1", date=now.date().isoformat())
    else:
        await ctx.redis.set(key, "retry", ex=_RETRY_TTL)
    return ok
