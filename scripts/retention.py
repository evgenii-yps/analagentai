#!/usr/bin/env python3
"""Политика хранения данных (§9 ТЗ 6.5).

Ограничивает рост БД: удаляет устаревшие «сырые» рыночные данные, сохраняя
ценные аналитические таблицы (``signals``, ``signal_evaluations``,
``agent_outputs`` хранятся дольше или бессрочно).

Удаление выполняется командами ``DELETE`` внутри контейнера postgres через
``docker compose exec -T postgres psql`` — поэтому скрипту не нужны
Python-драйверы БД на хосте (только стандартная библиотека). Запускается из
cron под пользователем ``agent`` (входит в группу docker).

Сроки хранения задаются переменными окружения (в днях), значения по умолчанию
см. ниже. Значение 0 отключает удаление для таблицы.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Таблица -> переменная окружения со сроком хранения и её значение по умолчанию.
# Колонка времени во всех этих таблицах называется ts.
RETENTION_RULES: list[tuple[str, str, int]] = [
    ("orderbook_snapshots", "RETENTION_ORDERBOOK_DAYS", 7),
    ("trades", "RETENTION_TRADES_DAYS", 14),
    ("ohlcv", "RETENTION_OHLCV_DAYS", 90),
    ("funding", "RETENTION_FUNDING_DAYS", 180),
    ("open_interest", "RETENTION_OI_DAYS", 180),
    ("agent_outputs", "RETENTION_AGENT_OUTPUTS_DAYS", 90),
    # signals и signal_evaluations НЕ чистим — это результат работы системы.
]


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


def _load_env_file(path: str) -> dict[str, str]:
    """Читает простой .env (KEY=value, без инлайновых комментариев)."""
    env: dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _psql(pg_user: str, pg_db: str, sql: str) -> str:
    """Выполняет SQL внутри контейнера postgres, возвращает stdout (строкой)."""
    cmd = [
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", pg_user, "-d", pg_db, "-t", "-A", "-c", sql,
    ]
    result = subprocess.run(
        cmd, cwd=APP_DIR, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def main() -> int:
    """Прогоняет правила хранения. Возвращает 0 при успехе, 1 при ошибке."""
    _log("=== Политика хранения данных ===")
    env_file = _load_env_file(os.path.join(APP_DIR, ".env"))
    pg_user = os.environ.get("POSTGRES_USER", env_file.get("POSTGRES_USER", "agenttrade"))
    pg_db = os.environ.get("POSTGRES_DB", env_file.get("POSTGRES_DB", "agenttrade"))

    had_error = False
    for table, env_var, default_days in RETENTION_RULES:
        days = int(os.environ.get(env_var, str(default_days)))
        if days <= 0:
            _log(f"{table}: хранение отключено (0 дней) — пропуск.")
            continue
        sql = (
            f"DELETE FROM {table} "
            f"WHERE ts < now() - interval '{days} days';"
        )
        try:
            out = _psql(pg_user, pg_db, sql)
            # psql печатает 'DELETE <N>'.
            deleted = out.split()[-1] if out.startswith("DELETE") else "?"
            _log(f"{table}: удалены записи старше {days} дн. (строк: {deleted}).")
        except subprocess.CalledProcessError as exc:
            had_error = True
            stderr = (exc.stderr or "").strip()
            _log(f"ОШИБКА при очистке {table}: {stderr or exc}")
        except Exception as exc:  # noqa: BLE001 — устойчивость важнее точности типа
            had_error = True
            _log(f"ОШИБКА при очистке {table}: {exc}")

    if had_error:
        _log("Завершено с ошибками (см. выше). Данные, где очистка не удалась, не тронуты.")
        return 1
    _log("Готово: политика хранения применена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
