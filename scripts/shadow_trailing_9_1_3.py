#!/usr/bin/env python3
"""ЧАСТЬ Б Этапа 9.1.3: теневой подвижный выход на ФАКТИЧЕСКИХ позициях.

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ, и на какой — нет.

Отвечает: сколько подвижный выход дал бы именно на тех сделках, которые система
ДЕЙСТВИТЕЛЬНО открыла — с их ценой входа, их слотом и их сроком.

НЕ отвечает на вопрос Этапа 8.10 и не складывается с ним. Там десятки тысяч
ГИПОТЕТИЧЕСКИХ пар «сигнал × горизонт» без порога вероятности; здесь десяток
реальных позиций с порогом 0.8. Смешивать их в одну сводную цифру запрещено
(§1 ТЗ), и этот скрипт ни одного числа части А не читает.

ГЛАВНОЕ ЧИСЛО ПЕЧАТАЕТСЯ ПЕРВЫМ, ДО ЛЮБЫХ СРЕДНИХ (§3.4 ТЗ). Подвижная цель
поднимает пол под уже полученной прибылью: сделка, ушедшая против сигнала до
предела убытка, до неё не доживает, и цель ни разу не сдвинется. Поэтому первый
вопрос — СКОЛЬКО ПОЗИЦИЙ МЕХАНИЗМ ВООБЩЕ ЗАДЕЛ. Если ни одной, средний прирост
равен нулю ПО ПОСТРОЕНИЮ, и это и есть ответ владельцу, а не отсутствие ответа.

КОНТРОЛЬ БЛОКИРУЮЩИЙ (§3.3 ТЗ). Тот же теневой прогон живым правилом обязан
воспроизвести уже записанное в ``positions`` — причину, бар, цену и итог. Не
воспроизвёл хотя бы по одной позиции — СРАВНЕНИЕ ВАРИАНТОВ НЕ ПУБЛИКУЕТСЯ
ВОВСЕ, код возврата 2. Если расчёт не умеет повторить случившееся, его числа по
НЕ случившимся вариантам не стоят ничего. Расширять допуск сравнения запрещено.

ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ ЗАПРЕЩЁН ПРЯМО (§0 ТЗ). Результат — таблицы чисел
и честное описание их статистической силы. Слова «лучше» и «хуже» при малой
выборке не печатаются вовсе: при десятке сделок доверительный интервал шире
любой наблюдаемой разницы, и сравнительная степень была бы выдумкой.

ЭТАП ЗАМЕРНЫЙ. Пишется ровно одна таблица — ``position_trailing_shadow``.
``positions``, ``signals``, ``signal_evaluations``, ``signal_targets``,
``risk_targets``, ``signal_outcomes_barrier``, ``strategy_outcomes`` и
``trailing_outcomes`` не изменяются ни одной строкой.

КОДЫ ВОЗВРАТА:
  0 — расчёт выполнен, контроль совпал;
  2 — контроль не совпал, сравнение НЕ опубликовано;
  3 — выборка пуста.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА. Каталог ``scripts/`` попадает только в образ
``backtest`` (§11.4 ТЗ), поэтому запускать через него:

    # 1. вхолостую — ни одной записи в базу:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/shadow_trailing_9_1_3.py

    # 2. если контроль совпал (код 0) — с записью:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/shadow_trailing_9_1_3.py --apply

Отдельным cron НЕ оформляется: это разовый замер, а не ночной расчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog  # noqa: E402

from src.barrier.outcomes import Bar  # noqa: E402
from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402
from src.shadow.trailing import (  # noqa: E402
    CONTROL_VARIANT,
    ShadowOutcome,
    closing_moment,
    resolve_position,
    variant_name,
)
from src.trailing.rule import ACTIVATION_RATIOS, TRAILING_VARIANTS  # noqa: E402

_log = structlog.get_logger().bind(component="shadow_trailing")

# Ниже этого числа наблюдений вывод о преимуществе не делается ВООБЩЕ (§3.4).
MIN_SAMPLE = 30

# Причины выхода живого правила, по которым идёт разбивка ЧИСЛА 2.
LIVE_REASONS: tuple[str, ...] = ("target", "stop", "timeout", "ambiguous")

# Точность сверки контроля. Числа хранятся NUMERIC(20,8) и NUMERIC(12,6);
# сравнение идёт по тому знаку, с которым величина ЛЕЖИТ В БАЗЕ, а не по
# «примерно равно». Расширять этот допуск запрещено (§3.3 ТЗ).
PRICE_PLACES = 8
PCT_PLACES = 6


def parse_since(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` → полночь UTC. Нет значения — нет и нижней границы."""
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def _round(value: Any, places: int) -> float | None:
    """Округление до знака хранения. ``None`` остаётся ``None``, а не нулём."""
    if value is None:
        return None
    return round(float(value), places)


def compare_control(
    position: dict[str, Any], control: ShadowOutcome | None
) -> list[str]:
    """Расхождения контроля с фактом. Пустой список — совпало.

    Сверяются четыре величины: причина выхода, МОМЕНТ ЗАКРЫТИЯ, цена выхода и
    итог в процентах. Каждая сравнивается с тем числом знаков, с которым она
    ЛЕЖИТ В БАЗЕ: сравнение «примерно» пропустило бы ровно то расхождение, ради
    которого сверка и делается.

    ОШИБКА В ТЗ, НАЗВАННАЯ, А НЕ ОБОЙДЁННАЯ. §3.3 ТЗ требует сверять
    ``exit_bar_ts``, но КОЛОНКИ С ТАКИМ ИМЕНЕМ В ``positions`` НЕТ — и §11.3
    того же ТЗ приводит полный состав таблицы, где её действительно нет.
    Требование противоречило само себе. Момент закрытия лежит в ``closed_at``.

    И СВЕРЯЕТСЯ ОН ЧЕРЕЗ :func:`closing_moment`, а не напрямую. ``check_exit``
    возвращает время ОТКРЫТИЯ бара выхода, а ``runner`` пишет в ``closed_at``
    время его ЗАКРЫТИЯ. Первая редакция сравнивала одно с другим, и на боевой
    базе 31.08.2026 все одиннадцать позиций разошлись ровно на 60 секунд — факт
    позже пересчёта. Допуск при этом остаётся НУЛЕВЫМ: изменилось не «насколько
    близко», а «что с чем».

    ``None`` вместо контроля — тоже расхождение, и названное словами: живое
    правило на этом ряде исхода не дало, а в базе исход есть.
    """
    if control is None:
        return ["живое правило не дало исхода на прочитанном ряде свечей"]
    problems: list[str] = []
    if control.exit_reason != str(position["exit_reason"]):
        problems.append(
            f"exit_reason: факт {position['exit_reason']}, "
            f"пересчёт {control.exit_reason}"
        )
    closed = closing_moment(control.exit_bar_ts, str(position["resolution"]))
    if closed != position["closed_at"]:
        problems.append(
            f"closed_at: факт {position['closed_at']}, пересчёт {closed} "
            f"(бар выхода {control.exit_bar_ts})"
        )
    fact_price = _round(position["exit_price"], PRICE_PLACES)
    if _round(control.exit_price, PRICE_PLACES) != fact_price:
        problems.append(
            f"exit_price: факт {fact_price}, "
            f"пересчёт {_round(control.exit_price, PRICE_PLACES)}"
        )
    fact_pct = _round(position["net_pnl_pct"], PCT_PLACES)
    if _round(control.net_pnl_pct, PCT_PLACES) != fact_pct:
        problems.append(
            f"net_pnl_pct: факт {fact_pct}, "
            f"пересчёт {_round(control.net_pnl_pct, PCT_PLACES)}"
        )
    return problems


def armed_counts(
    shadows: dict[int, list[ShadowOutcome]]
) -> dict[float, int]:
    """ЧИСЛО 1: сколько позиций задето на каждом уровне включения A.

    Позиция считается задетой при данном A, если задет ХОТЯ БЫ ОДИН вариант с
    этим A. Величина отката R на задетость не влияет — включение зависит только
    от того, дошла ли вершина до порога, — и это проверяется: у всех трёх R с
    одним A признак обязан совпасть.
    """
    counts: dict[float, int] = {a: 0 for a in ACTIVATION_RATIOS}
    for rows in shadows.values():
        by_activation: dict[float, set[bool]] = {}
        for row in rows:
            if row.variant == CONTROL_VARIANT or row.activation_frac is None:
                continue
            by_activation.setdefault(row.activation_frac, set()).add(row.armed)
        for activation, flags in by_activation.items():
            if len(flags) > 1:
                raise AssertionError(
                    f"A={activation}: задетость зависит от R — правило разошлось"
                )
            if flags == {True}:
                counts[activation] = counts.get(activation, 0) + 1
    return counts


def armed_by_reason(
    positions: list[dict[str, Any]], shadows: dict[int, list[ShadowOutcome]]
) -> dict[str, dict[str, int]]:
    """ЧИСЛО 2: разбивка задетости по ФАКТИЧЕСКОЙ причине выхода.

    Ожидание, записанное ДО прогона (правило проекта — предсказание фиксируется
    раньше расчёта): у позиций, закрытых по ``stop``, задетых должно быть мало
    или ноль. Сделка, ушедшая против сигнала, до порога включения не доходит.
    Если их окажется заметно — это НАХОДКА, требующая объяснения, а не повод
    порадоваться.
    """
    table: dict[str, dict[str, int]] = {
        reason: {"total": 0, **{f"A{a:.2f}": 0 for a in ACTIVATION_RATIOS}}
        for reason in LIVE_REASONS
    }
    for position in positions:
        reason = str(position["exit_reason"])
        if reason not in table:
            table[reason] = {
                "total": 0, **{f"A{a:.2f}": 0 for a in ACTIVATION_RATIOS}
            }
        table[reason]["total"] += 1
        rows = shadows.get(int(position["id"]), [])
        for activation in ACTIVATION_RATIOS:
            hit = any(
                row.armed for row in rows
                if row.activation_frac == activation
            )
            if hit:
                table[reason][f"A{activation:.2f}"] += 1
    return table


def variant_deltas(
    positions: list[dict[str, Any]], shadows: dict[int, list[ShadowOutcome]]
) -> list[dict[str, Any]]:
    """ЧИСЛО 3: по каждому из 12 вариантов — прирост против ФАКТА.

    Сравнение идёт с фактически записанным итогом позиции, а не с контрольной
    строкой пересчёта: они совпадают (иначе прогон уже остановился бы с кодом
    2), но фактом является запись в ``positions``.

    ПОЗИЦИИ С НЕИЗМЕРЕННЫМ ИСХОДОМ ВАРИАНТА (``ambiguous``, ``no_data``) В
    СРЕДНЕЕ НЕ ВХОДЯТ и считаются отдельно. Подставить им ноль значило бы
    утверждать «вариант не изменил результат», чего никто не измерял.
    """
    out: list[dict[str, Any]] = []
    for activation, retrace in TRAILING_VARIANTS:
        name = variant_name(activation, retrace)
        deltas: list[float] = []
        usd = 0.0
        better = worse = same = unmeasured = 0
        for position in positions:
            row = next(
                (r for r in shadows.get(int(position["id"]), [])
                 if r.variant == name),
                None,
            )
            if row is None or row.net_pnl_pct is None:
                unmeasured += 1
                continue
            delta = float(row.net_pnl_pct) - float(position["net_pnl_pct"])
            deltas.append(delta)
            usd += float(row.net_pnl_usd or 0.0) - float(position["net_pnl_usd"])
            if delta > 0:
                better += 1
            elif delta < 0:
                worse += 1
            else:
                same += 1
        out.append({
            "variant": name,
            "activation": activation,
            "retrace": retrace,
            "n": len(deltas),
            "mean_delta_pct": (sum(deltas) / len(deltas)) if deltas else None,
            "sum_delta_usd": usd,
            "improved": better,
            "worsened": worse,
            "unchanged": same,
            "unmeasured": unmeasured,
        })
    return out


def _fmt(value: float | None, places: int = 4) -> str:
    return "—" if value is None else f"{value:+.{places}f}"


def print_power_warning(n: int) -> None:
    """ЧИСЛО 4: статистическая сила, честно (§3.4 ТЗ).

    При ``n < 30`` строка печатается ЗАГЛАВНЫМИ и ни одно слово «лучше» или
    «хуже» в выводах не появляется. Это не осторожность ради вида: при десятке
    сделок доверительный интервал разницы шире любой из наблюдаемых разниц, и
    сравнительная степень была бы утверждением, которого данные не несут.
    """
    print()
    print(f"  Наблюдений в выборке: N = {n}")
    if n < MIN_SAMPLE:
        print()
        print("  ПРИ ТАКОМ N ДОВЕРИТЕЛЬНЫЙ ИНТЕРВАЛ РАЗНИЦЫ ШИРЕ ЛЮБОЙ ИЗ")
        print("  НАБЛЮДАЕМЫХ РАЗНИЦ. ВЫВОД О ПРЕИМУЩЕСТВЕ ОДНОГО ВАРИАНТА НАД")
        print("  ДРУГИМ НА ЭТИХ ДАННЫХ СДЕЛАТЬ НЕЛЬЗЯ. ЧИСЛА НИЖЕ — ОПИСАНИЕ")
        print("  ТОГО, ЧТО СЛУЧИЛОСЬ С ЭТИМИ КОНКРЕТНЫМИ СДЕЛКАМИ, И НИЧЕГО")
        print("  БОЛЕЕ: ОНИ НЕ ПРЕДСКАЗЫВАЮТ СЛЕДУЮЩИЕ.")
    else:
        print("  Выборка достигла порога, при котором интервал имеет смысл")
        print(f"  считать (N >= {MIN_SAMPLE}). Интервалы — часть А, §2.3.")


async def _load_bars(position: dict[str, Any], now: datetime) -> list[Bar]:
    """Ряд свечей позиции: от бара после входа до бара срока, ТОЛЬКО ЗАКРЫТЫЕ.

    ГОДНОСТЬ БАРА — ТО ЖЕ ПРАВИЛО, ЧТО ЗАКРЫЛО ДЕФЕКТ 8.10.1 (§3.2 ТЗ):
    верхняя граница чтения не дальше последнего заведомо закрытого бара.
    Запас берётся ``settle_seconds()`` — ИМПОРТОМ, а не копией формулы: копия
    формулы уже была причиной дефекта в этом проекте.

    Для закрытой позиции срок давно в прошлом, и ограничение обычно ни на что
    не влияет — но именно «обычно» и делает копию формулы опасной.
    """
    read_until = min(
        position["deadline_at"],
        datetime.fromtimestamp(now.timestamp() - settle_seconds(), tz=UTC),
    )
    raw = await db.get_ohlcv_bars(
        int(position["instrument_id"]),
        str(position["resolution"]),
        position["opened_at"],
        read_until,
    )
    return [
        Bar(ts=item["ts"], high=float(item["high"]),
            low=float(item["low"]), close=float(item["close"]))
        for item in raw
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Теневой подвижный выход на фактических виртуальных позициях "
            "(Этап 9.1.3, часть Б). Без --apply — только печать."
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="записать результаты в position_trailing_shadow",
    )
    parser.add_argument(
        "--since", default=None, metavar="YYYY-MM-DD",
        help="нижняя граница по opened_at; по умолчанию границы нет",
    )
    parser.add_argument(
        "--json", default=None, metavar="PATH",
        help="дополнительно сохранить сводку машиночитаемо",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        since = parse_since(args.since)
    except ValueError:
        parser.error(f"--since разобрать не удалось: {args.since!r} (нужен YYYY-MM-DD)")

    now = datetime.now(UTC)
    await db.connect()
    try:
        return await _run(args, since, now)
    finally:
        await db.close()


async def _run(args: argparse.Namespace, since: datetime | None,
               now: datetime) -> int:
    counts = await db.count_positions_for_shadow(since=since)
    positions = await db.get_positions_for_shadow(since=since)

    print("=" * 78)
    print(" ЧАСТЬ Б. ТЕНЕВОЙ ПОДВИЖНЫЙ ВЫХОД НА ФАКТИЧЕСКИХ ПОЗИЦИЯХ (9.1.3)")
    print("=" * 78)
    print(f"  Закрытых позиций всего:       {counts['closed_total']}")
    print(f"  Из них исключено (data_gap):  {counts['data_gap']}")
    print(f"  Открытых (в замер не идут):   {counts['still_open']}")
    print(f"  В выборке замера:             {len(positions)}")
    print(f"  Запас закрытия бара:          {settle_seconds()} с")

    _log.info(
        "Теневой замер: выборка",
        shadow_positions_total=len(positions),
        shadow_positions_skipped_gap=counts["data_gap"],
        shadow_positions_open=counts["still_open"],
        settle_seconds=settle_seconds(),
    )

    if not positions:
        print()
        print("  Выборка пуста: считать нечего.")
        return 3

    # --- Пересчёт ---------------------------------------------------------
    shadows: dict[int, list[ShadowOutcome]] = {}
    mismatches: list[tuple[int, list[str]]] = []
    failures: list[tuple[int, str]] = []
    for position in positions:
        position_id = int(position["id"])
        bars = await _load_bars(position, now)
        try:
            shadow = resolve_position(
                bars,
                opened_at=position["opened_at"],
                deadline_at=position["deadline_at"],
                horizon_h=int(position["horizon_h"]),
                entry_price=float(position["entry_price"]),
                target_pct=float(position["target_pct"]),
                stop_pct=float(position["stop_pct"]),
                cost_pct=float(position["cost_pct"]),
                notional_usd=float(position["notional_usd"]),
                resolution=str(position["resolution"]),
                direction=str(position["side"]),
            )
        except (ValueError, AssertionError) as exc:
            # Расчёт отказался считать: окно съехало или проходы разошлись.
            # Это то же по смыслу, что расхождение контроля, и молча пропустить
            # такую позицию нельзя.
            failures.append((position_id, f"{type(exc).__name__}: {exc}"))
            continue
        problems = compare_control(position, shadow.control)
        if problems:
            mismatches.append((position_id, problems))
        shadows[position_id] = shadow.rows

    # СВЕРЕНО — ЭТО ЧИСЛО ПОЗИЦИЙ, ПО КОТОРЫМ СВЕРКА ВООБЩЕ ПРОВОДИЛАСЬ, а не
    # число удавшихся. Первая редакция вычитала отсюда позиции, на которых
    # расчёт упал с исключением, но в расхождения их всё равно засчитывала — и
    # на боевом прогоне 31.08.2026 напечатала невозможное: сверено 11,
    # разошлось 12. Позиция, расчёт которой не удался, СВЕРЕНА (попытка была) и
    # РАЗОШЛАСЬ (совпадения не получено); в оба счётчика она входит одинаково.
    compared = len(positions)
    mismatched = len(mismatches) + len(failures)
    if mismatched > compared:
        # Печатать невозможное число хуже, чем упасть: увидев «11 и 12», человек
        # начинает сомневаться во ВСЁМ выводе, и правильно делает.
        raise AssertionError(
            f"расхождений {mismatched} при {compared} сверенных — счётчики "
            "разошлись; печатать такой отчёт нельзя"
        )
    _log.info(
        "Теневой замер: контроль",
        shadow_control_compared=compared,
        shadow_control_mismatched=mismatched,
    )

    print()
    print("-" * 78)
    print(" КОНТРОЛЬ: живое правило против записанного факта")
    print("-" * 78)
    print(f"  Сверено позиций:   {compared}")
    print(f"  Разошлось:         {mismatched}")

    # --- БЛОКИРУЮЩЕЕ УСЛОВИЕ (§3.3 ТЗ) ------------------------------------
    if mismatched:
        print()
        print("=" * 78)
        print(" ОТКАЗ: КОНТРОЛЬ НЕ СОВПАЛ — СРАВНЕНИЕ ВАРИАНТОВ НЕ ПУБЛИКУЕТСЯ")
        print("=" * 78)
        for position_id, reason in failures:
            print(f"  позиция {position_id}: {reason}")
        for position_id, problems in mismatches:
            print(f"  позиция {position_id}:")
            for problem in problems:
                print(f"      {problem}")
        print()
        print("  Если пересчёт не умеет повторить УЖЕ СЛУЧИВШЕЕСЯ, его числа по")
        print("  НЕ случившимся вариантам не стоят ничего. Расширять допуск")
        print("  сравнения запрещено (§3.3 ТЗ): расхождение надо расследовать.")
        print("  Ни одной строки не записано.")
        return 2

    # --- ЧИСЛО 1: сколько позиций механизм вообще задел -------------------
    armed = armed_counts(shadows)
    total = len(positions)
    print()
    print("-" * 78)
    print(" ЧИСЛО 1. СКОЛЬКО ПОЗИЦИЙ МЕХАНИЗМ ВООБЩЕ ЗАДЕЛ")
    print("-" * 78)
    print("  Подвижная цель поднимает пол под УЖЕ полученной прибылью. Сделка,")
    print("  ушедшая против сигнала до предела, до неё не доживает. Поэтому это")
    print("  число отвечает на вопрос владельца прямее любого среднего.")
    print()
    print(f"  {'уровень включения A':<24}{'задето позиций':>16}{'доля':>10}")
    print("  " + "-" * 50)
    for activation in ACTIVATION_RATIOS:
        hit = armed.get(activation, 0)
        share = 100.0 * hit / total if total else 0.0
        print(f"  A={activation:<22.2f}{hit:>10} из {total:<3}{share:>9.1f}%")
    _log.info("Теневой замер: задетость", shadow_armed_a025=armed.get(0.25, 0))

    if not any(armed.values()):
        print()
        print("  НИ ОДНА ПОЗИЦИЯ НЕ ЗАДЕТА НИ НА ОДНОМ УРОВНЕ ВКЛЮЧЕНИЯ.")
        print("  Значит средний прирост равен нулю ПО ПОСТРОЕНИЮ, а не по")
        print("  совпадению: механизму нечего было двигать. Это и есть ответ,")
        print("  а не отсутствие ответа.")

    # --- ЧИСЛО 2: разбивка по фактической причине выхода ------------------
    table = armed_by_reason(positions, shadows)
    print()
    print("-" * 78)
    print(" ЧИСЛО 2. ЗАДЕТОСТЬ ПО ФАКТИЧЕСКОЙ ПРИЧИНЕ ВЫХОДА")
    print("-" * 78)
    header = f"  {'причина':<12}{'всего':>8}"
    for activation in ACTIVATION_RATIOS:
        header += f"{'A=' + format(activation, '.2f'):>10}"
    print(header)
    print("  " + "-" * (12 + 8 + 10 * len(ACTIVATION_RATIOS)))
    for reason, row in table.items():
        line = f"  {reason:<12}{row['total']:>8}"
        for activation in ACTIVATION_RATIOS:
            line += f"{row[f'A{activation:.2f}']:>10}"
        print(line)
    stop_armed = sum(
        table.get("stop", {}).get(f"A{a:.2f}", 0) for a in ACTIVATION_RATIOS
    )
    print()
    print("  Ожидание, записанное ДО прогона: у закрытых по stop задетых мало")
    print("  или ноль — сделка, ушедшая против сигнала, до порога не доходит.")
    print(f"  Фактически задетых среди stop (сумма по A): {stop_armed}")
    if stop_armed:
        print("  ЭТО НАХОДКА, ТРЕБУЮЩАЯ ОБЪЯСНЕНИЯ, а не повод порадоваться:")
        print("  цена успела сходить вверх до порога и лишь затем упасть.")

    # --- ЧИСЛО 4 печатается ДО таблицы приростов --------------------------
    print()
    print("-" * 78)
    print(" ЧИСЛО 4. СТАТИСТИЧЕСКАЯ СИЛА")
    print("-" * 78)
    print_power_warning(total)

    # --- ЧИСЛО 3: приросты по вариантам -----------------------------------
    deltas = variant_deltas(positions, shadows)
    print()
    print("-" * 78)
    print(" ЧИСЛО 3. ПРИРОСТ ПО КАЖДОМУ ИЗ 12 ВАРИАНТОВ, ПРОТИВ ФАКТА")
    print("-" * 78)
    print(f"  {'вариант':<14}{'N':>4}{'ср. прирост, п.п.':>20}"
          f"{'сумма, $':>12}{'улучш.':>8}{'ухудш.':>8}{'без изм.':>10}"
          f"{'не изм-но':>11}")
    print("  " + "-" * 87)
    for row in deltas:
        print(
            f"  {row['variant']:<14}{row['n']:>4}"
            f"{_fmt(row['mean_delta_pct']):>20}"
            f"{_fmt(row['sum_delta_usd'], 4):>12}"
            f"{row['improved']:>8}{row['worsened']:>8}"
            f"{row['unchanged']:>10}{row['unmeasured']:>11}"
        )
    print()
    print("  «не изм-но» — позиции, у которых исход варианта НЕ ИЗМЕРЕН")
    print("  (ambiguous или no_data). В среднее они не входят: подставить им")
    print("  ноль значило бы утверждать «вариант ничего не изменил».")
    print()
    print("  ВЫБОР ПАРАМЕТРА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ")
    print("  (§0 ТЗ). Числа выше описывают эти конкретные сделки.")

    # --- Запись -----------------------------------------------------------
    written = 0
    if args.apply:
        if not await db.position_trailing_shadow_exists():
            print()
            print("  ОТКАЗ: таблицы position_trailing_shadow нет.")
            print("  Примените миграцию db/migrations/021_position_trailing_shadow.sql")
            return 2
        rows = [
            {
                "position_id": position_id,
                "variant": item.variant,
                "activation_frac": item.activation_frac,
                "pullback_frac": item.pullback_frac,
                "armed": item.armed,
                "armed_at": item.armed_at,
                "exit_reason": item.exit_reason,
                "exit_bar_ts": item.exit_bar_ts,
                "exit_price": item.exit_price,
                "net_pnl_pct": item.net_pnl_pct,
                "net_pnl_usd": item.net_pnl_usd,
                "bars_used": item.bars_used,
                "resolution": item.resolution,
                "logic_version": int(settings.LOGIC_VERSION),
            }
            for position_id, items in sorted(shadows.items())
            for item in items
        ]
        written = await db.save_position_trailing_shadow(rows)
        print()
        print(f"  Записано строк: {written}")
    else:
        print()
        print("  Ничего не записано: без --apply скрипт только считает.")
    _log.info("Теневой замер: запись", shadow_rows_written=written)

    if args.json:
        summary = {
            "generated_at": now.isoformat(timespec="seconds"),
            "positions_total": total,
            "positions_skipped_gap": counts["data_gap"],
            "positions_open": counts["still_open"],
            "control_compared": compared,
            "control_mismatched": mismatched,
            "settle_seconds": settle_seconds(),
            "armed_by_activation": {f"{a:.2f}": armed.get(a, 0)
                                    for a in ACTIVATION_RATIOS},
            "armed_by_reason": table,
            "variants": deltas,
            "rows_written": written,
            "min_sample_for_conclusions": MIN_SAMPLE,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
        print(f"  Сводка сохранена: {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
