"""Этап 9.7.1: несколько получателей — всё, что бот присылает, уходит каждому из списка.

Без сети: Telegram — httpx.MockTransport / заглушки urllib и curl, Redis — заглушка.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
import types
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing

from src.bot import poller
from src.core.config import Settings, settings
from src.demo import messages, runner
from src.notify import telegram
from tests.demo_support import Clock, FakeRedis
from tests.test_stage_9_7_bot import FakeClient, Sent, make_bot, press

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TOKEN = "123456:SECRET-TOKEN-VALUE"


@pytest.fixture
def logs(monkeypatch):
    """Перехват журнала telegram/demo. Модульные логгеры кэшируют процессоры при первом
    вызове (``cache_logger_on_first_use``), поэтому подменяются явно, а не через
    ``capture_logs`` — иначе результат зависит от порядка тестов."""
    cap = structlog.testing.LogCapture()
    proxy = structlog.wrap_logger(
        None, processors=[cap], wrapper_class=structlog.make_filtering_bound_logger(0))
    monkeypatch.setattr(telegram, "_log", proxy)
    monkeypatch.setattr(messages, "_log", proxy)
    return cap.entries


def make_settings(**kw) -> Settings:
    return Settings(POSTGRES_PASSWORD="x", _env_file=None, **kw)


# --------------------------------------------------------------------------
# 1. Список получателей
# --------------------------------------------------------------------------


def test_recipients_only_the_owner() -> None:
    assert make_settings(TELEGRAM_CHAT_ID="42").telegram_recipients == ["42"]
    assert make_settings().telegram_recipients == []


def test_recipients_owner_first_then_the_allowed_list_in_order() -> None:
    cfg = make_settings(TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="7,99")
    assert cfg.telegram_recipients == ["42", "7", "99"]


def test_the_owner_is_always_in_the_list_even_if_forgotten() -> None:
    cfg = make_settings(TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="99,7")
    assert cfg.telegram_recipients[0] == "42"
    assert make_settings(BOT_ALLOWED_CHAT_IDS="7").telegram_recipients == ["7"]


def test_recipients_drop_duplicates_blanks_and_spaces() -> None:
    cfg = make_settings(TELEGRAM_CHAT_ID=" 42 ", BOT_ALLOWED_CHAT_IDS=" 7 , ,42,7,, 99 ")
    assert cfg.telegram_recipients == ["42", "7", "99"]


def test_the_bot_whitelist_is_the_same_set() -> None:
    for owner, allowed in (("42", ""), ("42", "7,99"), ("", "7"), (" 42", "99, 42")):
        cfg = make_settings(TELEGRAM_CHAT_ID=owner, BOT_ALLOWED_CHAT_IDS=allowed)
        assert cfg.bot_allowed_chat_ids == set(cfg.telegram_recipients)
    # владельца не вписали в белый список — бот всё равно отвечает ему
    cfg = make_settings(TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="7")
    assert "42" in cfg.bot_allowed_chat_ids


def test_telegram_is_configured_with_a_token_and_any_recipient() -> None:
    assert make_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="42").telegram_configured
    assert not make_settings(TELEGRAM_CHAT_ID="42").telegram_configured
    assert not make_settings(TELEGRAM_BOT_TOKEN="t").telegram_configured


# --------------------------------------------------------------------------
# Заглушки Telegram
# --------------------------------------------------------------------------


class SpyRedis(FakeRedis):
    """Redis для антидребезга 403: запоминает ключи, умеет «упасть»."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    async def set(self, key, value, nx=False, ex=None):
        if self.broken:
            raise ConnectionError("redis недоступен")
        return await super().set(key, value, nx=nx, ex=ex)


@pytest.fixture
def tg(monkeypatch):
    """Транспорт-заглушка Telegram: ответ задаётся по chat_id; запросы собираются."""
    requests: list[httpx.Request] = []
    behaviour: dict[str, Any] = {"default": (200, {"ok": True, "result": {"message_id": 5}}),
                                 "by_chat": {}, "raise_for": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        chat = str(json.loads(request.content)["chat_id"])
        if chat in behaviour["raise_for"]:
            raise httpx.ConnectError(f"сбой у {request.url}")   # URL с токеном в тексте ошибки
        status, body = behaviour["by_chat"].get(chat, behaviour["default"])
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    redis = SpyRedis()
    monkeypatch.setattr(telegram.httpx, "AsyncClient", factory)
    monkeypatch.setattr("src.core.redis_client.get_redis", lambda: redis)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(settings, "BOT_ALLOWED_CHAT_IDS", "")
    return types.SimpleNamespace(requests=requests, behaviour=behaviour, redis=redis)


def two_recipients(monkeypatch) -> None:
    monkeypatch.setattr(settings, "BOT_ALLOWED_CHAT_IDS", "7")


def chats(requests) -> list[str]:
    return [str(json.loads(r.content)["chat_id"]) for r in requests]


def forbidden() -> tuple[int, dict]:
    return 403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}


# --------------------------------------------------------------------------
# 2–3. send_message
# --------------------------------------------------------------------------


async def test_without_chat_id_it_goes_to_every_recipient(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    assert await telegram.send_message("привет") == 5
    assert chats(tg.requests) == ["42", "7"]


async def test_the_first_failure_does_not_stop_the_second(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["by_chat"]["42"] = (400, {"ok": False})
    tg.behaviour["by_chat"]["7"] = (200, {"ok": True, "result": {"message_id": 9}})
    assert await telegram.send_message("x") == 9                  # id первой УДАЧНОЙ отправки
    assert chats(tg.requests) == ["42", "7"]


async def test_all_failed_means_none(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["default"] = (500, {"ok": False})
    assert await telegram.send_message("x") is None
    tg.behaviour["default"] = (200, {"ok": True, "result": {"message_id": 5}})
    tg.behaviour["raise_for"] = {"42", "7"}
    assert await telegram.send_message("x") is None


async def test_the_first_successful_message_id_is_returned(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["by_chat"]["42"] = (200, {"ok": True, "result": {"message_id": 11}})
    tg.behaviour["by_chat"]["7"] = (200, {"ok": True, "result": {"message_id": 22}})
    assert await telegram.send_message("x") == 11


async def test_a_failure_is_logged_with_chat_id_and_status(tg, monkeypatch, logs) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["by_chat"]["7"] = (400, {"ok": False})
    await telegram.send_message("x")
    failed = [e for e in logs if e["event"] == "telegram_send_failed=1"]
    assert len(failed) == 1 and failed[0]["chat_id"] == "7" and failed[0]["status"] == 400


async def test_403_is_logged_at_most_once_an_hour_per_chat(tg, monkeypatch, logs) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["by_chat"]["7"] = forbidden()
    for _ in range(3):
        assert await telegram.send_message("x") == 5              # владелец получает всегда
    failed = [e for e in logs if e["event"] == "telegram_send_failed=1"]
    assert len(failed) == 1 and failed[0]["status"] == 403
    assert tg.redis.data["tg:forbidden:7"] == "1" and tg.redis.ttl["tg:forbidden:7"] == 3600
    assert len(tg.requests) == 6                          # отправка при этом идёт каждый раз


async def test_403_without_redis_is_logged_every_time(tg, monkeypatch, logs) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["by_chat"]["7"] = forbidden()
    tg.redis.broken = True
    for _ in range(3):
        await telegram.send_message("x")
    assert len([e for e in logs if e["event"] == "telegram_send_failed=1"]) == 3


async def test_403_is_counted_per_chat(tg, monkeypatch, logs) -> None:
    monkeypatch.setattr(settings, "BOT_ALLOWED_CHAT_IDS", "7,8")
    tg.behaviour["by_chat"]["7"] = forbidden()
    tg.behaviour["by_chat"]["8"] = forbidden()
    await telegram.send_message("x")
    await telegram.send_message("x")
    failed = [e["chat_id"] for e in logs if e["event"] == "telegram_send_failed=1"]
    assert failed == ["7", "8"]


async def test_an_explicit_chat_id_is_one_request(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    assert await telegram.send_message("ответ", chat_id="555") == 5
    assert chats(tg.requests) == ["555"]


# --------------------------------------------------------------------------
# 4. Один получатель — запросы те же, что до этапа
# --------------------------------------------------------------------------


def legacy_payload(text, chat, reply_markup=None, disable_notification=False) -> dict:
    """Тело запроса ТАК, как его строил send_message до этапа 9.7.1 (эталон)."""
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if disable_notification:
        payload["disable_notification"] = True
    return payload


async def test_one_recipient_sends_the_legacy_request(tg) -> None:
    markup = {"inline_keyboard": [[{"text": "x", "callback_data": "v1:m"}]]}
    await telegram.send_message("a")
    await telegram.send_message("b", reply_markup=markup, disable_notification=True)
    assert [json.loads(r.content) for r in tg.requests] == [
        legacy_payload("a", "42"),
        legacy_payload("b", "42", markup, True),
    ]
    assert all(r.url.path == f"/bot{TOKEN}/sendMessage" for r in tg.requests)


async def test_one_recipient_demo_paths_keep_the_same_requests(tg) -> None:
    """Сообщения о сделках, сводка и тревога demo при одном получателе — те же запросы."""
    ctx = demo_ctx(("42",), notify=telegram.send_message)
    await messages.deliver(ctx, "сделка", reply_markup=messages.ACCOUNT_BUTTON)
    await runner.alert(ctx, "no_balance", "мало USDT")
    assert [json.loads(r.content) for r in tg.requests] == [
        legacy_payload("сделка", "42", messages.ACCOUNT_BUTTON),
        legacy_payload("⚠️ <b>demo</b>: мало USDT", "42"),
    ]


def _send_message_calls(path: str) -> list[ast.Call]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", "")) == "send_message"]


@pytest.mark.parametrize("path", [
    "src/agents/base.py", "src/export_main.py", "src/positions/runner.py",
    "src/notify/daily_trades.py", "src/demo_main.py",
])
def test_the_system_paths_call_send_message_without_chat_id(path) -> None:
    """Их вызовы — без chat_id, значит, уходят рассылкой §3 без правки их кода."""
    calls = _send_message_calls(path)
    for call in calls:
        assert all(kw.arg != "chat_id" for kw in call.keywords), path
        assert len(call.args) <= 1, path
    if path != "src/demo_main.py":
        assert calls, path


async def test_the_agent_failure_alert_reaches_everyone(tg, monkeypatch) -> None:
    from src.agents import base as agents_base

    two_recipients(monkeypatch)
    fake_agent = types.SimpleNamespace(key="market")
    await agents_base.BaseAgent._alert_failures(fake_agent, 3)
    assert chats(tg.requests) == ["42", "7"]
    assert all("3 сбоев подряд" in json.loads(r.content)["text"] for r in tg.requests)


async def test_the_export_alert_reaches_everyone(tg, monkeypatch) -> None:
    import structlog

    from src import export_main

    two_recipients(monkeypatch)
    await export_main._alert("сбой", structlog.get_logger())
    assert chats(tg.requests) == ["42", "7"]


# --------------------------------------------------------------------------
# 5–6. Демо: у каждого получателя свои тихие часы
# --------------------------------------------------------------------------


class QuietPool:
    """Пул-заглушка ``user_settings``: настройки по chat_id, ошибка чтения по chat_id."""

    def __init__(self, rows: dict[int, tuple[int, int] | None], failing=()) -> None:
        self.rows = rows
        self.failing = set(failing)
        self.reads: list[int] = []

    async def fetchrow(self, sql, chat_id):
        assert "user_settings" in sql
        self.reads.append(chat_id)
        if chat_id in self.failing:
            raise RuntimeError("таблицы нет")
        row = self.rows.get(chat_id)
        return None if row is None else {"quiet_from": row[0], "quiet_to": row[1]}


def demo_ctx(recipients, pool=None, hour_utc=23, notify=None, **config):
    clock = Clock(datetime(2026, 10, 7, hour_utc, 30, tzinfo=UTC))
    sent: list[tuple[str, dict]] = []

    async def collect(text, **kwargs):
        sent.append((text, kwargs))
        return True

    cfg = runner.DemoConfig(host="www.okx.com", recipients=tuple(recipients), **config)
    ctx = runner.Context(pool=pool, exchange=None, redis=FakeRedis(), config=cfg, symbols=[],
                         notify=notify or collect, now=clock.now)
    ctx.sent = sent  # type: ignore[attr-defined]
    return ctx


async def test_each_recipient_gets_a_separate_message_by_his_own_quiet_hours() -> None:
    pool = QuietPool({1: (20, 4), 2: None})                   # у первого тишина, у второго нет
    ctx = demo_ctx(("1", "2"), pool)
    assert await messages.deliver(ctx, "сделка", reply_markup=messages.ACCOUNT_BUTTON) is True
    (t1, k1), (t2, k2) = ctx.sent
    assert t1 == t2 == "сделка"
    assert k1 == {"chat_id": "1", "reply_markup": messages.ACCOUNT_BUTTON,
                  "disable_notification": True}
    assert k2 == {"chat_id": "2", "reply_markup": messages.ACCOUNT_BUTTON}
    assert "disable_notification" not in k2                    # у второго звук


async def test_quiet_hours_are_cached_per_recipient() -> None:
    pool = QuietPool({1: (20, 4), 2: (20, 4)})
    ctx = demo_ctx(("1", "2"), pool)
    await messages.deliver(ctx, "a")
    await messages.deliver(ctx, "b")
    assert sorted(pool.reads) == [1, 2]                        # по одному чтению на каждого
    assert set(ctx.quiet_cache) == {"1", "2"}


async def test_a_settings_read_error_of_one_means_loud_for_him_only(logs) -> None:
    pool = QuietPool({1: (20, 4), 2: (20, 4)}, failing={1})
    ctx = demo_ctx(("1", "2"), pool)
    assert await messages.deliver(ctx, "сделка") is True
    (_, k1), (_, k2) = ctx.sent
    assert "disable_notification" not in k1                    # ошибка — со звуком
    assert k2["disable_notification"] is True                  # у второго — по его настройкам
    assert any(e["event"] == "demo_quiet_hours_read_failed=1" and e["chat_id"] == "1"
               for e in logs)


async def test_delivered_means_at_least_one_got_it() -> None:
    outcomes = {"1": False, "2": True}

    async def notify(text, **kwargs):
        return outcomes[kwargs["chat_id"]]

    ctx = demo_ctx(("1", "2"), QuietPool({}), notify=notify)
    assert await messages.deliver(ctx, "x") is True
    outcomes["2"] = False
    assert await messages.deliver(ctx, "x") is False
    outcomes.update({"1": True, "2": False})
    assert await messages.deliver(ctx, "x") is True


async def test_one_recipient_raising_does_not_stop_the_other() -> None:
    calls: list[str] = []

    async def notify(text, **kwargs):
        calls.append(kwargs["chat_id"])
        if kwargs["chat_id"] == "1":
            raise RuntimeError("сеть")
        return True

    ctx = demo_ctx(("1", "2"), QuietPool({}), notify=notify)
    assert await messages.deliver(ctx, "x") is True and calls == ["1", "2"]

    async def always_raises(text, **kwargs):
        raise RuntimeError("сеть")

    ctx = demo_ctx(("1", "2"), QuietPool({}), notify=always_raises)
    with pytest.raises(RuntimeError):                          # как раньше: решает вызывающий
        await messages.deliver(ctx, "x")


async def test_a_failed_delivery_keeps_the_held_messages(monkeypatch) -> None:
    """«Не дошло никому — очередь не чистить» сохраняется по правилу «хотя бы одному»."""
    async def nobody(text, **kwargs):
        return False

    ctx = demo_ctx(("1", "2"), QuietPool({}), notify=nobody, trades_max_per_hour=1)
    await messages.send_trade_message(ctx, "первое")           # отказ — в счёт потолка не идёт
    await messages.send_trade_message(ctx, "второе")
    assert not ctx.redis.zsets.get(messages.SENT_KEY)
    ok_ctx = demo_ctx(("1", "2"), QuietPool({}), trades_max_per_hour=1)
    await messages.send_trade_message(ok_ctx, "первое")
    assert ok_ctx.redis.zsets[messages.SENT_KEY]               # дошло — учтено


async def test_alert_inside_everyones_quiet_hours_is_loud_for_everyone(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    pool = QuietPool({42: (20, 4), 7: (20, 4)})
    ctx = demo_ctx(("42", "7"), pool, hour_utc=23, notify=telegram.send_message)
    await runner.alert(ctx, "no_balance", "мало USDT")
    assert chats(tg.requests) == ["42", "7"]
    assert all("disable_notification" not in json.loads(r.content) for r in tg.requests)
    assert pool.reads == []                                    # тревога тишину не читает


async def test_a_trade_message_goes_to_both_with_their_own_sound(tg, monkeypatch) -> None:
    two_recipients(monkeypatch)
    pool = QuietPool({42: (20, 4), 7: None})
    ctx = demo_ctx(("42", "7"), pool, hour_utc=23, notify=telegram.send_message)
    await messages.send_trade_message(ctx, "сделка")
    bodies = {str(json.loads(r.content)["chat_id"]): json.loads(r.content) for r in tg.requests}
    assert bodies["42"]["disable_notification"] is True
    assert "disable_notification" not in bodies["7"]
    assert bodies["42"]["reply_markup"] == bodies["7"]["reply_markup"] == messages.ACCOUNT_BUTTON


def test_demo_config_takes_recipients_from_settings() -> None:
    cfg = runner.DemoConfig.from_settings(
        make_settings(TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="7"))
    assert cfg.recipients == ("42", "7")
    assert runner.DemoConfig.from_settings(make_settings()).recipients == ()


# --------------------------------------------------------------------------
# 7. Кнопка «💰 Счёт» у второго получателя
# --------------------------------------------------------------------------


async def test_the_account_button_works_for_the_second_recipient(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEMO_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(settings, "BOT_ALLOWED_CHAT_IDS", "222")
    sent, client, bot = Sent(monkeypatch), FakeClient(), make_bot()
    bot.allowed = settings.bot_allowed_chat_ids
    await bot._handle_callback(client, press(messages.ACCOUNT_CALLBACK, chat_id=222))
    [new] = sent.sent
    assert new["chat_id"] == "222" and "Демо-счёт OKX" in new["text"]   # счёт — в ЕГО чат
    assert sent.edits == []


def test_the_poller_takes_the_chat_from_the_callback_not_from_settings() -> None:
    source = (ROOT / "src" / "bot" / "poller.py").read_text(encoding="utf-8")
    assert "TELEGRAM_CHAT_ID" not in source and "telegram_recipients" not in source
    assert 'message.get("chat")' in source or "message.get('chat')" in source
    assert poller.is_allowed(222, {"1", "222"}) and not poller.is_allowed(3, {"1", "222"})


# --------------------------------------------------------------------------
# 8. Скрипты на хосте: watchdog, daily_report
# --------------------------------------------------------------------------


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def watchdog(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    return _load(SCRIPTS / "watchdog.py", "watchdog_9_7_1")


@pytest.fixture()
def daily_report(monkeypatch, tmp_path):
    from src.health import daily_report as mod

    monkeypatch.setattr(mod, "APP_DIR", str(tmp_path))
    return mod


RECIPIENT_CASES = [
    ({"TELEGRAM_CHAT_ID": "42"}, ["42"]),
    ({"TELEGRAM_CHAT_ID": "42", "BOT_ALLOWED_CHAT_IDS": "7,99"}, ["42", "7", "99"]),
    ({"TELEGRAM_CHAT_ID": "42", "BOT_ALLOWED_CHAT_IDS": "99,7"}, ["42", "99", "7"]),
    ({"BOT_ALLOWED_CHAT_IDS": "7"}, ["7"]),
    ({"TELEGRAM_CHAT_ID": " 42 ", "BOT_ALLOWED_CHAT_IDS": " 7 , ,42,7,, 99 "}, ["42", "7", "99"]),
    ({"TELEGRAM_CHAT_ID": "42", "BOT_ALLOWED_CHAT_IDS": "  # пояснение"}, ["42"]),
    ({}, []),
]


@pytest.mark.parametrize(("env", "expected"), RECIPIENT_CASES)
def test_host_scripts_use_the_same_recipient_rule(watchdog, daily_report, env, expected) -> None:
    assert watchdog._recipients(env) == expected
    assert daily_report._recipients(env) == expected
    assert make_settings(**{k: v.split("#")[0] for k, v in env.items()}).telegram_recipients == \
        expected                                                # то же правило, что в Settings


class UrlSpy:
    """Заглушка urllib.request.urlopen: запоминает тела запросов, по chat_id может падать."""

    def __init__(self, failing=()) -> None:
        self.bodies: list[dict[str, str]] = []
        self.timeouts: list[int] = []
        self.failing = set(failing)

    def __call__(self, req, timeout=None, context=None):
        body = dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))
        self.bodies.append(body)
        self.timeouts.append(timeout)
        if body["chat_id"] in self.failing:
            raise OSError(f"HTTP Error 403 at {req.full_url}")   # URL с токеном в тексте ошибки
        return types.SimpleNamespace(read=lambda: b"{}")


def host_env(monkeypatch, module, **env) -> None:
    monkeypatch.setattr(module, "ENV", {"TELEGRAM_BOT_TOKEN": TOKEN, **env})
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BOT_ALLOWED_CHAT_IDS", "SSL_CERT_FILE"):
        monkeypatch.delenv(key, raising=False)


def test_watchdog_sends_to_everyone_and_survives_one_failure(
    watchdog, monkeypatch, capsys
) -> None:
    host_env(monkeypatch, watchdog, TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="7")
    spy = UrlSpy(failing={"42"})
    monkeypatch.setattr("urllib.request.urlopen", spy)
    assert watchdog._telegram("тревога") is True                # дошло хотя бы одному
    assert [b["chat_id"] for b in spy.bodies] == ["42", "7"]
    assert all(b["text"] == "тревога" and b["parse_mode"] == "HTML" for b in spy.bodies)
    out = capsys.readouterr().out
    assert "chat_id=42" in out and TOKEN not in out             # токен в журнал не попадает
    spy.failing = {"42", "7"}
    assert watchdog._telegram("тревога") is False
    assert TOKEN not in capsys.readouterr().out


def test_watchdog_with_one_recipient_sends_the_legacy_request(watchdog, monkeypatch) -> None:
    host_env(monkeypatch, watchdog, TELEGRAM_CHAT_ID="42")
    spy = UrlSpy()
    monkeypatch.setattr("urllib.request.urlopen", spy)
    watchdog._telegram("т")
    assert spy.bodies == [{"chat_id": "42", "text": "т", "parse_mode": "HTML",
                           "disable_web_page_preview": "true"}]
    assert spy.timeouts == [10]


def test_daily_report_sends_to_everyone_and_survives_one_failure(
    daily_report, monkeypatch, capsys
) -> None:
    host_env(monkeypatch, daily_report, TELEGRAM_CHAT_ID="42", BOT_ALLOWED_CHAT_IDS="7,99")
    spy = UrlSpy(failing={"7"})
    monkeypatch.setattr("urllib.request.urlopen", spy)
    assert daily_report.send_telegram("сводка") is True
    assert [b["chat_id"] for b in spy.bodies] == ["42", "7", "99"]
    assert spy.timeouts == [15, 15, 15]
    out = capsys.readouterr().out
    assert "chat_id=7" in out and TOKEN not in out
    spy.failing = {"42", "7", "99"}
    assert daily_report.send_telegram("сводка") is False
    assert TOKEN not in capsys.readouterr().out


def test_daily_report_with_one_recipient_sends_the_legacy_request(
    daily_report, monkeypatch
) -> None:
    host_env(monkeypatch, daily_report, TELEGRAM_CHAT_ID="42")
    spy = UrlSpy()
    monkeypatch.setattr("urllib.request.urlopen", spy)
    assert daily_report.send_telegram("с") is True
    assert spy.bodies == [{"chat_id": "42", "text": "с", "parse_mode": "HTML",
                           "disable_web_page_preview": "true"}]


def test_host_scripts_without_token_or_recipients_send_nothing(
    watchdog, daily_report, monkeypatch
) -> None:
    spy = UrlSpy()
    monkeypatch.setattr("urllib.request.urlopen", spy)
    host_env(monkeypatch, watchdog, TELEGRAM_CHAT_ID="")
    assert watchdog._telegram("x") is False
    host_env(monkeypatch, daily_report)
    assert daily_report.send_telegram("x") is False
    assert spy.bodies == []


# --------------------------------------------------------------------------
# 9. scripts/lib_common.sh: telegram_alert
# --------------------------------------------------------------------------


def _bash_ok() -> bool:
    return subprocess.run(["bash", "-c", "true"], check=False).returncode == 0


@pytest.fixture()
def shell(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    curl = bindir / "curl"
    curl.write_text(f'#!/usr/bin/env bash\necho "curl $*" >> "{calls}"\n', encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)

    def run(env_file: str, snippet: str = 'telegram_alert "тревога"'):
        (app / ".env").write_text(env_file, encoding="utf-8")
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "APP_DIR": str(app), "HOME": str(tmp_path)}
        return subprocess.run(
            ["bash", "-c", f'source "{SCRIPTS}/lib_common.sh"; {snippet}'],
            env=env, capture_output=True, text=True, check=False)

    return types.SimpleNamespace(run=run, calls=calls)


pytestmark_bash = pytest.mark.skipif(not _bash_ok(), reason="нужен bash")


@pytestmark_bash
def test_telegram_alert_calls_curl_once_per_recipient(shell) -> None:
    result = shell.run("TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\nBOT_ALLOWED_CHAT_IDS=7, 99\n")
    assert result.returncode == 0, result.stderr
    lines = shell.calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [ln.split("chat_id=")[1].split(" ")[0] for ln in lines] == ["42", "7", "99"]
    assert all("bottok/sendMessage" in ln for ln in lines)


@pytestmark_bash
def test_telegram_alert_with_one_recipient_calls_curl_once(shell) -> None:
    shell.run("TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\nBOT_ALLOWED_CHAT_IDS=   # пояснение\n")
    assert len(shell.calls.read_text(encoding="utf-8").splitlines()) == 1


@pytestmark_bash
def test_telegram_alert_skips_duplicates_and_the_owner_is_first(shell) -> None:
    result = shell.run(
        "TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\nBOT_ALLOWED_CHAT_IDS=7,42,,7\n",
        snippet="telegram_recipients")
    assert result.stdout.split() == ["42", "7"]
    only_allowed = shell.run("TELEGRAM_BOT_TOKEN=tok\nBOT_ALLOWED_CHAT_IDS=7\n",
                             snippet="telegram_recipients")
    assert only_allowed.stdout.split() == ["7"]


@pytestmark_bash
def test_telegram_alert_without_token_or_recipients_does_nothing(shell) -> None:
    assert shell.run("TELEGRAM_CHAT_ID=42\n").returncode == 0
    assert shell.run("TELEGRAM_BOT_TOKEN=tok\n").returncode == 0
    assert shell.calls.read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------
# 10. Токен бота не попадает в журнал
# --------------------------------------------------------------------------


async def test_the_token_never_reaches_the_log_on_any_failure_path(
    tg, monkeypatch, logs
) -> None:
    two_recipients(monkeypatch)
    tg.behaviour["raise_for"] = {"42", "1"}                  # исключение с URL (с токеном)
    tg.behaviour["by_chat"]["7"] = (403, {"ok": False, "description": f"bad {TOKEN}"})
    await telegram.send_message("x")
    await telegram.edit_message(1, 5, "t")
    assert logs, "сбои должны попасть в журнал"
    assert TOKEN not in json.dumps(logs, default=str, ensure_ascii=False)


def test_the_source_of_the_senders_logs_no_url() -> None:
    text = (ROOT / "src" / "notify" / "telegram.py").read_text(encoding="utf-8")
    assert "url=" not in text and "_api_url(" in text


# --------------------------------------------------------------------------
# 7. deploy/verify_9_7_1.sh: без --test ничего не отправляется
# --------------------------------------------------------------------------


@pytest.fixture()
def verify(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    docker = bindir / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
echo "docker $*" >> "{calls}"
case "$*" in
  *"python -c"*) echo "42 7" ;;
  *"ps --format"*) echo running ;;
  *"config --services"*) echo export ;;
  *"python -"*) cat > /dev/null; echo "42 ok"; echo "7 ok" ;;
esac
""", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (app / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\nBOT_ALLOWED_CHAT_IDS=7\n", encoding="utf-8")

    def run(*args: str):
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "APP_DIR": str(app), "HOME": str(tmp_path)}
        return subprocess.run(["bash", str(ROOT / "deploy" / "verify_9_7_1.sh"), *args], env=env,
                              capture_output=True, text=True, check=False)

    return types.SimpleNamespace(run=run, calls=calls)


@pytestmark_bash
def test_verify_script_sends_nothing_without_the_flag(verify) -> None:
    result = verify.run()
    assert "получателей: 2" in result.stdout and "chat_id: 7" in result.stdout
    assert "владелец (TELEGRAM_CHAT_ID=42) среди получателей" in result.stdout
    calls = verify.calls.read_text(encoding="utf-8").splitlines()
    assert not any(ln.endswith("python -") or "PROBE_TEXT" in ln for ln in calls)


@pytestmark_bash
def test_verify_script_probe_goes_to_every_recipient_with_the_flag(verify) -> None:
    result = verify.run("--test")
    assert "chat_id 42: пробное сообщение доставлено" in result.stdout
    assert "chat_id 7: пробное сообщение доставлено" in result.stdout
    calls = verify.calls.read_text(encoding="utf-8")
    assert "PROBE_TEXT=Проверка связи Agent Trade — можно не отвечать" in calls
    assert verify.run("--bogus").returncode == 2
