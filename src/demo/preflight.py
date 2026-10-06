"""Ручная предпроверка демо-исполнения (Этап 9.5, §7 ТЗ).

Запуск владельцем на сервере:

    docker compose run --rm --no-deps demo python -m src.demo.preflight
    docker compose run --rm --no-deps demo python -m src.demo.preflight --test-order

БЕЗ ``--test-order`` ордеров не шлёт: только публичные запросы и чтение баланса.
С ``--test-order`` покупает BTC на сумму слота и сразу продаёт купленное. Строки в
``demo_orders`` НЕ пишутся — к базе предпроверка не подключается вовсе.

Печатает по-русски, по пунктам, итог ``ГОТОВО`` или ``НЕ ГОТОВО: <причина>``.
Код возврата: 0 — готово, 1 — не готово.

Работает и при ``DEMO_ENABLED=false`` — предпроверку делают ДО включения сервиса.
Значения ключей не печатаются.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from src.demo import exchange as demo_exchange
from src.demo import rules
from src.demo.runner import classify_order, error_code_of

# Расхождение часов, после которого подписанные запросы начнут отклоняться
# (биржа допускает около 30 секунд; берём запас).
CLOCK_SKEW_LIMIT_SEC = 20.0
# Инструмент пробного ордера — по ТЗ BTC.
TEST_SYMBOL = "BTC/USDT"
_FILL_WAIT_STEPS = 10


@dataclass
class Report:
    """Накопитель строк вывода и причин «не готово»."""

    lines: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def block(self, reason: str) -> None:
        self.blockers.append(reason)


def _reason_text(exc: BaseException) -> str:
    code = error_code_of(exc)
    if demo_exchange.is_auth_error(exc):
        if code == "50101" or "50101" in str(exc):
            return "код 50101: ключ не подходит к демо-режиму (создан не в режиме «Демо-торговля»?)"
        return f"ошибка авторизации{f' (код {code})' if code else ''}"
    return f"{type(exc).__name__}{f' (код {code})' if code else ''}"


async def _close(ex: Any) -> None:
    try:
        await ex.close()
    except Exception:  # noqa: BLE001 — закрытие клиента не должно ронять проверку
        pass


async def _wait_order(ex: Any, symbol: str, cl_ord_id: str) -> dict[str, Any] | None:
    order = None
    for step in range(_FILL_WAIT_STEPS):
        try:
            order = await demo_exchange.fetch_order_by_cl(ex, symbol, cl_ord_id)
        except ccxt.OrderNotFound:
            order = None
        if order is not None and classify_order(order) != "open":
            return order
        if step + 1 < _FILL_WAIT_STEPS:
            await asyncio.sleep(1.0)
    return order


async def _test_order(ex: Any, rep: Report, slot_usd: Decimal, lot_sz: Decimal,
                      min_sz: Decimal, base: str) -> None:
    """Пробная покупка на сумму слота и немедленная продажа купленного."""
    rep.say(f"6. Пробный ордер (покупка и продажа {TEST_SYMBOL} на ${slot_usd}):")
    stamp = int(time.time())
    buy_id, sell_id = f"at95pfb{stamp}", f"at95pfs{stamp}"
    try:
        ticker = await ex.fetch_ticker(TEST_SYMBOL)
        last = Decimal(str(ticker["last"]))
        t0 = time.perf_counter()
        await demo_exchange.place_market_buy(ex, TEST_SYMBOL, slot_usd, buy_id)
        buy = await _wait_order(ex, TEST_SYMBOL, buy_id)
        buy_lag = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001
        rep.say(f"   ✘ покупка не удалась: {_reason_text(exc)}")
        rep.block(f"пробная покупка не удалась: {_reason_text(exc)}")
        return
    if buy is None or classify_order(buy) != "filled":
        rep.say("   ✘ покупка не исполнена за отведённое время")
        rep.block("пробная покупка не исполнена")
        return
    filled = Decimal(str(buy["filled"]))
    avg = Decimal(str(buy["average"]))
    fee = buy.get("fee") or {}
    rep.say(f"   покупка: {filled} {base} по {avg}; цена до заявки {last}; "
            f"проскальзывание к последней цене {rules.slippage_pct('buy', last, avg):+.4f}%; "
            f"комиссия {fee.get('cost')} {fee.get('currency')}; задержка {buy_lag:.2f} с")
    qty = rules.sell_qty(filled, Decimal(str(fee["cost"])) if fee.get("cost") else None,
                         fee.get("currency"), base, lot_sz)
    if rules.is_dust(qty, min_sz):
        rep.say(f"   продажа не нужна: к продаже {qty} {base} меньше минимума {min_sz}")
        return
    try:
        t1 = time.perf_counter()
        await demo_exchange.place_market_sell(ex, TEST_SYMBOL, qty, sell_id, filled)
        sell = await _wait_order(ex, TEST_SYMBOL, sell_id)
        sell_lag = time.perf_counter() - t1
    except Exception as exc:  # noqa: BLE001
        rep.say(f"   ✘ продажа не удалась: {_reason_text(exc)}")
        rep.block(f"пробная продажа не удалась: {_reason_text(exc)}")
        return
    if sell is None or classify_order(sell) != "filled":
        rep.say("   ✘ продажа не исполнена за отведённое время")
        rep.block("пробная продажа не исполнена")
        return
    sell_avg = Decimal(str(sell["average"]))
    sell_fee = sell.get("fee") or {}
    sell_cost = Decimal(str(sell["cost"]))
    buy_cost = Decimal(str(buy["cost"]))
    try:
        fee_usd = rules.fee_in_usdt(
            Decimal(str(sell_fee["cost"])) if sell_fee.get("cost") else None,
            sell_fee.get("currency"), sell_avg, base)
        profit, pct = rules.pair_pnl(buy_cost, sell_cost, fee_usd)
        result = f"итог круга {profit:+.6f} $ ({pct:+.3f}%)"
    except ValueError as exc:
        result = f"итог круга не посчитан: {exc}"
    rep.say(f"   продажа: {sell['filled']} {base} по {sell_avg}; "
            f"комиссия {sell_fee.get('cost')} {sell_fee.get('currency')}; "
            f"задержка {sell_lag:.2f} с; {result}")
    rep.say("   ✔ пробный круг выполнен (строки в demo_orders не пишутся)")


async def run_preflight(
    make_exchange: Callable[[str], Any],
    configured_host: str,
    symbols: list[str],
    slot_usd: Decimal,
    min_usdt_balance: Decimal,
    test_order: bool = False,
    local_time: Callable[[], float] = time.time,
) -> Report:
    """Выполняет пункты 1–6. ``make_exchange(host)`` создаёт демо-клиент для хоста."""
    rep = Report()
    rep.say("ПРЕДПРОВЕРКА ДЕМО-ИСПОЛНЕНИЯ (OKX, спот, демо-счёт)")

    # 1. Хост и заголовок.
    rep.say("1. Хост и заголовок демо-режима:")
    clients: dict[str, Any] = {}
    try:
        primary = make_exchange(configured_host)
        clients[configured_host] = primary
        demo_exchange.assert_demo(primary)
        rep.say(f"   ✔ хост {configured_host}; заголовок "
                f"{demo_exchange.SIM_HEADER}: {demo_exchange.SIM_VALUE} включён")
    except Exception as exc:  # noqa: BLE001
        rep.say(f"   ✘ {exc}")
        rep.block(f"демо-режим клиента не включён: {exc}")
        return rep

    try:
        # 2. Время сервера.
        rep.say("2. Время сервера OKX и расхождение с часами сервера:")
        try:
            server_ms = await primary.fetch_time()
            skew = server_ms / 1000.0 - local_time()
            ok = abs(skew) <= CLOCK_SKEW_LIMIT_SEC
            rep.say(f"   {'✔' if ok else '✘'} расхождение {skew:+.2f} с "
                    f"(допустимо до {CLOCK_SKEW_LIMIT_SEC:.0f} с)")
            if not ok:
                rep.block(f"часы сервера расходятся с биржей на {skew:+.1f} с")
        except Exception as exc:  # noqa: BLE001
            rep.say(f"   ✘ время биржи не получено: {_reason_text(exc)}")
            rep.block(f"публичный запрос к {configured_host} не прошёл: {_reason_text(exc)}")

        # 3. Авторизация на обоих хостах.
        rep.say("3. Авторизация демо-ключа (какой хост принял ключ):")
        accepted: list[str] = []
        balances: dict[str, Decimal] = {}
        for host in demo_exchange.ALLOWED_HOSTS:
            ex = clients.get(host)
            if ex is None:
                try:
                    ex = clients[host] = make_exchange(host)
                except Exception as exc:  # noqa: BLE001
                    rep.say(f"   ✘ {host}: клиент не создан: {exc}")
                    continue
            try:
                balances[host] = await demo_exchange.fetch_usdt_free(ex)
                accepted.append(host)
                rep.say(f"   ✔ {host}: ключ принят")
            except Exception as exc:  # noqa: BLE001
                rep.say(f"   ✘ {host}: {_reason_text(exc)}")
        if configured_host in accepted:
            rep.say(f"   Итог: OKX_DEMO_HOST={configured_host} — верно")
            host = configured_host
        elif accepted:
            host = accepted[0]
            rep.say(f"   Итог: впишите в .env OKX_DEMO_HOST={host}")
            rep.block(f"ключ принят хостом {host}, а в настройках {configured_host}: "
                      f"впишите OKX_DEMO_HOST={host}")
        else:
            rep.say("   Итог: ни один хост ключ не принял")
            rep.block("демо-ключ не принят ни www.okx.com, ни eea.okx.com")
            return rep

        # 4. Баланс.
        rep.say("4. Баланс USDT на демо-счёте:")
        balance = balances[host]
        enough = balance >= min_usdt_balance
        rep.say(f"   {'✔' if enough else '✘'} {balance} USDT "
                f"(порог покупок {min_usdt_balance})")
        if not enough:
            rep.block(f"на демо-счёте {balance} USDT — меньше порога {min_usdt_balance}")

        # 5. Инструменты.
        rep.say(f"5. Инструменты (хватает ли ${slot_usd} на минимальный ордер):")
        ex = clients[host]
        limits: dict[str, tuple[Decimal, Decimal]] = {}
        try:
            await ex.load_markets()
        except Exception as exc:  # noqa: BLE001
            rep.say(f"   ✘ рынки не загружены: {_reason_text(exc)}")
            rep.block(f"рынки не загружены: {_reason_text(exc)}")
            return rep
        for symbol in symbols:
            market = (ex.markets or {}).get(symbol)
            if market is None or not market.get("spot") or ":" in symbol:
                rep.say(f"   ✘ {symbol}: нет на споте биржи")
                rep.block(f"{symbol} нет на споте биржи")
                continue
            lot, min_sz = demo_exchange.market_limits(market)
            limits[symbol] = (lot, min_sz)
            try:
                last = Decimal(str((await ex.fetch_ticker(symbol))["last"]))
            except Exception as exc:  # noqa: BLE001
                rep.say(f"   ✘ {symbol}: цена не получена: {_reason_text(exc)}")
                rep.block(f"{symbol}: цена не получена")
                continue
            min_usd = min_sz * last
            fits = slot_usd >= min_usd
            rep.say(f"   {'✔' if fits else '✘'} {symbol}: минимальный размер {min_sz}, "
                    f"шаг {lot}, минимум в долларах {min_usd:.4f} при цене {last}"
                    f"{'' if fits else f' — ${slot_usd} не хватает'}")
            if not fits:
                rep.block(f"{symbol}: ${slot_usd} меньше минимального ордера ${min_usd:.4f}")

        # 6. Пробный ордер.
        if test_order:
            lot, min_sz = limits.get(TEST_SYMBOL) or demo_exchange.market_limits(
                (ex.markets or {}).get(TEST_SYMBOL, {})
            )
            if lot <= 0:
                rep.say(f"6. ✘ нет данных по {TEST_SYMBOL} — пробный ордер не отправлен")
                rep.block(f"нет данных по {TEST_SYMBOL}")
            else:
                await _test_order(ex, rep, slot_usd, lot, min_sz, TEST_SYMBOL.split("/")[0])
        else:
            rep.say("6. Пробный ордер: не выполнялся (запустите с --test-order)")
        return rep
    finally:
        for client in clients.values():
            await _close(client)


def render(rep: Report) -> str:
    """Текст вывода с итоговой строкой."""
    verdict = "ГОТОВО" if not rep.blockers else "НЕ ГОТОВО: " + "; ".join(rep.blockers)
    return "\n".join([*rep.lines, "", verdict])


def main() -> None:
    """Точка входа CLI."""
    import sys

    from src.core.config import settings

    parser = argparse.ArgumentParser(description="Предпроверка демо-исполнения OKX")
    parser.add_argument("--test-order", action="store_true",
                        help="купить BTC на сумму слота и сразу продать")
    args = parser.parse_args()

    problems = [e for e in settings.demo_config_errors() if "DEMO_ENABLED" not in e]
    for name in ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE"):
        if not getattr(settings, name).strip():
            problems.append(f"{name} пуст")
    if problems:
        print("ПРЕДПРОВЕРКА ДЕМО-ИСПОЛНЕНИЯ\n\nНЕ ГОТОВО: " + "; ".join(problems))
        sys.exit(1)

    def make(host: str) -> Any:
        return demo_exchange.create_demo_exchange(
            settings.OKX_DEMO_API_KEY, settings.OKX_DEMO_SECRET_KEY,
            settings.OKX_DEMO_PASSPHRASE, host,
        )

    rep = asyncio.run(run_preflight(
        make, settings.OKX_DEMO_HOST,
        [pair.spot for pair in settings.symbol_pairs],
        Decimal(str(settings.POSITION_SLOT_USD)),
        Decimal(str(settings.DEMO_MIN_USDT_BALANCE)),
        test_order=args.test_order,
    ))
    print(render(rep))
    sys.exit(0 if not rep.blockers else 1)


if __name__ == "__main__":
    main()
