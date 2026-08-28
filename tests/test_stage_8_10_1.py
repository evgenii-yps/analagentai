"""Этап 8.10.1: разбор расхождения контрольного варианта и подпись клиента.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, по пунктам ТЗ.

§1. ПРИЧИНА РАСХОЖДЕНИЯ. Две строки из 65 594 разошлись только полем
``net_pnl_pct``, только на исходе ``timeout``, ровно на 0.0012. При исходе
``timeout`` итог считается по формуле ``(C − P)/P × 100 − cost``, где ``C`` —
закрытие ПОСЛЕДНЕГО бара окна. Поля ``P``, ``cost`` и ``direction`` сверяются
контролем и совпали, значит различаться могло ТОЛЬКО ``C``. Здесь это
воспроизведено числом: сдвиг закрытия на 1.202e-5 от цены входа даёт ровно
серверные 0.001202.

Почему ``C`` вообще мог измениться: окно ``t+1 … t+h`` кончается баром, который
ОТКРЫВАЕТСЯ в момент срока и закрывается через целый бар после него. Прежнее
правило годности («срок уже наступил») пускало расчёт к этому бару, пока он ещё
формировался, а коллектор перезаписывает такой бар следующим опросом (UPSERT с
DO UPDATE). Тесты ниже показывают этот разрыв в правиле годности прямо на
``window_bounds`` и проверяют, что запас его закрывает.

§ОТДЕЛЬНО. ПОДПИСЬ КЛИЕНТА. Любой код проекта, ходящий к бирже по HTTP, обязан
брать заголовки из ``src.core.http``. Проверяется не обещанием, а обходом
файлов: клиент со своей подписью тест роняет.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
"""

from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from src.barrier.outcomes import (
    BAR_SECONDS,
    BUY,
    OUTCOME_TIMEOUT,
    Bar,
    resolve,
    window_bounds,
)
from src.barrier.runner import settle_seconds
from src.core.config import settings
from src.core.http import (
    EXCHANGE_USER_AGENT,
    exchange_headers,
    is_browser_user_agent,
)
from src.trailing.rule import FIXED_VARIANT, resolve_all

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(например postgresql://agenttrade@127.0.0.1:5433/agenttrade)"
    ),
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_T0 = datetime(2026, 8, 28, 3, 9, 37, tzinfo=UTC)
_PRICE = 60000.0
_TARGET_PCT = 0.18288   # цель первой из двух разошедшихся пар сервера
_STOP_PCT = 1.0
_COST_PCT = 0.22

# Сдвиг закрытия, вычисленный ИЗ СЕРВЕРНЫХ ЧИСЕЛ: расхождение net_pnl_pct
# 0.001202 % при цене входа P означает разницу закрытия ровно в такую долю
# цены. Это не подобранная константа, а решение уравнения.
_SERVER_GAP_PCT = 0.001202
_SERVER_CLOSE_SHIFT_RATIO = _SERVER_GAP_PCT / 100.0


def _flat_window(minutes: int, price: float = _PRICE) -> list[Bar]:
    """Ровное окно без движения: исход заведомо ``timeout``, итог — по закрытию."""
    first, _last = window_bounds(_T0, 1, "1m")
    return [
        Bar(ts=first + timedelta(minutes=i), high=price, low=price, close=price)
        for i in range(minutes)
    ]


def _resolve_fixed(bars: list[Bar]):
    """Контрольный вариант 8.10 на этих барах."""
    return {
        (item.activation_ratio, item.retrace_ratio): item
        for item in resolve_all(
            bars, signal_ts=_T0, horizon_h=1, price_at_signal=_PRICE,
            target_pct=_TARGET_PCT, stop_pct=_STOP_PCT, cost_pct=_COST_PCT,
            direction=BUY, resolution="1m",
        )
    }[FIXED_VARIANT]


def _resolve_barrier(bars: list[Bar]):
    """Правило Этапа 8.8 на тех же барах."""
    return resolve(
        bars, signal_ts=_T0, horizon_h=1, price_at_signal=_PRICE,
        target_pct=_TARGET_PCT, stop_pct=_STOP_PCT, cost_pct=_COST_PCT,
        direction=BUY, resolution="1m",
    )


# --- §1. Причина: закрытие последнего бара, и только оно ---------------------

def test_same_bars_give_identical_rows() -> None:
    """На ОДНИХ И ТЕХ ЖЕ барах обе ветки совпадают. Отправная точка разбора.

    Если бы они расходились и здесь, причину надо было бы искать в коде. Они не
    расходятся — значит, дело в данных, и следующий тест показывает, в каких.
    """
    bars = _flat_window(60)
    fixed = _resolve_fixed(bars)
    barrier = _resolve_barrier(bars)
    assert fixed.exit_reason == barrier.outcome == OUTCOME_TIMEOUT
    assert fixed.net_pnl_pct == barrier.net_pnl_pct
    assert (fixed.mae_pct, fixed.mfe_pct) == (barrier.mae_pct, barrier.mfe_pct)


def test_only_the_last_bar_close_can_explain_the_server_gap() -> None:
    """Сдвиг закрытия последнего бара даёт РОВНО серверные 0.001202 %.

    Так воспроизводится расхождение сервера: старое значение (посчитанное 8.8 по
    ещё формировавшемуся бару) против нового (посчитанного 8.10 по тому же бару
    после его закрытия). Направление сдвига здесь положительное — как на
    сервере, где обе разошедшиеся строки оказались в пользу подвижного расчёта.
    """
    before = _flat_window(60)
    after = list(before)
    shifted = before[-1]
    after[-1] = Bar(
        ts=shifted.ts, high=shifted.high, low=shifted.low,
        close=shifted.close + _PRICE * _SERVER_CLOSE_SHIFT_RATIO,
    )

    old = _resolve_barrier(before)     # что записал Этап 8.8
    new = _resolve_fixed(after)        # что пересчитал Этап 8.10

    assert old.outcome == new.exit_reason == OUTCOME_TIMEOUT
    # Исход и момент совпали — ровно как на сервере.
    assert old.hit_at == new.hit_at is None
    assert old.bars_to_hit == new.bars_to_hit is None
    # Разошёлся ТОЛЬКО итог, и ровно на серверную величину.
    assert new.net_pnl_pct - old.net_pnl_pct == pytest.approx(
        _SERVER_GAP_PCT, abs=1e-9
    )


def test_a_revised_last_bar_does_not_move_mae_and_mfe() -> None:
    """И объяснение, почему расхождение видно ТОЛЬКО в итоге.

    ``mae``/``mfe`` — крайние значения по ВСЕМУ окну; правка одного бара сдвигает
    их лишь тогда, когда именно он держал крайнее значение. Итог же при
    ``timeout`` читает закрытие ровно этого бара — всегда. Поэтому пересчёт
    после правки последнего бара расходится в одном поле, а не в трёх, и
    выглядит это как «различие только в net_pnl_pct» из отчёта сервера.
    """
    before = _flat_window(60)
    after = list(before)
    shifted = before[-1]
    after[-1] = Bar(
        ts=shifted.ts, high=shifted.high, low=shifted.low,
        close=shifted.close + _PRICE * _SERVER_CLOSE_SHIFT_RATIO,
    )
    old, new = _resolve_barrier(before), _resolve_fixed(after)
    assert (new.mae_pct, new.mfe_pct) == (old.mae_pct, old.mfe_pct)


def test_price_rounding_cannot_explain_the_gap() -> None:
    """Опровержение второй гипотезы: округление цены входа до NUMERIC(20,8).

    Максимальная погрешность такого округления — 5e-9 абсолютных. Чтобы она
    дала сдвиг 0.0012 %, цена входа должна быть меньше 4.2e-4. Самый дешёвый
    инструмент проекта — DOGE порядка 0.1, и даже у него округление в 240 раз
    меньше наблюдённого расхождения; у BTC — в сто миллионов раз. Это не
    «маловероятно», а арифметически невозможно.

    Второе, независимое опровержение: ``price_at_signal`` ВХОДИТ В СВЕРКУ
    контрольного варианта и на сервере совпал. Изменись он — разошлось бы и это
    поле, и вместе с ним ``mae``/``mfe``, а разошёлся только итог.
    """
    worst_absolute_rounding = 5e-9
    for price in (60000.0, 3000.0, 150.0, 0.5, 0.1):
        shift_pct = 100.0 * worst_absolute_rounding / price
        assert shift_pct * 100.0 < _SERVER_GAP_PCT, price
    # И обратная сторона того же: цена, при которой округление дало бы серверный
    # сдвиг, вне всякого правдоподобия.
    price_needed = worst_absolute_rounding / _SERVER_CLOSE_SHIFT_RATIO
    assert price_needed < 1e-3


# --- §1. Разрыв в правиле годности, который это допускал ---------------------

def test_window_ends_on_a_bar_that_closes_after_the_deadline() -> None:
    """Последний бар окна ЗАКРЫВАЕТСЯ ПОЗЖЕ срока — на целый бар.

    Это и есть разрыв, в который проваливались две строки: правило годности
    «срок наступил» пускало расчёт к бару, который ещё формировался.
    """
    for horizon_h in (1, 4, 12, 24):
        for resolution in ("1m", "1h"):
            _first, last = window_bounds(_T0, horizon_h, resolution)
            bar = timedelta(seconds=BAR_SECONDS[resolution])
            deadline = _T0 + timedelta(hours=horizon_h)
            assert last <= deadline, (horizon_h, resolution)
            assert last + bar > deadline, (horizon_h, resolution)


def test_settle_covers_the_coarse_bar_and_the_collector_lag() -> None:
    """Запас перекрывает самый длинный бар источника, а не самый короткий.

    Разрешение выясняется ПОСЛЕ отбора кандидатов, поэтому ждать надо по
    часовому бару: иначе к расчёту допускались бы пары, которые посчитаются по
    часовому ряду с незакрытым последним часом.
    """
    coarse = BAR_SECONDS[settings.BARRIER_COARSE_TIMEFRAME]
    fine = BAR_SECONDS[settings.BARRIER_FINE_TIMEFRAME]
    assert settle_seconds() == coarse + settings.BARRIER_SETTLE_MINUTES * 60
    assert settle_seconds() > coarse >= fine
    # Запас на задержку коллектора — не ноль: последний опрос минуты приходит
    # уже после её конца.
    assert settings.BARRIER_SETTLE_MINUTES >= 1


def test_settle_would_have_excluded_the_pair_at_risk() -> None:
    """Пара, чей последний бар ещё формируется, при запасе НЕ годна.

    Проверяется тем же неравенством, что стоит в запросе отбора кандидатов:
    ``t + h + запас <= now``.
    """
    settle = timedelta(seconds=settle_seconds())
    for horizon_h in (1, 4, 12, 24):
        deadline = _T0 + timedelta(hours=horizon_h)
        _first, last = window_bounds(_T0, horizon_h, "1h")
        bar_closes = last + timedelta(seconds=BAR_SECONDS["1h"])
        # Момент, в который прежнее правило уже пускало расчёт...
        assert deadline >= _T0 + timedelta(hours=horizon_h)
        # ...а новое — ещё нет, и именно до закрытия последнего бара.
        assert deadline + settle > bar_closes


# --- §ОТДЕЛЬНО. Подпись клиента ----------------------------------------------

def test_user_agent_is_browser_shaped() -> None:
    """Подпись браузерная: питоновскую OKX отбивает 403 с кодом 1010."""
    assert is_browser_user_agent(EXCHANGE_USER_AGENT)
    assert exchange_headers()["User-Agent"] == EXCHANGE_USER_AGENT
    for rejected in ("python-requests/2.34.2", "python-httpx/0.28.1",
                     "Python-urllib/3.12", "agent-trade-geocheck/1.0", "", None):
        assert not is_browser_user_agent(rejected)


def test_extra_headers_add_but_do_not_replace_the_signature() -> None:
    headers = exchange_headers({"OK-ACCESS-KEY": "x"})
    assert headers["OK-ACCESS-KEY"] == "x"
    assert headers["User-Agent"] == EXCHANGE_USER_AGENT


def test_ccxt_client_carries_the_browser_signature() -> None:
    """Клиент коллектора — с браузерной подписью, а не с ``python-requests``.

    Проверяется СОЗДАННЫЙ объект, а не текст файла: ccxt подставляет свою
    подпись по умолчанию, и молчаливое возвращение к ней — ровно тот отказ,
    который надо ловить.
    """
    from src.core.exchange import create_exchange

    exchange = create_exchange("okx")
    try:
        assert is_browser_user_agent(exchange.userAgent)
        assert is_browser_user_agent(exchange.headers.get("User-Agent"))
    finally:
        # Сетевых соединений создание клиента не открывает, но закрыть корректно
        # дешевле, чем однажды разбираться, почему тесты держат сокеты.
        del exchange


def test_history_loader_client_carries_the_browser_signature() -> None:
    from backtest.loader import create_http_client

    client = create_http_client()
    try:
        assert is_browser_user_agent(client.headers.get("user-agent"))
    finally:
        pass


def test_no_exchange_client_invents_its_own_signature() -> None:
    """Ни один файл проекта не задаёт подпись клиента сам.

    Обход намеренно грубый и текстовый: он ловит именно то, что запрещено, —
    появление литерала ``User-Agent`` рядом с обращением к бирже мимо единого
    места. Единственное исключение — сам ``src/core/http.py``, где подпись и
    объявлена, и тесты, которые её проверяют.
    """
    allowed = {_ROOT / "src" / "core" / "http.py"}
    # Файл в порядке, если берёт заголовки из единого места ЛИБО получает
    # готовый клиент оттуда, где они уже проставлены.
    sources = ("exchange_headers", "EXCHANGE_USER_AGENT", "create_http_client")
    offenders: list[str] = []
    for folder in ("src", "backtest", "scripts"):
        for path in (_ROOT / folder).rglob("*.py"):
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if "User-Agent" not in text and "userAgent" not in text:
                continue
            if any(source in text for source in sources):
                continue
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"своя подпись клиента вместо единого места: {offenders}"


def test_the_places_that_talk_to_the_exchange_use_the_single_source() -> None:
    """Все три клиента к бирже берут заголовки из ``src.core.http``."""
    for relative in ("src/core/exchange.py", "backtest/loader.py",
                     "scripts/geo_check.py"):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert "src.core.http" in text, relative
        assert "exchange_headers" in text or "EXCHANGE_USER_AGENT" in text, relative


def test_geo_check_stays_importable_without_third_party_packages() -> None:
    """Гео-тест запускается на «голом» сервере — значит, тянуть httpx нельзя.

    Модуль подписи обязан оставаться на одной стандартной библиотеке: иначе
    ``scripts/geo_check.py`` перестанет работать до установки pip-пакетов, а он
    выполняется ИМЕННО до неё.
    """
    text = (_ROOT / "src" / "core" / "http.py").read_text(encoding="utf-8")
    for third_party in ("import httpx", "import ccxt", "import aiohttp",
                        "import requests", "from src.core.config"):
        assert third_party not in text


# --- §1. Проверка, которой нужна база ----------------------------------------

@needs_db
async def test_settle_lag_holds_back_a_pair_whose_last_bar_is_open() -> None:
    """Отбор кандидатов: без запаса пара годна, с запасом — ещё нет.

    Проверяется ШТАТНЫЙ метод ``db.get_barrier_candidates``, а не его копия в
    тесте: иначе проверялось бы не то, что поедет на сервер.
    """
    import asyncpg

    from src.core.db import db as database

    now = datetime.now(UTC).replace(microsecond=0)
    # Сигнал, чей часовой горизонт истёк ровно сейчас: срок наступил, но
    # последний бар окна ещё не закрыт.
    signal_ts = now - timedelta(hours=1)

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', 'TEST8101/USDT', 'TEST8101', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;"
        )
        signal_id = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
            "VALUES ($1, $2, 'buy', 5) RETURNING id;",
            instrument_id, signal_ts,
        )
        await conn.execute(
            "INSERT INTO signal_targets (signal_id, horizon_h, direction, "
            "price_at_signal, target_pct, covers_fees, targets_version) "
            "VALUES ($1, 1, 'buy', 100, 1.5, true, 1);",
            signal_id,
        )
    finally:
        await conn.close()

    database._pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
    try:
        without = await database.get_barrier_candidates(
            logic_version=5, horizon_h=1, now=now, settle_seconds=0
        )
        with_settle = await database.get_barrier_candidates(
            logic_version=5, horizon_h=1, now=now, settle_seconds=settle_seconds()
        )
        ids_without = {row["id"] for row in without}
        ids_with = {row["id"] for row in with_settle}
        assert signal_id in ids_without, "без запаса пара обязана быть годной"
        assert signal_id not in ids_with, "с запасом пара обязана ждать"

        # И тот же счётчик пропущенных обязан считать по тому же правилу.
        skipped_without = await database.count_barrier_skipped(
            logic_version=5, horizon_h=1, now=now, settle_seconds=0
        )
        skipped_with = await database.count_barrier_skipped(
            logic_version=5, horizon_h=1, now=now, settle_seconds=settle_seconds()
        )
        assert skipped_with <= skipped_without
    finally:
        await database.close()
        conn = await asyncpg.connect(dsn=TEST_DSN)
        try:
            await conn.execute(
                "DELETE FROM signal_targets WHERE signal_id = $1;", signal_id
            )
            await conn.execute("DELETE FROM signals WHERE id = $1;", signal_id)
        finally:
            await conn.close()
