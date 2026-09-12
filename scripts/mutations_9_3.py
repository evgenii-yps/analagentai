#!/usr/bin/env python3
"""МУТАЦИОННОЕ ТЕСТИРОВАНИЕ ЭТАПА 9.3 (§11 ТЗ) — обязательная часть этапа.

ЗАЧЕМ ЭТО НУЖНО, ЕСЛИ ТЕСТЫ И ТАК ЗЕЛЁНЫЕ. Зелёный набор доказывает, что код
делает то, что делает; он НЕ доказывает, что тесты заметили бы, если бы код
делал другое. Единственный способ это проверить — сломать код нарочно и
увидеть, что набор покраснел. §11 ТЗ требует не меньше двенадцати таких
поломок и называет каждую поимённо.

ПОЛОМКА, КОТОРАЯ НЕ ПОЙМАЛАСЬ, ЧИНИТСЯ ТЕСТОМ В ЭТОМ ЖЕ ЭТАПЕ — так велит
§11, и так и сделано: три теста набора 9.3 (перезагрузка модуля в опыте §10.3,
проверка запроса паузы на настоящей базе, проверка версии логики новой строки)
написаны именно потому, что без них поломки 4, 2, 6 и 8 проходили незамеченными.

КАК ЭТО УСТРОЕНО. Каждая поломка — точечная замена текста в файле исходников.
Скрипт применяет её, запускает набор тестов, ждёт ПАДЕНИЯ и возвращает файл в
исходное состояние в ``finally`` — при любом исходе, включая Ctrl+C.

ЗАПУСК:
    python scripts/mutations_9_3.py                # все поломки
    python scripts/mutations_9_3.py --only 1 4 11  # выборочно
    python scripts/mutations_9_3.py --full         # по ВСЕМУ набору тестов

БАЗА. Поломки 2, 6 и 10 живут в SQL, и ловят их тесты, которым нужна
настоящая PostgreSQL. Без ``AT_TEST_DSN`` такие тесты ПРОПУСКАЮТСЯ, а
пропущенный тест поломку не ловит — скрипт говорит об этом прямо и считает
такую поломку НЕ ПРОВЕРЕННОЙ, а не пойманной.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Набор, по которому гоняются поломки. Не весь набор проекта: он идёт полторы
# минуты, и двенадцать прогонов заняли бы двадцать. Сюда включено всё, что
# трогает торговый слой и уведомления; ``--full`` гоняет проект целиком.
FOCUS_TESTS = (
    "tests/test_stage_9_3.py",
    "tests/test_logic_7.py",
    "tests/test_stage_9_1.py",
    "tests/test_stage_9_1_1.py",
    "tests/test_stage_9_2.py",
    "tests/test_notify.py",
    "tests/test_bot.py",
)


@dataclasses.dataclass(frozen=True)
class Mutation:
    """Одна намеренная поломка.

    ``number`` — номер пункта §11 ТЗ. ``file`` — файл исходников относительно
    корня. ``old`` обязан встречаться в файле РОВНО ОДИН РАЗ: замена, попавшая
    в два места, ломает больше, чем описано, и вывод «тесты покраснели» стал бы
    относиться не к той поломке. ``needs_db`` — поломка живёт в SQL и ловится
    только тестом с настоящей базой.
    """

    number: int
    title: str
    file: str
    old: str
    new: str
    needs_db: bool = False


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        1, "POSITIONS_MAX_OPEN=0 трактуется как «ноль позиций»",
        "src/positions/rules.py",
        "if int(max_open) > 0 and int(open_count) >= int(max_open):",
        "if int(open_count) >= int(max_open):",
    ),
    Mutation(
        2, "пауза считается от закрытия, а не от открытия",
        "src/core/db.py",
        "SELECT p.instrument_id, max(p.opened_at) AS last_opened_at",
        "SELECT p.instrument_id, "
        "max(coalesce(p.closed_at, p.opened_at)) AS last_opened_at",
        needs_db=True,
    ),
    Mutation(
        3, "пауза обнуляется в начале часа",
        "src/positions/runner.py",
        "            latest_entry - timedelta(seconds=token_pause_sec),",
        "            latest_entry.replace(minute=0, second=0, microsecond=0),",
    ),
    Mutation(
        4, "метка последнего открытия берётся из памяти процесса",
        "src/positions/runner.py",
        "        last_open_at = await db.get_last_open_ts_by_instrument(\n"
        "            latest_entry - timedelta(seconds=token_pause_sec),\n"
        "            int(settings.LOGIC_VERSION),\n"
        "        )",
        "        last_open_at = dict(_MUTANT_MEMORY)",
    ),
    Mutation(
        5, "пауза применяется ко всем токенам сразу, а не к каждому отдельно",
        "src/positions/runner.py",
        "        last_open = last_open_at.get(instrument_id)",
        "        last_open = max(last_open_at.values(), default=None)",
    ),
    Mutation(
        6, "пауза читает позиции всех версий логики",
        "src/core/db.py",
        "      AND p.logic_version = $2\n",
        "      AND ($2::int IS NOT NULL OR TRUE)\n",
        needs_db=True,
    ),
    Mutation(
        7, "отказ cooldown не записывается в журнал отказов",
        "src/positions/runner.py",
        "        await db.record_position_rejections(rows)",
        "        pass",
    ),
    Mutation(
        8, "новая позиция получает logic_version = 6",
        "src/positions/runner.py",
        "        version = int(settings.LOGIC_VERSION)",
        "        version = 6",
    ),
    Mutation(
        9, "сигнальные уведомления продолжают уходить при выключенном флаге",
        "src/notify/agent.py",
        "    return bool(settings.NOTIFY_SIGNALS_ENABLED)",
        "    return True",
    ),
    Mutation(
        10, "сводка считает открытые сделки как закрытые",
        "src/core/db.py",
        "                WHERE logic_version = $3\n"
        "                  AND status = 'closed'\n"
        "                  AND closed_at >= $1 AND closed_at < $2",
        "                WHERE logic_version = $3\n"
        "                  AND opened_at >= $1 AND opened_at < $2",
        needs_db=True,
    ),
    Mutation(
        11, "сводка берёт сутки по местному времени вместо UTC",
        "src/notify/daily_trades.py",
        "    midnight = now.astimezone(UTC).replace(",
        '    midnight = now.astimezone(__import__("zoneinfo").ZoneInfo('
        '"Europe/Moscow")).replace(',
    ),
    Mutation(
        12, "выборка открытых позиций возвращает одну вместо списка",
        "src/positions/runner.py",
        "    for row in await db.get_open_positions():",
        "    for row in (await db.get_open_positions())[:1]:",
    ),
    # --- СВЕРХ ДВЕНАДЦАТИ, ТРЕБУЕМЫХ §11 -----------------------------------
    #
    # Три поломки на том, что этот этап трогал руками и что §11 назвать не мог,
    # потому что решения приняты исполнителем: граница окна отказов, потолок
    # сообщений о сделках и его почасовая сводка.
    Mutation(
        13, "инвариант «один инструмент — одна позиция» включён навсегда",
        "src/positions/rules.py",
        "    if one_per_token and int(token_open_count) > 0:",
        "    if int(token_open_count) > 0:",
    ),
    Mutation(
        14, "почасовая сводка придержанного входит в счёт потолка",
        "src/positions/runner.py",
        '    _log.info("notify_trade_rollup_sent=1", count=len(texts))',
        '    await rate_limit.record_sent(_TRADE_SENT_KEY, now)\n'
        '    _log.info("notify_trade_rollup_sent=1", count=len(texts))',
    ),
    Mutation(
        15, "суточная сводка уходит при каждой проверке расписания",
        "src/notify/daily_trades.py",
        '    return last_sent_day != now.strftime("%Y-%m-%d")',
        "    return True",
    ),
)

# Поломка 4 требует, чтобы в модуле было имя, которого там нет: без него
# служба упала бы на NameError, и «тесты покраснели» означало бы не то.
# Память процесса подкладывается вместе с поломкой.
PRELUDE = {
    4: (
        "_log = structlog.get_logger().bind(component=\"positions\")",
        "_log = structlog.get_logger().bind(component=\"positions\")\n"
        "_MUTANT_MEMORY: dict[int, object] = {}",
    ),
}


def _apply(mutation: Mutation) -> str:
    """Применяет поломку. Возвращает исходный текст файла для восстановления."""
    path = ROOT / mutation.file
    original = path.read_text(encoding="utf-8")
    count = original.count(mutation.old)
    if count != 1:
        raise SystemExit(
            f"поломка {mutation.number}: образец встречается {count} раз(а) в "
            f"{mutation.file}, а обязан ровно один. Код правили — поправьте "
            f"каталог поломок, иначе проверка §11 проверяет не то, что названо"
        )
    broken = original.replace(mutation.old, mutation.new, 1)
    if mutation.number in PRELUDE:
        old, new = PRELUDE[mutation.number]
        assert broken.count(old) == 1, mutation.number
        broken = broken.replace(old, new, 1)
    path.write_text(broken, encoding="utf-8")
    return original


def _run_tests(tests: tuple[str, ...]) -> tuple[bool, str]:
    """Прогоняет набор. Возвращает (упал ли он, последние строки вывода)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = "\n".join(result.stdout.strip().splitlines()[-4:])
    return result.returncode != 0, tail


def main() -> int:
    parser = argparse.ArgumentParser(description="Мутационное тестирование 9.3")
    parser.add_argument("--only", nargs="*", type=int, default=None)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    tests = ("tests",) if args.full else FOCUS_TESTS
    has_db = bool(os.environ.get("AT_TEST_DSN"))
    if not has_db:
        print(
            "⚠ AT_TEST_DSN не задан: поломки в SQL проверить нечем — тесты, "
            "которые их ловят, будут пропущены. Такие поломки считаются "
            "НЕ ПРОВЕРЕННЫМИ, а не пойманными.\n"
        )

    chosen = [
        m for m in MUTATIONS
        if args.only is None or m.number in args.only
    ]
    caught, missed, skipped = [], [], []
    for mutation in chosen:
        if mutation.needs_db and not has_db:
            skipped.append(mutation)
            print(f"— {mutation.number:2d}. {mutation.title}: НЕ ПРОВЕРЕНА")
            continue
        original = _apply(mutation)
        try:
            failed, tail = _run_tests(tests)
        finally:
            (ROOT / mutation.file).write_text(original, encoding="utf-8")
        mark = "поймана" if failed else "НЕ ПОЙМАНА"
        (caught if failed else missed).append(mutation)
        print(f"{'✔' if failed else '✘'} {mutation.number:2d}. "
              f"{mutation.title}: {mark}")
        print(f"      {tail.splitlines()[-1] if tail else '—'}")

    print(
        f"\nИтог: поймано {len(caught)}, не поймано {len(missed)}, "
        f"не проверено {len(skipped)} из {len(chosen)}."
    )
    if missed:
        print(
            "ПОЛОМКА, КОТОРАЯ НЕ ПОЙМАЛАСЬ, ЧИНИТСЯ ТЕСТОМ В ЭТОМ ЖЕ ЭТАПЕ "
            "(§11 ТЗ) — не в следующем и не «когда-нибудь»."
        )
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
