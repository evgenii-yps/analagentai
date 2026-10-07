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

from src.core import fmt
from src.notify.agent import (
    AGENT_ORDER,
    AGENT_RU,
    OPINION_RU,
    agreement_wording,
    compute_agreement,
    normalize_payload,
)
from src.notify.wording import target_block

# Часовой пояс отображения (заказчик в МСК). Сервер и БД живут в UTC.
_MSK = ZoneInfo("Europe/Moscow")

# Максимальная длина одного сообщения Telegram (ТЗ §5: режем по 4000).
MAX_MESSAGE_LEN = 4000

# Русские подписи решения для карточек и списков.
DECISION_RU = {"buy": "покупать", "sell": "продавать", "wait": "ждать"}
DECISION_EMOJI = {"buy": "🟢", "sell": "🔴", "wait": "⚪"}

# Известные команды (для маршрутизации и подсказки в /help).
KNOWN_COMMANDS = (
    "start", "menu", "help", "status", "last", "signals", "signal", "agents", "stats",
    "summary", "settings", "positions", "demo", "account", "trades", "results",
)


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
    """Проценты со знаком, русский формат (обёртка над :func:`fmt.pct`)."""
    return fmt.pct(value)


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

# Ответ на /positions при VIRTUAL_OUTPUT_ENABLED=false (этап 9.5, часть 3, §2.2).
HIDDEN_POSITIONS_TEXT = "Виртуальные сделки скрыты. Демо-счёт — /demo"


def render_hidden_positions() -> str:
    """/positions при выключенном выводе виртуальных сделок: одна строка, без данных."""
    return HIDDEN_POSITIONS_TEXT


def render_unknown() -> str:
    """Ответ на неизвестную команду или произвольный текст."""
    return "Не понимаю команду. Наберите /help — покажу список того, что умею."


def render_status(
    hb_rows: list[tuple[str, str | None, int]],
    db_facts: dict[str, Any],
    now: datetime,
    per_token: list[tuple[str, datetime | None]] | None = None,
    positions: dict[str, Any] | None = None,
    demo: dict[str, Any] | None = None,
) -> str:
    """/status: свежесть heartbeat-ключей, свежесть данных, счётчики сигналов.

    ``hb_rows`` — список ``(ключ, значение_или_None, интервал_цикла_сек)``.
    Порог «устарел» — 5× интервала (как в scripts/watchdog.py).

    ``per_token`` — свежесть данных ПО КАЖДОМУ токену (§4 ТЗ 8.3). Одна общая
    строка на пять токенов бесполезна: сбор мог встать по одному из них, а по
    остальным идти — общий показатель остался бы зелёным.

    ``positions`` — состояние сделок (§5 C6 ТЗ 7): сколько позиций открыто
    прямо сейчас и по каким токенам идёт пауза. УПОМИНАНИЯ СЛОТОВ ЗДЕСЬ НЕТ И
    БЫТЬ НЕ ДОЛЖНО: слотов не существует, и строка «занято 3 из 5» описывала бы
    ограничение, которого нет. Пауза пришла ему на смену — и ровно она теперь
    отвечает на вопрос «почему по этому токену сейчас не входим».
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

    if per_token:
        lines.append("")
        lines.append("<b>Свежесть данных по токенам:</b>")
        for symbol, last_ts in per_token:
            age = age_seconds(now, last_ts)
            token = esc(symbol.split("/", 1)[0])
            if age is None:
                lines.append(f"🔴 {token}: свечей нет")
            elif age > _TOKEN_STALE_SEC:
                lines.append(f"🔴 {token}: {age // 60} мин назад")
            else:
                lines.append(f"🟢 {token}: {age // 60} мин назад")

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

    if positions is not None:
        lines.append("")
        lines.append("<b>Сделки (виртуальные):</b>")
        lines.append(
            f"Открытых позиций: {int(positions.get('open_count') or 0)} "
            "(число не ограничено)"
        )
        pause_min = int(positions.get("token_pause_min") or 0)
        if pause_min <= 0:
            # ВЫКЛЮЧЕННАЯ ПАУЗА НАЗЫВАЕТСЯ ВСЛУХ. Пустой перечень пауз при
            # выключенном антиспаме выглядел бы ровно так же, как пустой
            # перечень при работающем, — и контрольный опыт §9 ТЗ было бы не
            # отличить от обычного дня.
            lines.append("Пауза по токенам выключена (POSITION_TOKEN_PAUSE_MIN=0)")
        else:
            pauses = positions.get("pauses") or []
            if not pauses:
                lines.append(
                    f"Пауза по токенам: {pause_min} мин, сейчас не держит ни один"
                )
            else:
                listed = ", ".join(
                    f"{esc(str(symbol).split('/', 1)[0])} "
                    f"{int(left_sec) // 60} мин"
                    for symbol, left_sec in pauses
                )
                lines.append(f"Пауза по токенам ({pause_min} мин): {listed}")

    # РАЗДЕЛ «ДЕМО-СЧЁТ» (этап 9.5, часть 3, §2.3): на месте раздела «Сделки
    # (виртуальные)» при VIRTUAL_OUTPUT_ENABLED=false. Передаётся только в этом режиме
    # и только при DEMO_ENABLED=true; иначе раздела нет вовсе.
    if demo is not None:
        state = demo["state"]
        diff_pct = (
            (state.equity - state.start_capital) / state.start_capital * 100
            if state.start_capital > 0 else None
        )
        lines.append("")
        lines.append("<b>Демо-счёт:</b>")
        lines.append(f"Баланс итого: {fmt.money(state.equity)}")
        lines.append(f"Открытых сделок: {int(state.open_count)}")
        lines.append("С начала: " + fmt.pct(diff_pct))

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
        # Этап 7.3: величина Decision Agent — ИНДЕКС СОГЛАСИЯ, а не вероятность.
        conviction = fmt.share(s.get("probability"))
        status = "закрыт" if s.get("status") == "closed" else "открыт"
        repeat_mark = " · повтор входов" if s.get("is_repeat") else ""
        head = (
            f"\n{emoji} <b>#{s['id']}</b> · {fmt_msk(s['ts'])}{repeat_mark}\n"
            f"   {decision}, индекс согласия {conviction}, {status}"
        )
        calibrated = s.get("calibrated_probability")
        if calibrated is not None:
            head += f"\n   вероятность успеха (по истории) {fmt.share(calibrated)}"
        lines.append(head)
        if s.get("status") == "closed":
            lines.append(
                f"   4ч: прибыль {_pct(s.get('pnl_pct'))}, "
                f"просадка {_pct(s.get('drawdown_pct'))}, {_hit(s.get('success'))}"
            )
    return "\n".join(lines)


def render_signal_card(
    card: dict[str, Any] | None,
    now: datetime,
    horizon_h: int | None = None,
) -> str:
    """/signal <id>: полная карточка одного сигнала.

    ``horizon_h`` — горизонт, выбранный человеком в /settings. Цель показывается
    ТОЛЬКО для него (§8 ТЗ 8.2): показ всех четырёх целей сразу превратил бы
    карточку в набор чисел, из которых человек выбирал бы удобное.
    """
    if card is None:
        return "Сигнал с таким id не найден. Проверьте номер и повторите."

    emoji = DECISION_EMOJI.get(card["decision"], "⚪")
    decision = DECISION_RU.get(card["decision"], card["decision"])
    conviction = fmt.share(card.get("probability"))

    # Этап 8.1: инструмент называется в заголовке — токенов пять, и без имени
    # карточка не отличает сигнал по XRP от сигнала по BTC.
    symbol = card.get("symbol")
    header = f"{emoji} <b>Сигнал #{card['id']}</b>"
    if symbol:
        header = f"{emoji} <b>Сигнал #{card['id']} · {symbol}</b>"
    lines = [
        header,
        f"Время: {fmt_msk(card['ts'])}",
        f"Решение: {decision}, индекс согласия {conviction}",
    ]
    # Слово «вероятность» — только когда число выведено из фактических исходов.
    calibrated = card.get("calibrated_probability")
    if calibrated is not None:
        mark = ""
        built_at = card.get("calibration_built_at")
        sample = card.get("calibration_sample_size")
        marks = []
        if isinstance(built_at, datetime):
            marks.append(f"кривая от {built_at.strftime('%d.%m')}")
        if sample is not None:
            marks.append(f"N={int(sample)}")
        if marks:
            mark = f"  [{', '.join(marks)}]"
        lines.append(
            f"Вероятность успеха (по истории): {fmt.share(calibrated)}{mark}"
        )
    if card.get("is_repeat"):
        lines.append("Повтор: решение принято на том же наборе мнений, что и предыдущее")
    price = card.get("price_at_signal")
    if price is not None:
        lines.append(f"Цена на момент сигнала: {fmt.price(price)}")

    # Цель и доля её достижения — ВСЕГДА вместе, одним блоком (§8, §12 ТЗ 8.2).
    # Решение 'wait' целей не имеет: цель — это ход в сторону сделки, а сделки нет.
    if card.get("decision") in ("buy", "sell") and horizon_h is not None:
        targets = card.get("targets_by_horizon") or {}
        block = target_block(targets.get(int(horizon_h)))
        lines.append("")
        lines.append(f"<b>Цель на {horizon_h} ч:</b>")
        lines.extend(block)

    payload = normalize_payload(card.get("agents_payload"))
    present = {e.get("agent") for e in payload}
    lines.append("")
    lines.append("<b>Мнения агентов:</b>")
    for name in AGENT_ORDER:
        entry = next((e for e in payload if e.get("agent") == name), None)
        if entry is not None:
            opinion = OPINION_RU.get(entry.get("signal"), entry.get("signal", "?"))
            conf = float(entry.get("confidence", 0.0))
            lines.append(f"• {AGENT_RU[name]}: {opinion} (уверенность {fmt.num(conf)})")
    for name in AGENT_ORDER:
        if name not in present:
            lines.append(f"• {AGENT_RU[name]}: нет данных, в решении не участвовал")

    agreement = compute_agreement(payload)
    if agreement is not None:
        lines.append(
            f"Согласованность: {fmt.num(agreement)} — {agreement_wording(agreement)}"
        )

    lines.append("")
    lines.append("<b>Результаты:</b>")
    # Этап 8.1: четыре горизонта. Подписи берутся из фактических данных, а не
    # зашиты: если состав горизонтов изменится, карточка не начнёт врать.
    evals = card.get("evals_by_horizon") or {}
    if evals:
        for hours in sorted(evals):
            lines.append(f"{hours} ч: " + _render_horizon(evals[hours]))
    else:
        lines.append("1 час: " + _render_horizon(card.get("eval_1h")))
        lines.append("4 часа: " + _render_horizon(card.get("eval_4h")))

    lines.append("")
    lines.append(f"Уведомление: {_notify_status(card)}")

    rationale = card.get("rationale")
    if rationale:
        lines.append("")
        lines.append(f"<b>Пояснение:</b> {esc(rationale)}")
    return "\n".join(lines)


def _render_horizon(ev: dict[str, Any] | None) -> str:
    """Строка результата по горизонту (цена закрытия, прибыль, просадка, угадал)."""
    if not ev:
        return "ещё не оценён"
    return (
        f"цена {fmt.price(ev.get('price_at_close'))}, прибыль {_pct(ev.get('pnl_pct'))}, "
        f"просадка {_pct(ev.get('drawdown_pct'))}, {_hit(ev.get('success'))}"
    )


def _notify_status(card: dict[str, Any]) -> str:
    """Три статуса уведомления (ТЗ §5): отправлен / поглощён / не отправлялся."""
    if card.get("notified_at") is not None:
        return f"отправлен ({fmt_msk(card['notified_at'])})"
    if card.get("notified"):
        return "поглощён анти-спамом"
    return "не отправлялся"


# §4 ТЗ 8.3: ниже этого числа наблюдений цифра не считается надёжной и
# сопровождается оговоркой. Константа, а не число в тексте: границу видно и
# менять её приходится осознанно.
SMALL_SAMPLE_N = 60

# Возраст свежей минутной свечи, после которого токен помечается красным.
# Порог тот же, что в §3 ТЗ 8.1 для остановки: два интервала таймфрейма
# берутся с запасом — 20 минут, чтобы редкая пауза биржи не давала
# ложной тревоги.
_TOKEN_STALE_SEC = 1200


def render_stats(
    blocks: list[tuple[str, dict[str, Any], dict[str, Any]]],
    horizon_h: int,
    now: datetime,
    logic_version: int | None = None,
    version_started_at: datetime | None = None,
    filter_counts: dict[str, Any] | None = None,
) -> str:
    """/stats: попадания по ВЫБРАННОМУ горизонту за каждый период (§4 ТЗ 8.3).

    ``blocks`` — список ``(подпись периода, честная выборка, все подряд)``.
    Бот не выдаёт суждений «хорошо/плохо» — только цифры и, где нужно,
    оговорку о размере выборки.

    ВЕРСИИ ЛОГИКИ НЕ СМЕШИВАЮТСЯ (§7 ТЗ): показывается только текущая, и рядом
    сказано, с какой даты она действует. Без даты «мало наблюдений» невозможно
    истолковать: это может значить и «система молчит», и «версия работает
    вторые сутки».
    """
    lines = [f"<b>📊 Статистика · горизонт {horizon_h} ч</b>"]
    if logic_version is not None:
        since = (
            f", действует с {version_started_at.strftime('%d.%m.%Y %H:%M')} UTC"
            if version_started_at is not None
            else ""
        )
        lines.append(f"Версия логики {logic_version}{since}. Прежние версии не учтены.")
    lines.append("")

    for label, independent, whole in blocks:
        lines.append(f"<b>{label}</b>")
        lines.append("Честная выборка (независимые 4-часовые окна):")
        lines.extend(_stats_body(independent))
        lines.append("Все сигналы подряд:")
        lines.extend(_stats_body(whole))
        lines.append("")

    lines.append("<b>Почему две цифры</b>")
    lines.append(
        "Решение принимается раз в минуту, а результат проверяется через "
        f"{horizon_h} ч. Поэтому сотни соседних сигналов описывают один и тот же "
        "кусок рынка. Ориентируйтесь на честную выборку: только она показывает "
        "реальный размер опыта."
    )

    if filter_counts:
        lines.append("")
        lines.append("<b>Фильтрация уведомлений</b>")
        lines.append(f"Реально отправлено: {filter_counts.get('sent', 0)}")
        lines.append(f"Поглощено анти-спамом: {filter_counts.get('absorbed', 0)}")
    return "\n".join(lines)


def _stats_body(block: dict[str, Any]) -> list[str]:
    """Тело блока статистики: число наблюдений стоит РЯДОМ с каждым процентом."""
    n = int(block.get("n", 0))
    if n == 0:
        return ["· закрытых сигналов пока нет"]
    body = [
        f"· наблюдений: {n} (покупать {block.get('buy', 0)}, "
        f"продавать {block.get('sell', 0)}, ждать {block.get('wait', 0)})",
        f"· угадано: покупать {_rate_with_n(block.get('sr_buy'), block.get('n_buy'))}, "
        f"продавать {_rate_with_n(block.get('sr_sell'), block.get('n_sell'))}",
        f"· средняя прибыль {_pct(block.get('avg_pnl'))}, "
        f"средняя просадка {_pct(block.get('avg_dd'))}",
    ]
    if n < SMALL_SAMPLE_N:
        body.append("⚠️ наблюдений мало, цифра ненадёжна")
    return body


def _rate_with_n(value: Any, n: Any) -> str:
    """Доля с числом наблюдений: ``62% из 84``. §4 ТЗ требует их рядом.

    Процент без знаменателя одинаково выглядит и при трёх наблюдениях, и при
    трёхстах, а значит одинаково читается — хотя весит совершенно по-разному.
    """
    if value is None:
        return "—"
    percent = fmt.share(value)
    count = int(n) if n is not None else None
    return f"{percent} из {count}" if count is not None else percent


def render_summary(
    hb_rows: list[tuple[str, str | None, int]],
    data_counts: list[tuple[str, int]],
    signal_counts: dict[str, Any],
    db_size: str | None,
    now: datetime,
    include_heartbeats: bool = True,
) -> str:
    """Суточная сводка по запросу (часть экрана «Подробно»).

    Метрики БД и Redis бот считает сам (§5). Хостовые метрики (аптайм, диск,
    память, статусы контейнеров) заменены строкой-заглушкой — из контейнера они
    недоступны, а логику host-скрипта daily_report.py трогать нельзя.
    """
    lines = [f"<b>📊 Суточная сводка по запросу</b>\n<i>{fmt_msk(now)}</i>", ""]

    # На экране «Подробно» этот список уже есть в состоянии системы: второй раз он не
    # печатается, иначе экран не помещался бы в одно сообщение.
    if include_heartbeats:
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
    decoded = ", ".join(
        f"{DECISION_RU.get(k, esc(k))}: {v}" for k, v in by_decision.items()
    ) or "нет"
    lines.append(f"По решениям: {decoded}")
    lines.append(f"Отправлено уведомлений: {signal_counts.get('sent', 0)}")
    lines.append(f"Поглощено анти-спамом: {signal_counts.get('absorbed', 0)}")
    lines.append(f"Кандидатов (индекс согласия ≥ порога): {signal_counts.get('candidates', 0)}")
    # Этап 7.3, Блок C: сколько из решений реально несут новую информацию.
    total_24h = sum(by_decision.values())
    repeats = signal_counts.get("repeats", 0)
    repeat_share = f" ({fmt.share(repeats / total_24h)})" if total_24h else ""
    lines.append(f"Из них повторных решений: {repeats}{repeat_share}")
    lines.append(f"Уникальных наборов мнений: {signal_counts.get('unique_inputs', 0)}")
    lines.append(f"Закрыто оценщиком: {signal_counts.get('closed', 0)}")

    lines.append("")
    lines.append(f"<b>🗄 Размер БД:</b> {esc(db_size) if db_size else 'неизвестно'}")

    lines.append("")
    lines.append(
        "🖥 Аптайм, диск, память и статусы контейнеров доступны только в "
        "суточной сводке в 06:00 UTC."
    )
    return "\n".join(lines)


# ПРИЧИНЫ ОТКАЗА ВО ВХОДЕ ЧЕЛОВЕЧЕСКИМ ЯЗЫКОМ (§7.2 ТЗ 9.2). Ключи — те же
# машиночитаемые значения, что считает сервис позиций
# (``src/positions/rules.REFUSAL_REASONS``); человеку показывается перевод.
# Неизвестный ключ печатается как есть: новая причина в журнале лучше молчания.
REFUSAL_RU: dict[str, str] = {
    "not_buy": "решение не «покупать»",
    "wrong_logic_version": "сигнал другой версии логики",
    "degraded": "неполный кворум агентов",
    "low_probability": "вероятность ниже порога",
    # §5 ТЗ 9.3: единственный ограничитель, который в бою срабатывает.
    "token_pause": "пауза по токену после недавнего входа",
    # ОГРАНИЧИТЕЛИ ЧИСЛА СДЕЛОК. При боевых настройках этапа 9.3 недостижимы
    # (POSITION_MAX_OPEN=0, POSITION_ONE_PER_TOKEN=false), и переводы их стоят
    # здесь именно поэтому: включённый одной строкой в ``.env`` тормоз обязан
    # называться в сводке человеческим языком с первой же сработавшей строки,
    # а не машинным ключом, который владелец увидит впервые.
    "max_open": "достигнут потолок одновременно открытых позиций",
    "instrument_busy": "по инструменту уже есть открытая позиция",
    "signal_too_old": "сигнал устарел",
    "no_fresh_bar": "нет свежей свечи для входа",
    "no_frozen_target": "нет замороженной цели",
    "no_free_capital": "не хватает свободных денег",
}


def render_positions(
    open_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    now: datetime,
    days: int = 7,
    capital: dict[str, Any] | None = None,
    budget_usd: float = 0.0,
    slot_usd: float = 0.0,
    logic_version: int | None = None,
    refusals: dict[str, int] | None = None,
) -> str:
    """/positions: открытые виртуальные позиции и итог за окно (§10 ТЗ 9.1).

    СЛОВО «ВИРТУАЛЬНО» СТОИТ В ЗАГОЛОВКЕ и убираться не должно, пока позиции
    виртуальные: человек, открывший бота, не обязан помнить, какой этап проекта
    сейчас идёт.

    ВЫВОДА О ПРИБЫЛЬНОСТИ ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО. На десятке позиций
    разница без доверительного интервала — впечатление, а не измерение.
    Печатаются числа; что они значат, решает не бот.

    ВЕРСИИ ЛОГИКИ РАЗДЕЛЕНЫ (§1.4 ТЗ 9.2). Итог за окно считается по ОДНОЙ
    версии, и она названа; накопленный итог печатается и общим числом, и по
    версиям отдельно. Сложить средние двух правил выхода в одно число значило
    бы описать смесь, а не систему.
    """
    lines = [
        "<b>💼 Виртуальные позиции (Этап 9.2)</b>",
        "<i>Ордера на биржу не отправляются.</i>",
    ]
    if logic_version is not None:
        lines.append(
            f"Версия правила выхода {logic_version}. "
            "Прежние версии в итог за окно не входят."
        )

    # ДВЕ СТРОКИ О ДЕНЬГАХ ПОД ЗАГОЛОВКОМ (Этап 9.1.1 §5.5). Занятый капитал и
    # накопленный итог стоят РАЗДЕЛЬНО и не складываются: прибыль не
    # реинвестируется, бюджет — постоянная величина. Сложенные вместе, они
    # описывали бы счёт, которого нет.
    if capital:
        # БЮДЖЕТ «БЕЗ ОГРАНИЧЕНИЯ» НАЗЫВАЕТСЯ СВОИМ ИМЕНЕМ (§3 A3 ТЗ 7).
        # «занято 12.00 из 0.00 USDT» — строка, которую человек прочтёт как
        # поломку, и будет прав: ноль в настройке означает «деньги не
        # ограничивают», а не «денег нет».
        if float(budget_usd) <= 0:
            lines.append(
                f"Капитал: занято {fmt.num(capital['committed_usd'])} USDT "
                f"(слот {fmt.num(slot_usd)}, бюджет не ограничен)"
            )
        else:
            lines.append(
                f"Капитал: занято {fmt.num(capital['committed_usd'])} из "
                f"{fmt.num(budget_usd)} USDT (слот {fmt.num(slot_usd)})"
            )
        lines.append(
            f"Накопленный итог: {_signed_num(capital['realized_usd'], 6)} USDT "
            "(не реинвестируется)"
        )
        # РАЗБИВКА ПО ВЕРСИЯМ — И ТОЛЬКО КОГДА ВЕРСИЙ БОЛЬШЕ ОДНОЙ. При
        # единственной версии строка «в т.ч. v6: …» повторяла бы строку выше и
        # приучала бы не читать её вовсе.
        by_version = capital.get("by_version") or []
        if len(by_version) > 1:
            parts = ", ".join(
                f"v{int(item['logic_version'])}: "
                f"{_signed_num(item['realized_usd'], 6)} ({int(item['closed'])})"
                for item in by_version
            )
            lines.append(f"По версиям правила (закрытых): {parts}")
    lines.append("")

    lines.append(f"<b>Открыто сейчас: {len(open_rows)}</b>")
    if not open_rows:
        lines.append("Открытых позиций нет.")
    for row in open_rows:
        left = row["deadline_at"] - now
        left_txt = (
            "срок истёк" if left.total_seconds() <= 0
            else f"до срока {int(left.total_seconds() // 3600)} ч "
                 f"{int(left.total_seconds() % 3600 // 60)} мин"
        )
        current = row.get("last_price")
        unreal = row.get("unrealized_pct")
        lines.append("")
        lines.append(f"<b>{esc(row['symbol'])}</b> (сигнал #{int(row['signal_id'])})")
        lines.append(
            f"вход {esc(_num_str(row['entry_price']))} → "
            f"сейчас {esc(_num_str(current)) if current is not None else '—'}"
        )
        lines.append(
            "нереализованный итог с учётом издержек: "
            + (_pct(unreal) if unreal is not None else "— (нет свежей свечи)")
        )
        # У ВЕРСИИ 6 ПРЕДЕЛА НЕТ, И СТРОКА О НЁМ ИСЧЕЗАЕТ ЦЕЛИКОМ (§8.2 ТЗ
        # 9.2). Прочерк на его месте читался бы как «предел есть, но неизвестен».
        if row.get("stop_price") is None or row.get("stop_pct") is None:
            lines.append(
                f"цель {esc(_num_str(row['target_price']))} "
                f"({fmt.pct(row['target_pct'])}) · предела убытка нет"
            )
        else:
            lines.append(
                f"цель {esc(_num_str(row['target_price']))} "
                f"({fmt.pct(row['target_pct'])}) · "
                f"предел {esc(_num_str(row['stop_price']))} "
                f"({fmt.pct(-abs(float(row['stop_pct'])))})"
            )
        lines.append(left_txt)

    closed = int(summary.get("closed") or 0)
    lines.append("")
    lines.append(f"<b>Закрыто за {days} дней: {closed}</b>")
    if closed:
        by_reason = summary.get("by_reason") or []
        decoded = ", ".join(f"{esc(name)}: {n}" for name, n in by_reason) or "нет"
        lines.append(f"По причинам выхода: {decoded}")
        lines.append(f"Средний итог: {_pct(summary.get('avg_net_pnl_pct'))}")
        total_usd = summary.get("sum_net_pnl_usd")
        lines.append(
            "Сумма итогов: "
            + fmt.money_signed(total_usd, 3)
        )
        # ОТДЕЛЬНОЙ СТРОКОЙ и только когда их больше нуля: у этих позиций цель и
        # предел задеты в одной свече, порядок событий внутри минуты неизвестен,
        # и итог взят по пределу — пессимистично.
        uncertain = int(summary.get("uncertain") or 0)
        if uncertain:
            lines.append(
                f"⚠ Из них с неопределённым порядком касаний: {uncertain} "
                f"({fmt.share(uncertain / closed)}) — итог взят по пределу"
            )
        # ЗАКРЫТИЯ ПО ПРОБЕЛУ В ДАННЫХ — ОТДЕЛЬНОЙ СТРОКОЙ И ТОЛЬКО КОГДА ОНИ
        # ЕСТЬ. У них цена выхода не наблюдалась, а восстановлена, поэтому в
        # средние и суммы выше они не входят; но само их число говорит о
        # состоянии сбора данных, и прятать его нельзя. Ноль не печатается:
        # строка «закрыто по пробелу: 0» приучает не читать её вовсе.
        data_gap = int(summary.get("data_gap") or 0)
        if data_gap:
            lines.append(
                f"⚠ Закрыто по пробелу в данных: {data_gap} "
                "(в средние не входят)"
            )
    else:
        lines.append("Закрытых позиций за окно нет.")

    # СКОЛЬКО СИГНАЛОВ СИСТЕМА ПРОПУСТИЛА. С версии 7 главным (и по §8 ТЗ 7
    # ожидаемо преобладающим — больше половины) отказом становится пауза по
    # токену: слотов больше нет, а частота ограничена. Отказы по деньгам при
    # бюджете «без ограничения» обязаны показывать ноль — если они появились,
    # значит бюджет снова конечен, и это видно здесь же.
    if refusals:
        lines.append("")
        lines.append(f"<b>Отказано во входе за {days} дней</b>")
        for reason, count in sorted(
            refusals.items(), key=lambda item: (-item[1], item[0])
        ):
            lines.append(f"{esc(REFUSAL_RU.get(reason, reason))}: {count}")

    return "\n".join(lines)


def _num_str(value: Any) -> str:
    """Цена строкой, русский формат (обёртка над :func:`fmt.price`)."""
    return fmt.price(value)


def _signed_num(value: Any, places: int) -> str:
    """Число со знаком и заданным числом знаков: ``+0,012345``."""
    number = fmt.num(value, places)
    if number == fmt.DASH or number.startswith(fmt.MINUS):
        return number
    return number if float(value) == 0 else "+" + number


def _parse_iso(value: str | None) -> datetime | None:
    """Разбирает ISO-таймстемп heartbeat в datetime (None при ошибке/пустоте)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
