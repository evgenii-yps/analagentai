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
    # Этап 7.3 §4.7: сама колонка и её позиция сохранены (иначе поедут все
    # выгруженные ранее строки), меняется только ПОДПИСЬ: это индекс согласия,
    # а не вероятность.
    "индекс согласия",
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
    "logic_version",
    "degraded",
    # Этап 7.3: новые колонки добавляются В КОНЕЦ тем же приёмом, что и раньше.
    "calibrated_probability",
    "calibration_id",
    "inputs_hash",
    "is_repeat",
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
    "avg_conviction",
    "logic_version_dominant",
    "degraded_count",
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


# --- Лист «Независимые окна» (Этап 8.1 §7) ---------------------------------

# Оговорка выводится ПЕРВОЙ строкой листа и обязана быть в нём всегда (§7 ТЗ 8.1):
# без неё число наблюдений читается как пятикратно выросшая мощность, чем оно
# не является.
INDEPENDENT_DISCLAIMER: list[Any] = [
    "ВНИМАНИЕ: пять токенов НЕ дают пятикратного роста статистической мощности. "
    "Криптовалюты сильно коррелированы: одно и то же движение рынка попадает в "
    "наблюдения по всем токенам сразу. Фактическая корреляция исходов между "
    "токенами приведена в колонке «корреляция с другими токенами» и на листе "
    "«Корреляция токенов»; эффективное число независимых наблюдений МЕНЬШЕ "
    "формального."
]

INDEPENDENT_HEADER: list[str] = [
    "горизонт_ч",
    "токен",
    "окно_utc",
    "signal_id",
    "ts_utc",
    "decision",
    "индекс согласия",
    "price_at_signal",
    "price_at_close",
    "pnl_pct",
    "drawdown_pct",
    "success",
    "logic_version",
    "degraded",
    "корреляция с другими токенами",
]

CORRELATION_HEADER: list[str] = [
    "горизонт_ч", "токен_a", "токен_b", "N совпавших окон", "корреляция исходов r",
    # §9.4 ТЗ 8.2. Колонки версии здесь не было вовсе, и корреляция исходов
    # считалась ПОПЕРЁК версий логики так, что увидеть это по листу было
    # невозможно. Значение «смешано» означает ровно то, что написано.
    "logic_version",
]

# Оговорка первой строкой листа при EXPORT_LOGIC_VERSION=all (§9.3 ТЗ 8.2).
# Прямая, а не сноской внизу: читатель обязан узнать о смешивании раньше, чем
# увидит первое число.
MIXED_VERSIONS_DISCLAIMER: list[Any] = [
    "ВНИМАНИЕ: лист собран при EXPORT_LOGIC_VERSION=all и СМЕШИВАЕТ РАЗНЫЕ "
    "ВЕРСИИ ЛОГИКИ. Сигналы разных версий несравнимы между собой: у них разный "
    "состав агентов, разные пороги и разный набор горизонтов. Сравнивать доли "
    "попаданий и корреляции по такому листу нельзя; версия каждой строки — в "
    "колонке logic_version."
]


def build_independent_row(
    signal: dict[str, Any],
    correlation: float | None = None,
) -> list[Any]:
    """Строка листа «Независимые окна»: одно наблюдение (токен × горизонт).

    ``correlation`` — средняя корреляция исходов этого токена с остальными на
    том же горизонте. Значение считается ежесуточно вместе с выгрузкой и
    выводится рядом с наблюдением, чтобы «много строк» не читалось как «много
    независимой информации».
    """
    ts: datetime = signal["ts"]
    probability = signal.get("probability")
    return [
        signal.get("horizon_h"),
        signal.get("token") or "",
        iso_utc(signal.get("win")),
        signal["id"],
        iso_utc(ts),
        signal.get("decision") or "",
        "" if probability is None else round(float(probability), 4),
        _num(signal.get("h_price_at_signal")),
        _num(signal.get("h_price_at_close")),
        _num(signal.get("h_pnl_pct")),
        _num(signal.get("h_drawdown_pct")),
        success_cell(signal.get("h_success")),
        signal.get("logic_version"),
        "да" if signal.get("degraded") else "нет",
        "" if correlation is None else round(float(correlation), 3),
    ]


def build_correlation_row(row: dict[str, Any]) -> list[Any]:
    """Строка листа «Корреляция токенов» (с версией логики, §9.4 ТЗ 8.2)."""
    r = row.get("r")
    return [
        row.get("horizon_h"),
        row.get("token_a") or "",
        row.get("token_b") or "",
        int(row.get("n") or 0),
        "" if r is None else round(float(r), 3),
        row.get("logic_version") if row.get("logic_version") is not None else "",
    ]


def mean_correlation_by_token(
    rows: list[dict[str, Any]]
) -> dict[tuple[int, str], float]:
    """Средняя корреляция исходов каждого токена с остальными: (горизонт, токен) → r.

    Пары считаются один раз (a < b), поэтому каждая строка учитывается для обоих
    токенов пары. Пустые значения (нет совпавших окон) пропускаются, а не
    заменяются нулём: «корреляции не измерено» и «корреляция равна нулю» —
    разные утверждения.
    """
    sums: dict[tuple[int, str], list[float]] = {}
    for row in rows:
        r = row.get("r")
        if r is None:
            continue
        horizon = int(row.get("horizon_h") or 0)
        for token in (row.get("token_a"), row.get("token_b")):
            if not token:
                continue
            sums.setdefault((horizon, str(token)), []).append(float(r))
    return {key: sum(values) / len(values) for key, values in sums.items()}


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
        # Версия логики последней колонкой, чтобы не сдвигать существующие (§D.3).
        int(signal["logic_version"]) if signal.get("logic_version") is not None else 1,
        # degraded новой колонкой в самом конце — тем же приёмом, чтобы не сдвигать
        # уже выгруженные столбцы (Этап 7.2, Задача A2). «да»/«нет».
        "да" if signal.get("degraded") else "нет",
        # Этап 7.3. Пустая ячейка вместо вероятности означает «кривой ещё нет»:
        # подставлять сюда индекс согласия нельзя — это разные величины.
        _rate(signal.get("calibrated_probability")),
        signal.get("calibration_id") if signal.get("calibration_id") is not None else "",
        signal.get("inputs_hash") or "",
        "да" if signal.get("is_repeat") else "нет",
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
        # Преобладающая версия логики за сутки (§D.3).
        int(row["logic_version_dominant"]) if row.get("logic_version_dominant") is not None else 1,
        # Число деградированных циклов за сутки (Этап 7.2, Задача A2).
        int(row.get("degraded_count") or 0),
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

    # В базе Notion свойство называется «Вероятность %» и переименованию не
    # подлежит (это поле базы, а не текст интерфейса). Чтобы не выдавать индекс
    # согласия за вероятность, в комментарий добавляется явная расшифровка, а
    # калиброванная вероятность выгружается отдельным числовым свойством.
    conviction = float(signal.get("probability") or 0.0)
    comment = (signal.get("rationale") or "")[:RATIONALE_LIMIT_NOTION]

    properties: dict[str, Any] = {
        "Сигнал": {"title": [{"text": {"content": title}}]},
        "Дата": {"date": {"start": ts.astimezone(UTC).isoformat()}},
        "Токен": {"select": {"name": signal.get("token") or "BTC"}},
        "Решение": {"select": {"name": decision_ru}},
        "Вероятность %": {"number": round(conviction * 100, 1)},
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

    # Этап 7.3: калиброванная вероятность и признак повтора входов дописываются
    # в текст комментария — новые свойства в базе Notion этим этапом не создаются.
    marks: list[str] = []
    calibrated = signal.get("calibrated_probability")
    if calibrated is not None:
        marks.append(
            f"вероятность успеха (по истории) {round(float(calibrated) * 100, 1)}%"
        )
    marks.append(
        "повтор входов" if signal.get("is_repeat") else "новый набор входов"
    )
    suffix = " | " + "; ".join(marks)
    properties["Комментарий"] = {
        "rich_text": [
            {"text": {"content": (comment + suffix)[:RATIONALE_LIMIT_NOTION]}}
        ]
    }
    return properties

# ===========================================================================
# ЭТАП 9.1.2. Торговый журнал: строки сделок в листе «торговля тест апи окх
# чтение»
# ===========================================================================
#
# ЧТО ЗДЕСЬ ПРОИСХОДИТ И ЧЕГО НЕ ПРОИСХОДИТ. Система поставляет в лист ТОЛЬКО
# ФАКТЫ: время, токен, цены, объём — плюс свой итог в заметке. Прибыль, доходы
# и комиссии считают ФОРМУЛЫ ВЛАДЕЛЬЦА в столбцах K и правее; ни одна из них не
# переписывается и ни одно наше число в них не подставляется.
#
# СТРОКА ЖИВЁТ ДВА ЭТАПА. При открытии пишутся столбцы A–G — сделка видна в
# листе, пока она ещё идёт. При закрытии в ТУ ЖЕ строку дозаписываются H, I, J.
# Поэтому и функций здесь четыре, а не одна: открытие и закрытие — два разных
# события, и склеить их в одну «строку целиком» значило бы показывать сделку
# только после её конца.

# Русские названия причин выхода для заметки. Ключи — те же машиночитаемые
# значения, что лежат в positions.exit_reason (ограничение positions_reason_chk,
# миграции 018 и 019); человеку показывается перевод, запросу — ключ.
POSITION_EXIT_RU: dict[str, str] = {
    "target": "цель достигнута",
    "stop": "сработал предел убытка",
    "timeout": "истёк срок",
    "ambiguous": "задеты обе границы",
    "data_gap": "пробел в данных",
}

# Текст вместо итога у закрытий по пробелу в данных. Их результат НЕ ИЗМЕРЕН:
# цена выхода не наблюдалась, а восстановлена по последней известной свече.
# Печатать рядом с ней «итог системы +0.28%» значило бы выдать восстановленное
# число за измеренное — и оно попало бы в сверку с листом наравне с настоящими.
DATA_GAP_NOTE = (
    "цена выхода восстановлена по последней известной свече, не наблюдалась, "
    "в статистику точности не идёт"
)

# Значение столбца D. Спот: продажа — это продажа того, чего нет, поэтому
# значение ровно одно и оно же записано ограничением positions_side_chk.
POSITION_SIDE_RU: dict[str, str] = {"buy": "покупать"}

# Столбцы записи (номера, с единицы). Открытие пишет A–G одним диапазоном,
# закрытие — H, I, J диапазоном с восьмого столбца. Столбцы K и правее — формулы
# владельца, и ни один из двух диапазонов их не задевает.
POSITION_OPEN_WIDTH = 7
POSITION_CLOSE_START_COLUMN = 8
POSITION_CLOSE_WIDTH = 3
# Заметка — столбец T (20-й). Он лежит за формулами и служит и подписью сделки,
# и ЯКОРЕМ для дозаписи: другого способа найти строку в листе, который владелец
# правит руками, не существует.
POSITION_NOTE_COLUMN = 20
# Метка строки. Обязана стоять В НАЧАЛЕ заметки; вокруг неё владелец волен
# дописывать что угодно, но саму метку трогать нельзя — по ней ищется строка.
POSITION_MARKER_TEMPLATE = "[поз. {id}]"
# Первый столбец формул листа (K). Приёмник протягивает формулы начиная с него.
POSITION_FORMULA_FROM_COLUMN = 11
# Текст, по которому ищется строка итогов: ниже неё писать нельзя.
POSITION_TOTALS_MARKER = "итого:"


def position_marker(position_id: Any) -> str:
    """Метка строки позиции: ``[поз. 123]``. Ищется приёмником подстрокой."""
    return POSITION_MARKER_TEMPLATE.format(id=int(position_id))


def _position_tz(timezone_name: str) -> ZoneInfo:
    """Пояс отображения по имени. Неизвестное имя — ошибка, а не откат к UTC.

    Тихий откат к UTC сдвинул бы все четыре поля времени на три часа, и строка
    листа разошлась бы с сообщением бота, оставаясь при этом правдоподобной.
    """
    return ZoneInfo(timezone_name)


def position_date(ts: datetime, timezone_name: str) -> str:
    """Дата в поясе отображения: ``31.08.2026``."""
    return ts.astimezone(_position_tz(timezone_name)).strftime("%d.%m.%Y")


def position_time(ts: datetime, timezone_name: str) -> str:
    """Время в поясе отображения: ``20:34:12``."""
    return ts.astimezone(_position_tz(timezone_name)).strftime("%H:%M:%S")


def position_token(symbol: Any) -> str:
    """``XRP/USDT`` → ``XRP``. В листе стоит короткое имя, а не пара."""
    return str(symbol).split("/", 1)[0]


def _position_price(value: Any) -> str:
    """Цена в заметке. Знаков — по величине: у DOGE их нужно больше.

    Два знака у дорогих инструментов и шесть у копеечных — не украшение:
    ``0.21`` вместо ``0.214370`` у DOGE прячет ровно тот масштаб движения, в
    котором эта позиция и живёт.
    """
    number = float(value)
    digits = 2 if abs(number) >= 100 else (4 if abs(number) >= 1 else 6)
    return f"{number:.{digits}f}"


def build_position_open_row(row: dict[str, Any], timezone_name: str) -> list[Any]:
    """Строка открытия: РОВНО СЕМЬ значений, столбцы A–G (§2 ТЗ 9.1.2).

    ЧИСЛА ОСТАЮТСЯ ЧИСЛАМИ. Цена и объём уходят числами JSON (точка как
    разделитель), а не строками: запятая, знак доллара и разрядные пробелы — это
    ФОРМАТ ЯЧЕЙКИ, а не содержимое. Число, отправленное строкой, ляжет текстом,
    будет выглядеть точно так же и тихо сломает все формулы, которые на него
    ссылаются.

    СТОЛБЕЦ E ПУСТ НАМЕРЕННО: в листе это столбец-разделитель. Пропустить его
    нельзя — диапазон записи сплошной, и без пустого места всё правее съехало бы
    на одну клетку.

    СТОЛБЕЦ G — ФАКТИЧЕСКИЙ РАЗМЕР СЛОТА, а не цепочка «объём = выход
    предыдущей сделки». В листе-образце там была цепочка: один кошелёк, прибыль
    реинвестируется. Система устроена иначе — пять одновременных слотов по два
    доллара, прибыль не реинвестируется (§5.1 ТЗ 9.1.1). Позиции по разным
    токенам идут внахлёст во времени, и сложить их в одну цепь значило бы
    показать доходность несуществующего счёта: числа получились бы
    правдоподобные, а это худший вид ошибки.
    """
    side = str(row["side"])
    if side not in POSITION_SIDE_RU:
        # НЕИЗВЕСТНОЕ НАПРАВЛЕНИЕ — ОШИБКА, А НЕ ПУСТАЯ ЯЧЕЙКА. Пустая ячейка в
        # столбце «сигнал» выглядит как «забыли заполнить» и живёт в листе
        # вечно; исключение видно сразу и в тот же прогон.
        raise ValueError(
            f"неизвестное направление позиции {side!r}: в листе столбец «сигнал» "
            f"знает только {sorted(POSITION_SIDE_RU)}"
        )
    return [
        position_date(row["opened_at"], timezone_name),
        position_time(row["signal_ts"], timezone_name),
        position_token(row["symbol"]),
        POSITION_SIDE_RU[side],
        "",
        float(row["entry_price"]),
        float(row["notional_usd"]),
    ]


def build_position_note(row: dict[str, Any]) -> str:
    """Заметка при открытии. НАЧИНАЕТСЯ С МЕТКИ ``[поз. <id>]`` (§2.1 ТЗ).

    Метка — единственный способ найти строку при дозаписи закрытия: лист
    владелец правит руками, строки могут переехать, а искать сделку по дате и
    цене значило бы однажды дописать выход не в ту строку.
    """
    probability = row.get("probability")
    prob = "—" if probability is None else f"{float(probability):.2f}"
    return (
        f"{position_marker(row['id'])} "
        f"цель {_position_price(row['target_price'])} "
        f"(+{float(row['target_pct']):.2f}%) · "
        f"предел {_position_price(row['stop_price'])} "
        f"(\u2212{float(row['stop_pct']):.2f}%) · "
        f"сигнал #{int(row['signal_id'])} · "
        f"вероятность {prob} · "
        f"задержка входа {int(row['entry_lag_sec'])} с"
    )


def build_position_close_values(
    row: dict[str, Any], timezone_name: str
) -> list[Any]:
    """Дозапись закрытия: РОВНО ТРИ значения, столбцы H, I, J (§2 ТЗ).

    Диапазон начинается с восьмого столбца и имеет ширину три — столбцы K и
    правее не задеваются ни одной ячейкой. Цена выхода, как и цена входа, —
    число, а не строка.
    """
    return [
        position_date(row["closed_at"], timezone_name),
        position_time(row["closed_at"], timezone_name),
        float(row["exit_price"]),
    ]


def build_position_close_note(
    row: dict[str, Any], timezone_name: str | None = None
) -> str:
    """Полный текст заметки после закрытия: открытие + причина + итог (§2.1 ТЗ).

    ЗАМЕТКА ПЕРЕПИСЫВАЕТСЯ ЦЕЛИКОМ, а не дополняется на стороне листа: приёмник
    не умеет и не должен уметь дописывать в конец чужой ячейки — владелец мог
    добавить вокруг метки свой текст, и «дописать в конец» означало бы угадывать,
    где кончается наш текст и начинается его. Метка при этом остаётся первой.

    У ЗАКРЫТИЙ ПО ПРОБЕЛУ В ДАННЫХ ВМЕСТО ИТОГА СТОИТ ОГОВОРКА. Их результат не
    измерен: цена выхода не наблюдалась, а восстановлена по последней известной
    свече. Печатать рядом с ней «итог системы +0.28%» значило бы выдать
    восстановленное число за измеренное — и оно попало бы в сверку с листом
    наравне с настоящими.

    ``timezone_name`` принимается для единообразия вызова с остальными тремя
    сборщиками и в тексте не участвует: в заметке закрытия нет ни одного поля
    времени — дата и время выхода стоят в столбцах H и I.
    """
    reason = str(row["exit_reason"])
    words = POSITION_EXIT_RU.get(reason, reason)
    head = f"{build_position_note(row)} · {words}"
    if reason == "data_gap":
        return f"{head} · {DATA_GAP_NOTE}"
    return (
        f"{head} · итог системы {float(row['net_pnl_pct']):+.2f}% "
        f"(${float(row['net_pnl_usd']):+.3f}) "
        f"с учётом издержек {float(row['cost_pct']):.2f}%"
    )


def build_position_full_row(
    row: dict[str, Any], timezone_name: str
) -> list[Any]:
    """Полная строка A–J одним куском — ТОЛЬКО для потерянной строки открытия.

    Приёмник не нашёл метку ``[поз. <id>]`` в листе: строку открытия удалили,
    или её никогда не было. Угадывать, какая из строк листа принадлежит этой
    сделке, нельзя — дописать выход не в ту строку хуже, чем не дописать вовсе.
    Поэтому сделка кладётся новой полной строкой, а заметка начинается прямыми
    словами о том, что произошло. ПОТЕРЯННАЯ СДЕЛКА ХУЖЕ ЛИШНЕЙ СТРОКИ.
    """
    return [
        *build_position_open_row(row, timezone_name),
        *build_position_close_values(row, timezone_name),
    ]


def build_position_orphan_note(
    row: dict[str, Any], timezone_name: str | None = None
) -> str:
    """Заметка потерянной строки: начинается прямыми словами о случившемся."""
    return (
        "строка открытия не найдена — сделка записана целиком новой строкой; "
        f"{build_position_close_note(row, timezone_name)}"
    )
