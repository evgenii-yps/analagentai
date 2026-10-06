"""Этап 9.7, §4: экраны бота — чистые функции «данные → (текст, клавиатура)»."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.bot import nav, screens, settings_menu
from src.bot.poller import HEARTBEAT_KEYS, heartbeat_keys
from src.core import fmt
from src.core.config import settings
from src.core.user_settings import UserSettings, is_quiet_hour
from src.demo.ledger import LedgerState

D = Decimal
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)          # 14:05 МСК
COINS5 = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
COINS3 = ["BTC", "ETH", "SOL"]
STATE = LedgerState(D(1000), D("996.10"), D("4.02"), D("1000.12"), 2)


def fresh_hb(extra_stale: dict[str, int] | None = None, demo: bool = True):
    keys = [k for k, _ in HEARTBEAT_KEYS] + (["demo:heartbeat"] if demo else [])
    rows = []
    for key in keys:
        age = (extra_stale or {}).get(key, 5)
        rows.append((key, (NOW - timedelta(seconds=age)).isoformat(), 30))
    return rows


def health(extra_stale=None, demo=True, coins=COINS5):
    return screens.compute_health(
        fresh_hb(extra_stale, demo), [(f"{c}/USDT", NOW - timedelta(minutes=1)) for c in coins],
        NOW)


# --- Д1–Д3: справка и главный экран говорят правду --------------------------------------


@pytest.mark.parametrize("coins", [COINS5, COINS3])
@pytest.mark.parametrize("signals", [True, False])
@pytest.mark.parametrize("demo", [True, False])
def test_help_and_main_tell_the_truth(coins, signals, demo) -> None:
    flags = screens.Flags(notify_signals=signals, demo_enabled=demo, demo_notify=True)
    help_text, _ = screens.help_screen(flags=flags, coins=coins)
    main_text, _ = screens.main_screen(
        flags=flags, coins=coins, state=STATE if demo else None,
        health=health(coins=coins), now=NOW, tz=MSK)
    for text in (help_text, main_text):
        for token in coins:
            assert token in text                          # список монет — из instruments (Д1)
        for lie in ("рынок BTC", "бот наблюдения", "Система не торгует сама"):
            assert lie not in text
        if not signals:
            assert "сильные сигналы приходят" not in text.lower()    # Д2
    assert f"за {len(coins)} монетами" in help_text and f"за {len(coins)} монетами" in main_text
    assert "<b>Agent Trade" in main_text and "Agent Trade" in help_text          # Д3
    assert ("Реальными деньгами система не торгует" in help_text)
    if demo:
        assert "Сделки — на демо-счёте OKX, деньги не настоящие. Реальными деньгами " \
               "система не торгует" in help_text
        assert "Сделки — на демо-счёте OKX (деньги не настоящие)" in main_text
    else:
        assert "Сделки на демо-счёте пока не запущены" in help_text
        assert "Демо-счёт ещё не запущен" in main_text
    if signals:
        assert "Сильные сигналы приходят вам в Telegram" in main_text
        assert "о сильных сигналах" in help_text


def test_the_main_screen_matches_the_sample() -> None:
    flags = screens.Flags(demo_enabled=True)
    text, keyboard = screens.main_screen(
        flags=flags, coins=COINS5, state=STATE, health=health(), now=NOW, tz=MSK)
    assert text.splitlines() == [
        "🤖 <b>Agent Trade</b>",
        "Следит за 5 монетами: BTC · ETH · SOL · XRP · DOGE",
        "Сделки — на демо-счёте OKX (деньги не настоящие)",
        "",
        "💰 Баланс <b>$1 000,12</b> · 🟢 +$0,12 (+0,01%) с начала",
        "📂 Открыто сделок: 2 · в рынке $4,02",
        "🩺 Система: ✅ всё работает",
        "Обновлено 07.10 14:05 МСК",
    ]
    rows = [[b["text"] for b in row] for row in keyboard["inline_keyboard"]]
    assert rows == [["💰 Счёт", "📂 Сделки"], ["📊 Итоги", "🧠 Агенты"],
                    ["📡 Сигналы", "🩺 Система"], ["⚙️ Настройки", "❓ Справка"]]


def test_the_signals_button_opens_all_decisions_while_signal_messages_are_off() -> None:
    def target(signals: bool) -> str:
        _, kb = screens.main_screen(
            flags=screens.Flags(notify_signals=signals), coins=COINS5, state=None,
            health=health(), now=NOW, tz=MSK)
        return kb["inline_keyboard"][2][0]["callback_data"]

    assert target(False) == "v1:sig:a" and target(True) == "v1:sig:s"


def test_the_main_screen_names_the_first_problem_and_counts_the_rest() -> None:
    stale = {"demo:heartbeat": 700, "notify:heartbeat": 700, "decision:heartbeat": 700}
    text, _ = screens.main_screen(
        flags=screens.Flags(demo_enabled=True), coins=COINS5, state=STATE,
        health=health(stale), now=NOW, tz=MSK)
    # первая по порядку экрана «Система»: решения → уведомления → демо; не машинный ключ
    assert "🩺 Система: ⚠️ проблема: решения и ещё 2" in text
    assert ":heartbeat" not in text
    one, _ = screens.main_screen(
        flags=screens.Flags(demo_enabled=True), coins=COINS5, state=STATE,
        health=health({"demo:heartbeat": 700}), now=NOW, tz=MSK)
    assert "⚠️ проблема: демо-исполнение" in one and "и ещё" not in one


def test_the_money_lines_become_one_phrase_when_the_demo_has_not_started() -> None:
    for flags, state in ((screens.Flags(demo_enabled=False), STATE),
                         (screens.Flags(demo_enabled=True), None)):
        text, kb = screens.main_screen(
            flags=flags, coins=COINS5, state=state, health=health(), now=NOW, tz=MSK)
        assert "💰 Демо-счёт ещё не запущен" in text and "Баланс" not in text
        assert len(kb["inline_keyboard"]) == 4              # «Счёт» и «Сделки» остаются


def test_notification_flags_change_the_text() -> None:
    muted, _ = screens.main_screen(
        flags=screens.Flags(demo_enabled=True, demo_notify=False), coins=COINS5, state=STATE,
        health=health(), now=NOW, tz=MSK)
    assert "Сообщения о каждой сделке отключены" in muted
    helptext, _ = screens.help_screen(
        flags=screens.Flags(demo_enabled=True, demo_notify=False), coins=COINS5)
    assert "Бот сам ничего не присылает" in helptext


# --- Д4–Д5: «Система» ---------------------------------------------------------------------


def test_the_demo_heartbeat_is_watched_only_while_the_demo_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    assert ("demo:heartbeat", "DEMO_INTERVAL") in heartbeat_keys()
    monkeypatch.setattr(settings, "DEMO_ENABLED", False)
    assert ("demo:heartbeat", "DEMO_INTERVAL") not in heartbeat_keys()
    assert heartbeat_keys() == HEARTBEAT_KEYS
    assert hasattr(settings, "DEMO_INTERVAL")


def test_every_watched_service_has_a_russian_name() -> None:
    for key, _ in [*HEARTBEAT_KEYS, ("demo:heartbeat", "DEMO_INTERVAL")]:
        assert key in screens.HEARTBEAT_RU and key in screens._HEARTBEAT_GROUP, key
    assert screens.HEARTBEAT_RU["demo:heartbeat"] == "демо-исполнение"


def test_the_system_screen_has_no_machine_keys_and_matches_the_sample() -> None:
    text, keyboard = screens.system_screen(health=health(), now=NOW, tz=MSK)
    assert ":heartbeat" not in text and "heartbeat" not in text.lower()
    assert text.splitlines() == [
        "🩺 <b>Состояние системы</b>",
        "✅ <b>Всё работает</b>",
        "",
        "Сбор данных: ✅ свечи · ✅ стакан · ✅ сделки · ✅ деривативы",
        "Агенты: ✅ теханализ · ✅ ликвидность · ✅ деривативы",
        "Решения: ✅ · Оценка сигналов: ✅",
        "Сделки: ✅ решения о сделках · ✅ демо-исполнение",
        "Уведомления: ✅",
        "Данные по монетам: ✅ BTC · ✅ ETH · ✅ SOL · ✅ XRP · ✅ DOGE",
        "Обновлено 07.10 14:05 МСК",
    ]
    assert keyboard["inline_keyboard"][0][0] == {
        "text": "🔧 Подробно", "callback_data": "v1:sysd"}


def test_a_problem_is_marked_with_the_age() -> None:
    text, _ = screens.system_screen(
        health=health({"demo:heartbeat": 12 * 60}), now=NOW, tz=MSK)
    assert "⚠️ <b>Есть проблема</b>" in text
    assert "❌ демо-исполнение (нет отметки 12 мин)" in text
    assert ":heartbeat" not in text


def test_a_missing_beat_and_a_stale_coin_are_problems() -> None:
    rows = [(k, None if k == "notify:heartbeat" else NOW.isoformat(), 30)
            for k, _ in HEARTBEAT_KEYS]
    items = screens.compute_health(
        rows, [("BTC/USDT", NOW - timedelta(minutes=30)), ("ETH/USDT", None),
               ("SOL/USDT", NOW)], NOW)
    text, _ = screens.system_screen(health=items, now=NOW, tz=MSK)
    assert "Уведомления: ❌ (нет отметки)" in text
    assert "❌ BTC (последняя свеча 30 мин назад)" in text
    assert "❌ ETH (свечей нет)" in text and "✅ SOL" in text


def test_the_demo_row_is_absent_when_the_demo_is_off() -> None:
    text, _ = screens.system_screen(health=health(demo=False), now=NOW, tz=MSK)
    assert "демо-исполнение" not in text and "решения о сделках" in text


# --- Д6: агенты по каждой монете ----------------------------------------------------------


def agent_row(signal, conf, age_sec=10):
    return {"signal": signal, "confidence": conf, "ts": NOW - timedelta(seconds=age_sec)}


def test_agents_are_shown_per_coin_with_their_own_opinions_and_decision() -> None:
    by_token = {
        "BTC": {"market": agent_row("bullish", 0.72), "liquidity": agent_row("neutral", 0.4),
                "futures": agent_row("bearish", 0.55)},
        "ETH": {"market": agent_row("bullish", 0.81), "liquidity": agent_row("bullish", 0.66),
                "futures": agent_row("neutral", 0.3)},
        "SOL": {"market": agent_row("bullish", 0.6), "liquidity": agent_row("neutral", 0.45),
                "futures": agent_row("bullish", 0.5, age_sec=4000)},      # устарел
    }
    decisions = {"BTC": {"decision": "wait", "probability": 0.41},
                 "ETH": {"decision": "buy", "probability": 0.74}}
    text, keyboard = screens.agents_screen(
        tokens=["BTC", "ETH", "SOL", "XRP"], by_token=by_token, decisions=decisions,
        freshness_sec=300, flags=screens.Flags(), now=NOW, tz=MSK)
    lines = text.splitlines()
    assert "<b>BTC</b> 🟢 0,72 · ⚪ 0,40 · 🔴 0,55 → ждать" in lines
    assert "<b>ETH</b> 🟢 0,81 · 🟢 0,66 · ⚪ 0,30 → покупать, согласие 74%" in lines
    assert "<b>SOL</b> 🟢 0,60 · ⚪ 0,45 · 💤 → решений пока нет" in lines     # старый → 💤
    assert "<b>XRP</b> 💤 · 💤 · 💤 → решений пока нет" in lines
    assert "<i>Порядок: теханализ · ликвидность · деривативы</i>" in lines
    assert "Работают 3 агента из 5: новости и ончейн ещё не подключены." in text
    assert keyboard["inline_keyboard"][-1][0]["callback_data"] == "v1:ag"


def test_an_unusable_opinion_is_shown_as_no_data() -> None:
    by_token = {"BTC": {"market": agent_row("insufficient_data", 0.0)}}
    text, _ = screens.agents_screen(
        tokens=["BTC"], by_token=by_token, decisions={}, freshness_sec=300,
        flags=screens.Flags(), now=NOW, tz=MSK)
    assert "<b>BTC</b> 💤 · 💤 · 💤" in text


# --- Д8: тихие часы по МСК, хранение в UTC ---------------------------------------------------


def test_quiet_hours_are_chosen_in_moscow_time_and_stored_in_utc() -> None:
    user = UserSettings(chat_id=1)
    instruments = [(1, "BTC/USDT")]
    updated, note = settings_menu.apply_callback(user, "qt", "23:8", instruments, tz=MSK, now=NOW)
    assert (updated.quiet_from, updated.quiet_to) == (20, 4)          # UTC, включительно
    assert note == "Тишина: с 23:00 до 08:00 МСК"
    assert settings_menu.quiet_text(updated, MSK, NOW) == "с 23:00 до 08:00 МСК"
    text = settings_menu.menu_text(updated, instruments, tz=MSK, now=NOW)
    assert "🔕 <b>Тихие часы:</b> 23:00–08:00 МСК" in text


def test_stored_quiet_hours_work_across_midnight_for_the_senders() -> None:
    """Окно хранится в UTC (20–4); отправители проверяют его единой is_quiet_hour."""
    user = UserSettings(chat_id=1, quiet_from=20, quiet_to=4)

    def quiet(hour_utc: int) -> bool:
        return is_quiet_hour(user, datetime(2026, 10, 7, hour_utc, 30, tzinfo=UTC))

    assert [quiet(h) for h in (19, 20, 23, 0, 3, 4, 5)] == [False, True, True, True, True,
                                                            True, False]
    # то же самое по Москве: 22:30 МСК → не тихо, 23:30 → тихо, 07:30 → тихо, 08:30 → не тихо
    assert not quiet(19) and quiet(20) and quiet(4) and not quiet(5)


def test_the_quiet_range_shown_back_equals_the_one_chosen() -> None:
    for start in range(24):
        for end in range(24):
            if start == end:
                continue
            updated, _ = settings_menu.apply_callback(
                UserSettings(chat_id=1), "qt", f"{start}:{end}", [], tz=MSK, now=NOW)
            assert settings_menu.quiet_bounds(updated, MSK, NOW) == (start, end)


def test_equal_start_and_end_is_refused_and_quiet_can_be_turned_off() -> None:
    user = UserSettings(chat_id=1)
    same, note = settings_menu.apply_callback(user, "qt", "5:5", [], tz=MSK, now=NOW)
    assert same == user and "совпадают" in note
    on, _ = settings_menu.apply_callback(user, "qt", "23:8", [], tz=MSK, now=NOW)
    off, _ = settings_menu.apply_callback(on, "qoff", "", [], tz=MSK, now=NOW)
    assert off.quiet_from is None and off.quiet_to is None
    assert "🔕 <b>Тихие часы:</b> выключены" in settings_menu.menu_text(off, [], tz=MSK, now=NOW)


def test_the_settings_screen_explains_what_each_setting_affects() -> None:
    instruments = [(1, "BTC/USDT"), (2, "ETH/USDT")]
    user = UserSettings(chat_id=1, instruments=(1, 2), horizon_h=24, min_score=0.7,
                        quiet_from=20, quiet_to=4)
    text, keyboard = screens.settings_screen(
        user=user, instruments=instruments, flags=screens.Flags(notify_signals=False),
        tz=MSK, now=NOW)
    assert text.splitlines() == [
        "⚙️ <b>Настройки</b>", "",
        "🔕 <b>Тихие часы:</b> 23:00–08:00 МСК",
        "   В эти часы сообщения о сделках приходят без звука.",
        "   Тревоги о сбоях — всегда со звуком.", "",
        "📡 <b>Для разделов «Сигналы» и «Качество сигналов»:</b>",
        "   Монеты: BTC, ETH", "   Горизонт: 24 ч", "   Порог силы: 0,70",
        "ℹ️ Уведомления о сигналах сейчас выключены.",
    ]
    assert keyboard["inline_keyboard"][-1] == [nav.home_button()]
    on, _ = screens.settings_screen(
        user=user, instruments=instruments, flags=screens.Flags(notify_signals=True),
        tz=MSK, now=NOW)
    assert "выключены" not in on.split("Порог силы")[1]
    buttons = [b["text"] for row in keyboard["inline_keyboard"] for b in row]
    assert "Тишина: с 23:00 до 08:00 МСК" in buttons


def test_the_old_settings_callbacks_are_untouched() -> None:
    for data in ("tok:3", "hor:12", "thr:0.80", "quiet", "qoff", "qf:22", "qt:22:6", "menu"):
        assert settings_menu.parse_callback(data) is not None
        assert nav.parse_nav(data) is None            # навигация их не перехватывает


# --- Д9–Д13 и остальные экраны -------------------------------------------------------------


def test_signals_screen_default_wording_and_buttons() -> None:
    rows = [{"id": 12345 - i, "symbol": f"{c}/USDT", "decision": d, "probability": p,
             "ts": NOW - timedelta(minutes=i)}
            for i, (c, d, p) in enumerate([("BTC", "buy", 0.72), ("SOL", "wait", 0.41),
                                           ("ETH", "sell", 0.8), ("XRP", "wait", 0.3),
                                           ("DOGE", "wait", 0.2), ("BTC", "wait", 0.1)])]
    text, keyboard = screens.signals_screen(
        signals=rows, mode="s", flags=screens.Flags(notify_signals=True), now=NOW, tz=MSK)
    assert "📡 <b>Последние сильные сигналы</b>" in text
    assert "🟢 #12345 · BTC · покупать · согласие 72% · 07.10 14:05 МСК" in text
    assert "🔴 #12343 · ETH · продавать · согласие 80%" in text
    buttons = keyboard["inline_keyboard"]
    assert [b["text"] for b in buttons[0]] == ["#12345 BTC", "#12344 SOL", "#12343 ETH"]
    assert [b["text"] for b in buttons[1]] == ["#12342 XRP", "#12341 DOGE"]   # до пяти кнопок
    assert buttons[0][0]["callback_data"] == "v1:sc:12345:s"
    assert buttons[-2][0] == {"text": "Показать все решения", "callback_data": "v1:sig:a"}
    all_text, all_kb = screens.signals_screen(
        signals=rows, mode="a", flags=screens.Flags(notify_signals=True), now=NOW, tz=MSK)
    assert "Последние решения системы" in all_text
    assert all_kb["inline_keyboard"][-2][0]["text"] == "Только сильные"
    assert [b["text"] for b in all_kb["inline_keyboard"][-1]] == ["🔄 Обновить", "🏠 Меню"]


def test_signals_screen_says_so_when_signal_messages_are_off() -> None:
    text, _ = screens.signals_screen(
        signals=[], mode="a", flags=screens.Flags(notify_signals=False), now=NOW, tz=MSK)
    assert "Последние решения системы" in text
    assert "Уведомления о сигналах выключены — система сообщает только о сделках" in text
    strong, _ = screens.signals_screen(
        signals=[], mode="s", flags=screens.Flags(notify_signals=False), now=NOW, tz=MSK)
    assert "Сильных сигналов, отправленных вам, нет" in strong


def test_signal_card_screen_goes_back_to_the_right_list() -> None:
    _, keyboard = screens.signal_card_screen("карточка", "a")
    assert keyboard["inline_keyboard"] == [[
        {"text": "← К сигналам", "callback_data": "v1:sig:a"},
        {"text": "🏠 Меню", "callback_data": "v1:m"}]]


def report(**over):
    base = {"closed": 14, "wins": 8, "losses": 6, "profit_usd": D("0.0421"),
            "avg_pct": D("0.15"), "best": {"pct": D("1.2"), "token": "SOL", "position_id": 957},
            "worst": {"pct": D("-2.1"), "token": "DOGE", "position_id": 930},
            "fees_usd": D("0.0560"), "avg_hold_sec": 19.5 * 3600,
            "skipped_by_reason": {"stale": 3}, "errors": 0}
    base.update(over)
    return base


def test_results_screen_matches_the_sample_and_marks_the_period() -> None:
    text, keyboard = screens.results_screen(period="7d", report=report(), now=NOW, tz=MSK)
    assert text.splitlines() == [
        "📊 <b>Итоги демо-счёта · 7 дней</b>",
        "Закрыто сделок: 14",
        "✅ в плюс 8 · ❌ в минус 6 · доля 57% из 14",
        "Прибыль: 🟢 +$0,0421 (в среднем +0,15% на сделку)",
        f"Лучшая: +1,20% (SOL #957) · худшая: {fmt.MINUS}2,10% (DOGE #930)",
        "Комиссии: $0,0560",
        "Среднее время в сделке: 19 ч 30 мин",
        "Пропущено сигналов: 3 — опоздание 3",
        "⚠️ Сделок меньше 30 — выводы делать рано.",
        "Обновлено 07.10 14:05 МСК",
    ]
    period_row = keyboard["inline_keyboard"][0]
    assert [b["text"] for b in period_row] == ["Сегодня", "• 7 дней", "30 дней", "Всё время"]
    assert [b["callback_data"] for b in period_row] == [
        "v1:res:today", "v1:res:7d", "v1:res:30d", "v1:res:all"]
    assert keyboard["inline_keyboard"][1][0]["text"] == "🎯 Качество сигналов"


def test_results_screen_small_sample_exact_edge_errors_and_empty() -> None:
    at_threshold, _ = screens.results_screen(
        period="all", report=report(closed=30, wins=20, losses=10), now=NOW, tz=MSK)
    assert "выводы делать рано" not in at_threshold          # порог: меньше 30
    below, _ = screens.results_screen(
        period="all", report=report(closed=29, wins=20, losses=9), now=NOW, tz=MSK)
    assert "Сделок меньше 30" in below
    errors, _ = screens.results_screen(
        period="today", report=report(errors=2), now=NOW, tz=MSK)
    assert "⚠️ Ошибки биржи: 2" in errors
    assert "Ошибки биржи" not in screens.results_screen(
        period="today", report=report(), now=NOW, tz=MSK)[0]
    empty, kb = screens.results_screen(
        period="30d", report=report(closed=0, wins=0, losses=0), now=NOW, tz=MSK)
    assert "За период закрытых сделок нет" in empty and "Закрыто сделок" not in empty
    assert len(kb["inline_keyboard"]) == 3                  # периоды, качество, обновить/меню


def test_skip_reasons_are_translated_and_unknown_ones_kept_as_is() -> None:
    text, _ = screens.results_screen(
        period="7d", report=report(skipped_by_reason={
            "no_capital": 2, "no_balance": 1, "stale": 3, "not_buy": 1, "странная": 1}),
        now=NOW, tz=MSK)
    line = next(item for item in text.splitlines() if item.startswith("Пропущено"))
    assert line == ("Пропущено сигналов: 8 — опоздание 3, не хватило денег 2, "
                    "мало USDT на демо-счёте 1, не покупка 1, странная 1")


def test_the_trades_screen_matches_the_sample() -> None:
    open_trades = [
        {"position_id": 957, "token": "SOL", "entry_price": D("120.52"),
         "current_price": D("120.71"), "current_pct": D("0.16"),
         "target_price": D("121.97"), "target_pct": D("1.2"),
         "opened_at": NOW - timedelta(hours=5),
         "deadline_at": NOW + timedelta(hours=18, minutes=40)},
        {"position_id": 958, "token": "BTC", "entry_price": D("85614.30"),
         "current_price": D("85451.60"), "current_pct": D("-0.19"),
         "target_price": D("86640"), "target_pct": D("1.2"),
         "opened_at": NOW - timedelta(hours=1),
         "deadline_at": NOW + timedelta(hours=20, minutes=5)},
    ]
    closed = [
        {"position_id": 940, "token": "ETH", "profit": D("0.0170"), "pct": D("0.85"),
         "hold_sec": 22 * 3600 + 600, "exit_reason": "target", "hold_hours": 24},
        {"position_id": 938, "token": "XRP", "profit": D("-0.0080"), "pct": D("-0.40"),
         "hold_sec": 48 * 3600, "exit_reason": "timeout", "hold_hours": 48},
    ]
    text, kb = screens.trades_screen(open_trades=open_trades, recent_closed=closed, now=NOW, tz=MSK)
    assert text.splitlines() == [
        "📂 <b>Сделки на демо-счёте</b>", "",
        "<b>Открыто: 2</b>",
        f"{'🔴'} <b>BTC</b> #958 · {fmt.MINUS}0,19%",                  # новые сверху
        "   вход 85 614,30 → сейчас 85 451,60 · цель 86 640,00 (+1,20%)",
        "   ⏳ осталось 20 ч 5 мин",
        "🟢 <b>SOL</b> #957 · +0,16%",
        "   вход 120,52 → сейчас 120,71 · цель 121,97 (+1,20%)",
        "   ⏳ осталось 18 ч 40 мин",
        "", "<b>Последние закрытые</b>",
        "✅ ETH #940 · +0,85% · +$0,0170 · 22 ч 10 мин · цель достигнута",
        f"❌ XRP #938 · {fmt.MINUS}0,40% · {fmt.MINUS}$0,0080 · 2 д · истёк срок 48 ч",
        "Обновлено 07.10 14:05 МСК",
    ]
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["💰 Счёт", "📊 Итоги"]


def test_the_account_screen_matches_the_sample() -> None:
    demo = {"state": STATE, "mirror_since": datetime(2026, 10, 5, 21, 30, tzinfo=UTC)}
    week = {"closed": 14, "wins": 8, "losses": 6, "profit_usd": D("0.0421"),
            "fees_usd": D("0.0560")}
    text, kb = screens.account_screen(
        demo=demo, yesterday_close=D("1000"), week=week, now=NOW, tz=MSK)
    assert text.splitlines() == [
        "💰 <b>Демо-счёт OKX</b>",
        "<i>Деньги не настоящие · старт 06.10.2026 с $1 000,00</i>", "",
        "Итого: <b>$1 000,12</b>", "Свободно: $996,10", "В рынке: $4,02",
        "С начала: 🟢 +$0,12 (+0,01%)",
        "За сегодня: 🟢 +$0,12 (+0,01%)", "",
        "<b>За 7 дней</b>",
        "Закрыто 14 · ✅ 8 · ❌ 6 · в плюс 57% из 14",
        "Прибыль: 🟢 +$0,0421 · комиссии $0,0560",
        "Обновлено 07.10 14:05 МСК",
    ]
    nothing, _ = screens.account_screen(
        demo=demo, yesterday_close=None, week={"closed": 0}, now=NOW, tz=MSK)
    assert "За сегодня: —" in nothing and "Закрытых сделок за 7 дней нет" in nothing
    assert "в плюс" not in nothing                           # «в плюс NN%» — только при N > 0


def test_the_agreement_word_replaces_probability_everywhere_in_the_screens() -> None:
    from src.bot import handlers

    card = {"id": 1, "ts": NOW, "decision": "buy", "probability": 0.62, "rationale": "довод",
            "notified": False, "notified_at": None, "status": "open", "agents_payload": [],
            "calibrated_probability": None, "symbol": "BTC/USDT", "price_at_signal": 85614.3}
    text = handlers.render_signal_card(card, NOW, 4)
    assert "Пояснение:" in text and "rationale" not in text          # Д11
    assert "индекс согласия 62%" in text and "Цена на момент сигнала: 85 614,30" in text
    stats = handlers.render_stats(
        [("за 7 дней", {"n": 5, "buy": 2, "sell": 2, "wait": 1, "sr_buy": 0.5, "sr_sell": 0.5,
                        "n_buy": 2, "n_sell": 2, "avg_pnl": 0.1, "avg_dd": 0.1},
          {"n": 0})], 4, NOW)
    for word in ("buy", "sell", "wait"):
        assert word not in stats
    summary = handlers.render_summary(
        [], [], {"by_decision": {"buy": 3, "wait": 5}}, "1 GB", NOW)
    assert "покупать: 3" in summary and "ждать: 5" in summary and "buy:" not in summary


def test_unknown_input_comes_with_a_menu_button() -> None:
    text, keyboard = screens.unknown_screen()
    assert "Не понимаю команду" in text
    assert keyboard["inline_keyboard"] == [[nav.home_button()]]


def test_the_help_screen_lists_the_nine_commands_without_positions() -> None:
    text, keyboard = screens.help_screen(flags=screens.Flags(demo_enabled=True), coins=COINS5)
    for command in ("/menu", "/demo", "/trades", "/results", "/agents", "/signals",
                    "/status", "/settings"):
        assert command in text
    assert "/positions" not in text
    assert keyboard["inline_keyboard"] == [[nav.home_button()]]
    assert "около 0,1%" in text and "Согласие агентов" in text


# --- §4.0: размеры и кнопки -----------------------------------------------------------------


def all_screens(n_open: int = 40, tokens=COINS5):
    open_trades = [
        {"position_id": 1000 + i, "token": tokens[i % len(tokens)], "entry_price": D("85614.30"),
         "current_price": D("85451.60"), "current_pct": D("-0.19"), "target_price": D("86640"),
         "target_pct": D("1.2"), "opened_at": NOW - timedelta(minutes=i),
         "deadline_at": NOW + timedelta(hours=20)} for i in range(n_open)]
    closed = [{"position_id": 900 + i, "token": "ETH", "profit": D("0.017"), "pct": D("0.85"),
               "hold_sec": 80000, "exit_reason": "target", "hold_hours": 24} for i in range(5)]
    signals = [{"id": 5000 - i, "symbol": f"{tokens[i % len(tokens)]}/USDT", "decision": "buy",
                "probability": 0.7, "ts": NOW} for i in range(20)]
    by_token = {t: {"market": agent_row("bullish", 0.7), "liquidity": agent_row("neutral", 0.5),
                    "futures": agent_row("bearish", 0.6)} for t in tokens}
    demo = {"state": STATE, "mirror_since": NOW - timedelta(days=1)}
    flags = screens.Flags(demo_enabled=True)
    user = UserSettings(chat_id=1, quiet_from=20, quiet_to=4)
    instruments = [(i + 1, f"{t}/USDT") for i, t in enumerate(tokens)]
    out = {
        "main": screens.main_screen(flags=flags, coins=tokens, state=STATE, health=health(
            coins=tokens), now=NOW, tz=MSK),
        "account": screens.account_screen(demo=demo, yesterday_close=D(1000), week={
            "closed": 3, "wins": 2, "losses": 1, "profit_usd": D(1), "fees_usd": D(1)},
            now=NOW, tz=MSK),
        "trades": screens.trades_screen(open_trades=open_trades, recent_closed=closed, now=NOW,
                                        tz=MSK),
        "results": screens.results_screen(period="all", report=report(), now=NOW, tz=MSK),
        "agents": screens.agents_screen(
            tokens=tokens, by_token=by_token, decisions={}, freshness_sec=300, flags=flags,
            now=NOW, tz=MSK),
        "signals": screens.signals_screen(signals=signals, mode="s", flags=flags, now=NOW, tz=MSK),
        "system": screens.system_screen(health=health(coins=tokens), now=NOW, tz=MSK),
        "settings": screens.settings_screen(user=user, instruments=instruments, flags=flags,
                                            tz=MSK, now=NOW),
        "help": screens.help_screen(flags=flags, coins=tokens),
        "card": screens.signal_card_screen("x", "s"),
        "detail": screens.detail_screen("x" * 10_000),
        "quality": screens.quality_screen("y" * 10_000),
    }
    return out


def test_every_screen_fits_in_one_message_with_forty_open_trades() -> None:
    for name, (text, _) in all_screens().items():
        assert len(text) <= screens.TEXT_LIMIT, (name, len(text))
    trades, _ = all_screens()["trades"]
    assert "…и ещё 25" in trades.splitlines()                # 40 − 15 показанных
    assert trades.count("#10") <= screens.MAX_OPEN_TRADES + 5


def test_every_callback_fits_in_64_bytes_and_every_navigation_one_parses() -> None:
    for name, (_, keyboard) in all_screens().items():
        for row in keyboard["inline_keyboard"]:
            for button in row:
                data = button["callback_data"]
                assert len(data.encode()) <= 64, (name, data)
                assert json.dumps(button)                       # сериализуется
                if data.startswith("v1:"):
                    assert nav.parse_nav(data) is not None, (name, data)
                    assert nav.parse_nav(data).data == data, (name, data)
    # самый длинный адрес — карточка с огромным номером
    longest = nav.NavTarget(nav.SIGNAL_CARD, mode="a", signal_id=2**63 - 1).data
    assert len(longest.encode()) <= 64


def test_every_screen_but_the_main_one_has_the_menu_button_and_refresh_or_back() -> None:
    for name, (_, keyboard) in all_screens().items():
        if name == "main":
            continue
        flat = [b for row in keyboard["inline_keyboard"] for b in row]
        assert any(b["callback_data"] == "v1:m" for b in flat), name
    for name in ("account", "trades", "results", "agents", "signals", "system"):
        last = all_screens()[name][1]["inline_keyboard"][-1]
        assert [b["text"] for b in last] == ["🔄 Обновить", "🏠 Меню"], name


def test_refresh_sends_the_same_data_that_opened_the_screen() -> None:
    cases = {
        "account": "v1:acc", "trades": "v1:tr", "agents": "v1:ag", "system": "v1:sys",
        "results": "v1:res:all", "signals": "v1:sig:s",
    }
    for name, data in cases.items():
        refresh = all_screens()[name][1]["inline_keyboard"][-1][0]
        assert refresh["callback_data"] == data, name


def test_every_footer_carries_the_update_time_in_local_time() -> None:
    for name in ("main", "account", "trades", "results", "agents", "signals", "system"):
        assert "Обновлено 07.10 14:05 МСК" in all_screens()[name][0], name


def test_the_word_demo_is_on_every_screen_with_money() -> None:
    for name in ("main", "account", "trades", "results"):
        text = all_screens()[name][0]
        assert "демо" in text.lower(), name


def test_parse_nav_on_garbage_never_raises() -> None:
    for junk in (None, "", "v1", "v1:", "v1:zzz", "v1:res:zzz", "v1:sc:abc:s", "v1:sc:1:x",
                 "v1:sc:-5:s", "v1:sc:99999999999999999999999:s", "v1:m:extra", "v2:m",
                 "tok:1", "v1:sig", "v1:sc:1", "💥", "v1:res:7d:7d", "v1:sc:١٢٣:s"):
        assert nav.parse_nav(junk) is None, junk
    assert nav.parse_nav("v1:m") == nav.NavTarget("m")
    assert nav.parse_nav("v1:res:30d") == nav.NavTarget("res", period="30d")
    assert nav.parse_nav("v1:sc:12345:a") == nav.NavTarget("sc", mode="a", signal_id=12345)
    assert nav.parse_nav("v1:accn") == nav.NavTarget("accn")


def test_every_screen_is_valid_telegram_html() -> None:
    """parse_mode=HTML: только теги <b> и <i>, парные; голых «<» и «&» в тексте нет."""
    import re

    texts = [text for text, _ in all_screens().values()]
    texts.append(screens.system_screen(
        health=health({"demo:heartbeat": 30}), now=NOW, tz=MSK)[0])
    texts.append(screens.trades_screen(
        open_trades=[{"position_id": 1, "token": "A<B&C", "entry_price": D(1),
                      "current_price": D(1), "current_pct": D(0), "target_price": None,
                      "target_pct": None, "opened_at": NOW, "deadline_at": NOW}],
        recent_closed=[{"position_id": 2, "token": "X", "profit": D(1), "pct": D(1),
                        "hold_sec": 30, "exit_reason": "a<b", "hold_hours": None}],
        now=NOW, tz=MSK)[0])
    for text in texts:
        stripped = re.sub(r"</?(?:b|i)>", "", text)
        stripped = re.sub(r"&(?:lt|gt|amp|quot|#x27);", "", stripped)
        assert "<" not in stripped and ">" not in stripped and "&" not in stripped, text[:200]
        for tag in ("b", "i"):
            assert text.count(f"<{tag}>") == text.count(f"</{tag}>"), (tag, text[:200])
