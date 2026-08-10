"""Клиент приёмника Google Apps Script (§8.3).

Отправляет пачку строк на веб-приложение таблицы. Apps Script отвечает
редиректом 302 на ``script.googleusercontent.com`` — редирект обязательно
следовать (httpx делает это по умолчанию). Успех — только HTTP 200 и
``{"ok": true}`` в теле.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

_log = structlog.get_logger().bind(component="export.sheets")

# Таймаут запроса и паузы между повторами (секунды) — из §8.3.
_TIMEOUT = 60.0
_RETRY_DELAYS = (5.0, 15.0, 45.0)


@dataclass
class SheetsResult:
    """Итог отправки пачки в таблицу."""

    ok: bool
    inserted: int = 0
    error: str | None = None


def _verify() -> str | bool:
    """CA для TLS: кастомный ``SSL_CERT_FILE`` при наличии, иначе встроенный."""
    ca_file = os.environ.get("SSL_CERT_FILE")
    return ca_file if ca_file else True


async def post_rows(
    url: str,
    secret: str,
    sheet: str,
    mode: str,
    rows: list[list[Any]],
    header: list[str] | None = None,
) -> SheetsResult:
    """POST пачки строк на Apps Script с таймаутом и повторами (5/15/45 сек).

    Возвращает :class:`SheetsResult`. Исключений не бросает — сеть/HTTP/парсинг
    ошибки сворачиваются в ``ok=False`` с текстом причины.
    """
    payload: dict[str, Any] = {
        "secret": secret,
        "sheet": sheet,
        "mode": mode,
        "rows": rows,
    }
    if header is not None:
        payload["header"] = header

    last_error = "неизвестная ошибка"
    for attempt, delay in enumerate((0.0, *_RETRY_DELAYS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, verify=_verify(), follow_redirects=True
            ) as client:
                response = await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 — сеть не должна ронять скрипт
            last_error = f"сетевая ошибка: {exc}"
            _log.warning("Sheets: попытка не удалась", attempt=attempt + 1, error=last_error)
            continue

        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}"
            _log.warning("Sheets: неуспешный код", attempt=attempt + 1, status=response.status_code)
            continue

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = f"нераспознанный ответ: {exc}"
            _log.warning("Sheets: ответ не JSON", attempt=attempt + 1)
            continue

        if body.get("ok") is True:
            return SheetsResult(ok=True, inserted=int(body.get("inserted", 0)))
        last_error = f"ok=false: {body.get('error', 'без описания')}"
        _log.warning("Sheets: ответ ok=false", attempt=attempt + 1, error=last_error)

    return SheetsResult(ok=False, error=last_error)
