"""Прогон агентов и Decision Agent по историческим снимкам.

Здесь НЕТ ни одной собственной формулы. Вызываются ровно те функции, что
работают в продакшне:

    src.agents.market.analyze_ohlcv       — Market Agent
    src.agents.futures.analyze_futures    — Futures Agent
    src.decision.agent.make_decision      — агрегация

и ровно с теми параметрами, что стоят в продакшн-конфигурации
(``src.core.config.settings``). Если бы здесь появилась «адаптированная под
историю» логика, измерялась бы другая система, и весь результат был бы
недействителен.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any

import structlog

from backtest import db
from backtest.clock import build_snapshot, decision_times, to_hourly_funding
from backtest.config import BacktestConfig
from backtest.integrity import is_excluded
from src.agents.futures import analyze_futures
from src.agents.market import analyze_ohlcv
from src.core.config import settings
from src.decision.agent import AGENTS, make_decision

_log = structlog.get_logger().bind(component="backtest.replay")

# Агенты, для которых существует историческая реконструкция входов.
# Liquidity сюда не входит и войти не может: истории стакана не существует
# ни у одной биржи (§3.3 ТЗ), а суррогат вместо неё запрещён.
REPLAYABLE = ("market", "futures")

# Числовое направление — та же таблица, что в src.decision.agent.
_SIGNAL_VALUE = {"bullish": 1, "bearish": -1, "neutral": 0}


def code_commit() -> str:
    """Хэш коммита, на котором выполняется прогон (для backtest.runs.code_commit)."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001 — вне git-дерева прогон всё равно возможен
        return "unknown"


def recompute_score_agreement(
    payload: list[dict[str, Any]],
    total_agents: int,
) -> tuple[float, float]:
    """Пересчитывает балл и согласованность по формуле Decision Agent.

    ``make_decision`` возвращает только решение и индекс согласия, а схема
    §8 ТЗ требует хранить балл и согласованность отдельными колонками. Значения
    пересчитываются здесь той же формулой — и немедленно сверяются с индексом,
    который вернул продакшн-код (см. ``_verify_conviction``). Если формула в
    продакшне изменится, прогон упадёт с явной ошибкой, а не запишет молча
    устаревшие числа.
    """
    numerator = 0.0
    denominator = 0.0
    pos = neg = 0
    for entry in payload:
        weight = settings.agent_weights.get(entry["agent"], 1.0)
        confidence = float(entry["confidence"])
        direction = _SIGNAL_VALUE[entry["signal"]]
        numerator += direction * confidence * weight
        denominator += weight * confidence
        pos += 1 if direction > 0 else 0
        neg += 1 if direction < 0 else 0
    score = numerator / denominator if denominator > 0 else 0.0
    agreement = abs(pos - neg) / total_agents if total_agents > 0 else 0.0
    return score, agreement


def _verify_conviction(score: float, agreement: float, conviction: float) -> None:
    """Сверяет пересчитанный индекс с тем, что вернул продакшн-код."""
    expected = round(min(abs(score) * (0.5 + 0.5 * agreement), 1.0), 4)
    if abs(expected - conviction) > 1e-9:
        raise RuntimeError(
            "Пересчёт балла/согласованности разошёлся с формулой Decision Agent "
            f"({expected} против {conviction}). Реплей остановлен: он обязан "
            "воспроизводить продакшн, а не приближать его."
        )


def score_agreement_for(
    payload: list[dict[str, Any]],
    conviction: float,
    min_agents: int,
) -> tuple[float, float | None]:
    """Балл и согласованность решения — ровно в том виде, в каком их считал продакшн.

    Тонкость, которую поймала защитная сверка выше: при нехватке кворума
    (``len(fresh) < MIN_AGENTS``) Decision Agent выходит РАНЬШЕ вычисления балла
    и возвращает ``wait`` с индексом 0.0 — формула не применяется вовсе. Значит,
    балла и согласованности у такого решения нет: записывать сюда пересчитанные
    значения означало бы приписать системе вычисление, которого она не делала.
    В этом случае возвращается ``(0.0, None)``, и колонка ``agreement``
    остаётся пустой.
    """
    if len(payload) < min_agents:
        return 0.0, None
    score, agreement = recompute_score_agreement(payload, len(AGENTS))
    _verify_conviction(score, agreement, conviction)
    return score, agreement


def agent_outputs_at(
    snapshot: Any,
    agents: tuple[str, ...] | list[str],
) -> list[dict[str, Any] | None]:
    """Выводы агентов на момент снимка — вызовом продакшн-функций.

    Возвращает список в порядке ``src.decision.agent.AGENTS``; агент, которого
    нет в ``agents`` (например Liquidity), представлен ``None`` — ровно так же,
    как в продакшне выглядит агент, который не высказался. Веса при этом не
    перераспределяются: используется штатный механизм агрегации (§3.4 ТЗ).
    """
    outputs: list[dict[str, Any] | None] = []
    for name in AGENTS:
        if name not in agents:
            outputs.append(None)
            continue
        if name == "market":
            signal, confidence, _metrics, _rationale = analyze_ohlcv(
                snapshot.candles, settings.AGENT_MIN_CANDLES
            )
        elif name == "futures":
            funding_window = to_hourly_funding(
                snapshot.funding, snapshot.ts, settings.FUTURES_LOOKBACK_HOURS
            )
            # Открытый интерес: исторического ряда среди разрешённых §4
            # эндпоинтов НЕТ, поэтому окно OI пустое. Это не подстановка
            # данных, а штатная ветка самого агента «окна OI нет»
            # (metrics.oi_enough = false): направление задаёт funding, а
            # подтверждение со стороны OI отсутствует. Следствие — уверенность
            # Futures в реплее систематически равна 0.4 от продакшновой.
            signal, confidence, _metrics, _rationale = analyze_futures(
                funding_window,
                [],
                snapshot.price,
                pct_high=settings.FUTURES_PCT_HIGH,
                pct_low=settings.FUTURES_PCT_LOW,
                min_points=settings.FUTURES_MIN_POINTS,
                lookback_hours=settings.FUTURES_LOOKBACK_HOURS,
            )
        else:  # pragma: no cover — защита от расширения списка агентов
            outputs.append(None)
            continue
        outputs.append(
            {
                "agent": name,
                "signal": signal,
                "confidence": float(confidence),
                "ts": snapshot.ts,
                "instrument_id": 0,
            }
        )
    return outputs


async def start_run(
    cfg: BacktestConfig,
    agents: list[str],
    criterion: dict[str, Any],
) -> int:
    """Открывает прогон и ФИКСИРУЕТ критерий успеха ДО появления результатов.

    Предрегистрация (§6 ТЗ) держится на том, что эта запись делается раньше
    первой строки в ``backtest.decisions``: временные метки ``runs.started_at``
    и данные прогона это подтверждают.
    """
    config_json = {
        "config": cfg.as_dict(),
        "criterion": criterion,
        "agents": list(agents),
        "production_settings": {
            "AGENT_TIMEFRAME": settings.AGENT_TIMEFRAME,
            "AGENT_MIN_CANDLES": settings.AGENT_MIN_CANDLES,
            "DECISION_THRESHOLD": settings.DECISION_THRESHOLD,
            "MIN_AGENTS": settings.MIN_AGENTS,
            "AGENT_FRESHNESS_SEC": settings.AGENT_FRESHNESS_SEC,
            "WEIGHTS": settings.agent_weights,
            "FUTURES_LOOKBACK_HOURS": settings.FUTURES_LOOKBACK_HOURS,
            "FUTURES_PCT_HIGH": settings.FUTURES_PCT_HIGH,
            "FUTURES_PCT_LOW": settings.FUTURES_PCT_LOW,
            "FUTURES_MIN_POINTS": settings.FUTURES_MIN_POINTS,
            "LOGIC_VERSION": settings.LOGIC_VERSION,
        },
        "limitations": {
            "liquidity": "не прогоняется: истории стакана не существует (§3.3 ТЗ)",
            "open_interest": (
                "исторического ряда среди разрешённых эндпоинтов нет; окно OI "
                "пустое, подтверждения OI в реплее не бывает"
            ),
            "funding_source": (
                "история даёт РАСЧЁТНУЮ ставку, продакшн пишет ТЕКУЩУЮ; "
                "величины близки, но не тождественны"
            ),
        },
    }
    run_id = await db.fetchval(
        """
        INSERT INTO backtest.runs
            (code_commit, agents_used, config_json, period_from, period_to, status)
        VALUES ($1, $2, $3::jsonb, $4, $5, 'running')
        RETURNING run_id;
        """,
        code_commit(),
        list(agents),
        json.dumps(config_json, ensure_ascii=False),
        cfg.period_from,
        cfg.period_to,
    )
    _log.info("Прогон открыт, критерий зафиксирован", run_id=int(run_id), agents=agents)
    return int(run_id)


async def finish_run(run_id: int, status: str) -> None:
    """Закрывает прогон отметкой времени и статусом."""
    await db.execute(
        "UPDATE backtest.runs SET finished_at = now(), status = $2 WHERE run_id = $1;",
        run_id, status,
    )
    _log.info("Прогон закрыт", run_id=run_id, status=status)


async def replay_instrument(
    run_id: int,
    inst_id: str,
    cfg: BacktestConfig,
    agents: list[str],
    *,
    excluded: list[tuple[datetime, datetime]] | None = None,
) -> int:
    """Прогоняет один инструмент и пишет решения. Возвращает число решений.

    Идемпотентность: строки пишутся с ``ON CONFLICT DO NOTHING`` по ключу
    (run_id, inst_id, ts), поэтому повторный прогон того же run_id не задваивает
    данные.
    """
    excluded = excluded or []
    total = 0
    skipped_gap = 0
    skipped_no_data = 0
    batch: list[tuple[Any, ...]] = []

    for ts in decision_times(cfg):
        if is_excluded(ts, excluded):
            skipped_gap += 1
            continue
        snapshot = await build_snapshot(inst_id, ts, cfg)
        price = snapshot.price
        if price is None:
            skipped_no_data += 1
            continue

        outputs = agent_outputs_at(snapshot, tuple(agents))
        decision, conviction, payload, _rationale = make_decision(
            outputs,
            weights=settings.agent_weights,
            threshold=settings.DECISION_THRESHOLD,
            min_agents=settings.MIN_AGENTS,
            freshness_sec=settings.AGENT_FRESHNESS_SEC,
            now=ts,
            total_agents=len(AGENTS),
        )
        score, agreement = score_agreement_for(payload, conviction, settings.MIN_AGENTS)

        batch.append(
            (
                run_id, inst_id, ts, decision,
                round(score, 8), round(conviction, 8),
                None if agreement is None else round(agreement, 8),
                json.dumps(payload, ensure_ascii=False), price,
            )
        )
        total += 1
        if len(batch) >= 500:
            await _flush(batch)
            batch = []

    if batch:
        await _flush(batch)

    _log.info(
        "Инструмент прогнан",
        run_id=run_id, inst_id=inst_id, decisions=total,
        skipped_gap=skipped_gap, skipped_no_data=skipped_no_data,
    )
    return total


async def _flush(batch: list[tuple[Any, ...]]) -> None:
    await db.pool().executemany(
        """
        INSERT INTO backtest.decisions
            (run_id, inst_id, ts, direction, score, probability, agreement,
             agents_payload, price_at_ts)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
        ON CONFLICT (run_id, inst_id, ts) DO NOTHING;
        """,
        batch,
    )
