#!/usr/bin/env python3
"""Зонд биржи для Этапа 8.2.0: что OKX готова отдать по свечам (вопрос 3 ТЗ).

ТОЛЬКО ЧТЕНИЕ И ТОЛЬКО ПЕЧАТЬ. Скрипт ничего не пишет в БД, не трогает схему,
конфигурацию и .env. Полученные свечи никуда не сохраняются.

Отличие от ``scripts/probe_history_depth.py`` (Этап 7.4): тот зондировал свечи
ТОЛЬКО на споте, а на контракте — только funding. Здесь свечи зондируются и на
СПОТЕ, и на КОНТРАКТЕ: цели по вероятности считаются по обоим рынкам, и глубина
контракта не выводится из глубины спота.

Имя контракта из имени спота НЕ достраивается — пары задаются явно, как в
SYMBOLS (§1 ТЗ 8.1). Формат аргумента — ``СПОТ:КОНТРАКТ`` в нотации instId OKX.

Отдельно проверяется наличие внутрисвечных максимума и минимума (high/low) на
всю доступную глубину: цели считаются по факту КАСАНИЯ уровня, а не по ценам
закрытия, и без high/low весь расчёт невозможен.

Запуск (внутри контейнера — на хосте нет httpx, правило D-3):

    sudo -u agent bash -c 'cd /opt/agent-trade && docker compose --profile backtest \
        run --rm backtest python scripts/recon_8_2_0_okx.py \
        --pairs BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP,XRP-USDT:XRP-USDT-SWAP,DOGE-USDT:DOGE-USDT-SWAP'
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from backtest.loader import OKX_BASE_URL, PATH_HISTORY_CANDLES, create_http_client

RATE_LIMIT_CODE = "50011"

# Кандидаты на максимальный limit — проверяются по убыванию, берётся первый,
# который биржа отдаёт целиком. Значение из документации не принимается на веру.
LIMIT_CANDIDATES = (300, 200, 100)

# Паузы между запросами при поиске потолка частоты, мс.
PACE_CANDIDATES_MS = (400, 200, 100, 50, 25)
PACE_BURST = 12

# Потолок страниц при поиске самой ранней точки и шаг печати прогресса.
MAX_PAGES = 600
PROGRESS_EVERY_PAGES = 25


def to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


async def call(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Один запрос. Любая ошибка возвращается как данные, а не бросается:
    зонд обязан отличить «истории нет» от «нас не пустили»."""
    try:
        response = await client.get(PATH_HISTORY_CANDLES, params=params)
    except Exception as exc:  # noqa: BLE001
        return {"code": "exception", "msg": str(exc), "data": []}
    if response.status_code != 200:
        return {"code": f"HTTP {response.status_code}", "msg": response.text[:200],
                "data": []}
    body = response.json()
    body["_headers"] = dict(response.headers)
    return body


async def probe_limit(client: Any, inst: str, bar: str) -> tuple[int, int]:
    """Максимальный ФАКТИЧЕСКИ отдаваемый limit → (запрошено, получено)."""
    for candidate in LIMIT_CANDIDATES:
        body = await call(client, {"instId": inst, "bar": bar, "limit": str(candidate)})
        data = body.get("data") or []
        if str(body.get("code")) == "0" and data:
            return candidate, len(data)
        await asyncio.sleep(0.3)
    return 0, 0


async def probe_row_shape(client: Any, inst: str, bar: str) -> list[str]:
    """Состав полей строки свечи и наличие внутрисвечных high/low.

    Проверяется не только присутствие колонок, но и их осмысленность:
    high >= max(open, close) и low <= min(open, close) на выборке.
    """
    body = await call(client, {"instId": inst, "bar": bar, "limit": "100"})
    data = body.get("data") or []
    if str(body.get("code")) != "0" or not data:
        return [f"    состав строки: НЕ ОПРЕДЕЛЁН (code={body.get('code')}, "
                f"msg={str(body.get('msg'))[:120]})"]
    row = data[0]
    out = [f"    полей в строке: {len(row)}; сырая первая строка: {row}"]
    if len(row) < 5:
        out.append("    high/low: ОТСУТСТВУЮТ — в строке меньше пяти полей")
        return out
    bad = 0
    degenerate = 0
    for item in data:
        o, h, low, c = (float(item[1]), float(item[2]), float(item[3]), float(item[4]))
        if h < max(o, c) - 1e-12 or low > min(o, c) + 1e-12:
            bad += 1
        if h == low:
            degenerate += 1
    out.append(
        f"    high/low: присутствуют (поля [2] и [3]); проверено свечей {len(data)}, "
        f"нарушений high>=max(o,c) и low<=min(o,c): {bad}; свечей с high==low: {degenerate}"
    )
    return out


async def probe_pagination(client: Any, inst: str, bar: str) -> list[str]:
    """Семантика постраничной выборки: куда ведут `after` и `before`."""
    out: list[str] = []
    first = await call(client, {"instId": inst, "bar": bar, "limit": "5"})
    data = first.get("data") or []
    if not data:
        return ["    пагинация: НЕ ОПРЕДЕЛЕНА (пустой первый ответ)"]
    stamps = [int(r[0]) for r in data]
    out.append(
        f"    порядок строк в ответе: "
        f"{'от новых к старым' if stamps[0] > stamps[-1] else 'от старых к новым'} "
        f"({to_dt(stamps[0]).isoformat()} … {to_dt(stamps[-1]).isoformat()})"
    )
    oldest = min(stamps)
    await asyncio.sleep(0.3)
    back = await call(client, {"instId": inst, "bar": bar, "limit": "5",
                               "after": str(oldest)})
    bd = back.get("data") or []
    if bd:
        newest_back = max(int(r[0]) for r in bd)
        out.append(
            f"    after={oldest} ({to_dt(oldest).isoformat()}) → самая поздняя строка "
            f"{to_dt(newest_back).isoformat()} — "
            f"{'НАЗАД по времени' if newest_back < oldest else 'НЕ назад (см. значение)'}"
        )
    else:
        out.append(f"    after={oldest}: страница пуста (край истории или отказ "
                   f"{back.get('code')})")
    await asyncio.sleep(0.3)
    newest = max(stamps)
    fwd = await call(client, {"instId": inst, "bar": bar, "limit": "5",
                              "before": str(newest)})
    fd = fwd.get("data") or []
    out.append(
        f"    before={newest}: строк {len(fd)}"
        + (f", самая ранняя {to_dt(min(int(r[0]) for r in fd)).isoformat()}" if fd else "")
    )
    return out


async def probe_pace(client: Any, inst: str, bar: str) -> str:
    """Фактический потолок частоты: наращиваем темп до первого кода 50011."""
    last_ok: int | None = None
    first_fail: int | None = None
    headers_seen = ""
    for pause_ms in PACE_CANDIDATES_MS:
        hit = False
        for _ in range(PACE_BURST):
            body = await call(client, {"instId": inst, "bar": bar, "limit": "1"})
            if not headers_seen:
                hdrs = body.get("_headers") or {}
                rate = {k: v for k, v in hdrs.items() if "rate" in k.lower()
                        or "limit" in k.lower()}
                headers_seen = str(rate) if rate else "заголовков о лимите нет"
            if str(body.get("code")) == RATE_LIMIT_CODE or RATE_LIMIT_CODE in str(
                body.get("msg", "")
            ):
                hit = True
                break
            await asyncio.sleep(pause_ms / 1000.0)
        if hit:
            first_fail = pause_ms
            break
        last_ok = pause_ms
        await asyncio.sleep(1.0)
    safe = (last_ok or PACE_CANDIDATES_MS[0]) * 2
    return (
        f"    темп: без 50011 при паузе {last_ok} мс; первый отказ при "
        f"{first_fail} мс; безопасное значение с запасом x2: {safe} мс. "
        f"Заголовки ответа о лимите: {headers_seen}"
    )


async def probe_earliest(
    client: Any, inst: str, bar: str, pause: float, label: str
) -> tuple[datetime | None, int, int]:
    """Самая ранняя доступная свеча: идём назад страницами до пустого ответа.

    Возвращает (самая ранняя метка, число страниц, суммарно свечей). Ход работы
    печатается: обход занимает минуты и без прогресса неотличим от зависания.
    """
    cursor: int | None = None
    earliest: datetime | None = None
    pages = 0
    total = 0
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, Any] = {"instId": inst, "bar": bar, "limit": "100"}
        if cursor is not None:
            params["after"] = str(cursor)
        body = await call(client, params)
        data = body.get("data") or []
        if str(body.get("code")) != "0":
            print(f"    [{label}] страница {page}: отказ {body.get('code')} — "
                  f"{str(body.get('msg'))[:120]}", flush=True)
            break
        if not data:
            print(f"    [{label}] страница {page}: пусто — край истории", flush=True)
            break
        pages = page
        total += len(data)
        oldest = min(int(r[0]) for r in data)
        earliest = to_dt(oldest)
        if page % PROGRESS_EVERY_PAGES == 0:
            days = (datetime.now(UTC) - earliest).days
            print(f"    [{label}] страница {page}: дошли до {earliest.isoformat()} "
                  f"(≈{days} сут назад)", flush=True)
        if cursor is not None and oldest >= cursor:
            print(f"    [{label}] страница {page}: пагинация не движется — останов",
                  flush=True)
            break
        cursor = oldest
        await asyncio.sleep(pause)
    return earliest, pages, total


async def probe_instrument(client: Any, inst: str, market: str, bar: str) -> list[str]:
    """Полный зонд по ОДНОМУ инструменту (спот или контракт — без догадок)."""
    out = [f"\n--- {inst} ({market}), бар {bar} ---"]
    print(f"\n--- {inst} ({market}) ---", flush=True)

    asked, got = await probe_limit(client, inst, bar)
    out.append(f"    свечей за один запрос: запрошено {asked}, получено {got}")
    out.extend(await probe_row_shape(client, inst, bar))
    out.extend(await probe_pagination(client, inst, bar))
    pace_line = await probe_pace(client, inst, bar)
    out.append(pace_line)

    pause = 0.4
    earliest, pages, total = await probe_earliest(client, inst, bar, pause, inst)
    if earliest is None:
        out.append("    глубина назад: НЕ ОПРЕДЕЛЕНА")
    else:
        days = (datetime.now(UTC) - earliest).days
        out.append(
            f"    глубина назад: самая ранняя свеча {earliest.isoformat()} "
            f"(≈{days} сут / ≈{days / 30.0:.1f} мес); страниц пройдено {pages}, "
            f"свечей получено {total}"
        )
        # high/low на САМОМ РАННЕМ краю: наличие их «сейчас» ничего не говорит
        # о том, что они есть на всю глубину.
        edge = await call(client, {"instId": inst, "bar": bar, "limit": "10",
                                   "after": str(int(earliest.timestamp() * 1000) + 1)})
        ed = edge.get("data") or []
        if ed:
            row = ed[0]
            ok = len(row) >= 5 and float(row[2]) >= max(float(row[1]), float(row[4]))
            out.append(
                f"    high/low на самом раннем крае: "
                f"{'есть' if ok else 'ПРОВЕРИТЬ'} — строка {row}"
            )
        else:
            out.append("    high/low на самом раннем крае: страница пуста "
                       "(край совпал с границей истории)")
    return out


async def _main(args: argparse.Namespace) -> int:
    pairs: list[tuple[str, str]] = []
    for item in args.pairs.split(","):
        item = item.strip()
        if not item:
            continue
        spot, sep, swap = item.partition(":")
        if not sep or not swap.strip():
            print(f"Ошибка в --pairs: «{item}» — не пара «СПОТ:КОНТРАКТ». "
                  f"Имя контракта из имени спота не достраивается.")
            return 2
        pairs.append((spot.strip(), swap.strip()))
    if not pairs:
        print("Список --pairs пуст")
        return 2

    client = create_http_client()
    started = time.monotonic()
    print("=" * 78)
    print(" ЗОНД БИРЖИ, Этап 8.2.0 (вопрос 3 ТЗ) — только чтение, ничего не пишется")
    print("=" * 78)
    print(f" Адрес API: {OKX_BASE_URL}{PATH_HISTORY_CANDLES}")
    print(f" Бар: {args.bar}")
    print(f" Время запуска (UTC): {datetime.now(UTC).isoformat()}")
    print(f" Подпись клиента (User-Agent): {client.headers.get('user-agent', '<нет>')}")
    for spot, swap in pairs:
        print(f"   спот {spot} | контракт {swap}")
    lines: list[str] = []
    try:
        for spot, swap in pairs:
            lines += await probe_instrument(client, spot, "спот", args.bar)
            lines += await probe_instrument(client, swap, "контракт", args.bar)
    finally:
        await client.aclose()
    print("\n" + "=" * 78)
    print(" ИТОГОВАЯ ТАБЛИЦА (перенести в reports/8_2_0_recon.md, вопрос 3)")
    print("=" * 78)
    for line in lines:
        print(line)
    print(f"\nВремя работы зонда: {time.monotonic() - started:.0f} с")
    print("Свечи никуда не сохранены: скрипт печатает и завершается.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Зонд глубины часовых свечей OKX по споту И по контракту (8.2.0)"
    )
    parser.add_argument(
        "--pairs",
        default=(
            "BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP,"
            "XRP-USDT:XRP-USDT-SWAP,DOGE-USDT:DOGE-USDT-SWAP"
        ),
        help="пары «СПОТ:КОНТРАКТ» в нотации instId OKX, через запятую",
    )
    parser.add_argument("--bar", default="1H")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
