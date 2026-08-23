"""Long polling getUpdates, контроль доступа и маршрутизация команд бота.

Сетевой и БД-ввод-вывод сосредоточены здесь; форматирование ответов — в
:mod:`src.bot.handlers` (чистые функции). Бот НЕ имеет ни одной команды,
меняющей состояние системы (ТЗ §7.6).

Ошибки сети и Telegram API только логируются — сервис продолжает работу.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from src.bot import handlers, settings_menu
from src.bot.queries import PERIOD_SECONDS, BotQueries
from src.core.config import settings
from src.core.instruments import horizon_label
from src.core.redis_client import get_redis
from src.core.user_settings import UserSettings, default_settings
from src.notify.agent import AGENT_ORDER
from src.notify.telegram import send_message

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
    ("bot:heartbeat", "BOT_POLL_TIMEOUT"),
]


def is_allowed(chat_id: Any, allowed: set[str]) -> bool:
    """Белый список: chat_id (число из Telegram) сверяется по строке (ТЗ §7.1)."""
    return str(chat_id) in allowed


async def check_rate_limit(redis: Any, chat_id: Any, rate_limit_sec: int) -> bool:
    """Анти-флуд: не чаще одной команды в ``rate_limit_sec`` на чат (ТЗ §7.2).

    Состояние — в Redis (атомарный SET NX EX). Возвращает True, если команду
    можно обработать; False — если её нужно тихо отбросить.
    """
    key = f"bot:ratelimit:{chat_id}"
    was_set = await redis.set(key, "1", ex=max(1, rate_limit_sec), nx=True)
    return bool(was_set)


def _verify() -> str | bool:
    """CA для TLS — как в src.notify.telegram (учитывает SSL_CERT_FILE)."""
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


class BotPoller:
    """Цикл long polling: получает апдейты, проверяет доступ, отвечает."""

    def __init__(self, queries: BotQueries) -> None:
        self.queries = queries
        self.redis = get_redis()
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.allowed = settings.bot_allowed_chat_ids
        self.poll_timeout = settings.BOT_POLL_TIMEOUT
        self.rate_limit_sec = settings.BOT_RATE_LIMIT_SEC
        self.max_rows = settings.BOT_MAX_ROWS
        self._offset: int | None = None
        self._log = structlog.get_logger().bind(component="bot")

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def run(self) -> None:
        """Основной цикл: сброс очереди → получение апдейтов → ответы → heartbeat."""
        self._log.info(
            "Бот запущен (long polling)",
            allowed_chats=len(self.allowed),
            poll_timeout=self.poll_timeout,
        )
        async with httpx.AsyncClient(verify=_verify()) as client:
            await self._flush_backlog(client)
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
            # Нажатия кнопок меню настроек — тоже апдейты (§1 ТЗ 8.3). Без
            # них Telegram их просто не пришлёт, и меню было бы декорацией.
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
        # /settings отвечает сообщением С КНОПКАМИ, поэтому идёт мимо общего
        # маршрута: тот возвращает только текст.
        cmd, _args = handlers.parse_command(text)
        if cmd == "settings":
            await self._cmd_settings(chat_id)
            return
        answer = await self._route(text, int(chat_id))
        for chunk in handlers.split_message(answer):
            await send_message(chunk, chat_id=str(chat_id))

    async def _cmd_settings(self, chat_id: Any) -> None:
        """Открывает меню настроек: текущее состояние + кнопки (§1 ТЗ 8.3)."""
        instruments = await self.queries.spot_instruments()
        current = await self._chat_settings(int(chat_id))
        await send_message(
            settings_menu.menu_text(current, instruments),
            chat_id=str(chat_id),
            reply_markup=settings_menu.menu_keyboard(current, instruments),
        )

    async def _route(self, text: str, chat_id: int) -> str:
        """Маршрутизация команды → готовый текст ответа.

        ``chat_id`` нужен командам, которые обязаны считаться с настройками
        человека (§4 ТЗ 8.3): ``/last`` показывает только выбранные токены,
        ``/stats`` считает по выбранному горизонту.
        """
        cmd, args = handlers.parse_command(text)
        now = datetime.now(UTC)

        if cmd in ("start", "help"):
            return handlers.render_help()
        if cmd == "status":
            return await self._cmd_status(now, chat_id)
        if cmd == "last":
            return await self._cmd_last(args, now, chat_id)
        if cmd == "signal":
            return await self._cmd_signal(args, now)
        if cmd == "agents":
            return await self._cmd_agents(now)
        if cmd == "stats":
            return await self._cmd_stats(args, now, chat_id)
        if cmd == "summary":
            return await self._cmd_summary(now)
        return handlers.render_unknown()

    async def _handle_callback(
        self, client: httpx.AsyncClient, callback: dict[str, Any]
    ) -> None:
        """Обрабатывает нажатие кнопки меню настроек (§1 ТЗ 8.3).

        Ответ на нажатие обязателен всегда: без ``answerCallbackQuery`` кнопка
        в Telegram остаётся «нажатой» и человек считает, что бот завис. Поэтому
        всплывающее подтверждение отправляется и тогда, когда настройки не
        изменились, — с причиной.
        """
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        callback_id = callback.get("id")
        if chat_id is None or callback_id is None:
            return
        if not is_allowed(chat_id, self.allowed):
            self._log.info("Игнорирую нажатие вне белого списка", chat_id=chat_id)
            return

        parsed = settings_menu.parse_callback(str(callback.get("data") or ""))
        if parsed is None:
            await self._answer_callback(client, callback_id, "")
            return
        action, value = parsed
        message_id = message.get("message_id")
        instruments = await self.queries.spot_instruments()
        current = await self._chat_settings(chat_id)

        # Выбор часов тишины — два шага, состояние несёт сама кнопка.
        if action == "quiet":
            await self._edit_markup(
                client, chat_id, message_id,
                settings_menu.hours_keyboard("qf:", "начала"),
            )
            await self._answer_callback(client, callback_id, "С какого часа молчать?")
            return
        if action == "qf":
            await self._edit_markup(
                client, chat_id, message_id,
                settings_menu.hours_keyboard(f"qt:{value}:", "конца"),
            )
            await self._answer_callback(client, callback_id, "До какого часа молчать?")
            return
        if action == "menu":
            await self._edit_markup(
                client, chat_id, message_id,
                settings_menu.menu_keyboard(current, instruments),
            )
            await self._answer_callback(client, callback_id, "")
            return

        updated, note = settings_menu.apply_callback(current, action, value, instruments)
        if updated != current:
            await self._save_settings(updated, instruments)
        await self._edit_markup(
            client, chat_id, message_id,
            settings_menu.menu_keyboard(updated, instruments),
        )
        await self._answer_callback(client, callback_id, note)

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
        """Всплывающее подтверждение над кнопкой. Ошибки только логируются."""
        try:
            await client.post(
                f"{self._base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("answerCallbackQuery не удался", error=str(exc))

    async def _edit_markup(
        self,
        client: httpx.AsyncClient,
        chat_id: Any,
        message_id: Any,
        markup: dict[str, Any],
    ) -> None:
        """Обновляет кнопки на месте: меню показывает текущее состояние (§1)."""
        if message_id is None:
            return
        try:
            await client.post(
                f"{self._base_url}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": markup,
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("editMessageReplyMarkup не удался", error=str(exc))

    async def _read_heartbeats(self) -> list[tuple[str, str | None, int]]:
        """Читает heartbeat-ключи из Redis → строки для рендера status/summary."""
        rows: list[tuple[str, str | None, int]] = []
        for key, env_attr in HEARTBEAT_KEYS:
            value = await self.redis.get(key)
            interval = int(getattr(settings, env_attr))
            rows.append((key, value, interval))
        return rows

    async def _cmd_status(self, now: datetime, chat_id: int) -> str:
        """/status: живость системы и свежесть данных ПО КАЖДОМУ токену (§4)."""
        hb_rows = await self._read_heartbeats()
        facts = await self.queries.status_facts()
        user = await self._chat_settings(chat_id)
        per_token = await self.queries.freshness_by_instrument(
            None if user.instruments is None else list(user.instruments)
        )
        return handlers.render_status(hb_rows, facts, now, per_token)

    async def _cmd_last(self, args: list[str], now: datetime, chat_id: int) -> str:
        """/last: последние сигналы ПО ВЫБРАННЫМ токенам (§4 ТЗ 8.3)."""
        notified_only, n = handlers.parse_last_args(args, self.max_rows)
        user = await self._chat_settings(chat_id)
        signals = await self.queries.last_signals(
            notified_only, n,
            None if user.instruments is None else list(user.instruments),
        )
        return handlers.render_last(signals, notified_only, now)

    async def _cmd_signal(self, args: list[str], now: datetime) -> str:
        signal_id = handlers.parse_signal_id(args)
        if signal_id is None:
            return "Укажите номер сигнала: например, /signal 1847"
        card = await self.queries.signal_card(signal_id)
        return handlers.render_signal_card(card, now)

    async def _cmd_agents(self, now: datetime) -> str:
        rows = await self.queries.latest_agents(list(AGENT_ORDER))
        return handlers.render_agents(rows, settings.AGENT_FRESHNESS_SEC, now)

    async def _cmd_stats(self, args: list[str], now: datetime, chat_id: int) -> str:
        """/stats: попадания за 7 и 30 дней по выбранному горизонту (§4 ТЗ 8.3)."""
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

    async def _cmd_summary(self, now: datetime) -> str:
        hb_rows = await self._read_heartbeats()
        data_counts = await self.queries.data_counts_24h()
        signal_counts = await self.queries.signal_counts_24h(
            settings.NOTIFY_MIN_PROBABILITY,
            horizon_label(settings.eval_primary_horizon_h),
        )
        db_size = await self.queries.db_size()
        return handlers.render_summary(hb_rows, data_counts, signal_counts, db_size, now)

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку живости бота (bot:heartbeat, ISO, TTL 300)."""
        now_iso = datetime.now(UTC).isoformat()
        await self.redis.set("bot:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
