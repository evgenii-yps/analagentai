"""Сверка реплея с живой системой — блокирующая проверка §13.2 ТЗ.

Смысл проверки: если на тех же моментах времени реплей выдаёт не то, что
продакшн записал в реальном времени, значит реплей воспроизводит ДРУГУЮ
систему, и никакие его результаты публиковать нельзя.

Проверка сделана не только тестом, но и ШАГОМ КОНВЕЙЕРА: pytest в среде
разработки не имеет доступа к продакшн-БД, а на сервере доступ есть. Поэтому
``python -m backtest.run`` выполняет сверку перед прогоном и отказывается
строить отчёт, если она не пройдена.

Что сравнивается:
  * Market — направление обязано совпадать точно, уверенность до 1e-6;
  * Futures — то же, но результат НЕ блокирует прогон целиком: его входы в
    реплее заведомо беднее (нет истории OI, история даёт расчётную ставку
    вместо текущей). Расхождение по Futures означает, что конфигурация B
    (Market + Futures) недостоверна и её результаты не публикуются;
  * Liquidity — из сравнения исключается: истории стакана не существует;
  * итоговое решение Decision Agent не сравнивается вовсе, потому что в
    продакшне в нём участвовал Liquidity. Это отмечается в отчёте явно.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from backtest import db
from backtest.clock import build_snapshot, to_hourly_funding
from backtest.config import BacktestConfig
from src.agents.futures import analyze_futures
from src.agents.market import analyze_ohlcv
from src.core.config import settings

_log = structlog.get_logger().bind(component="backtest.parity")

# Живое окно версии 4 началось 16.08.2026 16:25 UTC; фрагмент 16:11–16:17 UTC
# исключён из сравнения по условию §13.2 ТЗ.
LIVE_WINDOW_FROM = datetime(2026, 8, 16, 16, 25, tzinfo=UTC)
EXCLUDED_FROM = datetime(2026, 8, 16, 16, 11, tzinfo=UTC)
EXCLUDED_TO = datetime(2026, 8, 16, 16, 17, tzinfo=UTC)

CONFIDENCE_TOLERANCE = 1e-6


@dataclass
class AgentParity:
    """Результат сверки одного агента."""

    agent: str
    compared: int = 0
    direction_match: int = 0
    confidence_match: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.compared > 0 and self.direction_match == self.compared \
            and self.confidence_match == self.compared

    def summary(self) -> str:
        if self.compared == 0:
            return f"{self.agent}: сравнивать нечего (нет пересечения по времени)"
        return (
            f"{self.agent}: направление {self.direction_match}/{self.compared}, "
            f"уверенность (±{CONFIDENCE_TOLERANCE:g}) "
            f"{self.confidence_match}/{self.compared}"
        )


@dataclass
class ParityResult:
    """Итог сверки: что сравнили, что совпало, что это означает."""

    agents: dict[str, AgentParity]
    moments: int
    note: str = ""

    @property
    def market_ok(self) -> bool:
        market = self.agents.get("market")
        return bool(market and market.ok)

    @property
    def futures_ok(self) -> bool:
        futures = self.agents.get("futures")
        return bool(futures and futures.ok)

    @property
    def blocking_ok(self) -> bool:
        """Блокирующий критерий: Market обязан совпасть точно."""
        return self.market_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "moments": self.moments,
            "note": self.note,
            "blocking_ok": self.blocking_ok,
            "agents": {
                name: {
                    "compared": item.compared,
                    "direction_match": item.direction_match,
                    "confidence_match": item.confidence_match,
                    "ok": item.ok,
                    "mismatch_examples": item.mismatches[:5],
                }
                for name, item in self.agents.items()
            },
        }


async def production_moments(inst_id_hint: str, limit: int = 200) -> list[dict[str, Any]]:
    """Записи живого окна версии 4 с мнениями агентов.

    Читает ТОЛЬКО на чтение продакшн-таблицу сигналов. Момент времени берётся
    из ``signals.ts``, мнения — из ``agents_payload`` (та же структура, что и в
    ``backtest.decisions.agents_payload``, — это и делает сверку возможной).
    """
    rows = await db.fetch(
        """
        SELECT ts, agents_payload::text AS payload
        FROM public.signals
        WHERE logic_version = 4
          AND ts >= $1
          AND NOT (ts >= $2 AND ts <= $3)
          AND agents_payload IS NOT NULL
        ORDER BY ts
        LIMIT $4;
        """,
        LIVE_WINDOW_FROM, EXCLUDED_FROM, EXCLUDED_TO, limit,
    )
    moments: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, list):
            moments.append({"ts": row["ts"], "payload": payload})
    return moments


def _replay_agent_values(
    snapshot: Any,
) -> dict[str, tuple[str, float]]:
    """Выводы Market и Futures на снимке — вызовом продакшн-функций."""
    values: dict[str, tuple[str, float]] = {}
    market_signal, market_conf, _m, _r = analyze_ohlcv(
        snapshot.candles, settings.AGENT_MIN_CANDLES
    )
    values["market"] = (market_signal, float(market_conf))

    funding_window = to_hourly_funding(
        snapshot.funding, snapshot.ts, settings.FUTURES_LOOKBACK_HOURS
    )
    futures_signal, futures_conf, _m2, _r2 = analyze_futures(
        funding_window,
        [],
        snapshot.price,
        pct_high=settings.FUTURES_PCT_HIGH,
        pct_low=settings.FUTURES_PCT_LOW,
        min_points=settings.FUTURES_MIN_POINTS,
        lookback_hours=settings.FUTURES_LOOKBACK_HOURS,
    )
    values["futures"] = (futures_signal, float(futures_conf))
    return values


async def check_parity(
    inst_id: str,
    cfg: BacktestConfig,
    limit: int = 200,
) -> ParityResult:
    """Выполняет сверку и возвращает её результат (ничего не пишет в БД)."""
    moments = await production_moments(inst_id, limit)
    agents = {name: AgentParity(agent=name) for name in ("market", "futures")}

    for moment in moments:
        ts = moment["ts"]
        snapshot = await build_snapshot(inst_id, ts, cfg)
        if snapshot.candles.empty:
            continue  # история на этот момент не загружена — сравнивать нечего
        replayed = _replay_agent_values(snapshot)

        for entry in moment["payload"]:
            name = entry.get("agent")
            if name not in agents:
                continue  # liquidity исключён по §13.2
            live_signal = entry.get("signal")
            live_conf = float(entry.get("confidence", 0.0))
            replay_signal, replay_conf = replayed[name]

            item = agents[name]
            item.compared += 1
            if live_signal == replay_signal:
                item.direction_match += 1
            if abs(live_conf - replay_conf) <= CONFIDENCE_TOLERANCE:
                item.confidence_match += 1
            if live_signal != replay_signal or abs(live_conf - replay_conf) > CONFIDENCE_TOLERANCE:
                item.mismatches.append(
                    {
                        "ts": ts.isoformat(),
                        "live": {"signal": live_signal, "confidence": live_conf},
                        "replay": {"signal": replay_signal, "confidence": replay_conf},
                    }
                )

    note = (
        "Liquidity исключён из сравнения (истории стакана нет), поэтому итоговое "
        "решение Decision Agent не сравнивается: в продакшне в нём участвовал "
        "Liquidity."
    )
    result = ParityResult(agents=agents, moments=len(moments), note=note)
    for item in agents.values():
        _log.info("Сверка с продакшном", summary=item.summary())
    return result
