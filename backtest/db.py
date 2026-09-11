"""Доступ к БД для реплея: один пул на процесс, запись только в схему backtest.

Модуль не входит в перечень §7 ТЗ, но перечень не исчерпывающий (в нём нет и
``backtest/run.py``, требуемого §11). Держать здесь общий пул правильнее, чем
дублировать его в loader/replay/evaluate/report.

Подключение идёт ролью прогона (``BT_DB_USER``, по умолчанию ``agenttrade_bt``),
у которой есть запись в схему ``backtest`` и ТОЛЬКО чтение продакшн-схемы
``public``. Если роль не задана, используется продакшн-пользователь — тогда
ограничение обеспечивается кодом (ни один запрос здесь не пишет вне backtest),
и это состояние явно печатается при старте, чтобы не выглядело нормой.
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg

from backtest.htf import PRODUCTION_FORBIDDEN_TIMEFRAMES
from src.core.config import settings

_pool: asyncpg.Pool | None = None


def dsn() -> str:
    """DSN подключения прогона. Пароль в логи не печатается."""
    user = os.environ.get("BT_DB_USER", "").strip()
    password = os.environ.get("BT_DB_PASSWORD", "").strip()
    if user and password:
        return (
            f"postgresql://{user}:{password}"
            f"@{settings.PG_HOST}:{settings.PG_PORT}/{settings.POSTGRES_DB}"
        )
    return settings.pg_dsn


def using_backtest_role() -> bool:
    """Подключаемся ли выделенной ролью прогона (а не продакшн-пользователем)."""
    return bool(os.environ.get("BT_DB_USER", "").strip())


async def connect() -> asyncpg.Pool:
    """Создаёт пул (идемпотентно) и возвращает его."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=dsn(), min_size=1, max_size=4)
    return _pool


async def close() -> None:
    """Закрывает пул."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    """Текущий пул. Требует предварительного :func:`connect`."""
    if _pool is None:
        raise RuntimeError("пул БД не инициализирован: вызовите backtest.db.connect()")
    return _pool


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    return await pool().fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    return await pool().fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    return await pool().fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    return await pool().execute(query, *args)


async def schema_exists() -> bool:
    """Есть ли схема backtest (миграция 008 применена)."""
    return bool(
        await fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'backtest';"
        )
    )


async def production_row_counts() -> dict[str, int]:
    """Счётчики строк продакшн-таблиц. НАБЛЮДЕНИЕ, А НЕ ВЕРДИКТ.

    Снимается до и после прогона. Читает, но ничего не меняет.

    ЧЕМ ЭТИ ЧИСЛА НЕ ЯВЛЯЮТСЯ (Замер 0.1). Доказательством неприкосновенности
    продакшна они не являются НА БОЕВОЙ МАШИНЕ. Продакшн работает 24/7 и пишет
    в свои таблицы параллельно с любым прогоном: измерено 11.09.2026 — за
    60 секунд покоя signals +5, agent_outputs +15, ohlcv +5, open_interest +5.
    Разница «до» и «после» здесь означает «система живёт», и назвать её
    нарушением границы значило бы назвать нарушением работу системы.

    Границу этапа проверяет :func:`production_boundary_violations` — по
    ПРИЗНАКУ ПРОИСХОЖДЕНИЯ строки, а не по их числу.
    """
    tables = (
        "signals",
        "agent_outputs",
        "signal_evaluations",
        "ohlcv",
        "funding",
        "open_interest",
        "calibration_curves",
    )
    counts: dict[str, int] = {}
    for table in tables:
        exists = await fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = $1;",
            table,
        )
        if not exists:
            continue
        counts[table] = int(await fetchval(f"SELECT count(*) FROM public.{table};"))
    return counts


async def production_boundary_violations() -> dict[str, int] | None:
    """Строки в ``public.ohlcv``, которые мог записать ТОЛЬКО этот этап.

    Продакшн собирает 1m/5m/15m/1h и старших масштабов не пишет вовсе, поэтому
    их присутствие в продакшн-таблице свечей есть запись вне схемы ``backtest``
    — прямой запрет §1 ТЗ. Запрос тот же, что в пункте 7 deploy/verify_z0.sh.

    Возвращает ``{масштаб: строк}``; пустой словарь — нарушений нет. ``None``
    означает, что проверить НЕЧЕМ: таблицы ``public.ohlcv`` не существует. Это
    НЕ «нарушений нет», и вызывающий код обязан их различать.

    ТОЛЬКО ЧТЕНИЕ: ни одного запроса на запись здесь нет.
    """
    exists = await fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'ohlcv';"
    )
    if not exists:
        return None
    rows = await fetch(
        "SELECT timeframe, count(*) AS rows FROM public.ohlcv "
        "WHERE timeframe = ANY($1::text[]) GROUP BY timeframe ORDER BY timeframe;",
        list(PRODUCTION_FORBIDDEN_TIMEFRAMES),
    )
    return {str(row["timeframe"]): int(row["rows"]) for row in rows}
