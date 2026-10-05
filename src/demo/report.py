"""Суточная сводка демо-счёта в Telegram (Этап 9.5, редакция 2, §5 ТЗ).

Сводка идёт СРАЗУ ПОСЛЕ СНИМКА КОНЦА СУТОК: в 00:00 по ``NOTIFY_TIMEZONE`` сервис пишет
строку ``demo_balance_daily`` за ушедшие сутки (``ledger.ensure_snapshots``), и первая же
итерация после этого шлёт сводку за них. Отдельного часа у сводки нет
(``DEMO_REPORT_HOUR_UTC`` удалён).

Состав сообщения: баланс на начало и конец суток; изменение за сутки и с начала в $ и %;
число сделок открыто / закрыто / прибыльных / убыточных; пропуски по причинам; медианы
проскальзывания на входе и выходе; комиссии за сутки; ``lost`` и ``rejected`` (и ``dust``),
если они были. Сверх перечня ТЗ — одна строка «против виртуальной прибыли тех же позиций»
(осталась от редакции 1: она показывает, сколько съедает реальное исполнение).

Защита «одна сводка в сутки» — ключ Redis ``demo:report:<дата отчётных суток>``. Сбор
данных и форматирование разделены: форматирование — чистая функция.
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import structlog

from src.demo import ledger
from src.demo.runner import Context, _safe_error_text

_log = structlog.get_logger().bind(component="demo")

REPORT_KEY_PREFIX = "demo:report:"
_REPORT_KEY_TTL = 3 * 86400
# Пауза перед повтором, если отправка не удалась: ключ дня остаётся, но короткий.
_RETRY_TTL = 900


@dataclass
class PairTotals:
    """Итог по парам «покупка + продажа» за период: демо против виртуальной прибыли."""

    pairs: int = 0
    demo_usd: Decimal = Decimal(0)
    demo_cost_usd: Decimal = Decimal(0)
    virtual_usd: Decimal = Decimal(0)
    virtual_notional_usd: Decimal = Decimal(0)

    @property
    def demo_pct(self) -> Decimal | None:
        return self.demo_usd / self.demo_cost_usd * 100 if self.demo_cost_usd > 0 else None

    @property
    def virtual_pct(self) -> Decimal | None:
        if self.virtual_notional_usd <= 0:
            return None
        return self.virtual_usd / self.virtual_notional_usd * 100


def pair_totals(pairs: list[dict[str, Any]]) -> PairTotals:
    """Складывает пары: демо-прибыль (деньгами) и виртуальная прибыль тех же позиций.

    Закрытия ``data_gap`` в сравнение не входят: цена их выхода не наблюдалась, а
    восстановлена (как и в сводке позиций).
    """
    total = PairTotals()
    for pair in pairs:
        if pair.get("virtual_exit_reason") == "data_gap" or pair.get("virtual_pnl_usd") is None:
            continue
        total.pairs += 1
        total.demo_usd += ledger.pair_profit(pair)
        total.demo_cost_usd += ledger.pair_cost(pair)
        total.virtual_usd += Decimal(str(pair["virtual_pnl_usd"]))
        total.virtual_notional_usd += Decimal(str(pair["notional_usd"]))
    return total


def _median(values: list[Decimal]) -> Decimal | None:
    return statistics.median(values) if values else None


async def collect(ctx: Context, day: date) -> dict[str, Any] | None:
    """Читает строку снимка и данные за сутки ``day`` (только SELECT). ``None`` — снимка нет."""
    snap = await ctx.pool.fetchrow("SELECT * FROM demo_balance_daily WHERE day = $1;", day)
    if snap is None:
        return None
    start, end = ledger.day_bounds(day, ctx.tz)
    skipped = await ctx.pool.fetch(
        "SELECT skip_reason, count(*) AS n FROM demo_orders WHERE leg = 'buy' "
        "AND status = 'skipped' AND created_at >= $1 AND created_at < $2 GROUP BY skip_reason;",
        start, end,
    )
    problems = await ctx.pool.fetch(
        "SELECT status, count(*) AS n FROM demo_orders "
        "WHERE status IN ('lost', 'rejected', 'dust') "
        "AND created_at >= $1 AND created_at < $2 GROUP BY status;",
        start, end,
    )
    filled = await ctx.pool.fetch(
        "SELECT leg, slippage_pct FROM demo_orders WHERE status = 'filled' "
        "AND filled_at >= $1 AND filled_at < $2 AND slippage_pct IS NOT NULL;",
        start, end,
    )
    pairs = await ledger.fetch_pairs(ctx.pool, start, end)
    return {
        "day": day,
        "snap": dict(snap),
        "start_capital": ctx.start_capital,
        "skipped": {str(r["skip_reason"]): int(r["n"]) for r in skipped},
        "problems": {str(r["status"]): int(r["n"]) for r in problems},
        "slip_in": _median([Decimal(str(r["slippage_pct"])) for r in filled if r["leg"] == "buy"]),
        "slip_out": _median(
            [Decimal(str(r["slippage_pct"])) for r in filled if r["leg"] == "sell"]),
        "pairs": pair_totals(pairs),
    }


def _usd(value: Decimal, places: int = 2, sign: bool = False) -> str:
    number = float(value)
    return f"{number:+,.{places}f}".replace(",", " ") if sign else \
        f"{number:,.{places}f}".replace(",", " ")


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{float(value):+.2f}%"


def format_report(data: dict[str, Any]) -> str:
    """Собирает текст сводки. Чистая функция: без базы, сети и часов."""
    snap = data["snap"]
    open_, close = Decimal(str(snap["equity_open"])), Decimal(str(snap["equity_close"]))
    day_diff, day_pct = ledger.change(close, open_)
    all_diff, all_pct = ledger.change(close, Decimal(str(data["start_capital"])))
    skipped = data["skipped"]
    skipped_text = (
        ", ".join(f"{html.escape(r)} {n}" for r, n in sorted(skipped.items()))
        if skipped else "нет"
    )
    fmt = lambda v: "нет данных" if v is None else f"{float(v):+.4f}%"  # noqa: E731
    lines = [
        f"<b>Демо-счёт OKX — сводка за {data['day']:%d.%m.%Y}</b>"
        + (" (снимок дописан с опозданием)" if snap["late"] else ""),
        f"Баланс: на начало ${_usd(open_)} → на конец ${_usd(close)} "
        f"(свободно ${_usd(Decimal(str(snap['cash_close'])))} · "
        f"в рынке ${_usd(Decimal(str(snap['in_market_close'])))})",
        f"Изменение за сутки: {_usd(day_diff, 2, True)} $ ({_pct(day_pct)}) · "
        f"с начала: {_usd(all_diff, 2, True)} $ ({_pct(all_pct)})",
        f"Сделок: открыто {snap['opened_count']} · закрыто {snap['closed_count']} · "
        f"прибыльных {snap['wins']} · убыточных {snap['losses']}",
        f"Пропуски покупок: {skipped_text}",
        f"Проскальзывание, медиана: вход {fmt(data['slip_in'])} · выход {fmt(data['slip_out'])}",
        f"Комиссии за сутки: ${_usd(Decimal(str(snap['fees_usd'])), 4)}",
    ]
    totals: PairTotals = data["pairs"]
    if totals.pairs:
        diff = totals.demo_usd - totals.virtual_usd
        lines.append(
            f"Против виртуальной прибыли тех же позиций ({totals.pairs}): демо "
            f"{_usd(totals.demo_usd, 4, True)} $ · виртуально "
            f"{_usd(totals.virtual_usd, 4, True)} $ · разница {_usd(diff, 4, True)} $"
        )
    problems = data["problems"]
    if problems:
        lines.append("Внимание: " + " · ".join(
            f"{name} {problems[name]}" for name in ("lost", "rejected", "dust")
            if name in problems))
    return "\n".join(lines)


async def daily_report(ctx: Context) -> bool:
    """Шлёт сводку за вчерашние сутки, если снимок за них есть и сводка ещё не уходила.

    Ключ ``demo:report:<дата>`` ставится атомарно до отправки. Не удалась отправка —
    ключ перезаписывается на короткий срок, и повтор будет через 15 минут, а не на
    каждой итерации.
    """
    if ctx.notify is None:
        return False
    day = ledger.local_day(ctx.now(), ctx.tz) - timedelta(days=1)
    key = f"{REPORT_KEY_PREFIX}{day.isoformat()}"
    if await ctx.redis.get(key) is not None:      # уже ушла (или ждёт повтора)
        return False
    data = await collect(ctx, day)
    if data is None:                               # снимка за сутки ещё нет
        return False
    if not await ctx.redis.set(key, "sending", nx=True, ex=_REPORT_KEY_TTL):
        return False
    ok = False
    try:
        ok = bool(await ctx.notify(format_report(data)))
    except Exception as exc:  # noqa: BLE001 — сводка не должна ронять цикл
        _log.warning("demo_report_failed=1", error=_safe_error_text(ctx, exc))
    if ok:
        await ctx.redis.set(key, "sent", ex=_REPORT_KEY_TTL)
        _log.info("demo_report_sent=1", date=day.isoformat())
    else:
        await ctx.redis.set(key, "retry", ex=_RETRY_TTL)
    return ok
