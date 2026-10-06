"""Меню настроек `/settings`: клавиатура, разбор нажатий, подтверждения (§1 ТЗ 8.3).

Все функции чистые: состояние приходит параметрами, наружу отдаются готовая
клавиатура и новое состояние. Так меню проверяется тестами целиком, без
Telegram и без базы, — а это тот самый код, ошибка в котором молча лишает
человека уведомлений.

Данные кнопки (``callback_data``) ограничены 64 байтами протоколом Telegram,
поэтому они короткие и не несут ничего, кроме действия и значения. Состояние
между шагами выбора тишины передаётся В САМОЙ кнопке (``qt:22:6``), а не
хранится на сервере: незавершённый выбор не должен переживать перезапуск и
путать следующего, кто откроет меню.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.bot import nav
from src.core import fmt
from src.core.user_settings import UserSettings

# Значения, предлагаемые кнопками (§1 ТЗ 8.3).
HORIZONS = (1, 4, 12, 24)
THRESHOLDS = (0.60, 0.70, 0.80, 0.90)

MARK_ON = "✅"
MARK_OFF = "▫️"


def _token(symbol: str) -> str:
    """``BTC/USDT`` → ``BTC``."""
    return symbol.split("/", 1)[0]


def _selected(settings: UserSettings, instrument_id: int) -> bool:
    return settings.instruments is None or instrument_id in settings.instruments


UTC_ZONE = ZoneInfo("UTC")


# ЧАСЫ ТИШИНЫ: ХРАНЕНИЕ В UTC, ПОКАЗ И ВЫБОР — ПО ЧАСОВОМУ ПОЯСУ УВЕДОМЛЕНИЙ (Д8).
# В ``user_settings`` часы лежат в UTC и включительно с обеих сторон (``quiet_to = 4`` —
# тихо до 04:59): так их читает ``src.notify.agent`` и служба demo, и этот формат не
# меняется. Человек живёт по МСК, поэтому перевод туда и обратно делается здесь, по
# смещению пояса на момент ``now``. Пояса с нецелым смещением округляются до часа.

def _offset_hours(tz: ZoneInfo, now: datetime | None) -> int:
    moment = (now or datetime.now(UTC)).astimezone(tz)
    offset = moment.utcoffset()
    return round(offset.total_seconds() / 3600) if offset is not None else 0


def local_to_utc_hour(hour: int, tz: ZoneInfo, now: datetime | None = None) -> int:
    """Час по местному времени → час UTC."""
    return (int(hour) - _offset_hours(tz, now)) % 24


def utc_to_local_hour(hour: int, tz: ZoneInfo, now: datetime | None = None) -> int:
    """Час UTC → час по местному времени."""
    return (int(hour) + _offset_hours(tz, now)) % 24


def quiet_bounds(
    settings: UserSettings, tz: ZoneInfo = UTC_ZONE, now: datetime | None = None
) -> tuple[int, int] | None:
    """Окно тишины по местному времени: ``(с_часа, до_часа)``; правая граница — не включена.

    Хранится «до HH:59 включительно», а показывается «до (HH+1):00»: ``quiet_to = 4`` (UTC)
    в Москве — «до 08:00».
    """
    if settings.quiet_from is None or settings.quiet_to is None:
        return None
    start = utc_to_local_hour(settings.quiet_from, tz, now)
    end = (utc_to_local_hour(settings.quiet_to, tz, now) + 1) % 24
    return start, end


def quiet_text(
    settings: UserSettings, tz: ZoneInfo = UTC_ZONE, now: datetime | None = None
) -> str:
    """Человеческая подпись состояния тишины: «с 23:00 до 08:00 МСК» или «выключены»."""
    bounds = quiet_bounds(settings, tz, now)
    if bounds is None:
        return "выключены"
    return f"с {bounds[0]:02d}:00 до {bounds[1]:02d}:00 {fmt.tz_label(tz)}"


def menu_text(
    settings: UserSettings,
    instruments: list[tuple[int, str]],
    tz: ZoneInfo = UTC_ZONE,
    signals_enabled: bool = True,
    now: datetime | None = None,
) -> str:
    """Текст экрана настроек: что настроено сейчас и на что это влияет (Д7).

    Меню показывает ТЕКУЩЕЕ состояние (§1 ТЗ 8.3): человек должен видеть, что
    действует, не вспоминая, что он нажимал в прошлый раз. Монеты, горизонт и порог
    управляют только разделами «Сигналы» и «Качество сигналов» (и сигнальными
    уведомлениями, когда они включены) — тихие часы касаются сообщений о сделках.
    """
    chosen = [
        _token(symbol) for instrument_id, symbol in instruments
        if _selected(settings, instrument_id)
    ]
    bounds = quiet_bounds(settings, tz, now)
    quiet = (
        "выключены" if bounds is None
        else f"{bounds[0]:02d}:00–{bounds[1]:02d}:00 {fmt.tz_label(tz)}"
    )
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"🔕 <b>Тихие часы:</b> {quiet}",
        "   В эти часы сообщения о сделках приходят без звука.",
        "   Тревоги о сбоях — всегда со звуком.",
        "",
        "📡 <b>Для разделов «Сигналы» и «Качество сигналов»:</b>",
        f"   Монеты: {', '.join(chosen) if chosen else 'ни одной'}",
        f"   Горизонт: {settings.horizon_h} ч",
        f"   Порог силы: {fmt.num(settings.min_score)}",
    ]
    if not signals_enabled:
        lines.append("ℹ️ Уведомления о сигналах сейчас выключены.")
    return "\n".join(lines)


def menu_keyboard(
    settings: UserSettings,
    instruments: list[tuple[int, str]],
    tz: ZoneInfo = UTC_ZONE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Клавиатура меню: выбранное отмечено галочкой."""
    token_rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for instrument_id, symbol in instruments:
        mark = MARK_ON if _selected(settings, instrument_id) else MARK_OFF
        row.append({
            "text": f"{mark} {_token(symbol)}",
            "callback_data": f"tok:{instrument_id}",
        })
        if len(row) == 3:
            token_rows.append(row)
            row = []
    if row:
        token_rows.append(row)

    horizon_row = [
        {
            "text": f"{MARK_ON if settings.horizon_h == h else ''}{h} ч".strip(),
            "callback_data": f"hor:{h}",
        }
        for h in HORIZONS
    ]
    threshold_row = [
        {
            "text": (
                f"{MARK_ON if abs(settings.min_score - t) < 1e-9 else ''}"
                f"{fmt.num(t)}".strip()
            ),
            "callback_data": f"thr:{t:.2f}",
        }
        for t in THRESHOLDS
    ]
    quiet_row = [
        {"text": f"Тишина: {quiet_text(settings, tz, now)}", "callback_data": "quiet"}
    ]
    if settings.quiet_from is not None:
        quiet_row.append({"text": "Выключить", "callback_data": "qoff"})

    return {
        "inline_keyboard": [
            *token_rows, horizon_row, threshold_row, quiet_row, [nav.home_button()],
        ]
    }


def hours_keyboard(prefix: str, title_action: str) -> dict[str, Any]:
    """Клавиатура выбора часа (00–23, по часовому поясу уведомлений) для тишины."""
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for hour in range(24):
        row.append({"text": f"{hour:02d}", "callback_data": f"{prefix}{hour}"})
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "← Назад", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def hour_label(value: str) -> str:
    """``"23"`` → ``"23:00"`` (для подсказки при выборе часа тишины)."""
    try:
        return f"{int(value):02d}:00"
    except ValueError:
        return value


def parse_callback(data: str) -> tuple[str, str] | None:
    """``tok:3`` → ``("tok", "3")``. Неизвестное — ``None``."""
    raw = (data or "").strip()
    if not raw:
        return None
    action, _, value = raw.partition(":")
    if action not in {"tok", "hor", "thr", "quiet", "qoff", "qf", "qt", "menu"}:
        return None
    return action, value


def apply_callback(
    settings: UserSettings,
    action: str,
    value: str,
    instruments: list[tuple[int, str]],
    tz: ZoneInfo = UTC_ZONE,
    now: datetime | None = None,
) -> tuple[UserSettings, str]:
    """Применяет нажатие. Возвращает новые настройки и КОРОТКОЕ подтверждение.

    Подтверждение говорит, что настроено ТЕПЕРЬ, и не перечисляет меню заново
    (§1 ТЗ): человек только что видел меню, ему нужен результат нажатия.

    Настройки не меняются молча при недопустимом действии — возвращается тот же
    объект и текст с причиной. Молчаливый отказ человек принял бы за поломку.
    """
    all_ids = [instrument_id for instrument_id, _ in instruments]
    by_id = dict(instruments)

    if action == "tok":
        try:
            instrument_id = int(value)
        except ValueError:
            return settings, "Не понял, какой токен."
        current = (
            set(all_ids) if settings.instruments is None else set(settings.instruments)
        )
        token = _token(by_id.get(instrument_id, str(instrument_id)))
        if instrument_id in current:
            if len(current) == 1:
                # §1 ТЗ: минимум один токен. Пустой набор означал бы «не слать
                # ничего никогда», и человек не отличил бы это от поломки.
                return settings, "Нужен хотя бы один токен — этот оставлен."
            current.discard(instrument_id)
            note = f"{token} выключен"
        else:
            current.add(instrument_id)
            note = f"{token} включён"
        ordered = tuple(i for i in all_ids if i in current)
        return _replace(settings, instruments=ordered), note

    if action == "hor":
        try:
            horizon = int(value)
        except ValueError:
            return settings, "Не понял горизонт."
        if horizon not in HORIZONS:
            return settings, "Такого горизонта нет."
        return _replace(settings, horizon_h=horizon), f"Горизонт: {horizon} ч"

    if action == "thr":
        try:
            threshold = float(value)
        except ValueError:
            return settings, "Не понял порог."
        if not any(abs(threshold - t) < 1e-9 for t in THRESHOLDS):
            return settings, "Такого порога нет."
        return _replace(settings, min_score=threshold), f"Порог силы: {fmt.num(threshold)}"

    if action == "qoff":
        return (
            _replace(settings, quiet_from=None, quiet_to=None),
            "Тишина выключена",
        )

    if action == "qt":
        start_raw, _, end_raw = value.partition(":")
        try:
            start, end = int(start_raw), int(end_raw)
        except ValueError:
            return settings, "Не понял часы тишины."
        if not (0 <= start <= 23 and 0 <= end <= 23):
            return settings, "Часы задаются от 00 до 23."
        if start == end:
            # Граница конца — первый час, когда звук возвращается. Совпадение с началом
            # означало бы «тихо все сутки», а выключение звука навсегда — не тишина.
            return settings, "Начало и конец совпадают — выберите другой час конца."
        # Выбор — по местному времени, хранение — в UTC и включительно: последний тихий
        # час — тот, что стоит перед границей конца.
        stored_from = local_to_utc_hour(start, tz, now)
        stored_to = local_to_utc_hour((end - 1) % 24, tz, now)
        return (
            _replace(settings, quiet_from=stored_from, quiet_to=stored_to),
            f"Тишина: с {start:02d}:00 до {end:02d}:00 {fmt.tz_label(tz)}",
        )

    return settings, ""


def _replace(settings: UserSettings, **changes: Any) -> UserSettings:
    """Копия настроек с изменениями (dataclass неизменяем намеренно)."""
    from dataclasses import replace

    return replace(settings, **changes)
