"""Коллектор снимков стакана заявок."""

from __future__ import annotations

from typing import Any

from src.collectors.base import BaseCollector
from src.core.db import db


class OrderBookCollector(BaseCollector):
    """Снимает стакан заданной глубины и пишет его в БД."""

    def __init__(
        self,
        exchange: Any,
        instrument_id: int,
        symbol: str,
        depth: int,
        interval: float,
        name_suffix: str = "",
    ) -> None:
        super().__init__(name="orderbook", interval=interval, name_suffix=name_suffix)
        self.exchange = exchange
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.depth = depth

    async def collect_once(self) -> None:
        """Запрашивает снимок стакана и сохраняет его (spread/объёмы считает БД-слой)."""
        ob = await self.exchange.fetch_order_book(self.symbol, limit=self.depth)
        await db.insert_orderbook(self.instrument_id, ob)
        self._log.debug(
            "Снимок стакана сохранён",
            bids=len(ob.get("bids") or []),
            asks=len(ob.get("asks") or []),
        )
