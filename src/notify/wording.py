"""Человеческие формулировки мнений агентов для текста сигнала (§3 ТЗ 8.3).

Правило модуля одно и оно жёсткое: КАЖДАЯ фраза выводится из метрики, которую
агент ДЕЙСТВИТЕЛЬНО вернул. Ни одного наблюдения «вообще про рынок» здесь быть
не может (§7 ТЗ). Нет метрики — нет фразы; нет ни одной — так и сказано.

Причина не в аккуратности, а в назначении текста: человек читает объяснение,
чтобы решить, ставить ли деньги. Правдоподобная фраза, за которой нет
измерения, — это выдуманное основание для чужого решения.

Внутренние термины («перцентиль», «уверенность» числом, «EMA», «RSI», «ADX»,
«дисбаланс», «фандинг») в текст НЕ попадают: человек, который их знает, найдёт
числа в /signal, а тому, кто не знает, они мешают.
"""

from __future__ import annotations

from typing import Any

# Границы словесной оценки уверенности агента (§3 ТЗ: «высокая», «средняя»,
# «низкая»). Значения совпадают с теми, по которым уверенность уже трактуется
# в проекте: 0.7 — порог отправки уведомления по умолчанию
# (NOTIFY_MIN_PROBABILITY), 0.4 — граница, ниже которой мнение агента в
# диагностике 7.1 считалось слабым.
CONFIDENCE_HIGH = 0.70
CONFIDENCE_MEDIUM = 0.40

AGENT_TITLE = {
    "market": "Теханализ",
    "liquidity": "Ликвидность",
    "futures": "Деривативы",
}

VOICE = {
    "bullish": "Голос за покупку",
    "bearish": "Голос за продажу",
}


def confidence_word(confidence: float) -> str:
    """Уверенность агента словом. Число человеку не показывается."""
    value = float(confidence)
    if value >= CONFIDENCE_HIGH:
        return "высокая"
    if value >= CONFIDENCE_MEDIUM:
        return "средняя"
    return "низкая"


def is_confident(confidence: float) -> bool:
    """Считается ли голос уверенным (для строки «N из M уверенно»)."""
    return float(confidence) >= CONFIDENCE_MEDIUM


def _hours_phrase(hours: Any) -> str:
    """«За две недели», «за сутки» — срок наблюдения словами."""
    try:
        value = int(hours)
    except (TypeError, ValueError):
        return "за наблюдаемый срок"
    if value >= 24 * 13:
        return "за две недели"
    if value >= 24 * 6:
        return "за неделю"
    if value >= 24 * 2:
        return f"за {value // 24} суток"
    if value >= 24:
        return "за сутки"
    return f"за {value} ч"


def explain_market(metrics: dict[str, Any] | None) -> str:
    """Мнение агента теханализа человеческим языком.

    Источник — только метрики :func:`src.agents.market.analyze_ohlcv`:
    взаимное расположение средних, наклон средней, перегретость, наличие
    выраженного движения. Ничего про объём торгов здесь нет и быть не может:
    агент объём не измеряет и в метриках не возвращает.
    """
    if not metrics:
        return "показатели за этот момент не сохранились"
    parts: list[str] = []

    ema20, ema50, ema200 = metrics.get("ema20"), metrics.get("ema50"), metrics.get("ema200")
    if None not in (ema20, ema50, ema200):
        if ema20 > ema50 > ema200:
            parts.append("средние цены за короткий, средний и длинный срок выстроились по росту")
        elif ema20 < ema50 < ema200:
            parts.append("средние цены за короткий, средний и длинный срок выстроились по падению")
        else:
            parts.append("средние цены за разные сроки смотрят в разные стороны")

    slope = metrics.get("ema50_slope")
    if slope is not None:
        if float(slope) > 0:
            parts.append("средняя цена растёт")
        elif float(slope) < 0:
            parts.append("средняя цена снижается")
        else:
            parts.append("средняя цена стоит на месте")

    tail: list[str] = []
    rsi = metrics.get("rsi14")
    if rsi is not None:
        if float(rsi) >= 70:
            tail.append("рост в последнее время был слишком быстрым — возможен откат")
        elif float(rsi) <= 30:
            tail.append("падение в последнее время было слишком быстрым — возможен отскок")

    adx = metrics.get("adx14")
    if adx is not None:
        if float(adx) >= 25:
            tail.append("движение направленное, а не топтание на месте")
        elif float(adx) < 20:
            tail.append("выраженного движения нет, цена топчется на месте")

    if not parts and not tail:
        return "показатели за этот момент не сохранились"
    first = ", ".join(parts) if parts else ""
    second = "; ".join(tail)
    if first and second:
        return f"{first}. {second[0].upper()}{second[1:]}"
    return first or f"{second[0].upper()}{second[1:]}"


def explain_liquidity(metrics: dict[str, Any] | None) -> str:
    """Мнение агента ликвидности человеческим языком.

    Источник — метрики :func:`src.agents.liquidity.analyze_orderbook`: перевес
    заявок, разница цен покупки и продажи, крупные заявки в стакане.
    """
    if not metrics:
        return "показатели за этот момент не сохранились"
    parts: list[str] = []

    imbalance = metrics.get("imbalance")
    if imbalance is not None:
        value = float(imbalance)
        if value >= 0.30:
            parts.append("заявок на покупку заметно больше, чем на продажу")
        elif value >= 0.10:
            parts.append("заявок на покупку немного больше, чем на продажу")
        elif value <= -0.30:
            parts.append("заявок на продажу заметно больше, чем на покупку")
        elif value <= -0.10:
            parts.append("заявок на продажу немного больше, чем на покупку")
        else:
            parts.append("заявок на покупку и продажу примерно поровну")

    tail: list[str] = []
    rel_spread = metrics.get("rel_spread")
    if rel_spread is not None and float(rel_spread) >= 0.001:
        tail.append("разница между ценой покупки и продажи широкая — торговля идёт вяло")

    bid_wall, ask_wall = metrics.get("bid_wall_ratio"), metrics.get("ask_wall_ratio")
    if bid_wall is not None and float(bid_wall) >= 0.30:
        tail.append("на покупку выставлена крупная заявка, она держит цену снизу")
    elif ask_wall is not None and float(ask_wall) >= 0.30:
        tail.append("на продажу выставлена крупная заявка, она давит на цену сверху")

    if not parts and not tail:
        return "показатели за этот момент не сохранились"
    first = ", ".join(parts) if parts else ""
    second = "; ".join(tail)
    if first and second:
        return f"{first}. {second[0].upper()}{second[1:]}"
    return first or f"{second[0].upper()}{second[1:]}"


def explain_futures(metrics: dict[str, Any] | None) -> str:
    """Мнение агента деривативов человеческим языком.

    Источник — метрики :func:`src.agents.futures.analyze_futures`: положение
    платы за удержание позиции относительно её обычного размаха и подтверждение
    со стороны числа открытых позиций.
    """
    if not metrics:
        return "показатели за этот момент не сохранились"
    parts: list[str] = []

    funding_pct = metrics.get("funding_pct")
    span = _hours_phrase(metrics.get("lookback_hours"))
    if funding_pct is not None:
        value = float(funding_pct)
        if value >= 0.80:
            parts.append(
                f"плата за удержание позиции у верхней границы обычного размаха {span} — "
                "покупатели платят продавцам необычно много"
            )
        elif value <= 0.20:
            parts.append(
                f"плата за удержание позиции у нижней границы обычного размаха {span} — "
                "продавцы платят покупателям необычно много"
            )
        else:
            parts.append(f"плата за удержание позиции в середине обычного размаха {span}")

    tail: list[str] = []
    if metrics.get("oi_enough"):
        if metrics.get("oi_confirms"):
            tail.append("число открытых позиций подтверждает движение")
        else:
            tail.append("число открытых позиций движение не подтверждает")
    elif metrics.get("n_oi") is not None:
        tail.append("данных о числе открытых позиций пока мало")

    if not parts and not tail:
        return "показатели за этот момент не сохранились"
    first = ", ".join(parts) if parts else ""
    second = "; ".join(tail)
    if first and second:
        return f"{first}. {second[0].upper()}{second[1:]}"
    return first or f"{second[0].upper()}{second[1:]}"


EXPLAIN = {
    "market": explain_market,
    "liquidity": explain_liquidity,
    "futures": explain_futures,
}


def agent_paragraph(
    agent: str, signal: str, confidence: float, metrics: dict[str, Any] | None
) -> str:
    """Строка про одного высказавшегося агента: объяснение + голос + уверенность."""
    title = AGENT_TITLE.get(agent, agent)
    explanation = EXPLAIN.get(agent, lambda _m: "показатели за этот момент не сохранились")(
        metrics
    )
    voice = VOICE.get(signal)
    if voice is None:
        # Нейтральное мнение — это тоже мнение, и о нём говорится прямо:
        # «голоса нет» здесь означает «агент не выбрал сторону», а не «молчит».
        return f"· {title}: {explanation}. Ясной стороны не выбрал."
    return f"· {title}: {explanation}. {voice}, уверенность {confidence_word(confidence)}."


def agent_silent_paragraph(agent: str) -> str:
    """Строка про агента, который не высказался (§3 ТЗ: не пропускать молча)."""
    return f"· {AGENT_TITLE.get(agent, agent)}: недостаточно данных, голоса нет."
