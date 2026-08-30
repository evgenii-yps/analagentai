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
* по каждому контрактному инструменту — число точек funding в окне
  ``FUTURES_LOOKBACK_HOURS``, действующий порог ``FUTURES_MIN_POINTS`` и запас
  между ними; инструменты с запасом меньше трёх точек отмечаются (§7 ТЗ 8.7);
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
    # Этап 9.1.1 §4. Сервис ведения позиций пишет свой heartbeat с Этапа 9.1, но
    # ключ не читал никто: остановившийся сервис ничем себя не проявлял, а его
    # остановка означает, что уже открытые позиции повиснут — задетые за время
    # простоя цель и предел не будут замечены никогда, потому что бары уйдут за
    # отметку last_checked_ts только вместе с их разбором.
    ("positions:heartbeat", "POSITION_INTERVAL", 60),
]

# Этап 9.1.1 §4: контейнер positions добавлен вместе со своим heartbeat.
# Перечень контейнеров и перечень heartbeat-ключей обязаны описывать ОДИН И ТОТ
# ЖЕ стек: сервис, чей heartbeat в отчёте есть, а контейнер не назван, читается
# как «ключ протух сам по себе».
CONTAINERS = ["postgres", "redis", "collector", "agents", "decision", "notify", "evaluator",
              "bot", "positions"]

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
# Главный горизонт: в .env он может стоять как «4h» (до Этапа 8.1) или как «4»
# (после). Сводка приводит его к подписи «4h», под которой лежит текстовая
# колонка horizon в таблице оценок.
def _primary_horizon() -> str:
    raw = os.environ.get(
        "EVAL_PRIMARY_HORIZON", ENV.get("EVAL_PRIMARY_HORIZON", "4h")
    ) or "4h"
    head = raw.split("#", 1)[0].strip()
    return head if head.endswith("h") else f"{head}h"


PRIMARY_HORIZON = _primary_horizon()
# Порог индекса согласия для счётчика «кандидатов» — из .env, не зашивается.
NOTIFY_MIN_PROBABILITY = os.environ.get(
    "NOTIFY_MIN_PROBABILITY", ENV.get("NOTIFY_MIN_PROBABILITY", "0.7")
)
# Версия логики: по ней ищется активная калибровочная кривая (Этап 7.3).
# Значение может прийти с хвостовым комментарием (если .env правили вручную) —
# берём первое «слово» и не падаем на мусоре: сводка важнее одной строки в ней.
def _logic_version() -> int:
    raw = os.environ.get("LOGIC_VERSION", ENV.get("LOGIC_VERSION", "4")) or "4"
    head = raw.split("#", 1)[0].strip().split()
    try:
        return int(head[0]) if head else 4
    except ValueError:
        return 4


LOGIC_VERSION = _logic_version()


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
    lines.append(f"Кандидатов (индекс согласия ≥ порога): {candidates or '0'}")

    # Этап 7.3, Блок C: инерция входов. Решение, принятое на том же наборе мнений
    # агентов, что и предыдущее, новой информации не несёт. Отправку это не
    # фильтрует — но без этой строки статистика «1440 решений в сутки» вводит
    # в заблуждение.
    repeats = _psql(
        "SELECT count(*) FILTER (WHERE is_repeat) || '|' || count(*) "
        "|| '|' || count(DISTINCT inputs_hash) FROM signals "
        "WHERE ts > now() - interval '24 hours';"
    )
    if repeats and repeats.count("|") == 2:
        repeat_n, total_n, unique_n = (part.strip() for part in repeats.split("|"))
        share = (
            f" ({round(100.0 * int(repeat_n) / int(total_n))}%)"
            if total_n.isdigit() and int(total_n) > 0
            else ""
        )
        lines.append(f"Из них повторных решений: {repeat_n}{share}")
        lines.append(f"Уникальных наборов мнений: {unique_n}")

    # Калибровочная кривая (Этап 7.3, Блок B): есть ли она вообще и на чём стоит.
    curve = _psql(
        "SELECT to_char(built_at, 'DD.MM HH24:MI') || '|' || sample_size "
        "FROM calibration_curves WHERE is_active "
        f"AND logic_version = {int(LOGIC_VERSION)} LIMIT 1;"
    )
    if curve and "|" in curve:
        built, sample = (part.strip() for part in curve.split("|", 1))
        lines.append(f"Калибровка: кривая от {built} UTC, N={sample}")
    else:
        lines.append(
            "Калибровка: активной кривой нет — вероятность не показывается"
        )

    closed = _psql(
        "SELECT count(*) FROM signal_evaluations "
        f"WHERE horizon = '{PRIMARY_HORIZON}' "
        "AND evaluated_at > now() - interval '24 hours';"
    )
    lines.append(f"Закрыто оценщиком (горизонт {PRIMARY_HORIZON}): {closed or '0'}")

    # Деградированные циклы за сутки (Этап 7.2, Задача A2): решение принято при
    # неполном составе агентов (<3). По таким сигналам уведомление не отправлялось.
    degraded = _psql(
        "SELECT count(*) FROM signals "
        "WHERE degraded AND ts > now() - interval '24 hours';"
    )
    mark = "🔴" if (degraded or "0") not in ("", "0") else "🟢"
    lines.append(f"{mark} Деградированных циклов (агентов < 3): {degraded or '0'}")
    return lines


def section_agent_failures() -> list[str]:
    """Сбои итераций агентов за 24 часа (Этап 7.0, Задача B).

    Раньше сбой агента терялся молча (только warning в лог) — так 14% выводов
    Market Agent пропали незаметно. Теперь каждый сбой — строка в agent_failures,
    и здесь виден их счётчик по агентам с разбивкой compute/db_write.
    """
    lines = ["<b>🩺 Сбои агентов за 24 часа</b>"]
    out = _psql(
        "SELECT agent, "
        "count(*) FILTER (WHERE error_type='compute'), "
        "count(*) FILTER (WHERE error_type='db_write'), "
        "count(*) "
        "FROM agent_failures WHERE ts > now() - interval '24 hours' "
        "GROUP BY agent ORDER BY agent;"
    )
    if not out:
        lines.append("🟢 Сбоев не зафиксировано.")
        return lines
    total = 0
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) < 4:
            continue
        agent, compute, db_write, cnt = parts[0], parts[1], parts[2], parts[3]
        try:
            total += int(cnt)
        except ValueError:
            pass
        lines.append(
            f"🔴 {_esc(agent)}: {cnt} (расчёт {compute}, запись в БД {db_write})"
        )
    if total == 0:
        return ["<b>🩺 Сбои агентов за 24 часа</b>", "🟢 Сбоев не зафиксировано."]
    return lines


# Порог доли insufficient_data, выше которого молчание агента попадает в сводку
# как замечание (§6 ТЗ 8.6). Ниже порога строка не печатается вовсе: сводку
# читают каждый день, и шум в ней хуже отсутствия строки.
SILENCE_SHARE_WARN = 0.50

# Сколько часов подряд молчания по одному инструменту считать поводом для
# отдельного сообщения. Сутки выбраны потому, что именно столько агент
# деривативов молчал на четырёх токенах из пяти незамеченным (замер 8.5).
SILENCE_HOURS_ALERT = 24


def section_agent_silence() -> list[str]:
    """Доля insufficient_data по агенту и инструменту за 24 часа (§6 ТЗ 8.6).

    Зачем отдельная секция. ``insufficient_data`` — ШТАТНЫЙ исход: агент не
    бросает исключения и в ``agent_failures`` не попадает, и это сознательное
    решение Этапа 7.2 (отличать «данных нет» от «агент сломался»). Но из-за
    этого молчание агента не видел никто: замер 8.5 показал агент деривативов,
    молчавший на четырёх токенах из пяти полтора суток при нуле записей в
    журнале сбоев. Здесь молчание становится видимым, а логика агентов не
    меняется — это надзор, а не поведение.

    Отдельной строкой отмечается инструмент, по которому агент не сказал ничего
    содержательного дольше ``SILENCE_HOURS_ALERT`` часов подряд.
    """
    lines = ["<b>🔇 Молчание агентов за 24 часа</b>"]
    out = _psql(
        "SELECT a.agent, i.base, "
        "count(*) FILTER (WHERE a.signal='insufficient_data'), "
        "count(*), "
        "round(extract(epoch FROM now() - coalesce(max(a.ts) FILTER "
        "(WHERE a.signal<>'insufficient_data'), min(a.ts))) / 3600.0) "
        "FROM agent_outputs a JOIN instruments i ON i.id = a.instrument_id "
        "WHERE a.ts > now() - interval '24 hours' "
        "GROUP BY a.agent, i.base ORDER BY a.agent, i.base;"
    )
    if not out:
        lines.append("🟢 Выводов за сутки нет — сравнивать нечего.")
        return lines

    noisy: list[str] = []
    alerts: list[str] = []
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) < 5:
            continue
        agent, token, silent_raw, total_raw, hours_raw = parts[:5]
        try:
            silent, total = int(silent_raw), int(total_raw)
            hours = float(hours_raw or 0)
        except ValueError:
            continue
        if total == 0:
            continue
        share = silent / total
        if share >= SILENCE_SHARE_WARN:
            noisy.append(
                f"🟡 {_esc(agent)} / {_esc(token)}: молчит "
                f"{share * 100:.0f}% выводов ({silent} из {total})"
            )
        if silent == total and hours >= SILENCE_HOURS_ALERT:
            alerts.append(
                f"🔴 {_esc(agent)} / {_esc(token)}: ни одного содержательного "
                f"вывода {hours:.0f} ч подряд"
            )

    if not noisy and not alerts:
        lines.append("🟢 Все агенты говорят по всем инструментам.")
        return lines
    lines.extend(alerts)
    lines.extend(noisy)
    return lines


# Запас точек funding: сколько их ещё можно потерять, прежде чем агент
# деривативов замолчит. Меньше трёх — инструмент отмечается (§7 ТЗ 8.7).
FUNDING_RESERVE_ALERT = 3


def _env_int(key: str) -> int | None:
    """Целое из .env/окружения. None — «параметр не задан».

    Умолчание НЕ подставляется намеренно (та же дисциплина, что в §3 ТЗ 8.7):
    строка запаса, посчитанная от зашитого числа вместо действующего, врала бы
    молча — а именно этот дефект этап и устраняет. Нет параметра — так и
    написано.
    """
    raw = os.environ.get(key, ENV.get(key, ""))
    head = (raw or "").split("#", 1)[0].strip()
    if not head:
        return None
    try:
        return int(head)
    except ValueError:
        return None


def section_funding_reserve() -> list[str]:
    """Запас точек funding по каждому инструменту (§7 ТЗ 8.7).

    ЧИСТО ОТЧЁТНАЯ секция: ни порогов, ни поведения агентов она не меняет.
    До неё нехватку точек находили только руками — раздел молчания (Этап 8.6)
    показывал ФАКТ молчания, но не то, насколько близко к нему остальные.

    Точки считаются ТАК ЖЕ, как их видит агент: ``db.get_funding_window``
    прореживает окно до ОДНОЙ точки на час (``DISTINCT ON`` по часу), и с этим
    числом сравнивается ``FUTURES_MIN_POINTS``. Считать сырые строки было бы
    неверно: коллектор пишет чаще раза в час, и запас вышел бы завышенным.

    Инструменты — контрактные (``type='swap'``): именно их получает
    ``FuturesAgent`` (src/agents/runner.py). У спота funding нет.
    """
    lines = ["<b>⛽ Запас точек funding</b>"]
    lookback = _env_int("FUTURES_LOOKBACK_HOURS")
    min_points = _env_int("FUTURES_MIN_POINTS")

    if lookback is None:
        lines.append(
            "⚪ FUTURES_LOOKBACK_HOURS: параметр не задан — "
            "окно неизвестно, запас не считается."
        )
        return lines
    if min_points is None:
        lines.append(
            "⚪ FUTURES_MIN_POINTS: параметр не задан — "
            "порог неизвестен, запас не считается."
        )
        lines.append(f"Окно: {lookback} ч.")
        return lines

    lines.append(
        f"Окно {lookback} ч, порог {min_points} точек "
        f"(точка = один час с данными, как их считает агент)."
    )
    out = _psql(
        "SELECT i.base, "
        "count(DISTINCT date_trunc('hour', f.ts)) "
        "FROM instruments i "
        "LEFT JOIN funding f ON f.instrument_id = i.id "
        f"AND f.ts >= now() - make_interval(hours => {lookback}) "
        "WHERE i.type = 'swap' AND i.active "
        "GROUP BY i.base ORDER BY i.base;"
    )
    if not out:
        # Пустой ответ значит одно из двух, и различить их отсюда нечем: либо
        # контрактных инструментов нет, либо запрос не выполнился. Писать
        # «всё хорошо» нельзя ни в том, ни в другом случае.
        lines.append(
            "⚪ Ответа от базы нет: либо контрактных инструментов нет, "
            "либо запрос не выполнился. Строка запаса не построена."
        )
        return lines

    tight = 0
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) < 2:
            continue
        token, points_raw = parts[0], parts[1]
        try:
            points = int(points_raw)
        except ValueError:
            continue
        reserve = points - min_points
        if reserve < 0:
            mark = "🔴"
            tail = f"агент уже молчит (не хватает {-reserve})"
            tight += 1
        elif reserve < FUNDING_RESERVE_ALERT:
            mark = "🟡"
            tail = "до молчания меньше трёх точек"
            tight += 1
        else:
            mark = "🟢"
            tail = ""
        line = (
            f"{mark} {_esc(token)}: точек {points}, порог {min_points}, "
            f"запас {reserve:+d}"
        )
        lines.append(f"{line} — {tail}" if tail else line)

    if tight == 0:
        lines.append(f"🟢 Запас не меньше {FUNDING_RESERVE_ALERT} точек у всех инструментов.")
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
        "\n".join(section_agent_failures()),
        "\n".join(section_agent_silence()),
        "\n".join(section_funding_reserve()),
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
