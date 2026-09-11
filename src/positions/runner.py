"""Сервис ведения позиций: работа с базой поверх чистых правил (§7.2 ТЗ 9.1).

ЧТО ДЕЛАЕТ ИТЕРАЦИЯ, по шагам:

  1. ``sync_open_positions`` — по каждой открытой позиции читает бары окна,
     прогоняет правило ``check_exit`` и либо закрывает позицию одним UPDATE,
     либо двигает отметку «докуда разобрано»;
  2. ``open_new_positions`` — отбирает кандидатов, проверяет ``should_open`` и
     открывает позиции по тем, кто прошёл.

ПОРЯДОК СОХРАНЁН, ХОТЯ ПРИЧИНА У НЕГО ТЕПЕРЬ ДРУГАЯ. До версии 7 закрытая на
этой же итерации позиция обязана была освободить слот немедленно, иначе
инструмент простаивал лишнюю минуту. Слотов больше нет, освобождать нечего — но
считать деньги и вести открытые позиции по состоянию НАЧАЛА итерации значило бы
описывать базу, которой уже нет.

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
import math
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
    PLUS_WAIT_MIN_LOGIC_VERSION,
    REASON_TOKEN_PAUSE,
    REFUSAL_TTL_SEC,
    SIDE_BUY,
    Bar,
    breakeven_price,
    check_exit,
    check_exit_plus_wait,
    check_gap_exit,
    levels,
    net_pnl,
    qty_for_slot,
    refusal_key,
    should_open,
    slippage_pct,
    target_price_of,
    token_pause_left_sec,
)

_log = structlog.get_logger().bind(component="positions")

# TTL heartbeat-ключа (секунды) — как у остальных сервисов проекта.
_HEARTBEAT_TTL = 300

# Разрешение, которым ведётся позиция. Записано ограничением positions_resolution_chk
# и здесь повторено единственной константой, а не строковым литералом в трёх местах.
RESOLUTION = "1m"


def without_stop(logic_version: int) -> bool:
    """Ведётся ли позиция ЭТОЙ версии по правилу без предела убытка (Этап 9.2).

    Спрашивается по СТРОКЕ позиции, а не по настройке ``LOGIC_VERSION``, и это
    не педантизм. В таблице лежат позиции обеих версий одновременно: 88 строк
    версии 5 с пределом и растущее число строк версии 6 без него. Ведение
    открытых позиций обязано спрашивать правило у самой строки — иначе после
    подъёма версии сервис попытался бы дочитать НЕДОЗАКРЫТЫЕ позиции версии 5
    новым правилом, то есть пересчитать их (§1.2 и §9.2 ТЗ прямо запрещают).
    """
    return int(logic_version) >= PLUS_WAIT_MIN_LOGIC_VERSION


def hold_hours(logic_version: int) -> int:
    """Срок жизни позиции в часах: 48 у версии 6, горизонт сигнала у версии 5.

    ГОРИЗОНТ СИГНАЛА И СРОК ЖИЗНИ ПОЗИЦИИ — РАЗНЫЕ ВЕЛИЧИНЫ (§3.3 ТЗ 9.2). До
    версии 6 они совпадали, и оттого выглядели одним параметром;
    ``POSITION_HORIZON_H`` этим этапом не трогается вовсе.
    """
    if without_stop(logic_version):
        return int(settings.POSITION_MAX_HOLD_HOURS)
    return int(settings.POSITION_HORIZON_H)


def plus_start_of(row: dict[str, Any]) -> datetime:
    """Момент, с которого у позиции версии 6 начинает проверяться условие плюса.

    ОТСЧИТЫВАЕТСЯ ОТ ``opened_at`` САМОЙ ПОЗИЦИИ, а не от «сейчас» и не от срока:
    правило §3.4 ТЗ 9.1.6 говорит именно «с отметки 24 часа ПОСЛЕ ВХОДА».

    ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ ОБ ЭТОМ СКАЗАНО ВСЛУХ. Отдельной колонки под эту
    отметку в ``positions`` не заведено (§9.3 ТЗ запрещает менять устройство
    таблицы шире заказанного), поэтому число берётся из НЫНЕШНЕЙ настройки. Из
    этого следует ровно одно неприятное свойство: правь
    ``POSITION_PLUS_WAIT_START_HOURS`` при открытых позициях — и уже открытые
    позиции будут дочитаны по новой отметке, а не по той, что действовала при
    входе. Настройку поэтому меняют, когда открытых позиций нет; ограничение
    названо здесь, а не оставлено на догадку.

    ``deadline_at`` при этом берётся ИЗ СТРОКИ и настройкой не пересчитывается:
    срок позиции — записанный факт, и подменять его сегодняшним значением
    значило бы закрыть позицию раньше или позже, чем ей было обещано при входе.
    """
    return row["opened_at"] + timedelta(
        hours=int(settings.POSITION_PLUS_WAIT_START_HOURS)
    )


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


async def _count_refusals(now: datetime, refusals: dict[str, int]) -> None:
    """Складывает отказы итерации в суточные счётчики Redis (§7.2 ТЗ 9.2).

    ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. Владелец обязан видеть, СКОЛЬКО сигналов система
    пропускает, а не узнавать об этом из тишины: журнал сервиса отвечает на
    этот вопрос только тому, кто читает журнал за сутки целиком. С версии 7
    счётчик отвечает ещё и на главный вопрос этапа — сколько потока режет пауза
    по токену (§8 ТЗ предсказывает больше половины всех отклонённых
    кандидатов), — и без него предсказание нечем было бы проверить.

    СЧЁТЧИК НЕ ЗАМЕНЯЕТ ЖУРНАЛ, А ДОПОЛНЯЕТ ЕГО. Каждый отказ по-прежнему
    пишется отдельной строкой ``positions_skipped=1`` с причиной и номером
    сигнала; счётчик отвечает на другой вопрос — «сколько их».

    ОШИБКА REDIS НЕ РОНЯЕТ ИТЕРАЦИЮ. Позиции важнее счётчиков: сервис, упавший
    из-за недоступного Redis, перестал бы ВЕСТИ открытые позиции.
    """
    if not refusals:
        return
    day = now.strftime("%Y-%m-%d")
    version = int(settings.LOGIC_VERSION)
    try:
        redis = get_redis()
        for reason, count in refusals.items():
            key = refusal_key(version, day, reason)
            await redis.incrby(key, int(count))
            await redis.expire(key, REFUSAL_TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — счётчик не важнее позиции
        _log.warning("positions_refusal_metric_failed=1", error=str(exc))


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
        #
        # ПРАВИЛО ВЫБИРАЕТСЯ ПО ВЕРСИИ САМОЙ СТРОКИ (Этап 9.2). Позиции версий
        # 5 и 6 лежат в одной таблице и ведутся РАЗНЫМИ правилами; спросить
        # правило у настройки значило бы дочитывать недозакрытые позиции
        # версии 5 новым правилом — то есть пересчитывать их.
        decision = None
        if bars and without_stop(row["logic_version"]):
            decision = check_exit_plus_wait(
                bars=bars,
                target_price=float(row["target_price"]),
                plus_price=breakeven_price(
                    entry_price, float(row["cost_pct"]), side=str(row["side"])
                ),
                entry_price=entry_price,
                plus_start_at=plus_start_of(row),
                deadline_at=deadline_at,
                cost_pct=float(row["cost_pct"]),
            )
        elif bars:
            decision = check_exit(
                bars=bars,
                target_price=float(row["target_price"]),
                stop_price=float(row["stop_price"]),
                entry_price=entry_price,
                deadline_at=deadline_at,
                cost_pct=float(row["cost_pct"]),
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
                # §8 ТЗ 9.2: сообщение обязано назвать исход человеческим
                # языком и НЕ упоминать предел убытка у версии 6 — его больше
                # нет, и писать о нём означало бы вводить в заблуждение.
                logic_version=int(row["logic_version"]),
                hold_hours=hold_hours(row["logic_version"]),
            ))
    return stats


async def open_new_positions(now: datetime) -> OpenedStats:
    """Отбирает кандидатов и открывает позиции (§4.1–§4.3 ТЗ 9.1, §3–§4 ТЗ 7).

    СВОБОДНЫХ СЛОТОВ БОЛЬШЕ НЕТ — НЕТ И САМИХ СЛОТОВ (§3 ТЗ 7). Число
    одновременно открытых позиций не ограничено ничем, по одному токену их может
    быть сколько угодно, а единственный ограничитель потока — ПАУЗА ПО ТОКЕНУ.
    """
    stats = OpenedStats()
    # МОМЕНТ ПОСЛЕДНЕГО ОТКРЫТИЯ ПО КАЖДОМУ ТОКЕНУ — единственное состояние,
    # которое теперь требуется отбору. Читается ОДНИМ запросом на итерацию, а не
    # запросом на кандидата: кандидатов бывает несколько, и пять обращений к
    # базе ради пяти чисел — это пять сетевых задержек там, где хватает одной.
    #
    # ПРИ ВЫКЛЮЧЕННОЙ ПАУЗЕ (POSITION_TOKEN_PAUSE_MIN=0, контрольный опыт §9 ТЗ)
    # запроса не делается вовсе: спрашивать базу о величине, которая ни на что
    # не влияет, значит тратить время на ответ, который будет отброшен.
    token_pause_sec = float(settings.POSITION_TOKEN_PAUSE_MIN) * 60.0
    last_open_at: dict[int, datetime] = {}
    if token_pause_sec > 0:
        last_open_at = await db.get_last_open_ts_by_instrument(
            now - timedelta(seconds=token_pause_sec)
        )
    # ЗАНЯТЫЙ КАПИТАЛ. Читается из того же единственного источника, что и
    # прежде, — списка открытых позиций.
    open_rows = await db.get_open_positions()
    committed = sum(float(row["notional_usd"]) for row in open_rows)
    # БЮДЖЕТ В РЕЖИМЕ «БЕЗ ОГРАНИЧЕНИЯ» (§3 A3 ТЗ 7). Ноль означает, что деньги
    # не ограничивают число сделок, и свободных денег БЕСКОНЕЧНО много — а не
    # «нисколько». Записано бесконечностью намеренно: вычитание из неё остаётся
    # бесконечностью, и ни одна ветка ниже не требует особого случая.
    #
    # ПРИБЫЛЬ ПРИ ЭТОМ ПО-ПРЕЖНЕМУ НЕ РЕИНВЕСТИРУЕТСЯ: накопленный итог
    # закрытых позиций к бюджету НЕ ПРИБАВЛЯЕТСЯ ни при каких условиях, а
    # размер слота остаётся тем же (POSITION_SLOT_USD=2.0) — именно он делает
    # проценты версии 7 сопоставимыми с версиями 5 и 6.
    budget = float(settings.POSITION_BUDGET_USD)
    free_capital = math.inf if budget <= 0 else budget - committed

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

        # ВОЗРАСТ ПОСЛЕДНЕГО ОТКРЫТИЯ ПО ЭТОМУ ТОКЕНУ. ``None`` означает «по
        # токену не открывались в пределах паузы» — то есть пауза не держит.
        last_open = last_open_at.get(instrument_id)
        last_open_age_sec = (
            None if last_open is None else (now - last_open).total_seconds()
        )
        verdict = should_open(
            decision=str(row["decision"]),
            logic_version=int(row["logic_version"]),
            expected_version=settings.LOGIC_VERSION,
            degraded=bool(row["degraded"]),
            probability=None if row["probability"] is None
            else float(row["probability"]),
            min_probability=settings.POSITION_MIN_PROBABILITY,
            last_open_age_sec=last_open_age_sec,
            token_pause_sec=token_pause_sec,
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
            # ОТКАЗ ПО ПАУЗЕ ПИШЕТСЯ ПОДРОБНЕЕ ОСТАЛЬНЫХ (§4 B4 ТЗ 7): токен,
            # вероятность сигнала и СКОЛЬКО ЖДАТЬ. Без последнего числа по
            # журналу нельзя отличить «пауза только началась» от «пауза
            # кончалась через секунду», а именно из этих отказов и состоит
            # ответ на вопрос, сколько потока режет антиспам.
            if verdict.reason == REASON_TOKEN_PAUSE:
                _log.info(
                    "positions_skipped=1",
                    signal_id=int(row["signal_id"]), symbol=row["symbol"],
                    reason=verdict.reason,
                    probability=None if row["probability"] is None
                    else round(float(row["probability"]), 6),
                    pause_left_sec=int(token_pause_left_sec(
                        last_open_age_sec, token_pause_sec
                    )),
                    token_pause_min=int(settings.POSITION_TOKEN_PAUSE_MIN),
                )
            else:
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
        version = int(settings.LOGIC_VERSION)
        # ПРЕДЕЛА У ВЕРСИИ 6 НЕТ, И ХРАНИТЬ ЕГО ЗАПРЕЩЕНО (§4.1 ТЗ 9.2). Не
        # «есть, но не проверяется», а нет: ``stop_pct`` и ``stop_price``
        # уходят в базу NULL, и это же требует ограничение
        # ``positions_no_stop_chk``. Записать вычисленный, но никогда не
        # срабатывающий уровень значило бы положить в таблицу число, которое
        # любой запрос «сколько сделок закрыл предел» посчитал бы наравне с
        # настоящими.
        #
        # ЦЕЛЬ ПРИ ЭТОМ СЧИТАЕТСЯ ТОЙ ЖЕ ФОРМУЛОЙ, что и у версии 5, но другой
        # функцией: ``levels`` отвергает ``stop_pct <= 0`` — и отвергать
        # обязана (§9.4 ТЗ), — поэтому версия 6 её просто не зовёт, а зовёт
        # ``target_price_of``, которую ``levels`` зовёт и сама.
        if without_stop(version):
            stop_pct = None
            stop_price = None
            target_price = target_price_of(entry_price, target_pct)
        else:
            stop_pct = settings.BARRIER_STOP_PCT
            target_price, stop_price = levels(entry_price, target_pct, stop_pct)
        # opened_at — время ЗАКРЫТИЯ бара входа (§4.2): по закрытию и покупаем.
        opened_at = bar["ts"] + timedelta(seconds=60)
        # СРОК ЖИЗНИ ПОЗИЦИИ, А НЕ ГОРИЗОНТ СИГНАЛА (§3.2, §3.3 ТЗ 9.2):
        # 48 часов у версии 6, прежние 24 у версии 5. ``horizon_h`` в строке
        # остаётся горизонтом СИГНАЛА и не трогается.
        deadline_at = opened_at + timedelta(hours=hold_hours(version))

        position_id = await db.open_position({
            "instrument_id": instrument_id,
            "signal_id": int(row["signal_id"]),
            "logic_version": version,
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
            "deadline_at": deadline_at,
            "last_checked_ts": bar["ts"],
            "resolution": RESOLUTION,
        })
        if position_id is None:
            # Кто-то опередил: гонку закрыла база, а не код. Штатный исход.
            stats.races += 1
            _log.info("positions_race_skipped=1",
                      signal_id=int(row["signal_id"]), stage="open")
            continue

        # ПАУЗА ПО ТОКЕНУ НАЧИНАЕТСЯ НЕМЕДЛЕННО, В ТОЙ ЖЕ ИТЕРАЦИИ. Кандидатов
        # по одному токену в одной итерации бывает несколько (сигналы моложе
        # POSITION_MAX_SIGNAL_AGE_SEC), и не отметь мы открытие здесь — все они
        # вошли бы разом, а пауза начала бы действовать только со следующей
        # минуты. Антиспам, пропускающий пачку и придерживающий одиночек, —
        # это не антиспам.
        last_open_at[instrument_id] = opened_at
        # Свободный капитал уменьшается ровно на РАЗМЕР СЛОТА — ту же величину,
        # что ушла в notional_usd. При бюджете «без ограничения» вычитание из
        # бесконечности остаётся бесконечностью — особый случай не нужен.
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
            deadline_at=deadline_at,
            signal_id=int(row["signal_id"]),
            probability=None if row["probability"] is None
            else float(row["probability"]),
            entry_lag_sec=lag,
            # §8.2 ТЗ 9.2: у версии 6 предела нет, и сообщение о нём молчит.
            plus_price=breakeven_price(
                entry_price, settings.RISK_COST_ROUNDTRIP_PCT
            ) if without_stop(version) else None,
            plus_wait_hours=int(settings.POSITION_PLUS_WAIT_START_HOURS),
        ))
    return stats


async def run_once(now: datetime | None = None) -> IterationStats:
    """Одна итерация: сначала ведение открытых, потом открытие новых."""
    now = now or datetime.now(UTC)
    closed = await sync_open_positions(now)
    opened = await open_new_positions(now)
    await _count_refusals(now, opened.refusals)
    return IterationStats(closed=closed, opened=opened)


async def _heartbeat() -> None:
    """Отметка времени последней успешной итерации — как у остальных сервисов."""
    now_iso = datetime.now(UTC).isoformat()
    await get_redis().set("positions:heartbeat", now_iso, ex=_HEARTBEAT_TTL)


# ПОЛЯ СТРОКИ ЗАПУСКА, НАЗВАННЫЕ §9 ТЗ 7 ПОИМЁННО. Перечень вынесен в функцию
# не ради красоты: §9 требует, чтобы служба печатала при старте ИМЕННО эти
# восемь величин, и проверить это можно только тогда, когда их собирает одно
# место, а не форматная строка внутри вечного цикла.
STARTUP_FIELDS: tuple[str, ...] = (
    "logic_version", "max_open", "token_pause_min", "budget_usd", "slot_usd",
    "max_hold_hours", "plus_wait_start_hours", "min_probability",
)


def startup_fields() -> dict[str, Any]:
    """Величины, которые служба позиций называет в журнале при старте (§9 ТЗ 7).

    ТРИ ЧИСЛА РЯДОМ НАМЕРЕННО: горизонт СИГНАЛА, срок жизни ПОЗИЦИИ и отметка
    начала ожидания плюса — разные величины, и в журнале запуска они обязаны
    быть видны все три, иначе первая же правка одного из них будет истолкована
    как правка другого.

    ``max_open=0`` И ``budget_usd=0`` ЗДЕСЬ ЧИТАЮТСЯ КАК «БЕЗ ОГРАНИЧЕНИЯ», а
    не как «ноль позиций» и «нет денег» (§3 A1, A3 ТЗ 7).
    """
    return {
        "logic_version": int(settings.LOGIC_VERSION),
        "max_open": int(settings.POSITION_MAX_OPEN),
        "token_pause_min": int(settings.POSITION_TOKEN_PAUSE_MIN),
        "budget_usd": float(settings.POSITION_BUDGET_USD),
        "slot_usd": float(settings.POSITION_SLOT_USD),
        "max_hold_hours": int(settings.POSITION_MAX_HOLD_HOURS),
        "plus_wait_start_hours": int(settings.POSITION_PLUS_WAIT_START_HOURS),
        "min_probability": float(settings.POSITION_MIN_PROBABILITY),
        "horizon_h": int(settings.POSITION_HORIZON_H),
        "interval": int(settings.POSITION_INTERVAL),
        "gap_grace_sec": int(settings.POSITION_GAP_GRACE_SEC),
        "settle_sec": int(settings.POSITION_SETTLE_SEC),
    }


def _warn_about_settings_that_do_nothing() -> None:
    """Говорит вслух о настройках, которые больше ни на что не влияют (§3 A1).

    ``POSITION_MAX_OPEN`` остался в настройках — его печатает строка запуска, и
    удалённый ключ уронил бы ``.env`` при откате, — но проверки по нему в коде
    больше нет ни одной. Ненулевое значение поэтому НЕ ограничивает ничего, и
    молчать об этом нельзя: человек, поставивший «3», обязан узнать, что число
    не работает, от службы, а не по расхождению журнала с ожиданием через
    неделю.
    """
    if int(settings.POSITION_MAX_OPEN) != 0:
        _log.warning(
            "positions_setting_ignored=1",
            setting="POSITION_MAX_OPEN",
            value=int(settings.POSITION_MAX_OPEN),
            reason=(
                "с версии 7 число одновременно открытых позиций не "
                "ограничивается; ограничивается частота — "
                "POSITION_TOKEN_PAUSE_MIN"
            ),
        )


async def run() -> None:
    """Вечный цикл. Не падает ни при каких ошибках итерации."""
    _log.info(
        "Сервис ведения позиций запущен (версия логики 7, позиции ВИРТУАЛЬНЫЕ)",
        **startup_fields(),
    )
    _warn_about_settings_that_do_nothing()
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
