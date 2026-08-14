"""NotifyAgent: отбирает сильные сигналы и шлёт уведомления в Telegram.

Принцип «только сильный сигнал»: уведомление уходит, только если одновременно
выполнены условия (decision ≠ wait; probability ≥ порога; сигнал не отправлен ранее;
решение сменилось ИЛИ прошёл cooldown). Сервис не падает ни при каких ошибках.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.redis_client import get_redis
from src.notify.telegram import send_message

# TTL heartbeat-ключа (секунды).
_HEARTBEAT_TTL = 300

# Порядок агентов, участвующих в решении (совпадает с src.decision.agent.AGENTS).
AGENT_ORDER: tuple[str, ...] = ("market", "liquidity", "futures")

# Русские имена агентов для текста сообщения (ТЗ §6).
AGENT_RU = {"market": "Теханализ", "liquidity": "Ликвидность", "futures": "Деривативы"}

# Русская расшифровка мнения агента (ТЗ §6).
OPINION_RU = {"bullish": "за рост", "bearish": "за падение", "neutral": "нейтрально"}

# Числовое направление сигнала — та же таблица, что в src.decision.agent.
SIGNAL_VALUE = {"bullish": 1, "bearish": -1, "neutral": 0}


@dataclass
class NotifyConfig:
    """Параметры решения об отправке."""

    min_probability: float
    cooldown_sec: float
    # Минимум агентов со свежим содержательным выводом для отправки (Задача A2).
    min_agents: int = 3


@dataclass
class SignalFormatConfig:
    """Параметры форматирования сообщения о сигнале (без обращений к сети/БД)."""

    symbol: str
    tz_name: str
    primary_horizon: str


def should_notify(
    signal: dict[str, Any],
    last_decision: str | None,
    last_sent_ts: datetime | None,
    now: datetime,
    cfg: NotifyConfig,
) -> bool:
    """Нужно ли отправлять уведомление по сигналу. Чистая, детерминированная.

    Условие «сигнал ещё не отправлен» обеспечивается выборкой из БД
    (``notified = FALSE``); здесь проверяются остальные условия.
    """
    # 1. Только направленные решения.
    if signal["decision"] == "wait":
        return False
    # 2. Достаточная вероятность.
    if float(signal["probability"]) < cfg.min_probability:
        return False
    # 3. Раньше ничего не отправляли по этому инструменту → можно слать.
    if last_decision is None or last_sent_ts is None:
        return True
    # 4. Решение сменилось → слать; то же решение → только после cooldown.
    if signal["decision"] != last_decision:
        return True
    return (now - last_sent_ts).total_seconds() >= cfg.cooldown_sec


# Короткие метки для часто используемых зон (иначе берём abbr из самой зоны).
_TZ_LABELS = {"Europe/Moscow": "МСК"}

# Заключительная строка сообщения — обязательна и неизменна (ТЗ §6).
_CLOSING_LINE = "Решение за вами. Система не торгует сама."


def normalize_payload(agents_payload: Any) -> list[dict[str, Any]]:
    """Приводит ``agents_payload`` (JSON-строка / список / None) к списку словарей.

    asyncpg возвращает JSONB строкой, поэтому строку разбираем через json.loads.
    """
    if agents_payload is None:
        return []
    if isinstance(agents_payload, str):
        try:
            parsed = json.loads(agents_payload)
        except (ValueError, TypeError):
            return []
    else:
        parsed = agents_payload
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, dict)]


def count_meaningful_agents(agents_payload: Any) -> int:
    """Число агентов со свежим СОДЕРЖАТЕЛЬНЫМ выводом в payload (Задача A2).

    payload формирует Decision Agent уже из свежих не-``insufficient_data``
    выводов, но проверяем ещё раз по сигналу: содержательным считаем
    bullish/bearish/neutral, но НЕ insufficient_data/неизвестное.
    """
    return sum(
        1
        for e in normalize_payload(agents_payload)
        if e.get("signal") in SIGNAL_VALUE
    )


def compute_agreement(agents_payload: Any) -> float | None:
    """Пересчитывает согласованность из ``agents_payload`` ТОЙ ЖЕ формулой, что и
    Decision Agent (src.decision.agent.make_decision, Задача B1):

        directions = [SIGNAL_VALUE[o["signal"]] for o in fresh]
        agreement = abs(pos - neg) / TOTAL_AGENTS

    Знаменатель — ПОЛНОЕ число агентов (len(AGENT_ORDER)), а не число свежих:
    так согласованность в уведомлении совпадает с той, что использовал Decision
    Agent (выпадение агента её понижает). Парсинг rationale регуляркой ЗАПРЕЩЁН
    (ТЗ §6): формат текстовый и может измениться. Здесь только чтение payload,
    логика решений не затрагивается. Возвращает None, если направленных мнений нет.
    """
    directions = [
        SIGNAL_VALUE[e["signal"]]
        for e in normalize_payload(agents_payload)
        if e.get("signal") in SIGNAL_VALUE
    ]
    if not directions:
        return None
    pos = sum(1 for d in directions if d > 0)
    neg = sum(1 for d in directions if d < 0)
    return abs(pos - neg) / len(AGENT_ORDER)


def agreement_wording(agreement: float) -> str:
    """Словесная расшифровка согласованности (ТЗ §6)."""
    if agreement >= 0.9:
        return "агенты единодушны"
    if agreement >= 0.5:
        return "агенты скорее согласны"
    return "мнения расходятся"


def horizon_ru(horizon: str) -> str:
    """Человекочитаемый горизонт: ``4h`` → «4 часа», ``1h`` → «1 час»."""
    horizon = horizon.strip().lower()
    if horizon.endswith("h") and horizon[:-1].isdigit():
        n = int(horizon[:-1])
        # Русские окончания: 1 час, 2-4 часа, 5+ часов (без спец. случаев 11-14 —
        # горизонты малы, но обрабатываем корректно).
        if n % 10 == 1 and n % 100 != 11:
            word = "час"
        elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            word = "часа"
        else:
            word = "часов"
        return f"{n} {word}"
    return horizon


def format_price(price: float, quote: str) -> str:
    """Цена с разделением тысяч пробелом: ``64210.0`` → ``64 210 USDT``."""
    grouped = f"{round(float(price)):,}".replace(",", " ")
    return f"{grouped} {quote}"


def _split_symbol(symbol: str) -> tuple[str, str]:
    """base/quote из символа: ``BTC/USDT`` → (``BTC``, ``USDT``)."""
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return base, quote.split(":", 1)[0]
    return symbol, "USDT"


def _agent_line(entry: dict[str, Any]) -> str:
    """Строка мнения присутствующего агента."""
    name = AGENT_RU.get(entry.get("agent"), entry.get("agent", "?"))
    opinion = OPINION_RU.get(entry.get("signal"), entry.get("signal", "?"))
    confidence = float(entry.get("confidence", 0.0))
    return f"• {name}: {opinion} (уверенность {confidence:.2f})"


def format_signal_message(
    signal: dict[str, Any],
    price: float | None,
    cfg: SignalFormatConfig,
) -> str:
    """Самодостаточное HTML-сообщение о сигнале (ТЗ §6). Чистая функция.

    Показывает мнение каждого агента, ЯВНО отмечает отсутствующих (сигнал на
    двух агентах из трёх слабее — человек должен это видеть), пересчитанную
    согласованность, цену на момент сигнала, горизонт оценки. Если цены нет —
    строка про цену пропускается, сообщение всё равно формируется.
    """
    base, quote = _split_symbol(cfg.symbol)
    decision = signal["decision"]
    if decision == "buy":
        emoji, action = "🟢", "ПОКУПАТЬ"
    else:
        emoji, action = "🔴", "ПРОДАВАТЬ"
    probability = round(float(signal["probability"]) * 100)

    payload = normalize_payload(signal.get("agents_payload"))
    present = {e.get("agent") for e in payload}

    lines = [
        f"{emoji} <b>СИГНАЛ: {action} {base}</b>",
        f"Вероятность: <b>{probability}%</b>",
    ]
    if price is not None:
        lines.append(f"Цена сейчас: {format_price(price, quote)}")

    lines.append("")
    lines.append("Мнения агентов:")
    for name in AGENT_ORDER:
        entry = next((e for e in payload if e.get("agent") == name), None)
        if entry is not None:
            lines.append(_agent_line(entry))
    # Отсутствующие агенты — отдельными явными строками (важнее всего прочего).
    for name in AGENT_ORDER:
        if name not in present:
            lines.append(
                f"• {AGENT_RU[name]}: нет данных, в решении не участвовал"
            )

    agreement = compute_agreement(payload)
    lines.append("")
    if agreement is not None:
        lines.append(
            f"Согласованность: {agreement:.2f} — {agreement_wording(agreement)}"
        )
    lines.append(f"Горизонт оценки: {horizon_ru(cfg.primary_horizon)}")

    ts: datetime = signal["ts"]
    local_ts = ts.astimezone(ZoneInfo(cfg.tz_name))
    label = _TZ_LABELS.get(cfg.tz_name) or local_ts.tzname() or cfg.tz_name
    when = local_ts.strftime("%d.%m.%Y %H:%M")
    lines.append(f"Сигнал #{signal['id']} · {when} {label}")

    lines.append("")
    lines.append(_CLOSING_LINE)
    return "\n".join(lines)


class NotifyAgent:
    """Сервис уведомлений: читает сильные сигналы и шлёт их в Telegram."""

    def __init__(
        self,
        interval: float,
        min_probability: float,
        cooldown_sec: float,
        symbol: str,
        tz_name: str,
        primary_horizon: str,
        min_agents: int = 3,
    ) -> None:
        self.interval = interval
        self.cfg = NotifyConfig(min_probability, cooldown_sec, min_agents)
        self.symbol = symbol
        self.tz_name = tz_name
        self.fmt_cfg = SignalFormatConfig(symbol, tz_name, primary_horizon)
        self._log = structlog.get_logger().bind(component="notify")

    async def process_once(self) -> None:
        """Обрабатывает все неотправленные сильные сигналы."""
        if not settings.telegram_configured:
            # Сервис не падает — просто простаивает с предупреждением.
            self._log.warning("Telegram не настроен — ожидание (сигналы не шлются)")
            return

        signals = await db.get_unnotified_strong_signals(self.cfg.min_probability)
        for signal in signals:
            await self._process_signal(signal)

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """Решает, слать ли конкретный сигнал, и при необходимости шлёт."""
        instrument_id = signal["instrument_id"]

        # Деградированный режим (Задача A2): агентов со свежим содержательным
        # выводом меньше порога → уведомление НЕ шлём. Сигнал уже сохранён и
        # помечен degraded (статистика не теряется) — «поглощаем» его, чтобы он не
        # висел в очереди, но notified_at НЕ ставим: отправки не было.
        n_agents = count_meaningful_agents(signal.get("agents_payload"))
        if n_agents < self.cfg.min_agents:
            await db.mark_signal_absorbed(signal["id"])
            self._log.info(
                "Уведомление подавлено: деградированный режим",
                signal_id=signal["id"],
                agents=n_agents,
                min_agents=self.cfg.min_agents,
            )
            return

        last_decision, last_sent_ts = await self._get_last_state(instrument_id)
        now = datetime.now(UTC)

        if not should_notify(signal, last_decision, last_sent_ts, now, self.cfg):
            # Дубль/в пределах cooldown — «поглощаем» сигнал, чтобы не висел.
            # notified_at НЕ ставим: отправки в Telegram здесь не было.
            await db.mark_signal_absorbed(signal["id"])
            return

        # Цена на момент сигнала — переиспользуем готовый get_price_at (ТЗ §6).
        # Чтение цены не влияет на условия отправки; при отсутствии — None.
        price = await db.get_price_at(instrument_id, signal["ts"])
        sent = await send_message(format_signal_message(signal, price, self.fmt_cfg))
        if sent:
            await db.mark_signal_notified(signal["id"])
            await self._set_last_state(instrument_id, signal["decision"], now)
            self._log.info(
                "Уведомление отправлено",
                signal_id=signal["id"],
                decision=signal["decision"],
                probability=signal["probability"],
            )
        else:
            # Не отправилось (сеть/Telegram) — не помечаем, повторим позже.
            self._log.warning("Отправка не удалась, повтор позже", signal_id=signal["id"])

    async def _get_last_state(
        self, instrument_id: int
    ) -> tuple[str | None, datetime | None]:
        """Читает из Redis последнее отправленное решение по инструменту."""
        raw = await get_redis().get(f"notify:last:{instrument_id}")
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
            return data["decision"], datetime.fromisoformat(data["sent_ts"])
        except Exception:
            return None, None

    async def _set_last_state(
        self, instrument_id: int, decision: str, sent_ts: datetime
    ) -> None:
        """Сохраняет в Redis последнее отправленное решение и время."""
        data = json.dumps({"decision": decision, "sent_ts": sent_ts.isoformat()})
        await get_redis().set(f"notify:last:{instrument_id}", data)

    async def run(self) -> None:
        """Бесконечный цикл: process_once → heartbeat → пауза. Не падает на ошибках."""
        self._log.info("Сервис уведомлений запущен", interval=self.interval)
        while True:
            try:
                await self.process_once()
                await self._heartbeat()
            except asyncio.CancelledError:
                self._log.info("Сервис уведомлений остановлен")
                raise
            except Exception as exc:
                self._log.warning("Ошибка итерации уведомлений", error=str(exc))
            await asyncio.sleep(self.interval)

    async def _heartbeat(self) -> None:
        """Пишет в Redis отметку времени последней успешной итерации."""
        now_iso = datetime.now(UTC).isoformat()
        await get_redis().set("notify:heartbeat", now_iso, ex=_HEARTBEAT_TTL)
