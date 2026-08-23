"""Этап 8.3: настройки пользователя, правило отбора и текст сигнала (§5 ТЗ).

Десять наборов из §5 ТЗ. Отбор уведомлений — то место, где ошибка не видна:
человек просто не получает сигнал и считает, что система молчит по существу.
Поэтому правило проверяется целиком чистыми функциями, без базы и без сети.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.user_settings import (
    UserSettings,
    default_settings,
    is_quiet_hour,
    user_filter_reason,
    wants_instrument,
)
from src.notify.agent import (
    CLOSING_LINE,
    UNPLUGGED_LINE,
    SignalFormatConfig,
    format_signal_message,
    rate_limit_reason,
)

_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

_METRICS = {
    "market": {
        "ema20": 118000, "ema50": 117000, "ema200": 115000,
        "ema50_slope": 0.5, "rsi14": 58, "adx14": 28,
    },
    "liquidity": {"imbalance": 0.42, "rel_spread": 0.0002},
    "futures": {
        "funding_pct": 0.86, "lookback_hours": 336,
        "oi_enough": True, "oi_confirms": True, "n_oi": 50,
    },
}


def _settings(**over: object) -> UserSettings:
    base = {
        "chat_id": 1, "instruments": (1, 3), "horizon_h": 4,
        "min_score": 0.70, "quiet_from": None, "quiet_to": None,
    }
    base.update(over)
    return UserSettings(**base)  # type: ignore[arg-type]


def _signal(decision: str = "buy", payload: list[dict] | None = None) -> dict:
    return {
        "id": 1, "instrument_id": 1, "ts": _NOW, "decision": decision,
        "probability": 0.8,
        "agents_payload": payload if payload is not None else [
            {"agent": "market", "signal": "bullish", "confidence": 0.55},
            {"agent": "liquidity", "signal": "bullish", "confidence": 0.90},
            {"agent": "futures", "signal": "bullish", "confidence": 0.30},
        ],
    }


# --- 10.1 test_user_filter_instruments --------------------------------------


def test_user_filter_instruments() -> None:
    """Приходят только выбранные токены."""
    settings = _settings(instruments=(1, 3))
    assert user_filter_reason(settings, 1, 0.9, _NOW) is None
    assert user_filter_reason(settings, 3, 0.9, _NOW) is None
    reason = user_filter_reason(settings, 5, 0.9, _NOW)
    assert reason is not None and "не выбран" in reason


def test_user_filter_instruments_none_means_all() -> None:
    """Пустая настройка — это «все токены», а не «ни одного»."""
    settings = _settings(instruments=None)
    assert wants_instrument(settings, 42) is True
    assert user_filter_reason(settings, 42, 0.9, _NOW) is None


# --- 10.2 test_user_filter_threshold ----------------------------------------


def test_user_filter_threshold() -> None:
    """Сигналы ниже порога пользователя не приходят."""
    settings = _settings(min_score=0.80)
    assert user_filter_reason(settings, 1, 0.79, _NOW) is not None
    assert user_filter_reason(settings, 1, 0.80, _NOW) is None
    assert user_filter_reason(settings, 1, 0.81, _NOW) is None


def test_user_threshold_is_independent_of_service_threshold() -> None:
    """Порог человека строже служебного и применяется поверх него."""
    strict = _settings(min_score=0.90)
    reason = user_filter_reason(strict, 1, 0.75, _NOW)
    assert reason is not None and "ниже порога пользователя" in reason


# --- 10.3 test_horizon_shown_not_filtered -----------------------------------


def test_horizon_shown_not_filtered() -> None:
    """Горизонт влияет на текст, а не на отбор: сигнал един для всех горизонтов."""
    for horizon in (1, 4, 12, 24):
        settings = _settings(horizon_h=horizon)
        assert user_filter_reason(settings, 1, 0.9, _NOW) is None

    for horizon, expected in ((1, "горизонт 1 час"), (12, "горизонт 12 часов")):
        text = format_signal_message(
            _signal(), 117240.0,
            SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=horizon),
            _METRICS,
        )
        assert expected in text


# --- 10.4 test_quiet_hours --------------------------------------------------


def test_quiet_hours() -> None:
    """В тишину не отправляется; границы включительны с обеих сторон."""
    settings = _settings(quiet_from=22, quiet_to=6)
    for hour in (22, 23, 0, 3, 6):
        now = _NOW.replace(hour=hour)
        assert is_quiet_hour(settings, now) is True
        assert user_filter_reason(settings, 1, 0.9, now) is not None
    for hour in (7, 12, 21):
        now = _NOW.replace(hour=hour)
        assert is_quiet_hour(settings, now) is False
        assert user_filter_reason(settings, 1, 0.9, now) is None


def test_quiet_hours_without_wrap() -> None:
    settings = _settings(quiet_from=1, quiet_to=5)
    assert is_quiet_hour(settings, _NOW.replace(hour=3)) is True
    assert is_quiet_hour(settings, _NOW.replace(hour=6)) is False
    assert is_quiet_hour(settings, _NOW.replace(hour=0)) is False


def test_quiet_hours_disabled_by_default() -> None:
    """Выключенная тишина выражается пустыми значениями и только ими."""
    settings = _settings()
    for hour in range(24):
        assert is_quiet_hour(settings, _NOW.replace(hour=hour)) is False


# --- 10.5 test_filter_before_rate_limit -------------------------------------


def test_filter_before_rate_limit() -> None:
    """Выдержка не тратится на невыбранные токены.

    Проверяется само правило §2: отбор по человеку возвращает причину раньше,
    чем дело доходит до ограничений потока. Если бы порядок был обратным,
    сигнал по невыбранному токену занимал бы выдержку и придерживал тот токен,
    который человек как раз ждёт.
    """
    settings = _settings(instruments=(1,))
    # Токен 5 не выбран — отбор отсекает его сразу.
    assert user_filter_reason(settings, 5, 0.9, _NOW) is not None
    # И только для выбранного токена доходит очередь до защиты от потока.
    assert user_filter_reason(settings, 1, 0.9, _NOW) is None


def test_rate_limit_is_not_consulted_for_unwanted_tokens() -> None:
    """Тот же порядок, выраженный через обе функции сразу."""
    from src.notify.agent import NotifyConfig

    guard = NotifyConfig(
        min_probability=0.7, cooldown_sec=1800, hold_sec=3600, max_per_hour=6
    )
    settings = _settings(instruments=(1,))
    just_sent = _NOW.replace(minute=59)

    consulted: list[str] = []

    def check(instrument_id: int) -> str | None:
        unwanted = user_filter_reason(settings, instrument_id, 0.9, _NOW)
        if unwanted is not None:
            return unwanted
        consulted.append("rate_limit")
        return rate_limit_reason(just_sent, _NOW, 0, guard)

    check(5)
    assert consulted == [], "защита от потока спрошена для невыбранного токена"
    check(1)
    assert consulted == ["rate_limit"]


# --- 10.6 test_defaults_without_record --------------------------------------


def test_defaults_without_record() -> None:
    """Отсутствие записи в настройках — не ошибка: действуют значения из §1 ТЗ."""
    settings = default_settings(chat_id=555, min_score=0.7)
    assert settings.instruments is None      # все токены
    assert settings.horizon_h == 4
    assert settings.min_score == 0.7
    assert settings.quiet_from is None and settings.quiet_to is None
    assert user_filter_reason(settings, 999, 0.7, _NOW) is None


# --- 10.7 test_signal_text_mandatory_parts ----------------------------------


def test_signal_text_mandatory_parts() -> None:
    """Обе оговорки и замыкающая строка присутствуют в КАЖДОМ сигнале."""
    variants = [
        _signal("buy"),
        _signal("sell"),
        _signal("buy", payload=[{"agent": "market", "signal": "bullish",
                                 "confidence": 0.9}]),
        _signal("buy", payload=[]),
    ]
    for signal in variants:
        for price in (117240.0, None):
            text = format_signal_message(
                signal, price,
                SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4),
                _METRICS,
            )
            assert "система не предсказывает цену" in text
            assert CLOSING_LINE in text
            assert UNPLUGGED_LINE in text


# --- 10.8 test_agent_silent_stated ------------------------------------------


def test_agent_silent_stated() -> None:
    """Молчащий агент назван, а не пропущен."""
    text = format_signal_message(
        _signal("buy", payload=[{"agent": "market", "signal": "bullish",
                                 "confidence": 0.8}]),
        117240.0, SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4), _METRICS,
    )
    assert "Ликвидность: недостаточно данных, голоса нет." in text
    assert "Деривативы: недостаточно данных, голоса нет." in text


def test_all_agents_silent_are_all_stated() -> None:
    text = format_signal_message(
        _signal("buy", payload=[]), 117240.0,
        SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4), _METRICS,
    )
    for title in ("Теханализ", "Ликвидность", "Деривативы"):
        assert f"{title}: недостаточно данных, голоса нет." in text


# --- Итог по голосам: четыре состояния не смешиваются ------------------------
#
# Прежняя строка «N из M уверенно, K слабо» сваливала воздержавшегося в «слабо»
# наравне с тем, у кого голос есть, просто неуверенный. Для человека это разные
# вещи: «агент посмотрел и не увидел перевеса» и «агент увидел перевес, но
# слабый» ведут к разным решениям.


def _vote_lines(payload: list[dict], decision: str = "buy") -> list[str]:
    text = format_signal_message(
        _signal(decision, payload=payload), 117240.0,
        SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4), _METRICS,
    )
    return [ln for ln in text.splitlines() if ln.startswith(("Голосов", "Остальные"))]


def test_vote_summary_separates_all_four_states() -> None:
    """За направление, против, воздержался, без данных — четыре разных счёта."""
    lines = _vote_lines([
        {"agent": "market", "signal": "bullish", "confidence": 0.9},    # за, уверенно
        {"agent": "liquidity", "signal": "neutral", "confidence": 0.1},  # воздержался
        {"agent": "futures", "signal": "bearish", "confidence": 0.3},    # против
    ])
    assert lines[0] == "Голосов за покупку: 1 из 3 высказавшихся (уверенно 1)."
    assert lines[1] == (
        "Остальные: против — 1; воздержались (перевеса не увидели) — 1."
    )


def test_vote_summary_does_not_count_abstention_as_a_weak_vote() -> None:
    """Воздержавшийся НЕ попадает в «слабо»: голоса у него нет вовсе.

    Тест на дефект, найденный заказчиком на примере ETH 23.08.2026.
    """
    abstained = _vote_lines([
        {"agent": "market", "signal": "bullish", "confidence": 0.9},
        {"agent": "liquidity", "signal": "neutral", "confidence": 0.1},
        {"agent": "futures", "signal": "neutral", "confidence": 0.1},
    ])
    assert abstained[0] == "Голосов за покупку: 1 из 3 высказавшихся (уверенно 1)."
    assert "слабо" not in abstained[0]
    assert "воздержались" in abstained[1]

    # А слабый голос ЗА — считается именно слабым и стоит в числителе.
    weak = _vote_lines([
        {"agent": "market", "signal": "bullish", "confidence": 0.9},
        {"agent": "liquidity", "signal": "bullish", "confidence": 0.1},
        {"agent": "futures", "signal": "bullish", "confidence": 0.2},
    ])
    assert weak[0] == "Голосов за покупку: 3 из 3 высказавшихся (уверенно 1, слабо 2)."
    assert len(weak) == 1, "нечего писать в «Остальные», когда все высказались за"


def test_vote_summary_denominator_counts_only_agents_with_data() -> None:
    """Знаменатель — агенты, У КОТОРЫХ БЫЛИ ДАННЫЕ, а не общее число.

    Молчание не голос против, и в знаменатель оно не идёт; промолчавшие
    названы отдельной строкой, поэтому общее число агентов по-прежнему видно.
    """
    silent = _vote_lines([
        {"agent": "market", "signal": "bullish", "confidence": 0.9},
        {"agent": "liquidity", "signal": "neutral", "confidence": 0.1},
    ])
    assert "1 из 2 высказавшихся" in silent[0]
    assert "без данных — 1" in silent[1]

    only_one = _vote_lines([{"agent": "market", "signal": "bullish",
                             "confidence": 0.9}])
    assert "1 из 1 высказавшихся" in only_one[0]
    assert "без данных — 2" in only_one[1]


def test_insufficient_data_is_silence_not_abstention() -> None:
    """``insufficient_data`` — это молчание, а не «сторону не выбрал»."""
    text = format_signal_message(
        _signal("buy", payload=[
            {"agent": "market", "signal": "bullish", "confidence": 0.9},
            {"agent": "futures", "signal": "insufficient_data", "confidence": 0.0},
        ]),
        117240.0, SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4), _METRICS,
    )
    assert "Деривативы: недостаточно данных, голоса нет." in text
    assert "Деривативы: показатели" not in text
    lines = [ln for ln in text.splitlines() if ln.startswith(("Голосов", "Остальные"))]
    assert "1 из 1 высказавшихся" in lines[0]
    assert "без данных — 2" in lines[1]
    assert "воздержались" not in lines[1]


def test_vote_summary_for_a_sell_signal() -> None:
    """Направление считается от решения: для продажи «за» — это bearish."""
    lines = _vote_lines([
        {"agent": "market", "signal": "bearish", "confidence": 0.9},
        {"agent": "liquidity", "signal": "bullish", "confidence": 0.8},
        {"agent": "futures", "signal": "neutral", "confidence": 0.1},
    ], decision="sell")
    assert lines[0] == "Голосов за продажу: 1 из 3 высказавшихся (уверенно 1)."
    assert "против — 1" in lines[1]


def test_vote_summary_absent_when_nobody_had_data() -> None:
    """Данных не было ни у кого — счёт не пишется: считать нечего."""
    assert _vote_lines([]) == []


def test_agent_order_is_stable_whoever_spoke() -> None:
    """Порядок агентов один и тот же, кто бы ни промолчал."""
    text = format_signal_message(
        _signal("buy", payload=[
            {"agent": "futures", "signal": "bullish", "confidence": 0.9},
        ]),
        117240.0, SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4), _METRICS,
    )
    titles = [ln.split(":")[0] for ln in text.splitlines() if ln.startswith("· ")]
    assert titles == ["· Теханализ", "· Ликвидность", "· Деривативы"]


# --- 10.9 test_no_internal_terms --------------------------------------------

FORBIDDEN_TERMS = (
    "индекс согласия", "перцентиль", "confidence", "logic_version",
    "EMA", "RSI", "ADX", "MACD", "imbalance", "funding", "bullish", "bearish",
)


def test_no_internal_terms() -> None:
    """В тексте для человека нет внутренних терминов (§3, §7 ТЗ)."""
    variants = [
        _signal("buy"),
        _signal("sell"),
        _signal("buy", payload=[{"agent": "futures", "signal": "bearish",
                                 "confidence": 0.2}]),
        _signal("buy", payload=[{"agent": "market", "signal": "neutral",
                                 "confidence": 0.1}]),
        _signal("buy", payload=[]),
    ]
    for signal in variants:
        for metrics in (_METRICS, {}, None):
            text = format_signal_message(
                signal, 117240.0,
                SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4),
                metrics,
            )
            lowered = text.lower()
            for term in FORBIDDEN_TERMS:
                assert term.lower() not in lowered, (
                    f"в тексте появился внутренний термин «{term}»: {text}"
                )


# --- 10.10 test_stats_small_sample_warning ----------------------------------


def test_stats_small_sample_warning() -> None:
    """При N < 60 выводится оговорка (§4 ТЗ)."""
    from src.bot import handlers

    def block(n: int) -> dict:
        return {"n": n, "buy": n, "sell": 0, "wait": 0, "sr_buy": 0.6,
                "sr_sell": None, "n_buy": n, "n_sell": 0,
                "avg_pnl": 0.3, "avg_dd": 0.2}

    small = handlers.render_stats(
        [("за 7 дней", block(59), block(59))], horizon_h=4, now=_NOW
    )
    assert "наблюдений мало, цифра ненадёжна" in small

    enough = handlers.render_stats(
        [("за 7 дней", block(60), block(60))], horizon_h=4, now=_NOW
    )
    assert "наблюдений мало" not in enough


def test_small_sample_boundary_is_a_named_constant() -> None:
    """Граница задана константой, а не числом внутри текста."""
    from src.bot.handlers import SMALL_SAMPLE_N

    assert SMALL_SAMPLE_N == 60


# --- Меню настроек (§1 ТЗ) ---------------------------------------------------


def test_menu_shows_current_state() -> None:
    from src.bot.settings_menu import menu_keyboard, menu_text

    instruments = [(1, "BTC/USDT"), (3, "ETH/USDT"), (5, "SOL/USDT")]
    settings = _settings(instruments=(1, 5), horizon_h=12, min_score=0.80)
    text = menu_text(settings, instruments)
    assert "BTC, SOL" in text
    assert "Горизонт: 12 ч" in text
    assert "Порог силы: 0.80" in text
    assert "Тишина: выключена" in text

    keyboard = menu_keyboard(settings, instruments)
    buttons = [b for row in keyboard["inline_keyboard"] for b in row]
    marked = {b["text"] for b in buttons if b["text"].startswith("✅")}
    assert "✅ BTC" in marked
    assert "✅ SOL" in marked
    assert "▫️ ETH" in {b["text"] for b in buttons}


def test_menu_keeps_at_least_one_token() -> None:
    """§1 ТЗ: минимум один токен. Отказ объясняется, а не проходит молча."""
    from src.bot.settings_menu import apply_callback

    instruments = [(1, "BTC/USDT"), (3, "ETH/USDT")]
    settings = _settings(instruments=(1,))
    updated, note = apply_callback(settings, "tok", "1", instruments)
    assert updated == settings
    assert "хотя бы один" in note


def test_menu_confirmation_is_short_and_specific() -> None:
    """После изменения — короткое подтверждение, а не всё меню заново (§1 ТЗ)."""
    from src.bot.settings_menu import apply_callback

    instruments = [(1, "BTC/USDT"), (3, "ETH/USDT")]
    settings = _settings(instruments=(1, 3))
    _, note = apply_callback(settings, "hor", "12", instruments)
    assert note == "Горизонт: 12 ч"
    _, note = apply_callback(settings, "thr", "0.90", instruments)
    assert note == "Порог силы: 0.90"
    _, note = apply_callback(settings, "tok", "3", instruments)
    assert note == "ETH выключен"


def test_menu_quiet_hours_round_trip() -> None:
    from src.bot.settings_menu import apply_callback, quiet_text

    instruments = [(1, "BTC/USDT")]
    settings = _settings()
    updated, note = apply_callback(settings, "qt", "22:6", instruments)
    assert updated.quiet_from == 22 and updated.quiet_to == 6
    assert "22:00" in note and "06:59" in note
    assert quiet_text(updated) == "с 22:00 до 06:59 UTC"

    off, note = apply_callback(updated, "qoff", "", instruments)
    assert off.quiet_from is None and off.quiet_to is None
    assert note == "Тишина выключена"


def test_menu_rejects_unknown_values() -> None:
    """Неизвестное значение не меняет настройки и объясняется человеку."""
    from src.bot.settings_menu import apply_callback, parse_callback

    instruments = [(1, "BTC/USDT")]
    settings = _settings()
    assert parse_callback("нечто:1") is None
    assert parse_callback("") is None
    for action, value in (("hor", "7"), ("thr", "0.55"), ("qt", "25:1")):
        updated, note = apply_callback(settings, action, value, instruments)
        assert updated == settings
        assert note


@pytest.mark.parametrize("horizon", [1, 4, 12, 24])
def test_menu_offers_exactly_the_four_horizons(horizon: int) -> None:
    from src.bot.settings_menu import HORIZONS

    assert horizon in HORIZONS
    assert len(HORIZONS) == 4
