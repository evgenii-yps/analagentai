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
from dataclasses import dataclass, field
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
    # --- Этап 9.1.2, режимы торгового журнала ---
    # Сколько строк ДОСТРОЕНО (table_update), номер первой созданной строки
    # (table_append) — по нему видно, куда именно легла пачка.
    updated: int = 0
    start_row: int | None = None
    # Предупреждение приёмника при ok=true. Сегодня оно одно: формулы протянуть
    # было неоткуда. Это НЕ ошибка (строки записаны), но и не мелочь: строка без
    # формул выглядит записанной и не считается никак.
    warning: str | None = None
    # Метки, для которых строка в листе не нашлась. Приёмник не угадывает
    # строку, а возвращает метку сюда; что с ней делать, решает вызывающий.
    not_found: list[str] = field(default_factory=list)


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
    *,
    notes: list[str] | None = None,
    note_column: int | None = None,
    totals_marker: str | None = None,
    formula_from_column: int | None = None,
    updates: list[dict[str, Any]] | None = None,
) -> SheetsResult:
    """POST пачки строк на Apps Script с таймаутом и повторами (5/15/45 сек).

    Возвращает :class:`SheetsResult`. Исключений не бросает — сеть/HTTP/парсинг
    ошибки сворачиваются в ``ok=False`` с текстом причины.

    ЧЕТЫРЕ РЕЖИМА, И ДВА ИЗ НИХ ОСТОРОЖНЕЕ ОСТАЛЬНЫХ:

    * ``append`` и ``replace`` (Этап 6.6) — служебные листы выгрузки, целиком
      наши: их можно и создать, и переписать;
    * ``table_append`` и ``table_update`` (Этап 9.1.2) — торговый журнал, лист
      ВЛАДЕЛЬЦА с его формулами. Заголовок в него не отправляется: он там уже
      есть, и посылать свой значило бы переписать чужую строку 1.

    Пятый режим — ``version`` (Этап 9.1.2 §15): ничего не пишет и не несёт, а
    только спрашивает версию приёмника. Клиент обязан задать этот вопрос ПЕРЕД
    первой записью в торговый журнал: старый приёмник новых режимов не знает и
    обрабатывает их общим путём — молча и не туда.

    ``notes``/``note_column``/``totals_marker``/``formula_from_column`` — поля
    ``table_append``; ``updates`` — поле ``table_update``. Лишние поля в других
    режимах не отправляются вовсе: приёмник их проигнорировал бы, но запрос,
    несущий бессмысленные поля, вводит в заблуждение того, кто смотрит журнал.
    """
    payload: dict[str, Any] = {
        "secret": secret,
        "sheet": sheet,
        "mode": mode,
    }
    if mode == "version":
        # ВОПРОС О ВЕРСИИ НЕ НЕСЁТ НИЧЕГО (Этап 9.1.2 §15): ни строк, ни
        # заголовка. Он безвреден и на СТАРОМ приёмнике — тот не знает режима,
        # идёт общим путём, не находит строк и возвращает свою версию, ничего
        # не записав. Пустой список ``rows`` при этом не отправляется вовсе:
        # запрос, несущий бессмысленные поля, вводит в заблуждение того, кто
        # потом смотрит журнал.
        pass
    elif mode == "table_update":
        # Строк здесь нет вовсе: дозапись адресуется метками, а не порядком.
        payload["updates"] = updates or []
        if note_column is not None:
            payload["noteColumn"] = note_column
    else:
        # Выравнивание ширины ДО отправки (Этап 8.4.1) — см. модульный докстринг.
        rows, header = normalize_batch(rows, header)
        payload["rows"] = rows
        if header is not None:
            payload["header"] = header
        if notes is not None:
            payload["notes"] = notes
        if note_column is not None:
            payload["noteColumn"] = note_column
        if totals_marker is not None:
            payload["totalsMarker"] = totals_marker
        if formula_from_column is not None:
            payload["formulaFromColumn"] = formula_from_column

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
            warning = body.get("warning")
            not_found = [str(item) for item in (body.get("notFound") or [])]
            # Версию печатаем всегда: по ней владелец видит в журнале, что
            # развёрнутая на стороне Google версия приёмника действительно
            # обновилась. Её отсутствие — признак версии до 8.4.1.
            _log.info(
                "Sheets: пачка принята",
                sheet=sheet,
                mode=mode,
                rows=len(rows),
                updated=int(body.get("updated", 0)),
                receiver_version=version or "до 8.4.1 (версию не сообщает)",
            )
            if warning:
                # ПРЕДУПРЕЖДЕНИЕ ПРИ ok=true ПЕЧАТАЕТСЯ УРОВНЕМ warning, а не
                # тонет в info: сегодня оно означает «строки записаны, но
                # формулы протянуть было неоткуда», и такая строка выглядит
                # записанной, ничего при этом не считая.
                _log.warning(
                    "Sheets: приёмник предупреждает",
                    sheet=sheet, mode=mode, warning=str(warning),
                )
            return SheetsResult(
                ok=True,
                inserted=int(body.get("inserted", 0)),
                receiver_version=None if version is None else str(version),
                updated=int(body.get("updated", 0)),
                start_row=(
                    None if body.get("startRow") is None
                    else int(body["startRow"])
                ),
                warning=None if warning is None else str(warning),
                not_found=not_found,
            )
        last_error = f"ok=false: {body.get('error', 'без описания')}"
        _log.warning("Sheets: ответ ok=false", attempt=attempt + 1, error=last_error)

    return SheetsResult(ok=False, error=last_error)
