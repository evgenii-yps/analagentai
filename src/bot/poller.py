"""Long polling getUpdates, контроль доступа и маршрутизация команд бота.

Сетевой и БД-ввод-вывод сосредоточены здесь; форматирование ответов — в
:mod:`src.bot.handlers` (чистые функции). Бот НЕ имеет ни одной команды,
меняющей состояние системы (ТЗ §7.6).

Ошибки сети и Telegram API только логируются — сервис продолжает работу.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from src.bot import handlers, nav, screens, settings_menu
from src.bot.nav import NavTarget
from src.bot.queries import PERIOD_SECONDS, BotQueries
from src.core import fmt
from src.core.config import settings
from src.core.instruments import horizon_label
from src.core.redis_client import get_redis
from src.core.user_settings import UserSettings, default_settings
from src.demo import ledger
from src.notify.agent import AGENT_ORDER
from src.notify.telegram import edit_message, send_message, set_my_commands
from src.positions.rules import REFUSAL_REASONS, refusal_key

# TTL heartbeat-ключа (секунды) — как у остальных сервисов.
_HEARTBEAT_TTL = 300

# Пауза при HTTP 409 (запущен второй экземпляр бота) — ТЗ §10.
_CONFLICT_SLEEP_SEC = 30

# heartbeat-ключи для /status и /summary: (ключ, имя env-интервала). Список и
# интервалы согласованы с scripts/watchdog.py; свой ключ бота добавлен последним.
HEARTBEAT_KEYS: list[tuple[str, str]] = [
    ("collector:heartbeat:ohlcv", "OHLCV_INTERVAL"),
    ("collector:heartbeat:orderbook", "ORDERBOOK_INTERVAL"),
    ("collector:heartbeat:trades", "TRADES_INTERVAL"),
    ("collector:heartbeat:futures", "FUTURES_INTERVAL"),
    ("agent:heartbeat:market", "AGENT_INTERVAL"),
    ("agent:heartbeat:liquidity", "AGENT_INTERVAL"),
    ("agent:heartbeat:futures", "AGENT_INTERVAL"),
    ("decision:heartbeat", "DECISION_INTERVAL"),
    ("notify:heartbeat", "NOTIFY_INTERVAL"),
    ("evaluator:heartbeat", "EVAL_INTERVAL"),
    # Этап 9.1.1 §4. Сервис ведения позиций пишет свой heartbeat с Этапа 9.1, но
    # ключ не читал никто: остановившийся сервис ничем себя не проявлял, а его
    # остановка означает, что уже открытые позиции повиснут — задетые за время
    # простоя цель и предел не будут замечены никогда, потому что бары уйдут за
    # отметку last_checked_ts только вместе с их разбором.
    ("positions:heartbeat", "POSITION_INTERVAL"),
]

# Служба демо-исполнения (Д4): главная служба при DEMO_ENABLED=true. Её отметку
# ждёт и /status, и главный экран; при выключенном демо ключа нет вовсе — служба
# в этом режиме пишет отметку «вхолостую», но проверять её незачем.
DEMO_HEARTBEAT_KEY: tuple[str, str] = ("demo:heartbeat", "DEMO_INTERVAL")


def heartbeat_keys() -> list[tuple[str, str]]:
    """Какие отметки служб проверяются сейчас: базовый список плюс demo при DEMO_ENABLED."""
    keys = list(HEARTBEAT_KEYS)
    if settings.DEMO_ENABLED:
        keys.append(DEMO_HEARTBEAT_KEY)
    return keys


# Меню команд Telegram (кнопка «Меню» слева от поля ввода). /positions в него не входит.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("menu", "Главный экран"),
    ("demo", "Демо-счёт"),
    ("trades", "Сделки"),
    ("results", "Итоги"),
    ("agents", "Что думают агенты"),
    ("signals", "Сигналы"),
    ("status", "Состояние системы"),
    ("settings", "Настройки"),
    ("help", "Справка"),
]

THROTTLED_TEXT = "Секунду…"
NOT_CHANGED_TEXT = "Данные не изменились"
FAILED_TEXT = "Не получилось обновить, попробуйте ещё раз"


def is_allowed(chat_id: Any, allowed: set[str]) -> bool:
    """Белый список: chat_id (число из Telegram) сверяется по строке (ТЗ §7.1)."""
    return str(chat_id) in allowed


async def check_rate_limit(
    redis: Any, chat_id: Any, rate_limit_sec: int, scope: str = ""
) -> bool:
    """Анти-флуд: не чаще одной команды в ``rate_limit_sec`` на чат (ТЗ §7.2).

    Состояние — в Redis (атомарный SET NX EX). Возвращает True, если команду
    можно обработать; False — если её нужно тихо отбросить. ``scope`` отделяет счётчик
    нажатий кнопок от счётчика текстовых команд: команда и сразу за ней нажатие кнопки
    друг друга не блокируют.
    """
    key = f"bot:ratelimit:{scope}:{chat_id}" if scope else f"bot:ratelimit:{chat_id}"
    was_set = await redis.set(key, "1", ex=max(1, rate_limit_sec), nx=True)
    return bool(was_set)


def _verify() -> str | bool:
    """CA для TLS — как в src.notify.telegram (учитывает SSL_CERT_FILE)."""
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


class BotPoller:
    """Цикл long polling: получает апдейты, проверяет доступ, отвечает экранами."""

    def __init__(self, queries: BotQueries) -> None:
        self.queries = queries
        self.redis = get_redis()
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.allowed = settings.bot_allowed_chat_ids
        self.poll_timeout = settings.BOT_POLL_TIMEOUT
        self.rate_limit_sec = settings.BOT_RATE_LIMIT_SEC
        self.max_rows = settings.BOT_MAX_ROWS
        self.tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
        self._offset: int | None = None
        self._log = structlog.get_logger().bind(component="bot")

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def flags(self) -> screens.Flags:
        """Фактические флаги системы: тексты экранов строятся от них."""
        return screens.Flags(
            notify_signals=bool(settings.NOTIFY_SIGNALS_ENABLED),
            demo_enabled=bool(settings.DEMO_ENABLED),
            demo_notify=bool(settings.DEMO_NOTIFY_ENABLED),
        )

    async def run(self) -> None:
        """Основной цикл: сброс очереди → меню команд → апдейты → ответы → heartbeat."""
        self._log.info(
            "Бот запущен (long polling)",
            allowed_chats=len(self.allowed),
            poll_timeout=self.poll_timeout,
        )
        async with httpx.AsyncClient(verify=_verify()) as client:
            await self._flush_backlog(client)
            await self._register_commands()
            await self._heartbeat()
            while True:
                try:
                    updates = await self._get_updates(client, self._offset)
                    for update in updates:
                        self._offset = int(update["update_id"]) + 1
                        await self._handle_update(client, update)
                    if updates:
                        await self.redis.set("bot:offset", self._offset)
                    await self._heartbeat()
                except asyncio.CancelledError:
                    self._log.info("Бот остановлен")
                    raise
                except Exception as exc:  # noqa: BLE001 — сервис не падает
                    self._log.warning("Ошибка итерации бота", error=str(exc))
                    await asyncio.sleep(3)

    async def _register_commands(self) -> None:
        """Один раз при старте задаёт меню команд Telegram. Ошибка не мешает работе."""
        try:
            ok = await set_my_commands(BOT_COMMANDS)
        except Exception as exc:  # noqa: BLE001
            ok = False
            self._log.warning("bot_set_my_commands_failed=1", error=str(exc))
        if ok:
            self._log.info("bot_set_my_commands=1", commands=len(BOT_COMMANDS))
        else:
            self._log.warning("bot_set_my_commands_failed=1", note="меню команд не задано")

    async def _flush_backlog(self, client: httpx.AsyncClient) -> None:
        """Отбрасывает очередь старых апдейтов при старте (offset=-1, ТЗ §7.3).

        Иначе после перезапуска бот ответил бы на все накопившиеся команды сразу.
        update_id последнего апдейта сохраняем в Redis (ключ bot:offset).
        """
        try:
            updates = await self._get_updates(client, offset=-1, timeout=0)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось сбросить очередь апдейтов", error=str(exc))
            updates = []
        if updates:
            self._offset = int(updates[-1]["update_id"]) + 1
        else:
            stored = await self.redis.get("bot:offset")
            self._offset = int(stored) if stored else None
        if self._offset is not None:
            await self.redis.set("bot:offset", self._offset)
        self._log.info("Очередь старых апдейтов сброшена", next_offset=self._offset)

    async def _get_updates(
        self,
        client: httpx.AsyncClient,
        offset: int | None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Вызывает getUpdates. Возвращает список апдейтов ([] при любой ошибке)."""
        poll_timeout = self.poll_timeout if timeout is None else timeout
        params: dict[str, Any] = {
            "timeout": poll_timeout,
            # Нажатия кнопок — тоже апдейты. Без них Telegram их просто не пришлёт,
            # и кнопки были бы декорацией.
            "allowed_updates": '["message", "callback_query"]',
        }
        if offset is not None:
            params["offset"] = offset
        resp = await client.get(
            f"{self._base_url}/getUpdates",
            params=params,
            timeout=poll_timeout + 10,
        )
        if resp.status_code == 409:
            # Terminated by other getUpdates — где-то запущен второй экземпляр.
            self._log.warning(
                "HTTP 409: getUpdates перехвачен другим экземпляром бота — "
                f"жду {_CONFLICT_SLEEP_SEC}с и повторяю"
            )
            await asyncio.sleep(_CONFLICT_SLEEP_SEC)
            return []
        if resp.status_code != 200:
            self._log.warning("getUpdates вернул ошибку", status=resp.status_code)
            return []
        data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("result", [])

    # ------------------------------------------------------------------ #
    # Текстовые команды.
    # ------------------------------------------------------------------ #

    async def _handle_update(self, client: httpx.AsyncClient, update: dict[str, Any]) -> None:
        """Проверяет доступ и, если можно, отвечает на команду или нажатие."""
        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(client, callback)
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return

        # Белый список: чужие чаты игнорируем МОЛЧА, в лог только chat_id (§7.1).
        if not is_allowed(chat_id, self.allowed):
            self._log.info("Игнорирую сообщение вне белого списка", chat_id=chat_id)
            return

        # Анти-флуд: лишние команды тихо отбрасываем (§7.2).
        if not await check_rate_limit(self.redis, chat_id, self.rate_limit_sec):
            return

        text = message.get("text") or ""
        answer = await self._answer_command(text, int(chat_id))
        if isinstance(answer, str):
            for chunk in handlers.split_message(answer):
                await send_message(chunk, chat_id=str(chat_id))
            return
        body, markup = answer
        await send_message(body, chat_id=str(chat_id), reply_markup=markup)

    async def _answer_command(self, text: str, chat_id: int) -> str | screens.Screen:
        """Команда → экран (текст + кнопки) или, для /positions, просто текст."""
        cmd, args = handlers.parse_command(text)
        now = datetime.now(UTC)

        if cmd in ("start", "menu"):
            return await self._render(NavTarget(nav.MENU), chat_id, now)
        if cmd in ("help",):
            return await self._render(NavTarget(nav.HELP), chat_id, now)
        if cmd in ("demo", "account"):
            return await self._render(NavTarget(nav.ACCOUNT), chat_id, now)
        if cmd == "trades":
            return await self._render(NavTarget(nav.TRADES), chat_id, now)
        if cmd == "results":
            period = args[0].lower() if args and args[0].lower() in nav.PERIODS \
                else nav.DEFAULT_PERIOD
            return await self._render(NavTarget(nav.RESULTS, period=period), chat_id, now)
        if cmd == "stats":
            return await self._render(NavTarget(nav.QUALITY), chat_id, now)
        if cmd == "agents":
            return await self._render(NavTarget(nav.AGENTS), chat_id, now)
        if cmd in ("signals", "last"):
            notified_only, limit = handlers.parse_last_args(args, self.max_rows)
            # /last — как раньше: сильные, а «all» — все. /signals при выключенных
            # сигнальных уведомлениях открывается на ВСЕХ решениях: иначе список пуст
            # и выглядит как поломка.
            wants_all = not notified_only
            if cmd == "signals" and not self.flags.notify_signals:
                wants_all = True
            mode = "a" if wants_all else "s"
            return await self._render(
                NavTarget(nav.SIGNALS, mode=mode), chat_id, now, limit=limit
            )
        if cmd == "signal":
            signal_id = handlers.parse_signal_id(args)
            if signal_id is None:
                return "Укажите номер сигнала: например, /signal 1847"
            default_mode = "s" if self.flags.notify_signals else "a"
            return await self._render(
                NavTarget(nav.SIGNAL_CARD, mode=default_mode, signal_id=signal_id),
                chat_id, now,
            )
        if cmd == "status":
            return await self._render(NavTarget(nav.SYSTEM), chat_id, now)
        if cmd == "summary":
            return await self._render(NavTarget(nav.SYSTEM_DETAIL), chat_id, now)
        if cmd == "settings":
            return await self._render(NavTarget(nav.SETTINGS), chat_id, now)
        if cmd == "positions":
            # Команда остаётся известной, но при VIRTUAL_OUTPUT_ENABLED=false отвечает
            # одной строкой и в базу за позициями не ходит (§2.2). В меню команд и в
            # справке её нет.
            if not settings.VIRTUAL_OUTPUT_ENABLED:
                return handlers.render_hidden_positions()
            return await self._cmd_positions(now)
        return screens.unknown_screen()

    # ------------------------------------------------------------------ #
    # Нажатия кнопок.
    # ------------------------------------------------------------------ #

    async def _handle_callback(
        self, client: httpx.AsyncClient, callback: dict[str, Any]
    ) -> None:
        """Обрабатывает нажатие кнопки.

        Ответ на нажатие обязателен всегда: без ``answerCallbackQuery`` кнопка в
        Telegram остаётся «нажатой» и человек считает, что бот завис. Поэтому он
        отправляется в любом исходе, в том числе при ошибке внутри обработчика.
        """
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        callback_id = callback.get("id")
        if callback_id is None:
            return
        if chat_id is None or not is_allowed(chat_id, self.allowed):
            if chat_id is not None:
                self._log.info("Игнорирую нажатие вне белого списка", chat_id=chat_id)
            # Закрываем «часики» и чужому: бот молчит о причине, но не зависает кнопка.
            await self._answer_callback(client, callback_id, "")
            return

        reply = ""
        try:
            reply = await self._dispatch_callback(
                client, callback, int(chat_id), message.get("message_id")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — кнопка не должна ронять бота
            self._log.warning("Ошибка обработки нажатия", error=str(exc))
            reply = FAILED_TEXT
        finally:
            await self._answer_callback(client, callback_id, reply)

    async def _dispatch_callback(
        self,
        client: httpx.AsyncClient,
        callback: dict[str, Any],
        chat_id: int,
        message_id: Any,
    ) -> str:
        """Выполняет нажатие и возвращает текст всплывающего ответа (может быть пустым)."""
        data = str(callback.get("data") or "")
        target = nav.parse_nav(data)
        if target is not None:
            return await self._handle_nav(chat_id, message_id, target)

        parsed = settings_menu.parse_callback(data)
        if parsed is None:
            return screens.stale_button_text()
        return await self._handle_settings_callback(chat_id, message_id, *parsed)

    async def _handle_nav(self, chat_id: int, message_id: Any, target: NavTarget) -> str:
        """Нажатие кнопки навигации: экран правится В ТОМ ЖЕ сообщении."""
        # Анти-флуд для нажатий — тот же BOT_RATE_LIMIT_SEC, но вместо молчания ответ.
        if not await check_rate_limit(self.redis, chat_id, self.rate_limit_sec, scope="cb"):
            return THROTTLED_TEXT
        now = datetime.now(UTC)
        try:
            text, markup = await self._render(target, chat_id, now)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось собрать экран", screen=target.screen, error=str(exc))
            return FAILED_TEXT
        if target.screen == nav.ACCOUNT_NEW or message_id is None:
            # Сообщение о сделке — запись: его не правят, счёт приходит новым сообщением.
            if not await send_message(text, chat_id=str(chat_id), reply_markup=markup):
                return FAILED_TEXT
            return ""
        result = await edit_message(chat_id, int(message_id), text, markup)
        if result == "not_modified":
            return NOT_CHANGED_TEXT
        if result == "failed":
            # Правка не удалась (сообщение удалено, сеть): шлём тот же экран новым.
            if not await send_message(text, chat_id=str(chat_id), reply_markup=markup):
                return FAILED_TEXT
        return ""

    async def _handle_settings_callback(
        self, chat_id: int, message_id: Any, action: str, value: str
    ) -> str:
        """Кнопки меню настроек (tok/hor/thr/quiet/qoff/qf/qt/menu). Без анти-флуда:
        человек нажимает монеты подряд, и пауза в три секунды здесь вредила бы."""
        instruments = await self.queries.spot_instruments()
        current = await self._chat_settings(chat_id)
        now = datetime.now(UTC)

        async def show(user: UserSettings) -> None:
            text, markup = screens.settings_screen(
                user=user, instruments=instruments, flags=self.flags, tz=self.tz, now=now
            )
            await self._edit_or_send(chat_id, message_id, text, markup)

        # Выбор часов тишины — два шага, состояние несёт сама кнопка.
        label = fmt.tz_label(self.tz)
        if action == "quiet":
            await self._edit_or_send(
                chat_id, message_id,
                f"🔕 <b>Тихие часы</b>\nС какого часа молчать? Время — по {label}.",
                settings_menu.hours_keyboard("qf:", "начала"),
            )
            return "С какого часа молчать?"
        if action == "qf":
            await self._edit_or_send(
                chat_id, message_id,
                "🔕 <b>Тихие часы</b>\nС какого часа молчать? Время — по "
                f"{label}.\nНачало: {settings_menu.hour_label(value)}. "
                "Теперь выберите час, в который звук вернётся.",
                settings_menu.hours_keyboard(f"qt:{value}:", "конца"),
            )
            return "До какого часа молчать?"
        if action == "menu":
            await show(current)
            return ""

        updated, note = settings_menu.apply_callback(
            current, action, value, instruments, tz=self.tz, now=now
        )
        if updated != current:
            await self._save_settings(updated, instruments)
        await show(updated)
        return note

    async def _edit_or_send(
        self, chat_id: int, message_id: Any, text: str, markup: dict[str, Any]
    ) -> None:
        """Правит сообщение на месте; не вышло — присылает новым (текст не теряется)."""
        if message_id is not None:
            result = await edit_message(chat_id, int(message_id), text, markup)
            if result in ("ok", "not_modified"):
                return
        await send_message(text, chat_id=str(chat_id), reply_markup=markup)

    async def _chat_settings(self, chat_id: int) -> UserSettings:
        """Настройки чата; при отсутствии записи — значения по умолчанию (§1)."""
        row = await self.queries.user_settings(chat_id)
        if row is None:
            return default_settings(chat_id, settings.NOTIFY_MIN_PROBABILITY)
        return UserSettings(
            chat_id=chat_id,
            instruments=tuple(int(i) for i in row["instruments"]),
            horizon_h=int(row["horizon_h"]),
            min_score=float(row["min_score"]),
            quiet_from=None if row["quiet_from"] is None else int(row["quiet_from"]),
            quiet_to=None if row["quiet_to"] is None else int(row["quiet_to"]),
        )

    async def _save_settings(
        self, updated: UserSettings, instruments: list[tuple[int, str]]
    ) -> None:
        """Сохраняет настройки чата целиком."""
        chosen = (
            [instrument_id for instrument_id, _ in instruments]
            if updated.instruments is None
            else list(updated.instruments)
        )
        await self.queries.save_user_settings(
            updated.chat_id, chosen, updated.horizon_h, updated.min_score,
            updated.quiet_from, updated.quiet_to,
        )

    async def _answer_callback(
        self, client: httpx.AsyncClient, callback_id: str, text: str
    ) -> None:
        """Всплывающий ответ над кнопкой (закрывает «часики»). Ошибки только логируются."""
        try:
            await client.post(
                f"{self._base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("answerCallbackQuery не удался", error=str(exc))

    # ------------------------------------------------------------------ #
    # Сборка экранов: запросы → чистые функции screens.
    # ------------------------------------------------------------------ #

    async def _render(
        self, target: NavTarget, chat_id: int, now: datetime, limit: int | None = None
    ) -> screens.Screen:
        """Экран по адресу. Время сборки пишется в журнал (замер ≤ 1 с на боевой базе)."""
        started = time.perf_counter()
        screen = await self._build(target, chat_id, now, limit)
        self._log.info(
            "bot_screen=1", screen=target.screen,
            ms=round((time.perf_counter() - started) * 1000),
        )
        return screen

    async def _build(
        self, target: NavTarget, chat_id: int, now: datetime, limit: int | None
    ) -> screens.Screen:
        flags = self.flags
        name = target.screen

        if name == nav.MENU:
            state = None
            if flags.demo_enabled:
                demo = await self._demo_state()
                state = demo["state"] if demo else None
            instruments = await self.queries.spot_instruments()
            return screens.main_screen(
                flags=flags, coins=screens.coin_names([sym for _, sym in instruments]),
                state=state, health=await self._health(now), now=now, tz=self.tz,
            )

        if name in (nav.ACCOUNT, nav.ACCOUNT_NEW):
            demo = await self._demo_state() if flags.demo_enabled else None
            yesterday = None
            week = None
            if demo is not None:
                yesterday = await self.queries.demo_yesterday_close(
                    ledger.local_day(now, self.tz)
                )
                week = await self.queries.demo_report(now - timedelta(days=7))
            return screens.account_screen(
                demo=demo, yesterday_close=yesterday, week=week, now=now, tz=self.tz,
                demo_enabled=flags.demo_enabled,
            )

        if name == nav.TRADES:
            overview = await self._demo_overview() if flags.demo_enabled else None
            closed = await self.queries.demo_recent_closed(5) if overview else []
            return screens.trades_screen(
                open_trades=overview["open_trades"] if overview else None,
                recent_closed=closed, now=now, tz=self.tz, demo_enabled=flags.demo_enabled,
            )

        if name == nav.RESULTS:
            period = target.period or nav.DEFAULT_PERIOD
            demo = await self._demo_state() if flags.demo_enabled else None
            if demo is None:
                return screens.demo_not_started_screen(
                    "📊 <b>Итоги демо-счёта</b>", NavTarget(nav.RESULTS, period=period),
                    now, self.tz,
                )
            if period == "today":
                since = ledger.day_bounds(ledger.local_day(now, self.tz), self.tz)[0]
            elif period == "7d":
                since = now - timedelta(days=7)
            elif period == "30d":
                since = now - timedelta(days=30)
            else:
                since = demo["mirror_since"]
            report = await self.queries.demo_report(since)
            return screens.results_screen(period=period, report=report, now=now, tz=self.tz)

        if name == nav.QUALITY:
            return screens.quality_screen(await self._stats_text(now, chat_id))

        if name == nav.AGENTS:
            instruments = await self.queries.spot_instruments()
            return screens.agents_screen(
                tokens=screens.coin_names([sym for _, sym in instruments]),
                by_token=await self.queries.agents_by_token(
                    list(AGENT_ORDER), max_age_sec=max(3600, 2 * settings.AGENT_FRESHNESS_SEC)
                ),
                decisions=await self.queries.latest_signal_by_token(),
                freshness_sec=settings.AGENT_FRESHNESS_SEC, flags=flags, now=now, tz=self.tz,
            )

        if name == nav.SIGNALS:
            mode = target.mode or "s"
            user = await self._chat_settings(chat_id)
            rows = await self.queries.last_signals(
                mode == "s", limit or 5,
                None if user.instruments is None else list(user.instruments),
            )
            return screens.signals_screen(
                signals=rows, mode=mode, flags=flags, now=now, tz=self.tz
            )

        if name == nav.SIGNAL_CARD:
            user = await self._chat_settings(chat_id)
            card = await self.queries.signal_card(int(target.signal_id or 0))
            return screens.signal_card_screen(
                handlers.render_signal_card(card, now, user.horizon_h), target.mode or "s"
            )

        if name == nav.SYSTEM:
            return screens.system_screen(health=await self._health(now), now=now, tz=self.tz)

        if name == nav.SYSTEM_DETAIL:
            return screens.detail_screen(await self._detail_text(now, chat_id))

        if name == nav.SETTINGS:
            instruments = await self.queries.spot_instruments()
            return screens.settings_screen(
                user=await self._chat_settings(chat_id), instruments=instruments,
                flags=flags, tz=self.tz, now=now,
            )

        if name == nav.HELP:
            instruments = await self.queries.spot_instruments()
            return screens.help_screen(
                flags=flags, coins=screens.coin_names([sym for _, sym in instruments])
            )

        return screens.unknown_screen()

    async def _demo_state(self) -> dict[str, Any] | None:
        """Состояние учёта; сбой базы не роняет экран — демо выглядит «не запущенным»."""
        try:
            return await self.queries.demo_state()
        except Exception as exc:  # noqa: BLE001 — демо не важнее ответа
            self._log.warning("bot_demo_state_failed=1", error=str(exc))
            return None

    async def _demo_overview(self) -> dict[str, Any] | None:
        try:
            return await self.queries.demo_overview(days=7)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("bot_demo_state_failed=1", error=str(exc))
            return None

    async def _read_heartbeats(self) -> list[tuple[str, str | None, int]]:
        """Читает heartbeat-ключи из Redis → строки для рендера status/summary."""
        rows: list[tuple[str, str | None, int]] = []
        for key, env_attr in heartbeat_keys():
            value = await self.redis.get(key)
            interval = int(getattr(settings, env_attr))
            rows.append((key, value, interval))
        return rows

    async def _health(self, now: datetime) -> list[screens.HealthItem]:
        """Пункты состояния системы — один расчёт для экрана «Система» и главного."""
        hb_rows = await self._read_heartbeats()
        per_token = await self.queries.freshness_by_instrument(None)
        return screens.compute_health(hb_rows, per_token, now)

    async def _detail_text(self, now: datetime, chat_id: int) -> str:
        """«Подробно»: /status и /summary одним экраном — с машинными ключами."""
        status = await self._status_text(now, chat_id)
        summary = await self._summary_text(now, include_heartbeats=False)
        return status + "\n\n" + summary

    async def _status_text(self, now: datetime, chat_id: int) -> str:
        """Прежний подробный /status: свежесть heartbeat-ключей, данных, счётчики сигналов.

        СОСТОЯНИЕ СДЕЛОК ДОБАВЛЕНО ВЕРСИЕЙ 7 (§5 C6 ТЗ): число открытых позиций
        и паузы по токенам. Оно НЕ ДОЛЖНО ронять команду: /status отвечает на
        вопрос «жива ли система», и потерять ответ из-за недоступной таблицы
        позиций значило бы промолчать ровно тогда, когда спрашивают.
        """
        hb_rows = await self._read_heartbeats()
        facts = await self.queries.status_facts()
        user = await self._chat_settings(chat_id)
        per_token = await self.queries.freshness_by_instrument(
            None if user.instruments is None else list(user.instruments)
        )
        # VIRTUAL_OUTPUT_ENABLED=false (этап 9.5, часть 3, §2.3): раздела «Сделки
        # (виртуальные)» нет, вместо него — «Демо-счёт» (только при DEMO_ENABLED=true).
        if not settings.VIRTUAL_OUTPUT_ENABLED:
            demo = None
            if settings.DEMO_ENABLED:
                try:
                    demo = await self.queries.demo_overview(days=7)
                except Exception as exc:  # noqa: BLE001 — демо не важнее ответа
                    self._log.warning("bot_demo_state_failed=1", error=str(exc))
            return handlers.render_status(hb_rows, facts, now, per_token, None, demo)
        pause_sec = float(settings.POSITION_TOKEN_PAUSE_MIN) * 60.0
        try:
            positions = await self.queries.positions_state(
                pause_sec, int(settings.LOGIC_VERSION)
            )
        except Exception as exc:  # noqa: BLE001 — позиции не важнее ответа
            self._log.warning("bot_positions_state_failed=1", error=str(exc))
            positions = None
        if positions is not None:
            positions["token_pause_min"] = int(settings.POSITION_TOKEN_PAUSE_MIN)
        return handlers.render_status(hb_rows, facts, now, per_token, positions)

    async def _stats_text(self, now: datetime, chat_id: int) -> str:
        """«Качество сигналов»: попадания за 7 и 30 дней по выбранному горизонту (§4 ТЗ 8.3)."""
        user = await self._chat_settings(chat_id)
        # §D.4 и §7 ТЗ 8.3: считаем ровно по ОДНОЙ версии логики — последней.
        # Смешивать «до» и «после» смены версии нельзя ни при каких условиях.
        versions = await self.queries.stats_versions(PERIOD_SECONDS["30d"])
        target_version = max(versions) if versions else None
        started_at = (
            await self.queries.logic_version_started_at(target_version)
            if target_version is not None
            else None
        )
        blocks = []
        for period in ("7d", "30d"):
            period_sec = PERIOD_SECONDS[period]
            blocks.append((
                handlers.STATS_PERIODS.get(period, period),
                await self.queries.stats_block(
                    period_sec, True, target_version, user.horizon_h
                ),
                await self.queries.stats_block(
                    period_sec, False, target_version, user.horizon_h
                ),
            ))
        filter_counts = await self.queries.notify_filter_counts(
            PERIOD_SECONDS["7d"], target_version
        )
        return handlers.render_stats(
            blocks, user.horizon_h, now, target_version, started_at, filter_counts
        )

    async def _summary_text(self, now: datetime, include_heartbeats: bool = True) -> str:
        hb_rows = await self._read_heartbeats()
        data_counts = await self.queries.data_counts_24h()
        signal_counts = await self.queries.signal_counts_24h(
            settings.NOTIFY_MIN_PROBABILITY,
            horizon_label(settings.eval_primary_horizon_h),
        )
        db_size = await self.queries.db_size()
        return handlers.render_summary(
            hb_rows, data_counts, signal_counts, db_size, now,
            include_heartbeats=include_heartbeats,
        )

    async def _cmd_positions(self, now: datetime) -> str:
        """/positions: виртуальные позиции (Этап 9.1 §10).

        БОТ ОСТАЁТСЯ ТОЛЬКО НА ЧТЕНИЕ. Право записи у роли ``agenttrade_ro``
        единственное — таблица ``user_settings``, — и этот этап его не
        расширяет: команда ничего не открывает и не закрывает.
        """
        version = int(settings.LOGIC_VERSION)
        open_rows = await self.queries.positions_open()
        # ИТОГ ЗА ОКНО — ПО ТЕКУЩЕЙ ВЕРСИИ ПРАВИЛА, А НЕ ПО ВСЕМ СРАЗУ (§1.4
        # ТЗ 9.2): позиции версий 5 и 6 закрывались разными правилами выхода и
        # несравнимы. Накопленный итог при этом показывается и общий (деньги на
        # счёте общие), и по версиям отдельно — это разные вопросы.
        summary = await self.queries.positions_summary(
            days=7, logic_version=version
        )
        capital = await self.queries.positions_capital()
        return handlers.render_positions(
            open_rows, summary, now, days=7,
            capital=capital,
            budget_usd=settings.POSITION_BUDGET_USD,
            slot_usd=settings.POSITION_SLOT_USD,
            logic_version=version,
            refusals=await self._read_refusals(now, version, days=7),
        )

    async def _read_refusals(
        self, now: datetime, logic_version: int, days: int
    ) -> dict[str, int]:
        """Суточные счётчики отказов во входе из Redis (§7.2 ТЗ 9.2).

        КЛЮЧИ ПИШЕТ СЕРВИС ПОЗИЦИЙ (``src/positions/runner._count_refusals``),
        и имя ключа собирается ТОЙ ЖЕ функцией ``refusal_key``, а не повторено
        здесь строкой: две одинаковые строки в разных файлах однажды разошлись
        бы, и счётчики просто перестали бы находиться — молча, без единой
        ошибки, показывая честный ноль.

        ОШИБКА REDIS НЕ ЛОМАЕТ КОМАНДУ: /positions отвечает и без счётчиков.
        Позиции — это база, а счётчики — удобство, и терять первое из-за
        второго нельзя.
        """
        out: dict[str, int] = {}
        try:
            for offset in range(int(days)):
                day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
                for reason in REFUSAL_REASONS:
                    raw = await self.redis.get(
                        refusal_key(logic_version, day, reason)
                    )
                    if raw is None:
                        continue
                    out[reason] = out.get(reason, 0) + int(raw)
        except Exception as exc:  # noqa: BLE001 — счётчик не важнее ответа
            self._log.warning("bot_refusals_read_failed=1", error=str(exc))
            return {}
        return out

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку живости бота (bot:heartbeat, ISO, TTL 300)."""
        now_iso = datetime.now(UTC).isoformat()
        await self.redis.set("bot:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
