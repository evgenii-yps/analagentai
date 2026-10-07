"""Сообщения о демо-сделках в Telegram (Этап 9.5, редакция 2, §4 ТЗ).

Два сообщения на сделку, и больше никаких: «КУПЛЕНО на демо-счёте» (после исполненной
покупки) и «ПРОДАНО» (после исполненной продажи). Пропуски (``skipped``) отдельными
сообщениями НЕ шлются: о них говорят суточная сводка и алерты ``no_capital`` /
``no_balance``. Время — по ``NOTIFY_TIMEZONE``; причины выхода — тот же перевод, что в
сообщениях ``positions`` (``src.positions.messages.exit_ru``).

ПРЕДОХРАНИТЕЛЬ ПОТОКА — тот же принцип, что у ``positions`` (``NOTIFY_TRADES_MAX_PER_HOUR``,
0 = без потолка): сверх потолка сообщения НЕ ТЕРЯЮТСЯ, а придерживаются в Redis и уходят
одной почасовой сводкой, которая в счёт потолка не входит. Свои ключи ``demo:trades:*``.

СДЕЛКА ПЕРВИЧНА, СООБЩЕНИЕ ВТОРИЧНО: сбой отправки не откатывает и не откладывает ни ордер,
ни запись в базе — он пишется в журнал (``demo_notify_failed=1``) и только.

Тексты строятся чистыми функциями (``opened_text``, ``closed_text``): данные → строка.
"""

from __future__ import annotations

import html
import secrets
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

from src.core import fmt
from src.core.user_settings import UserSettings, is_quiet_hour
from src.demo import ledger, rules
from src.positions.messages import exit_ru

if TYPE_CHECKING:
    from src.demo.runner import Context

_log = structlog.get_logger().bind(component="demo")

SENT_KEY = "demo:trades:sent:hour"
HELD_KEY = "demo:trades:held"
ROLLUP_KEY = "demo:trades:rollup_at"
_HELD_TTL = 24 * 3600
_SENT_TTL = 7200
_WINDOW_SEC = 3600


# --------------------------------------------------------------------------
# Форматирование (единый русский формат — ``src.core.fmt``)
# --------------------------------------------------------------------------

# Кнопка под каждым сообщением о сделке. Её нажатие обрабатывает бот (``src.bot.nav``,
# адрес ``v1:accn``): экран счёта приходит НОВЫМ сообщением, а сообщение о сделке —
# запись — не редактируется.
ACCOUNT_CALLBACK = "v1:accn"
ACCOUNT_BUTTON: dict[str, Any] = {
    "inline_keyboard": [[{"text": "💰 Счёт", "callback_data": ACCOUNT_CALLBACK}]]
}

# Причины пропуска покупки человеческим языком. Неизвестная причина печатается как есть:
# новая причина в сообщении лучше молчания.
DEMO_SKIP_RU: dict[str, str] = {
    rules.SKIP_NO_CAPITAL: "не хватило денег",
    rules.SKIP_NO_BALANCE: "мало USDT на демо-счёте",
    rules.SKIP_STALE: "опоздание",
    rules.SKIP_NOT_BUY: "не покупка",
    rules.SKIP_BEFORE_START: "до запуска демо",
    rules.SKIP_DUST: "слишком маленькая сумма",
    "no_market": "нет рынка на бирже",
    "closed_before_buy": "сделка закрылась до покупки",
}


def _esc(value: Any) -> str:
    return html.escape(str(value))


def token_of(symbol: str) -> str:
    return symbol.split("/", 1)[0]


# --------------------------------------------------------------------------
# Тексты (чистые функции)
# --------------------------------------------------------------------------


def opened_text(
    *, position_id: int, symbol: str, avg_price: Decimal, signal_price: Decimal | None,
    slippage_pct: Decimal | None, cost_usd: Decimal, qty: Decimal, fee_usd: Decimal | None,
    probability: float | None, target_price: Decimal, target_pct: Decimal,
    deadline_at: datetime, cash: Decimal | None, equity: Decimal | None, tz: ZoneInfo,
    calibrated_probability: float | None = None,
) -> str:
    """«Куплено»: цена, сумма, комиссия, цель и срок, согласие агентов, баланс.

    ``probability`` — ИНДЕКС СОГЛАСИЯ агентов (с этапа 7.3 это не вероятность), и подпись
    говорит именно так. Вероятность успеха по истории — отдельной строкой и только если
    она посчитана (``signals.calibrated_probability`` не пусто).
    """
    token = token_of(symbol)
    lines = [
        f"🛒 <b>Куплено · {_esc(token)}</b> #{int(position_id)} <i>(демо)</i>",
        f"Цена {fmt.price(avg_price)} · сигнал {fmt.price(signal_price)} · "
        f"проскальзывание {fmt.pct(slippage_pct)}",
        f"Сумма {fmt.money(cost_usd)} · {fmt.qty(qty)} {_esc(token)} · "
        f"комиссия {fmt.money(fee_usd, 4)}",
        f"🎯 Цель {fmt.price(target_price)} ({fmt.pct(target_pct)}) · "
        f"⏳ до {fmt.local_dt(deadline_at, tz)}",
        f"Согласие агентов {fmt.share(probability)}",
    ]
    if calibrated_probability is not None:
        lines.append(f"Вероятность успеха по истории {fmt.share(calibrated_probability)}")
    if cash is None or equity is None:
        lines.append("💰 Баланс: нет данных")
    else:
        lines.append(
            f"💰 Баланс {fmt.money(equity)} · свободно {fmt.money(cash)} · "
            f"в рынке {fmt.money(equity - cash)}"
        )
    return "\n".join(lines)


def closed_text(
    *, position_id: int, symbol: str, exit_reason: str, hold_hours: int | None,
    entry_price: Decimal, exit_price: Decimal, held_sec: float, profit_usd: Decimal,
    profit_pct: Decimal | None, fees_usd: Decimal, equity: Decimal | None,
    start_capital: Decimal,
) -> str:
    """«Продано»: значок и слово — по результату (прибыль больше нуля — ✅ «в плюс»,
    ноль и меньше — ❌ «в минус»), причина, цены, время в сделке, результат, баланс."""
    token = token_of(symbol)
    won = profit_usd > 0
    head = "✅ <b>Продано в плюс" if won else "❌ <b>Продано в минус"
    if equity is None:
        balance = "💰 Баланс: нет данных"
    else:
        diff, diff_pct = ledger.change(equity, start_capital)
        balance = (
            f"💰 Баланс {fmt.money(equity)} · с начала {fmt.sign_emoji(diff)} "
            f"{fmt.money_signed(diff)} ({fmt.pct(diff_pct)})"
        )
    return (
        f"{head} · {_esc(token)}</b> #{int(position_id)} <i>(демо)</i>\n"
        f"Причина: {_esc(exit_ru(exit_reason, hold_hours))}\n"
        f"{fmt.price(entry_price)} → {fmt.price(exit_price)} · "
        f"в сделке {fmt.duration(held_sec)}\n"
        f"Результат {fmt.sign_emoji(profit_usd)} {fmt.money_signed(profit_usd, 4)} "
        f"({fmt.pct(profit_pct)}) · комиссии {fmt.money(fees_usd, 4)}\n"
        f"{balance}"
    )


# --------------------------------------------------------------------------
# Данные для сообщения и отправка
# --------------------------------------------------------------------------

_FILL_SQL = (
    "SELECT d.id, d.position_id, d.leg, d.symbol, d.avg_price, d.filled_qty, d.cost_usd, "
    "d.fee_usd, d.fee_ccy, d.slippage_pct, d.filled_at, d.cash_after_usd, "
    "d.equity_after_usd, i.base, p.entry_price, p.signal_price, p.target_price, "
    "p.target_pct, p.deadline_at, p.opened_at, p.exit_reason, sg.probability, "
    "sg.calibrated_probability "
    "FROM demo_orders d JOIN instruments i ON i.id = d.instrument_id "
    "JOIN positions p ON p.id = d.position_id "
    "LEFT JOIN signals sg ON sg.id = p.signal_id WHERE d.id = $1;"
)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def build_fill_text(ctx: Context, order_id: int) -> str | None:
    """Текст сообщения по исполненной ноге; ``None`` — строки нет или нога не исполнена."""
    row = await ctx.pool.fetchrow(_FILL_SQL, order_id)
    if row is None or row["avg_price"] is None:
        return None
    equity = _dec(row["equity_after_usd"])
    if row["leg"] == rules.LEG_BUY:
        return opened_text(
            position_id=row["position_id"], symbol=row["symbol"],
            avg_price=_dec(row["avg_price"]), signal_price=_dec(row["signal_price"]),
            slippage_pct=_dec(row["slippage_pct"]), cost_usd=_dec(row["cost_usd"]),
            qty=_dec(row["filled_qty"]), fee_usd=_dec(row["fee_usd"]),
            probability=row["probability"], target_price=_dec(row["target_price"]),
            target_pct=_dec(row["target_pct"]), deadline_at=row["deadline_at"],
            cash=_dec(row["cash_after_usd"]), equity=equity, tz=ctx.tz,
            calibrated_probability=row["calibrated_probability"],
        )
    buy = await ctx.pool.fetchrow(
        "SELECT avg_price, cost_usd, fee_usd, fee_ccy, filled_at FROM demo_orders "
        "WHERE position_id = $1 AND leg = 'buy' AND status = 'filled';",
        row["position_id"],
    )
    if buy is None:
        return None
    pair = {
        "buy_cost": buy["cost_usd"], "buy_fee_usd": buy["fee_usd"], "buy_fee_ccy": buy["fee_ccy"],
        "sell_cost": row["cost_usd"], "sell_fee_usd": row["fee_usd"],
        "sell_fee_ccy": row["fee_ccy"],
    }
    profit = ledger.pair_profit(pair)
    cost = ledger.pair_cost(pair)
    hold_hours = round((row["deadline_at"] - row["opened_at"]).total_seconds() / 3600)
    return closed_text(
        position_id=row["position_id"], symbol=row["symbol"],
        exit_reason=str(row["exit_reason"] or ""), hold_hours=hold_hours,
        entry_price=_dec(buy["avg_price"]), exit_price=_dec(row["avg_price"]),
        held_sec=(row["filled_at"] - buy["filled_at"]).total_seconds(), profit_usd=profit,
        profit_pct=(profit / cost * 100 if cost > 0 else None),
        fees_usd=(_dec(buy["fee_usd"]) or ledger.ZERO) + (_dec(row["fee_usd"]) or ledger.ZERO),
        equity=equity, start_capital=ctx.start_capital,
    )


async def notify_fill(ctx: Context, order_id: int) -> None:
    """Сообщение об исполненной ноге. Не бросает: сообщение вторично."""
    if not ctx.config.notify_enabled or ctx.notify is None:
        return
    try:
        text = await build_fill_text(ctx, order_id)
        if text is not None:
            await send_trade_message(ctx, text)
    except Exception as exc:  # noqa: BLE001 — сообщение не важнее сделки
        _log.warning("demo_notify_failed=1", order_id=order_id, error=str(exc))


# --------------------------------------------------------------------------
# Предохранитель потока
# --------------------------------------------------------------------------


async def _sent_last_hour(ctx: Context, now: datetime) -> int:
    try:
        await ctx.redis.zremrangebyscore(SENT_KEY, "-inf", now.timestamp() - _WINDOW_SEC)
        return int(await ctx.redis.zcard(SENT_KEY))
    except Exception as exc:  # noqa: BLE001 — недоступный Redis потолок не включает
        _log.warning("demo_notify_rate_read_failed=1", error=str(exc))
        return 0


async def _record_sent(ctx: Context, now: datetime) -> None:
    try:
        # Суффикс делает член уникальным: два сообщения в один и тот же момент (пачка
        # исполнений за одну итерацию) иначе схлопнулись бы в одно и потолок не считал бы.
        member = f"{now.isoformat()}#{secrets.token_hex(4)}"
        await ctx.redis.zadd(SENT_KEY, {member: now.timestamp()})
        await ctx.redis.expire(SENT_KEY, _SENT_TTL)
    except Exception as exc:  # noqa: BLE001
        _log.warning("demo_notify_rate_write_failed=1", error=str(exc))


# --------------------------------------------------------------------------
# Тихие часы
# --------------------------------------------------------------------------

_QUIET_CACHE_SEC = 60


async def is_quiet_for(ctx: Context, chat_id: str) -> bool:
    """Сейчас тихие часы этого получателя? Настройки читаются из ``user_settings`` (кэш 60 с
    на каждого получателя).

    Хранятся они в UTC и включительно; окно через полночь (23–07) разбирает единая
    :func:`src.core.user_settings.is_quiet_hour`. Нет чата, нет записи или тишина
    выключена — ``False``. ОШИБКА ЧТЕНИЯ — тоже ``False`` (сообщение пойдёт со звуком) и
    предупреждение в журнал: лишний звук лучше пропущенной сделки.
    """
    if not chat_id:
        return False
    now = ctx.now()
    cached = ctx.quiet_cache.get(chat_id)
    if cached is not None and now.timestamp() - cached[0] < _QUIET_CACHE_SEC:
        return is_quiet_hour(cached[1], now)
    try:
        row = await ctx.pool.fetchrow(
            "SELECT quiet_from, quiet_to FROM user_settings WHERE chat_id = $1;", int(chat_id)
        )
        user = UserSettings(
            chat_id=int(chat_id),
            quiet_from=None if row is None or row["quiet_from"] is None
            else int(row["quiet_from"]),
            quiet_to=None if row is None or row["quiet_to"] is None else int(row["quiet_to"]),
        )
    except Exception as exc:  # noqa: BLE001 — настройки не важнее сообщения о сделке
        _log.warning("demo_quiet_hours_read_failed=1", chat_id=chat_id, error=str(exc))
        return False
    ctx.quiet_cache[chat_id] = (now.timestamp(), user)
    return is_quiet_hour(user, now)


async def deliver(
    ctx: Context, text: str, *, reply_markup: dict[str, Any] | None = None,
    quiet_aware: bool = True,
) -> bool:
    """Отправка через ``ctx.notify``.

    ``quiet_aware`` (сообщения о сделках и сводки): отдельная отправка КАЖДОМУ получателю,
    у каждого свои тихие часы — в тишине сообщение приходит без звука, но приходит.
    Алерты (``quiet_aware=False``) — одна рассылка без адреса (``send_message`` сама шлёт
    всем получателям), всегда со звуком. Получателей в настройках нет — тоже рассылка.

    Результат истинный, если дошло ХОТЯ БЫ ОДНОМУ. Сбой одного не мешает остальным;
    исключение пробрасывается, только если не дошло никому и хоть один вызов упал.
    """
    assert ctx.notify is not None
    recipients = ctx.config.recipients
    if not quiet_aware or not recipients:
        kwargs: dict[str, Any] = {}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return bool(await ctx.notify(text, **kwargs))
    delivered = False
    failure: Exception | None = None
    for chat in recipients:
        kwargs = {"chat_id": chat}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if await is_quiet_for(ctx, chat):
            kwargs["disable_notification"] = True
        try:
            delivered = bool(await ctx.notify(text, **kwargs)) or delivered
        except Exception as exc:  # noqa: BLE001 — сбой одного получателя не мешает остальным
            failure = exc
            _log.warning("demo_notify_failed=1", chat_id=chat, error=str(exc))
    if not delivered and failure is not None:
        raise failure
    return delivered


async def send_trade_message(ctx: Context, text: str) -> None:
    """Отправляет сообщение о сделке или придерживает его сверх потолка в час."""
    if not ctx.config.notify_enabled or ctx.notify is None:
        return
    now = ctx.now()
    cap = int(ctx.config.trades_max_per_hour)
    if cap > 0 and await _sent_last_hour(ctx, now) >= cap:
        try:
            await ctx.redis.rpush(HELD_KEY, text)
            await ctx.redis.expire(HELD_KEY, _HELD_TTL)
            _log.info("demo_notify_deferred=1", cap=cap)
        except Exception as exc:  # noqa: BLE001
            _log.warning("demo_notify_failed=1", stage="hold", error=str(exc))
        return
    try:
        ok = await deliver(ctx, text, reply_markup=ACCOUNT_BUTTON)
    except Exception as exc:  # noqa: BLE001
        _log.warning("demo_notify_failed=1", error=str(exc))
        return
    if not ok:
        _log.warning("demo_notify_failed=1", reason="Telegram вернул отказ")
        return
    if cap > 0:
        await _record_sent(ctx, now)


async def flush_rollup(ctx: Context) -> bool:
    """Шлёт почасовую сводку придержанных сообщений. В счёт потолка НЕ входит."""
    cap = int(ctx.config.trades_max_per_hour)
    if cap <= 0 or not ctx.config.notify_enabled or ctx.notify is None:
        return False
    now = ctx.now()
    try:
        last_raw = await ctx.redis.get(ROLLUP_KEY)
        if now.timestamp() - (float(last_raw) if last_raw else 0.0) < _WINDOW_SEC:
            return False
        held = await ctx.redis.lrange(HELD_KEY, 0, -1)
        if not held:
            return False
        await ctx.redis.delete(HELD_KEY)
        await ctx.redis.set(ROLLUP_KEY, str(now.timestamp()), ex=2 * _WINDOW_SEC)
    except Exception as exc:  # noqa: BLE001
        _log.warning("demo_notify_rollup_failed=1", error=str(exc))
        return False
    texts = [item if isinstance(item, str) else item.decode() for item in held]
    body = "\n\n".join(texts)
    text = (
        f"📦 <b>Придержано сообщений о демо-сделках: {len(texts)}</b>\n"
        f"Потолок {cap} сообщений в час (NOTIFY_TRADES_MAX_PER_HOUR). Сами сделки идут "
        "своим чередом — придержаны только сообщения о них.\n\n" + body
    )
    try:
        ok = bool(await deliver(ctx, text, reply_markup=ACCOUNT_BUTTON))
    except Exception as exc:  # noqa: BLE001
        ok = False
        _log.warning("demo_notify_failed=1", stage="rollup", error=str(exc))
    if ok:
        _log.info("demo_notify_rollup_sent=1", count=len(texts))
    return ok
