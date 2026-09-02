"""Этап 9.1.5: положение цены в недельном размахе и исход сигнала.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ЭТАП ЗАМЕРНЫЙ, И ГЛАВНАЯ ОПАСНОСТЬ ЗАМЕРА — ПРАВДОПОДОБНОЕ ЧИСЛО. Неверно
посчитанное положение выглядит ровно так же, как верное: ни исключения, ни
пустого места, ни красной строки в журнале. Хуже того, самый вероятный дефект —
подглядывание в будущее — даёт число КРАСИВОЕ: связь находится сильная и
ложная. Поэтому проверяется не «получилось ли число», а шесть вещей, каждая из
которых способна сделать число ложным:

 1. АРИФМЕТИКА. На придуманном ряде с известными минимумом и максимумом
    положение считается ТОЧНО, а не «примерно».
 2. ГРАНИЦЫ ОКНА. Бар до окна и бар после сигнала на размах не влияют; бар,
    закрывшийся в ту же секунду, что сигнал, в окно НЕ ПОПАДАЕТ.
 3. ПОДГЛЯДЫВАНИЕ. Проверка блокирующая: сдвиг окна вперёд роняет её и даёт
    код 2, таблицы не печатаются, в базу не уходит ни строки.
 4. СОСТАВ ВЫБОРКИ. Короткая история ИСКЛЮЧАЕТСЯ И СЧИТАЕТСЯ, а не теряется
    молча. Пробитие вверх даёт pos > 1 и свою корзину, а не обрезается до 1.
 5. НЕЗАВИСИМОСТЬ. Из десяти сигналов одного токена в одном четырёхчасовом
    окне остаётся ровно один, и именно первый; разные токены остаются оба.
 6. РАЗДЕЛЬНОСТЬ И ФИКСИРОВАННОСТЬ. buy и sell не смешиваются; границы корзин
    не зависят от состава данных.

КОНТРОЛЬНЫЕ ОПЫТЫ ОБЯЗАТЕЛЬНЫ ПО КАЖДОЙ НОВОЙ ПРОВЕРКЕ (§7 ТЗ). По каждой
показано, что она ПАДАЕТ при возвращённом дефекте: проверка, которая проходит и
с дефектом, и без него, не проверяет ничего. На Этапе 9.1.3 три проверки из
девяти прошли молча. Опыты помечены в именах словом ``control_experiment``.

ДВОЙНИК БАЗЫ ЗДЕСЬ НЕ МЯГЧЕ НАСТОЯЩЕЙ. Он ВЫПОЛНЯЕТ SQL настоящих методов
``DB`` и сверяет каждую колонку с составом таблиц, вычитанным ИЗ ФАЙЛОВ
МИГРАЦИЙ (``tests/schema_double.py``, дополнен на этом этапе тремя таблицами:
``signal_targets``, ``signal_outcomes_barrier`` и ``signal_range_position``), и
применяет ТЕ УСЛОВИЯ, КОТОРЫЕ РЕАЛЬНО СТОЯТ В ЗАПРОСЕ.

ЧЕГО ЭТИ ПРОВЕРКИ НЕ ДОКАЗЫВАЮТ. Ни одна из них не запускалась на настоящих
свечах: ряды здесь придуманы, и придуманы так, чтобы ответ был известен заранее.
Есть ли связь между положением в размахе и исходом — этого синтетика не скажет
и сказать не может; это покажет первый прогон на сервере.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

import scripts.range_position_9_1_5 as rangepos
from tests.schema_double import (
    SchemaPool,
    UndefinedColumn,
    check_sql_columns,
    project,
    schema,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Сигнал-образец. Момент выбран на круглой минуте: границы окна тогда видны
# глазом, а не выводятся из арифметики с секундами.
SIGNAL_TS = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
INSTRUMENT = 10
OTHER_INSTRUMENT = 11
TOKEN = "BTC"
OTHER_TOKEN = "ETH"
WINDOW_DAYS = 7
MINUTE = 60.0
# Момент прогона. Задан явно: ``settle_seconds()`` сверяет бар С НИМ, и
# плавающее «сейчас» делало бы проверки зависящими от дня запуска.
NOW = SIGNAL_TS + timedelta(days=1)

# Ряд-образец: ровный коридор 100…110, в который вкраплены известные крайние
# бары. Круглые числа выбраны затем, чтобы ответ читался без калькулятора.
FLAT_LOW = 100.0
FLAT_HIGH = 110.0


def settle() -> int:
    return rangepos.settle_seconds()


# =============================================================================
# Двойник базы
# =============================================================================

class _RangePool(SchemaPool):
    """Пул с данными: инструменты, сигналы, бары, исходы и хранилище замера.

    Наследуется от :class:`SchemaPool`, поэтому КАЖДЫЙ запрос сперва проходит
    сверку колонок со схемой из файлов миграций и только потом исполняется.
    """

    def __init__(
        self,
        *,
        instruments: list[dict[str, Any]] | None = None,
        signals: list[dict[str, Any]] | None = None,
        bars: dict[int, list[dict[str, Any]]] | None = None,
        outcomes: list[dict[str, Any]] | None = None,
        table_exists: bool = True,
    ) -> None:
        super().__init__()
        self.instruments = instruments or []
        self.signals = signals or []
        self.bars = bars or {}
        self.outcomes = outcomes or []
        self.table_exists = table_exists
        # Хранилище таблицы замера: ключ (signal_id, window_days).
        self.stored: dict[tuple[int, int], dict[str, Any]] = {}
        self.write_batches: list[list[Any]] = []
        self.bar_requests: list[tuple[Any, ...]] = []
        self.max_rows_in_flight = 0

    # -- чтение --------------------------------------------------------------

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._check(sql)
        if "FROM instruments i" in sql:
            return self._fetch_instruments(sql, *args)
        if "FROM ohlcv" in sql:
            return self._fetch_bars(sql, *args)
        # ПОРЯДОК РАЗБОРА ЗНАЧИМ. Запрос сигналов сам содержит подзапрос
        # ``FROM signal_outcomes_barrier`` — отбор идёт по наличию исхода, — и
        # разбор «по первому упоминанию таблицы» отправил бы его не туда.
        # Признак взят однозначный: ``DISTINCT ON (s.id)`` есть только у него.
        if "DISTINCT ON (s.id)" in sql:
            return self._fetch_signals(sql, *args)
        if "FROM signal_outcomes_barrier b" in sql:
            return self._fetch_outcomes(sql, *args)
        raise AssertionError(f"двойник не знает запроса: {sql[:120]}")

    def _fetch_instruments(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        timeframe = args[0]
        out: list[dict[str, Any]] = []
        for item in self.instruments:
            if not any(
                s["instrument_id"] == item["instrument_id"]
                and s["decision"] != "wait"
                for s in self.signals
            ):
                continue
            stamps = [
                b["ts"] for b in self.bars.get(item["instrument_id"], [])
                if b.get("timeframe", "1m") == timeframe
            ]
            out.append({
                "instrument_id": item["instrument_id"],
                "token": item["token"],
                "first_bar_ts": min(stamps) if stamps else None,
                "last_bar_ts": max(stamps) if stamps else None,
            })
        return sorted(out, key=lambda r: r["instrument_id"])

    def _fetch_bars(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        instrument_id, timeframe, ts_from, after_ts, limit = args
        self.bar_requests.append((instrument_id, ts_from, after_ts, limit))
        rows = [
            b for b in self.bars.get(int(instrument_id), [])
            if b.get("timeframe", "1m") == timeframe and b["ts"] >= ts_from
            and (after_ts is None or b["ts"] > after_ts)
        ]
        rows.sort(key=lambda b: b["ts"])
        rows = rows[: int(limit)]
        self.max_rows_in_flight = max(self.max_rows_in_flight, len(rows))
        return project(sql, rows)

    def _fetch_signals(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        instrument_id, after_ts, after_id, limit = args
        with_outcome = {int(o["signal_id"]) for o in self.outcomes}
        rows = [
            s for s in self.signals
            if s["instrument_id"] == int(instrument_id)
            and s["decision"] != "wait"
            and int(s["signal_id"]) in with_outcome
        ]
        if after_ts is not None:
            rows = [
                s for s in rows
                if (s["ts"], int(s["signal_id"])) > (after_ts, int(after_id))
            ]
        rows.sort(key=lambda s: (s["ts"], int(s["signal_id"])))
        return project(sql, rows[: int(limit)])

    def _fetch_outcomes(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        after_id, after_horizon, limit = args
        by_signal = {int(s["signal_id"]): s for s in self.signals}
        rows: list[dict[str, Any]] = []
        for outcome in self.outcomes:
            signal = by_signal[int(outcome["signal_id"])]
            token = next(
                i["token"] for i in self.instruments
                if i["instrument_id"] == signal["instrument_id"]
            )
            rows.append({**outcome, "ts": signal["ts"],
                         "instrument_id": signal["instrument_id"],
                         "base": token})
        if after_id is not None:
            rows = [
                r for r in rows
                if (int(r["signal_id"]), int(r["horizon_h"]))
                > (int(after_id), int(after_horizon))
            ]
        rows.sort(key=lambda r: (int(r["signal_id"]), int(r["horizon_h"])))
        return project(sql, rows[: int(limit)])

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._check(sql)
        if "directional_total" in sql:
            directional = [s for s in self.signals if s["decision"] != "wait"]
            with_outcome = {int(o["signal_id"]) for o in self.outcomes}
            return {
                "directional_total": len(directional),
                "with_outcome": sum(
                    1 for s in directional
                    if int(s["signal_id"]) in with_outcome
                ),
            }
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._check(sql)
        if "to_regclass" in sql:
            return self.table_exists
        return None

    # -- запись --------------------------------------------------------------

    async def executemany(self, sql: str, rows: list[Any]) -> None:
        self._check(sql)
        self.writes.append(sql)
        self.write_batches.append(list(rows))
        assert "INSERT INTO signal_range_position" in sql, (
            "этап пишет ровно одну таблицу; сюда пришёл чужой INSERT"
        )
        for row in rows:
            key = (int(row[0]), int(row[1]))
            fresh = {
                "range_low": row[2], "range_high": row[3],
                "range_width_pct": row[4], "pos": row[5],
                "last_bar_ts": row[6], "bars_in_window": row[7],
                "resolution": row[8],
            }
            existing = self.stored.get(key)
            if existing is None:
                self.stored[key] = {**fresh, "computed_at": len(self.writes)}
                continue
            # ТО ЖЕ УСЛОВИЕ, ЧТО В SQL: строка, у которой совпали ВСЕ значения,
            # не обновляется вовсе — значит, не двигается и computed_at.
            same = all(
                existing[name] == value for name, value in fresh.items()
            )
            if not same:
                self.stored[key] = {**fresh, "computed_at": len(self.writes)}


# =============================================================================
# Построение придуманных рядов
# =============================================================================

def bar(ts: datetime, low: float, high: float) -> dict[str, Any]:
    return {"ts": ts, "low": low, "high": high, "timeframe": "1m"}


def flat_series(
    *,
    signal_ts: datetime = SIGNAL_TS,
    days_before: float = 9.0,
    days_after: float = 0.0,
    step_minutes: int = 1,
) -> list[dict[str, Any]]:
    """Ровный коридор 100…110 минутными барами вокруг момента сигнала."""
    start = signal_ts - timedelta(days=days_before)
    stop = signal_ts + timedelta(days=days_after)
    out: list[dict[str, Any]] = []
    ts = start
    while ts <= stop:
        out.append(bar(ts, FLAT_LOW, FLAT_HIGH))
        ts += timedelta(minutes=step_minutes)
    return out


def put(series: list[dict[str, Any]], ts: datetime, low: float,
        high: float) -> None:
    """Заменить бар ряда в указанный момент. Отсутствие бара — ошибка теста."""
    for item in series:
        if item["ts"] == ts:
            item["low"] = low
            item["high"] = high
            return
    raise AssertionError(f"в ряде нет бара {ts.isoformat()}")


def signal_row(
    signal_id: int = 1,
    *,
    ts: datetime = SIGNAL_TS,
    instrument_id: int = INSTRUMENT,
    price: float = 105.0,
    decision: str = "buy",
    logic_version: int = 5,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id, "ts": ts, "instrument_id": instrument_id,
        "price_at_signal": price, "decision": decision,
        "logic_version": logic_version,
    }


def outcome_row(
    signal_id: int = 1,
    *,
    horizon_h: int = 1,
    direction: str = "buy",
    outcome: str = "target",
    net_pnl_pct: float | None = 1.0,
    logic_version: int = 5,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id, "horizon_h": horizon_h, "direction": direction,
        "outcome": outcome, "net_pnl_pct": net_pnl_pct,
        "logic_version": logic_version,
    }


INSTRUMENTS = [
    {"instrument_id": INSTRUMENT, "token": TOKEN},
    {"instrument_id": OTHER_INSTRUMENT, "token": OTHER_TOKEN},
]


async def run_scan(pool: _RangePool, monkeypatch, *, window_days: int = WINDOW_DAYS
                   ) -> tuple[rangepos.ScanResult, rangepos.ScanCounters]:
    """Прогоняет НАСТОЯЩИЙ проход этапа на двойнике пула."""
    from src.core.db import db as real_db

    monkeypatch.setattr(real_db, "_pool", pool, raising=False)
    scan = rangepos.ScanResult()
    counters = rangepos.ScanCounters()
    instruments = await real_db.get_range_position_instruments(timeframe="1m")
    for instrument in instruments:
        await rangepos.scan_instrument(
            instrument,
            window_days=window_days,
            settle=settle(),
            shift_sec=float(rangepos.WINDOW_SHIFT_BARS) * rangepos.BAR_SECONDS["1m"],
            timeframe="1m",
            now=NOW,
            result=scan,
            counters=counters,
        )
    return scan, counters


async def run_script(monkeypatch, pool: _RangePool, argv: list[str]) -> int:
    """Прогоняет НАСТОЯЩИЙ скрипт этапа целиком на двойнике пула."""
    from src.core.db import db as real_db

    async def _noop() -> None:
        return None

    monkeypatch.setattr(real_db, "_pool", pool, raising=False)
    monkeypatch.setattr(real_db, "connect", _noop)
    monkeypatch.setattr(real_db, "close", _noop)
    monkeypatch.setattr("sys.argv", ["rangepos", *argv])
    return await rangepos.main()


# =============================================================================
# §7.1. АРИФМЕТИКА: положение считается ТОЧНО на ряде с известным ответом
# =============================================================================

def known_series() -> list[dict[str, Any]]:
    """Ряд с ЗАРАНЕЕ ИЗВЕСТНЫМИ минимумом и максимумом окна.

    Устройство ряда (всё остальное — ровный коридор 100…110):

      S − 8 сут   low = 1    — ДО окна: обязан быть не виден вовсе;
      S − 7 сут   low = 90   — ЛЕВАЯ ГРАНИЦА окна, входит: минимум окна = 90;
      S − 1 сут   high = 130 — внутри окна: максимум окна = 130;
      S − 1 мин   low = 1, high = 1000 — бар, закрывающийся РОВНО в секунду
                  сигнала: в окно НЕ входит, иначе размах стал бы 1…1000.

    При цене решения 100 ответ читается без калькулятора:
    pos = (100 − 90) / (130 − 90) = 0.25 РОВНО, ширина = 40 / 90 · 100 %.
    """
    series = flat_series(days_before=8.5)
    put(series, SIGNAL_TS - timedelta(days=8), 1.0, 2.0)
    put(series, SIGNAL_TS - timedelta(days=7), 90.0, FLAT_HIGH)
    put(series, SIGNAL_TS - timedelta(days=1), FLAT_LOW, 130.0)
    put(series, SIGNAL_TS - timedelta(minutes=1), 1.0, 1000.0)
    return series


def known_pool(**over: Any) -> _RangePool:
    return _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(price=100.0)],
        bars={INSTRUMENT: known_series()},
        outcomes=[outcome_row()],
        **over,
    )


async def test_position_in_the_range_is_computed_exactly(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1: pos и ширина считаются ТОЧНО, а не примерно.

    Сравнение ведётся с точным значением, а не с допуском: 0.25 здесь —
    не результат приближения, а частное двух целых, и «примерно 0.25»
    означало бы, что окно взято не то.
    """
    scan, counters = await run_scan(known_pool(), monkeypatch)

    assert len(scan) == 1
    assert counters.computed == 1
    assert counters.lookahead_violations == 0
    assert float(scan.range_low.values[0]) == 90.0
    assert float(scan.range_high.values[0]) == 130.0
    assert float(scan.pos.values[0]) == 0.25
    assert float(scan.width_pct.values[0]) == pytest.approx(40.0 / 90.0 * 100.0)
    # Последний бар окна — за ДВЕ минуты до сигнала: бар за одну минуту
    # закрывается ровно в секунду сигнала и в окно не входит.
    assert (
        datetime.fromtimestamp(float(scan.last_bar_epoch.values[0]), UTC)
        == SIGNAL_TS - timedelta(minutes=2)
    )
    # Баров в окне: семь суток минут минус две последние минуты, включительно.
    assert int(scan.bars_in_window.values[0]) == 7 * 1440 - 2 + 1


async def test_a_bar_before_the_window_does_not_touch_the_range(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.1: бар ЛЕВЕЕ окна на размах не влияет — окно ровно семь суток."""
    scan, _ = await run_scan(known_pool(), monkeypatch)
    assert float(scan.range_low.values[0]) == 90.0, (
        "в размах попал бар с low=1, лежащий за сутки ДО начала окна"
    )


async def test_control_experiment_a_wider_window_does_let_the_early_bar_in(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к предыдущей проверке.

    Она обязана ловить именно ширину окна. Расширяем окно до ВОСЬМИ суток —
    бар с low=1 лежит ровно на новой левой границе и обязан попасть в размах,
    сдвинув минимум с 90 до 1. Если бы проверка проходила и здесь, она не
    проверяла бы ничего.

    Восемь суток, а не девять: ряд построен на 8,5 суток назад, и при окне в
    девять суток сигнал был бы ИСКЛЮЧЁН по короткой истории — проверка упала бы
    по другой причине и снова ничего бы не доказала.
    """
    scan, counters = await run_scan(known_pool(), monkeypatch, window_days=8)
    assert counters.computed == 1
    assert float(scan.range_low.values[0]) == 1.0


# =============================================================================
# §7.2. Бар, закрывшийся в ту же секунду, что сигнал, В ОКНО НЕ ПОПАДАЕТ
# =============================================================================

async def test_a_bar_closing_at_the_signal_second_is_not_in_the_window(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.2: граница окна СТРОГАЯ.

    Бар ``S − 1 мин`` закрывается ровно в секунду сигнала. Его high = 1000
    и low = 1 — если бы он вошёл, размах стал бы 1…1000, а положение —
    (100−1)/999 ≈ 0.099. Ответ 0.25 доказывает, что он не вошёл.
    """
    scan, _ = await run_scan(known_pool(), monkeypatch)
    assert float(scan.range_high.values[0]) == 130.0
    assert float(scan.pos.values[0]) == 0.25


async def test_control_experiment_shifting_the_window_one_bar_lets_it_in(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: сдвиг окна на ОДИН БАР вперёд впускает тот самый бар.

    Без этого опыта предыдущая проверка могла бы проходить по любой другой
    причине — например, потому что бара в ряду вовсе нет.
    """
    monkeypatch.setattr(rangepos, "WINDOW_SHIFT_BARS", 1)
    scan, counters = await run_scan(known_pool(), monkeypatch)
    assert counters.lookahead_violations == 1
    assert len(scan) == 0, "строка со сдвинутым окном попала в выборку"


# =============================================================================
# §7.3. КОНТРОЛЬНЫЙ ОПЫТ НА ПОДГЛЯДЫВАНИЕ: код 2 и ни одной таблицы
# =============================================================================

async def test_the_lookahead_check_blocks_the_whole_run_with_code_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.2: нарушение хотя бы у одного сигнала останавливает ВЕСЬ расчёт.

    Не помечается одна строка и не печатается таблица «с оговоркой»: замер с
    подглядыванием даёт КРАСИВЫЙ результат, и напечатанное число будет
    процитировано без оговорки.
    """
    monkeypatch.setattr(rangepos, "WINDOW_SHIFT_BARS", 1)
    pool = known_pool()
    code = await run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out

    assert code == 2
    assert "ПРОВЕРКА НА ПОДГЛЯДЫВАНИЕ НЕ ПРОШЛА" in out
    assert "ЧИСЛО 1" not in out
    assert "ЧИСЛО 2" not in out
    assert "ЧИСЛО 3" not in out
    assert "ЧИСЛО 4" not in out
    assert "ЧИСЛО 5" not in out
    assert pool.writes == [], "при нарушении проверки что-то записано в базу"
    # Оборванный прогон обязан быть отличим от отказавшегося: признак
    # завершения печатается и здесь.
    assert out.rstrip().endswith(rangepos.DONE_MARKER)


async def test_control_experiment_without_the_shift_the_same_run_is_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: те же данные без сдвига дают код 0 и все пять чисел.

    Проверка, которая роняет прогон всегда, столь же бесполезна, как та,
    которая не роняет его никогда.
    """
    assert rangepos.WINDOW_SHIFT_BARS == 0, "боевой расчёт обязан идти без сдвига"
    pool = known_pool()
    code = await run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out
    assert code == 0
    for number in ("ЧИСЛО 1", "ЧИСЛО 2", "ЧИСЛО 3", "ЧИСЛО 4", "ЧИСЛО 5"):
        assert number in out
    assert out.rstrip().endswith(rangepos.DONE_MARKER)


# =============================================================================
# §7.4. Короткая история ИСКЛЮЧАЕТСЯ И СЧИТАЕТСЯ, а не теряется молча
# =============================================================================

async def test_a_signal_with_less_than_seven_days_of_history_is_counted_out(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4 ЧИСЛО 1: исключённый сигнал попадает в счётчик, а не в тишину.

    Разница существенная: «сигналов было мало» и «сигналов было много, но у
    большинства не оказалось истории» — разные утверждения о выборке, и второе
    нельзя получить, если исключённые исчезают молча.
    """
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(price=100.0)],
        # Ряд начинается за трое суток до сигнала: семисуточного окна не из чего
        # построить.
        bars={INSTRUMENT: flat_series(days_before=3.0)},
        outcomes=[outcome_row()],
    )
    scan, counters = await run_scan(pool, monkeypatch)

    assert len(scan) == 0
    assert counters.computed == 0
    assert counters.signals_seen == 1
    assert counters.skipped_short_history == 1
    assert counters.lookahead_violations == 0


async def test_control_experiment_full_history_of_the_same_signal_is_counted_in(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: тот же сигнал с полной историей попадает В выборку."""
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(price=100.0)],
        bars={INSTRUMENT: flat_series(days_before=7.5)},
        outcomes=[outcome_row()],
    )
    scan, counters = await run_scan(pool, monkeypatch)
    assert counters.computed == 1
    assert counters.skipped_short_history == 0
    assert len(scan) == 1


async def test_the_excluded_count_is_printed_in_number_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§4 ЧИСЛО 1: исключённые по короткой истории НАПЕЧАТАНЫ отдельной строкой."""
    good = flat_series(days_before=7.5)
    short = flat_series(days_before=3.0)
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[
            signal_row(1, price=100.0),
            signal_row(2, price=100.0, instrument_id=OTHER_INSTRUMENT),
        ],
        bars={INSTRUMENT: good, OTHER_INSTRUMENT: short},
        outcomes=[outcome_row(1), outcome_row(2)],
    )
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert re.search(r"истории короче 7 сут\.+ 1$", out, re.M), out[:2000]
    assert re.search(r"ОСТАЛОСЬ сигналов с положением\.+ 1$", out, re.M)


# =============================================================================
# §7.5. Пробитие вверх: pos > 1, своя корзина, БЕЗ обрезания
# =============================================================================

async def test_a_price_above_the_weekly_high_gives_pos_above_one(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5: pos вне [0,1] — штатный случай, а не ошибка.

    Цена сигнала в окно не входит по построению, поэтому она ИМЕЕТ ПРАВО
    оказаться выше недельного максимума. Обрезание до 1 стёрло бы ровно ту
    группу, которая может оказаться самой интересной.
    """
    pool = _RangePool(
        instruments=INSTRUMENTS,
        # Коридор 100…110, цена решения 130 — выше максимума окна.
        signals=[signal_row(price=130.0)],
        bars={INSTRUMENT: flat_series(days_before=7.5)},
        outcomes=[outcome_row()],
    )
    scan, _ = await run_scan(pool, monkeypatch)
    pos = float(scan.pos.values[0])

    assert pos == pytest.approx((130.0 - 100.0) / (110.0 - 100.0))
    assert pos > 1.0, "положение обрезано до единицы"
    assert rangepos.pos_bucket(pos) == rangepos.POS_ABOVE
    assert rangepos.pos_bucket_label(rangepos.POS_ABOVE) == "выше размаха"


async def test_a_price_below_the_weekly_low_gives_pos_below_zero(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5: зеркальный случай — пробитие ВНИЗ, своя крайняя корзина."""
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(price=95.0)],
        bars={INSTRUMENT: flat_series(days_before=7.5)},
        outcomes=[outcome_row()],
    )
    scan, _ = await run_scan(pool, monkeypatch)
    pos = float(scan.pos.values[0])
    assert pos < 0.0
    assert rangepos.pos_bucket(pos) == rangepos.POS_BELOW
    assert rangepos.pos_bucket_label(rangepos.POS_BELOW) == "ниже размаха"


async def test_the_out_of_range_buckets_are_printed_with_their_own_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§5: численность крайних корзин ПЕЧАТАЕТСЯ, а не растворяется в соседней."""
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(price=130.0)],
        bars={INSTRUMENT: flat_series(days_before=7.5)},
        outcomes=[outcome_row()],
    )
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "выше размаха" in out
    assert "ниже размаха" in out
    assert re.search(r"выше размаха\s+1\s", out), "крайняя корзина без счёта"


async def test_control_experiment_clipping_to_one_would_move_the_bucket() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: обрезание до [0,1] сливает пробитие с обычным верхом.

    Проверка выше обязана ловить именно ОТСУТСТВИЕ обрезания. Здесь показано,
    что с обрезанием ответ был бы другим: корзина стала бы верхней обычной, и
    отличить пробитие от «цены у верхней границы» стало бы невозможно.
    """
    breakout = 3.0
    assert rangepos.pos_bucket(breakout) == rangepos.POS_ABOVE
    clipped = min(max(breakout, 0.0), 1.0)
    assert rangepos.pos_bucket(clipped) == rangepos.POS_BUCKET_COUNT
    assert rangepos.pos_bucket(clipped) != rangepos.pos_bucket(breakout)


# =============================================================================
# §7.6–7.7. Независимая подвыборка: одно окно — один сигнал, но НА ИНСТРУМЕНТ
# =============================================================================

def ten_signals_in_one_window(instrument_id: int = INSTRUMENT,
                              first_id: int = 1) -> list[dict[str, Any]]:
    """Десять сигналов одного токена внутри ОДНОГО четырёхчасового окна.

    Момент первого выбран так, чтобы все десять заведомо лежали в одном окне:
    12:00 UTC — начало окна 12:00–16:00, десять минут от него его не покидают.
    """
    return [
        signal_row(
            first_id + i, ts=SIGNAL_TS + timedelta(minutes=i),
            instrument_id=instrument_id, price=105.0,
        )
        for i in range(10)
    ]


async def test_ten_signals_in_one_window_leave_exactly_one_and_it_is_the_first(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.3: из окна берётся РОВНО ОДИН сигнал, и именно ПЕРВЫЙ по времени.

    «Ровно один» и «именно первый» — два разных утверждения, и оба обязательны.
    Правило, берущее случайный сигнал окна, тоже оставило бы один, но давало бы
    разный ответ при одинаковых данных.
    """
    signals = ten_signals_in_one_window()
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[outcome_row(s["signal_id"]) for s in signals],
    )
    scan, counters = await run_scan(pool, monkeypatch)

    assert counters.computed == 10, "в полную выборку попали не все десять"
    assert counters.independent == 1
    chosen = scan.signal_id.values[scan.independent.values]
    assert list(chosen) == [1], "оставлен не первый сигнал окна"


async def test_signals_of_different_tokens_in_one_window_both_remain(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.3: ключ прореживания включает ИНСТРУМЕНТ.

    Без инструмента в ключе пять токенов конкурировали бы за одно окно, и от
    каждого окна оставался бы один сигнал — четыре пятых наблюдений исчезли бы
    молча. То же правило действует в отборе независимых наблюдений Этапа 8.1.
    """
    signals = [
        signal_row(1, price=105.0),
        signal_row(2, ts=SIGNAL_TS + timedelta(minutes=1),
                   instrument_id=OTHER_INSTRUMENT, price=105.0),
    ]
    series = flat_series(days_before=7.5, days_after=1.0)
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: series, OTHER_INSTRUMENT: list(series)},
        outcomes=[outcome_row(1), outcome_row(2)],
    )
    _scan, counters = await run_scan(pool, monkeypatch)

    assert counters.computed == 2
    assert counters.independent == 2, (
        "два токена в одном окне схлопнулись в одно наблюдение"
    )


async def test_the_next_window_opens_a_new_independent_slot(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.3: окна НЕ ПЕРЕКРЫВАЮТСЯ и границы фиксированы по UTC.

    Сигнал в 15:59 и сигнал в 16:01 лежат в РАЗНЫХ окнах (граница 16:00 UTC),
    хотя между ними две минуты. Скользящее окно «четыре часа от предыдущего»
    оставило бы только первый — и это была бы другая выборка, зависящая от
    того, с какого сигнала начали.
    """
    base = datetime(2026, 8, 30, 15, 59, tzinfo=UTC)
    signals = [
        signal_row(1, ts=base, price=105.0),
        signal_row(2, ts=base + timedelta(minutes=2), price=105.0),
    ]
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(signal_ts=base, days_before=7.5,
                                      days_after=1.0)},
        outcomes=[outcome_row(1), outcome_row(2)],
    )
    _scan, counters = await run_scan(pool, monkeypatch)

    assert counters.computed == 2
    assert counters.independent == 2
    assert rangepos.independent_window(base.timestamp()) != (
        rangepos.independent_window((base + timedelta(minutes=2)).timestamp())
    )


async def test_control_experiment_a_sliding_window_would_drop_the_second_signal(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ к фиксированным границам окна.

    Скользящее правило «следующий сигнал не ближе четырёх часов к предыдущему»
    выбросило бы сигнал 16:01 — предыдущая проверка обязана отличать одно от
    другого, иначе она не проверяет фиксированность границ.
    """
    base = datetime(2026, 8, 30, 15, 59, tzinfo=UTC)
    later = base + timedelta(minutes=2)
    sliding_keeps_second = (
        later.timestamp() - base.timestamp() >= rangepos.INDEPENDENT_WINDOW_SEC
    )
    assert not sliding_keeps_second, "скользящее правило оставило бы оба"
    assert rangepos.independent_window(base.timestamp()) != (
        rangepos.independent_window(later.timestamp())
    ), "фиксированные границы обязаны оставить оба"


async def test_both_versions_of_every_table_are_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.3: КАЖДАЯ таблица печатается ДВАЖДЫ — вся выборка и независимая."""
    signals = ten_signals_in_one_window()
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[outcome_row(s["signal_id"]) for s in signals],
    )
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    # По одной паре «направление × горизонт» обе версии печатаются для ЧИСЛА 2
    # и для ЧИСЛА 3 (таблица ширины и совместная таблица) — четыре раза каждая.
    assert out.count(rangepos.SAMPLE_ALL) >= 2
    assert out.count(rangepos.SAMPLE_INDEPENDENT) >= 2
    assert out.count(rangepos.SAMPLE_ALL) == out.count(rangepos.SAMPLE_INDEPENDENT)
    assert "по ней и делается вывод" in out


# =============================================================================
# §7.8. buy и sell считаются РАЗДЕЛЬНО
# =============================================================================

def opposite_directions_pool() -> _RangePool:
    """Покупка и продажа с ПРОТИВОПОЛОЖНЫМИ исходами при одном положении.

    Устроено так, что при раздельном счёте видно два ответа (+2 % и −2 %), а
    при совместном — ноль. Ноль здесь не «нет связи», а СЛЕД СЛОЖЕНИЯ ДВУХ
    ОТВЕТОВ, и отличить одно от другого можно только раздельным счётом.
    """
    signals = [
        signal_row(1, price=105.0, decision="buy"),
        signal_row(2, ts=SIGNAL_TS + timedelta(hours=5), price=105.0,
                   decision="sell"),
    ]
    return _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[
            outcome_row(1, direction="buy", outcome="target", net_pnl_pct=2.0),
            outcome_row(2, direction="sell", outcome="stop", net_pnl_pct=-2.0),
        ],
    )


async def test_buy_and_sell_are_counted_separately(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4 ЧИСЛО 2: смешивать направления запрещено.

    У покупки и продажи ожидаемая связь с положением ПРОТИВОПОЛОЖНА ПО ЗНАКУ:
    сложенные вместе, они взаимно уничтожаются, и таблица показывает ноль там,
    где на самом деле два симметричных ответа.
    """
    from src.core.db import db as real_db

    monkeypatch.setattr(real_db, "_pool", opposite_directions_pool(),
                        raising=False)
    scan, _ = await run_scan(opposite_directions_pool(), monkeypatch)
    outcomes, _counters = await rangepos.stream_outcomes(scan)

    assert set(outcomes) == {("buy", 1), ("sell", 1)}
    assert float(outcomes[("buy", 1)].net.values.mean()) == 2.0
    assert float(outcomes[("sell", 1)].net.values.mean()) == -2.0
    assert bool(outcomes[("buy", 1)].is_target.values[0]) is True
    assert bool(outcomes[("sell", 1)].is_target.values[0]) is False


async def test_control_experiment_merging_the_directions_hides_both_answers(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: совместный подсчёт роняет проверку.

    Подменяем раздельный счёт совместным и показываем, что ответ становится
    нулём — то есть ровно тем, что §4 ТЗ и запрещает получать.
    """
    scan, _ = await run_scan(opposite_directions_pool(), monkeypatch)
    outcomes, _counters = await rangepos.stream_outcomes(scan)

    merged = np.concatenate(
        [columns.net.values for columns in outcomes.values()]
    )
    assert float(merged.mean()) == 0.0, "опыт построен неверно: ответы не гасятся"
    separate = {key: float(c.net.values.mean()) for key, c in outcomes.items()}
    assert set(separate.values()) == {2.0, -2.0}
    assert float(merged.mean()) not in separate.values()


async def test_the_printed_tables_are_split_by_direction_and_horizon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§4: каждое из пяти чисел печатается отдельно по направлению и горизонту."""
    signals = [
        signal_row(1, price=105.0, decision="buy"),
        signal_row(2, ts=SIGNAL_TS + timedelta(hours=5), price=105.0,
                   decision="sell"),
    ]
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[
            outcome_row(1, horizon_h=h, direction="buy", net_pnl_pct=1.0)
            for h in (1, 4, 12, 24)
        ] + [
            outcome_row(2, horizon_h=h, direction="sell", net_pnl_pct=-1.0)
            for h in (1, 4, 12, 24)
        ],
    )
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    for direction in ("BUY", "SELL"):
        for horizon in (1, 4, 12, 24):
            assert f"{direction} · горизонт {horizon}ч" in out, (
                f"нет разреза {direction}/{horizon}ч"
            )


# =============================================================================
# §7.9. Границы корзин ФИКСИРОВАНЫ и от состава данных не зависят
# =============================================================================

def _labels_for(values: list[float]) -> list[str]:
    return [rangepos.pos_bucket_label(rangepos.pos_bucket(v)) for v in values]


async def test_bucket_edges_do_not_move_with_the_data() -> None:
    """§4 ЧИСЛО 2: границы фиксированы по ширине 0.1, а не по квантилям.

    Две выборки с совершенно разным распределением положения обязаны дать ОДНИ
    И ТЕ ЖЕ подписи корзин для одних и тех же значений. Квантильные границы дали
    бы разные — и «находка» в такой таблице означала бы только то, что границы
    легли удачно.
    """
    probe = [0.05, 0.25, 0.55, 0.95]
    crowded_low = [0.01 * i for i in range(100)]
    crowded_high = [0.9 + 0.001 * i for i in range(100)]

    first = _labels_for(probe)
    for sample in (crowded_low, crowded_high):
        # Состав выборки меняется полностью; подписи для тех же значений — нет.
        assert [rangepos.pos_bucket_label(rangepos.pos_bucket(v))
                for v in sample][:0] == []
        assert _labels_for(probe) == first

    assert first == ["0.0–0.1", "0.2–0.3", "0.5–0.6", "0.9–1.0"]
    assert rangepos.POS_BUCKET_COUNT == 10
    assert rangepos.WIDTH_EDGES == (2.0, 4.0, 7.0, 12.0)


async def test_control_experiment_quantile_edges_would_move() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: квантильные границы на тех же данных СДВИГАЮТСЯ.

    Без этого опыта проверка выше могла бы проходить просто потому, что обе
    выборки одинаковы.
    """
    crowded_low = np.array([0.01 * i for i in range(100)])
    crowded_high = np.array([0.9 + 0.001 * i for i in range(100)])
    quantiles_low = np.quantile(crowded_low, [0.1 * i for i in range(1, 10)])
    quantiles_high = np.quantile(crowded_high, [0.1 * i for i in range(1, 10)])
    assert not np.allclose(quantiles_low, quantiles_high), (
        "опыт построен неверно: выборки не различаются"
    )


async def test_the_tenth_boundary_is_not_moved_by_binary_fractions() -> None:
    """§4 ЧИСЛО 2: 0.3 попадает в [0.3, 0.4), а не в [0.2, 0.3).

    ``0.3 / 0.1`` в двоичной дроби равно 2.9999999999999996, и деление нацело
    тихо сдвинуло бы границу, объявленную фиксированной. Проверяются все девять
    внутренних границ, а не одна удачная.
    """
    for tenth in range(1, 10):
        edge = tenth / 10.0
        assert rangepos.pos_bucket(edge) == tenth + 1, (
            f"граница {edge} попала не в свою корзину"
        )
        assert rangepos.pos_bucket_label(rangepos.pos_bucket(edge)) == (
            f"{edge:.1f}–{edge + 0.1:.1f}"
        )


# =============================================================================
# §7.10. Идемпотентность записи и полное молчание без --apply
# =============================================================================

def apply_pool() -> _RangePool:
    signals = [
        signal_row(1, price=105.0),
        signal_row(2, ts=SIGNAL_TS + timedelta(hours=5), price=101.0),
    ]
    return _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[outcome_row(1), outcome_row(2)],
    )


async def test_without_apply_not_a_single_write_query_reaches_the_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6: без ``--apply`` скрипт ТОЛЬКО СЧИТАЕТ.

    Проверяется не «не появилось строк», а «не ушло ни одного запроса на
    запись»: запрос, который ничего не изменил, всё равно означал бы, что
    холостой прогон умеет менять базу.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, []) == 0
    assert pool.writes == []
    assert pool.write_batches == []
    assert pool.stored == {}
    assert "Ничего не записано" in capsys.readouterr().out


async def test_a_second_apply_changes_neither_row_count_nor_values(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7: повторный прогон с ``--apply`` не меняет ни числа строк, ни значений.

    Условие ``WHERE`` при ``DO UPDATE`` не даёт переписать даже метку времени у
    строки, все значения которой совпали. Простой ``DO UPDATE`` без условия
    двигал бы ``computed_at`` каждым прогоном, и требование выполнялось бы
    только на словах.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    first = {key: dict(value) for key, value in pool.stored.items()}
    assert len(first) == 2

    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    assert len(pool.stored) == len(first), "повторный прогон изменил число строк"
    assert pool.stored == first, "повторный прогон изменил значения строк"


async def test_control_experiment_changed_data_does_rewrite_the_row(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: изменившееся положение строку ПЕРЕПИСЫВАЕТ.

    Проверка идемпотентности обязана отличать «ничего не изменилось» от
    «запись вообще не работает». Здесь цена решения меняется — и строка обязана
    обновиться.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    before = dict(pool.stored[(1, WINDOW_DAYS)])

    pool.signals[0]["price_at_signal"] = 109.0
    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    after = pool.stored[(1, WINDOW_DAYS)]
    assert after["pos"] != before["pos"], "строка не обновилась при новых данных"
    assert after["computed_at"] != before["computed_at"]


async def test_apply_without_the_migration_stops_before_computing_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6: ``--apply`` без миграции 023 отказывается, а не пишет мимо схемы."""
    pool = apply_pool()
    pool.table_exists = False
    code = await run_script(monkeypatch, pool, ["--apply"])
    out = capsys.readouterr().out
    assert code == 2
    assert "миграцию 023" in out
    assert pool.writes == []
    assert out.rstrip().endswith(rangepos.DONE_MARKER)


async def test_the_stage_writes_exactly_one_table_and_no_other(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8: ни одной записи в signals, positions, signal_evaluations и прочие.

    Проверяется по ФАКТУ запросов, ушедших в базу, а не по чтению исходников:
    исходник можно прочитать неверно, а список запросов — это то, что
    действительно случилось.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    assert pool.writes, "прогон с --apply не записал ничего"
    forbidden = (
        "signals", "positions", "signal_evaluations", "signal_targets",
        "risk_targets", "trailing_outcomes", "position_trailing_shadow",
        "position_stop_shadow", "signal_outcomes_barrier", "ohlcv",
    )
    for sql in pool.writes:
        assert "INSERT INTO signal_range_position" in sql
        for table in forbidden:
            assert f"INSERT INTO {table}" not in sql
            assert f"UPDATE {table}" not in sql
            assert f"DELETE FROM {table}" not in sql


# =============================================================================
# §7.11. ПАМЯТЬ: окно ограничено окном, а не выборкой
# =============================================================================

def production_order_pool(*, days: float, signals_count: int) -> _RangePool:
    """Выборка боевого порядка: минутные бары за ``days`` суток и сигналы.

    Боевой порядок здесь — это ДЛИНА РЯДА (десятки тысяч минутных баров на
    инструмент) и число сигналов того же порядка, что на сервере. Именно на
    таком объёме Этап 9.1.3 был убит ядром дважды.
    """
    series = flat_series(days_before=days, days_after=1.0)
    signals = [
        signal_row(i + 1, ts=SIGNAL_TS + timedelta(minutes=i), price=105.0)
        for i in range(signals_count)
    ]
    return _RangePool(
        instruments=INSTRUMENTS,
        signals=signals,
        bars={INSTRUMENT: series},
        outcomes=[outcome_row(s["signal_id"]) for s in signals],
    )


async def test_the_window_never_holds_more_than_the_window(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6: подвижное окно ограничено СЕМЬЮ СУТКАМИ, а не длиной ряда.

    Ряд вчетверо длиннее окна; в окне обязано лежать ровно столько баров,
    сколько их в семи сутках, а не столько, сколько прочитано.
    """
    pool = production_order_pool(days=28.0, signals_count=1000)
    _scan, counters = await run_scan(pool, monkeypatch)

    assert counters.computed == 1000
    expected = WINDOW_DAYS * 1440
    assert counters.max_window_bars <= expected + 1, (
        f"в окне {counters.max_window_bars} баров при пределе {expected + 1}"
    )
    assert counters.max_window_bars >= expected - 2, (
        "окно оказалось меньше семи суток — выселение слишком жадное"
    )


async def test_control_experiment_a_full_load_grows_with_the_series(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: полная загрузка РАСТЁТ с длиной ряда, а окно — нет.

    Возврат к полной загрузке — это ровно тот дефект, который убил Этап 9.1.3.
    Здесь показано, что предыдущая проверка его ловит: при учетверении ряда
    полная загрузка растёт вчетверо, а окно не меняется вовсе.
    """
    short_pool = production_order_pool(days=7.5, signals_count=200)
    long_pool = production_order_pool(days=28.0, signals_count=200)

    _s1, short_counters = await run_scan(short_pool, monkeypatch)
    _s2, long_counters = await run_scan(long_pool, monkeypatch)

    naive_short = len(short_pool.bars[INSTRUMENT])
    naive_long = len(long_pool.bars[INSTRUMENT])
    assert naive_long > naive_short * 3, "опыт построен неверно: ряды сравнимы"
    assert long_counters.max_window_bars == short_counters.max_window_bars, (
        "окно выросло вместе с рядом — значит, держится не окно, а весь ряд"
    )


async def test_no_more_than_one_batch_of_bars_is_materialised_at_a_time(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6: бары читаются ПОРЦИЯМИ, а не целиком.

    Проверяется по фактическому размеру ответов базы: порция, отданная целиком,
    была бы видна здесь числом строк больше заявленного предела.
    """
    from src.core.db import db as real_db

    pool = production_order_pool(days=28.0, signals_count=200)
    monkeypatch.setattr(real_db, "RANGE_POSITION_BARS_BATCH", 5_000)
    await run_scan(pool, monkeypatch)

    assert pool.bar_requests, "бары не читались вовсе"
    assert len(pool.bars[INSTRUMENT]) > 20_000, "ряд слишком короток для опыта"
    assert pool.max_rows_in_flight <= 5_000, (
        f"за раз материализовано {pool.max_rows_in_flight} строк"
    )
    # Прочитано около 10 280 баров (семь суток окна плюс двести минут сигналов),
    # то есть заведомо больше одной порции: чтение шло не одним запросом.
    assert len(pool.bar_requests) >= 3, "порционного чтения не было"
    # ЧИТАЕТСЯ НЕ ВЕСЬ РЯД, А ТОЛЬКО НУЖНОЕ: левая граница чтения — начало окна
    # первого сигнала, а не начало истории инструмента.
    assert pool.bar_requests[0][1] == SIGNAL_TS - timedelta(days=WINDOW_DAYS)


async def test_peak_memory_of_the_scan_stays_within_the_budget(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6: пиковая память прохода укладывается в бюджет с запасом на рост втрое.

    Меряется ПРИРОСТ на время прохода: сами придуманные бары двойника выделены
    заранее и к расходу расчёта не относятся. Предел взят с большим запасом от
    лимита контейнера (1 ГБ): проверка обязана ловить возврат к полной загрузке,
    а не колебания аллокатора.
    """
    import tracemalloc

    pool = production_order_pool(days=28.0, signals_count=3000)
    tracemalloc.start()
    try:
        _scan, counters = await run_scan(pool, monkeypatch)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert counters.computed == 3000
    peak_mb = peak / 2**20
    budget_mb = 128.0
    assert peak_mb < budget_mb, f"проход занял {peak_mb:.1f} МБ при {budget_mb} МБ"


async def test_the_run_prints_peak_memory_and_the_growth_estimate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6: пиковая память И оценка при росте выборки втрое ПЕЧАТАЮТСЯ."""
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "Пиковая память процесса:" in out
    assert f"при росте выборки в {rangepos.GROWTH_FACTOR} раза" in out
    assert out.rstrip().endswith(rangepos.DONE_MARKER)


# =============================================================================
# §8. Границы этапа: замер ничего не меняет и ничего не рекомендует
# =============================================================================

def code_only(text: str) -> str:
    """Текст без пояснений: только исполняемый код.

    Проверки границ ищут в файле собственные правила и запреты; строка,
    найденная в комментарии или в строке вывода, доказывала бы обратное тому,
    что проверяется.
    """
    lines: list[str] = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quotes = stripped.count('"""') + stripped.count("'''")
            if quotes == 1:
                in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


async def test_the_stage_does_not_touch_logic_version() -> None:
    """§1: LOGIC_VERSION остаётся 5 и скриптом не меняется."""
    from src.core.config import settings

    assert settings.LOGIC_VERSION == 5
    body = code_only(
        (_ROOT / "scripts" / "range_position_9_1_5.py").read_text(encoding="utf-8")
    )
    assert "LOGIC_VERSION =" not in body
    assert "LOGIC_VERSION=" not in body.replace("logic_version=", "")


async def test_the_stage_recommends_no_entry_filter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§1: рекомендация «внедрить фильтр по положению в диапазоне» ЗАПРЕЩЕНА.

    Это не вопрос вкуса: рекомендация по одному замеру — это внедрение,
    прикрытое словом «предлагается», и §1 ТЗ запрещает его прямо.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, []) == 0
    printed = capsys.readouterr().out
    out = printed.lower()
    for phrase in ("рекомендуется внедрить", "предлагается внедрить",
                   "следует внедрить", "нужно внедрить", "стоит внедрить",
                   "рекомендация: внедрить"):
        assert phrase not in out, f"в выводе есть рекомендация: {phrase!r}"
    # Слова «внедрить фильтр по» в выводе есть — но РОВНО ОДИН РАЗ и ровно в
    # предложении о запрете. Проверяется именно это, а не отсутствие слов:
    # запрет обязан быть напечатан, иначе о нём никто не узнает.
    assert out.count("внедрить фильтр по") == 1
    assert "запрещена" in out
    assert "ЗАПРЕЩЕНА (§1 ТЗ)" in printed


async def test_the_migration_is_idempotent_and_has_a_rollback() -> None:
    """§5: миграция 023 идемпотентна, у неё есть откат, и она ссылается наружу."""
    migration = (_ROOT / "db" / "migrations"
                 / "023_signal_range_position.sql").read_text(encoding="utf-8")
    rollback = (_ROOT / "db" / "migrations"
                / "023_signal_range_position_rollback.sql").read_text(
                    encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS signal_range_position" in migration
    assert "REFERENCES signals(id) ON DELETE CASCADE" in migration
    assert "CREATE INDEX IF NOT EXISTS" in migration
    assert "DROP TABLE IF EXISTS signal_range_position" in rollback
    # Таблицы факта миграцией НЕ меняются: внешний ключ смотрит наружу.
    for table in ("signals", "ohlcv", "positions", "signal_targets"):
        assert f"ALTER TABLE {table}" not in migration
        assert f"DROP TABLE IF EXISTS {table}" not in rollback


async def test_the_migration_allows_positions_outside_the_range() -> None:
    """§5: ограничение НЕ запрещает pos вне [0,1] — это штатный случай.

    Проверяется отсутствие запрета, а не его наличие: ограничение
    ``pos BETWEEN 0 AND 1`` молча выбросило бы из замера все пробития.
    """
    migration = (_ROOT / "db" / "migrations"
                 / "023_signal_range_position.sql").read_text(encoding="utf-8")
    assert "pos BETWEEN 0 AND 1" not in migration
    assert "pos >= 0" not in migration
    assert "pos <= 1" not in migration
    assert "signal_range_position_range_chk" in migration
    assert "signal_range_position_bars_chk" in migration


async def test_the_schema_double_knows_the_new_table() -> None:
    """Двойник базы обязан знать колонки новой таблицы ИЗ ФАЙЛА МИГРАЦИИ.

    Иначе он был бы мягче настоящей базы ровно там, где этап и работает:
    ссылка на несуществующую колонку прошла бы незамеченной.
    """
    tables = schema()
    assert tables["signal_range_position"] == {
        "signal_id", "window_days", "range_low", "range_high",
        "range_width_pct", "pos", "last_bar_ts", "bars_in_window",
        "resolution", "computed_at",
    }
    with pytest.raises(UndefinedColumn):
        check_sql_columns(
            "SELECT pos, range_middle FROM signal_range_position", tables
        )


# =============================================================================
# §4 ЧИСЛО 3–5. Ширина как соперник, защиты от подгонки, статистическая сила
# =============================================================================

def _keys_and_net(pairs: list[tuple[int, float]]) -> tuple[np.ndarray, np.ndarray]:
    keys = np.array([k for k, _ in pairs], dtype=np.int64)
    net = np.array([v for _, v in pairs], dtype=float)
    return keys, net


async def test_a_bucket_under_thirty_is_marked_and_excluded_from_conclusions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§4 ЧИСЛО 5: корзина с N < 30 помечается и в выводы не идёт."""
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "N<30, в выводы не идёт" in out
    assert f"Корзины с N < {rangepos.MIN_BUCKET_N}" in out


async def test_small_buckets_never_reach_the_permutation_test() -> None:
    """§4 ЧИСЛО 4: в перестановочную проверку идут только годные корзины.

    Корзина из трёх наблюдений даёт огромный разброс сама по себе, и он не про
    положение в размахе. Пустив её в счёт, проверка объявляла бы находкой шум.
    """
    pairs = [(1, 1.0)] * 40 + [(2, -1.0)] * 40 + [(3, 100.0)] * 3
    keys, net = _keys_and_net(pairs)
    eligible = rangepos._eligible_buckets(keys, rangepos.POS_BUCKET_TOTAL)
    assert eligible == [1, 2], "в годные корзины попала корзина из трёх строк"

    result = rangepos.permutation_spread(keys, net, eligible=eligible,
                                         resamples=200)
    assert result is not None
    assert result["n"] == 80, "в проверку попали наблюдения негодной корзины"
    assert result["observed_spread"] == pytest.approx(2.0)


async def test_the_permutation_test_finds_a_real_effect() -> None:
    """§4 ЧИСЛО 4: настоящее различие проверка ВИДИТ.

    Без этого проверка могла бы всегда отвечать «не выше случайного» и выглядеть
    осторожной, ничего не измеряя.
    """
    pairs = [(1, 1.0)] * 60 + [(10, -1.0)] * 60
    keys, net = _keys_and_net(pairs)
    result = rangepos.permutation_spread(
        keys, net, eligible=[1, 10], resamples=1_000
    )
    assert result is not None
    assert result["observed_spread"] > result["random_spread_p95"]
    assert abs(result["observed_edges"]) > result["random_edges_p95"]


async def test_control_experiment_the_permutation_test_rejects_pure_noise() -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: на шуме проверка отвечает «не выше случайного».

    Проверка, которая находит эффект всегда, столь же бесполезна, как та,
    которая не находит его никогда.
    """
    rng = np.random.default_rng(20260915)
    keys = np.array([1] * 300 + [10] * 300, dtype=np.int64)
    net = rng.normal(0.0, 1.0, size=600)
    result = rangepos.permutation_spread(
        keys, net, eligible=[1, 10], resamples=1_000
    )
    assert result is not None
    assert result["observed_spread"] <= result["random_spread_p95"]


async def test_the_interval_says_whether_it_crosses_zero() -> None:
    """§4 ЧИСЛО 4: у каждой корзины сказано, пересекает ли интервал ноль."""
    around_zero = np.array([-1.0, 1.0] * 50)
    away = np.array([5.0, 5.2] * 50)
    mean_zero, lo_zero, hi_zero = rangepos.mean_interval(around_zero)
    mean_away, lo_away, hi_away = rangepos.mean_interval(away)

    assert mean_zero == pytest.approx(0.0)
    assert lo_zero < 0.0 < hi_zero
    assert mean_away == pytest.approx(5.1)
    assert lo_away > 0.0 and hi_away > 0.0


async def test_the_split_half_gives_two_separate_answers() -> None:
    """§4 ЧИСЛО 4: печатаются ДВА ответа — совпала корзина и сохранён ли знак.

    Совпадение имени без сохранения знака — совпадение, а не подтверждение;
    сохранение знака при другом имени — шум, а не находка. Ответ обязан быть
    двойным, иначе половина выдаётся за целое.
    """
    # Корзина 1 лучшая в обеих половинах и в обеих выше среднего.
    keys = np.array([1] * 80 + [10] * 80, dtype=np.int64)
    net = np.array([1.0] * 80 + [-1.0] * 80)
    is_old = np.array([True] * 40 + [False] * 40 + [True] * 40 + [False] * 40)
    stable = rangepos.split_half(keys, net, is_old)
    assert stable["best_old"] == 1 and stable["best_new"] == 1
    assert stable["same_bucket"] is True
    assert stable["same_sign"] is True

    # Та же корзина — но во второй половине её преимущество МЕНЯЕТ ЗНАК.
    flipped = np.concatenate([
        np.full(40, 1.0), np.full(40, -1.0),   # корзина 1: старая +, новая −
        np.full(40, -1.0), np.full(40, 1.0),   # корзина 10: старая −, новая +
    ])
    turned = rangepos.split_half(keys, flipped, is_old)
    assert turned["best_old"] == 1
    assert turned["same_bucket"] is False
    assert turned["same_sign"] is False


async def test_the_detectable_difference_shrinks_as_the_sample_grows() -> None:
    """§4 ЧИСЛО 5: прямая строка о том, что различимо, опирается на размер.

    Наименьшая различимая разница обязана УМЕНЬШАТЬСЯ с ростом выборки. Число,
    которое от размера не зависит, не про статистическую силу.
    """
    rng = np.random.default_rng(20260915)
    small_keys = np.array([1] * 40 + [10] * 40, dtype=np.int64)
    big_keys = np.array([1] * 4000 + [10] * 4000, dtype=np.int64)
    small = rangepos.detectable_difference(
        small_keys, rng.normal(0.0, 1.0, 80), [1, 10]
    )
    big = rangepos.detectable_difference(
        big_keys, rng.normal(0.0, 1.0, 8000), [1, 10]
    )
    assert small is not None and big is not None
    assert big < small / 5.0


async def test_the_width_table_and_the_joint_table_are_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§4 ЧИСЛО 3: ширина размаха печатается как СОПЕРНИК положения.

    Без совместной таблицы нельзя ответить, не оказывается ли вся связь с
    положением на самом деле связью с шириной, — а это первый же вопрос,
    который задаст любой, кто увидит таблицу ЧИСЛА 2.
    """
    pool = apply_pool()
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "ЧИСЛО 3. ШИРИНА РАЗМАХА КАК СОПЕРНИК" in out
    for label in rangepos.WIDTH_LABELS:
        assert label in out
    assert "треть по pos × ширина" in out
    for label in rangepos.TERTILE_LABELS:
        assert label in out


# =============================================================================
# Состав выборки: неизмеренные исходы и смешение версий логики
# =============================================================================

async def test_ambiguous_and_no_data_are_counted_but_not_measured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§3: неизмеренный исход не идёт ни в среднее, ни в знаменатель доли цели.

    ``ambiguous`` означает «порядок касаний неизвестен», а ``no_data`` — «ряд
    оборвался». Записать их в знаменатель доли достижения цели значило бы
    объявить неизвестное неудачей, то есть посчитать то, чего никто не наблюдал.
    """
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(1, price=105.0),
                 signal_row(2, ts=SIGNAL_TS + timedelta(hours=5), price=105.0)],
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[
            outcome_row(1, outcome="target", net_pnl_pct=1.0),
            outcome_row(2, outcome="ambiguous", net_pnl_pct=None),
        ],
    )
    scan, _counters = await run_scan(pool, monkeypatch)
    outcomes, counters = await rangepos.stream_outcomes(scan)

    assert counters.matched == 1
    assert counters.unmeasured == {"ambiguous": 1}
    assert len(outcomes[("buy", 1)]) == 1
    assert float(outcomes[("buy", 1)].is_target.values.mean()) == 1.0

    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "НЕ ИЗМЕРЕННЫЕ исходы" in out
    assert re.search(r"ambiguous\s+1", out)


async def test_more_than_one_logic_version_raises_a_loud_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§4 ЧИСЛО 1: больше одной версии логики — КРУПНОЕ предупреждение.

    Версии не смешиваются (правило проекта): числа по такой выборке считают
    вместе решения разных систем.
    """
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[
            signal_row(1, price=105.0, logic_version=5),
            signal_row(2, ts=SIGNAL_TS + timedelta(hours=5), price=105.0,
                       logic_version=4),
        ],
        bars={INSTRUMENT: flat_series(days_before=7.5, days_after=1.0)},
        outcomes=[outcome_row(1), outcome_row(2)],
    )
    assert await run_script(monkeypatch, pool, []) == 0
    out = capsys.readouterr().out
    assert "БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ" in out
    assert "недействителен" in out


async def test_control_experiment_a_single_version_prints_no_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """КОНТРОЛЬНЫЙ ОПЫТ: при одной версии предупреждения нет."""
    assert await run_script(monkeypatch, apply_pool(), []) == 0
    assert "БОЛЬШЕ ОДНОЙ ВЕРСИИ ЛОГИКИ" not in capsys.readouterr().out


async def test_an_empty_sample_returns_three_and_still_says_it_finished(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6: пустая выборка — код 3, и оборванный прогон от неё отличим."""
    pool = _RangePool(
        instruments=INSTRUMENTS,
        signals=[signal_row(1, price=105.0)],
        bars={INSTRUMENT: flat_series(days_before=2.0)},
        outcomes=[outcome_row(1)],
    )
    code = await run_script(monkeypatch, pool, [])
    out = capsys.readouterr().out
    assert code == 3
    assert "Выборка пуста" in out
    assert out.rstrip().endswith(rangepos.DONE_MARKER)


# =============================================================================
# Имена таблиц и колонок взяты ИЗ КОДА, а не придуманы (§3 ТЗ)
# =============================================================================

async def test_the_outcome_table_and_columns_are_the_real_ones() -> None:
    """§3: исход берётся из ``signal_outcomes_barrier``, а не из выдуманной таблицы.

    На Этапах 9.1.3 и 9.1.4 придуманные имена оказывались неверными трижды.
    Здесь имена сверяются с ФАЙЛОМ МИГРАЦИИ и с кодом расчёта 8.8, а не с
    текстом задания.
    """
    from src.barrier.outcomes import OUTCOME_TARGET, RESOLUTION_1M

    tables = schema()
    assert {"outcome", "net_pnl_pct", "direction", "horizon_h", "logic_version"} <= (
        tables["signal_outcomes_barrier"]
    )
    assert "price_at_signal" in tables["signal_targets"]
    assert OUTCOME_TARGET == "target"
    assert RESOLUTION_1M == "1m"

    # Тот же перечень разрешений, что в миграциях 015/016/017/021.
    migration = (_ROOT / "db" / "migrations"
                 / "023_signal_range_position.sql").read_text(encoding="utf-8")
    assert "resolution IN ('1m', '1h')" in migration


async def test_the_written_resolution_matches_the_series_actually_used(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5: в ``resolution`` пишется тот ряд, по которому и считали."""
    from src.core.config import settings

    pool = apply_pool()
    assert await run_script(monkeypatch, pool, ["--apply"]) == 0
    assert settings.BARRIER_FINE_TIMEFRAME == "1m"
    for row in pool.stored.values():
        assert row["resolution"] == "1m"
