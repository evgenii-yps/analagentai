#!/usr/bin/env python3
"""ЗАМЕР 0: цели по вероятности до и после загрузки старших рядов. БЛОКИРУЮЩАЯ.

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ. Замер 0 кладёт дневные, недельные и месячные бары
в ``backtest.candles`` — ТУ ЖЕ таблицу, из которой продакшн считает цели по
вероятности (``src/risk/runner.py`` → ``src/core/db.py:get_backtest_candles``).
Решение опиралось на утверждение: все читатели фильтруют по ``bar``, поэтому
новые строки им не видны. Утверждение проверено ЧТЕНИЕМ КОДА — восемь мест,
перечень в заголовке миграции 026. Этот скрипт проверяет его ИЗМЕРЕНИЕМ.

Чтение кода и измерение — разные вещи. Первое отвечает «я не вижу, как это
могло бы сломаться», второе — «оно не сломалось». Замерный этап обязан
предъявлять второе.

КАК ЭТО УСТРОЕНО:

    --snapshot ФАЙЛ   снять слепок целей ДО загрузки старших рядов;
    --compare  ФАЙЛ   пересчитать ПОСЛЕ и сравнить со слепком;
    --control  ФАЙЛ   контрольный опыт: показать, что сравнение умеет падать.

МОМЕНТ РАСЧЁТА ЗАМОРАЖИВАЕТСЯ, И БЕЗ ЭТОГО ПРОВЕРКА БЫЛА БЫ ШУМОМ. Окно целей
отсчитывается от ТЕКУЩЕГО ЧАСА: ``window_from = час_сейчас − 90 суток``. Между
слепком и пересчётом проходят часы загрузки, окно сдвигается, и цели изменились
бы САМИ — без всякого отношения к старшим барам. Поэтому ``--compare`` считает
с тем же моментом, что записан в слепке. Любое расхождение после этого
относится к ДАННЫМ, а не к часам.

ЧТО ЕЩЁ МОЖЕТ СДВИНУТЬ ЦЕЛИ, И ПОЧЕМУ ЭТО НАЗВАНО ОТДЕЛЬНО. Шаг 2 Замера 0
дописывает свежие ЧАСОВЫЕ свечи, и они попадают внутрь того же окна — цели от
них изменятся законно. Свалить это на старшие бары было бы ложным обвинением,
а промолчать — ложным оправданием. Поэтому слепок хранит ОТПЕЧАТОК ЧАСОВОГО
РЯДА (число строк, границы и sha256 по значениям), и при расхождении печатается,
изменился ли часовой ряд. Читатель видит, какое из двух объяснений применимо.

ПОРЯДОК ЗАПУСКА — СЛЕПОК ПОСЛЕ ЧАСОВОГО ШАГА, НО ДО СТАРШЕГО. Тогда между
двумя точками меняются ТОЛЬКО старшие бары, и проверка отвечает ровно на свой
вопрос. Порядок закреплён в deploy/verify_z0.sh и в отчёте этапа.

ЦЕЛИ СЧИТАЮТСЯ ПРОДАКШН-КОДОМ, А НЕ ПОВТОРЕНЫ ЗДЕСЬ. Читает
``db.get_backtest_candles`` — тот самый метод, которым читает продакшн; считает
``src.risk.runner.build_rows`` и ``src.risk.quality.check_series`` — те самые
функции, которыми считает продакшн. Своя реализация расчёта означала бы, что
сравнивается не то, что работает.

КОДЫ ВОЗВРАТА:
  ``--snapshot``  0 — слепок снят; 2 — снять не удалось;
  ``--compare``   0 — совпало; 2 — расхождение (различающиеся строки печатаются);
  ``--control``   0 — контрольный опыт УПАЛ, как обязан; 4 — контроль слеп либо
                  неприменим (старших баров в таблице нет — доказывать нечем).

НИ ОДНОЙ ЗАПИСИ В БАЗУ ЭТОТ СКРИПТ НЕ ДЕЛАЕТ НИ В ОДНОМ РЕЖИМЕ. Он читает
продакшн-таблицы и схему ``backtest`` и пишет только свой файл слепка.

ЗАПУСК (внутри собранного образа):

    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run --rm -e PYTHONUNBUFFERED=1 backtest \\
        python scripts/risk_targets_parity_z0.py --snapshot \\
        /opt/agent-trade/analysis_out/z0_targets.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog  # noqa: E402

from backtest.htf import HTF_BARS  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402
from src.risk.quality import check_series  # noqa: E402
from src.risk.runner import DIRECTIONS, bt_inst_id, build_rows  # noqa: E402
from src.risk.targets import Candle  # noqa: E402

_log = structlog.get_logger().bind(component="z0_risk_targets")

DEFAULT_SNAPSHOT = "/opt/agent-trade/analysis_out/z0_targets.json"

# Поля результата расчёта, попадающие в слепок. Перечень ЗАКРЫТ намеренно:
# сравнивать «весь объект» значило бы однажды поймать расхождение по полю,
# которое к целям не относится, и наоборот — не заметить нового поля.
COMPARED_FIELDS = (
    "n_observations", "target_pct", "hit_rate",
    "mfe_p25", "mfe_p50", "mfe_p75",
    "covers_fees", "no_target_reason", "skipped_gap", "skipped_tail",
)

# Поля предпроверки ряда. Их изменение — тоже расхождение: если после загрузки
# продакшн стал бы считать ряд негодным, цели заменились бы строками data_gap,
# и «цели не изменились» было бы неправдой.
PRECHECK_FIELDS = (
    "candles", "max_run_hours", "bad_invariants", "flat", "duplicates", "failures",
)


class SnapshotError(RuntimeError):
    """Слепок снять или прочитать не удалось. Сравнение не выполняется."""


def target_windows(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Границы окна целей: ``(час_сейчас, window_from, read_from)``.

    ЭТО КОПИЯ ТРЁХ СТРОК ``src/risk/runner.py``, И ЭТО СКАЗАНО ПРЯМО.

    Правильно было бы вынести арифметику в общую функцию и звать её обоими
    местами. Сделать этого нельзя: §1 ТЗ Замера 0 запрещает менять
    ``src/risk/*``, а замерный этап, правящий продакшн-код ради удобства
    собственной проверки, перестаёт быть замерным.

    Копия не оставлена без присмотра. Тест
    ``test_window_arithmetic_copy_matches_production`` читает исходник
    ``src/risk/runner.py`` и падает, если эти выражения там изменились, — то
    есть расхождение копии с оригиналом будет замечено на стенде, а не
    проявится молча неверным окном. Когда запрет §1 перестанет действовать,
    эту функцию надо удалить, а арифметику вынести в ``src/risk``.
    """
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    window_from = hour_now - timedelta(days=settings.RISK_WINDOW_DAYS)
    read_from = hour_now - timedelta(
        days=settings.RISK_WINDOW_DAYS + settings.RISK_BACKFILL_MARGIN_DAYS
    )
    return hour_now, window_from, read_from


def fingerprint(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Отпечаток прочитанного часового ряда: сколько, откуда докуда и sha256.

    Нужен, чтобы отличить «цели изменились из-за старших баров» от «цели
    изменились из-за свежих часовых свечей». Без него расхождение пришлось бы
    ОБЪЯСНЯТЬ, а объяснение без данных — это догадка.

    В хеш идут значения, а не только их число: дописанная свеча меняет счётчик,
    а исправленная — нет, и вторая опаснее первой.
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row['open_time'].isoformat()}|{row['open']}|{row['high']}|"
            f"{row['low']}|{row['close']}\n".encode()
        )
    return {
        "rows": len(rows),
        "first": rows[0]["open_time"].isoformat() if rows else None,
        "last": rows[-1]["open_time"].isoformat() if rows else None,
        "sha256": digest.hexdigest(),
    }


async def read_candles_production(inst_id: str, read_from: datetime) -> list[dict[str, Any]]:
    """Часовые свечи ТЕМ ЖЕ методом, которым их читает продакшн."""
    return await db.get_backtest_candles(inst_id, settings.RISK_BAR, read_from)


async def read_candles_without_bar_filter(
    inst_id: str, read_from: datetime
) -> list[dict[str, Any]]:
    """ЗАВЕДОМО НЕПРАВИЛЬНОЕ чтение — БЕЗ фильтра по ``bar``. Только для контроля.

    Это единственная копия продакшн-запроса в файле, и она СПЕЦИАЛЬНО сломана:
    контрольный опыт §11 ТЗ должен показать, что случилось бы, если бы читатель
    свечей забыл про масштаб. Если после такого чтения цели НЕ изменятся,
    значит сравнение целей нечувствительно к посторонним строкам — и тогда его
    «совпало» в основном режиме не доказывает ничего.

    Функция не вызывается ни из одного режима, кроме ``--control``.
    """
    rows = await db.pool.fetch(
        """
        SELECT open_time, open, high, low, close
        FROM backtest.candles
        WHERE inst_id = $1 AND open_time >= $2
        ORDER BY open_time ASC;
        """,
        inst_id, read_from,
    )
    return [dict(row) for row in rows]


def to_candles(rows: list[dict[str, Any]]) -> list[Candle]:
    """Строки БД → свечи расчёта. Повторяет ``src/risk/runner.py:_candles``.

    Функция там приватная (подчёркивание), и звать её отсюда значило бы
    опираться на то, что автор пометил как внутреннее. Три строки приведения
    типа воспроизведены; поля перечислены явно, поэтому расхождение по составу
    полей проявилось бы ошибкой ключа, а не тихо.
    """
    return [
        Candle(
            open_time=row["open_time"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        for row in rows
    ]


async def compute_targets(
    now: datetime, *, reader=read_candles_production
) -> dict[str, Any]:
    """Считает цели по всем продакшн-инструментам на ЗАДАННЫЙ момент.

    ``reader`` подменяется только контрольным опытом. По умолчанию — ровно тот
    путь чтения, которым ходит продакшн.
    """
    hour_now, window_from, read_from = target_windows(now)
    horizons = settings.eval_horizons_hours
    instruments: dict[str, Any] = {}

    for pair in settings.symbol_pairs:
        inst_id = bt_inst_id(pair.spot)
        rows = await reader(inst_id, read_from)
        loaded = to_candles(rows)
        # Выборка — ровно окно расчёта; предпроверка смотрит на весь
        # прочитанный ряд, включая запас. Так же в продакшне.
        candles = [item for item in loaded if item.open_time >= window_from]
        check = check_series(
            loaded,
            now=now,
            min_run_hours=settings.RISK_MIN_RUN_HOURS,
            max_age_hours=settings.RISK_MAX_AGE_HOURS,
            max_flat_pct=settings.RISK_MAX_FLAT_PCT,
        )
        _, results = build_rows(
            # instrument_id участвует только в строке для записи в базу, а её
            # мы не пишем: сравниваются ВЫЧИСЛЕННЫЕ величины. Ноль здесь —
            # признак того, что строка никуда не пойдёт, а не идентификатор.
            instrument_id=0,
            candles=candles,
            horizons=horizons,
            computed_at=now,
            window_from=window_from,
            window_to=now,
        )
        instruments[inst_id] = {
            "symbol": pair.spot,
            "hourly": fingerprint(rows),
            "precheck": {
                "candles": check.candles,
                "max_run_hours": check.max_run_hours,
                "bad_invariants": check.bad_invariants,
                "flat": check.flat,
                "duplicates": check.duplicates,
                "failures": list(check.failures),
            },
            "targets": {
                f"{horizon_h}|{direction}": {
                    field: getattr(result, field) for field in COMPARED_FIELDS
                }
                for (horizon_h, direction), result in results.items()
            },
        }

    return {
        "frozen_now": now.isoformat(),
        "hour_now": hour_now.isoformat(),
        "window_from": window_from.isoformat(),
        "read_from": read_from.isoformat(),
        "risk_bar": settings.RISK_BAR,
        "window_days": settings.RISK_WINDOW_DAYS,
        "backfill_margin_days": settings.RISK_BACKFILL_MARGIN_DAYS,
        "cost_roundtrip_pct": settings.RISK_COST_ROUNDTRIP_PCT,
        "min_observations": settings.RISK_MIN_OBSERVATIONS,
        "targets_version": settings.RISK_TARGETS_VERSION,
        "horizons": list(horizons),
        "directions": list(DIRECTIONS),
        "instruments": instruments,
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Сравнивает два слепка и возвращает ПЕРЕЧЕНЬ РАЗЛИЧАЮЩИХСЯ СТРОК.

    Строка различия называет инструмент, горизонт, направление, поле и оба
    значения. Печатать «слепки не совпали» без этого значило бы отправить
    человека сравнивать два json-файла глазами.
    """
    diffs: list[str] = []

    # Настройки расчёта. Изменившийся порог сделал бы сравнение целей
    # бессмысленным, и молча принять его за расхождение данных нельзя.
    for key in ("risk_bar", "window_days", "backfill_margin_days",
                "cost_roundtrip_pct", "min_observations", "targets_version",
                "horizons", "hour_now", "window_from", "read_from"):
        if before.get(key) != after.get(key):
            diffs.append(
                f"НАСТРОЙКА {key}: было {before.get(key)!r}, стало {after.get(key)!r}"
            )

    names = sorted(set(before["instruments"]) | set(after["instruments"]))
    for inst_id in names:
        old = before["instruments"].get(inst_id)
        new = after["instruments"].get(inst_id)
        if old is None or new is None:
            diffs.append(
                f"{inst_id}: инструмент "
                + ("появился" if old is None else "исчез")
                + " между слепком и пересчётом"
            )
            continue
        for field in PRECHECK_FIELDS:
            if old["precheck"].get(field) != new["precheck"].get(field):
                diffs.append(
                    f"{inst_id} предпроверка.{field}: было "
                    f"{old['precheck'].get(field)!r}, стало "
                    f"{new['precheck'].get(field)!r}"
                )
        keys = sorted(set(old["targets"]) | set(new["targets"]))
        for key in keys:
            old_row = old["targets"].get(key)
            new_row = new["targets"].get(key)
            if old_row is None or new_row is None:
                diffs.append(f"{inst_id} {key}: строка "
                             + ("появилась" if old_row is None else "исчезла"))
                continue
            horizon_h, direction = key.split("|")
            for field in COMPARED_FIELDS:
                if old_row.get(field) != new_row.get(field):
                    diffs.append(
                        f"{inst_id} горизонт {horizon_h}ч {direction} {field}: "
                        f"было {old_row.get(field)!r}, стало {new_row.get(field)!r}"
                    )
    return diffs


def hourly_changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Инструменты, у которых изменился сам ЧАСОВОЙ ряд.

    Их расхождение по целям объясняется данными часового ряда, а не старшими
    барами. Различать эти два случая обязательно: иначе законное изменение
    выглядело бы нарушением границы этапа, и наоборот.
    """
    changed: list[str] = []
    for inst_id, new in after["instruments"].items():
        old = before["instruments"].get(inst_id)
        if old is None:
            continue
        if old["hourly"]["sha256"] != new["hourly"]["sha256"]:
            changed.append(
                f"{inst_id}: строк было {old['hourly']['rows']}, стало "
                f"{new['hourly']['rows']}; край был {old['hourly']['last']}, "
                f"стал {new['hourly']['last']}"
            )
    return changed


def load_snapshot(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SnapshotError(
            f"слепка {path} нет. Он снимается ДО загрузки старших рядов: "
            "python scripts/risk_targets_parity_z0.py --snapshot " + path
        ) from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"слепок {path} не читается как JSON: {exc}") from exc


def save_snapshot(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)


def print_snapshot_summary(data: dict[str, Any]) -> None:
    print(f"  момент расчёта (заморожен): {data['frozen_now']}", flush=True)
    print(f"  окно целей: {data['window_from']} … {data['hour_now']} "
          f"({data['window_days']} суток, масштаб {data['risk_bar']})", flush=True)
    for inst_id, item in sorted(data["instruments"].items()):
        rows = item["hourly"]
        failures = item["precheck"]["failures"]
        print(f"  {inst_id:<12} часовых строк {rows['rows']:>6}, "
              f"край {rows['last']}, целей {len(item['targets'])}, "
              f"предпроверка {failures or 'ok'}", flush=True)


async def run(args: argparse.Namespace) -> int:
    setup_logging()
    print("=" * 100, flush=True)
    print(" ЗАМЕР 0: цели по вероятности до и после загрузки старших рядов", flush=True)
    print("=" * 100, flush=True)
    print(f" Время запуска: {datetime.now(UTC).isoformat()} (UTC)", flush=True)
    print(f" Файл слепка:   {args.path}", flush=True)
    print(" Скрипт НИЧЕГО не пишет в базу ни в одном режиме.", flush=True)

    await db.connect()
    try:
        htf_rows = int(await db.pool.fetchval(
            "SELECT count(*) FROM backtest.candles WHERE bar = ANY($1::text[]);",
            list(HTF_BARS),
        ) or 0)
        print(f" Старших баров в backtest.candles: {htf_rows}", flush=True)

        if args.mode == "snapshot":
            return await _snapshot(args.path)
        if args.mode == "compare":
            return await _compare(args.path, htf_rows)
        return await _control(args.path, htf_rows)
    finally:
        await db.close()


async def _snapshot(path: str) -> int:
    now = datetime.now(UTC)
    print("\n=== СЛЕПОК ЦЕЛЕЙ (снимать ДО загрузки старших рядов) ===", flush=True)
    data = await compute_targets(now)
    if not data["instruments"]:
        print("\n  🔴 ни одного инструмента: сравнивать будет нечего", flush=True)
        return 2
    save_snapshot(path, data)
    print_snapshot_summary(data)
    total = sum(len(item["targets"]) for item in data["instruments"].values())
    print(f"\n  Слепок сохранён: {path} ({total} строк целей)", flush=True)
    _log.info("Замер 0: слепок целей снят",
              z0_targets_snapshot_rows=total,
              z0_targets_instruments=len(data["instruments"]))
    print(" СЛЕПОК СНЯТ, код возврата 0.", flush=True)
    return 0


async def _compare(path: str, htf_rows: int) -> int:
    before = load_snapshot(path)
    frozen = datetime.fromisoformat(before["frozen_now"])
    print("\n=== ПЕРЕСЧЁТ И СРАВНЕНИЕ ===", flush=True)
    print(f"  момент расчёта взят ИЗ СЛЕПКА: {frozen.isoformat()}", flush=True)
    print("  (иначе окно целей сдвинулось бы за часы загрузки, и цели "
          "изменились бы сами — без отношения к старшим барам)", flush=True)
    after = await compute_targets(frozen)

    diffs = compare(before, after)
    changed = hourly_changed(before, after)
    total = sum(len(item["targets"]) for item in after["instruments"].values())

    print(f"\n  сверено строк целей:            {total}", flush=True)
    print(f"  старших баров в таблице:        {htf_rows}", flush=True)
    print(f"  различающихся строк:            {len(diffs)}", flush=True)

    if changed:
        print("\n  🟡 ЧАСОВОЙ РЯД ИЗМЕНИЛСЯ между слепком и пересчётом:", flush=True)
        for line in changed:
            print(f"      {line}", flush=True)
        print("      Расхождение целей у этих инструментов объясняется свежими "
              "ЧАСОВЫМИ свечами, а не старшими барами. Чтобы проверка отвечала "
              "на свой вопрос, слепок снимается ПОСЛЕ шага 2 и ДО шага 3.",
              flush=True)
    else:
        print("\n  Часовой ряд НЕ ИЗМЕНИЛСЯ (sha256 совпал по всем инструментам): "
              "любое расхождение ниже относится к старшим барам.", flush=True)

    if htf_rows == 0:
        print("\n  🟡 старших баров в таблице НЕТ: пересчёт сравнивается сам с "
              "собой, и его совпадение ничего не доказывает. Запускать сравнение "
              "надо ПОСЛЕ загрузки (scripts/load_htf_z0.py --apply).", flush=True)

    _log.info("Замер 0: сравнение целей выполнено",
              z0_targets_compared=total,
              z0_targets_mismatches=len(diffs),
              z0_targets_hourly_changed=len(changed))

    if diffs:
        print("\n  🔴 РАЗЛИЧАЮЩИЕСЯ СТРОКИ:", flush=True)
        for line in diffs[:200]:
            print(f"      {line}", flush=True)
        if len(diffs) > 200:
            print(f"      … и ещё {len(diffs) - 200} строк", flush=True)
        print("\n  Цели по вероятности — ПРОДАКШН-величина. Их изменение означает, "
              "что загрузка старших рядов повлияла на живую систему, а этап "
              "обещал обратное.", flush=True)
        print(" СРАВНЕНИЕ ЦЕЛЕЙ ЗАВЕРШЕНО, код возврата 2.", flush=True)
        return 2

    print("\n  🟢 цели совпали до последнего знака по всем инструментам, "
          "горизонтам и направлениям.", flush=True)
    print(" СРАВНЕНИЕ ЦЕЛЕЙ ЗАВЕРШЕНО, код возврата 0.", flush=True)
    return 0


async def _control(path: str, htf_rows: int) -> int:
    """Контрольный опыт: сравнение обязано уметь падать.

    Цели пересчитываются чтением БЕЗ фильтра по ``bar`` — то есть так, как
    читал бы код, забывший про масштаб. Расхождение обязано появиться. Его
    отсутствие означает, что «совпало» в основном режиме не доказывает ничего.
    """
    before = load_snapshot(path)
    frozen = datetime.fromisoformat(before["frozen_now"])
    print("\n=== КОНТРОЛЬНЫЙ ОПЫТ: чтение свечей БЕЗ фильтра по bar ===", flush=True)
    print("  Так читал бы код, забывший про масштаб. Расхождение ОБЯЗАНО "
          "появиться — иначе проверка нечувствительна к посторонним строкам.",
          flush=True)

    if htf_rows == 0:
        print("\n  ⚪ КОНТРОЛЬ НЕПРИМЕНИМ: старших баров в таблице нет, и чтение "
              "без фильтра вернуло бы ровно то же самое. Это НЕ «контроль "
              "пройден»: доказывать пока нечем. Запускать после загрузки.",
              flush=True)
        print(" КОНТРОЛЬНЫЙ ОПЫТ НЕ ВЫПОЛНЕН, код возврата 4.", flush=True)
        return 4

    spoiled = await compute_targets(frozen, reader=read_candles_without_bar_filter)
    diffs = compare(before, spoiled)
    print(f"\n  различающихся строк при чтении без фильтра: {len(diffs)}", flush=True)
    for line in diffs[:10]:
        print(f"      {line}", flush=True)
    if len(diffs) > 10:
        print(f"      … и ещё {len(diffs) - 10} строк", flush=True)

    _log.info("Замер 0: контрольный опыт целей",
              z0_targets_control_mismatches=len(diffs))

    if not diffs:
        print("\n  🔴 КОНТРОЛЬНЫЙ ОПЫТ НЕ УПАЛ: посторонние строки на цели не "
              "повлияли даже при чтении без фильтра. Значит сравнение целей "
              "нечувствительно, и его «совпало» ничего не доказывает.", flush=True)
        print(" КОНТРОЛЬНЫЙ ОПЫТ НЕ ВЫПОЛНЕН, код возврата 4.", flush=True)
        return 4

    print("\n  🟢 контрольный опыт УПАЛ, как и обязан: сравнение целей "
          "чувствительно к посторонним строкам в backtest.candles. Значит "
          "совпадение в режиме --compare — содержательный результат, а не "
          "свойство проверки.", flush=True)
    print(" КОНТРОЛЬНЫЙ ОПЫТ ВЫПОЛНЕН, код возврата 0.", flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Замер 0: слепок целей до загрузки старших рядов и сверка после"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", dest="snapshot", metavar="ФАЙЛ",
                       help="снять слепок целей (ДО загрузки старших рядов)")
    group.add_argument("--compare", dest="compare", metavar="ФАЙЛ",
                       help="пересчитать и сравнить со слепком: 0 — совпало, 2 — нет")
    group.add_argument("--control", dest="control", metavar="ФАЙЛ",
                       help="контрольный опыт: сравнение обязано уметь падать")
    args = parser.parse_args()
    for mode in ("snapshot", "compare", "control"):
        value = getattr(args, mode)
        if value:
            args.mode, args.path = mode, value
            break
    try:
        raise SystemExit(asyncio.run(run(args)))
    except SnapshotError as exc:
        print(f"\n 🔴 {exc}", flush=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
