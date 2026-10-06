#!/usr/bin/env python3
"""Вотчдог стека Agent Trade (§11 ТЗ 6.5).

Запускается из cron под пользователем ``agent`` каждые 10 минут. Шлёт алерт в
Telegram, если:

* контейнер не в состоянии ``running``;
* heartbeat сервиса не обновлялся дольше 5× интервала его цикла;
* свободное место на диске меньше двух порогов (Этап 9.4, блок E):
  ``WATCHDOG_DISK_CRIT_PCT`` (15) — тревога, антиспам 1 раз в час, и
  ``WATCHDOG_DISK_WARN_PCT`` (25) — раннее предупреждение, не чаще 1 раза в
  24 часа под отдельным ключом антиспама.

Раз в сутки вотчдог дописывает строку ``дата;занято_ГБ;свободно_процент`` в
``logs/disk_usage.csv``; по последним 14 записям считается средний суточный
прирост, и оба сообщения несут оценку «до порога 15% осталось N суток» (пока
записей меньше 7 — «недостаточно данных»).

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

# Прогноз диска — общий модуль с суточной сводкой (одна реализация расчёта).
sys.path.insert(0, os.path.join(APP_DIR, "src", "health"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "health"))
import disk_forecast  # noqa: E402

# Этап 9.5: контейнер demo в списке (решение архитектора). Он работает и при
# DEMO_ENABLED=false (простаивает и пишет heartbeat), поэтому «не running» — всегда сбой:
# упавший контейнер должен замечаться и по состоянию, а не только по heartbeat.
CONTAINERS = ["postgres", "redis", "collector", "agents", "decision", "notify", "evaluator",
              "bot", "positions", "demo"]

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
    # Этап 9.5. Перезапуск контейнера demo БЕЗОПАСЕН: ордер сначала записывается
    # строкой в demo_orders и лишь потом уходит на биржу, а сервис при старте
    # разбирает строки pending/sent запросом по clOrdId, не отправляя ордер
    # повторно. В выключенном режиме (DEMO_ENABLED=false) сервис тоже обновляет
    # heartbeat — тревоги по выключенному сервису не будет.
    ("demo:heartbeat", "DEMO_INTERVAL", 15, "demo"),
]



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

DISK_ANTISPAM_CRIT_SEC = 3600          # тревога: не чаще раза в час
DISK_ANTISPAM_WARN_SEC = 24 * 3600     # предупреждение: не чаще раза в сутки
DISK_USAGE_CSV = os.path.join(APP_DIR, "logs", "disk_usage.csv")


def _pct_setting(*keys: str, default: float) -> float:
    """Порог из окружения или .env; первое найденное имя из ``keys``.

    Хвостовой комментарий в .env (``KEY=15   # пояснение``) отбрасывается.
    Окружение процесса перекрывает .env — так предупреждение проверяется
    принудительно: ``WATCHDOG_DISK_WARN_PCT=90 scripts/watchdog.py``.
    """
    for key in keys:
        raw = os.environ.get(key, ENV.get(key))
        if raw is None:
            continue
        raw = raw.split("#", 1)[0].strip()
        if raw:
            return float(raw)
    return default


# Пороги свободного места, %. В боевом .env заданы явно (Этап 9.4 §8); умолчания
# — страховка. Прежнее имя WATCHDOG_DISK_MIN_FREE_PCT читается как запасное для
# CRIT, чтобы старый .env продолжал работать.
DISK_CRIT_PCT = _pct_setting("WATCHDOG_DISK_CRIT_PCT", "WATCHDOG_DISK_MIN_FREE_PCT", default=15.0)
DISK_WARN_PCT = _pct_setting("WATCHDOG_DISK_WARN_PCT", default=25.0)


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


def _alert_allowed(reason: str, redis_up: bool, ttl_sec: int = 3600) -> bool:
    """True — если по этой причине можно слать алерт (анти-спам, по умолчанию 1/час)."""
    if not redis_up:
        # Redis недоступен — дедуп невозможен, но и сам Redis лежит: алертим.
        return True
    r = _compose("exec", "-T", "redis", "redis-cli",
                 "SET", f"watchdog:alert:{reason}", "1", "EX", str(ttl_sec), "NX")
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


def _disk_usage() -> tuple[float, float] | None:
    """(занято ГБ, свободно %) корневой ФС по данным ``df``. None — не удалось.

    Доля считается по тем же величинам, что и «Use%» в ``df`` (занято против
    занято+доступно), но без округления до целых процентов.
    """
    r = _run(["df", "-P", "-B1", "/"])
    rows = r.stdout.splitlines()
    if len(rows) < 2:
        return None
    parts = rows[1].split()
    if len(parts) < 4:
        return None
    try:
        used, avail = float(parts[2]), float(parts[3])
    except ValueError:
        return None
    if used + avail <= 0:
        return None
    return used / 1024**3, avail / (used + avail) * 100.0


def _disk_free_pct() -> float | None:
    """Свободно на корне ФС, %. None — если не удалось определить."""
    usage = _disk_usage()
    return None if usage is None else usage[1]


def disk_forecast_text(used_gb: float, free_pct: float, crit_pct: float = DISK_CRIT_PCT,
                       csv_path: str = DISK_USAGE_CSV) -> str:
    """Оценка «до порога осталось N суток» по ``logs/disk_usage.csv``."""
    capacity_gb = used_gb / (1.0 - free_pct / 100.0) if free_pct < 100.0 else used_gb
    records = disk_forecast.read_records(csv_path)
    days = disk_forecast.days_to_threshold(records, capacity_gb, crit_pct)
    return disk_forecast.describe(days, crit_pct)


def disk_alerts(free_pct: float, forecast: str, redis_up: bool) -> list[str]:
    """Сообщения по двум порогам диска (без записи в журнал).

    Ниже CRIT — тревога (антиспам 1/час, ключ ``disk``); ниже WARN, но не ниже
    CRIT — предупреждение (антиспам 24 ч, отдельный ключ ``disk_warn``). При
    прохождении CRIT предупреждение не дублируется: тревога уже несёт то же.
    """
    alerts: list[str] = []
    if free_pct < DISK_CRIT_PCT:
        _log(f"Мало места на диске: свободно {free_pct:.0f}% (< {DISK_CRIT_PCT:.0f}%). {forecast}.")
        if _alert_allowed("disk", redis_up, DISK_ANTISPAM_CRIT_SEC):
            alerts.append(
                f"💾 Мало места на диске: свободно {free_pct:.0f}% "
                f"(порог {DISK_CRIT_PCT:.0f}%). {forecast}"
            )
    elif free_pct < DISK_WARN_PCT:
        _log(f"Предупреждение: свободно {free_pct:.0f}% (< {DISK_WARN_PCT:.0f}%). {forecast}.")
        if _alert_allowed("disk_warn", redis_up, DISK_ANTISPAM_WARN_SEC):
            alerts.append(
                f"🟡 Место на диске убывает: свободно {free_pct:.0f}% "
                f"(предупреждение при {DISK_WARN_PCT:.0f}%, тревога при {DISK_CRIT_PCT:.0f}%). "
                f"{forecast}"
            )
        else:
            _log("Предупреждение о диске подавлено антиспамом (уже сообщали за последние 24 ч).")
    return alerts


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

    # 3) Свободное место на диске: два порога и прогноз.
    usage = _disk_usage()
    free_pct = None if usage is None else usage[1]
    _log(
        f"Пороги диска: предупреждение при свободных < {DISK_WARN_PCT:.0f}%, "
        f"тревога при < {DISK_CRIT_PCT:.0f}%."
    )
    if usage is not None:
        used_gb, free_now = usage
        try:
            if disk_forecast.append_daily(
                DISK_USAGE_CSV, datetime.now(UTC).date(), used_gb, free_now
            ):
                _log(f"Дописана суточная запись в {DISK_USAGE_CSV}: занято {used_gb:.2f} ГБ, "
                     f"свободно {free_now:.2f}%.")
        except OSError as exc:
            _log(f"Не удалось дописать {DISK_USAGE_CSV}: {exc}")
        forecast = disk_forecast_text(used_gb, free_now)
        _log(f"Диск: занято {used_gb:.2f} ГБ, свободно {free_now:.1f}%. {forecast}.")
        alerts.extend(disk_alerts(free_now, forecast, redis_up))

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
