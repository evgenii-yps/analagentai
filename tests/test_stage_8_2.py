"""Этап 8.2: цели по вероятности (§10 ТЗ). Десять обязательных наборов и разбор
краевых случаев вокруг них.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Цель — единственное число этапа,
которое человек может принять за обещание. Ошибка в ней не видна: она даёт
правдоподобную величину, по которой человек ставит деньги. Поэтому проверяются
не «функции вообще», а те четыре места, где ошибка была бы незаметной:

  * заглядывание в будущее (свеча решения в собственном окне);
  * обрезка отрицательных наблюдений нулём;
  * подстановка 0.60 вместо фактической доли касаний;
  * показ цели без вероятности её достижения.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.notify.wording import FEES_WARNING, target_block
from src.risk.quality import (
    FAIL_SHORT_SERIES,
    FAIL_STALE_LAST_CANDLE,
    check_series,
    longest_run_hours,
)
from src.risk.targets import (
    REASON_FEW_OBSERVATIONS,
    REASON_NEGATIVE_PERCENTILE,
    Candle,
    MfeSample,
    compute_target,
    covers_fees_flag,
    mfe_sample,
    percentile_cont,
    round_significant,
    target_price,
)

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5455/agenttrade)"
    ),
)

_T0 = datetime(2026, 5, 1, tzinfo=UTC)
_COST = 0.22


def _series(closes: list[float], *, highs=None, lows=None, start=_T0) -> list[Candle]:
    """Часовой ряд из перечисленных цен закрытия (высоты и низы — по умолчанию)."""
    out: list[Candle] = []
    for index, close in enumerate(closes):
        high = closes[index] + 1.0 if highs is None else highs[index]
        low = closes[index] - 1.0 if lows is None else lows[index]
        out.append(Candle(
            open_time=start + timedelta(hours=index),
            open=close, high=high, low=low, close=close,
        ))
    return out


def _sample(values: list[float]) -> MfeSample:
    return MfeSample(values=tuple(values), skipped_gap=0, skipped_tail=0)


# --- 10.1 ------------------------------------------------------------------

def test_no_lookahead_targets() -> None:
    """Свеча t не видит собственного экстремума (§4.1).

    Ряд ровный, и ровно у одной свечи (индекс 10) внутрисвечный максимум
    задран до 200 при цене решения 100. Если бы окно включало саму свечу,
    MFE для неё был бы +100%; правильный ответ — +1%, взятый из СЛЕДУЮЩИХ
    свечей, а гигантский ход обязан достаться свече 9 и более ранним.
    """
    closes = [100.0] * 30
    highs = [101.0] * 30
    highs[10] = 200.0
    candles = _series(closes, highs=highs, lows=[99.0] * 30)

    values = mfe_sample(candles, 4, "buy").values

    assert values[10] == pytest.approx(1.0), "свеча увидела собственный максимум"
    assert values[9] == pytest.approx(100.0), "максимум свечи 10 обязан войти в окно свечи 9"
    assert values[6] == pytest.approx(100.0), "окно длиной 4 захватывает свечу 10"
    assert values[5] == pytest.approx(1.0), "окно свечи 5 кончается до свечи 10"


def test_no_lookahead_targets_sell_side() -> None:
    """То же правило для продажи: минимум собственной свечи в MFE не входит."""
    closes = [100.0] * 30
    lows = [99.0] * 30
    lows[10] = 50.0
    candles = _series(closes, highs=[101.0] * 30, lows=lows)

    values = mfe_sample(candles, 4, "sell").values

    assert values[10] == pytest.approx(1.0)
    assert values[9] == pytest.approx(50.0)


def test_window_excludes_gap_observations() -> None:
    """Наблюдение, окно которого пересекает разрыв ряда, выбрасывается (§4.3)."""
    candles = _series([100.0] * 10)
    # Разрыв: следующая свеча приходит через 5 часов вместо одного.
    tail = [
        Candle(open_time=_T0 + timedelta(hours=14 + i), open=100.0,
               high=101.0, low=99.0, close=100.0)
        for i in range(10)
    ]
    sample = mfe_sample(candles + tail, 2, "buy")

    # Из первой части выбывают две последние базовые свечи (их окно упирается
    # в разрыв), из второй — две последние (правый край ряда).
    assert sample.skipped_gap == 2
    assert sample.skipped_tail == 2
    assert sample.n == 16


# --- 10.2 ------------------------------------------------------------------

def test_percentile_known_distribution() -> None:
    """40-й процентиль совпадает с аналитическим с точностью 1e-9 (§4.4).

    Ряд 0, 1, ..., 999. Линейная интерполяция даёт позицию 0.4 × 999 = 399.6,
    то есть значение ровно 399.6. Совпадение с ``percentile_cont`` PostgreSQL
    важно потому, что ту же величину проверяют прямым запросом к базе.
    """
    values = [float(i) for i in range(1000)]

    assert percentile_cont(values, 0.40) == pytest.approx(399.6, abs=1e-9)
    assert percentile_cont(values, 0.25) == pytest.approx(249.75, abs=1e-9)
    assert percentile_cont(values, 0.50) == pytest.approx(499.5, abs=1e-9)
    assert percentile_cont(values, 0.75) == pytest.approx(749.25, abs=1e-9)

    result = compute_target(
        _sample(values), cost_roundtrip_pct=_COST, min_observations=500
    )
    assert result.target_pct == pytest.approx(399.6, abs=1e-9)
    assert result.mfe_p50 == pytest.approx(499.5, abs=1e-9)


def test_percentile_order_independent() -> None:
    """Порядок наблюдений на процентиль не влияет (внутри — сортировка)."""
    values = [3.0, 1.0, 2.0, 10.0, 5.0]
    assert percentile_cont(values, 0.40) == pytest.approx(
        percentile_cont(list(reversed(values)), 0.40)
    )


# --- 10.3 ------------------------------------------------------------------

def test_hit_rate_matches() -> None:
    """Фактическая доля касаний около 0.60 на выборке из 10 000 (§4.5).

    Величина СЧИТАЕТСЯ, а не подставляется: подстановка 0.60 скрыла бы любую
    ошибку в расчёте цели, потому что проверять было бы нечего.
    """
    # Детерминированная «неровная» выборка: остатки по простому модулю дают
    # разброс без обращения к генератору случайных чисел.
    values = [((i * 7919) % 10000) / 1000.0 - 2.0 for i in range(10000)]

    result = compute_target(
        _sample(values), cost_roundtrip_pct=_COST, min_observations=500
    )

    assert result.n_observations == 10000
    assert result.hit_rate is not None
    assert 0.58 <= result.hit_rate <= 0.62, result.hit_rate
    assert abs(result.hit_rate - 0.60) <= 0.02


def test_hit_rate_is_measured_not_assumed() -> None:
    """На вырожденной выборке доля касаний равна единице — и это видно.

    Все наблюдения одинаковы, поэтому цель совпадает с ними, и MFE не меньше
    цели у ВСЕХ. Подставленное 0.60 показало бы здесь 0.60 — то есть скрыло бы
    и вырожденность выборки, и любую ошибку в расчёте цели.
    """
    result = compute_target(
        _sample([1.0] * 1000), cost_roundtrip_pct=_COST, min_observations=500
    )
    assert result.target_pct == pytest.approx(1.0)
    assert result.hit_rate == pytest.approx(1.0)
    assert abs(result.hit_rate - 0.60) > 0.02, "доля касаний подставлена, а не измерена"


# --- 10.4 ------------------------------------------------------------------

def test_negative_mfe_not_clipped() -> None:
    """Монотонное падение даёт отрицательный процентиль для покупки (§4.2, §4.7)."""
    closes = [1000.0 - i for i in range(600)]
    candles = _series(closes, highs=[c - 0.5 for c in closes], lows=[c - 1.5 for c in closes])

    sample = mfe_sample(candles, 4, "buy")
    assert sample.n >= 500
    assert all(v < 0 for v in sample.values), "отрицательные MFE обрезаны нулём"

    result = compute_target(
        sample, cost_roundtrip_pct=_COST, min_observations=500
    )
    assert result.target_pct is None
    assert result.no_target_reason == REASON_NEGATIVE_PERCENTILE
    assert result.covers_fees is False
    # Процентили сохранены: они описывают рынок и нужны отчёту.
    assert result.mfe_p50 is not None and result.mfe_p50 < 0


def test_negative_percentile_sell_side_is_positive_on_falling_market() -> None:
    """На том же падении продажа получает НОРМАЛЬНУЮ цель: направления раздельны (§4.8)."""
    closes = [1000.0 - i for i in range(600)]
    candles = _series(closes, highs=[c + 0.5 for c in closes], lows=[c - 1.5 for c in closes])

    result = compute_target(
        mfe_sample(candles, 4, "sell"),
        cost_roundtrip_pct=_COST, min_observations=500,
    )
    assert result.target_pct is not None and result.target_pct > 0
    assert result.no_target_reason is None


# --- 10.5 ------------------------------------------------------------------

def test_few_observations() -> None:
    """При n < 500 цель не выдаётся, причина названа (§4.6)."""
    result = compute_target(
        _sample([1.0] * 499), cost_roundtrip_pct=_COST, min_observations=500
    )
    assert result.target_pct is None
    assert result.hit_rate is None
    assert result.no_target_reason == REASON_FEW_OBSERVATIONS
    assert result.n_observations == 499


def test_few_observations_boundary() -> None:
    """Ровно 500 наблюдений — цель считается: порог «не меньше», а не «больше»."""
    result = compute_target(
        _sample([1.0] * 500), cost_roundtrip_pct=_COST, min_observations=500
    )
    assert result.target_pct == pytest.approx(1.0)
    assert result.hit_rate == pytest.approx(1.0)
    assert result.no_target_reason is None


# --- 10.8 ------------------------------------------------------------------

def test_covers_fees_threshold() -> None:
    """Граница покрытия издержек: 3 × 0.22 = 0.66 (§5).

    Проверяются ровно те значения, на которых двоичная арифметика ошибается:
    ``3 * 0.22`` в float равно 0.66000000000000003, и цель 0.66 объявлялась бы
    непокрывающей, хотя ТЗ проводит границу именно по ней.
    """
    assert covers_fees_flag(0.659, _COST) is False
    assert covers_fees_flag(0.66, _COST) is True
    assert covers_fees_flag(0.661, _COST) is True
    assert covers_fees_flag(None, _COST) is False


def test_covers_fees_follows_cost_setting() -> None:
    """Порог берётся из настройки, а не зашит числом: другой тариф — другая граница."""
    assert covers_fees_flag(0.66, 0.30) is False
    assert covers_fees_flag(0.90, 0.30) is True


# --- 10.10 -----------------------------------------------------------------

def test_wording_no_target() -> None:
    """При пустой цели текст говорит «Цель не рассчитана» и не содержит чисел (§8)."""
    lines = target_block({
        "direction": "buy", "target_pct": None, "target_price": None,
        "hit_rate": None, "covers_fees": False,
        "no_target_reason": REASON_FEW_OBSERVATIONS,
    })
    text = "\n".join(lines)

    assert "Цель не рассчитана" in text
    assert "недостаточно истории" in text
    assert not any(ch.isdigit() for ch in text), text
    assert "%" not in text


def test_wording_target_never_without_probability() -> None:
    """Цель и доля её достижения выводятся ТОЛЬКО ВМЕСТЕ (§8, §12)."""
    lines = target_block({
        "direction": "buy", "target_pct": 0.54, "target_price": 65480.0,
        "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None,
    })
    assert lines[0].startswith("Цель:")
    assert "случаях из 10" in lines[1]
    assert "за последние 90 суток" in lines[1]
    # Слово «вероятность» в тексте для человека не употребляется.
    assert "вероятн" not in "\n".join(lines).lower()

    # Цель есть, доли касаний нет — цель НЕ показывается вовсе.
    broken = target_block({
        "direction": "buy", "target_pct": 0.54, "target_price": 65480.0,
        "hit_rate": None, "covers_fees": True, "no_target_reason": None,
    })
    assert all("65" not in line for line in broken)
    assert "Цель не рассчитана" in broken[0]


def test_wording_fees_warning_present_and_absent() -> None:
    """Предупреждение о непокрытой комиссии выводится ровно тогда, когда должно."""
    covered = target_block({
        "direction": "buy", "target_pct": 0.80, "target_price": 65900.0,
        "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None,
    })
    assert FEES_WARNING not in covered

    uncovered = target_block({
        "direction": "buy", "target_pct": 0.18, "target_price": 0.2214,
        "hit_rate": 0.597, "covers_fees": False, "no_target_reason": None,
    })
    assert uncovered[-1] == FEES_WARNING
    assert uncovered[0] == "Цель: 0,2214 (+0,18%)"


def test_wording_matches_the_samples_of_the_specification() -> None:
    """Образцы §8 воспроизводятся дословно."""
    lines = target_block({
        "direction": "buy", "target_pct": 0.54, "target_price": 65480.0,
        "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None,
    })
    assert lines[0] == "Цель: 65 480 (+0,54%)"
    assert lines[1] == (
        "Так далеко цена доходила в 6 случаях из 10 за последние 90 суток."
    )


# --- Цена цели и округление (§4.9) -----------------------------------------

def test_target_price_directions_and_rounding() -> None:
    """Цена цели идёт вверх для покупки и вниз для продажи, дешёвые токены не теряются."""
    assert target_price(65000.0, 0.54, "buy") == pytest.approx(65351.0, abs=0.5)
    assert target_price(65000.0, 0.54, "sell") == pytest.approx(64649.0, abs=0.5)
    # DOGE: округление до двух знаков уничтожило бы цель, шесть значащих цифр — нет.
    doge = target_price(0.221, 0.18, "buy")
    assert doge != pytest.approx(0.22, abs=1e-9)
    assert round_significant(0.2213978, 6) == pytest.approx(0.221398)
    assert round_significant(65480.34567, 6) == pytest.approx(65480.3)


# --- Предпроверка §1 --------------------------------------------------------

def test_precheck_rejects_short_and_stale_series() -> None:
    """Короткий и устаревший ряд предпроверку не проходит, причины машиночитаемы."""
    candles = _series([100.0] * 100)
    check = check_series(
        candles, now=_T0 + timedelta(hours=200),
        min_run_hours=2160, max_age_hours=3, max_flat_pct=5.0,
    )
    assert check.ok is False
    assert FAIL_SHORT_SERIES in check.failures
    assert FAIL_STALE_LAST_CANDLE in check.failures
    assert all(key.isascii() for key in check.failures)


def test_longest_run_ignores_total_length() -> None:
    """Порог сравнивается с НЕПРЕРЫВНЫМ отрезком, а не с числом свечей."""
    first = _series([100.0] * 100)
    second = [
        Candle(open_time=_T0 + timedelta(hours=200 + i), open=100.0,
               high=101.0, low=99.0, close=100.0)
        for i in range(150)
    ]
    both = first + second
    assert len(both) == 250
    assert longest_run_hours(both) == 150


# --- 10.6 ------------------------------------------------------------------

class _RecordingPool:
    """Двойник пула asyncpg, запоминающий КАЖДЫЙ выполненный запрос.

    Нужен, чтобы утверждение «пересчёт не трогает signal_targets» проверялось
    по фактически отправленным в базу запросам, а не по намерению автора.
    """

    def __init__(self, rows: dict[str, object] | None = None) -> None:
        self.queries: list[str] = []
        self.rows = rows or {}

    async def execute(self, query: str, *args: object) -> str:
        self.queries.append(query)
        return "OK"

    async def fetch(self, query: str, *args: object) -> list:
        self.queries.append(query)
        return list(self.rows.get("fetch", []))

    async def fetchrow(self, query: str, *args: object):
        self.queries.append(query)
        return self.rows.get("fetchrow")

    async def fetchval(self, query: str, *args: object):
        self.queries.append(query)
        return self.rows.get("fetchval")


def _writes_to(queries: list[str], table: str) -> list[str]:
    """Запросы, ИЗМЕНЯЮЩИЕ таблицу (INSERT/UPDATE/DELETE/TRUNCATE)."""
    changed = []
    for query in queries:
        upper = " ".join(query.upper().split())
        if table.upper() not in upper:
            continue
        if any(word in upper for word in ("INSERT", "UPDATE", "DELETE", "TRUNCATE")):
            changed.append(query)
    return changed


@pytest.mark.asyncio
async def test_signal_targets_frozen(monkeypatch) -> None:
    """Пересчёт risk_targets не выполняет НИ ОДНОГО изменения signal_targets (§3, §7).

    Проверяется по журналу отправленных в базу запросов: строки уже выданных
    целей неизменны, иначе проверить систему постфактум невозможно — сегодняшняя
    цель заменила бы вчерашнюю, и сверить сказанное со случившимся было бы не с чем.
    """
    from src.core.db import DB
    from src.risk import runner as risk_runner

    pool = _RecordingPool()
    # Свойство ``pool`` подменяется целиком: так через двойник проходит КАЖДЫЙ
    # запрос слоя доступа, включая те, что метод пишет напрямую.
    monkeypatch.setattr(DB, "pool", property(lambda self: pool))
    monkeypatch.setattr(risk_runner, "db", DB())
    # Догрузка свежего края в этом наборе не участвует: проверяется запись, а
    # не сеть. Выключаем её так же, как это делает настройка на сервере.
    monkeypatch.setattr(risk_runner.settings, "RISK_BACKFILL_ENABLED", False)
    # Инструмент существует, свечей нет → строки data_gap. Для проверки
    # неизменности signal_targets важно, что запись вообще происходит.
    pool.rows["fetchval"] = 1

    outcomes = await risk_runner.recompute(now=datetime(2026, 8, 24, 3, 40, tzinfo=UTC))

    assert outcomes, "пересчёт не дал ни одного итога"
    assert _writes_to(pool.queries, "risk_targets"), "пересчёт ничего не записал"
    assert _writes_to(pool.queries, "signal_targets") == [], (
        "пересчёт изменил signal_targets — замороженные цели обязаны быть неизменны"
    )


@needs_db
@pytest.mark.asyncio
async def test_signal_targets_frozen_on_real_db(monkeypatch) -> None:
    """То же на настоящей базе: снимок signal_targets до и после пересчёта совпадает."""
    import asyncpg

    from src.core.db import db as real_db
    from src.risk import runner as risk_runner

    # Проверяется НЕИЗМЕННОСТЬ записи, а не загрузка: догрузка свежего края
    # выключается, иначе набор зависел бы от доступности биржи.
    monkeypatch.setattr(risk_runner.settings, "RISK_BACKFILL_ENABLED", False)

    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    try:
        original = getattr(real_db, "_pool", None)
        real_db._pool = pool  # noqa: SLF001 — подмена пула на тестовый
        await real_db.ensure_risk_targets_schema()
        before = await pool.fetch("SELECT * FROM signal_targets ORDER BY signal_id;")
        await risk_runner.recompute(now=datetime.now(UTC))
        after = await pool.fetch("SELECT * FROM signal_targets ORDER BY signal_id;")
        assert [dict(r) for r in before] == [dict(r) for r in after]
    finally:
        real_db._pool = original  # noqa: SLF001
        await pool.close()


# --- 10.7 ------------------------------------------------------------------

class _FailingTargetsDB:
    """Двойник слоя БД: расчёт целей падает, всё остальное работает."""

    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.failures: list[tuple[str, str]] = []

    async def get_latest_agent_output(self, agent: str, instrument_id: int):
        return {
            "agent": agent, "signal": "bullish", "confidence": 0.9,
            "ts": datetime.now(UTC),
        }

    async def get_last_inputs_hash(self, instrument_id: int) -> None:
        return None

    async def get_active_calibration(self, logic_version: int) -> None:
        return None

    async def get_price_at(self, instrument_id: int, ts: datetime) -> float:
        return 65000.0

    async def get_latest_risk_target(self, instrument_id, horizon_h, direction):
        raise RuntimeError("таблицы risk_targets нет на этом томе")

    async def save_signal(self, *args, **kwargs) -> int:
        self.saved.append(kwargs)
        return 4242

    async def record_agent_failure(self, agent, error_type, exc_type, detail) -> None:
        self.failures.append((agent, error_type))


@pytest.mark.asyncio
async def test_signal_written_without_targets(monkeypatch) -> None:
    """Исключение в расчёте целей не мешает записи сигнала, сбой учтён (§6).

    Сигнал важнее украшения: без сигнала система слепа, без цели — только менее
    удобна. Факт сбоя при этом не теряется — он попадает в ``agent_failures``
    под именем ``risk_targets``.
    """
    from src.decision import agent as decision_agent

    fake = _FailingTargetsDB()
    monkeypatch.setattr(decision_agent, "db", fake)
    monkeypatch.setattr(decision_agent, "get_redis", lambda: None)

    worker = decision_agent.DecisionAgent(
        instrument_id=1,
        agent_instruments={"market": 1, "liquidity": 1, "futures": 2},
        interval=60, weights={"market": 1.0, "liquidity": 1.0, "futures": 1.0},
        threshold=0.3, min_agents=2, freshness_sec=300, token="BTC",
    )
    await worker.decide_once()

    assert len(fake.saved) == 1, "сигнал не записан"
    assert fake.saved[0]["targets"] is None, "цели должны отсутствовать, а не быть выдуманы"
    assert ("risk_targets", "compute") in fake.failures, "сбой целей не учтён"


@pytest.mark.asyncio
async def test_wait_decision_gets_no_targets(monkeypatch) -> None:
    """Решение wait целей не получает вовсе (§6.1): сделки нет — хода нет."""
    from src.decision import agent as decision_agent

    fake = _FailingTargetsDB()

    async def _neutral(agent: str, instrument_id: int):
        return {
            "agent": agent, "signal": "neutral", "confidence": 0.5,
            "ts": datetime.now(UTC),
        }

    fake.get_latest_agent_output = _neutral  # type: ignore[assignment]
    monkeypatch.setattr(decision_agent, "db", fake)
    monkeypatch.setattr(decision_agent, "get_redis", lambda: None)

    worker = decision_agent.DecisionAgent(
        instrument_id=1,
        agent_instruments={"market": 1, "liquidity": 1, "futures": 2},
        interval=60, weights={"market": 1.0, "liquidity": 1.0, "futures": 1.0},
        threshold=0.3, min_agents=2, freshness_sec=300, token="BTC",
    )
    await worker.decide_once()

    assert fake.saved[0]["targets"] is None
    assert fake.failures == [], "wait не должен порождать записей о сбоях"


def test_freeze_row_records_missing_target_instead_of_hiding_it() -> None:
    """Нет строки risk_targets — строка signal_targets всё равно пишется (§6.5)."""
    from src.decision.agent import DecisionAgent
    from src.risk.targets import REASON_NO_RISK_TARGET

    row = DecisionAgent._freeze_one(4, "buy", 65000.0, None)
    assert row["target_pct"] is None
    assert row["no_target_reason"] == REASON_NO_RISK_TARGET
    assert row["price_at_signal"] == 65000.0

    # Цель есть — считается цена цели и переносится доля касаний.
    filled = DecisionAgent._freeze_one(4, "buy", 65000.0, {
        "computed_at": datetime(2026, 8, 24, 3, 40, tzinfo=UTC),
        "targets_version": 1, "target_pct": 0.54, "hit_rate": 0.6,
        "covers_fees": True, "no_target_reason": None,
    })
    assert filled["target_price"] == pytest.approx(65351.0, abs=0.5)
    assert filled["hit_rate"] == pytest.approx(0.6)
    assert filled["no_target_reason"] is None

    # Причина отказа переносится из risk_targets как есть, а не переписывается.
    gap = DecisionAgent._freeze_one(4, "buy", 65000.0, {
        "computed_at": datetime(2026, 8, 24, 3, 40, tzinfo=UTC),
        "targets_version": 1, "target_pct": None, "hit_rate": None,
        "covers_fees": False, "no_target_reason": "data_gap",
    })
    assert gap["no_target_reason"] == "data_gap"


# --- 10.9 ------------------------------------------------------------------

class _CapturingConn:
    """Двойник соединения: запоминает запрос и аргументы, данных не возвращает."""

    def __init__(self, version: int | None = 5) -> None:
        self.query = ""
        self.args: tuple = ()
        self.version = version

    async def fetch(self, query: str, *args):
        self.query = query
        self.args = args
        return []

    async def fetchval(self, query: str, *args):
        self.query = query
        self.args = args
        return self.version


@pytest.mark.asyncio
async def test_export_logic_version_filter() -> None:
    """Обе выборки выгрузки фильтруют по версии логики и отсекают версию 0 (§9)."""
    from src.export import queries

    conn = _CapturingConn(version=5)
    resolved = await queries.resolve_logic_version(conn, "current")
    assert resolved == 5
    assert "logic_version_windows" in conn.query
    assert "logic_version > 0" in conn.query

    for fetcher in (
        queries.fetch_independent_by_token_horizon,
        queries.fetch_outcome_correlation,
    ):
        conn = _CapturingConn()
        await fetcher(conn, [1, 4, 12, 24], 5)
        assert "s.logic_version = $2" in conn.query, fetcher.__name__
        assert conn.args[1] == 5

        # При «all» фильтра по конкретной версии нет, но версия 0 отсекается
        # ВСЕГДА: «версия неизвестна» — это не версия (§9.5).
        conn = _CapturingConn()
        await fetcher(conn, [1, 4, 12, 24], None)
        assert "s.logic_version <> 0" in conn.query, fetcher.__name__
        assert len(conn.args) == 1


@pytest.mark.asyncio
async def test_export_logic_version_values_are_validated() -> None:
    """Допустимы current, целое число и all; ноль и мусор — остановка выгрузки."""
    from src.export import queries

    assert await queries.resolve_logic_version(_CapturingConn(), "all") is None
    assert await queries.resolve_logic_version(_CapturingConn(), "4") == 4
    assert await queries.resolve_logic_version(_CapturingConn(), "") == 5

    with pytest.raises(queries.ExportVersionError):
        await queries.resolve_logic_version(_CapturingConn(), "0")
    with pytest.raises(queries.ExportVersionError):
        await queries.resolve_logic_version(_CapturingConn(), "последняя")
    # current без зафиксированной границы версии — тоже остановка, а не «all».
    with pytest.raises(queries.ExportVersionError):
        await queries.resolve_logic_version(_CapturingConn(version=None), "current")


def test_correlation_sheet_names_the_version() -> None:
    """В листе «Корреляция токенов» появилась колонка версии (§9.4)."""
    from src.export.transform import (
        CORRELATION_HEADER,
        MIXED_VERSIONS_DISCLAIMER,
        build_correlation_row,
    )

    assert CORRELATION_HEADER[-1] == "logic_version"
    row = build_correlation_row({
        "horizon_h": 4, "token_a": "BTC", "token_b": "ETH",
        "n": 12, "r": 0.71, "logic_version": "5",
    })
    assert len(row) == len(CORRELATION_HEADER)
    assert row[-1] == "5"
    # Оговорка о смешивании версий — прямая, без обиняков.
    text = " ".join(str(cell) for cell in MIXED_VERSIONS_DISCLAIMER)
    assert "СМЕШИВАЕТ РАЗНЫЕ" in text


@needs_db
@pytest.mark.asyncio
async def test_export_logic_version_filter_on_real_db() -> None:
    """На настоящей базе: выборка не содержит чужой версии и версии 0 (§9.5)."""
    import asyncpg

    from src.export import queries

    pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            versions = {
                row["logic_version"]
                for row in await queries.fetch_independent_by_token_horizon(
                    conn, [1, 4, 12, 24], 5
                )
            }
            assert versions <= {5}, versions

            mixed = await queries.fetch_independent_by_token_horizon(
                conn, [1, 4, 12, 24], None
            )
            assert all(row["logic_version"] != 0 for row in mixed)
    finally:
        await pool.close()


# --- Цель в готовом тексте сигнала -----------------------------------------

def test_target_block_lands_in_the_signal_message() -> None:
    """Блок цели встаёт в сообщение сразу после цены, ничего не ломая (§8).

    Место под блок было предусмотрено Этапом 8.3: сборка текста не переписана
    ни одной строкой, добавлен только сам блок.
    """
    from src.notify.agent import SignalFormatConfig, format_signal_message

    signal = {
        "id": 1, "instrument_id": 1, "ts": datetime(2026, 8, 24, 12, tzinfo=UTC),
        "decision": "buy", "probability": 0.82,
        "agents_payload": [
            {"agent": "market", "signal": "bullish", "confidence": 0.9},
            {"agent": "liquidity", "signal": "bullish", "confidence": 0.8},
            {"agent": "futures", "signal": "neutral", "confidence": 0.4},
        ],
        "calibrated_probability": None,
    }
    cfg = SignalFormatConfig(
        symbol="BTC/USDT", tz_name="Europe/Moscow", primary_horizon="4h", horizon_h=4
    )
    block = target_block({
        "direction": "buy", "target_pct": 0.54, "target_price": 65480.0,
        "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None,
    })
    text = format_signal_message(signal, 65000.0, cfg, {}, target_block=block)

    assert "Цель: 65 480 (+0,54%)" in text
    assert "в 6 случаях из 10" in text
    # Цель стоит ПОСЛЕ цены и ДО разбора мнений — там, где её ищет читатель.
    assert text.index("Цена сейчас") < text.index("Цель:") < text.index("Почему такой вывод")
    # Без цели сообщение остаётся прежним: блок просто отсутствует.
    without = format_signal_message(signal, 65000.0, cfg, {})
    assert "Цель:" not in without


def test_signal_card_shows_target_for_chosen_horizon_only() -> None:
    """Карточка /signal показывает цель ТОЛЬКО выбранного горизонта (§8)."""
    from src.bot.handlers import render_signal_card

    card = {
        "id": 7, "instrument_id": 1, "ts": datetime(2026, 8, 24, 12, tzinfo=UTC),
        "decision": "buy", "probability": 0.8, "agents_payload": "[]",
        "notified": False, "notified_at": None, "status": "open",
        "symbol": "BTC/USDT", "price_at_signal": 65000.0,
        "targets_by_horizon": {
            4: {"direction": "buy", "target_pct": 0.54, "target_price": 65351.0,
                "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None},
            24: {"direction": "buy", "target_pct": 1.20, "target_price": 65780.0,
                 "hit_rate": 0.6, "covers_fees": True, "no_target_reason": None},
        },
    }
    text = render_signal_card(card, datetime(2026, 8, 24, 13, tzinfo=UTC), 4)

    assert "Цель на 4 ч:" in text
    assert "65 351" in text
    assert "65 780" not in text, "показана цель невыбранного горизонта"
    assert "случаях из 10" in text


def test_signal_card_wait_has_no_target() -> None:
    """Решение wait цели не имеет: сделки нет — хода нет."""
    from src.bot.handlers import render_signal_card

    card = {
        "id": 8, "instrument_id": 1, "ts": datetime(2026, 8, 24, 12, tzinfo=UTC),
        "decision": "wait", "probability": 0.1, "agents_payload": "[]",
        "notified": False, "notified_at": None, "status": "open",
        "symbol": "BTC/USDT", "price_at_signal": 65000.0, "targets_by_horizon": {},
    }
    text = render_signal_card(card, datetime(2026, 8, 24, 13, tzinfo=UTC), 4)
    assert "Цель" not in text
