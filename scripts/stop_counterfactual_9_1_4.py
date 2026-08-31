#!/usr/bin/env python3
"""ЭТАП 9.1.4: что было бы с закрытыми позициями при ДРУГИХ уровнях предела.

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ, и на какой — нет.

Владелец спросил: «торговля спотовая, ликвидации нет, портфель разнесён по пяти
токенам — нужен ли нам вообще предел убытка?» Рассуждение верно в том, что на
споте позицию никто не закроет принудительно. Но у предела есть вторая роль,
которая в рассуждение не вошла: ОН ОСВОБОЖДАЕТ СЛОТ. Слотов пять, и повисшая в
минусе позиция стоит не только своего убытка, но и всех входов по этому
инструменту за время её висения. Здесь считаются обе цены — в процентах и в
слотах — на тех сделках, которые система ДЕЙСТВИТЕЛЬНО открыла.

НЕ отвечает на вопрос «а что было бы дальше с теми входами, которые не
состоялись бы». Чтобы узнать их итог, пришлось бы проиграть целиком ДРУГУЮ
историю позиций, где каждый вход меняет занятость следующих. Это другой этап;
здесь заблокированные входы только СЧИТАЮТСЯ (§3 ТЗ).

ПЯТЬ ТОЧЕК, А НЕ ДВЕ. Ответ «убрать или оставить» может оказаться неверным
обеими сторонами: возможно, предел нужен, но стоит слишком близко. Поэтому
считаются control (фактический уровень позиции), 1.5%, 2.0%, 3.0% и вариант без
предела вовсе — кривая по пяти точкам это покажет, бинарный ответ нет.

ПРАВИЛО ВЫХОДА ЗДЕСЬ НЕ ПЕРЕПИСАНО НИ ОДНОЙ СТРОКОЙ. Все пять вариантов
считаются вызовом ``src/positions/rules.check_exit`` — ровно той функции,
которой закрыты настоящие позиции, — с разным ``stop_price``. Своё правило
писать было нельзя: тогда контроль перестал бы быть контролем, потому что
сверял бы факт не с тем расчётом, которым считаются варианты.

КОНТРОЛЬ БЛОКИРУЮЩИЙ (§2.2 ТЗ). Вариант ``control`` обязан воспроизвести
записанное в ``positions`` — причину, момент закрытия, цену и итог — до
последнего знака хранения. Не воспроизвёл хотя бы по одной позиции — СРАВНЕНИЕ
ВАРИАНТОВ НЕ ПУБЛИКУЕТСЯ ВОВСЕ, код возврата 2. Если расчёт не умеет повторить
УЖЕ СЛУЧИВШЕЕСЯ, его числа по НЕ случившимся вариантам не стоят ничего.
Расширять допуск сравнения запрещено.

ЭТАП ЗАМЕРНЫЙ, И ГРАНИЦА ЖЁСТКАЯ (§1 ТЗ). Ни одно правило системы не меняется:
LOGIC_VERSION остаётся 5, ``BARRIER_STOP_PCT`` не трогается, предел убытка
остаётся на месте. РЕКОМЕНДАЦИЯ «УБРАТЬ ПРЕДЕЛ» ИЛИ «ПОСТАВИТЬ ЕГО НА X%» ЭТИМ
ЭТАПОМ ЗАПРЕЩЕНА и скриптом не печатается ни в каком виде. Пишется ровно одна
таблица — ``position_stop_shadow``.

КОДЫ ВОЗВРАТА:
  0 — расчёт выполнен, контроль совпал;
  2 — контроль не совпал (или нет таблицы при ``--apply``), сравнение НЕ
      опубликовано;
  3 — выборка пуста.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА. Каталог ``scripts/`` попадает только в образ
``backtest``, поэтому запускать через него:

    # 1. вхолостую — ни одной записи в базу:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/stop_counterfactual_9_1_4.py

    # 2. если контроль совпал (код 0) — с записью:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/stop_counterfactual_9_1_4.py --apply

Отдельным cron НЕ оформляется: это разовый замер, а не ночной расчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog  # noqa: E402

from src.barrier.outcomes import OUTCOME_NO_DATA  # noqa: E402
from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402
from src.positions import rules as position_rules  # noqa: E402
from src.shadow.trailing import closing_moment  # noqa: E402

_log = structlog.get_logger().bind(component="stop_counterfactual")

# Ниже этого числа наблюдений вывод о преимуществе не делается ВООБЩЕ (§3 ТЗ,
# ЧИСЛО 4). Значение то же, что у Этапа 9.1.3: порог не свой у каждого замера.
MIN_SAMPLE = 30

# Точность сверки контроля. Числа хранятся NUMERIC(20,8) и NUMERIC(12,6);
# сравнение идёт по тому знаку, с которым величина ЛЕЖИТ В БАЗЕ, а не по
# «примерно равно». Расширять этот допуск запрещено (§2.2 ТЗ).
PRICE_PLACES = 8
PCT_PLACES = 6

CONTROL_VARIANT = "control"
NO_STOP_VARIANT = "no_stop"

# Три пересчётных уровня предела, в процентах. Фактический уровень позиции
# сюда не входит: он берётся из самой позиции вариантом ``control``.
STOP_LEVELS: tuple[float, ...] = (1.5, 2.0, 3.0)

# КАК ОТКЛЮЧАЕТСЯ ПРЕДЕЛ, И ПОЧЕМУ ИМЕННО ТАК (§2.1 ТЗ требует ДОКАЗАТЬ
# эквивалентность, а не объявить её).
#
# ``check_exit`` пользуется ``stop_price`` ровно в трёх местах: в ``_touches``
# (``bar.low <= stop_price``) и в двух ветках выхода — ``ambiguous`` и ``stop``,
# — куда та же величина уходит ценой выхода. Значение 0.0 делает сравнение
# ``bar.low <= 0`` ложным на любом баре с положительным минимумом, обе ветки
# становятся недостижимы, и ``stop_price`` перестаёт влиять на ответ вовсе:
# остаются цель и срок. Это и есть «предела нет».
#
# У доказательства РОВНО ОДНА ПОСЫЛКА — положительный минимум каждого бара, — и
# она не предполагается, а проверяется на каждом ряде: :func:`assert_no_stop`.
# Бар с ``low <= 0`` означал бы либо испорченный ряд, либо инструмент с
# неположительной ценой; молча посчитать по нему «выход без предела» нельзя.
#
# КРАЙНИЕ ЗНАЧЕНИЯ ``check_exit`` НЕ ОБРАБАТЫВАЕТ ОСОБО, и это проверено по
# тексту правила: ``stop_price`` не проверяется им ни на знак, ни на ноль
# (проверяется только ``entry_price > 0``). А вот ``rules.levels`` ноль
# ОТВЕРГАЕТ — ``stop_pct <= 0`` там ValueError, — поэтому уровень цели для
# варианта без предела берётся тем же вызовом ``levels`` с ФАКТИЧЕСКИМ
# ``stop_pct`` позиции, а вернувшийся уровень предела отбрасывается. Обходить
# проверку ``levels`` подстановкой нуля значило бы спорить с правилом вместо
# того, чтобы им пользоваться.
NO_STOP_PRICE = 0.0

# Исход, которого НЕ БЫВАЕТ у правила: признание того, что исход не измерен.
# ``check_exit`` возвращает None, когда бар срока не предъявлен и ни один
# уровень не задет — неизвестно, кончилось окно или данные не подъехали. Имя
# то же, что в ``src/barrier/outcomes`` и в миграции 021.
UNMEASURED = OUTCOME_NO_DATA

# Причины выхода, которые возвращает САМО правило (``src/positions/rules.py``).
# Перечень сверен с кодом, а не со списком в тексте ТЗ (§4 ТЗ этого и требует).
RULE_REASONS: tuple[str, ...] = (
    position_rules.EXIT_TARGET,
    position_rules.EXIT_STOP,
    position_rules.EXIT_TIMEOUT,
    position_rules.EXIT_AMBIGUOUS,
)
SHADOW_REASONS: tuple[str, ...] = (*RULE_REASONS, UNMEASURED)

SECONDS_IN_HOUR = 3600.0


def peak_rss_mb() -> float:
    """Пик собственной памяти процесса, МБ. ``ru_maxrss`` на Linux — в КБ."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def stop_variant_name(level: float) -> str:
    """Имя варианта по уровню предела: ``stop_1.5`` (§2 ТЗ)."""
    return f"stop_{float(level):.1f}"


def variant_names() -> tuple[str, ...]:
    """Пять вариантов в порядке печати: контроль, три уровня, без предела."""
    return (
        CONTROL_VARIANT,
        *(stop_variant_name(level) for level in STOP_LEVELS),
        NO_STOP_VARIANT,
    )


def variant_stop_pct(variant: str, position_stop_pct: float) -> float | None:
    """Уровень предела варианта. ``None`` — предела нет вовсе.

    У контроля уровень берётся ИЗ САМОЙ ПОЗИЦИИ, а не из константы этапа:
    подставить сюда ``BARRIER_STOP_PCT`` значило бы сверять факт с настройкой
    сегодняшнего дня, тогда как позиция велась по своему записанному уровню.
    """
    if variant == CONTROL_VARIANT:
        return float(position_stop_pct)
    if variant == NO_STOP_VARIANT:
        return None
    for level in STOP_LEVELS:
        if stop_variant_name(level) == variant:
            return float(level)
    raise ValueError(f"неизвестный вариант предела: {variant!r}")


def stop_width(stop_pct: float | None) -> float:
    """Ширина предела для упорядочивания. Без предела — бесконечность."""
    return float("inf") if stop_pct is None else float(stop_pct)


@dataclass(frozen=True)
class VariantOutcome:
    """Исход одной позиции при одном уровне предела — строка таблицы замера.

    ``closed_at`` — момент ЗАКРЫТИЯ бара выхода, то есть то, что живой сервис
    пишет в ``positions.closed_at``, а не время открытия бара, которое
    возвращает правило. Разница ровно в один бар, и на Этапе 9.1.3 она стоила
    одиннадцати ложных расхождений (см. :func:`src.shadow.trailing.closing_moment`).
    """

    variant: str
    stop_pct: float | None
    exit_reason: str
    exit_bar_ts: datetime | None
    exit_price: float | None
    net_pnl_pct: float | None
    net_pnl_usd: float | None
    closed_at: datetime | None
    held_sec: int
    extra_held_sec: int
    bars_used: int
    resolution: str

    @property
    def measured(self) -> bool:
        """Исход измерен, а не признан неизмеримым."""
        return self.exit_reason != UNMEASURED


def assert_no_stop(bars: list[position_rules.Bar]) -> None:
    """Единственная посылка эквивалентности ``stop_price = 0`` (см. заголовок).

    Проверяется, а не предполагается: бар с неположительным минимумом сделал бы
    сравнение ``bar.low <= 0`` истинным, и «вариант без предела» тихо
    превратился бы в вариант с пределом на нуле.
    """
    for bar in bars:
        if float(bar.low) <= 0.0:
            raise ValueError(
                "вариант без предела неприменим к ряду с неположительным "
                f"минимумом бара: ts={bar.ts.isoformat()}, low={bar.low}"
            )


def resolve_variant(
    bars: list[position_rules.Bar],
    *,
    variant: str,
    position_stop_pct: float,
    entry_price: float,
    target_pct: float,
    cost_pct: float,
    notional_usd: float,
    opened_at: datetime,
    deadline_at: datetime,
    fact_closed_at: datetime,
    resolution: str,
) -> VariantOutcome:
    """Исход одной позиции при одном пределе. ЧИСТАЯ функция.

    Ни базы, ни сети, ни ``datetime.now()``: всё состояние приходит
    параметрами (§5 ТЗ). Это условие проверяемости — правило проверяется на
    придуманных рядах с заранее известным ответом.

    ``bars`` — ЗАКРЫТЫЕ бары по возрастанию времени открытия. Что такое
    «закрытый», знает только вызывающий (задержка коллектора, ``settle_seconds``),
    и правило об этом не спрашивает.
    """
    stop_pct = variant_stop_pct(variant, position_stop_pct)
    # Уровень цели считается ТОЙ ЖЕ ``rules.levels``, что считала его при
    # открытии позиции, и от той же фактической цены входа.
    target_price, own_stop_price = position_rules.levels(
        entry_price, target_pct, position_stop_pct
    )
    if stop_pct is None:
        stop_price = NO_STOP_PRICE
    elif variant == CONTROL_VARIANT:
        stop_price = own_stop_price
    else:
        _, stop_price = position_rules.levels(entry_price, target_pct, stop_pct)

    window = [bar for bar in bars if bar.ts >= opened_at]
    if stop_pct is None:
        assert_no_stop(window)

    decision = position_rules.check_exit(
        bars=window,
        target_price=target_price,
        stop_price=stop_price,
        entry_price=entry_price,
        deadline_at=deadline_at,
        cost_pct=cost_pct,
    )
    if decision is None:
        # ИСХОД НЕ ИЗМЕРЕН. Ряд оборвался раньше срока, бар срока не предъявлен
        # и ни один уровень не задет: назвать это ``timeout`` значило бы выдать
        # неизмеренное за измеренное. Удержание при этом НЕ РАВНО НУЛЮ, а НЕ
        # ОПРЕДЕЛЕНО — читать нули ниже в отрыве от ``exit_reason`` нельзя, и
        # ограничение position_stop_shadow_gap_chk связывает их именно так.
        return VariantOutcome(
            variant=variant,
            stop_pct=stop_pct,
            exit_reason=UNMEASURED,
            exit_bar_ts=None,
            exit_price=None,
            net_pnl_pct=None,
            net_pnl_usd=None,
            closed_at=None,
            held_sec=0,
            extra_held_sec=0,
            bars_used=sum(1 for bar in window if bar.ts < deadline_at),
            resolution=resolution,
        )

    net_pct = position_rules.net_pnl(entry_price, decision.exit_price, cost_pct)
    closed_at = closing_moment(decision.exit_bar_ts, resolution)
    return VariantOutcome(
        variant=variant,
        stop_pct=stop_pct,
        exit_reason=decision.exit_reason,
        exit_bar_ts=decision.exit_bar_ts,
        exit_price=float(decision.exit_price),
        net_pnl_pct=net_pct,
        # Итог в долларах — от ФАКТИЧЕСКОГО слота позиции (§2.1 ТЗ), а не от
        # константы: слот мог отличаться, и константа превратила бы замер в
        # оценку.
        net_pnl_usd=float(notional_usd) * net_pct / 100.0,
        closed_at=closed_at,
        held_sec=int((closed_at - opened_at).total_seconds()),
        extra_held_sec=int((closed_at - fact_closed_at).total_seconds()),
        bars_used=decision.bars_held,
        resolution=resolution,
    )


def check_monotonic(outcomes: list[VariantOutcome]) -> None:
    """Следствие правила, проверяемое на каждой позиции: шире предел — не раньше выход.

    Расширение предела способно только УБРАТЬ выход по пределу; цели и срока оно
    не касается. Значит удержание при более широком пределе не может оказаться
    короче, чем при более узком. Нарушение означает, что расчёт разошёлся с
    правилом, — и тогда падаем, а не печатаем правдоподобные числа.

    Неизмеренные исходы в цепочку не входят: у них удержание не определено.
    """
    chain = [row for row in outcomes if row.measured]
    chain.sort(key=lambda row: stop_width(row.stop_pct))
    for narrow, wide in zip(chain, chain[1:], strict=False):
        if wide.held_sec < narrow.held_sec:
            raise AssertionError(
                f"{wide.variant} держит {wide.held_sec} с, а более узкий "
                f"{narrow.variant} — {narrow.held_sec} с: расширение предела "
                "не может укоротить удержание"
            )


def resolve_position_stops(
    bars: list[position_rules.Bar],
    *,
    position_stop_pct: float,
    entry_price: float,
    target_pct: float,
    cost_pct: float,
    notional_usd: float,
    opened_at: datetime,
    deadline_at: datetime,
    fact_closed_at: datetime,
    resolution: str,
    side: str,
) -> list[VariantOutcome]:
    """Пять исходов одной позиции на ОДНОМ и том же ряде свечей. ЧИСТАЯ функция."""
    if side != position_rules.SIDE_BUY:
        # Спот: позиции только на покупку (positions_side_chk). Продажа сюда
        # попасть не может, и посчитать её «как-нибудь» нельзя — уровень предела
        # у продажи лежит по другую сторону цены входа.
        raise ValueError(f"позиции ведутся только на покупку, получено: {side}")
    outcomes = [
        resolve_variant(
            bars,
            variant=variant,
            position_stop_pct=position_stop_pct,
            entry_price=entry_price,
            target_pct=target_pct,
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            opened_at=opened_at,
            deadline_at=deadline_at,
            fact_closed_at=fact_closed_at,
            resolution=resolution,
        )
        for variant in variant_names()
    ]
    check_monotonic(outcomes)
    return outcomes


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
    position: dict[str, Any], control: VariantOutcome | None
) -> list[str]:
    """Расхождения контроля с фактом. Пустой список — совпало (§2.2 ТЗ).

    Сверяются четыре величины: причина выхода, МОМЕНТ ЗАКРЫТИЯ, цена выхода и
    итог в процентах — каждая с тем числом знаков, с которым она ЛЕЖИТ В БАЗЕ.
    Сравнение «примерно» пропустило бы ровно то расхождение, ради которого
    сверка и делается.

    ГРАБЛИ 9.1.3, НАЗВАННЫЕ В §2.2 ТЗ. Колонки ``exit_bar_ts`` в ``positions``
    НЕТ. Живой сервис пишет ``closed_at`` = время ЗАКРЫТИЯ бара выхода
    (``src/positions/runner.py``), а правило возвращает время его ОТКРЫТИЯ.
    Приведение делает :func:`src.shadow.trailing.closing_moment` — ПЕРЕИСПОЛЬЗУЕТСЯ
    готовая, вторая такая же здесь не пишется. Допуск при этом остаётся НУЛЕВЫМ:
    приведено «что с чем», а не «насколько близко».

    ``None`` вместо контроля — тоже расхождение, и названное словами: живое
    правило на этом ряде исхода не дало, а в базе исход есть.
    """
    if control is None or not control.measured:
        return ["живое правило не дало исхода на прочитанном ряде свечей"]
    problems: list[str] = []
    if control.exit_reason != str(position["exit_reason"]):
        problems.append(
            f"exit_reason: факт {position['exit_reason']}, "
            f"пересчёт {control.exit_reason}"
        )
    if control.closed_at != position["closed_at"]:
        problems.append(
            f"closed_at: факт {position['closed_at']}, "
            f"пересчёт {control.closed_at} (бар выхода {control.exit_bar_ts})"
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


def outcome_table(
    positions: list[dict[str, Any]],
    shadows: dict[int, list[VariantOutcome]],
) -> dict[str, dict[str, int]]:
    """ЧИСЛО 1: чем кончились бы позиции, ФАКТИЧЕСКИ закрытые по пределу.

    Печатается первым — это прямой ответ на вопрос владельца. Разбивка по
    вариантам и причинам выхода, включая ``no_data``: неизмеренный исход обязан
    быть виден, иначе выборка, из которой что-то молча выпало, неотличима от
    выборки, в которой этого не было.
    """
    stop_ids = [
        int(p["id"]) for p in positions
        if str(p["exit_reason"]) == position_rules.EXIT_STOP
    ]
    table: dict[str, dict[str, int]] = {
        variant: {reason: 0 for reason in SHADOW_REASONS}
        for variant in variant_names()
    }
    for position_id in stop_ids:
        for row in shadows.get(position_id, []):
            table[row.variant][row.exit_reason] += 1
    return table


def money_summary(
    positions: list[dict[str, Any]],
    shadows: dict[int, list[VariantOutcome]],
    *,
    only_stop: bool,
) -> list[dict[str, Any]]:
    """ЧИСЛО 2: цена вопроса в процентах, по каждому варианту.

    ХУДШАЯ СДЕЛКА СЧИТАЕТСЯ ОБЯЗАТЕЛЬНО (§3 ТЗ). Смысл предела убытка — ограничить
    хвост, а не поднять среднее; среднее без крайнего случая скрыло бы ровно то,
    ради чего предел стоит.

    НЕИЗМЕРЕННЫЕ ИСХОДЫ В СРЕДНЕЕ НЕ ВХОДЯТ и считаются отдельно. Подставить им
    ноль значило бы утверждать «уровень предела не изменил результат», чего никто
    не измерял.
    """
    chosen = [
        p for p in positions
        if not only_stop or str(p["exit_reason"]) == position_rules.EXIT_STOP
    ]
    out: list[dict[str, Any]] = []
    for variant in variant_names():
        values: list[float] = []
        usd = 0.0
        plus = minus = same = unmeasured = 0
        worst: tuple[float, int, str] | None = None
        for position in chosen:
            row = next(
                (r for r in shadows.get(int(position["id"]), [])
                 if r.variant == variant),
                None,
            )
            if row is None or row.net_pnl_pct is None:
                unmeasured += 1
                continue
            value = float(row.net_pnl_pct)
            values.append(value)
            usd += float(row.net_pnl_usd or 0.0)
            if worst is None or value < worst[0]:
                worst = (value, int(position["id"]), str(position["symbol"]))
            delta = value - float(position["net_pnl_pct"])
            if delta > 0:
                plus += 1
            elif delta < 0:
                minus += 1
            else:
                same += 1
        out.append({
            "variant": variant,
            "n": len(values),
            "mean_pct": (sum(values) / len(values)) if values else None,
            "sum_usd": usd if values else None,
            "worst_pct": worst[0] if worst else None,
            "worst_position_id": worst[1] if worst else None,
            "worst_symbol": worst[2] if worst else None,
            "plus_vs_fact": plus,
            "minus_vs_fact": minus,
            "same_vs_fact": same,
            "unmeasured": unmeasured,
        })
    return out


def slot_summary(
    shadows: dict[int, list[VariantOutcome]],
    blocked: dict[tuple[int, str], int],
) -> list[dict[str, Any]]:
    """ЧИСЛО 3: цена вопроса в слотах.

    ``extra_held_sec`` — насколько дольше слот был бы занят против ФАКТА:
    среднее, медиана и максимум, в часах. ``blocked_signals`` — сколько годных
    входов по тому же инструменту попало бы в это окно.

    ЗАБЛОКИРОВАННЫЕ ВХОДЫ ТОЛЬКО СЧИТАЮТСЯ, НО НЕ ОЦЕНИВАЮТСЯ (§3 ТЗ).
    """
    out: list[dict[str, Any]] = []
    for variant in variant_names():
        extras: list[int] = []
        blocked_total = 0
        unmeasured = 0
        for position_id, rows in shadows.items():
            row = next((r for r in rows if r.variant == variant), None)
            if row is None or not row.measured:
                unmeasured += 1
                continue
            extras.append(row.extra_held_sec)
            blocked_total += blocked.get((position_id, variant), 0)
        out.append({
            "variant": variant,
            "n": len(extras),
            "mean_extra_h": (
                sum(extras) / len(extras) / SECONDS_IN_HOUR if extras else None
            ),
            "median_extra_h": (
                statistics.median(extras) / SECONDS_IN_HOUR if extras else None
            ),
            "max_extra_h": (max(extras) / SECONDS_IN_HOUR if extras else None),
            "blocked_signals": blocked_total,
            "blocked_per_position": (
                blocked_total / len(extras) if extras else None
            ),
            "unmeasured": unmeasured,
        })
    return out


def _fmt(value: float | None, places: int = 4, sign: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{places}f}" if sign else f"{value:.{places}f}"


def print_power_warning(n: int) -> None:
    """ЧИСЛО 4: статистическая сила, честно (§3 ТЗ).

    При ``n < 30`` строка печатается ЗАГЛАВНЫМИ, и ни одного оценочного слова в
    выводах не появляется. Это не осторожность ради вида: при десятке сделок
    доверительный интервал разницы шире любой из наблюдаемых разниц, и
    сравнительная степень была бы утверждением, которого данные не несут.
    """
    print()
    print(f"  Наблюдений в выборке: N = {n}")
    if n < MIN_SAMPLE:
        print()
        print("  ПРИ ТАКОМ N ДОВЕРИТЕЛЬНЫЙ ИНТЕРВАЛ РАЗНИЦЫ ШИРЕ ЛЮБОЙ ИЗ")
        print("  НАБЛЮДАЕМЫХ РАЗНИЦ. ВЫВОД О ПРЕИМУЩЕСТВЕ ОДНОГО УРОВНЯ ПРЕДЕЛА")
        print("  НАД ДРУГИМ НА ЭТИХ ДАННЫХ СДЕЛАТЬ НЕЛЬЗЯ. ЧИСЛА НИЖЕ — ОПИСАНИЕ")
        print("  ТОГО, ЧТО СЛУЧИЛОСЬ С ЭТИМИ КОНКРЕТНЫМИ СДЕЛКАМИ, И НИЧЕГО")
        print("  БОЛЕЕ: ОНИ НЕ ПРЕДСКАЗЫВАЮТ СЛЕДУЮЩИЕ.")
    else:
        print(f"  Выборка достигла порога N >= {MIN_SAMPLE}, при котором")
        print("  доверительный интервал имеет смысл считать. Сам интервал этим")
        print("  этапом не считается — это отдельная работа.")


async def _load_bars(
    position: dict[str, Any], now: datetime
) -> list[position_rules.Bar]:
    """Ряд свечей позиции: от бара входа до бара срока, ТОЛЬКО ЗАКРЫТЫЕ (§2.1 ТЗ).

    ГОДНОСТЬ БАРА — ТО ЖЕ ПРАВИЛО, ЧТО ЗАКРЫЛО ДЕФЕКТ 8.10.1. Верхняя граница
    чтения не дальше последнего заведомо закрытого бара, а запас берётся
    ``settle_seconds()`` — ИМПОРТОМ, а не копией формулы: копия формулы уже была
    причиной дефекта в этом проекте.

    Для закрытой позиции срок давно в прошлом, и ограничение обычно ни на что не
    влияет — но именно «обычно» и делает копию формулы опасной.
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
        position_rules.Bar(
            ts=item["ts"], high=float(item["high"]),
            low=float(item["low"]), close=float(item["close"]),
        )
        for item in raw
    ]


async def _count_blocked(
    positions: list[dict[str, Any]],
    shadows: dict[int, list[VariantOutcome]],
) -> dict[tuple[int, str], int]:
    """Заблокированные входы по каждой паре «позиция × вариант» (§3 ТЗ, ЧИСЛО 3).

    Окно — от ФАКТИЧЕСКОГО закрытия (включительно) до пересчётного (не
    включительно). Границы разобраны в ``db.count_blocked_signals``.
    """
    by_id = {int(p["id"]): p for p in positions}
    out: dict[tuple[int, str], int] = {}
    for position_id, rows in sorted(shadows.items()):
        position = by_id[position_id]
        for row in rows:
            if not row.measured or row.closed_at is None:
                continue
            out[(position_id, row.variant)] = await db.count_blocked_signals(
                instrument_id=int(position["instrument_id"]),
                min_probability=float(settings.POSITION_MIN_PROBABILITY),
                ts_from=position["closed_at"],
                ts_to=row.closed_at,
            )
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Замер: что было бы с закрытыми позициями при других уровнях "
            "предела убытка (Этап 9.1.4). Без --apply — только печать."
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="записать результаты в position_stop_shadow",
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
        parser.error(
            f"--since разобрать не удалось: {args.since!r} (нужен YYYY-MM-DD)"
        )

    now = datetime.now(UTC)
    await db.connect()
    try:
        return await _run(args, since, now)
    finally:
        await db.close()


async def _run(
    args: argparse.Namespace, since: datetime | None, now: datetime
) -> int:
    # ВЫБОРКА ТА ЖЕ, ЧТО У 9.1.3, И БЕРЁТСЯ ТЕМ ЖЕ ЗАПРОСОМ: закрытые позиции
    # без data_gap. Второй запрос с тем же смыслом однажды разошёлся бы с
    # первым, и два замера «на одной и той же выборке» стали бы несравнимы.
    counts = await db.count_positions_for_shadow(since=since)
    positions = await db.get_positions_for_shadow(since=since)
    stop_positions = [
        p for p in positions
        if str(p["exit_reason"]) == position_rules.EXIT_STOP
    ]

    print("=" * 78)
    print(" ЭТАП 9.1.4. ЧТО БЫЛО БЫ БЕЗ ПРЕДЕЛА УБЫТКА (И ПРИ ДРУГИХ ПРЕДЕЛАХ)")
    print("=" * 78)
    print(f"  Закрытых позиций всего:       {counts['closed_total']}")
    print(f"  Из них исключено (data_gap):  {counts['data_gap']}")
    print(f"  Открытых (в замер не идут):   {counts['still_open']}")
    print(f"  В выборке замера:             {len(positions)}")
    print(f"  Из них закрыто по пределу:    {len(stop_positions)}")
    print(f"  Запас закрытия бара:          {settle_seconds()} с")
    print(f"  Порог годного входа:          "
          f"POSITION_MIN_PROBABILITY = {settings.POSITION_MIN_PROBABILITY}")

    _log.info(
        "Замер предела: выборка",
        stopcf_positions_total=len(positions),
        stopcf_positions_stop=len(stop_positions),
        stopcf_positions_skipped_gap=counts["data_gap"],
        stopcf_positions_open=counts["still_open"],
        settle_seconds=settle_seconds(),
    )

    if not positions:
        print()
        print("  Выборка пуста: считать нечего.")
        _log.info("Замер предела: выборка пуста", stopcf_positions_total=0)
        return 3

    # --- Пересчёт ---------------------------------------------------------
    shadows: dict[int, list[VariantOutcome]] = {}
    mismatches: list[tuple[int, list[str]]] = []
    failures: list[tuple[int, str]] = []
    for position in positions:
        position_id = int(position["id"])
        bars = await _load_bars(position, now)
        try:
            rows = resolve_position_stops(
                bars,
                position_stop_pct=float(position["stop_pct"]),
                entry_price=float(position["entry_price"]),
                target_pct=float(position["target_pct"]),
                cost_pct=float(position["cost_pct"]),
                notional_usd=float(position["notional_usd"]),
                opened_at=position["opened_at"],
                deadline_at=position["deadline_at"],
                fact_closed_at=position["closed_at"],
                resolution=str(position["resolution"]),
                side=str(position["side"]),
            )
        except (ValueError, AssertionError) as exc:
            # Расчёт отказался считать: ряд негоден или проходы разошлись. Это
            # то же по смыслу, что расхождение контроля, и молча пропустить
            # такую позицию нельзя.
            failures.append((position_id, f"{type(exc).__name__}: {exc}"))
            continue
        control = next(
            (r for r in rows if r.variant == CONTROL_VARIANT), None
        )
        problems = compare_control(position, control)
        if problems:
            mismatches.append((position_id, problems))
        shadows[position_id] = rows

    # СВЕРЕНО — ЭТО ЧИСЛО ПОЗИЦИЙ, ПО КОТОРЫМ СВЕРКА ВООБЩЕ ПРОВОДИЛАСЬ, а не
    # число удавшихся (грабли 9.1.3, §7.3 ТЗ). Позиция, расчёт которой упал,
    # СВЕРЕНА (попытка была) и РАЗОШЛАСЬ (совпадения не получено); в оба
    # счётчика она входит одинаково.
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
        "Замер предела: контроль",
        stopcf_control_compared=compared,
        stopcf_control_mismatched=mismatched,
    )

    print()
    print("-" * 78)
    print(" КОНТРОЛЬ: живое правило при фактическом пределе против записанного факта")
    print("-" * 78)
    print(f"  Сверено позиций:   {compared}")
    print(f"  Разошлось:         {mismatched}")

    # --- БЛОКИРУЮЩЕЕ УСЛОВИЕ (§2.2 ТЗ) ------------------------------------
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
        print("  сравнения запрещено (§2.2 ТЗ): расхождение надо расследовать.")
        print("  Ни одной строки не записано.")
        _log.warning(
            "Замер предела: контроль не совпал",
            stopcf_control_compared=compared,
            stopcf_control_mismatched=mismatched,
            stopcf_rows_written=0,
        )
        return 2

    blocked = await _count_blocked(positions, shadows)

    # --- ЧИСЛО 1 ----------------------------------------------------------
    table = outcome_table(positions, shadows)
    no_stop_target = table[NO_STOP_VARIANT][position_rules.EXIT_TARGET]
    print()
    print("-" * 78)
    print(" ЧИСЛО 1. СКОЛЬКО УБЫТОЧНЫХ СДЕЛОК ДОШЛИ БЫ ДО ЦЕЛИ")
    print("-" * 78)
    print("  Только по позициям, ФАКТИЧЕСКИ закрытым по пределу. Это прямой")
    print("  ответ на вопрос владельца, поэтому он и печатается первым.")
    print()
    print(f"  Таких позиций: {len(stop_positions)}")
    print()
    header = f"  {'вариант':<12}"
    for reason in SHADOW_REASONS:
        header += f"{reason:>11}"
    print(header)
    print("  " + "-" * (12 + 11 * len(SHADOW_REASONS)))
    for variant in variant_names():
        line = f"  {variant:<12}"
        for reason in SHADOW_REASONS:
            line += f"{table[variant][reason]:>11}"
        print(line)
    print()
    print(f"  Без предела дошли бы до цели: {no_stop_target} "
          f"из {len(stop_positions)}")
    print("  Столбец no_data — исход НЕ ИЗМЕРЕН (ряд свечей оборвался раньше")
    print("  срока). Он показан отдельно, а не растворён в timeout: неизмеренное")
    print("  не должно выглядеть измеренным.")
    _log.info(
        "Замер предела: ЧИСЛО 1",
        stopcf_target_reached_no_stop=no_stop_target,
        stopcf_positions_stop=len(stop_positions),
    )

    # --- ЧИСЛО 4 печатается ДО таблиц с числами ---------------------------
    # Порядок содержателен: предупреждение о силе выборки должно стоять ПЕРЕД
    # тем, что оно ограничивает, а не после — иначе таблицы прочитают раньше,
    # чем узнают, как их читать нельзя.
    print()
    print("-" * 78)
    print(" ЧИСЛО 4. СТАТИСТИЧЕСКАЯ СИЛА")
    print("-" * 78)
    print_power_warning(len(positions))

    # --- ЧИСЛО 2 ----------------------------------------------------------
    whole = money_summary(positions, shadows, only_stop=False)
    only_stop = money_summary(positions, shadows, only_stop=True)
    print()
    print("-" * 78)
    print(" ЧИСЛО 2. ЦЕНА ВОПРОСА В ПРОЦЕНТАХ")
    print("-" * 78)
    print("  ХУДШАЯ СДЕЛКА ПЕЧАТАЕТСЯ ОБЯЗАТЕЛЬНО: смысл предела убытка —")
    print("  ограничить хвост, а не поднять среднее. Среднее без крайнего")
    print("  случая скрыло бы ровно то, ради чего предел стоит.")
    for title, block in (
        ("вся выборка", whole),
        ("только закрытые по пределу", only_stop),
    ):
        print()
        print(f"  — {title} —")
        print(f"  {'вариант':<12}{'N':>4}{'ср. итог,%':>13}{'сумма,$':>11}"
              f"{'худшая,%':>12}{'в плюс':>8}{'в минус':>9}{'без изм.':>10}"
              f"{'не изм-но':>11}")
        print("  " + "-" * 90)
        for row in block:
            print(
                f"  {row['variant']:<12}{row['n']:>4}"
                f"{_fmt(row['mean_pct']):>13}{_fmt(row['sum_usd']):>11}"
                f"{_fmt(row['worst_pct']):>12}"
                f"{row['plus_vs_fact']:>8}{row['minus_vs_fact']:>9}"
                f"{row['same_vs_fact']:>10}{row['unmeasured']:>11}"
            )
        for row in block:
            if row["worst_pct"] is not None:
                print(f"      худшая у {row['variant']}: "
                      f"{_fmt(row['worst_pct'])}% — позиция "
                      f"{row['worst_position_id']} ({row['worst_symbol']})")
    print()
    print("  «в плюс» / «в минус» — сколько сделок дали итог выше или ниже")
    print("  ФАКТИЧЕСКОГО, «без изм.» — совпали с ним до знака. «не изм-но» —")
    print("  позиции, у которых исход варианта не измерен; в среднее и в сумму")
    print("  они не входят: подставить им ноль значило бы утверждать «уровень")
    print("  предела ничего не изменил».")

    # --- ЧИСЛО 3 ----------------------------------------------------------
    slots = slot_summary(shadows, blocked)
    print()
    print("-" * 78)
    print(" ЧИСЛО 3. ЦЕНА ВОПРОСА В СЛОТАХ")
    print("-" * 78)
    print(f"  {'вариант':<12}{'N':>4}{'ср. лишних,ч':>15}{'медиана,ч':>12}"
          f"{'максимум,ч':>13}{'входов':>9}{'на позицию':>12}")
    print("  " + "-" * 77)
    for row in slots:
        print(
            f"  {row['variant']:<12}{row['n']:>4}"
            f"{_fmt(row['mean_extra_h'], 2):>15}"
            f"{_fmt(row['median_extra_h'], 2):>12}"
            f"{_fmt(row['max_extra_h'], 2):>13}"
            f"{row['blocked_signals']:>9}"
            f"{_fmt(row['blocked_per_position'], 2, sign=False):>12}"
        )
    print()
    print("  ЗАБЛОКИРОВАННЫЕ ВХОДЫ ТОЛЬКО СЧИТАЮТСЯ, НО НЕ ОЦЕНИВАЮТСЯ. Чтобы")
    print("  узнать их итог, пришлось бы проиграть целиком ДРУГУЮ историю")
    print("  позиций, где каждый вход меняет занятость следующих. Это другой")
    print("  этап. Читать это число как «столько-то прибыли потеряно» НЕЛЬЗЯ.")
    print()
    print("  Годным входом считается: тот же инструмент, decision='buy',")
    print(f"  probability >= {settings.POSITION_MIN_PROBABILITY} "
          "(POSITION_MIN_PROBABILITY), degraded=false,")
    print("  момент в окне [фактическое закрытие, пересчётное закрытие).")
    print("  Живой отбор проверяет сверх этого версию логики, замороженную цель,")
    print("  свежесть свечи и свободный слот, поэтому число выше — ВЕРХНЯЯ")
    print("  ГРАНИЦА числа заблокированных входов, а не оно само.")

    print()
    print("  ВЫБОР УРОВНЯ ПРЕДЕЛА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ")
    print("  (§1 ТЗ). Предел убытка остаётся на месте, BARRIER_STOP_PCT не")
    print("  меняется. Числа выше описывают эти конкретные сделки.")

    # --- Запись -----------------------------------------------------------
    written = 0
    if args.apply:
        if not await db.position_stop_shadow_exists():
            print()
            print("  ОТКАЗ: таблицы position_stop_shadow нет.")
            print("  Примените миграцию db/migrations/022_position_stop_shadow.sql")
            return 2
        rows_to_save = [
            {
                "position_id": position_id,
                "variant": item.variant,
                "stop_pct": item.stop_pct,
                "exit_reason": item.exit_reason,
                "exit_bar_ts": item.exit_bar_ts,
                "exit_price": item.exit_price,
                "net_pnl_pct": item.net_pnl_pct,
                "net_pnl_usd": item.net_pnl_usd,
                "held_sec": item.held_sec,
                "extra_held_sec": item.extra_held_sec,
                "blocked_signals": blocked.get((position_id, item.variant), 0),
                "bars_used": item.bars_used,
                "resolution": item.resolution,
                "logic_version": int(settings.LOGIC_VERSION),
            }
            for position_id, items in sorted(shadows.items())
            for item in items
        ]
        written = await db.save_position_stop_shadow(rows_to_save)
        print()
        print(f"  Записано строк: {written}")
    else:
        print()
        print("  Ничего не записано: без --apply скрипт только считает.")

    peak = peak_rss_mb()
    print(f"  Пиковая память: {peak:,.0f} МБ")
    _log.info(
        "Замер предела: расчёт завершён",
        stopcf_rows_written=written,
        peak_rss_mb=round(peak, 1),
    )

    if args.json:
        summary = {
            "generated_at": now.isoformat(timespec="seconds"),
            "positions_total": len(positions),
            "positions_stop": len(stop_positions),
            "positions_skipped_gap": counts["data_gap"],
            "positions_open": counts["still_open"],
            "control_compared": compared,
            "control_mismatched": mismatched,
            "settle_seconds": settle_seconds(),
            "min_probability": float(settings.POSITION_MIN_PROBABILITY),
            "outcomes_of_stop_positions": table,
            "target_reached_no_stop": no_stop_target,
            "money_whole_sample": whole,
            "money_stop_only": only_stop,
            "slots": slots,
            "rows_written": written,
            "min_sample_for_conclusions": MIN_SAMPLE,
            "peak_rss_mb": round(peak, 1),
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
        print(f"  Сводка сохранена: {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
