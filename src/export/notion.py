"""Клиент REST API Notion (§9): создание страниц в «Журнале сигналов».

Версия API 2022-06-28 зафиксирована ТЗ — в новых версиях изменились parent и
схема свойств. Опции select/multi_select не создаём: они уже есть в базе.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

_log = structlog.get_logger().bind(component="export.notion")

_API_URL = "https://api.notion.com/v1/pages"
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 30.0


@dataclass
class NotionResult:
    """Итог создания одной страницы."""

    ok: bool
    error: str | None = None


def _verify() -> str | bool:
    """CA для TLS: кастомный ``SSL_CERT_FILE`` при наличии, иначе встроенный."""
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


async def create_page(
    token: str,
    database_id: str,
    properties: dict[str, Any],
) -> NotionResult:
    """Создаёт страницу в базе. Исключений не бросает — ошибку возвращает в результате."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=_verify()) as client:
            response = await client.post(_API_URL, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001 — сеть не должна ронять скрипт
        return NotionResult(ok=False, error=f"сетевая ошибка: {exc}")

    if response.status_code == 200:
        return NotionResult(ok=True)

    # Тело ответа содержит человекочитаемое описание ошибки Notion.
    detail = response.text[:300]
    _log.warning("Notion: ошибка создания страницы", status=response.status_code, body=detail)
    return NotionResult(ok=False, error=f"HTTP {response.status_code}: {detail}")
