"""Оценщик результатов сигналов: дооценка фактом движения цены.

Для каждого сигнала и горизонта (1ч/4ч) считаем, пошла ли цена в предсказанную
сторону (pnl_pct), какова была макс. просадка против сигнала (drawdown_pct) и
успех (pnl_pct > 0). Расчёт детерминирован и вынесен в чистые функции.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.core.db import db
from src.core.redis_client import get_redis

# TTL heartbeat-ключа (секунды).
_HEARTBEAT_TTL = 300

# Допуск неполноты данных окна: последняя свеча должна быть близко к концу окна.
_WINDOW_TOLERANCE_SEC = 120


def horizon_to_seconds(horizon: str) -> int:
    """Переводит горизонт вида ``1h``/``30m``/``90s`` в секунды."""
    horizon = horizon.strip().lower()
    units = {"h": 3600, "m": 60, "s": 1}
    unit = horizon[-1]
    if unit not in units:
        raise ValueError(f"Неизвестный горизонт: {horizon}")
    return int(horizon[:-1]) * units[unit]


def compute_evaluation(
    decision: str,
    price_at_signal: float,
    window_candles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Считает метрики результата → dict(price_at_close, pnl_pct, drawdown_pct, success).

    ``window_candles`` — 1m-свечи окна (после сигнала по конец горизонта),
    по возрастанию ts. pnl_pct положителен, если цена пошла в сторону сигнала.
    drawdown_pct — макс. ход против сигнала внутри окна (всегда ≥ 0).
    """
    if not window_candles:
        raise ValueError("Пустое окно свечей")
    if price_at_signal <= 0:
        raise ValueError("Некорректная цена сигнала")

    close_end = float(window_candles[-1]["close"])

    if decision == "buy":
        pnl_pct = (close_end - price_at_signal) / price_at_signal * 100.0
        min_low = min(float(c["low"]) for c in window_candles)
        drawdown_pct = max(0.0, (price_at_signal - min_low) / price_at_signal * 100.0)
    elif decision == "sell":
        pnl_pct = (price_at_signal - close_end) / price_at_signal * 100.0
        max_high = max(float(c["high"]) for c in window_candles)
        drawdown_pct = max(0.0, (max_high - price_at_signal) / price_at_signal * 100.0)
    else:
        raise ValueError(f"Недопустимое решение для оценки: {decision}")

    return {
        "price_at_close": round(close_end, 8),
        "pnl_pct": round(pnl_pct, 6),
        "drawdown_pct": round(drawdown_pct, 6),
        "success": pnl_pct > 0,
    }


class Evaluator:
    """Сервис оценки: проходит по горизонтам и дооценивает готовые сигналы."""

    def __init__(
        self,
        interval: float,
        horizons: list[str],
        primary_horizon: str,
        stats_log_interval: float,
    ) -> None:
        self.interval = interval
        self.horizons = horizons
        self.primary_horizon = primary_horizon
        self.stats_log_interval = stats_log_interval
        self._last_stats_ts: datetime | None = None
        self._log = structlog.get_logger().bind(component="evaluator")

    async def evaluate_once(self) -> None:
        """Один прогон оценки по всем горизонтам."""
        for horizon in self.horizons:
            horizon_sec = horizon_to_seconds(horizon)
            signals = await db.get_signals_to_evaluate(horizon, horizon_sec)
            for signal in signals:
                await self._evaluate_signal(signal, horizon, horizon_sec)

    async def _evaluate_signal(
        self,
        signal: dict[str, Any],
        horizon: str,
        horizon_sec: int,
    ) -> None:
        """Оценивает один сигнал по одному горизонту (мягко пропускает при нехватке)."""
        instrument_id = signal["instrument_id"]
        signal_ts = signal["ts"]
        decision = signal["decision"]
        end_ts = signal_ts + timedelta(seconds=horizon_sec)

        price_at_signal = await db.get_price_at(instrument_id, signal_ts)
        if price_at_signal is None:
            return  # нет цены на момент сигнала — повторим позже

        window = await db.get_ohlcv_window(instrument_id, signal_ts, end_ts)
        if not window:
            return  # нет свечей окна — повторим позже
        # Данные окна неполны (последняя свеча далеко от конца) — ждём догрузки.
        if window[-1]["ts"] < end_ts - timedelta(seconds=_WINDOW_TOLERANCE_SEC):
            return

        result = compute_evaluation(decision, float(price_at_signal), window)
        await db.save_evaluation(
            signal["id"],
            horizon,
            float(price_at_signal),
            result["price_at_close"],
            result["pnl_pct"],
            result["drawdown_pct"],
            result["success"],
        )
        # Главный горизонт → сводка в signals и закрытие сигнала.
        if horizon == self.primary_horizon:
            await db.finalize_signal(
                signal["id"],
                result["pnl_pct"],
                result["drawdown_pct"],
                result["success"],
            )
        self._log.info(
            "Сигнал оценён",
            signal_id=signal["id"],
            horizon=horizon,
            decision=decision,
            pnl_pct=result["pnl_pct"],
            success=result["success"],
        )

    async def log_stats_if_due(self, now: datetime) -> None:
        """Периодически логирует статистику успеха по decision×horizon."""
        if (
            self._last_stats_ts is not None
            and (now - self._last_stats_ts).total_seconds() < self.stats_log_interval
        ):
            return
        self._last_stats_ts = now
        stats = await db.get_success_stats()
        for row in stats:
            self._log.info(
                "Статистика результатов",
                decision=row["decision"],
                horizon=row["horizon"],
                n=row["n"],
                success_rate=round(float(row["success_rate"]), 4),
                avg_pnl_pct=round(float(row["avg_pnl_pct"]), 4),
            )

    async def run(self) -> None:
        """Бесконечный цикл: оценка → статистика → heartbeat → пауза. Не падает."""
        self._log.info("Оценщик запущен", interval=self.interval, horizons=self.horizons)
        while True:
            try:
                await self.evaluate_once()
                await self.log_stats_if_due(datetime.now(UTC))
                await self._heartbeat()
            except asyncio.CancelledError:
                self._log.info("Оценщик остановлен")
                raise
            except Exception as exc:
                self._log.warning("Ошибка итерации оценки", error=str(exc))
            await asyncio.sleep(self.interval)

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последней успешной итерации."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set("evaluator:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
