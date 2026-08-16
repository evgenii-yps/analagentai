#!/usr/bin/env python3
"""Зонд глубины истории OKX (§4.1 ТЗ). Выполняется ПЕРВЫМ, до любой другой работы.

Ничего не пишет в БД и не меняет. Печатает таблицу в stdout. Ни одно значение
из ТЗ не принимается на веру: максимальный limit, параметр пагинации, самая
ранняя доступная точка, фактический потолок частоты запросов и интервал между
записями funding определяются ЭМПИРИЧЕСКИ.

Запуск (внутри контейнера, на хосте pip-пакетов нет — правило D-3):

    docker compose --profile backtest run --rm backtest \\
        python scripts/probe_history_depth.py \\
        --instruments BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP

Потолок частоты определяется наращиванием темпа до первого кода 50011;
в качестве безопасного значения печатается пауза с запасом ×2.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.config import settings
from src.core.exchange import create_exchange

RATE_LIMIT_CODE = "50011"

# Кандидаты на максимальный limit: проверяются по убыванию, берётся первый,
# который биржа реально отдаёт целиком.
LIMIT_CANDIDATES = (300, 200, 100, 50)

# Темпы для поиска потолка частоты: пауза между запросами, мс.
PACE_CANDIDATES_MS = (400, 200, 100, 50, 25)

# Сколько запросов подряд делать на каждом темпе.
PACE_BURST = 12


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


async def _call(exchange: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Один вызов эндпоинта. Возвращает разобранный ответ или описание ошибки."""
    try:
        response = await getattr(exchange, method)(params)
    except Exception as exc:  # noqa: BLE001 — зонд обязан пережить любую ошибку
        return {"code": "exception", "msg": str(exc), "data": []}
    return response


async def probe_limit(exchange: Any, method: str, base: dict[str, Any]) -> tuple[int, int]:
    """Максимальный фактически отдаваемый limit → (запрошено, получено)."""
    for candidate in LIMIT_CANDIDATES:
        params = dict(base)
        params["limit"] = str(candidate)
        response = await _call(exchange, method, params)
        data = response.get("data") or []
        if str(response.get("code")) == "0" and data:
            return candidate, len(data)
        await asyncio.sleep(0.3)
    return 0, 0


async def probe_pagination(
    exchange: Any, method: str, base: dict[str, Any], ts_key: str
) -> str:
    """Семантика параметра пагинации: проверяется, что `after` идёт НАЗАД по времени."""
    first = await _call(exchange, method, {**base, "limit": "5"})
    data = first.get("data") or []
    if not data:
        return "не определена (пустой первый ответ)"
    oldest = min(int(_ts_of(row, ts_key)) for row in data)
    await asyncio.sleep(0.3)
    second = await _call(exchange, method, {**base, "limit": "5", "after": str(oldest)})
    data2 = second.get("data") or []
    if not data2:
        return "after: страница пуста (вероятно, достигнут край истории)"
    newest2 = max(int(_ts_of(row, ts_key)) for row in data2)
    if newest2 < oldest:
        return "after = записи РАНЬШЕ указанной метки (движение назад по времени)"
    return f"after ведёт себя иначе: newest={to_dt(newest2)} при after={to_dt(oldest)}"


def _ts_of(row: Any, ts_key: str) -> Any:
    return row[0] if isinstance(row, list) else row[ts_key]


async def probe_earliest(
    exchange: Any, method: str, base: dict[str, Any], ts_key: str, pause: float
) -> datetime | None:
    """Самая ранняя доступная метка времени: идём назад страницами до пустого ответа."""
    cursor: int | None = None
    earliest: datetime | None = None
    for _ in range(400):  # потолок шагов, чтобы зонд не работал бесконечно
        params = {**base, "limit": "100"}
        if cursor is not None:
            params["after"] = str(cursor)
        response = await _call(exchange, method, params)
        data = response.get("data") or []
        if str(response.get("code")) != "0" or not data:
            break
        oldest = min(int(_ts_of(row, ts_key)) for row in data)
        earliest = to_dt(oldest)
        if cursor is not None and oldest >= cursor:
            break
        cursor = oldest
        await asyncio.sleep(pause)
    return earliest


async def probe_pace(exchange: Any, method: str, base: dict[str, Any]) -> dict[str, Any]:
    """Фактический потолок частоты: наращиваем темп до первого кода 50011."""
    last_ok_ms: int | None = None
    first_fail_ms: int | None = None
    for pause_ms in PACE_CANDIDATES_MS:
        hit_limit = False
        started = time.monotonic()
        for _ in range(PACE_BURST):
            response = await _call(exchange, method, {**base, "limit": "1"})
            if str(response.get("code")) == RATE_LIMIT_CODE or (
                RATE_LIMIT_CODE in str(response.get("msg", ""))
            ):
                hit_limit = True
                break
            await asyncio.sleep(pause_ms / 1000.0)
        elapsed = time.monotonic() - started
        if hit_limit:
            first_fail_ms = pause_ms
            break
        last_ok_ms = pause_ms
        await asyncio.sleep(1.0)
        del elapsed
    safe = (last_ok_ms or PACE_CANDIDATES_MS[0]) * 2
    return {
        "last_ok_pause_ms": last_ok_ms,
        "first_fail_pause_ms": first_fail_ms,
        "safe_pause_ms": safe,
    }


async def probe_funding_interval(
    exchange: Any, inst_id: str
) -> tuple[str, list[timedelta]]:
    """Фактический интервал между записями funding — подтверждается, а не берётся из ТЗ."""
    response = await _call(
        exchange,
        "publicGetPublicFundingRateHistory",
        {"instId": inst_id, "limit": "20"},
    )
    data = response.get("data") or []
    stamps = sorted(to_dt(row["fundingTime"]) for row in data)
    if len(stamps) < 2:
        return "недостаточно записей для оценки", []
    deltas = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    unique = sorted({int(d.total_seconds() // 3600) for d in deltas})
    return f"интервалы, ч: {unique}; записей в сутки ≈ {24 / (unique[0] or 1):.1f}", deltas


async def probe_instrument(exchange: Any, inst_id: str, bar: str) -> list[str]:
    """Полный зонд по одному инструменту. Возвращает строки отчёта."""
    out: list[str] = [f"\n=== {inst_id} ==="]

    candles_base = {"instId": inst_id, "bar": bar}
    funding_base = {"instId": inst_id}

    asked, got = await probe_limit(exchange, "publicGetMarketHistoryCandles", candles_base)
    out.append(f"  свечи: максимальный limit — запрошено {asked}, получено {got}")
    pagination = await probe_pagination(
        exchange, "publicGetMarketHistoryCandles", candles_base, "ts"
    )
    out.append(f"  свечи: пагинация — {pagination}")

    pace = await probe_pace(exchange, "publicGetMarketHistoryCandles", candles_base)
    out.append(
        f"  свечи: темп — без 50011 при паузе {pace['last_ok_pause_ms']} мс, "
        f"первый отказ при {pace['first_fail_pause_ms']} мс, "
        f"БЕЗОПАСНОЕ значение с запасом x2: {pace['safe_pause_ms']} мс"
    )

    pause = (pace["safe_pause_ms"] or 400) / 1000.0
    earliest = await probe_earliest(
        exchange, "publicGetMarketHistoryCandles", candles_base, "ts", pause
    )
    if earliest is None:
        out.append("  свечи: самая ранняя точка НЕ ОПРЕДЕЛЕНА")
    else:
        months = (datetime.now(UTC) - earliest).days / 30.0
        verdict = "ГОДИТСЯ" if months >= 24 else "КОРОТКО — инструмент исключается (§4.2)"
        out.append(
            f"  свечи: самая ранняя точка {earliest.isoformat()} "
            f"(≈{months:.1f} мес назад) — {verdict}"
        )

    asked_f, got_f = await probe_limit(
        exchange, "publicGetPublicFundingRateHistory", funding_base
    )
    out.append(f"  funding: максимальный limit — запрошено {asked_f}, получено {got_f}")
    interval, _deltas = await probe_funding_interval(exchange, inst_id)
    out.append(f"  funding: {interval}")
    earliest_f = await probe_earliest(
        exchange, "publicGetPublicFundingRateHistory", funding_base, "fundingTime", pause
    )
    out.append(
        "  funding: самая ранняя точка "
        + (earliest_f.isoformat() if earliest_f else "НЕ ОПРЕДЕЛЕНА")
    )
    out.append(
        "  открытый интерес: исторического ряда среди разрешённых §4 эндпоинтов НЕТ — "
        "ветка подтверждения OI в реплее недоступна (подстановка запрещена)"
    )
    return out


async def _main(args: argparse.Namespace) -> int:
    instruments = [x.strip() for x in args.instruments.split(",") if x.strip()]
    exchange = create_exchange(settings.EXCHANGE)
    print("=" * 78)
    print(" ЗОНД ГЛУБИНЫ ИСТОРИИ OKX (Этап 7.4, §4.1 ТЗ)")
    print("=" * 78)
    print(f" Биржа (из продакшн-конфигурации): {settings.EXCHANGE}")
    print(f" Бар: {args.bar}")
    print(f" Время запуска (UTC): {datetime.now(UTC).isoformat()}")
    print(" Скрипт ничего не пишет в БД и не меняет конфигурацию.")
    try:
        for inst_id in instruments:
            for line in await probe_instrument(exchange, inst_id, args.bar):
                print(line)
    finally:
        await exchange.close()
    print("\nПеренесите эту таблицу в docs/STAGE_7_4_REPORT.md (§14.1) и подставьте")
    print("BT_PERIOD_FROM и BT_REQUEST_PAUSE_MS в backtest/.env.backtest.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Зонд глубины истории OKX (§4.1)")
    parser.add_argument(
        "--instruments",
        default="BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP",
        help="список инструментов через запятую",
    )
    parser.add_argument("--bar", default="1H")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
