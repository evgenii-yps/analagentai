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
  3. Торговый журнал (Этап 9.1.2): позиции → лист «торговля тест апи окх чтение».
  4. Notion: закрытые сигналы с notified_at без отметки target='notion' → страницы.
  5. При любой ошибке — алерт в Telegram (бот вотчдога) и выход с кодом 1;
     невыгруженное уйдёт на следующем запуске (данные не теряются).

РЕЖИМ ``--positions-only`` (Этап 9.1.2 §8). Полная выгрузка идёт раз в сутки и
ПЕРЕСОБИРАЕТ СЛУЖЕБНЫЕ ЛИСТЫ ЦЕЛИКОМ (mode=replace): гонять её каждые пятнадцать
минут значило бы переписывать четыре листа ради пятого. Позиции же живут десятки
минут, и час ожидания для них — это половина сделки. Поэтому отдельный режим,
в котором обрабатывается ТОЛЬКО лист сделок:

    docker compose --profile tools run --rm --no-deps export \
        python -m src.export_main --positions-only

Он же стоит в deploy/agent-trade-positions.cron каждые 15 минут. Полная выгрузка
лист сделок тоже обрабатывает — на случай, если отдельный cron не установят.

Скрипт идемпотентен: повторный запуск не создаёт дублей ни в таблице, ни в Notion.
Код возврата 1 пробрасывается наружу через ``docker compose run`` — cron видит сбой.
"""

from __future__ import annotations

import argparse
import asyncio
import html
from typing import Any

import asyncpg
import structlog

from src.core.config import mask_secret, settings
from src.core.logging import setup_logging
from src.export import notion, queries, sheets
from src.export.transform import (
    CORRELATION_HEADER,
    INDEPENDENT_DISCLAIMER,
    INDEPENDENT_HEADER,
    MIXED_VERSIONS_DISCLAIMER,
    POSITION_CLOSE_START_COLUMN,
    POSITION_FORMULA_FROM_COLUMN,
    POSITION_NOTE_COLUMN,
    POSITION_TOTALS_MARKER,
    SIGNALS_HEADER,
    SUMMARY_HEADER,
    build_correlation_row,
    build_independent_row,
    build_notion_properties,
    build_position_close_note_tail,
    build_position_close_values,
    build_position_full_row,
    build_position_note,
    build_position_open_row,
    build_position_orphan_note,
    build_signal_row,
    build_summary_row,
    mean_correlation_by_token,
    position_marker,
)
from src.notify.telegram import send_message

# Пауза между запросами к Notion (лимит API ~3 запр/сек) — §6.6.
_NOTION_PAUSE = 0.35

# Имена листов Google Таблицы.
_SHEET_SIGNALS = "Сигналы"
_SHEET_SUMMARY = "Сводка по дням"
_SHEET_WINDOWS = "Независимые окна"
# Этап 8.1 §7: корреляция исходов между токенами — отдельным листом, чтобы её
# нельзя было не заметить.
_SHEET_CORRELATION = "Корреляция токенов"
# Этап 9.1.2: торговый журнал владельца. Лист живой и правится руками, поэтому
# выгрузка обращается с ним осторожнее, чем со своими служебными листами:
# заголовок не отправляет, лист не создаёт, формулы не переписывает.
_SHEET_TRADES = "торговля тест апи окх чтение"
# ТРЕБУЕМАЯ ВЕРСИЯ ПРИЁМНИКА для работы с торговым журналом (Этап 9.1.2 §15).
#
# ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ КОДОМ, А НЕ ИНСТРУКЦИЕЙ. Старый приёмник новых режимов
# НЕ ЗНАЕТ и не отвергает их — он обрабатывает их веткой по умолчанию, и оба
# исхода тихие: table_append уходит appendRow-ом в КОНЕЦ ЛИСТА (ниже блока
# «баланс / начало», вне таблицы и вне формул) с честным ok:true, а table_update
# не делает НИЧЕГО и возвращает inserted:0 — после чего клиент ставит отметку
# sheet_closed_at, и закрытие сделки теряется НАВСЕГДА: отметка необратима, и
# повторно позиция в выборку не попадёт. Инструкция «сначала обновите скрипт»
# защищает от этого только словами.
#
# СРАВНЕНИЕ — ТОЧНОЕ РАВЕНСТВО, а не «не меньше». Сравнение версий по частям —
# отдельный код с собственными краевыми случаями, который здесь не нужен и
# однажды ошибётся; «ровно эта строка» ошибиться не может.
#
# ЭТАП 9.1.2.2 ПОДНЯЛ ТРЕБОВАНИЕ ДО 9.1.2.2, и версия 9.1.2 теперь тоже
# отвергается. Это не формальность: 9.1.2 затирает заметку созданной строки
# содержимым строки выше и дозаписывает закрытие в ПЕРВУЮ строку с меткой, не
# замечая, что таких строк несколько. Оба исхода тихие, и оба уже случились на
# боевом листе 31.08.2026.
_TRADES_RECEIVER_VERSION = "9.1.2.2"


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


def _report_ambiguous(
    result: sheets.SheetsResult,
    rows: list[dict[str, Any]],
    log: structlog.types.WrappedLogger,
    *,
    stage: str,
) -> set[str]:
    """Печатает неоднозначные метки в журнал и возвращает их множество (§3 ТЗ).

    СТРОКА ЖУРНАЛА — УРОВНЯ ``error`` И С МАШИНОЧИТАЕМЫМИ КЛЮЧАМИ
    (``sheets_ambiguous_marker=1``, ``position_id``, ``rows``). Уровень выбран не
    по громкости, а по смыслу: сделка НЕ ЗАПИСАНА в лист и записана не будет,
    пока человек не разберёт строки руками. Ключ-признак стоит отдельным полем,
    чтобы такие случаи можно было посчитать по журналу одной командой, не
    разбирая текст сообщения.

    ``position_id`` берётся из пачки по метке, а не разбирается из её текста:
    формат метки — договор двух сторон провода, и второй разборщик того же
    формата однажды разошёлся бы с первым.

    Возвращает МНОЖЕСТВО МЕТОК, а не список позиций: вызывающему нужно ровно
    одно — исключить их из тех, кому ставится отметка экспорта.
    """
    if not result.ambiguous:
        return set()
    by_marker = {position_marker(row["id"]): row for row in rows}
    markers: set[str] = set()
    for item in result.ambiguous:
        marker = str(item.get("marker", ""))
        if not marker:
            continue
        markers.add(marker)
        row = by_marker.get(marker)
        log.error(
            "Торговый журнал: метка неоднозначна — не записано ничего",
            sheets_ambiguous_marker=1,
            position_id=None if row is None else int(row["id"]),
            marker=marker,
            rows=list(item.get("rows") or []),
            stage=stage,
        )
    return markers


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

    # Независимые окна: по одному наблюдению на ТОКЕН и ГОРИЗОНТ (§7 ТЗ 8.1),
    # ОДНОЙ версии логики (§9 ТЗ 8.2). Версия разрешается один раз на прогон:
    # оба листа обязаны быть собраны по одной и той же выборке, иначе
    # корреляция описывала бы не те наблюдения, что попали в лист окон.
    horizons = settings.eval_horizons_hours
    logic_version = await queries.resolve_logic_version(
        conn, settings.EXPORT_LOGIC_VERSION
    )
    log.info(
        "Версия логики в выгрузке",
        export_logic_version=settings.EXPORT_LOGIC_VERSION,
        resolved=logic_version if logic_version is not None else "all",
    )
    windows = await queries.fetch_independent_by_token_horizon(
        conn, horizons, logic_version
    )
    correlation = await queries.fetch_outcome_correlation(
        conn, horizons, logic_version
    )
    mean_corr = mean_correlation_by_token(correlation)
    window_rows = [
        build_independent_row(
            sig,
            mean_corr.get((int(sig.get("horizon_h") or 0), str(sig.get("token") or ""))),
        )
        for sig in windows
    ]
    # Оговорка §7 — ПЕРВОЙ строкой листа, до данных: она обязательна и не
    # зависит от того, посмотрит ли читатель отдельный лист корреляции.
    # При EXPORT_LOGIC_VERSION=all перед ней идёт оговорка о смешивании версий
    # (§9.3 ТЗ 8.2): о том, что лист несравним внутри себя, читатель обязан
    # узнать раньше всего остального.
    prefix: list[list[Any]] = [INDEPENDENT_DISCLAIMER]
    if logic_version is None:
        prefix.insert(0, MIXED_VERSIONS_DISCLAIMER)
    res = await sheets.post_rows(
        url, secret, _SHEET_WINDOWS, "replace",
        [*prefix, *window_rows], header=INDEPENDENT_HEADER,
    )
    if not res.ok:
        raise ExportError(f"лист «Независимые окна»: {res.error}")

    correlation_rows = [build_correlation_row(row) for row in correlation]
    correlation_payload: list[list[Any]] = (
        [MIXED_VERSIONS_DISCLAIMER, *correlation_rows]
        if logic_version is None
        else correlation_rows
    )
    res = await sheets.post_rows(
        url, secret, _SHEET_CORRELATION, "replace", correlation_payload,
        header=CORRELATION_HEADER,
    )
    if not res.ok:
        raise ExportError(f"лист «Корреляция токенов»: {res.error}")

    log.info(
        "Служебные листы пересобраны",
        summary_days=len(summary_rows),
        windows=len(window_rows),
        horizons_h=horizons,
        correlation_pairs=len(correlation_rows),
        logic_version=logic_version if logic_version is not None else "all",
    )
    return total


async def _export_trades(
    conn: asyncpg.Connection,
    log: structlog.types.WrappedLogger,
) -> tuple[int, int]:
    """Ведёт сделки строками в торговом журнале (Этап 9.1.2). ``(создано, дописано)``.

    ПОРЯДОК ВНУТРИ ОДНОГО ПРОГОНА ЖЁСТКИЙ: сначала ОТКРЫТИЯ, потом ЗАКРЫТИЯ.
    Позиция, открытая и закрытая между двумя прогонами, получает строку и
    дозапись в один проход — это гарантирует именно порядок, а не удача:
    выборка закрытий требует уже проставленной отметки открытия.

    ОТМЕТКА СТАВИТСЯ ТОЛЬКО ПОСЛЕ ``ok:true``. Ошибка отправки оставляет пачку
    в очереди целиком; повторная запись одной позиции невозможна по построению.
    """
    if not settings.SHEETS_TRADES_ENABLED:
        # ВЫКЛЮЧЕНО — НЕ ДЕЛАЕТСЯ НИЧЕГО: ни запроса к базе, ни обращения к
        # сети. Первая запись в чужой рабочий лист обязана произойти по
        # сознательному решению владельца, а не сама.
        log.info("Торговый журнал: выключен (SHEETS_TRADES_ENABLED=false)")
        return (0, 0)
    if not settings.SHEETS_WEBAPP_URL or not settings.SHEETS_SHARED_SECRET:
        raise ExportError("не заданы SHEETS_WEBAPP_URL / SHEETS_SHARED_SECRET")
    if not await queries.positions_table_exists(conn):
        # Этап 9.1 не развёрнут — выгружать нечего, и это не ошибка.
        log.info("Торговый журнал: таблицы positions нет — пропуск")
        return (0, 0)

    url = settings.SHEETS_WEBAPP_URL
    secret = settings.SHEETS_SHARED_SECRET
    tz = settings.NOTIFY_TIMEZONE
    batch = settings.EXPORT_BATCH_SIZE
    created = 0
    updated = 0

    # ПУСТАЯ ОЧЕРЕДЬ — В СЕТЬ НЕ ХОДИМ ВОВСЕ (§15.4). Задача cron идёт каждые
    # 15 минут, и в большинстве прогонов писать нечего: спрашивать у Google
    # версию сто раз в сутки ради ответа «нечего делать» — это трафик и записи
    # в журнале ради ничего.
    to_open, to_close = await queries.count_positions_pending(conn)
    if not to_open and not to_close:
        log.info("Торговый журнал: нечего выгружать")
        return (0, 0)

    # ВЕРСИЯ ПРИЁМНИКА СПРАШИВАЕТСЯ ДО ПЕРВОЙ ЗАПИСИ (§15.2). Режим "version"
    # безвреден и на СТАРОМ приёмнике тоже: тот не знает его, идёт общим путём,
    # не находит строк (rows отсутствует, ширина ноль) и возвращает свою версию,
    # ничего не записав. Имя листа при этом посылается настоящее — старый
    # приёмник создаёт лист, если его нет, и подсовывать ему выдуманное имя
    # значило бы завести в книге владельца пустой лишний лист.
    probe = await sheets.post_rows(url, secret, _SHEET_TRADES, "version", [])
    if not probe.ok or probe.receiver_version != _TRADES_RECEIVER_VERSION:
        got = probe.receiver_version or (
            "нет ответа" if not probe.ok else "поле version не возвращено"
        )
        log.error(
            "Торговый журнал: приёмник не той версии — не записано НИЧЕГО",
            receiver_version=got,
            required=_TRADES_RECEIVER_VERSION,
            to_open=to_open, to_close=to_close,
        )
        raise ExportError(
            f"торговый журнал: приёмник версии {got}, требуется "
            f"{_TRADES_RECEIVER_VERSION} — обновите скрипт в Google (§10 ТЗ "
            "9.1.2). Ни одной строки не записано, отметки не поставлены: "
            "старая версия записала бы открытия в конец листа, а закрытия "
            "потеряла бы молча"
        )

    # --- 1. ОТКРЫТИЯ: новые строки, столбцы A–G плюс заметка --------------
    pending_open = await queries.fetch_positions_pending_open(conn, batch)
    if pending_open:
        rows = [build_position_open_row(row, tz) for row in pending_open]
        notes = [build_position_note(row) for row in pending_open]
        result = await sheets.post_rows(
            url, secret, _SHEET_TRADES, "table_append", rows,
            notes=notes,
            note_column=POSITION_NOTE_COLUMN,
            totals_marker=POSITION_TOTALS_MARKER,
            formula_from_column=POSITION_FORMULA_FROM_COLUMN,
        )
        if not result.ok:
            raise ExportError(f"торговый журнал, открытия: {result.error}")

        # МЕТКА УЖЕ ЗАНЯТА — СТРОКА НЕ СОЗДАНА, И ОТМЕТКА НЕ СТАВИТСЯ (§3 ТЗ
        # 9.1.2.2). Отметка необратима: поставив её здесь, мы объявили бы
        # позицию выгруженной, тогда как строки в листе у неё нет — а есть
        # чужая строка с её меткой. Позиция останется в очереди и будет
        # выгружаться каждым следующим прогоном, пока лист не приведут в
        # порядок. Повторяющаяся ошибка в журнале — это цена, которую платит
        # владелец за то, чтобы сделка не потерялась молча.
        skipped = _report_ambiguous(result, pending_open, log, stage="открытие")
        opened_ids = [
            int(row["id"]) for row in pending_open
            if position_marker(row["id"]) not in skipped
        ]
        await queries.mark_positions_sheet_opened(conn, opened_ids)
        created = len(opened_ids)
        log.info(
            "Торговый журнал: строки открытия созданы",
            created=created, start_row=result.start_row,
            receiver_version=result.receiver_version,
            skipped_ambiguous=len(skipped),
        )

    # --- 2. ЗАКРЫТИЯ: дозапись H, I, J в СУЩЕСТВУЮЩУЮ строку по метке ------
    pending_close = await queries.fetch_positions_pending_close(conn, batch)
    if pending_close:
        by_marker = {
            position_marker(row["id"]): row for row in pending_close
        }
        updates = [
            {
                "marker": position_marker(row["id"]),
                "startColumn": POSITION_CLOSE_START_COLUMN,
                "values": build_position_close_values(row, tz),
                # ТОЛЬКО ХВОСТ (§16 ТЗ): приёмник допишет его к тому, что в
                # ячейке уже есть. Прислать заметку целиком значило бы стереть
                # текст, который владелец занёс туда руками, пока сделка шла.
                "noteAppend": build_position_close_note_tail(row),
            }
            for row in pending_close
        ]
        result = await sheets.post_rows(
            url, secret, _SHEET_TRADES, "table_update", [],
            note_column=POSITION_NOTE_COLUMN,
            updates=updates,
        )
        if not result.ok:
            raise ExportError(f"торговый журнал, закрытия: {result.error}")

        # НЕОДНОЗНАЧНАЯ МЕТКА: НИ ОТМЕТКИ, НИ НОВОЙ СТРОКИ (§3 ТЗ 9.1.2.2).
        # Приёмник по такой метке не записал НИЧЕГО, и создавать взамен новую
        # строку нельзя: строк с этой меткой в листе и без того больше одной.
        ambiguous_markers = _report_ambiguous(
            result, pending_close, log, stage="закрытие"
        )
        done_ids = [
            int(row["id"]) for marker, row in by_marker.items()
            if marker not in set(result.not_found)
            and marker not in ambiguous_markers
        ]
        updated = len(done_ids)

        # СТРОКУ ОТКРЫТИЯ НЕ НАШЛИ — сделка не теряется. Приёмник не угадывает
        # строку (дописать выход не в ту строку хуже, чем не дописать вовсе), а
        # клиент кладёт такую сделку отдельной ПОЛНОЙ строкой A–J с заметкой,
        # начинающейся прямыми словами о случившемся. Потерянная сделка хуже
        # лишней строки.
        if result.not_found:
            orphans = [by_marker[marker] for marker in result.not_found
                       if marker in by_marker]
            log.warning(
                "Торговый журнал: строка открытия не найдена",
                markers=list(result.not_found), count=len(orphans),
            )
            if orphans:
                rescue = await sheets.post_rows(
                    url, secret, _SHEET_TRADES, "table_append",
                    [build_position_full_row(row, tz) for row in orphans],
                    notes=[build_position_orphan_note(row, tz)
                           for row in orphans],
                    note_column=POSITION_NOTE_COLUMN,
                    totals_marker=POSITION_TOTALS_MARKER,
                    formula_from_column=POSITION_FORMULA_FROM_COLUMN,
                )
                if not rescue.ok:
                    raise ExportError(
                        f"торговый журнал, потерянные строки: {rescue.error}"
                    )
                # Спасательная строка тоже может упереться в занятую метку —
                # тогда она не создана, и отметка по ней не ставится.
                rescue_skipped = _report_ambiguous(
                    rescue, orphans, log, stage="потерянная строка"
                )
                saved = [
                    row for row in orphans
                    if position_marker(row["id"]) not in rescue_skipped
                ]
                done_ids.extend(int(row["id"]) for row in saved)
                created += len(saved)
                updated += len(saved)

        await queries.mark_positions_sheet_closed(conn, done_ids)
        log.info("Торговый журнал: закрытия дописаны", updated=updated)

    return (created, updated)


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


async def _run(positions_only: bool = False) -> int:
    """Основной сценарий. Возвращает код выхода (0 — успех, 1 — были ошибки)."""
    setup_logging()
    log = structlog.get_logger().bind(component="export")

    if not settings.EXPORT_ENABLED:
        log.info("Выгрузка отключена (EXPORT_ENABLED=false) — выход")
        return 0

    log.info(
        "Старт выгрузки сигналов" if not positions_only
        else "Старт выгрузки торгового журнала (--positions-only)",
        sheets_url=mask_secret(settings.SHEETS_WEBAPP_URL),
        notion_token=mask_secret(settings.NOTION_API_TOKEN),
        batch_size=settings.EXPORT_BATCH_SIZE,
        positions_only=positions_only,
        trades_enabled=settings.SHEETS_TRADES_ENABLED,
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

        # ТРИ ЦЕЛИ НЕЗАВИСИМЫ: сбой одной не блокирует остальные. Сломанный
        # приёмник торгового журнала не должен останавливать выгрузку сигналов,
        # и наоборот.
        if not positions_only:
            try:
                sheets_n = await _export_sheets(conn, log)
                log.info("Sheets: готово", exported=sheets_n)
            except ExportError as exc:
                errors.append(str(exc))
                log.error("Ошибка выгрузки в Sheets", error=str(exc))

        try:
            trades_created, trades_updated = await _export_trades(conn, log)
            log.info(
                "Торговый журнал: готово",
                created=trades_created, updated=trades_updated,
            )
        except ExportError as exc:
            errors.append(str(exc))
            log.error("Ошибка выгрузки торгового журнала", error=str(exc))

        if not positions_only:
            try:
                notion_n = await _export_notion(conn, log)
                log.info("Notion: готово", exported=notion_n)
            except ExportError as exc:
                errors.append(str(exc))
                log.error("Ошибка выгрузки в Notion", error=str(exc))

        if errors:
            to_open, to_close = await queries.count_positions_pending(conn)
            summary = " | ".join(errors)
            if positions_only:
                await _alert(
                    f"{summary}\nНе записано в торговый журнал: "
                    f"открытий={to_open}, закрытий={to_close}. "
                    f"Повтор на следующем запуске.",
                    log,
                )
                return 1
            left_sheets = await queries.count_unexported(conn, "sheets")
            left_notion = await queries.count_unexported(conn, "notion")
            await _alert(
                f"{summary}\nНе выгружено: Sheets={left_sheets}, "
                f"Notion={left_notion}, торговый журнал: открытий={to_open}, "
                f"закрытий={to_close}. Повтор на следующем запуске.",
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
    parser = argparse.ArgumentParser(
        description=(
            "Выгрузка сигналов в Google Таблицу и Notion (Этап 6.6) и ведение "
            "сделок в торговом журнале (Этап 9.1.2). Без аргументов — полная "
            "выгрузка."
        )
    )
    parser.add_argument(
        "--positions-only",
        action="store_true",
        help=(
            "обработать ТОЛЬКО лист сделок и выйти. Служебные листы "
            "пересобираются целиком и потому идут раз в сутки; позиции живут "
            "десятки минут и потому идут раз в 15 минут."
        ),
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(positions_only=args.positions_only)))


if __name__ == "__main__":
    main()
