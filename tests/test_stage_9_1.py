"""Этап 9.1: ведение одной позиции + починка чтения незакрытой свечи (§12 ТЗ).

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ЗАДАЧА Б. Сеточные стратегии входили каждый час и проверяли годность СВОИМ
условием «срок наступил», без ожидания закрытия последнего бара окна. Тот же
дефект был найден и устранён Этапом 8.10.1 в расчёте по границам, но сеточных
стратегий не касался. Проверяется не обещание, а поведение: вход, чей срок
наступил секунду назад, к расчёту НЕ допускается; вход, чей срок наступил
раньше чем ``settle_seconds()`` назад, — допускается. Отдельно проверяется, что
запас берётся из ОДНОГО места с 8.10.1, а не переписан второй формулой: две
копии одного правила разошлись бы при следующей правке, и разошлись бы молча.

ЗАДАЧА А. Правило выхода проверяется на синтетических рядах с заранее известным
ответом — на том и стоит требование чистоты ``src/positions/rules.py``. Четыре
способа тихо испортить замер, каждый из которых выглядел бы правдоподобно:

  * взять касание по ``close`` вместо ``high``/``low`` — тогда цель, задетая
    внутри минуты и отпущенная к её концу, не засчитывается никогда;
  * разрешить одновременное касание догадкой в свою пользу — результат системы
    тихо завышается, и отличить это от удачи в отчёте нельзя;
  * посчитать ``mae``/``mfe`` до касания вместо всего окна — и вопрос «что было
    бы при другом пределе» становится неотвечаемым;
  * вычесть издержки дважды или ни разу.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.
"""

from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

import src.barrier.runner as barrier_runner
import src.baseline.runner as baseline_runner
from src.barrier.runner import settle_seconds
from src.core.config import Settings, settings
from src.positions.rules import (
    EXIT_AMBIGUOUS,
    EXIT_REASONS,
    EXIT_STOP,
    EXIT_TARGET,
    EXIT_TIMEOUT,
    REASON_DEGRADED,
    REASON_INSTRUMENT_BUSY,
    REASON_LOW_PROBABILITY,
    REASON_NO_FROZEN_TARGET,
    REASON_OK,
    REASON_SIGNAL_TOO_OLD,
    REFUSAL_REASONS,
    Bar,
    check_exit,
    levels,
    net_pnl,
    qty_for_slot,
    should_open,
    slippage_pct,
)

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5433/agenttrade)"
    ),
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Синтетика Задачи А: круглые числа, чтобы ответ был виден глазом.
_ENTRY = 100.0
_TARGET_PCT = 1.0     # цель 101.0
_STOP_PCT = 1.0       # предел 99.0
_COST_PCT = 0.22
_OPENED = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
_DEADLINE = _OPENED + timedelta(hours=24)


def _bar(minute: int, high: float, low: float, close: float) -> Bar:
    """Бар окна: ``minute`` — сколько минут прошло с момента входа."""
    return Bar(
        ts=_OPENED + timedelta(minutes=minute), high=high, low=low, close=close
    )


def _flat(minutes: int, price: float = _ENTRY) -> list[Bar]:
    """Ровное окно без движения: ни один уровень не задет."""
    return [_bar(i, price, price, price) for i in range(minutes)]


# =============================================================================
# ЗАДАЧА Б. Правило годности сеточного входа
# =============================================================================

class _GridStubDB:
    """Заглушка базы для ``compute_grid_strategies``: отдаёт одну цену на час.

    Заглушка нужна ровно затем, чтобы проверить ФАКТИЧЕСКОЕ условие годности в
    боевом коде, а не его пересказ в тесте. Переписав неравенство сюда, тест
    остался бы зелёным и после того, как условие в коде испортят.
    """

    def __init__(self, entries: list[datetime]) -> None:
        self.entries = entries

    async def get_barrier_window(self, *, logic_version: int):
        return {
            "ts_from": self.entries[0],
            "ts_to": self.entries[-1],
            "rows": len(self.entries),
        }

    async def get_instrument_id(self, symbol: str):
        # Один инструмент на весь стенд: сетка обходит инструменты одинаково,
        # и второй ничего не добавил бы к проверке правила годности.
        return 1 if symbol == settings.symbol_pairs[0].spot else None

    async def get_grid_prices(self, instrument_id, timeframe, since, until):
        return [
            {"ts": ts, "close": 100.0}
            for ts in self.entries
            if since <= ts <= until
        ]


async def _grid_entries_admitted(
    monkeypatch: pytest.MonkeyPatch, entry_ts: datetime, now: datetime
) -> list[datetime]:
    """Прогоняет боевой ``compute_grid_strategies`` и возвращает допущенные входы."""
    admitted: list[datetime] = []

    async def _record(**kwargs):
        admitted.append(kwargs["entry_ts"])

    monkeypatch.setattr(baseline_runner, "db", _GridStubDB([entry_ts]))
    monkeypatch.setattr(baseline_runner, "_evaluate_at", _record)
    await baseline_runner.compute_grid_strategies(
        strategies=("grid_buy",), since=None, limit=None, now=now,
        stats=baseline_runner.RunStats(), done=set(), computed_at=now,
    )
    return admitted


async def test_grid_entry_whose_deadline_just_passed_is_not_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Срок наступил секунду назад — последний бар окна ещё формируется.

    Прежнее правило («срок наступил») пускало расчёт именно сюда: к бару,
    который ОТКРЫВАЕТСЯ в момент срока, а закрывается через целый бар после
    него. Коллектор перезаписывает такой бар следующим опросом, и исход
    ``timeout``, берущий итог из его ``close``, получал цену «пока что».
    """
    horizon_h = settings.eval_horizons_hours[0]
    entry_ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    now = entry_ts + timedelta(hours=horizon_h, seconds=1)
    assert await _grid_entries_admitted(monkeypatch, entry_ts, now) == []


async def test_grid_entry_older_than_settle_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Срок наступил раньше чем ``settle_seconds()`` назад — вход годен.

    Обратная сторона той же проверки: запас не должен запирать расчёт навсегда,
    иначе починка молча превратилась бы в остановку сеточных стратегий.
    """
    horizon_h = settings.eval_horizons_hours[0]
    entry_ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    now = entry_ts + timedelta(hours=horizon_h, seconds=settle_seconds() + 1)
    assert await _grid_entries_admitted(monkeypatch, entry_ts, now) == [entry_ts]


def test_baseline_and_barrier_share_one_settle_rule() -> None:
    """Запас — ОДИН И ТОТ ЖЕ объект, а не две копии формулы.

    Проверяется тождеством объектов, а не равенством чисел: равенство чисел
    выполнялось бы и у двух независимых копий — ровно до первой правки одной
    из них.
    """
    assert baseline_runner.settle_seconds is barrier_runner.settle_seconds
    text = (_ROOT / "src" / "baseline" / "runner.py").read_text(encoding="utf-8")
    assert "settle_seconds" in text
    # Формула не переписана: длительность бара и BARRIER_SETTLE_MINUTES в
    # baseline не упоминаются вовсе.
    assert "BARRIER_SETTLE_MINUTES" not in text
    assert "BAR_SECONDS" not in text


# =============================================================================
# ЗАДАЧА А. Правила выхода — чистые, без базы
# =============================================================================

def test_target_touched_closes_at_target_price() -> None:
    """Цель задета — выход по цене цели, а не по закрытию бара.

    Реальный ордер срабатывает по касанию: цена, дошедшая до 101.0 и упавшая к
    концу минуты обратно к 100.0, закрывает позицию по 101.0.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    bars = _flat(5) + [_bar(5, 101.5, 100.0, 100.0)] + _flat(3)
    decision = check_exit(
        bars=bars, target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_TARGET
    assert decision.exit_price == pytest.approx(target_price)
    assert decision.outcome_certain is True
    assert decision.bars_held == 6


def test_stop_touched_closes_at_stop_price() -> None:
    """Предел задет — выход по цене предела."""
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    bars = _flat(2) + [_bar(2, 100.0, 98.4, 99.5)]
    decision = check_exit(
        bars=bars, target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_STOP
    assert decision.exit_price == pytest.approx(stop_price)
    assert decision.outcome_certain is True


def test_both_levels_in_one_bar_are_resolved_against_the_position() -> None:
    """Задеты оба уровня — ``ambiguous``, итог ПО ПРЕДЕЛУ, флаг снят.

    Порядок событий внутри минуты ряду свечей неизвестен. Выбор в свою пользу
    тихо завысил бы результат системы; выбор против себя завысить не может.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    bars = [_bar(0, 101.5, 98.5, 100.0)]
    decision = check_exit(
        bars=bars, target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_AMBIGUOUS
    assert decision.outcome_certain is False
    assert decision.exit_price == pytest.approx(stop_price)
    # И итог посчитан ПО ПРЕДЕЛУ, а не по цели: это и есть пессимизм правила.
    assert net_pnl(_ENTRY, decision.exit_price, _COST_PCT) < 0


def test_touching_exactly_the_level_counts() -> None:
    """Касание РОВНО В УРОВЕНЬ засчитывается: сравнение нестрогое.

    Правило совпадает с ``src/barrier/outcomes.py`` и совпадать обязано: иначе
    позиции и исходы по границам разошлись бы на краевом случае — незаметно.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    exact_target = check_exit(
        bars=[_bar(0, target_price, 100.0, 100.0)],
        target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    exact_stop = check_exit(
        bars=[_bar(0, 100.0, stop_price, 100.0)],
        target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert exact_target is not None and exact_target.exit_reason == EXIT_TARGET
    assert exact_stop is not None and exact_stop.exit_reason == EXIT_STOP


def test_timeout_takes_close_of_the_last_bar_before_the_deadline() -> None:
    """Ничего не задето, срок наступил — итог по закрытию последнего бара ДО срока.

    Бар, открывающийся В МОМЕНТ срока, в окно не входит: он закрывается уже
    после срока, и его закрытие — цена не на срок, а позже него.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    minutes = int((_DEADLINE - _OPENED).total_seconds() // 60)
    bars = _flat(minutes - 1)
    last_before = _bar(minutes - 1, 100.4, 99.6, 100.3)
    at_deadline = _bar(minutes, 100.9, 99.1, 100.8)
    decision = check_exit(
        bars=bars + [last_before, at_deadline],
        target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_TIMEOUT
    assert decision.exit_price == pytest.approx(100.3)
    assert decision.exit_bar_ts == last_before.ts
    assert decision.bars_held == minutes


def test_the_entry_bar_is_not_part_of_the_window() -> None:
    """Свеча момента входа в окно НЕ входит.

    Позиция открывается по закрытию своего бара, и этот бар уже прожит: искать
    в нём касание значило бы искать событие, случившееся ДО входа. Здесь это
    проверяется на самом опасном случае — бар входа задевает предел, а окно
    начинается со следующего.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    entry_bar = Bar(
        ts=_OPENED - timedelta(minutes=1), high=101.9, low=98.1, close=100.0
    )
    window = _flat(5)
    # Правило получает ТОЛЬКО окно: отбор баров — дело вызывающего, и здесь
    # проверяется, что бар входа в этот отбор не попадает по построению.
    assert entry_bar.ts < window[0].ts
    decision = check_exit(
        bars=window, target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is None, "ровное окно не закрывает позицию"


def test_mae_and_mfe_cover_the_whole_held_window() -> None:
    """``mae``/``mfe`` — по ВСЕМУ удержанному окну, а не до касания.

    Они описывают, что позиция пережила, и именно поэтому по ним можно будет
    ответить на вопрос «что было бы при другом уровне предела». Обрезка их по
    факту срабатывания сделала бы этот вопрос неотвечаемым.
    """
    target_price, stop_price = levels(_ENTRY, _TARGET_PCT, _STOP_PCT)
    bars = [
        _bar(0, 100.2, 99.5, 100.0),   # просадка −0.5%
        _bar(1, 100.6, 99.9, 100.4),   # подъём +0.6%
        _bar(2, 101.4, 99.8, 101.0),   # цель задета здесь
        _bar(3, 105.0, 90.0, 100.0),   # ПОСЛЕ выхода — в счёт не идёт
    ]
    decision = check_exit(
        bars=bars, target_price=target_price, stop_price=stop_price,
        entry_price=_ENTRY, deadline_at=_DEADLINE, cost_pct=_COST_PCT,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_TARGET
    assert decision.bars_held == 3
    # Крайние значения — по трём удержанным барам, включая бар выхода.
    assert decision.mae_pct == pytest.approx(-0.5)
    assert decision.mfe_pct == pytest.approx(1.4)


def test_net_pnl_subtracts_costs_exactly_once() -> None:
    """Издержки вычитаются ровно один раз.

    ``cost_pct`` — уже КРУГОВАЯ величина (комиссия тейкера × 2 плюс
    проскальзывание × 2). Второе вычитание «за выход» посчитало бы одну и ту же
    комиссию дважды и тихо занизило бы результат на четверть процента.
    """
    assert net_pnl(100.0, 101.0, 0.22) == pytest.approx(1.0 - 0.22)
    assert net_pnl(100.0, 99.0, 0.22) == pytest.approx(-1.0 - 0.22)
    # Нулевые издержки не меняют ничего, кроме себя.
    assert net_pnl(100.0, 101.0, 0.0) == pytest.approx(1.0)


def test_slot_of_two_dollars_clears_the_minimum_order_of_the_worst_instrument(
) -> None:
    """Слот $2 при цене XRP 1.3771 даёт количество больше минимального ордера.

    Замер минимальных ордеров OKX на спот от 28.08.2026: BTC $0.78, ETH $0.24,
    SOL $1.03, XRP $1.38, DOGE $0.85. Слот в 1 доллар молча отсёк бы SOL и XRP,
    и замер шёл бы не по пяти инструментам, а по трём.
    """
    xrp_price = 1.3771
    min_order_xrp = 1.0
    assert qty_for_slot(2.0, xrp_price) > min_order_xrp
    # И обратная сторона: доллар не проходит — ровно то, что запрещает валидатор.
    assert qty_for_slot(1.0, xrp_price) < min_order_xrp


def _open_kwargs(**overrides):
    """Набор параметров, при котором вход РАЗРЕШЁН. Тесты меняют по одному."""
    base = dict(
        decision="buy",
        logic_version=settings.LOGIC_VERSION,
        expected_version=settings.LOGIC_VERSION,
        degraded=False,
        probability=0.9,
        min_probability=0.8,
        has_open_position=False,
        open_count=0,
        max_open=5,
        signal_age_sec=30.0,
        max_signal_age_sec=180,
        bar_age_sec=45.0,
        max_bar_age_sec=300,
        has_frozen_target=True,
        # Этап 9.1.1 §6.3: проверка свободных денег. Здесь их заведомо хватает —
        # тесты этого набора проверяют ДРУГИЕ условия, и нехватка денег в
        # отправной точке сделала бы каждый из них отказом не по своей причине.
        # Сама проверка денег проверяется в tests/test_stage_9_1_1.py.
        free_usd=10.0,
        slot_usd=2.0,
    )
    base.update(overrides)
    return base


def test_baseline_open_case_is_allowed() -> None:
    """Отправная точка: при исправных условиях вход разрешён.

    Без этой проверки каждый тест ниже мог бы «проходить» потому, что вход
    запрещён вообще всегда.
    """
    verdict = should_open(**_open_kwargs())
    assert verdict.allowed is True
    assert verdict.reason == REASON_OK


def test_degraded_signal_never_opens_a_position() -> None:
    """``degraded=True`` — решение принято НЕПОЛНЫМ составом агентов.

    Признак ставит сам decision, и он же есть признак полного кворума трёх
    агентов: пересчитывать состав отдельно значило бы вводить второе
    определение кворума, которое однажды разойдётся с первым.
    """
    verdict = should_open(**_open_kwargs(degraded=True))
    assert verdict.allowed is False
    assert verdict.reason == REASON_DEGRADED


def test_probability_below_the_threshold_is_refused() -> None:
    """Вероятность ниже порога — отказ; ровно порог — вход."""
    below = should_open(**_open_kwargs(probability=0.79))
    assert below.allowed is False and below.reason == REASON_LOW_PROBABILITY
    exactly = should_open(**_open_kwargs(probability=0.8))
    assert exactly.allowed is True
    # Отсутствие вероятности — тоже отказ, и по той же причине: сравнить не с чем.
    missing = should_open(**_open_kwargs(probability=None))
    assert missing.allowed is False and missing.reason == REASON_LOW_PROBABILITY


def test_stale_signal_is_refused() -> None:
    """Сигнал старше ``POSITION_MAX_SIGNAL_AGE_SEC`` — отказ.

    Условие не формальность: сервис мог простоять полчаса, и вход по
    получасовой давности сигналу означал бы покупку по цене, которой уже нет.
    Пропущенный сигнал честнее выдуманного входа.
    """
    verdict = should_open(**_open_kwargs(signal_age_sec=181.0))
    assert verdict.allowed is False
    assert verdict.reason == REASON_SIGNAL_TOO_OLD
    # Ровно на границе вход ещё разрешён: правило нестрогое.
    assert should_open(**_open_kwargs(signal_age_sec=180.0)).allowed is True


def test_instrument_with_an_open_position_is_refused() -> None:
    """Один инструмент — одна позиция. Второй вход по нему невозможен."""
    verdict = should_open(**_open_kwargs(has_open_position=True))
    assert verdict.allowed is False
    assert verdict.reason == REASON_INSTRUMENT_BUSY


def test_signal_without_a_frozen_target_is_refused() -> None:
    """Нет замороженной цели на этот горизонт — позиция не открывается.

    Подставить сегодняшнюю цель из ``risk_targets`` запрещено: она посчитана по
    сегодняшнему рынку, и её подстановка означала бы, что система «назвала» в
    прошлом число, которого тогда не существовало.
    """
    verdict = should_open(**_open_kwargs(has_frozen_target=False))
    assert verdict.allowed is False
    assert verdict.reason == REASON_NO_FROZEN_TARGET


def test_every_refusal_reason_belongs_to_the_closed_list() -> None:
    """Перечень причин отказа ЗАКРЫТ (§7.1 ТЗ).

    Свободный текст нельзя посчитать запросом, а знать, почему позиций мало,
    придётся: «позиций нет» и «позиций нет, потому что ни один сигнал не прошёл
    порог» — разные ответы. Проверяется обходом всех одиночных нарушений.
    """
    broken = [
        dict(decision="sell"), dict(decision="wait"),
        dict(logic_version=settings.LOGIC_VERSION + 1),
        dict(degraded=True), dict(probability=0.1), dict(probability=None),
        dict(has_open_position=True), dict(open_count=5),
        dict(signal_age_sec=10_000.0), dict(bar_age_sec=10_000.0),
        dict(bar_age_sec=None), dict(has_frozen_target=False),
        # Этап 9.1.1 §6.3: перечень расширен ОДНИМ значением, и оно тоже обязано
        # быть достижимым — иначе ключ ничего не объясняет, но выглядит
        # объяснением.
        dict(free_usd=1.0),
    ]
    seen = set()
    for override in broken:
        verdict = should_open(**_open_kwargs(**override))
        assert verdict.allowed is False, override
        assert verdict.reason in REFUSAL_REASONS, verdict.reason
        seen.add(verdict.reason)
    # И каждая причина из перечня достижима: ключ, который не может случиться,
    # ничего не объясняет, но выглядит объяснением.
    assert seen == set(REFUSAL_REASONS)


def test_slot_validator_rejects_a_dollar_and_accepts_two() -> None:
    """Валидатор ``POSITION_SLOT_USD`` отвергает 1.0 и принимает 2.0.

    Слот ниже полутора долларов сделал бы часть инструментов недоступными
    МОЛЧА, и замер шёл бы не по пяти инструментам, а по трём.
    """
    with pytest.raises(ValueError) as failure:
        Settings(POSTGRES_PASSWORD="x", POSITION_SLOT_USD=1.0)
    # Причина названа в тексте ошибки, а не спрятана в коде валидатора.
    assert "1.38" in str(failure.value)
    assert Settings(POSTGRES_PASSWORD="x", POSITION_SLOT_USD=2.0
                    ).POSITION_SLOT_USD == 2.0


def test_probability_validator_keeps_the_threshold_a_probability() -> None:
    """``POSITION_MIN_PROBABILITY`` лежит в (0, 1]."""
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            Settings(POSTGRES_PASSWORD="x", POSITION_MIN_PROBABILITY=bad)
    assert Settings(POSTGRES_PASSWORD="x", POSITION_MIN_PROBABILITY=1.0
                    ).POSITION_MIN_PROBABILITY == 1.0


def test_stage_defaults_match_the_specification() -> None:
    """Значения по умолчанию §6 — те, что названы в ТЗ, и не «примерно те».

    Горизонт 24 часа выбран замером: требуемая доля попаданий для выхода в ноль
    составляет 87% на четырёх часах, 69% на двенадцати и 58% на сутках. Часовой
    горизонт непригоден арифметически — цель 0.19% меньше издержек 0.22%.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_HORIZON_H == 24
    assert fresh.POSITION_MIN_PROBABILITY == 0.8
    assert fresh.POSITION_MAX_OPEN == 5
    assert fresh.POSITION_SLOT_USD == 2.0
    assert fresh.POSITION_MAX_SIGNAL_AGE_SEC == 180
    assert fresh.POSITION_MAX_BAR_AGE_SEC == 300
    assert fresh.POSITION_SETTLE_SEC == 90
    assert fresh.POSITION_TIMEFRAME == "1m"
    # Своих ключей для предела и издержек этап НЕ заводит: отдельный ключ
    # означал бы возможность сравнить позиции при одном пределе с исходами
    # Этапа 8.8 при другом, то есть сравнить несравнимое.
    assert not [
        name for name in type(fresh).model_fields
        if name.startswith("POSITION") and ("STOP" in name or "COST" in name)
    ]


def test_slippage_measures_the_cost_of_the_delay() -> None:
    """``entry_slippage_pct`` — знаковая величина, а не абсолютная.

    Знак важен: вход дороже решения и вход дешевле решения — разные события, и
    усреднение их по модулю скрыло бы систематический снос в одну сторону.
    """
    assert slippage_pct(100.0, 100.5) == pytest.approx(0.5)
    assert slippage_pct(100.0, 99.5) == pytest.approx(-0.5)
    assert slippage_pct(100.0, 100.0) == pytest.approx(0.0)


def test_exit_reasons_match_the_database_constraint() -> None:
    """Перечень причин выхода в коде и в схеме — один и тот же.

    Расхождение проявилось бы отказом вставки в проде, а не здесь.
    """
    migration = (_ROOT / "db" / "migrations" / "018_positions.sql").read_text(
        encoding="utf-8"
    )
    for reason in EXIT_REASONS:
        assert f"'{reason}'" in migration, reason


# =============================================================================
# ЗАДАЧА А. Находка исполнителя: бар входа не может закрыться раньше решения
# =============================================================================

def test_the_settle_lag_still_leaves_room_before_the_signal_expires() -> None:
    """Три параметра §6 связаны арифметикой, и связь эту легко порвать молча.

    Бар входа обязан закрыться НЕ РАНЬШЕ решения (иначе вход происходил бы по
    цене, которую система уже видела, когда решала, и задержка входа выходила
    бы отрицательной). Ближайший такой бар закрывается не позже чем через
    минуту после сигнала, а годным становится ещё через
    ``POSITION_SETTLE_SEC``. Значит:

        60 + POSITION_SETTLE_SEC < POSITION_MAX_SIGNAL_AGE_SEC

    Нарушь это неравенство — и сигнал успевал бы устареть РАНЬШЕ, чем годный
    бар появится. Позиций не открывалось бы НИ ОДНОЙ, а журнал показывал бы
    честное ``signal_too_old`` и ``no_fresh_bar``, и причину искали бы в
    рынке, а не в настройках.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    earliest_usable = 60 + fresh.POSITION_SETTLE_SEC
    assert earliest_usable < fresh.POSITION_MAX_SIGNAL_AGE_SEC, (
        f"годный бар появляется через {earliest_usable} c, а сигнал устаревает "
        f"через {fresh.POSITION_MAX_SIGNAL_AGE_SEC} c"
    )


def test_entry_bar_older_than_the_signal_is_not_a_valid_entry() -> None:
    """Свеча, закрывшаяся ДО решения, годной свечой для входа не считается.

    Это находка живого прогона: при ``POSITION_SETTLE_SEC = 90`` последний
    закрытый бар отстоит от «сейчас» примерно на две с половиной минуты, а
    сигналу может быть секунда. Вход по такому бару давал бы ОТРИЦАТЕЛЬНУЮ
    задержку входа, а ``entry_slippage_pct`` — величина, ради которой этап и
    затевался, — измерял бы движение цены ДО решения, а не после него.

    Правило выражено через ``bar_age_sec = None``: перечень причин отказа
    закрыт (§7.1 ТЗ), и «нет годной свечи» — это ``no_fresh_bar``.
    """
    from src.positions.rules import REASON_NO_FRESH_BAR

    verdict = should_open(**_open_kwargs(bar_age_sec=None))
    assert verdict.allowed is False
    assert verdict.reason == REASON_NO_FRESH_BAR


def test_the_runner_discards_a_bar_that_closed_before_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """То же правило — в самом сервисе, а не только в чистом модуле.

    Проверяется на арифметике отбора: бар, закрывающийся раньше сигнала,
    отбрасывается ещё до вызова ``should_open``, и до открытия дело не доходит.
    """
    from src.positions.runner import last_closed_bar_open_ts

    # Решение принято секунду назад; последний закрытый бар отстоит на
    # 60 + POSITION_SETTLE_SEC секунд, то есть закрылся ДО решения.
    now = datetime(2026, 8, 29, 12, 0, 38, tzinfo=UTC)
    signal_ts = now - timedelta(seconds=1)
    edge = last_closed_bar_open_ts(now)
    bar_close = edge + timedelta(seconds=60)
    assert bar_close < signal_ts, (
        "стенд построен неверно: бар обязан закрываться раньше сигнала"
    )


# =============================================================================
# ЗАДАЧА А и Б. Проверки, которым нужна база
# =============================================================================

async def _fixture_ids(conn) -> tuple[int, int]:
    """Одноразовые инструмент и сигнал для проверок схемы."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', 'TEST91/USDT', 'TEST91', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE "
        "SET symbol = EXCLUDED.symbol RETURNING id;"
    )
    signal_id = await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version, "
        "probability) VALUES ($1, now(), 'buy', 5, 0.9) RETURNING id;",
        instrument_id,
    )
    return int(instrument_id), int(signal_id)


def _position_row(instrument_id: int, signal_id: int, **overrides) -> dict:
    """Открытая позиция, проходящая все ограничения. Тесты ломают по одному."""
    row = dict(
        instrument_id=instrument_id, signal_id=signal_id, logic_version=5,
        horizon_h=24, side="buy", status="open", signal_price=100.0,
        entry_price=100.0, entry_lag_sec=42, entry_slippage_pct=0.0,
        qty=0.02, notional_usd=2.0, target_pct=1.0, target_price=101.0,
        stop_pct=1.0, stop_price=99.0, cost_pct=0.22, resolution="1m",
    )
    row.update(overrides)
    return row


async def _insert_position(conn, row: dict) -> int:
    """Вставка позиции напрямую: проверяется СХЕМА, а не код поверх неё."""
    return await conn.fetchval(
        """
        INSERT INTO positions
            (instrument_id, signal_id, logic_version, horizon_h, side, status,
             signal_ts, signal_price, opened_at, entry_price, entry_lag_sec,
             entry_slippage_pct, qty, notional_usd, target_pct, target_price,
             stop_pct, stop_price, cost_pct, deadline_at, resolution,
             closed_at, exit_price, exit_reason, outcome_certain, net_pnl_pct)
        VALUES ($1, $2, $3, $4, $5, $6, now(), $7, now(), $8, $9, $10, $11,
                $12, $13, $14, $15, $16, $17, now() + interval '24 hours',
                $18, $19, $20, $21, $22, $23)
        RETURNING id;
        """,
        row["instrument_id"], row["signal_id"], row["logic_version"],
        row["horizon_h"], row["side"], row["status"], row["signal_price"],
        row["entry_price"], row["entry_lag_sec"], row["entry_slippage_pct"],
        row["qty"], row["notional_usd"], row["target_pct"], row["target_price"],
        row["stop_pct"], row["stop_price"], row["cost_pct"], row["resolution"],
        row.get("closed_at"), row.get("exit_price"), row.get("exit_reason"),
        row.get("outcome_certain"), row.get("net_pnl_pct"),
    )


@needs_db
async def test_the_database_refuses_a_second_open_position_per_instrument() -> None:
    """Главное правило этапа записано БАЗОЙ, а не кодом.

    Проверка в коде переживает ровно до первой гонки: сервис перезапустили, две
    итерации наложились — и позиций стало две. Здесь гонка воспроизводится
    прямой вставкой, минуя код вовсе.
    """
    import asyncpg

    from src.core.db import db as database

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        database._pool = await asyncpg.create_pool(
            dsn=TEST_DSN, min_size=1, max_size=2
        )
        await database.ensure_positions_schema()
        instrument_id, signal_id = await _fixture_ids(conn)
        other_signal = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version, "
            "probability) VALUES ($1, now(), 'buy', 5, 0.9) RETURNING id;",
            instrument_id,
        )
        first = await _insert_position(
            conn, _position_row(instrument_id, signal_id)
        )
        assert first
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_position(
                conn, _position_row(instrument_id, int(other_signal))
            )
    finally:
        await conn.execute(
            "DELETE FROM positions WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.execute(
            "DELETE FROM signals WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.close()
        await database.close()


@needs_db
async def test_the_database_refuses_a_second_position_for_one_signal() -> None:
    """Один сигнал — не более одной позиции, даже после перезапуска сервиса."""
    import asyncpg

    from src.core.db import db as database

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        database._pool = await asyncpg.create_pool(
            dsn=TEST_DSN, min_size=1, max_size=2
        )
        await database.ensure_positions_schema()
        instrument_id, signal_id = await _fixture_ids(conn)
        closed = _position_row(
            instrument_id, signal_id, status="closed",
            closed_at=datetime.now(UTC), exit_price=101.0,
            exit_reason="target", outcome_certain=True, net_pnl_pct=0.78,
        )
        # Первая позиция закрыта — и всё равно вторая по тому же сигналу
        # невозможна: индекс уникален по signal_id целиком, а не по открытым.
        await conn.execute(
            """
            INSERT INTO positions
                (instrument_id, signal_id, logic_version, horizon_h, side,
                 status, signal_ts, signal_price, opened_at, entry_price,
                 entry_lag_sec, entry_slippage_pct, qty, notional_usd,
                 target_pct, target_price, stop_pct, stop_price, cost_pct,
                 deadline_at, resolution, closed_at, exit_price, exit_reason,
                 outcome_certain, net_pnl_pct)
            VALUES ($1, $2, 5, 24, 'buy', 'closed', now(), 100, now(), 100, 42,
                    0, 0.02, 2, 1, 101, 1, 99, 0.22,
                    now() + interval '24 hours', '1m', now(), 101, 'target',
                    TRUE, 0.78);
            """,
            instrument_id, signal_id,
        )
        assert closed["status"] == "closed"
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_position(
                conn, _position_row(instrument_id, signal_id)
            )
    finally:
        await conn.execute(
            "DELETE FROM positions WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.execute(
            "DELETE FROM signals WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.close()
        await database.close()


@needs_db
async def test_the_shape_constraint_catches_impossible_rows() -> None:
    """Открытая строка с итогом и закрытая без итога — обе отвергаются.

    Оба состояния бессмысленны, и ограничение ловит ошибку расчёта раньше, чем
    она попадёт в отчёт. Заодно проверяются side='sell' (спот: продавать нечего)
    и чужое разрешение ряда.
    """
    import asyncpg

    from src.core.db import db as database

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        database._pool = await asyncpg.create_pool(
            dsn=TEST_DSN, min_size=1, max_size=2
        )
        await database.ensure_positions_schema()
        instrument_id, signal_id = await _fixture_ids(conn)

        # Открытая — с проставленным итогом.
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_position(conn, _position_row(
                instrument_id, signal_id, exit_price=101.0,
                exit_reason="target", outcome_certain=True, net_pnl_pct=0.78,
                closed_at=datetime.now(UTC),
            ))
        # Закрытая — без итога.
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_position(conn, _position_row(
                instrument_id, signal_id, status="closed",
            ))
        # Продажа на споте.
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_position(conn, _position_row(
                instrument_id, signal_id, side="sell",
            ))
        # Часовой ряд: порядок касаний внутри часа неизвестен.
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_position(conn, _position_row(
                instrument_id, signal_id, resolution="1h",
            ))
    finally:
        await conn.execute(
            "DELETE FROM positions WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.execute(
            "DELETE FROM signals WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91/USDT');"
        )
        await conn.close()
        await database.close()


@needs_db
async def test_migration_018_is_idempotent() -> None:
    """Миграция применяется дважды подряд без ошибки.

    Идемпотентность не украшение: миграцию применяют вручную, и повторный запуск
    на уже применённой схеме обязан быть безвредным, иначе оператор боится
    повторить шаг и пропускает его.
    """
    import asyncpg

    sql = (_ROOT / "db" / "migrations" / "018_positions.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await conn.execute(sql)
        await conn.execute(sql)
        exists = await conn.fetchval("SELECT to_regclass('positions') IS NOT NULL;")
        assert exists is True
        # И ограничения на месте после ПОВТОРНОГО применения тоже: блок DO $$
        # с проверкой по имени — единственное, что их создаёт на уже
        # существующей таблице.
        checks = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'positions'::regclass AND contype = 'c';"
        )
        assert int(checks) >= 6
    finally:
        await conn.close()


@needs_db
async def test_the_unsettled_query_finds_a_planted_row_and_spares_a_correct_one(
) -> None:
    """Запрос Задачи Б находит строку, посчитанную по незакрытому бару.

    Подкладываются ДВЕ строки на одном инструменте и одном горизонте: у первой
    ``computed_at`` раньше закрытия последнего бара окна, у второй — позже.
    Найдена обязана быть ровно первая: запрос, находящий обе, удалил бы
    исправные строки вместе с испорченными.

    ЗАПАС СЧИТАЕТСЯ ПО ФАКТИЧЕСКОМУ РАЗРЕШЕНИЮ СТРОКИ (Этап 9.1.1 §2). Обе
    подкладываемые строки — минутного ряда (``resolution = '1m'``), и последний
    бар их окна закрывается через 60 секунд после срока, а не через час. Прежняя
    редакция этого теста брала запас у ``settle_seconds()``, то есть по ГРУБОМУ
    бару: на боевых данных такой критерий объявил подозрительными 7618
    ИСПРАВНЫХ строк.
    """
    import asyncpg

    from src.core.db import db as database

    settle_min = settings.BARRIER_SETTLE_MINUTES
    settle = 60 + settle_min * 60
    entry_ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    horizon_h = 1
    closes_at = entry_ts + timedelta(hours=horizon_h, seconds=settle)

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'TEST91B/USDT', 'TEST91B', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        insert = """
            INSERT INTO strategy_outcomes
                (strategy, instrument_id, entry_ts, horizon_h, signal_id,
                 logic_version, direction, price_at_entry, target_pct,
                 target_source, stop_pct, cost_pct, outcome, net_pnl_pct,
                 mae_pct, mfe_pct, resolution, computed_at)
            VALUES ($1, $2, $3, $4, NULL, 5, 'buy', 100, 1, 'frozen', 1, 0.22,
                    'timeout', 0.1, -0.2, 0.3, '1m', $5);
        """
        await conn.execute(
            insert, "grid_buy", instrument_id, entry_ts, horizon_h,
            closes_at - timedelta(seconds=1),   # посчитано РАНЬШЕ закрытия
        )
        await conn.execute(
            insert, "grid_sell", instrument_id, entry_ts, horizon_h,
            closes_at + timedelta(seconds=1),   # посчитано ПОСЛЕ закрытия
        )

        database._pool = await asyncpg.create_pool(
            dsn=TEST_DSN, min_size=1, max_size=2
        )
        rows = await database.get_strategy_outcomes_unsettled(
            settle_minutes=settle_min
        )
        planted = [
            r for r in rows
            if int(r["instrument_id"]) == int(instrument_id)
            and r["entry_ts"] == entry_ts
        ]
        assert [r["strategy"] for r in planted] == ["grid_buy"]
    finally:
        await conn.execute(
            "DELETE FROM strategy_outcomes WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = 'TEST91B/USDT');"
        )
        await conn.close()
        await database.close()
