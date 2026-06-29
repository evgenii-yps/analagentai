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


@dataclass
class NotifyConfig:
    """Параметры решения об отправке."""

    min_probability: float
    cooldown_sec: float


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


def format_signal(signal: dict[str, Any], symbol: str, tz_name: str) -> str:
    """Форматирует сигнал в HTML-сообщение на русском.

    Время сигnala переводится из UTC в ``tz_name`` (напр. Europe/Moscow → МСК).
    """
    base = symbol.split("/")[0]
    decision = signal["decision"]
    if decision == "buy":
        emoji, action = "🟢", "ПОКУПАТЬ"
    else:
        emoji, action = "🔴", "ПРОДАВАТЬ"
    probability = round(float(signal["probability"]) * 100)

    ts: datetime = signal["ts"]
    local_ts = ts.astimezone(ZoneInfo(tz_name))
    label = _TZ_LABELS.get(tz_name) or local_ts.tzname() or tz_name
    when = local_ts.strftime("%Y-%m-%d %H:%M")

    rationale = signal.get("rationale") or "—"
    return (
        f"{emoji} <b>СИГНАЛ: {action} {base}</b>\n"
        f"Вероятность: <b>{probability}%</b>\n"
        f"Почему: {rationale}\n"
        f"Время: {when} {label}"
    )


class NotifyAgent:
    """Сервис уведомлений: читает сильные сигналы и шлёт их в Telegram."""

    def __init__(
        self,
        interval: float,
        min_probability: float,
        cooldown_sec: float,
        symbol: str,
        tz_name: str,
    ) -> None:
        self.interval = interval
        self.cfg = NotifyConfig(min_probability, cooldown_sec)
        self.symbol = symbol
        self.tz_name = tz_name
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
        last_decision, last_sent_ts = await self._get_last_state(instrument_id)
        now = datetime.now(UTC)

        if not should_notify(signal, last_decision, last_sent_ts, now, self.cfg):
            # Дубль/в пределах cooldown — «поглощаем» сигнал, чтобы не висел.
            await db.mark_signal_notified(signal["id"])
            return

        sent = await send_message(format_signal(signal, self.symbol, self.tz_name))
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
