"""Чистые функции бота: данные → текст ответа и разбор аргументов команд.

Здесь НЕТ обращений к сети и БД — только детерминированные преобразования, чтобы
всю логику формата и разбора аргументов можно было покрыть юнит-тестами. Ввод —
уже прочитанные данные (значения Redis, строки БД); вывод — готовый HTML-текст.

Время везде показывается в МСК с пометкой (сервер живёт в UTC, заказчик — нет).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.notify.agent import (
    AGENT_ORDER,
    AGENT_RU,
    OPINION_RU,
    agreement_wording,
    compute_agreement,
    normalize_payload,
)

# Часовой пояс отображения (заказчик в МСК). Сервер и БД живут в UTC.
_MSK = ZoneInfo("Europe/Moscow")

# Максимальная длина одного сообщения Telegram (ТЗ §5: режем по 4000).
MAX_MESSAGE_LEN = 4000

# Русские подписи решения для карточек и списков.
DECISION_RU = {"buy": "покупать", "sell": "продавать", "wait": "ждать"}
DECISION_EMOJI = {"buy": "🟢", "sell": "🔴", "wait": "⚪"}

# Известные команды (для маршрутизации и подсказки в /help).
KNOWN_COMMANDS = ("start", "help", "status", "last", "signal", "agents", "stats", "summary")


# --------------------------------------------------------------------------- #
# Вспомогательные форматтеры времени и чисел.
# --------------------------------------------------------------------------- #

def esc(text: Any) -> str:
    """Экранирование под parse_mode=HTML (текст из БД может содержать <, >, &)."""
    return html.escape(str(text))


def fmt_msk(ts: datetime | None) -> str:
    """UTC-время → строка ``dd.mm.yyyy HH:MM МСК`` (или прочерк для None)."""
    if ts is None:
        return "—"
    return ts.astimezone(_MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def age_seconds(now: datetime, ts: datetime | None) -> int | None:
    """Возраст отметки в секундах. Отрицательные приводятся к нулю (ТЗ §5).

    Часы контейнеров и БД могут слегка расходиться — «отметка в будущем» дала бы
    отрицательный возраст, поэтому кламп к нулю.
    """
    if ts is None:
        return None
    return max(0, int((now - ts).total_seconds()))


def _pct(value: Any) -> str:
    """Число процентов с двумя знаками либо прочерк для None."""
    if value is None:
        return "—"
    return f"{float(value):+.2f}%"


def _hit(success: Any) -> str:
    """Успех сигнала словами: угадал / не угадал / прочерк."""
    if success is None:
        return "—"
    return "угадал" if success else "не угадал"


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Режет длинный ответ на части ≤ limit, по возможности по границам строк."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        # Одна строка длиннее лимита — режем жёстко по символам.
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# --------------------------------------------------------------------------- #
# Разбор аргументов команд (чистый, тестируемый).
# --------------------------------------------------------------------------- #

def parse_command(text: str) -> tuple[str | None, list[str]]:
    """Разбирает текст в ``(команда, аргументы)``.

    Поддерживает суффикс ``@botname`` (в группах Telegram его добавляет). Команда
    без ведущего ``/`` не считается командой (вернётся None). Регистр команды
    приводится к нижнему.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None, []
    tokens = text.split()
    cmd = tokens[0][1:].split("@", 1)[0].lower()
    return cmd, tokens[1:]


def parse_last_args(args: list[str], max_rows: int, default_n: int = 5) -> tuple[bool, int]:
    """Разбирает аргументы ``/last``: возвращает ``(только_отправленные, N)``.

    * ``/last``          → (True, default_n)
    * ``/last 10``       → (True, 10)
    * ``/last all``      → (False, default_n)
    * ``/last all 10``   → (False, 10)

    N ограничен диапазоном [1, max_rows]. Нечисловой N игнорируется (берётся
    значение по умолчанию), чтобы мусорный ввод не ломал ответ.
    """
    notified_only = True
    rest = list(args)
    if rest and rest[0].lower() == "all":
        notified_only = False
        rest = rest[1:]
    n = default_n
    if rest:
        try:
            n = int(rest[0])
        except ValueError:
            n = default_n
    n = max(1, min(n, max_rows))
    return notified_only, n


def parse_signal_id(args: list[str]) -> int | None:
    """Разбирает ``/signal <id>`` → int или None (нет/некорректный аргумент)."""
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


# Допустимые периоды /stats и человекочитаемые подписи.
STATS_PERIODS = {
    "24h": "за 24 часа",
    "7d": "за 7 дней",
    "30d": "за 30 дней",
    "all": "за всё время",
}


def parse_stats_period(args: list[str], default: str = "7d") -> str:
    """Разбирает период ``/stats``. Неизвестное значение → период по умолчанию."""
    if args and args[0].lower() in STATS_PERIODS:
        return args[0].lower()
    return default


# --------------------------------------------------------------------------- #
# Рендеры ответов (чистые: данные → HTML-текст).
# --------------------------------------------------------------------------- #

def render_help() -> str:
    """/start и /help: краткое описание и список команд человеческим языком."""
    return (
        "🤖 <b>Agent Trade — бот наблюдения</b>\n"
        "Система анализирует рынок BTC тремя агентами и раз в минуту формирует "
        "решение; сильные сигналы приходят вам в Telegram.\n"
        "<b>Система не торгует сама. Все решения принимаете вы.</b>\n\n"
        "<b>Команды (только чтение):</b>\n"
        "/status — жива ли система прямо сейчас\n"
        "/last [N] — последние отправленные сигналы (N по умолчанию 5)\n"
        "/last all [N] — последние сигналы, включая неотправленные\n"
        "/signal &lt;id&gt; — подробный разбор одного сигнала\n"
        "/agents — что три агента думают прямо сейчас\n"
        "/stats [24h|7d|30d|all] — как система отрабатывает (по умолчанию 7d)\n"
        "/summary — суточная сводка по запросу\n"
        "/help — эта справка"
    )


def render_unknown() -> str:
    """Ответ на неизвестную команду или произвольный текст."""
    return "Не понимаю команду. Наберите /help — покажу список того, что умею."


def render_status(
    hb_rows: list[tuple[str, str | None, int]],
    db_facts: dict[str, Any],
    now: datetime,
) -> str:
    """/status: свежесть heartbeat-ключей, свежесть данных, счётчики сигналов.

    ``hb_rows`` — список ``(ключ, значение_или_None, интервал_цикла_сек)``.
    Порог «устарел» — 5× интервала (как в scripts/watchdog.py).
    """
    lines = ["<b>📟 Состояние системы</b>", "", "<b>Сервисы (heartbeat):</b>"]
    problems: list[str] = []
    for key, value, interval in hb_rows:
        ts = _parse_iso(value)
        age = age_seconds(now, ts)
        threshold = 5 * interval
        if age is None:
            lines.append(f"🔴 {esc(key)}: нет отметки")
            problems.append(key)
        elif age > threshold:
            lines.append(f"🔴 {esc(key)}: {age} сек назад (порог 5×{interval})")
            problems.append(key)
        else:
            lines.append(f"🟢 {esc(key)}: {age} сек назад")

    lines.append("")
    lines.append("<b>Свежесть данных:</b>")
    lines.append(f"Последняя свеча (ohlcv): {fmt_msk(db_facts.get('last_ohlcv_ts'))}")
    lines.append(f"Последний стакан: {fmt_msk(db_facts.get('last_orderbook_ts'))}")
    lines.append(f"Последнее решение: {fmt_msk(db_facts.get('last_signal_ts'))}")

    lines.append("")
    lines.append(
        f"Сигналов открыто: {db_facts.get('open_count', 0)}, "
        f"закрыто всего: {db_facts.get('closed_count', 0)}"
    )

    lines.append("")
    if problems:
        lines.append("⚠️ <b>Есть проблемы:</b> " + ", ".join(esc(p) for p in problems))
    else:
        lines.append("✅ <b>Всё работает нормально</b>")
    return "\n".join(lines)


def render_last(signals: list[dict[str, Any]], notified_only: bool, now: datetime) -> str:
    """/last: краткий список последних сигналов."""
    title = "последние отправленные сигналы" if notified_only else "последние сигналы (все)"
    lines = [f"<b>🗒 {title.capitalize()}</b>"]
    if not signals:
        lines.append("Пока нет данных.")
        return "\n".join(lines)
    for s in signals:
        emoji = DECISION_EMOJI.get(s["decision"], "⚪")
        decision = DECISION_RU.get(s["decision"], s["decision"])
        prob = round(float(s["probability"]) * 100) if s.get("probability") is not None else "—"
        status = "закрыт" if s.get("status") == "closed" else "открыт"
        head = (
            f"\n{emoji} <b>#{s['id']}</b> · {fmt_msk(s['ts'])}\n"
            f"   {decision}, вероятность {prob}%, {status}"
        )
        lines.append(head)
        if s.get("status") == "closed":
            lines.append(
                f"   4ч: прибыль {_pct(s.get('pnl_pct'))}, "
                f"просадка {_pct(s.get('drawdown_pct'))}, {_hit(s.get('success'))}"
            )
    return "\n".join(lines)


def render_signal_card(card: dict[str, Any] | None, now: datetime) -> str:
    """/signal <id>: полная карточка одного сигнала."""
    if card is None:
        return "Сигнал с таким id не найден. Проверьте номер и повторите."

    emoji = DECISION_EMOJI.get(card["decision"], "⚪")
    decision = DECISION_RU.get(card["decision"], card["decision"])
    prob = round(float(card["probability"]) * 100) if card.get("probability") is not None else "—"

    lines = [
        f"{emoji} <b>Сигнал #{card['id']}</b>",
        f"Время: {fmt_msk(card['ts'])}",
        f"Решение: {decision}, вероятность {prob}%",
    ]
    price = card.get("price_at_signal")
    if price is not None:
        lines.append(f"Цена на момент сигнала: {round(float(price)):,}".replace(",", " "))

    payload = normalize_payload(card.get("agents_payload"))
    present = {e.get("agent") for e in payload}
    lines.append("")
    lines.append("<b>Мнения агентов:</b>")
    for name in AGENT_ORDER:
        entry = next((e for e in payload if e.get("agent") == name), None)
        if entry is not None:
            opinion = OPINION_RU.get(entry.get("signal"), entry.get("signal", "?"))
            conf = float(entry.get("confidence", 0.0))
            lines.append(f"• {AGENT_RU[name]}: {opinion} (уверенность {conf:.2f})")
    for name in AGENT_ORDER:
        if name not in present:
            lines.append(f"• {AGENT_RU[name]}: нет данных, в решении не участвовал")

    agreement = compute_agreement(payload)
    if agreement is not None:
        lines.append(f"Согласованность: {agreement:.2f} — {agreement_wording(agreement)}")

    lines.append("")
    lines.append("<b>Результаты:</b>")
    lines.append("1 час: " + _render_horizon(card.get("eval_1h")))
    lines.append("4 часа: " + _render_horizon(card.get("eval_4h")))

    lines.append("")
    lines.append(f"Уведомление: {_notify_status(card)}")

    rationale = card.get("rationale")
    if rationale:
        lines.append("")
        lines.append(f"<b>rationale:</b> {esc(rationale)}")
    return "\n".join(lines)


def _render_horizon(ev: dict[str, Any] | None) -> str:
    """Строка результата по горизонту (цена закрытия, прибыль, просадка, угадал)."""
    if not ev:
        return "ещё не оценён"
    price = ev.get("price_at_close")
    price_str = f"{round(float(price)):,}".replace(",", " ") if price is not None else "—"
    return (
        f"цена {price_str}, прибыль {_pct(ev.get('pnl_pct'))}, "
        f"просадка {_pct(ev.get('drawdown_pct'))}, {_hit(ev.get('success'))}"
    )


def _notify_status(card: dict[str, Any]) -> str:
    """Три статуса уведомления (ТЗ §5): отправлен / поглощён / не отправлялся."""
    if card.get("notified_at") is not None:
        return f"отправлен ({fmt_msk(card['notified_at'])})"
    if card.get("notified"):
        return "поглощён анти-спамом"
    return "не отправлялся"


def render_agents(
    agent_rows: dict[str, dict[str, Any] | None],
    freshness_sec: int,
    now: datetime,
) -> str:
    """/agents: последний вывод каждого из трёх агентов и его возраст."""
    lines = ["<b>🧠 Что агенты думают сейчас</b>", ""]
    for name in AGENT_ORDER:
        row = agent_rows.get(name)
        if row is None:
            lines.append(f"• {AGENT_RU[name]}: выводов пока нет")
            continue
        opinion = OPINION_RU.get(row.get("signal"), row.get("signal", "?"))
        conf = float(row.get("confidence", 0.0))
        age = age_seconds(now, row.get("ts"))
        age_str = f"{age} сек назад" if age is not None else "возраст неизвестен"
        stale = age is not None and age > freshness_sec
        tail = " — устарел, в решении не участвует" if stale else ""
        lines.append(
            f"• {AGENT_RU[name]}: {opinion} (уверенность {conf:.2f}), {age_str}{tail}"
        )
    lines.append("")
    lines.append("Работают 3 агента из 5. News и OnChain пока не реализованы.")
    return "\n".join(lines)


def render_stats(
    block1: dict[str, Any],
    block2: dict[str, Any],
    block5: dict[str, Any],
    period: str,
    now: datetime,
) -> str:
    """/stats: пять блоков — честная выборка, все подряд, пояснение, предупреждение,
    фильтрация уведомлений. Бот НЕ выдаёт суждений «хорошо/плохо» — только цифры.
    """
    label = STATS_PERIODS.get(period, period)
    lines = [f"<b>📊 Статистика {label}</b>", ""]

    lines.append("<b>Блок 1 — честная выборка (независимые 4-часовые окна)</b>")
    lines.extend(_stats_body(block1))

    lines.append("")
    lines.append("<b>Блок 2 — все сигналы подряд</b>")
    lines.extend(_stats_body(block2))

    lines.append("")
    lines.append("<b>Блок 3 — почему две цифры</b>")
    lines.append(
        "Решение принимается раз в минуту, а результат проверяется через 4 часа. "
        "Поэтому сотни соседних сигналов описывают один и тот же кусок рынка. "
        "Ориентируйтесь на блок 1: только он показывает реальный размер опыта."
    )

    lines.append("")
    lines.append("<b>Блок 4 — размер выборки</b>")
    if int(block1.get("n", 0)) < 30:
        lines.append("⚠️ Данных пока мало, выводы делать рано.")
    else:
        lines.append("Наблюдений в блоке 1 ≥ 30.")

    lines.append("")
    lines.append("<b>Блок 5 — фильтрация уведомлений</b>")
    lines.append(f"Реально отправлено: {block5.get('sent', 0)}")
    lines.append(f"Поглощено анти-спамом: {block5.get('absorbed', 0)}")
    return "\n".join(lines)


def _stats_body(block: dict[str, Any]) -> list[str]:
    """Тело блока статистики (размер, раскладка, доли, средние)."""
    n = int(block.get("n", 0))
    if n == 0:
        return ["Закрытых сигналов пока нет."]
    return [
        f"Размер выборки: {n}",
        f"Раскладка: buy {block.get('buy', 0)}, sell {block.get('sell', 0)}, "
        f"wait {block.get('wait', 0)}",
        f"Угадано по 4ч: buy {_rate(block.get('sr_buy'))}, sell {_rate(block.get('sr_sell'))}",
        f"Средняя прибыль: {_pct(block.get('avg_pnl'))}, "
        f"средняя просадка: {_pct(block.get('avg_dd'))}",
    ]


def _rate(value: Any) -> str:
    """Доля 0..1 → проценты либо прочерк (нет наблюдений данного типа)."""
    if value is None:
        return "—"
    return f"{round(float(value) * 100)}%"


def render_summary(
    hb_rows: list[tuple[str, str | None, int]],
    data_counts: list[tuple[str, int]],
    signal_counts: dict[str, Any],
    db_size: str | None,
    now: datetime,
) -> str:
    """/summary: суточная сводка по запросу.

    Метрики БД и Redis бот считает сам (§5). Хостовые метрики (аптайм, диск,
    память, статусы контейнеров) заменены строкой-заглушкой — из контейнера они
    недоступны, а логику host-скрипта daily_report.py трогать нельзя.
    """
    lines = [f"<b>📊 Суточная сводка по запросу</b>\n<i>{fmt_msk(now)}</i>", ""]

    lines.append("<b>💓 Heartbeat сервисов</b>")
    for key, value, interval in hb_rows:
        ts = _parse_iso(value)
        age = age_seconds(now, ts)
        if age is None:
            lines.append(f"🔴 {esc(key)}: нет отметки")
        else:
            mark = "🟢" if age <= 5 * interval else "🔴"
            lines.append(f"{mark} {esc(key)}: {age} сек назад")

    lines.append("")
    lines.append("<b>📈 Приток данных за 24 часа</b>")
    for label, count in data_counts:
        mark = "🔴" if count == 0 else "🟢"
        lines.append(f"{mark} {esc(label)}: +{count}")

    lines.append("")
    lines.append("<b>🚦 Сигналы за 24 часа</b>")
    by_decision = signal_counts.get("by_decision") or {}
    decoded = ", ".join(f"{k}: {v}" for k, v in by_decision.items()) or "нет"
    lines.append(f"По решениям: {decoded}")
    lines.append(f"Отправлено уведомлений: {signal_counts.get('sent', 0)}")
    lines.append(f"Поглощено анти-спамом: {signal_counts.get('absorbed', 0)}")
    lines.append(f"Кандидатов (вероятность ≥ порога): {signal_counts.get('candidates', 0)}")
    lines.append(f"Закрыто оценщиком: {signal_counts.get('closed', 0)}")

    lines.append("")
    lines.append(f"<b>🗄 Размер БД:</b> {esc(db_size) if db_size else 'неизвестно'}")

    lines.append("")
    lines.append(
        "🖥 Аптайм, диск, память и статусы контейнеров доступны только в "
        "суточной сводке в 06:00 UTC."
    )
    return "\n".join(lines)


def _parse_iso(value: str | None) -> datetime | None:
    """Разбирает ISO-таймстемп heartbeat в datetime (None при ошибке/пустоте)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
