"""Разовый скрипт «разбор сигнала под капотом».

Берёт ТЕКУЩИЕ реальные данные по BTC с биржи (по умолчанию OKX), прогоняет их
через те же чистые функции, что и боевые агенты
(``analyze_ohlcv`` / ``analyze_orderbook`` / ``analyze_futures`` / ``make_decision``),
печатает подробный отчёт на русском и (если не указан ``--no-db``) записывает
сигнал в таблицу ``signals`` — ровно как это делает Decision Agent, чтобы через
1ч/4ч его автоматически дооценил evaluator.

ВАЖНО: скрипт НЕ трогает основной пайплайн. Он только:
  * читает данные с биржи напрямую (свой ccxt-клиент);
  * вызывает уже существующие чистые функции агентов;
  * пишет одну строку в ``signals`` тем же набором колонок, что и
    ``db.save_signal`` (плюс RETURNING id, чтобы знать signal_id), с пометкой
    «[РУЧНОЙ РАЗБОР]» в rationale.

Запуск (в окружении с доступом к бирже и БД):
    python -m scripts.explain_signal
    python scripts/explain_signal.py --no-db   # без записи в БД (только отчёт)

Биржу можно переопределить переменной EXPLAIN_EXCHANGE (по умолчанию берётся
EXCHANGE из конфигурации; если та не задана явно — okx).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

# Чистые функции боевых агентов — переиспользуем как есть, ничего не дублируя.
from src.agents.futures import (
    _FUNDING_EXTREME,
    _OI_RISE_PCT,
    analyze_futures,
)
from src.agents.liquidity import (
    _IMBALANCE_THRESHOLD,
    _MIN_SNAPSHOTS,
    _WIDE_SPREAD_REL,
    analyze_orderbook,
)
from src.agents.market import (
    _ADX_TREND_MIN,
    _RSI_BEAR,
    _RSI_BULL,
    analyze_ohlcv,
)
from src.core.config import settings
from src.core.db import compute_orderbook_metrics, db
from src.core.exchange import create_exchange
from src.decision.agent import _SIGNAL_VALUE, make_decision

MSK = ZoneInfo("Europe/Moscow")

# Сколько снимков стакана собрать «живьём» и пауза между ними (сек).
_OB_SNAPSHOTS = max(_MIN_SNAPSHOTS + 1, 6)
_OB_PAUSE_SEC = 1.5
# Период и глубина истории open interest (для оценки динамики OI).
_OI_HISTORY_TF = "5m"
_OI_HISTORY_LIMIT = 30
# Сколько 1m-свечей засеять в БД (чтобы у evaluator была цена на момент сигнала).
_SEED_1M_LIMIT = 200

# Перевод направления агента в человекочитаемую метку.
_DIR_RU = {"bullish": "рост (бычий)", "bearish": "падение (медвежий)",
           "neutral": "нейтрально", "insufficient_data": "недостаточно данных"}


# --------------------------------------------------------------------------- #
# Сбор реальных данных с биржи
# --------------------------------------------------------------------------- #
async def gather_market_data(ex: Any, symbol: str, timeframe: str, min_candles: int):
    """Свечи нужного таймфрейма (с запасом на прогрев EMA200)."""
    limit = max(min_candles, 200) + 60
    candles = await ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(
        candles, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    return df, candles


async def gather_orderbook_snapshots(ex: Any, symbol: str, depth: int):
    """Собирает несколько «живых» снимков стакана подряд (как боевой коллектор)."""
    snapshots: list[dict[str, Any]] = []
    for i in range(_OB_SNAPSHOTS):
        ob = await ex.fetch_order_book(symbol, limit=depth)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        spread, bid_vol, ask_vol = compute_orderbook_metrics(bids, asks)
        snapshots.append(
            {
                "ts": datetime.now(UTC),
                "bids": bids,
                "asks": asks,
                "spread": spread,
                "bid_volume": bid_vol,
                "ask_volume": ask_vol,
            }
        )
        if i < _OB_SNAPSHOTS - 1:
            await asyncio.sleep(_OB_PAUSE_SEC)
    return snapshots


async def gather_futures_data(ex: Any, swap_symbol: str):
    """Funding rate (текущий) и история open interest. Возвращает (funding, oi, meta)."""
    meta: dict[str, Any] = {}
    funding: list[dict[str, Any]] = []
    try:
        fr = await ex.fetch_funding_rate(swap_symbol)
        rate = fr.get("fundingRate")
        if rate is not None:
            funding.append({"rate": float(rate), "ts": datetime.now(UTC)})
        meta["funding_interval"] = fr.get("interval")
        meta["next_funding_ts"] = fr.get("fundingTimestamp") or fr.get("nextFundingTime")
    except Exception as exc:  # noqa: BLE001
        meta["funding_error"] = str(exc)

    oi: list[dict[str, Any]] = []
    try:
        hist = await ex.fetch_open_interest_history(
            swap_symbol, _OI_HISTORY_TF, limit=_OI_HISTORY_LIMIT
        )
        for h in hist:
            val = (
                h.get("openInterestValue")
                if h.get("openInterestValue") is not None
                else h.get("openInterestAmount")
                if h.get("openInterestAmount") is not None
                else h.get("openInterest")
            )
            if val is not None:
                oi.append({"value": float(val), "ts": h.get("timestamp")})
        meta["oi_unit"] = "USD (openInterestValue)" if hist else "—"
        meta["oi_history_tf"] = _OI_HISTORY_TF
    except Exception as exc:  # noqa: BLE001
        meta["oi_error"] = str(exc)
        # Фолбэк: один моментальный снимок OI (истории не хватит для динамики).
        try:
            cur = await ex.fetch_open_interest(swap_symbol)
            val = cur.get("openInterestAmount") or cur.get("openInterestValue")
            if val is not None:
                oi.append({"value": float(val), "ts": cur.get("timestamp")})
            meta["oi_unit"] = "контракты (openInterestAmount, моментальный)"
        except Exception as exc2:  # noqa: BLE001
            meta["oi_error2"] = str(exc2)
    return funding, oi, meta


# --------------------------------------------------------------------------- #
# Рендеринг отчёта
# --------------------------------------------------------------------------- #
def _fmt(x: Any, nd: int = 2) -> str:
    try:
        return f"{float(x):,.{nd}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(x)


def _rsi_note(rsi: float) -> str:
    if rsi >= 70:
        return "зона перекупленности (>70) — риск отката"
    if rsi >= _RSI_BULL:
        return f"выше {_RSI_BULL:.0f} — бычий уклон, но не экстремум"
    if rsi <= 30:
        return "зона перепроданности (<30) — возможен отскок"
    if rsi <= _RSI_BEAR:
        return f"ниже {_RSI_BEAR:.0f} — медвежий уклон"
    return f"в нейтральной зоне ({_RSI_BEAR:.0f}–{_RSI_BULL:.0f})"


def _adx_note(adx: float) -> str:
    if adx >= 40:
        return "очень сильный тренд (≥40)"
    if adx >= 25:
        return "выраженный тренд (25–40)"
    if adx >= _ADX_TREND_MIN:
        return f"умеренный тренд ({_ADX_TREND_MIN:.0f}–25)"
    return f"тренда почти нет (<{_ADX_TREND_MIN:.0f}) — рынок во флэте"


def compute_trade_levels(
    decision: str,
    price: float | None,
    support: float | None,
    resistance: float | None,
    atr: float | None,
    *,
    atr_mult: float = 1.5,
    min_rr: float = 1.5,
) -> dict[str, Any]:
    """Чистая функция расчёта торговых уровней из S/R и ATR. Детерминирована.

    Считает только на основе того, что уже добывает Market Agent: ближайшая
    поддержка, ближайшее сопротивление и ATR(14). За вход берётся текущая цена.

    Для buy: зона входа = [поддержка; цена], стоп = поддержка − atr_mult×ATR,
    цель = сопротивление. Для sell — зеркально. R/R = потенциал / риск.

    Возвращает dict со ``status``:
      * ``wait``         — направления нет, уровни не считаем;
      * ``insufficient`` — нет S/R или ATR (с полем ``reason``);
      * ``ok``           — полный расклад уровней.
    """
    if decision == "wait":
        return {"status": "wait"}
    if (
        price is None or price <= 0
        or atr is None or atr <= 0
        or support is None or support <= 0
        or resistance is None or resistance <= 0
    ):
        return {
            "status": "insufficient",
            "reason": "нет уровней поддержки/сопротивления или недоступен ATR",
        }
    # Поддержка должна быть ниже цены, сопротивление — выше (иначе уровни вырождены).
    if not support < price < resistance:
        return {
            "status": "insufficient",
            "reason": (
                "поддержка/сопротивление не охватывают текущую цену "
                "(нет поддержки ниже или сопротивления выше в окне 50 свечей)"
            ),
        }

    buffer = atr_mult * atr
    entry = float(price)
    if decision == "buy":
        zone_low, zone_high = support, entry
        stop = support - buffer
        target = resistance
        risk = entry - stop
        reward = target - entry
    elif decision == "sell":
        zone_low, zone_high = entry, resistance
        stop = resistance + buffer
        target = support
        risk = stop - entry
        reward = entry - target
    else:
        return {"status": "insufficient", "reason": f"неизвестное решение: {decision}"}

    if risk <= 0 or reward <= 0:
        return {
            "status": "insufficient",
            "reason": "вырожденные уровни (риск или потенциал ≤ 0)",
        }

    rr = reward / risk
    return {
        "status": "ok",
        "decision": decision,
        "entry": round(entry, 2),
        "zone_low": round(zone_low, 2),
        "zone_high": round(zone_high, 2),
        "stop": round(stop, 2),
        "stop_dist_pct": round(abs(entry - stop) / entry * 100, 2),
        "stop_dist_atr": round(abs(entry - stop) / atr, 2),
        "target": round(target, 2),
        "target_pct": round(reward / entry * 100, 2),
        "risk_abs": round(risk, 2),
        "reward_abs": round(reward, 2),
        "rr": round(rr, 2),
        "rr_weak": rr < min_rr,
        "min_rr": min_rr,
        "atr": round(float(atr), 2),
        "atr_mult": atr_mult,
        "support": round(float(support), 2),
        "resistance": round(float(resistance), 2),
    }


def render_market(out: dict[str, Any]) -> str:
    sig = out["signal"]
    if sig == "insufficient_data":
        return (
            "## 1. Market Agent — технический анализ (свечи)\n\n"
            f"**Недостаточно данных:** {out['rationale']}\n"
        )
    m = out["metrics"]
    votes = m["votes"]
    close = m["close"]
    ema_stack_txt = {1: "EMA20 > EMA50 > EMA200 (бычий порядок)",
                     -1: "EMA20 < EMA50 < EMA200 (медвежий порядок)",
                     0: "EMA переплетены (нет чёткого порядка)"}[votes["ema_stack"]]
    macd_txt = ("гистограмма > 0 — импульс вверх" if m["macd_hist"] > 0
                else "гистограмма < 0 — импульс вниз" if m["macd_hist"] < 0
                else "гистограмма ≈ 0")
    price_vs_support = (close - m["support"]) / close * 100 if close else 0
    price_vs_res = (m["resistance"] - close) / close * 100 if close else 0
    lines = [
        "## 1. Market Agent — технический анализ (свечи)",
        "",
        f"Таймфрейм: **{m.get('timeframe')}**, свечей в расчёте: **{m['n_candles']}**. "
        f"Цена закрытия последней свечи: **{_fmt(close)} USDT**.",
        "",
        "| Индикатор | Значение | Что показывает |",
        "|---|---|---|",
        f"| EMA20 | {_fmt(m['ema20'])} | быстрая средняя (краткосрочный тренд) |",
        f"| EMA50 | {_fmt(m['ema50'])} | средняя средняя |",
        f"| EMA200 | {_fmt(m['ema200'])} | медленная средняя (долгосрочный тренд) |",
        f"| Порядок EMA | {votes['ema_stack']:+d} | {ema_stack_txt} |",
        f"| Наклон EMA50 (за 10 свечей) | {_fmt(m['ema50_slope'], 2)} | "
        f"{'вверх' if m['ema50_slope'] > 0 else 'вниз' if m['ema50_slope'] < 0 else 'плоско'} |",
        f"| RSI(14) | {_fmt(m['rsi14'])} | {_rsi_note(m['rsi14'])} |",
        f"| ATR(14) | {_fmt(m['atr14'])} | средняя волатильность (амплитуда свечи) |",
        f"| MACD линия | {_fmt(m['macd'], 2)} | {macd_txt} |",
        f"| MACD сигнал | {_fmt(m['macd_signal'], 2)} | сигнальная линия MACD |",
        f"| MACD гистограмма | {_fmt(m['macd_hist'], 2)} | разница линии и сигнала |",
        f"| ADX(14) | {_fmt(m['adx14'])} | {_adx_note(m['adx14'])} |",
        f"| +DI / −DI | {_fmt(m['plus_di'])} / {_fmt(m['minus_di'])} | "
        f"{'покупатели сильнее' if m['plus_di'] > m['minus_di'] else 'продавцы сильнее'} |",
        f"| Поддержка (мин. за 50) | {_fmt(m['support'])} | "
        f"ниже на {price_vs_support:.2f}% от цены |",
        f"| Сопротивление (макс. за 50) | {_fmt(m['resistance'])} | "
        f"выше на {price_vs_res:.2f}% от цены |",
        "",
        "**Как агент пришёл к выводу (5 направленных голосов в {−1, 0, +1}):**",
        "",
        f"- порядок EMA: `{votes['ema_stack']:+d}`",
        f"- наклон EMA50: `{votes['ema_slope']:+d}`",
        f"- MACD: `{votes['macd']:+d}`",
        f"- RSI: `{votes['rsi']:+d}` (порог бычий {_RSI_BULL:.0f} / медвежий {_RSI_BEAR:.0f})",
        f"- +DI vs −DI: `{votes['di']:+d}`",
        "",
        f"Сумма голосов = **{m['score']:+d}** из 5. ADX={_fmt(m['adx14'])} "
        f"(порог силы тренда {_ADX_TREND_MIN:.0f}). "
        f"Согласованность голосов = |{m['score']}|/5 = {abs(m['score'])/5:.2f}.",
        "",
        f"**Итог агента:** направление **{_DIR_RU[sig]}**, "
        f"уверенность **{out['confidence']:.2f}**.",
        f"_{out['rationale']}_",
        "",
    ]
    return "\n".join(lines)


def render_liquidity(out: dict[str, Any]) -> str:
    sig = out["signal"]
    if sig == "insufficient_data":
        return (
            "## 2. Liquidity Agent — анализ стакана\n\n"
            f"**Недостаточно данных:** {out['rationale']}\n"
        )
    m = out["metrics"]
    extra = out.get("_extra", {})
    imb_pct = m["imbalance"] * 100
    avg_pct = m["avg_imbalance"] * 100
    pressure = ("давление покупателей (перевес бидов)" if m["imbalance"] > 0
                else "давление продавцов (перевес асков)" if m["imbalance"] < 0
                else "баланс сторон")
    lines = [
        "## 2. Liquidity Agent — анализ стакана заявок",
        "",
        f"Снимков стакана собрано: **{m['n_snapshots']}** (живьём, с интервалом "
        f"~{_OB_PAUSE_SEC} c). Глубина: топ-{extra.get('depth', '?')} уровней с каждой стороны.",
        "",
        "| Параметр | Значение | Что показывает |",
        "|---|---|---|",
        f"| Объём bids (последний снимок) | {_fmt(m['bid_volume'], 3)} BTC | "
        f"суммарный спрос в стакане |",
        f"| Объём asks (последний снимок) | {_fmt(m['ask_volume'], 3)} BTC | "
        f"суммарное предложение в стакане |",
        f"| Дисбаланс (текущий) | {imb_pct:+.1f}% | {pressure} |",
        f"| Дисбаланс (средний по снимкам) | {avg_pct:+.1f}% | устойчивость перекоса |",
        f"| Разброс дисбаланса (std) | {m['imbalance_std']:.3f} | насколько стабилен перекос |",
        f"| Спред | {_fmt(m['spread'], 2)} USDT | разница лучшей покупки/продажи |",
        f"| Отн. спред | {m['rel_spread']*100:.4f}% | "
        f"порог «неликвидно» {_WIDE_SPREAD_REL*100:.1f}% |",
        f"| «Стена» в бидах | x{m['bid_wall_ratio']:.1f} | крупнейшая заявка к средней |",
        f"| «Стена» в асках | x{m['ask_wall_ratio']:.1f} | крупнейшая заявка к средней |",
        "",
    ]
    walls = extra.get("walls")
    if walls:
        lines.append("**Заметные крупные заявки (последний снимок):**")
        lines.append("")
        for w in walls:
            lines.append(
                f"- {w['side']}: {_fmt(w['amount'], 3)} BTC @ {_fmt(w['price'])} USDT "
                f"(x{w['ratio']:.1f} от средней заявки)"
            )
        lines.append("")
    lines += [
        f"Порог направленного сигнала по дисбалансу — **{_IMBALANCE_THRESHOLD*100:.0f}%**. "
        f"Текущий перекос {imb_pct:+.1f}% → "
        f"{'выше порога' if abs(m['imbalance']) > _IMBALANCE_THRESHOLD else 'ниже порога'}.",
        "",
        f"**Итог агента:** направление **{_DIR_RU[sig]}**, "
        f"уверенность **{out['confidence']:.2f}**.",
        f"_{out['rationale']}_",
        "",
    ]
    return "\n".join(lines)


def render_futures(out: dict[str, Any], meta: dict[str, Any]) -> str:
    sig = out["signal"]
    if sig == "insufficient_data":
        body = [
            "## 3. Futures Agent — деривативы (funding + OI)",
            "",
            f"**Недостаточно данных:** {out['rationale']}",
        ]
        if meta.get("oi_error"):
            body.append(f"\n_История OI недоступна: {meta['oi_error']}_")
        return "\n".join(body) + "\n"
    m = out["metrics"]
    rate = m["funding_rate"]
    funding_side = ("лонги платят шортам (перевес лонгов, бычье позиционирование)"
                    if rate > 0 else
                    "шорты платят лонгам (перевес шортов, медвежье позиционирование)"
                    if rate < 0 else "нейтрально")
    oi_dir = ("растёт" if m["oi_rising"] else "не растёт / падает")
    interval = meta.get("funding_interval") or "?"
    lines = [
        "## 3. Futures Agent — деривативы (funding rate + open interest)",
        "",
        f"Свежих значений funding: **{m['n_funding']}**, точек истории OI: **{m['n_oi']}** "
        f"(шаг {meta.get('oi_history_tf', '?')}, единицы: {meta.get('oi_unit', '?')}).",
        "",
        "| Параметр | Значение | Что показывает |",
        "|---|---|---|",
        f"| Funding rate | {rate:+.6f} ({rate*100:+.4f}%) | {funding_side} |",
        f"| Интервал funding | {interval} | как часто списывается ставка |",
        f"| Funding экстремум? | {'ДА' if m['funding_extreme'] else 'нет'} | "
        f"порог |{_FUNDING_EXTREME:.4f}| ({_FUNDING_EXTREME*100:.2f}%) |",
        f"| OI (начало окна) | {_fmt(m['oi_first'])} | открытый интерес в начале окна |",
        f"| OI (конец окна) | {_fmt(m['oi_last'])} | открытый интерес сейчас |",
        f"| Δ OI | {m['oi_change_pct']:+.2f}% | {oi_dir} (порог роста {_OI_RISE_PCT:.1f}%) |",
        "",
        "**Логика агента:**",
        "",
    ]
    if m["funding_extreme"]:
        lines.append(
            f"- funding **экстремальный** (|{rate:+.6f}| > {_FUNDING_EXTREME}) → "
            f"рынок перегрет, ставка на **разворот** против толпы."
        )
    elif m["oi_rising"] and rate > 0:
        lines.append(
            "- рост OI + положительный funding → приток денег в лонги, "
            "**продолжение роста**."
        )
    elif m["oi_rising"] and rate < 0:
        lines.append(
            "- рост OI + отрицательный funding → приток денег в шорты, "
            "**продолжение снижения**."
        )
    else:
        lines.append(
            "- OI не растёт или funding близок к нулю → **нет подтверждения** тренда, "
            "нейтрально."
        )
    lines += [
        "",
        f"**Итог агента:** направление **{_DIR_RU[sig]}**, "
        f"уверенность **{out['confidence']:.2f}**.",
        f"_{out['rationale']}_",
        "",
    ]
    return "\n".join(lines)


def render_decision(
    outputs: list[dict[str, Any]],
    decision: str,
    probability: float,
    weights: dict[str, float],
    threshold: float,
    min_agents: int,
) -> str:
    """Показывает арифметику Decision Agent с подставленными числами."""
    fresh = [o for o in outputs if o["signal"] in _SIGNAL_VALUE]
    lines = [
        "## 4. Decision Agent — арифметика решения",
        "",
        "Decision Agent сам рынок не анализирует — он только взвешивает выводы "
        "трёх агентов. Формула балла:",
        "",
        "```",
        "balance = Σ(direction_i × confidence_i × weight_i) / Σ(weight_i × confidence_i)",
        "  где direction: bullish=+1, bearish=−1, neutral=0",
        "```",
        "",
        f"Учитываются только свежие выводы с направлением. В расчёте участвуют "
        f"**{len(fresh)}** агент(а/ов) (минимум по конфигу: {min_agents}).",
        "",
        "| Агент | Сигнал | dir | confidence | weight | dir×conf×w | w×conf |",
        "|---|---|---|---|---|---|---|",
    ]
    numerator = 0.0
    denominator = 0.0
    for o in fresh:
        d = _SIGNAL_VALUE[o["signal"]]
        c = float(o["confidence"])
        w = weights.get(o["agent"], 1.0)
        num_i = d * c * w
        den_i = w * c
        numerator += num_i
        denominator += den_i
        lines.append(
            f"| {o['agent']} | {o['signal']} | {d:+d} | {c:.4f} | {w:.2f} | "
            f"{num_i:+.4f} | {den_i:.4f} |"
        )
    score = numerator / denominator if denominator > 0 else 0.0
    lines.append(f"| **Σ** | | | | | **{numerator:+.4f}** | **{denominator:.4f}** |")
    lines.append("")

    # Согласованность направлений.
    dirs = [_SIGNAL_VALUE[o["signal"]] for o in fresh]
    pos = sum(1 for x in dirs if x > 0)
    neg = sum(1 for x in dirs if x < 0)
    agreement = abs(pos - neg) / len(fresh) if fresh else 0.0

    if denominator > 0:
        lines.append(
            f"**Балл = {numerator:+.4f} / {denominator:.4f} = "
            f"`{score:+.4f}`** (диапазон −1…+1)."
        )
    else:
        lines.append("**Балл = 0** (нет вклада: сумма весов×уверенностей равна нулю).")
    lines += [
        "",
        f"Порог решения: **±{threshold:.2f}**.",
        f"- если балл > +{threshold:.2f} → **buy**",
        f"- если балл < −{threshold:.2f} → **sell**",
        "- иначе → **wait**",
        "",
        f"Балл `{score:+.4f}` → итог **{decision.upper()}**.",
        "",
        f"**Согласованность агентов** = |{pos}−{neg}| / {len(fresh)} = "
        f"`{agreement:.2f}` (1.0 = все смотрят в одну сторону).",
        f"**Вероятность** = min(|балл| × (0.5 + 0.5×согл.), 1.0) = "
        f"min({abs(score):.4f} × {0.5 + 0.5*agreement:.2f}, 1.0) = **{probability:.4f}**.",
        "",
    ]
    return "\n".join(lines)


def render_verdict(
    decision: str,
    probability: float,
    price: float,
    outputs: list[dict[str, Any]],
    signal_id: Any,
    created_msk: datetime,
) -> str:
    decision_ru = {"buy": "ПОКУПКА (buy)", "sell": "ПРОДАЖА (sell)",
                   "wait": "ОЖИДАНИЕ (wait)"}[decision]
    parts = []
    for o in outputs:
        parts.append(f"{o['agent']} → {_DIR_RU.get(o['signal'], o['signal'])} "
                     f"({o['confidence']:.2f})")
    summary = "; ".join(parts)
    lines = [
        "## 5. Финальный вердикт",
        "",
        f"- **Решение:** {decision_ru}",
        f"- **Вероятность (уверенность системы):** {probability:.2%}",
        f"- **Цена BTC на момент сигнала:** {_fmt(price)} USDT",
        "",
        f"**Почему именно так:** {summary}.",
        "",
    ]
    if decision == "wait":
        lines.append(
            "Агенты не дали достаточного перевеса в одну сторону (балл не пробил "
            "порог) — система осознанно воздерживается от входа. Такой сигнал "
            "evaluator по дизайну фактом НЕ дооценивает (оцениваются только buy/sell)."
        )
    else:
        side = "вверх" if decision == "buy" else "вниз"
        lines.append(
            f"Большинство факторов указали в одну сторону, балл пробил порог — "
            f"система ждёт движения цены **{side}**. Через 1ч и 4ч evaluator "
            f"автоматически сверит это с фактом (pnl_pct/drawdown_pct/success)."
        )
    lines.append("")
    if signal_id not in (None, "DRYRUN"):
        lines += [
            f"- **signal_id:** `{signal_id}`",
            f"- **Записан (МСК):** {created_msk:%Y-%m-%d %H:%M:%S}",
            f"- **Когда смотреть результат:** через 1 час (~{_plus(created_msk, 1)}) "
            f"и через 4 часа (~{_plus(created_msk, 4)}) МСК — "
            f"evaluator проставит pnl_pct, drawdown_pct, success "
            f"(главный горизонт {settings.EVAL_PRIMARY_HORIZON} закроет сигнал).",
        ]
    elif signal_id == "DRYRUN":
        lines.append("- _Режим --no-db: сигнал в БД не записан, signal_id отсутствует._")
    return "\n".join(lines)


def render_trade_plan(levels: dict[str, Any]) -> str:
    head = [
        "## 6. Торговый план (пилотная логика)",
        "",
        "> ⚠️ **Пилотная логика для разбора со специалистом, НЕ финансовый совет.** "
        "Уровни рассчитаны механически из ближайших поддержки/сопротивления и ATR — "
        "это иллюстрация подхода, а не рекомендация к сделке.",
        "",
    ]
    status = levels.get("status")
    if status == "wait":
        head.append(
            "Решение системы — **wait**: входить не во что, торговые уровни "
            "не рассчитываются."
        )
        return "\n".join(head) + "\n"
    if status != "ok":
        head.append(
            f"**Недостаточно данных для уровней:** {levels.get('reason', '—')}. "
            "Фиктивные цифры не выводим."
        )
        return "\n".join(head) + "\n"

    L = levels
    is_buy = L["decision"] == "buy"
    side = "покупку (buy)" if is_buy else "продажу (sell)"
    verb = "откупать" if is_buy else "продавать"
    zone_from = "поддержки" if is_buy else "текущей цены"
    zone_to = "текущей цены" if is_buy else "сопротивления"
    stop_anchor = "поддержкой" if is_buy else "сопротивлением"
    target_anchor = "сопротивление выше" if is_buy else "поддержка ниже"
    rr_note = (
        f"⚠️ слабое соотношение (хуже 1:{L['min_rr']:.1f}) — потенциал не оправдывает риск"
        if L["rr_weak"]
        else f"приемлемо (не хуже 1:{L['min_rr']:.1f})"
    )
    lines = head + [
        f"Расчёт под **{side}**. За вход принята текущая цена "
        f"**{_fmt(L['entry'])} USDT**. ATR(14) = {_fmt(L['atr'])}, "
        f"буфер стопа = {L['atr_mult']}×ATR.",
        "",
        "| Уровень | Цена | Пояснение |",
        "|---|---|---|",
        f"| Зона входа | {_fmt(L['zone_low'])} – {_fmt(L['zone_high'])} | "
        f"коридор, где разумно {verb}: от {zone_from} до {zone_to} |",
        f"| Стоп | {_fmt(L['stop'])} | за {stop_anchor} на {L['atr_mult']}×ATR; "
        f"{L['stop_dist_pct']:.2f}% ({L['stop_dist_atr']:.2f}×ATR) от входа |",
        f"| Цель | {_fmt(L['target'])} | ближайшее {target_anchor} цены; "
        f"потенциал {L['target_pct']:.2f}% |",
        f"| Риск / Прибыль | **{L['rr']:.2f} : 1** | "
        f"(цель−вход)/(вход−стоп) — {rr_note} |",
        "",
        "**Простыми словами:**",
        f"- **Вход {_fmt(L['entry'])} USDT** — текущая цена; докупать имеет смысл "
        f"в коридоре {_fmt(L['zone_low'])}–{_fmt(L['zone_high'])} USDT.",
        f"- **Стоп {_fmt(L['stop'])} USDT** — если цена дойдёт сюда, идея не "
        f"сработала; убыток ≈ {L['stop_dist_pct']:.2f}% от входа "
        f"(это {L['stop_dist_atr']:.2f} «средних свечей» ATR).",
        f"- **Цель {_fmt(L['target'])} USDT** — ближайший разворотный уровень; "
        f"потенциал ≈ {L['target_pct']:.2f}%.",
        f"- **R/R {L['rr']:.2f}** — на каждый 1 USDT риска приходится "
        f"{L['rr']:.2f} USDT потенциальной прибыли."
        + (
            " Соотношение слабое: по риску сделка невыгодная."
            if L["rr_weak"]
            else ""
        ),
        "",
    ]
    return "\n".join(lines)


def _plus(dt: datetime, hours: int) -> str:
    from datetime import timedelta
    return (dt + timedelta(hours=hours)).strftime("%H:%M")


# --------------------------------------------------------------------------- #
# Сборка полного отчёта
# --------------------------------------------------------------------------- #
def build_report(ctx: dict[str, Any]) -> str:
    header = [
        "# Разбор торгового сигнала «под капотом»",
        "",
        "_Сгенерировано вручную скриптом `scripts/explain_signal.py` для "
        "технического разбора. Это разовый прогон, основной пайплайн не затронут._",
        "",
        "## 0. Момент и цена",
        "",
        f"- **Биржа:** {ctx['exchange']}  (спот `{ctx['symbol']}`, своп `{ctx['swap_symbol']}`)",
        f"- **Время запуска (МСК):** {ctx['now_msk']:%Y-%m-%d %H:%M:%S}  "
        f"(UTC: {ctx['now_utc']:%Y-%m-%d %H:%M:%S})",
        f"- **Текущая цена BTC:** {_fmt(ctx['price'])} USDT",
        f"- **Свечи для Market Agent:** {ctx['candles_span']}",
        f"- **Стакан:** {ctx['ob_span']}",
        f"- **Деривативы:** {ctx['fut_span']}",
        "",
    ]
    if ctx["exchange"].lower() != "okx":
        header.insert(
            4,
            f"> ⚠️ Внимание: данные взяты с биржи **{ctx['exchange']}**, а не OKX "
            f"(переопределите EXPLAIN_EXCHANGE=okx при необходимости).\n",
        )
    body = [
        render_market(ctx["market"]),
        render_liquidity(ctx["liquidity"]),
        render_futures(ctx["futures"], ctx["futures_meta"]),
        render_decision(
            ctx["decision_outputs"], ctx["decision"], ctx["probability"],
            ctx["weights"], ctx["threshold"], ctx["min_agents"],
        ),
        render_verdict(
            ctx["decision"], ctx["probability"], ctx["price"],
            ctx["decision_outputs"], ctx["signal_id"], ctx["now_msk"],
        ),
        render_trade_plan(ctx["levels"]),
    ]
    return "\n".join(header) + "\n" + "\n".join(body)


# --------------------------------------------------------------------------- #
# Основной поток
# --------------------------------------------------------------------------- #
async def run(no_db: bool) -> None:
    exchange_id = os.environ.get("EXPLAIN_EXCHANGE") or settings.EXCHANGE or "okx"
    symbol = settings.SYMBOL
    swap_symbol = settings.SWAP_SYMBOL
    timeframe = settings.AGENT_TIMEFRAME
    min_candles = settings.AGENT_MIN_CANDLES

    now_utc = datetime.now(UTC)
    now_msk = now_utc.astimezone(MSK)

    ex = create_exchange(exchange_id)
    try:
        await ex.load_markets()

        # --- 1. Реальные данные с биржи ---
        df, raw_candles = await gather_market_data(ex, symbol, timeframe, min_candles)
        try:
            ticker = await ex.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close"))
        except Exception:  # noqa: BLE001
            price = float(df["close"].iloc[-1]) if not df.empty else 0.0

        snapshots = await gather_orderbook_snapshots(
            ex, symbol, settings.ORDERBOOK_DEPTH
        )
        funding, oi, fut_meta = await gather_futures_data(ex, swap_symbol)

        # --- 2. Прогон через боевые чистые функции агентов ---
        m_sig, m_conf, m_metrics, m_rat = analyze_ohlcv(df, min_candles)
        m_metrics["timeframe"] = timeframe  # как в MarketAgent.analyze
        market_out = {"agent": "market", "signal": m_sig, "confidence": m_conf,
                      "metrics": m_metrics, "rationale": m_rat}

        l_sig, l_conf, l_metrics, l_rat = analyze_orderbook(snapshots)
        liquidity_out = {"agent": "liquidity", "signal": l_sig, "confidence": l_conf,
                         "metrics": l_metrics, "rationale": l_rat,
                         "_extra": _orderbook_extra(snapshots)}

        f_sig, f_conf, f_metrics, f_rat = analyze_futures(funding, oi, price)
        futures_out = {"agent": "futures", "signal": f_sig, "confidence": f_conf,
                       "metrics": f_metrics, "rationale": f_rat}

        # --- 3. Decision Agent (та же чистая функция make_decision) ---
        decision_outputs = [
            {"agent": "market", "instrument_id": 0, "ts": now_utc,
             "signal": m_sig, "confidence": m_conf, "metrics": m_metrics,
             "rationale": m_rat},
            {"agent": "liquidity", "instrument_id": 0, "ts": now_utc,
             "signal": l_sig, "confidence": l_conf, "metrics": l_metrics,
             "rationale": l_rat},
            {"agent": "futures", "instrument_id": 0, "ts": now_utc,
             "signal": f_sig, "confidence": f_conf, "metrics": f_metrics,
             "rationale": f_rat},
        ]
        weights = settings.agent_weights
        threshold = settings.DECISION_THRESHOLD
        min_agents = settings.MIN_AGENTS
        decision, probability, payload, dec_rationale = make_decision(
            decision_outputs,
            weights=weights,
            threshold=threshold,
            min_agents=min_agents,
            freshness_sec=10**9,  # все выводы только что получены — заведомо свежие
            now=now_utc,
        )

        # --- 4. Запись сигнала в БД (как делает Decision Agent) ---
        signal_id: Any = "DRYRUN"
        if not no_db:
            signal_id = await _persist_signal(
                exchange_id, symbol, swap_symbol, raw_candles,
                decision, probability, payload, dec_rationale,
            )

        # --- 4b. Торговые уровни (из S/R и ATR, добытых Market Agent) ---
        mm = market_out["metrics"] if m_sig != "insufficient_data" else {}
        levels = compute_trade_levels(
            decision,
            price,
            mm.get("support"),
            mm.get("resistance"),
            mm.get("atr14"),
        )

        # --- 5. Отчёт ---
        ctx = {
            "exchange": exchange_id,
            "symbol": symbol,
            "swap_symbol": swap_symbol,
            "now_utc": now_utc,
            "now_msk": now_msk,
            "price": price,
            "candles_span": _candles_span(df, timeframe),
            "ob_span": f"{len(snapshots)} снимков, глубина {settings.ORDERBOOK_DEPTH}",
            "fut_span": _fut_span(funding, oi, fut_meta),
            "market": market_out,
            "liquidity": liquidity_out,
            "futures": futures_out,
            "futures_meta": fut_meta,
            "decision_outputs": decision_outputs,
            "decision": decision,
            "probability": probability,
            "weights": weights,
            "threshold": threshold,
            "min_agents": min_agents,
            "signal_id": signal_id,
            "levels": levels,
        }
        report = build_report(ctx)
        print(report)

        # --- 6. Сохранение в файл ---
        os.makedirs("reports", exist_ok=True)
        fname = f"reports/signal_{signal_id}.md"
        with open(fname, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\n[OK] Отчёт сохранён: {fname}")
        if signal_id not in (None, "DRYRUN"):
            print(f"[OK] signal_id={signal_id} записан в таблицу signals "
                  f"(decision={decision}, probability={probability:.4f}).")
    finally:
        await ex.close()
        if not no_db:
            await db.close()


def _orderbook_extra(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Достаёт крупные заявки последнего снимка для отчёта."""
    latest = snapshots[-1]
    walls: list[dict[str, Any]] = []
    for side_name, levels in (("BID (покупка)", latest["bids"]),
                              ("ASK (продажа)", latest["asks"])):
        amounts = [float(lvl[1]) for lvl in levels if len(lvl) >= 2]
        if not amounts:
            continue
        avg = sum(amounts) / len(amounts)
        idx = max(range(len(levels)), key=lambda i: float(levels[i][1]))
        big = levels[idx]
        walls.append({
            "side": side_name,
            "price": float(big[0]),
            "amount": float(big[1]),
            "ratio": (float(big[1]) / avg) if avg > 0 else 0.0,
        })
    return {"depth": settings.ORDERBOOK_DEPTH, "walls": walls}


def _candles_span(df: pd.DataFrame, timeframe: str) -> str:
    if df.empty:
        return "нет данных"
    t0 = datetime.fromtimestamp(int(df["ts"].iloc[0]) / 1000, tz=UTC).astimezone(MSK)
    t1 = datetime.fromtimestamp(int(df["ts"].iloc[-1]) / 1000, tz=UTC).astimezone(MSK)
    return (f"{len(df)} свечей {timeframe}, период "
            f"{t0:%Y-%m-%d %H:%M}–{t1:%Y-%m-%d %H:%M} МСК")


def _fut_span(funding: list, oi: list, meta: dict) -> str:
    bits = [f"funding точек: {len(funding)}", f"OI точек: {len(oi)}"]
    if meta.get("oi_history_tf"):
        bits.append(f"шаг OI {meta['oi_history_tf']}")
    if meta.get("oi_error"):
        bits.append("история OI недоступна (фолбэк на снимок)")
    return ", ".join(bits)


async def _persist_signal(
    exchange_id: str,
    symbol: str,
    swap_symbol: str,
    raw_candles: list,
    decision: str,
    probability: float,
    payload: list,
    dec_rationale: str,
) -> int:
    """Пишет сигнал в БД под тем же spot-инструментом, что и боевой пайплайн.

    Колонки те же, что в db.save_signal; добавлен RETURNING id и пометка
    «[РУЧНОЙ РАЗБОР]» в rationale. Дополнительно засеваются 1m-свечи, чтобы у
    evaluator была цена на момент сигнала (будущее окно догрузят коллекторы).
    """
    await db.connect()
    spot_id = await db.get_or_create_instrument(exchange_id, symbol, "spot")
    # Засев 1m-свечей для evaluator (идемпотентный UPSERT, как у коллектора).
    try:
        m1 = await _fetch_1m(exchange_id, symbol)
        if m1:
            await db.upsert_ohlcv(spot_id, "1m", m1)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] не удалось засеять 1m-свечи: {exc}")

    rationale = "[РУЧНОЙ РАЗБОР] " + dec_rationale
    query = """
        INSERT INTO signals
            (instrument_id, decision, probability, agents_payload, rationale)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        RETURNING id;
    """
    signal_id = await db.pool.fetchval(
        query,
        spot_id,
        decision,
        float(probability),
        json.dumps(payload),
        rationale,
    )
    return int(signal_id)


async def _fetch_1m(exchange_id: str, symbol: str) -> list:
    """Отдельный короткий клиент только для засева 1m-свечей."""
    ex = create_exchange(exchange_id)
    try:
        await ex.load_markets()
        return await ex.fetch_ohlcv(symbol, "1m", limit=_SEED_1M_LIMIT)
    finally:
        await ex.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Разбор торгового сигнала по реальным данным.")
    parser.add_argument(
        "--no-db", action="store_true",
        help="не записывать сигнал в БД (только отчёт в консоль и файл).",
    )
    args = parser.parse_args()
    asyncio.run(run(no_db=args.no_db))


if __name__ == "__main__":
    main()
