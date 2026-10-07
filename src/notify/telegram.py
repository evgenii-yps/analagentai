"""Отправка и правка сообщений в Telegram через Bot API (httpx, async)."""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from src.core.config import settings

_log = structlog.get_logger().bind(component="telegram")

# Таймаут запроса к Telegram (секунды).
_TIMEOUT = 10.0

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


async def send_message(
    text: str,
    chat_id: str | None = None,
    reply_markup: dict[str, Any] | None = None,
    disable_notification: bool = False,
) -> int | None:
    """Отправляет HTML-сообщение в чат. Возвращает ``message_id`` при успехе, иначе ``None``.

    ``chat_id`` необязателен: по умолчанию берётся ``TELEGRAM_CHAT_ID`` из конфига
    (поведение прежних вызовов не меняется). Бот-ответы адресуют конкретный чат,
    передавая chat_id явно. Достаточно наличия токена — chat_id может прийти
    аргументом, поэтому проверяем именно токен, а не ``telegram_configured``.

    ``disable_notification=True`` — сообщение приходит без звука (тихие часы).

    Результат пригоден для проверки «отправилось ли»: идентификатор сообщения —
    всегда положительное число, то есть истинное значение; ``None`` — ложное.

    Любые ошибки сети/Telegram ловятся и логируются — функция НЕ бросает
    исключений и возвращает ``None``.
    """
    target_chat = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID
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
        _log.warning(
            "Telegram вернул ошибку",
            status=response.status_code,
            body=response.text[:200],
        )
        return None
    except Exception as exc:
        _log.warning("Ошибка отправки в Telegram", error=str(exc))
        return None


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
        _log.warning("Ошибка правки сообщения в Telegram", error=str(exc))
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
        _log.warning("Ошибка установки меню команд", error=str(exc))
        return False
