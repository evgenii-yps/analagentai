"""Получатели и отправка в Telegram для скриптов на ХОСТЕ (сторож, суточный отчёт здоровья).

Скрипты на хосте читают ``.env`` сами и не импортируют ``Settings``, поэтому правило списка
получателей (то же, что ``Settings.telegram_recipients``) повторено здесь, в одном месте для
обоих скриптов: ``TELEGRAM_CHAT_ID`` (владелец, всегда первый), затем ``BOT_ALLOWED_CHAT_IDS``
в порядке записи; пробелы срезаются, пустые и повторы выбрасываются.

Только стандартная библиотека.
"""

from __future__ import annotations

import os
import ssl
import urllib.parse
import urllib.request
from collections.abc import Mapping


def _clean(raw: str) -> str:
    """Значение из ``.env``: хвостовой комментарий (``KEY=  # пояснение``) и пробелы долой."""
    return raw.split("#", 1)[0].strip()


def recipients(env: Mapping[str, str]) -> list[str]:
    """Список получателей по словарю окружения (``.env`` + переменные процесса)."""
    out: list[str] = []
    parts = [env.get("TELEGRAM_CHAT_ID", ""), *env.get("BOT_ALLOWED_CHAT_IDS", "").split(",")]
    for raw in parts:
        chat = _clean(raw)
        if chat and chat not in out:
            out.append(chat)
    return out


def send_all(
    token: str, chats: list[str], text: str, timeout: int
) -> tuple[list[str], dict[str, str]]:
    """Шлёт ``text`` КАЖДОМУ из ``chats`` по очереди.

    Сбой одного не мешает остальным. Возвращает ``(дошло, {chat_id: причина сбоя})``;
    токен в причину не попадает. Тело запроса — то же, что слали скрипты до этапа 9.7.1.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ca_file = os.environ.get("SSL_CERT_FILE")
    delivered: list[str] = []
    failed: dict[str, str] = {}
    for chat in chats:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data)
            if ca_file:
                ctx = ssl.create_default_context(cafile=ca_file)
                urllib.request.urlopen(req, timeout=timeout, context=ctx).read()
            else:
                urllib.request.urlopen(req, timeout=timeout).read()
            delivered.append(chat)
        except Exception as exc:  # noqa: BLE001 — один получатель не должен ронять рассылку
            failed[chat] = str(exc).replace(token, "***")
    return delivered, failed
