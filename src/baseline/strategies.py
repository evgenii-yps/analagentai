"""Правила базовых стратегий (§4–§5 ТЗ 8.9).

ЗАЧЕМ ОНИ СУЩЕСТВУЮТ. Этап 8.8 показал, что продажи на 12 и 24 часах
прибыльны. Но двое суток рынок падал, а на падающем рынке выигрывает ЛЮБОЙ
сигнал на продажу — включая брошенную монету. Числа системы не с чем
сравнивать, и отличить умение от погоды нечем. Здесь задаются правила, которые
заведомо не содержат никакого умения: если система их не обгоняет, вопрос о
весах агентов теряет смысл.

Модуль ЧИСТЫЙ: ни базы, ни сети, ни времени «сейчас». Правило исхода здесь НЕ
ПОВТОРЯЕТСЯ — оно берётся из ``src.barrier.outcomes`` как есть (§2 ТЗ). Любое
расхождение в правилах сделало бы сравнение недействительным, поэтому второй
реализации того же правила в проекте не появляется.

ШЕСТЬ СТРАТЕГИЙ, И ОНИ РАЗНОГО РОДА:

  always_buy, always_sell, coin_flip, system — привязаны к моментам, когда
  система выдала сигнал. Они отвечают на вопрос «в тех же обстоятельствах
  безмозглое правило справилось бы хуже?».

  grid_buy, grid_sell — вход каждый час независимо от системы. Они отвечают на
  ДРУГОЙ вопрос: «а каков вообще фон рынка?». Он нужен потому, что система
  выдаёт сигналы неравномерно — например, чаще при высокой волатильности, — и
  сравнение только по её моментам этого перекоса не видит.

МОНЕТА ОБЯЗАНА БЫТЬ ВОСПРОИЗВОДИМОЙ. Случайность, которую нельзя повторить, для
сравнения непригодна: два прогона дали бы два разных ответа, и ни один нельзя
было бы проверить. Поэтому направление не берётся из ``random``, а вычисляется
как функция от (зерно, сигнал, горизонт) — та же тройка всегда даёт то же
направление, на любой машине и в любой версии Python.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from src.barrier.outcomes import BUY, SELL

# --- Ключи стратегий. Перечень ЗАКРЫТ и повторён ограничением БД (016). ---
ALWAYS_BUY = "always_buy"
ALWAYS_SELL = "always_sell"
COIN_FLIP = "coin_flip"
SYSTEM = "system"
GRID_BUY = "grid_buy"
GRID_SELL = "grid_sell"

# Стратегии, привязанные к моментам сигналов системы (§4).
SIGNAL_STRATEGIES = (ALWAYS_BUY, ALWAYS_SELL, COIN_FLIP, SYSTEM)
# Стратегии сетки, к системе не привязанные (§5).
GRID_STRATEGIES = (GRID_BUY, GRID_SELL)
STRATEGIES = SIGNAL_STRATEGIES + GRID_STRATEGIES

# Источник цели (§6). Второй вид — 'risk_targets:<ГГГГ-ММ-ДД>'.
SOURCE_FROZEN = "frozen"
SOURCE_RISK_PREFIX = "risk_targets:"


def risk_target_source(computed_at: datetime) -> str:
    """Подпись источника цели для исторической строки ``risk_targets``.

    Дата, а не полная метка времени: колонка отвечает на вопрос «за какой день
    взята цель», и секунды в ней создавали бы вид точности, которого у сути
    вопроса нет. Формат закреплён ограничением БД.
    """
    return f"{SOURCE_RISK_PREFIX}{computed_at.date().isoformat()}"


def coin_flip_direction(seed: int, signal_id: int, horizon_h: int) -> str:
    """Направление монеты — детерминированная функция от (зерно, сигнал, горизонт).

    ПОЧЕМУ НЕ ``random``. Модуль ``random``, засеянный один раз на прогон, даёт
    воспроизводимую ПОСЛЕДОВАТЕЛЬНОСТЬ, но не воспроизводимое СООТВЕТСТВИЕ:
    стоит изменить порядок обхода сигналов, добавить один сигнал в середину или
    посчитать горизонты в другом порядке — и та же монета ляжет на другие
    сигналы. Здесь же направление привязано к самой тройке, поэтому не зависит
    ни от порядка обхода, ни от состава выборки, ни от числа прогонов.

    ``sha256`` взят вместо встроенного ``hash()`` намеренно: ``hash()`` для строк
    рандомизируется от запуска к запуску (PYTHONHASHSEED), и монета переставала
    бы быть воспроизводимой ровно тем способом, который эта функция должна
    исключить.
    """
    payload = f"{seed}:{signal_id}:{horizon_h}".encode()
    digest = hashlib.sha256(payload).digest()
    return BUY if digest[0] % 2 == 0 else SELL


def direction_for(
    strategy: str,
    *,
    signal_direction: str | None = None,
    seed: int | None = None,
    signal_id: int | None = None,
    horizon_h: int | None = None,
) -> str:
    """Направление, которое стратегия заняла бы в этот момент."""
    if strategy in (ALWAYS_BUY, GRID_BUY):
        return BUY
    if strategy in (ALWAYS_SELL, GRID_SELL):
        return SELL
    if strategy == SYSTEM:
        if signal_direction not in (BUY, SELL):
            raise ValueError(
                f"стратегия system требует направления сигнала: {signal_direction!r}"
            )
        return signal_direction
    if strategy == COIN_FLIP:
        if seed is None or signal_id is None or horizon_h is None:
            raise ValueError("монете нужны зерно, сигнал и горизонт")
        return coin_flip_direction(seed, signal_id, horizon_h)
    raise ValueError(f"неизвестная стратегия: {strategy}")


def hourly_grid_entries(
    start: datetime, end: datetime
) -> list[datetime]:
    """Моменты входа сетки: каждый час ровно в 00 минут, ``[start, end]``.

    Начало округляется ВВЕРХ до целого часа, а не вниз: момент до ``start``
    в окно наблюдения не входит, и вход в него означал бы вход раньше, чем
    начались данные.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("границы сетки обязаны быть с часовым поясом")
    first = start.replace(minute=0, second=0, microsecond=0)
    if first < start:
        first = first + timedelta(hours=1)
    out: list[datetime] = []
    current = first
    while current <= end:
        out.append(current)
        current = current + timedelta(hours=1)
    return out
