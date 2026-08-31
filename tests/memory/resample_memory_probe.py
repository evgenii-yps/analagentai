#!/usr/bin/env python3
"""Замер памяти части А на выборке боевого объёма. Запускается ПОДПРОЦЕССОМ.

ПОЧЕМУ ОТДЕЛЬНЫМ ПРОЦЕССОМ. ``ru_maxrss`` — это высшая отметка за всю жизнь
процесса, и она не опускается. Измеряя её внутри общего прогона проверок, мы
получили бы пик, оставленный чужой проверкой, а не своей: число было бы
правдоподобным и ничего не значащим.

ПОЧЕМУ БЕЗ БАЗЫ. Проверяется потребление РАСЧЁТА, а не сети: строки выдаёт
двойник пула, порождающий их на лету и не хранящий ни одной лишней. Если бы он
держал полтора миллиона строк списком, замер мерил бы двойник.

Запуск:
    python tests/memory/resample_memory_probe.py <строк> [--load-everything]

``--load-everything`` возвращает ПРЕЖНЕЕ поведение — чтение всей выборки одним
запросом — и нужен для контрольного опыта: без него зелёный замер неотличим от
отсутствия замера.

Печатает одну строку JSON: {"rows":…, "pairs":…, "peak_rss_mb":…}.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))
os.environ.setdefault("POSTGRES_PASSWORD", "memory-probe")

from src.trailing.rule import VARIANTS  # noqa: E402

BOUNDARY = datetime(2026, 8, 29, tzinfo=UTC)
TOKENS = ("BTC", "DOGE", "ETH", "SOL", "XRP")
HORIZONS = (1, 4, 12, 24)


def make_row(pair_index: int, variant_index: int) -> dict[str, Any]:
    """Одна строка ``trailing_outcomes``, порождённая на лету.

    Форма та же, что отдаёт запрос части А. Числа неслучайны и повторяемы:
    замеряется память, а не статистика.
    """
    activation, retrace = VARIANTS[variant_index]
    return {
        "signal_id": pair_index + 1,
        "horizon_h": HORIZONS[pair_index % len(HORIZONS)],
        "activation_ratio": activation,
        "retrace_ratio": retrace,
        "logic_version": 5,
        "exit_reason": "timeout" if activation == 0 else "trail",
        "net_pnl_pct": 0.05 - 0.30 * retrace + 0.001 * (pair_index % 97),
        "computed_at": BOUNDARY + timedelta(
            days=-1 if pair_index % 2 == 0 else 1
        ),
        "ts": datetime(2026, 8, 25, tzinfo=UTC) + timedelta(
            seconds=60 * pair_index
        ),
        "token": TOKENS[pair_index % len(TOKENS)],
    }


class _GeneratingPool:
    """Двойник пула, ПОРОЖДАЮЩИЙ строки порциями и не хранящий их."""

    def __init__(self, pairs: int) -> None:
        self.pairs = pairs
        self.per_pair = len(VARIANTS)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        after, limit = args[0], int(args[3])
        start = 0 if after is None else self._index_after(after)
        end = min(start + limit, self.pairs * self.per_pair)
        return [
            make_row(i // self.per_pair, i % self.per_pair)
            for i in range(start, end)
        ]

    def _index_after(self, after_ts: datetime) -> int:
        """Первая строка СТРОГО после пары с этим временем сигнала."""
        seconds = int((after_ts - datetime(2026, 8, 25, tzinfo=UTC)
                       ).total_seconds())
        return (seconds // 60 + 1) * self.per_pair

    async def fetch_all(self) -> list[dict[str, Any]]:
        """ПРЕЖНЕЕ поведение: вся выборка одним списком (контрольный опыт)."""
        return [
            make_row(i // self.per_pair, i % self.per_pair)
            for i in range(self.pairs * self.per_pair)
        ]


async def _run(pairs: int, load_everything: bool) -> dict[str, Any]:
    import scripts.trailing_resample_9_1_3 as script
    from scripts.trailing_stats import collect, matrix
    from src.core.db import db

    pool = _GeneratingPool(pairs)
    db._pool = pool  # type: ignore[assignment]

    if load_everything:
        # ДЕФЕКТ, ВОЗВРАЩЁННЫЙ НАРОЧНО: вся таблица списком словарей, как было
        # до правки. Ровно это ядро и убило на боевой машине.
        rows = await pool.fetch_all()
        pairs_all, _dropped = collect(rows)
        values = matrix(pairs_all)
        count = values.shape[0]
    else:
        built, _composition, _dropped, _read = await script.stream_sample()
        count = len(built)

    return {
        "rows": pairs * len(VARIANTS),
        "pairs": count,
        "peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1
        ),
    }


def main() -> int:
    import asyncio

    rows_wanted = int(sys.argv[1])
    load_everything = "--load-everything" in sys.argv
    pairs = rows_wanted // len(VARIANTS)
    print(json.dumps(asyncio.run(_run(pairs, load_everything))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
