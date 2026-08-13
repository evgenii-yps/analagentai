#!/usr/bin/env python3
"""Ежесуточная сводка о состоянии системы в Telegram (§10 ТЗ 6.5).

Собирает подробный отчёт и отправляет его в Telegram (на русском, HTML):

* аптайм сервера и статус каждого контейнера;
* heartbeat каждого сервиса из Redis (фактические ключи Этапов 2–6) и время
  последнего обновления;
* за 24 часа: число новых строк в ohlcv, trades, orderbook_snapshots, funding,
  open_interest, agent_outputs; число сигналов с разбивкой по decision; сколько
  уведомлений отправлено; сколько сигналов закрыто оценщиком;
* размер БД, свободное место на диске, свободная память;
* число записей уровня ERROR в логах за сутки;
* если поток данных за сутки не пополнялся — строка помечается красным (🔴):
  это главный признак «тихой» поломки.

Скрипт запускается на ХОСТЕ (не в контейнере), т.к. ему нужны данные хоста и
Docker: статус контейнеров, логи, диск, память. Использует только стандартную
библиотеку Python + docker compose (psql/redis-cli). Cron под пользователем
``agent`` ежедневно в 06:00 UTC (09:00 МСК).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Ожидаемые heartbeat-ключи (см. runner'ы Этапов 2–6) и env-переменная их
# интервала цикла (для оценки свежести). Значение ключа — ISO-время.
HEARTBEATS: list[tuple[str, str, int]] = [
    ("collector:heartbeat:ohlcv", "OHLCV_INTERVAL", 30),
    ("collector:heartbeat:orderbook", "ORDERBOOK_INTERVAL", 10),
    ("collector:heartbeat:trades", "TRADES_INTERVAL", 15),
    ("collector:heartbeat:futures", "FUTURES_INTERVAL", 60),
    ("agent:heartbeat:market", "AGENT_INTERVAL", 60),
    ("agent:heartbeat:liquidity", "AGENT_INTERVAL", 60),
    ("agent:heartbeat:futures", "AGENT_INTERVAL", 60),
    ("decision:heartbeat", "DECISION_INTERVAL", 60),
    ("notify:heartbeat", "NOTIFY_INTERVAL", 30),
    ("evaluator:heartbeat", "EVAL_INTERVAL", 300),
    ("bot:heartbeat", "BOT_POLL_TIMEOUT", 30),
]

CONTAINERS = ["postgres", "redis", "collector", "agents", "decision", "notify", "evaluator", "bot"]

# Потоки данных для проверки «тихой» поломки: (подпись, таблица).
DATA_STREAMS = [
    ("OHLCV", "ohlcv"),
    ("Сделки (trades)", "trades"),
    ("Стакан (orderbook)", "orderbook_snapshots"),
    ("Funding", "funding"),
    ("Open interest", "open_interest"),
    ("Выводы агентов", "agent_outputs"),
]


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
PG_USER = os.environ.get("POSTGRES_USER", ENV.get("POSTGRES_USER", "agenttrade"))
PG_DB = os.environ.get("POSTGRES_DB", ENV.get("POSTGRES_DB", "agenttrade"))
PRIMARY_HORIZON = os.environ.get(
    "EVAL_PRIMARY_HORIZON", ENV.get("EVAL_PRIMARY_HORIZON", "4h")
)
# Порог вероятности для счётчика «кандидатов» — берётся из .env, не зашивается.
NOTIFY_MIN_PROBABILITY = os.environ.get(
    "NOTIFY_MIN_PROBABILITY", ENV.get("NOTIFY_MIN_PROBABILITY", "0.7")
)


def _run(cmd: list[str], timeout: int = 60) -> str:
    """Запускает команду в APP_DIR, возвращает stdout ('' при ошибке)."""
    try:
        r = subprocess.run(
            cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _psql(sql: str, field_sep: str = "|") -> str:
    """Выполняет SQL в контейнере postgres, возвращает stdout ('' при ошибке)."""
    return _run([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", PG_USER, "-d", PG_DB, "-tA", "-F", field_sep, "-c", sql,
    ])


def _redis_get(key: str) -> str:
    return _run(["docker", "compose", "exec", "-T", "redis", "redis-cli", "GET", key])


def _esc(text: str) -> str:
    """Экранирование под HTML Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Секции отчёта ---

def section_server() -> list[str]:
    lines = ["<b>🖥 Сервер</b>"]
    uptime = _run(["uptime", "-p"]) or "неизвестно"
    lines.append(f"Аптайм: {_esc(uptime)}")

    # Свободная память (available), МБ.
    mem = _run(["free", "-m"])
    for row in mem.splitlines():
        if row.lower().startswith("mem:"):
            parts = row.split()
            if len(parts) >= 7:
                lines.append(f"Память: свободно ~{parts[6]} МБ из {parts[1]} МБ")
            break

    # Свободное место на диске корня.
    df = _run(["df", "-h", "/"])
    df_rows = df.splitlines()
    if len(df_rows) >= 2:
        p = df_rows[1].split()
        if len(p) >= 5:
            lines.append(f"Диск /: свободно {p[3]} (занято {p[4]})")
    return lines


def section_containers() -> list[str]:
    lines = ["<b>📦 Контейнеры</b>"]
    states: dict[str, str] = {}
    out = _run(["docker", "compose", "ps", "--format", "json"])
    rows: list[dict] = []
    if out:
        try:
            parsed = json.loads(out)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            for line in out.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    for r in rows:
        name = r.get("Service") or r.get("Name") or ""
        if name:
            states[name] = (r.get("State") or "").lower()
    for c in CONTAINERS:
        st = states.get(c, "не запущен")
        mark = "🟢" if st == "running" else "🔴"
        lines.append(f"{mark} {c}: {_esc(st)}")
    return lines


def section_heartbeats() -> list[str]:
    lines = ["<b>💓 Heartbeat сервисов</b>"]
    now = datetime.now(UTC)
    for key, env_var, default in HEARTBEATS:
        interval = int(os.environ.get(env_var, ENV.get(env_var, str(default))))
        val = _redis_get(key)
        if not val:
            lines.append(f"🔴 {key}: нет отметки (просрочен/сервис молчит)")
            continue
        try:
            ts = datetime.fromisoformat(val)
            # Часы контейнера и хост-скрипта могут расходиться → отметка «в
            # будущем» давала бы «-1 сек назад». Приводим к нулю (ТЗ 6.6.1 §8.4).
            age = max(0.0, (now - ts).total_seconds())
            fresh = age <= 5 * interval
            mark = "🟢" if fresh else "🔴"
            lines.append(
                f"{mark} {key}: {ts:%Y-%m-%d %H:%M:%S} UTC "
                f"({int(age)} сек назад)"
            )
        except ValueError:
            lines.append(f"🔴 {key}: некорректная отметка ({_esc(val[:40])})")
    return lines


def section_data_24h() -> list[str]:
    lines = ["<b>📈 Приток данных за 24 часа</b>"]
    tables = ",".join(
        f"(SELECT count(*) FROM {t} WHERE ts > now() - interval '24 hours')"
        for _, t in DATA_STREAMS
    )
    out = _psql(f"SELECT {tables};")
    counts = out.split("|") if out else []
    for i, (label, _table) in enumerate(DATA_STREAMS):
        raw = counts[i] if i < len(counts) else ""
        try:
            n = int(raw)
        except ValueError:
            lines.append(f"⚠️ {label}: нет данных запроса")
            continue
        # 🔴 если поток за сутки не пополнялся — признак тихой поломки.
        mark = "🔴" if n == 0 else "🟢"
        lines.append(f"{mark} {label}: +{n}")
    return lines


def section_signals_24h() -> list[str]:
    lines = ["<b>🚦 Сигналы за 24 часа</b>"]
    grp = _psql(
        "SELECT decision, count(*) FROM signals "
        "WHERE ts > now() - interval '24 hours' GROUP BY decision ORDER BY decision;"
    )
    if grp:
        parts = []
        for row in grp.splitlines():
            if "|" in row:
                dec, cnt = row.split("|", 1)
                parts.append(f"{dec}: {cnt}")
        lines.append("По решениям: " + (", ".join(parts) if parts else "нет"))
    else:
        lines.append("По решениям: нет")

    # Три раздельных счётчика (ТЗ 6.6.1 §7): фактические отправки, поглощённые
    # анти-спамом дубли/cooldown и кандидаты, прошедшие порог вероятности.
    # Раньше выводилась одна строка по флагу `notified`, который ставится и при
    # поглощении, — отсюда завышение (416 вместо 10–50 в сводке от 10.08.2026).
    sent = _psql(
        "SELECT count(*) FROM signals "
        "WHERE notified_at > now() - interval '24 hours';"
    )
    lines.append(f"Отправлено уведомлений: {sent or '0'}")

    absorbed = _psql(
        "SELECT count(*) FROM signals "
        "WHERE notified AND notified_at IS NULL "
        "AND ts > now() - interval '24 hours';"
    )
    lines.append(f"Поглощено анти-спамом: {absorbed or '0'}")

    candidates = _psql(
        "SELECT count(*) FROM signals "
        "WHERE decision <> 'wait' "
        f"AND probability >= {float(NOTIFY_MIN_PROBABILITY)} "
        "AND ts > now() - interval '24 hours';"
    )
    lines.append(f"Кандидатов (вероятность ≥ порога): {candidates or '0'}")

    closed = _psql(
        "SELECT count(*) FROM signal_evaluations "
        f"WHERE horizon = '{PRIMARY_HORIZON}' "
        "AND evaluated_at > now() - interval '24 hours';"
    )
    lines.append(f"Закрыто оценщиком (горизонт {PRIMARY_HORIZON}): {closed or '0'}")
    return lines


def section_db_and_errors() -> list[str]:
    lines = ["<b>🗄 БД и ошибки</b>"]
    size = _psql("SELECT pg_size_pretty(pg_database_size(current_database()));")
    lines.append(f"Размер БД: {_esc(size) if size else 'неизвестно'}")

    # Число записей уровня ERROR в логах контейнеров за сутки.
    logs = _run(
        ["docker", "compose", "logs", "--since", "24h", "--no-color"], timeout=90
    )
    errors = len(re.findall(r'"level"\s*:\s*"error"', logs, flags=re.IGNORECASE))
    mark = "🔴" if errors > 0 else "🟢"
    lines.append(f"{mark} Ошибок уровня ERROR за сутки: {errors}")
    return lines


def build_message() -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    blocks = [
        f"<b>📊 Agent Trade — суточная сводка</b>\n<i>{now}</i>",
        "\n".join(section_server()),
        "\n".join(section_containers()),
        "\n".join(section_heartbeats()),
        "\n".join(section_data_24h()),
        "\n".join(section_signals_24h()),
        "\n".join(section_db_and_errors()),
    ]
    return "\n\n".join(blocks)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", ENV.get("TELEGRAM_BOT_TOKEN", ""))
    chat = os.environ.get("TELEGRAM_CHAT_ID", ENV.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chat:
        print("daily_report: Telegram не настроен — сводка не отправлена.", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    ca_file = os.environ.get("SSL_CERT_FILE")
    try:
        req = urllib.request.Request(url, data=data)
        if ca_file:
            import ssl
            ctx = ssl.create_default_context(cafile=ca_file)
            urllib.request.urlopen(req, timeout=15, context=ctx).read()
        else:
            urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"daily_report: не удалось отправить сводку: {exc}", flush=True)
        return False


def main() -> None:
    """Точка входа. Никогда не завершается трейсбеком (устойчивость §14)."""
    try:
        message = build_message()
    except Exception as exc:  # noqa: BLE001
        message = f"<b>Agent Trade — суточная сводка</b>\nОшибка при сборе метрик: {exc}"
    if send_telegram(message):
        print("daily_report: сводка отправлена.", flush=True)


if __name__ == "__main__":
    main()
