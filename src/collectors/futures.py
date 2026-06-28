"""Коллектор данных по бессрочному фьючерсу: funding rate и open interest."""

from __future__ import annotations

from typing import Any

from src.collectors.base import BaseCollector
from src.core.db import _ms_to_dt, db


class FuturesCollector(BaseCollector):
    """Опрашивает funding rate и open interest по swap-символу.

    Каждый из двух запросов обёрнут в отдельный try: если биржа/символ
    не поддерживает метод, итерация логирует это и продолжает без падения.
    """

    def __init__(
        self,
        exchange: Any,
        instrument_id: int,
        swap_symbol: str,
        interval: float,
    ) -> None:
        super().__init__(name="futures", interval=interval)
        self.exchange = exchange
        self.instrument_id = instrument_id
        self.swap_symbol = swap_symbol

    async def collect_once(self) -> None:
        """Собирает funding rate и open interest (каждый независимо)."""
        await self._collect_funding()
        await self._collect_open_interest()

    async def _collect_funding(self) -> None:
        """Запрашивает и сохраняет ставку финансирования."""
        try:
            data = await self.exchange.fetch_funding_rate(self.swap_symbol)
        except Exception as exc:
            self._log.warning("funding rate недоступен", error=str(exc))
            return
        rate = data.get("fundingRate")
        if rate is None:
            return
        ts = _ms_to_dt(data.get("timestamp") or data.get("fundingTimestamp"))
        await db.insert_funding(self.instrument_id, ts, rate)
        self._log.debug("Funding rate сохранён", rate=rate)

    async def _collect_open_interest(self) -> None:
        """Запрашивает и сохраняет открытый интерес."""
        try:
            data = await self.exchange.fetch_open_interest(self.swap_symbol)
        except Exception as exc:
            self._log.warning("open interest недоступен", error=str(exc))
            return
        # Разные биржи отдают объём в разных полях.
        value = (
            data.get("openInterestAmount")
            or data.get("openInterestValue")
            or data.get("openInterest")
        )
        if value is None:
            return
        ts = _ms_to_dt(data.get("timestamp"))
        await db.insert_open_interest(self.instrument_id, ts, value)
        self._log.debug("Open interest сохранён", value=value)
