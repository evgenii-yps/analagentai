#!/usr/bin/env python3
"""Вотчдог стека Agent Trade (§11 ТЗ 6.5).

Периодически (из cron под пользователем ``agent``) проверяет, что все контейнеры
подняты и работают. Если какой-то сервис остановлен/нездоров — поднимает стек
заново (``docker compose up -d``) и шлёт предупреждение в Telegram.

Особенности:
* только стандартная библиотека Python (docker вызывается через subprocess,
  Telegram — через urllib), поэтому не зависит от драйверов БД на хосте;
* Telegram-уведомление отправляется НАПРЯМУЮ (не через контейнер), чтобы алерт
  доходил даже когда контейнеры лежат;
* устойчивость (§14): любые ошибки логируются, скрипт не «падает» трейсбеком;
* идемпотентность: повторный запуск при здоровом стеке ничего не меняет.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Сервисы приложения, которые обязаны быть подняты (postgres/redis — инфра).
EXPECTED_SERVICES = [
    "postgres", "redis", "collector", "agents", "decision", "notify", "evaluator",
]


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


def _load_env_file(path: str) -> dict[str, str]:
    """Читает простой .env (KEY=value)."""
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


def _telegram_alert(text: str) -> None:
    """Шлёт предупреждение в Telegram напрямую. Ошибки не пробрасываются."""
    env = _load_env_file(os.path.join(APP_DIR, ".env"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", env.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", env.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat_id:
        _log("Telegram не настроен — алерт не отправлен.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    ).encode("utf-8")
    ca_file = os.environ.get("SSL_CERT_FILE")
    try:
        req = urllib.request.Request(url, data=data)
        # Если задан кастомный CA — используем его.
        if ca_file:
            import ssl
            ctx = ssl.create_default_context(cafile=ca_file)
            urllib.request.urlopen(req, timeout=10, context=ctx).read()
        else:
            urllib.request.urlopen(req, timeout=10).read()
        _log("Алерт отправлен в Telegram.")
    except Exception as exc:  # noqa: BLE001
        _log(f"Не удалось отправить алерт в Telegram: {exc}")


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    """Запускает docker compose с заданными аргументами в каталоге приложения."""
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=APP_DIR, capture_output=True, text=True, check=False,
    )


def _service_states() -> dict[str, str]:
    """Возвращает {service: state} по данным ``docker compose ps``.

    state — строка вида 'running'/'exited'/'restarting'. Отсутствующий в выводе
    сервис трактуется вызывающим кодом как «не поднят».
    """
    states: dict[str, str] = {}
    proc = _compose("ps", "--format", "json")
    if proc.returncode != 0:
        _log(f"docker compose ps завершился с ошибкой: {proc.stderr.strip()}")
        return states
    out = proc.stdout.strip()
    if not out:
        return states
    # Compose может печатать либо JSON-массив, либо по одному объекту на строку.
    rows: list[dict] = []
    try:
        parsed = json.loads(out)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    for row in rows:
        name = row.get("Service") or row.get("Name") or ""
        state = (row.get("State") or "").lower()
        if name:
            states[name] = state
    return states


def main() -> int:
    """Проверяет стек и при необходимости поднимает его. 0 — всё здорово."""
    _log("=== Вотчдог Agent Trade ===")
    if not os.path.isdir(APP_DIR):
        _log(f"ОШИБКА: каталог {APP_DIR} не найден.")
        return 1

    states = _service_states()
    problems: list[str] = []
    for svc in EXPECTED_SERVICES:
        state = states.get(svc)
        if state is None:
            problems.append(f"{svc}: не запущен")
        elif state != "running":
            problems.append(f"{svc}: состояние '{state}'")

    if not problems:
        _log("Все сервисы работают (running). Действий не требуется.")
        return 0

    _log("Обнаружены проблемы: " + "; ".join(problems))
    _log("Пробую поднять стек: docker compose up -d")
    up = _compose("up", "-d", "--remove-orphans")
    if up.returncode == 0:
        _log("docker compose up -d выполнен успешно.")
        outcome = "восстановлено"
    else:
        _log(f"docker compose up -d завершился с ошибкой: {up.stderr.strip()}")
        outcome = "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО"

    _telegram_alert(
        "⚠️ <b>Agent Trade — вотчдог</b>\n"
        "Обнаружены неработающие сервисы:\n"
        + "\n".join(f"• {p}" for p in problems)
        + f"\n\nДействие: docker compose up -d → <b>{outcome}</b>"
    )
    return 0 if up.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
