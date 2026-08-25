#!/usr/bin/env python3
"""Слепок решений системы на фиксированном наборе входов (§1 и §8 ТЗ 8.7).

ЗАЧЕМ. Жёсткая граница Этапа 8.7: ни одна задача не меняет решение системы. Это
утверждение проверяется не обещанием, а замером — один и тот же набор входов
прогоняется через штатный код ДО и ПОСЛЕ правок, и три величины обязаны совпасть
побайтно: ``decision``, ``probability`` (индекс согласия) и
``calibrated_probability``.

ПОЧЕМУ ИМЕННО ТАК. Скрипт вызывает продакшн-функции, а не их копии:

* ``src.decision.agent.make_decision``      — решение и индекс согласия;
* ``src.calibration.curve.probability_for_index`` — калиброванная вероятность.

Пороги, веса и состав агентов заданы В САМОМ СКРИПТЕ литералами, а не берутся из
``.env``: сравниваться должен КОД, а не окружение двух запусков. Момент времени
тоже фиксирован — иначе проверка свежести выводов сделала бы результат
зависящим от часов.

ЗАПУСК (одинаково на обеих ревизиях):

    python3 scripts/decision_parity_8_7.py > before.json
    git checkout <ветка> && python3 scripts/decision_parity_8_7.py > after.json
    diff before.json after.json && echo "решения совпали"

Скрипт НИЧЕГО не читает из БД и ничего никуда не пишет: он чистый.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta

# Пароль нужен только для импорта конфигурации; к БД скрипт не обращается.
os.environ.setdefault("POSTGRES_PASSWORD", "parity")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.calibration.curve import probability_for_index  # noqa: E402
from src.decision.agent import make_decision  # noqa: E402

# --- Фиксированные условия прогона ----------------------------------------
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
WEIGHTS = {"market": 1.0, "liquidity": 1.0, "futures": 1.0}
THRESHOLD = 0.3
MIN_AGENTS = 2
FRESHNESS_SEC = 300.0
TOTAL_AGENTS = 3

# Калибровочная кривая с известными корзинами: калиброванная вероятность обязана
# получаться из индекса согласия детерминированно.
CURVE_BINS = [
    {"lo": 0.0, "hi": 0.2, "n": 120, "p": 0.41},
    {"lo": 0.2, "hi": 0.4, "n": 110, "p": 0.48},
    {"lo": 0.4, "hi": 0.6, "n": 95, "p": 0.55},
    {"lo": 0.6, "hi": 0.8, "n": 70, "p": 0.63},
    {"lo": 0.8, "hi": 1.0, "n": 40, "p": 0.71},
]

SIGNALS = ["bullish", "bearish", "neutral", "insufficient_data"]
CONFIDENCES = [0.0, 0.25, 0.5, 0.75, 1.0]
# Возрасты выводов: свежий, на границе свежести и заведомо устаревший.
AGES_SEC = [0, 299, 600]


def _output(agent: str, signal: str, confidence: float, age_sec: int) -> dict:
    return {
        "agent": agent,
        "signal": signal,
        "confidence": confidence,
        "ts": NOW - timedelta(seconds=age_sec),
    }


def cases() -> list[dict]:
    """Детерминированный набор входов.

    Полный перебор трёх агентов по всем сигналам и уверенностям дал бы 8000
    случаев — избыточно и нечитаемо. Берётся сетка, покрывающая все ветки
    ``make_decision``: buy/sell/wait по порогу, нехватка свежих выводов,
    ``insufficient_data``, устаревание, нулевой знаменатель, отсутствие агента.
    """
    rows: list[dict] = []
    idx = 0
    for market_signal in SIGNALS:
        for liquidity_signal in SIGNALS:
            for futures_signal in SIGNALS:
                for confidence in CONFIDENCES:
                    age = AGES_SEC[idx % len(AGES_SEC)]
                    idx += 1
                    outputs = [
                        _output("market", market_signal, confidence, 0),
                        _output("liquidity", liquidity_signal, confidence, age),
                        _output("futures", futures_signal, round(1.0 - confidence, 4), 0),
                    ]
                    rows.append({"id": idx, "outputs": outputs})
    # Отдельно — отсутствующие агенты: None в списке выводов штатен.
    for missing in range(3):
        idx += 1
        outputs: list[dict | None] = [
            _output("market", "bullish", 0.8, 0),
            _output("liquidity", "bullish", 0.6, 0),
            _output("futures", "bearish", 0.4, 0),
        ]
        outputs[missing] = None
        rows.append({"id": idx, "outputs": outputs})
    return rows


def snapshot() -> dict:
    results = []
    for case in cases():
        decision, conviction, payload, _rationale = make_decision(
            case["outputs"],
            weights=WEIGHTS,
            threshold=THRESHOLD,
            min_agents=MIN_AGENTS,
            freshness_sec=FRESHNESS_SEC,
            now=NOW,
            total_agents=TOTAL_AGENTS,
        )
        results.append({
            "id": case["id"],
            "inputs": [
                None if o is None else
                {"agent": o["agent"], "signal": o["signal"],
                 "confidence": o["confidence"], "ts": o["ts"].isoformat()}
                for o in case["outputs"]
            ],
            # Три величины §1 ТЗ. Индекс согласия хранится в колонке probability.
            "decision": decision,
            "probability": conviction,
            "calibrated_probability": probability_for_index(CURVE_BINS, conviction),
            "n_agents_in_payload": len(payload),
        })

    body = json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "cases": len(results),
        "digest_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "results": results,
    }


def main() -> None:
    data = snapshot()
    json.dump(data, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    print(
        f"# случаев: {data['cases']}, отпечаток: {data['digest_sha256']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
