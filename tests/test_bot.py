"""Тесты бота: разбор аргументов, белый список, rate limit, согласованность,
рендеры ответов. Все функции чистые (кроме rate limit — тестируется с фейковым
Redis), обращений к реальным БД/сети нет.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.bot import handlers
from src.bot.poller import check_rate_limit, is_allowed
from src.notify.agent import compute_agreement

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Разбор аргументов команд.
# --------------------------------------------------------------------------- #

def test_parse_command_basic() -> None:
    assert handlers.parse_command("/status") == ("status", [])
    assert handlers.parse_command("/last all 10") == ("last", ["all", "10"])
    assert handlers.parse_command("  /Signal 42 ") == ("signal", ["42"])


def test_parse_command_strips_bot_suffix() -> None:
    assert handlers.parse_command("/last@AgentTradeBot 5") == ("last", ["5"])


def test_parse_command_non_command() -> None:
    assert handlers.parse_command("привет") == (None, [])
    assert handlers.parse_command("") == (None, [])


def test_parse_last_args_defaults() -> None:
    assert handlers.parse_last_args([], max_rows=20) == (True, 5)


def test_parse_last_args_number() -> None:
    assert handlers.parse_last_args(["10"], max_rows=20) == (True, 10)


def test_parse_last_args_all() -> None:
    assert handlers.parse_last_args(["all"], max_rows=20) == (False, 5)
    assert handlers.parse_last_args(["all", "3"], max_rows=20) == (False, 3)


def test_parse_last_args_caps_at_max_rows() -> None:
    assert handlers.parse_last_args(["100"], max_rows=20) == (True, 20)


def test_parse_last_args_garbage_number() -> None:
    assert handlers.parse_last_args(["abc"], max_rows=20) == (True, 5)


def test_parse_signal_id() -> None:
    assert handlers.parse_signal_id(["1847"]) == 1847
    assert handlers.parse_signal_id([]) is None
    assert handlers.parse_signal_id(["nope"]) is None


def test_parse_stats_period() -> None:
    assert handlers.parse_stats_period([]) == "7d"
    assert handlers.parse_stats_period(["24h"]) == "24h"
    assert handlers.parse_stats_period(["all"]) == "all"
    assert handlers.parse_stats_period(["bogus"]) == "7d"


# --------------------------------------------------------------------------- #
# Белый список (§7.1).
# --------------------------------------------------------------------------- #

def test_whitelist_allows_listed_chat() -> None:
    assert is_allowed(1462906955, {"1462906955"}) is True
    # chat_id приходит числом, а список хранит строки — сверка по строке.
    assert is_allowed("1462906955", {"1462906955"}) is True


def test_whitelist_blocks_foreign_chat() -> None:
    assert is_allowed(999, {"1462906955"}) is False


# --------------------------------------------------------------------------- #
# Rate limit (§7.2) — с фейковым Redis (атомарный SET NX EX).
# --------------------------------------------------------------------------- #

class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):  # noqa: ANN001
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.mark.asyncio
async def test_rate_limit_first_allows_second_drops() -> None:
    redis = _FakeRedis()
    first = await check_rate_limit(redis, 123, rate_limit_sec=3)
    second = await check_rate_limit(redis, 123, rate_limit_sec=3)
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_rate_limit_is_per_chat() -> None:
    redis = _FakeRedis()
    assert await check_rate_limit(redis, 1, 3) is True
    # Другой чат не должен быть заблокирован из-за первого.
    assert await check_rate_limit(redis, 2, 3) is True


# --------------------------------------------------------------------------- #
# Расчёт согласованности (§6, §11) — из agents_payload, НЕ из rationale.
# --------------------------------------------------------------------------- #

def test_agreement_unanimous() -> None:
    payload = [
        {"agent": "market", "signal": "bullish", "confidence": 0.7},
        {"agent": "liquidity", "signal": "bullish", "confidence": 0.6},
        {"agent": "futures", "signal": "bullish", "confidence": 0.5},
    ]
    assert compute_agreement(payload) == pytest.approx(1.0)


def test_agreement_mixed() -> None:
    # 2 bullish + 1 neutral → |2-0|/3.
    payload = [
        {"agent": "market", "signal": "bullish", "confidence": 0.7},
        {"agent": "liquidity", "signal": "neutral", "confidence": 0.1},
        {"agent": "futures", "signal": "bullish", "confidence": 0.5},
    ]
    assert compute_agreement(payload) == pytest.approx(2 / 3)


def test_agreement_opposed() -> None:
    payload = [
        {"agent": "market", "signal": "bullish", "confidence": 0.7},
        {"agent": "futures", "signal": "bearish", "confidence": 0.6},
    ]
    assert compute_agreement(payload) == pytest.approx(0.0)


def test_agreement_from_json_string() -> None:
    # asyncpg отдаёт JSONB строкой — согласованность должна считаться и по строке.
    # Знаменатель = полное число агентов (Задача B1, Этап 7.2): один свежий агент
    # из трёх даёт |1-0|/3 ≈ 0.33, а не 1.0 (выпадение агентов понижает согласие).
    payload = '[{"agent":"market","signal":"bullish","confidence":0.7}]'
    assert compute_agreement(payload) == pytest.approx(1 / 3)


def test_agreement_empty_is_none() -> None:
    assert compute_agreement([]) is None
    assert compute_agreement(None) is None


# --------------------------------------------------------------------------- #
# Рендеры ответов.
# --------------------------------------------------------------------------- #

def test_help_contains_mandatory_phrase() -> None:
    text = handlers.render_help()
    assert "Система не торгует сама. Все решения принимаете вы" in text


def test_status_all_fresh() -> None:
    fresh = _NOW.isoformat()
    hb_rows = [("collector:heartbeat:ohlcv", fresh, 30), ("bot:heartbeat", fresh, 30)]
    facts = {
        "last_ohlcv_ts": _NOW,
        "last_orderbook_ts": _NOW,
        "last_signal_ts": _NOW,
        "open_count": 2,
        "closed_count": 100,
    }
    text = handlers.render_status(hb_rows, facts, _NOW)
    assert "Всё работает нормально" in text
    assert "🟢" in text


def test_status_reports_problem_when_stale() -> None:
    stale = (_NOW - timedelta(seconds=10_000)).isoformat()
    fresh = _NOW.isoformat()
    hb_rows = [("collector:heartbeat:ohlcv", stale, 30), ("bot:heartbeat", fresh, 30)]
    text = handlers.render_status(hb_rows, {}, _NOW)
    assert "Есть проблемы" in text
    assert "collector:heartbeat:ohlcv" in text


def test_status_negative_age_clamped_to_zero() -> None:
    future = (_NOW + timedelta(seconds=5)).isoformat()
    hb_rows = [("bot:heartbeat", future, 30)]
    text = handlers.render_status(hb_rows, {}, _NOW)
    assert "0 сек назад" in text


def test_last_notified_only_header() -> None:
    signals = [
        {
            "id": 10, "ts": _NOW, "decision": "buy", "probability": 0.8,
            "status": "closed", "pnl_pct": 1.2, "drawdown_pct": 0.3, "success": True,
        }
    ]
    text = handlers.render_last(signals, notified_only=True, now=_NOW)
    assert "отправленные" in text
    assert "#10" in text
    assert "угадал" in text


def test_signal_card_status_sent() -> None:
    card = _card(notified=True, notified_at=_NOW)
    text = handlers.render_signal_card(card, _NOW)
    assert "Уведомление: отправлен" in text


def test_signal_card_status_absorbed() -> None:
    card = _card(notified=True, notified_at=None)
    text = handlers.render_signal_card(card, _NOW)
    assert "поглощён анти-спамом" in text


def test_signal_card_status_not_sent() -> None:
    card = _card(notified=False, notified_at=None)
    text = handlers.render_signal_card(card, _NOW)
    assert "не отправлялся" in text


def test_signal_card_missing_agent_visible() -> None:
    card = _card(notified=True, notified_at=_NOW)
    # payload только market — liquidity и futures должны быть «нет данных».
    card["agents_payload"] = [{"agent": "market", "signal": "bullish", "confidence": 0.7}]
    text = handlers.render_signal_card(card, _NOW)
    assert "Ликвидность: нет данных, в решении не участвовал" in text
    assert "Деривативы: нет данных, в решении не участвовал" in text


def test_signal_card_not_found() -> None:
    text = handlers.render_signal_card(None, _NOW)
    assert "не найден" in text


def test_agents_stale_marked() -> None:
    old = _NOW - timedelta(seconds=1000)
    rows = {
        "market": {"agent": "market", "signal": "bullish", "confidence": 0.7, "ts": _NOW},
        "liquidity": {"agent": "liquidity", "signal": "neutral", "confidence": 0.1, "ts": old},
        "futures": None,
    }
    text = handlers.render_agents(rows, freshness_sec=300, now=_NOW)
    assert "устарел, в решении не участвует" in text
    assert "Работают 3 агента из 5. News и OnChain пока не реализованы." in text


# /stats переписан §4 ТЗ 8.3: попадания за 7 и 30 дней по ВЫБРАННОМУ горизонту,
# число наблюдений рядом с каждым процентом, оговорка при N < 60, только текущая
# версия логики с датой начала. Прежние проверки описывали разбивку «Блок 1…5»,
# которую ТЗ прямо заменяет.

def _stats_block(n: int, **over: object) -> dict:
    block = {
        "n": n, "buy": n // 2, "sell": n - n // 2, "wait": 0,
        "sr_buy": 0.66, "sr_sell": 0.5, "n_buy": n // 2, "n_sell": n - n // 2,
        "avg_pnl": 0.4, "avg_dd": 0.2,
    }
    block.update(over)
    return block


def test_stats_shows_both_periods_and_chosen_horizon() -> None:
    blocks = [
        ("за 7 дней", _stats_block(80), _stats_block(300)),
        ("за 30 дней", _stats_block(200), _stats_block(1200)),
    ]
    text = handlers.render_stats(blocks, horizon_h=12, now=_NOW)
    assert "горизонт 12 ч" in text
    assert "за 7 дней" in text
    assert "за 30 дней" in text
    assert "Честная выборка" in text
    assert "Все сигналы подряд" in text


def test_stats_puts_the_count_next_to_every_percent() -> None:
    """§4 ТЗ: процент без знаменателя одинаково читается при 3 и при 300."""
    blocks = [("за 7 дней", _stats_block(80, sr_buy=0.62, n_buy=84), _stats_block(90))]
    text = handlers.render_stats(blocks, horizon_h=4, now=_NOW)
    assert "buy 62% из 84" in text


def test_stats_small_sample_warning() -> None:
    """N < 60 → оговорка «наблюдений мало, цифра ненадёжна»."""
    blocks = [("за 7 дней", _stats_block(12), _stats_block(300))]
    text = handlers.render_stats(blocks, horizon_h=4, now=_NOW)
    assert "наблюдений мало, цифра ненадёжна" in text


def test_stats_no_warning_when_enough_sample() -> None:
    blocks = [("за 7 дней", _stats_block(80), _stats_block(300))]
    text = handlers.render_stats(blocks, horizon_h=4, now=_NOW)
    assert "наблюдений мало" not in text


def test_stats_names_the_version_and_when_it_started() -> None:
    """Версии логики не смешиваются: показана текущая и дата начала (§4, §7)."""
    blocks = [("за 7 дней", _stats_block(80), _stats_block(300))]
    text = handlers.render_stats(
        blocks, horizon_h=4, now=_NOW,
        logic_version=5,
        version_started_at=datetime(2026, 8, 22, 22, 59, tzinfo=UTC),
    )
    assert "Версия логики 5" in text
    assert "действует с 22.08.2026 22:59 UTC" in text
    assert "Прежние версии не учтены." in text


def test_stats_empty_sample_says_so() -> None:
    blocks = [("за 7 дней", _stats_block(0), _stats_block(0))]
    text = handlers.render_stats(blocks, horizon_h=4, now=_NOW)
    assert "закрытых сигналов пока нет" in text


def test_split_message_splits_long_text() -> None:
    text = "\n".join(f"строка {i}" for i in range(2000))
    parts = handlers.split_message(text, limit=4000)
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)


def _card(notified: bool, notified_at) -> dict:  # noqa: ANN001
    return {
        "id": 1847,
        "instrument_id": 1,
        "ts": _NOW,
        "decision": "buy",
        "probability": 0.78,
        "rationale": "market=bullish(0.70); балл=+0.42 → buy.",
        "notified": notified,
        "notified_at": notified_at,
        "status": "closed",
        "agents_payload": [
            {"agent": "market", "signal": "bullish", "confidence": 0.7},
            {"agent": "liquidity", "signal": "neutral", "confidence": 0.05},
            {"agent": "futures", "signal": "bullish", "confidence": 0.6},
        ],
        "price_at_signal": 64210.0,
        "eval_1h": {
            "price_at_close": 64300.0, "pnl_pct": 0.14, "drawdown_pct": 0.1, "success": True,
        },
        "eval_4h": {
            "price_at_close": 64800.0, "pnl_pct": 0.9, "drawdown_pct": 0.3, "success": True,
        },
    }
