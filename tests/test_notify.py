"""Тесты логики уведомлений: should_notify и форматирование сообщения."""

from datetime import UTC, datetime, timedelta

from src.notify.agent import (
    NotifyConfig,
    SignalFormatConfig,
    format_digest_message,
    rate_limit_reason,
    should_notify,
)

_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
_CFG = NotifyConfig(min_probability=0.7, cooldown_sec=1800)


def _sig(decision: str, probability: float) -> dict:
    return {
        "id": 1,
        "instrument_id": 1,
        "ts": _NOW,
        "decision": decision,
        "probability": probability,
        "rationale": "тест",
    }


def test_wait_is_not_notified() -> None:
    assert should_notify(_sig("wait", 0.9), None, None, _NOW, _CFG) is False


def test_low_probability_is_not_notified() -> None:
    assert should_notify(_sig("buy", 0.5), None, None, _NOW, _CFG) is False


def test_first_strong_signal_is_notified() -> None:
    assert should_notify(_sig("buy", 0.8), None, None, _NOW, _CFG) is True


def test_repeat_same_decision_within_cooldown_is_not_notified() -> None:
    last_sent = _NOW - timedelta(seconds=600)  # 10 мин < 30 мин cooldown
    assert should_notify(_sig("buy", 0.8), "buy", last_sent, _NOW, _CFG) is False


def test_same_decision_after_cooldown_is_notified() -> None:
    last_sent = _NOW - timedelta(seconds=2000)  # > 1800 cooldown
    assert should_notify(_sig("buy", 0.8), "buy", last_sent, _NOW, _CFG) is True


def test_decision_change_is_notified_even_within_cooldown() -> None:
    last_sent = _NOW - timedelta(seconds=60)  # только что отправляли buy
    assert should_notify(_sig("sell", 0.8), "buy", last_sent, _NOW, _CFG) is True


def test_probability_at_threshold_is_notified() -> None:
    assert should_notify(_sig("buy", 0.7), None, None, _NOW, _CFG) is True


_FMT = SignalFormatConfig(symbol="BTC/USDT", tz_name="Europe/Moscow", primary_horizon="4h")


def _payload(*agents: tuple[str, str, float]) -> list[dict]:
    """Собирает agents_payload из троек (agent, signal, confidence)."""
    return [
        {"agent": a, "signal": s, "confidence": c, "ts": _NOW.isoformat()}
        for a, s, c in agents
    ]


def _sig_full(decision: str, probability: float, payload: list[dict]) -> dict:
    sig = _sig(decision, probability)
    sig["agents_payload"] = payload
    return sig


# Текст сигнала переписан §3 ТЗ 8.3: развёрнутое объяснение человеческим
# языком вместо прежней сводки с индексом согласия. Тесты ниже проверяют НОВЫЙ
# текст — прежние проверки описывали то, что ТЗ прямо заменяет.

_METRICS = {
    "market": {
        "ema20": 118000, "ema50": 117000, "ema200": 115000,
        "ema50_slope": 0.5, "rsi14": 58, "adx14": 28,
    },
    "liquidity": {
        "imbalance": 0.42, "rel_spread": 0.0002,
        "bid_wall_ratio": 0.1, "ask_wall_ratio": 0.1,
    },
    "futures": {
        "funding_pct": 0.86, "lookback_hours": 336,
        "oi_enough": True, "oi_confirms": True, "n_oi": 50,
    },
}


def test_format_message_header_names_token_action_and_horizon() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(
        ("market", "bullish", 0.55),
        ("liquidity", "bullish", 0.90),
        ("futures", "bullish", 0.30),
    )
    text = format_signal_message(
        _sig_full("buy", 0.78, payload), 117240.0,
        SignalFormatConfig("BTC/USDT", "Europe/Moscow", "4h", horizon_h=4),
        _METRICS,
    )
    assert "BTC · ПОКУПКА · горизонт 4 часа" in text
    assert "Цена сейчас: 117 240 USDT" in text
    assert "Почему такой вывод:" in text
    assert "Согласие агентов: 2 из 3 уверенно, 1 слабо." in text


def test_format_message_explains_each_agent_from_its_metrics() -> None:
    """Объяснение каждого агента строится из ЕГО метрик, а не из общих слов."""
    from src.notify.agent import format_signal_message

    payload = _payload(
        ("market", "bullish", 0.55),
        ("liquidity", "bullish", 0.90),
        ("futures", "bullish", 0.30),
    )
    text = format_signal_message(
        _sig_full("buy", 0.78, payload), 117240.0, _FMT, _METRICS
    )
    assert "Теханализ: средние цены" in text
    assert "заявок на покупку заметно больше" in text
    assert "плата за удержание позиции у верхней границы" in text
    assert "Голос за покупку, уверенность высокая." in text
    assert "Голос за покупку, уверенность низкая." in text


def test_format_message_without_metrics_says_so() -> None:
    """Метрик нет — так и сказано, а не заменено правдоподобной фразой."""
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bullish", 0.7))
    text = format_signal_message(_sig_full("buy", 0.8, payload), 64000.0, _FMT, {})
    assert "показатели за этот момент не сохранились" in text


def test_format_message_missing_agent_is_explicit() -> None:
    from src.notify.agent import format_signal_message

    # Только два агента из трёх — отсутствующий должен быть виден ЯВНО.
    payload = _payload(("market", "bullish", 0.70), ("liquidity", "neutral", 0.05))
    text = format_signal_message(
        _sig_full("buy", 0.6, payload), 64000.0, _FMT, _METRICS
    )
    assert "Деривативы: недостаточно данных, голоса нет." in text


def test_format_message_no_price_line_skipped() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bullish", 0.70), ("futures", "bullish", 0.60))
    text = format_signal_message(_sig_full("buy", 0.6, payload), None, _FMT, _METRICS)
    assert "Цена сейчас" not in text
    # Сообщение всё равно формируется целиком.
    assert "BTC · ПОКУПКА" in text
    assert "система не торгует сама" in text


def test_format_message_sell() -> None:
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bearish", 0.80), ("futures", "bearish", 0.70))
    text = format_signal_message(
        _sig_full("sell", 0.9, payload), 64000.0, _FMT, _METRICS
    )
    assert "BTC · ПРОДАЖА" in text
    assert "Голос за продажу" in text


def test_format_message_neutral_agent_is_not_counted_as_a_voice() -> None:
    """Нейтральное мнение — не голос: сторона не выбрана, и так и написано."""
    from src.notify.agent import format_signal_message

    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "neutral", 0.1),
        ("futures", "bullish", 0.5),
    )
    text = format_signal_message(
        _sig_full("buy", 0.8, payload), 64000.0, _FMT, _METRICS
    )
    assert "Ясной стороны не выбрал." in text
    # Нейтральный агент высказался, поэтому он в знаменателе согласия.
    assert "Согласие агентов: 2 из 3 уверенно, 1 слабо." in text


def test_format_message_target_block_has_a_place_reserved() -> None:
    """Место под блок цели Этапа 8.2 есть и встаёт сразу после цены (§3 ТЗ).

    Проверяется именно то, ради чего блок предусмотрен: Этап 8.2 передаёт
    строки — и они появляются на своём месте, а сборка текста не меняется.
    """
    from src.notify.agent import format_signal_message

    payload = _payload(("market", "bullish", 0.7))
    without = format_signal_message(
        _sig_full("buy", 0.8, payload), 64000.0, _FMT, _METRICS
    )
    with_target = format_signal_message(
        _sig_full("buy", 0.8, payload), 64000.0, _FMT, _METRICS,
        target_block=["Цель: 65 000", "Комиссия: 0.1%"],
    )
    assert "Цель:" not in without
    assert "Комиссия:" not in without
    lines = [ln for ln in with_target.splitlines() if ln.strip()]
    assert lines.index("Цель: 65 000") > lines.index("Цена сейчас: 64 000 USDT")
    assert lines.index("Цель: 65 000") < lines.index("Почему такой вывод:")


# --- Задача A2 (Этап 7.2): подсчёт содержательных агентов для порога отправки ---

def test_count_meaningful_agents_counts_directional_and_neutral() -> None:
    from src.notify.agent import count_meaningful_agents

    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "neutral", 0.1),
        ("futures", "bullish", 0.5),
    )
    assert count_meaningful_agents(payload) == 3


def test_count_meaningful_agents_excludes_insufficient() -> None:
    from src.notify.agent import count_meaningful_agents

    # insufficient_data содержательным НЕ считается (ТЗ A2).
    payload = _payload(
        ("market", "bullish", 0.7),
        ("liquidity", "insufficient_data", 0.0),
    )
    assert count_meaningful_agents(payload) == 1


def test_count_meaningful_agents_empty() -> None:
    from src.notify.agent import count_meaningful_agents

    assert count_meaningful_agents([]) == 0
    assert count_meaningful_agents(None) == 0


# --- Задача B1 (Этап 7.2): согласованность в тексте — знаменатель = 3 агента ---

def test_compute_agreement_uses_total_agents_denominator() -> None:
    from src.notify.agent import compute_agreement

    # Два агента из трёх, оба bullish. Было |2-0|/2 = 1.0; стало |2-0|/3 ≈ 0.667 —
    # выпадение агента понижает согласованность (та же формула, что у Decision).
    payload = _payload(("market", "bullish", 0.7), ("futures", "bullish", 0.6))
    assert abs(compute_agreement(payload) - 2 / 3) < 1e-9


# --- Этап 7.3, Блок B: калиброванный режим отбора и текст сообщения ---------

_CFG_CALIBRATED = NotifyConfig(
    min_probability=0.7,
    cooldown_sec=1800,
    use_calibrated=True,
    min_calibrated=0.55,
)


def _sig_calibrated(probability: float, calibrated: float | None) -> dict:
    signal = _sig("buy", probability)
    signal["calibrated_probability"] = calibrated
    return signal


def test_calibrated_mode_sends_nothing_without_curve() -> None:
    """NOTIFY_USE_CALIBRATED=true и нет кривой → уведомления не уходят.

    Индекс согласия при этом высокий: в прежнем режиме сигнал бы ушёл. Это и
    есть требование ТЗ — вероятность не выдумывается из индекса.
    """
    signal = _sig_calibrated(probability=0.95, calibrated=None)
    assert should_notify(signal, None, None, _NOW, _CFG_CALIBRATED) is False


def test_calibrated_mode_uses_its_own_threshold() -> None:
    low = _sig_calibrated(probability=0.95, calibrated=0.40)
    high = _sig_calibrated(probability=0.20, calibrated=0.60)
    assert should_notify(low, None, None, _NOW, _CFG_CALIBRATED) is False
    assert should_notify(high, None, None, _NOW, _CFG_CALIBRATED) is True


def test_default_mode_ignores_calibrated_value() -> None:
    """По умолчанию поведение уведомлений не изменилось: отбор по индексу согласия."""
    signal = _sig_calibrated(probability=0.8, calibrated=0.01)
    assert should_notify(signal, None, None, _NOW, _CFG) is True


def test_message_gives_no_number_without_a_curve() -> None:
    """Без кривой система не называет НИКАКОГО числа как долю сбывшихся.

    Индекс согласия под видом вероятности не подставляется (Этап 7.3 §4.1) —
    и самого индекса в тексте больше нет: это внутренний термин (§7 ТЗ 8.3).
    """
    from src.notify.agent import format_signal_message

    signal = _sig("buy", 0.74)
    signal["agents_payload"] = [
        {"agent": "market", "signal": "bullish", "confidence": 0.8},
        {"agent": "liquidity", "signal": "bullish", "confidence": 0.6},
        {"agent": "futures", "signal": "neutral", "confidence": 0.4},
    ]
    text = format_signal_message(
        signal, price=60000.0,
        cfg=SignalFormatConfig("BTC/USDT", "Europe/Moscow", "4h"),
        metrics_by_agent=_METRICS,
    )
    assert "сбывались раньше" not in text
    assert "74%" not in text


def test_message_shows_history_share_only_with_curve() -> None:
    """С кривой появляется строка о доле сбывшихся, с размером выборки и датой."""
    from src.notify.agent import format_signal_message

    signal = _sig("buy", 0.74)
    signal["agents_payload"] = []
    signal["calibrated_probability"] = 0.31
    signal["calibration_built_at"] = datetime(2026, 8, 16, 5, 30, tzinfo=UTC)
    signal["calibration_sample_size"] = 87
    text = format_signal_message(
        signal, price=60000.0,
        cfg=SignalFormatConfig("BTC/USDT", "Europe/Moscow", "4h"),
    )
    assert "Такие сигналы сбывались раньше в 31% случаев" in text
    assert "по 87 наблюдениям с 16.08" in text


# --- Защита от потока уведомлений (§2 ТЗ 8.3) -------------------------------
#
# Пороги заданы вслепую и уточняются по измеренному потоку, поэтому тесты
# проверяют ПОВЕДЕНИЕ при заданных значениях, а не сами значения.

_GUARD = NotifyConfig(
    min_probability=0.7, cooldown_sec=1800, hold_sec=3600, max_per_hour=6
)


def test_no_limits_when_thresholds_are_zero() -> None:
    """Нули выключают защиту: поведение как до Этапа 8.3."""
    off = NotifyConfig(min_probability=0.7, cooldown_sec=1800)
    assert rate_limit_reason(_NOW - timedelta(seconds=1), _NOW, 1000, off) is None


def test_first_notification_passes_the_hold() -> None:
    """По инструменту ещё ничего не слали — выдержке не от чего отсчитывать."""
    assert rate_limit_reason(None, _NOW, 0, _GUARD) is None


def test_hold_blocks_within_the_window() -> None:
    last_sent = _NOW - timedelta(minutes=30)      # 30 мин < 60 мин выдержки
    reason = rate_limit_reason(last_sent, _NOW, 0, _GUARD)
    assert reason is not None and "выдержка" in reason


def test_hold_releases_after_the_window() -> None:
    last_sent = _NOW - timedelta(minutes=61)
    assert rate_limit_reason(last_sent, _NOW, 0, _GUARD) is None


def test_hold_ignores_the_decision() -> None:
    """Главное отличие выдержки от cooldown: смена решения её НЕ обходит.

    cooldown придерживает только повтор того же решения, поэтому пара,
    колеблющаяся buy → sell → buy, слала бы уведомления без пауз. Выдержка
    получает лишь время последней отправки — решение до неё не доходит по
    сигнатуре, и обойти её сменой решения нельзя.
    """
    last_sent = _NOW - timedelta(minutes=5)
    # То же решение и смена решения дают ОДИН И ТОТ ЖЕ результат.
    assert rate_limit_reason(last_sent, _NOW, 0, _GUARD) is not None
    assert should_notify(_sig("sell", 0.9), "buy", last_sent, _NOW, _GUARD) is True


def test_hourly_cap_blocks_at_the_limit() -> None:
    reason = rate_limit_reason(None, _NOW, 6, _GUARD)
    assert reason is not None and "потолок" in reason


def test_hourly_cap_allows_below_the_limit() -> None:
    assert rate_limit_reason(None, _NOW, 5, _GUARD) is None


def test_hold_is_reported_before_the_cap() -> None:
    """Причина в логе — та, что сработала первой по инструменту."""
    last_sent = _NOW - timedelta(minutes=1)
    reason = rate_limit_reason(last_sent, _NOW, 6, _GUARD)
    assert reason is not None and "выдержка" in reason


def test_thresholds_are_independent() -> None:
    """Каждый порог выключается отдельно: значения ещё будут уточняться."""
    only_cap = NotifyConfig(
        min_probability=0.7, cooldown_sec=1800, hold_sec=0, max_per_hour=6
    )
    only_hold = NotifyConfig(
        min_probability=0.7, cooldown_sec=1800, hold_sec=3600, max_per_hour=0
    )
    just_sent = _NOW - timedelta(seconds=1)
    assert rate_limit_reason(just_sent, _NOW, 0, only_cap) is None
    assert rate_limit_reason(just_sent, _NOW, 0, only_hold) is not None
    assert rate_limit_reason(None, _NOW, 99, only_cap) is not None
    assert rate_limit_reason(None, _NOW, 99, only_hold) is None


def test_negative_thresholds_are_rejected_by_config() -> None:
    """Отрицательный порог молча выключал бы защиту, выглядя заданным."""
    import pytest

    from src.core.config import Settings

    with pytest.raises(ValueError, match="отрицательным"):
        Settings(NOTIFY_HOLD_MIN=-1)
    with pytest.raises(ValueError, match="отрицательным"):
        Settings(NOTIFY_MAX_PER_HOUR=-1)


# --- Сводное сообщение при исчерпанном потолке (§2 ТЗ 8.3) ------------------


def _held(symbol: str, decision: str, strength: float) -> dict:
    return {"symbol": symbol, "decision": decision, "strength": strength}


def test_digest_orders_by_strength_descending() -> None:
    """Порядок задаётся силой, а не тем, как сигналы попали в очередь."""
    text = format_digest_message(
        [
            _held("ETH/USDT", "sell", 0.74),
            _held("BTC/USDT", "buy", 0.91),
            _held("SOL/USDT", "buy", 0.82),
        ],
        max_per_hour=6,
    )
    listed = [ln for ln in text.splitlines() if ln.startswith(("🟢", "🔴"))]
    assert [ln.split(" —")[0].split()[-1] for ln in listed] == [
        "BTC/USDT", "SOL/USDT", "ETH/USDT",
    ], text


def test_digest_names_the_number_held() -> None:
    text = format_digest_message(
        [_held("BTC/USDT", "buy", 0.9), _held("ETH/USDT", "sell", 0.8)], 6
    )
    assert "Придержано сигналов: 2" in text
    assert "Потолок 6 уведомлений в час исчерпан" in text


def test_digest_keeps_the_closing_line() -> None:
    """Замыкающая строка обязательна и в сводке: это тоже уведомление."""
    text = format_digest_message([_held("BTC/USDT", "buy", 0.9)], 6)
    assert text.rstrip().endswith("Решение за вами. Система не торгует сама.")


def test_digest_names_what_did_not_fit() -> None:
    """Остаток назван числом, а не отброшен молча."""
    entries = [_held(f"T{i}/USDT", "buy", 0.9 - i / 100) for i in range(25)]
    text = format_digest_message(entries, 6, max_listed=20)
    assert "Придержано сигналов: 25" in text
    assert "и ещё 5" in text
    assert len([ln for ln in text.splitlines() if ln.startswith(("🟢", "🔴"))]) == 20


def test_digest_says_nothing_extra_when_everything_fits() -> None:
    text = format_digest_message([_held("BTC/USDT", "buy", 0.9)], 6, max_listed=20)
    assert "и ещё" not in text


def test_digest_marks_direction() -> None:
    text = format_digest_message(
        [_held("BTC/USDT", "buy", 0.9), _held("ETH/USDT", "sell", 0.8)], 6
    )
    assert "🟢 BTC/USDT — ПОКУПАТЬ, 90%" in text
    assert "🔴 ETH/USDT — ПРОДАВАТЬ, 80%" in text


def test_cap_reason_is_recognisable_without_parsing_text() -> None:
    """Потолок отличается от выдержки сравнением, а не разбором сообщения.

    В сводку идёт придержанное ПОТОЛКОМ; придержанное выдержкой — нет.
    Различать их по подстроке в человеческом тексте нельзя: текст меняется.
    """
    from src.notify.agent import _CAP_REASON_PREFIX

    cap = rate_limit_reason(None, _NOW, 6, _GUARD)
    hold = rate_limit_reason(_NOW - timedelta(minutes=1), _NOW, 0, _GUARD)
    assert cap is not None and cap.startswith(_CAP_REASON_PREFIX)
    assert hold is not None and not hold.startswith(_CAP_REASON_PREFIX)
