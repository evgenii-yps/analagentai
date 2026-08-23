"""Тесты Этапа 8.1 (§10 ТЗ): пять токенов и четыре горизонта оценки.

Каждый тест назван требованием, которое он стережёт:

  10.1 test_market_split          — Market получает СПОТ, Futures — КОНТРАКТ;
  10.2 test_eval_four_horizons    — один сигнал даёт ровно четыре оценки;
  10.3 test_no_backfill_horizons  — старым сигналам горизонты не досчитываются;
  10.4 test_retention             — старое удаляется, защищённое не трогается;
  10.5 test_closed_bars_toggle    — переключатель незавершённого бара.

Тесты работают без БД и без биржи: слой доступа к данным подменяется двойником,
который записывает, ЧТО у него спросили. Именно это и проверяется — какой рынок
у какого агента и какие строки создаёт оценщик.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from src.core.instruments import (
    InstrumentConfigError,
    ensure_instruments,
    horizon_label,
    parse_horizon_hours,
    parse_symbol_pairs,
)

# --- §1: разбор пар «спот:контракт» ----------------------------------------


def test_pairs_are_parsed_by_the_first_colon() -> None:
    """Имя контракта само содержит ':' — разделитель ищется ПЕРВЫЙ."""
    pairs = parse_symbol_pairs("BTC/USDT:BTC/USDT:USDT,ETH/USDT:ETH/USDT:USDT")
    assert [(p.spot, p.swap) for p in pairs] == [
        ("BTC/USDT", "BTC/USDT:USDT"),
        ("ETH/USDT", "ETH/USDT:USDT"),
    ]
    assert [p.token for p in pairs] == ["BTC", "ETH"]


def test_five_tokens_are_parsed() -> None:
    raw = (
        "BTC/USDT:BTC/USDT:USDT,ETH/USDT:ETH/USDT:USDT,SOL/USDT:SOL/USDT:USDT,"
        "XRP/USDT:XRP/USDT:USDT,DOGE/USDT:DOGE/USDT:USDT"
    )
    pairs = parse_symbol_pairs(raw)
    assert len(pairs) == 5
    assert [p.token for p in pairs] == ["BTC", "ETH", "SOL", "XRP", "DOGE"]


def test_symbol_without_contract_is_rejected() -> None:
    """Достраивание имени контракта запрещено: пара без него — ошибка."""
    with pytest.raises(InstrumentConfigError, match="не пара"):
        parse_symbol_pairs("BTC/USDT")


def test_duplicate_pair_is_rejected() -> None:
    with pytest.raises(InstrumentConfigError, match="дважды"):
        parse_symbol_pairs("BTC/USDT:BTC/USDT:USDT,BTC/USDT:BTC/USDT:USDT")


def test_horizon_parsing_accepts_both_formats() -> None:
    """«4» и «4h» — один и тот же горизонт: .env не обязан меняться с кодом."""
    assert parse_horizon_hours("4") == parse_horizon_hours("4h") == 4
    assert parse_horizon_hours(12) == 12
    assert horizon_label(24) == "24h"


# --- 10.1 Market — спот, Futures — контракт ---------------------------------


class _FakeDb:
    """Двойник слоя БД: раздаёт идентификаторы и помнит, что у него просили."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self._next_id = 1

    async def get_or_create_instrument(
        self, exchange: str, symbol: str, type_: str = "spot"
    ) -> int:
        self.created.append((exchange, symbol, type_))
        instrument_id = self._next_id
        self._next_id += 1
        return instrument_id


async def _instrument_ids(raw: str):
    return await ensure_instruments(_FakeDb(), "okx", parse_symbol_pairs(raw))


async def test_market_split() -> None:
    """§10.1: Market работает на споте, Futures — на контракте.

    Проверяется не намерение, а фактические аргументы конструкторов: агенту
    передаётся идентификатор инструмента, и подмена одного другим обязана
    ломать тест.
    """
    from src.agents.futures import FuturesAgent
    from src.agents.liquidity import LiquidityAgent
    from src.agents.market import MarketAgent

    items = await _instrument_ids("BTC/USDT:BTC/USDT:USDT,ETH/USDT:ETH/USDT:USDT")
    assert [(i.spot_id, i.swap_id) for i in items] == [(1, 2), (3, 4)]

    for item in items:
        market = MarketAgent(item.spot_id, "1h", 200, 60, name_suffix=item.token)
        liquidity = LiquidityAgent(item.spot_id, 60, name_suffix=item.token)
        futures = FuturesAgent(item.swap_id, "1h", 60, name_suffix=item.token)

        # Рынок каждого агента.
        assert market.instrument_id == item.spot_id, "Market обязан работать на СПОТЕ"
        assert liquidity.instrument_id == item.spot_id
        assert futures.instrument_id == item.swap_id, (
            "Futures обязан работать на КОНТРАКТЕ"
        )
        # Спот и контракт — разные инструменты, иначе проверка ничего не значит.
        assert market.instrument_id != futures.instrument_id

        # Имя агента остаётся плоским (по нему ищет Decision Agent и настроены
        # веса), а токен различает экземпляры в Redis-ключах.
        assert (market.name, futures.name) == ("market", "futures")
        assert market.key.endswith(f":{item.token}")
        assert futures.key.endswith(f":{item.token}")


async def test_instruments_are_created_as_spot_and_swap() -> None:
    """Спот заводится типом spot, контракт — типом swap (иначе перепутаются)."""
    fake = _FakeDb()
    await ensure_instruments(fake, "okx", parse_symbol_pairs("SOL/USDT:SOL/USDT:USDT"))
    assert fake.created == [
        ("okx", "SOL/USDT", "spot"),
        ("okx", "SOL/USDT:USDT", "swap"),
    ]


# --- 10.2 и 10.3: горизонты оценки ------------------------------------------


class _EvalDb:
    """Двойник БД для оценщика: сигналы, свечи и запись оценок в память."""

    def __init__(self, signals: list[dict[str, Any]]) -> None:
        self.signals = signals
        self.saved: list[tuple[int, int]] = []      # (signal_id, horizon_h)
        self.finalized: list[int] = []
        self.asked_since: list[Any] = []

    async def get_signals_to_evaluate(self, horizon_h: int, since=None):
        self.asked_since.append(since)
        now = datetime.now(UTC)
        result = []
        for signal in self.signals:
            if signal["decision"] == "wait":
                continue
            if (now - signal["ts"]).total_seconds() < horizon_h * 3600:
                continue
            if since is not None and signal["ts"] < since:
                continue          # досчёт задним числом запрещён (§12 ТЗ 8.1)
            if (signal["id"], horizon_h) in self.saved:
                continue
            result.append(signal)
        return result

    async def get_price_at(self, instrument_id: int, ts, timeframe: str = "1m"):
        return 100.0

    async def get_ohlcv_window(self, instrument_id: int, start_ts, end_ts,
                               timeframe: str = "1m"):
        # Окно «полное»: последняя свеча совпадает с концом горизонта.
        return [{"ts": end_ts, "high": 110.0, "low": 95.0, "close": 105.0}]

    async def save_evaluation(self, signal_id, horizon_h, *args, **kwargs):
        self.saved.append((signal_id, int(horizon_h)))

    async def finalize_signal(self, signal_id, *args, **kwargs):
        self.finalized.append(signal_id)

    async def get_success_stats(self):
        return []


async def _run_evaluator(fake_db, monkeypatch, evaluate_from=None, horizons=None):
    from src.evaluator import evaluator as module

    monkeypatch.setattr(module, "db", fake_db)
    ev = module.Evaluator(
        interval=1,
        horizons=horizons or [1, 4, 12, 24],
        primary_horizon=4,
        stats_log_interval=3600,
        evaluate_from=evaluate_from,
    )
    await ev.evaluate_once()
    return ev


async def test_eval_four_horizons(monkeypatch) -> None:
    """§10.2: один сигнал порождает РОВНО четыре записи оценки."""
    ts = datetime.now(UTC) - timedelta(hours=30)
    fake = _EvalDb([{"id": 7, "instrument_id": 1, "ts": ts, "decision": "buy"}])

    await _run_evaluator(fake, monkeypatch)

    assert sorted(h for _sid, h in fake.saved) == [1, 4, 12, 24]
    assert len(fake.saved) == 4, "сигнал один, оценок ровно четыре"
    # Сигнал закрывается ОДИН раз — по главному горизонту.
    assert fake.finalized == [7]


async def test_eval_horizons_are_independent(monkeypatch) -> None:
    """Не набравший срок горизонт не мешает остальным.

    Сигналу 5 часов: горизонты 1ч и 4ч оценены, 12ч и 24ч ждут своего времени.
    """
    ts = datetime.now(UTC) - timedelta(hours=5)
    fake = _EvalDb([{"id": 8, "instrument_id": 1, "ts": ts, "decision": "sell"}])

    await _run_evaluator(fake, monkeypatch)

    assert sorted(h for _sid, h in fake.saved) == [1, 4]


async def test_no_backfill_horizons(monkeypatch) -> None:
    """§10.3: сигналам ДО границы версии логики горизонты не досчитываются."""
    boundary = datetime.now(UTC) - timedelta(hours=10)
    old_signal = {
        "id": 1, "instrument_id": 1,
        "ts": boundary - timedelta(hours=40), "decision": "buy",
    }
    new_signal = {
        "id": 2, "instrument_id": 1,
        "ts": boundary + timedelta(hours=1), "decision": "buy",
    }
    fake = _EvalDb([old_signal, new_signal])

    await _run_evaluator(fake, monkeypatch, evaluate_from=boundary)

    evaluated_ids = {sid for sid, _h in fake.saved}
    assert evaluated_ids == {2}, (
        "сигнал старше границы версии получил оценку — это досчёт задним числом"
    )
    # Граница действительно передавалась в выборку, а не проверялась «на глаз».
    assert fake.asked_since and all(s == boundary for s in fake.asked_since)


def test_horizon_label_written_alongside_hours() -> None:
    """Текстовая колонка горизонта заполняется подписью того же значения."""
    assert horizon_label(1) == "1h"
    assert horizon_label(4) == "4h"
    assert horizon_label(12) == "12h"
    assert horizon_label(24) == "24h"


# --- 10.4 Срок хранения ------------------------------------------------------


def test_retention_rules_match_the_specification() -> None:
    """§10.4: сроки заданы ТЗ 8.1 §4, минутные свечи чистятся по таймфрейму."""
    import scripts.retention as retention

    rules = {table: (days, where) for table, days, where in retention.RETENTION_RULES}
    assert rules["orderbook_snapshots"][0] == 14
    assert rules["ohlcv"][0] == 30
    assert "timeframe = '1m'" in rules["ohlcv"][1], (
        "правило по ohlcv без условия по таймфрейму снесло бы часовые свечи"
    )


def test_retention_refuses_protected_tables() -> None:
    """§10.4: защищённые таблицы не трогаются — правило отклоняется целиком."""
    import scripts.retention as retention

    for table in ("signals", "signal_evaluations", "funding", "open_interest"):
        with pytest.raises(retention.RetentionRuleError):
            retention._check_protected(table, "")


def test_retention_refuses_ohlcv_without_timeframe() -> None:
    """Часовые свечи не удаляются никогда — правило без таймфрейма запрещено."""
    import scripts.retention as retention

    with pytest.raises(retention.RetentionRuleError, match="часовые"):
        retention._check_protected("ohlcv", "")
    # С условием по таймфрейму правило допустимо.
    retention._check_protected("ohlcv", "AND timeframe = '1m'")


def test_retention_delete_statement_carries_the_timeframe(monkeypatch) -> None:
    """В SQL удаления минутных свечей ДОЛЖНО быть условие по таймфрейму."""
    import scripts.retention as retention

    executed: list[str] = []

    def fake_psql(sql: str) -> str:
        executed.append(sql)
        return "DELETE 0"

    monkeypatch.setattr(retention, "_psql", fake_psql)
    retention._delete_in_batches("ohlcv", 30, "AND timeframe = '1m'")

    assert executed, "запрос не выполнялся"
    assert "timeframe = '1m'" in executed[0]
    assert "interval '30 days'" in executed[0]


# --- 10.5 Незавершённый бар --------------------------------------------------


def _frame(last_ts: datetime) -> pd.DataFrame:
    """Три часовые свечи, последняя с меткой ``last_ts``."""
    rows = [
        {
            "ts": last_ts - timedelta(hours=2 - i),
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0 + i, "volume": 10.0,
        }
        for i in range(3)
    ]
    return pd.DataFrame(rows)


def test_closed_bars_toggle() -> None:
    """§10.5: при false окно включает незавершённый бар, при true — нет."""
    from src.agents.market import drop_unclosed_bar

    now = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    # Последний бар — 17:00, он закроется только в 18:00, то есть НЕ закрыт.
    df = _frame(datetime(2026, 8, 22, 17, 0, tzinfo=UTC))

    # false: функция не вызывается вовсе — окно остаётся как есть.
    assert len(df) == 3

    # true: незавершённый бар исключается.
    trimmed = drop_unclosed_bar(df, "1h", now)
    assert len(trimmed) == 2
    assert trimmed["ts"].iloc[-1] == datetime(2026, 8, 22, 16, 0, tzinfo=UTC)


def test_closed_bar_at_the_boundary_stays() -> None:
    """Бар 17:00 закрыт РОВНО в 18:00:00 — и остаётся в окне."""
    from src.agents.market import drop_unclosed_bar

    df = _frame(datetime(2026, 8, 22, 17, 0, tzinfo=UTC))
    exactly_closed = drop_unclosed_bar(df, "1h", datetime(2026, 8, 22, 18, 0, tzinfo=UTC))
    assert len(exactly_closed) == 3


def test_default_leaves_behaviour_unchanged() -> None:
    """Значение по умолчанию — false: поведение системы не меняется (§8 ТЗ 8.1)."""
    from src.core.config import settings

    assert settings.MARKET_CLOSED_BARS_ONLY is False


def test_unknown_timeframe_is_not_guessed() -> None:
    """Незнакомый таймфрейм не даёт повода угадывать длительность бара."""
    from src.agents.market import drop_unclosed_bar, timeframe_seconds

    assert timeframe_seconds("7h") is None
    df = _frame(datetime(2026, 8, 22, 17, 0, tzinfo=UTC))
    assert len(drop_unclosed_bar(df, "7h", datetime(2026, 8, 22, 17, 30, tzinfo=UTC))) == 3


# --- Многотокенные уведомления ----------------------------------------------


async def test_notification_names_the_signals_own_token(monkeypatch) -> None:
    """Уведомление называет инструмент СИГНАЛА, а не символ из настройки.

    С пятью токенами подпись из настройки SYMBOL сделала бы все пять сообщений
    сигналами по биткоину — читатель не отличил бы XRP от BTC.
    """
    from src.notify import agent as notify_module

    class _SymbolDb:
        def __init__(self) -> None:
            self.calls = 0

        async def get_instrument_symbol(self, instrument_id: int):
            self.calls += 1
            return {7: "XRP/USDT", 9: "DOGE/USDT"}.get(instrument_id)

    fake = _SymbolDb()
    monkeypatch.setattr(notify_module, "db", fake)
    notifier = notify_module.NotifyAgent(
        interval=30, min_probability=0.7, cooldown_sec=600,
        symbol="BTC/USDT", tz_name="UTC", primary_horizon="4h",
        min_agents=2, use_calibrated=False, min_calibrated=0.55,
    )

    cfg = await notifier._format_config(7)
    assert cfg.symbol == "XRP/USDT"
    assert (await notifier._format_config(9)).symbol == "DOGE/USDT"

    # Символ кэшируется: повторный сигнал по тому же инструменту не идёт в БД.
    await notifier._format_config(7)
    assert fake.calls == 2, "символ должен запрашиваться один раз на инструмент"

    # Неизвестный инструмент не теряет уведомление — берётся подпись настройки.
    assert (await notifier._format_config(404)).symbol == "BTC/USDT"


# --- Сырая лента сделок: кто её читает (решение по §4.3, пункт 3) ------------


def test_no_component_reads_raw_trades() -> None:
    """Ни один агент, отчёт или выгрузка не читает таблицу ``trades``.

    От этого зависит решение хранить сырьё всего трое суток: содержательная
    часть уходит в ``trade_flow_1m``, а сырьё нужно только для разбора
    инцидентов. Если кто-то начнёт читать сделки напрямую, тест упадёт — и это
    напоминание, что данных старше трёх суток там уже нет.

    Разрешены ровно три места, и все три — счётчики за окно не длиннее суток
    (детекторы «тихой поломки» потока), а не чтение самих сделок:
      * ``src/health/daily_report.py`` и ``src/bot/queries.py`` — count(*) за
        24 часа по белому списку таблиц ``DATA_STREAMS``;
      * ``scripts/measure_load.sh`` — count(*) за час в замере §3.
    Свёртка и удаление живут в ``scripts/retention.py`` — это работа с сырьём,
    а не его чтение для анализа.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"\b(from|join)\s+trades\b", re.IGNORECASE)

    offenders: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")

    assert not offenders, (
        "появилось прямое чтение сырой ленты сделок — сырьё живёт трое суток, "
        "используйте trade_flow_1m:\n" + "\n".join(offenders)
    )


def test_daily_report_counts_trades_by_whitelist() -> None:
    """Счётчик потока сделок остаётся — он работает и при сроке в трое суток."""
    from src.bot.queries import DATA_STREAMS

    tables = {table for _label, table in DATA_STREAMS}
    assert "trades" in tables, (
        "счётчик потока сделок исчез: пропажу ленты станет нечем заметить"
    )


def test_trades_retention_is_two_days_and_rollups_run_first() -> None:
    """Сырьё сделок — ДВОЕ суток (поправка по бюджету диска), свёртки — ДО удаления.

    Порядок проверяется по исходному тексту задачи, а не по договорённости:
    обе свёртки обязаны вызываться раньше первого удаления, иначе сырьё уйдёт,
    не оставив итогов.
    """
    import inspect

    import scripts.retention as retention

    rules = {table: days for table, days, _where in retention.RETENTION_RULES}
    assert rules["trades"] == 2

    source = inspect.getsource(retention.main)
    delete_at = source.index("_delete_in_batches")
    assert source.index("rollup_sql()") < delete_at, "лента сделок удаляется до свёртки"
    assert source.index("rollup_daily_sql()") < delete_at, (
        "журнал выводов удаляется до суточной свёртки"
    )
    # Защита «не свернулось — не удаляем» для обеих таблиц.
    assert "rollup_ok" in source and "daily_ok" in source


def test_agent_outputs_has_a_retention_rule_and_is_not_protected() -> None:
    """Журнал выводов агентов чистится по сроку: §4 ТЗ не относит его к вечным.

    Без срока он даёт 6.6 ГБ в год на пять токенов и один делает недостижимым
    порог §3 (свободного места не меньше 40%) на горизонте года. Мнения,
    участвовавшие в решении, остаются навсегда в ``signals.agents_payload``.
    """
    import scripts.retention as retention

    rules = {table: days for table, days, _where in retention.RETENTION_RULES}
    assert rules.get("agent_outputs") == 90
    assert "agent_outputs" not in retention.PROTECTED_TABLES


def test_protected_tables_match_the_specification() -> None:
    """Перечень вечных таблиц — дословно из §4 ТЗ 8.1 плюс итоги сделок."""
    import scripts.retention as retention

    for table in ("funding", "open_interest", "signals", "signal_evaluations",
                  "trade_flow_1m"):
        assert table in retention.PROTECTED_TABLES
        with pytest.raises(retention.RetentionRuleError):
            retention._check_protected(table, "")


# --- Ноль не может быть настоящей версией логики (дефект 22.08.2026) ---------


def test_logic_version_zero_is_rejected_by_config() -> None:
    """LOGIC_VERSION=0 отвергается на старте, а не роняет сервис ошибкой БД.

    Ноль зарезервирован под признак «версия неизвестна» в agent_outputs_daily.
    Если бы его можно было задать настоящей версией, отличить «версия 0» от
    «версии нет» стало бы невозможно — а вечную таблицу переписать нечем.
    """
    import pytest as _pytest

    from src.core.config import Settings

    with _pytest.raises(ValueError, match="LOGIC_VERSION"):
        Settings(LOGIC_VERSION=0)


def test_logic_version_negative_is_rejected_by_config() -> None:
    """Отрицательная версия тоже отвергается: версии нумеруются с единицы."""
    import pytest as _pytest

    from src.core.config import Settings

    with _pytest.raises(ValueError, match="LOGIC_VERSION"):
        Settings(LOGIC_VERSION=-1)
