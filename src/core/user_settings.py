"""Настройки уведомлений пользователя и правило отбора (§1, §2 ТЗ 8.3).

Модуль сознательно не знает ни про базу, ни про Telegram: всё состояние
приходит параметрами, все функции чистые. Отбор уведомлений — это то место,
где ошибка не видна: человек просто не получает сигнал и считает, что система
молчит по существу. Поэтому правило отбора должно быть проверяемо тестами без
базы, без сети и без времени «сейчас», взятого изнутри.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Значения по умолчанию (§1 ТЗ 8.3). Действуют, когда записи в user_settings
# НЕТ: человек мог ни разу не открыть меню, и это не ошибка. Строка в базу при
# первом же сигнале не создаётся — молчаливая запись настроек, которых человек
# не задавал, потом выглядела бы как его собственный выбор.
DEFAULT_HORIZON_H = 4
# None означает «все инструменты», а не пустой список. Список пришлось бы
# пересобирать при добавлении токена, и старые пользователи не увидели бы
# новый; None остаётся верным всегда.
DEFAULT_INSTRUMENTS: tuple[int, ...] | None = None


# DDL таблицы настроек в ОДНОМ месте. Её создают и гарантируют три разных
# сервиса (бот пишет, уведомления читают, миграция применяется вручную), а
# порядок их старта не задан. Три копии текста рано или поздно разъехались бы —
# и разъехались бы молча, потому что CREATE TABLE IF NOT EXISTS существующую
# таблицу не меняет.
USER_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS user_settings (
    chat_id     BIGINT       PRIMARY KEY,
    instruments INTEGER[]    NOT NULL
        CHECK (array_length(instruments, 1) >= 1),
    horizon_h   SMALLINT     NOT NULL DEFAULT 4 CHECK (horizon_h > 0),
    min_score   NUMERIC(4,3) NOT NULL DEFAULT 0.700,
    quiet_from  SMALLINT,
    quiet_to    SMALLINT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT user_settings_quiet_hours_valid CHECK (
        (quiet_from IS NULL AND quiet_to IS NULL)
        OR (quiet_from BETWEEN 0 AND 23 AND quiet_to BETWEEN 0 AND 23)
    )
);
"""


@dataclass(frozen=True)
class UserSettings:
    """Настройки одного чата. ``instruments = None`` — все инструменты."""

    chat_id: int
    instruments: tuple[int, ...] | None = DEFAULT_INSTRUMENTS
    horizon_h: int = DEFAULT_HORIZON_H
    min_score: float = 0.7
    quiet_from: int | None = None
    quiet_to: int | None = None


def default_settings(chat_id: int, min_score: float) -> UserSettings:
    """Настройки по умолчанию: все токены, горизонт 4 ч, порог из конфигурации.

    Порог берётся из настройки сервиса (``NOTIFY_MIN_PROBABILITY``), а не из
    константы: значение по умолчанию должно совпадать с тем, по которому
    сигналы вообще отбираются к отправке, иначе меню показывало бы одно, а
    система применяла другое.
    """
    return UserSettings(chat_id=chat_id, min_score=float(min_score))


def wants_instrument(settings: UserSettings, instrument_id: int) -> bool:
    """Выбран ли инструмент. ``None`` — выбраны все."""
    if settings.instruments is None:
        return True
    return int(instrument_id) in settings.instruments


def is_quiet_hour(settings: UserSettings, now: datetime) -> bool:
    """Попадает ли текущий час UTC в диапазон тишины.

    Границы ВКЛЮЧИТЕЛЬНЫ с обеих сторон: человек, выбравший «с 22 до 6», ждёт
    тишины и в 22, и в 6. Диапазон может пересекать полночь — тогда он
    проверяется двумя отрезками, а не сравнением «от меньше до».

    Совпадение границ (``с 3 до 3``) означает ровно один час, а не сутки:
    «тишина выключена» выражается пустыми значениями, и второго способа
    выключить её быть не должно — иначе одно и то же состояние читалось бы
    двумя разными способами.
    """
    if settings.quiet_from is None or settings.quiet_to is None:
        return False
    hour = now.astimezone(UTC).hour
    start, end = int(settings.quiet_from), int(settings.quiet_to)
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def user_filter_reason(
    settings: UserSettings,
    instrument_id: int,
    strength: float,
    now: datetime,
) -> str | None:
    """Почему уведомление не нужно этому человеку. ``None`` — нужно.

    Порядок проверок задан §2 ТЗ и не случаен: сначала отсекается то, что
    человеку не нужно, и только потом применяются ограничения потока
    (:func:`src.notify.agent.rate_limit_reason`). Иначе выдержка по токену
    тратилась бы на токены, которых человек не выбирал: пришёл бы сигнал по
    DOGE, занял выдержку, а сигнал по BTC, который человек ждёт, оказался бы
    придержан из-за него.

    Горизонт пользователя в отборе НЕ участвует: сигнал един для всех
    горизонтов (Этап 8.1), горизонт влияет только на текст сообщения.
    """
    if not wants_instrument(settings, instrument_id):
        return "инструмент не выбран пользователем"
    if float(strength) < float(settings.min_score):
        return (
            f"сила {float(strength):.2f} ниже порога пользователя "
            f"{float(settings.min_score):.2f}"
        )
    if is_quiet_hour(settings, now):
        return f"тишина с {settings.quiet_from:02d} до {settings.quiet_to:02d} UTC"
    return None
