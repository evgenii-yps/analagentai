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

from src.bot import handlers
from src.bot.queries import PERIOD_SECONDS, BotQueries
from src.core.config import settings
from src.core.instruments import horizon_label
from src.core.redis_client import get_redis
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
            "allowed_updates": '["message"]',
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
        """Проверяет доступ и, если можно, отвечает на команду."""
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
        answer = await self._route(text)
        for chunk in handlers.split_message(answer):
            await send_message(chunk, chat_id=str(chat_id))

    async def _route(self, text: str) -> str:
        """Маршрутизация команды → готовый текст ответа."""
        cmd, args = handlers.parse_command(text)
        now = datetime.now(UTC)

        if cmd in ("start", "help"):
            return handlers.render_help()
        if cmd == "status":
            return await self._cmd_status(now)
        if cmd == "last":
            return await self._cmd_last(args, now)
        if cmd == "signal":
            return await self._cmd_signal(args, now)
        if cmd == "agents":
            return await self._cmd_agents(now)
        if cmd == "stats":
            return await self._cmd_stats(args, now)
        if cmd == "summary":
            return await self._cmd_summary(now)
        return handlers.render_unknown()

    async def _read_heartbeats(self) -> list[tuple[str, str | None, int]]:
        """Читает heartbeat-ключи из Redis → строки для рендера status/summary."""
        rows: list[tuple[str, str | None, int]] = []
        for key, env_attr in HEARTBEAT_KEYS:
            value = await self.redis.get(key)
            interval = int(getattr(settings, env_attr))
            rows.append((key, value, interval))
        return rows

    async def _cmd_status(self, now: datetime) -> str:
        hb_rows = await self._read_heartbeats()
        facts = await self.queries.status_facts()
        return handlers.render_status(hb_rows, facts, now)

    async def _cmd_last(self, args: list[str], now: datetime) -> str:
        notified_only, n = handlers.parse_last_args(args, self.max_rows)
        signals = await self.queries.last_signals(notified_only, n)
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

    async def _cmd_stats(self, args: list[str], now: datetime) -> str:
        period = handlers.parse_stats_period(args)
        period_sec = PERIOD_SECONDS[period]
        # §D.4: считаем ровно по одной версии логики (по умолчанию — последней),
        # смешивать «до» и «после» правок Этапа 7.0 нельзя.
        versions = await self.queries.stats_versions(period_sec)
        target_version = max(versions) if versions else None
        mixed = len(versions) > 1
        block1 = await self.queries.stats_block(period_sec, True, target_version)
        block2 = await self.queries.stats_block(period_sec, False, target_version)
        block5 = await self.queries.notify_filter_counts(period_sec, target_version)
        return handlers.render_stats(
            block1, block2, block5, period, now, target_version, mixed
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
