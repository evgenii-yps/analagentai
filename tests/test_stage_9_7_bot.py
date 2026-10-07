"""Этап 9.7: бот — нажатия кнопок, правка сообщений, команды, Telegram-вызовы, тихие часы демо.

Без сети: Telegram — заглушки, запросы бота — заглушка ``FakeQueries``.
"""

from __future__ import annotations

import json
import re
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import structlog

from src.bot import handlers, nav, poller, screens
from src.bot.poller import BOT_COMMANDS, BotPoller
from src.core.config import settings
from src.demo import messages, report, runner
from src.demo.ledger import LedgerState
from src.notify import telegram
from tests.demo_support import Clock, FakeRedis, capture_demo_logs

D = Decimal
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Заглушки
# --------------------------------------------------------------------------


class FakeQueries:
    """Все запросы, которые экраны бота могут задать, — с правдоподобными ответами."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.saved: list[tuple] = []
        self.settings_row: dict[str, Any] | None = None

    def _note(self, name: str) -> None:
        self.calls.append(name)

    async def spot_instruments(self):
        self._note("spot_instruments")
        return [(1, "BTC/USDT"), (2, "ETH/USDT"), (3, "SOL/USDT"), (4, "XRP/USDT"),
                (5, "DOGE/USDT")]

    async def user_settings(self, chat_id):
        return self.settings_row

    async def save_user_settings(self, chat_id, instruments, horizon_h, min_score, qf, qt):
        self.saved.append((chat_id, list(instruments), horizon_h, min_score, qf, qt))
        self.settings_row = {"instruments": list(instruments), "horizon_h": horizon_h,
                             "min_score": min_score, "quiet_from": qf, "quiet_to": qt}

    async def freshness_by_instrument(self, instruments):
        return [(f"{t}/USDT", NOW) for t in ("BTC", "ETH", "SOL", "XRP", "DOGE")]

    async def status_facts(self):
        return {"last_ohlcv_ts": NOW, "last_orderbook_ts": NOW, "last_signal_ts": NOW,
                "open_count": 1, "closed_count": 2}

    async def demo_state(self):
        return {"state": LedgerState(D(1000), D("996.10"), D("4.02"), D("1000.12"), 2),
                "mirror_since": NOW - timedelta(days=1)}

    async def demo_overview(self, days: int = 7):
        state = (await self.demo_state())["state"]
        return {"state": state, "open_trades": [], "days": days, "closed": 0, "wins": 0,
                "profit_usd": D(0)}

    async def demo_report(self, since):
        self.calls.append(f"demo_report:{since.isoformat() if since else None}")
        return {"closed": 3, "wins": 2, "losses": 1, "profit_usd": D("0.01"),
                "avg_pct": D("0.2"), "best": None, "worst": None, "fees_usd": D("0.01"),
                "avg_hold_sec": 3600.0, "skipped_by_reason": {}, "errors": 0}

    async def demo_recent_closed(self, limit=5):
        return []

    async def demo_yesterday_close(self, today_local):
        return D("1000")

    async def agents_by_token(self, agents, max_age_sec=3600):
        return {}

    async def latest_signal_by_token(self):
        return {}

    async def last_signals(self, notified_only, limit, instruments=None):
        self.calls.append(f"last_signals:{notified_only}:{limit}")
        return [{"id": 77, "symbol": "BTC/USDT", "decision": "buy", "probability": 0.7,
                 "ts": NOW}]

    async def signal_card(self, signal_id):
        return {"id": signal_id, "ts": NOW, "decision": "buy", "probability": 0.7,
                "rationale": "довод", "notified": False, "notified_at": None,
                "status": "open", "agents_payload": [], "symbol": "BTC/USDT"}

    async def stats_versions(self, period_sec):
        return [7]

    async def logic_version_started_at(self, version):
        return NOW

    async def stats_block(self, period_sec, independent, version, horizon_h=4):
        return {"n": 3, "buy": 1, "sell": 1, "wait": 1, "sr_buy": 0.5, "sr_sell": 0.5,
                "n_buy": 1, "n_sell": 1, "avg_pnl": 0.1, "avg_dd": 0.1}

    async def notify_filter_counts(self, period_sec, version):
        return {"sent": 0, "absorbed": 0}

    async def data_counts_24h(self):
        return [("OHLCV", 100)]

    async def signal_counts_24h(self, min_probability, primary_horizon):
        return {"by_decision": {"buy": 1}, "sent": 0, "absorbed": 0, "candidates": 0,
                "repeats": 0, "unique_inputs": 1, "closed": 0}

    async def db_size(self):
        return "10 MB"

    async def positions_state(self, pause_sec, logic_version):
        return {"open_count": 0, "pauses": []}

    async def positions_open(self):
        return []

    async def positions_summary(self, **kwargs):
        return {}

    async def positions_capital(self):
        return {"committed_usd": 0.0, "realized_usd": 0.0, "by_version": []}


class FakeClient:
    """Клиент httpx для answerCallbackQuery: запоминает ответы на нажатия."""

    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002
        assert url.endswith("/answerCallbackQuery")
        self.answers.append(json)
        return types.SimpleNamespace(status_code=200)


class Sent:
    """Перехват send_message / edit_message поллера."""

    def __init__(self, monkeypatch, edit_result: str = "ok") -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_result = edit_result

        async def send(text, chat_id=None, reply_markup=None, disable_notification=False):
            self.sent.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
            return 100 + len(self.sent)

        async def edit(chat_id, message_id, text, reply_markup=None):
            self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                               "reply_markup": reply_markup})
            return self.edit_result

        monkeypatch.setattr(poller, "send_message", send)
        monkeypatch.setattr(poller, "edit_message", edit)


def make_bot(queries: FakeQueries | None = None, redis: FakeRedis | None = None) -> BotPoller:
    bot = object.__new__(BotPoller)
    bot.queries = queries or FakeQueries()
    bot.redis = redis or FakeRedis()
    bot.token = "T"
    bot.allowed = {"1"}
    bot.rate_limit_sec = 3
    bot.max_rows = 20
    bot.tz = MSK
    bot._offset = None
    bot._log = structlog.get_logger()

    async def heartbeats():
        return [(key, NOW.isoformat(), 30) for key, _ in poller.heartbeat_keys()]

    async def refusals(*a, **k):
        return {}

    bot._read_heartbeats = heartbeats
    bot._read_refusals = refusals
    return bot


def press(data: str, chat_id: int = 1, message_id: int = 55, cb_id: str = "cb1") -> dict:
    return {"id": cb_id, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}}}


@pytest.fixture(autouse=True)
def now_is_fixed(monkeypatch):
    """Часы бота — фиксированные: тексты экранов не зависят от времени запуска теста."""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(poller, "datetime", FrozenDatetime)
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", False)
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)


# --------------------------------------------------------------------------
# Нажатия: правка на месте, «Данные не изменились», запасная отправка, ответ на каждое
# --------------------------------------------------------------------------


async def test_a_press_edits_the_same_message_and_always_answers(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    await bot._handle_callback(client, press("v1:acc"))
    assert sent.sent == []                               # новое сообщение НЕ шлётся
    [edit] = sent.edits
    assert edit["message_id"] == 55 and edit["chat_id"] == 1
    assert "Демо-счёт OKX" in edit["text"] and edit["reply_markup"]["inline_keyboard"]
    assert client.answers == [{"callback_query_id": "cb1", "text": ""}]


async def test_not_modified_answers_that_nothing_changed(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch, edit_result="not_modified"), FakeClient(), make_bot()
    await bot._handle_callback(client, press("v1:sys"))
    assert sent.sent == []
    assert client.answers[0]["text"] == "Данные не изменились"


async def test_a_failed_edit_falls_back_to_a_new_message_with_the_same_content(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch, edit_result="failed"), FakeClient(), make_bot()
    await bot._handle_callback(client, press("v1:tr"))
    [edit], [new] = sent.edits, sent.sent
    assert new["text"] == edit["text"] and new["reply_markup"] == edit["reply_markup"]
    assert new["chat_id"] == "1"
    assert len(client.answers) == 1


async def test_the_account_button_under_a_trade_message_sends_a_new_message(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    await bot._handle_callback(client, press(messages.ACCOUNT_CALLBACK, message_id=900))
    assert sent.edits == []                              # сообщение о сделке — запись, не правится
    [new] = sent.sent
    assert "Демо-счёт OKX" in new["text"]
    assert client.answers[0]["text"] == ""


async def test_an_unknown_or_old_button_gets_an_answer_and_no_exception(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    for index, data in enumerate(("v1:zzz", "v2:m", "", "мусор", "v1:res:всё")):
        await bot._handle_callback(client, press(data, cb_id=f"c{index}"))
    assert sent.sent == [] and sent.edits == []
    assert [a["text"] for a in client.answers] == ["Кнопка устарела, откройте /menu"] * 5


async def test_a_press_is_throttled_with_an_answer_instead_of_silence(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    await bot._handle_callback(client, press("v1:acc", cb_id="a"))
    await bot._handle_callback(client, press("v1:tr", cb_id="b"))
    assert len(sent.edits) == 1                          # второй показ не состоялся
    assert [a["text"] for a in client.answers] == ["", "Секунду…"]


async def test_a_broken_screen_still_answers_the_press(monkeypatch) -> None:
    sent, client = Sent(monkeypatch), FakeClient()

    class Broken(FakeQueries):
        async def demo_state(self):
            raise RuntimeError("база недоступна")

        async def demo_overview(self, days=7):
            raise RuntimeError("база недоступна")

        async def agents_by_token(self, agents, max_age_sec=3600):
            raise RuntimeError("база недоступна")

    bot = make_bot(Broken())
    await bot._handle_callback(client, press("v1:ag"))
    assert len(client.answers) == 1 and "попробуйте" in client.answers[0]["text"]
    # демо-экраны при сбое базы показывают «ещё не запущен», а не падают
    await bot._handle_callback(client, press("v1:acc", cb_id="x"))
    bot.redis = FakeRedis()
    await bot._handle_callback(client, press("v1:tr", cb_id="y"))
    assert len(client.answers) == 3
    assert any("ещё не запущен" in e["text"] for e in sent.edits)


async def test_a_foreign_chat_gets_nothing_but_the_spinner_is_closed(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    await bot._handle_callback(client, press("v1:m", chat_id=999))
    assert sent.sent == [] and sent.edits == []
    assert client.answers == [{"callback_query_id": "cb1", "text": ""}]


async def test_every_navigation_target_renders_with_buttons(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    targets = ["v1:m", "v1:acc", "v1:tr", "v1:res:today", "v1:res:7d", "v1:res:30d",
               "v1:res:all", "v1:sq", "v1:sig:s", "v1:sig:a", "v1:sc:77:s", "v1:sc:77:a",
               "v1:ag", "v1:sys", "v1:sysd", "v1:set", "v1:help"]
    for index, data in enumerate(targets):
        bot.redis = FakeRedis()                          # анти-флуд здесь не проверяется
        await bot._handle_callback(client, press(data, cb_id=f"t{index}"))
    assert len(sent.edits) == len(targets) and sent.sent == []
    for edit in sent.edits:
        assert 0 < len(edit["text"]) <= screens.TEXT_LIMIT
        assert edit["reply_markup"]["inline_keyboard"]
    detail = next(e for e in sent.edits if "Heartbeat" in e["text"] or ":heartbeat" in e["text"])
    assert "collector:heartbeat:ohlcv" in detail["text"]          # машинные ключи — только тут
    for edit in sent.edits:
        if edit is not detail:
            assert ":heartbeat" not in edit["text"]


async def test_results_periods_use_the_right_window(monkeypatch) -> None:
    Sent(monkeypatch)
    queries = FakeQueries()
    bot = make_bot(queries)
    for period in ("today", "7d", "30d", "all"):
        await bot._render(nav.NavTarget(nav.RESULTS, period=period), 1, NOW)
    windows = [c.split(":", 1)[1] for c in queries.calls if c.startswith("demo_report:")]
    assert windows[0].startswith("2026-10-06T21:00:00")          # полночь МСК = 21:00 UTC
    assert windows[1].startswith("2026-09-30T11:05:00")
    assert windows[2].startswith("2026-09-07T11:05:00")
    assert windows[3].startswith("2026-10-06T11:05:00")          # с mirror_since


async def test_the_settings_buttons_edit_the_screen_and_save(monkeypatch) -> None:
    sent, client, queries = Sent(monkeypatch), FakeClient(), FakeQueries()
    bot = make_bot(queries)
    await bot._handle_callback(client, press("tok:2"))
    assert queries.saved and 2 not in queries.saved[-1][1]
    assert "Монеты: BTC, SOL, XRP, DOGE" in sent.edits[-1]["text"]
    assert client.answers[-1]["text"] == "ETH выключен"
    await bot._handle_callback(client, press("quiet", cb_id="q"))
    assert "Время — по МСК" in sent.edits[-1]["text"]
    assert sent.edits[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "qf:0"
    await bot._handle_callback(client, press("qf:23", cb_id="q2"))
    await bot._handle_callback(client, press("qt:23:8", cb_id="q3"))
    assert queries.saved[-1][4:] == (20, 4)                    # в UTC и включительно
    assert "23:00–08:00 МСК" in sent.edits[-1]["text"]
    assert client.answers[-1]["text"] == "Тишина: с 23:00 до 08:00 МСК"
    assert len(client.answers) == 4 and all("text" in a for a in client.answers)


async def test_settings_buttons_are_not_rate_limited(monkeypatch) -> None:
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    for index, token in enumerate((1, 2, 3, 4)):
        await bot._handle_callback(client, press(f"tok:{token}", cb_id=f"s{index}"))
    assert len(sent.edits) == 4 and all(a["text"] != "Секунду…" for a in client.answers)


# --------------------------------------------------------------------------
# Команды: все старые работают, новые открывают экраны
# --------------------------------------------------------------------------

COMMANDS = ["/start", "/menu", "/help", "/demo", "/account", "/trades", "/results",
            "/results today", "/results 30d", "/results all", "/results мусор", "/stats",
            "/stats 24h", "/agents", "/signals", "/signals all", "/last", "/last 3",
            "/last all", "/last all 2", "/signal 77", "/signal", "/signal abc", "/status",
            "/summary", "/settings", "/positions", "/start@AgentTradeBot", "привет", "/zzz",
            "", "/MENU"]


@pytest.mark.parametrize("virtual", [True, False])
async def test_every_command_answers_and_none_crashes(monkeypatch, virtual) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", virtual)
    bot = make_bot()
    for command in COMMANDS:
        answer = await bot._answer_command(command, 1)
        if isinstance(answer, str):
            assert answer, command
        else:
            text, markup = answer
            assert text and markup["inline_keyboard"], command
            assert len(text) <= screens.TEXT_LIMIT, command
    for command in handlers.KNOWN_COMMANDS:
        assert isinstance(await bot._answer_command(f"/{command}", 1), (str, tuple)), command


async def test_commands_map_to_the_screens() -> None:
    bot = make_bot()
    expect = {
        "/start": "Agent Trade", "/menu": "Следит за", "/demo": "Демо-счёт OKX",
        "/account": "Демо-счёт OKX", "/trades": "Сделки на демо-счёте",
        "/results": "Итоги демо-счёта · 7 дней", "/results today": "Итоги демо-счёта · сегодня",
        "/stats": "Статистика", "/agents": "Что думают агенты", "/signals": "Последние",
        "/status": "Состояние системы", "/settings": "Настройки", "/help": "Как читать бота",
    }
    for command, needle in expect.items():
        text, _ = await bot._answer_command(command, 1)
        assert needle in text, command
    summary, markup = await bot._answer_command("/summary", 1)
    assert "Суточная сводка" in summary and "collector:heartbeat:ohlcv" in summary
    assert markup["inline_keyboard"][0][0]["callback_data"] == "v1:sys"


async def test_unknown_input_answers_with_the_same_phrase_and_a_menu_button() -> None:
    bot = make_bot()
    for text in ("привет", "/zzz", "что-то"):
        reply, markup = await bot._answer_command(text, 1)
        assert reply == handlers.render_unknown()
        assert markup["inline_keyboard"] == [[nav.home_button()]]


async def test_signals_default_follows_the_notification_flag(monkeypatch) -> None:
    queries = FakeQueries()
    bot = make_bot(queries)
    await bot._answer_command("/signals", 1)
    assert queries.calls[-1] == "last_signals:False:5"            # уведомления выключены → все
    await bot._answer_command("/last", 1)
    assert queries.calls[-1] == "last_signals:True:5"             # /last — как раньше
    await bot._answer_command("/last all 3", 1)
    assert queries.calls[-1] == "last_signals:False:3"
    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", True)
    await bot._answer_command("/signals", 1)
    assert queries.calls[-1] == "last_signals:True:5"


async def test_positions_keeps_its_one_line_when_virtual_output_is_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", False)
    bot = make_bot()
    assert await bot._answer_command("/positions", 1) == handlers.HIDDEN_POSITIONS_TEXT
    monkeypatch.setattr(settings, "VIRTUAL_OUTPUT_ENABLED", True)
    text = await bot._answer_command("/positions", 1)
    assert isinstance(text, str) and "Виртуальные позиции" in text


async def test_a_text_message_gets_the_screen_as_a_new_message(monkeypatch) -> None:
    sent, bot = Sent(monkeypatch), make_bot()
    update = {"update_id": 1, "message": {"chat": {"id": 1}, "text": "/menu"}}
    await bot._handle_update(FakeClient(), update)
    [message] = sent.sent
    assert "Agent Trade" in message["text"] and message["chat_id"] == "1"
    assert message["reply_markup"]["inline_keyboard"]
    # чужой чат — молчание
    await bot._handle_update(FakeClient(), {"update_id": 2, "message": {
        "chat": {"id": 999}, "text": "/menu"}})
    assert len(sent.sent) == 1
    # анти-флуд для текста — молчание, как раньше
    await bot._handle_update(FakeClient(), update)
    assert len(sent.sent) == 1


def test_the_command_menu_has_nine_russian_commands_without_positions() -> None:
    assert [name for name, _ in BOT_COMMANDS] == [
        "menu", "demo", "trades", "results", "agents", "signals", "status", "settings", "help"]
    for name, description in BOT_COMMANDS:
        assert re.fullmatch(r"[a-z_]{1,32}", name) and re.search("[а-я]", description.lower())
        assert 3 <= len(description) <= 256
    assert "positions" not in dict(BOT_COMMANDS)
    for command in dict(BOT_COMMANDS):
        assert command in handlers.KNOWN_COMMANDS


async def test_the_command_menu_is_registered_once_and_a_failure_is_not_fatal(monkeypatch) -> None:
    calls: list[list] = []

    async def good(commands):
        calls.append(commands)
        return True

    async def bad(commands):
        raise RuntimeError("Telegram недоступен")

    bot = make_bot()
    events: list[str] = []

    class Log:
        def info(self, event, **kwargs):
            events.append(event)

        def warning(self, event, **kwargs):
            events.append(event)

    bot._log = Log()
    monkeypatch.setattr(poller, "set_my_commands", good)
    await bot._register_commands()
    monkeypatch.setattr(poller, "set_my_commands", bad)
    await bot._register_commands()
    assert calls == [BOT_COMMANDS]
    assert "bot_set_my_commands=1" in events and "bot_set_my_commands_failed=1" in events


# --------------------------------------------------------------------------
# src/notify/telegram.py
# --------------------------------------------------------------------------


@pytest.fixture
def telegram_api(monkeypatch):
    """Подменяет httpx-клиент Telegram транспортом-заглушкой; собирает запросы."""
    requests: list[httpx.Request] = []
    behaviour: dict[str, Any] = {"status": 200, "body": {"ok": True, "result": {"message_id": 321}}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if behaviour.get("raise"):
            raise httpx.ConnectError("нет сети")
        return httpx.Response(behaviour["status"], json=behaviour["body"])

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(telegram.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "42")
    return requests, behaviour


async def test_send_message_returns_the_message_id_and_passes_the_options(telegram_api) -> None:
    requests, _ = telegram_api
    markup = {"inline_keyboard": [[{"text": "x", "callback_data": "v1:m"}]]}
    assert await telegram.send_message("привет", reply_markup=markup,
                                       disable_notification=True) == 321
    body = json.loads(requests[0].content)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    assert body["disable_notification"] is True and body["reply_markup"] == markup
    assert requests[0].url.path.endswith("/sendMessage")
    await telegram.send_message("тихо не надо")
    assert "disable_notification" not in json.loads(requests[1].content)
    assert "reply_markup" not in json.loads(requests[1].content)


async def test_send_message_returns_none_on_any_error(telegram_api) -> None:
    _, behaviour = telegram_api
    behaviour.update(status=400, body={"ok": False, "description": "Bad Request"})
    assert await telegram.send_message("x") is None
    behaviour["raise"] = True
    assert await telegram.send_message("x") is None


async def test_the_message_id_is_always_a_true_value_on_success(telegram_api) -> None:
    _, behaviour = telegram_api
    behaviour["body"] = {"ok": True}                      # нестандартное тело — но ответ 200
    assert bool(await telegram.send_message("x")) is True


async def test_edit_message_reports_ok_not_modified_and_failed(telegram_api) -> None:
    requests, behaviour = telegram_api
    assert await telegram.edit_message(1, 5, "текст", {"inline_keyboard": []}) == "ok"
    body = json.loads(requests[0].content)
    assert body["message_id"] == 5 and body["chat_id"] == 1 and body["parse_mode"] == "HTML"
    assert requests[0].url.path.endswith("/editMessageText")
    behaviour.update(status=400, body={"ok": False, "description":
                     "Bad Request: message is not modified: specified new message content"})
    assert await telegram.edit_message(1, 5, "текст") == "not_modified"
    behaviour.update(status=400, body={"ok": False, "description":
                     "Bad Request: message to edit not found"})
    assert await telegram.edit_message(1, 5, "текст") == "failed"
    behaviour["raise"] = True
    assert await telegram.edit_message(1, 5, "текст") == "failed"


async def test_set_my_commands(telegram_api) -> None:
    requests, behaviour = telegram_api
    assert await telegram.set_my_commands(BOT_COMMANDS) is True
    body = json.loads(requests[0].content)
    assert len(body["commands"]) == 9 and body["commands"][0] == {
        "command": "menu", "description": "Главный экран"}
    behaviour.update(status=400, body={"ok": False})
    assert await telegram.set_my_commands(BOT_COMMANDS) is False
    behaviour["raise"] = True
    assert await telegram.set_my_commands(BOT_COMMANDS) is False


async def test_old_callers_that_check_the_result_still_work(monkeypatch) -> None:
    """send_message был bool и стал int | None: все проверки «отправилось ли» — на истинность."""
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "telegram.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(?:ok|sent|result)\s*(?:is|==)\s*(?:True|False)", text):
            line = text[max(0, match.start() - 120):match.end()]
            assert "send_message" not in line, (path.name, match.group(0))


# --------------------------------------------------------------------------
# Тихие часы службы demo (§6.5)
# --------------------------------------------------------------------------


class QuietPool:
    """Пул-заглушка: читает только ``user_settings`` и считает обращения."""

    def __init__(self, quiet_from=20, quiet_to=4, fail=False) -> None:
        self.row = None if quiet_from is None else {
            "quiet_from": quiet_from, "quiet_to": quiet_to}
        self.fail = fail
        self.reads = 0

    async def fetchrow(self, sql, *args):
        assert "user_settings" in sql and args == (777,)
        self.reads += 1
        if self.fail:
            raise RuntimeError("таблицы нет")
        return self.row


def quiet_ctx(pool: QuietPool, hour_utc: int, **config):
    clock = Clock(datetime(2026, 10, 7, hour_utc, 30, tzinfo=UTC))
    sent: list[tuple[str, dict]] = []

    async def notify(text, **kwargs):
        sent.append((text, kwargs))
        return True

    cfg = runner.DemoConfig(host="www.okx.com", recipients=("777",), **config)
    ctx = runner.Context(pool=pool, exchange=None, redis=FakeRedis(), config=cfg, symbols=[],
                         notify=notify, now=clock.now)
    return ctx, clock, sent


async def test_a_trade_message_inside_the_quiet_window_is_silent_outside_it_is_loud() -> None:
    inside_ctx, _, inside = quiet_ctx(QuietPool(), hour_utc=23)          # 02:30 МСК
    await messages.send_trade_message(inside_ctx, "сделка")
    assert inside[0][1]["disable_notification"] is True                  # внутри окна
    assert inside[0][1]["reply_markup"] == messages.ACCOUNT_BUTTON       # кнопка «Счёт»
    for hour in (5, 12, 19):
        ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=hour)
        await messages.send_trade_message(ctx, "сделка")
        assert "disable_notification" not in sent[0][1], hour            # снаружи со звуком
    for hour in (20, 0, 4):                                              # границы включены
        ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=hour)
        await messages.send_trade_message(ctx, "сделка")
        assert sent[0][1].get("disable_notification") is True, hour


async def test_the_quiet_window_is_not_lost_or_delayed_just_silenced() -> None:
    ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=23, trades_max_per_hour=0)
    await messages.send_trade_message(ctx, "A")
    await messages.send_trade_message(ctx, "B")
    assert [text for text, _ in sent] == ["A", "B"]


async def test_alerts_are_always_loud_even_inside_the_quiet_window() -> None:
    ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=23)
    await runner.alert(ctx, "no_balance", "мало USDT")
    assert len(sent) == 1 and sent[0][1] == {}                           # рассылка всем, со звуком
    assert "disable_notification" not in sent[0][1]


async def test_a_settings_read_error_means_a_loud_message_and_a_warning(monkeypatch) -> None:
    ctx, _, sent = quiet_ctx(QuietPool(fail=True), hour_utc=23)
    with capture_demo_logs(monkeypatch) as logs:
        await messages.send_trade_message(ctx, "сделка")
    assert "disable_notification" not in sent[0][1]
    assert any(e["event"] == "demo_quiet_hours_read_failed=1" for e in logs.entries)


async def test_no_settings_row_or_quiet_off_means_loud() -> None:
    for pool in (QuietPool(quiet_from=None), QuietPool(quiet_from=None, quiet_to=None)):
        ctx, _, sent = quiet_ctx(pool, hour_utc=23)
        await messages.send_trade_message(ctx, "сделка")
        assert "disable_notification" not in sent[0][1]
    ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=23)
    ctx.config = runner.DemoConfig(host="www.okx.com")                   # получателей нет
    await messages.send_trade_message(ctx, "сделка")
    assert "disable_notification" not in sent[0][1]


async def test_settings_are_cached_for_sixty_seconds() -> None:
    pool = QuietPool()
    ctx, clock, sent = quiet_ctx(pool, hour_utc=23)
    await messages.send_trade_message(ctx, "1")
    await messages.send_trade_message(ctx, "2")
    assert pool.reads == 1
    clock.advance(61)
    await messages.send_trade_message(ctx, "3")
    assert pool.reads == 2 and len(sent) == 3


async def test_the_rollup_and_the_daily_report_follow_the_quiet_window() -> None:
    ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=23, trades_max_per_hour=1)
    await messages.send_trade_message(ctx, "первое")                     # уйдёт
    await messages.send_trade_message(ctx, "второе")                     # придержано
    assert await messages.flush_rollup(ctx) is True
    rollup_text, rollup_kwargs = sent[-1]
    assert "Придержано сообщений" in rollup_text
    assert rollup_kwargs["disable_notification"] is True and rollup_kwargs["chat_id"] == "777"
    sent.clear()
    await messages.deliver(ctx, "суточная сводка")
    assert sent[0][1] == {"chat_id": "777", "disable_notification": True}


def test_the_demo_uses_the_one_shared_window_function() -> None:
    from src.core import user_settings

    assert messages.is_quiet_hour is user_settings.is_quiet_hour
    text = (ROOT / "src" / "demo" / "messages.py").read_text(encoding="utf-8")
    assert "quiet_from <=" not in text and "hour >=" not in text         # своей копии нет
    from src.notify import agent

    assert agent.user_filter_reason.__module__ == "src.core.user_settings"


async def test_the_daily_report_goes_through_the_quiet_aware_sender(monkeypatch) -> None:
    ctx, _, sent = quiet_ctx(QuietPool(), hour_utc=23)

    async def collect(ctx_, day):
        return {"snap": {"equity_open": 1000, "equity_close": 1001, "late": False,
                         "cash_close": 1000, "in_market_close": 1, "opened_count": 0,
                         "closed_count": 0, "wins": 0, "losses": 0, "fees_usd": 0},
                "day": datetime(2026, 10, 6).date(), "start_capital": D(1000), "skipped": {},
                "problems": {}, "slip_in": None, "slip_out": None,
                "pairs": report.PairTotals()}

    monkeypatch.setattr(report, "collect", collect)
    assert await report.daily_report(ctx) is True
    assert sent[0][1] == {"chat_id": "777", "disable_notification": True}
    assert "сводка за" in sent[0][0]


# --------------------------------------------------------------------------
# Границы этапа
# --------------------------------------------------------------------------


def test_the_bot_writes_only_to_user_settings() -> None:
    from tests.test_demo_safety import sql_write_targets

    found: set[str] = set()
    for path in sorted((ROOT / "src" / "bot").glob("*.py")):
        found |= sql_write_targets(path.read_text(encoding="utf-8"))
    assert found == {"user_settings"}, found


def test_no_new_dependencies_and_no_migrations_were_added() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "httpx==0.28.1" in requirements
    migrations = sorted(p.name for p in (ROOT / "db" / "migrations").glob("*.sql")
                        if not p.name.endswith("_rollback.sql"))
    assert migrations[-1].startswith("030_")                              # новых нет


def test_the_logic_version_is_untouched() -> None:
    from tests.conftest import CURRENT_LOGIC_VERSION

    assert settings.LOGIC_VERSION == CURRENT_LOGIC_VERSION


def test_old_local_formatters_are_gone_or_wrappers() -> None:
    for name in ("price_str", "qty_str", "usd", "pct", "local_time"):
        assert not hasattr(messages, name), name
    from src.demo import report as report_module

    assert not hasattr(report_module, "_usd") and not hasattr(report_module, "_pct")
    handler_src = (ROOT / "src" / "bot" / "handlers.py").read_text(encoding="utf-8")
    assert "round(float(" not in handler_src
    assert not re.search(r":[+,]?\.\d+f\}|:,\.\d+f\}|:\+,\.\d+f\}", handler_src)


def test_the_verification_script_is_read_only_and_covers_the_listed_checks() -> None:
    import subprocess

    path = ROOT / "deploy" / "verify_9_7.sh"
    text = path.read_text(encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in (r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b", r"\bALTER\b",
                      r"docker compose (up|down|restart|stop|start|rm)",
                      r"git (fetch|pull|checkout|reset)",
                      r"\bsendMessage\b"):
        assert not re.search(forbidden, code, re.I), forbidden
    for needle in ("bot_set_my_commands=1", "bot:heartbeat", "demo:heartbeat", "idx_agent_outputs",
                   "has_table_privilege", "user_settings", "ls-remote", "bot_screen=1"):
        assert needle in text, needle
