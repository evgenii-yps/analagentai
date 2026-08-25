"""Конфигурация прогона: чтение backtest/.env.backtest и валидация.

Файл ``.env`` продакшна здесь НЕ читается и не изменяется. Параметры самих
агентов (пороги, веса, окна, MIN_AGENTS) в этой конфигурации отсутствуют
намеренно: реплей обязан работать на тех же значениях, что и продакшн, и берёт
их из ``src.core.config.settings``. Любой параметр агента, продублированный
здесь, стал бы способом незаметно перенастроить систему под результат.

РАЗДЕЛЕНИЕ РЫНКОВ (поправка к §5.2 ТЗ). Продакшн работает на ДВУХ разных
инструментах одновременно, и это измерено на сервере 22.08.2026:

    .env:            SYMBOL=BTC/USDT (спот), SWAP_SYMBOL=BTC/USDT:USDT (контракт)
    runner.py:47-54  MarketAgent получает spot_id, FuturesAgent — swap_id
    таблица ohlcv:   свечи ТОЛЬКО по инструменту 1 (спот)
    таблица funding: пишется по инструменту 2 (контракт)

Первая редакция ТЗ трактовала инструмент как один идентификатор на оба ряда, и
это дало два наблюдавшихся отказа: сверка §13.2 на BTC-USDT-SWAP показала
market 0/200 (сравнивались РАЗНЫЕ РЫНКИ, а не разные формулы), а прогон на
BTC-USDT упал на ``funding-rate-history`` с кодом 51000 «Parameter instId
error» — у спота funding не существует.

Поэтому инструмент прогона — это ПАРА «спот → контракт», задаваемая ЯВНО:

    BT_INSTRUMENTS=BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP

Свечи грузятся и читаются по СПОТУ, funding — по КОНТРАКТУ. Достраивание имени
контракта из имени спота (``BTC-USDT`` + ``-SWAP``) запрещено намеренно: имена
инструментов принадлежат бирже, а не нашим соглашениям, и молчаливая догадка
здесь означала бы прогон не на том рынке — ровно та ошибка, которую эта правка
устраняет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Разделитель пары «спот → контракт» в BT_INSTRUMENTS.
PAIR_SEPARATOR = ":"

# Агенты, для которых существует историческая реконструкция входов. Liquidity
# сюда не входит и войти не может: истории стакана не существует (§3.3 ТЗ).
REPLAYABLE_AGENTS = ("market", "futures")


class ConfigError(ValueError):
    """Ошибка конфигурации прогона: прогон не начинается вовсе."""


@dataclass(frozen=True)
class InstrumentPair:
    """Пара рынков одного актива: спот (свечи) и бессрочный контракт (funding).

    ``swap`` может отсутствовать — но ТОЛЬКО когда Futures не участвует в
    прогоне (``BT_AGENTS=market``). Проверку выполняет :func:`load_config`:
    здесь пара ничего не знает о составе агентов.
    """

    spot: str
    swap: str | None = None

    @property
    def key(self) -> str:
        """Ключ инструмента в таблицах прогона.

        Ключом служит СПОТ: решения, цены и исходы считаются по свечам, а свечи
        существуют только на споте. Контракт участвует одним рядом funding и
        своего столбца исходов не имеет.
        """
        return self.spot

    @property
    def label(self) -> str:
        return f"{self.spot}{PAIR_SEPARATOR}{self.swap}" if self.swap else self.spot

    def as_dict(self) -> dict[str, str | None]:
        return {"spot": self.spot, "swap": self.swap}

    @classmethod
    def from_dict(cls, data: dict[str, str | None]) -> InstrumentPair:
        return cls(spot=str(data["spot"]), swap=data.get("swap") or None)

    @classmethod
    def parse(cls, item: str) -> InstrumentPair:
        """Разбирает элемент BT_INSTRUMENTS вида ``СПОТ:КОНТРАКТ`` или ``СПОТ``."""
        parts = [part.strip() for part in item.split(PAIR_SEPARATOR)]
        if len(parts) > 2:
            raise ConfigError(
                f"BT_INSTRUMENTS: «{item}» содержит больше одного разделителя "
                f"«{PAIR_SEPARATOR}». Формат: СПОТ{PAIR_SEPARATOR}КОНТРАКТ, "
                f"например BTC-USDT{PAIR_SEPARATOR}BTC-USDT-SWAP"
            )
        spot = parts[0]
        swap = parts[1] if len(parts) == 2 else None
        if not spot:
            raise ConfigError(f"BT_INSTRUMENTS: пустое имя спота в «{item}»")
        if len(parts) == 2 and not swap:
            raise ConfigError(
                f"BT_INSTRUMENTS: в «{item}» после «{PAIR_SEPARATOR}» пусто. "
                "Имя контракта достраивать из имени спота запрещено — впишите его явно"
            )
        return cls(spot=spot, swap=swap)


@dataclass(frozen=True)
class BacktestConfig:
    """Параметры прогона (§11 ТЗ). Неизменяемая: подмена значений по ходу исключена."""

    instruments: tuple[InstrumentPair, ...]
    agents: tuple[str, ...]
    bar: str
    period_from: datetime
    period_to: datetime
    step_hours: int
    horizons: tuple[int, ...]
    fee_roundtrip_pct: Decimal
    slippage_pct: Decimal
    oos_months: int
    request_pause_ms: int

    @property
    def with_futures(self) -> bool:
        """Участвует ли Futures — и, значит, нужен ли ряд funding вообще.

        При ``BT_AGENTS=market`` funding не запрашивается у биржи, не
        проверяется на непрерывность и не читается из БД: главный вопрос этапа
        (Market на 55 месяцах) от funding не зависит, а глубина истории funding
        в три месяца уже признана разведочной.
        """
        return "futures" in self.agents

    @property
    def spot_ids(self) -> tuple[str, ...]:
        return tuple(pair.spot for pair in self.instruments)

    @property
    def swap_ids(self) -> tuple[str, ...]:
        return tuple(pair.swap for pair in self.instruments if pair.swap)

    def agent_sets(self) -> tuple[tuple[str, list[str]], ...]:
        """Конфигурации агентов к прогону (§3.4 ТЗ), ограниченные BT_AGENTS.

        A — только Market. B — Market + Futures; она прогоняется, только если
        Futures разрешён в BT_AGENTS. Никакого перебора вариантов сверх этого
        нет: две конфигурации фиксированы ТЗ.
        """
        sets: list[tuple[str, list[str]]] = [("A", ["market"])]
        if self.with_futures:
            sets.append(("B", ["market", "futures"]))
        return tuple(sets)

    @property
    def oos_from(self) -> datetime:
        """Начало проверочного отрезка (последние ``oos_months`` месяцев периода).

        Месяц считается как 30 суток: календарная арифметика здесь не нужна,
        граница служит только разделением выборки и обязана быть одинаковой
        для всех инструментов и горизонтов.
        """
        return self.period_to - _months(self.oos_months)

    def as_dict(self) -> dict[str, object]:
        """Сериализуемое представление для backtest.runs.config_json."""
        return {
            "instruments": [pair.as_dict() for pair in self.instruments],
            "agents": list(self.agents),
            "bar": self.bar,
            "period_from": self.period_from.isoformat(),
            "period_to": self.period_to.isoformat(),
            "oos_from": self.oos_from.isoformat(),
            "step_hours": self.step_hours,
            "horizons": list(self.horizons),
            "fee_roundtrip_pct": str(self.fee_roundtrip_pct),
            "slippage_pct": str(self.slippage_pct),
            "oos_months": self.oos_months,
            "request_pause_ms": self.request_pause_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BacktestConfig:
        """Восстанавливает конфигурацию из config_json (нужно отчёту)."""
        return cls(
            instruments=tuple(
                InstrumentPair.from_dict(item) for item in data["instruments"]
            ),
            agents=tuple(data.get("agents") or ("market",)),
            bar=data["bar"],
            period_from=datetime.fromisoformat(data["period_from"]),
            period_to=datetime.fromisoformat(data["period_to"]),
            step_hours=int(data["step_hours"]),
            horizons=tuple(int(h) for h in data["horizons"]),
            fee_roundtrip_pct=Decimal(str(data["fee_roundtrip_pct"])),
            slippage_pct=Decimal(str(data["slippage_pct"])),
            oos_months=int(data["oos_months"]),
            request_pause_ms=int(data["request_pause_ms"]),
        )


def _months(n: int) -> timedelta:
    """Длина ``n`` месяцев для разделения выборки: месяц = 30 суток.

    Календарная арифметика здесь не нужна — граница служит только делением
    выборки и обязана быть одинаковой для всех инструментов и горизонтов.
    """
    return timedelta(days=30 * n)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Читает файл вида KEY=VALUE, отбрасывая комментарии и пустые строки."""
    if path.is_dir():
        # Дефект D-9 в чистом виде: docker создаёт КАТАЛОГ на месте
        # отсутствующего файла, указанного в volumes. Дальше конфигурация
        # «не читается» по непонятной причине — поэтому причина названа прямо.
        raise ConfigError(
            f"{path} — это КАТАЛОГ, а не файл. Так бывает, когда контейнер "
            "запускали с пробросом ./backtest/.env.backtest, которого на хосте "
            "не существовало: docker создал каталог. Удалите его, создайте файл "
            "из backtest/.env.backtest.example и пересоберите образ с --no-cache."
        )
    if not path.is_file():
        raise ConfigError(f"файл конфигурации не найден: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Хвостовой комментарий отсекается: «0.10  # 2 x тейкер» → «0.10».
        values[key.strip()] = value.split("#", 1)[0].strip()
    return values


def _require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ConfigError(
            f"параметр {key} не задан. Значения, которые определяются зондом "
            f"(scripts/probe_history_depth.py), подставлять «по памяти» нельзя."
        )
    return value


def _parse_ts(value: str, key: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{key}: не разбирается как время ISO-8601: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_agents(raw: str) -> tuple[str, ...]:
    """Разбирает BT_AGENTS: ``market`` или ``market,futures``.

    Market обязателен: без него нечего сверять с продакшном (§13.2 блокирующая
    сверка держится именно на Market), и главный вопрос этапа — про Market.
    """
    agents = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    if not agents:
        raise ConfigError("BT_AGENTS пуст: допустимо «market» или «market,futures»")
    if len(set(agents)) != len(agents):
        raise ConfigError(f"BT_AGENTS содержит повторы: {raw}")
    unknown = [name for name in agents if name not in REPLAYABLE_AGENTS]
    if "liquidity" in unknown:
        raise ConfigError(
            "BT_AGENTS: liquidity прогнать нельзя — истории стакана не "
            "существует ни у одной биржи (§3.3 ТЗ)"
        )
    if unknown:
        raise ConfigError(
            f"BT_AGENTS: неизвестные агенты {unknown}; допустимы {list(REPLAYABLE_AGENTS)}"
        )
    if "market" not in agents:
        raise ConfigError(
            "BT_AGENTS обязан включать market: на нём держится блокирующая "
            "сверка §13.2 и главный вопрос этапа"
        )
    # Порядок фиксируется, чтобы состав агентов в отчёте не зависел от того,
    # как его записали в конфигурации.
    return tuple(name for name in REPLAYABLE_AGENTS if name in agents)


def parse_instruments(raw: str, *, with_futures: bool) -> tuple[InstrumentPair, ...]:
    """Разбирает BT_INSTRUMENTS в пары «спот → контракт».

    При участии Futures контракт ОБЯЗАТЕЛЕН у каждой пары: без него запрос
    funding уходит с идентификатором спота и биржа отвечает 51000 «Parameter
    instId error» — это уже наблюдалось.
    """
    pairs = tuple(
        InstrumentPair.parse(item) for item in raw.split(",") if item.strip()
    )
    if not pairs:
        raise ConfigError("BT_INSTRUMENTS пуст")

    spots = [pair.spot for pair in pairs]
    if len(set(spots)) != len(spots):
        raise ConfigError(f"BT_INSTRUMENTS: спот-инструмент указан дважды: {spots}")

    if with_futures:
        missing = [pair.spot for pair in pairs if not pair.swap]
        if missing:
            raise ConfigError(
                "BT_INSTRUMENTS: при BT_AGENTS с futures у каждой пары обязан быть "
                f"контракт (формат СПОТ{PAIR_SEPARATOR}КОНТРАКТ). Без контракта: "
                f"{missing}. У спота истории funding не существует — биржа "
                "отвечает 51000 «Parameter instId error»"
            )
    return pairs


def load_config(path: Path) -> BacktestConfig:
    """Читает и проверяет конфигурацию прогона.

    Валидация жёсткая и на входе: неверная конфигурация обязана останавливать
    прогон до первого запроса к бирже, а не проявляться странными числами
    в отчёте.
    """
    values = _parse_env_file(path)

    agents = parse_agents(_require(values, "BT_AGENTS"))
    instruments = parse_instruments(
        _require(values, "BT_INSTRUMENTS"), with_futures="futures" in agents
    )

    horizons = tuple(
        int(item.strip()) for item in _require(values, "BT_HORIZONS").split(",") if item.strip()
    )
    if not horizons or any(h <= 0 for h in horizons):
        raise ConfigError("BT_HORIZONS должен содержать положительные целые часы")

    period_from = _parse_ts(_require(values, "BT_PERIOD_FROM"), "BT_PERIOD_FROM")
    period_to = _parse_ts(_require(values, "BT_PERIOD_TO"), "BT_PERIOD_TO")
    if period_from >= period_to:
        raise ConfigError("BT_PERIOD_FROM должен быть раньше BT_PERIOD_TO")

    oos_months = int(_require(values, "BT_OOS_MONTHS"))
    if oos_months <= 0:
        raise ConfigError("BT_OOS_MONTHS должен быть положительным")

    step_hours = int(_require(values, "BT_STEP_HOURS"))
    if step_hours <= 0:
        raise ConfigError("BT_STEP_HOURS должен быть положительным")

    request_pause_ms = int(_require(values, "BT_REQUEST_PAUSE_MS"))
    if request_pause_ms < 0:
        raise ConfigError("BT_REQUEST_PAUSE_MS не может быть отрицательным")

    cfg = BacktestConfig(
        instruments=instruments,
        agents=agents,
        bar=_require(values, "BT_BAR"),
        period_from=period_from,
        period_to=period_to,
        step_hours=step_hours,
        horizons=horizons,
        fee_roundtrip_pct=Decimal(_require(values, "BT_FEE_ROUNDTRIP_PCT")),
        slippage_pct=Decimal(_require(values, "BT_SLIPPAGE_PCT")),
        oos_months=oos_months,
        request_pause_ms=request_pause_ms,
    )

    # Проверочный отрезок обязан помещаться внутрь периода, иначе разделение
    # выборки на обучающую и проверочную части бессмысленно.
    if cfg.oos_from <= cfg.period_from:
        raise ConfigError(
            "BT_OOS_MONTHS покрывает весь период: проверочный отрезок должен быть "
            "короче периода прогона"
        )
    return cfg
