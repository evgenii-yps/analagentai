"""Сервис ведения позиций: работа с базой поверх чистых правил (§7.2 ТЗ 9.1).

ЧТО ДЕЛАЕТ ИТЕРАЦИЯ, по шагам:

  1. ``sync_open_positions`` — по каждой открытой позиции читает бары окна,
     прогоняет правило ``check_exit`` и либо закрывает позицию одним UPDATE,
     либо двигает отметку «докуда разобрано»;
  2. ``open_new_positions`` — отбирает кандидатов, проверяет ``should_open`` и
     открывает позиции в свободных слотах;
  3. ``export_closed_to_sheet`` — пишет закрытые позиции одного инструмента в
     рабочий лист владельца (Этап 9.1.1 §7). ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО.

ПОРЯДОК СОДЕРЖАТЕЛЕН, А НЕ ПРОИЗВОЛЕН. Закрытая на этой же итерации позиция
обязана освободить слот немедленно, иначе инструмент простаивает лишнюю минуту
на ровном месте.

ПОЗИЦИИ ВИРТУАЛЬНЫЕ. Ордера на биржу не отправляются, ключи API не читаются,
сетевых обращений к бирже этот код не делает вовсе: он читает только
собственные свечи из ``public.ohlcv``.

ЖЁСТКАЯ ГРАНИЦА ЭТАПА. Ни одно решение системы не меняется. Сервис не пишет ни
в ``signals``, ни в ``signal_evaluations``, ни в ``signal_targets``, ни в
``risk_targets`` — ни одной строкой, ни при каких условиях.

СЕРВИС НЕ ПАДАЕТ НИ ПРИ КАКИХ ОШИБКАХ ИТЕРАЦИИ. Причина та же, что у ``notify``
и ``evaluator``, и здесь она весомее: упавший сервис перестаёт ВЕСТИ уже
открытые позиции, и они повисают навсегда — цель и предел, задетые за время
простоя, не будут замечены никогда, потому что бары уйдут за отметку
``last_checked_ts`` только вместе с их разбором.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import get_redis
from src.export import sheets
from src.notify.telegram import send_message
from src.positions import messages
from src.positions.rules import (
    SIDE_BUY,
    Bar,
    check_exit,
    levels,
    net_pnl,
    qty_for_slot,
    should_open,
    slippage_pct,
)
from src.positions.sheet import (
    SHEET_HEADERS,
    build_position_row,
    is_exportable,
)

_log = structlog.get_logger().bind(component="positions")

# TTL heartbeat-ключа (секунды) — как у остальных сервисов проекта.
_HEARTBEAT_TTL = 300

# Разрешение, которым ведётся позиция. Записано ограничением positions_resolution_chk
# и здесь повторено единственной константой, а не строковым литералом в трёх местах.
RESOLUTION = "1m"


@dataclass
class OpenedStats:
    """Итог шага открытия — для журнала и отчёта."""

    candidates: int = 0
    opened: int = 0
    races: int = 0
    # Отказы по машиночитаемым ключам: знать, ПОЧЕМУ позиций мало, придётся.
    refusals: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1


@dataclass
class ClosedStats:
    """Итог шага ведения открытых позиций."""

    checked: int = 0
    closed: int = 0
    touched: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def count(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


@dataclass
class SheetStats:
    """Итог шага записи закрытых позиций в Google Таблицу (§7 ТЗ 9.1.1)."""

    candidates: int = 0
    written: int = 0
    failed: int = 0
    # Выключенная настройкой запись — это НЕ ноль записанных, а отсутствие шага:
    # ноль читался бы как «пробовали и не нашли что писать».
    disabled: bool = False


@dataclass
class IterationStats:
    """Итог итерации целиком."""

    closed: ClosedStats = field(default_factory=ClosedStats)
    opened: OpenedStats = field(default_factory=OpenedStats)
    sheet: SheetStats = field(default_factory=SheetStats)


def last_closed_bar_open_ts(now: datetime) -> datetime:
    """Время ОТКРЫТИЯ последнего бара, который заведомо закрыт.

    Бар считается закрытым, когда с момента его ЗАКРЫТИЯ прошло не меньше
    ``POSITION_SETTLE_SEC``. Это то же правило, что чинится Задачей Б, и по той
    же причине: коллектор перезаписывает формирующуюся свечу (UPSERT с
    DO UPDATE), и её ``close`` — цена «пока что».

    Возвращается время ОТКРЫТИЯ, потому что именно так бары лежат в ``ohlcv`` и
    приходят от ccxt. Бар с меткой T закрывается в T + минута; он годен, когда
    ``T + минута + запас <= now``.
    """
    return now - timedelta(seconds=60 + settings.POSITION_SETTLE_SEC)


async def _balance() -> dict[str, Any] | None:
    """Пять величин счёта либо ``None``, если их не удалось получить.

    ОШИБКА ЧТЕНИЯ БАЛАНСА НЕ ОТМЕНЯЕТ НИ ОТКРЫТИЯ, НИ ЗАКРЫТИЯ. Баланс —
    описание уже случившегося, и сорвать из-за него запись факта значило бы
    потерять факт ради его описания. Сообщение в этом случае уходит без строки
    о счёте: строка отсутствует честнее, чем строка с нулями.
    """
    try:
        return await db.get_balance(
            capital_start=settings.POSITION_START_BALANCE_USD
        )
    except Exception as exc:  # noqa: BLE001 — баланс не важнее позиции
        _log.warning("positions_balance_failed=1", error=str(exc))
        return None


async def _send(text: str) -> None:
    """Отправка уведомления. Молчит, если уведомления выключены настройкой.

    Ошибку отправки сервис НЕ считает поводом уронить итерацию: позиция уже
    открыта или закрыта в базе, и несостоявшееся сообщение не отменяет факта.
    """
    if not settings.POSITION_NOTIFY_ENABLED:
        return
    try:
        await send_message(text)
    except Exception as exc:  # noqa: BLE001 — уведомление не важнее позиции
        _log.warning("positions_notify_failed=1", error=str(exc))


async def sync_open_positions(now: datetime) -> ClosedStats:
    """Разбирает открытые позиции по закрытым барам (§4.4 ТЗ).

    ОКНО ЧИТАЕТСЯ ЦЕЛИКОМ — от бара ПОСЛЕ входа до последнего закрытого, — а не
    только новые бары. Так требует само правило: ``mae_pct``, ``mfe_pct`` и
    ``bars_held`` считаются по ВСЕМУ удержанному окну, и посчитать их по одному
    свежему бару нельзя. Отметка ``last_checked_ts`` при этом не лишняя: по ней
    видно, появились ли вообще новые бары, и позиция без новых баров не
    трогается — ни чтением окна, ни записью.

    Цена этого решения известна и мала: пять позиций по 1440 минутных баров —
    это тысячи строк в минуту, а не миллионы.
    """
    stats = ClosedStats()
    settle_edge = last_closed_bar_open_ts(now)

    for row in await db.get_open_positions():
        position_id = int(row["id"])
        opened_at = row["opened_at"]
        deadline_at = row["deadline_at"]
        last_checked = row["last_checked_ts"] or opened_at

        # Новых закрытых баров нет — позицию не трогаем вовсе.
        if settle_edge <= last_checked:
            continue

        stats.checked += 1
        entry_price = float(row["entry_price"])
        # Верхняя граница чтения — последний ЗАКРЫТЫЙ бар, но не дальше самого
        # срока: бар срока нужен правилу как признак «окно кончилось», а бары
        # за ним к позиции не относятся.
        read_until = min(settle_edge, deadline_at)
        # Нижняя граница — САМ ``opened_at``: это время закрытия бара входа и
        # одновременно время ОТКРЫТИЯ следующего бара. Свеча момента входа в
        # окно не входит (её метка на минуту раньше), а первая свеча после
        # входа входит — сдвиг границы «на всякий случай» её бы потерял.
        raw = await db.get_ohlcv_bars(
            int(row["instrument_id"]), settings.POSITION_TIMEFRAME,
            opened_at, read_until,
        )
        bars = [
            Bar(ts=item["ts"], high=float(item["high"]),
                low=float(item["low"]), close=float(item["close"]))
            for item in raw
        ]
        if not bars:
            continue

        decision = check_exit(
            bars=bars,
            target_price=float(row["target_price"]),
            stop_price=float(row["stop_price"]),
            entry_price=entry_price,
            deadline_at=deadline_at,
            cost_pct=float(row["cost_pct"]),
        )
        seen_until = bars[-1].ts

        if decision is None:
            await db.touch_position(position_id, seen_until)
            stats.touched += 1
            continue

        pnl_pct = net_pnl(entry_price, decision.exit_price, float(row["cost_pct"]))
        pnl_usd = float(row["notional_usd"]) * pnl_pct / 100.0
        # Момент закрытия — время ЗАКРЫТИЯ бара выхода: бар прожит целиком, и
        # относить выход к его открытию значило бы закрывать позицию раньше,
        # чем случилось событие, по которому она закрыта.
        closed_at = decision.exit_bar_ts + timedelta(seconds=60)
        changed = await db.close_position(
            position_id,
            closed_at=closed_at,
            exit_price=decision.exit_price,
            exit_reason=decision.exit_reason,
            outcome_certain=decision.outcome_certain,
            net_pnl_pct=pnl_pct,
            net_pnl_usd=pnl_usd,
            bars_held=decision.bars_held,
            mae_pct=decision.mae_pct,
            mfe_pct=decision.mfe_pct,
            last_checked_ts=seen_until,
        )
        if not changed:
            # Позицию закрыла другая итерация. Штатный исход, не сбой.
            _log.info("positions_race_skipped=1", position_id=position_id,
                      stage="close")
            continue

        stats.closed += 1
        stats.count(decision.exit_reason)
        _log.info(
            "positions_closed=1",
            position_id=position_id, symbol=row["symbol"],
            exit_reason=decision.exit_reason,
            outcome_certain=decision.outcome_certain,
            net_pnl_pct=round(pnl_pct, 6), net_pnl_usd=round(pnl_usd, 6),
            bars_held=decision.bars_held,
        )
        # Баланс читается ПОСЛЕ закрытия: сообщение обязано показывать счёт
        # уже с учётом только что закрытой сделки, иначе человек увидел бы итог
        # сделки и счёт, в котором её ещё нет.
        await _send(messages.closed_text(
            symbol=str(row["symbol"]),
            exit_reason=decision.exit_reason,
            entry_price=entry_price,
            exit_price=decision.exit_price,
            net_pnl_pct=pnl_pct,
            net_pnl_usd=pnl_usd,
            cost_pct=float(row["cost_pct"]),
            held_sec=(closed_at - opened_at).total_seconds(),
            balance=await _balance(),
        ))
    return stats


async def open_new_positions(now: datetime) -> OpenedStats:
    """Отбирает кандидатов и открывает позиции в свободных слотах (§4.1–§4.3)."""
    stats = OpenedStats()
    # Занятые инструменты и число занятых слотов — из ОДНОГО чтения: два
    # запроса могли бы разойтись между собой, если позиция закроется между ними.
    open_rows = await db.get_open_positions()
    busy = {int(row["instrument_id"]) for row in open_rows}
    open_count = len(open_rows)
    # СВОБОДНЫЕ ДЕНЬГИ ЧИТАЮТСЯ ОДИН РАЗ ЗА ИТЕРАЦИЮ и уменьшаются в самом
    # цикле. Спрашивать базу после каждого открытия значило бы ждать сетевой
    # задержки ради числа, которое известно и так: открылась позиция — свободных
    # денег стало ровно на слот меньше.
    balance = await _balance()
    slot_usd = float(settings.POSITION_SLOT_USD)
    # Баланс не прочитался — вход НЕ запрещается. Правило «денег нет» обязано
    # срабатывать по измеренной нехватке денег, а не по неудавшемуся запросу:
    # иначе сбой соединения выглядел бы как пустой счёт.
    free_usd = (
        float(settings.POSITION_START_BALANCE_USD) if balance is None
        else float(balance["free"])
    )

    candidates = await db.get_position_candidates(
        logic_version=settings.LOGIC_VERSION,
        horizon_h=settings.POSITION_HORIZON_H,
        min_probability=settings.POSITION_MIN_PROBABILITY,
        max_signal_age_sec=settings.POSITION_MAX_SIGNAL_AGE_SEC,
        now=now,
    )
    stats.candidates = len(candidates)
    settle_edge = last_closed_bar_open_ts(now)

    for row in candidates:
        instrument_id = int(row["instrument_id"])
        # Последняя ЗАКРЫТАЯ свеча инструмента: по её закрытию и покупаем.
        bar = await db.get_last_closed_bar(
            instrument_id, settings.POSITION_TIMEFRAME, settle_edge
        )
        signal_ts = row["signal_ts"]
        # БАР ВХОДА ОБЯЗАН ЗАКРЫТЬСЯ НЕ РАНЬШЕ РЕШЕНИЯ, и это не придирка.
        # Последний ЗАКРЫТЫЙ бар отстоит от «сейчас» на минуту плюс запас
        # POSITION_SETTLE_SEC, то есть примерно на две с половиной минуты, — а
        # сигналу к этому моменту может быть всего секунда. Тогда «последняя
        # закрытая свеча» закрылась ДО того, как решение было принято, и вход
        # по ней означал бы покупку по цене, которую система уже видела, когда
        # решала. Задержка входа при этом получалась бы ОТРИЦАТЕЛЬНОЙ, а
        # entry_slippage_pct — измеряющий, сколько стоит задержка между
        # решением и входом, — измерял бы вместо этого движение цены ДО
        # решения. Числа выглядели бы правдоподобно и отвечали бы не на тот
        # вопрос.
        #
        # Ждать в таком случае недолго и не бесконечно: бар, закрывающийся на
        # ближайшей минутной границе после сигнала, станет годным через
        # 60 + POSITION_SETTLE_SEC секунд после неё, то есть не позже чем через
        # 150 секунд после сигнала — раньше, чем сигнал устареет по
        # POSITION_MAX_SIGNAL_AGE_SEC (180). Пока такого бара нет, годной свечи
        # для входа НЕТ — это и есть ``no_fresh_bar``, а не отдельная причина:
        # перечень причин отказа закрыт (§7.1 ТЗ).
        bar_close = None if bar is None else bar["ts"] + timedelta(seconds=60)
        if bar_close is not None and bar_close < signal_ts:
            bar, bar_close = None, None
        # Возраст свечи считается от её ЗАКРЫТИЯ, а не от открытия: свежесть —
        # это «как давно мы в последний раз знали цену», и метка открытия
        # завышала бы возраст ровно на длину бара.
        bar_age_sec = (
            None if bar_close is None else (now - bar_close).total_seconds()
        )

        verdict = should_open(
            decision=str(row["decision"]),
            logic_version=int(row["logic_version"]),
            expected_version=settings.LOGIC_VERSION,
            degraded=bool(row["degraded"]),
            probability=None if row["probability"] is None
            else float(row["probability"]),
            min_probability=settings.POSITION_MIN_PROBABILITY,
            has_open_position=instrument_id in busy,
            open_count=open_count,
            max_open=settings.POSITION_MAX_OPEN,
            signal_age_sec=float(row["age_sec"]),
            max_signal_age_sec=settings.POSITION_MAX_SIGNAL_AGE_SEC,
            bar_age_sec=bar_age_sec,
            max_bar_age_sec=settings.POSITION_MAX_BAR_AGE_SEC,
            has_frozen_target=row["target_pct"] is not None,
            free_usd=free_usd,
            slot_usd=slot_usd,
        )
        if not verdict.allowed:
            stats.refuse(verdict.reason)
            _log.info(
                "positions_skipped=1",
                signal_id=int(row["signal_id"]), symbol=row["symbol"],
                reason=verdict.reason,
            )
            continue

        assert bar is not None  # гарантировано verdict.allowed (no_fresh_bar)
        entry_price = float(bar["close"])
        signal_price = float(row["price_at_signal"])
        target_pct = float(row["target_pct"])
        stop_pct = settings.BARRIER_STOP_PCT
        target_price, stop_price = levels(entry_price, target_pct, stop_pct)
        # opened_at — время ЗАКРЫТИЯ бара входа (§4.2): по закрытию и покупаем.
        opened_at = bar["ts"] + timedelta(seconds=60)

        position_id = await db.open_position({
            "instrument_id": instrument_id,
            "signal_id": int(row["signal_id"]),
            "logic_version": settings.LOGIC_VERSION,
            "horizon_h": settings.POSITION_HORIZON_H,
            "side": SIDE_BUY,
            "signal_ts": signal_ts,
            "signal_price": signal_price,
            "opened_at": opened_at,
            "entry_price": entry_price,
            "entry_lag_sec": int((opened_at - signal_ts).total_seconds()),
            "entry_slippage_pct": slippage_pct(signal_price, entry_price),
            "qty": qty_for_slot(settings.POSITION_SLOT_USD, entry_price),
            "notional_usd": settings.POSITION_SLOT_USD,
            "target_pct": target_pct,
            "target_price": target_price,
            "stop_pct": stop_pct,
            "stop_price": stop_price,
            "cost_pct": settings.RISK_COST_ROUNDTRIP_PCT,
            "deadline_at": opened_at + timedelta(hours=settings.POSITION_HORIZON_H),
            "last_checked_ts": bar["ts"],
            "resolution": RESOLUTION,
        })
        if position_id is None:
            # Кто-то опередил: гонку закрыла база, а не код. Штатный исход.
            stats.races += 1
            _log.info("positions_race_skipped=1",
                      signal_id=int(row["signal_id"]), stage="open")
            continue

        open_count += 1
        busy.add(instrument_id)
        # РАЗМЕР СЛОТА НЕ ЗАВИСИТ ОТ НАКОПЛЕННОЙ ПРИБЫЛИ: из свободных денег
        # вычитается ровно POSITION_SLOT_USD — та же величина, что записана в
        # notional_usd. Прибыль не реинвестируется (§6.1 ТЗ 9.1.1).
        free_usd -= slot_usd
        stats.opened += 1
        lag = int((opened_at - signal_ts).total_seconds())
        slip = slippage_pct(signal_price, entry_price)
        _log.info(
            "positions_opened=1",
            position_id=position_id, signal_id=int(row["signal_id"]),
            symbol=row["symbol"], entry_price=entry_price,
            signal_price=signal_price, entry_lag_sec=lag,
            entry_slippage_pct=round(slip, 6),
        )
        await _send(messages.opened_text(
            symbol=str(row["symbol"]),
            entry_price=entry_price,
            notional_usd=settings.POSITION_SLOT_USD,
            target_price=target_price,
            target_pct=target_pct,
            stop_price=stop_price,
            stop_pct=stop_pct,
            deadline_at=opened_at + timedelta(hours=settings.POSITION_HORIZON_H),
            signal_id=int(row["signal_id"]),
            probability=None if row["probability"] is None
            else float(row["probability"]),
            entry_lag_sec=lag,
            balance=await _balance(),
        ))
    return stats


async def export_closed_to_sheet() -> SheetStats:
    """Пишет закрытые позиции одного инструмента в лист владельца (§7 ТЗ 9.1.1).

    ШАГ ОТДЕЛЬНЫЙ, А НЕ ВНУТРИ ЗАКРЫТИЯ, и это не косметика. Очередь берётся из
    базы по признаку «закрыта и ещё не записана», поэтому включённый позже флаг
    подхватывает ВСЁ накопленное — задним числом и в порядке закрытия. Запись,
    приделанная к моменту закрытия, дала бы только те позиции, что закрылись
    после включения, а прежние не записались бы никогда.

    ПОРЯДОК ЖЁСТКИЙ: позиция уже закрыта в базе (это сделал предыдущий шаг),
    затем пишется строка, и только после ПОДТВЕРЖДЁННОЙ записи ставится отметка
    ``sheet_exported_at``. Обратный порядок при сбое между шагами дал бы строку
    в листе без позиции в базе — число, которое нечем проверить.

    ПЕРВАЯ ЖЕ НЕУДАЧА ОСТАНАВЛИВАЕТ ОЧЕРЕДЬ. Лист — цепочка: объём каждой
    строки считается от предыдущей. Пропустить сбойную позицию и записать
    следующую значило бы построить цепочку по неполному ряду, и починить её
    потом можно было бы только руками.
    """
    stats = SheetStats()
    if not settings.POSITIONS_SHEETS_ENABLED:
        # ВЫКЛЮЧЕНО — КОД ЗАПИСИ НЕ ВЫПОЛНЯЕТСЯ ВОВСЕ: ни запроса к базе, ни
        # обращения к сети. Первая автоматическая запись в чужой рабочий
        # документ обязана произойти по сознательному решению владельца.
        stats.disabled = True
        return stats
    if not settings.SHEETS_WEBAPP_URL or not settings.SHEETS_SHARED_SECRET:
        stats.disabled = True
        _log.warning(
            "positions_sheet_not_configured=1",
            reason="не заданы SHEETS_WEBAPP_URL / SHEETS_SHARED_SECRET",
        )
        return stats

    instrument = settings.POSITIONS_SHEET_INSTRUMENT
    rows = await db.get_positions_for_sheet(instrument_symbol=instrument)
    stats.candidates = len(rows)
    for row in rows:
        if not is_exportable(row, instrument_symbol=instrument):
            # Запрос уже отобрал закрытые и незаписанные строки нужного
            # инструмента; проверка стоит второй раз потому, что правило
            # «что годится в лист» обязано быть ОДНИМ, и запрос — его удобная,
            # но не единственная запись.
            continue
        values = build_position_row(
            row, timezone_name=settings.NOTIFY_TIMEZONE
        )
        result = await sheets.post_position_row(
            settings.SHEETS_WEBAPP_URL,
            settings.SHEETS_SHARED_SECRET,
            settings.POSITIONS_SHEET_NAME,
            values,
            SHEET_HEADERS,
        )
        if not result.ok:
            stats.failed += 1
            _log.warning(
                "positions_sheet_failed=1",
                position_id=int(row["id"]), error=result.error,
            )
            break
        await db.mark_position_sheet_exported(int(row["id"]))
        stats.written += 1
        _log.info(
            "positions_sheet_written=1",
            position_id=int(row["id"]), symbol=row["symbol"],
            sheet=settings.POSITIONS_SHEET_NAME,
        )
    return stats


async def run_once(now: datetime | None = None) -> IterationStats:
    """Одна итерация: ведение открытых, открытие новых, запись в лист."""
    now = now or datetime.now(UTC)
    closed = await sync_open_positions(now)
    opened = await open_new_positions(now)
    # ЗАПИСЬ В ЛИСТ — ПОСЛЕДНЯЯ. Она обращается в сеть, и её место в конце
    # означает, что ни одно решение о позиции от неё не зависит: даже наглухо
    # недоступный приёмник не мешает ни закрыть позицию, ни открыть новую.
    sheet_stats = await export_closed_to_sheet()
    return IterationStats(closed=closed, opened=opened, sheet=sheet_stats)


async def _heartbeat() -> None:
    """Отметка времени последней успешной итерации — как у остальных сервисов."""
    now_iso = datetime.now(UTC).isoformat()
    await get_redis().set("positions:heartbeat", now_iso, ex=_HEARTBEAT_TTL)


async def run() -> None:
    """Вечный цикл. Не падает ни при каких ошибках итерации."""
    _log.info(
        "Сервис ведения позиций запущен (Этап 9.1, позиции ВИРТУАЛЬНЫЕ)",
        interval=settings.POSITION_INTERVAL,
        horizon_h=settings.POSITION_HORIZON_H,
        min_probability=settings.POSITION_MIN_PROBABILITY,
        max_open=settings.POSITION_MAX_OPEN,
        slot_usd=settings.POSITION_SLOT_USD,
        settle_sec=settings.POSITION_SETTLE_SEC,
    )
    while True:
        try:
            stats = await run_once()
            if (stats.opened.opened or stats.closed.closed
                    or stats.opened.refusals):
                _log.info(
                    "positions_iteration=1",
                    candidates=stats.opened.candidates,
                    opened=stats.opened.opened,
                    races=stats.opened.races,
                    refusals=stats.opened.refusals,
                    checked=stats.closed.checked,
                    closed=stats.closed.closed,
                    by_reason=stats.closed.by_reason,
                    sheet_written=stats.sheet.written,
                    sheet_failed=stats.sheet.failed,
                )
            await _heartbeat()
        except asyncio.CancelledError:
            _log.info("Сервис ведения позиций остановлен")
            raise
        except Exception as exc:  # noqa: BLE001 — сервис не падает (§7.2 ТЗ)
            _log.warning(
                "positions_iteration_failed=1",
                error=str(exc), error_type=type(exc).__name__,
            )
        await asyncio.sleep(settings.POSITION_INTERVAL)


def summary_line(stats: IterationStats) -> dict[str, Any]:
    """Итог итерации словарём — для тестов и разовых прогонов."""
    return {
        "opened": stats.opened.opened,
        "closed": stats.closed.closed,
        "candidates": stats.opened.candidates,
        "refusals": dict(stats.opened.refusals),
        "sheet_written": stats.sheet.written,
        "sheet_failed": stats.sheet.failed,
        "sheet_disabled": stats.sheet.disabled,
    }
