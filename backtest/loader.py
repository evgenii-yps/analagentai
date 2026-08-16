"""Загрузка истории OKX в схему backtest.

Используется ТОТ ЖЕ клиент, которым к OKX ходит коллектор
(``src.core.exchange.create_exchange`` → ccxt.async_support), новых внешних
зависимостей не вводится (§12.1 ТЗ). Обращения идут к явным эндпоинтам §4:

    GET /api/v5/market/history-candles          → publicGetMarketHistoryCandles
    GET /api/v5/public/funding-rate-history      → publicGetPublicFundingRateHistory

Загрузка идемпотентна: повторный запуск не перекачивает уже загруженное
(``ON CONFLICT DO NOTHING`` плюс старт пагинации от самой ранней имеющейся
точки). Незакрытые свечи (``confirm = 0``) не сохраняются вообще (§4.4).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from backtest import db
from src.core.exchange import create_exchange

_log = structlog.get_logger().bind(component="backtest.loader")

# Длительность бара в секундах. Список сознательно короткий: реплей работает на
# том же таймфрейме, что Market Agent в продакшне (AGENT_TIMEFRAME=1h).
BAR_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
}

# Код OKX «слишком много запросов». Ловится отдельно: это не сбой загрузки,
# а сигнал сбавить темп.
RATE_LIMIT_CODE = "50011"

# Сколько раз повторять запрос при ошибке темпа, с удвоением паузы.
_MAX_RETRIES = 6


class LoaderError(RuntimeError):
    """Ошибка загрузки истории: страница не получена, данные не пишутся."""


def bar_seconds(bar: str) -> int:
    if bar not in BAR_SECONDS:
        raise LoaderError(f"неизвестный бар {bar}: допустимы {sorted(BAR_SECONDS)}")
    return BAR_SECONDS[bar]


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _dt(ms: str | int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


class OkxHistory:
    """Тонкая обёртка над двумя историческими эндпоинтами OKX.

    Держит паузу между запросами (значение берётся из конфигурации и должно
    быть получено ЗОНДОМ, а не выбрано наугад) и повторяет запрос при коде
    ограничения темпа.
    """

    def __init__(self, exchange: Any, pause_ms: int) -> None:
        self.exchange = exchange
        self.pause_ms = pause_ms

    async def _call(self, method_name: str, params: dict[str, Any]) -> list[Any]:
        method = getattr(self.exchange, method_name)
        pause = self.pause_ms / 1000.0
        for attempt in range(_MAX_RETRIES):
            await asyncio.sleep(pause)
            try:
                response = await method(params)
            except Exception as exc:  # noqa: BLE001 — сеть/биржа, решаем по тексту
                text = str(exc)
                if RATE_LIMIT_CODE in text and attempt < _MAX_RETRIES - 1:
                    pause *= 2
                    _log.warning("OKX: превышен темп запросов, пауза удвоена",
                                 method=method_name, pause_sec=round(pause, 3))
                    continue
                raise LoaderError(f"{method_name}: {text}") from exc
            code = str(response.get("code", "0"))
            if code == RATE_LIMIT_CODE and attempt < _MAX_RETRIES - 1:
                pause *= 2
                _log.warning("OKX: код 50011, пауза удвоена",
                             method=method_name, pause_sec=round(pause, 3))
                continue
            if code != "0":
                raise LoaderError(f"{method_name}: код {code}, {response.get('msg')}")
            return list(response.get("data") or [])
        raise LoaderError(f"{method_name}: не удалось получить страницу за {_MAX_RETRIES} попыток")

    async def candles_page(
        self, inst_id: str, bar: str, after_ms: int | None, limit: int
    ) -> list[list[str]]:
        """Страница исторических свечей, записи РАНЬШЕ ``after_ms`` (пагинация OKX)."""
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after_ms is not None:
            params["after"] = str(after_ms)
        return await self._call("publicGetMarketHistoryCandles", params)

    async def funding_page(
        self, inst_id: str, after_ms: int | None, limit: int
    ) -> list[dict[str, Any]]:
        """Страница истории funding, записи РАНЬШЕ ``after_ms``."""
        params: dict[str, Any] = {"instId": inst_id, "limit": str(limit)}
        if after_ms is not None:
            params["after"] = str(after_ms)
        return await self._call("publicGetPublicFundingRateHistory", params)


def parse_candles(
    rows: Sequence[Sequence[str]],
    inst_id: str,
    bar: str,
) -> list[tuple[Any, ...]]:
    """Разбирает ответ history-candles в строки для вставки.

    Формат OKX: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm].
    Незакрытые свечи (``confirm != '1'``) отбрасываются здесь же — в БД они
    не попадают ни при каких условиях (§4.4 ТЗ): по незакрытой свече агент в
    продакшне решения не принимает, и в реплее её быть не должно.
    """
    step = bar_seconds(bar)
    parsed: list[tuple[Any, ...]] = []
    for row in rows:
        if len(row) < 6:
            continue
        if len(row) >= 9 and str(row[8]) != "1":
            continue
        open_time = _dt(row[0])
        parsed.append(
            (
                inst_id,
                bar,
                open_time,
                datetime.fromtimestamp(open_time.timestamp() + step, tz=UTC),
                Decimal(str(row[1])),
                Decimal(str(row[2])),
                Decimal(str(row[3])),
                Decimal(str(row[4])),
                Decimal(str(row[5])),
                Decimal(str(row[6])) if len(row) > 6 and row[6] not in ("", None) else None,
            )
        )
    return parsed


def parse_funding(
    rows: Sequence[dict[str, Any]],
    inst_id: str,
) -> list[tuple[Any, ...]]:
    """Разбирает ответ funding-rate-history в строки для вставки."""
    parsed: list[tuple[Any, ...]] = []
    for row in rows:
        ts_raw = row.get("fundingTime")
        rate_raw = row.get("realizedRate") or row.get("fundingRate")
        if ts_raw is None or rate_raw in (None, ""):
            continue
        parsed.append((inst_id, _dt(ts_raw), Decimal(str(rate_raw))))
    return parsed


async def _insert_candles(rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    result = await db.pool().executemany(
        """
        INSERT INTO backtest.candles
            (inst_id, bar, open_time, close_time, open, high, low, close,
             volume, volume_ccy)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (inst_id, bar, open_time) DO NOTHING;
        """,
        rows,
    )
    return len(rows) if result is None else len(rows)


async def _insert_funding(rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    await db.pool().executemany(
        """
        INSERT INTO backtest.funding (inst_id, funding_time, funding_rate)
        VALUES ($1,$2,$3)
        ON CONFLICT (inst_id, funding_time) DO NOTHING;
        """,
        rows,
    )
    return len(rows)


async def backfill_candles(
    inst_id: str,
    bar: str,
    since: datetime,
    until: datetime,
    *,
    client: OkxHistory | None = None,
    page_limit: int = 100,
) -> int:
    """Догружает свечи [since, until] и возвращает число записанных строк.

    Идемпотентность: точка старта пагинации берётся от самой ранней уже
    загруженной свечи, поэтому повторный запуск докачивает только недостающее
    и не запрашивает историю заново.
    """
    own_client = client is None
    exchange = None
    if own_client:
        from src.core.config import settings

        exchange = create_exchange(settings.EXCHANGE)
        client = OkxHistory(exchange, pause_ms=200)

    inserted = 0
    try:
        earliest = await db.fetchval(
            "SELECT min(open_time) FROM backtest.candles WHERE inst_id=$1 AND bar=$2;",
            inst_id, bar,
        )
        cursor_ms = _ms(earliest) if earliest is not None else _ms(until)
        _log.info("Загрузка свечей", inst_id=inst_id, bar=bar,
                  since=since.isoformat(), until=until.isoformat(),
                  resume_from=None if earliest is None else earliest.isoformat())

        while True:
            page = await client.candles_page(inst_id, bar, cursor_ms, page_limit)
            if not page:
                break
            rows = parse_candles(page, inst_id, bar)
            fresh = [r for r in rows if since <= r[2] <= until]
            inserted += await _insert_candles(fresh)
            oldest_ms = min(int(item[0]) for item in page)
            if oldest_ms >= cursor_ms:   # пагинация не движется — обрываем
                break
            cursor_ms = oldest_ms
            if _dt(oldest_ms) <= since:
                break
    finally:
        if own_client and exchange is not None:
            await exchange.close()
    _log.info("Свечи загружены", inst_id=inst_id, bar=bar, rows=inserted)
    return inserted


async def backfill_funding(
    inst_id: str,
    since: datetime,
    until: datetime,
    *,
    client: OkxHistory | None = None,
    page_limit: int = 100,
) -> int:
    """Догружает историю funding [since, until] и возвращает число строк."""
    own_client = client is None
    exchange = None
    if own_client:
        from src.core.config import settings

        exchange = create_exchange(settings.EXCHANGE)
        client = OkxHistory(exchange, pause_ms=200)

    inserted = 0
    try:
        earliest = await db.fetchval(
            "SELECT min(funding_time) FROM backtest.funding WHERE inst_id=$1;", inst_id
        )
        cursor_ms = _ms(earliest) if earliest is not None else _ms(until)
        while True:
            page = await client.funding_page(inst_id, cursor_ms, page_limit)
            if not page:
                break
            rows = parse_funding(page, inst_id)
            fresh = [r for r in rows if since <= r[1] <= until]
            inserted += await _insert_funding(fresh)
            oldest_ms = min(int(item["fundingTime"]) for item in page)
            if oldest_ms >= cursor_ms:
                break
            cursor_ms = oldest_ms
            if _dt(oldest_ms) <= since:
                break
    finally:
        if own_client and exchange is not None:
            await exchange.close()
    _log.info("Funding загружен", inst_id=inst_id, rows=inserted)
    return inserted
