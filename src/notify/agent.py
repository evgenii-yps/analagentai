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

import structlog

from src.core.config import settings
from src.core.db import db
from src.core.instruments import horizon_label
from src.core.redis_client import get_redis
from src.core.user_settings import (
    UserSettings,
    default_settings,
    user_filter_reason,
)
from src.notify.telegram import send_message
from src.notify.wording import (
    agent_paragraph,
    agent_silent_paragraph,
    is_confident,
)

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
# Кэш настроек получателя. Короткий намеренно: человек меняет их из
# бота в любой момент, и уведомления обязаны это заметить без
# перезапуска сервиса.
_SETTINGS_CACHE_SEC = 30
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
    # Горизонт, выбранный ПОЛЬЗОВАТЕЛЕМ (§1 ТЗ 8.3). В отборе не участвует —
    # сигнал един для всех горизонтов (Этап 8.1) — и влияет только на то, какой
    # горизонт назван в тексте.
    horizon_h: int = 4


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
# §3 ТЗ 8.3: обе оговорки присутствуют в КАЖДОМ сигнале и сокращению не
# подлежат. Вынесены константами, чтобы «сократить на одну строку» нельзя было
# случайно — правку константы видно в разборе изменений.
CLOSING_LINE = "Решение за вами, система не торгует сама."
UNPLUGGED_LINE = "Новостной и ончейн-анализ пока не подключены."


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
    """Цена с разделением тысяч пробелом: ``64210.0`` → ``64 210 USDT``.

    Дробная часть показывается только у дешёвых инструментов. Округление до
    целого было верно, пока в системе был один биткоин; с Этапа 8.1 в составе
    DOGE и XRP, и «0 USDT» вместо «0.1234 USDT» — не округление, а потеря цены.
    """
    value = float(price)
    magnitude = abs(value)
    if magnitude >= 100:
        return f"{round(value):,}".replace(",", " ") + f" {quote}"
    digits = 4 if magnitude >= 1 else 6
    return f"{value:.{digits}f}".rstrip("0").rstrip(".") + f" {quote}"


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
    since = built_at.strftime("%d.%m") if isinstance(built_at, datetime) else None
    if sample is not None and since:
        suffix = f" (по {int(sample)} наблюдениям с {since})"
    elif sample is not None:
        suffix = f" (по {int(sample)} наблюдениям)"
    elif since:
        suffix = f" (по наблюдениям с {since})"
    else:
        suffix = ""
    return f"Такие сигналы сбывались раньше в {percent}% случаев{suffix}"


def format_signal_message(
    signal: dict[str, Any],
    price: float | None,
    cfg: SignalFormatConfig,
    metrics_by_agent: dict[str, dict[str, Any] | None] | None = None,
    target_block: list[str] | None = None,
) -> str:
    """Развёрнутый текст сигнала человеческим языком (§3 ТЗ 8.3). Чистая функция.

    СОБИРАЕТСЯ ИЗ БЛОКОВ, а не из плоского списка строк. Это не стилистика:
    §3 требует предусмотреть место под блок цели так, чтобы Этап 8.2 добавлял
    его, НЕ переписывая сборку текста. Блок цели уже стоит в порядке блоков и
    сейчас пуст; пустые блоки выпадают из результата. Этапу 8.2 останется
    передать ``target_block`` — ни одна строка этой функции не изменится.

    ``metrics_by_agent`` — метрики каждого высказавшегося агента, взятые у
    ТОГО САМОГО вывода, который участвовал в решении. Именно из них строится
    объяснение (:mod:`src.notify.wording`); выдумывать наблюдения, которых в
    метриках нет, запрещено (§7 ТЗ). Метрик нет — так и написано, а не
    заменено правдоподобной фразой.

    Внутренние термины («индекс согласия», «перцентиль», ``logic_version``,
    ``confidence``) в тексте не появляются: он предназначен человеку, который
    биржевых терминов не знает.
    """
    base, _quote = _split_symbol(cfg.symbol)
    action = "ПОКУПКА" if signal["decision"] == "buy" else "ПРОДАЖА"
    metrics_by_agent = metrics_by_agent or {}

    header = [
        f"<b>{base} · {action} · горизонт {horizon_ru(horizon_label(cfg.horizon_h))}</b>"
    ]

    price_block = [] if price is None else [f"Цена сейчас: {format_price(price, _quote)}"]
    # Единственное число, которое система называет вероятностью, и только когда
    # оно выведено из фактических исходов (Этап 7.3 §4.1). Нет кривой — нет
    # строки: подставлять вместо неё что-то похожее запрещено.
    history_line = format_calibrated_line(signal)
    if history_line:
        price_block.append(history_line)

    # --- Блок цели (Этап 8.2). Сейчас пуст: таблиц risk_targets и
    # signal_targets ещё нет, а «Цель», «Вероятность достижения», «Возможная
    # просадка» и «Комиссия» без них были бы выдуманными числами.
    goal_block = list(target_block or ())

    payload = normalize_payload(signal.get("agents_payload"))
    present = {e.get("agent") for e in payload}
    reasons = ["Почему такой вывод:"]
    for name in AGENT_ORDER:
        entry = next((e for e in payload if e.get("agent") == name), None)
        if entry is not None:
            reasons.append(agent_paragraph(
                name,
                str(entry.get("signal")),
                float(entry.get("confidence", 0.0)),
                metrics_by_agent.get(name),
            ))
    # Молчащий агент называется ЯВНО и НИКОГДА не пропускается (§3, §7 ТЗ):
    # отсутствие мнения — это тоже сведение о качестве сигнала, и человек,
    # не увидев строки, счёл бы, что высказались все.
    for name in AGENT_ORDER:
        if name not in present:
            reasons.append(agent_silent_paragraph(name))

    agreement_block = _agreement_block(payload)

    disclaimer = [
        "Важно: система не предсказывает цену. Она оценивает вероятность "
        "и уменьшает неопределённость — гарантий не даёт. " + CLOSING_LINE,
        "",
        UNPLUGGED_LINE,
    ]

    blocks = [header, price_block, goal_block, reasons, agreement_block, disclaimer]
    return "\n\n".join("\n".join(block) for block in blocks if block)


def _agreement_block(payload: list[dict[str, Any]]) -> list[str]:
    """Строка «Согласие агентов: N из M уверенно, K слабо».

    Считаются агенты, которые ВЫСКАЗАЛИСЬ: молчащий не входит ни в уверенные,
    ни в слабые — он вообще не голосовал, и включать его в знаменатель значило
    бы засчитывать молчание за мнение.
    """
    voiced = [e for e in payload if e.get("signal") in SIGNAL_VALUE]
    if not voiced:
        return []
    confident = sum(1 for e in voiced if is_confident(float(e.get("confidence", 0.0))))
    weak = len(voiced) - confident
    tail = f", {weak} слабо" if weak else ""
    return [f"Согласие агентов: {confident} из {len(voiced)} уверенно{tail}."]


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
        recipients: tuple[int, ...] | list[int] | None = None,
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
        # Получатели уведомлений — белый список чатов бота (§1 ТЗ 8.3:
        # настройки принадлежат чату). По умолчанию это единственный чат
        # владельца, и тогда поведение ровно то же, что было до этапа.
        self.recipients: tuple[int, ...] = tuple(
            int(chat) for chat in sorted(recipients or ()) if str(chat).strip()
        )
        self._settings_cache: dict[int, tuple[UserSettings, datetime]] = {}
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
        now = datetime.now(UTC)
        for chat_id in self.recipients:
            await self._flush_digest(chat_id, now)

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """Разбирает сигнал и рассылает его тем, кому он нужен (§2 ТЗ 8.3).

        Получателей может быть несколько (белый список чатов), и настройки у
        каждого свои, поэтому решение «слать или нет» принимается ДЛЯ КАЖДОГО
        ОТДЕЛЬНО. Признак «сигнал обработан» ставится один раз в конце:
        ``notified`` — если ушёл хотя бы одному, иначе «поглощён».
        """
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

        now = datetime.now(UTC)
        strength = self._strength(signal)
        metrics_by_agent: dict[str, dict[str, Any] | None] | None = None
        price_loaded = False
        price: float | None = None
        sent_any = False

        for chat_id in self.recipients:
            user = await self._user_settings(chat_id)

            # ПОРЯДОК ПРОВЕРОК ЗАДАН §2 ТЗ и не случаен: сначала отсекается то,
            # что человеку не нужно, и только потом применяются ограничения
            # потока. Иначе сигнал по невыбранному токену занимал бы выдержку и
            # придерживал бы тот токен, который человек как раз ждёт.
            unwanted = user_filter_reason(user, instrument_id, strength, now)
            if unwanted is not None:
                self._log.info(
                    "Уведомление не нужно получателю",
                    signal_id=signal["id"], chat_id=chat_id, reason=unwanted,
                )
                continue

            last_decision, last_sent_ts = await self._get_last_state(
                chat_id, instrument_id
            )
            if not should_notify(signal, last_decision, last_sent_ts, now, self.cfg):
                continue

            # Защита от потока. Придержанный сигнал не откладывается:
            # уведомление о рыночном сигнале имеет смысл в свой момент, а через
            # час это уже не уведомление, а история. Сам сигнал сохранён и
            # попадает в оценку — теряется только сообщение.
            limited = rate_limit_reason(
                last_sent_ts, now, await self._sent_last_hour(chat_id, now), self.cfg
            )
            if limited is not None:
                # Придержанное ПОТОЛКОМ не пропадает молча: §2 требует одного
                # сводного сообщения. Придержанное ВЫДЕРЖКОЙ в сводку не идёт —
                # это нормальный ход по одному токену, а не переполнение.
                if limited.startswith(_CAP_REASON_PREFIX):
                    await self._queue_for_digest(chat_id, signal, instrument_id)
                self._log.info(
                    "Уведомление придержано защитой от потока",
                    signal_id=signal["id"], chat_id=chat_id,
                    instrument_id=instrument_id, reason=limited,
                )
                continue

            # Цена и метрики читаются ОДИН раз на сигнал и только когда он
            # кому-то нужен: это чтения из БД, и делать их ради сигнала, который
            # никому не уйдёт, незачем.
            if not price_loaded:
                price = await db.get_price_at(instrument_id, signal["ts"])
                price_loaded = True
            if metrics_by_agent is None:
                metrics_by_agent = await self._agent_metrics(signal, instrument_id)

            fmt_cfg = replace(
                await self._format_config(instrument_id), horizon_h=user.horizon_h
            )
            text = format_signal_message(signal, price, fmt_cfg, metrics_by_agent)
            if not await send_message(text, chat_id=str(chat_id)):
                # Не отправилось (сеть/Telegram) — состояние не двигаем, чтобы
                # следующая итерация попробовала снова.
                self._log.warning(
                    "Отправка не удалась, повтор позже",
                    signal_id=signal["id"], chat_id=chat_id,
                )
                continue

            sent_any = True
            await self._set_last_state(chat_id, instrument_id, signal["decision"], now)
            await self._record_sent(chat_id, now)
            self._log.info(
                "Уведомление отправлено",
                signal_id=signal["id"], chat_id=chat_id,
                decision=signal["decision"], probability=signal["probability"],
            )

        if sent_any:
            await db.mark_signal_notified(signal["id"])
        else:
            # Никому не ушло — «поглощаем», чтобы сигнал не висел в очереди
            # вечно. notified_at НЕ ставится: отправки не было.
            await db.mark_signal_absorbed(signal["id"])

    async def _user_settings(self, chat_id: int) -> UserSettings:
        """Настройки получателя; при отсутствии записи — значения по умолчанию.

        Отсутствие записи не ошибка и не повод её создавать: человек мог ни
        разу не открыть меню (§1 ТЗ). Кэш короткий — настройки меняются из бота
        в любой момент, и уведомления обязаны это замечать без перезапуска.
        """
        cached = self._settings_cache.get(chat_id)
        if cached is not None:
            value, cached_at = cached
            if (datetime.now(UTC) - cached_at).total_seconds() < _SETTINGS_CACHE_SEC:
                return value
        row = None
        try:
            row = await db.get_user_settings(chat_id)
        except Exception as exc:  # noqa: BLE001
            # Настройки недоступны — шлём по умолчанию, а не молчим: тишина
            # выглядела бы как отсутствие сигналов.
            self._log.warning(
                "Настройки получателя не прочитаны, действуют значения по умолчанию",
                chat_id=chat_id, error=str(exc),
            )
        settings_obj = (
            default_settings(chat_id, self.cfg.min_probability)
            if row is None
            else UserSettings(
                chat_id=chat_id,
                instruments=tuple(int(i) for i in row["instruments"]),
                horizon_h=int(row["horizon_h"]),
                min_score=float(row["min_score"]),
                quiet_from=None if row["quiet_from"] is None else int(row["quiet_from"]),
                quiet_to=None if row["quiet_to"] is None else int(row["quiet_to"]),
            )
        )
        self._settings_cache[chat_id] = (settings_obj, datetime.now(UTC))
        return settings_obj

    async def _agent_metrics(
        self, signal: dict[str, Any], instrument_id: int
    ) -> dict[str, dict[str, Any] | None]:
        """Метрики каждого высказавшегося агента на момент ЕГО вывода (§3 ТЗ).

        Момент берётся из ``agents_payload``: объяснять решение показаниями,
        которых в нём не было, нельзя. Не нашлось — остаётся ``None``, и текст
        честно скажет, что показатели не сохранились.
        """
        result: dict[str, dict[str, Any] | None] = {}
        for entry in normalize_payload(signal.get("agents_payload")):
            agent = str(entry.get("agent"))
            ts_raw = entry.get("ts")
            if not ts_raw:
                result[agent] = None
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                result[agent] = None
                continue
            try:
                result[agent] = await db.get_agent_metrics(agent, instrument_id, ts)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "Метрики агента не прочитаны", agent=agent, error=str(exc)
                )
                result[agent] = None
        return result

    async def _get_last_state(
        self, chat_id: int, instrument_id: int
    ) -> tuple[str | None, datetime | None]:
        """Последнее отправленное ЭТОМУ получателю решение по инструменту.

        Ключ несёт и получателя: настройки у людей разные, и выдержка одного не
        должна затыкать уведомления другому.
        """
        raw = await get_redis().get(f"notify:last:{chat_id}:{instrument_id}")
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
            return data["decision"], datetime.fromisoformat(data["sent_ts"])
        except Exception:
            return None, None

    async def _set_last_state(
        self, chat_id: int, instrument_id: int, decision: str, sent_ts: datetime
    ) -> None:
        """Сохраняет последнее отправленное решение и время по получателю."""
        data = json.dumps({"decision": decision, "sent_ts": sent_ts.isoformat()})
        await get_redis().set(f"notify:last:{chat_id}:{instrument_id}", data)

    def _strength(self, signal: dict[str, Any]) -> float:
        """Сила сигнала — та же величина, по которой он отбирался к отправке."""
        if self.cfg.use_calibrated:
            calibrated = signal.get("calibrated_probability")
            if calibrated is not None:
                return float(calibrated)
        return float(signal.get("probability") or 0.0)

    async def _queue_for_digest(
        self, chat_id: int, signal: dict[str, Any], instrument_id: int
    ) -> None:
        """Кладёт придержанный потолком сигнал в очередь сводки получателя."""
        try:
            fmt_cfg = await self._format_config(instrument_id)
            entry = json.dumps({
                "symbol": fmt_cfg.symbol,
                "decision": signal["decision"],
                "strength": self._strength(signal),
            })
            redis = get_redis()
            await redis.rpush(f"{_DIGEST_KEY}:{chat_id}", entry)
            # Срок жизни заведомо больше периода сводки: очередь без сводки не
            # должна копиться вечно, если сервис остановлен.
            await redis.expire(f"{_DIGEST_KEY}:{chat_id}", _DIGEST_EVERY_SEC * 2)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Не удалось поставить сигнал в очередь сводки", error=str(exc)
            )

    async def _flush_digest(self, chat_id: int, now: datetime) -> None:
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
            queue_key = f"{_DIGEST_KEY}:{chat_id}"
            last_key = f"{_DIGEST_LAST_KEY}:{chat_id}"
            if await redis.llen(queue_key) == 0:
                return
            last_raw = await redis.get(last_key)
            if last_raw:
                elapsed = (now - datetime.fromisoformat(last_raw)).total_seconds()
                if elapsed < _DIGEST_EVERY_SEC:
                    return
            raw_entries = await redis.lrange(queue_key, 0, -1)
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
            format_digest_message(entries, self.cfg.max_per_hour),
            chat_id=str(chat_id),
        )
        if not sent:
            # Не отправилось — очередь НЕ чистим: сводка уйдёт следующей
            # итерацией. Потерять её значит потерять единственное упоминание
            # придержанных сигналов.
            self._log.warning("Сводка не отправлена, повтор позже", held=len(entries))
            return
        try:
            redis = get_redis()
            await redis.delete(f"{_DIGEST_KEY}:{chat_id}")
            await redis.set(
                f"{_DIGEST_LAST_KEY}:{chat_id}", now.isoformat(),
                ex=_DIGEST_EVERY_SEC * 2,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("Сводка отправлена, очередь не очищена", error=str(exc))
        self._log.info(
            "Отправлена сводка придержанных сигналов",
            chat_id=chat_id, held=len(entries),
        )

    async def _sent_last_hour(self, chat_id: int, now: datetime) -> int:
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
            key = f"{_SENT_KEY}:{chat_id}"
            edge = now.timestamp() - 3600
            await redis.zremrangebyscore(key, "-inf", edge)
            return int(await redis.zcard(key))
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Не удалось прочитать счётчик уведомлений за час", error=str(exc)
            )
            return 0

    async def _record_sent(self, chat_id: int, now: datetime) -> None:
        """Отмечает факт отправки в скользящем окне часа этого получателя."""
        try:
            redis = get_redis()
            key = f"{_SENT_KEY}:{chat_id}"
            await redis.zadd(key, {now.isoformat(): now.timestamp()})
            # Ключ живёт заведомо дольше окна: чистка идёт по весу, а срок жизни
            # нужен только чтобы ключ не оставался навсегда после остановки.
            await redis.expire(key, 7200)
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
