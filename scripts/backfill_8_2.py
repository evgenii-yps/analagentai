"""Догрузка часовых свечей спота под цели по вероятности (§2 ТЗ 8.2).

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, если суточный пересчёт догружает свежий край сам.
Первая загрузка — это не «свежий край»: по XRP и DOGE истории нет вовсе, и
качать нужно 95 суток с нуля. Это часы работы и тысячи запросов к бирже, и
делать это внутри задачи, у которой есть срок (03:40 UTC) и которая пишет
цели, — значит смешать разовую загрузку с регламентной работой.

ЧТО ДЕЛАЕТ:
  * грузит [сегодня − 95 суток; сейчас] по каждому спотовому инструменту;
  * печатает числа ДО и ПОСЛЕ по каждому инструменту (их требует отчёт §2);
  * останавливается, если свободного места на диске меньше 40% (§2 ТЗ).

ЗАПУСК (профиль backtest — только там есть пакет загрузчика и сеть до биржи;
профиль «*» при сборке обязателен, иначе образ молча не соберётся):

    docker compose --profile backtest run --rm backtest \
        python scripts/backfill_8_2.py

Повторный запуск безопасен: вставка идёт с ON CONFLICT DO NOTHING, границы
проходов берутся из БД, уже загруженное не перекачивается.

Загрузка идемпотентна и НИЧЕГО НЕ УДАЛЯЕТ. Продакшн-таблицы (public) не
затрагиваются вовсе: пишется только backtest.candles.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from datetime import UTC, datetime, timedelta

import structlog

from backtest import db as bt_db
from backtest.loader import LoaderError, OkxHistory, backfill_candles, create_http_client
from src.core.config import settings
from src.core.logging import setup_logging
from src.risk.runner import bt_inst_id

# Порог остановки по ресурсам (§2 ТЗ): свободного места меньше 40% — загрузку
# прекращаем. Диск делится с продакшн-данными, и забить его историей ради
# украшения сигнала — худший из возможных разменов.
MIN_FREE_SHARE = 0.40

_log = structlog.get_logger().bind(component="backfill_8_2")


def free_share(path: str = "/") -> float:
    """Доля свободного места на разделе (0.0–1.0)."""
    usage = shutil.disk_usage(path)
    return usage.free / usage.total if usage.total else 0.0


async def _counts(inst_id: str, bar: str) -> dict[str, object]:
    """Числа по инструменту: сколько свечей и где границы ряда."""
    row = await bt_db.fetchrow(
        "SELECT count(*) AS n, min(open_time) AS lo, max(open_time) AS hi "
        "FROM backtest.candles WHERE inst_id=$1 AND bar=$2;",
        inst_id, bar,
    )
    return {
        "candles": int(row["n"] or 0),
        "from": None if row["lo"] is None else row["lo"].isoformat(),
        "to": None if row["hi"] is None else row["hi"].isoformat(),
    }


async def run(days: int, bar: str) -> int:
    """Загрузка по всем спотовым инструментам. Возвращает код выхода."""
    setup_logging()
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    inst_ids = [bt_inst_id(pair.spot) for pair in settings.symbol_pairs]

    share = free_share()
    _log.info("backfill_8_2_start=1", free_share=round(share, 3),
              instruments=inst_ids, since=since.isoformat(), bar=bar)
    if share < MIN_FREE_SHARE:
        _log.error("backfill_8_2_no_space=1", free_share=round(share, 3),
                   need=MIN_FREE_SHARE)
        return 1

    await bt_db.connect()
    client = create_http_client()
    failed = 0
    try:
        history = OkxHistory(client, pause_ms=200)
        for inst_id in inst_ids:
            before = await _counts(inst_id, bar)
            if free_share() < MIN_FREE_SHARE:
                # Останов, а не пропуск одного инструмента: место кончается для
                # всех сразу, и продолжать по остальным бессмысленно.
                _log.error("backfill_8_2_stopped_no_space=1", inst_id=inst_id,
                           free_share=round(free_share(), 3))
                return 1
            try:
                inserted = await backfill_candles(
                    inst_id, bar, since, now, client=history
                )
            except LoaderError as exc:
                failed += 1
                _log.error("backfill_8_2_instrument_failed=1",
                           inst_id=inst_id, error=str(exc), **{"before": before})
                continue
            after = await _counts(inst_id, bar)
            _log.info(
                "backfill_8_2_instrument_done=1",
                inst_id=inst_id, inserted=int(inserted or 0),
                before_candles=before["candles"], after_candles=after["candles"],
                before_from=before["from"], before_to=before["to"],
                after_from=after["from"], after_to=after["to"],
            )
    finally:
        await client.aclose()
        await bt_db.close()

    _log.info("backfill_8_2_done=1", instruments=len(inst_ids), failed=failed,
              free_share=round(free_share(), 3))
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python scripts/backfill_8_2.py",
        description="Догрузка часовых свечей спота под цели (Этап 8.2 §2)",
    )
    parser.add_argument(
        "--days", type=int,
        default=settings.RISK_WINDOW_DAYS + settings.RISK_BACKFILL_MARGIN_DAYS,
        help="глубина загрузки в сутках (по умолчанию 95: окно 90 плюс запас 5)",
    )
    parser.add_argument("--bar", default=settings.RISK_BAR, help="бар свечей (1H)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.days, args.bar)))


if __name__ == "__main__":
    main()
