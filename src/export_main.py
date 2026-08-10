"""Выгрузка закрытых сигналов в Google Таблицу и Notion (Этап 6.6 / 6.6.1).

Запускается ВНУТРИ контейнера (тот же образ, что и остальные сервисы), обычно из
cron на хосте командой:

    docker compose --profile tools run --rm --no-deps export

Почему в контейнере, а не на хосте: скрипту нужны сторонние пакеты (asyncpg,
httpx, structlog, pydantic) и сеть наружу — всё это есть в образе, но НЕ на
хосте (ТЗ 6.6.1, дефект D-3). БД внутри сети compose видна как ``postgres:5432``
(штатный ``settings.pg_dsn``), поэтому host-DSN и хак с sys.path больше не нужны.

Алгоритм (см. §6 ТЗ 6.6):
  1. Применить идемпотентные миграции §4.
  2. Sheets: закрытые сигналы без отметки target='sheets' → лист «Сигналы»
     (append) пачками; пересобрать «Сводка по дням» и «Независимые окна» (replace).
  3. Notion: закрытые сигналы с notified_at без отметки target='notion' → страницы.
  4. При любой ошибке — алерт в Telegram (бот вотчдога) и выход с кодом 1;
     невыгруженное уйдёт на следующем запуске (данные не теряются).

Скрипт идемпотентен: повторный запуск не создаёт дублей ни в таблице, ни в Notion.
Код возврата 1 пробрасывается наружу через ``docker compose run`` — cron видит сбой.
"""

from __future__ import annotations

import asyncio
import html

import asyncpg
import structlog

from src.core.config import mask_secret, settings
from src.core.logging import setup_logging
from src.export import notion, queries, sheets
from src.export.transform import (
    SIGNALS_HEADER,
    SUMMARY_HEADER,
    build_notion_properties,
    build_signal_row,
    build_summary_row,
)
from src.notify.telegram import send_message

# Пауза между запросами к Notion (лимит API ~3 запр/сек) — §6.6.
_NOTION_PAUSE = 0.35

# Имена листов Google Таблицы.
_SHEET_SIGNALS = "Сигналы"
_SHEET_SUMMARY = "Сводка по дням"
_SHEET_WINDOWS = "Независимые окна"


class ExportError(Exception):
    """Ошибка выгрузки в одну из целей (прерывает работу с этой целью)."""


async def _alert(text: str, log: structlog.types.WrappedLogger) -> None:
    """Шлёт алерт в Telegram тем же ботом/chat_id, что и вотчдог. Не бросает."""
    if not settings.telegram_configured:
        log.warning("Алерт не отправлен: Telegram не настроен", alert=text)
        return
    # Экранируем спецсимволы: send_message использует parse_mode=HTML.
    safe = html.escape(text)
    ok = await send_message(f"⚠️ <b>Выгрузка сигналов</b>\n{safe}")
    if not ok:
        log.warning("Не удалось отправить алерт в Telegram", alert=text)


async def _export_sheets(
    conn: asyncpg.Connection,
    log: structlog.types.WrappedLogger,
) -> int:
    """Выгружает лист «Сигналы» (append) пачками и пересобирает служебные листы.

    Возвращает число выгруженных сигналов. Бросает :class:`ExportError` при
    неуспешном ответе Apps Script (пачка считается невыгруженной, повтор позже).
    """
    if not settings.SHEETS_WEBAPP_URL or not settings.SHEETS_SHARED_SECRET:
        raise ExportError("не заданы SHEETS_WEBAPP_URL / SHEETS_SHARED_SECRET")

    url = settings.SHEETS_WEBAPP_URL
    secret = settings.SHEETS_SHARED_SECRET
    batch_size = settings.EXPORT_BATCH_SIZE
    total = 0

    while True:
        batch = await queries.fetch_unexported_for_sheets(conn, batch_size)
        if not batch:
            break
        rows = [build_signal_row(sig) for sig in batch]
        result = await sheets.post_rows(
            url, secret, _SHEET_SIGNALS, "append", rows, header=SIGNALS_HEADER
        )
        if not result.ok:
            raise ExportError(f"лист «Сигналы»: {result.error}")
        # Отметки ставим ТОЛЬКО после ok:true — иначе повторим пачку позже.
        await queries.mark_exported(conn, [sig["id"] for sig in batch], "sheets")
        total += len(batch)
        log.info("Пачка выгружена в Sheets", inserted=len(batch), total=total)

    # Служебные листы всегда пересобираются целиком (replace), в учёт не идут.
    summary = await queries.fetch_daily_summary(conn, settings.NOTIFY_MIN_PROBABILITY)
    summary_rows = [build_summary_row(r) for r in summary]
    res = await sheets.post_rows(
        url, secret, _SHEET_SUMMARY, "replace", summary_rows, header=SUMMARY_HEADER
    )
    if not res.ok:
        raise ExportError(f"лист «Сводка по дням»: {res.error}")

    windows = await queries.fetch_independent_windows(conn)
    window_rows = [build_signal_row(sig) for sig in windows]
    res = await sheets.post_rows(
        url, secret, _SHEET_WINDOWS, "replace", window_rows, header=SIGNALS_HEADER
    )
    if not res.ok:
        raise ExportError(f"лист «Независимые окна»: {res.error}")

    log.info(
        "Служебные листы пересобраны",
        summary_days=len(summary_rows),
        windows=len(window_rows),
    )
    return total


async def _export_notion(
    conn: asyncpg.Connection,
    log: structlog.types.WrappedLogger,
) -> int:
    """Создаёт страницы в «Журнале сигналов» Notion. Возвращает число созданных.

    Отметку в signal_exports ставим после КАЖДОЙ успешной страницы. Ошибки по
    отдельным сигналам собираются и в конце превращаются в :class:`ExportError`.
    """
    if not settings.NOTION_API_TOKEN:
        raise ExportError("не задан NOTION_API_TOKEN")

    pending = await queries.fetch_notion_pending(conn)
    if not pending:
        log.info("Notion: нечего выгружать")
        return 0

    token = settings.NOTION_API_TOKEN
    db_id = settings.NOTION_SIGNALS_DB_ID
    created = 0
    errors: list[str] = []

    for index, sig in enumerate(pending):
        if index:
            await asyncio.sleep(_NOTION_PAUSE)
        properties = build_notion_properties(sig, db_id)
        result = await notion.create_page(token, db_id, properties)
        if result.ok:
            await queries.mark_exported(conn, [sig["id"]], "notion")
            created += 1
        else:
            errors.append(f"signal_id={sig['id']}: {result.error}")
            log.warning("Notion: страница не создана", signal_id=sig["id"], error=result.error)

    log.info("Notion: выгрузка завершена", created=created, failed=len(errors))
    if errors:
        head = "; ".join(errors[:3])
        raise ExportError(f"Notion: {len(errors)} ошибок ({head})")
    return created


async def _run() -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — были ошибки)."""
    setup_logging()
    log = structlog.get_logger().bind(component="export")

    if not settings.EXPORT_ENABLED:
        log.info("Выгрузка отключена (EXPORT_ENABLED=false) — выход")
        return 0

    log.info(
        "Старт выгрузки сигналов",
        sheets_url=mask_secret(settings.SHEETS_WEBAPP_URL),
        notion_token=mask_secret(settings.NOTION_API_TOKEN),
        batch_size=settings.EXPORT_BATCH_SIZE,
    )

    # Подключение к БД (в сети compose — postgres:5432). Недоступность → алерт+выход.
    try:
        conn = await asyncpg.connect(dsn=settings.pg_dsn)
    except Exception as exc:  # noqa: BLE001
        log.error("БД недоступна", error=str(exc))
        await _alert(f"БД недоступна: {exc}", log)
        return 1

    errors: list[str] = []
    try:
        await queries.apply_migrations(conn)

        # Цель Sheets и цель Notion независимы: сбой одной не блокирует другую.
        try:
            sheets_n = await _export_sheets(conn, log)
            log.info("Sheets: готово", exported=sheets_n)
        except ExportError as exc:
            errors.append(str(exc))
            log.error("Ошибка выгрузки в Sheets", error=str(exc))

        try:
            notion_n = await _export_notion(conn, log)
            log.info("Notion: готово", exported=notion_n)
        except ExportError as exc:
            errors.append(str(exc))
            log.error("Ошибка выгрузки в Notion", error=str(exc))

        if errors:
            left_sheets = await queries.count_unexported(conn, "sheets")
            left_notion = await queries.count_unexported(conn, "notion")
            summary = " | ".join(errors)
            await _alert(
                f"{summary}\nНе выгружено: Sheets={left_sheets}, Notion={left_notion}. "
                f"Повтор на следующем запуске.",
                log,
            )
            return 1
    except Exception as exc:  # noqa: BLE001 — не роняем без алерта
        log.error("Непредвиденная ошибка выгрузки", error=str(exc))
        await _alert(f"непредвиденная ошибка: {exc}", log)
        return 1
    finally:
        await conn.close()

    log.info("Выгрузка завершена успешно")
    return 0


def main() -> None:
    """Точка входа: запускает сценарий и выставляет код возврата процесса.

    Код возврата пробрасывается через ``docker compose run`` наружу, поэтому
    cron видит 1 при ошибке выгрузки.
    """
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
