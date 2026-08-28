#!/usr/bin/env python3
"""Зонд глубины истории OKX (§4.1 ТЗ). Выполняется ПЕРВЫМ, до любой другой работы.

Ничего не пишет в БД и не меняет. Печатает таблицу в stdout. Ни одно значение
из ТЗ не принимается на веру: максимальный limit, параметр пагинации, самая
ранняя доступная точка, фактический потолок частоты запросов и интервал между
записями funding определяются ЭМПИРИЧЕСКИ.

HTTP-клиент — тот же ``backtest.loader.create_http_client``, а подпись у него
БРАУЗЕРНАЯ, из ``src.core.http`` (правка Этапа 8.10.1). Прежде здесь было
написано, что штатной подписи ``python-httpx`` достаточно; 28.08.2026 на
сервере она стала получать 403 с кодом 1010 ещё до проверки ключа, и требование
сменилось на обратное. Первым делом зонд печатает фактическую подпись клиента и
код ответа — чтобы отказ по подписи был виден сразу, а не выглядел «нет
истории».

ИНСТРУМЕНТЫ ЗАДАЮТСЯ ПАРАМИ «спот:контракт». Свечи зондируются на СПОТЕ (там
их собирает Market Agent), funding — на КОНТРАКТЕ (у спота его не существует:
биржа отвечает HTTP 400, код 51000 «Parameter instId error»). Имя контракта
из имени спота не достраивается — оно указывается явно.

ХОД РАБОТЫ ПЕЧАТАЕТСЯ (дефект D-10). Поиск самой ранней точки идёт сотнями
страниц назад по времени и занимает минуты; каждые ``PROGRESS_EVERY_PAGES``
страниц печатается строка с достигнутой датой, чтобы работа зонда не выглядела
зависанием.

Запуск (внутри контейнера, на хосте pip-пакетов нет — правило D-3):

    docker compose --profile backtest run --rm backtest \\
        python scripts/probe_history_depth.py \\
        --instruments BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP

Потолок частоты определяется наращиванием темпа до первого кода 50011;
в качестве безопасного значения печатается пауза с запасом ×2.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from backtest.config import ConfigError, InstrumentPair
from backtest.loader import (
    OKX_BASE_URL,
    PATH_FUNDING_HISTORY,
    PATH_HISTORY_CANDLES,
    create_http_client,
)

RATE_LIMIT_CODE = "50011"

# Кандидаты на максимальный limit: проверяются по убыванию, берётся первый,
# который биржа реально отдаёт целиком.
LIMIT_CANDIDATES = (300, 200, 100, 50)

# Темпы для поиска потолка частоты: пауза между запросами, мс.
PACE_CANDIDATES_MS = (400, 200, 100, 50, 25)

# Сколько запросов подряд делать на каждом темпе.
PACE_BURST = 12

# Через сколько страниц печатать ход поиска самой ранней точки (дефект D-10).
PROGRESS_EVERY_PAGES = 10


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


async def _call(client: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Один запрос к эндпоинту. Возвращает тело ответа или описание ошибки.

    Зонд обязан пережить любую ошибку и напечатать её: он для того и нужен,
    чтобы отличить «нет истории» от «нас не пустили».
    """
    try:
        response = await client.get(path, params=params)
    except Exception as exc:  # noqa: BLE001
        return {"code": "exception", "msg": str(exc), "data": []}
    if response.status_code != 200:
        return {
            "code": f"HTTP {response.status_code}",
            "msg": response.text[:200],
            "data": [],
        }
    return response.json()


async def probe_limit(client: Any, path: str, base: dict[str, Any]) -> tuple[int, int]:
    """Максимальный фактически отдаваемый limit → (запрошено, получено)."""
    for candidate in LIMIT_CANDIDATES:
        params = dict(base)
        params["limit"] = str(candidate)
        response = await _call(client, path, params)
        data = response.get("data") or []
        if str(response.get("code")) == "0" and data:
            return candidate, len(data)
        await asyncio.sleep(0.3)
    return 0, 0


async def probe_pagination(
    client: Any, path: str, base: dict[str, Any], ts_key: str
) -> str:
    """Семантика параметра пагинации: проверяется, что `after` идёт НАЗАД по времени."""
    first = await _call(client, path, {**base, "limit": "5"})
    data = first.get("data") or []
    if not data:
        return "не определена (пустой первый ответ)"
    oldest = min(int(_ts_of(row, ts_key)) for row in data)
    await asyncio.sleep(0.3)
    second = await _call(client, path, {**base, "limit": "5", "after": str(oldest)})
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
    client: Any, path: str, base: dict[str, Any], ts_key: str, pause: float,
    label: str = "",
) -> datetime | None:
    """Самая ранняя доступная метка времени: идём назад страницами до пустого ответа.

    Ход работы печатается каждые ``PROGRESS_EVERY_PAGES`` страниц: на длинной
    истории обход занимает минуты, и без строки прогресса зонд неотличим от
    зависшего (дефект D-10).
    """
    cursor: int | None = None
    earliest: datetime | None = None
    for page in range(1, 401):  # потолок шагов, чтобы зонд не работал бесконечно
        params = {**base, "limit": "100"}
        if cursor is not None:
            params["after"] = str(cursor)
        response = await _call(client, path, params)
        data = response.get("data") or []
        if str(response.get("code")) != "0" or not data:
            if str(response.get("code")) != "0":
                print(f"    [{label}] страница {page}: отказ "
                      f"{response.get('code')} — {str(response.get('msg'))[:120]}",
                      flush=True)
            else:
                print(f"    [{label}] страница {page}: пусто — край истории",
                      flush=True)
            break
        oldest = min(int(_ts_of(row, ts_key)) for row in data)
        earliest = to_dt(oldest)
        if page % PROGRESS_EVERY_PAGES == 0:
            months = (datetime.now(UTC) - earliest).days / 30.0
            print(
                f"    [{label}] страница {page}: дошли до {earliest.isoformat()} "
                f"(≈{months:.1f} мес назад)",
                flush=True,
            )
        if cursor is not None and oldest >= cursor:
            print(f"    [{label}] страница {page}: пагинация не движется — останов",
                  flush=True)
            break
        cursor = oldest
        await asyncio.sleep(pause)
    return earliest


async def probe_pace(client: Any, path: str, base: dict[str, Any]) -> dict[str, Any]:
    """Фактический потолок частоты: наращиваем темп до первого кода 50011."""
    last_ok_ms: int | None = None
    first_fail_ms: int | None = None
    for pause_ms in PACE_CANDIDATES_MS:
        hit_limit = False
        started = time.monotonic()
        for _ in range(PACE_BURST):
            response = await _call(client, path, {**base, "limit": "1"})
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
    client: Any, inst_id: str
) -> tuple[str, list[timedelta]]:
    """Фактический интервал между записями funding — подтверждается, а не берётся из ТЗ."""
    response = await _call(
        client,
        PATH_FUNDING_HISTORY,
        {"instId": inst_id, "limit": "20"},
    )
    data = response.get("data") or []
    stamps = sorted(to_dt(row["fundingTime"]) for row in data)
    if len(stamps) < 2:
        return "недостаточно записей для оценки", []
    deltas = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    unique = sorted({int(d.total_seconds() // 3600) for d in deltas})
    return f"интервалы, ч: {unique}; записей в сутки ≈ {24 / (unique[0] or 1):.1f}", deltas


async def probe_instrument(client: Any, pair: InstrumentPair, bar: str) -> list[str]:
    """Полный зонд по одной ПАРЕ рынков. Возвращает строки отчёта.

    Свечи зондируются на споте, funding — на контракте. Если контракт не задан,
    ряд funding не зондируется вовсе и это печатается прямо: молча выдавать
    отсутствие строки за отсутствие истории нельзя.
    """
    out: list[str] = [f"\n=== {pair.label} ==="]
    print(f"\n=== {pair.label} === (свечи: {pair.spot}, funding: {pair.swap or '—'})",
          flush=True)

    candles_base = {"instId": pair.spot, "bar": bar}

    asked, got = await probe_limit(client, PATH_HISTORY_CANDLES, candles_base)
    out.append(f"  свечи ({pair.spot}): максимальный limit — "
               f"запрошено {asked}, получено {got}")
    pagination = await probe_pagination(
        client, PATH_HISTORY_CANDLES, candles_base, "ts"
    )
    out.append(f"  свечи ({pair.spot}): пагинация — {pagination}")

    pace = await probe_pace(client, PATH_HISTORY_CANDLES, candles_base)
    out.append(
        f"  свечи ({pair.spot}): темп — без 50011 при паузе "
        f"{pace['last_ok_pause_ms']} мс, "
        f"первый отказ при {pace['first_fail_pause_ms']} мс, "
        f"БЕЗОПАСНОЕ значение с запасом x2: {pace['safe_pause_ms']} мс"
    )

    pause = (pace["safe_pause_ms"] or 400) / 1000.0
    earliest = await probe_earliest(
        client, PATH_HISTORY_CANDLES, candles_base, "ts", pause,
        label=f"свечи {pair.spot}",
    )
    if earliest is None:
        out.append(f"  свечи ({pair.spot}): самая ранняя точка НЕ ОПРЕДЕЛЕНА")
    else:
        months = (datetime.now(UTC) - earliest).days / 30.0
        verdict = "ГОДИТСЯ" if months >= 24 else "КОРОТКО — инструмент исключается (§4.2)"
        out.append(
            f"  свечи ({pair.spot}): самая ранняя точка {earliest.isoformat()} "
            f"(≈{months:.1f} мес назад) — {verdict}"
        )

    if not pair.swap:
        out.append(
            "  funding: НЕ ЗОНДИРУЕТСЯ — контракт для этой пары не задан. "
            "У спота истории funding не существует (код 51000)"
        )
        return out

    funding_base = {"instId": pair.swap}
    asked_f, got_f = await probe_limit(
        client, PATH_FUNDING_HISTORY, funding_base
    )
    out.append(f"  funding ({pair.swap}): максимальный limit — "
               f"запрошено {asked_f}, получено {got_f}")
    interval, _deltas = await probe_funding_interval(client, pair.swap)
    out.append(f"  funding ({pair.swap}): {interval}")
    earliest_f = await probe_earliest(
        client, PATH_FUNDING_HISTORY, funding_base, "fundingTime", pause,
        label=f"funding {pair.swap}",
    )
    out.append(
        f"  funding ({pair.swap}): самая ранняя точка "
        + (earliest_f.isoformat() if earliest_f else "НЕ ОПРЕДЕЛЕНА")
    )
    out.append(
        "  открытый интерес: исторического ряда среди разрешённых §4 эндпоинтов НЕТ — "
        "ветка подтверждения OI в реплее недоступна (подстановка запрещена)"
    )
    return out


async def probe_client_signature(client: Any, probe_inst_id: str) -> list[str]:
    """Подпись клиента и код ответа — печатается ПЕРВОЙ.

    Отказ по подписи выглядит как «истории нет», и без этой проверки его легко
    принять за отсутствие данных. Здесь видно и то, чем мы представились, и что
    ответила биржа.
    """
    user_agent = client.headers.get("user-agent", "<не задан>")
    out = [f"  подпись клиента (User-Agent): {user_agent}"]
    try:
        response = await client.get(
            PATH_HISTORY_CANDLES,
            params={"instId": probe_inst_id, "bar": "1H", "limit": "1"},
        )
        out.append(f"  ответ OKX на пробный запрос: HTTP {response.status_code}")
        if response.status_code != 200:
            out.append(f"  тело ответа: {response.text[:200]}")
            out.append("  ВНИМАНИЕ: биржа не пустила. Дальнейшие пустые результаты —")
            out.append("  следствие отказа, а НЕ отсутствия истории.")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  пробный запрос не прошёл: {exc}")
    return out


async def _main(args: argparse.Namespace) -> int:
    try:
        pairs = [
            InstrumentPair.parse(item)
            for item in args.instruments.split(",")
            if item.strip()
        ]
    except ConfigError as exc:
        print(f"Ошибка в --instruments: {exc}")
        return 2
    if not pairs:
        print("Список --instruments пуст")
        return 2

    client = create_http_client()
    print("=" * 78)
    print(" ЗОНД ГЛУБИНЫ ИСТОРИИ OKX (Этап 7.4, §4.1 ТЗ)")
    print("=" * 78)
    print(f" Адрес API: {OKX_BASE_URL}")
    print(f" Бар: {args.bar}")
    print(f" Время запуска (UTC): {datetime.now(UTC).isoformat()}")
    print(" Пары «спот → контракт»: свечи зондируются на споте, funding — на контракте.")
    for pair in pairs:
        print(f"   {pair.spot} → {pair.swap or 'контракт не задан, funding не зондируется'}")
    print(" Скрипт ничего не пишет в БД и не меняет конфигурацию.", flush=True)
    try:
        print("\n=== Проверка доступа ===", flush=True)
        for line in await probe_client_signature(client, pairs[0].spot):
            print(line, flush=True)
        for pair in pairs:
            for line in await probe_instrument(client, pair, args.bar):
                print(line, flush=True)
    finally:
        await client.aclose()
    print("\nПеренесите эту таблицу в docs/STAGE_7_4_REPORT.md (§14.1) и подставьте")
    print("BT_PERIOD_FROM и BT_REQUEST_PAUSE_MS в backtest/.env.backtest.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Зонд глубины истории OKX (§4.1)")
    parser.add_argument(
        "--instruments",
        default="BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP,SOL-USDT:SOL-USDT-SWAP",
        help=(
            "пары «спот:контракт» через запятую, например "
            "BTC-USDT:BTC-USDT-SWAP. Имя контракта из имени спота не достраивается"
        ),
    )
    parser.add_argument("--bar", default="1H")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
