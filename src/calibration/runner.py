"""Разовый прогон построения калибровочной кривой (Этап 7.3, Блок B).

Запускается контейнером по профилю ``tools`` (как выгрузка Этапа 6.6.1), раз
в сутки из cron. На хост никакие пакеты не ставятся: весь код исполняется
внутри образа приложения.

Поведение при нехватке данных — ШТАТНОЕ, а не ошибочное: кривая не строится,
активная кривая не подменяется, в лог пишется, сколько наблюдений есть и
сколько нужно. Код возврата остаётся нулевым, чтобы cron не слал письма
об ошибке каждую ночь до накопления выборки.
"""

from __future__ import annotations

import structlog

from src.calibration.curve import Observation, build_bins, curve_summary, to_independent
from src.core.config import settings
from src.core.db import db
from src.core.redis_client import close_redis, get_redis

# Ключ кэша активной кривой в Redis (его же читает Decision Agent).
CACHE_KEY_PREFIX = "calibration:active:"


async def build_once() -> int | None:
    """Строит и активирует кривую → id кривой либо None, если данных мало.

    Возвращает None и в случае, когда наблюдений меньше
    ``CALIBRATION_MIN_SAMPLES``: это не ошибка, а нормальное состояние первых
    суток после смены версии логики.
    """
    log = structlog.get_logger().bind(component="calibration")
    logic_version = settings.LOGIC_VERSION
    horizon = settings.CALIBRATION_HORIZON

    rows = await db.get_independent_outcomes(logic_version, horizon)
    # Прореживание уже сделано запросом; повторное применение чистой функции —
    # страховка и единственное место, где правило независимости описано кодом.
    observations = to_independent(
        Observation(
            ts=row["ts"],
            index=float(row["probability"]),
            success=bool(row["success"]),
        )
        for row in rows
    )

    if len(observations) < settings.CALIBRATION_MIN_SAMPLES:
        log.info(
            "Кривая не строится: независимых наблюдений меньше минимума",
            logic_version=logic_version,
            horizon=horizon,
            observations=len(observations),
            required=settings.CALIBRATION_MIN_SAMPLES,
        )
        return None

    bins, base_rate = build_bins(
        observations,
        settings.CALIBRATION_BINS,
        settings.CALIBRATION_PRIOR_WEIGHT,
    )
    window_from = observations[0].ts
    window_to = observations[-1].ts
    notes = (
        f"Независимые 4-часовые окна, горизонт {horizon}, "
        f"сглаживание k={settings.CALIBRATION_PRIOR_WEIGHT}, "
        f"монотонность не навязывалась."
    )
    curve_id = await db.save_calibration_curve(
        logic_version=logic_version,
        sample_size=len(observations),
        window_from=window_from,
        window_to=window_to,
        base_rate=base_rate,
        bins=bins,
        notes=notes,
    )
    log.info(
        "Калибровочная кривая построена и активирована",
        curve_id=curve_id,
        logic_version=logic_version,
        sample_size=len(observations),
        base_rate=round(base_rate, 4),
        bins=curve_summary(bins),
    )

    # Сбрасываем кэш, чтобы Decision Agent увидел новую кривую сразу, а не через
    # TTL. Недоступность Redis не должна валить построение — кривая уже в БД.
    try:
        await get_redis().delete(f"{CACHE_KEY_PREFIX}{logic_version}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось сбросить кэш кривой в Redis", error=str(exc))
    return curve_id


async def run() -> None:
    """Точка входа разового прогона: соединения → построение → освобождение."""
    log = structlog.get_logger().bind(component="calibration")
    log.info(
        "Запуск построения калибровочной кривой (Этап 7.3)",
        logic_version=settings.LOGIC_VERSION,
        min_samples=settings.CALIBRATION_MIN_SAMPLES,
        bins=settings.CALIBRATION_BINS,
    )
    await db.connect()
    try:
        await db.ensure_calibration_schema()
        await build_once()
    finally:
        await db.close()
        await close_redis()
        log.info("Построение калибровки завершено, ресурсы освобождены")
