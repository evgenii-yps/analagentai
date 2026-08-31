"""Двойник схемы базы: состав таблиц ЧИТАЕТСЯ ИЗ МИГРАЦИЙ, а не переписан руками.

ЗАЧЕМ ЭТОТ МОДУЛЬ СУЩЕСТВУЕТ. На Этапе 9.1.2.2 правило «тестовый двойник обязан
быть не мягче оригинала» нарушилось дважды за один этап. Двойник Google Таблицы
при ``PASTE_FORMULA`` пропускал ячейки без формул, тогда как настоящий Google
копирует их значением. Двойник базы подменял метод целиком и SQL не выполнял
вовсе, поэтому запрос к несуществующей колонке ``positions.symbol`` был для него
так же верен, как любой другой. Оба раза стенд был зелёным, а боевой прогон
падал.

ДВА СВОЙСТВА, РАДИ КОТОРЫХ ОН НАПИСАН ИМЕННО ТАК:

 1. СОСТАВ КОЛОНОК БЕРЁТСЯ ИЗ ФАЙЛОВ МИГРАЦИЙ. Переписанный руками в тест
    список однажды разошёлся бы с базой, и разошёлся бы МОЛЧА: проверки
    продолжали бы проходить, а падать начал бы боевой прогон — то есть ровно
    то, что уже произошло.
 2. ПРОВЕРЯЮТСЯ ОБА ВИДА ССЫЛОК, и второй важнее первого: квалифицированные
    (``p.symbol``) и НЕквалифицированные (``SELECT id, symbol … FROM positions``,
    когда в запросе участвует одна таблица). Проверять только первый вид значило
    бы пропустить ровно ту форму запроса, на которой всё и сломалось.

Модуль ЧИСТЫЙ: ни базы, ни сети. Он читает файлы репозитория и разбирает строки.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Слова, которые в SQL не являются именами колонок. Ошибиться в эту сторону
# безопаснее, чем в другую: лишнее слово в перечне даёт пропущенную проверку по
# одному имени, а недостающее — ложное падение на исправном запросе, и его
# видно сразу.
SQL_KEYWORDS: frozenset[str] = frozenset(
    word.lower() for word in (
        "SELECT", "FROM", "WHERE", "ORDER", "BY", "GROUP", "HAVING", "JOIN",
        "LEFT", "RIGHT", "INNER", "OUTER", "ON", "AND", "OR", "NOT", "IS",
        "NULL", "TRUE", "FALSE", "AS", "ANY", "ALL", "IN", "SET", "UPDATE",
        "INSERT", "INTO", "VALUES", "DELETE", "ASC", "DESC", "LIMIT", "OFFSET",
        "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END", "FILTER", "EXISTS",
        "CONFLICT", "DO", "NOTHING", "EXCLUDED", "RETURNING", "WITH", "UNION",
        # Имена типов в приведениях вроде ``$1::bigint[]``.
        "bigint", "int", "integer", "smallint", "text", "boolean", "numeric",
        "timestamptz", "date", "double", "precision", "interval", "float8",
        "jsonb", "serial", "bigserial",
    )
)


class UndefinedColumn(Exception):
    """То, чем на настоящей базе оборачивается ``asyncpg.UndefinedColumnError``."""


def table_columns(text: str, table: str) -> set[str]:
    """Состав колонок таблицы из её ``CREATE TABLE`` в переданном тексте."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    if marker not in text:
        raise ValueError(f"в тексте нет CREATE TABLE для {table}")
    block = text.split(marker, 1)[1].split("\n);", 1)[0]
    columns: set[str] = set()
    for line in block.splitlines():
        found = re.match(r"\s{4}([a-z_]+)\s+[A-Za-z]", line)
        if found and found.group(1) not in {"unique", "check", "constraint",
                                            "primary", "foreign"}:
            columns.add(found.group(1))
    return columns


def _added_columns(text: str, table: str) -> set[str]:
    """Колонки, добавленные к таблице через ``ALTER TABLE … ADD COLUMN``."""
    return set(
        re.findall(
            rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS\s+([a-z_]+)", text
        )
    )


def schema() -> dict[str, set[str]]:
    """Схема из файлов репозитория: ``{таблица: {колонки}}``.

    Источники — те же файлы, которыми схема заводится на боевой машине:
    ``db/init.sql`` для исходных таблиц и ``db/migrations/*.sql`` для добавленных
    позже. Ничего не переписано: если завтра миграция добавит колонку, двойник
    узнает о ней сам.
    """
    init = (_ROOT / "db" / "init.sql").read_text(encoding="utf-8")
    out: dict[str, set[str]] = {
        "instruments": table_columns(init, "instruments"),
        "ohlcv": table_columns(init, "ohlcv"),
        "signals": table_columns(init, "signals"),
    }

    migrations = _ROOT / "db" / "migrations"
    positions = table_columns(
        (migrations / "018_positions.sql").read_text(encoding="utf-8"), "positions"
    )
    for name in ("019_positions_data_gap.sql", "020_positions_sheet_export.sql"):
        positions |= _added_columns(
            (migrations / name).read_text(encoding="utf-8"), "positions"
        )
    out["positions"] = positions

    out["trailing_outcomes"] = table_columns(
        (migrations / "017_trailing_outcomes.sql").read_text(encoding="utf-8"),
        "trailing_outcomes",
    )
    out["position_trailing_shadow"] = table_columns(
        (migrations / "021_position_trailing_shadow.sql").read_text(
            encoding="utf-8"
        ),
        "position_trailing_shadow",
    )

    # Колонки, добавленные к signals миграциями и кодом выгрузки, — иначе
    # двойник объявил бы исправные запросы неверными.
    for path in sorted(migrations.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        out["signals"] |= _added_columns(text, "signals")
    return out


def check_sql_columns(sql: str, tables: dict[str, set[str]] | None = None) -> None:
    """Сверяет каждую колонку запроса с составом таблиц. Бросает при чужой.

    Проверяются оба вида ссылок (см. заголовок модуля). Неквалифицированные —
    только когда в запросе участвует РОВНО ОДНА известная таблица: иначе имя
    может принадлежать любой из них, и утверждать что-либо нельзя.
    """
    known = schema() if tables is None else tables
    text = re.sub(r"'[^']*'", "''", sql)
    bindings: dict[str, str] = {}
    participating: set[str] = set()
    for found in re.finditer(
        r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)(?:\s+(?:AS\s+)?([a-z_]+))?",
        text, re.I,
    ):
        table, alias = found.group(1), found.group(2)
        if table not in known:
            continue
        participating.add(table)
        use_alias = alias if alias and alias.lower() not in SQL_KEYWORDS else table
        bindings[use_alias] = table

    for found in re.finditer(r"\b([a-z_]+)\.([a-z_*]+)", text):
        alias, column = found.groups()
        table = bindings.get(alias)
        if table and column != "*" and column not in known[table]:
            raise UndefinedColumn(
                f'column "{column}" of relation "{table}" does not exist'
            )

    if len(participating) != 1:
        return
    only = next(iter(participating))
    # ПСЕВДОНИМЫ РЕЗУЛЬТАТА (``count(*) AS closed_total``) КОЛОНКАМИ НЕ
    # ЯВЛЯЮТСЯ. Это имена, которые запрос СОЗДАЁТ, а не читает, и требовать их
    # наличия в таблице значило бы объявлять исправный запрос неверным.
    bare = re.sub(r"\bAS\s+[a-z_][a-z_0-9]*", " ", text, flags=re.I)
    for found in re.finditer(r"(?<![.\w$])([a-z_][a-z_0-9]*)\s*(\.|\()?", bare):
        word, after = found.group(1), found.group(2)
        if after or word in SQL_KEYWORDS or word in known or word in bindings:
            continue
        if word not in known[only]:
            raise UndefinedColumn(f'column "{word}" does not exist')


def output_columns(sql: str) -> list[tuple[str, str]]:
    """Имена колонок ответа: ``[(исходное, как названо в ответе), …]``.

    ЗАЧЕМ ЭТО НУЖНО ДВОЙНИКУ. Настоящая база отдаёт строку с теми ключами,
    которые названы в SELECT: ``s.ts AS signal_ts`` даёт ключ ``signal_ts``, а не
    ``ts``. Двойник, отдающий свои ключи независимо от запроса, мягче базы — и
    это не теория: на Этапе 9.1.3 запрос отдавал момент сигнала под именем
    ``signal_ts``, тогда как переиспользуемая функция отбора читает ``ts``.
    Скрипт падал на первой же строке расчёта, а проверки проходили, потому что
    двойник подставлял ключ, которого запрос не просил.

    Разбирается только простой список колонок — такой, какие в этом проекте и
    пишутся. Выражение со скобками в SELECT приводит к ``ValueError``, а не к
    догадке: молча пропустить его значило бы вернуть неполную строку.
    """
    select = sql.split("SELECT", 1)[1].split("\n            FROM", 1)[0]
    if "FROM" in select:
        select = select.split("FROM", 1)[0]
    out: list[tuple[str, str]] = []
    for item in select.split(","):
        text = " ".join(item.split())
        if not text:
            continue
        found = re.fullmatch(
            r"(?:[a-z_]+\.)?([a-z_]+)(?:\s+AS\s+([a-z_]+))?", text, re.I
        )
        if not found:
            raise ValueError(f"двойник не разбирает выражение в SELECT: {text!r}")
        source, alias = found.group(1), found.group(2)
        out.append((source, alias or source))
    return out


def project(sql: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки с теми ключами, которые НАЗВАНЫ В ЗАПРОСЕ, а не удобны двойнику.

    Значения берутся по ИСХОДНОМУ имени колонки, ключ ставится по имени в
    ответе. Переименование в запросе меняет ключ ответа — ровно как в базе.
    """
    names = output_columns(sql)
    return [{alias: row[source] for source, alias in names} for row in rows]


class SchemaPool:
    """Двойник пула asyncpg, СВЕРЯЮЩИЙ колонки каждого запроса со схемой.

    Строг там же, где строга настоящая база: запрос к несуществующей колонке не
    выполняется, а бросает — ту же по смыслу ошибку, что приходит с боевой базы.

    Поведение (что именно вернуть) задаётся наследником: этот класс отвечает
    только за то, чтобы неверный SQL не прошёл незамеченным.
    """

    def __init__(self) -> None:
        self.schema = schema()
        self.queries: list[str] = []
        self.writes: list[str] = []

    def _check(self, sql: str) -> None:
        self.queries.append(sql)
        check_sql_columns(sql, self.schema)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._check(sql)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self._check(sql)
        self.writes.append(sql)
        return "UPDATE 0"

    async def executemany(self, sql: str, rows: list[Any]) -> None:
        self._check(sql)
        self.writes.append(sql)
