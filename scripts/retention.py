#!/usr/bin/env python3
"""Политика хранения данных (§9 ТЗ 6.5).

Удаляет устаревшие «сырые» высокочастотные данные, НЕ трогая ценные таблицы:

* ``orderbook_snapshots`` — удаляются записи старше 30 дней;
* ``trades``              — удаляются записи старше 30 дней;
* ``ohlcv``, ``funding``, ``open_interest``, ``agent_outputs``, ``signals``,
  ``signal_evaluations`` — НЕ удаляются никогда.

Удаление идёт батчами по 50 000 строк с паузой между батчами, чтобы не
блокировать запись коллектора. После удаления по затронутым таблицам
выполняется ``VACUUM ANALYZE`` (без ``FULL``). Число удалённых строк логируется.

Работает через ``docker compose exec -T postgres psql`` (только стандартная
библиотека Python). Запускается из cron под пользователем ``agent`` ежедневно
в 03:40 UTC.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Таблицы под очисткой: (имя, срок хранения в днях). Остальные не чистятся.
RETENTION_RULES: list[tuple[str, int]] = [
    ("orderbook_snapshots", int(os.environ.get("RETENTION_ORDERBOOK_DAYS", "30"))),
    ("trades", int(os.environ.get("RETENTION_TRADES_DAYS", "30"))),
]

BATCH = int(os.environ.get("RETENTION_BATCH", "50000"))
PAUSE_SEC = float(os.environ.get("RETENTION_PAUSE_SEC", "0.5"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


def _env_value(key: str, default: str) -> str:
    """Читает значение из окружения или из .env (KEY=value)."""
    if key in os.environ:
        return os.environ[key]
    path = os.path.join(APP_DIR, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    return default


PG_USER = _env_value("POSTGRES_USER", "agenttrade")
PG_DB = _env_value("POSTGRES_DB", "agenttrade")


def _psql(sql: str) -> str:
    """Выполняет SQL в контейнере postgres, возвращает stdout (обрезанный)."""
    cmd = [
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", PG_USER, "-d", PG_DB, "-t", "-A", "-c", sql,
    ]
    result = subprocess.run(
        cmd, cwd=APP_DIR, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _delete_in_batches(table: str, days: int) -> int:
    """Удаляет из ``table`` записи старше ``days`` дней батчами. Возвращает всего."""
    total = 0
    # ctid-подзапрос с LIMIT — быстрый батч без блокировки всей таблицы.
    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("
        f"  SELECT ctid FROM {table} "
        f"  WHERE ts < now() - interval '{days} days' "
        f"  LIMIT {BATCH}"
        f");"
    )
    while True:
        out = _psql(sql)  # psql печатает 'DELETE <N>'
        deleted = 0
        if out.startswith("DELETE"):
            try:
                deleted = int(out.split()[-1])
            except (ValueError, IndexError):
                deleted = 0
        total += deleted
        if deleted < BATCH:
            break
        time.sleep(PAUSE_SEC)  # пауза, чтобы не мешать записи коллектора
    return total


def main() -> int:
    """Прогоняет правила хранения. Возвращает 0 при успехе, 1 при ошибке."""
    _log("=== Политика хранения данных (orderbook_snapshots, trades) ===")
    had_error = False
    for table, days in RETENTION_RULES:
        try:
            deleted = _delete_in_batches(table, days)
            _log(f"{table}: удалено строк старше {days} дн.: {deleted}.")
            if deleted > 0:
                _log(f"{table}: выполняю VACUUM ANALYZE…")
                _psql(f"VACUUM ANALYZE {table};")
                _log(f"{table}: VACUUM ANALYZE выполнен.")
            else:
                _log(f"{table}: удалять нечего — VACUUM ANALYZE пропущен.")
        except subprocess.CalledProcessError as exc:
            had_error = True
            _log(f"ОШИБКА при очистке {table}: {(exc.stderr or '').strip() or exc}")
        except Exception as exc:  # noqa: BLE001
            had_error = True
            _log(f"ОШИБКА при очистке {table}: {exc}")

    if had_error:
        _log("Завершено с ошибками (см. выше).")
        return 1
    _log("Готово: политика хранения применена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
