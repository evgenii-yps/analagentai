"""Суточная сводка ПО СДЕЛКАМ — третье и последнее сообщение системы (§6.2 ТЗ 9.3).

ТРИ СООБЩЕНИЯ, И БОЛЬШЕ НИКАКИХ. Открытие сделки и закрытие сделки шлёт служба
позиций (``src.positions.messages``); эту сводку — служба уведомлений, раз в
сутки в ``NOTIFY_DAILY_SUMMARY_UTC``. Сообщения о сигналах прекращены флагом
``NOTIFY_SIGNALS_ENABLED=false`` (§6.1).

ПОЧЕМУ СВОДКА ЖИВЁТ ЗДЕСЬ, А НЕ В ХОСТОВОМ ``scripts/health/daily_report.py``,
где уже есть раздел о сделках. Хостовая сводка отвечает на вопрос «жива ли
система»: контейнеры, диск, ошибки, heartbeat-и — и раздел о сделках там
служит признаком работоспособности, а не отчётом о торговле. Эта отвечает на
вопрос «что наторговали», её поля перечислены ТЗ поимённо, и каждое сверяется
с выписанным числом (§10.6). Свести их в одну значило бы либо потерять поля,
либо превратить сообщение о состоянии сервера в торговый отчёт.

СУТКИ — КАЛЕНДАРНЫЕ, ПО UTC, И ЗАКОНЧИВШИЕСЯ. Сводка, вышедшая в 06:00 UTC,
описывает ПРЕДЫДУЩИЕ календарные сутки целиком, а не шесть часов с полуночи и
не «последние 24 часа». Три причины, и все три проверяются:

  * «последние 24 часа» от 06:00 — это отрезок, у которого нет имени: назвать
    его датой в заголовке нельзя, а без даты сводку нельзя сверить ни с чем;
  * местное время дало бы сутки, чья длина дважды в год не равна суткам
    (§11.11 ТЗ — намеренная поломка ровно на этом);
  * незакончившиеся сутки означали бы, что сумма суточных итогов не сходится
    с накопленным итогом — и объяснять расхождение пришлось бы каждый раз.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

# Ключ Redis с датой последней отправленной сводки. Нужен ровно затем, чтобы
# перезапуск контейнера в 06:05 не прислал вторую сводку за те же сутки:
# отметка в памяти процесса перезапуск не переживает, а сводка выходит раз в
# сутки — вторая читается как расхождение данных, а не как повтор.
LAST_SENT_KEY = "notify:daily_trades:last_day"


def parse_hh_mm(value: str) -> tuple[int, int]:
    """``"06:00"`` → ``(6, 0)``. Разбор настройки уже проверен валидатором."""
    hour, minute = str(value).split(":")
    return int(hour), int(minute)


def is_due(now: datetime, at_utc: str, last_sent_day: str | None) -> bool:
    """Пора ли слать сводку прямо сейчас.

    ДВА УСЛОВИЯ, И ОБА ОБЯЗАТЕЛЬНЫ: время суток наступило И за эти сутки ещё не
    слали. Одного времени мало — служба опрашивает себя раз в 30 секунд, и без
    отметки владелец получил бы сводку сто двадцать раз подряд.

    ПОРОГ «НАСТУПИЛО», А НЕ РАВЕНСТВО МИНУТЕ. Равенство ``now.hour == 6 and
    now.minute == 0`` пропустило бы сводку целиком при любой заминке итерации,
    и пропустило бы МОЛЧА: следующая попытка была бы через сутки. Поэтому
    сводка уходит при первой итерации ПОСЛЕ назначенного времени, а отметка
    дня не даёт ей повториться.
    """
    hour, minute = parse_hh_mm(at_utc)
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < moment:
        return False
    return last_sent_day != now.strftime("%Y-%m-%d")


def window_for(now: datetime) -> tuple[datetime, datetime, str]:
    """Границы описываемых суток и их подпись: ПРЕДЫДУЩИЕ календарные сутки UTC.

    Возвращается полуоткрытое окно ``[начало, конец)``. Закрытое с обеих сторон
    посчитало бы сделку, закрывшуюся ровно в полночь, дважды — в двух соседних
    сводках, — и сумма суточных итогов перестала бы сходиться с накопленным.
    """
    midnight = now.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since = midnight - timedelta(days=1)
    return since, midnight, since.strftime("%d.%m.%Y")


def _hhmm(seconds: float | None) -> str:
    """Секунды → ``ЧЧ:ММ``. ``None`` (сделок не было) → прочерк, а не ``00:00``.

    ``00:00`` означало бы «сделки были, и все мгновенные»; прочерк означает
    «сделок не было». Разные утверждения, и путать их нельзя.
    """
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _pct_share(part: int, whole: int) -> str:
    """Доля в процентах; при нулевом знаменателе — прочерк, а не ``0%``."""
    if whole <= 0:
        return "—"
    return f"{round(100.0 * part / whole)}%"


def format_daily_summary(
    *,
    day_label: str,
    logic_version: int,
    stats: dict[str, Any],
    rejections: dict[str, int],
    messages_sent: int = 0,
    messages_failed: int = 0,
) -> str:
    """Текст суточной сводки. ЧИСТАЯ функция: числа → строка.

    ``stats`` — то, что вернул ``db.positions_daily_summary``; ``rejections`` —
    ``db.count_position_rejections``. Ни базы, ни «сейчас» внутри: сводку
    обязано быть можно сверить с выписанным числом (§9, §10.6 ТЗ), а функция,
    сама добывающая свои данные, проверяется только сама собой.

    СТРОКА ОТКАЗОВ ОБЯЗАТЕЛЬНА — это ключевое число этапа (§6.2, §12.2 ТЗ).
    Пауза по токену названа отдельно от «прочего» именно затем, чтобы
    предсказание «не меньше 70% отказов после порога — cooldown» можно было
    проверить, не заходя в базу.
    """
    def num(key: str, default: float = 0.0) -> float:
        value = stats.get(key)
        return default if value is None else float(value)

    closed = int(num("closed"))
    measured = int(num("measured"))
    winners = int(num("winners"))
    gaps = int(num("data_gaps"))
    total_trades = int(num("total_trades"))
    pause = int(rejections.get("token_pause", 0))
    refused = sum(int(value) for value in rejections.values())

    lines = [
        f"📊 <b>Сутки {day_label}</b> · версия логики {int(logic_version)}",
        "",
        f"Открыто сделок: {int(num('opened'))}",
        f"Закрыто сделок: {closed}",
        f"Открыто сейчас: {int(num('open_now'))}",
        "",
        "Из закрытых за сутки:",
    ]
    if measured > 0:
        lines += [
            f"  прибыльных {winners} ({_pct_share(winners, measured)})",
            f"  итог {num('pnl_usd'):+.3f}$ ({num('pnl_pct'):+.2f}%)",
            f"  лучшая {num('best_pct'):+.2f}% · худшая {num('worst_pct'):+.2f}%",
            f"  среднее время в сделке {_hhmm(stats.get('avg_hold_sec'))}",
        ]
    else:
        # ЧЕСТНЫЙ ПРОЧЕРК ВМЕСТО ЧЕСТНЫХ НУЛЕЙ. «итог +0.000$ (+0.00%), лучшая
        # +0.00% · худшая +0.00%» читается как «торговали и вышли в ноль» —
        # утверждение, которого никто не делал.
        lines.append("  измеренных закрытий за сутки нет")
    if gaps > 0:
        # ЗАКРЫТИЯ ПО ПРОБЕЛУ В ДАННЫХ НАЗЫВАЮТСЯ, НО В ИТОГ НЕ ВХОДЯТ: у них
        # цена выхода не наблюдалась, а восстановлена. Прятать их число нельзя
        # — по нему видно состояние коллектора.
        lines.append(f"  ⚠ по пробелу в данных {gaps} (в итог не входят)")
    lines += [
        "",
        f"Накоплено с начала версии {int(logic_version)}:",
        f"  сделок {total_trades}, прибыльных "
        f"{_pct_share(int(num('total_winners')), total_trades)}, "
        f"итог {num('total_pnl_usd'):+.3f}$",
        "",
        f"Отказано во входе: {refused} "
        f"(пауза по токену {pause}, прочее {refused - pause})",
    ]
    # СТРОКА О НЕОТПРАВЛЕННЫХ ПОЯВЛЯЕТСЯ, ТОЛЬКО КОГДА ЕСТЬ ЧТО СООБЩИТЬ.
    # §6.4 ТЗ требует, чтобы неотправленное сообщение «попадало счётчиком в
    # суточную сводку», а §6.2 выписывает образец сводки построчно и такой
    # строки в нём нет. Разрешено в пользу обоих: при нуле образец соблюдён
    # буквально, при ненуле владелец узнаёт о потере — а не остаётся с
    # сообщением, которого не было, и сводкой, которая об этом молчит.
    if int(messages_failed) > 0:
        lines += [
            "",
            f"⚠ Сообщений о сделках не отправлено: {int(messages_failed)} "
            f"(отправлено {int(messages_sent)}). Сделки при этом состоялись: "
            "сообщение вторично, сделка первична",
        ]
    return "\n".join(lines)


# --- Отправка и расписание ---------------------------------------------------
#
# ГРАНИЦА МЕЖДУ ЧИСТОЙ ЧАСТЬЮ И СЛУЖЕБНОЙ ПРОХОДИТ ЗДЕСЬ. Всё выше — числа в
# строку, без базы и без «сейчас», и проверяется сверкой с выписанным числом
# (§9 ТЗ). Всё ниже — обращения к базе, Redis и Telegram, и проверяется
# двойниками.


async def send_once(now: datetime | None = None, force: bool = False) -> bool:
    """Шлёт сводку, если пора. Возвращает True, если сообщение ушло.

    ``force=True`` — послать немедленно, не спрашивая расписание и не трогая
    отметку дня. Нужно ровно одному вызывающему — ручной проверке владельца при
    развёртывании (§15.6 ТЗ), которому ждать до утра нечем.

    ОТМЕТКА ДНЯ СТАВИТСЯ ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ОТПРАВКИ. Поставленная заранее,
    она превратила бы одну неудачу сети в сутки без сводки — а сводка выходит
    раз в сутки, и пропуск неотличим от «нечего было сообщать».

    ОШИБКА НЕ ПОДНИМАЕТСЯ ВЫШЕ: служба уведомлений по построению не падает.
    """
    from src.core.config import settings
    from src.core.db import db
    from src.core.redis_client import get_redis
    from src.notify.telegram import send_message

    log = structlog.get_logger().bind(component="notify-daily-trades")
    now = now or datetime.now(UTC)
    if not settings.POSITION_NOTIFY_ENABLED:
        return False

    last_day: str | None = None
    if not force:
        try:
            raw = await get_redis().get(LAST_SENT_KEY)
            last_day = raw if isinstance(raw, str) else (
                raw.decode() if raw else None
            )
        except Exception as exc:  # noqa: BLE001 — отметка не важнее сводки
            log.warning("notify_daily_mark_read_failed=1", error=str(exc))
        if not is_due(now, settings.NOTIFY_DAILY_SUMMARY_UTC, last_day):
            return False

    since, until, label = window_for(now)
    version = int(settings.LOGIC_VERSION)
    try:
        stats = await db.positions_daily_summary(since, until, version)
        rejections = await db.count_position_rejections(since, until, version)
    except Exception as exc:  # noqa: BLE001 — служба не падает
        log.warning("notify_daily_stats_failed=1", error=str(exc))
        return False

    # СЧЁТЧИКИ СООБЩЕНИЙ ЖИВУТ В REDIS, А НЕ В БАЗЕ, и читаются отдельно от
    # чисел о сделках: недоступный Redis обязан стоить сводке одной строки, а
    # не самой сводки. Сутки те же, что у окна, — календарные UTC.
    sent, failed = await _message_counters(since.strftime("%Y-%m-%d"), version)
    text = format_daily_summary(
        day_label=label, logic_version=version,
        stats=stats, rejections=rejections,
        messages_sent=sent, messages_failed=failed,
    )
    try:
        ok = await send_message(text)
    except Exception as exc:  # noqa: BLE001 — сообщение не важнее службы
        log.warning("notify_daily_send_failed=1", error=str(exc))
        return False
    if not ok:
        log.warning("notify_daily_send_failed=1", day=label)
        return False
    if not force:
        try:
            await get_redis().set(
                LAST_SENT_KEY, now.strftime("%Y-%m-%d"), ex=48 * 3600
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("notify_daily_mark_write_failed=1", error=str(exc))
    log.info("notify_daily_summary_sent=1", day=label, logic_version=version)
    return True


async def _message_counters(day: str, version: int) -> tuple[int, int]:
    """Отправленные и неотправленные сообщения о сделках за сутки (§6.4, §13.2).

    Ошибка Redis стоит сводке одной строки, а не всей сводки: возвращается
    ``(0, 0)``, и строка о потерях не печатается — утверждать «потерь не было»
    на основании недоступного хранилища нельзя, но и пугать владельца
    выдуманным числом тем более.
    """
    from src.core.redis_client import get_redis
    from src.positions.runner import trade_stat_key

    log = structlog.get_logger().bind(component="notify-daily-trades")
    try:
        redis = get_redis()
        raw = [
            await redis.get(trade_stat_key(version, day, kind))
            for kind in ("sent", "failed")
        ]
    except Exception as exc:  # noqa: BLE001 — счётчик не важнее сводки
        log.warning("notify_daily_counters_failed=1", error=str(exc))
        return 0, 0
    return tuple(  # type: ignore[return-value]
        int(value) if value not in (None, "") else 0 for value in raw
    )


async def run_loop(interval_sec: int = 60) -> None:
    """Вечный цикл проверки расписания. Не падает ни при каких ошибках.

    МИНУТНЫЙ ШАГ, А НЕ ``sleep`` ДО НАЗНАЧЕННОГО ВРЕМЕНИ. Сон длиной в сутки
    переживает перезапуск контейнера ровно никак и пересчитывается заново при
    каждом старте; минутная проверка расписания стоит ничего и работает
    одинаково при любом числе перезапусков.
    """
    import asyncio

    log = structlog.get_logger().bind(component="notify-daily-trades")
    while True:
        try:
            await send_once()
        except asyncio.CancelledError:
            log.info("Суточная сводка по сделкам остановлена")
            raise
        except Exception as exc:  # noqa: BLE001 — служба не падает
            log.warning("notify_daily_iteration_failed=1", error=str(exc))
        await asyncio.sleep(interval_sec)
