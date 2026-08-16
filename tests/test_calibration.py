"""Этап 7.3, Блок B: построение калибровочной кривой и её применение.

Проверяется главное свойство этапа: система не называет вероятностью число,
не выведенное из фактических исходов. Пока кривой нет — ``calibrated_probability``
равна NULL, в тексте уведомления строки про вероятность нет, а в калиброванном
режиме отбора не уходит ничего.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.calibration.curve import (
    Observation,
    bin_index,
    build_bins,
    probability_for_index,
    to_independent,
)

_T0 = datetime(2026, 8, 16, 0, 0, 0, tzinfo=UTC)


def _obs(minutes: float, index: float, success: bool) -> Observation:
    return Observation(ts=_T0 + timedelta(minutes=minutes), index=index, success=success)


# --- Прореживание до независимых наблюдений --------------------------------

def test_thinning_collapses_one_window_to_single_observation() -> None:
    """240 подряд идущих сигналов внутри одного 4-часового окна → одно наблюдение."""
    dense = [_obs(i, 0.5, i % 2 == 0) for i in range(240)]
    independent = to_independent(dense)
    assert len(independent) == 1
    # Берётся ПЕРВЫЙ по времени сигнал окна.
    assert independent[0].ts == _T0


def test_thinning_keeps_one_per_window() -> None:
    """Сигналы из разных окон остаются каждый своим наблюдением."""
    dense = [_obs(i, 0.5, True) for i in range(0, 24 * 60, 5)]  # сутки, шаг 5 минут
    independent = to_independent(dense)
    assert len(independent) == 6  # 24 часа / 4 часа
    starts = {o.ts.hour for o in independent}
    assert starts == {0, 4, 8, 12, 16, 20}


def test_thinning_is_order_independent() -> None:
    # Минуты 5 и 100 — первое окно (00:00–04:00), минута 300 — второе.
    shuffled = [_obs(300, 0.4, False), _obs(5, 0.6, True), _obs(100, 0.2, True)]
    assert [o.ts for o in to_independent(shuffled)] == [
        _T0 + timedelta(minutes=5),
        _T0 + timedelta(minutes=300),
    ]


# --- Корзины и сглаживание --------------------------------------------------

def test_bin_index_covers_full_range() -> None:
    assert bin_index(0.0, 5) == 0
    assert bin_index(0.19, 5) == 0
    assert bin_index(0.2, 5) == 1
    assert bin_index(1.0, 5) == 4  # ровно 1.0 попадает в последнюю корзину


def test_build_bins_matches_known_rates() -> None:
    """Синтетическая выборка с известными долями даёт ожидаемые значения корзин."""
    observations: list[Observation] = []
    minute = 0
    # Корзина [0.0–0.2): 10 наблюдений, 8 успехов.
    for i in range(10):
        observations.append(_obs(minute, 0.1, i < 8))
        minute += 240
    # Корзина [0.8–1.0]: 10 наблюдений, 2 успеха.
    for i in range(10):
        observations.append(_obs(minute, 0.9, i < 2))
        minute += 240

    bins, base_rate = build_bins(observations, n_bins=5, prior_weight=10.0)
    assert base_rate == pytest.approx(0.5)
    assert bins[0]["n"] == 10 and bins[0]["successes"] == 8
    assert bins[4]["n"] == 10 and bins[4]["successes"] == 2
    # p = (8 + 10*0.5) / (10 + 10) = 0.65 ; (2 + 5) / 20 = 0.35
    assert bins[0]["p"] == pytest.approx(0.65, abs=1e-6)
    assert bins[4]["p"] == pytest.approx(0.35, abs=1e-6)
    # Пустая корзина получает ровно базовую ставку.
    assert bins[2]["n"] == 0
    assert bins[2]["p"] == pytest.approx(base_rate, abs=1e-6)


def test_smoothing_prevents_zero_probability() -> None:
    """Корзина n=3, successes=0 при base_rate=0.35 и k=10 даёт 0.269…, а не 0.00."""
    observations = [_obs(i * 240, 0.1, False) for i in range(3)]
    # Добиваем выборку так, чтобы общая доля успеха была ровно 0.35 (7 из 20).
    observations += [_obs((3 + i) * 240, 0.5, i < 7) for i in range(17)]

    bins, base_rate = build_bins(observations, n_bins=5, prior_weight=10.0)
    assert base_rate == pytest.approx(0.35, abs=1e-9)
    assert bins[0]["n"] == 3 and bins[0]["successes"] == 0
    assert bins[0]["p"] == pytest.approx(3.5 / 13.0, abs=1e-6)
    assert bins[0]["p"] > 0.0


def test_decreasing_dependence_is_preserved() -> None:
    """Убывающая связь индекса с исходом сохраняется, кривая не выпрямляется.

    Это прямое требование ТЗ §4.3.7: изотоническая регрессия и любые методы,
    предполагающие рост вероятности с ростом индекса, не применяются.
    """
    observations: list[Observation] = []
    minute = 0
    # Доли успеха убывают с ростом индекса: 0.8, 0.6, 0.4, 0.2, 0.0.
    for bin_no, rate in enumerate([0.8, 0.6, 0.4, 0.2, 0.0]):
        index = bin_no * 0.2 + 0.1
        for i in range(10):
            observations.append(_obs(minute, index, i < rate * 10))
            minute += 240

    bins, _ = build_bins(observations, n_bins=5, prior_weight=10.0)
    values = [b["p"] for b in bins]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]


def test_probability_for_index_reads_the_right_bin() -> None:
    bins = [
        {"lo": 0.0, "hi": 0.5, "n": 10, "successes": 4, "p": 0.4},
        {"lo": 0.5, "hi": 1.0, "n": 10, "successes": 2, "p": 0.2},
    ]
    assert probability_for_index(bins, 0.1) == pytest.approx(0.4)
    assert probability_for_index(bins, 0.75) == pytest.approx(0.2)
    assert probability_for_index(bins, 1.0) == pytest.approx(0.2)
    # Нет корзин или значение вне диапазона → вероятности нет вовсе.
    assert probability_for_index([], 0.5) is None
    assert probability_for_index(bins, 1.5) is None


# --- Прогон построения: мало данных, достаточно данных ----------------------

class _FakeDB:
    """Минимальная замена слоя БД: запоминает вызовы вместо походов в базу."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.saved: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ensure_calibration_schema(self) -> None:
        return None

    async def get_independent_outcomes(
        self, logic_version: int, horizon: str
    ) -> list[dict[str, Any]]:
        return self.rows

    async def save_calibration_curve(self, **kwargs: Any) -> int:
        self.saved.append(kwargs)
        self.active = kwargs
        return 42


def _rows(n: int, index: float = 0.5, success: bool = True) -> list[dict[str, Any]]:
    """n наблюдений в РАЗНЫХ 4-часовых окнах (иначе прореживание схлопнет их)."""
    return [
        {
            "ts": _T0 + timedelta(hours=4 * i),
            "probability": index,
            "success": success,
        }
        for i in range(n)
    ]


async def test_curve_not_built_below_min_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Наблюдений меньше минимума → кривая не строится, активная не подменяется."""
    from src.calibration import runner as runner_mod

    fake = _FakeDB(_rows(59))
    monkeypatch.setattr(runner_mod, "db", fake)
    monkeypatch.setattr(runner_mod.settings, "CALIBRATION_MIN_SAMPLES", 60)

    curve_id = await runner_mod.build_once()

    assert curve_id is None
    assert fake.saved == []       # ни одной записи
    assert fake.active is None    # активная кривая не подменена


async def test_curve_built_when_enough_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.calibration import runner as runner_mod

    rows = _rows(30, index=0.1, success=True) + [
        {
            "ts": _T0 + timedelta(hours=4 * (30 + i)),
            "probability": 0.9,
            "success": False,
        }
        for i in range(30)
    ]
    fake = _FakeDB(rows)
    monkeypatch.setattr(runner_mod, "db", fake)
    monkeypatch.setattr(runner_mod.settings, "CALIBRATION_MIN_SAMPLES", 60)

    curve_id = await runner_mod.build_once()

    assert curve_id == 42
    saved = fake.saved[0]
    assert saved["sample_size"] == 60
    assert saved["base_rate"] == pytest.approx(0.5)
    # Убывающая связь сохранена: нижняя корзина успешнее верхней.
    assert saved["bins"][0]["p"] > saved["bins"][4]["p"]


async def test_dense_input_is_thinned_before_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Плотный поток решений не создаёт ложной выборки: остаётся одно на окно."""
    from src.calibration import runner as runner_mod

    dense = [
        {"ts": _T0 + timedelta(minutes=i), "probability": 0.5, "success": True}
        for i in range(240)
    ]
    fake = _FakeDB(dense)
    monkeypatch.setattr(runner_mod, "db", fake)
    monkeypatch.setattr(runner_mod.settings, "CALIBRATION_MIN_SAMPLES", 2)

    assert await runner_mod.build_once() is None  # одно наблюдение < 2
    assert fake.saved == []


# --- Схема: одна активная кривая на версию логики ---------------------------

def test_unique_active_index_is_declared_in_schema() -> None:
    """Уникальность активной кривой обеспечена частичным индексом в самой схеме."""
    for path in (
        Path("db/init.sql"),
        Path("db/migrations/007_calibration_inertia.sql"),
    ):
        sql = path.read_text(encoding="utf-8")
        assert re.search(
            r"CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_active\s+"
            r"ON calibration_curves \(logic_version\) WHERE is_active",
            sql,
        ), f"нет частичного уникального индекса в {path}"


def test_rollback_migration_drops_everything_it_added() -> None:
    """Обратная миграция снимает ровно то, что добавила прямая."""
    sql = Path("db/migrations/007_calibration_inertia_rollback.sql").read_text(
        encoding="utf-8"
    )
    for fragment in (
        "DROP COLUMN IF EXISTS calibrated_probability",
        "DROP COLUMN IF EXISTS calibration_id",
        "DROP COLUMN IF EXISTS inputs_hash",
        "DROP COLUMN IF EXISTS is_repeat",
        "DROP TABLE IF EXISTS calibration_curves",
    ):
        assert fragment in sql
    # Колонка probability не трогается ни прямой, ни обратной миграцией.
    assert "DROP COLUMN IF EXISTS probability" not in sql


def test_activation_deactivates_previous_curve_first() -> None:
    """Новая кривая активируется только после снятия флага со старой.

    Порядок важен: частичный уникальный индекс не допускает двух активных
    одновременно, поэтому INSERT до UPDATE упал бы.
    """
    source = Path("src/core/db.py").read_text(encoding="utf-8")
    body = source.split("async def save_calibration_curve", 1)[1]
    update_pos = body.index("UPDATE calibration_curves SET is_active = FALSE")
    insert_pos = body.index("INSERT INTO calibration_curves")
    assert update_pos < insert_pos
    assert "conn.transaction()" in body[:insert_pos]
