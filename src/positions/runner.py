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
from src.notify import rate_limit
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
    # Отказы ПОИМЁННО — строки журнала public.position_rejections (§7.1 ТЗ 9.3).
    # Копятся в итерации и пишутся одной пачкой: вставка на каждый отказ дала
    # бы столько обращений к базе, сколько было кандидатов.
    rejections: list[dict[str, Any]] = field(default_factory=list)

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


# --- ПРЕДОХРАНИТЕЛЬ НА ПОТОК СООБЩЕНИЙ О СДЕЛКАХ (§6.3, §6.4 ТЗ 9.3) --------
#
# ЧТО ЭТО И ЗАЧЕМ. Слотов больше нет, сделок ожидается несколько десятков в
# сутки (§12.1 ТЗ), то есть 40–120 сообщений: два на сделку плюс сводка.
# Владелец эту цену принял. Предохранитель встраивается СРАЗУ и выключенным
# (``NOTIFY_TRADES_MAX_PER_HOUR=0``): если поток окажется невыносимым, решение
# принимается правкой одной строки в ``.env``, а не спешной правкой кода в тот
# самый день, когда всё и так плохо.
#
# СВЕРХ ПОТОЛКА СООБЩЕНИЯ НЕ ТЕРЯЮТСЯ, А СВОРАЧИВАЮТСЯ. Придержанные копятся
# списком в Redis и уходят одной почасовой сводкой, которая В СЧЁТ ПОТОЛКА НЕ
# ВХОДИТ (§6.3): включи её в счёт — и предохранитель глушил бы сам себя, а
# владелец переставал бы узнавать о сделках вовсе.
_TRADE_SENT_KEY = "positions:trades:sent:hour"
_TRADE_HELD_KEY = "positions:trades:held"
_TRADE_ROLLUP_KEY = "positions:trades:rollup_at"
_TRADE_HELD_TTL_SEC = 24 * 3600
# Суточные счётчики отправленных и неотправленных сообщений (§6.4, §13.2 ТЗ).
# Живут неделю — столько же, сколько счётчики отказов, и по той же причине.
_TRADE_STAT_TEMPLATE = "positions:notify:{version}:{day}:{kind}"
_TRADE_STAT_TTL_SEC = 7 * 24 * 3600


def trade_stat_key(version: int, day: str, kind: str) -> str:
    """Имя суточного счётчика сообщений о сделках. Одно место, а не литерал.

    Собирают его ДВОЕ — служба позиций (пишет) и суточная сводка (читает), — и
    две одинаковые строки в двух файлах однажды разошлись бы: счётчики просто
    перестали бы находиться, молча, показывая честный ноль.
    """
    return _TRADE_STAT_TEMPLATE.format(
        version=int(version), day=day, kind=kind
    )


async def _count_trade_message(now: datetime, kind: str) -> None:
    """Считает отправленное и неотправленное сообщение (§6.4 ТЗ 9.3)."""
    try:
        redis = get_redis()
        key = trade_stat_key(
            int(settings.LOGIC_VERSION), now.strftime("%Y-%m-%d"), kind
        )
        await redis.incrby(key, 1)
        await redis.expire(key, _TRADE_STAT_TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — счётчик не важнее позиции
        _log.warning("positions_trade_metric_failed=1", error=str(exc))


async def _hold_trade_message(text: str) -> None:
    """Придерживает сообщение до почасовой сводки (§6.3).

    ОШИБКА REDIS ЗДЕСЬ ТЕРЯЕТ СООБЩЕНИЕ, и молчать об этом нельзя: оно уже не
    ушло в Telegram и теперь не попадёт в сводку. Поэтому потеря считается тем
    же счётчиком ``failed``, что и неудачная отправка, — в суточной сводке она
    и означает ровно это: владелец о сделке не узнал.
    """
    try:
        redis = get_redis()
        await redis.rpush(_TRADE_HELD_KEY, text)
        await redis.expire(_TRADE_HELD_KEY, _TRADE_HELD_TTL_SEC)
    except Exception as exc:  # noqa: BLE001
        _log.warning("notify_trade_failed=1", stage="hold", error=str(exc))
        await _count_trade_message(datetime.now(UTC), "failed")


async def _send(text: str, now: datetime | None = None) -> None:
    """Отправка сообщения о сделке. Молчит, если сообщения выключены настройкой.

    СДЕЛКА ПЕРВИЧНА, СООБЩЕНИЕ ВТОРИЧНО (§6.4 ТЗ 9.3). Неудачная отправка не
    отменяет и не откладывает сделку: она уже открыта или закрыта в базе, и
    несостоявшееся сообщение факта не отменяет. Неотправленное записывается в
    журнал машиночитаемым ключом ``notify_trade_failed`` и попадает счётчиком в
    суточную сводку.
    """
    if not settings.POSITION_NOTIFY_ENABLED:
        return
    now = now or datetime.now(UTC)
    cap = int(settings.NOTIFY_TRADES_MAX_PER_HOUR)
    if cap > 0:
        sent = await rate_limit.sent_last_hour(_TRADE_SENT_KEY, now, True)
        if sent >= cap:
            _log.info(
                "notify_trade_deferred=1", sent_last_hour=sent, cap=cap,
                reason="сверх потолка — уйдёт почасовой сводкой (§6.3 ТЗ 9.3)",
            )
            await _hold_trade_message(text)
            return
    try:
        ok = await send_message(text)
    except Exception as exc:  # noqa: BLE001 — сообщение не важнее позиции
        _log.warning("notify_trade_failed=1", error=str(exc))
        await _count_trade_message(now, "failed")
        return
    if not ok:
        _log.warning("notify_trade_failed=1", reason="Telegram вернул отказ")
        await _count_trade_message(now, "failed")
        return
    await _count_trade_message(now, "sent")
    if cap > 0:
        await rate_limit.record_sent(_TRADE_SENT_KEY, now)


async def _flush_trade_rollup(now: datetime) -> None:
    """Шлёт почасовую сводку придержанных сообщений (§6.3 ТЗ 9.3).

    В СЧЁТ ПОТОЛКА НЕ ВХОДИТ — ``rate_limit.record_sent`` здесь не зовётся, и
    это главное свойство этой функции. Иначе первая же сводка съедала бы часть
    потолка следующего часа, и предохранитель душил бы сам себя.

    ЧАС ОТСЧИТЫВАЕТСЯ ОТ ПРЕДЫДУЩЕЙ СВОДКИ, А НЕ ОТ НАЧАЛА КАЛЕНДАРНОГО ЧАСА —
    по той же причине, по которой скользит окно паузы по токену (§5.2 ТЗ) и
    окно потолка уведомлений: обнуление в начале часа даёт две сводки за две
    минуты на границе.
    """
    if int(settings.NOTIFY_TRADES_MAX_PER_HOUR) <= 0:
        return
    try:
        redis = get_redis()
        last_raw = await redis.get(_TRADE_ROLLUP_KEY)
        last = float(last_raw) if last_raw else 0.0
        if now.timestamp() - last < 3600:
            return
        held = await redis.lrange(_TRADE_HELD_KEY, 0, -1)
        if not held:
            return
        await redis.delete(_TRADE_HELD_KEY)
        await redis.set(_TRADE_ROLLUP_KEY, str(now.timestamp()), ex=7200)
    except Exception as exc:  # noqa: BLE001 — сводка не важнее позиции
        _log.warning("notify_trade_rollup_failed=1", error=str(exc))
        return
    texts = [
        item if isinstance(item, str) else item.decode() for item in held
    ]
    body = "\n\n".join(texts)
    text = (
        f"📦 <b>Придержано сообщений о сделках: {len(texts)}</b>\n"
        f"Потолок {int(settings.NOTIFY_TRADES_MAX_PER_HOUR)} сообщений в час "
        "(NOTIFY_TRADES_MAX_PER_HOUR). Сами сделки идут своим чередом — "
        "придержаны только сообщения о них.\n\n"
        f"{body}"
    )
    try:
        ok = await send_message(text)
    except Exception as exc:  # noqa: BLE001
        ok = False
        _log.warning("notify_trade_failed=1", stage="rollup", error=str(exc))
    if not ok:
        await _count_trade_message(now, "failed")
        return
    _log.info("notify_trade_rollup_sent=1", count=len(texts))


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
                position_id=position_id,
                symbol=str(row["symbol"]),
                entry_price=entry_price,
                exit_price=decision.exit_price,
                last_bar_ts=decision.exit_bar_ts,
                gap_sec=(now - decision.exit_bar_ts).total_seconds(),
                net_pnl_pct=pnl_pct,
                net_pnl_usd=pnl_usd,
                cost_pct=float(row["cost_pct"]),
                held_sec=(closed_at - opened_at).total_seconds(),
                bars_held=decision.bars_held,
            ))
        else:
            await _send(messages.closed_text(
                position_id=position_id,
                symbol=str(row["symbol"]),
                exit_reason=decision.exit_reason,
                entry_price=entry_price,
                exit_price=decision.exit_price,
                net_pnl_pct=pnl_pct,
                net_pnl_usd=pnl_usd,
                cost_pct=float(row["cost_pct"]),
                held_sec=(closed_at - opened_at).total_seconds(),
                # §6.2 ТЗ 9.3: срок берётся из СТРОКИ позиции, а не из
                # настройки — «истёк срок 48 ч» у сделки, жившей сутки, было бы
                # неверным утверждением.
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
    # ГРАНИЦА «ПОСЛЕДНЕГО ЗАВЕДОМО ЗАКРЫТОГО БАРА» нужна дважды: по ней
    # выбирается бар входа и по ней же считается окно паузы. Считается один
    # раз: два вызова в одной итерации дали бы две разные границы, разойдись
    # часы между ними.
    settle_edge = last_closed_bar_open_ts(now)
    # САМЫЙ ПОЗДНИЙ МОМЕНТ, В КОТОРЫЙ КАНДИДАТ ЭТОЙ ИТЕРАЦИИ МОЖЕТ ВОЙТИ.
    # Позиция открывается по ЗАКРЫТИЮ бара входа, а бар входа не новее
    # ``settle_edge``; значит вход не позже ``settle_edge + минута``. Окно
    # паузы отсчитывается ОТ ЭТОГО момента, а не от «сейчас», — по той же
    # причине, по которой от него же считается возраст последнего открытия
    # ниже: иначе окно отбрасывало бы позицию, которая паузу ещё держит.
    latest_entry = settle_edge + timedelta(seconds=60)
    last_open_at: dict[int, datetime] = {}
    if token_pause_sec > 0:
        # ОКНО ПАУЗЫ — СКОЛЬЗЯЩЕЕ, ОТ МЕТКИ ВРЕМЕНИ (§5.2 ТЗ 9.3), а не от
        # начала календарного часа. Прямой урок этапа 8.3: при обнулении в
        # начале часа два входа в 10:59 и 11:01 формально укладываются в
        # правило и дают два входа за две минуты.
        #
        # ВЕРСИЯ ЛОГИКИ ПЕРЕДАЁТСЯ В ЗАПРОС (§5.3): пауза — свойство выборки
        # версии 7, и позиции версии 6, открытые перед развёртыванием, её
        # токены не держат.
        last_open_at = await db.get_last_open_ts_by_instrument(
            latest_entry - timedelta(seconds=token_pause_sec),
            int(settings.LOGIC_VERSION),
        )
    # ЗАНЯТЫЙ КАПИТАЛ И ЧИСЛО ОТКРЫТЫХ ПОЗИЦИЙ. Читаются из того же
    # единственного источника — списка открытых позиций, — а не тремя
    # запросами: три ответа о трёх разных мгновениях базы описали бы состояние,
    # которого не было ни в один момент.
    open_rows = await db.get_open_positions()
    committed = sum(float(row["notional_usd"]) for row in open_rows)
    # ЧИСЛО ОТКРЫТЫХ ПОЗИЦИЙ — ЭТО СЧЁТ СТРОК, А НЕ ХРАНИМАЯ ВЕЛИЧИНА (§7.2
    # ТЗ 9.3: таблица positions не меняется ни одним столбцом).
    open_count = len(open_rows)
    # СКОЛЬКО ОТКРЫТО ПО КАЖДОМУ ТОКЕНУ — для инварианта §4.1. Считается по
    # ВСЕМ версиям логики: инвариант «один инструмент — одна позиция» говорит
    # о занятости инструмента, а инструмент занят независимо от того, каким
    # правилом открыта занявшая его сделка.
    token_open: dict[int, int] = {}
    for row in open_rows:
        key = int(row["instrument_id"])
        token_open[key] = token_open.get(key, 0) + 1
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
        #
        # ВОЗРАСТ СЧИТАЕТСЯ ДО МОМЕНТА ПРЕДПОЛАГАЕМОГО ВХОДА, А НЕ ДО «СЕЙЧАС»,
        # и это не придирка. Позиция открывается по ЗАКРЫТИЮ бара входа, и
        # ``opened_at`` в базе — именно эта метка, на 60 + POSITION_SETTLE_SEC
        # секунд младше «сейчас». Считай мы до «сейчас» — пауза сравнивала бы
        # одну шкалу времени с другой, и расстояние между двумя СОСЕДНИМИ
        # ``opened_at`` систематически выходило бы на полторы минуты короче
        # настройки: при часовой паузе входы по одному токену случались бы раз
        # в 58,5 минуты. Смещение маленькое, постоянное и невидимое — ровно
        # такое, какое не обнаруживает никто.
        #
        # ТО ЖЕ ЧИСЛО ЧИТАЕТСЯ ОБРАТНО (§5.3: ``MAX(opened_at)``), так что
        # измеряется и хранится одна и та же величина.
        #
        # БАРА МОЖЕТ НЕ БЫТЬ ВОВСЕ — тогда момент входа неизвестен, и берётся
        # «сейчас». Такой кандидат всё равно получит отказ ``no_fresh_bar``;
        # выдумывать ему момент входа незачем.
        entry_at = bar_close if bar_close is not None else now
        last_open = last_open_at.get(instrument_id)
        last_open_age_sec = (
            None if last_open is None
            else (entry_at - last_open).total_seconds()
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
            open_count=open_count,
            max_open=int(settings.POSITION_MAX_OPEN),
            token_open_count=token_open.get(instrument_id, 0),
            one_per_token=bool(settings.POSITION_ONE_PER_TOKEN),
        )
        if not verdict.allowed:
            stats.refuse(verdict.reason)
            # ОТКАЗ ЗАПИСЫВАЕТСЯ ПОИМЁННО (§5.5, §7.1 ТЗ 9.3): токен, сигнал,
            # причина, минута. Счётчик Redis отвечает «сколько», журнал
            # контейнера — «в какой строке», а эта запись — «по какому токену
            # и какому сигналу», и только по ней считается доля отказов
            # token_pause среди прошедших порог (предсказание §12.2).
            stats.rejections.append({
                "ts_utc": now,
                "signal_id": int(row["signal_id"]),
                "token": str(row["symbol"]),
                "reason": verdict.reason,
                "logic_version": int(settings.LOGIC_VERSION),
            })
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
        # ЧИСЛО ОТКРЫТЫХ РАСТЁТ В ТОЙ ЖЕ ИТЕРАЦИИ. Без этого потолок
        # ``max_open`` пропускал бы за одну итерацию сколько угодно кандидатов:
        # он сравнивался бы с числом, снятым до первого открытия. Ограничитель,
        # считающий состояние минутной давности, — это не ограничитель.
        open_count += 1
        token_open[instrument_id] = token_open.get(instrument_id, 0) + 1
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
            position_id=position_id,
            symbol=str(row["symbol"]),
            entry_price=entry_price,
            target_price=target_price,
            target_pct=target_pct,
            probability=None if row["probability"] is None
            else float(row["probability"]),
            logic_version=version,
            # §6.2 ТЗ 9.3: «без предела убытка» — это факт СТРОКИ позиции
            # (``stop_pct is None``), а не пересказ настройки.
            stop_pct=stop_pct,
            hold_hours=hold_hours(version),
        ))
    return stats


async def _record_rejections(rows: list[dict[str, Any]]) -> None:
    """Пишет отказы итерации в public.position_rejections (§7.1 ТЗ 9.3).

    ОШИБКА ЗАПИСИ НЕ РОНЯЕТ ИТЕРАЦИЮ — по той же причине, по какой её не
    роняет недоступный Redis: сделки важнее наблюдений за отказами, и служба,
    упавшая на журнале отказов, перестала бы ВЕСТИ открытые позиции. Но молчать
    об этом нельзя: предупреждение с числом непроставленных строк — единственный
    признак, по которому потом объяснится расхождение счётчика Redis с выборкой.
    """
    if not rows:
        return
    try:
        await db.record_position_rejections(rows)
    except Exception as exc:  # noqa: BLE001 — журнал отказов не важнее позиции
        _log.warning(
            "positions_rejection_write_failed=1",
            error=str(exc), lost=len(rows),
        )


async def run_once(now: datetime | None = None) -> IterationStats:
    """Одна итерация: сначала ведение открытых, потом открытие новых."""
    now = now or datetime.now(UTC)
    closed = await sync_open_positions(now)
    opened = await open_new_positions(now)
    await _count_refusals(now, opened.refusals)
    await _record_rejections(opened.rejections)
    # Почасовая сводка придержанных сообщений — в конце итерации: сначала
    # сделки, потом рассказ о них.
    await _flush_trade_rollup(now)
    return IterationStats(closed=closed, opened=opened)


async def _heartbeat() -> None:
    """Отметка времени последней успешной итерации — как у остальных сервисов."""
    now_iso = datetime.now(UTC).isoformat()
    await get_redis().set("positions:heartbeat", now_iso, ex=_HEARTBEAT_TTL)


# ПОЛЯ СТРОКИ ЗАПУСКА, НАЗВАННЫЕ §8 ТЗ 9.3 ПОИМЁННО. Перечень вынесен в
# функцию не ради красоты: §8 требует, чтобы служба печатала при старте ИМЕННО
# эти девять величин, и проверить это можно только тогда, когда их собирает
# одно место, а не форматная строка внутри вечного цикла.
#
# ПЕЧАТАЮТСЯ ФАКТИЧЕСКИ ПРИМЕНЁННЫЕ ЗНАЧЕНИЯ, А НЕ УМОЛЧАНИЯ КОДА (§8 ТЗ).
# Отсюда и обращение к ``settings`` в теле функции: константа, собранная при
# импорте модуля, показывала бы то, что написано в коде, а не то, что стоит в
# ``.env``, — и строка запуска, ради которой всё это и печатается, врала бы
# ровно в тот момент, когда по ней сверяют развёртывание.
STARTUP_FIELDS: tuple[str, ...] = (
    "logic_version", "max_open", "slot_usd", "budget_usd",
    "min_probability", "max_hold_hours", "plus_wait_start_hours",
    "cooldown_sec", "one_per_token",
)


def startup_fields() -> dict[str, Any]:
    """Величины, которые служба позиций называет в журнале при старте (§8 ТЗ 9.3).

    ТРИ ЧИСЛА РЯДОМ НАМЕРЕННО: горизонт СИГНАЛА, срок жизни ПОЗИЦИИ и отметка
    начала ожидания плюса — разные величины, и в журнале запуска они обязаны
    быть видны все три, иначе первая же правка одного из них будет истолкована
    как правка другого.

    ``max_open=0`` И ``budget_usd=0`` ЗДЕСЬ ЧИТАЮТСЯ КАК «ОГРАНИЧЕНИЕ НЕ
    ПРИМЕНЯЕТСЯ», а не как «ноль позиций» и «нет денег» (§3 ТЗ 9.3).

    ``cooldown_sec`` — ПАУЗА В СЕКУНДАХ, ХОТЯ НАСТРОЙКА ЗАДАНА В МИНУТАХ.
    Имя и единица взяты из §8 ТЗ, значение — пересчётом из существующей
    настройки ``POSITION_TOKEN_PAUSE_MIN``. Второй настройки для той же
    величины при этом не заводится (§3 ТЗ прямо это запрещает, и в проекте
    такой дефект уже случался: ``NOTIFY_THRESHOLD`` / ``NOTIFY_MIN_PROBABILITY``).
    """
    return {
        "logic_version": int(settings.LOGIC_VERSION),
        "max_open": int(settings.POSITION_MAX_OPEN),
        "cooldown_sec": int(settings.POSITION_TOKEN_PAUSE_MIN) * 60,
        "one_per_token": bool(settings.POSITION_ONE_PER_TOKEN),
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


def _warn_about_limits_that_are_on() -> None:
    """Говорит вслух об ограничителях ЧИСЛА, если они включены (§3, §4 ТЗ 9.3).

    БОЕВАЯ НАСТРОЙКА ЭТАПА — ОБА ВЫКЛЮЧЕНЫ, и ровно на этом стоит весь замер:
    §12.1 ТЗ предсказывает 15–60 сделок в сутки вместо нынешних 2,75, а
    §13.2 требует назвать числом максимум одновременно открытых позиций.
    Включённый потолок или возвращённый инвариант делают эти числа
    бессмысленными — и узнать об этом владелец обязан из строки запуска, а не
    через неделю по расхождению предсказания с фактом.

    ЭТО ПРЕДУПРЕЖДЕНИЕ, А НЕ ОТКАЗ. Оба выключателя оставлены в коде именно
    затем, чтобы ими пользоваться (§3 ТЗ: «возврат должен быть правкой одной
    строки в .env»); служба, отказавшаяся стартовать с включённым потолком,
    отняла бы у владельца тормоз, ради которого проверки и сохранены.
    """
    if int(settings.POSITION_MAX_OPEN) > 0:
        _log.warning(
            "positions_limit_enabled=1",
            setting="POSITION_MAX_OPEN",
            value=int(settings.POSITION_MAX_OPEN),
            reason=(
                "потолок числа одновременно открытых позиций ВКЛЮЧЁН; боевая "
                "настройка этапа 9.3 — 0, «ограничение не применяется». "
                "Отказы max_open перестанут быть нулевыми, и замер потолка "
                "торговли не состоится"
            ),
        )
    if bool(settings.POSITION_ONE_PER_TOKEN):
        _log.warning(
            "positions_limit_enabled=1",
            setting="POSITION_ONE_PER_TOKEN",
            value=True,
            reason=(
                "инвариант «один инструмент — одна позиция» ВКЛЮЧЁН; боевая "
                "настройка этапа 9.3 — false. Вторых одновременных позиций по "
                "токену не будет, и предсказание §12.5 проверить будет нечем"
            ),
        )


async def run() -> None:
    """Вечный цикл. Не падает ни при каких ошибках итерации."""
    _log.info(
        "Сервис ведения позиций запущен (позиции ВИРТУАЛЬНЫЕ)",
        **startup_fields(),
    )
    _warn_about_limits_that_are_on()
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
