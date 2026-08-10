"""Чистые функции преобразования сигналов в строки таблицы и свойства Notion.

Здесь нет ввода-вывода и обращений к БД/сети — только детерминированные
преобразования, чтобы логику формата можно было покрыть юнит-тестами.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Часовой пояс для колонки ts_msk листа «Сигналы».
_MSK = ZoneInfo("Europe/Moscow")

# Агенты, чьи сигнал/уверенность выносятся в отдельные колонки (порядок фиксирован).
AGENT_ORDER: tuple[str, ...] = ("market", "liquidity", "futures")

# Русские подписи решения (для заголовка и свойства «Решение» в Notion).
DECISION_RU = {"buy": "Покупать", "sell": "Продавать", "wait": "Ждать"}

# Человекочитаемые имена агентов для multi_select «Источник агента» в Notion.
AGENT_LABELS = {"market": "Market", "liquidity": "Liquidity", "futures": "Futures"}

# Заголовок листа «Сигналы» (и листа «Независимые окна») — порядок из §7.1.
SIGNALS_HEADER: list[str] = [
    "signal_id",
    "ts_utc",
    "ts_msk",
    "window_4h_utc",
    "token",
    "decision",
    "probability",
    "notified",
    "notified_at_utc",
    "agents_count",
    "market_signal",
    "market_confidence",
    "liquidity_signal",
    "liquidity_confidence",
    "futures_signal",
    "futures_confidence",
    "price_at_signal",
    "pnl_1h",
    "drawdown_1h",
    "success_1h",
    "price_at_close_4h",
    "pnl_4h",
    "drawdown_4h",
    "success_4h",
    "status",
    "rationale",
    "agents_payload_json",
]

# Заголовок листа «Сводка по дням» — порядок из §7.2.
SUMMARY_HEADER: list[str] = [
    "date_utc",
    "decisions_total",
    "buy",
    "sell",
    "wait",
    "candidates",
    "notified",
    "closed_4h",
    "success_rate_buy_4h",
    "success_rate_sell_4h",
    "avg_pnl_buy_4h",
    "avg_pnl_sell_4h",
    "avg_drawdown_4h",
    "avg_probability",
]

# Ограничения длины текста (Google Таблица / Notion).
RATIONALE_LIMIT_SHEETS = 2000
RATIONALE_LIMIT_NOTION = 1800


def window_4h_start(ts: datetime) -> datetime:
    """Начало непересекающегося 4-часового окна UTC, в которое попадает ``ts``.

    Эквивалент SQL:
    ``date_trunc('hour', ts) - (extract(hour from ts)::int % 4) * interval '1 hour'``.
    """
    ts_utc = ts.astimezone(UTC)
    truncated = ts_utc.replace(minute=0, second=0, microsecond=0)
    return truncated - timedelta(hours=truncated.hour % 4)


def iso_utc(ts: datetime | None) -> str:
    """ISO-8601 в UTC или пустая строка для ``None``."""
    if ts is None:
        return ""
    return ts.astimezone(UTC).isoformat()


def notified_cell(notified: bool | None, notified_at: datetime | None) -> str:
    """Значение колонки ``notified``: ``да`` / ``поглощён`` / ``нет`` (ТЗ 6.6.1 §9).

    * ``да`` — есть ``notified_at`` (уведомление реально ушло в Telegram);
    * ``поглощён`` — ``notified = TRUE``, но ``notified_at`` пуст (дубль/cooldown
      «поглощён» анти-спамом, отправки не было);
    * ``нет`` — сигнал ещё не обрабатывался notify.

    Историю до правки §5 не различаем: у старых сигналов ``notified=TRUE`` и
    ``notified_at=NULL`` → «поглощён». Это ожидаемо и не костыляется (ТЗ §7).
    """
    if notified_at is not None:
        return "да"
    if notified:
        return "поглощён"
    return "нет"


def success_cell(success: bool | None) -> str:
    """Значение колонок success_1h/4h: ``да`` / ``нет`` / пусто."""
    if success is None:
        return ""
    return "да" if success else "нет"


def _num(value: Any) -> Any:
    """Число как есть либо пустая строка для ``None`` (пустая ячейка, не 0)."""
    return "" if value is None else float(value)


def extract_agent_columns(agents_payload: Any) -> dict[str, Any]:
    """Раскладывает agents_payload по колонкам market/liquidity/futures.

    Возвращает словарь с ключами ``{agent}_signal`` и ``{agent}_confidence``.
    Если агента нет в payload — обе ячейки пустые (пустая строка), НЕ ноль:
    отсутствие агента (не набрал данных) — значимый факт, затирать его нельзя.
    """
    payload = _as_payload_list(agents_payload)
    by_agent = {
        entry.get("agent"): entry
        for entry in payload
        if isinstance(entry, dict) and entry.get("agent") is not None
    }
    columns: dict[str, Any] = {}
    for name in AGENT_ORDER:
        entry = by_agent.get(name)
        if entry is None:
            columns[f"{name}_signal"] = ""
            columns[f"{name}_confidence"] = ""
        else:
            columns[f"{name}_signal"] = entry.get("signal", "")
            columns[f"{name}_confidence"] = _num(entry.get("confidence"))
    return columns


def participating_agents(agents_payload: Any) -> list[str]:
    """Человекочитаемые метки агентов, реально участвовавших в решении."""
    payload = _as_payload_list(agents_payload)
    present = {
        entry.get("agent")
        for entry in payload
        if isinstance(entry, dict) and entry.get("agent") is not None
    }
    return [AGENT_LABELS[name] for name in AGENT_ORDER if name in present]


def _as_payload_list(agents_payload: Any) -> list[Any]:
    """Приводит agents_payload (JSON-строка / список / None) к списку."""
    if agents_payload is None:
        return []
    if isinstance(agents_payload, str):
        try:
            parsed = json.loads(agents_payload)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(agents_payload, list):
        return agents_payload
    return []


def _payload_json(agents_payload: Any) -> str:
    """Исходный JSON строкой без изменений (для колонки agents_payload_json)."""
    if agents_payload is None:
        return ""
    if isinstance(agents_payload, str):
        return agents_payload
    return json.dumps(agents_payload, ensure_ascii=False)


def build_signal_row(signal: dict[str, Any]) -> list[Any]:
    """Собирает одну строку листа «Сигналы» (27 колонок) из записи БД.

    ``signal`` — плоский словарь с полями сигнала и приджойненными оценками
    горизонтов 1h/4h (см. :mod:`src.export.queries`). Порядок и состав колонок
    строго соответствуют :data:`SIGNALS_HEADER`.
    """
    ts: datetime = signal["ts"]
    agents = extract_agent_columns(signal.get("agents_payload"))

    # price_at_signal — из горизонта 4h, при отсутствии — из 1h.
    price_at_signal = signal.get("p_signal_4h")
    if price_at_signal is None:
        price_at_signal = signal.get("p_signal_1h")

    probability = signal.get("probability")
    probability_cell = "" if probability is None else round(float(probability), 4)

    rationale = (signal.get("rationale") or "")[:RATIONALE_LIMIT_SHEETS]

    return [
        signal["id"],
        iso_utc(ts),
        ts.astimezone(_MSK).strftime("%Y-%m-%d %H:%M:%S"),
        iso_utc(window_4h_start(ts)),
        signal.get("token") or "",
        signal.get("decision") or "",
        probability_cell,
        notified_cell(signal.get("notified"), signal.get("notified_at")),
        iso_utc(signal.get("notified_at")),
        len(_as_payload_list(signal.get("agents_payload"))),
        agents["market_signal"],
        agents["market_confidence"],
        agents["liquidity_signal"],
        agents["liquidity_confidence"],
        agents["futures_signal"],
        agents["futures_confidence"],
        _num(price_at_signal),
        _num(signal.get("pnl_1h")),
        _num(signal.get("dd_1h")),
        success_cell(signal.get("succ_1h")),
        _num(signal.get("p_close_4h")),
        _num(signal.get("pnl_4h")),
        _num(signal.get("dd_4h")),
        success_cell(signal.get("succ_4h")),
        signal.get("status") or "",
        rationale,
        _payload_json(signal.get("agents_payload")),
    ]


def _rate(value: Any) -> Any:
    """Доля 0..1 с четырьмя знаками либо пустая ячейка для ``None``."""
    return "" if value is None else round(float(value), 4)


def build_summary_row(row: dict[str, Any]) -> list[Any]:
    """Собирает строку листа «Сводка по дням» (14 колонок) из агрегата БД.

    Пустые ячейки (``None``) остаются пустыми, а не заполняются нулём: «не было
    сигналов нужного типа» и «доля 0» — разные вещи.
    """
    day = row["day"]
    date_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return [
        date_str,
        int(row.get("decisions_total") or 0),
        int(row.get("buy") or 0),
        int(row.get("sell") or 0),
        int(row.get("wait") or 0),
        int(row.get("candidates") or 0),
        int(row.get("notified") or 0),
        int(row.get("closed_4h") or 0),
        _rate(row.get("sr_buy")),
        _rate(row.get("sr_sell")),
        _num(_round_or_none(row.get("avg_pnl_buy"), 4)),
        _num(_round_or_none(row.get("avg_pnl_sell"), 4)),
        _num(_round_or_none(row.get("avg_dd"), 4)),
        _rate(row.get("avg_prob")),
    ]


def _round_or_none(value: Any, digits: int) -> Any:
    """Округляет значение до ``digits`` знаков либо возвращает ``None``."""
    return None if value is None else round(float(value), digits)


def build_notion_properties(
    signal: dict[str, Any],
    database_id: str,
) -> dict[str, Any]:
    """Формирует ``properties`` страницы Notion по сигналу (§9.3).

    Имена свойств и типы строго соответствуют базе «Журнал сигналов» — менять
    нельзя. Опции select/multi_select уже существуют в базе, новые не создаются.
    """
    ts: datetime = signal["ts"]
    decision = signal.get("decision") or "wait"
    decision_ru = DECISION_RU.get(decision, decision)
    title = f"BTC · {decision_ru} · {ts.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC"

    probability = float(signal.get("probability") or 0.0)
    comment = (signal.get("rationale") or "")[:RATIONALE_LIMIT_NOTION]

    properties: dict[str, Any] = {
        "Сигнал": {"title": [{"text": {"content": title}}]},
        "Дата": {"date": {"start": ts.astimezone(UTC).isoformat()}},
        "Токен": {"select": {"name": signal.get("token") or "BTC"}},
        "Решение": {"select": {"name": decision_ru}},
        "Вероятность %": {"number": round(probability * 100, 1)},
        "Статус": {"status": {"name": "Done"}},
        "Источник агента": {
            "multi_select": [
                {"name": label}
                for label in participating_agents(signal.get("agents_payload"))
            ]
        },
        "Комментарий": {"rich_text": [{"text": {"content": comment}}]},
    }

    # Прибыль/Просадка/Успешность — по горизонту 4h; отсутствуют → свойство пустое.
    pnl_4h = signal.get("pnl_4h")
    if pnl_4h is not None:
        properties["Прибыль %"] = {"number": round(float(pnl_4h), 2)}
    dd_4h = signal.get("dd_4h")
    if dd_4h is not None:
        properties["Просадка %"] = {"number": round(float(dd_4h), 2)}
    succ_4h = signal.get("succ_4h")
    if succ_4h is not None:
        properties["Успешность"] = {"select": {"name": "Успех" if succ_4h else "Неудача"}}

    return properties
