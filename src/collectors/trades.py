"""Коллектор сделок (тиков)."""

from __future__ import annotations

from typing import Any

from src.collectors.base import BaseCollector
from src.core.db import db

# Сколько сделок запрашивать за раз.
_LIMIT = 1000


class TradesCollector(BaseCollector):
    """Опрашивает последние сделки и пишет их в БД с дедупликацией."""

    def __init__(
        self,
        exchange: Any,
        instrument_id: int,
        symbol: str,
        interval: float,
    ) -> None:
        super().__init__(name="trades", interval=interval)
        self.exchange = exchange
        self.instrument_id = instrument_id
        self.symbol = symbol

    async def collect_once(self) -> None:
        """Запрашивает сделки и сохраняет их (дедуп по (instrument_id, trade_id))."""
        trades = await self.exchange.fetch_trades(self.symbol, limit=_LIMIT)
        await db.insert_trades(self.instrument_id, trades)
        self._log.debug("Сделки сохранены", count=len(trades))
