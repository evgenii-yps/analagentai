"""Отправка и правка сообщений в Telegram через Bot API (httpx, async)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import structlog

from src.core.config import settings

_log = structlog.get_logger().bind(component="telegram")

# Таймаут запроса к Telegram (секунды).
_TIMEOUT = 10.0

# Антидребезг предупреждения о 403 (человек не нажал Start / заблокировал бота).
_FORBIDDEN_KEY_PREFIX = "tg:forbidden:"
_FORBIDDEN_TTL = 3600

# Ответ Bot API на правку сообщения без изменений: это не ошибка, а «нечего менять».
_NOT_MODIFIED = "message is not modified"


def _verify() -> str | bool:
    """CA для проверки TLS.

    Если задан стандартный ``SSL_CERT_FILE`` (среда с корпоративным/egress-прокси
    и собственным CA) — используем его. Иначе httpx берёт встроенный certifi
    (поведение в обычной среде не меняется).
    """
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"


def _scrub(text: object) -> str:
    """Убирает токен бота из текста: токен зашит в URL запроса и в журнал не попадает."""
    out = str(text)
    token = settings.TELEGRAM_BOT_TOKEN
    return out.replace(token, "***") if token else out


async def _forbidden_log_due(chat_id: str) -> bool:
    """Можно ли писать предупреждение о 403 по этому чату (не чаще раза в час).

    Ключ ``tg:forbidden:<chat_id>`` с TTL 3600 ставится атомарно (``SET NX``). Redis
    недоступен — пишем каждый раз: лишняя строка в журнале лучше молчания.
    """
    try:
        from src.core.redis_client import get_redis

        return bool(
            await asyncio.wait_for(
                get_redis().set(f"{_FORBIDDEN_KEY_PREFIX}{chat_id}", "1", nx=True,
                                ex=_FORBIDDEN_TTL),
                timeout=2.0,
            )
        )
    except Exception:  # noqa: BLE001 — антидребезг не важнее самого предупреждения
        return True


async def _send_one(
    text: str,
    target_chat: str,
    reply_markup: dict[str, Any] | None,
    disable_notification: bool,
) -> int | None:
    """Одна отправка в один чат. Возвращает ``message_id`` или ``None``; не бросает."""
    if not settings.TELEGRAM_BOT_TOKEN or not target_chat:
        _log.warning("Telegram не настроен (нет токена/chat_id) — пропуск отправки")
        return None

    payload: dict[str, Any] = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    # Кнопки (меню настроек, экраны бота). Поле добавляется только когда клавиатура
    # есть: у обычных уведомлений её быть не должно.
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if disable_notification:
        payload["disable_notification"] = True
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_verify()) as client:
            response = await client.post(_api_url("sendMessage"), json=payload)
        if response.status_code == 200:
            try:
                message_id = response.json()["result"]["message_id"]
            except Exception:  # noqa: BLE001 — тело нестандартное: сообщение всё же ушло
                return 1
            return int(message_id) if message_id else 1
        # 403: человек не нажал Start или заблокировал бота — повторяется на каждом
        # сообщении, поэтому предупреждение по одному чату не чаще раза в час.
        if response.status_code != 403 or await _forbidden_log_due(target_chat):
            _log.warning(
                "telegram_send_failed=1",
                chat_id=target_chat,
                status=response.status_code,
                body=_scrub(response.text[:200]),
            )
        return None
    except Exception as exc:
        _log.warning("telegram_send_failed=1", chat_id=target_chat, error=_scrub(exc))
        return None


async def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict[str, Any] | None = None,
    disable_notification: bool = False,
) -> int | None:
    """Отправляет HTML-сообщение. Возвращает ``message_id`` при успехе, иначе ``None``.

    ``chat_id`` задан — ровно один получатель (ответы бота адресуют свой чат). Не задан —
    рассылка КАЖДОМУ из ``settings.telegram_recipients`` по очереди (владелец и все из
    ``BOT_ALLOWED_CHAT_IDS``); так все вызовы без ``chat_id`` (агенты, выгрузка, позиции,
    сводки, тревоги demo) доходят до всех без правки их кода. Достаточно наличия токена —
    chat_id может прийти аргументом, поэтому проверяем именно токен, а не
    ``telegram_configured``.

    Сбой одного получателя не мешает остальным: он пишется в журнал
    (``telegram_send_failed=1 chat_id=… status=…``), токен в журнал не попадает.
    Возвращается ``message_id`` первой удачной отправки; ``None`` — не дошло никому.

    ``disable_notification=True`` — сообщение приходит без звука (тихие часы).

    Результат пригоден для проверки «отправилось ли»: идентификатор сообщения —
    всегда положительное число, то есть истинное значение; ``None`` — ложное.

    Любые ошибки сети/Telegram ловятся и логируются — функция НЕ бросает
    исключений и возвращает ``None``.
    """
    if chat_id is not None:
        return await _send_one(text, chat_id, reply_markup, disable_notification)
    recipients = settings.telegram_recipients
    if not recipients:
        # Нет ни одного получателя — прежнее поведение: предупреждение и ``None``.
        return await _send_one(text, "", reply_markup, disable_notification)
    first: int | None = None
    for chat in recipients:
        message_id = await _send_one(text, chat, reply_markup, disable_notification)
        if first is None and message_id:
            first = message_id
    return first


async def edit_message(
    chat_id: str | int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> str:
    """Правит текст и кнопки сообщения на месте.

    Возвращает ``"ok"`` (изменено), ``"not_modified"`` (текст и кнопки те же — Telegram
    отвечает 400, но это не сбой) или ``"failed"`` (всё остальное: сообщение удалено,
    сеть, лимиты). Исключений не бросает.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        return "failed"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_verify()) as client:
            response = await client.post(_api_url("editMessageText"), json=payload)
        if response.status_code == 200:
            return "ok"
        body = response.text[:300]
        if response.status_code == 400 and _NOT_MODIFIED in body.lower():
            return "not_modified"
        _log.warning("Telegram не принял правку", status=response.status_code, body=body[:200])
        return "failed"
    except Exception as exc:
        _log.warning("Ошибка правки сообщения в Telegram", error=_scrub(exc))
        return "failed"


async def set_my_commands(commands: list[tuple[str, str]]) -> bool:
    """Задаёт меню команд бота (кнопка «Меню» у поля ввода). ``True`` — Telegram принял."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return False
    payload = {
        "commands": [{"command": name, "description": text} for name, text in commands],
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_verify()) as client:
            response = await client.post(_api_url("setMyCommands"), json=payload)
        if response.status_code == 200 and response.json().get("ok"):
            return True
        _log.warning(
            "Telegram не принял меню команд",
            status=response.status_code,
            body=response.text[:200],
        )
        return False
    except Exception as exc:
        _log.warning("Ошибка установки меню команд", error=_scrub(exc))
        return False
