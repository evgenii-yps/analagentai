"""Конфигурация прогона: чтение backtest/.env.backtest и валидация.

Файл ``.env`` продакшна здесь НЕ читается и не изменяется. Параметры самих
агентов (пороги, веса, окна, MIN_AGENTS) в этой конфигурации отсутствуют
намеренно: реплей обязан работать на тех же значениях, что и продакшн, и берёт
их из ``src.core.config.settings``. Любой параметр агента, продублированный
здесь, стал бы способом незаметно перенастроить систему под результат.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class BacktestConfig:
    """Параметры прогона (§11 ТЗ). Неизменяемая: подмена значений по ходу исключена."""

    instruments: tuple[str, ...]
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
            "instruments": list(self.instruments),
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


def _months(n: int) -> timedelta:
    """Длина ``n`` месяцев для разделения выборки: месяц = 30 суток.

    Календарная арифметика здесь не нужна — граница служит только делением
    выборки и обязана быть одинаковой для всех инструментов и горизонтов.
    """
    return timedelta(days=30 * n)


class ConfigError(ValueError):
    """Ошибка конфигурации прогона: прогон не начинается вовсе."""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Читает файл вида KEY=VALUE, отбрасывая комментарии и пустые строки."""
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


def load_config(path: Path) -> BacktestConfig:
    """Читает и проверяет конфигурацию прогона.

    Валидация жёсткая и на входе: неверная конфигурация обязана останавливать
    прогон до первого запроса к бирже, а не проявляться странными числами
    в отчёте.
    """
    values = _parse_env_file(path)

    instruments = tuple(
        item.strip() for item in _require(values, "BT_INSTRUMENTS").split(",") if item.strip()
    )
    if not instruments:
        raise ConfigError("BT_INSTRUMENTS пуст")

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
