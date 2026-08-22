#!/usr/bin/env python3
"""Политика хранения данных (§9 ТЗ 6.5, расширено §4 ТЗ 8.1).

Пять токенов увеличивают приток данных впятеро, поэтому сроки заданы явно:

* ``orderbook_snapshots`` — старше ``RETENTION_ORDERBOOK_DAYS`` (по умолчанию 14);
* ``trades``              — старше ``RETENTION_TRADES_DAYS`` (по умолчанию 30);
* ``ohlcv`` с ``timeframe = '1m'`` — старше ``RETENTION_1M_DAYS`` (по умолчанию 30).

НИКОГДА НЕ УДАЛЯЮТСЯ (§4 ТЗ 8.1): часовые и любые НЕ минутные свечи, funding,
открытый интерес, сигналы и оценки. На них держится весь анализ, и восстановить
их неоткуда: биржа отдаёт историю свечей, но не историю наших решений.

Защита от опечатки сделана кодом, а не внимательностью: список защищённых
таблиц проверяется перед каждым удалением (:func:`_check_protected`), а правило
для ``ohlcv`` обязано нести условие по таймфрейму — без него удаление снесло бы
часовые свечи.

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

# Таблицы, которые НЕ УДАЛЯЮТСЯ НИКОГДА (§4 ТЗ 8.1). Список проверяется перед
# каждым удалением: опечатка в правиле обязана остановить задачу, а не стереть
# то, что восстановить неоткуда.
PROTECTED_TABLES: frozenset[str] = frozenset(
    {
        "funding",
        "open_interest",
        "signals",
        "signal_evaluations",
        "agent_outputs",
        "instruments",
        "calibration_curves",
        "signal_exports",
        "logic_version_windows",
    }
)


class RetentionRuleError(RuntimeError):
    """Правило хранения нарушает защиту таблиц — задача останавливается."""


def _rule(table: str, days_env: str, default: str, extra_where: str = "") -> tuple:
    """Правило: (таблица, срок в днях, дополнительное условие)."""
    return (table, int(_env_value(days_env, default)), extra_where)


def _check_protected(table: str, extra_where: str) -> None:
    """Останавливает задачу, если правило метит в защищённую таблицу.

    Отдельный случай — ``ohlcv``: сама таблица не защищена целиком (минутные
    свечи удалять можно и нужно), но правило по ней ОБЯЗАНО ограничиваться
    таймфреймом ``1m``. Без этого условия удаление снесло бы часовые свечи, на
    которых работает Market Agent и держится весь исторический анализ.
    """
    if table in PROTECTED_TABLES:
        raise RetentionRuleError(
            f"таблица {table} защищена от удаления (§4 ТЗ 8.1) — правило отклонено"
        )
    if table == "ohlcv" and "timeframe" not in extra_where:
        raise RetentionRuleError(
            "правило для ohlcv без условия по таймфрейму удалило бы часовые "
            "свечи — они не удаляются никогда (§4 ТЗ 8.1)"
        )

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

# Правила очистки: (таблица, срок хранения в днях, дополнительное условие).
# Значения читаются из окружения или из .env — сроки задаёт конфигурация, а не
# код: §4 ТЗ 8.1 требует RETENTION_1M_DAYS=30 и RETENTION_ORDERBOOK_DAYS=14.
RETENTION_RULES: list[tuple[str, int, str]] = [
    _rule("orderbook_snapshots", "RETENTION_ORDERBOOK_DAYS", "14"),
    _rule("trades", "RETENTION_TRADES_DAYS", "30"),
    _rule("ohlcv", "RETENTION_1M_DAYS", "30", "AND timeframe = '1m'"),
]


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


def _delete_in_batches(table: str, days: int, extra_where: str = "") -> int:
    """Удаляет из ``table`` записи старше ``days`` дней батчами. Возвращает всего.

    ``extra_where`` — дополнительное условие (``AND timeframe = '1m'``), без
    которого правило по ``ohlcv`` снесло бы часовые свечи.
    """
    _check_protected(table, extra_where)
    total = 0
    # ctid-подзапрос с LIMIT — быстрый батч без блокировки всей таблицы.
    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("
        f"  SELECT ctid FROM {table} "
        f"  WHERE ts < now() - interval '{days} days' {extra_where} "
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
    _log("=== Политика хранения данных (Этап 8.1 §4) ===")
    _log(
        "Никогда не удаляются: часовые (и любые не 1m) свечи, "
        + ", ".join(sorted(PROTECTED_TABLES))
    )
    had_error = False
    for table, days, extra_where in RETENTION_RULES:
        try:
            what = f"{table}{' ' + extra_where.strip() if extra_where else ''}"
            deleted = _delete_in_batches(table, days, extra_where)
            _log(f"{what}: удалено строк старше {days} дн.: {deleted}.")
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
