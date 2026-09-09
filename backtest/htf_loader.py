"""Загрузка старших рядов с биржи и потоковое чтение их из базы (Замер 0).

РАЗДЕЛЕНИЕ С ``backtest/htf.py`` СДЕЛАНО НАМЕРЕННО. Там — календарь, сборка и
проверки: ни базы, ни сети, и потому всё это проверяется тестами без стенда.
Здесь — только ввод-вывод. Смешать их значило бы сделать сборку проверяемой
лишь при живой базе, то есть почти не проверяемой.

РЕШЕНИЕ §6 ТЗ: СТАРШИЕ РЯДЫ ЗАГРУЖАЮТСЯ С БИРЖИ, А НЕ СОБИРАЮТСЯ ИЗ ЧАСОВЫХ.
Два довода. Первый: у биржи дневной ряд, скорее всего, глубже, чем позволяет
часовой (часовой упирается в 28.01.2022 по замеру 22.08.2026), а Замеру 1
глубина нужна прямо. Второй: в бою агенту проще брать готовый бар биржи — тогда
история и бой считают буквально одно и то же. Сборка из часовых сохраняется, но
как КОНТРОЛЬ (§7), а не как источник, и в таблицу не пишется никогда.

ДВА ЭНДПОИНТА, А НЕ ОДИН, И ЭТО ВАЖНО ДЛЯ §8. ``/market/history-candles`` не
отдаёт формирующийся бар вовсе, поэтому загрузка ТОЛЬКО через него дала бы
счётчик отброшенных незакрытых баров, равный нулю ПО ПОСТРОЕНИЮ — то есть
проверка выглядела бы успешной, ничего не проверяя. ``/market/candles`` отдаёт
текущий бар с ``confirm = 0``, и свежий край берётся оттуда: тогда ноль
отброшенных означает настоящий ноль, и §8 ТЗ прав, называя его подозрительным.

КУДА ПИШЕТСЯ. Только в ``backtest.candles`` — ту самую таблицу, что заведена
миграцией 008 и уже допускает указание масштаба колонкой ``bar``. Отдельная
таблица не заводится; разбор решения — в заголовке миграции 026.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from backtest import db
from backtest.htf import (
    SOURCE_EXCHANGE,
    HtfError,
    SourceBar,
    StoredBar,
    next_period_start,
    parse_htf_candles,
)
from backtest.loader import OkxHistory

_log = structlog.get_logger().bind(component="backtest.htf_loader")

# Свежий эндпоинт: отдаёт в том числе ФОРМИРУЮЩИЙСЯ бар с ``confirm = 0``.
PATH_CANDLES = "/api/v5/market/candles"

# Как часто печатать строку прогресса (в страницах), дефект D-10.
PROGRESS_EVERY_PAGES = 10

# Потолок страниц на один ряд: загрузка обязана завершаться даже при
# зациклившейся пагинации биржи.
MAX_PAGES = 3000

# Сколько строк за раз тянуть из курсора базы. Величина влияет на память
# ЛИНЕЙНО и держится маленькой намеренно: весь смысл потокового чтения в том,
# что в памяти не лежит ряд целиком.
CURSOR_PREFETCH = 512


class HtfHistory(OkxHistory):
    """Пагинация старших баров ТЕМ ЖЕ клиентом и с тем же разбором отказов.

    Наследование, а не вторая реализация: разбор кода 50011, удвоение паузы,
    печать тела ответа при не-200 — всё это уже написано и уже проверено. Своя
    копия здесь означала бы, что следующее изменение поведения биржи придётся
    чинить в двух местах, а починят в одном.

    ``candles_page`` родителя годится как есть: ``bar`` для него — просто
    строка, и ``1Dutc`` уходит в запрос без изменений. Добавляется ровно один
    метод — свежая страница с формирующимся баром.
    """

    async def recent_page(self, inst_id: str, bar: str, limit: int) -> list[list[str]]:
        """Свежая страница ``/market/candles`` — С НЕЗАКРЫТЫМ баром.

        Незакрытый бар отбрасывается разбором (:func:`parse_htf_candles`), а не
        здесь: чем позже он отброшен, тем виднее, что он вообще приходил.
        """
        return await self._get(
            PATH_CANDLES,
            {"instId": inst_id, "bar": bar, "limit": str(limit)},
        )


@dataclass
class HtfLoadResult:
    """Итог загрузки одного ряда. Все числа — фактические, ни одно не расчётное.

    ``rows_parsed`` и ``appended`` — РАЗНЫЕ числа, и путать их нельзя.
    Разобранных баров больше: свежая страница перекрывается с первой страницей
    истории, и один и тот же бар приходит дважды. ``appended`` — это РАЗНИЦА
    числа строк в таблице до и после, то есть сколько строк действительно
    прибавилось; ``ON CONFLICT DO NOTHING`` молча гасит повторы, и считать
    записанным всё отправленное значило бы завышать отчёт при каждом повторном
    прогоне.
    """

    inst_id: str
    bar: str
    already_in_db: int = 0
    appended: int = 0
    rows_parsed: int = 0
    dropped_unconfirmed: int = 0
    rows_seen: int = 0
    pages: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    chain_pairs_checked: int = 0
    notes: list[str] = field(default_factory=list)


async def _insert(rows: list[tuple[Any, ...]]) -> None:
    """Вставка старших баров. Идемпотентна: повторная загрузка ничего не меняет."""
    if not rows:
        return
    await db.pool().executemany(
        """
        INSERT INTO backtest.candles
            (inst_id, bar, open_time, close_time, open, high, low, close,
             volume, volume_ccy, source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        ON CONFLICT (inst_id, bar, open_time) DO NOTHING;
        """,
        rows,
    )


async def _bounds(inst_id: str, bar: str) -> tuple[datetime | None, datetime | None, int]:
    row = await db.fetchrow(
        "SELECT min(open_time) AS lo, max(open_time) AS hi, count(*) AS n "
        "FROM backtest.candles WHERE inst_id=$1 AND bar=$2;",
        inst_id, bar,
    )
    if row is None:
        return None, None, 0
    return row["lo"], row["hi"], int(row["n"] or 0)


async def backfill_htf(
    inst_id: str,
    bar: str,
    *,
    client: HtfHistory,
    page_limit: int,
    apply: bool,
) -> HtfLoadResult:
    """Догружает старший ряд НА ВСЮ ДОСТУПНУЮ ГЛУБИНУ и возвращает факты о загрузке.

    Границы периода здесь нет намеренно: §2.2 ТЗ требует «максимальной
    доступной глубины», а предсказание архитектора §14.1 — что прямой дневной
    ряд окажется ГЛУБЖЕ, чем позволяет часовой. Обрезав загрузку по
    ``BT_PERIOD_FROM``, этап отрезал бы ровно то, ради чего затеян.

    Проходов два, как у часового загрузчика: свежий край и недостающее начало.
    Пагинация OKX идёт только назад по времени, и одним проходом уже
    загруженный ряд никогда не пополнялся бы свежими барами.

    ``apply=False`` — ни одного запроса на запись в базу. Сеть при этом
    опрашивается: без неё нечего было бы показать, а прочитать биржу — не
    значит изменить базу.
    """
    result = HtfLoadResult(inst_id=inst_id, bar=bar)
    earliest, newest, existing = await _bounds(inst_id, bar)
    result.already_in_db = existing

    async def store(rows: list[tuple[Any, ...]]) -> None:
        result.rows_parsed += len(rows)
        if apply:
            await _insert(rows)

    # 1. Свежий край — через /market/candles, ВМЕСТЕ с формирующимся баром.
    #    Именно здесь счётчик отброшенных незакрытых баров становится не нулём
    #    по построению, а измерением.
    fresh = await client.recent_page(inst_id, bar, min(page_limit, 100))
    parsed = parse_htf_candles(fresh, inst_id, bar)
    result.rows_seen += parsed.seen
    result.dropped_unconfirmed += parsed.dropped_unconfirmed
    await store(parsed.rows)
    if parsed.dropped_unconfirmed == 0:
        result.notes.append(
            "биржа не вернула НИ ОДНОГО незакрытого бара на свежем крае — "
            "подозрительно, подлежит докладу (§8 ТЗ), а не считается успехом"
        )

    # 2. Глубина — через /market/history-candles, назад до пустой страницы.
    #    Проходов два, как у часового загрузчика: пагинация OKX идёт ТОЛЬКО
    #    назад по времени, и одним проходом от самой ранней точки уже
    #    загруженный ряд никогда не пополнялся бы свежими барами. Свежая
    #    страница из пункта 1 этого не решает: она покрывает сотню баров, а
    #    перерыв между прогонами может быть длиннее.
    async def walk(cursor: int | None, stop_at: datetime | None, phase: str) -> None:
        while result.pages < MAX_PAGES:
            page = await client.candles_page(inst_id, bar, cursor, page_limit)
            result.pages += 1
            if not page:
                _log.info("Старший ряд: пустая страница — история кончилась",
                          inst_id=inst_id, bar=bar, phase=phase, pages=result.pages)
                return
            parsed_page = parse_htf_candles(page, inst_id, bar)
            result.rows_seen += parsed_page.seen
            result.dropped_unconfirmed += parsed_page.dropped_unconfirmed
            await store(parsed_page.rows)
            oldest_ms = min(int(row[0]) for row in page)
            reached = datetime.fromtimestamp(oldest_ms / 1000, tz=UTC)
            if result.pages % PROGRESS_EVERY_PAGES == 0:
                print(f"    [{inst_id} {bar}] {phase}, страница {result.pages}: "
                      f"дошли до {reached.isoformat()}, разобрано "
                      f"{result.rows_parsed}", flush=True)
            if cursor is not None and oldest_ms >= cursor:
                result.notes.append(f"{phase}: пагинация не движется — обход прерван")
                return
            cursor = oldest_ms
            if stop_at is not None and reached <= stop_at:
                return
        result.notes.append(
            f"{phase}: обход прерван потолком в {MAX_PAGES} страниц — "
            "ряд может быть загружен НЕ ПОЛНОСТЬЮ"
        )

    if newest is None:
        await walk(None, None, "вся глубина")
    else:
        await walk(None, newest, "свежий край")
        await walk(int(earliest.timestamp() * 1000), None, "недостающая глубина")

    result.earliest, result.latest, total = await _bounds(inst_id, bar)
    result.appended = total - result.already_in_db
    if not apply:
        result.notes.append(
            "без --apply ни одного запроса на запись не отправлено: "
            f"разобрано {result.rows_parsed} баров, записано 0"
        )
    return result


async def verify_stored_chain(inst_id: str, bar: str) -> int:
    """Сверяет ХРАНИМЫЙ конец бара с началом следующего. Читает ПОТОКОМ.

    Это и есть доказательство календарного правила (§3 ТЗ: границы не
    предполагать). Если ``close_time`` бара не совпал с ``open_time``
    следующего, значит правило длины периода расходится с рядом биржи, и
    сравнение §7 считать нельзя — ошибка была бы приписана арифметике сборки.

    Возвращает число проверенных пар: «ноль пар» и «все пары сошлись» обязаны
    различаться в отчёте.
    """
    previous: tuple[datetime, datetime] | None = None
    checked = 0
    pool = db.pool()
    async with pool.acquire() as connection, connection.transaction():
        cursor = connection.cursor(
            "SELECT open_time, close_time FROM backtest.candles "
            "WHERE inst_id=$1 AND bar=$2 ORDER BY open_time;",
            inst_id, bar, prefetch=CURSOR_PREFETCH,
        )
        async for row in cursor:
            current = (row["open_time"], row["close_time"])
            if previous is not None:
                if previous[1] != current[0]:
                    raise HtfError(
                        f"{inst_id} {bar}: конец бара {previous[0].isoformat()} "
                        f"записан как {previous[1].isoformat()}, а следующий бар "
                        f"открылся {current[0].isoformat()}"
                    )
                expected = next_period_start(bar, previous[0])
                if expected != current[0]:
                    raise HtfError(
                        f"{inst_id} {bar}: календарное правило разошлось с рядом "
                        f"биржи после {previous[0].isoformat()}: ожидалось "
                        f"{expected.isoformat()}, в базе {current[0].isoformat()}"
                    )
                checked += 1
            previous = current
    return checked


async def stream_source_bars(
    inst_id: str,
    bar: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> AsyncIterator[SourceBar]:
    """Читает ряд из базы ПОТОКОМ, по возрастанию времени. Память O(1).

    Курсор на стороне сервера, а не ``fetch`` в список: §10 ТЗ запрещает
    загрузку всего ряда в память, и запрет не декоративный — на Этапе 9.1.3
    расчёт был убит ядром дважды, пока не стал считать потоком.

    Значения приходят из базы как ``Decimal`` и такими и остаются: точность
    хранения ``NUMERIC(20,8)`` обязана дожить до сравнения (урок DOGE).
    """
    conditions = ["inst_id = $1", "bar = $2"]
    args: list[Any] = [inst_id, bar]
    if since is not None:
        args.append(since)
        conditions.append(f"open_time >= ${len(args)}")
    if until is not None:
        args.append(until)
        conditions.append(f"open_time < ${len(args)}")
    query = (
        "SELECT open_time, open, high, low, close, volume FROM backtest.candles "
        f"WHERE {' AND '.join(conditions)} ORDER BY open_time"
    )
    pool = db.pool()
    async with pool.acquire() as connection, connection.transaction():
        cursor = connection.cursor(query, *args, prefetch=CURSOR_PREFETCH)
        async for row in cursor:
            yield SourceBar(
                ts=row["open_time"],
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
            )


async def stream_stored_htf(bars: tuple[str, ...]) -> AsyncIterator[StoredBar]:
    """Все сохранённые старшие бары — для проверки §8. Тоже потоком."""
    pool = db.pool()
    async with pool.acquire() as connection, connection.transaction():
        cursor = connection.cursor(
            "SELECT inst_id, bar, open_time, close_time FROM backtest.candles "
            "WHERE bar = ANY($1::text[]) ORDER BY bar, inst_id, open_time;",
            list(bars), prefetch=CURSOR_PREFETCH,
        )
        async for row in cursor:
            yield StoredBar(
                inst_id=row["inst_id"], bar=row["bar"],
                open_time=row["open_time"], close_time=row["close_time"],
            )


async def stored_bar_at(inst_id: str, bar: str, open_time: datetime) -> dict[str, Any] | None:
    """Один сохранённый старший бар — правая сторона сравнения §7."""
    row = await db.fetchrow(
        "SELECT open, high, low, close, volume FROM backtest.candles "
        "WHERE inst_id=$1 AND bar=$2 AND open_time=$3;",
        inst_id, bar, open_time,
    )
    return dict(row) if row is not None else None


def source_row_for_test(
    inst_id: str, bar: str, opened_at: datetime, price: Decimal, volume: Decimal
) -> tuple[Any, ...]:
    """Строка вставки старшего бара — для контрольных опытов в тестах.

    Живёт рядом с настоящей вставкой намеренно: контрольный опыт §8.2 обязан
    класть в таблицу строку ТОЙ ЖЕ формы, что кладёт загрузка, иначе он
    проверял бы не то хранилище.
    """
    return (
        inst_id, bar, opened_at, next_period_start(bar, opened_at),
        price, price, price, price, volume, None, SOURCE_EXCHANGE,
    )
