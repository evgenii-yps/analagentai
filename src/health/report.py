"""Чистые функции суточной сводки: формат строк и расчёт возраста heartbeat.

Ввод-вывод (БД/Redis/Telegram) вынесен в ``scripts/daily_report.py``; здесь —
только детерминированная логика, покрываемая юнит-тестами.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def clamp_age_seconds(last_seen: datetime | None, now: datetime) -> float | None:
    """Возраст heartbeat в секундах, отрицательные значения приводятся к нулю.

    Часы контейнера и хост-скрипта могут расходиться, из-за чего наивная
    разница даёт «-1 сек назад» (§13.3). Возвращает ``None``, если отметки нет.
    """
    if last_seen is None:
        return None
    age = (now - last_seen).total_seconds()
    return max(0.0, age)


def format_daily_report(stats: dict[str, Any]) -> str:
    """Собирает текст суточной сводки.

    Ключевая правка §5: «Отправлено уведомлений» считается по ``notified_at``
    (реальные отправки), а «Кандидатов» — по прежнему порогу вероятности.
    Расхождение цифр показывает работу анти-спама.
    """
    lines = [
        "📊 Суточная сводка Agent Trade",
        f"Решений всего: {stats.get('decisions_total', 0)}",
        f"  buy: {stats.get('buy', 0)}, sell: {stats.get('sell', 0)}, "
        f"wait: {stats.get('wait', 0)}",
        f"Отправлено уведомлений: {stats.get('notified', 0)}",
        f"Кандидатов (вероятность ≥ порога): {stats.get('candidates', 0)}",
        f"Закрыто сигналов (4ч): {stats.get('closed', 0)}",
    ]

    heartbeats = stats.get("heartbeats") or {}
    if heartbeats:
        lines.append("Heartbeat (сек назад):")
        for name, age in heartbeats.items():
            shown = "нет данных" if age is None else f"{int(age)}"
            lines.append(f"  {name}: {shown}")

    return "\n".join(lines)
