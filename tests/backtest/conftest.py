"""Фикстуры тестов реплея (Этап 7.4).

Тесты, которым нужна БД, включаются переменной окружения ``BT_TEST_DSN``:

    BT_TEST_DSN=postgresql://postgres@127.0.0.1:5433/bt_test \
        python -m pytest tests/backtest

Без неё такие тесты ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не
выполнялись. Тесты чистых функций (издержки, независимые окна, целостность)
работают всегда и БД не требуют.

Данные и помощники вынесены в ``helpers.py``: каталог тестов намеренно НЕ
является пакетом, иначе пакет ``tests.backtest`` затенил бы настоящий
``backtest/``.
"""

from __future__ import annotations

import pytest
from helpers import TEST_DSN


@pytest.fixture
async def pool():
    """Пул к тестовой БД со свежей схемой backtest."""
    import asyncpg

    connection_pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    yield connection_pool
    await connection_pool.close()


@pytest.fixture
async def bt_db(pool, monkeypatch):
    """Подменяет пул модуля backtest.db на тестовый и чистит данные прогонов."""
    from backtest import db as bt

    monkeypatch.setattr(bt, "_pool", pool, raising=False)
    await pool.execute("TRUNCATE backtest.runs CASCADE;")
    await pool.execute("TRUNCATE backtest.candles;")
    await pool.execute("TRUNCATE backtest.funding;")
    await pool.execute("TRUNCATE backtest.gaps;")
    yield bt
