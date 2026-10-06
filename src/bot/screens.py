"""Экраны бота: данные → ``(текст, клавиатура)``. ЧИСТЫЕ функции: ни сети, ни базы, ни часов.

Каждый экран — одно сообщение HTML не длиннее :data:`TEXT_LIMIT` символов. Нажатие кнопки
правит то же сообщение, поэтому экраны не знают, как они будут показаны: они отдают
текст и клавиатуру, остальное делает поллер.

Все суммы, проценты, цены и время идут через :mod:`src.core.fmt` (один формат на весь бот
и на сообщения демо). Каждый текст — простым русским языком: владелец не программист.
Машинные ключи (heartbeat, имена полей) человеку не показываются; они остаются только на
экране «Подробно».
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.bot import handlers, nav, settings_menu
from src.bot.nav import NavTarget
from src.core import fmt
from src.core.user_settings import UserSettings
from src.demo.ledger import LedgerState
from src.demo.messages import DEMO_SKIP_RU
from src.notify.agent import AGENT_ORDER, AGENT_RU
from src.positions.messages import exit_ru

# Предел длины одного экрана (у Telegram — 4096; запас на теги и «…и ещё N»).
TEXT_LIMIT = 3900

# Ниже этого числа закрытых сделок выводы делать рано. Только подсказка на экране.
DEMO_SMALL_SAMPLE_N = 30

# Сколько открытых сделок показывает экран «Сделки» и сколько последних сигналов
# получают кнопки-ссылки на карточку.
MAX_OPEN_TRADES = 15
MAX_SIGNAL_BUTTONS = 5
SIGNAL_BUTTONS_PER_ROW = 3

# Порог «нет свежей свечи по монете» — тот же, что в подробном /status.
TOKEN_STALE_SEC = handlers._TOKEN_STALE_SEC

# Русские названия служб (машинные ключи heartbeat — только в «Подробно»).
HEARTBEAT_RU: dict[str, str] = {
    "collector:heartbeat:ohlcv": "свечи",
    "collector:heartbeat:orderbook": "стакан",
    "collector:heartbeat:trades": "сделки",
    "collector:heartbeat:futures": "деривативы",
    "agent:heartbeat:market": "теханализ",
    "agent:heartbeat:liquidity": "ликвидность",
    "agent:heartbeat:futures": "деривативы",
    "decision:heartbeat": "решения",
    "evaluator:heartbeat": "оценка сигналов",
    "positions:heartbeat": "решения о сделках",
    "demo:heartbeat": "демо-исполнение",
    "notify:heartbeat": "уведомления",
}

# Группа каждой службы на экране «Система»: (порядок, ключ группы).
_HEARTBEAT_GROUP: dict[str, str] = {
    "collector:heartbeat:ohlcv": "collect",
    "collector:heartbeat:orderbook": "collect",
    "collector:heartbeat:trades": "collect",
    "collector:heartbeat:futures": "collect",
    "agent:heartbeat:market": "agents",
    "agent:heartbeat:liquidity": "agents",
    "agent:heartbeat:futures": "agents",
    "decision:heartbeat": "decide",
    "evaluator:heartbeat": "evaluate",
    "positions:heartbeat": "trades",
    "demo:heartbeat": "trades",
    "notify:heartbeat": "notify",
}

OPINION_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
NO_DATA_EMOJI = "💤"

PERIOD_LABELS = {
    "today": "Сегодня",
    "7d": "7 дней",
    "30d": "30 дней",
    "all": "Всё время",
}


# --------------------------------------------------------------------------- #
# Общие кусочки.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Flags:
    """Фактические флаги системы: текст экранов говорит ровно то, что включено."""

    notify_signals: bool = False
    demo_enabled: bool = False
    demo_notify: bool = True


Screen = tuple[str, dict[str, Any]]


def esc(value: Any) -> str:
    return html.escape(str(value))


def coin_names(symbols: list[str]) -> list[str]:
    return [symbol.split("/", 1)[0] for symbol in symbols]


def _footer(now: datetime, tz: ZoneInfo) -> str:
    return f"Обновлено {fmt.local_dt(now, tz)}"


def _fit(text: str) -> str:
    """Подстраховка: экран длиннее предела режется по границе строки."""
    if len(text) <= TEXT_LIMIT:
        return text
    cut = text[: TEXT_LIMIT - 3]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    return cut + "\n…"


def _keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows]}


def _footer_keyboard(
    refresh: NavTarget, *rows: list[dict[str, str]]
) -> dict[str, Any]:
    return _keyboard(*rows, nav.footer_row(refresh))


def _coins_word(count: int) -> str:
    """«за 5 монетами», «за 1 монетой»: творительный падеж."""
    return "монетой" if count % 10 == 1 and count % 100 != 11 else "монетами"


def _signals_target(flags: Flags) -> NavTarget:
    """Кнопка «Сигналы» открывает ВСЕ решения, пока сигнальные уведомления выключены."""
    return NavTarget(nav.SIGNALS, mode="s" if flags.notify_signals else "a")


def _change_line(label: str, diff: Any, diff_pct: Any, suffix: str = "") -> str:
    return (
        f"{label}: {fmt.sign_emoji(diff)} {fmt.money_signed(diff)} "
        f"({fmt.pct(diff_pct)}){suffix}"
    )


def _notify_sentence(flags: Flags) -> str:
    """Что бот присылает сам — по фактическим флагам (Д2)."""
    sends: list[str] = []
    if flags.demo_enabled and flags.demo_notify:
        sends.append("о каждой покупке и продаже на демо-счёте")
    if flags.notify_signals:
        sends.append("о сильных сигналах")
    if sends:
        return "Бот сам пишет вам " + " и ".join(sends) + "."
    if flags.demo_enabled:
        return "Бот сам ничего не присылает: смотрите разделы «Сделки» и «Итоги»."
    return "Бот сам ничего не присылает: смотрите разделы меню."


# --------------------------------------------------------------------------- #
# Состояние системы (одни и те же правила для «Системы» и строки на главном экране).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthItem:
    group: str          # collect | agents | decide | evaluate | trades | notify | coins
    name: str           # русское название
    ok: bool
    note: str = ""      # «нет отметки 12 мин» — только у проблемного пункта


def compute_health(
    hb_rows: list[tuple[str, str | None, int]],
    per_token: list[tuple[str, datetime | None]],
    now: datetime,
) -> list[HealthItem]:
    """Пункты состояния в порядке показа. Порог «не работает» — 5 интервалов службы
    (как в ``/status`` и ``scripts/watchdog.py``); свежесть монеты — ``TOKEN_STALE_SEC``.
    """
    items: list[HealthItem] = []
    for key, value, interval in hb_rows:
        name = HEARTBEAT_RU.get(key)
        group = _HEARTBEAT_GROUP.get(key)
        if name is None or group is None:
            continue
        age = handlers.age_seconds(now, handlers._parse_iso(value))
        if age is None:
            items.append(HealthItem(group, name, False, "нет отметки"))
        elif age > 5 * interval:
            items.append(HealthItem(group, name, False, f"нет отметки {fmt.duration(age)}"))
        else:
            items.append(HealthItem(group, name, True))
    for symbol, last_ts in per_token:
        token = symbol.split("/", 1)[0]
        age = handlers.age_seconds(now, last_ts)
        if age is None:
            items.append(HealthItem("coins", token, False, "свечей нет"))
        elif age > TOKEN_STALE_SEC:
            items.append(
                HealthItem("coins", token, False, f"последняя свеча {fmt.duration(age)} назад")
            )
        else:
            items.append(HealthItem("coins", token, True))
    order = {"collect": 0, "agents": 1, "decide": 2, "evaluate": 3, "trades": 4,
             "notify": 5, "coins": 6}
    return sorted(items, key=lambda item: order[item.group])  # стабильная: порядок внутри группы


def health_problems(items: list[HealthItem]) -> list[HealthItem]:
    return [item for item in items if not item.ok]


def _problem_name(item: HealthItem) -> str:
    return f"данные по {item.name}" if item.group == "coins" else item.name


def _mark(item: HealthItem, with_name: bool = True) -> str:
    text = ("✅" if item.ok else "❌") + (f" {esc(item.name)}" if with_name else "")
    if item.note:
        text += f" ({esc(item.note)})"
    return text


# --------------------------------------------------------------------------- #
# 4.1 Главный экран.
# --------------------------------------------------------------------------- #


def main_screen(
    *,
    flags: Flags,
    coins: list[str],
    state: LedgerState | None,
    health: list[HealthItem],
    now: datetime,
    tz: ZoneInfo,
) -> Screen:
    lines = ["🤖 <b>Agent Trade</b>"]
    if coins:
        lines.append(
            f"Следит за {len(coins)} {_coins_word(len(coins))}: "
            + " · ".join(esc(c) for c in coins)
        )
    if flags.demo_enabled:
        lines.append("Сделки — на демо-счёте OKX (деньги не настоящие)")
    else:
        lines.append("Сделки на демо-счёте пока не запущены")
    if flags.notify_signals:
        lines.append("🔔 Сильные сигналы приходят вам в Telegram")
    if flags.demo_enabled and not flags.demo_notify:
        lines.append("🔕 Сообщения о каждой сделке отключены — смотрите раздел «Сделки»")
    lines.append("")

    if not flags.demo_enabled or state is None:
        lines.append("💰 Демо-счёт ещё не запущен")
    else:
        diff = state.equity - state.start_capital
        diff_pct = diff / state.start_capital * 100 if state.start_capital > 0 else None
        lines.append(
            f"💰 Баланс <b>{fmt.money(state.equity)}</b> · "
            f"{fmt.sign_emoji(diff)} {fmt.money_signed(diff)} ({fmt.pct(diff_pct)}) с начала"
        )
        lines.append(
            f"📂 Открыто сделок: {state.open_count} · в рынке {fmt.money(state.in_market)}"
        )

    problems = health_problems(health)
    if not problems:
        lines.append("🩺 Система: ✅ всё работает")
    else:
        more = f" и ещё {len(problems) - 1}" if len(problems) > 1 else ""
        lines.append(f"🩺 Система: ⚠️ проблема: {esc(_problem_name(problems[0]))}{more}")
    lines.append(_footer(now, tz))

    keyboard = _keyboard(
        [nav.button("💰 Счёт", NavTarget(nav.ACCOUNT)),
         nav.button("📂 Сделки", NavTarget(nav.TRADES))],
        [nav.button("📊 Итоги", NavTarget(nav.RESULTS, period=nav.DEFAULT_PERIOD)),
         nav.button("🧠 Агенты", NavTarget(nav.AGENTS))],
        [nav.button("📡 Сигналы", _signals_target(flags)),
         nav.button("🩺 Система", NavTarget(nav.SYSTEM))],
        [nav.button("⚙️ Настройки", NavTarget(nav.SETTINGS)),
         nav.button("❓ Справка", NavTarget(nav.HELP))],
    )
    return _fit("\n".join(lines)), keyboard


# --------------------------------------------------------------------------- #
# 4.2 Счёт.
# --------------------------------------------------------------------------- #


def demo_not_started_screen(title: str, refresh: NavTarget, now: datetime, tz: ZoneInfo) -> Screen:
    text = "\n".join([title, "Демо-счёт ещё не запущен", _footer(now, tz)])
    return text, _footer_keyboard(refresh)


def account_screen(
    *,
    demo: dict[str, Any] | None,
    yesterday_close: Any,
    week: dict[str, Any] | None,
    now: datetime,
    tz: ZoneInfo,
    demo_enabled: bool = True,
) -> Screen:
    """``demo`` — ``{state, mirror_since}`` (``BotQueries.demo_state``); ``week`` — итог
    ``demo_report`` за последние 7×24 часа."""
    title = "💰 <b>Демо-счёт OKX</b>"
    refresh = NavTarget(nav.ACCOUNT)
    if not demo_enabled or demo is None:
        return demo_not_started_screen(title, refresh, now, tz)
    state: LedgerState = demo["state"]
    started = demo["mirror_since"].astimezone(tz).strftime("%d.%m.%Y")
    diff = state.equity - state.start_capital
    diff_pct = diff / state.start_capital * 100 if state.start_capital > 0 else None
    lines = [
        title,
        f"<i>Деньги не настоящие · старт {started} с {fmt.money(state.start_capital)}</i>",
        "",
        f"Итого: <b>{fmt.money(state.equity)}</b>",
        f"Свободно: {fmt.money(state.cash)}",
        f"В рынке: {fmt.money(state.in_market)}",
        _change_line("С начала", diff, diff_pct),
    ]
    if yesterday_close is None or yesterday_close <= 0:
        lines.append("За сегодня: —")
    else:
        today = state.equity - yesterday_close
        lines.append(_change_line("За сегодня", today, today / yesterday_close * 100))
    lines.append("")
    lines.append("<b>За 7 дней</b>")
    closed = int(week["closed"]) if week else 0
    if closed == 0:
        lines.append("Закрытых сделок за 7 дней нет")
    else:
        wins, losses = int(week["wins"]), int(week["losses"])
        lines.append(
            f"Закрыто {closed} · ✅ {wins} · ❌ {losses} · "
            f"в плюс {fmt.share(wins / closed)} из {closed}"
        )
        lines.append(
            f"Прибыль: {fmt.sign_emoji(week['profit_usd'])} "
            f"{fmt.money_signed(week['profit_usd'], 4)} · "
            f"комиссии {fmt.money(week['fees_usd'], 4)}"
        )
    lines.append(_footer(now, tz))
    keyboard = _footer_keyboard(
        refresh,
        [nav.button("📂 Сделки", NavTarget(nav.TRADES)),
         nav.button("📊 Итоги", NavTarget(nav.RESULTS, period=nav.DEFAULT_PERIOD))],
    )
    return _fit("\n".join(lines)), keyboard


# --------------------------------------------------------------------------- #
# 4.3 Сделки.
# --------------------------------------------------------------------------- #


def _left_text(deadline: datetime | None, now: datetime) -> str:
    if deadline is None:
        return "⏳ срок неизвестен"
    left = (deadline - now).total_seconds()
    if left <= 0:
        return "⏳ срок вышел, ждём продажи"
    return f"⏳ осталось {fmt.duration(left)}"


def trades_screen(
    *,
    open_trades: list[dict[str, Any]] | None,
    recent_closed: list[dict[str, Any]],
    now: datetime,
    tz: ZoneInfo,
    demo_enabled: bool = True,
) -> Screen:
    """``open_trades=None`` — демо ещё не запускалось."""
    title = "📂 <b>Сделки на демо-счёте</b>"
    refresh = NavTarget(nav.TRADES)
    if not demo_enabled or open_trades is None:
        return demo_not_started_screen(title, refresh, now, tz)
    lines = [title, ""]
    opened = sorted(open_trades, key=lambda t: t.get("opened_at") or now, reverse=True)
    lines.append(f"<b>Открыто: {len(opened)}</b>")
    if not opened:
        lines.append("Открытых сделок нет")
    for trade in opened[:MAX_OPEN_TRADES]:
        target = ""
        if trade.get("target_price") is not None:
            target = f" · цель {fmt.price(trade['target_price'])}"
            if trade.get("target_pct") is not None:
                target += f" ({fmt.pct(trade['target_pct'])})"
        lines.append(
            f"{fmt.sign_emoji(trade['current_pct'])} <b>{esc(trade['token'])}</b> "
            f"#{trade['position_id']} · {fmt.pct(trade['current_pct'])}"
        )
        lines.append(
            f"   вход {fmt.price(trade['entry_price'])} → сейчас "
            f"{fmt.price(trade.get('current_price'))}{target}"
        )
        lines.append(f"   {_left_text(trade.get('deadline_at'), now)}")
    if len(opened) > MAX_OPEN_TRADES:
        lines.append(f"…и ещё {len(opened) - MAX_OPEN_TRADES}")
    if recent_closed:
        lines.append("")
        lines.append("<b>Последние закрытые</b>")
        for trade in recent_closed:
            mark = "✅" if trade["profit"] > 0 else "❌"
            lines.append(
                f"{mark} {esc(trade['token'])} #{trade['position_id']} · "
                f"{fmt.pct(trade['pct'])} · {fmt.money_signed(trade['profit'], 4)} · "
                f"{fmt.duration(trade['hold_sec'])} · "
                f"{esc(exit_ru(trade['exit_reason'], trade.get('hold_hours')))}"
            )
    lines.append(_footer(now, tz))
    keyboard = _footer_keyboard(
        refresh,
        [nav.button("💰 Счёт", NavTarget(nav.ACCOUNT)),
         nav.button("📊 Итоги", NavTarget(nav.RESULTS, period=nav.DEFAULT_PERIOD))],
    )
    return _fit("\n".join(lines)), keyboard


# --------------------------------------------------------------------------- #
# 4.4 Итоги.
# --------------------------------------------------------------------------- #


def skip_text(reason: str) -> str:
    return esc(DEMO_SKIP_RU.get(reason, reason))


def results_screen(
    *,
    period: str,
    report: dict[str, Any],
    now: datetime,
    tz: ZoneInfo,
) -> Screen:
    period = period if period in PERIOD_LABELS else nav.DEFAULT_PERIOD
    label = PERIOD_LABELS[period].lower()
    refresh = NavTarget(nav.RESULTS, period=period)
    lines = [f"📊 <b>Итоги демо-счёта · {label}</b>"]
    closed = int(report["closed"])
    if closed == 0:
        lines.append("За период закрытых сделок нет")
    else:
        wins, losses = int(report["wins"]), int(report["losses"])
        lines.append(f"Закрыто сделок: {closed}")
        lines.append(
            f"✅ в плюс {wins} · ❌ в минус {losses} · "
            f"доля {fmt.share(wins / closed)} из {closed}"
        )
        average = (
            f" (в среднем {fmt.pct(report['avg_pct'])} на сделку)"
            if report.get("avg_pct") is not None else ""
        )
        lines.append(
            f"Прибыль: {fmt.sign_emoji(report['profit_usd'])} "
            f"{fmt.money_signed(report['profit_usd'], 4)}{average}"
        )
        best, worst = report.get("best"), report.get("worst")
        if best and worst:
            lines.append(
                f"Лучшая: {fmt.pct(best['pct'])} ({esc(best['token'])} #{best['position_id']}) · "
                f"худшая: {fmt.pct(worst['pct'])} ({esc(worst['token'])} #{worst['position_id']})"
            )
        lines.append(f"Комиссии: {fmt.money(report['fees_usd'], 4)}")
        lines.append(f"Среднее время в сделке: {fmt.duration(report.get('avg_hold_sec'))}")
        skipped = report.get("skipped_by_reason") or {}
        total_skipped = sum(skipped.values())
        reasons = ", ".join(
            f"{skip_text(reason)} {count}"
            for reason, count in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(
            f"Пропущено сигналов: {total_skipped}" + (f" — {reasons}" if reasons else "")
        )
        if int(report.get("errors") or 0) > 0:
            lines.append(f"⚠️ Ошибки биржи: {int(report['errors'])}")
        if closed < DEMO_SMALL_SAMPLE_N:
            lines.append(f"⚠️ Сделок меньше {DEMO_SMALL_SAMPLE_N} — выводы делать рано.")
    lines.append(_footer(now, tz))

    period_row = [
        nav.button(
            ("• " if key == period else "") + PERIOD_LABELS[key],
            NavTarget(nav.RESULTS, period=key),
        )
        for key in nav.PERIODS
    ]
    keyboard = _footer_keyboard(
        refresh,
        period_row,
        [nav.button("🎯 Качество сигналов", NavTarget(nav.QUALITY))],
    )
    return _fit("\n".join(lines)), keyboard


def quality_screen(stats_text: str) -> Screen:
    """«Качество сигналов» — прежний текст /stats с кнопками возврата."""
    keyboard = _keyboard([
        nav.button("← Итоги", NavTarget(nav.RESULTS, period=nav.DEFAULT_PERIOD)),
        nav.home_button(),
    ])
    return _fit(stats_text), keyboard


# --------------------------------------------------------------------------- #
# 4.5 Агенты.
# --------------------------------------------------------------------------- #


def agents_screen(
    *,
    tokens: list[str],
    by_token: dict[str, dict[str, dict[str, Any]]],
    decisions: dict[str, dict[str, Any]],
    freshness_sec: int,
    flags: Flags,
    now: datetime,
    tz: ZoneInfo,
) -> Screen:
    order = " · ".join(AGENT_RU[name].lower() for name in AGENT_ORDER)
    lines = ["🧠 <b>Что думают агенты</b>", f"<i>Порядок: {order}</i>", ""]
    for token in tokens:
        cells = []
        for name in AGENT_ORDER:
            row = (by_token.get(token) or {}).get(name)
            age = handlers.age_seconds(now, row["ts"]) if row else None
            emoji = OPINION_EMOJI.get(row["signal"]) if row else None
            if row is None or emoji is None or age is None or age > freshness_sec:
                cells.append(NO_DATA_EMOJI)
            else:
                cells.append(f"{emoji} {fmt.num(row['confidence'])}")
        decision = decisions.get(token)
        if decision is None:
            verdict = "решений пока нет"
        else:
            verdict = handlers.DECISION_RU.get(decision["decision"], str(decision["decision"]))
            if decision["decision"] in ("buy", "sell") and decision.get("probability") is not None:
                verdict += f", согласие {fmt.share(decision['probability'])}"
        lines.append(f"<b>{esc(token)}</b> {' · '.join(cells)} → {esc(verdict)}")
    lines.append("")
    lines.append("🟢 рост · 🔴 падение · ⚪ нейтрально · 💤 нет свежих данных")
    lines.append(
        f"Работают {len(AGENT_ORDER)} агента из 5: новости и ончейн ещё не подключены."
    )
    lines.append(_footer(now, tz))
    keyboard = _footer_keyboard(
        NavTarget(nav.AGENTS), [nav.button("📡 Сигналы", _signals_target(flags))]
    )
    return _fit("\n".join(lines)), keyboard


# --------------------------------------------------------------------------- #
# 4.6 Сигналы.
# --------------------------------------------------------------------------- #


def signals_screen(
    *,
    signals: list[dict[str, Any]],
    mode: str,
    flags: Flags,
    now: datetime,
    tz: ZoneInfo,
) -> Screen:
    mode = mode if mode in nav.SIGNAL_MODES else "s"
    refresh = NavTarget(nav.SIGNALS, mode=mode)
    lines = [
        "📡 <b>Последние решения системы</b>" if not flags.notify_signals
        else "📡 <b>Последние сильные сигналы</b>" if mode == "s"
        else "📡 <b>Последние решения системы</b>"
    ]
    if not flags.notify_signals:
        lines.append("<i>Уведомления о сигналах выключены — система сообщает только о сделках</i>")
    lines.append("")
    if not signals:
        if mode == "s":
            lines.append(
                "Сильных сигналов, отправленных вам, нет. "
                "Нажмите «Показать все решения»."
            )
        else:
            lines.append("Решений пока нет")
    for signal in signals:
        decision = signal["decision"]
        token = str(signal.get("symbol") or "?").split("/", 1)[0]
        lines.append(
            f"{handlers.DECISION_EMOJI.get(decision, '⚪')} #{signal['id']} · {esc(token)} · "
            f"{handlers.DECISION_RU.get(decision, esc(decision))} · "
            f"согласие {fmt.share(signal.get('probability'))} · {fmt.local_dt(signal['ts'], tz)}"
        )
    lines.append(_footer(now, tz))

    buttons = [
        nav.button(
            f"#{signal['id']} {str(signal.get('symbol') or '?').split('/', 1)[0]}",
            NavTarget(nav.SIGNAL_CARD, mode=mode, signal_id=int(signal["id"])),
        )
        for signal in signals[:MAX_SIGNAL_BUTTONS]
    ]
    rows = [
        buttons[i:i + SIGNAL_BUTTONS_PER_ROW]
        for i in range(0, len(buttons), SIGNAL_BUTTONS_PER_ROW)
    ]
    toggle = (
        nav.button("Показать все решения", NavTarget(nav.SIGNALS, mode="a"))
        if mode == "s"
        else nav.button("Только сильные", NavTarget(nav.SIGNALS, mode="s"))
    )
    return _fit("\n".join(lines)), _footer_keyboard(refresh, *rows, [toggle])


def signal_card_screen(card_text: str, mode: str) -> Screen:
    mode = mode if mode in nav.SIGNAL_MODES else "s"
    keyboard = _keyboard([
        nav.button("← К сигналам", NavTarget(nav.SIGNALS, mode=mode)),
        nav.home_button(),
    ])
    return _fit(card_text), keyboard


# --------------------------------------------------------------------------- #
# 4.8 Система.
# --------------------------------------------------------------------------- #


def system_screen(*, health: list[HealthItem], now: datetime, tz: ZoneInfo) -> Screen:
    by_group: dict[str, list[HealthItem]] = {}
    for item in health:
        by_group.setdefault(item.group, []).append(item)
    problems = health_problems(health)
    lines = [
        "🩺 <b>Состояние системы</b>",
        "✅ <b>Всё работает</b>" if not problems else "⚠️ <b>Есть проблема</b>",
        "",
    ]

    def listed(label: str, group: str) -> None:
        items = by_group.get(group)
        if items:
            lines.append(f"{label}: " + " · ".join(_mark(item) for item in items))

    listed("Сбор данных", "collect")
    listed("Агенты", "agents")
    singles = []
    for label, group in (("Решения", "decide"), ("Оценка сигналов", "evaluate")):
        if by_group.get(group):
            singles.append(f"{label}: {_mark(by_group[group][0], with_name=False)}")
    if singles:
        lines.append(" · ".join(singles))
    listed("Сделки", "trades")
    if by_group.get("notify"):
        lines.append(f"Уведомления: {_mark(by_group['notify'][0], with_name=False)}")
    listed("Данные по монетам", "coins")
    lines.append(_footer(now, tz))
    keyboard = _footer_keyboard(
        NavTarget(nav.SYSTEM), [nav.button("🔧 Подробно", NavTarget(nav.SYSTEM_DETAIL))]
    )
    return _fit("\n".join(lines)), keyboard


def detail_screen(text: str) -> Screen:
    """«Подробно»: прежние /status и /summary — с машинными ключами, одним экраном."""
    keyboard = _keyboard([nav.button("← Система", NavTarget(nav.SYSTEM)), nav.home_button()])
    return _fit(text), keyboard


# --------------------------------------------------------------------------- #
# 4.7 Настройки.
# --------------------------------------------------------------------------- #


def settings_screen(
    *,
    user: UserSettings,
    instruments: list[tuple[int, str]],
    flags: Flags,
    tz: ZoneInfo,
    now: datetime,
) -> Screen:
    text = settings_menu.menu_text(
        user, instruments, tz=tz, signals_enabled=flags.notify_signals, now=now
    )
    return text, settings_menu.menu_keyboard(user, instruments, tz=tz, now=now)


# --------------------------------------------------------------------------- #
# 4.9 Справка.
# --------------------------------------------------------------------------- #


def help_screen(*, flags: Flags, coins: list[str]) -> Screen:
    count = len(coins)
    watching = (
        f"Agent Trade круглосуточно следит за {count} {_coins_word(count)}"
        + (f" ({', '.join(esc(c) for c in coins)})" if coins else "")
        + ". Раз в минуту три независимых агента — теханализ, ликвидность, деривативы — "
        "оценивают рынок, решающий агент собирает их мнения в решение: покупать, "
        "продавать или ждать."
    )
    if flags.demo_enabled:
        trading = (
            "Когда решение «покупать» достаточно сильное, система покупает монету на "
            "демо-счёте OKX и сама продаёт её по правилам выхода. Деньги не настоящие: "
            "мы проверяем, есть ли у системы преимущество над случаем."
        )
    else:
        trading = "Сделки на демо-счёте пока не запущены."
    lines = [
        "❓ <b>Как читать бота</b>",
        watching,
        trading,
        (
            "Сделки — на демо-счёте OKX, деньги не настоящие. "
            "Реальными деньгами система не торгует."
            if flags.demo_enabled else "Реальными деньгами система не торгует."
        ),
        _notify_sentence(flags),
        "",
        "<b>Словарь</b>",
        "- Согласие агентов — насколько агенты единодушны, 0–100%. "
        "Это не вероятность прибыли.",
        "- Цель — цена, при которой сделка закрывается в плюс.",
        "- Срок — когда сделка закрывается по времени, если цель не достигнута.",
        "- Проскальзывание — насколько цена покупки хуже цены в момент сигнала.",
        "- Свободно / в рынке — деньги на счёте / деньги в открытых сделках.",
        "- Комиссия — плата бирже, около 0,1% с каждой покупки и продажи.",
        "",
        "<b>Команды</b> (то же, что кнопки)",
        "/menu — главный экран · /demo — счёт · /trades — сделки",
        "/results — итоги · /agents — агенты · /signals — сигналы",
        "/status — система · /settings — настройки",
    ]
    return _fit("\n".join(lines)), _keyboard([nav.home_button()])


# --------------------------------------------------------------------------- #
# Прочее.
# --------------------------------------------------------------------------- #


def unknown_screen() -> Screen:
    return handlers.render_unknown(), _keyboard([nav.home_button()])


def stale_button_text() -> str:
    return "Кнопка устарела, откройте /menu"
