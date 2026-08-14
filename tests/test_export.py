"""Тесты выгрузки: сборка строк листов, расчёт 4-часового окна, маппинг Notion."""

from datetime import UTC, datetime

from src.export.transform import (
    SIGNALS_HEADER,
    build_notion_properties,
    build_signal_row,
    build_summary_row,
    extract_agent_columns,
    notified_cell,
    participating_agents,
    success_cell,
    window_4h_start,
)

_TS = datetime(2026, 8, 10, 13, 47, 0, tzinfo=UTC)

# Полный payload из трёх агентов (как пишет Decision Agent).
_PTS = "2026-08-10T13:46:00+00:00"
_PAYLOAD_FULL = [
    {"agent": "market", "signal": "bullish", "confidence": 0.42, "ts": _PTS},
    {"agent": "liquidity", "signal": "bearish", "confidence": 0.31, "ts": _PTS},
    {"agent": "futures", "signal": "neutral", "confidence": 0.1, "ts": _PTS},
]

# Payload без futures (агент не набрал данных).
_PAYLOAD_NO_FUTURES = _PAYLOAD_FULL[:2]


def _signal(**overrides) -> dict:
    """Базовая запись сигнала с приджойненными оценками горизонтов."""
    base = {
        "id": 101,
        "ts": _TS,
        "decision": "buy",
        "probability": 0.8123,
        "status": "closed",
        "rationale": "market=bullish; балл=+0.40 → buy.",
        "notified": False,
        "notified_at": None,
        "agents_payload": _PAYLOAD_FULL,
        "token": "BTC",
        "p_signal_1h": 60000.0,
        "p_close_1h": 60300.0,
        "pnl_1h": 0.5,
        "dd_1h": 0.2,
        "succ_1h": True,
        "p_signal_4h": 60000.0,
        "p_close_4h": 60600.0,
        "pnl_4h": 1.0,
        "dd_4h": 0.3,
        "succ_4h": True,
        "logic_version": 2,
    }
    base.update(overrides)
    return base


# --- window_4h_start (§7.3) ---

def test_window_4h_start_aligns_to_4h_grid() -> None:
    # 13:47 → окно 12:00 (12 = ближайшая кратная 4 граница снизу).
    assert window_4h_start(_TS) == datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def test_window_4h_start_exact_boundary() -> None:
    assert window_4h_start(datetime(2026, 8, 10, 16, 0, 0, tzinfo=UTC)) == datetime(
        2026, 8, 10, 16, 0, 0, tzinfo=UTC
    )


def test_window_4h_start_first_hours() -> None:
    for hour in (0, 1, 2, 3):
        ts = datetime(2026, 8, 10, hour, 30, tzinfo=UTC)
        assert window_4h_start(ts) == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


def test_window_4h_start_windows_do_not_overlap() -> None:
    # Часы 20–23 попадают в окно 20:00, а не 16:00 или 00:00 следующих суток.
    for hour in (20, 21, 22, 23):
        ts = datetime(2026, 8, 10, hour, 5, tzinfo=UTC)
        assert window_4h_start(ts) == datetime(2026, 8, 10, 20, 0, tzinfo=UTC)


# --- extract_agent_columns (колонки 11–16) ---

def test_agent_columns_full_payload() -> None:
    cols = extract_agent_columns(_PAYLOAD_FULL)
    assert cols["market_signal"] == "bullish"
    assert cols["market_confidence"] == 0.42
    assert cols["futures_signal"] == "neutral"
    assert cols["futures_confidence"] == 0.1


def test_agent_columns_missing_futures_is_empty_not_zero() -> None:
    cols = extract_agent_columns(_PAYLOAD_NO_FUTURES)
    # Ключевой критерий приёмки: пусто, а не 0.
    assert cols["futures_signal"] == ""
    assert cols["futures_confidence"] == ""
    assert cols["futures_confidence"] != 0


def test_agent_columns_accepts_json_string() -> None:
    import json

    cols = extract_agent_columns(json.dumps(_PAYLOAD_FULL))
    assert cols["liquidity_signal"] == "bearish"


# --- notified_cell (колонка 8), три значения (ТЗ 6.6.1 §9) ---

def test_notified_cell_sent() -> None:
    # Есть notified_at → «да» независимо от флага notified.
    assert notified_cell(True, _TS) == "да"


def test_notified_cell_absorbed() -> None:
    # notified=TRUE, notified_at пуст → поглощён анти-спамом.
    assert notified_cell(True, None) == "поглощён"


def test_notified_cell_not_processed() -> None:
    # notify ещё не трогал сигнал.
    assert notified_cell(False, None) == "нет"


# --- success_cell ---

def test_success_cell_variants() -> None:
    assert success_cell(True) == "да"
    assert success_cell(False) == "нет"
    assert success_cell(None) == ""


# --- build_signal_row ---

def test_signal_row_length_matches_header() -> None:
    row = build_signal_row(_signal())
    assert len(row) == len(SIGNALS_HEADER) == 29


def test_signal_row_logic_version_then_degraded_last() -> None:
    # logic_version — предпоследняя, degraded — последняя (Этап 7.2): новые
    # колонки добавлены в конец, существующие не сдвинуты (§D.3 / A2).
    assert SIGNALS_HEADER[-2] == "logic_version"
    assert SIGNALS_HEADER[-1] == "degraded"
    assert build_signal_row(_signal(logic_version=2))[27] == 2
    # Отсутствие поля → версия 1 (исторические сигналы «до» правок).
    assert build_signal_row(_signal(logic_version=None))[27] == 1


def test_signal_row_degraded_cell() -> None:
    assert build_signal_row(_signal(degraded=True))[28] == "да"
    assert build_signal_row(_signal(degraded=False))[28] == "нет"
    # Отсутствие поля (старый сигнал) → «нет».
    assert build_signal_row(_signal())[28] == "нет"


def test_signal_row_core_fields() -> None:
    row = build_signal_row(_signal(notified_at=_TS))
    assert row[0] == 101                       # signal_id
    assert row[1] == "2026-08-10T13:47:00+00:00"  # ts_utc
    assert row[3] == "2026-08-10T12:00:00+00:00"  # window_4h_utc
    assert row[4] == "BTC"                     # token
    assert row[5] == "buy"                     # decision
    assert row[6] == 0.8123                    # probability, 4 знака
    assert row[7] == "да"                      # notified
    assert row[9] == 3                         # agents_count
    assert row[24] == "closed"                 # status


def test_signal_row_notified_tristate() -> None:
    # да (есть notified_at) / поглощён (notified без notified_at) / нет.
    assert build_signal_row(_signal(notified=True, notified_at=_TS))[7] == "да"
    assert build_signal_row(_signal(notified=True, notified_at=None))[7] == "поглощён"
    assert build_signal_row(_signal(notified=False, notified_at=None))[7] == "нет"


def test_signal_row_price_falls_back_to_1h() -> None:
    # Нет оценки 4h → price_at_signal берётся из 1h.
    row = build_signal_row(_signal(p_signal_4h=None))
    assert row[16] == 60000.0


def test_signal_row_missing_futures_columns_empty() -> None:
    row = build_signal_row(_signal(agents_payload=_PAYLOAD_NO_FUTURES))
    assert row[14] == ""  # futures_signal
    assert row[15] == ""  # futures_confidence
    assert row[9] == 2    # agents_count


def test_signal_row_rationale_truncated() -> None:
    row = build_signal_row(_signal(rationale="x" * 3000))
    assert len(row[25]) == 2000


def test_signal_row_payload_json_passthrough_for_string() -> None:
    raw = '[{"agent": "market"}]'
    row = build_signal_row(_signal(agents_payload=raw))
    assert row[26] == raw


# --- build_summary_row (§7.2) ---

def test_summary_row_empty_cells_not_zero() -> None:
    row = build_summary_row(
        {
            "day": datetime(2026, 8, 10).date(),
            "decisions_total": 100,
            "buy": 10,
            "sell": 0,
            "wait": 90,
            "candidates": 5,
            "notified": 3,
            "closed_4h": 8,
            "sr_buy": 0.5,
            "sr_sell": None,     # sell-сигналов не было → пусто, не 0
            "avg_pnl_buy": 0.42,
            "avg_pnl_sell": None,
            "avg_dd": 0.3,
            "avg_prob": 0.6543,
            "logic_version_dominant": 2,
            "degraded_count": 7,
        }
    )
    assert row[0] == "2026-08-10"
    assert row[8] == 0.5      # success_rate_buy_4h
    assert row[9] == ""       # success_rate_sell_4h пусто (не было sell)
    assert row[14] == 2       # logic_version_dominant
    assert row[15] == 7       # degraded_count (последняя колонка, Этап 7.2)
    assert len(row) == 16
    assert row[11] == ""      # avg_pnl_sell_4h пусто
    assert row[13] == 0.6543  # avg_probability
    assert row[14] == 2       # logic_version_dominant (последняя колонка)


# --- participating_agents / build_notion_properties (§9.3) ---

def test_participating_agents_labels() -> None:
    assert participating_agents(_PAYLOAD_FULL) == ["Market", "Liquidity", "Futures"]
    assert participating_agents(_PAYLOAD_NO_FUTURES) == ["Market", "Liquidity"]


def test_notion_properties_full_mapping() -> None:
    props = build_notion_properties(_signal(), "db-123")
    assert props["Сигнал"]["title"][0]["text"]["content"] == (
        "BTC · Покупать · 2026-08-10 13:47 UTC"
    )
    assert props["Дата"]["date"]["start"] == "2026-08-10T13:47:00+00:00"
    assert props["Токен"]["select"]["name"] == "BTC"
    assert props["Решение"]["select"]["name"] == "Покупать"
    assert props["Вероятность %"]["number"] == 81.2   # 0.8123*100, 1 знак
    assert props["Прибыль %"]["number"] == 1.0
    assert props["Просадка %"]["number"] == 0.3
    assert props["Успешность"]["select"]["name"] == "Успех"
    assert props["Статус"]["status"]["name"] == "Done"
    assert [o["name"] for o in props["Источник агента"]["multi_select"]] == [
        "Market",
        "Liquidity",
        "Futures",
    ]
    assert props["Комментарий"]["rich_text"][0]["text"]["content"].startswith("market")


def test_notion_properties_sell_and_failure() -> None:
    props = build_notion_properties(_signal(decision="sell", succ_4h=False), "db")
    assert props["Решение"]["select"]["name"] == "Продавать"
    assert props["Успешность"]["select"]["name"] == "Неудача"
    assert props["Сигнал"]["title"][0]["text"]["content"].startswith("BTC · Продавать")


def test_notion_comment_truncated_to_1800() -> None:
    props = build_notion_properties(_signal(rationale="y" * 5000), "db")
    assert len(props["Комментарий"]["rich_text"][0]["text"]["content"]) == 1800


def test_notion_properties_omit_missing_eval() -> None:
    # Нет оценки 4h → свойства прибыли/просадки/успешности отсутствуют.
    props = build_notion_properties(
        _signal(pnl_4h=None, dd_4h=None, succ_4h=None), "db"
    )
    assert "Прибыль %" not in props
    assert "Просадка %" not in props
    assert "Успешность" not in props
