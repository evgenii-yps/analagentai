"""Асинхронный слой доступа к PostgreSQL поверх пула asyncpg.

Все запросы параметризованы для защиты от SQL-инъекций.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from src.core.config import settings

if TYPE_CHECKING:
    # Импорт только для аннотаций — без циклической зависимости в рантайме.
    from src.agents.base import AgentOutput


class DB:
    """Обёртка над ``asyncpg.Pool`` с методами доступа к данным."""

    def __init__(self) -> None:
        # Пул создаётся лениво в connect(); до этого он отсутствует.
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        """Возвращает активный пул либо падает, если он не инициализирован."""
        if self._pool is None:
            raise RuntimeError("Пул не инициализирован: сначала вызовите connect().")
        return self._pool

    async def connect(self) -> None:
        """Создаёт пул соединений (min_size=2, max_size=10)."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=2,
            max_size=10,
        )

    async def close(self) -> None:
        """Закрывает пул соединений."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        """Проверяет доступность БД запросом ``SELECT 1``."""
        try:
            result = await self.pool.fetchval("SELECT 1;")
            return result == 1
        except Exception:
            return False

    async def get_or_create_instrument(
        self,
        exchange: str,
        symbol: str,
        type: str = "spot",
        base: str | None = None,
        quote: str | None = None,
    ) -> int:
        """UPSERT инструмента в таблицу ``instruments`` и возврат его ``id``.

        Если ``base``/``quote`` не переданы, они выводятся из ``symbol``
        (поддерживаются разделители ``/`` и ``-``, напр. ``BTC/USDT``).
        """
        if base is None or quote is None:
            derived_base, derived_quote = _split_symbol(symbol)
            base = base or derived_base
            quote = quote or derived_quote

        # ON CONFLICT ... DO UPDATE нужен, чтобы RETURNING вернул id и при конфликте.
        query = """
            INSERT INTO instruments (exchange, symbol, base, quote, type)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (exchange, symbol, type)
            DO UPDATE SET base = EXCLUDED.base, quote = EXCLUDED.quote
            RETURNING id;
        """
        return await self.pool.fetchval(query, exchange, symbol, base, quote, type)

    async def upsert_ohlcv(
        self,
        instrument_id: int,
        timeframe: str,
        candles: list[list[float]],
    ) -> None:
        """Пакетный UPSERT свечей. ``candle = [ts_ms, open, high, low, close, volume]``.

        При конфликте по PK (instrument_id, timeframe, ts) текущая свеча
        обновляется — это делает повторный сбор идемпотентным.
        """
        if not candles:
            return
        query = """
            INSERT INTO ohlcv
                (instrument_id, timeframe, ts, open, high, low, close, volume)
            VALUES
                ($1, $2, to_timestamp($3::double precision / 1000.0), $4, $5, $6, $7, $8)
            ON CONFLICT (instrument_id, timeframe, ts)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume;
        """
        rows = [
            (
                instrument_id,
                timeframe,
                int(c[0]),
                float(c[1]),
                float(c[2]),
                float(c[3]),
                float(c[4]),
                float(c[5]),
            )
            for c in candles
        ]
        await self.pool.executemany(query, rows)

    async def insert_trades(
        self,
        instrument_id: int,
        trades: list[dict[str, Any]],
    ) -> None:
        """Пакетная вставка сделок с дедупликацией по (instrument_id, trade_id)."""
        if not trades:
            return
        # Убираем дубли внутри пакета; кросс-запусковую дедупликацию даёт ON CONFLICT.
        unique = dedupe_trades(trades)
        query = """
            INSERT INTO trades
                (instrument_id, trade_id, ts, price, amount, side)
            VALUES
                ($1, $2, to_timestamp($3::double precision / 1000.0), $4, $5, $6)
            ON CONFLICT (instrument_id, trade_id) DO NOTHING;
        """
        rows = [
            (
                instrument_id,
                str(t["id"]),
                int(t["timestamp"]),
                float(t["price"]),
                float(t["amount"]),
                t.get("side"),
            )
            for t in unique
            if t.get("id") is not None and t.get("timestamp") is not None
        ]
        await self.pool.executemany(query, rows)

    async def insert_orderbook(
        self,
        instrument_id: int,
        ob: dict[str, Any],
    ) -> None:
        """Вставка снимка стакана: bids/asks в JSONB, расчёт spread и объёмов."""
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        spread, bid_volume, ask_volume = compute_orderbook_metrics(bids, asks)
        ts = _ms_to_dt(ob.get("timestamp"))
        query = """
            INSERT INTO orderbook_snapshots
                (instrument_id, ts, bids, asks, spread, bid_volume, ask_volume)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7);
        """
        await self.pool.execute(
            query,
            instrument_id,
            ts,
            json.dumps(bids),
            json.dumps(asks),
            spread,
            bid_volume,
            ask_volume,
        )

    async def insert_funding(
        self,
        instrument_id: int,
        ts: datetime,
        rate: float,
    ) -> None:
        """UPSERT ставки финансирования по PK (instrument_id, ts)."""
        query = """
            INSERT INTO funding (instrument_id, ts, rate)
            VALUES ($1, $2, $3)
            ON CONFLICT (instrument_id, ts) DO UPDATE SET rate = EXCLUDED.rate;
        """
        await self.pool.execute(query, instrument_id, ts, float(rate))

    async def insert_open_interest(
        self,
        instrument_id: int,
        ts: datetime,
        value: float,
    ) -> None:
        """UPSERT открытого интереса по PK (instrument_id, ts)."""
        query = """
            INSERT INTO open_interest (instrument_id, ts, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (instrument_id, ts) DO UPDATE SET value = EXCLUDED.value;
        """
        await self.pool.execute(query, instrument_id, ts, float(value))

    # --- Чтение данных для агентов (Этап 3) ---

    async def get_ohlcv(
        self,
        instrument_id: int,
        timeframe: str,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` свечей по возрастанию ts (старые → новые)."""
        query = """
            SELECT ts, open, high, low, close, volume
            FROM (
                SELECT ts, open, high, low, close, volume
                FROM ohlcv
                WHERE instrument_id = $1 AND timeframe = $2
                ORDER BY ts DESC
                LIMIT $3
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, timeframe, limit)

    async def get_recent_orderbook(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` снимков стакана по возрастанию ts (старые → новые)."""
        query = """
            SELECT ts, bids, asks, spread, bid_volume, ask_volume
            FROM (
                SELECT ts, bids, asks, spread, bid_volume, ask_volume
                FROM orderbook_snapshots
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def get_recent_funding(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` значений funding по возрастанию ts."""
        query = """
            SELECT ts, rate
            FROM (
                SELECT ts, rate
                FROM funding
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def get_recent_open_interest(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` значений open interest по возрастанию ts."""
        query = """
            SELECT ts, value
            FROM (
                SELECT ts, value
                FROM open_interest
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def save_agent_output(self, output: AgentOutput) -> None:
        """INSERT заключения агента в ``agent_outputs``."""
        query = """
            INSERT INTO agent_outputs
                (agent, instrument_id, signal, confidence, metrics, rationale)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6);
        """
        await self.pool.execute(
            query,
            output.agent,
            output.instrument_id,
            output.signal,
            float(output.confidence),
            json.dumps(output.metrics),
            output.rationale,
        )

    # --- Decision Agent (Этап 4) ---

    async def get_latest_agent_output(
        self,
        agent: str,
        instrument_id: int,
    ) -> dict[str, Any] | None:
        """Последний вывод агента по инструменту (ts DESC) или None."""
        query = """
            SELECT agent, instrument_id, ts, signal, confidence, metrics, rationale
            FROM agent_outputs
            WHERE agent = $1 AND instrument_id = $2
            ORDER BY ts DESC
            LIMIT 1;
        """
        row = await self.pool.fetchrow(query, agent, instrument_id)
        return dict(row) if row is not None else None

    async def save_signal(
        self,
        instrument_id: int,
        decision: str,
        probability: float,
        agents_payload: list[dict[str, Any]],
        rationale: str,
    ) -> None:
        """INSERT итогового решения в ``signals`` (status остаётся 'open')."""
        query = """
            INSERT INTO signals
                (instrument_id, decision, probability, agents_payload, rationale)
            VALUES ($1, $2, $3, $4::jsonb, $5);
        """
        await self.pool.execute(
            query,
            instrument_id,
            decision,
            float(probability),
            json.dumps(agents_payload),
            rationale,
        )


def _ms_to_dt(ms: int | None) -> datetime:
    """Преобразует Unix-время в мс (UTC-aware datetime). None → текущее время."""
    if ms is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def compute_orderbook_metrics(
    bids: list[list[float]],
    asks: list[list[float]],
) -> tuple[float | None, float, float]:
    """Считает spread (best_ask - best_bid) и суммарные объёмы по сторонам.

    ``bids``/``asks`` — списки пар ``[price, amount]``. Если одна из сторон
    пуста, spread не определён (None).
    """
    bid_volume = sum(float(level[1]) for level in bids)
    ask_volume = sum(float(level[1]) for level in asks)
    spread: float | None = None
    if bids and asks:
        spread = float(asks[0][0]) - float(bids[0][0])
    return spread, bid_volume, ask_volume


def dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Удаляет дубли сделок по ``id`` внутри пакета, сохраняя порядок."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in trades:
        tid = t.get("id")
        if tid is None:
            continue
        key = str(tid)
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    return unique


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Разбивает символ инструмента на базовый и котируемый активы."""
    for sep in ("/", "-"):
        if sep in symbol:
            base, quote = symbol.split(sep, 1)
            # Для деривативов символ вида BTC/USDT:USDT — отбрасываем settle-суффикс.
            quote = quote.split(":", 1)[0]
            return base, quote
    # Разделитель не найден — считаем весь символ базовым активом.
    return symbol, ""


# Глобальный синглтон слоя доступа к БД.
db = DB()
