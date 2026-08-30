"""Клиент приёмника Google Apps Script (§8.3).

Отправляет пачку строк на веб-приложение таблицы. Apps Script отвечает
редиректом 302 на ``script.googleusercontent.com`` — редирект обязательно
следовать (httpx делает это по умолчанию). Успех — только HTTP 200 и
``{"ok": true}`` в теле.

ЭТАП 8.4.1. Перед отправкой пачка приводится к ОДНОЙ ширине — самой широкой
строке набора с учётом заголовка (:func:`normalize_batch`). Приёмник считает
ширину диапазона сам, но живёт на стороне Google и обновляется вручную,
отдельно от образа: пока развёрнута старая версия, она берёт ширину из ПЕРВОЙ
строки пачки. На листе «Независимые окна» первой идёт оговорка из одного
элемента, и запись пятнадцати колонок обрывалась ошибкой «In den Daten sind es
15, im Bereich jedoch 1». Выравнивание здесь снимает отказ независимо от того,
обновлён приёмник или ещё нет, и не зависит от числа колонок: ширина считается
по набору, а не задаётся константой.
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
    # Версия развёрнутого приёмника из его ответа (Этап 8.4.1). None означает
    # «приёмник версию не сообщил» — то есть развёрнута версия до 8.4.1.
    receiver_version: str | None = None


def normalize_batch(
    rows: list[list[Any]],
    header: list[str] | None = None,
) -> tuple[list[list[Any]], list[Any] | None]:
    """Приводит строки пачки к одной ширине — самой широкой в наборе.

    Ширина считается ПО НАБОРУ (заголовок участвует наравне со строками), а не
    берётся у первой строки и не задаётся константой: набор строк разной длины
    — штатный случай, и следующее изменение состава колонок не должно ломать
    запись. Короткие строки дополняются пустыми ячейками; пустая ячейка в
    таблице выглядит пустой, тогда как ноль выглядел бы значением.

    Порядок строк не меняется: оговорка листа «Независимые окна» остаётся
    первой строкой.
    """
    width = len(header) if header else 0
    for row in rows:
        width = max(width, len(row))
    if width == 0:
        return [list(row) for row in rows], header
    padded = [list(row) + [""] * (width - len(row)) for row in rows]
    padded_header = (
        None if header is None else list(header) + [""] * (width - len(header))
    )
    return padded, padded_header


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
    # Выравнивание ширины ДО отправки (Этап 8.4.1) — см. модульный докстринг.
    rows, header = normalize_batch(rows, header)
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
            version = body.get("version")
            # Версию печатаем всегда: по ней владелец видит в журнале, что
            # развёрнутая на стороне Google версия приёмника действительно
            # обновилась. Её отсутствие — признак версии до 8.4.1.
            _log.info(
                "Sheets: пачка принята",
                sheet=sheet,
                rows=len(rows),
                receiver_version=version or "до 8.4.1 (версию не сообщает)",
            )
            return SheetsResult(
                ok=True,
                inserted=int(body.get("inserted", 0)),
                receiver_version=None if version is None else str(version),
            )
        last_error = f"ok=false: {body.get('error', 'без описания')}"
        _log.warning("Sheets: ответ ok=false", attempt=attempt + 1, error=last_error)

    return SheetsResult(ok=False, error=last_error)


async def post_position_row(
    url: str,
    secret: str,
    sheet: str,
    values: dict[str, Any],
    headers: dict[str, str],
) -> SheetsResult:
    """Одна строка закрытой позиции в лист владельца (Этап 9.1.1 §7.5).

    ОТДЕЛЬНОГО КАНАЛА НЕ ЗАВОДИТСЯ. Действие ``append_position`` живёт в том же
    приёмнике ``deploy/apps_script.gs`` рядом с выгрузкой Этапа 6.6, и проверка
    общего секрета — та же самая: второй секрет пришлось бы хранить, менять и
    однажды забыть поменять в одном из двух мест.

    ``values`` — объект «буква столбца → значение», ровно восемь ключей
    (:data:`src.positions.sheet.SHEET_COLUMNS`). ``headers`` — ожидаемые
    заголовки строки 1 в тех же столбцах; приёмник сверяет их с листом и при
    расхождении ОТКАЗЫВАЕТСЯ писать. Владелец правит лист руками, и молчаливая
    запись в переименованные столбцы — это порча данных.

    Повторы и таймаут — те же, что у :func:`post_rows` (5/15/45 секунд).
    Исключений не бросает: сеть, HTTP и разбор ответа сворачиваются в
    ``ok=False`` с текстом причины. ЭТО ВАЖНО ИМЕННО ЗДЕСЬ: отметка
    ``sheet_exported_at`` ставится ТОЛЬКО после ``ok=True``, и упавший запрос
    обязан оставить позицию в очереди, а не уронить сервис.
    """
    payload: dict[str, Any] = {
        "secret": secret,
        "action": "append_position",
        "sheet": sheet,
        "values": values,
        "headers": headers,
    }

    last_error = "неизвестная ошибка"
    for attempt, delay in enumerate((0.0, *_RETRY_DELAYS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, verify=_verify(), follow_redirects=True
            ) as client:
                response = await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 — сеть не должна ронять сервис
            last_error = f"сетевая ошибка: {exc}"
            _log.warning("Sheets: позиция не записана", attempt=attempt + 1,
                         error=last_error)
            continue

        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}"
            _log.warning("Sheets: неуспешный код", attempt=attempt + 1,
                         status=response.status_code)
            continue

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = f"нераспознанный ответ: {exc}"
            _log.warning("Sheets: ответ не JSON", attempt=attempt + 1)
            continue

        if body.get("ok") is True:
            version = body.get("version")
            _log.info(
                "Sheets: строка позиции записана",
                sheet=sheet,
                row=body.get("row"),
                receiver_version=version or "до 9.1.1 (версию не сообщает)",
            )
            return SheetsResult(
                ok=True,
                inserted=1,
                receiver_version=None if version is None else str(version),
            )

        # ОТКАЗ ПРИЁМНИКА НЕ ПОВТОРЯЕТСЯ. Он означает, что лист устроен не так,
        # как ждёт код: имя листа не найдено, заголовки переименованы, формулы
        # не протянулись. Повтор через пять секунд не изменит лист — он лишь
        # трижды напишет в журнал одно и то же и отложит настоящий разбор.
        last_error = f"ok=false: {body.get('error', 'без описания')}"
        _log.warning("Sheets: приёмник отказал", error=last_error)
        return SheetsResult(ok=False, error=last_error)

    return SheetsResult(ok=False, error=last_error)
