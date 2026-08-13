"""Отправка сообщений в Telegram через Bot API (httpx, async)."""

from __future__ import annotations

import os

import httpx
import structlog

from src.core.config import settings

_log = structlog.get_logger().bind(component="telegram")

# Таймаут запроса к Telegram (секунды).
_TIMEOUT = 10.0


def _verify() -> str | bool:
    """CA для проверки TLS.

    Если задан стандартный ``SSL_CERT_FILE`` (среда с корпоративным/egress-прокси
    и собственным CA) — используем его. Иначе httpx берёт встроенный certifi
    (поведение в обычной среде не меняется).
    """
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


async def send_message(text: str, chat_id: str | None = None) -> bool:
    """Отправляет HTML-сообщение в чат. Возвращает True при успехе.

    ``chat_id`` необязателен: по умолчанию берётся ``TELEGRAM_CHAT_ID`` из конфига
    (поведение прежних вызовов не меняется). Бот-ответы адресуют конкретный чат,
    передавая chat_id явно. Достаточно наличия токена — chat_id может прийти
    аргументом, поэтому проверяем именно токен, а не ``telegram_configured``.

    Любые ошибки сети/Telegram ловятся и логируются — функция НЕ бросает
    исключений и возвращает False.
    """
    target_chat = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID
    if not settings.TELEGRAM_BOT_TOKEN or not target_chat:
        _log.warning("Telegram не настроен (нет токена/chat_id) — пропуск отправки")
        return False

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_verify()) as client:
            response = await client.post(url, json=payload)
        if response.status_code == 200:
            return True
        _log.warning(
            "Telegram вернул ошибку",
            status=response.status_code,
            body=response.text[:200],
        )
        return False
    except Exception as exc:
        _log.warning("Ошибка отправки в Telegram", error=str(exc))
        return False
