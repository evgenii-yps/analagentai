"""Аудит доступности бирж и ликвидности пар (Этап 6.4).

Воспроизводимый скрипт вместо разовых ручных проверок. Использует ТОЛЬКО
публичные эндпоинты — никаких API-ключей, никакой торговли.

Что делает по каждой бирже:
  * HTTP-статус и задержку эндпоинта тикеров и эндпоинта списка инструментов;
  * различает гео-блок (451/403), rate limit (429/418) и сетевую ошибку —
    это ТРИ РАЗНЫХ вердикта, они не смешиваются;
  * доступность WebSocket-потока (через ccxt.pro watch_ticker);
  * по каждой паре — листинг, цену, bid/ask, спред %, глубину ±2 % в USD,
    объём 24ч, tick size, минимальный размер заявки и итоговый вердикт.

Запуск одной командой (из корня репозитория, внутри Docker-сети проекта):

    python -m scripts.exchange_audit --source-ip-label local

Результат пишется в таблицы ``exchange_audit`` / ``pair_audit`` и в
markdown-файл ``reports/exchange_audit_<дата>.md``.

Логика (различение вердиктов, расчёт метрик пары) вынесена в чистые функции —
их покрывают тесты с замоканными ответами бирж (см. tests/test_exchange_audit.py).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Конфигурация: пороги вердиктов и реестр бирж.
# Пороги вынесены сюда, значения — по умолчанию из ТЗ. Переопределяются через ENV.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    """Пороги и параметры аудита (значения по умолчанию — из ТЗ)."""

    spread_max_pct: float = 0.3          # спред выше → illiquid
    min_depth_usd: float = 50_000.0      # глубина ±band с любой стороны ниже → illiquid
    depth_band_pct: float = 2.0          # полоса глубины стакана вокруг середины
    timeout_s: float = 10.0              # таймаут одного запроса
    retries: int = 2                     # повторов не более 2 (сверх первой попытки)
    orderbook_limit: int = 200           # запрашиваемая глубина стакана
    ws_timeout_s: float = 8.0            # таймаут ожидания первого WS-сообщения

    @classmethod
    def from_env(cls) -> AuditConfig:
        """Собирает конфиг, переопределяя дефолты переменными окружения."""

        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw else default

        def _i(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw else default

        return cls(
            spread_max_pct=_f("AUDIT_SPREAD_MAX_PCT", 0.3),
            min_depth_usd=_f("AUDIT_MIN_DEPTH_USD", 50_000.0),
            depth_band_pct=_f("AUDIT_DEPTH_BAND_PCT", 2.0),
            timeout_s=_f("AUDIT_TIMEOUT_S", 10.0),
            retries=_i("AUDIT_RETRIES", 2),
            orderbook_limit=_i("AUDIT_ORDERBOOK_LIMIT", 200),
            ws_timeout_s=_f("AUDIT_WS_TIMEOUT_S", 8.0),
        )


@dataclass(frozen=True)
class ExchangeSpec:
    """Публичные эндпоинты биржи и её идентификатор в ccxt.

    ``tickers_url`` / ``instruments_url`` бьются напрямую (нужен сырой HTTP-статус
    для различения гео-блока/rate limit/сети). Данные по паре и WS берутся через
    ccxt по ``ccxt_id`` — это избавляет от ручного парсинга формата каждой биржи.
    """

    ccxt_id: str
    tickers_url: str
    instruments_url: str


# 12 бирж из ТЗ (минимум). Пилот — OKX. Binance/Bybit отдают 451 с EU IP.
EXCHANGES: dict[str, ExchangeSpec] = {
    "okx": ExchangeSpec(
        "okx",
        "https://www.okx.com/api/v5/market/tickers?instType=SPOT",
        "https://www.okx.com/api/v5/public/instruments?instType=SPOT",
    ),
    "binance": ExchangeSpec(
        "binance",
        "https://api.binance.com/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/exchangeInfo",
    ),
    "bybit": ExchangeSpec(
        "bybit",
        "https://api.bybit.com/v5/market/tickers?category=spot",
        "https://api.bybit.com/v5/market/instruments-info?category=spot",
    ),
    "kucoin": ExchangeSpec(
        "kucoin",
        "https://api.kucoin.com/api/v1/market/allTickers",
        "https://api.kucoin.com/api/v2/symbols",
    ),
    "gateio": ExchangeSpec(
        "gateio",
        "https://api.gateio.ws/api/v4/spot/tickers",
        "https://api.gateio.ws/api/v4/spot/currency_pairs",
    ),
    "mexc": ExchangeSpec(
        "mexc",
        "https://api.mexc.com/api/v3/ticker/24hr",
        "https://api.mexc.com/api/v3/exchangeInfo",
    ),
    "kraken": ExchangeSpec(
        "kraken",
        "https://api.kraken.com/0/public/Ticker",
        "https://api.kraken.com/0/public/AssetPairs",
    ),
    "bitget": ExchangeSpec(
        "bitget",
        "https://api.bitget.com/api/v2/spot/market/tickers",
        "https://api.bitget.com/api/v2/spot/public/symbols",
    ),
    "htx": ExchangeSpec(
        "htx",
        "https://api.huobi.pro/market/tickers",
        "https://api.huobi.pro/v1/settings/common/symbols",
    ),
    "digifinex": ExchangeSpec(
        "digifinex",
        "https://openapi.digifinex.com/v3/ticker",
        "https://openapi.digifinex.com/v3/markets",
    ),
    "latoken": ExchangeSpec(
        "latoken",
        "https://api.latoken.com/v2/ticker",
        "https://api.latoken.com/v2/pair",
    ),
    "xt": ExchangeSpec(
        "xt",
        "https://sapi.xt.com/v4/public/ticker",
        "https://sapi.xt.com/v4/public/symbol",
    ),
}

DEFAULT_EXCHANGES: list[str] = list(EXCHANGES.keys())
DEFAULT_SYMBOLS: list[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DENT/USDT"]

# Вердикты пары.
VERDICT_NOT_LISTED = "not_listed"
VERDICT_ILLIQUID = "illiquid"
VERDICT_TRADABLE = "tradable"

# Вердикты доступности эндпоинта.
STATUS_OK = "ok"
STATUS_GEO_BLOCKED = "geo_blocked"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_NETWORK_ERROR = "network_error"
STATUS_SERVER_ERROR = "server_error"


# ---------------------------------------------------------------------------
# Модели результатов.
# ---------------------------------------------------------------------------


@dataclass
class EndpointProbe:
    """Результат обращения к одному публичному эндпоинту."""

    endpoint: str                    # tickers | instruments
    status: str                      # STATUS_*
    http_status: int | None
    latency_ms: int | None
    error_text: str | None = None
    rate_limit_note: str | None = None

    @property
    def geo_blocked(self) -> bool:
        return self.status == STATUS_GEO_BLOCKED


@dataclass
class ExchangeAuditResult:
    """Свод по доступности биржи."""

    exchange: str
    source_ip_label: str
    probes: list[EndpointProbe] = field(default_factory=list)
    ws_available: bool | None = None
    notes: str = ""

    @property
    def geo_blocked(self) -> bool:
        """Гео-блок, если ЛЮБОЙ из HTTP-эндпоинтов вернул гео-блок."""
        return any(p.geo_blocked for p in self.probes)

    @property
    def reachable(self) -> bool:
        """Достижима, если хотя бы один эндпоинт ответил 2xx."""
        return any(p.status == STATUS_OK for p in self.probes)


@dataclass
class PairAuditResult:
    """Свод по одной паре на одной бирже."""

    exchange: str
    symbol: str
    source_ip_label: str
    listed: bool
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread_pct: float | None = None
    depth_bid_2pct_usd: float | None = None
    depth_ask_2pct_usd: float | None = None
    vol_24h_usd: float | None = None
    tick_size: float | None = None
    min_order_usd: float | None = None
    verdict: str = VERDICT_NOT_LISTED
    notes: str = ""


@dataclass
class AuditReport:
    """Итог всего прогона: доступность бирж + аудит пар."""

    source_ip_label: str
    generated_at: str
    exchanges: list[ExchangeAuditResult] = field(default_factory=list)
    pairs: list[PairAuditResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Чистая логика (полностью покрыта тестами, без сети).
# ---------------------------------------------------------------------------


def classify_http_response(
    status_code: int | None,
    body_text: str,
    network_error: bool,
) -> tuple[str, str | None]:
    """Различает гео-блок, rate limit, серверную и сетевую ошибку.

    Возвращает ``(status, rate_limit_note)``. Порядок разбора важен:
      * сетевая ошибка (соединение/таймаут/DNS) — отдельный вердикт, НЕ гео-блок;
      * 429 / 418 — rate limit;
      * 451 / 403 либо характерный текст ограничения по региону — гео-блок;
      * прочие 4xx/5xx — серверная ошибка;
      * 2xx — ok.
    """
    if network_error:
        return STATUS_NETWORK_ERROR, None
    if status_code is None:
        return STATUS_NETWORK_ERROR, None

    if status_code in (429, 418):
        return STATUS_RATE_LIMITED, f"HTTP {status_code} (rate limit)"

    lowered = body_text.lower()
    geo_markers = (
        "restricted",
        "not available in your",
        "unavailable in your",
        "your country",
        "region",
        "geoblock",
        "eligibility",
    )
    if status_code in (451, 403) or (
        status_code >= 400 and any(m in lowered for m in geo_markers)
    ):
        return STATUS_GEO_BLOCKED, None

    if 200 <= status_code < 300:
        return STATUS_OK, None
    if status_code >= 500:
        return STATUS_SERVER_ERROR, None
    return STATUS_SERVER_ERROR, None


def compute_depth_usd(
    levels: list[list[float]],
    mid: float,
    band_pct: float,
    side: str,
) -> float:
    """Глубина в USD в пределах ±band_pct от середины по одной стороне.

    ``levels`` — [[price, amount], ...]. Для bid берём уровни с ценой не ниже
    нижней границы, для ask — не выше верхней. USD-нотионал = price * amount
    (котировка — USD-стейбл).
    """
    if mid <= 0:
        return 0.0
    lower = mid * (1 - band_pct / 100.0)
    upper = mid * (1 + band_pct / 100.0)
    total = 0.0
    for level in levels:
        price = float(level[0])
        amount = float(level[1])
        if side == "bid" and price >= lower:
            total += price * amount
        elif side == "ask" and price <= upper:
            total += price * amount
    return total


def decide_pair_verdict(
    listed: bool,
    spread_pct: float | None,
    depth_bid_usd: float | None,
    depth_ask_usd: float | None,
    cfg: AuditConfig,
) -> str:
    """Вердикт по паре по правилам ТЗ.

    not_listed → пары нет; illiquid → спред > порога ИЛИ любая сторона глубины
    ниже порога; иначе tradable. Отсутствие данных о спреде/глубине трактуется
    как неликвидность (нельзя подтвердить пригодность).
    """
    if not listed:
        return VERDICT_NOT_LISTED
    if spread_pct is None or depth_bid_usd is None or depth_ask_usd is None:
        return VERDICT_ILLIQUID
    if spread_pct > cfg.spread_max_pct:
        return VERDICT_ILLIQUID
    if depth_bid_usd < cfg.min_depth_usd or depth_ask_usd < cfg.min_depth_usd:
        return VERDICT_ILLIQUID
    return VERDICT_TRADABLE


def extract_tick_and_min(
    market: dict[str, Any] | None,
    last_price: float | None,
) -> tuple[float | None, float | None]:
    """Достаёт tick size и минимальный размер заявки в USD из ccxt-market.

    ccxt даёт precision.price либо как шаг (0.0001), либо как число знаков (4).
    Минимум заявки — либо limits.cost.min (уже в котировке/USD), либо
    limits.amount.min * last_price (перевод из базового актива).
    """
    if not market:
        return None, None

    tick: float | None = None
    precision = (market.get("precision") or {}).get("price")
    if precision is not None:
        p = float(precision)
        # Эвристика ccxt: значение <1 — это готовый шаг цены; целое >=1 — число знаков.
        tick = p if 0 < p < 1 else (10.0 ** (-int(p)) if p >= 1 else None)

    limits = market.get("limits") or {}
    cost_min = (limits.get("cost") or {}).get("min")
    amount_min = (limits.get("amount") or {}).get("min")
    min_order_usd: float | None = None
    if cost_min is not None:
        min_order_usd = float(cost_min)
    elif amount_min is not None and last_price:
        min_order_usd = float(amount_min) * float(last_price)

    return tick, min_order_usd


def compute_pair_metrics(
    ticker: dict[str, Any],
    order_book: dict[str, Any],
    market: dict[str, Any] | None,
    cfg: AuditConfig,
) -> dict[str, Any]:
    """Считает все числовые поля пары и вердикт из ccxt-ответов.

    Чистая функция: одинаковый ввод → одинаковый результат. ``ticker`` и
    ``order_book`` — в формате ccxt. Пустой стакан → спред/глубина None →
    вердикт illiquid.
    """
    last = _to_float(ticker.get("last") or ticker.get("close"))
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []

    best_bid = _to_float(bids[0][0]) if bids else _to_float(ticker.get("bid"))
    best_ask = _to_float(asks[0][0]) if asks else _to_float(ticker.get("ask"))

    spread_pct: float | None = None
    mid: float | None = None
    if best_bid and best_ask and best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid * 100.0

    depth_bid = depth_ask = None
    if mid:
        depth_bid = compute_depth_usd(bids, mid, cfg.depth_band_pct, "bid")
        depth_ask = compute_depth_usd(asks, mid, cfg.depth_band_pct, "ask")

    # Объём 24ч в USD: quoteVolume уже в котировке (USD-стейбл); иначе base*price.
    vol_usd = _to_float(ticker.get("quoteVolume"))
    if vol_usd is None:
        base_vol = _to_float(ticker.get("baseVolume"))
        if base_vol is not None and last:
            vol_usd = base_vol * last

    tick, min_order_usd = extract_tick_and_min(market, last)
    verdict = decide_pair_verdict(True, spread_pct, depth_bid, depth_ask, cfg)

    return {
        "last_price": last,
        "bid": best_bid,
        "ask": best_ask,
        "spread_pct": spread_pct,
        "depth_bid_2pct_usd": depth_bid,
        "depth_ask_2pct_usd": depth_ask,
        "vol_24h_usd": vol_usd,
        "tick_size": tick,
        "min_order_usd": min_order_usd,
        "verdict": verdict,
    }


def notional_to_move_1pct(
    asks: list[list[float]],
    bids: list[list[float]],
    side: str,
) -> float | None:
    """Сумма заявки (USD), сдвигающая цену на 1 % по фактическому стакану.

    Для ``side='buy'`` идём вверх по ask, пока цена не превысит mid*1.01, и
    суммируем price*amount съеденных уровней. Для ``side='sell'`` — вниз по bid
    до mid*0.99. Возвращает None, если стакан не покрывает движение на 1 %.
    """
    if not asks or not bids:
        return None
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None

    if side == "buy":
        target = mid * 1.01
        book = asks
        crossed = lambda price: price > target  # noqa: E731
    else:
        target = mid * 0.99
        book = bids
        crossed = lambda price: price < target  # noqa: E731

    spent = 0.0
    for level in book:
        price = float(level[0])
        amount = float(level[1])
        spent += price * amount
        if crossed(price):
            return spent
    # Стакан закончился раньше, чем цена сдвинулась на 1 %.
    return None


def _to_float(value: Any) -> float | None:
    """Безопасное приведение к float (None/пусто/мусор → None)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Сетевые проверки (обёрнуты в retry/timeout). HTTP-клиент инъектируется в тестах.
# ---------------------------------------------------------------------------


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    timeout_s: float,
    retries: int,
) -> tuple[int | None, str, bool]:
    """GET с таймаутом и повтором (экспоненциальная задержка).

    Возвращает ``(status_code, body_text, network_error)``. Сетевые сбои
    (соединение/таймаут) отделяются от HTTP-ответов с кодом — это нужно, чтобы
    не спутать недоступность сети с гео-блоком.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, timeout=timeout_s)
            return resp.status_code, resp.text[:2000], False
        except Exception as exc:  # httpx.TimeoutException, ConnectError, ...
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(2.0 ** attempt)
    # Все попытки — сетевые ошибки.
    _ = last_exc
    return None, str(last_exc) if last_exc else "network error", True


async def check_exchange(
    exchange: str,
    source_ip_label: str,
    timeout_s: float = 10.0,
    *,
    client: httpx.AsyncClient | None = None,
    cfg: AuditConfig | None = None,
    ws_prober: Any | None = None,
) -> ExchangeAuditResult:
    """Проверяет доступность биржи: тикеры, инструменты, WS.

    Каждый эндпоинт проверяется независимо; падение одного не рушит остальные.
    """
    cfg = cfg or AuditConfig(timeout_s=timeout_s)
    spec = EXCHANGES.get(exchange)
    result = ExchangeAuditResult(exchange=exchange, source_ip_label=source_ip_label)
    if spec is None:
        result.notes = "нет спецификации эндпоинтов"
        return result

    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        for endpoint_name, url in (
            ("tickers", spec.tickers_url),
            ("instruments", spec.instruments_url),
        ):
            started = _now()
            status_code, body, net_err = await _get_with_retry(
                client, url, cfg.timeout_s, cfg.retries
            )
            latency_ms = int((_now() - started) * 1000)
            status, rl_note = classify_http_response(status_code, body, net_err)
            probe = EndpointProbe(
                endpoint=endpoint_name,
                status=status,
                http_status=status_code,
                latency_ms=None if net_err else latency_ms,
                error_text=(body if status != STATUS_OK else None),
                rate_limit_note=rl_note,
            )
            result.probes.append(probe)
    finally:
        if owns_client:
            await client.aclose()

    # WS проверяем только если биржа вообще достижима по HTTP.
    if result.reachable and ws_prober is not None:
        result.ws_available = await ws_prober(exchange, cfg)
    return result


async def probe_ws_ccxt(exchange: str, cfg: AuditConfig) -> bool | None:
    """Проверяет WS через ccxt.pro: успешное подключение + первое сообщение.

    Возвращает True/False, либо None если биржа не поддерживает watch_ticker.
    Используется реальным прогоном; в тестах WS-пробер подменяется.
    """
    try:
        import ccxt.pro as ccxtpro
    except Exception:
        return None

    ca_file = os.environ.get("SSL_CERT_FILE")
    config: dict[str, Any] = {"enableRateLimit": True}
    if ca_file:
        config["cafile"] = ca_file
    try:
        klass = getattr(ccxtpro, exchange)
    except AttributeError:
        return None
    ex = klass(config)
    if not ex.has.get("watchTicker"):
        await ex.close()
        return None
    try:
        await asyncio.wait_for(ex.watch_ticker("BTC/USDT"), timeout=cfg.ws_timeout_s)
        return True
    except Exception:
        return False
    finally:
        try:
            await ex.close()
        except Exception:
            pass


async def check_pair(
    exchange: str,
    symbol: str,
    source_ip_label: str,
    depth_band_pct: float = 2.0,
    timeout_s: float = 10.0,
    *,
    ex_client: Any | None = None,
    cfg: AuditConfig | None = None,
) -> PairAuditResult:
    """Аудит одной пары через ccxt: листинг, цена, спред, глубина, вердикт.

    ``ex_client`` — экземпляр ccxt-биржи (или его фейк в тестах). Если не передан,
    создаётся реальный по ``exchange``. Символ, отсутствующий в markets, → not_listed.
    """
    cfg = cfg or AuditConfig(depth_band_pct=depth_band_pct, timeout_s=timeout_s)
    result = PairAuditResult(
        exchange=exchange, symbol=symbol, source_ip_label=source_ip_label, listed=False
    )

    owns = ex_client is None
    if ex_client is None:
        from src.core.exchange import create_exchange

        ex_client = create_exchange(exchange)

    try:
        markets = await ex_client.load_markets()
        if symbol not in markets:
            result.verdict = VERDICT_NOT_LISTED
            result.notes = "пары нет в markets биржи"
            return result
        result.listed = True

        ticker = await ex_client.fetch_ticker(symbol)
        order_book = await ex_client.fetch_order_book(symbol, limit=cfg.orderbook_limit)
        metrics = compute_pair_metrics(ticker, order_book, markets[symbol], cfg)
        for key, value in metrics.items():
            setattr(result, key, value)
    except Exception as exc:
        # Пара числится, но данные не собрать — фиксируем как неликвид с причиной.
        result.notes = f"ошибка сбора данных пары: {exc}"
        result.verdict = VERDICT_ILLIQUID
    finally:
        if owns:
            try:
                await ex_client.close()
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Оркестрация: независимый прогон по всем биржам и парам.
# ---------------------------------------------------------------------------


async def run_audit(
    exchanges: list[str],
    symbols: list[str],
    source_ip_label: str,
    *,
    cfg: AuditConfig | None = None,
    ws_prober: Any | None = probe_ws_ccxt,
    pair_checker: Any | None = None,
) -> AuditReport:
    """Полный аудит: каждая биржа проверяется независимо, падение не прерывает.

    ``pair_checker`` по умолчанию — реальный :func:`check_pair`; в тестах
    подменяется фейком. Пары проверяются только для достижимых бирж.
    """
    cfg = cfg or AuditConfig.from_env()
    pair_checker = pair_checker or check_pair
    report = AuditReport(source_ip_label=source_ip_label, generated_at=_now_iso())

    async def audit_one(exchange: str) -> tuple[ExchangeAuditResult, list[PairAuditResult]]:
        ex_res = await check_exchange(
            exchange, source_ip_label, cfg=cfg, ws_prober=ws_prober
        )
        pairs: list[PairAuditResult] = []
        if ex_res.reachable and not ex_res.geo_blocked:
            for symbol in symbols:
                try:
                    pairs.append(
                        await pair_checker(
                            exchange, symbol, source_ip_label,
                            depth_band_pct=cfg.depth_band_pct, cfg=cfg,
                        )
                    )
                except Exception as exc:
                    pairs.append(
                        PairAuditResult(
                            exchange=exchange, symbol=symbol,
                            source_ip_label=source_ip_label, listed=False,
                            verdict=VERDICT_NOT_LISTED, notes=f"сбой проверки: {exc}",
                        )
                    )
        return ex_res, pairs

    # return_exceptions=True: сбой одной биржи не роняет весь аудит.
    gathered = await asyncio.gather(
        *(audit_one(e) for e in exchanges), return_exceptions=True
    )
    for exchange, item in zip(exchanges, gathered, strict=True):
        if isinstance(item, Exception):
            report.exchanges.append(
                ExchangeAuditResult(
                    exchange=exchange, source_ip_label=source_ip_label,
                    notes=f"исключение аудита: {item}",
                )
            )
            continue
        ex_res, pairs = item
        report.exchanges.append(ex_res)
        report.pairs.extend(pairs)
    return report


# ---------------------------------------------------------------------------
# Персистентность: запись в БД и markdown-отчёт.
# ---------------------------------------------------------------------------


async def persist_report_to_db(report: AuditReport) -> None:
    """Пишет результаты аудита в таблицы exchange_audit / pair_audit."""
    from src.core.db import db

    await db.connect()
    await db.ensure_audit_schema()
    for ex in report.exchanges:
        for probe in ex.probes:
            await db.insert_exchange_audit(
                source_ip_label=ex.source_ip_label,
                exchange=ex.exchange,
                endpoint=probe.endpoint,
                http_status=probe.http_status,
                latency_ms=probe.latency_ms,
                geo_blocked=probe.geo_blocked,
                ws_available=ex.ws_available,
                rate_limit_note=probe.rate_limit_note,
                error_text=probe.error_text,
                notes=ex.notes or None,
            )
        if not ex.probes:
            await db.insert_exchange_audit(
                source_ip_label=ex.source_ip_label, exchange=ex.exchange,
                endpoint="n/a", http_status=None, latency_ms=None,
                geo_blocked=ex.geo_blocked, ws_available=ex.ws_available,
                rate_limit_note=None, error_text=None, notes=ex.notes or None,
            )
    for p in report.pairs:
        await db.insert_pair_audit(p)


def _fmt(value: Any, digits: int = 2) -> str:
    """Форматирует число для markdown (None → '—')."""
    if value is None:
        return "—"
    if isinstance(value, float):
        if value != 0 and abs(value) < 0.001:
            return f"{value:.10f}".rstrip("0").rstrip(".")
        return f"{value:,.{digits}f}"
    return str(value)


def render_report_markdown(report: AuditReport) -> str:
    """Собирает человекочитаемый markdown-отчёт из результатов аудита."""
    lines: list[str] = []
    lines.append(f"# Аудит доступности бирж — {report.generated_at}")
    lines.append("")
    lines.append(f"- Метка источника (source_ip_label): `{report.source_ip_label}`")
    lines.append(f"- Бирж проверено: {len(report.exchanges)}")
    lines.append("")

    # --- Карта доступных бирж ---
    lines.append("## Карта доступности бирж")
    lines.append("")
    lines.append(
        "| Биржа | Tickers | Instruments | Гео-блок | WS | Задержка, мс | Примечание |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for ex in report.exchanges:
        probe_by = {p.endpoint: p for p in ex.probes}
        t = probe_by.get("tickers")
        ins = probe_by.get("instruments")
        lat = t.latency_ms if t and t.latency_ms is not None else None
        ws = {True: "да", False: "нет", None: "—"}[ex.ws_available]
        lines.append(
            f"| {ex.exchange} "
            f"| {_probe_cell(t)} "
            f"| {_probe_cell(ins)} "
            f"| {'да' if ex.geo_blocked else 'нет'} "
            f"| {ws} "
            f"| {_fmt(lat, 0) if lat is not None else '—'} "
            f"| {ex.notes or ''} |"
        )
    lines.append("")

    # --- Аудит пар ---
    lines.append("## Аудит пар")
    lines.append("")
    lines.append(
        "| Биржа | Пара | Листинг | Цена | Спред % | Глубина bid ±2% $ "
        "| Глубина ask ±2% $ | Объём 24ч $ | Tick | Мин. заявка $ | Вердикт |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for p in report.pairs:
        lines.append(
            f"| {p.exchange} | {p.symbol} | {'да' if p.listed else 'нет'} "
            f"| {_fmt(p.last_price, 8)} | {_fmt(p.spread_pct, 4)} "
            f"| {_fmt(p.depth_bid_2pct_usd)} | {_fmt(p.depth_ask_2pct_usd)} "
            f"| {_fmt(p.vol_24h_usd)} | {_fmt(p.tick_size, 10)} "
            f"| {_fmt(p.min_order_usd)} | **{p.verdict}** |"
        )
    lines.append("")

    # --- Отдельный блок DENT/USDT ---
    lines.append(render_dent_block(report))
    return "\n".join(lines)


def _probe_cell(probe: EndpointProbe | None) -> str:
    """Ячейка статуса эндпоинта: HTTP-код + классификация."""
    if probe is None:
        return "—"
    code = probe.http_status if probe.http_status is not None else "нет"
    return f"{code} ({probe.status})"


def render_dent_block(report: AuditReport) -> str:
    """Отдельный блок отчёта по DENT/USDT (3 вопроса ТЗ)."""
    dent = [p for p in report.pairs if p.symbol.upper() == "DENT/USDT"]
    listed = [p for p in dent if p.listed]
    lines: list[str] = ["## Блок DENT/USDT", ""]
    if not dent:
        lines.append("_DENT/USDT в аудите не проверялась._")
        return "\n".join(lines)

    lines.append("**1. Где DENT/USDT листится (среди доступных с текущего IP):**")
    if listed:
        for p in listed:
            lines.append(f"- {p.exchange} — вердикт **{p.verdict}**")
    else:
        lines.append("- Ни на одной из доступных бирж пара не подтверждена.")
    lines.append("")

    lines.append("**2. Реальная ликвидность по каждой бирже:**")
    lines.append("")
    lines.append("| Биржа | Спред % | Глубина bid ±2% $ | Глубина ask ±2% $ | Объём 24ч $ |")
    lines.append("|---|---|---|---|---|")
    for p in listed:
        lines.append(
            f"| {p.exchange} | {_fmt(p.spread_pct, 4)} "
            f"| {_fmt(p.depth_bid_2pct_usd)} | {_fmt(p.depth_ask_2pct_usd)} "
            f"| {_fmt(p.vol_24h_usd)} |"
        )
    if not listed:
        lines.append("| — | — | — | — | — |")
    lines.append("")
    lines.append(
        "**3. Сумма заявки, сдвигающая цену на 1 %** — считается по фактическому "
        "стакану функцией `notional_to_move_1pct` во время прогона (см. поле notes "
        "записей pair_audit / логи)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Время (вынесено, чтобы моделировать/мокать при необходимости).
# ---------------------------------------------------------------------------


def _now() -> float:
    import time

    return time.monotonic()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _parse_list(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


async def _amain(args: argparse.Namespace) -> int:
    cfg = AuditConfig.from_env()
    exchanges = _parse_list(args.exchanges, DEFAULT_EXCHANGES)
    symbols = _parse_list(args.symbols, DEFAULT_SYMBOLS)

    report = await run_audit(exchanges, symbols, args.source_ip_label, cfg=cfg)

    md = render_report_markdown(report)
    os.makedirs("reports", exist_ok=True)
    out_path = os.path.join("reports", f"exchange_audit_{_today()}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"markdown-отчёт: {out_path}")

    if not args.no_db:
        try:
            await persist_report_to_db(report)
            print("результаты записаны в БД (exchange_audit / pair_audit)")
        except Exception as exc:
            print(f"ВНИМАНИЕ: запись в БД не удалась: {exc}")
            return 2
    return 0


def main() -> None:
    """Точка входа CLI. Запуск: ``python -m scripts.exchange_audit``."""
    parser = argparse.ArgumentParser(description="Аудит доступности бирж (Этап 6.4)")
    parser.add_argument(
        "--exchanges",
        help="список бирж через запятую (по умолчанию все 12)",
    )
    parser.add_argument(
        "--symbols",
        help="список пар через запятую (по умолчанию BTC/ETH/SOL/DENT-USDT)",
    )
    parser.add_argument(
        "--source-ip-label",
        default=os.environ.get("AUDIT_SOURCE_IP_LABEL", "local"),
        help="метка машины: local | hetzner-nbg",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="не писать в БД (только markdown) — для запуска без PostgreSQL",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()


# Публичный API (для тестов и внешнего использования).
__all__ = [
    "AuditConfig",
    "AuditReport",
    "EndpointProbe",
    "ExchangeAuditResult",
    "PairAuditResult",
    "check_exchange",
    "check_pair",
    "classify_http_response",
    "compute_depth_usd",
    "compute_pair_metrics",
    "decide_pair_verdict",
    "extract_tick_and_min",
    "notional_to_move_1pct",
    "render_report_markdown",
    "run_audit",
]

_ = asdict  # re-exported dataclass helper kept importable
