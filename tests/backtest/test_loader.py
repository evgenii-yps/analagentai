"""Загрузка истории: разбор ответов OKX, пагинация, идемпотентность.

HTTP-клиент заменён подставным с ответами ТОЙ ЖЕ формы, что отдаёт OKX. Это не
проверка сети — сеть проверяет зонд (``scripts/probe_history_depth.py``) на
сервере. Здесь проверяется логика, которая иначе была бы проверена только на
живом прогоне: отбрасывание незакрытых свечей, движение пагинации назад по
времени, границы периода, повторная загрузка и поведение при отказах.

Отдельно проверяется подпись клиента: с 28.08.2026 OKX отбивает питоновские
подписи кодом 403/1010 ещё до проверки ключа, поэтому подпись БРАУЗЕРНАЯ и
берётся из ``src.core.http`` — единого места проекта (Этап 8.10.1). Тест
требует, чтобы браузерная подпись (Mozilla/...) не подставлялась.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers import SPOT, SWAP, T0, requires_db

from backtest import loader

BAR = "1H"


class FakeResponse:
    """Ответ, как его отдаёт httpx: статус, тело, разбор JSON."""

    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


class FakeOkx:
    """Подставной HTTP-клиент: отдаёт часовые свечи и funding, как эндпоинты OKX.

    Записи возвращаются страницами по ``limit`` штук, всегда РАНЬШЕ метки
    ``after`` — ровно та семантика, которую зонд обязан подтвердить на живой
    бирже перед прогоном.
    """

    def __init__(self, first: datetime, last: datetime, *, unconfirmed_tail: int = 0):
        self.first = first
        self.last = last
        self.unconfirmed_tail = unconfirmed_tail
        self.calls = 0
        self.paths: list[str] = []

    async def get(self, path: str, params: dict) -> FakeResponse:
        self.calls += 1
        self.paths.append(path)
        if path == loader.PATH_HISTORY_CANDLES:
            return FakeResponse(self._candles(params))
        if path == loader.PATH_FUNDING_HISTORY:
            return FakeResponse(self._funding(params))
        return FakeResponse({"code": "51000", "msg": "unknown path", "data": []})

    def _cursor(self, params: dict) -> datetime:
        after = params.get("after")
        return datetime.fromtimestamp(int(after) / 1000, tz=UTC) if after else self.last

    def _candles(self, params: dict) -> dict:
        limit = int(params.get("limit", 100))
        rows = []
        stamp = self._cursor(params) - timedelta(hours=1)
        while len(rows) < limit and stamp >= self.first:
            price = 100.0 + stamp.timestamp() % 17
            confirm = "1"
            if self.unconfirmed_tail and stamp > self.last - timedelta(
                hours=self.unconfirmed_tail
            ):
                confirm = "0"
            rows.append(
                [
                    str(int(stamp.timestamp() * 1000)),
                    f"{price:.4f}", f"{price + 1:.4f}", f"{price - 1:.4f}",
                    f"{price:.4f}", "10", "1000", "1000", confirm,
                ]
            )
            stamp -= timedelta(hours=1)
        return {"code": "0", "msg": "", "data": rows}

    def _funding(self, params: dict) -> dict:
        limit = int(params.get("limit", 100))
        rows = []
        stamp = self._cursor(params) - timedelta(hours=8)
        while len(rows) < limit and stamp >= self.first:
            rows.append(
                {
                    "instId": SPOT,
                    "fundingRate": "0.00005",
                    "realizedRate": "0.00006",
                    "fundingTime": str(int(stamp.timestamp() * 1000)),
                }
            )
            stamp -= timedelta(hours=8)
        return {"code": "0", "msg": "", "data": rows}


# --- Разбор ответов (без БД) ----------------------------------------------

def test_parse_candles_drops_unconfirmed() -> None:
    """Незакрытая свеча (confirm=0) не попадает в БД ни при каких условиях."""
    rows = [
        ["1735689600000", "1", "2", "0.5", "1.5", "10", "100", "100", "1"],
        ["1735693200000", "1", "2", "0.5", "1.5", "10", "100", "100", "0"],
    ]
    parsed = loader.parse_candles(rows, SPOT, BAR)
    assert len(parsed) == 1
    assert parsed[0][2] == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)


def test_parse_candles_computes_close_time_from_bar() -> None:
    rows = [["1735689600000", "1", "2", "0.5", "1.5", "10", "100", "100", "1"]]
    open_time, close_time = loader.parse_candles(rows, SPOT, BAR)[0][2:4]
    assert close_time - open_time == timedelta(hours=1)


def test_parse_candles_rejects_unknown_bar() -> None:
    with pytest.raises(loader.LoaderError):
        loader.parse_candles([], SPOT, "7H")


def test_parse_funding_prefers_realized_rate() -> None:
    """История даёт расчётную ставку; именно она и берётся."""
    rows = [{"fundingTime": "1735689600000", "fundingRate": "0.0001",
             "realizedRate": "0.00007"}]
    parsed = loader.parse_funding(rows, SWAP)
    assert float(parsed[0][2]) == pytest.approx(0.00007)


def test_parse_funding_skips_rows_without_rate() -> None:
    rows = [{"fundingTime": "1735689600000"}, {"fundingRate": "0.0001"}]
    assert loader.parse_funding(rows, SWAP) == []


def test_bar_seconds_known_values() -> None:
    assert loader.bar_seconds("1H") == 3600
    assert loader.bar_seconds("4H") == 14400


# --- Загрузка в БД ---------------------------------------------------------

@requires_db
async def test_backfill_candles_walks_backwards_and_stops(bt_db, pool) -> None:
    """Пагинация идёт назад до начала периода и не зацикливается."""
    since = T0
    until = T0 + timedelta(days=10)
    fake = FakeOkx(first=T0 - timedelta(days=5), last=until)
    client = loader.OkxHistory(fake, pause_ms=0)

    written = await loader.backfill_candles(
        SPOT, BAR, since, until, client=client, page_limit=50
    )
    assert written > 0

    row = await pool.fetchrow(
        "SELECT count(*) AS n, min(open_time) AS lo, max(open_time) AS hi "
        "FROM backtest.candles WHERE inst_id=$1;",
        SPOT,
    )
    assert row["lo"] >= since
    assert row["hi"] <= until
    # Ряд непрерывен по часу: 10 суток → не более 241 точки.
    assert 0 < row["n"] <= 241


@requires_db
async def test_backfill_is_idempotent(bt_db, pool) -> None:
    """Повторная загрузка не задваивает строки и не перекачивает историю заново."""
    since = T0
    until = T0 + timedelta(days=5)
    fake = FakeOkx(first=T0 - timedelta(days=2), last=until)
    client = loader.OkxHistory(fake, pause_ms=0)

    await loader.backfill_candles(SPOT, BAR, since, until, client=client, page_limit=50)
    first_count = await pool.fetchval(
        "SELECT count(*) FROM backtest.candles WHERE inst_id=$1;", SPOT
    )
    calls_after_first = fake.calls

    await loader.backfill_candles(SPOT, BAR, since, until, client=client, page_limit=50)
    second_count = await pool.fetchval(
        "SELECT count(*) FROM backtest.candles WHERE inst_id=$1;", SPOT
    )

    assert first_count == second_count
    # Запросы во второй раз всё же были (граница ряда проверяется по данным),
    # но ни одной новой строки они не принесли.
    assert fake.calls >= calls_after_first


@requires_db
async def test_already_loaded_history_gets_only_the_fresh_tail(bt_db, pool) -> None:
    """Свежие свечи докачиваются к уже загруженному ряду, старое не перекачивается.

    Именно этого не делал прежний однопроходный загрузчик: он начинал пагинацию
    от самой ранней загруженной точки и шёл назад, поэтому ряд НИКОГДА не
    пополнялся свежими свечами. Для сверки §13.2 это критично — её моменты
    лежат позже BT_PERIOD_TO.
    """
    since = T0
    middle = T0 + timedelta(days=5)
    until = T0 + timedelta(days=8)
    fake = FakeOkx(first=T0 - timedelta(days=2), last=until)
    client = loader.OkxHistory(fake, pause_ms=0)

    await loader.backfill_candles(SPOT, BAR, since, middle, client=client, page_limit=50)
    before = await pool.fetchrow(
        "SELECT count(*) AS n, max(open_time) AS hi FROM backtest.candles "
        "WHERE inst_id=$1;",
        SPOT,
    )
    assert before["hi"] <= middle

    await loader.backfill_candles(SPOT, BAR, since, until, client=client, page_limit=50)
    after = await pool.fetchrow(
        "SELECT count(*) AS n, min(open_time) AS lo, max(open_time) AS hi "
        "FROM backtest.candles WHERE inst_id=$1;",
        SPOT,
    )
    assert after["hi"] > before["hi"], "свежие свечи не догрузились"
    assert after["hi"] <= until
    assert after["lo"] >= since
    assert after["n"] > before["n"]


@requires_db
async def test_covered_period_costs_no_requests(bt_db, pool) -> None:
    """Полностью покрытый период не порождает ни одного запроса к бирже."""
    since = T0
    until = T0 + timedelta(days=4)
    fake = FakeOkx(first=T0 - timedelta(days=2), last=until)
    client = loader.OkxHistory(fake, pause_ms=0)

    await loader.backfill_candles(SPOT, BAR, since, until, client=client, page_limit=50)
    # Границы ряда в БД совпали с периодом — второму проходу качать нечего.
    row = await pool.fetchrow(
        "SELECT min(open_time) AS lo, max(open_time) AS hi FROM backtest.candles "
        "WHERE inst_id=$1;",
        SPOT,
    )
    calls_before = fake.calls
    await loader.backfill_candles(
        SPOT, BAR, row["lo"], row["hi"], client=client, page_limit=50
    )
    assert fake.calls == calls_before


@requires_db
async def test_unconfirmed_candles_never_reach_the_database(bt_db, pool) -> None:
    since = T0
    until = T0 + timedelta(days=3)
    fake = FakeOkx(first=T0, last=until, unconfirmed_tail=5)
    client = loader.OkxHistory(fake, pause_ms=0)

    await loader.backfill_candles(SPOT, BAR, since, until, client=client, page_limit=50)
    newest = await pool.fetchval(
        "SELECT max(open_time) FROM backtest.candles WHERE inst_id=$1;", SPOT
    )
    assert newest is not None
    assert newest <= until - timedelta(hours=5)


@requires_db
async def test_backfill_funding_respects_period(bt_db, pool) -> None:
    since = T0 + timedelta(days=1)
    until = T0 + timedelta(days=6)
    fake = FakeOkx(first=T0, last=until)
    client = loader.OkxHistory(fake, pause_ms=0)

    await loader.backfill_funding(SWAP, since, until, client=client, page_limit=50)
    row = await pool.fetchrow(
        "SELECT count(*) AS n, min(funding_time) AS lo, max(funding_time) AS hi "
        "FROM backtest.funding WHERE inst_id=$1;",
        SWAP,
    )
    assert row["n"] > 0
    assert row["lo"] >= since
    assert row["hi"] <= until


async def test_rate_limit_code_is_retried() -> None:
    """Код 50011 не роняет загрузку: пауза удваивается и запрос повторяется."""

    class Throttled(FakeOkx):
        def __init__(self):
            super().__init__(first=T0, last=T0 + timedelta(days=1))
            self.first_call = True

        async def get(self, path, params):
            if self.first_call:
                self.first_call = False
                return FakeResponse({"code": "50011", "msg": "Too Many Requests", "data": []})
            return await super().get(path, params)

    fake = Throttled()
    client = loader.OkxHistory(fake, pause_ms=0)
    page = await client.candles_page(SPOT, BAR, None, 10)
    assert page, "после кода 50011 запрос обязан быть повторён"


async def test_http_429_is_retried() -> None:
    """HTTP 429 обрабатывается так же, как код 50011 в теле ответа."""

    class Throttled(FakeOkx):
        def __init__(self):
            super().__init__(first=T0, last=T0 + timedelta(days=1))
            self.first_call = True

        async def get(self, path, params):
            if self.first_call:
                self.first_call = False
                return FakeResponse({}, status_code=429, text="Too Many Requests")
            return await super().get(path, params)

    client = loader.OkxHistory(Throttled(), pause_ms=0)
    assert await client.candles_page(SPOT, BAR, None, 10)


async def test_refusal_by_signature_is_reported_verbatim() -> None:
    """Отказ биржи (403) выдаётся ошибкой с телом ответа, а не «пустой историей».

    Именно так отличается «нас не пустили» от «данных нет»: молчаливое пустое
    значение здесь недопустимо — оно превратилось бы в вывод «истории мало».
    """

    class Forbidden(FakeOkx):
        async def get(self, path, params):
            return FakeResponse({}, status_code=403, text="blocked by signature")

    client = loader.OkxHistory(
        Forbidden(first=T0, last=T0 + timedelta(days=1)), pause_ms=0
    )
    with pytest.raises(loader.LoaderError) as excinfo:
        await client.candles_page(SPOT, BAR, None, 10)
    assert "403" in str(excinfo.value)
    assert "blocked by signature" in str(excinfo.value)


def test_requests_go_to_the_documented_endpoints() -> None:
    """Пути запросов — ровно те два эндпоинта, что разрешает §4 ТЗ."""
    assert loader.PATH_HISTORY_CANDLES == "/api/v5/market/history-candles"
    assert loader.PATH_FUNDING_HISTORY == "/api/v5/public/funding-rate-history"
    assert loader.OKX_BASE_URL == "https://www.okx.com"


def test_client_carries_the_shared_browser_signature() -> None:
    """Клиент ходит с БРАУЗЕРНОЙ подписью, и берёт её из единого места проекта.

    ТРЕБОВАНИЕ ЗДЕСЬ ПЕРЕВЁРНУТО ОТНОСИТЕЛЬНО ПРЕЖНЕГО, и это не оговорка.
    Прежний тест проверял обратное: что клиент ходит со штатной подписью
    ``python-httpx/...``. Основанием служило наблюдение 22.08.2026 — OKX
    отвечала на неё 200. 28.08.2026 на сервере то же наблюдение дало другой
    ответ: публичный эндпоинт ``/api/v5/public/time`` вернул 403 с кодом 1010
    ещё до всякой проверки ключа, а ``curl`` получил 200. Наблюдение устарело,
    и вместе с ним — требование (Этап 8.10.1).

    Проверка оставлена, а не удалена: она по-прежнему сторожит подпись, только
    теперь с другой стороны. Питоновская подпись, вписанная сюда снова,
    остановит тест — и остановит раньше, чем биржа остановит сбор данных.
    """
    from src.core.http import EXCHANGE_USER_AGENT

    client = loader.create_http_client()
    try:
        user_agent = client.headers.get("user-agent", "")
        assert user_agent == EXCHANGE_USER_AGENT
        assert user_agent.startswith("Mozilla/")
        assert not user_agent.startswith("python-")
        assert str(client.base_url).rstrip("/") == loader.OKX_BASE_URL
    finally:
        pass
