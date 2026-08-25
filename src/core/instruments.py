"""Инструменты прогона: пары «спот → контракт» и разбор горизонтов.

ДВА РЫНКА У ОДНОГО ТОКЕНА (§1 ТЗ 8.1, замеры 22.08.2026). Market Agent
работает на СПОТЕ, Futures Agent — на КОНТРАКТЕ:

    таблица ohlcv          — свечи только по споту;
    таблицы funding / open_interest — только по контракту;
    src/agents/runner.py   — MarketAgent получает spot_id, FuturesAgent — swap_id.

Поэтому инструмент системы — это ПАРА, и задаётся она ЯВНО:

    SYMBOLS=BTC/USDT:BTC/USDT:USDT,ETH/USDT:ETH/USDT:USDT

Разделитель пары — ПЕРВОЕ двоеточие: имя бессрочного контракта на бирже само
содержит двоеточие (``BTC/USDT:USDT``), и разбор по последнему разделителю
разорвал бы его. Достраивание имени контракта из имени спота
(``BTC/USDT`` + ``:USDT``) ЗАПРЕЩЕНО: имена инструментов принадлежат бирже, а
не нашим соглашениям, и молчаливая догадка означала бы сбор не с того рынка —
ровно та ошибка, которая стоила Этапу 7.4 двух прогонов.
"""

from __future__ import annotations

from dataclasses import dataclass

# Разделитель пары в SYMBOLS. Ищется ПЕРВОЕ вхождение.
PAIR_SEPARATOR = ":"


class InstrumentConfigError(ValueError):
    """Ошибка описания инструментов: сервис не стартует вовсе."""


@dataclass(frozen=True)
class SymbolPair:
    """Пара рынков одного токена: спот (свечи, стакан, сделки) и контракт."""

    spot: str
    swap: str

    @property
    def token(self) -> str:
        """Базовая валюта: ``BTC/USDT`` → ``BTC``. Только для подписей и логов."""
        return self.spot.split("/", 1)[0]

    @property
    def label(self) -> str:
        return f"{self.spot}{PAIR_SEPARATOR}{self.swap}"


def parse_symbol_pairs(raw: str) -> list[SymbolPair]:
    """Разбирает SYMBOLS в список пар. Проверяет строго, на входе.

    Неверная строка обязана останавливать сервис при старте, а не проявляться
    отсутствием данных по «пропавшему» токену через сутки.
    """
    pairs: list[SymbolPair] = []
    seen_spot: set[str] = set()
    seen_swap: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        spot, sep, swap = item.partition(PAIR_SEPARATOR)
        spot, swap = spot.strip(), swap.strip()
        if not sep or not swap:
            raise InstrumentConfigError(
                f"SYMBOLS: «{item}» — не пара. Формат: СПОТ:КОНТРАКТ, например "
                f"BTC/USDT:BTC/USDT:USDT. Имя контракта из имени спота не "
                f"достраивается (§1 ТЗ 8.1)"
            )
        if not spot:
            raise InstrumentConfigError(f"SYMBOLS: пустое имя спота в «{item}»")
        if spot in seen_spot:
            raise InstrumentConfigError(f"SYMBOLS: спот {spot} указан дважды")
        if swap in seen_swap:
            raise InstrumentConfigError(f"SYMBOLS: контракт {swap} указан дважды")
        seen_spot.add(spot)
        seen_swap.add(swap)
        pairs.append(SymbolPair(spot=spot, swap=swap))
    if not pairs:
        raise InstrumentConfigError("SYMBOLS пуст: нужна хотя бы одна пара")
    return pairs


def parse_horizon_hours(value: str | int) -> int:
    """Горизонт оценки в часах: принимает ``4``, ``4h``, ``240m``, ``1d``.

    Прежний формат (``1h,4h``) читается наравне с новым (``1,4,12,24``): в .env
    на сервере может стоять любой из них, и молча получить другой набор
    горизонтов из-за формата записи нельзя.
    """
    if isinstance(value, int):
        hours = value
    else:
        text = str(value).strip().lower()
        if not text:
            raise InstrumentConfigError("пустой горизонт оценки")
        units = {"h": 1, "d": 24}
        if text[-1].isdigit():
            hours = int(text)
        elif text.endswith("m"):
            minutes = int(text[:-1])
            if minutes % 60 != 0:
                raise InstrumentConfigError(
                    f"горизонт {value}: оценка ведётся в целых часах"
                )
            hours = minutes // 60
        elif text[-1] in units:
            hours = int(text[:-1]) * units[text[-1]]
        else:
            raise InstrumentConfigError(f"неизвестный горизонт: {value}")
    if hours <= 0:
        raise InstrumentConfigError(f"горизонт должен быть положительным: {value}")
    return hours


def horizon_label(hours: int) -> str:
    """Подпись горизонта для текстовых колонок и сообщений: ``4`` → ``4h``."""
    return f"{hours}h"


@dataclass(frozen=True)
class InstrumentIds:
    """Идентификаторы пары в таблице ``instruments``."""

    pair: SymbolPair
    spot_id: int
    swap_id: int

    @property
    def token(self) -> str:
        return self.pair.token


async def ensure_instruments(db: object, exchange_name: str, pairs: list[SymbolPair]):
    """Заводит (идемпотентно) записи инструментов для каждой пары.

    Возвращает список :class:`InstrumentIds` в порядке пар. ``db`` — объект с
    методом ``get_or_create_instrument``; параметр вынесен, чтобы модуль не
    зависел от слоя доступа к БД и проверялся без него.
    """
    result: list[InstrumentIds] = []
    for pair in pairs:
        spot_id = await db.get_or_create_instrument(exchange_name, pair.spot, "spot")
        swap_id = await db.get_or_create_instrument(exchange_name, pair.swap, "swap")
        result.append(InstrumentIds(pair=pair, spot_id=spot_id, swap_id=swap_id))
    return result
