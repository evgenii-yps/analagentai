"""Коллектор свечей OHLCV по нескольким таймфреймам."""

from __future__ import annotations

from typing import Any

from src.collectors.base import BaseCollector
from src.core.db import db

# Сколько свечей запрашивать за раз.
_LIMIT = 200


class OHLCVCollector(BaseCollector):
    """Опрашивает свечи по каждому таймфрейму и пишет их в БД (UPSERT)."""

    def __init__(
        self,
        exchange: Any,
        instrument_id: int,
        symbol: str,
        timeframes: list[str],
        interval: float,
    ) -> None:
        super().__init__(name="ohlcv", interval=interval)
        self.exchange = exchange
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.timeframes = timeframes

    async def collect_once(self) -> None:
        """Запрашивает свечи по каждому таймфрейму и сохраняет их."""
        for tf in self.timeframes:
            candles = await self.exchange.fetch_ohlcv(self.symbol, tf, limit=_LIMIT)
            await db.upsert_ohlcv(self.instrument_id, tf, candles)
            self._log.debug("Свечи сохранены", timeframe=tf, count=len(candles))
