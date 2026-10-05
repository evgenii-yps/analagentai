"""Чистые правила демо-исполнения: без сети, без базы, без чтения настроек (§5 ТЗ 9.5).

Всё, что здесь решается, решается ЧИСЛАМИ на входе. Деньги и количества — только
``Decimal``: рыночный ордер на два доллара по BTC — это количество порядка
``0.00002``, и ошибка float в одиннадцатом знаке оказалась бы продажей
несуществующего остатка.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

# Ноги сделки и статусы строки demo_orders — те же значения, что в ограничениях
# миграции 030; повторены константами, а не строками по коду.
LEG_BUY = "buy"
LEG_SELL = "sell"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"
STATUS_DUST = "dust"
STATUS_LOST = "lost"

# Причины пропуска покупки (skip_reason). before_start / stale / not_buy выдаёт
# ``should_buy``; no_balance — сервис, когда на демо-счёте мало USDT.
SKIP_BEFORE_START = "before_start"
SKIP_STALE = "stale"
SKIP_NOT_BUY = "not_buy"
SKIP_NO_BALANCE = "no_balance"
# Пропуск ПРОДАЖИ: купленного меньше минимального ордера биржи.
SKIP_DUST = "below_min_size"

# Префикс clOrdId: «at» — Agent Trade, «95» — этап. Только буквы и цифры (требование
# OKX к clOrdId), не длиннее 32 символов.
_CL_PREFIX = "at95"
_CL_MAX_LEN = 32


def order_cost_usd(position: dict[str, Any]) -> Decimal:
    """Сумма рыночной покупки в долларах — ЕДИНСТВЕННОЕ место, где она решается.

    Этап 9.5: ровно ``positions.notional_usd`` (сейчас $2). Этап 9.6
    (манименеджмент) заменит только эту функцию. Размер ВИРТУАЛЬНЫХ позиций она
    не меняет и менять не может: читает позицию, а не пишет.
    """
    return Decimal(str(position["notional_usd"]))


def cl_ord_id(position_id: int, leg: str) -> str:
    """Клиентский идентификатор ордера: ``at95b123`` / ``at95s123``.

    Детерминирован по (позиция, нога): повторная отправка того же ордера получила
    бы тот же идентификатор, и биржа отвергла бы дубликат, а восстановление после
    сбоя находит ордер по нему же.
    """
    if leg not in (LEG_BUY, LEG_SELL):
        raise ValueError(f"нога сделки должна быть buy или sell, получено {leg!r}")
    pid = int(position_id)
    if pid < 0:
        raise ValueError(f"position_id не может быть отрицательным: {pid}")
    value = f"{_CL_PREFIX}{'b' if leg == LEG_BUY else 's'}{pid}"
    if len(value) > _CL_MAX_LEN:
        raise ValueError(f"clOrdId длиннее {_CL_MAX_LEN} символов: {value}")
    return value


def should_buy(
    pos: dict[str, Any],
    mirror_since: datetime,
    now: datetime,
    max_delay_sec: int,
) -> tuple[bool, str | None]:
    """Можно ли зеркалить открытие позиции покупкой. ``(False, причина)`` — нет.

    Проверки идут в порядке ТЗ:
      * ``before_start`` — позиция открыта ДО начала зеркалирования
        (``opened_at < mirror_since``): продавать то, что не покупали, нельзя;
      * ``stale`` — с открытия прошло БОЛЬШЕ ``max_delay_sec`` секунд: сделка
        по цене, которой давно нет, ничего не измерит;
      * ``not_buy`` — позиция не покупка: продажа без покупки запрещена кодом.

    Границы строгие с обеих сторон: ``opened_at == mirror_since`` и возраст ровно
    ``max_delay_sec`` ЕЩЁ годятся.
    """
    opened_at = pos["opened_at"]
    if opened_at < mirror_since:
        return False, SKIP_BEFORE_START
    if (now - opened_at).total_seconds() > max_delay_sec:
        return False, SKIP_STALE
    if pos.get("side") != LEG_BUY:
        return False, SKIP_NOT_BUY
    return True, None


def sell_qty(
    filled_qty: Decimal,
    fee: Decimal | None,
    fee_ccy: str | None,
    base: str,
    lot_sz: Decimal,
) -> Decimal:
    """Сколько продавать из купленного.

    Комиссия за покупку на споте OKX берётся В БАЗОВОЙ МОНЕТЕ: купили 0.00002 BTC,
    на счёт пришло на комиссию меньше. Продать все 0.00002 нельзя — биржа
    ответит нехваткой средств. Поэтому: ``filled_qty - |fee|``, если комиссия
    в базовой монете, иначе (в USDT) без вычета. Результат округляется ВНИЗ до
    шага количества ``lot_sz`` — вверх округлять нельзя по той же причине.
    """
    lot = Decimal(str(lot_sz))
    if lot <= 0:
        raise ValueError(f"шаг количества должен быть > 0, получено {lot_sz}")
    qty = Decimal(str(filled_qty))
    if fee and fee_ccy and fee_ccy.upper() == base.upper():
        qty -= abs(Decimal(str(fee)))
    if qty <= 0:
        return Decimal(0)
    return (qty / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def is_dust(qty: Decimal, min_sz: Decimal) -> bool:
    """Меньше ли количество минимального размера ордера биржи (продавать нечего)."""
    return qty <= 0 or qty < Decimal(str(min_sz))


def slippage_pct(leg: str, virtual_price: Decimal, demo_price: Decimal) -> Decimal:
    """Проскальзывание в процентах: ПЛЮС = ХУЖЕ ДЛЯ НАС.

    Покупка: заплатили больше виртуальной цены — плохо, ``(demo/virtual - 1) * 100``.
    Продажа: получили меньше виртуальной цены — плохо, ``(1 - demo/virtual) * 100``.
    """
    virtual = Decimal(str(virtual_price))
    demo = Decimal(str(demo_price))
    if virtual <= 0:
        raise ValueError(f"виртуальная цена должна быть > 0, получено {virtual_price}")
    if leg == LEG_BUY:
        return (demo / virtual - 1) * 100
    if leg == LEG_SELL:
        return (1 - demo / virtual) * 100
    raise ValueError(f"нога сделки должна быть buy или sell, получено {leg!r}")


def pair_pnl(
    buy_cost_usd: Decimal,
    sell_cost_usd: Decimal,
    sell_fee_usdt: Decimal,
) -> tuple[Decimal, Decimal]:
    """Результат пары «покупка + продажа»: ``(прибыль $, прибыль %)``.

    Комиссия покупки в базовой монете в формулу не входит: она уже съела часть
    купленного количества, и продана была меньшая часть — это видно в
    ``sell_cost_usd``. Комиссия продажи берётся в USDT и вычитается явно. Процент —
    от стоимости покупки.
    """
    buy = Decimal(str(buy_cost_usd))
    if buy <= 0:
        raise ValueError(f"стоимость покупки должна быть > 0, получено {buy_cost_usd}")
    profit = Decimal(str(sell_cost_usd)) - buy - abs(Decimal(str(sell_fee_usdt)))
    return profit, profit / buy * 100


def fee_in_usdt(
    fee: Decimal | None, fee_ccy: str | None, price: Decimal, base: str
) -> Decimal:
    """Комиссия в долларах: в USDT — как есть, в базовой монете — по цене исполнения.

    Другая валюта комиссии (например, собственный токен биржи) пересчитать нечем:
    ``ValueError``, а не молчаливый ноль, который занизил бы издержки.
    """
    if not fee:
        return Decimal(0)
    amount = abs(Decimal(str(fee)))
    ccy = (fee_ccy or "").upper()
    if ccy in ("USDT", "USD"):
        return amount
    if ccy == base.upper():
        return amount * Decimal(str(price))
    raise ValueError(f"комиссия в валюте {fee_ccy!r} не пересчитывается в USDT")


def lag_seconds(filled_at: datetime, virtual_ts: datetime) -> int:
    """Задержка исполнения: от момента виртуальной сделки до фактического исполнения."""
    return int(round((filled_at - virtual_ts).total_seconds()))
