"""Скользящее часовое окно отправок — ОДНА реализация на две службы (§6.3 ТЗ 9.3).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. §6.3 ТЗ требует буквально: «Логика ограничителя
переиспользуется из службы уведомлений, а не пишется заново». До этапа 9.3
она жила приватными методами ``NotifyAgent._sent_last_hour`` и
``NotifyAgent._record_sent``; службе позиций, которая шлёт сообщения о сделках,
дотянуться до них было нечем — оставалось скопировать. Скопированный
ограничитель — это два места, где однажды разойдётся смысл слова «час», и
разойдётся молча: поток сообщений не падает, он просто перестаёт совпадать с
настройкой.

ПОЧЕМУ ОКНО СКОЛЬЗЯЩЕЕ, А НЕ СЧЁТЧИК С ОБНУЛЕНИЕМ В НАЧАЛЕ ЧАСА. Это прямой
урок этапа 8.3, повторённый §5.2 ТЗ 9.3 уже про паузу по токену: при обнулении
в начале часа шесть сообщений в 10:59 и ещё шесть в 11:01 формально
укладываются в «потолок 6 в час», а человек получает двенадцать за две минуты.
Хранится поэтому упорядоченное множество Redis: элемент — момент отправки, вес
— он же в секундах.

НЕДОСТУПНОСТЬ REDIS НЕ ЗАТЫКАЕТ ПОТОК. При ошибке чтения считается ноль: в этот
момент потолок не действует. Выбор осознанный и несимметричный — «ошибка Redis
= потолок исчерпан» означала бы, что владелец перестаёт узнавать о сделках
из-за сбоя вспомогательного хранилища.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from src.core.redis_client import get_redis

_log = structlog.get_logger().bind(component="notify-rate-limit")

# Срок жизни ключа заведомо больше окна: чистка идёт по весу, а срок нужен
# только чтобы ключ не оставался навсегда после остановки службы.
_KEY_TTL_SEC = 7200
_WINDOW_SEC = 3600


async def sent_last_hour(key: str, now: datetime, enabled: bool = True) -> int:
    """Сколько сообщений ушло по этому ключу за последний час.

    ``enabled=False`` (потолок выключен) — ноль без обращения к Redis:
    спрашивать хранилище о числе, которое ни на что не влияет, незачем.
    """
    if not enabled:
        return 0
    try:
        redis = get_redis()
        await redis.zremrangebyscore(key, "-inf", now.timestamp() - _WINDOW_SEC)
        return int(await redis.zcard(key))
    except Exception as exc:  # noqa: BLE001 — счётчик не важнее сообщения
        _log.warning("notify_rate_read_failed=1", key=key, error=str(exc))
        return 0


async def record_sent(key: str, now: datetime) -> None:
    """Отмечает факт отправки в скользящем окне часа."""
    try:
        redis = get_redis()
        await redis.zadd(key, {now.isoformat(): now.timestamp()})
        await redis.expire(key, _KEY_TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — счётчик не важнее сообщения
        _log.warning("notify_rate_write_failed=1", key=key, error=str(exc))
