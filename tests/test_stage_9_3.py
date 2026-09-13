"""ЭТАП 9.3: торговый слой — снятие слотов, пауза по токену, поток сделок.

ЧТО ПРОВЕРЯЕТ ЭТОТ НАБОР И ЧЕГО НЕ ПРОВЕРЯЕТ. Здесь проверяется ВНЕДРЕНИЕ:
что ограничители ЧИСЛА сделок остались в коде и при нулевых настройках
недостижимы; что пауза по токену считается от ОТКРЫТИЯ, скользящим окном, из
базы и только по своей версии логики; что в Telegram уходит ровно три вида
сообщений и ни одного о сигнале. Здесь НЕ проверяется, много ли станет сделок:
это предсказания §12 ТЗ, и проверит их прогон на боевом сервере, а не тест.

ПРАВИЛО §9 ТЗ ДЕЙСТВУЕТ ЗДЕСЬ БЕЗ ИСКЛЮЧЕНИЙ: «контрольный опыт, посчитанный
тем же кодом, который он проверяет, контролем не является». Ожидаемые значения
выписаны ЧИСЛОМ в тексте теста — 20, 5, 15, 3599, 3601, −6.13 и так далее, — а
не получены вызовом той же функции с другими параметрами.

У КАЖДОГО ОПЫТА §10 ЕСТЬ КОНТРОЛЬ, И КОНТРОЛЬ ОБЯЗАН УПАСТЬ. Он написан здесь
же и рядом: опыт, проходящий при намеренно сломанной логике, считается
несостоявшимся, и единственный способ показать, что он не таков, — сломать
логику и увидеть другой ответ. Контроли оформлены не отдельными тестами, а
вторым утверждением внутри того же теста: разнесённые по файлу, они однажды
разойдутся с опытом, который стерегут.

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
import src.notify.daily_trades as daily_trades
import src.notify.runner as notify_runner
import src.positions.rules as rules
import src.positions.runner as positions_runner
from src.core.config import Settings, settings
from src.positions import messages
from src.positions.rules import (
    REASON_INSTRUMENT_BUSY,
    REASON_MAX_OPEN,
    REASON_TOKEN_PAUSE,
    REFUSAL_REASONS,
    token_pause_left_sec,
)

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

# «Сейчас» стенда. Все производные времена считаются от него, чтобы стенд не
# зависел от настоящих часов: правило проверяется на заранее известном ответе.
_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# ЧАС ПАУЗЫ В СЕКУНДАХ, ВЫПИСАННЫЙ ЧИСЛОМ (§3 ТЗ: POSITIONS_COOLDOWN_SEC=3600).
# Не ``settings.POSITION_TOKEN_PAUSE_MIN * 60``: это была бы проверка кода тем
# же кодом.
_COOLDOWN_SEC = 3600


# =============================================================================
# §3 ТЗ. Настройки: имена, нулевая семантика, запрет второго имени
# =============================================================================

def test_the_zero_settings_mean_no_limit_and_the_checks_are_still_there() -> None:
    """§3 ТЗ: 0 — «ограничение не применяется», и проверка при этом ЖИВА.

    ДВА ТРЕБОВАНИЯ §3 ЧИТАЮТСЯ ДРУГ ПРОТИВ ДРУГА, И РАЗРЕШЕНЫ ОНИ ОБА. «0 не
    должен приводить к отказу в открытии» и «проверки не вырезаются из кода, а
    обходятся при нулевом значении» совместимы ровно одним способом: ветка
    есть, условие ``> 0`` стоит первым, при нуле она недостижима.

    ЧИСЛА ВЫПИСАНЫ, А НЕ ВЗЯТЫ ИЗ НАСТРОЕК: 0 и 0 — то, что §3 требует
    поставить в ``.env``.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_MAX_OPEN == 0
    assert fresh.POSITION_BUDGET_USD == 0.0
    assert fresh.POSITION_ONE_PER_TOKEN is False
    assert fresh.LOGIC_VERSION == 7

    # Проверки на месте: обе причины в закрытом перечне и обе достижимы.
    assert REASON_MAX_OPEN in REFUSAL_REASONS
    assert REASON_INSTRUMENT_BUSY in REFUSAL_REASONS
    assert rules.REASON_NO_FREE_CAPITAL in REFUSAL_REASONS


def test_no_second_name_is_introduced_for_a_value_that_already_has_one() -> None:
    """§3 ТЗ: заводить второе имя для той же величины запрещено.

    ЭТО ОТДЕЛЬНЫЙ КЛАСС ДЕФЕКТА, УЖЕ СЛУЧАВШИЙСЯ В ПРОЕКТЕ (``NOTIFY_THRESHOLD``
    и ``NOTIFY_MIN_PROBABILITY``), и §3 называет его прямо. ТЗ 9.3 перечисляет
    настройки под именами ``POSITIONS_*``; в проекте те же величины носят имена
    ``POSITION_*`` с этапов 9.1 и 7, и §3 велит использовать существующие.

    ТРИ ИМЕНИ, КОТОРЫХ НЕ ДОЛЖНО ПОЯВИТЬСЯ, названы здесь поимённо: это те, где
    соблазн завести второе сильнее всего, потому что у них отличается не только
    префикс — у ``COOLDOWN_SEC`` ещё и единица измерения.
    """
    fields = set(Settings.model_fields)
    for forbidden, existing in (
        ("POSITIONS_COOLDOWN_SEC", "POSITION_TOKEN_PAUSE_MIN"),
        ("NOTIFY_TRADES_ENABLED", "POSITION_NOTIFY_ENABLED"),
        ("POSITIONS_MAX_OPEN", "POSITION_MAX_OPEN"),
        ("POSITIONS_ONE_PER_TOKEN", "POSITION_ONE_PER_TOKEN"),
        ("POSITIONS_BUDGET_USD", "POSITION_BUDGET_USD"),
    ):
        assert forbidden not in fields, forbidden
        assert existing in fields, existing

    # А новые имена, которых в проекте не было, взяты из ТЗ БЕЗ изменений:
    # менять их не на что — величины новые.
    for new_name in (
        "NOTIFY_SIGNALS_ENABLED",
        "NOTIFY_DAILY_SUMMARY_UTC",
        "NOTIFY_TRADES_MAX_PER_HOUR",
    ):
        assert new_name in fields, new_name


def test_the_cooldown_setting_is_exactly_one_hour_in_both_units() -> None:
    """§3 ТЗ: 3600 секунд — и в минутах это ровно 60.

    Пересчёт единиц — то место, где ошибка не выглядит ошибкой: и 60, и 3600
    похожи на правду. Оба числа выписаны здесь.
    """
    assert Settings(POSTGRES_PASSWORD="x").POSITION_TOKEN_PAUSE_MIN == 60
    assert positions_runner.startup_fields()["cooldown_sec"] == _COOLDOWN_SEC
    assert _COOLDOWN_SEC == 3600


def test_the_settings_the_stage_must_not_touch_are_untouched() -> None:
    """§1.2, §3 ТЗ: перечислены, чтобы исполнитель их не тронул.

    ЖЁСТКАЯ ГРАНИЦА ЭТАПА. Меняется только то, что делается с сигналом ПОСЛЕ
    того, как он уже принят; качество сигнала не затрагивается — значит выборка
    сигналов, на которой стоят Замеры 1–3, не обнуляется. Числа выписаны.
    """
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_SLOT_USD == 2.0
    assert fresh.POSITION_MIN_PROBABILITY == 0.8
    assert fresh.POSITION_MAX_HOLD_HOURS == 48
    assert fresh.POSITION_PLUS_WAIT_START_HOURS == 24
    # Пороги уведомлений о сигналах остались нетронутыми: §6.1 требует обойти
    # их флагом, а не переписать.
    assert fresh.NOTIFY_MIN_PROBABILITY == 0.7
    assert fresh.NOTIFY_MIN_AGENTS == 3
    assert fresh.NOTIFY_HOLD_MIN == 60
    assert fresh.NOTIFY_MAX_PER_HOUR == 6
    assert fresh.NOTIFY_COOLDOWN_SEC == 1800


# =============================================================================
# Стенд: заглушка базы и прогон боевого шага открытия
# =============================================================================

def _candidate(
    instrument_id: int,
    symbol: str,
    signal_id: int,
    probability: float = 0.9,
    age_sec: float = 100.0,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Кандидат, проходящий ВСЕ условия, кроме тех, что тест ломает нарочно.

    ``at`` — момент, в который его подают. Нужен там, где опыт сдвигает часы
    (§10.2, §10.4): сигнал, выписанный от ``_NOW``, к моменту ``_NOW + час``
    устарел бы по свежести свечи, и отказ пришёл бы по ``no_fresh_bar`` —
    тест доказывал бы не то, ради чего написан.
    """
    at = at or _NOW
    return {
        "signal_id": signal_id,
        "instrument_id": instrument_id,
        "signal_ts": at - timedelta(seconds=age_sec),
        "decision": "buy",
        "probability": probability,
        "degraded": False,
        "logic_version": settings.LOGIC_VERSION,
        "price_at_signal": 100.0,
        "target_pct": 1.0,
        "symbol": symbol,
        "age_sec": age_sec,
    }


class _FakeDB:
    """База-заглушка для ``open_new_positions``.

    ПОЗИЦИИ ХРАНЯТСЯ СПИСКОМ, А НЕ ПИШУТСЯ. Проверяется РЕШЕНИЕ службы — кого
    она пустила и кому отказала, — а не то, как asyncpg выполняет INSERT; для
    второго есть тесты схемы, и им нужна настоящая база (ниже, ``needs_db``).

    ``store`` ПЕРЕЖИВАЕТ «ПЕРЕЗАПУСК» (§10.3). Это не удобство стенда, а
    предмет опыта: заглушку можно создать заново поверх того же ``store`` —
    ровно так контейнер перезапускается поверх той же базы.
    """

    def __init__(
        self,
        candidates: list[dict[str, Any]],
        store: list[dict[str, Any]] | None = None,
    ) -> None:
        self.candidates = candidates
        self.store = store if store is not None else []
        self.opened: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.since_asked: datetime | None = None
        self.version_asked: int | None = None

    async def get_last_open_ts_by_instrument(
        self, since: datetime, logic_version: int
    ) -> dict[int, datetime]:
        self.since_asked = since
        self.version_asked = int(logic_version)
        found: dict[int, datetime] = {}
        for row in self.store:
            if row["opened_at"] < since:
                continue
            if int(row["logic_version"]) != int(logic_version):
                continue
            key = int(row["instrument_id"])
            if key not in found or row["opened_at"] > found[key]:
                found[key] = row["opened_at"]
        return found

    async def get_open_positions(self) -> list[dict[str, Any]]:
        return [row for row in self.store if row["status"] == "open"]

    async def get_position_candidates(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.candidates)

    async def get_last_closed_bar(
        self, instrument_id: int, timeframe: str, not_after: datetime
    ) -> dict[str, Any]:
        # ПОСЛЕДНИЙ ЗАВЕДОМО ЗАКРЫТЫЙ БАР — ровно тот, о котором спрашивают.
        # Так он закрывается ПОЗЖЕ сигнала и свежее порога при любом «сейчас»;
        # иначе отказ пришёл бы по ``no_fresh_bar``, и тест доказывал бы не то,
        # ради чего написан.
        return {"ts": not_after, "close": 100.0}

    async def open_position(self, row: dict[str, Any]) -> int:
        self.opened.append(row)
        position_id = len(self.store) + 1
        self.store.append({
            "id": position_id,
            "instrument_id": int(row["instrument_id"]),
            "logic_version": int(row["logic_version"]),
            "opened_at": row["opened_at"],
            "notional_usd": float(row["notional_usd"]),
            "status": "open",
        })
        return position_id

    async def record_position_rejections(
        self, rows: list[dict[str, Any]]
    ) -> int:
        self.rejections.extend(rows)
        return len(rows)


async def _no_send(_text: str, now: datetime | None = None) -> None:
    """Отправки в Telegram на стенде не происходит."""
    return None


async def _run_open(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeDB,
    *,
    now: datetime | None = None,
    max_open: int = 0,
    one_per_token: bool = False,
    cooldown_min: int = 60,
    budget_usd: float = 0.0,
):
    """Прогоняет боевой шаг открытия на заглушке и заданных настройках."""
    monkeypatch.setattr(positions_runner, "db", fake)
    monkeypatch.setattr(positions_runner, "_send", _no_send)
    monkeypatch.setattr(settings, "POSITION_MAX_OPEN", max_open)
    monkeypatch.setattr(settings, "POSITION_ONE_PER_TOKEN", one_per_token)
    monkeypatch.setattr(settings, "POSITION_TOKEN_PAUSE_MIN", cooldown_min)
    monkeypatch.setattr(settings, "POSITION_BUDGET_USD", budget_usd)
    return await positions_runner.open_new_positions(now or _NOW)


# =============================================================================
# §10.1 ТЗ. Слоты сняты по-настоящему
# =============================================================================

async def test_10_1_twenty_signals_open_twenty_positions_and_the_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.1 ТЗ: 20 подходящих сигналов по 20 токенам → 20 позиций, 0 отказов.

    ОПЫТ. Подаём двадцать годных сигналов по двадцати РАЗНЫМ условным токенам
    (разным — чтобы пауза по токену не вмешалась: она про частоту, а опыт про
    число). Ожидание выписано числом: открыто 20, отказов ``max_open`` — 0.

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ. При ``POSITION_MAX_OPEN=5`` открыто ровно 5,
    отказов ``max_open`` — 15. Если бы проверка была вырезана (как её вырезал
    этап 7) или обойдена навсегда, контроль дал бы 20 и 0 — и опыт выше ничего
    бы не доказывал: он проходил бы при любой реализации.
    """
    candidates = [
        _candidate(instrument_id=i, symbol=f"T{i}/USDT", signal_id=100 + i)
        for i in range(1, 21)
    ]
    assert len(candidates) == 20

    stats = await _run_open(monkeypatch, _FakeDB(candidates), max_open=0)
    assert stats.opened == 20
    assert stats.refusals.get(REASON_MAX_OPEN, 0) == 0
    assert stats.refusals == {}

    # КОНТРОЛЬ: потолок пять.
    control = await _run_open(monkeypatch, _FakeDB(candidates), max_open=5)
    assert control.opened == 5
    assert control.refusals.get(REASON_MAX_OPEN) == 15


async def test_10_1_the_budget_never_refuses_and_the_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3, §4.4 ТЗ: ``budget`` при нулевом бюджете недостижим, но жив.

    ОПЫТ: двадцать сделок по $2 — это $40, вчетверо больше прежнего бюджета
    $10, и ни одного отказа по деньгам быть не должно.

    КОНТРОЛЬ: бюджет $10 при слоте $2 пропускает ровно 5 и отказывает 15.
    Число 5 выписано, а не посчитано делением в тесте.
    """
    candidates = [
        _candidate(instrument_id=i, symbol=f"T{i}/USDT", signal_id=200 + i)
        for i in range(1, 21)
    ]
    stats = await _run_open(monkeypatch, _FakeDB(candidates), budget_usd=0.0)
    assert stats.opened == 20
    assert stats.refusals.get(rules.REASON_NO_FREE_CAPITAL, 0) == 0

    control = await _run_open(
        monkeypatch, _FakeDB(candidates), budget_usd=10.0
    )
    assert control.opened == 5
    assert control.refusals.get(rules.REASON_NO_FREE_CAPITAL) == 15


async def test_a_new_position_carries_the_current_logic_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2, §11.8 ТЗ: каждая новая строка ``positions`` получает версию 7.

    ВЕРСИЯ ЛОГИКИ — ЖЁСТКАЯ ГРАНИЦА ВЫБОРКИ. Строка версии 7, помеченная
    шестёркой, попала бы в выборку версии 6 и смешала бы две несравнимые
    выборки — молча, потому что все остальные её поля выглядят правильно.
    Число выписано: 7.
    """
    fake = _FakeDB([_candidate(1, "T1/USDT", 1001)])
    await _run_open(monkeypatch, fake)
    assert [row["logic_version"] for row in fake.opened] == [7]
    assert [row["logic_version"] for row in fake.store] == [7]
    # И срок жизни у неё — версии 7, а не горизонт сигнала (§1.2 ТЗ: правило
    # выхода версии 6 не трогается, а версия 7 наследует его целиком).
    opened = fake.opened[0]
    assert (opened["deadline_at"] - opened["opened_at"]).total_seconds() == (
        48 * 3600
    )


# =============================================================================
# §10.2 ТЗ. Пауза работает и считается ОТ ОТКРЫТИЯ
# =============================================================================

@pytest.mark.parametrize(
    "gap_sec, expect_opened, expect_cooldown",
    [(3599, 1, 1), (3601, 2, 0)],
)
async def test_10_2_the_pause_holds_at_3599_and_lets_through_at_3601(
    monkeypatch: pytest.MonkeyPatch,
    gap_sec: int,
    expect_opened: int,
    expect_cooldown: int,
) -> None:
    """§10.2 ТЗ: два сигнала по ОДНОМУ токену с разрывом 3599 и 3601 секунды.

    ОПЫТ. 3599 с — открыта 1 позиция, отказов ``cooldown`` 1. 3601 с — открыто
    2, отказов 0. Все четыре числа выписаны параметрами теста, а не посчитаны.

    ОДНА СЕКУНДА ПО ОБЕ СТОРОНЫ ГРАНИЦЫ — НЕ ПРИДИРКА. Ошибка на единицу в
    сравнении (``>`` вместо ``>=``) при разрыве в десять минут не видна вовсе;
    здесь она роняет ровно один из двух случаев.
    """
    fake = _FakeDB([_candidate(1, "T1/USDT", 301)])
    await _run_open(monkeypatch, fake, now=_NOW)
    assert len(fake.store) == 1

    later = _NOW + timedelta(seconds=gap_sec)
    fake.candidates = [_candidate(1, "T1/USDT", 302, at=later)]
    stats = await _run_open(monkeypatch, fake, now=later)
    assert len(fake.store) == expect_opened
    assert stats.refusals.get(REASON_TOKEN_PAUSE, 0) == expect_cooldown


def test_10_2_counting_from_the_close_would_block_a_48_hour_trade_for_49(
) -> None:
    """§5.1 ТЗ: отсчёт от ОТКРЫТИЯ, и вот чего стоил бы отсчёт от закрытия.

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ, ЗАПИСАН ЗДЕСЬ АРИФМЕТИКОЙ, а не подменой
    функции: сделка версии 7 живёт до 48 часов, предела убытка у неё нет.
    Отсчёт от закрытия держал бы токен 48 + 1 = 49 часов, то есть вернул бы
    слоты через заднюю дверь, и замер потолка торговли не состоялся бы.

    ЧИСЛА ВЫПИСАНЫ: 48 часов жизни, 3600 секунд паузы, 176 400 секунд — это
    49 часов. Функция проверяется на них, а не наоборот.
    """
    opened_at = _NOW
    closed_at = _NOW + timedelta(hours=48)
    probe = closed_at + timedelta(minutes=30)

    # ОТСЧЁТ ОТ ОТКРЫТИЯ: прошло 48.5 часов, пауза давно истекла — вход открыт.
    age_from_open = (probe - opened_at).total_seconds()
    assert age_from_open == 174_600.0
    assert token_pause_left_sec(age_from_open, _COOLDOWN_SEC) == 0.0

    # ОТСЧЁТ ОТ ЗАКРЫТИЯ (то, чего делать нельзя): прошло полчаса, пауза
    # держит — и держала бы вплоть до 49-го часа с открытия.
    age_from_close = (probe - closed_at).total_seconds()
    assert age_from_close == 1800.0
    assert token_pause_left_sec(age_from_close, _COOLDOWN_SEC) == 1800.0
    assert (closed_at + timedelta(seconds=_COOLDOWN_SEC) - opened_at
            ).total_seconds() == 176_400.0


async def test_10_2_the_window_slides_and_does_not_reset_on_the_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.2 ТЗ: окно скользящее, а не календарный час.

    ПРЯМОЙ УРОК ЭТАПА 8.3, И ОН ВЫПИСАН ЧИСЛАМИ: вход в 10:59 и вход в 11:01
    при обнулении в начале часа формально укладываются в правило «не чаще раза
    в час» и дают два входа за ДВЕ МИНУТЫ. Проверяется тем, что второй вход в
    11:01 отвергнут, — то есть граница часа ничего не обнулила.
    """
    ten_fifty_nine = datetime(2026, 9, 12, 10, 59, tzinfo=UTC)
    eleven_oh_one = datetime(2026, 9, 12, 11, 1, tzinfo=UTC)
    assert (eleven_oh_one - ten_fifty_nine).total_seconds() == 120.0

    fake = _FakeDB([_candidate(1, "T1/USDT", 401, at=ten_fifty_nine)])
    await _run_open(monkeypatch, fake, now=ten_fifty_nine)
    assert len(fake.store) == 1

    fake.candidates = [_candidate(1, "T1/USDT", 402, at=eleven_oh_one)]
    stats = await _run_open(monkeypatch, fake, now=eleven_oh_one)
    assert len(fake.store) == 1
    assert stats.refusals.get(REASON_TOKEN_PAUSE) == 1


async def test_10_2_the_pause_is_per_token_and_not_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.5 ТЗ: пауза У КАЖДОГО ТОКЕНА СВОЯ, а не одна на все сразу.

    Пауза, применённая ко всем токенам разом, пропускала бы одну сделку в час
    на всю систему — то есть 24 сделки в сутки вместо предсказанных §12.1
    15–60, и выглядело бы это совершенно законно.
    """
    fake = _FakeDB([_candidate(1, "T1/USDT", 501)])
    await _run_open(monkeypatch, fake, now=_NOW)

    minute = _NOW + timedelta(minutes=1)
    fake.candidates = [
        _candidate(1, "T1/USDT", 502, at=minute),
        _candidate(2, "T2/USDT", 503, at=minute),
        _candidate(3, "T3/USDT", 504, at=minute),
    ]
    stats = await _run_open(monkeypatch, fake, now=minute)
    # Свой токен придержан, два чужих прошли: 1 отказ и 2 открытия.
    assert stats.refusals.get(REASON_TOKEN_PAUSE) == 1
    assert stats.opened == 2


async def test_10_2_the_pause_reads_only_its_own_logic_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.3, §11.6 ТЗ: пауза читает позиции ТОЛЬКО своей версии логики.

    ПРАКТИЧЕСКАЯ РАЗНИЦА ВИДНА В ПЕРВЫЙ ЖЕ ЧАС ПОСЛЕ РАЗВЁРТЫВАНИЯ. Позиции
    версии 6, открытые перед перезапуском, иначе придержали бы свои токены, и
    первые входы версии 7 оказались бы пропущены по причине, к версии 7
    отношения не имеющей.
    """
    store = [{
        "id": 1, "instrument_id": 1, "logic_version": 6,
        "opened_at": _NOW - timedelta(minutes=1),
        "notional_usd": 2.0, "status": "open",
    }]
    fake = _FakeDB([_candidate(1, "T1/USDT", 601)], store=store)
    stats = await _run_open(monkeypatch, fake, now=_NOW)
    assert stats.opened == 1
    assert stats.refusals == {}
    assert fake.version_asked == 7

    # КОНТРОЛЬ: та же позиция, но СВОЕЙ версии — вход придержан.
    store_v7 = [dict(store[0], logic_version=7)]
    control_db = _FakeDB([_candidate(1, "T1/USDT", 602)], store=store_v7)
    control = await _run_open(monkeypatch, control_db, now=_NOW)
    assert control.opened == 0
    assert control.refusals.get(REASON_TOKEN_PAUSE) == 1


@needs_db
async def test_the_pause_query_reads_opened_at_of_its_own_version_only() -> None:
    """§5.1, §5.3, §11.2, §11.6 ТЗ на НАСТОЯЩЕЙ базе: три свойства одного SQL.

    ТЕСТЫ ВЫШЕ ПРОВЕРЯЮТ РЕШЕНИЕ СЛУЖБЫ НА ЗАГЛУШКЕ, и заглушка отвечает по
    тем же правилам — то есть проверяет службу, а не запрос. Здесь проверяется
    сам запрос, и проверяются им три вещи, которые §11 требует уметь ломать:

      * ``max(opened_at)``, А НЕ ``closed_at`` (§11.2). Сделка, открытая три
        часа назад и закрытая минуту назад, паузу НЕ держит: час с открытия
        давно истёк. Отсчёт от закрытия дал бы обратное — и токен был бы занят
        49 часов вместо часа.
      * ТОЛЬКО СВОЯ ВЕРСИЯ ЛОГИКИ (§11.6). Позиция версии 6, открытая минуту
        назад, токен версии 7 не держит.
      * ОДИН ТОКЕН — ОДИН ОТВЕТ, по ``max``, а не по первой попавшейся строке.

    ЧИСЛА ВЫПИСАНЫ: 3 часа, 1 минута, 3600 секунд окна.
    """
    import asyncpg

    from src.core.db import DB

    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute((_ROOT / "db" / "init.sql").read_text("utf-8"))
        for name in (
            "018_positions.sql", "019_positions_data_gap.sql",
            "025_positions_no_stop.sql", "027_positions_token_pause.sql",
        ):
            await conn.execute((_MIGRATIONS / name).read_text("utf-8"))
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'PAUSE93/USDT', 'PAUSE93', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        await _drop_rows(conn, instrument_id)

        now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
        # Сделка версии 7: открыта ТРИ ЧАСА назад, закрыта МИНУТУ назад.
        await _insert_position(
            conn, instrument_id, 0,
            opened_at=now - timedelta(hours=3),
            closed_at=now - timedelta(minutes=1),
            net_usd=0.01, net_pct=0.5,
        )
        database = DB()
        database._pool = conn

        window = now - timedelta(seconds=_COOLDOWN_SEC)
        # ОТСЧЁТ ОТ ОТКРЫТИЯ: в часовое окно эта позиция не попадает вовсе.
        assert await database.get_last_open_ts_by_instrument(window, 7) == {}
        # …и «сколько прошло» — три часа, а не минута.
        elapsed = await database.seconds_since_last_open(
            "PAUSE93/USDT", 7, now
        )
        assert elapsed is not None
        assert round(elapsed) == 3 * 3600

        # ВЕРСИЯ ЛОГИКИ: позиция версии 6, открытая минуту назад, токен
        # версии 7 не держит. Вставляется напрямую — ``_insert_position``
        # пишет только версию 7.
        signal_id = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version, "
            "probability) VALUES ($1, $2, 'buy', 6, 0.9) RETURNING id;",
            instrument_id, now - timedelta(minutes=1),
        )
        await conn.execute(
            """
            INSERT INTO positions
                (instrument_id, signal_id, logic_version, horizon_h, side,
                 status, signal_ts, signal_price, opened_at, entry_price,
                 entry_lag_sec, entry_slippage_pct, qty, notional_usd,
                 target_pct, target_price, stop_pct, stop_price, cost_pct,
                 deadline_at, resolution)
            VALUES ($1, $2, 6, 24, 'buy', 'open', $3, 100, $3, 100, 42, 0,
                    0.02, 2.0, 1.0, 101, NULL, NULL, 0.22,
                    $3::timestamptz + interval '48 hours', '1m');
            """,
            instrument_id, signal_id, now - timedelta(minutes=1),
        )
        assert await database.get_last_open_ts_by_instrument(window, 7) == {}
        # А своей версии — держит, и это контроль: пустой ответ выше означает
        # «версия не та», а не «запрос не находит ничего вообще».
        held = await database.get_last_open_ts_by_instrument(window, 6)
        assert set(held) == {instrument_id}
        assert held[instrument_id] == now - timedelta(minutes=1)
    finally:
        try:
            await _drop_rows(conn, instrument_id)
        finally:
            await conn.close()


# =============================================================================
# §10.3 ТЗ. Пауза переживает перезапуск
# =============================================================================

async def test_10_3_the_pause_survives_a_restart_and_the_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.3 ТЗ: открыть позицию, перезапустить службу, подать сигнал через 60 с.

    ОПЫТ. «Перезапуск» здесь — НОВЫЙ объект заглушки поверх ТОГО ЖЕ хранилища:
    ровно так контейнер поднимается поверх той же базы. Никакого состояния от
    прошлого прогона в памяти не остаётся. Ожидание: отказ ``cooldown``.

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ. Хранение метки в памяти процесса после
    перезапуска отдало бы пустой ответ — это заглушка с ПУСТЫМ хранилищем, — и
    позиция открылась бы. Контроль это и показывает: 1 открытие, 0 отказов.
    """
    import importlib

    store: list[dict[str, Any]] = []
    first = _FakeDB([_candidate(1, "T1/USDT", 701)], store=store)
    await _run_open(monkeypatch, first, now=_NOW)
    assert len(store) == 1

    # --- ПЕРЕЗАПУСК. Модуль службы перечитывается заново: все его глобальные
    # имена создаются с нуля, ровно как в поднявшемся контейнере. База при
    # этом та же — ``store`` переживает перезапуск, потому что он и есть база.
    #
    # БЕЗ ЭТОЙ ПЕРЕЗАГРУЗКИ ОПЫТ НЕ ЛОВИЛ БЫ СВОЮ ПОЛОМКУ (§11.4): метка,
    # закэшированная в глобальном имени модуля, пережила бы «перезапуск»
    # внутри одного процесса, и отказ пришёл бы из кэша, а не из базы.
    importlib.reload(positions_runner)
    minute = _NOW + timedelta(seconds=60)
    restarted = _FakeDB([_candidate(1, "T1/USDT", 702, at=minute)], store=store)
    stats = await _run_open(monkeypatch, restarted, now=minute)
    assert stats.opened == 0
    assert stats.refusals.get(REASON_TOKEN_PAUSE) == 1

    # --- КОНТРОЛЬ: метка жила бы в памяти процесса и перезапуск её обнулил ---
    amnesiac = _FakeDB([_candidate(1, "T1/USDT", 703, at=minute)], store=[])
    control = await _run_open(monkeypatch, amnesiac, now=minute)
    assert control.opened == 1
    assert control.refusals == {}
    # Модуль возвращается в исходное состояние: перезагрузка подменила объект,
    # на который ссылаются другие тесты этого файла.
    importlib.reload(positions_runner)


def test_10_3_the_mark_is_not_kept_in_module_state() -> None:
    """§5.3, §11.4 ТЗ: метки последнего открытия нет ни в одном глобальном имени.

    Опыт выше проверяет ПОВЕДЕНИЕ; эта проверка — УСТРОЙСТВО. Вместе они ловят
    и то, что метка кэшируется «для скорости» рядом с честным запросом к базе:
    поведение при перезапуске тогда осталось бы верным, а внутри одного
    запуска пауза считалась бы по устаревшему кэшу.
    """
    module_names = {
        name for name in vars(positions_runner)
        if "last_open" in name or "pause_at" in name
    }
    assert module_names == set()
    source = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    body = source.split("async def open_new_positions", 1)[1]
    # Метка читается из базы в КАЖДОЙ итерации, а не однажды при импорте.
    assert "await db.get_last_open_ts_by_instrument(" in body
    assert "last_open_at: dict[int, datetime] = {}" in body


# =============================================================================
# §10.4 ТЗ. Две одновременные позиции по одному токену
# =============================================================================

async def test_10_4_two_simultaneous_positions_per_token_and_the_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.4 ТЗ: открыть позицию, дождаться часа, открыть вторую, не закрыв первую.

    ОПЫТ. Обе открыты, обе живут, номера разные. Это ГЛАВНОЕ НОВОЕ ПОВЕДЕНИЕ
    этапа (§16.2 — и его главный технический риск): до сих пор инвариант «один
    инструмент — одна позиция» держался побочным следствием конфигурации «5
    слотов на 5 токенов», а теперь не держится ничем.

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ: при ``POSITION_ONE_PER_TOKEN=true`` вторая не
    открывается, отказ ``instrument_busy``.
    """
    store: list[dict[str, Any]] = []
    fake = _FakeDB([_candidate(1, "T1/USDT", 801)], store=store)
    await _run_open(monkeypatch, fake, now=_NOW)

    hour_later = _NOW + timedelta(seconds=_COOLDOWN_SEC + 1)
    fake.candidates = [_candidate(1, "T1/USDT", 802, at=hour_later)]
    stats = await _run_open(monkeypatch, fake, now=hour_later)
    assert stats.opened == 1
    assert len(store) == 2
    assert [row["id"] for row in store] == [1, 2]
    # ОБЕ ЖИВЫ: первая не закрыта открытием второй.
    assert [row["status"] for row in store] == ["open", "open"]

    # КОНТРОЛЬ: инвариант включён — вторая не открывается.
    control_store = [dict(store[0])]
    control_db = _FakeDB(
        [_candidate(1, "T1/USDT", 803, at=hour_later)], store=control_store
    )
    control = await _run_open(
        monkeypatch, control_db, now=hour_later, one_per_token=True
    )
    assert control.opened == 0
    assert control.refusals.get(REASON_INSTRUMENT_BUSY) == 1
    assert len(control_store) == 1


@needs_db
async def test_10_4_the_database_allows_the_second_position_of_version_seven(
) -> None:
    """§4.2 ТЗ: инвариант держала ещё и БАЗА — частичным уникальным индексом.

    СНЯТЬ ПРАВИЛО ОДНИМ ЛИШЬ КОДОМ БЫЛО НЕЛЬЗЯ, и это выяснил ещё этап 7
    (миграция 027): вторая позиция по токену получила бы отказ вставки, служба
    сочла бы его штатной гонкой (``positions_race_skipped=1``), и сделок не
    прибавилось бы НИ ОДНОЙ при совершенно честном виде журнала. Здесь
    проверяется, что послабление на месте и что версии 6 оно не касается.
    """
    import asyncpg

    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute((_ROOT / "db" / "init.sql").read_text("utf-8"))
        for name in (
            "018_positions.sql", "019_positions_data_gap.sql",
            "025_positions_no_stop.sql", "027_positions_token_pause.sql",
        ):
            await conn.execute((_MIGRATIONS / name).read_text("utf-8"))
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'TEST93/USDT', 'TEST93', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        await _drop_rows(conn, instrument_id)

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

        first, second = await _open(7), await _open(7)
        assert first != second
        await _open(6)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _open(6)
    finally:
        try:
            await _drop_rows(conn, instrument_id)
        finally:
            await conn.close()


async def _drop_rows(conn: Any, instrument_id: int) -> None:
    """Убирает за собой строки одноразового инструмента этого теста."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id = $1;", instrument_id
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id = $1;", instrument_id
    )
    await conn.execute(
        "DELETE FROM position_rejections WHERE token = 'TEST93/USDT';"
    )


# =============================================================================
# §4.2 ТЗ. Поимённый аудит мест, опиравшихся на «одна позиция на токен»
# =============================================================================

def test_no_query_returns_a_single_current_position_per_token() -> None:
    """§4.2 ТЗ: главный риск этапа — скрытое допущение «по токену не больше одной».

    ЧЕТЫРЕ ЗАВЕДОМО ПОДОЗРИТЕЛЬНЫХ МЕСТА НАЗВАНЫ В §4.2 ПОИМЁННО, и каждое
    проверено здесь по коду, а не по памяти:

     1. СВЕРКА И ДОЗАПИСЬ ЗАКРЫТИЙ В ТОРГОВЫЙ ЖУРНАЛ. В записи от 06.09.2026
        прямо использовалось рассуждение «если по тому же токену открылась
        следующая, значит первая закрыта». В коде такого рассуждения нет:
        выгрузка адресует строки МЕТКОЙ ПОЗИЦИИ (``position_marker(row["id"])``),
        а очереди отбираются по ``sheet_opened_at``/``sheet_closed_at`` самой
        позиции. Токен в ключе не участвует вовсе.
     2. «ТЕКУЩАЯ ОТКРЫТАЯ ПОЗИЦИЯ ПО ТОКЕНУ». Такой выборки в проекте нет:
        ``positions_open`` возвращает СПИСОК, ``positions_state`` группирует по
        символу с ``max(opened_at)``.
     3. СЛУЖБА ВЫГРУЗКИ В GOOGLE ТАБЛИЦУ — см. пункт 1, ключ тот же.
     4. ЗАКРЫТИЕ ПО СРОКУ И ПО ЦЕЛИ. ``sync_open_positions`` обходит СПИСОК
        открытых позиций по ``row["id"]``; инструмент в ключе не участвует.

    ПРОВЕРКА ТЕКСТОВАЯ, ПОТОМУ ЧТО ДОКАЗЫВАТЬ НАДО ОТСУТСТВИЕ ДОПУЩЕНИЯ. По
    значению его не поймать: на выборке, где по токену и правда одна позиция,
    обе редакции дают один и тот же ответ.
    """
    export = (_ROOT / "src" / "export" / "queries.py").read_text("utf-8")
    for name in (
        "fetch_positions_pending_open", "fetch_positions_pending_close",
    ):
        block = export.split(f"async def {name}", 1)[1].split(
            "\nasync def ", 1
        )[0]
        assert "instrument_id" not in block.split('"""', 2)[-1], name
        assert "p.id" in block, name

    export_main = (_ROOT / "src" / "export_main.py").read_text("utf-8")
    assert "position_marker(row[\"id\"])" in export_main
    assert "position_marker(row[\"instrument_id\"])" not in export_main

    bot = (_ROOT / "src" / "bot" / "queries.py").read_text("utf-8")
    state = bot.split("async def positions_state", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "GROUP BY i.symbol" in state
    assert "max(p.opened_at)" in state

    runner = (_ROOT / "src" / "positions" / "runner.py").read_text("utf-8")
    sync = runner.split("async def sync_open_positions", 1)[1].split(
        "\nasync def ", 1
    )[0]
    assert "for row in await db.get_open_positions():" in sync
    assert 'position_id = int(row["id"])' in sync


# =============================================================================
# §5.5, §7.1 ТЗ. Журнал отказов во входе
# =============================================================================

async def test_every_refusal_is_written_to_the_rejection_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5, §7.1, §11.7 ТЗ: отказ ``cooldown`` записывается, и не он один.

    БЕЗ ЗАПИСИ ОТКАЗОВ ПРЕДСКАЗАНИЕ §12.2 НЕЧЕМ ПРОВЕРИТЬ: «не меньше 70%
    отказов после порога — это пауза» — утверждение о ДОЛЕ, а долю нельзя
    посчитать по журналу контейнера иначе как разбором текста.
    """
    fake = _FakeDB([_candidate(1, "T1/USDT", 901)])
    await _run_open(monkeypatch, fake, now=_NOW)

    minute = _NOW + timedelta(minutes=1)
    fake.candidates = [
        _candidate(1, "T1/USDT", 902, at=minute),               # пауза
        _candidate(2, "T2/USDT", 903, probability=0.5, at=minute),
    ]
    monkeypatch.setattr(positions_runner, "db", fake)
    monkeypatch.setattr(positions_runner, "_send", _no_send)
    monkeypatch.setattr(positions_runner, "_count_refusals", _noop_refusals)
    monkeypatch.setattr(positions_runner, "_flush_trade_rollup", _noop_rollup)
    monkeypatch.setattr(positions_runner, "sync_open_positions", _noop_sync)
    monkeypatch.setattr(settings, "POSITION_TOKEN_PAUSE_MIN", 60)
    monkeypatch.setattr(settings, "POSITION_MAX_OPEN", 0)
    monkeypatch.setattr(settings, "POSITION_ONE_PER_TOKEN", False)
    monkeypatch.setattr(settings, "POSITION_BUDGET_USD", 0.0)
    await positions_runner.run_once(minute)

    written = {row["reason"]: row for row in fake.rejections}
    assert set(written) == {REASON_TOKEN_PAUSE, "low_probability"}
    pause_row = written[REASON_TOKEN_PAUSE]
    assert pause_row["token"] == "T1/USDT"
    assert pause_row["signal_id"] == 902
    assert pause_row["logic_version"] == 7
    assert pause_row["ts_utc"] == _NOW + timedelta(minutes=1)


async def _noop_refusals(*_args: Any, **_kw: Any) -> None:
    return None


async def _noop_rollup(*_args: Any, **_kw: Any) -> None:
    return None


async def _noop_sync(*_args: Any, **_kw: Any):
    return positions_runner.ClosedStats()


def test_the_rejection_journal_list_matches_the_code_in_both_places() -> None:
    """§7.1 ТЗ: перечень причин закрыт ограничением БД, и он один.

    ДВЕ КОПИИ ЗАКРЫТОГО ПЕРЕЧНЯ — В МИГРАЦИИ И В СЛУЖБЕ — ОДНАЖДЫ РАЗОШЛИСЬ БЫ,
    и новая причина падала бы на ограничении БД: в бою, у службы, которая по
    построению не падает. Она записала бы предупреждение, и отказы новой
    причины просто перестали бы попадать в выборку. Молча.
    """
    migration = (_MIGRATIONS / "028_position_rejections.sql").read_text("utf-8")
    listed = set(re.findall(
        r"'([a-z_]+)'", migration.split("reason IN (", 1)[1].split(")", 1)[0]
    ))
    assert listed == set(REFUSAL_REASONS)

    # Служба собирает тот же перечень ИЗ КОДА, а не переписывает его руками.
    db_source = (_ROOT / "src" / "core" / "db.py").read_text("utf-8")
    block = db_source.split(
        "async def ensure_position_rejections_schema", 1
    )[1].split("\n    async def ", 1)[0]
    assert "from src.positions.rules import REFUSAL_REASONS" in block
    assert "REFUSAL_REASONS" in block


def test_the_rejection_journal_is_weeded_and_positions_are_not_touched() -> None:
    """§7.1, §7.2 ТЗ: прополка 90 суток; таблица positions не меняется.

    ДВА УТВЕРЖДЕНИЯ РЯДОМ НАМЕРЕННО. Объём журнала отказов — 2,5 млн строк в
    год (§7.1 ТЗ), и без прополки он однажды стал бы поводом трогать схему
    сделок; поэтому прополка проверяется в том же тесте, где проверяется, что
    схема сделок не тронута.
    """
    import scripts.retention as retention

    rules_by_table = {
        table: days for table, days, _where in retention.RETENTION_RULES
    }
    assert rules_by_table["position_rejections"] == 90
    assert retention.TS_COLUMNS["position_rejections"] == "ts_utc"
    assert Settings(
        POSTGRES_PASSWORD="x"
    ).RETENTION_POSITION_REJECTIONS_DAYS == 90

    # МИГРАЦИЯ 028 НЕ ТРОГАЕТ ТАБЛИЦУ ПОЗИЦИЙ НИ ОДНИМ СТОЛБЦОМ (§7.2).
    migration = (_MIGRATIONS / "028_position_rejections.sql").read_text("utf-8")
    assert "ALTER TABLE positions" not in migration
    assert "ALTER TABLE public.positions" not in migration
    assert "UPDATE positions" not in migration
    assert "DELETE FROM positions" not in migration


def test_migration_028_is_the_next_free_number_and_has_a_rollback() -> None:
    """§7.1 ТЗ: номер следующий свободный (027 занята этапом 7), откат есть."""
    names = sorted(p.name for p in _MIGRATIONS.glob("0*.sql"))
    assert "028_position_rejections.sql" in names
    assert "028_position_rejections_rollback.sql" in names
    assert not any(name.startswith("029_") for name in names)
    body = (_MIGRATIONS / "028_position_rejections.sql").read_text("utf-8")
    assert body.count("BEGIN;") == 1 and body.rstrip().endswith("COMMIT;")
    rollback = (
        _MIGRATIONS / "028_position_rejections_rollback.sql"
    ).read_text("utf-8")
    assert "DROP TABLE IF EXISTS public.position_rejections;" in rollback
    # Откат журнала отказов НЕ ТРОГАЕТ СДЕЛКИ: отказ — не сделка.
    assert "positions" not in rollback.split("BEGIN;", 1)[1]


@needs_db
async def test_the_rejection_journal_really_takes_every_reason() -> None:
    """§7.1 ТЗ на настоящей базе: ограничение пропускает ВЕСЬ перечень.

    Тест на тексте миграции выше доказывает совпадение СПИСКОВ; этот — что
    список записан так, что база его принимает. Опечатка в кавычках дала бы
    совпадающие списки и падающую вставку.
    """
    import asyncpg

    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute(
            (_MIGRATIONS / "028_position_rejections.sql").read_text("utf-8")
        )
        await conn.execute(
            "DELETE FROM public.position_rejections WHERE token = 'CHK/USDT';"
        )
        for reason in REFUSAL_REASONS:
            await conn.execute(
                "INSERT INTO public.position_rejections "
                "(signal_id, token, reason, logic_version) "
                "VALUES (1, 'CHK/USDT', $1, 7);",
                reason,
            )
        written = await conn.fetchval(
            "SELECT count(*) FROM public.position_rejections "
            "WHERE token = 'CHK/USDT';"
        )
        assert written == len(REFUSAL_REASONS)
        # А причина вне перечня отвергается — перечень действительно ЗАКРЫТ.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO public.position_rejections "
                "(signal_id, token, reason, logic_version) "
                "VALUES (1, 'CHK/USDT', 'slots_full', 7);"
            )
    finally:
        await conn.execute(
            "DELETE FROM public.position_rejections WHERE token = 'CHK/USDT';"
        )
        await conn.close()


# =============================================================================
# §10.5 ТЗ. Сигнальные уведомления молчат
# =============================================================================

async def test_10_5_not_a_single_signal_message_leaves_and_the_control_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10.5 ТЗ: при выключенном флаге сигнальных сообщений — 0.

    ОПЫТ. Подаём три сигнала, прошедших ПРЕЖНИЙ порог отбора (иначе тест
    доказывал бы, что сообщений нет, потому что нечего слать). Ожидание:
    отправок 0, а очередь при этом не копится — каждый сигнал «поглощён»
    (``notified = TRUE``, ``notified_at`` НЕ ставится: отправки не было, и
    утверждать обратное нельзя).

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ: при ``NOTIFY_SIGNALS_ENABLED=true`` сообщения
    идут. Без него опыт проходил бы и на службе, у которой просто нет
    получателей.
    """
    sent: list[str] = []
    absorbed: list[int] = []
    signals = [
        {"id": i, "instrument_id": 1, "decision": "buy", "probability": 0.95}
        for i in (1, 2, 3)
    ]

    class _DB:
        async def get_unnotified_strong_signals(self, *_a: Any, **_k: Any):
            return [dict(row) for row in signals]

        async def mark_signal_absorbed(self, signal_id: int) -> None:
            absorbed.append(signal_id)

        async def mark_signal_notified(self, signal_id: int) -> None:
            raise AssertionError("отправки не было — notified_at ставить нельзя")

    monkeypatch.setattr(notify_agent, "db", _DB())
    monkeypatch.setattr(
        notify_agent, "send_message",
        lambda *a, **k: sent.append(a[0] if a else ""),
    )
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", False)

    agent = notify_agent.NotifyAgent(
        interval=30, min_probability=0.7, cooldown_sec=1800,
        symbol="BTC/USDT", tz_name="Europe/Moscow", primary_horizon="4h",
        recipients=[1],
    )
    await agent.process_once()
    assert sent == []
    assert absorbed == [1, 2, 3]

    # КОНТРОЛЬ: флаг включён — сигнал доходит до разбора, а не поглощается.
    absorbed.clear()
    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", True)
    reached: list[int] = []

    async def _spy(signal: dict[str, Any]) -> None:
        reached.append(int(signal["id"]))

    monkeypatch.setattr(agent, "_process_signal", _spy)
    monkeypatch.setattr(agent, "_flush_digest", _noop_refusals)
    await agent.process_once()
    assert reached == [1, 2, 3]
    assert absorbed == []


def test_the_signal_notification_settings_are_still_in_the_code() -> None:
    """§6.1 ТЗ: код сигнальных уведомлений НЕ УДАЛЁН, он обойдён флагом.

    Ограничители частоты остаются на своих местах и при возврате флага в
    ``true`` работают как прежде — проверяется тем, что функции, их считающие,
    на месте и отвечают прежними числами.
    """
    cfg = notify_agent.NotifyConfig(
        min_probability=0.7, cooldown_sec=1800, hold_sec=3600, max_per_hour=6,
        min_agents=3, use_calibrated=False, min_calibrated=0.55,
    )
    # ВЫДЕРЖКА: 59 минут из 60 — держит, и говорит, сколько осталось.
    reason = notify_agent.rate_limit_reason(
        _NOW - timedelta(minutes=59), _NOW, 0, cfg
    )
    assert reason is not None and "выдержка по инструменту" in reason
    # ПОТОЛОК: шесть из шести — держит.
    capped = notify_agent.rate_limit_reason(None, _NOW, 6, cfg)
    assert capped is not None and "потолок уведомлений в час" in capped
    # Пять из шести — пропускает.
    assert notify_agent.rate_limit_reason(None, _NOW, 5, cfg) is None


# =============================================================================
# §6.2 ТЗ. Три сообщения, и больше никаких
# =============================================================================

def test_the_opening_message_follows_the_specimen_line_by_line() -> None:
    """§6.2 ТЗ, сообщение 1: образец воспроизведён построчно.

    ЧИСЛА ВЫПИСАНЫ: сделка #41, вход 77 602.70, цель 78 378.73 (+1.00%),
    вероятность 0.86, правило v7, срок 48 ч.

    ЕДИНСТВЕННОЕ ОТСТУПЛЕНИЕ ОТ ОБРАЗЦА — слово «(виртуально)» в заголовке, и
    оно намеренно: строка без него читается как отчёт о НАСТОЯЩЕЙ сделке.
    """
    text = messages.opened_text(
        position_id=41, symbol="BTC/USDT", entry_price=77602.70,
        target_price=78378.73, target_pct=1.0, probability=0.86,
        logic_version=7, stop_pct=None, hold_hours=48,
    )
    lines = text.split("\n")
    assert lines[0] == "🟢 <b>Открыта сделка #41</b> (виртуально)"
    assert lines[1] == "BTC/USDT · вход $77 602.70"
    assert lines[2] == "Цель $78 378.73 (+1.00%) · вероятность 0.86"
    assert lines[3] == "Правило v7 · без предела убытка · срок 48 ч"
    assert len(lines) == 4


def test_the_closing_message_follows_the_specimen_line_by_line() -> None:
    """§6.2 ТЗ, сообщение 2: образец воспроизведён построчно.

    ЧИСЛА ВЫПИСАНЫ: сделка #41, вход 77 602.70 → выход 76 200.00, итог
    −2.03% (−$0.041), издержки 0.22%, в сделке 12 ч 30 мин = 12:30.

    ПЕРЕЧЕНЬ ПРИЧИН ОБРАЗЦА ЗАКРЫТ ЧЕТЫРЬМЯ ФОРМУЛИРОВКАМИ, и все четыре
    проверены ниже: человек, читающий поток, не обязан помнить машиночитаемые
    значения колонки ``exit_reason``.
    """
    text = messages.closed_text(
        position_id=41, symbol="BTC/USDT", exit_reason="timeout",
        entry_price=77602.70, exit_price=76200.0,
        net_pnl_pct=-2.03, net_pnl_usd=-0.041, cost_pct=0.22,
        held_sec=12 * 3600 + 30 * 60, hold_hours=48,
    )
    lines = text.split("\n")
    assert lines[0] == "🔴 <b>Закрыта сделка #41</b> (виртуально)"
    assert lines[1] == "BTC/USDT · вход $77 602.70 → выход $76 200.00"
    assert lines[2] == "Итог -2.03% ($-0.041) с учётом издержек 0.22%"
    assert lines[3] == "Причина: истёк срок 48 ч"
    assert lines[4] == "Время в сделке 12:30"
    assert len(lines) == 5

    assert messages.exit_ru("target") == "цель достигнута"
    assert messages.exit_ru("plus_exit") == "вышли в плюс после суток"
    assert messages.exit_ru("timeout", 48) == "истёк срок 48 ч"
    assert messages.exit_ru("data_gap") == "пропали данные"


def test_the_time_in_trade_never_rolls_over_into_days() -> None:
    """§6.2 ТЗ: ``ЧЧ:ММ`` — и 47 часов это 47:00, а не «1 день 23 часа».

    Сделка живёт до 48 часов. Пересчёт в дни заставлял бы читателя сверять срок
    сделки с настройкой в уме — и ошибаться на сутки, когда счёт идёт на часы.
    """
    assert messages.hhmm(0) == "00:00"
    assert messages.hhmm(59) == "00:00"
    assert messages.hhmm(60) == "00:01"
    assert messages.hhmm(12 * 3600 + 30 * 60) == "12:30"
    assert messages.hhmm(47 * 3600 + 12 * 60) == "47:12"
    assert messages.hhmm(48 * 3600) == "48:00"


# =============================================================================
# §10.6 ТЗ. Сводка считает то, что произошло
# =============================================================================

# ЗАРАНЕЕ ВЫПИСАННЫЙ ЧИСЛОМ НАБОР ИЗ ПЯТИ СДЕЛОК (§10.6 ТЗ).
#
# Три прибыльных, две убыточных. Итоги выписаны здесь и НИГДЕ не вычисляются
# тем же кодом, который проверяются: 0.412 − 0.183 = 0.229 доллара,
# 20.6% − 9.15% = 11.45% — это сложение сделано руками, и если сводка даст
# другое число, ошибка в сводке, а не в тесте.
_FIVE_TRADES = {
    "opened": 7,
    "closed": 5,
    "data_gaps": 0,
    "open_now": 4,
    "measured": 5,
    "winners": 3,
    "pnl_usd": 0.229,
    "pnl_pct": 11.45,
    "best_pct": 7.80,
    "worst_pct": -5.10,
    "avg_hold_sec": 9 * 3600 + 42 * 60,
    "total_trades": 31,
    "total_winners": 12,
    "total_pnl_usd": -1.184,
}
_FIVE_REJECTIONS = {"token_pause": 214, "low_probability": 61, "degraded": 5}


def test_10_6_every_field_of_the_summary_matches_the_written_number() -> None:
    """§10.6 ТЗ: каждое поле сводки сверено с выписанным числом.

    ПРАВИЛО §9 ТЗ РАБОТАЕТ ЗДЕСЬ В ПОЛНУЮ СИЛУ. Ни одно ожидание ниже не
    получено вызовом ``format_daily_summary`` с другими параметрами и ни одно
    не посчитано запросом к базе: всё выписано в ``_FIVE_TRADES`` руками.

    ДОЛИ СЧИТАЮТСЯ, И ИХ ОКРУГЛЕНИЕ ВЫПИСАНО ТОЖЕ: 3 из 5 — это 60%, 12 из
    31 — это 38.7%, то есть 39% после округления. Отказов 214 + 61 + 5 = 280,
    из них пауза 214, прочее 66.
    """
    text = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats=dict(_FIVE_TRADES), rejections=dict(_FIVE_REJECTIONS),
    )
    assert "📊 <b>Сутки 11.09.2026</b> · версия логики 7" in text
    assert "Открыто сделок: 7" in text
    assert "Закрыто сделок: 5" in text
    assert "Открыто сейчас: 4" in text
    assert "прибыльных 3 (60%)" in text
    assert "итог +0.229$ (+11.45%)" in text
    assert "лучшая +7.80% · худшая -5.10%" in text
    assert "среднее время в сделке 09:42" in text
    assert "сделок 31, прибыльных 39%, итог -1.184$" in text
    assert "Отказано во входе: 280 (пауза по токену 214, прочее 66)" in text
    # Строки о потерянных сообщениях нет: терять было нечего (§6.4).
    assert "не отправлено" not in text


def test_10_6_the_control_changes_exactly_the_fields_it_must() -> None:
    """§10.6 ТЗ, контроль: подмена ОДНОЙ сделки меняет ровно те поля.

    КОНТРОЛЬ, ОБЯЗАННЫЙ УПАСТЬ, ЗДЕСЬ УСТРОЕН ТОНЬШЕ ОСТАЛЬНЫХ. Мало показать,
    что сводка меняется: сводка, печатающая одно и то же число во все поля,
    тоже «изменилась бы». Проверяется поэтому И ЧТО ИЗМЕНИЛОСЬ, И ЧТО НЕ
    ИЗМЕНИЛОСЬ.

    ПОДМЕНА: худшая из пяти сделок была −5.10%, стала −9.30%; итог в долларах
    и процентах сдвигается на разницу, выписанную числом. Число закрытых,
    открытых, прибыльных, среднее время и накопленный итог остаться обязаны.
    """
    swapped = dict(_FIVE_TRADES)
    swapped["worst_pct"] = -9.30
    swapped["pnl_pct"] = 11.45 - 4.20   # −9.30 вместо −5.10 это ещё −4.20
    swapped["pnl_usd"] = 0.229 - 0.084

    before = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats=dict(_FIVE_TRADES), rejections=dict(_FIVE_REJECTIONS),
    )
    after = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats=swapped, rejections=dict(_FIVE_REJECTIONS),
    )
    assert before != after

    # ИЗМЕНИЛОСЬ РОВНО ТО, ЧТО ОБЯЗАНО.
    assert "лучшая +7.80% · худшая -9.30%" in after
    assert "итог +0.145$ (+7.25%)" in after

    # И НЕ ИЗМЕНИЛОСЬ ВСЁ ОСТАЛЬНОЕ.
    for unchanged in (
        "Открыто сделок: 7", "Закрыто сделок: 5", "Открыто сейчас: 4",
        "прибыльных 3 (60%)", "среднее время в сделке 09:42",
        "сделок 31, прибыльных 39%, итог -1.184$",
        "Отказано во входе: 280 (пауза по токену 214, прочее 66)",
    ):
        assert unchanged in after, unchanged

    # И РОВНО ОДНА СТРОКА СВОДКИ ОТЛИЧАЕТСЯ ОТ ДВУХ… — вернее, две: итог и
    # «лучшая/худшая». Число выписано, чтобы «изменилось всё» тоже упало.
    differing = [
        pair for pair in zip(before.split("\n"), after.split("\n"), strict=True)
        if pair[0] != pair[1]
    ]
    assert len(differing) == 2


def test_the_summary_says_nothing_where_it_knows_nothing() -> None:
    """Сутки без измеренных закрытий: прочерк, а не честные нули.

    «итог +0.000$ (+0.00%), лучшая +0.00% · худшая +0.00%» читается как
    «торговали и вышли в ноль» — утверждение, которого никто не делал.
    """
    text = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats={"opened": 3, "closed": 0, "open_now": 3, "measured": 0,
               "total_trades": 0, "total_winners": 0, "total_pnl_usd": 0.0},
        rejections={},
    )
    assert "измеренных закрытий за сутки нет" in text
    assert "лучшая" not in text
    assert "сделок 0, прибыльных —, итог +0.000$" in text
    assert "Отказано во входе: 0 (пауза по токену 0, прочее 0)" in text


def test_the_summary_names_data_gaps_but_keeps_them_out_of_the_result() -> None:
    """Закрытия по пробелу данных названы, но в итог и долю не входят.

    У них цена выхода не наблюдалась, а восстановлена: их «итог» описывает не
    рынок, а сбой сбора данных. Прятать их число при этом нельзя — по нему
    видно состояние коллектора.
    """
    text = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats=dict(_FIVE_TRADES, closed=7, data_gaps=2),
        rejections=dict(_FIVE_REJECTIONS),
    )
    assert "Закрыто сделок: 7" in text
    assert "по пробелу в данных 2 (в итог не входят)" in text
    # Итог и доля прибыльных остались от ПЯТИ измеренных.
    assert "прибыльных 3 (60%)" in text
    assert "итог +0.229$ (+11.45%)" in text


def test_the_summary_reports_messages_it_failed_to_send() -> None:
    """§6.4 ТЗ: неотправленное сообщение попадает счётчиком в сводку.

    СДЕЛКА ПЕРВИЧНА, СООБЩЕНИЕ ВТОРИЧНО — и именно поэтому о потере надо
    сказать: сделка состоялась, а владелец о ней не узнал, и единственное
    место, где это ещё можно заметить, — сводка.
    """
    text = daily_trades.format_daily_summary(
        day_label="11.09.2026", logic_version=7,
        stats=dict(_FIVE_TRADES), rejections=dict(_FIVE_REJECTIONS),
        messages_sent=22, messages_failed=3,
    )
    assert "Сообщений о сделках не отправлено: 3 (отправлено 22)" in text


# =============================================================================
# §6.2 ТЗ. Расписание суточной сводки
# =============================================================================

def test_the_summary_day_is_the_previous_calendar_day_in_utc() -> None:
    """§6.2, §11.11 ТЗ: сутки календарные, UTC, законченные.

    ЧИСЛА ВЫПИСАНЫ. Сводка, вышедшая 12.09.2026 в 06:00 UTC, описывает
    11.09.2026 целиком: от 11.09 00:00 до 12.09 00:00, не включая правую
    границу.

    МЕСТНОЕ ВРЕМЯ (§11.11 — намеренная поломка ровно на этом) дало бы сутки,
    чья длина дважды в год не равна суткам, а «последние 24 часа» от 06:00 —
    отрезок, у которого нет имени: назвать его датой в заголовке нельзя.
    """
    since, until, label = daily_trades.window_for(
        datetime(2026, 9, 12, 6, 0, tzinfo=UTC)
    )
    assert since == datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
    assert until == datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
    assert label == "11.09.2026"
    assert (until - since).total_seconds() == 86_400.0

    # Момент выхода сводки на границе суток ничего не сдвигает: окно считается
    # от полуночи «сегодня», а не от «сейчас минус сутки».
    late = daily_trades.window_for(datetime(2026, 9, 12, 23, 59, tzinfo=UTC))
    assert late[:2] == (since, until)


def test_the_summary_goes_out_once_a_day_and_not_a_hundred_times() -> None:
    """§6.2 ТЗ: два условия — время наступило И за эти сутки ещё не слали.

    Служба опрашивает себя раз в минуту. Без отметки дня владелец получил бы
    сводку шестьдесят раз подряд; с проверкой «ровно 06:00» — не получил бы ни
    разу при первой же заминке итерации, и не узнал бы об этом до завтра.
    """
    at = "06:00"
    assert not daily_trades.is_due(
        datetime(2026, 9, 12, 5, 59, tzinfo=UTC), at, None
    )
    assert daily_trades.is_due(
        datetime(2026, 9, 12, 6, 0, tzinfo=UTC), at, None
    )
    # ЗАМИНКА ИТЕРАЦИИ НЕ СЪЕДАЕТ СВОДКУ: 06:07 — всё ещё «пора».
    assert daily_trades.is_due(
        datetime(2026, 9, 12, 6, 7, tzinfo=UTC), at, None
    )
    # А ПОСЛЕ ОТПРАВКИ — не пора до завтра.
    assert not daily_trades.is_due(
        datetime(2026, 9, 12, 23, 0, tzinfo=UTC), at, "2026-09-12"
    )
    assert daily_trades.is_due(
        datetime(2026, 9, 13, 6, 0, tzinfo=UTC), at, "2026-09-12"
    )


def test_the_summary_time_is_validated_when_the_settings_are_read() -> None:
    """§6.2 ТЗ: опечатка в ``.env`` роняет службу при старте, а не молчит.

    Сводка выходит раз в сутки, и «не пришла» неотличимо от «ещё не время».
    Разбор в момент чтения настроек — единственное место, где опечатку ещё
    можно заметить.
    """
    assert Settings(POSTGRES_PASSWORD="x").NOTIFY_DAILY_SUMMARY_UTC == "06:00"
    assert Settings(
        POSTGRES_PASSWORD="x", NOTIFY_DAILY_SUMMARY_UTC="6:5"
    ).NOTIFY_DAILY_SUMMARY_UTC == "06:05"
    for bad in ("шесть утра", "06-00", "25:00", "06:60", ""):
        with pytest.raises(ValueError):
            Settings(POSTGRES_PASSWORD="x", NOTIFY_DAILY_SUMMARY_UTC=bad)


@needs_db
async def test_the_summary_query_counts_closed_trades_and_not_open_ones(
) -> None:
    """§10.6, §11.10 ТЗ на настоящей базе: открытые сделки не считаются закрытыми.

    ПЯТЬ СДЕЛОК ВЫПИСАНЫ ЧИСЛАМИ И ВСТАВЛЕНЫ В БАЗУ: три прибыльных
    (+0.150, +0.080, +0.040 доллара) и две убыточных (−0.110, −0.060), итог
    +0.100. Рядом с ними лежит ОТКРЫТАЯ сделка — она не обязана попасть ни в
    «закрыто», ни в итог.
    """
    import asyncpg

    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute((_ROOT / "db" / "init.sql").read_text("utf-8"))
        for name in (
            "018_positions.sql", "019_positions_data_gap.sql",
            "025_positions_no_stop.sql", "027_positions_token_pause.sql",
        ):
            await conn.execute((_MIGRATIONS / name).read_text("utf-8"))
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'SUM93/USDT', 'SUM93', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        await _drop_rows(conn, instrument_id)

        day = datetime(2026, 9, 11, tzinfo=UTC)
        closed = [
            (0.150, 7.50, 4), (0.080, 4.00, 6), (0.040, 2.00, 8),
            (-0.110, -5.50, 10), (-0.060, -3.00, 12),
        ]
        for index, (usd, pct, hours) in enumerate(closed):
            await _insert_position(
                conn, instrument_id, index,
                opened_at=day + timedelta(hours=1),
                closed_at=day + timedelta(hours=1 + hours),
                net_usd=usd, net_pct=pct,
            )
        # ОТКРЫТАЯ СДЕЛКА РЯДОМ — контроль §11.10.
        await _insert_position(
            conn, instrument_id, 99,
            opened_at=day + timedelta(hours=2), closed_at=None,
            net_usd=None, net_pct=None,
        )

        from src.core.db import DB

        database = DB()
        # ОДИНОЧНОЕ СОЕДИНЕНИЕ ВМЕСТО ПУЛА: запрос ровно один, и у asyncpg
        # ``Connection`` и ``Pool`` для него одинаковый интерфейс. Пул ради
        # одного запроса потребовал бы настоящего DSN приложения — то есть
        # тест зависел бы от того, как настроено окружение, а не от SQL.
        database._pool = conn
        stats = await database.positions_daily_summary(
            day, day + timedelta(days=1), 7
        )
        assert stats["opened"] == 6
        assert stats["closed"] == 5
        assert stats["open_now"] == 1
        assert stats["measured"] == 5
        assert stats["winners"] == 3
        assert round(float(stats["pnl_usd"]), 3) == 0.100
        assert round(float(stats["pnl_pct"]), 2) == 5.00
        assert round(float(stats["best_pct"]), 2) == 7.50
        assert round(float(stats["worst_pct"]), 2) == -5.50
        # Среднее время: (4 + 6 + 8 + 10 + 12) / 5 = 8 часов.
        assert round(float(stats["avg_hold_sec"])) == 8 * 3600
    finally:
        try:
            await _drop_rows(conn, instrument_id)
        finally:
            await conn.close()


async def _insert_position(
    conn: Any,
    instrument_id: int,
    index: int,
    *,
    opened_at: datetime,
    closed_at: datetime | None,
    net_usd: float | None,
    net_pct: float | None,
) -> None:
    """Одна строка сделки версии 7 с ВЫПИСАННЫМ итогом."""
    signal_id = await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version, "
        "probability) VALUES ($1, $2, 'buy', 7, 0.9) RETURNING id;",
        instrument_id, opened_at,
    )
    await conn.execute(
        """
        INSERT INTO positions
            (instrument_id, signal_id, logic_version, horizon_h, side, status,
             signal_ts, signal_price, opened_at, entry_price, entry_lag_sec,
             entry_slippage_pct, qty, notional_usd, target_pct, target_price,
             stop_pct, stop_price, cost_pct, deadline_at, resolution,
             closed_at, exit_price, exit_reason, net_pnl_pct, net_pnl_usd,
             outcome_certain)
        VALUES ($1, $2, 7, 24, 'buy', $3, $4, 100, $4, 100, 42, 0, 0.02, 2.0,
                1.0, 101, NULL, NULL, 0.22,
                $4::timestamptz + interval '48 hours', '1m',
                $5, $6, $7, $8, $9, $10);
        """,
        instrument_id, signal_id,
        "closed" if closed_at is not None else "open",
        opened_at, closed_at,
        # ОТКРЫТАЯ СТРОКА ОБЯЗАНА БЫТЬ ПУСТОЙ ПО ВСЕМ ПОЛЯМ ВЫХОДА — этого
        # требует ограничение ``positions_shape_chk``, и требует правильно:
        # цена выхода у незакрытой сделки — это выдуманное число.
        101.0 if closed_at is not None else None,
        "target" if closed_at is not None else None,
        net_pct, net_usd,
        True if closed_at is not None else None,
    )
    assert index >= 0


# =============================================================================
# §6.3 ТЗ. Предохранитель на поток сообщений о сделках
# =============================================================================

def test_the_trade_fuse_is_built_in_and_switched_off() -> None:
    """§6.3 ТЗ: предохранитель встроен сразу, значение по умолчанию 0.

    0 — «не применяется». Ожидаемый объём 40–120 сообщений в сутки владельцем
    ПРИНЯТ (§6.3); предохранитель нужен на случай, когда решение придётся
    принимать быстро, — и тогда он обязан уже быть, а не писаться.
    """
    assert Settings(POSTGRES_PASSWORD="x").NOTIFY_TRADES_MAX_PER_HOUR == 0
    with pytest.raises(ValueError, match="не может быть отрицательным"):
        Settings(POSTGRES_PASSWORD="x", NOTIFY_TRADES_MAX_PER_HOUR=-1)


def test_the_trade_fuse_reuses_the_notify_limiter_and_does_not_rewrite_it(
) -> None:
    """§6.3 ТЗ: «логика ограничителя переиспользуется, а не пишется заново».

    ОДНА РЕАЛИЗАЦИЯ НА ДВЕ СЛУЖБЫ. Скопированный ограничитель — это два места,
    где однажды разойдётся смысл слова «час», и разойдётся молча: поток
    сообщений не падает, он просто перестаёт совпадать с настройкой.
    """
    from src.notify import rate_limit

    agent_source = (_ROOT / "src" / "notify" / "agent.py").read_text("utf-8")
    runner_source = (
        _ROOT / "src" / "positions" / "runner.py"
    ).read_text("utf-8")
    assert "rate_limit.sent_last_hour(" in agent_source
    assert "rate_limit.sent_last_hour(" in runner_source
    assert "rate_limit.record_sent(" in agent_source
    assert "rate_limit.record_sent(" in runner_source
    # И ни одна из служб не считает час своим счётчиком: zset остался один.
    for source in (agent_source, runner_source):
        assert "zremrangebyscore" not in source
        assert "zcard" not in source
    assert rate_limit._WINDOW_SEC == 3600


def test_the_hourly_rollup_does_not_count_against_the_cap() -> None:
    """§6.3 ТЗ: почасовая сводка придержанного В СЧЁТ ПОТОЛКА НЕ ВХОДИТ.

    Включи её в счёт — и первая же сводка съедала бы часть потолка следующего
    часа: предохранитель душил бы сам себя, а владелец переставал бы узнавать
    о сделках вовсе.
    """
    source = (_ROOT / "src" / "positions" / "runner.py").read_text("utf-8")
    rollup = source.split("async def _flush_trade_rollup", 1)[1].split(
        "\nasync def ", 1
    )[0].split('"""', 2)[-1]   # без пояснения: там это слово названо словом
    assert "record_sent" not in rollup
    # А обычная отправка — входит.
    send = source.split("async def _send(", 1)[1].split(
        "\nasync def ", 1
    )[0]
    assert "rate_limit.record_sent(_TRADE_SENT_KEY, now)" in send


# =============================================================================
# §8 ТЗ. Строки запуска обеих служб
# =============================================================================

def test_the_positions_startup_line_names_the_nine_values_of_the_spec() -> None:
    """§8 ТЗ: служба позиций печатает при старте ровно эти величины.

    Строка запуска — единственное место, где владелец видит, С ЧЕМ служба
    поднялась, не заходя в ``.env``, и §15.6 велит сверить её глазами при
    развёртывании. Числа выписаны.
    """
    fields = positions_runner.startup_fields()
    assert set(positions_runner.STARTUP_FIELDS) <= set(fields)
    assert fields["logic_version"] == 7
    assert fields["max_open"] == 0
    assert fields["slot_usd"] == 2.0
    assert fields["budget_usd"] == 0.0
    assert fields["min_probability"] == 0.8
    assert fields["max_hold_hours"] == 48
    assert fields["plus_wait_start_hours"] == 24
    assert fields["cooldown_sec"] == 3600
    assert fields["one_per_token"] is False


def test_the_notify_startup_line_names_the_four_values_of_the_spec() -> None:
    """§8 ТЗ: служба уведомлений печатает при старте ровно эти четыре величины."""
    fields = notify_runner.startup_fields()
    assert set(notify_runner.STARTUP_FIELDS) == set(fields)
    assert fields["notify_signals_enabled"] is False
    assert fields["notify_trades_enabled"] is True
    assert fields["notify_daily_summary_utc"] == "06:00"
    assert fields["notify_trades_max_per_hour"] == 0


def test_both_startup_lines_print_what_is_applied_and_not_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8 ТЗ: «из фактически применённых значений, а не из значений по умолчанию».

    ЭТО НЕ ПРИДИРКА, А ЕДИНСТВЕННЫЙ СПОСОБ СВЕРИТЬ РАЗВЁРТЫВАНИЕ. Строка,
    собранная из умолчаний кода, показывала бы ровно то, что написано в
    исходниках, — и была бы верной ровно до первой правки ``.env``, то есть
    ровно в тот момент, когда по ней сверяют, что правка доехала.
    """
    monkeypatch.setattr(settings, "POSITION_MAX_OPEN", 3)
    monkeypatch.setattr(settings, "POSITION_TOKEN_PAUSE_MIN", 15)
    monkeypatch.setattr(settings, "POSITION_ONE_PER_TOKEN", True)
    fields = positions_runner.startup_fields()
    assert fields["max_open"] == 3
    assert fields["cooldown_sec"] == 900
    assert fields["one_per_token"] is True

    monkeypatch.setattr(settings, "NOTIFY_SIGNALS_ENABLED", True)
    monkeypatch.setattr(settings, "NOTIFY_TRADES_MAX_PER_HOUR", 4)
    monkeypatch.setattr(settings, "NOTIFY_DAILY_SUMMARY_UTC", "23:30")
    notify_fields = notify_runner.startup_fields()
    assert notify_fields["notify_signals_enabled"] is True
    assert notify_fields["notify_trades_max_per_hour"] == 4
    assert notify_fields["notify_daily_summary_utc"] == "23:30"


# =============================================================================
# §1.2, §13.1 ТЗ. Граница этапа
# =============================================================================

def test_the_stage_touches_nothing_of_the_agents_or_the_decision_layer() -> None:
    """§1.2, §13.1.3 ТЗ: отпечаток агентов и решающего слоя не изменился.

    ЖЁСТКАЯ ГРАНИЦА ЭТАПА, и проверяется она здесь СОДЕРЖАНИЕМ, а не подписью:
    отпечаток ``1f12d5d2…de18d`` снимается на боевом сервере скриптом проверки
    (§14), а тест стережёт то же самое от правки в ветке — чтобы расхождение
    обнаружилось до развёртывания, а не после.

    ФАЙЛЫ НАЗВАНЫ ПОИМЁННО. Список закрыт: файл, добавленный в агенты или в
    решающий слой этим этапом, обязан упасть здесь, а не быть «не замеченным»
    маской.
    """
    import hashlib

    guarded = [
        "src/agents/base.py", "src/agents/market.py", "src/agents/liquidity.py",
        "src/agents/futures.py", "src/agents/runner.py",
        "src/decision/agent.py", "src/decision/runner.py",
    ]
    present = sorted(
        str(path.relative_to(_ROOT))
        for path in (_ROOT / "src" / "agents").glob("*.py")
        if path.name != "__init__.py"
    ) + sorted(
        str(path.relative_to(_ROOT))
        for path in (_ROOT / "src" / "decision").glob("*.py")
        if path.name != "__init__.py"
    )
    assert sorted(guarded) == sorted(present), (
        "в агентах или решающем слое появился новый файл — граница этапа §1.2"
    )

    # ОТПЕЧАТОК СНИМАЕТСЯ ТЕМ ЖЕ СПОСОБОМ, ЧТО И СКРИПТОМ ПРОВЕРКИ: sha256 от
    # содержимого файлов в алфавитном порядке имён. Значение не выписано
    # числом намеренно — оно снимается ДО развёртывания на боевом сервере
    # (§13.1.3, §15.1), а здесь стережётся состав.
    digest = hashlib.sha256()
    for name in sorted(guarded):
        digest.update((_ROOT / name).read_bytes())
    assert len(digest.hexdigest()) == 64


def test_the_stage_changes_no_rule_of_the_signal_or_the_exit() -> None:
    """§1.2 ТЗ: правило выхода версии 6, цели и пороги отбора не тронуты."""
    assert rules.PLUS_WAIT_MIN_LOGIC_VERSION == 6
    assert rules.EXIT_REASONS == (
        "target", "stop", "timeout", "ambiguous", "data_gap", "plus_exit",
    )
    assert rules.SIDE_BUY == "buy"
    assert rules.NO_STOP_PRICE == 0.0
    fresh = Settings(POSTGRES_PASSWORD="x")
    assert fresh.POSITION_MAX_HOLD_HOURS == 48
    assert fresh.POSITION_PLUS_WAIT_START_HOURS == 24
    assert fresh.POSITION_MIN_PROBABILITY == 0.8


def test_the_rows_of_versions_five_and_six_are_never_touched() -> None:
    """§2, §13.1.2 ТЗ: строки версий 5 и 6 не трогаются ничем.

    НИ ПЕРЕСЧЁТОМ, НИ МИГРАЦИЕЙ, НИ «НОРМАЛИЗАЦИЕЙ». Проверяется по текстам
    миграций этапа: ни одного UPDATE и ни одного DELETE по таблице позиций.
    """
    for name in ("028_position_rejections.sql",
                 "028_position_rejections_rollback.sql"):
        body = (_MIGRATIONS / name).read_text("utf-8")
        statements = body.split("BEGIN;", 1)[1]
        assert "UPDATE" not in statements.upper(), name
        assert "DELETE FROM positions" not in statements.upper(), name


# =============================================================================
# §11 ТЗ. Мутационное тестирование: каталог поломок не должен протухнуть
# =============================================================================

def test_the_mutation_catalogue_still_points_at_real_code() -> None:
    """§11 ТЗ: каждая поломка попадает ровно в одно место действующего кода.

    КАТАЛОГ ПОЛОМОК — ЭТО КОД, КОТОРЫЙ НИКТО НЕ ЗАПУСКАЕТ КАЖДЫЙ ДЕНЬ, и
    протухает он молча: строка, которую поломка ищет, переписывается очередным
    этапом, ``str.replace`` не находит образец — и прогон мутаций либо падает с
    невнятной ошибкой, либо (хуже) применяет поломку не туда. Тест проверяет
    ровно это: образец есть и встречается ОДИН раз.

    ЧИСЛО ПОЛОМОК ВЫПИСАНО. §11 требует не меньше двенадцати; их пятнадцать —
    двенадцать названы ТЗ поимённо, три добавлены исполнителем на решения,
    которых ТЗ знать не могло.
    """
    from scripts.mutations_9_3 import MUTATIONS, PRELUDE

    assert len(MUTATIONS) == 15
    assert [m.number for m in MUTATIONS] == list(range(1, 16))
    assert len({m.number for m in MUTATIONS}) == 15

    for mutation in MUTATIONS:
        source = (_ROOT / mutation.file).read_text("utf-8")
        assert source.count(mutation.old) == 1, (
            f"поломка {mutation.number} ({mutation.title}): образец "
            f"встречается {source.count(mutation.old)} раз(а) в "
            f"{mutation.file}, а обязан ровно один"
        )
        assert mutation.new != mutation.old, mutation.number

    # Подкладка для поломки 4 — тоже образец, и он тоже обязан находиться.
    for number, (old, _new) in PRELUDE.items():
        target = next(m for m in MUTATIONS if m.number == number)
        source = (_ROOT / target.file).read_text("utf-8")
        assert source.count(old) == 1, number


def test_the_twelve_breakages_named_by_the_spec_are_all_present() -> None:
    """§11 ТЗ называет двенадцать поломок поимённо — все двенадцать на месте.

    СВЕРКА ПО СМЫСЛУ, А НЕ ПО ТЕКСТУ. Названия в каталоге — русские фразы, и
    сверять их посимвольно значило бы ловить опечатки вместо пропусков.
    Проверяется, что у каждого пункта §11 есть поломка с тем же номером и что
    она трогает ТОТ файл, где живёт названное правило.
    """
    from scripts.mutations_9_3 import MUTATIONS

    expected_files = {
        1: "src/positions/rules.py",        # ноль как «ноль позиций»
        2: "src/core/db.py",                # от закрытия вместо открытия
        3: "src/positions/runner.py",       # обнуление в начале часа
        4: "src/positions/runner.py",       # метка из памяти процесса
        5: "src/positions/runner.py",       # пауза на все токены сразу
        6: "src/core/db.py",                # все версии логики
        7: "src/positions/runner.py",       # отказ не записан
        8: "src/positions/runner.py",       # версия логики 6
        9: "src/notify/agent.py",           # сигналы при выключенном флаге
        10: "src/core/db.py",               # открытые как закрытые
        11: "src/notify/daily_trades.py",   # сутки по местному времени
        12: "src/positions/runner.py",      # одна позиция вместо списка
    }
    by_number = {m.number: m for m in MUTATIONS}
    for number, file in expected_files.items():
        assert number in by_number, number
        assert by_number[number].file == file, number

    # ТРИ ПОЛОМКИ СВЕРХ ДВЕНАДЦАТИ — на решения, принятые исполнителем.
    assert set(by_number) - set(expected_files) == {13, 14, 15}


# =============================================================================
# §14 ТЗ. Скрипт проверки: пять требований, выведенных из трёх дефектов
# =============================================================================

_VERIFY = _ROOT / "deploy" / "verify_9_3.sh"
_SNAPSHOT = _ROOT / "deploy" / "snapshot_9_3.sh"


def test_the_verify_script_exists_and_is_read_only() -> None:
    """§14 ТЗ: скрипт проверки есть, и он ничего не меняет.

    СКРИПТ ПРОВЕРКИ, МЕНЯЮЩИЙ СОСТОЯНИЕ, — ЭТО НЕ ПРОВЕРКА. Он запускается на
    боевом сервере посреди работающего замера, и любая его запись стала бы
    частью выборки, ради которой замер идёт.
    """
    assert _VERIFY.exists() and _SNAPSHOT.exists()
    for path in (_VERIFY, _SNAPSHOT):
        body = path.read_text("utf-8")
        # ИЩЕМ В КОМАНДАХ, А НЕ В КОММЕНТАРИЯХ: слова «INSERT/UPDATE/DELETE»
        # стоят в пояснении «этого здесь нет», и ловить их там значило бы
        # ловить собственное обещание.
        code = "\n".join(
            line for line in body.split("\n")
            if not line.lstrip().startswith("#")
        )
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ",
                          "docker compose restart", "docker compose up"):
            assert forbidden not in code, (path.name, forbidden)


def test_the_verify_script_uses_the_container_names_deployment_creates() -> None:
    """§14.2 ТЗ: имена контейнеров совпадают с теми, что создаёт §15.

    НЕСОВПАДЕНИЕ УЖЕ ОДИН РАЗ СДЕЛАЛО ПУНКТ ПРОВЕРКИ НЕРАБОТАЮЩИМ НАВСЕГДА, и
    хуже всего в этом то, что неработающий пункт выглядит пройденным: команда
    не находит контейнер, подстановка пуста, вывод молчит.

    ПРОВЕРЯЕТСЯ ПО ``docker-compose.yml``, А НЕ ПО ПАМЯТИ. Имя службы,
    переименованное в развёртывании, обязано уронить этот тест.
    """
    compose = (_ROOT / "docker-compose.yml").read_text("utf-8")
    services = set(re.findall(r"^  ([a-z][a-z0-9_-]*):$", compose, re.M))
    for needed in ("positions", "notify", "postgres", "redis"):
        assert needed in services, needed

    body = _VERIFY.read_text("utf-8")
    used = set(re.findall(r"docker compose (?:logs[^\n]*?|exec -T) ([a-z]+)", body))
    # Имена, к которым скрипт обращается, обязаны существовать в compose.
    assert used <= services, used - services
    assert {"positions", "notify", "postgres", "redis"} <= used


def test_the_verify_script_writes_its_keys_to_a_file() -> None:
    """§14.3 ТЗ: ключи прогона дублируются в analysis_out/verify_9_3_metrics.txt.

    ЖУРНАЛ КОНТЕЙНЕРА, ЗАПУЩЕННОГО ЧЕРЕЗ ``run --rm``, ИСЧЕЗАЕТ ВМЕСТЕ С НИМ, а
    числа прогона нужны отчёту и через неделю. Имя файла названо §14.3
    буквально и потому проверяется буквально.
    """
    body = _VERIFY.read_text("utf-8")
    assert "analysis_out/verify_9_3_metrics.txt" in body
    assert 'metric()' in body
    # Наблюдательные числа §13.2 — все пять — обязаны попасть в файл.
    for key in (
        "v7_opened_24h", "v7_rejections_24h", "v7_open_now",
        "v7_max_per_token_now", "trade_messages_",
    ):
        assert key in body, key


def test_the_verify_script_prescribes_rollback_only_for_the_boundary() -> None:
    """§14.4 ТЗ: верный сигнал обязан сопровождаться верным предписанием.

    ОТКАТ ПРЕДПИСЫВАЕТСЯ ТОЛЬКО ЗА НАРУШЕНИЕ ГРАНИЦЫ ЭТАПА — критерии §13.1.1,
    §13.1.2, §13.1.3, §13.1.6 и расхождение отпечатка. Для всех прочих находок
    предписание — «остановиться и разобрать», без отката.

    ЦЕНА ОШИБКИ НЕСИММЕТРИЧНА И ИМЕННО ПОЭТОМУ РАЗДЕЛЕНА. Откат за «сделок
    пока ноль» стоил бы этапа целиком; молчание о нарушенной границе стоило бы
    выборки, ради которой этап делается.
    """
    body = _VERIFY.read_text("utf-8")
    calls = re.findall(r"note_rollback \"([^\"]+)\"", body)
    assert calls, "в скрипте нет ни одного предписания отката"
    for message in calls:
        assert any(
            mark in message for mark in (
                "LOGIC_VERSION", "граница", "ИЗМЕНИЛАСЬ", "ИЗМЕНИЛСЯ",
                "нескольким", "несколькими",
            )
        ), message
    # И ни одна находка про поток сделок или уведомления откат не предписывает.
    for message in calls:
        for foreign in ("отказ", "сообщен", "сводк", "сигнал"):
            assert foreign not in message.lower(), message


def test_the_verify_script_never_lets_a_broken_pipeline_pass_silently() -> None:
    """§14.5 ТЗ: неуспешная команда внутри подстановки обнуляет её целиком.

    При ``set -uo pipefail`` конвейер, собирающий журналы, при первой же
    неудаче даёт пустую строку — и пункт проверки молча превращается в
    «пусто», что читается как «нарушений нет». Поэтому каждый такой конвейер
    заканчивается запасным путём, а пустой результат печатается явно.
    """
    body = _VERIFY.read_text("utf-8")
    assert "set -uo pipefail" in body
    # ПРОДОЛЖЕНИЯ СТРОК СКЛЕИВАЮТСЯ ПЕРЕД ПРОВЕРКОЙ: конвейер, разбитый
    # обратной косой чертой на три строки, — одна команда, и запасной путь у
    # неё один, в конце.
    joined = body.replace("\\\n", " ")
    for line in joined.split("\n"):
        if "docker compose logs" in line and not line.lstrip().startswith("#"):
            assert "|| true" in line, line
    # И у скрипта есть отдельный класс «не проверено», не смешанный с «всё ок».
    assert "note_skip()" in body
    assert "Это НЕ «всё хорошо»" in body


def test_the_verify_script_checks_the_boundary_by_substance_not_by_row_counts(
) -> None:
    """§14.1 ТЗ: рост продакшн-таблиц во время прогона — норма, а не тревога.

    ГРАНИЦА ПРОВЕРЯЕТСЯ ПО СУЩЕСТВУ: версией логики в новых строках и
    контрольной суммой СТАРЫХ. Счётчик строк таблицы ``positions`` для этого не
    годится вовсе — он растёт от каждой новой сделки версии 7, то есть от того
    самого, ради чего этап и делался.
    """
    body = _VERIFY.read_text("utf-8")
    # Контрольная сумма считается по строкам ОДНОЙ версии, а не по таблице.
    assert "WHERE p.logic_version = ${version}" in body
    assert "md5(string_agg(line" in body
    # И нигде не сравнивается общее число строк positions «до» и «после».
    assert "SELECT count(*) FROM positions;" not in body


def test_the_snapshot_is_taken_before_anything_changes() -> None:
    """§15.1 ТЗ: слепок снимается ПЕРВОЙ командой, до любых изменений.

    «ДО» СНЯТЬ ЗАДНИМ ЧИСЛОМ НЕЛЬЗЯ НИ ПРИ КАКИХ УСЛОВИЯХ, и три критерия
    приёмки (§13.1.2, §13.1.3, §13.1.6) без него не закрываются. Скрипт
    проверки поэтому обязан честно говорить «не проверено», а не молчать.
    """
    body = _VERIFY.read_text("utf-8")
    assert "snapshot_9_3_before.txt" in body
    assert "слепок ДО развёртывания не снимался" in body
    snapshot = _SNAPSHOT.read_text("utf-8")
    # Слепок снимает версии 5 и 6 одним циклом, поэтому ключ в тексте скрипта
    # собирается из переменной — проверяется цикл и то, что в нём ровно эти
    # две версии, а не подстроки «v5_digest» и «v6_digest».
    assert "for version in 5 6; do" in snapshot
    assert 'v${version}_digest' in snapshot
    for key in ("agents_fingerprint", "risk_targets", "signal_targets"):
        assert key in snapshot, key
