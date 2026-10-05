"""Прогноз заполнения диска по суточным записям (Этап 9.4, блок E).

Общий модуль вотчдога (``scripts/watchdog.py``) и суточной сводки
(``src/health/daily_report.py``): оценка «до порога осталось N суток» обязана
быть ОДНОЙ И ТОЙ ЖЕ в обоих местах, второй реализации расчёта быть не должно.

Источник — ``logs/disk_usage.csv``: по строке в сутки, формат
``дата;занято_ГБ;свободно_процент`` (без заголовка). Прирост считается по
последним 14 записям как разность занятого места, делённая на число суток между
первой и последней датой (а не на число записей: пропущенные сутки не должны
завышать прирост).

Только стандартная библиотека.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

WINDOW_RECORDS = 14   # сколько последних записей берётся для среднего прироста
MIN_RECORDS = 7       # меньше — оценку не печатаем

NO_DATA_TEXT = "недостаточно данных"


@dataclass(frozen=True)
class DiskRecord:
    """Одна суточная запись: дата, занято (ГБ), свободно (%)."""

    day: date
    used_gb: float
    free_pct: float


def parse_line(line: str) -> DiskRecord | None:
    """Разбирает строку CSV. Мусорную строку пропускает (None), а не падает."""
    parts = line.strip().split(";")
    if len(parts) != 3:
        return None
    try:
        return DiskRecord(date.fromisoformat(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return None


def read_records(path: str) -> list[DiskRecord]:
    """Все разобранные записи файла по порядку. Нет файла — пустой список."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        records = [r for r in (parse_line(line) for line in fh) if r is not None]
    return records


def append_daily(path: str, today: date, used_gb: float, free_pct: float) -> bool:
    """Дописывает строку за ``today``, если за эту дату записи ещё нет.

    Вотчдог запускается каждые 10 минут, а запись нужна одна в сутки, поэтому
    проверяется дата последней строки. Возвращает True, если строка дописана.
    """
    records = read_records(path)
    if records and records[-1].day >= today:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{today.isoformat()};{used_gb:.2f};{free_pct:.2f}\n")
    return True


def daily_growth_gb(records: list[DiskRecord]) -> float | None:
    """Средний суточный прирост занятого места, ГБ. None — данных мало.

    По последним :data:`WINDOW_RECORDS` записям. Меньше :data:`MIN_RECORDS`
    записей или все они на одну дату — None.
    """
    window = records[-WINDOW_RECORDS:]
    if len(window) < MIN_RECORDS:
        return None
    span_days = (window[-1].day - window[0].day).days
    if span_days <= 0:
        return None
    return (window[-1].used_gb - window[0].used_gb) / span_days


def days_to_threshold(
    records: list[DiskRecord], capacity_gb: float, crit_free_pct: float
) -> float | None:
    """Через сколько суток свободного места останется ``crit_free_pct`` процентов.

    ``capacity_gb`` — объём диска в тех же единицах, что и занятое место.
    Возвращает: ``None`` — данных недостаточно; ``0.0`` — порог уже пройден;
    ``inf`` — прироста нет (диск не растёт), срок не определён; иначе число суток.
    """
    growth = daily_growth_gb(records)
    if growth is None:
        return None
    used_now = records[-1].used_gb
    used_at_threshold = capacity_gb * (1.0 - crit_free_pct / 100.0)
    remaining = used_at_threshold - used_now
    if remaining <= 0:
        return 0.0
    if growth <= 0:
        return float("inf")
    return remaining / growth


def describe(days: float | None, crit_free_pct: float) -> str:
    """Текст оценки для сообщений вотчдога и суточной сводки."""
    if days is None:
        return NO_DATA_TEXT
    if days == 0.0:
        return f"порог {crit_free_pct:.0f}% уже пройден"
    if days == float("inf"):
        return f"до порога {crit_free_pct:.0f}%: прироста нет, срок не определён"
    return f"до порога {crit_free_pct:.0f}% осталось {days:.0f} суток"
