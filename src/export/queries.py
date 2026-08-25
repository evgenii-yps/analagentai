"""SQL-запросы выгрузки: выборка сигналов, агрегаты, учёт выгруженного.

Функции принимают соединение/пул asyncpg и возвращают простые словари, чтобы
оставаться независимыми от остального слоя доступа к БД (скрипт работает на
хосте по 127.0.0.1 и держит собственный пул).
"""

from __future__ import annotations

from typing import Any

import asyncpg

# Общий набор колонок сигнала с приджойненными оценками горизонтов 1h/4h.
# agents_payload берём как текст (::text), чтобы сохранить исходный JSON дословно.
_SIGNAL_COLUMNS = """
    s.id,
    s.ts,
    s.decision,
    s.probability,
    s.status,
    s.rationale,
    s.notified,
    s.notified_at,
    s.agents_payload::text AS agents_payload,
    i.base AS token,
    e1.price_at_signal AS p_signal_1h,
    e1.price_at_close  AS p_close_1h,
    e1.pnl_pct         AS pnl_1h,
    e1.drawdown_pct    AS dd_1h,
    e1.success         AS succ_1h,
    e4.price_at_signal AS p_signal_4h,
    e4.price_at_close  AS p_close_4h,
    e4.pnl_pct         AS pnl_4h,
    e4.drawdown_pct    AS dd_4h,
    e4.success         AS succ_4h,
    s.logic_version    AS logic_version,
    s.degraded         AS degraded,
    s.calibrated_probability AS calibrated_probability,
    s.calibration_id   AS calibration_id,
    s.inputs_hash      AS inputs_hash,
    s.is_repeat        AS is_repeat
"""

_SIGNAL_JOINS = """
    FROM signals s
    JOIN instruments i ON i.id = s.instrument_id
    LEFT JOIN signal_evaluations e1 ON e1.signal_id = s.id AND e1.horizon = '1h'
    LEFT JOIN signal_evaluations e4 ON e4.signal_id = s.id AND e4.horizon = '4h'
"""

# Выражение начала 4-часового окна (совпадает с transform.window_4h_start).
_WINDOW_EXPR = (
    "date_trunc('hour', s.ts) - (extract(hour from s.ts)::int % 4) * interval '1 hour'"
)


# --- Версия логики в выборке (Этап 8.2 §9) ----------------------------------
#
# ЗАМЕР 24.08.2026: лист «Независимые окна» содержал строки версий 1, 2 и 3
# вперемешку, потому что в условии отбора не было ни слова про logic_version.
# Смешанный лист непригоден как источник: сравнивать исходы разных версий
# логики нельзя по построению — у них разный состав агентов, разные пороги и
# разный набор горизонтов. Ждать на нём «накопления статистики» бессмысленно.
#
# Версия НЕ зашивается числом в код: она берётся из ``logic_version_windows``,
# где её фиксирует Decision Agent при первом старте на новой версии.

LOGIC_VERSION_ALL = "all"
LOGIC_VERSION_CURRENT = "current"

# Версия 0 означает «версия неизвестна» (миграция 012) и не является настоящей
# версией. Такие строки не попадают в выборку НИ ПРИ КАКОМ значении параметра
# (§9.5): смешать их с чем угодно — значит объявить неизвестное известным.
UNKNOWN_LOGIC_VERSION = 0


class ExportVersionError(ValueError):
    """Неверное значение EXPORT_LOGIC_VERSION: выгрузка не начинается вовсе."""


async def resolve_logic_version(
    conn: asyncpg.Connection,
    raw: str,
) -> int | None:
    """Значение EXPORT_LOGIC_VERSION → номер версии или ``None`` для «all».

    ``current`` — ПОСЛЕДНЯЯ ОТКРЫТАЯ версия из ``logic_version_windows``, то
    есть та, на которой система работает сейчас. Число — именно эта версия.
    ``all`` — фильтра по версии нет, и лист обязан начинаться прямой оговоркой
    о смешивании (§9.3).

    Пустая ``logic_version_windows`` при ``current`` — это остановка, а не
    молчаливый переход к «all»: без границы версии выборка была бы смешанной
    ровно в том виде, ради устранения которого параметр и заводился.
    """
    value = (raw or "").strip().lower()
    if not value:
        value = LOGIC_VERSION_CURRENT
    if value == LOGIC_VERSION_ALL:
        return None
    if value == LOGIC_VERSION_CURRENT:
        version = await conn.fetchval(
            "SELECT logic_version FROM logic_version_windows "
            "WHERE logic_version > 0 ORDER BY started_at DESC, logic_version DESC "
            "LIMIT 1;"
        )
        if version is None:
            raise ExportVersionError(
                "EXPORT_LOGIC_VERSION=current, но таблица logic_version_windows "
                "пуста: границу версии фиксирует Decision Agent при старте. "
                "Пока её нет, выборка была бы смешанной по версиям — а именно "
                "это §9 ТЗ 8.2 и запрещает"
            )
        return int(version)
    try:
        version = int(value)
    except ValueError as exc:
        raise ExportVersionError(
            f"EXPORT_LOGIC_VERSION={raw!r}: допустимы «current», целое число "
            f"или «all»"
        ) from exc
    if version <= 0:
        raise ExportVersionError(
            "EXPORT_LOGIC_VERSION: ноль зарезервирован под признак «версия "
            "неизвестна» и настоящей версией быть не может"
        )
    return version


def logic_version_condition(version: int | None, placeholder: str) -> str:
    """Условие отбора по версии логики для WHERE.

    Версия 0 отсекается ВСЕГДА, даже при ``all``: «версия неизвестна» — это не
    версия, и в выборку такие строки не попадают ни при каком значении (§9.5).
    """
    if version is None:
        return f"s.logic_version <> {UNKNOWN_LOGIC_VERSION}"
    return f"s.logic_version = {placeholder}"


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Идемпотентно применяет схему §4 (таблица учёта + колонка notified_at)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_exports (
            id          BIGSERIAL PRIMARY KEY,
            signal_id   BIGINT NOT NULL REFERENCES signals(id),
            target      TEXT NOT NULL,
            exported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (signal_id, target)
        );
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_exports_target "
        "ON signal_exports (target, signal_id);"
    )
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;"
    )
    # Колонка degraded (Этап 7.2, Задача A2). Выгрузка держит собственный пул и
    # может стартовать раньше, чем Decision Agent добавит колонку, — гарантируем
    # её и здесь, чтобы SELECT s.degraded не падал на старом томе.
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS degraded BOOLEAN NOT NULL "
        "DEFAULT FALSE;"
    )
    # Колонки Этапа 7.3 (калиброванная вероятность и учёт инерции входов) — по той
    # же причине: выгрузка держит свой пул и может стартовать раньше сервисов,
    # применяющих миграцию, а SELECT по отсутствующей колонке упал бы.
    await conn.execute(
        "ALTER TABLE signals "
        "ADD COLUMN IF NOT EXISTS calibrated_probability DOUBLE PRECISION;"
    )
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS calibration_id BIGINT;"
    )
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS inputs_hash TEXT;"
    )
    await conn.execute(
        "ALTER TABLE signals ADD COLUMN IF NOT EXISTS is_repeat BOOLEAN NOT NULL "
        "DEFAULT FALSE;"
    )


async def fetch_unexported_for_sheets(
    conn: asyncpg.Connection,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Закрытые сигналы без отметки target='sheets', ts ASC, не более batch_size."""
    query = f"""
        SELECT {_SIGNAL_COLUMNS}
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
          AND NOT EXISTS (
              SELECT 1 FROM signal_exports x
              WHERE x.signal_id = s.id AND x.target = 'sheets'
          )
        ORDER BY s.ts ASC
        LIMIT $1;
    """
    rows = await conn.fetch(query, batch_size)
    return [dict(r) for r in rows]


async def fetch_independent_windows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """По одному (самому раннему) закрытому сигналу на каждое 4-часовое окно.

    Оставлено ради совместимости прежнего листа. Этап 8.1 §7 требует разбиения
    по КАЖДОМУ ТОКЕНУ и КАЖДОМУ ГОРИЗОНТУ — см. :func:`fetch_independent_by_token_horizon`.
    """
    query = f"""
        SELECT DISTINCT ON (i.id, win) {_SIGNAL_COLUMNS},
               {_WINDOW_EXPR} AS win
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
        ORDER BY i.id, win, s.ts ASC;
    """
    rows = await conn.fetch(query)
    return [dict(r) for r in rows]


async def fetch_independent_by_token_horizon(
    conn: asyncpg.Connection,
    horizons_h: list[int],
    logic_version: int | None = None,
) -> list[dict[str, Any]]:
    """Независимые наблюдения ПО ТОКЕНУ И ПО ГОРИЗОНТУ (§7 ТЗ 8.1).

    Окно равно длине горизонта, границы кратны ему от эпохи; из окна берётся
    ПЕРВЫЙ по времени сигнал. Прореживание считается отдельно для каждого
    (токен, горизонт): решения выдаются раз в минуту, и без прореживания
    соседние наблюдения описывали бы почти один отрезок рынка.

    Ключ прореживания включает инструмент. Без него пять токенов делили бы одно
    окно и от каждого окна оставался бы один сигнал — четыре пятых наблюдений
    исчезли бы молча.
    """
    if not horizons_h:
        return []
    # Фильтр по версии логики (§9.1 ТЗ 8.2). Без него лист смешивал версии, и
    # прореживание «одно наблюдение на окно» выбирало ПЕРВЫЙ сигнал окна —
    # то есть систематически более старую версию.
    args: list[Any] = [[int(h) for h in horizons_h]]
    if logic_version is not None:
        args.append(int(logic_version))
    condition = logic_version_condition(logic_version, f"${len(args)}")
    query = f"""
        SELECT DISTINCT ON (e.horizon_h, i.id, win)
               {_SIGNAL_COLUMNS},
               e.horizon_h        AS horizon_h,
               e.price_at_signal  AS h_price_at_signal,
               e.price_at_close   AS h_price_at_close,
               e.pnl_pct          AS h_pnl_pct,
               e.drawdown_pct     AS h_drawdown_pct,
               e.success          AS h_success,
               to_timestamp(
                   floor(extract(epoch FROM s.ts) / (e.horizon_h * 3600))
                   * (e.horizon_h * 3600)
               ) AS win
        {_SIGNAL_JOINS}
        JOIN signal_evaluations e ON e.signal_id = s.id
        WHERE s.decision <> 'wait'
          AND e.horizon_h = ANY($1::int[])
          AND {condition}
        ORDER BY e.horizon_h, i.id, win, s.ts ASC;
    """
    rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def fetch_outcome_correlation(
    conn: asyncpg.Connection,
    horizons_h: list[int],
    logic_version: int | None = None,
) -> list[dict[str, Any]]:
    """Корреляция исходов между токенами по каждому горизонту (§7 ТЗ 8.1).

    Считается по НЕЗАВИСИМЫМ наблюдениям (одно на окно и токен) на совпадающих
    окнах: корреляция попаданий (success как 1/0) для каждой пары токенов.
    Именно эта величина показывает, почему пять токенов не дают пятикратного
    роста статистической мощности.
    """
    if not horizons_h:
        return []
    # §9.1 и §9.4 ТЗ 8.2. До этой правки корреляция исходов считалась ПОПЕРЁК
    # версий логики, и увидеть это по листу было невозможно: колонки версии в
    # нём не было вовсе. Теперь версия и отбирается, и печатается.
    args: list[Any] = [[int(h) for h in horizons_h]]
    if logic_version is not None:
        args.append(int(logic_version))
    condition = logic_version_condition(logic_version, f"${len(args)}")
    query = f"""
        WITH indep AS (
            SELECT DISTINCT ON (e.horizon_h, s.instrument_id, win)
                   e.horizon_h,
                   s.instrument_id,
                   i.base AS token,
                   s.logic_version,
                   to_timestamp(
                       floor(extract(epoch FROM s.ts) / (e.horizon_h * 3600))
                       * (e.horizon_h * 3600)
                   ) AS win,
                   e.success
            FROM signals s
            JOIN instruments i ON i.id = s.instrument_id
            JOIN signal_evaluations e ON e.signal_id = s.id
            WHERE s.decision <> 'wait'
              AND e.horizon_h = ANY($1::int[])
              AND {condition}
            ORDER BY e.horizon_h, s.instrument_id, win, s.ts ASC
        )
        SELECT a.horizon_h,
               a.token AS token_a,
               b.token AS token_b,
               count(*) AS n,
               corr(CASE WHEN a.success THEN 1.0 ELSE 0.0 END,
                    CASE WHEN b.success THEN 1.0 ELSE 0.0 END) AS r,
               -- Версия печатается ОДНОЙ на пару: при фильтре она одна по
               -- построению, при «all» разные версии дают строку с пометкой
               -- «смешано» — молча усреднять их по колонке нельзя.
               CASE WHEN count(DISTINCT a.logic_version) = 1
                         AND count(DISTINCT b.logic_version) = 1
                         AND min(a.logic_version) = min(b.logic_version)
                    THEN min(a.logic_version)::text
                    ELSE 'смешано'
               END AS logic_version
        FROM indep a
        JOIN indep b
          ON b.horizon_h = a.horizon_h AND b.win = a.win AND a.token < b.token
        GROUP BY a.horizon_h, a.token, b.token
        ORDER BY a.horizon_h, a.token, b.token;
    """
    rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def fetch_notion_pending(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Закрытые сигналы с notified_at, без отметки target='notion', ts ASC."""
    query = f"""
        SELECT {_SIGNAL_COLUMNS}
        {_SIGNAL_JOINS}
        WHERE s.status = 'closed'
          AND s.notified_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM signal_exports x
              WHERE x.signal_id = s.id AND x.target = 'notion'
          )
        ORDER BY s.ts ASC;
    """
    rows = await conn.fetch(query)
    return [dict(r) for r in rows]


async def fetch_daily_summary(
    conn: asyncpg.Connection,
    min_probability: float,
) -> list[dict[str, Any]]:
    """Агрегаты по календарным суткам UTC для листа «Сводка по дням»."""
    query = """
        SELECT
            (s.ts AT TIME ZONE 'UTC')::date AS day,
            count(*) AS decisions_total,
            count(*) FILTER (WHERE s.decision = 'buy')  AS buy,
            count(*) FILTER (WHERE s.decision = 'sell') AS sell,
            count(*) FILTER (WHERE s.decision = 'wait') AS wait,
            count(*) FILTER (WHERE s.probability >= $1) AS candidates,
            count(*) FILTER (
                WHERE s.status = 'closed' AND s.notified_at IS NOT NULL
            ) AS notified,
            count(*) FILTER (WHERE s.status = 'closed') AS closed_4h,
            avg(CASE WHEN s.decision = 'buy'  AND e4.success IS NOT NULL
                     THEN e4.success::int END) AS sr_buy,
            avg(CASE WHEN s.decision = 'sell' AND e4.success IS NOT NULL
                     THEN e4.success::int END) AS sr_sell,
            avg(CASE WHEN s.decision = 'buy'  THEN e4.pnl_pct END) AS avg_pnl_buy,
            avg(CASE WHEN s.decision = 'sell' THEN e4.pnl_pct END) AS avg_pnl_sell,
            avg(e4.drawdown_pct) AS avg_dd,
            avg(s.probability)   AS avg_prob,
            mode() WITHIN GROUP (ORDER BY s.logic_version) AS logic_version_dominant,
            count(*) FILTER (WHERE s.degraded) AS degraded_count
        FROM signals s
        LEFT JOIN signal_evaluations e4
               ON e4.signal_id = s.id AND e4.horizon = '4h'
        GROUP BY day
        ORDER BY day ASC;
    """
    rows = await conn.fetch(query, float(min_probability))
    return [dict(r) for r in rows]


async def mark_exported(
    conn: asyncpg.Connection,
    signal_ids: list[int],
    target: str,
) -> None:
    """Ставит отметки о выгрузке (идемпотентно, ON CONFLICT DO NOTHING)."""
    if not signal_ids:
        return
    await conn.executemany(
        "INSERT INTO signal_exports (signal_id, target) VALUES ($1, $2) "
        "ON CONFLICT (signal_id, target) DO NOTHING;",
        [(sid, target) for sid in signal_ids],
    )


async def count_unexported(conn: asyncpg.Connection, target: str) -> int:
    """Сколько закрытых сигналов ещё не выгружено в указанную цель."""
    if target == "notion":
        query = """
            SELECT count(*) FROM signals s
            WHERE s.status = 'closed' AND s.notified_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM signal_exports x
                  WHERE x.signal_id = s.id AND x.target = 'notion'
              );
        """
    else:
        query = """
            SELECT count(*) FROM signals s
            WHERE s.status = 'closed'
              AND NOT EXISTS (
                  SELECT 1 FROM signal_exports x
                  WHERE x.signal_id = s.id AND x.target = 'sheets'
              );
        """
    return int(await conn.fetchval(query) or 0)


async def count_exported(conn: asyncpg.Connection, target: str) -> int:
    """Сколько строк учтено в signal_exports по цели (для сверки в отчёте)."""
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM signal_exports WHERE target = $1;", target
        )
        or 0
    )
