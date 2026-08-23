"""NotifyAgent: отбирает сильные сигналы и шлёт уведомления в Telegram.

Принцип «только сильный сигнал»: уведомление уходит, только если одновременно
выполнены условия (decision ≠ wait; probability ≥ порога; сигнал не отправлен ранее;
решение сменилось ИЛИ прошёл cooldown). Сервис не падает ни при каких ошибках.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
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
# Скользящее окно отправленных уведомлений (§2 ТЗ 8.3): один ключ на все
# инструменты — потолок общий, потому что человек читает один поток.
_SENT_KEY = "notify:sent:hour"
# Очередь сигналов, придержанных ПОТОЛКОМ, и момент последней сводки
# (§2 ТЗ 8.3). Придержанные ВЫДЕРЖКОЙ сюда не попадают: выдержка — это
# нормальный ход по одному токену, а не переполнение.
_DIGEST_KEY = "notify:digest:pending"
_DIGEST_LAST_KEY = "notify:digest:last"
# Сводка выходит не чаще одного раза в час: «одно сводное сообщение» на
# исчерпанный потолок, иначе сводки сами стали бы потоком.
_DIGEST_EVERY_SEC = 3600
# Потолок строк в сводке: в сообщение Telegram помещается около 4096
# знаков, а сводка из полусотни строк перестала бы читаться.
_DIGEST_MAX_LISTED = 20
# Начало причины «сработал потолок». Вынесено константой, чтобы отличать
# потолок от выдержки сравнением, а не разбором текста сообщения.
_CAP_REASON_PREFIX = "потолок уведомлений в час"

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
    # Этап 7.3, Блок B. false (по умолчанию) — отбор по ИНДЕКСУ СОГЛАСИЯ и
    # прежнему порогу min_probability, поведение уведомлений не меняется.
    # true — отбор по КАЛИБРОВАННОЙ вероятности и порогу min_calibrated; пока
    # активной кривой нет, calibrated_probability = NULL, и не уходит ничего.
    use_calibrated: bool = False
    min_calibrated: float = 0.55
    # Защита от потока уведомлений (§2 ТЗ 8.3). 0 — ограничение выключено.
    hold_sec: float = 0.0        # выдержка по одному токену, независимо от решения
    max_per_hour: int = 0        # потолок уведомлений в час по всем токенам


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
    # 2. Порог силы сигнала. По умолчанию — индекс согласия, как и раньше.
    #    В калиброванном режиме — вероятность из кривой; её отсутствие (кривая
    #    ещё не построена) означает «не отправлять», а не «отправить всё».
    if cfg.use_calibrated:
        calibrated = signal.get("calibrated_probability")
        if calibrated is None or float(calibrated) < cfg.min_calibrated:
            return False
    elif float(signal["probability"]) < cfg.min_probability:
        return False
    # 3. Раньше ничего не отправляли по этому инструменту → можно слать.
    if last_decision is None or last_sent_ts is None:
        return True
    # 4. Решение сменилось → слать; то же решение → только после cooldown.
    if signal["decision"] != last_decision:
        return True
    return (now - last_sent_ts).total_seconds() >= cfg.cooldown_sec


def rate_limit_reason(
    last_sent_ts: datetime | None,
    now: datetime,
    sent_last_hour: int,
    cfg: NotifyConfig,
) -> str | None:
    """Почему уведомление придержано потоковой защитой. ``None`` — можно слать.

    Чистая, детерминированная: всё состояние приходит параметрами.

    Проверки НАМЕРЕННО разделены с :func:`should_notify`. Та отвечает на вопрос
    «стоит ли вообще говорить об этом сигнале»; эта — «не слишком ли часто мы
    говорим». Смешивать их нельзя: причина, по которой уведомление не ушло,
    попадает в лог, и «сигнал слабый» и «уведомлений и так много» — разные
    события, требующие разных действий.

    ВЫДЕРЖКА не смотрит на решение — этим она и отличается от
    ``cooldown_sec``. Cooldown придерживает только повтор ТОГО ЖЕ решения, а
    смена решения проходит мимо него; именно смена и даёт поток, когда пара
    колеблется. Выдержка держит паузу по токену в любом случае.

    ПОТОЛОК считается по скользящему часу и общий на все токены: он ограничивает
    то, что видит человек, а человек читает один поток, а не пять.

    Ноль в любом из порогов выключает соответствующее ограничение — поведение
    возвращается к тому, что было до Этапа 8.3.
    """
    if cfg.hold_sec > 0 and last_sent_ts is not None:
        elapsed = (now - last_sent_ts).total_seconds()
        if elapsed < cfg.hold_sec:
            left = int(cfg.hold_sec - elapsed)
            return f"выдержка по инструменту: осталось {left} с из {int(cfg.hold_sec)}"
    if cfg.max_per_hour > 0 and sent_last_hour >= cfg.max_per_hour:
        return (
            f"{_CAP_REASON_PREFIX}: отправлено {sent_last_hour} "
            f"из {cfg.max_per_hour}"
        )
    return None


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


def format_calibrated_line(signal: dict[str, Any]) -> str | None:
    """Строка «Вероятность успеха (по истории)» — или None, если кривой нет.

    Правило Этапа 7.3 §4.1: система никогда не показывает число, называя его
    вероятностью, если это число не выведено из фактических исходов. Пока
    ``calibrated_probability`` пуста, строки просто нет — вместо неё не
    подставляется индекс согласия и не пишется «нет данных».

    Дата кривой и размер её выборки печатаются, когда они известны: человек
    должен видеть, на чём основано число (кривая от 16.08 по 87 наблюдениям —
    это совсем не то же самое, что кривая по 8 наблюдениям).
    """
    value = signal.get("calibrated_probability")
    if value is None:
        return None
    percent = round(float(value) * 100)
    built_at = signal.get("calibration_built_at")
    sample = signal.get("calibration_sample_size")
    marks: list[str] = []
    if isinstance(built_at, datetime):
        marks.append(f"кривая от {built_at.strftime('%d.%m')}")
    if sample is not None:
        marks.append(f"N={int(sample)}")
    suffix = f"  [{', '.join(marks)}]" if marks else ""
    return f"Вероятность успеха (по истории): <b>{percent}%</b>{suffix}"


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
    conviction = round(float(signal["probability"]) * 100)

    payload = normalize_payload(signal.get("agents_payload"))
    present = {e.get("agent") for e in payload}

    # Этап 7.3 §4.6: величина, которую считает Decision Agent, называется
    # ИНДЕКСОМ СОГЛАСИЯ. Слово «вероятность» появляется ТОЛЬКО отдельной строкой
    # и ТОЛЬКО когда она выведена из фактических исходов (есть кривая).
    lines = [
        f"{emoji} <b>СИГНАЛ: {action} {base}</b>",
        f"Индекс согласия: <b>{conviction}%</b>",
    ]
    calibrated_line = format_calibrated_line(signal)
    if calibrated_line:
        lines.append(calibrated_line)
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


def format_digest_message(
    entries: list[dict[str, Any]], max_per_hour: int, max_listed: int = _DIGEST_MAX_LISTED
) -> str:
    """Сводное сообщение о сигналах, придержанных потолком (§2 ТЗ 8.3).

    Чистая функция: список уже отсортированных быть не обязан — сортировка по
    убыванию силы делается здесь, чтобы порядок не зависел от того, в каком
    порядке сигналы попали в очередь.

    СИЛА — та же величина, по которой сигнал прошёл порог отправки: индекс
    согласия либо калиброванная вероятность в калиброванном режиме. Брать здесь
    другую величину нельзя: человек сравнивал бы строки сводки по одной шкале,
    а отбирались они по другой.

    Список ограничен ``max_listed`` строками: в сообщение Telegram помещается
    около 4096 знаков, и сводка из полусотни строк перестала бы читаться —
    ровно то, от чего защищает потолок. Остаток назван числом, а не отброшен
    молча.
    """
    ordered = sorted(entries, key=lambda e: float(e.get("strength") or 0.0), reverse=True)
    lines = [
        f"📋 <b>Придержано сигналов: {len(ordered)}</b>",
        f"Потолок {max_per_hour} уведомлений в час исчерпан. "
        f"Ниже — придержанные сигналы по убыванию силы.",
        "",
    ]
    for entry in ordered[:max_listed]:
        emoji = "🟢" if entry.get("decision") == "buy" else "🔴"
        action = "ПОКУПАТЬ" if entry.get("decision") == "buy" else "ПРОДАВАТЬ"
        strength = round(float(entry.get("strength") or 0.0) * 100)
        lines.append(f"{emoji} {entry.get('symbol', '?')} — {action}, {strength}%")
    hidden = len(ordered) - max_listed
    if hidden > 0:
        lines.append(f"…и ещё {hidden} — не поместились в сообщение")
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
        use_calibrated: bool = False,
        min_calibrated: float = 0.55,
        hold_sec: float = 0.0,
        max_per_hour: int = 0,
    ) -> None:
        self.interval = interval
        self.cfg = NotifyConfig(
            min_probability,
            cooldown_sec,
            min_agents,
            use_calibrated=use_calibrated,
            min_calibrated=min_calibrated,
            hold_sec=hold_sec,
            max_per_hour=max_per_hour,
        )
        self.symbol = symbol
        self.tz_name = tz_name
        self.fmt_cfg = SignalFormatConfig(symbol, tz_name, primary_horizon)
        # Кэш «идентификатор инструмента → символ» (Этап 8.1). Токенов пять, и
        # сообщение обязано называть ТОТ инструмент, по которому выдан сигнал:
        # с одной подписью из настройки SYMBOL все пять уведомлений выглядели бы
        # сигналами по биткоину. Символы инструментов не меняются, поэтому кэш
        # без срока жизни: один запрос к БД на инструмент за всё время работы.
        self._symbols: dict[int, str] = {}
        self._log = structlog.get_logger().bind(component="notify")

    async def _format_config(self, instrument_id: int) -> SignalFormatConfig:
        """Параметры форматирования для конкретного инструмента."""
        symbol = self._symbols.get(instrument_id)
        if symbol is None:
            symbol = await db.get_instrument_symbol(instrument_id)
            if symbol is None:
                # Инструмента нет в справочнике — берём подпись из настройки и
                # говорим об этом в логе, но уведомление не теряем.
                self._log.warning(
                    "Символ инструмента не найден, подпись из настройки",
                    instrument_id=instrument_id, symbol=self.fmt_cfg.symbol,
                )
                return self.fmt_cfg
            self._symbols[instrument_id] = symbol
        return replace(self.fmt_cfg, symbol=symbol)

    async def process_once(self) -> None:
        """Обрабатывает все неотправленные сильные сигналы."""
        if not settings.telegram_configured:
            # Сервис не падает — просто простаивает с предупреждением.
            self._log.warning("Telegram не настроен — ожидание (сигналы не шлются)")
            return

        signals = await db.get_unnotified_strong_signals(
            self.cfg.min_probability,
            use_calibrated=self.cfg.use_calibrated,
            min_calibrated=self.cfg.min_calibrated,
        )
        for signal in signals:
            await self._process_signal(signal)
        # Сводка — после разбора всех сигналов итерации: иначе в неё попадала бы
        # часть очереди, а «одно сводное сообщение» превратилось бы в несколько.
        await self._flush_digest(datetime.now(UTC))

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

        # Защита от потока (§2 ТЗ 8.3). Придержанный сигнал «поглощается», а не
        # откладывается: уведомление о рыночном сигнале имеет смысл в свой
        # момент, а через час это уже не уведомление, а история. Сигнал при этом
        # сохранён и попадает в оценку — теряется только сообщение.
        limited = rate_limit_reason(
            last_sent_ts, now, await self._sent_last_hour(now), self.cfg
        )
        if limited is not None:
            await db.mark_signal_absorbed(signal["id"])
            # Придержанное ПОТОЛКОМ не пропадает молча: §2 ТЗ 8.3 требует одного
            # сводного сообщения. Придержанное ВЫДЕРЖКОЙ в сводку не идёт —
            # это нормальный ход по одному токену, а не переполнение, и попав
            # туда, оно превратило бы сводку в тот же поток.
            if limited.startswith(_CAP_REASON_PREFIX):
                await self._queue_for_digest(signal, instrument_id)
            self._log.info(
                "Уведомление придержано защитой от потока",
                signal_id=signal["id"],
                instrument_id=instrument_id,
                reason=limited,
            )
            return

        # Цена на момент сигнала — переиспользуем готовый get_price_at (ТЗ §6).
        # Чтение цены не влияет на условия отправки; при отсутствии — None.
        price = await db.get_price_at(instrument_id, signal["ts"])
        fmt_cfg = await self._format_config(instrument_id)
        sent = await send_message(format_signal_message(signal, price, fmt_cfg))
        if sent:
            await db.mark_signal_notified(signal["id"])
            await self._set_last_state(instrument_id, signal["decision"], now)
            await self._record_sent(now)
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

    def _strength(self, signal: dict[str, Any]) -> float:
        """Сила сигнала — та же величина, по которой он отбирался к отправке."""
        if self.cfg.use_calibrated:
            calibrated = signal.get("calibrated_probability")
            if calibrated is not None:
                return float(calibrated)
        return float(signal.get("probability") or 0.0)

    async def _queue_for_digest(
        self, signal: dict[str, Any], instrument_id: int
    ) -> None:
        """Кладёт придержанный потолком сигнал в очередь сводного сообщения."""
        try:
            fmt_cfg = await self._format_config(instrument_id)
            entry = json.dumps({
                "symbol": fmt_cfg.symbol,
                "decision": signal["decision"],
                "strength": self._strength(signal),
            })
            redis = get_redis()
            await redis.rpush(_DIGEST_KEY, entry)
            # Срок жизни заведомо больше периода сводки: очередь без сводки не
            # должна копиться вечно, если сервис остановлен.
            await redis.expire(_DIGEST_KEY, _DIGEST_EVERY_SEC * 2)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Не удалось поставить сигнал в очередь сводки", error=str(exc)
            )

    async def _flush_digest(self, now: datetime) -> None:
        """Отправляет ОДНО сводное сообщение о придержанных потолком сигналах.

        Сводка выходит не чаще одного раза в час: она сама является сообщением,
        и без ограничения сводки стали бы тем самым потоком, от которого
        защищает потолок. В счёт потолка сводка НЕ идёт — иначе переполнение
        отнимало бы слот у обычных уведомлений, то есть наказывало бы за то,
        о чём отчитывается. Потолок пользовательских сообщений в час поэтому
        равен ``max_per_hour`` плюс одна сводка.
        """
        try:
            redis = get_redis()
            if await redis.llen(_DIGEST_KEY) == 0:
                return
            last_raw = await redis.get(_DIGEST_LAST_KEY)
            if last_raw:
                elapsed = (now - datetime.fromisoformat(last_raw)).total_seconds()
                if elapsed < _DIGEST_EVERY_SEC:
                    return
            raw_entries = await redis.lrange(_DIGEST_KEY, 0, -1)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Не удалось прочитать очередь сводки", error=str(exc))
            return

        entries = []
        for raw in raw_entries:
            try:
                entries.append(json.loads(raw))
            except Exception:  # noqa: BLE001, S112
                continue
        if not entries:
            return

        sent = await send_message(
            format_digest_message(entries, self.cfg.max_per_hour)
        )
        if not sent:
            # Не отправилось — очередь НЕ чистим: сводка уйдёт следующей
            # итерацией. Потерять её значит потерять единственное упоминание
            # придержанных сигналов.
            self._log.warning("Сводка не отправлена, повтор позже", held=len(entries))
            return
        try:
            redis = get_redis()
            await redis.delete(_DIGEST_KEY)
            await redis.set(_DIGEST_LAST_KEY, now.isoformat(), ex=_DIGEST_EVERY_SEC * 2)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Сводка отправлена, очередь не очищена", error=str(exc))
        self._log.info("Отправлена сводка придержанных сигналов", held=len(entries))

    async def _sent_last_hour(self, now: datetime) -> int:
        """Сколько уведомлений ушло за последний час (по всем инструментам).

        Хранится упорядоченным множеством Redis: ключ — момент отправки, вес —
        он же в секундах. Скользящее окно, а не счётчик с обнулением в начале
        часа: со счётчиком шесть уведомлений в 10:59 и ещё шесть в 11:01
        уложились бы в «потолок 6 в час», хотя человек получил бы двенадцать за
        две минуты.

        Недоступность Redis не должна затыкать уведомления совсем, поэтому при
        ошибке возвращается 0 — ограничение по потолку в этот момент не
        действует, а выдержка по инструменту продолжает работать.
        """
        if self.cfg.max_per_hour <= 0:
            return 0
        try:
            redis = get_redis()
            edge = now.timestamp() - 3600
            await redis.zremrangebyscore(_SENT_KEY, "-inf", edge)
            return int(await redis.zcard(_SENT_KEY))
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Не удалось прочитать счётчик уведомлений за час", error=str(exc)
            )
            return 0

    async def _record_sent(self, now: datetime) -> None:
        """Отмечает факт отправки в скользящем окне часа."""
        try:
            redis = get_redis()
            await redis.zadd(_SENT_KEY, {now.isoformat(): now.timestamp()})
            # Ключ живёт заведомо дольше окна: чистка идёт по весу, а срок жизни
            # нужен только чтобы ключ не оставался навсегда после остановки.
            await redis.expire(_SENT_KEY, 7200)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Не удалось записать отправку в счётчик часа", error=str(exc)
            )

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
