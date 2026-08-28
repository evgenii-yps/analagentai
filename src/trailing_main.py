"""Расчёт исходов подвижного выхода (Этап 8.10, §7 ТЗ).

Запускается ВНУТРИ контейнера (тот же образ и тот же сервис профиля tools, что
и расчёты 8.8 и 8.9 — нового сервиса этап не заводит):

    docker compose --profile tools run --rm --no-deps barrier \\
        python -m src.trailing_main

Почему в контейнере: нужны asyncpg/structlog/pydantic, которых на хосте нет и
не будет (правило D-3).

ЭТАП НИЧЕГО НЕ УЛУЧШАЕТ. Он измеряет. Ни одно решение системы от этого модуля
не зависит: сервисы decision, notify, evaluator и risk о нём не знают,
LOGIC_VERSION остаётся 5, а §5.4 ТЗ прямо запрещает выбирать «лучший» вариант
для внедрения. Задача этапа — таблица, а не решение.

КОДЫ ВОЗВРАТА:
    0 — расчёт выполнен. Исход ``no_data`` сбоем НЕ считается: это честная
        запись о том, что минутного ряда на интервале не было. Исход
        ``ambiguous`` — тоже не сбой: это признание, что порядок событий внутри
        бара неизвестен.
    1 — реальный сбой: база недоступна, ошибка записи, ЛИБО расхождение
        контрольного варианта с ``signal_outcomes_barrier``.

ПОЧЕМУ РАСХОЖДЕНИЕ КОНТРОЛЬНОГО ВАРИАНТА — ЭТО КОД 1, а не предупреждение.
Контрольный вариант обязан совпасть с Этапом 8.8 до последнего знака (§4 ТЗ).
Не совпал — значит, правила касания разошлись, и тогда недействительно ВСЁ
сравнение вариантов, включая таблицу §5. Такой прогон не «выполнен с
замечанием»: его результатом пользоваться нельзя, и узнать об этом надо из кода
возврата, который увидит cron, а не из строки в середине журнала.

АРГУМЕНТЫ:
    --since     нижняя граница момента сигнала, ISO-8601 (например 2026-08-25);
    --limit     потолок числа пар — для замера на части выборки, а не на всей;
    --recompute снести строки текущей версии логики и посчитать заново.

ЗАМЕР ВРЕМЕНИ ВЫВОДИТСЯ ВСЕГДА (§7 ТЗ), ключами ``seconds``, ``windows_read`` и
``rows_per_second``. Он нужен не для красоты: расчёт идёт между двумя другими
задачами по расписанию, и если он перестанет укладываться в окно, узнать об
этом надо из журнала, а не по последствиям.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog

from src.core.config import settings
from src.core.logging import setup_logging
from src.trailing.rule import VARIANTS
from src.trailing.runner import run


def parse_since(value: str | None) -> datetime | None:
    """Разбор ``--since``. Наивное время трактуется как UTC, а не как местное.

    Молчаливая трактовка наивного времени как местного сдвинула бы границу на
    смещение пояса сервера — и выборка тихо оказалась бы другой.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m src.trailing_main",
        description="Подвижный выход: замер на истории (Этап 8.10)",
    )
    parser.add_argument(
        "--since", default=None,
        help="нижняя граница момента сигнала, ISO-8601 (например 2026-08-25)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="потолок числа пар (сигнал, горизонт) — для замера на части выборки",
    )
    parser.add_argument(
        "--recompute", action="store_true",
        help="снести строки текущей версии логики и посчитать заново",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — сбой)."""
    setup_logging()
    log = structlog.get_logger().bind(component="trailing_outcomes")
    try:
        stats = await run(
            since=parse_since(args.since),
            limit=args.limit,
            recompute=args.recompute,
        )
    except Exception as exc:  # noqa: BLE001 — код возврата важнее трассировки в cron
        log.error("trailing_compute_failed=1", error=str(exc))
        return 1

    compared = stats.control.get("compared", 0)
    if compared > 0 and not stats.control_ok:
        log.error(
            "trailing_exit=1",
            reason="контрольный вариант не совпал с signal_outcomes_barrier",
            control_compared=compared,
            control_mismatched=stats.control.get("mismatched"),
            control_missing=stats.control.get("missing"),
        )
        return 1

    log.info(
        "trailing_exit=0",
        variants=len(VARIANTS),
        pairs=stats.pairs,
        windows_read=stats.windows_read,
        written=stats.written,
        seconds=round(stats.seconds, 3),
        control_compared=compared,
        logic_version=settings.LOGIC_VERSION,
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса."""
    raise SystemExit(asyncio.run(_run(parse_args(argv))))


if __name__ == "__main__":
    main()
