"""Расчёт исходов, издержек, режимов рынка и разметки выборки (§9.3–§9.6 ТЗ).

Все величины считаются от УЖЕ записанных решений и свечей; ничего не
досчитывается «по памяти» и не подставляется. Решения ``wait`` строк исхода не
получают вовсе: направления у них нет, а значит нет и попадания направления.
Их количество приводится в отчёте отдельной строкой.

ГРАНИЦА ПЕРИОДА ЖЁСТКАЯ. В ``backtest.candles`` есть свечи ПОЗЖЕ
``BT_PERIOD_TO``: ряд пары, на которой идёт сверка §13.2, догружается до живого
окна, иначе сверку не с чем выполнять. Расчёт исходов их не видит вовсе —
верхняя граница выборки стоит в SQL и равна ``BT_PERIOD_TO``, а наблюдение,
горизонт которого выходит за границу, ИСКЛЮЧАЕТСЯ. Досчитывать его свежими
данными нельзя по двум причинам: конец периода отодвинут назад намеренно
(§5.3 ТЗ), чтобы результат нельзя было подогнать под уже известное поведение
живой системы, и потому что иначе число исходов зависело бы от дня запуска.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import structlog

from backtest import db
from backtest.config import BacktestConfig

_log = structlog.get_logger().bind(component="backtest.evaluate")

# Границы режима рынка по доходности за предшествующие 30 суток (§9.4 ТЗ).
REGIME_UP_PCT = 10.0
REGIME_DOWN_PCT = -10.0
REGIME_LOOKBACK_DAYS = 30
VOL_LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class Costs:
    """Издержки сделки в процентах (§9.3 ТЗ)."""

    fee_roundtrip_pct: Decimal
    slippage_pct: Decimal

    @property
    def total(self) -> float:
        return float(self.fee_roundtrip_pct) + float(self.slippage_pct)


def gross_pnl_pct(direction: str, price_start: float, price_end: float) -> float:
    """Доходность до издержек, со знаком по направлению.

    Для ``sell`` знак инвертируется: падение цены — это попадание.
    """
    if price_start <= 0:
        raise ValueError("цена на момент решения должна быть положительной")
    raw = (price_end - price_start) / price_start * 100.0
    return raw if direction == "buy" else -raw


def net_pnl_pct(gross: float, costs: Costs) -> float:
    """Доходность после комиссии и проскальзывания.

    Издержки вычитаются ВСЕГДА и не зависят от знака результата: заход и выход
    оплачиваются независимо от того, куда пошла цена.
    """
    return gross - costs.total


def is_independent(hour: int, horizon_h: int) -> bool:
    """Непересекающиеся окна: наблюдение независимо, если час кратен горизонту."""
    return hour % horizon_h == 0


def regime_of(return_30d_pct: float | None) -> str:
    """Режим рынка по доходности за предшествующие 30 суток."""
    if return_30d_pct is None:
        return "flat"
    if return_30d_pct > REGIME_UP_PCT:
        return "up"
    if return_30d_pct < REGIME_DOWN_PCT:
        return "down"
    return "flat"


def quartile_of(value: float, edges: tuple[float, float, float]) -> int:
    """Номер квартиля (1..4) по трём границам."""
    q1, q2, q3 = edges
    if value <= q1:
        return 1
    if value <= q2:
        return 2
    if value <= q3:
        return 3
    return 4


def realized_vol(closes: list[float]) -> float:
    """Реализованная волатильность: стандартное отклонение часовых лог-доходностей."""
    if len(closes) < 3:
        return 0.0
    returns = [
        math.log(closes[i + 1] / closes[i])
        for i in range(len(closes) - 1)
        if closes[i] > 0 and closes[i + 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance)


async def _price_at_close(
    inst_id: str, bar: str, close_time, not_after
) -> float | None:
    """Цена закрытия конкретной свечи, но НЕ ПОЗЖЕ ``not_after``.

    Верхняя граница стоит в самом SQL, а не в проверке вызывающего кода: свечи
    позже BT_PERIOD_TO в таблице ЕСТЬ (ряд пары сверки догружается до живого
    окна, §13.2), и запрос без границы молча брал бы их. Тогда исход решения у
    конца периода считался бы по данным из-за границы — то есть исчезла бы
    защита §5.3, ради которой конец периода отодвинут назад.
    """
    if close_time > not_after:
        return None
    value = await db.fetchval(
        "SELECT close FROM backtest.candles "
        "WHERE inst_id=$1 AND bar=$2 AND close_time=$3 AND close_time <= $4;",
        inst_id, bar, close_time, not_after,
    )
    return None if value is None else float(value)


async def _context_series(inst_id: str, bar: str, cfg: BacktestConfig) -> dict:
    """Готовит вспомогательные ряды: цены по времени закрытия для контекста.

    Верхняя граница выборки — РОВНО ``BT_PERIOD_TO``. Ни одна свеча позже
    границы в расчёт исходов не попадает: ни как цена конца горизонта, ни как
    контекст режима рынка или волатильности. Нижняя граница уходит на 32 суток
    назад — режим рынка считается по доходности за предшествующие 30 суток
    (§9.4), и это данные ДО решения, а не после него.
    """
    rows = await db.fetch(
        "SELECT close_time, close FROM backtest.candles "
        "WHERE inst_id=$1 AND bar=$2 AND close_time BETWEEN $3 AND $4 "
        "ORDER BY close_time;",
        inst_id, bar,
        cfg.period_from - timedelta(days=REGIME_LOOKBACK_DAYS + 2),
        cfg.period_to,
    )
    return {r["close_time"]: float(r["close"]) for r in rows}


async def evaluate_run(run_id: int, cfg: BacktestConfig) -> int:
    """Считает исходы по всем решениям прогона. Возвращает число строк исходов."""
    costs = Costs(cfg.fee_roundtrip_pct, cfg.slippage_pct)
    written = 0

    for pair in cfg.instruments:
        # Исходы считаются по свечам, а свечи есть только у спота: ключ
        # инструмента в таблицах прогона — спот пары.
        inst_id = pair.key
        prices = await _context_series(inst_id, cfg.bar, cfg)
        decisions = await db.fetch(
            "SELECT ts, direction, price_at_ts FROM backtest.decisions "
            "WHERE run_id=$1 AND inst_id=$2 AND direction <> 'wait' ORDER BY ts;",
            run_id, inst_id,
        )
        if not decisions:
            _log.info("Нет направленных решений", run_id=run_id, inst_id=inst_id)
            continue

        # Квартили волатильности считаются по ВСЕМУ периоду прогона этого
        # инструмента (§9.4): границы обязаны быть общими, иначе квартиль
        # перестаёт быть сопоставимым между наблюдениями.
        vols: dict = {
            row["ts"]: realized_vol(
                sorted_values(prices, row["ts"], VOL_LOOKBACK_DAYS)
            )
            for row in decisions
        }
        edges = _quartile_edges(list(vols.values()))

        batch = []
        skipped_after_period = 0
        for row in decisions:
            ts = row["ts"]
            direction = row["direction"]
            price_start = float(row["price_at_ts"])

            past = prices.get(ts - timedelta(days=REGIME_LOOKBACK_DAYS))
            return_30d = (
                None if past is None or past <= 0
                else (price_start - past) / past * 100.0
            )
            regime = regime_of(return_30d)
            vol_quartile = quartile_of(vols[ts], edges)
            oos = ts >= cfg.oos_from

            for horizon in cfg.horizons:
                horizon_end = ts + timedelta(hours=horizon)
                if horizon_end > cfg.period_to:
                    # Горизонт выходит ЗА ГРАНИЦУ ПЕРИОДА. Наблюдение
                    # ИСКЛЮЧАЕТСЯ, а не досчитывается свежими свечами, хотя они
                    # в таблице есть (ряд пары сверки загружен до живого окна).
                    # Конец периода отодвинут назад намеренно (§5.3 ТЗ): результат
                    # не должен опираться на уже известное поведение живой
                    # системы. Кроме того, это делает прогон воспроизводимым:
                    # число исходов не зависит от того, в какой день его запустили.
                    skipped_after_period += 1
                    continue
                price_end = prices.get(horizon_end)
                if price_end is None:
                    price_end = await _price_at_close(
                        inst_id, cfg.bar, horizon_end, cfg.period_to
                    )
                if price_end is None:
                    continue  # горизонт выходит за пределы истории — исхода нет
                gross = gross_pnl_pct(direction, price_start, price_end)
                net = net_pnl_pct(gross, costs)
                batch.append(
                    (
                        run_id, inst_id, ts, horizon, price_end,
                        round(gross, 6), round(net, 6), gross > 0,
                        is_independent(ts.hour, horizon), oos, regime, vol_quartile,
                    )
                )

        if batch:
            await db.pool().executemany(
                """
                INSERT INTO backtest.outcomes
                    (run_id, inst_id, ts, horizon_h, price_end, gross_pnl_pct,
                     net_pnl_pct, direction_hit, is_independent, is_oos,
                     regime, vol_quartile)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (run_id, inst_id, ts, horizon_h) DO NOTHING;
                """,
                batch,
            )
            written += len(batch)
        _log.info(
            "Исходы посчитаны", run_id=run_id, inst_id=inst_id, rows=len(batch),
            # Сколько наблюдений отброшено границей периода — величина должна
            # быть видна, а не подразумеваться.
            skipped_horizon_after_period_to=skipped_after_period,
        )

    return written


def sorted_values(prices: dict, ts, days: int) -> list[float]:
    """Цены закрытия за ``days`` суток до ``ts`` включительно, по возрастанию времени."""
    start = ts - timedelta(days=days)
    return [prices[t] for t in sorted(prices) if start <= t <= ts]


def _quartile_edges(values: list[float]) -> tuple[float, float, float]:
    """Границы квартилей по всей выборке значений."""
    if not values:
        return (0.0, 0.0, 0.0)
    ordered = sorted(values)
    return (
        _percentile(ordered, 0.25),
        _percentile(ordered, 0.50),
        _percentile(ordered, 0.75),
    )


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] * (high - position) + ordered[high] * (position - low)
