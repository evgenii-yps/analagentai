#!/usr/bin/env python3
"""Сброс отметок выгрузки в лист по перечню позиций (Этап 9.1.2.2, Задача 4).

ЗАЧЕМ ЭТО НУЖНО. Дефект Этапа 9.1.2 (протяжка формул затирала заметку) уже
создал в торговом журнале строки с ЧУЖИМИ заметками, а отметки
``sheet_opened_at`` / ``sheet_closed_at`` по ним проставлены. Отметка необратима
по построению: строки с ней в выборку выгрузки больше не попадают
(``fetch_positions_pending_open`` / ``fetch_positions_pending_close``). Значит,
исправленный приёмник этих строк уже не тронет — сам по себе он чинит только
будущие записи.

ВОССТАНОВЛЕНИЕ ДЕЛАЕТСЯ В ДВА ШАГА, И ПЕРВЫЙ — НЕ ЗДЕСЬ:

  1. ВЛАДЕЛЕЦ УДАЛЯЕТ испорченные строки в листе — удаляет СТРОКУ целиком, а не
     очищает ячейки. Очищенная строка сохранила бы за собой место, и заново
     созданная легла бы ниже неё; кроме того, в столбце заметок могла остаться
     чужая метка, и новая строка упёрлась бы в неё как в занятую (§2 ТЗ).
  2. ЭТОТ СКРИПТ снимает отметки, после чего очередной штатный прогон выгрузки
     создаёт строки заново — уже исправленным приёмником.

ЭТОТ СКРИПТ НЕ ПИШЕТ В ЛИСТ И НЕ ХОДИТ В СЕТЬ ВОВСЕ. Он меняет ровно две
колонки в ``positions`` и ничего больше: ни строк не удаляет, ни решений не
пересчитывает. Записи в ``signals``, ``signal_evaluations``, ``signal_targets``,
``risk_targets`` не делаются ни одной.

ПОЧЕМУ ПЕРЕЧЕНЬ ЗАДАЁТСЯ РУКАМИ, А НЕ ИЩЕТСЯ САМ. Признак испорченной строки
живёт В ЛИСТЕ (чужая метка в заметке), а не в базе: в базе такая позиция
выглядит совершенно здоровой — у неё верные цели, верный сигнал и проставленные
отметки. Единственный, кто видит настоящий признак, — человек, смотрящий в лист.
Скрипт, который «сам нашёл бы» такие позиции, искал бы их по догадке.

ПОДТВЕРЖДЕНИЕ ЧИСЛОМ ОБЯЗАТЕЛЬНО — по той же причине, что и в
``repair_9_1_strategy_settle.py``: сброс возможен только тогда, когда оператор
своими глазами видел отчёт и назвал то же самое число. Здесь цена ошибки ниже
(отметку можно поставить обратно очередным прогоном), но природа действия та
же: перечень набирается руками, и опечатка в нём — самая вероятная из ошибок.

КОДЫ ВОЗВРАТА:
  0 — выполнено (в том числе печать отчёта без ``--apply``);
  3 — ``--confirm-count`` не совпал с фактическим числом строк, НИЧЕГО не
      сброшено;
  4 — среди указанных id есть несуществующие, НИЧЕГО не сброшено.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА (правило проекта: пакетов на хосте нет). В образ
копируется только ``src/``, поэтому каталог скриптов подключается томом:

    # 1. только посмотреть (ничего не меняет):
    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro \\
        export python scripts/repair_9_1_2_2_marks.py --ids 11,12,13

    # 2. сброс. Число берётся ИЗ ОТЧЁТА шага 1 и набирается руками:
    docker compose --profile tools run --rm --no-deps \\
        -v ./scripts:/app/scripts:ro \\
        export python scripts/repair_9_1_2_2_marks.py --ids 11,12,13 \\
        --apply --confirm-count=3

    # 3. и уже затем — выгрузка, которая создаст строки заново:
    docker compose --profile tools run --rm --no-deps \\
        export python -m src.export_main --positions-only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.db import db  # noqa: E402


def parse_ids(raw: str) -> list[int]:
    """``"11, 12,13"`` → ``[11, 12, 13]``. Порядок сохраняется, повторы убираются.

    ПОВТОР В ПЕРЕЧНЕ — НЕ ОШИБКА, А ОПЕЧАТКА, и она не должна ломать счёт: без
    снятия повторов ``--ids 11,11`` дал бы «строк 2» при одной строке в базе, и
    подтверждение числом проверяло бы длину аргумента вместо факта.

    Пустой перечень и нечисловой элемент — ошибка: молча превратить их в «ничего
    не делать» значило бы сообщить об успехе там, где команда не понята.
    """
    ids: list[int] = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"не число: «{text}»") from None
        if value not in ids:
            ids.append(value)
    if not ids:
        raise ValueError("перечень пуст")
    return ids


def _fmt_ts(value: Any) -> str:
    """Время как ``2026-08-31 12:08:46`` в UTC либо прочерк для отсутствующего."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def report_text(rows: list[dict[str, Any]]) -> str:
    """Таблица «что будет сброшено» (§4 ТЗ): по строке на позицию.

    ПЕЧАТАЮТСЯ И ТЕКУЩИЕ ЗНАЧЕНИЯ ОБЕИХ ОТМЕТОК. Без них отчёт не позволял бы
    отличить позицию, которую выгрузка ещё не трогала (сброс ей ничего не
    изменит), от той, что уже легла в лист испорченной строкой, — а именно эта
    разница и решает, надо ли вообще удалять строку в листе.
    """
    lines = [
        f"{'id':>6}  {'токен':<10}{'статус':<10}{'открыта (UTC)':<21}"
        f"{'закрыта (UTC)':<21}{'причина выхода':<22}"
        f"{'sheet_opened_at':<21}{'sheet_closed_at':<21}",
        "-" * 132,
    ]
    for row in rows:
        lines.append(
            f"{int(row['id']):>6}  "
            f"{str(row['symbol']):<10}"
            f"{str(row['status']):<10}"
            f"{_fmt_ts(row['opened_at']):<21}"
            f"{_fmt_ts(row['closed_at']):<21}"
            f"{str(row['exit_reason'] or '—'):<22}"
            f"{_fmt_ts(row['sheet_opened_at']):<21}"
            f"{_fmt_ts(row['sheet_closed_at']):<21}"
        )
    if not rows:
        lines.append("(строк нет)")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Сброс отметок выгрузки в торговый журнал (Этап 9.1.2.2, Задача 4). "
            "Без --apply только печатает отчёт."
        )
    )
    parser.add_argument(
        "--ids",
        required=True,
        help="перечень position_id через запятую, например 11,12,13",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="выполнить сброс; без него скрипт только печатает отчёт",
    )
    parser.add_argument(
        "--confirm-count",
        type=int,
        default=None,
        help=(
            "сколько строк ожидается сбросить. Обязателен при --apply и "
            "берётся ИЗ ОТЧЁТА, набранный руками"
        ),
    )
    args = parser.parse_args()

    if args.apply and args.confirm_count is None:
        parser.error(
            "--apply без --confirm-count запрещён. Число берётся из отчёта "
            "(запустите ту же команду без --apply) и набирается руками: сброс "
            "возможен только тогда, когда человек своими глазами видел, что "
            "именно сбрасывается"
        )
    if args.confirm_count is not None and not args.apply:
        parser.error("--confirm-count имеет смысл только вместе с --apply")

    try:
        ids = parse_ids(args.ids)
    except ValueError as exc:
        parser.error(f"--ids разобрать не удалось: {exc}")

    await db.connect()
    try:
        rows = await db.get_positions_sheet_marks(ids)

        print("=" * 132)
        print(" ОТМЕТКИ ВЫГРУЗКИ В ТОРГОВЫЙ ЖУРНАЛ (Этап 9.1.2.2, Задача 4)")
        print("=" * 132)
        print(f"  Запрошено id: {len(ids)} — {', '.join(str(i) for i in ids)}")
        print(f"  Найдено строк: {len(rows)}")
        print()
        print(report_text(rows))

        # ОГРАЖДЕНИЕ 1: НЕСУЩЕСТВУЮЩИЙ id — ОСТАНОВКА, А НЕ ПРОПУСК. Перечень
        # набирается руками, и опечатка в нём — самая вероятная из ошибок.
        # Сбросить «те, что нашлись», и промолчать про остальные значило бы
        # выполнить не ту команду, которую дали, и отчитаться об успехе.
        found = {int(row["id"]) for row in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            print()
            print("=" * 132)
            print(" ОТКАЗ: среди указанных id есть несуществующие")
            print("=" * 132)
            print(f"  Нет в positions: {', '.join(str(i) for i in missing)}")
            print()
            print("  Перечень набирается руками, и опечатка в нём вероятнее")
            print("  всего остального. Сбросить найденные и промолчать про")
            print("  остальные значило бы выполнить не ту команду, которую дали.")
            print("  Ничего не сброшено.")
            return 4

        if not args.apply:
            print()
            print("  Ничего не изменено: без --apply скрипт только печатает.")
            print("  Чтобы сбросить, назовите это же число своими руками:")
            print(f"    ... --ids {args.ids} --apply --confirm-count={len(rows)}")
            print()
            print("  НАПОМИНАНИЕ О ПОРЯДКЕ: строки в листе удаляются ДО сброса")
            print("  отметок — целиком, а не очисткой ячеек. Иначе очередная")
            print("  выгрузка упрётся в чужую метку, оставшуюся в заметке, и")
            print("  строку не создаст (это и есть защита §2 ТЗ 9.1.2.2).")
            return 0

        # ОГРАЖДЕНИЕ 2: ПОДТВЕРЖДЕНИЕ ЧИСЛОМ. Счёт берётся по факту выборки, а
        # не по длине аргумента: расхождение означает, что база изменилась между
        # отчётом и сбросом, и молча работать в этот момент нельзя.
        if len(rows) != int(args.confirm_count):
            print()
            print("=" * 132)
            print(" ОТКАЗ: подтверждение не совпало с фактическим числом строк")
            print("=" * 132)
            print(f"  Названо в --confirm-count: {int(args.confirm_count)}")
            print(f"  Фактически найдено строк:  {len(rows)}")
            print()
            print("  Сбрасываемое множество уже не то, которое видел человек.")
            print("  Ничего не сброшено. Перечитайте отчёт и повторите команду")
            print(f"  с --confirm-count={len(rows)}, если это по-прежнему то,")
            print("  что вы хотите сбросить.")
            return 3

        reset = await db.reset_positions_sheet_marks(ids)
        print()
        print(f"  Отметки сброшены у строк: {reset}")
        print()
        print("  Строки в листе создаст ОЧЕРЕДНОЙ штатный прогон выгрузки:")
        print("  позиции вернулись в очередь, и приёмник версии 9.1.2.2 запишет")
        print("  им СВОИ заметки. Вручную:")
        print("    docker compose --profile tools run --rm --no-deps \\")
        print("        export python -m src.export_main --positions-only")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
