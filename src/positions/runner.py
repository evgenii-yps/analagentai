"""Сервис ведения позиций: работа с базой поверх чистых правил (§7.2 ТЗ 9.1).

ЧТО ДЕЛАЕТ ИТЕРАЦИЯ, по шагам:

  1. ``sync_open_positions`` — по каждой открытой позиции читает бары окна,
     прогоняет правило ``check_exit`` и либо закрывает позицию одним UPDATE,
     либо двигает отметку «докуда разобрано»;
  2. ``open_new_positions`` — отбирает кандидатов, проверяет ``should_open`` и
     открывает позиции в свободных слотах.

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
from src.notify.telegram import send_message
from src.positions import messages
from src.positions.rules import (
    EXIT_DATA_GAP,
    SIDE_BUY,
    Bar,
    check_exit,
    check_gap_exit,
    levels,
    net_pnl,
    qty_for_slot,
    should_open,
    slippage_pct,
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
class IterationStats:
    """Итог итерации целиком."""

    closed: ClosedStats = field(default_factory=ClosedStats)
    opened: OpenedStats = field(default_factory=OpenedStats)


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

        # ДВА УСЛОВИЯ ПРОПУСКА, А НЕ ОДНО (Этап 9.1.1 §6.5). Прежнее «нет
        # новых закрытых баров — не трогаем вовсе» само по себе верно и бережёт
        # базу, но оно закрывало путь ровно к тому случаю, ради которого
        # написан data_gap: позиция без единого нового бара — это и есть
        # позиция, по инструменту которой пропали данные. Пропускаем её теперь
        # только пока ЕЩЁ НЕ ПОРА закрывать её по пробелу.
        gap_deadline = deadline_at + timedelta(
            seconds=settings.POSITION_GAP_GRACE_SEC
        )
        if settle_edge <= last_checked and now < gap_deadline:
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

        # ПУСТОЙ РЯД БОЛЬШЕ НЕ ОЗНАЧАЕТ «идём дальше»: именно он и бывает при
        # пропаже данных. Правило выхода на пустом ряде исхода не даёт (и не
        # должно), поэтому спрашиваем его только когда есть о чём спрашивать.
        decision = (
            check_exit(
                bars=bars,
                target_price=float(row["target_price"]),
                stop_price=float(row["stop_price"]),
                entry_price=entry_price,
                deadline_at=deadline_at,
                cost_pct=float(row["cost_pct"]),
            )
            if bars else None
        )
        # «Докуда разобрано»: при пустом ряде отметка остаётся на месте — ничего
        # нового мы не видели, и двигать её вперёд значило бы соврать.
        seen_until = bars[-1].ts if bars else last_checked

        if decision is None:
            # ИСХОДА НЕТ. Либо окно ещё не кончилось, либо данных нет. Разницу
            # между «ещё рано» и «данных не будет» знает только правило
            # ``check_gap_exit`` — и решает её ОДНИМ способом: по времени.
            decision = check_gap_exit(
                bars=bars,
                entry_price=entry_price,
                deadline_at=deadline_at,
                now=now,
                grace_sec=settings.POSITION_GAP_GRACE_SEC,
            )
        if decision is None:
            if bars:
                await db.touch_position(position_id, seen_until)
                stats.touched += 1
            continue

        by_gap = decision.exit_reason == EXIT_DATA_GAP
        pnl_pct = net_pnl(entry_price, decision.exit_price, float(row["cost_pct"]))
        pnl_usd = float(row["notional_usd"]) * pnl_pct / 100.0
        # Момент закрытия — время ЗАКРЫТИЯ бара выхода: бар прожит целиком, и
        # относить выход к его открытию значило бы закрывать позицию раньше,
        # чем случилось событие, по которому она закрыта.
        #
        # У ЗАКРЫТИЯ ПО ПРОБЕЛУ МОМЕНТ ДРУГОЙ — ``now``. Такая позиция закрыта
        # не событием на графике, а истечением ожидания, и отнести её закрытие
        # к бару, случившемуся часы назад, значило бы записать в журнал момент,
        # в который ничего не происходило.
        closed_at = (
            now if by_gap else decision.exit_bar_ts + timedelta(seconds=60)
        )
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
        if by_gap:
            # ПРЕДУПРЕЖДЕНИЕ, А НЕ info: закрытие по пробелу означает, что сбор
            # данных по инструменту встал, и это событие про СИСТЕМУ, а не про
            # рынок. Пять таких закрытий подряд — повод идти чинить коллектор.
            _log.warning(
                "positions_data_gap=1",
                position_id=position_id, symbol=row["symbol"],
                last_bar_ts=decision.exit_bar_ts.isoformat(),
                gap_sec=int((now - decision.exit_bar_ts).total_seconds()),
                grace_sec=settings.POSITION_GAP_GRACE_SEC,
            )
        _log.info(
            "positions_closed=1",
            position_id=position_id, symbol=row["symbol"],
            exit_reason=decision.exit_reason,
            outcome_certain=decision.outcome_certain,
            net_pnl_pct=round(pnl_pct, 6), net_pnl_usd=round(pnl_usd, 6),
            bars_held=decision.bars_held,
        )
        # ДЛЯ ПРОБЕЛА — СВОЙ ТЕКСТ. Обычное сообщение о закрытии утверждало бы
        # результат, которого не измеряли.
        if by_gap:
            await _send(messages.data_gap_text(
                symbol=str(row["symbol"]),
                entry_price=entry_price,
                exit_price=decision.exit_price,
                last_bar_ts=decision.exit_bar_ts,
                gap_sec=(now - decision.exit_bar_ts).total_seconds(),
                net_pnl_pct=pnl_pct,
                net_pnl_usd=pnl_usd,
                bars_held=decision.bars_held,
            ))
        else:
            await _send(messages.closed_text(
                symbol=str(row["symbol"]),
                exit_reason=decision.exit_reason,
                entry_price=entry_price,
                exit_price=decision.exit_price,
                net_pnl_pct=pnl_pct,
                net_pnl_usd=pnl_usd,
                cost_pct=float(row["cost_pct"]),
                held_sec=(closed_at - opened_at).total_seconds(),
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
    # ЗАНЯТЫЙ КАПИТАЛ — ИЗ ТОГО ЖЕ ЕДИНСТВЕННОГО ЧТЕНИЯ, что и занятые
    # инструменты. Отдельный запрос «сколько денег в позициях» мог бы разойтись
    # с этим списком, закройся позиция между двумя запросами, — и тогда слоты
    # считались бы по одному состоянию базы, а деньги по другому.
    committed = sum(float(row["notional_usd"]) for row in open_rows)
    # ПРИБЫЛЬ НЕ РЕИНВЕСТИРУЕТСЯ: бюджет — постоянная величина из настройки, и
    # накопленный итог закрытых позиций к нему НЕ ПРИБАВЛЯЕТСЯ ни при каких
    # условиях. Иначе поздняя сделка весила бы больше ранней просто потому, что
    # она поздняя, и замер перестал бы быть замером.
    free_capital = float(settings.POSITION_BUDGET_USD) - committed

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
            free_capital_usd=free_capital,
            slot_usd=settings.POSITION_SLOT_USD,
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
        # Свободный капитал уменьшается ровно на РАЗМЕР СЛОТА — ту же величину,
        # что ушла в notional_usd, — так же, как здесь же растёт open_count.
        free_capital -= float(settings.POSITION_SLOT_USD)
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
        ))
    return stats


async def run_once(now: datetime | None = None) -> IterationStats:
    """Одна итерация: сначала ведение открытых, потом открытие новых."""
    now = now or datetime.now(UTC)
    closed = await sync_open_positions(now)
    opened = await open_new_positions(now)
    return IterationStats(closed=closed, opened=opened)


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
        budget_usd=settings.POSITION_BUDGET_USD,
        gap_grace_sec=settings.POSITION_GAP_GRACE_SEC,
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
    }
