#!/usr/bin/env python3
"""Вотчдог стека Agent Trade (§11 ТЗ 6.5).

Запускается из cron под пользователем ``agent`` каждые 10 минут. Шлёт алерт в
Telegram, если:

* контейнер не в состоянии ``running``;
* heartbeat сервиса не обновлялся дольше 5× интервала его цикла;
* свободное место на диске меньше 15%.

Анти-спам: не чаще одного алерта в час по одной и той же причине (состояние
хранится в Redis: ключ ``watchdog:alert:<причина>`` с TTL 3600, атомарный
``SET NX``). Дополнительно вотчдог пытается восстановить работу (поднять/
перезапустить сервис). Устойчив к сбоям: ошибки логируются, трейсбеком не падает.

Только стандартная библиотека Python (docker/redis-cli через subprocess,
Telegram — через urllib).
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

CONTAINERS = ["postgres", "redis", "collector", "agents", "decision", "notify", "evaluator",
              "bot", "positions"]

# heartbeat-ключ -> (env интервала, дефолт, имя контейнера-владельца).
HEARTBEATS: list[tuple[str, str, int, str]] = [
    ("collector:heartbeat:ohlcv", "OHLCV_INTERVAL", 30, "collector"),
    ("collector:heartbeat:orderbook", "ORDERBOOK_INTERVAL", 10, "collector"),
    ("collector:heartbeat:trades", "TRADES_INTERVAL", 15, "collector"),
    ("collector:heartbeat:futures", "FUTURES_INTERVAL", 60, "collector"),
    ("agent:heartbeat:market", "AGENT_INTERVAL", 60, "agents"),
    ("agent:heartbeat:liquidity", "AGENT_INTERVAL", 60, "agents"),
    ("agent:heartbeat:futures", "AGENT_INTERVAL", 60, "agents"),
    ("decision:heartbeat", "DECISION_INTERVAL", 60, "decision"),
    ("notify:heartbeat", "NOTIFY_INTERVAL", 30, "notify"),
    ("evaluator:heartbeat", "EVAL_INTERVAL", 300, "evaluator"),
    ("bot:heartbeat", "BOT_POLL_TIMEOUT", 30, "bot"),
    # Этап 9.1.1 §4. Перезапуск контейнера positions вотчдогом БЕЗОПАСЕН: всё
    # состояние позиций лежит в базе, в памяти сервиса состояния нет, а
    # повторный разбор уже разобранных баров идемпотентен — закрытие идёт одним
    # UPDATE ... WHERE status = 'open', и отставшая итерация получает ноль
    # изменённых строк вместо второго закрытия.
    ("positions:heartbeat", "POSITION_INTERVAL", 60, "positions"),
]

DISK_MIN_FREE_PCT = float(os.environ.get("WATCHDOG_DISK_MIN_FREE_PCT", "15"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


def _env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.path.join(APP_DIR, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


ENV = _env_file()


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout, check=False
    )


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args])


def _redis_ping() -> bool:
    r = _compose("exec", "-T", "redis", "redis-cli", "PING")
    return r.returncode == 0 and r.stdout.strip().upper() == "PONG"


def _redis_get(key: str) -> str:
    r = _compose("exec", "-T", "redis", "redis-cli", "GET", key)
    return r.stdout.strip() if r.returncode == 0 else ""


def _alert_allowed(reason: str, redis_up: bool) -> bool:
    """True — если по этой причине можно слать алерт (анти-спам 1/час)."""
    if not redis_up:
        # Redis недоступен — дедуп невозможен, но и сам Redis лежит: алертим.
        return True
    r = _compose("exec", "-T", "redis", "redis-cli",
                 "SET", f"watchdog:alert:{reason}", "1", "EX", "3600", "NX")
    return r.returncode == 0 and r.stdout.strip().upper() == "OK"


def _telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", ENV.get("TELEGRAM_BOT_TOKEN", ""))
    chat = os.environ.get("TELEGRAM_CHAT_ID", ENV.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat:
        _log("Telegram не настроен — алерт не отправлен.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    ca_file = os.environ.get("SSL_CERT_FILE")
    try:
        req = urllib.request.Request(url, data=data)
        if ca_file:
            import ssl
            ctx = ssl.create_default_context(cafile=ca_file)
            urllib.request.urlopen(req, timeout=10, context=ctx).read()
        else:
            urllib.request.urlopen(req, timeout=10).read()
        _log("Алерт отправлен в Telegram.")
    except Exception as exc:  # noqa: BLE001
        _log(f"Не удалось отправить алерт в Telegram: {exc}")


def _container_states() -> dict[str, str]:
    states: dict[str, str] = {}
    r = _compose("ps", "--format", "json")
    if r.returncode != 0 or not r.stdout.strip():
        return states
    import json
    rows: list[dict] = []
    try:
        parsed = json.loads(r.stdout)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        for line in r.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    for row in rows:
        name = row.get("Service") or row.get("Name") or ""
        if name:
            states[name] = (row.get("State") or "").lower()
    return states


def _disk_free_pct() -> float | None:
    """Свободно на корне ФС, %. None — если не удалось определить."""
    r = _run(["df", "-P", "/"])
    rows = r.stdout.splitlines()
    if len(rows) < 2:
        return None
    parts = rows[1].split()
    if len(parts) < 5:
        return None
    try:
        used_pct = float(parts[4].rstrip("%"))
        return 100.0 - used_pct
    except ValueError:
        return None


def main() -> int:
    _log("=== Вотчдог Agent Trade ===")
    if not os.path.isdir(APP_DIR):
        _log(f"ОШИБКА: каталог {APP_DIR} не найден.")
        return 1

    redis_up = _redis_ping()
    states = _container_states()
    alerts: list[str] = []           # причины, по которым реально шлём алерт
    recovered = False

    # 1) Контейнеры не в состоянии running.
    down = [c for c in CONTAINERS if states.get(c) != "running"]
    if down:
        _log("Не запущены контейнеры: " + ", ".join(down) + " — поднимаю (up -d).")
        up = _compose("up", "-d", "--remove-orphans")
        recovered = up.returncode == 0
        for c in down:
            if _alert_allowed(f"container:{c}", redis_up):
                st = states.get(c, "не запущен")
                alerts.append(f"📦 Контейнер <b>{c}</b> не running (состояние: {st})")

    # 2) Устаревшие heartbeat (только для запущенных контейнеров и живого Redis).
    if redis_up:
        now = datetime.now(UTC)
        restarted: set[str] = set()
        for key, env_var, default, owner in HEARTBEATS:
            if states.get(owner) != "running":
                continue  # контейнер и так лежит — уже учтено выше
            interval = int(os.environ.get(env_var, ENV.get(env_var, str(default))))
            threshold = 5 * interval
            val = _redis_get(key)
            stale = False
            detail = ""
            if not val:
                stale, detail = True, "нет отметки"
            else:
                try:
                    age = (now - datetime.fromisoformat(val)).total_seconds()
                    if age > threshold:
                        stale, detail = True, f"{int(age)} сек > 5×{interval}"
                except ValueError:
                    stale, detail = True, "некорректная отметка"
            if stale:
                if owner not in restarted:
                    _log(f"heartbeat {key} устарел ({detail}) — перезапускаю {owner}.")
                    _compose("restart", owner)
                    restarted.add(owner)
                    recovered = True
                if _alert_allowed(f"heartbeat:{key}", redis_up):
                    alerts.append(f"💓 Heartbeat <b>{key}</b> устарел ({detail})")

    # 3) Свободное место на диске.
    free_pct = _disk_free_pct()
    if free_pct is not None and free_pct < DISK_MIN_FREE_PCT:
        _log(f"Мало места на диске: свободно {free_pct:.0f}% (< {DISK_MIN_FREE_PCT:.0f}%).")
        if _alert_allowed("disk", redis_up):
            alerts.append(
                f"💾 Мало места на диске: свободно {free_pct:.0f}% "
                f"(порог {DISK_MIN_FREE_PCT:.0f}%)"
            )

    if not down and free_pct is not None and not alerts:
        _log("Всё в норме. Действий не требуется.")
        return 0

    if alerts:
        body = "⚠️ <b>Agent Trade — вотчдог</b>\n" + "\n".join(f"• {a}" for a in alerts)
        if recovered:
            body += "\n\nПопытка авто-восстановления выполнена (up/restart)."
        _telegram(body)
    else:
        _log("Проблемы есть, но алерты подавлены анти-спамом (уже сообщали за последний час).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
