"""Снимки данных «на момент T»: единственное место, где задаётся видимость.

ЗАПРЕТ ЗАГЛЯДЫВАНИЯ В БУДУЩЕЕ (§9.2 ТЗ) обеспечивается НА УРОВНЕ ЗАПРОСА:
условие ``close_time <= $ts`` / ``funding_time <= $ts`` стоит в самом SQL, а не
в фильтрации после выборки. Свеча, закрывшаяся ровно в T, включается; ещё не
закрывшаяся — не включается никогда.

О FUNDING И ЧАСОВОЙ СЕТКЕ. Продакшн читает funding окном за время и прореживает
до ОДНОЙ точки в час (``db.get_funding_window``: ``DISTINCT ON (час)``, берётся
последнее значение часа), потому что коллектор пишет текущую ставку раз в
минуту. Историческая выгрузка OKX даёт ставки в моменты расчёта (интервал
подтверждается зондом, ожидается 8 часов). Чтобы агент в реплее получал вход
ТОЙ ЖЕ формы, что в продакшне, исторический ряд разворачивается в почасовой
ступенчатой протяжкой последнего известного значения.

Это НЕ подстановка недостающих данных: между расчётами ставка не «отсутствует»,
она постоянна и равна последнему объявленному значению. Протяжка использует
только значения, известные НА момент T, и ни при каких условиях не берёт
будущее. Ограничение, которое из этого следует, зафиксировано в отчёте:
продакшн пишет ТЕКУЩУЮ (прогнозную) ставку, история — РАСЧЁТНУЮ; величины
близки, но не тождественны, и именно это измеряет сверка §13.2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from backtest import db
from backtest.config import BacktestConfig
from src.agents.market import _EMA_SLOW
from src.core.config import settings


@dataclass(frozen=True)
class Snapshot:
    """Данные, видимые системе в момент ``ts``, и ничего сверх того."""

    ts: datetime
    inst_id: str
    candles: pd.DataFrame     # только close_time <= ts, по возрастанию
    funding: pd.DataFrame     # только funding_time <= ts, по возрастанию

    @property
    def price(self) -> float | None:
        """Цена на момент T — close последней закрытой свечи."""
        if self.candles.empty:
            return None
        return float(self.candles["close"].iloc[-1])


def market_window_size() -> int:
    """Сколько свечей запрашивает Market Agent в продакшне.

    Значение вычисляется ровно так же, как в ``MarketAgent.analyze``:
    ``max(min_candles, EMA200) + 50``. Дублировать число нельзя — оно должно
    меняться вместе с продакшном.
    """
    return max(settings.AGENT_MIN_CANDLES, _EMA_SLOW) + 50


async def candles_at(inst_id: str, bar: str, ts: datetime, limit: int) -> pd.DataFrame:
    """Последние ``limit`` ЗАКРЫТЫХ свечей на момент ``ts``, по возрастанию."""
    rows = await db.fetch(
        """
        SELECT open_time, close_time, open, high, low, close, volume
        FROM (
            SELECT open_time, close_time, open, high, low, close, volume
            FROM backtest.candles
            WHERE inst_id = $1 AND bar = $2 AND close_time <= $3
            ORDER BY close_time DESC
            LIMIT $4
        ) sub
        ORDER BY close_time ASC;
        """,
        inst_id, bar, ts, limit,
    )
    if not rows:
        return pd.DataFrame(
            columns=["open_time", "close_time", "open", "high", "low", "close", "volume"]
        )
    frame = pd.DataFrame([dict(r) for r in rows])
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    return frame


async def funding_at(
    inst_id: str,
    ts: datetime,
    lookback_hours: int,
) -> pd.DataFrame:
    """Ставки финансирования, известные на момент ``ts``, за окно ``lookback_hours``.

    Берётся одна дополнительная точка ДО начала окна: без неё первые часы окна
    остались бы без известного значения, хотя на момент T оно существовало.
    """
    window_start = ts - timedelta(hours=lookback_hours)
    rows = await db.fetch(
        """
        (SELECT funding_time, funding_rate
           FROM backtest.funding
          WHERE inst_id = $1 AND funding_time <= $2 AND funding_time >= $3
          ORDER BY funding_time)
        UNION ALL
        (SELECT funding_time, funding_rate
           FROM backtest.funding
          WHERE inst_id = $1 AND funding_time < $3
          ORDER BY funding_time DESC
          LIMIT 1)
        ORDER BY funding_time;
        """,
        inst_id, ts, window_start,
    )
    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate"])
    frame = pd.DataFrame([dict(r) for r in rows])
    frame["funding_rate"] = frame["funding_rate"].astype(float)
    return frame


def to_hourly_funding(
    frame: pd.DataFrame,
    ts: datetime,
    lookback_hours: int,
) -> list[dict[str, float]]:
    """Разворачивает ступенчатый ряд ставок в почасовые точки окна.

    Возвращает список в формате, который принимает ``analyze_futures``
    (``[{"rate": ...}, ...]`` по возрастанию времени). Часы, для которых на
    момент T ещё не было ни одного объявленного значения, ПРОПУСКАЮТСЯ — они
    не заполняются нулём, средним или чем-либо ещё.
    """
    if frame.empty:
        return []
    stamps = list(frame["funding_time"])
    rates = list(frame["funding_rate"])

    window_start = ts - timedelta(hours=lookback_hours)
    # Часовая сетка окна: от первого целого часа после начала окна до T.
    first_hour = window_start.replace(minute=0, second=0, microsecond=0)
    if first_hour < window_start:
        first_hour += timedelta(hours=1)
    hours: list[datetime] = []
    cursor = first_hour
    while cursor <= ts:
        hours.append(cursor)
        cursor += timedelta(hours=1)

    result: list[dict[str, float]] = []
    index = 0
    last_known: float | None = None
    for hour in hours:
        while index < len(stamps) and stamps[index] <= hour:
            last_known = float(rates[index])
            index += 1
        if last_known is not None:
            result.append({"rate": last_known})
    return result


async def build_snapshot(inst_id: str, ts: datetime, cfg: BacktestConfig) -> Snapshot:
    """Собирает снимок на момент ``ts``."""
    candles = await candles_at(inst_id, cfg.bar, ts, market_window_size())
    funding = await funding_at(inst_id, ts, settings.FUTURES_LOOKBACK_HOURS)
    return Snapshot(ts=ts, inst_id=inst_id, candles=candles, funding=funding)


def decision_times(cfg: BacktestConfig) -> list[datetime]:
    """Моменты принятия решений: закрытие каждой свечи с шагом BT_STEP_HOURS."""
    times: list[datetime] = []
    cursor = cfg.period_from.replace(minute=0, second=0, microsecond=0)
    if cursor < cfg.period_from:
        cursor += timedelta(hours=1)
    while cursor <= cfg.period_to:
        times.append(cursor)
        cursor += timedelta(hours=cfg.step_hours)
    return times


async def iter_snapshots(inst_id: str, cfg: BacktestConfig) -> AsyncIterator[Snapshot]:
    """Снимки по всем моментам решения (§11 ТЗ).

    Отличие от сигнатуры §11 ТЗ: возвращается АСИНХРОННЫЙ итератор
    (``AsyncIterator``), потому что данные читаются из БД асинхронным драйвером.
    Обходится как ``async for``. При 24 месяцах и шаге в час это около 17 000
    снимков на инструмент — держать их все в памяти незачем.
    """
    for ts in decision_times(cfg):
        yield await build_snapshot(inst_id, ts, cfg)


def utcnow() -> datetime:
    return datetime.now(UTC)
