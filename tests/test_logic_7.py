"""ВЕРСИЯ ЛОГИКИ 7: снятие слотов, антиспам по токену, уведомления по сделкам.

ЧТО ПРОВЕРЯЕТ ЭТОТ НАБОР И ЧЕГО НЕ ПРОВЕРЯЕТ. Здесь проверяется ВНЕДРЕНИЕ: что
боевой путь открытия позиции действительно перестал спрашивать про слоты и
занятость инструмента, что пауза по токену считается от ОТКРЫТИЯ и держит ровно
столько, сколько обещано, и что в Telegram больше не уходит ни одного сообщения
о сигнале. Здесь НЕ проверяется, много ли станет сделок: это предсказания §8 ТЗ,
и проверит их прогон, а не тест.

ГЛАВНЫЙ ТЕСТ ФАЙЛА — :func:`test_the_stand_reproduces_the_three_cases_of_the_spec`
(§9 ТЗ). Он воспроизводит на стенде три случая, названные критериями приёмки: две
одновременные позиции по одному токену открываются; кандидат в ту же минуту
отклоняется с причиной ``token_pause``; через 60 минут после открытия — входит.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ, а не выдумывают ответ. ``AT_TEST_DSN`` обязан указывать на
ОДНОРАЗОВУЮ базу: тесты пишут и удаляют строки.
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import src.notify.agent as notify_agent
import src.positions.rules as rules
import src.positions.runner as positions_runner
from src.bot import handlers
from src.core.config import Settings, settings
from src.notify.agent import (
    SIGNAL_NOTIFY_MAX_LOGIC_VERSION,
    signal_notifications_enabled,
)
from src.positions.rules import (
    REASON_NO_FREE_CAPITAL,
    REASON_OK,
    REASON_TOKEN_PAUSE,
    REFUSAL_REASONS,
    refusal_key,
    should_open,
    token_pause_left_sec,
)
from src.positions.runner import STARTUP_FIELDS, open_new_positions, startup_fields
from tests.conftest import CURRENT_LOGIC_VERSION

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "db" / "migrations"

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(ОДНОРАЗОВАЯ база, тест пишет и удаляет строки)"
    ),
)

# Час паузы в секундах — то же число, что стоит настройкой по умолчанию.
_HOUR = 3600.0


# =============================================================================
# §1, §2 ТЗ. Версия логики и состав работ
# =============================================================================

def test_the_logic_version_is_raised_to_seven() -> None:
    """§1 ТЗ: версия логики поднята 6 → 7 решением владельца.

    ПОЧЕМУ ЭТО ВООБЩЕ НОВАЯ ВЕРСИЯ. Ни агенты, ни пороги, ни веса, ни правило
    выхода не менялись — изменилось правило ДОПУСКА к сделке. Сделок стало
    больше не потому, что рынок другой, а потому, что их перестали резать
    слоты; сложить итоги версий 6 и 7 значило бы описать смесь двух правил
    отбора и назвать её результатом системы.
    """
    assert settings.LOGIC_VERSION == 7 == CURRENT_LOGIC_VERSION


def test_the_data_of_version_six_stays_comparable_because_the_slot_is_untouched(
) -> None:
    """§3 A3 и §6 ТЗ: размер слота и порог входа НЕ МЕНЯЮТСЯ.

    Именно эти два числа делают проценты версии 7 сопоставимыми с версиями 5 и
    6. Тронь слот — и сравнивать стало бы нечего: доля прибыльных считалась бы
    по сделкам разного размера, а сумма в долларах — по разным ставкам.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_SLOT_USD == 2.0
    assert fresh.POSITION_MIN_PROBABILITY == 0.8
    # Замороженный список §6 ТЗ целиком: правило выхода и сроки не тронуты.
    assert fresh.POSITION_MAX_HOLD_HOURS == 48
    assert fresh.POSITION_PLUS_WAIT_START_HOURS == 24
    assert fresh.POSITION_SETTLE_SEC == 90
    assert fresh.POSITION_MAX_SIGNAL_AGE_SEC == 180
    assert fresh.POSITION_GAP_GRACE_SEC == 7200
    assert fresh.DECISION_THRESHOLD == 0.3
    assert fresh.MIN_AGENTS == 2
    assert fresh.AGENT_FRESHNESS_SEC == 300
    assert fresh.WEIGHT_MARKET == fresh.WEIGHT_LIQUIDITY == fresh.WEIGHT_FUTURES == 1.0


# =============================================================================
# §3 A1, A4 ТЗ. Ограничения ЧИСЛА сделок сняты — оба
# =============================================================================

def test_zero_max_open_means_no_limit_and_not_zero_positions() -> None:
    """§3 A1 ТЗ: 0 — это «без ограничения», а НЕ «ноль позиций».

    ЭТО ТА САМАЯ ОШИБКА, КОТОРУЮ ТЗ ТРЕБУЕТ ЗАКРЕПИТЬ ТЕСТОМ. Поставь в .env
    ноль, оставив в коде прежнюю проверку ``open_count >= max_open``, — и
    отказано будет КАЖДОМУ кандидату, при совершенно честном виде журнала:
    «отказов по слотам столько же, сколько кандидатов». Причину искали бы в
    рынке.

    ЗАКРЕПЛЯЕТСЯ ОТСУТСТВИЕМ ПРОВЕРКИ, А НЕ ЕЁ МЯГКОСТЬЮ (§3 A4 ТЗ): у правила
    не осталось ни параметра ``max_open``, ни причины отказа ``slots_full``.
    Ветка, которая никогда не срабатывает, однажды снова начнёт срабатывать от
    чужой правки — и объяснить, почему сделок стало вдвое меньше, будет нечем.
    """
    assert Settings(POSTGRES_PASSWORD="x").POSITION_MAX_OPEN == 0
    assert "slots_full" not in REFUSAL_REASONS
    assert not hasattr(rules, "REASON_SLOTS_FULL")
    rules_text = (_ROOT / "src" / "positions" / "rules.py").read_text(
        encoding="utf-8"
    )
    # Имя настройки в чистом модуле правил не упоминается вовсе — ни в коде,
    # ни в сигнатуре: это и есть «проверки нет», а не «проверка проходит».
    assert "max_open" not in rules_text.replace("max_open``", "")
    assert "max_open" not in should_open.__code__.co_varnames


def test_a_nonzero_max_open_is_reported_and_not_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройка, которая больше ни на что не влияет, названа вслух.

    ``POSITION_MAX_OPEN`` остался в настройках: его печатает строка запуска
    (§9 ТЗ), и удалённый ключ уронил бы ``.env`` при откате. Но человек,
    поставивший туда «3», обязан узнать, что число не работает, — от службы и
    сразу, а не по расхождению журнала с ожиданием через неделю.
    """
    said: list[dict[str, Any]] = []
    monkeypatch.setattr(
        positions_runner._log, "warning",
        lambda event, **kw: said.append({"event": event, **kw}),
    )
    monkeypatch.setattr(settings, "POSITION_MAX_OPEN", 3)
    positions_runner._warn_about_settings_that_do_nothing()
    assert said and said[0]["setting"] == "POSITION_MAX_OPEN"
    assert said[0]["value"] == 3

    # При нуле служба молчит: предупреждение о правильной настройке приучает не
    # читать предупреждения вовсе.
    said.clear()
    monkeypatch.setattr(settings, "POSITION_MAX_OPEN", 0)
    positions_runner._warn_about_settings_that_do_nothing()
    assert said == []


def test_the_one_position_per_instrument_rule_left_the_query_too() -> None:
    """§3 A2 ТЗ: правило снято и в отборе кандидатов, а не только в правиле.

    ОТБОР КАНДИДАТОВ — ВТОРОЕ МЕСТО, ГДЕ ЖИЛО ТО ЖЕ ПРАВИЛО. Сними его в чистом
    модуле и оставь в запросе — и не изменилось бы ничего: кандидат по занятому
    токену просто не дошёл бы до правила, а журнал показал бы «кандидатов нет».
    Проверка текстовая, потому что доказывать надо ОТСУТСТВИЕ условия: по
    значению его не поймать — на пустой таблице позиций обе редакции запроса
    дают один и тот же ответ.
    """
    db_text = (_ROOT / "src" / "core" / "db.py").read_text(encoding="utf-8")
    block = db_text.split("async def get_position_candidates", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "p.status = 'open'" not in block, (
        "отбор кандидатов по-прежнему исключает токены с открытой позицией"
    )
    # А условие «один сигнал — одна позиция» ОСТАЁТСЯ: оно не про частоту
    # сделок, а про повторное чтение того же сигнала после перезапуска.
    assert "p.signal_id = s.id" in block


# =============================================================================
# §3 A3 ТЗ. Бюджет «без ограничения», причина no_free_capital недостижима
# =============================================================================

def test_zero_budget_means_unlimited_and_the_validator_knows_it() -> None:
    """§3 A3 ТЗ: 0 — «бюджет не ограничивает», и старт этого не запрещает.

    ЗАПРЕТ ОСТАЁТСЯ ДЛЯ ЗНАЧЕНИЙ СТРОГО МЕЖДУ НУЛЁМ И СЛОТОМ. Вот они
    по-прежнему означают «не откроется ни одна позиция», и молчать о них
    нельзя: сутки пустого журнала выглядят как законный результат замера.
    """
    assert Settings(POSTGRES_PASSWORD="x").POSITION_BUDGET_USD == 0.0
    assert Settings(POSTGRES_PASSWORD="x", POSITION_BUDGET_USD=0).POSITIONS_ENABLED
    with pytest.raises(ValueError, match="не откроется НИ ОДНА позиция"):
        Settings(POSTGRES_PASSWORD="x", POSITION_BUDGET_USD=1.0,
                 POSITION_SLOT_USD=2.0)


def test_the_no_free_capital_reason_survives_and_becomes_unreachable() -> None:
    """§3 A3 ТЗ: проверку денег СОХРАНИТЬ, счётчик обязан показывать ноль.

    ОТЛИЧИЕ ОТ СЛОТОВ ЗДЕСЬ СОЗНАТЕЛЬНОЕ, а не непоследовательность ТЗ. Слоты
    удалены потому, что понятия «слот» больше нет вовсе; деньги остались —
    просто их сейчас бесконечно много. Разница между «денег нет» и «слотов нет»
    станет содержательной в тот день, когда бюджет снова сделают конечным, и
    обнаружить её будет некому, если ограждение снять сейчас.
    """
    assert REASON_NO_FREE_CAPITAL in REFUSAL_REASONS
    # Достижима она по-прежнему — на искусственном остатке: правило не знает,
    # откуда взялось число, и обязано отвечать на него одинаково всегда.
    assert should_open(**_open_kwargs(free_capital_usd=1.99)).reason == (
        REASON_NO_FREE_CAPITAL
    )
    # А при бесконечном свободном капитале — не срабатывает никогда.
    assert should_open(**_open_kwargs(free_capital_usd=float("inf"))).allowed


async def test_an_unlimited_budget_never_refuses_for_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """То же — на боевом пути, а не только в чистом правиле.

    Сорок позиций уже открыто, бюджет 0 — и сорок первая открывается. При
    прежней арифметике (``0 - 80 = -80``) она получила бы ``no_free_capital``,
    и счётчик, который §3 A3 требует держать на нуле, показывал бы поток.
    """
    fake = _FakeDB(
        candidates=[_candidate(1, "BTC/USDT", 1001)],
        open_rows=[{"instrument_id": 9, "notional_usd": 2.0} for _ in range(40)],
    )
    stats = await _run_open(monkeypatch, fake, token_pause_min=0, budget_usd=0.0)
    assert stats.opened == 1
    assert stats.refusals == {}


# =============================================================================
# §4 ТЗ. Пауза по токену: чистое правило
# =============================================================================

def test_the_pause_counts_from_the_opening_and_not_from_the_closing() -> None:
    """§4 B3 ТЗ: отсчёт от ОТКРЫТИЯ последней позиции по токену.

    ЭТО ГЛАВНОЕ РЕШЕНИЕ ЭТАПА, И ОНО ЛЕГКО ЛОМАЕТСЯ НЕЗАМЕТНО. Отсчитывай
    правило от ЗАКРЫТИЯ — и оно снова зависело бы от длительности сделки, а у
    версий 6 и 7 предела убытка нет: позиция живёт до 48 часов и блокировала бы
    токен почти на двое суток. Журнал при этом выглядел бы честно, а сделок
    было бы столько же, сколько при слотах.

    Проверяется тем, что правило ВООБЩЕ НЕ ЗНАЕТ о закрытии: единственная
    величина, которую оно принимает, — возраст ОТКРЫТИЯ.
    """
    assert "closed" not in token_pause_left_sec.__code__.co_varnames
    assert "last_open_age_sec" in token_pause_left_sec.__code__.co_varnames
    # Позиция открыта 10 минут назад и уже закрыта — пауза всё равно держит
    # оставшиеся 50 минут: правилу нечем узнать о закрытии, и в этом смысл.
    assert token_pause_left_sec(600.0, _HOUR) == pytest.approx(3000.0)


def test_the_pause_boundary_is_inclusive_in_favour_of_the_entry() -> None:
    """Ровно час — вход РАЗРЕШЁН; на секунду раньше — отказ.

    Сравнение нестрогое в пользу входа, как и у остальных границ этого правила
    (возраст сигнала, свежесть свечи, размер слота): «пауза кончилась ровно
    сейчас» — это кончившаяся пауза.
    """
    assert token_pause_left_sec(3600.0, _HOUR) == 0.0
    assert token_pause_left_sec(3599.0, _HOUR) == pytest.approx(1.0)
    assert should_open(**_open_kwargs(last_open_age_sec=3600.0)).allowed is True
    refused = should_open(**_open_kwargs(last_open_age_sec=3599.0))
    assert refused.allowed is False and refused.reason == REASON_TOKEN_PAUSE


def test_no_previous_position_means_the_pause_does_not_hold() -> None:
    """``None`` — «по токену не входили», и это не отказ, а разрешение.

    Различать «не входили» и «входили давно» правилу незачем, и оно не
    различает: оба случая одинаково ничего не держат. Но записать это надо
    явно — ``None`` в арифметике легко превращается в ноль, а ноль означал бы
    «вошли прямо сейчас», то есть полную остановку торговли по всем токенам.
    """
    assert token_pause_left_sec(None, _HOUR) == 0.0
    assert should_open(**_open_kwargs(last_open_age_sec=None)).reason == REASON_OK


def test_zero_pause_switches_the_antispam_off_completely() -> None:
    """§4 B5 ТЗ: 0 выключает паузу полностью — для контрольного опыта.

    Опыт, который обязан упасть, обязан упасть: при выключенной паузе отказов
    ``token_pause`` не бывает ВООБЩЕ, даже если по токену вошли секунду назад.
    """
    assert token_pause_left_sec(1.0, 0.0) == 0.0
    assert should_open(
        **_open_kwargs(last_open_age_sec=1.0, token_pause_sec=0.0)
    ).reason == REASON_OK
    assert Settings(POSTGRES_PASSWORD="x", POSITION_TOKEN_PAUSE_MIN=0
                    ).POSITION_TOKEN_PAUSE_MIN == 0
    with pytest.raises(ValueError, match="не может быть отрицательной"):
        Settings(POSTGRES_PASSWORD="x", POSITION_TOKEN_PAUSE_MIN=-1)


def test_the_signal_own_properties_are_checked_before_the_pause() -> None:
    """Порядок причин: свойства сигнала раньше свойств окружения.

    §8 ТЗ предсказывает, что отказы по паузе составят больше половины
    отклонённых кандидатов. Проверить это предсказание можно только если пауза
    не приписывает себе чужие отказы: сигнал с вероятностью 0.5, пришедший во
    время паузы, — это отказ по вероятности, а не по паузе.
    """
    verdict = should_open(**_open_kwargs(probability=0.5, last_open_age_sec=1.0))
    assert verdict.reason == "low_probability"


def test_every_refusal_reason_is_reachable_and_named_in_the_bot() -> None:
    """Перечень причин закрыт, каждая достижима, у каждой есть перевод.

    Причина, которой не бывает, ничего не объясняет, но выглядит объяснением;
    причина без перевода превращает сводку бота в машинный ключ посреди
    русского текста.
    """
    broken = [
        dict(decision="wait"),
        dict(logic_version=settings.LOGIC_VERSION + 1),
        dict(degraded=True),
        dict(probability=0.1),
        dict(last_open_age_sec=1.0),
        dict(signal_age_sec=10_000.0),
        dict(has_frozen_target=False),
        dict(bar_age_sec=None),
        dict(free_capital_usd=0.0),
    ]
    seen = {should_open(**_open_kwargs(**case)).reason for case in broken}
    assert seen == set(REFUSAL_REASONS)
    for reason in REFUSAL_REASONS:
        assert reason in handlers.REFUSAL_RU, reason
    # И обратно: перевода причины, которой больше не бывает, в боте нет.
    assert set(handlers.REFUSAL_RU) == set(REFUSAL_REASONS)


# =============================================================================
# §4, §9 ТЗ. Пауза по токену на боевом пути службы позиций
# =============================================================================

def _candidate(
    instrument_id: int, symbol: str, signal_id: int, probability: float = 0.9
) -> dict[str, Any]:
    """Кандидат, проходящий все условия, кроме тех, что тест ломает нарочно."""
    return {
        "signal_id": signal_id,
        "instrument_id": instrument_id,
        "signal_ts": _NOW - timedelta(seconds=100),
        "decision": "buy",
        "probability": probability,
        "degraded": False,
        "logic_version": settings.LOGIC_VERSION,
        "price_at_signal": 100.0,
        "target_pct": 1.0,
        "symbol": symbol,
        "age_sec": 100.0,
    }


# «Сейчас» стенда. Все производные времена считаются от него, чтобы стенд не
# зависел от настоящих часов: правило проверяется на заранее известном ответе.
_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class _FakeDB:
    """База-заглушка для ``open_new_positions``: ровно пять нужных методов.

    ПОЗИЦИИ ЗАПОМИНАЮТСЯ, А НЕ ПИШУТСЯ. Проверяется РЕШЕНИЕ службы — кого она
    пустила и кому отказала, — а не то, как asyncpg выполняет INSERT; для
    второго есть тесты схемы, и им нужна настоящая база.
    """

    def __init__(
        self,
        candidates: list[dict[str, Any]],
        open_rows: list[dict[str, Any]] | None = None,
        last_open: dict[int, datetime] | None = None,
    ) -> None:
        self.candidates = candidates
        self.open_rows = open_rows or []
        self.last_open = last_open or {}
        self.opened: list[dict[str, Any]] = []
        self.since_asked: datetime | None = None

    async def get_last_open_ts_by_instrument(
        self, since: datetime
    ) -> dict[int, datetime]:
        self.since_asked = since
        return {
            key: value for key, value in self.last_open.items() if value >= since
        }

    async def get_open_positions(self) -> list[dict[str, Any]]:
        return list(self.open_rows)

    async def get_position_candidates(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.candidates)

    async def get_last_closed_bar(
        self, instrument_id: int, timeframe: str, not_after: datetime
    ) -> dict[str, Any]:
        # Бар закрывается ПОЗЖЕ сигнала и свежее порога: иначе отказ пришёл бы
        # по ``no_fresh_bar``, и тест доказывал бы не то, ради чего написан.
        return {"ts": _NOW - timedelta(seconds=150), "close": 100.0}

    async def open_position(self, row: dict[str, Any]) -> int:
        self.opened.append(row)
        return len(self.opened)


async def _run_open(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeDB,
    *,
    token_pause_min: int = 60,
    budget_usd: float = 0.0,
    now: datetime | None = None,
):
    """Прогоняет шаг открытия на заглушке базы и заданных настройках."""
    monkeypatch.setattr(positions_runner, "db", fake)
    monkeypatch.setattr(positions_runner, "_send", _no_send)
    monkeypatch.setattr(settings, "POSITION_TOKEN_PAUSE_MIN", token_pause_min)
    monkeypatch.setattr(settings, "POSITION_BUDGET_USD", budget_usd)
    return await open_new_positions(now or _NOW)


async def _no_send(_text: str) -> None:
    """Отправки в Telegram на стенде не происходит."""
    return None


def _open_kwargs(**overrides: Any) -> dict[str, Any]:
    """Набор параметров, при котором вход РАЗРЕШЁН. Тесты меняют по одному."""
    base = dict(
        decision="buy",
        logic_version=settings.LOGIC_VERSION,
        expected_version=settings.LOGIC_VERSION,
        degraded=False,
        probability=0.9,
        min_probability=0.8,
        last_open_age_sec=None,
        token_pause_sec=_HOUR,
        signal_age_sec=30.0,
        max_signal_age_sec=180,
        bar_age_sec=45.0,
        max_bar_age_sec=300,
        has_frozen_target=True,
        free_capital_usd=float("inf"),
        slot_usd=2.0,
    )
    base.update(overrides)
    return base


async def test_the_stand_reproduces_the_three_cases_of_the_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9 ТЗ: три случая приёмки, воспроизведённые на стенде.

    СЛУЧАЙ ПЕРВЫЙ — ДВЕ ОДНОВРЕМЕННЫЕ ПОЗИЦИИ ПО ОДНОМУ ТОКЕНУ. Первая открыта
    час назад и ещё жива (позиция версии 7 живёт до 48 часов); вторая
    открывается сейчас. До версии 7 это было невозможно дважды: правилом
    «один инструмент — одна позиция» и уникальным индексом базы.

    СЛУЧАЙ ВТОРОЙ — ТРЕТИЙ КАНДИДАТ В ТУ ЖЕ МИНУТУ ОТКЛОНЁН. Пауза начинается
    НЕМЕДЛЕННО, в той же итерации: иначе все кандидаты по токену, набравшиеся
    за 180 секунд, вошли бы пачкой, а антиспам придерживал бы одиночек.

    СЛУЧАЙ ТРЕТИЙ — ЧЕРЕЗ 60 МИНУТ ПОСЛЕ ОТКРЫТИЯ ВХОД РАЗРЕШЁН. Проверяется
    последним, потому что именно он отличает паузу от запрета.
    """
    # Случай 1 и 2 сразу: по одному токену пришли два кандидата, а час назад по
    # нему уже входили и позиция ещё открыта.
    opened_an_hour_ago = _NOW - timedelta(hours=1)
    fake = _FakeDB(
        candidates=[
            _candidate(1, "BTC/USDT", 1001),
            _candidate(1, "BTC/USDT", 1002),
        ],
        open_rows=[{"instrument_id": 1, "notional_usd": 2.0}],
        last_open={1: opened_an_hour_ago},
    )
    stats = await _run_open(monkeypatch, fake)
    # Первый кандидат вошёл — пауза с прошлого входа истекла ровно час назад.
    assert stats.opened == 1
    assert len(fake.opened) == 1
    # ...и по токену теперь ДВЕ одновременно открытые позиции: старая и новая.
    assert fake.open_rows[0]["instrument_id"] == fake.opened[0]["instrument_id"]
    # Второй кандидат в ту же минуту отклонён — и именно паузой.
    assert stats.refusals == {REASON_TOKEN_PAUSE: 1}

    # Случай 3: та же картина спустя 60 минут после последнего открытия — вход
    # снова разрешён, и это тот же кандидат, что минуту назад получал отказ.
    later = _NOW + timedelta(hours=1)
    fake_later = _FakeDB(
        candidates=[dict(_candidate(1, "BTC/USDT", 1002), age_sec=100.0,
                         signal_ts=later - timedelta(seconds=100))],
        open_rows=[{"instrument_id": 1, "notional_usd": 2.0}],
        last_open={1: _NOW},
    )
    monkeypatch.setattr(
        _FakeDB, "get_last_closed_bar",
        lambda self, instrument_id, timeframe, not_after: _bar_at(later),
    )
    stats_later = await _run_open(monkeypatch, fake_later, now=later)
    assert stats_later.opened == 1
    assert stats_later.refusals == {}


async def _bar_at(now: datetime) -> dict[str, Any]:
    """Свежий закрытый бар для стенда со сдвинутым «сейчас»."""
    return {"ts": now - timedelta(seconds=150), "close": 100.0}


async def test_the_control_experiment_switches_the_refusals_off_and_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9 ТЗ: при паузе 0 отказов token_pause ноль, при 60 — больше нуля.

    ОДИН И ТОТ ЖЕ СТЕНД, ОДНА РАЗЛИЧАЮЩАЯСЯ НАСТРОЙКА. Это и есть контрольный
    опыт: два прогона, отличающиеся ровно тем, что проверяется. Совпади их
    результаты — и утверждение «пауза режет поток» не имело бы подтверждения,
    сколько бы отказов ни было в журнале.
    """
    def stand() -> _FakeDB:
        return _FakeDB(
            candidates=[
                _candidate(1, "BTC/USDT", 2001),
                _candidate(1, "BTC/USDT", 2002),
                _candidate(1, "BTC/USDT", 2003),
            ],
        )

    with_pause = await _run_open(monkeypatch, stand(), token_pause_min=60)
    assert with_pause.opened == 1
    assert with_pause.refusals.get(REASON_TOKEN_PAUSE) == 2

    without_pause = await _run_open(monkeypatch, stand(), token_pause_min=0)
    assert without_pause.opened == 3
    assert REASON_TOKEN_PAUSE not in without_pause.refusals


async def test_the_pause_does_not_reach_across_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пауза по одному токену не держит остальные.

    Ограничитель называется «пауза ПО ТОКЕНУ», и общий на все токены потолок —
    это уже другое правило (такое есть у уведомлений: NOTIFY_MAX_PER_HOUR).
    Перепутай их — и пять токенов давали бы одну сделку в час на всех.
    """
    fake = _FakeDB(
        candidates=[
            _candidate(1, "BTC/USDT", 3001),
            _candidate(2, "ETH/USDT", 3002),
            _candidate(3, "SOL/USDT", 3003),
        ],
        last_open={1: _NOW - timedelta(minutes=5)},
    )
    stats = await _run_open(monkeypatch, fake)
    assert stats.opened == 2
    assert stats.refusals == {REASON_TOKEN_PAUSE: 1}
    assert {row["instrument_id"] for row in fake.opened} == {2, 3}


async def test_the_service_asks_the_database_only_for_the_pause_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Граница чтения — ровно длина паузы, и при нуле чтения нет вовсе.

    Позиции старше паузы на ответ не влияют, и тянуть их значило бы читать всю
    таблицу ради пяти чисел. А при выключенной паузе спрашивать базу не о чем:
    ответ был бы отброшен целиком.
    """
    fake = _FakeDB(candidates=[_candidate(1, "BTC/USDT", 4001)])
    await _run_open(monkeypatch, fake, token_pause_min=60)
    assert fake.since_asked == _NOW - timedelta(hours=1)

    quiet = _FakeDB(candidates=[_candidate(1, "BTC/USDT", 4002)])
    await _run_open(monkeypatch, quiet, token_pause_min=0)
    assert quiet.since_asked is None


async def test_the_refusal_log_names_the_token_probability_and_time_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4 B4 ТЗ: отказ по паузе пишется с токеном, вероятностью и остатком.

    БЕЗ ОСТАТКА ВРЕМЕНИ ПО ЖУРНАЛУ НЕЛЬЗЯ ОТЛИЧИТЬ «пауза только началась» от
    «пауза кончалась через секунду», а из этих отказов и состоит ответ на
    вопрос, сколько потока режет антиспам (§8 ТЗ предсказывает больше половины
    всех отклонённых кандидатов).
    """
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        positions_runner._log, "info",
        lambda event, **kw: written.append({"event": event, **kw}),
    )
    fake = _FakeDB(
        candidates=[_candidate(1, "BTC/USDT", 5001, probability=0.93)],
        last_open={1: _NOW - timedelta(minutes=50)},
    )
    await _run_open(monkeypatch, fake)
    skipped = [row for row in written if row["event"] == "positions_skipped=1"]
    assert len(skipped) == 1
    line = skipped[0]
    assert line["reason"] == REASON_TOKEN_PAUSE
    assert line["symbol"] == "BTC/USDT"
    assert line["probability"] == pytest.approx(0.93)
    # Десять минут до конца часовой паузы — с точностью до секунды.
    assert line["pause_left_sec"] == 600


def test_the_refusal_counter_key_carries_the_new_reason() -> None:
    """Счётчик отказов по паузе собирается той же функцией, что и остальные.

    Имя ключа живёт в чистом модуле, потому что собирают его ДВОЕ: служба
    позиций (пишет) и бот со сводкой (читают). Две одинаковые строки в разных
    файлах однажды разошлись бы, и счётчик просто перестал бы находиться —
    молча, показывая честный ноль.
    """
    key = refusal_key(7, "2026-09-11", REASON_TOKEN_PAUSE)
    assert key == "positions:refused:7:2026-09-11:token_pause"
    report = (_ROOT / "src" / "health" / "daily_report.py").read_text(
        encoding="utf-8"
    )
    assert "positions:refused:" in report and "token_pause" in report


# =============================================================================
# §5 ТЗ. Уведомления только по сделкам
# =============================================================================

def test_signal_notifications_stop_at_version_seven() -> None:
    """§5 C1 ТЗ: сообщения о сигналах прекращаются полностью.

    ГРАНИЦА ЗАПИСАНА ВЕРСИЕЙ ЛОГИКИ, А НЕ ОТДЕЛЬНЫМ ВЫКЛЮЧАТЕЛЕМ: откат (§10
    ТЗ) состоит из возврата .env и LOGIC_VERSION=6, и сигнальные уведомления
    обязаны вернуться вместе с ним, не требуя правки кода.
    """
    assert SIGNAL_NOTIFY_MAX_LOGIC_VERSION == 6
    assert signal_notifications_enabled(5)
    assert signal_notifications_enabled(6)
    assert not signal_notifications_enabled(7)
    assert not signal_notifications_enabled(settings.LOGIC_VERSION)


async def test_not_a_single_signal_message_leaves_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5 C1 ТЗ на боевом пути: ни одной отправки, и очередь не копится.

    ПРОСТО ПЕРЕСТАТЬ ЧИТАТЬ ОЧЕРЕДЬ БЫЛО БЫ ХУЖЕ МОЛЧАНИЯ: неотправленные
    сигналы висели бы в выборке вечно, и каждая итерация перечитывала бы их
    заново. Сигнал поэтому «поглощается» — ровно так же, как сегодня
    поглощается сигнал, никому не ушедший: ``notified`` ставится,
    ``notified_at`` НЕТ, потому что отправки не было.
    """
    sent: list[str] = []
    absorbed: list[int] = []
    notified: list[int] = []

    class _DB:
        async def get_unnotified_strong_signals(self, *_a: Any, **_k: Any):
            return [{"id": 1, "decision": "buy", "probability": 0.95,
                     "instrument_id": 1, "ts": _NOW, "agents_payload": None}]

        async def mark_signal_absorbed(self, signal_id: int) -> None:
            absorbed.append(signal_id)

        async def mark_signal_notified(self, signal_id: int) -> None:
            notified.append(signal_id)

    monkeypatch.setattr(notify_agent, "db", _DB())
    monkeypatch.setattr(
        notify_agent, "send_message",
        lambda *a, **k: sent.append(a[0] if a else ""),
    )
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")

    agent = notify_agent.NotifyAgent(
        interval=30, min_probability=0.7, cooldown_sec=1800,
        symbol="BTC/USDT", tz_name="Europe/Moscow", primary_horizon="4h",
        recipients=[1],
    )
    await agent.process_once()
    assert sent == []
    assert absorbed == [1]
    assert notified == []


def test_the_notify_settings_stay_in_place_untouched() -> None:
    """§5 C4 ТЗ: пороги и ограничения потока остаются в коде и в .env.

    Они перестают влиять на то, что видит человек, но удалить их нельзя: при
    возврате сигнальных уведомлений их пришлось бы выводить заново — а
    выводились они замерами, а не выбраны.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.NOTIFY_MIN_PROBABILITY == 0.7
    assert fresh.NOTIFY_MIN_AGENTS == 3
    assert fresh.NOTIFY_HOLD_MIN == 60
    assert fresh.NOTIFY_MAX_PER_HOUR == 6
    assert fresh.NOTIFY_COOLDOWN_SEC == 1800
    env = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "NOTIFY_MIN_PROBABILITY=0.7", "NOTIFY_MIN_AGENTS=3",
        "NOTIFY_COOLDOWN_SEC=1800",
    ):
        assert re.search(rf"^{re.escape(name)}\b", env, re.M), name
    # И сам отбор сигналов к отправке не тронут: правило ``should_notify``
    # осталось на месте целиком (§5 C3 — поток сигналов трогать нельзя).
    assert callable(notify_agent.should_notify)
    assert callable(notify_agent.rate_limit_reason)


def test_trade_messages_have_no_rate_limit() -> None:
    """§5 C5 ТЗ: ограничителя частоты на сообщениях о сделках нет.

    Будет тридцать сделок в сутки — придут тридцать пар сообщений. Это ровно
    то, что владелец хочет увидеть; станет невыносимо — решение примут по
    факту, а не по предположению. Проверяется по тексту службы: доказывать надо
    ОТСУТСТВИЕ ограничителя, а значением его не поймать — на стенде из двух
    сделок и с потолком ответ был бы тот же.
    """
    runner_text = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    for name in ("NOTIFY_MAX_PER_HOUR", "NOTIFY_HOLD_MIN", "NOTIFY_COOLDOWN_SEC"):
        assert name not in runner_text, (
            f"в службу позиций попало ограничение потока уведомлений {name}"
        )


def test_the_daily_digest_reports_what_the_spec_asks_for() -> None:
    """§5 C2 ТЗ: суточная сводка называет шесть величин, и время её то же.

    НОВОЙ РАССЫЛКИ НЕ ЗАВОДИТСЯ: сводка добавлена разделом в ту, что уходит в
    06:00 UTC, — этап сокращает число потоков, а не увеличивает его.
    """
    report = (_ROOT / "src" / "health" / "daily_report.py").read_text(
        encoding="utf-8"
    )
    block = report.split("def section_positions_24h", 1)[1].split("\ndef ", 1)[0]
    for fragment in (
        "Открыто:", "Закрыто:", "Итог за сутки", "Доля прибыльных",
        "token_pause", "Открыто на момент сводки",
    ):
        assert fragment in block, fragment
    assert "section_positions_24h()" in report.split("def build_message", 1)[1]


# =============================================================================
# §5 C6 ТЗ. /status: слотов нет, есть позиции и паузы
# =============================================================================

def test_status_drops_the_slots_and_shows_positions_and_pauses() -> None:
    """§5 C6 ТЗ: в /status нет слотов, есть число позиций и состояние пауз."""
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    text = handlers.render_status(
        [("decision:heartbeat", now.isoformat(), 60)],
        {"open_count": 3, "closed_count": 7},
        now,
        per_token=[("BTC/USDT", now)],
        positions={
            "open_count": 4,
            "token_pause_min": 60,
            "pauses": [("BTC/USDT", 600.0)],
        },
    )
    assert "слот" not in text.lower()
    assert "Открытых позиций: 4" in text
    assert "BTC 10 мин" in text

    # Выключенная пауза названа вслух: пустой перечень при выключенном
    # антиспаме выглядел бы ровно как пустой перечень при работающем.
    off = handlers.render_status(
        [], {}, now, positions={"open_count": 0, "token_pause_min": 0,
                                "pauses": []},
    )
    assert "выключена" in off


# =============================================================================
# §7 ТЗ. База: миграция 027 и её откат
# =============================================================================

def _sql_only(text: str) -> str:
    """Текст миграции без строк-пояснений: проверяется ТО, ЧТО ИСПОЛНЯЕТСЯ.

    Пояснения в миграциях этого проекта длиннее самого SQL и намеренно
    называют то, чего миграция НЕ делает. Искать запрещённые слова вместе с
    ними значило бы ловить обещание как нарушение.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


def test_migration_027_is_the_next_free_number_and_is_wrapped_in_a_transaction(
) -> None:
    """§7.3 ТЗ: следующий свободный номер, BEGIN … COMMIT, есть откат."""
    migration = _MIGRATIONS / "027_positions_token_pause.sql"
    rollback = _MIGRATIONS / "027_positions_token_pause_rollback.sql"
    assert migration.exists() and rollback.exists()
    # Номер свободен: 027 не занят ничем другим.
    numbered = sorted(
        path.name for path in _MIGRATIONS.glob("027_*")
    )
    assert numbered == [migration.name, rollback.name]
    text = migration.read_text(encoding="utf-8")
    assert text.count("BEGIN;") == 1 and text.rstrip().endswith("COMMIT;")
    # И ни одного изменения данных: §7.1 требует не тронуть ни одного поля
    # позиций прежних версий. Проверяются ИСПОЛНЯЕМЫЕ строки, а не пояснения:
    # слова «ни UPDATE, ни DELETE здесь нет» — это как раз обещание, которое
    # проверяется, и ловить его как нарушение было бы издевательством.
    for forbidden in ("UPDATE ", "DELETE ", "INSERT "):
        assert forbidden not in _sql_only(text).upper(), forbidden


def test_the_relaxation_is_scoped_to_version_seven_in_both_places() -> None:
    """§7.2 ТЗ: ослабление действует ТОЛЬКО для версии 7.

    ДВА МЕСТА, А НЕ ОДНО, и это главное в этой проверке. Индекс создаёт не
    только миграция: его же при каждом старте гарантирует
    ``ensure_positions_schema``. Поправь одно и забудь другое — и первая же
    перезагрузка службы вернула бы прежнее правило молча, без единой ошибки.
    """
    migration = (_MIGRATIONS / "027_positions_token_pause.sql").read_text(
        encoding="utf-8"
    )
    db_text = (_ROOT / "src" / "core" / "db.py").read_text(encoding="utf-8")
    predicate = "WHERE status = 'open' AND logic_version < 7"
    assert predicate in migration
    assert predicate.replace("WHERE ", "") in db_text.replace('"\n            "', "")
    # Индекс «один сигнал — одна позиция» миграция не трогает ни одной
    # исполняемой строкой (упомянуть его в пояснении — можно и нужно), а служба
    # по-прежнему его гарантирует.
    assert "ux_positions_signal" not in _sql_only(migration)
    assert "ux_positions_signal" in db_text


def test_the_rollback_refuses_to_destroy_version_seven_data() -> None:
    """§10 ТЗ: данные версии 7 при откате не удаляются.

    Вернуть прежний индекс при двух открытых позициях по одному токену можно
    ровно двумя способами: закрыть лишние выдуманной ценой или удалить их.
    Первое — данные, которые лгут; второе — потеря факта. Откат не делает ни
    того, ни другого и НЕ МОЛЧИТ об этом.
    """
    rollback = (_MIGRATIONS / "027_positions_token_pause_rollback.sql").read_text(
        encoding="utf-8"
    )
    assert "RAISE EXCEPTION" in rollback
    sql = _sql_only(rollback).upper()
    assert "DELETE FROM" not in sql
    assert "UPDATE " not in sql


# =============================================================================
# §7.4 ТЗ. Торговый журнал: сделки версии 7 уходят, версии 6 остаются
# =============================================================================

def test_the_trade_journal_takes_every_version_and_deletes_nothing() -> None:
    """§7.4 ТЗ: в журнал уходят сделки версии 7, сделки версии 6 не удаляются.

    ИСПОЛНИТЕЛЬ ПРОВЕРИЛ И ГОВОРИТ ПРЯМО: правки здесь не потребовалось, и это
    проверяемое утверждение, а не отговорка. Очередь торгового журнала отбирает
    позиции по ОТМЕТКАМ ``sheet_opened_at``/``sheet_closed_at``, а не по версии
    логики: условия ``logic_version`` в ней нет ни одного. Значит, сделки
    версии 7 попадут в лист сразу, как появятся, а строки версии 6 останутся
    там, где лежат, — ни одна выгрузка их не трогает.

    ``EXPORT_LOGIC_VERSION`` при этом управляет ЛИСТАМИ СИГНАЛОВ, а не
    журналом; значение ``current`` означает «последняя открытая версия из
    ``logic_version_windows``», а границу версии фиксирует Decision Agent при
    первом старте на ней. Менять настройку не нужно и НЕЛЬЗЯ: число 6 в ней
    удержало бы листы сигналов на прошлой версии.
    """
    queries_text = (_ROOT / "src" / "export" / "queries.py").read_text(
        encoding="utf-8"
    )
    journal = queries_text.split("ЭТАП 9.1.2. Торговый журнал", 1)[1]
    for block_name in (
        "fetch_positions_pending_open", "fetch_positions_pending_close",
    ):
        block = journal.split(f"async def {block_name}", 1)[1].split(
            "\nasync def ", 1
        )[0]
        assert "logic_version = " not in block, block_name
        assert "DELETE" not in block.upper(), block_name
    assert Settings(POSTGRES_PASSWORD="x").EXPORT_LOGIC_VERSION == "current"


# =============================================================================
# §9 ТЗ. Строка запуска службы позиций
# =============================================================================

def test_the_startup_line_names_every_value_the_spec_lists() -> None:
    """§9 ТЗ: служба печатает при старте ровно эти восемь величин.

    Строка запуска — единственное место, где владелец видит, С ЧЕМ служба
    поднялась, не заходя в .env. Пропусти в ней ``token_pause_min`` — и
    отличить «пауза час» от «пауза выключена» можно было бы только по
    отсутствию отказов, то есть постфактум и по косвенному признаку.
    """
    fields = startup_fields()
    assert set(STARTUP_FIELDS) <= set(fields)
    assert fields["logic_version"] == 7
    assert fields["max_open"] == 0
    assert fields["token_pause_min"] == 60
    assert fields["budget_usd"] == 0.0
    assert fields["slot_usd"] == 2.0
    assert fields["max_hold_hours"] == 48
    assert fields["plus_wait_start_hours"] == 24
    assert fields["min_probability"] == 0.8


def test_the_settle_lag_still_leaves_room_before_the_signal_expires() -> None:
    """§6 ТЗ: тонкая связь 60 + SETTLE < MAX_SIGNAL_AGE осталась зелёной.

    ТЗ прямо требует проверить, что эта связь не порвана (§6, последний
    абзац). Порви её — и позиций не открывалось бы НИ ОДНОЙ при внешне честном
    журнале: бар входа не успевал бы стать годным раньше, чем сигнал устареет.
    Проверка живёт и в tests/test_stage_9_1.py; здесь она повторена намеренно,
    потому что этап трогал именно отбор кандидатов.
    """
    assert 60 + settings.POSITION_SETTLE_SEC < settings.POSITION_MAX_SIGNAL_AGE_SEC
    assert 60 + settings.POSITION_SETTLE_SEC == 150
    assert settings.POSITION_MAX_SIGNAL_AGE_SEC == 180


# =============================================================================
# §7 ТЗ. Проверки, которым нужна база
# =============================================================================

@needs_db
async def test_the_database_allows_two_open_positions_per_token_only_for_v7(
) -> None:
    """§7.2 ТЗ на настоящей базе: ослабление касается ТОЛЬКО версии 7.

    ЭТО ЕДИНСТВЕННАЯ ПРОВЕРКА, КОТОРУЮ НЕЛЬЗЯ СДЕЛАТЬ БЕЗ БАЗЫ. Правило
    записано частичным уникальным индексом, а не кодом, и «сняли ли мы его для
    версии 7, не сняв для шестой» — вопрос к самому индексу.
    """
    import asyncpg

    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute(
            (_ROOT / "db" / "init.sql").read_text(encoding="utf-8")
        )
        for name in (
            "018_positions.sql", "019_positions_data_gap.sql",
            "025_positions_no_stop.sql", "027_positions_token_pause.sql",
        ):
            await conn.execute((_MIGRATIONS / name).read_text(encoding="utf-8"))

        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'TEST7/USDT', 'TEST7', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        # СВОИ СТРОКИ ТЕСТ УБИРАЕТ ЗА СОБОЙ — И ПЕРЕД РАБОТОЙ, А НЕ ТОЛЬКО
        # ПОСЛЕ. Прерванный прогон оставил бы открытую позицию по этому
        # инструменту, и следующий прогон падал бы на ней, а не на том, что
        # проверяет. Чужие строки при этом не трогаются: всё ограничено
        # одноразовым инструментом TEST7/USDT.
        await _drop_test_rows(conn, instrument_id)

        async def _open(version: int) -> int:
            signal_id = await conn.fetchval(
                "INSERT INTO signals (instrument_id, ts, decision, "
                "logic_version, probability) "
                "VALUES ($1, now(), 'buy', $2, 0.9) RETURNING id;",
                instrument_id, version,
            )
            return await conn.fetchval(
                """
                INSERT INTO positions
                    (instrument_id, signal_id, logic_version, horizon_h, side,
                     status, signal_ts, signal_price, opened_at, entry_price,
                     entry_lag_sec, entry_slippage_pct, qty, notional_usd,
                     target_pct, target_price, stop_pct, stop_price, cost_pct,
                     deadline_at, resolution)
                VALUES ($1, $2, $3, 24, 'buy', 'open', now(), 100, now(), 100,
                        42, 0, 0.02, 2.0, 1.0, 101, NULL, NULL, 0.22,
                        now() + interval '48 hours', '1m')
                RETURNING id;
                """,
                instrument_id, signal_id, version,
            )

        # Версия 7: две одновременные позиции по одному токену допускаются.
        first = await _open(7)
        second = await _open(7)
        assert first != second

        # Версия 6: вторая открытая позиция по тому же токену по-прежнему
        # невозможна — её правила задним числом не переписываются.
        await _open(6)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _open(6)
    finally:
        try:
            await _drop_test_rows(conn, instrument_id)
        finally:
            await conn.close()


async def _drop_test_rows(conn: Any, instrument_id: int) -> None:
    """Удаляет позиции и сигналы одноразового инструмента этого теста."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id = $1;", instrument_id
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id = $1;", instrument_id
    )
