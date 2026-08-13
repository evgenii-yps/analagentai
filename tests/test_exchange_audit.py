"""Тесты аудита бирж с замоканными ответами (без сети).

Покрывают: различение гео-блока / rate limit / таймаута / сетевой ошибки,
расчёт метрик пары (спред, глубина ±2 %, вердикт), пустой стакан, случай 451,
расчёт суммы заявки на сдвиг 1 %, оркестрацию с падением одной биржи и
формирование markdown-отчёта.
"""

from __future__ import annotations

import httpx
import pytest

from scripts.exchange_audit import (
    STATUS_GEO_BLOCKED,
    STATUS_NETWORK_ERROR,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_SERVER_ERROR,
    VERDICT_ILLIQUID,
    VERDICT_NOT_LISTED,
    VERDICT_TRADABLE,
    AuditConfig,
    ExchangeAuditResult,
    PairAuditResult,
    check_exchange,
    check_pair,
    classify_http_response,
    compute_depth_usd,
    compute_pair_metrics,
    decide_pair_verdict,
    extract_tick_and_min,
    notional_to_move_1pct,
    render_report_markdown,
    run_audit,
)

CFG = AuditConfig(retries=0)  # без задержек-повторов в тестах


# --- classify_http_response: три РАЗНЫХ вердикта не смешиваются -------------


def test_classify_geo_block_451() -> None:
    """451 — гео-блок (Binance/Bybit с EU IP)."""
    status, note = classify_http_response(451, "Unavailable For Legal Reasons", False)
    assert status == STATUS_GEO_BLOCKED
    assert note is None


def test_classify_geo_block_403() -> None:
    """403 трактуется как гео-блок для публичного эндпоинта."""
    status, _ = classify_http_response(403, "Forbidden", False)
    assert status == STATUS_GEO_BLOCKED


def test_classify_geo_block_by_text() -> None:
    """4xx с характерным текстом об ограничении по региону — гео-блок."""
    status, _ = classify_http_response(
        400, '{"msg":"Service not available in your country"}', False
    )
    assert status == STATUS_GEO_BLOCKED


def test_classify_rate_limit_429() -> None:
    """429 — rate limit, НЕ гео-блок."""
    status, note = classify_http_response(429, "Too Many Requests", False)
    assert status == STATUS_RATE_LIMITED
    assert note is not None


def test_classify_rate_limit_418() -> None:
    """418 (Binance IP ban) — rate limit."""
    status, _ = classify_http_response(418, "banned", False)
    assert status == STATUS_RATE_LIMITED


def test_classify_network_error() -> None:
    """Сетевой сбой (таймаут/соединение) — отдельный вердикт, НЕ гео-блок."""
    status, _ = classify_http_response(None, "ConnectTimeout", True)
    assert status == STATUS_NETWORK_ERROR


def test_classify_ok() -> None:
    """2xx — доступно."""
    status, _ = classify_http_response(200, "{}", False)
    assert status == STATUS_OK


def test_classify_server_error() -> None:
    """5xx без гео-текста — серверная ошибка, не гео-блок и не rate limit."""
    status, _ = classify_http_response(503, "Service Unavailable", False)
    assert status == STATUS_SERVER_ERROR


# --- compute_depth_usd ------------------------------------------------------


def test_compute_depth_usd_bid_side() -> None:
    """Глубина bid суммирует уровни в пределах -band % от середины."""
    mid = 100.0
    bids = [[99.5, 10.0], [99.0, 5.0], [95.0, 100.0]]  # 95 вне полосы 2 %
    depth = compute_depth_usd(bids, mid, 2.0, "bid")
    assert depth == pytest.approx(99.5 * 10 + 99.0 * 5)


def test_compute_depth_usd_ask_side() -> None:
    """Глубина ask суммирует уровни в пределах +band % от середины."""
    mid = 100.0
    asks = [[100.5, 10.0], [101.0, 5.0], [105.0, 100.0]]  # 105 вне полосы
    depth = compute_depth_usd(asks, mid, 2.0, "ask")
    assert depth == pytest.approx(100.5 * 10 + 101.0 * 5)


# --- decide_pair_verdict ----------------------------------------------------


def test_verdict_not_listed() -> None:
    assert decide_pair_verdict(False, None, None, None, CFG) == VERDICT_NOT_LISTED


def test_verdict_illiquid_wide_spread() -> None:
    """Спред выше порога → illiquid даже при большой глубине."""
    v = decide_pair_verdict(True, 0.5, 1_000_000, 1_000_000, CFG)
    assert v == VERDICT_ILLIQUID


def test_verdict_illiquid_thin_depth() -> None:
    """Тонкая глубина с одной стороны → illiquid."""
    v = decide_pair_verdict(True, 0.05, 1_000_000, 10_000, CFG)
    assert v == VERDICT_ILLIQUID


def test_verdict_illiquid_missing_data() -> None:
    """Нет данных о спреде/глубине → нельзя подтвердить → illiquid."""
    assert decide_pair_verdict(True, None, None, None, CFG) == VERDICT_ILLIQUID


def test_verdict_tradable() -> None:
    """Узкий спред и достаточная глубина с обеих сторон → tradable."""
    v = decide_pair_verdict(True, 0.02, 500_000, 500_000, CFG)
    assert v == VERDICT_TRADABLE


# --- extract_tick_and_min ---------------------------------------------------


def test_extract_tick_from_step() -> None:
    """precision.price как готовый шаг (<1) берётся напрямую."""
    tick, _ = extract_tick_and_min({"precision": {"price": 0.0001}}, 1.0)
    assert tick == pytest.approx(0.0001)


def test_extract_tick_from_decimals() -> None:
    """precision.price как число знаков (целое) → 10**-n."""
    tick, _ = extract_tick_and_min({"precision": {"price": 4}}, 1.0)
    assert tick == pytest.approx(0.0001)


def test_extract_min_order_from_cost() -> None:
    """limits.cost.min уже в USD — берётся как есть."""
    _, min_usd = extract_tick_and_min({"limits": {"cost": {"min": 5.0}}}, 100.0)
    assert min_usd == pytest.approx(5.0)


def test_extract_min_order_from_amount() -> None:
    """При отсутствии cost.min считаем amount.min * цена."""
    _, min_usd = extract_tick_and_min(
        {"limits": {"amount": {"min": 0.001}}}, 20_000.0
    )
    assert min_usd == pytest.approx(20.0)


# --- compute_pair_metrics ---------------------------------------------------


def test_compute_pair_metrics_tradable() -> None:
    """Полный расчёт: спред, глубина, объём, вердикт tradable."""
    ticker = {"last": 100.0, "bid": 99.99, "ask": 100.01, "quoteVolume": 5_000_000}
    order_book = {
        "bids": [[99.99, 1000], [99.0, 1000]],
        "asks": [[100.01, 1000], [101.0, 1000]],
    }
    market = {"precision": {"price": 0.01}, "limits": {"cost": {"min": 5}}}
    m = compute_pair_metrics(ticker, order_book, market, CFG)
    assert m["spread_pct"] == pytest.approx(0.02, abs=0.005)
    assert m["depth_bid_2pct_usd"] > 100_000
    assert m["depth_ask_2pct_usd"] > 100_000
    assert m["vol_24h_usd"] == 5_000_000
    assert m["verdict"] == VERDICT_TRADABLE


def test_compute_pair_metrics_empty_orderbook() -> None:
    """Пустой стакан → спред/глубина None → illiquid (не падение)."""
    ticker = {"last": 100.0, "quoteVolume": 1_000}
    order_book = {"bids": [], "asks": []}
    m = compute_pair_metrics(ticker, order_book, {}, CFG)
    assert m["spread_pct"] is None
    assert m["depth_bid_2pct_usd"] is None
    assert m["verdict"] == VERDICT_ILLIQUID


def test_compute_pair_metrics_dent_precision() -> None:
    """Цена DENT-масштаба (~0.0000277) сохраняется, не округляется в ноль."""
    ticker = {"last": 0.00002771, "bid": 0.00002770, "ask": 0.00002772}
    order_book = {
        "bids": [[0.00002770, 1_000_000_000]],
        "asks": [[0.00002772, 1_000_000_000]],
    }
    m = compute_pair_metrics(ticker, order_book, {}, CFG)
    assert m["last_price"] == pytest.approx(0.00002771, rel=1e-9)
    assert m["last_price"] > 0


# --- notional_to_move_1pct --------------------------------------------------


def test_notional_move_1pct_buy() -> None:
    """Сумма покупки, двигающая цену на +1 %, — по фактическим уровням ask."""
    bids = [[99.0, 100]]
    asks = [[100.0, 10], [100.5, 10], [101.5, 10]]  # mid≈99.5, target≈100.495
    spent = notional_to_move_1pct(asks, bids, "buy")
    # Съедаются уровни 100.0 и 100.5 (100.5>100.495) → 100*10 + 100.5*10.
    assert spent == pytest.approx(100.0 * 10 + 100.5 * 10)


def test_notional_move_1pct_insufficient_book() -> None:
    """Если стакан не покрывает движение на 1 % — None."""
    bids = [[99.0, 100]]
    asks = [[100.0, 1]]  # одного тонкого уровня не хватает
    assert notional_to_move_1pct(asks, bids, "buy") is None


# --- check_exchange с httpx.MockTransport (451, timeout, ok) ----------------


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_check_exchange_geo_blocked_451() -> None:
    """Оба эндпоинта отдают 451 → биржа помечена гео-блоком, WS не проверяется."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, text="Unavailable For Legal Reasons")

    async with _client(handler) as client:
        res = await check_exchange("binance", "local", cfg=CFG, client=client)
    assert res.geo_blocked is True
    assert res.reachable is False
    assert all(p.status == STATUS_GEO_BLOCKED for p in res.probes)


async def test_check_exchange_timeout() -> None:
    """Таймаут запроса → сетевая ошибка, НЕ гео-блок."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _client(handler) as client:
        res = await check_exchange("okx", "local", cfg=CFG, client=client)
    assert res.geo_blocked is False
    assert res.reachable is False
    assert all(p.status == STATUS_NETWORK_ERROR for p in res.probes)


async def test_check_exchange_ok_triggers_ws_probe() -> None:
    """200 по эндпоинтам → биржа достижима, вызывается WS-пробер."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def fake_ws(exchange: str, cfg: AuditConfig) -> bool:
        return True

    async with _client(handler) as client:
        res = await check_exchange(
            "okx", "local", cfg=CFG, client=client, ws_prober=fake_ws
        )
    assert res.reachable is True
    assert res.geo_blocked is False
    assert res.ws_available is True


# --- check_pair с фейковой ccxt-биржей --------------------------------------


class _FakeExchange:
    """Мини-фейк ccxt-биржи для check_pair (без сети)."""

    def __init__(self, markets, ticker=None, order_book=None, raise_on=None):
        self._markets = markets
        self._ticker = ticker or {}
        self._order_book = order_book or {"bids": [], "asks": []}
        self._raise_on = raise_on

    async def load_markets(self):
        return self._markets

    async def fetch_ticker(self, symbol):
        if self._raise_on == "ticker":
            raise RuntimeError("boom")
        return self._ticker

    async def fetch_order_book(self, symbol, limit=None):
        return self._order_book

    async def close(self):
        return None


async def test_check_pair_not_listed() -> None:
    """Символа нет в markets → not_listed."""
    ex = _FakeExchange(markets={"BTC/USDT": {}})
    res = await check_pair("okx", "DENT/USDT", "local", ex_client=ex, cfg=CFG)
    assert res.listed is False
    assert res.verdict == VERDICT_NOT_LISTED


async def test_check_pair_tradable() -> None:
    """Ликвидная пара → tradable с заполненными метриками."""
    ex = _FakeExchange(
        markets={"BTC/USDT": {"precision": {"price": 0.01},
                              "limits": {"cost": {"min": 5}}}},
        ticker={"last": 100.0, "bid": 99.99, "ask": 100.01,
                "quoteVolume": 9_000_000},
        order_book={"bids": [[99.99, 5000]], "asks": [[100.01, 5000]]},
    )
    res = await check_pair("okx", "BTC/USDT", "local", ex_client=ex, cfg=CFG)
    assert res.listed is True
    assert res.verdict == VERDICT_TRADABLE
    assert res.last_price == pytest.approx(100.0)


async def test_check_pair_data_error_is_illiquid() -> None:
    """Пара числится, но данные не собрать → illiquid с причиной, без падения."""
    ex = _FakeExchange(markets={"DENT/USDT": {}}, raise_on="ticker")
    res = await check_pair("okx", "DENT/USDT", "local", ex_client=ex, cfg=CFG)
    assert res.listed is True
    assert res.verdict == VERDICT_ILLIQUID
    assert "ошибка" in res.notes.lower()


# --- run_audit: падение одной биржи не рушит аудит --------------------------


async def test_run_audit_isolates_failures() -> None:
    """Одна биржа падает — остальные всё равно в отчёте."""
    calls = {"n": 0}

    async def flaky_pair(exchange, symbol, source_ip_label, **kw):
        return PairAuditResult(
            exchange=exchange, symbol=symbol, source_ip_label=source_ip_label,
            listed=True, verdict=VERDICT_TRADABLE,
        )

    async def fake_ws(exchange, cfg):
        return True

    # Подменяем check_exchange через monkeypatch на уровне модуля.
    import scripts.exchange_audit as mod

    async def fake_check_exchange(exchange, source_ip_label, **kw):
        calls["n"] += 1
        if exchange == "binance":
            raise RuntimeError("network down")
        return ExchangeAuditResult(
            exchange=exchange, source_ip_label=source_ip_label,
            probes=[], ws_available=True,
        )

    orig = mod.check_exchange
    mod.check_exchange = fake_check_exchange  # type: ignore[assignment]
    try:
        report = await run_audit(
            ["okx", "binance"], ["BTC/USDT"], "local",
            cfg=CFG, ws_prober=fake_ws, pair_checker=flaky_pair,
        )
    finally:
        mod.check_exchange = orig  # type: ignore[assignment]

    names = {e.exchange for e in report.exchanges}
    assert names == {"okx", "binance"}
    binance = next(e for e in report.exchanges if e.exchange == "binance")
    assert "исключение" in binance.notes.lower()


# --- render_report_markdown -------------------------------------------------


async def test_render_markdown_has_sections() -> None:
    """Markdown содержит карту бирж, таблицу пар и блок DENT."""
    report = await run_audit(
        [], ["DENT/USDT"], "local", cfg=CFG, ws_prober=None, pair_checker=None
    )
    # Добавим руками пару DENT, чтобы блок был содержательным.
    report.exchanges.append(
        ExchangeAuditResult(exchange="okx", source_ip_label="local", probes=[])
    )
    report.pairs.append(
        PairAuditResult(
            exchange="okx", symbol="DENT/USDT", source_ip_label="local",
            listed=True, last_price=0.00002771, spread_pct=0.1,
            depth_bid_2pct_usd=80_000, depth_ask_2pct_usd=90_000,
            vol_24h_usd=1_000_000, verdict=VERDICT_TRADABLE,
        )
    )
    md = render_report_markdown(report)
    assert "Карта доступности бирж" in md
    assert "Аудит пар" in md
    assert "Блок DENT/USDT" in md
    assert "okx" in md
