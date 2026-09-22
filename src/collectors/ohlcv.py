"""Коллектор свечей OHLCV по нескольким таймфреймам."""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt

from src.collectors.base import BaseCollector
from src.core.db import db

# Сколько свечей запрашивать за раз. ccxt сам приводит значение к максимуму
# конкретной биржи (например, у OKX — min(limit, 300)), так что число здесь не
# нужно подбирать под биржу отдельно.
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
        name_suffix: str = "",
    ) -> None:
        super().__init__(name="ohlcv", interval=interval, name_suffix=name_suffix)
        self.exchange = exchange
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.timeframes = timeframes
        self._log = self._log.bind(exchange=exchange.id)

    async def collect_once(self) -> None:
        """Запрашивает свечи по каждому таймфрейму и сохраняет их.

        Таймфрейм обёрнут в СВОЙ try: набор таймфреймов у бирж различается
        (см. ``.env.example``), и если бы один неподдерживаемый таймфрейм ронял
        всю итерацию, ни один из остальных, идущих за ним в списке, не собрался
        бы НИКОГДА — ошибка при разборе списка повторялась бы на одном и том же
        месте до бесконечности. Отсутствие данных по такому таймфрейму —
        законный результат (graceful degradation), а не сбой коллектора.
        """
        for tf in self.timeframes:
            try:
                candles = await self.exchange.fetch_ohlcv(self.symbol, tf, limit=_LIMIT)
            except ccxt.NotSupported:
                self._log.warning(
                    "Таймфрейм не поддерживается биржей", symbol=self.symbol, timeframe=tf
                )
                continue
            await db.upsert_ohlcv(self.instrument_id, tf, candles)
            self._log.debug("Свечи сохранены", timeframe=tf, count=len(candles))
