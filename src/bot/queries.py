"""SELECT-запросы бота и идемпотентное создание роли только на чтение.

Сервис бота подключается к БД ролью ``agenttrade_ro``. Права — SELECT на всё
и запись ТОЛЬКО в ``user_settings`` (§1 ТЗ 8.3: меню настроек обязано их
сохранять). Наблюдения, выводы агентов и решения бот по-прежнему испортить не
может ничем — даже при ошибке в коде (ТЗ §3.5, §8).

Границу 4-часового окна считаем ТЕМ ЖЕ выражением, что и выгрузка 6.6
(src.export.queries) и §7.1 ТЗ 6.6 — чтобы «честная выборка» совпадала.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import structlog

from src.core.db import BALANCE_SQL
from src.core.user_settings import USER_SETTINGS_DDL

_log = structlog.get_logger().bind(component="bot-queries")

# Выражение начала непересекающегося 4-часового окна UTC.
_WINDOW_EXPR = (
    "date_trunc('hour', s.ts) - (extract(hour from s.ts)::int % 4) * interval '1 hour'"
)

# Потоки данных для /summary (те же таблицы, что в daily_report.py — §5).
DATA_STREAMS: list[tuple[str, str]] = [
    ("OHLCV", "ohlcv"),
    ("Сделки (trades)", "trades"),
    ("Стакан (orderbook)", "orderbook_snapshots"),
    ("Funding", "funding"),
    ("Open interest", "open_interest"),
    ("Выводы агентов", "agent_outputs"),
]

# Секунды в периоде /stats. 0 — «за всё время» (без ограничения по ts).
PERIOD_SECONDS: dict[str, int] = {
    "24h": 86_400,
    "7d": 604_800,
    "30d": 2_592_000,
    "all": 0,
}


async def ensure_readonly_role(
    conn: asyncpg.Connection,
    ro_password: str,
    db_name: str,
) -> None:
    """Идемпотентно создаёт роль ``agenttrade_ro`` (SELECT-only) и выдаёт права.

    Пароль подставляется через ``quote_literal`` (%L) на стороне PostgreSQL —
    без ручной сборки строки. Пароль синхронизируется с .env при каждом старте
    (ALTER), чтобы вход роли работал даже после ротации пароля установщиком.
    Соединение ``conn`` — под основным пользователем (владельцем БД), только для
    этого одноразового действия; запросы бота идут уже под ролью ro.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro';"
    )
    if not exists:
        stmt = await conn.fetchval(
            "SELECT format('CREATE ROLE agenttrade_ro LOGIN PASSWORD %L', $1::text);",
            ro_password,
        )
        await conn.execute(stmt)
        _log.info("Роль agenttrade_ro создана")
    else:
        stmt = await conn.fetchval(
            "SELECT format('ALTER ROLE agenttrade_ro LOGIN PASSWORD %L', $1::text);",
            ro_password,
        )
        await conn.execute(stmt)

    grant_db = await conn.fetchval(
        "SELECT format('GRANT CONNECT ON DATABASE %I TO agenttrade_ro', $1::text);",
        db_name,
    )
    await conn.execute(grant_db)
    await conn.execute("GRANT USAGE ON SCHEMA public TO agenttrade_ro;")
    await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO agenttrade_ro;")
    await conn.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT ON TABLES TO agenttrade_ro;"
    )
    # Таблица настроек создаётся ЗДЕСЬ же, до выдачи прав на неё. Порядок
    # старта сервисов не задан: бот может подняться раньше уведомлений, и тогда
    # GRANT на несуществующую таблицу срывал бы подготовку роли — бот повторял
    # бы попытку бесконечно и не отвечал бы вообще. DDL общий (один источник).
    await conn.execute(USER_SETTINGS_DDL)

    # ЕДИНСТВЕННОЕ исключение из «только чтение»: настройки самого пользователя
    # (§1 ТЗ 8.3). Бот обязан их сохранять, иначе меню было бы декорацией.
    # Право выдано ТОЧЕЧНО на одну таблицу: испортить данные наблюдений или
    # решений бот по-прежнему не может ничем.
    await conn.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON user_settings TO agenttrade_ro;"
    )
    _log.info(
        "Права выданы: SELECT на всё, запись — только в user_settings (§1 ТЗ 8.3)"
    )


class BotQueries:
    """Обёртка над RO-пулом asyncpg: только SELECT-запросы для команд бота."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # --- /status ---

    async def spot_instruments(self) -> list[tuple[int, str]]:
        """Спотовые инструменты для меню настроек, по возрастанию идентификатора.

        Именно спот: сигналы выдаются по нему (Decision Agent пишет
        ``instrument_id`` спота), и настройки человека сравниваются с ним же.
        Контракты в меню не показываются — человек выбирает ТОКЕН, а не рынок.
        """
        rows = await self._pool.fetch(
            "SELECT id, symbol FROM instruments "
            " WHERE symbol NOT LIKE '%:%' ORDER BY id;"
        )
        return [(int(r["id"]), str(r["symbol"])) for r in rows]

    async def user_settings(self, chat_id: int) -> dict[str, Any] | None:
        """Настройки чата или ``None``, если человек их ни разу не открывал."""
        row = await self._pool.fetchrow(
            "SELECT chat_id, instruments, horizon_h, min_score, quiet_from, quiet_to "
            "  FROM user_settings WHERE chat_id = $1;",
            int(chat_id),
        )
        return dict(row) if row else None

    async def save_user_settings(
        self,
        chat_id: int,
        instruments: list[int],
        horizon_h: int,
        min_score: float,
        quiet_from: int | None,
        quiet_to: int | None,
    ) -> None:
        """Сохраняет настройки чата целиком (единственная запись бота в БД)."""
        await self._pool.execute(
            """
            INSERT INTO user_settings
                (chat_id, instruments, horizon_h, min_score, quiet_from, quiet_to,
                 updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (chat_id) DO UPDATE SET
                instruments = EXCLUDED.instruments,
                horizon_h   = EXCLUDED.horizon_h,
                min_score   = EXCLUDED.min_score,
                quiet_from  = EXCLUDED.quiet_from,
                quiet_to    = EXCLUDED.quiet_to,
                updated_at  = now();
            """,
            int(chat_id), [int(i) for i in instruments], int(horizon_h),
            float(min_score),
            None if quiet_from is None else int(quiet_from),
            None if quiet_to is None else int(quiet_to),
        )

    async def status_facts(self) -> dict[str, Any]:
        """Свежесть данных и счётчики сигналов для /status."""
        row = await self._pool.fetchrow(
            """
            SELECT
                (SELECT max(ts) FROM ohlcv)                               AS last_ohlcv_ts,
                (SELECT max(ts) FROM orderbook_snapshots)                 AS last_orderbook_ts,
                (SELECT max(ts) FROM signals)                             AS last_signal_ts,
                (SELECT count(*) FROM signals WHERE status = 'open')      AS open_count,
                (SELECT count(*) FROM signals WHERE status = 'closed')    AS closed_count;
            """
        )
        return dict(row) if row else {}

    # --- /last ---

    async def last_signals(
        self,
        notified_only: bool,
        limit: int,
        instruments: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Последние сигналы. notified_only → только реально отправленные (§5).

        ``instruments`` — отбор по выбранным пользователем токенам (§4 ТЗ 8.3);
        ``None`` означает «все», а не «ни одного»: у человека, не открывавшего
        настройки, выбраны все токены.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, ts, decision, probability, status,
                   pnl_pct, drawdown_pct, success,
                   calibrated_probability, is_repeat, instrument_id
            FROM signals
            WHERE (NOT $1 OR notified_at IS NOT NULL)
              AND ($3::int[] IS NULL OR instrument_id = ANY($3))
            ORDER BY ts DESC
            LIMIT $2;
            """,
            notified_only,
            limit,
            instruments,
        )
        return [dict(r) for r in rows]

    async def freshness_by_instrument(
        self, instruments: list[int] | None = None
    ) -> list[tuple[str, Any]]:
        """Возраст самой свежей минутной свечи по каждому токену (§4 ТЗ 8.3).

        Одна общая строка «данные свежие» на пять токенов бесполезна: сбор мог
        встать по одному из них, а по остальным идти — и общий показатель
        остался бы зелёным.
        """
        rows = await self._pool.fetch(
            """
            SELECT i.symbol AS symbol, max(o.ts) AS last_ts
              FROM instruments i
              LEFT JOIN ohlcv o
                     ON o.instrument_id = i.id AND o.timeframe = '1m'
             WHERE i.symbol NOT LIKE '%:%'
               AND ($1::int[] IS NULL OR i.id = ANY($1))
             GROUP BY i.symbol
             ORDER BY i.symbol;
            """,
            instruments,
        )
        return [(str(r["symbol"]), r["last_ts"]) for r in rows]

    # --- /signal <id> ---

    async def signal_card(self, signal_id: int) -> dict[str, Any] | None:
        """Полная карточка сигнала: поля, оценки 1ч/4ч и цена на момент сигнала."""
        row = await self._pool.fetchrow(
            """
            SELECT s.id, s.instrument_id, s.ts, s.decision, s.probability,
                   s.rationale, s.notified, s.notified_at, s.status,
                   s.agents_payload::text AS agents_payload,
                   s.calibrated_probability, s.calibration_id,
                   s.inputs_hash, s.is_repeat,
                   -- Этап 8.1: токенов пять, и карточка обязана называть тот,
                   -- по которому выдан сигнал.
                   i.symbol      AS symbol,
                   c.built_at    AS calibration_built_at,
                   c.sample_size AS calibration_sample_size
            FROM signals s
            JOIN instruments i ON i.id = s.instrument_id
            LEFT JOIN calibration_curves c ON c.id = s.calibration_id
            WHERE s.id = $1;
            """,
            signal_id,
        )
        if row is None:
            return None
        card = dict(row)

        evals = await self._pool.fetch(
            """
            SELECT horizon, horizon_h, price_at_close, pnl_pct, drawdown_pct, success
            FROM signal_evaluations WHERE signal_id = $1;
            """,
            signal_id,
        )
        by_horizon = {e["horizon"]: dict(e) for e in evals}
        # Этап 8.1: горизонтов четыре. Ключи 1h/4h сохранены (их читает прежняя
        # разметка карточки), 12h/24h добавлены рядом.
        card["evals_by_horizon"] = {
            int(e["horizon_h"]): dict(e) for e in evals if e["horizon_h"] is not None
        }
        card["eval_1h"] = by_horizon.get("1h")
        card["eval_4h"] = by_horizon.get("4h")

        # Замороженные цели сигнала (Этап 8.2 §6). Читается именно то, что было
        # сказано человеку В МОМЕНТ СИГНАЛА, а не сегодняшняя цель из
        # risk_targets: карточка обязана показывать сказанное, а не текущее.
        # Отсутствие таблицы — не повод ронять карточку: она существует с
        # миграции 014, а /signal работает и на томе, где её ещё не применили.
        try:
            targets = await self._pool.fetch(
                """
                SELECT horizon_h, direction, target_pct, target_price, hit_rate,
                       covers_fees, no_target_reason
                FROM signal_targets WHERE signal_id = $1;
                """,
                signal_id,
            )
            card["targets_by_horizon"] = {
                int(t["horizon_h"]): dict(t) for t in targets
            }
        except Exception:  # noqa: BLE001 — карточка важнее блока цели
            card["targets_by_horizon"] = {}

        # Цена на момент сигнала — close ближайшей 1m-свечи на/до ts.
        card["price_at_signal"] = await self._pool.fetchval(
            """
            SELECT close FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = '1m' AND ts <= $2
            ORDER BY ts DESC LIMIT 1;
            """,
            card["instrument_id"],
            card["ts"],
        )
        return card

    # --- /agents ---

    async def latest_agents(self, agents: list[str]) -> dict[str, dict[str, Any] | None]:
        """Последний вывод каждого агента (по всем инструментам берём самый свежий)."""
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (agent) agent, signal, confidence, ts
            FROM agent_outputs
            WHERE agent = ANY($1::text[])
            ORDER BY agent, ts DESC;
            """,
            agents,
        )
        by_agent = {r["agent"]: dict(r) for r in rows}
        return {name: by_agent.get(name) for name in agents}

    # --- /stats ---

    async def stats_versions(self, period_sec: int) -> list[int]:
        """Версии логики (logic_version), встречающиеся у закрытых сигналов периода.

        Нужно, чтобы /stats считал статистику по ОДНОЙ версии (§D.4): смешивать
        сигналы «до» и «после» правок Этапа 7.0 статистически некорректно.
        """
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT logic_version FROM signals
            WHERE status = 'closed'
              AND ($1 = 0 OR ts > now() - $1 * interval '1 second')
            ORDER BY logic_version;
            """,
            period_sec,
        )
        return [int(r["logic_version"]) for r in rows]

    async def logic_version_started_at(self, logic_version: int) -> Any:
        """Момент начала версии логики (§6 Этапа 8.1) или ``None``.

        §4 ТЗ 8.3: статистика показывается по ТЕКУЩЕЙ версии с указанием, с
        какой даты она действует. Без даты человек не может понять, почему
        выборка мала: «мало сигналов» и «версия работает вторые сутки» — разные
        вещи, требующие разных решений.
        """
        return await self._pool.fetchval(
            "SELECT started_at FROM logic_version_windows WHERE logic_version = $1;",
            int(logic_version),
        )

    async def stats_block(
        self,
        period_sec: int,
        independent: bool,
        logic_version: int | None,
        horizon_h: int = 4,
    ) -> dict[str, Any]:
        """Агрегаты по закрытым сигналам за период И одну версию логики.

        ``independent=True`` — по одному (самому раннему) сигналу на каждое
        4-часовое окно (честная выборка). Иначе — по всей массе подряд.
        ``logic_version=None`` → без фильтра версии (когда закрытых сигналов нет).

        ``horizon_h`` — горизонт оценки, выбранный пользователем (§4 ТЗ 8.3).
        Оценка ищется по числовой колонке ``horizon_h``, а не по текстовой
        подписи: подпись — для человека, ключ — для соединения (Этап 8.1).
        Возвращаются и счётчики попаданий отдельно по buy и sell: §4 требует
        показывать число наблюдений РЯДОМ с процентом, а знаменатель у долей
        свой и от размера выборки отличается.
        """
        if independent:
            query = f"""
                WITH windowed AS (
                    SELECT DISTINCT ON (win) s.decision,
                           e4.success AS success, e4.pnl_pct AS pnl,
                           e4.drawdown_pct AS dd, {_WINDOW_EXPR} AS win
                    FROM signals s
                    LEFT JOIN signal_evaluations e4
                           ON e4.signal_id = s.id AND e4.horizon_h = $3
                    WHERE s.status = 'closed'
                      AND ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
                      AND ($2::smallint IS NULL OR s.logic_version = $2)
                    ORDER BY win, s.ts ASC
                )
                SELECT
                    count(*) AS n,
                    count(*) FILTER (WHERE decision = 'buy')  AS buy,
                    count(*) FILTER (WHERE decision = 'sell') AS sell,
                    count(*) FILTER (WHERE decision = 'wait') AS wait,
                    avg(CASE WHEN decision = 'buy'  AND success IS NOT NULL
                             THEN success::int END) AS sr_buy,
                    avg(CASE WHEN decision = 'sell' AND success IS NOT NULL
                             THEN success::int END) AS sr_sell,
                    count(*) FILTER (WHERE decision = 'buy'
                                     AND success IS NOT NULL) AS n_buy,
                    count(*) FILTER (WHERE decision = 'sell'
                                     AND success IS NOT NULL) AS n_sell,
                    avg(pnl) AS avg_pnl,
                    avg(dd)  AS avg_dd
                FROM windowed;
            """
        else:
            query = """
                SELECT
                    count(*) AS n,
                    count(*) FILTER (WHERE s.decision = 'buy')  AS buy,
                    count(*) FILTER (WHERE s.decision = 'sell') AS sell,
                    count(*) FILTER (WHERE s.decision = 'wait') AS wait,
                    avg(CASE WHEN s.decision = 'buy'  AND e4.success IS NOT NULL
                             THEN e4.success::int END) AS sr_buy,
                    avg(CASE WHEN s.decision = 'sell' AND e4.success IS NOT NULL
                             THEN e4.success::int END) AS sr_sell,
                    count(*) FILTER (WHERE s.decision = 'buy'
                                     AND e4.success IS NOT NULL) AS n_buy,
                    count(*) FILTER (WHERE s.decision = 'sell'
                                     AND e4.success IS NOT NULL) AS n_sell,
                    avg(e4.pnl_pct)      AS avg_pnl,
                    avg(e4.drawdown_pct) AS avg_dd
                FROM signals s
                LEFT JOIN signal_evaluations e4
                       ON e4.signal_id = s.id AND e4.horizon_h = $3
                WHERE s.status = 'closed'
                  AND ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
                  AND ($2::smallint IS NULL OR s.logic_version = $2);
            """
        row = await self._pool.fetchrow(
            query, period_sec, logic_version, int(horizon_h)
        )
        return dict(row) if row else {}

    async def notify_filter_counts(
        self,
        period_sec: int,
        logic_version: int | None,
    ) -> dict[str, Any]:
        """Блок 5 /stats: сколько отправлено и поглощено за период (одна версия)."""
        row = await self._pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE notified_at IS NOT NULL) AS sent,
                count(*) FILTER (WHERE notified AND notified_at IS NULL) AS absorbed
            FROM signals s
            WHERE ($1 = 0 OR s.ts > now() - $1 * interval '1 second')
              AND ($2::smallint IS NULL OR s.logic_version = $2);
            """,
            period_sec,
            logic_version,
        )
        return dict(row) if row else {"sent": 0, "absorbed": 0}

    # --- /summary ---

    async def data_counts_24h(self) -> list[tuple[str, int]]:
        """Приток данных за 24 часа по таблицам (§5, свои запросы)."""
        result: list[tuple[str, int]] = []
        for label, table in DATA_STREAMS:
            # Имя таблицы — из фиксированного белого списка DATA_STREAMS, не из ввода.
            count = await self._pool.fetchval(
                f"SELECT count(*) FROM {table} WHERE ts > now() - interval '24 hours';"
            )
            result.append((label, int(count or 0)))
        return result

    async def signal_counts_24h(
        self,
        min_probability: float,
        primary_horizon: str,
    ) -> dict[str, Any]:
        """Сигналы за 24 часа: раскладка, отправлено/поглощено, кандидаты, закрыто."""
        grp = await self._pool.fetch(
            """
            SELECT decision, count(*) AS n FROM signals
            WHERE ts > now() - interval '24 hours'
            GROUP BY decision ORDER BY decision;
            """
        )
        by_decision = {r["decision"]: int(r["n"]) for r in grp}

        row = await self._pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE notified_at IS NOT NULL) AS sent,
                count(*) FILTER (WHERE notified AND notified_at IS NULL) AS absorbed,
                count(*) FILTER (
                    WHERE decision <> 'wait' AND probability >= $1
                ) AS candidates,
                -- Этап 7.3, Блок C: сколько решений принято на том же наборе
                -- мнений, что и предыдущее, и сколько разных наборов вообще было.
                count(*) FILTER (WHERE is_repeat)      AS repeats,
                count(DISTINCT inputs_hash)            AS unique_inputs
            FROM signals
            WHERE ts > now() - interval '24 hours';
            """,
            float(min_probability),
        )
        closed = await self._pool.fetchval(
            """
            SELECT count(*) FROM signal_evaluations
            WHERE horizon = $1 AND evaluated_at > now() - interval '24 hours';
            """,
            primary_horizon,
        )
        return {
            "by_decision": by_decision,
            "sent": int(row["sent"]) if row else 0,
            "absorbed": int(row["absorbed"]) if row else 0,
            "candidates": int(row["candidates"]) if row else 0,
            "repeats": int(row["repeats"]) if row else 0,
            "unique_inputs": int(row["unique_inputs"]) if row else 0,
            "closed": int(closed or 0),
        }

    async def db_size(self) -> str | None:
        """Человекочитаемый размер БД (pg_size_pretty)."""
        return await self._pool.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()));"
        )

    # --- /positions (Этап 9.1 §10) ---

    async def positions_open(self) -> list[dict[str, Any]]:
        """Открытые позиции с ТЕКУЩЕЙ ценой инструмента.

        Текущая цена — закрытие последней минутной свечи. Она подтягивается
        боковым запросом (LATERAL), а не отдельным вызовом на каждую позицию:
        позиций не больше пяти, но пять последовательных обращений к базе ради
        пяти чисел — это пять сетевых задержек там, где достаточно одной.

        Нереализованный итог считается ЗДЕСЬ ЖЕ и с вычетом издержек: без них
        человек видел бы «+0.15%» у позиции, которая на самом деле в минусе,
        потому что круговые издержки 0.22% ещё не заплачены только на бумаге.
        """
        rows = await self._pool.fetch(
            """
            SELECT p.id, p.symbol_id AS instrument_id, p.symbol, p.entry_price,
                   p.target_price, p.stop_price, p.target_pct, p.stop_pct,
                   p.cost_pct, p.opened_at, p.deadline_at, p.notional_usd,
                   p.signal_id, p.last_price,
                   CASE WHEN p.last_price IS NULL THEN NULL
                        ELSE (p.last_price / p.entry_price - 1) * 100 - p.cost_pct
                   END AS unrealized_pct
            FROM (
                SELECT pos.id, pos.instrument_id AS symbol_id, i.symbol,
                       pos.entry_price, pos.target_price, pos.stop_price,
                       pos.target_pct, pos.stop_pct, pos.cost_pct,
                       pos.opened_at, pos.deadline_at, pos.notional_usd,
                       pos.signal_id, last.close AS last_price
                FROM positions pos
                JOIN instruments i ON i.id = pos.instrument_id
                LEFT JOIN LATERAL (
                    SELECT o.close
                    FROM ohlcv o
                    WHERE o.instrument_id = pos.instrument_id
                      AND o.timeframe = '1m'
                    ORDER BY o.ts DESC
                    LIMIT 1
                ) last ON TRUE
                WHERE pos.status = 'open'
            ) p
            ORDER BY p.opened_at ASC;
            """
        )
        return [dict(r) for r in rows]

    async def positions_balance(self, capital_start: float) -> dict[str, Any]:
        """Пять величин счёта (Этап 9.1.1 §6.2).

        Запрос — ТОТ ЖЕ САМЫЙ, что у сервиса позиций
        (:data:`src.core.db.BALANCE_SQL`), а не его копия здесь. Копия
        разошлась бы при первой правке, и тогда бот и сообщения показывали бы
        два РАЗНЫХ баланса одного счёта — расхождение, которое нечем было бы
        объяснить владельцу.

        Бот остаётся ТОЛЬКО НА ЧТЕНИЕ: это SELECT, ничего не открывающий и не
        закрывающий.
        """
        row = await self._pool.fetchrow(BALANCE_SQL, float(capital_start))
        if row is None:
            return {}
        return {
            "capital_start": float(row["capital_start"]),
            "realized_pnl": float(row["realized_pnl"]),
            "in_positions": float(row["in_positions"]),
            "free": float(row["free"]),
            "open_count": int(row["open_count"]),
        }

    async def positions_summary(self, days: int = 7) -> dict[str, Any]:
        """Итог по закрытым позициям за окно: счёт, разбивка, средние.

        Число закрытий с ``outcome_certain = FALSE`` считается ОТДЕЛЬНО и
        показывается отдельной строкой: у таких позиций итог взят по пределу
        (пессимистично), потому что порядок событий внутри минуты неизвестен, —
        и знать их долю нужно, иначе средний итог читался бы как измеренный,
        а он частично оценочный.
        """
        row = await self._pool.fetchrow(
            """
            SELECT count(*) AS closed,
                   count(*) FILTER (WHERE outcome_certain = FALSE) AS uncertain,
                   avg(net_pnl_pct) AS avg_net_pnl_pct,
                   sum(net_pnl_usd) AS sum_net_pnl_usd,
                   avg(entry_slippage_pct) AS avg_slippage_pct
            FROM positions
            WHERE status = 'closed'
              AND closed_at >= now() - make_interval(days => $1::int);
            """,
            int(days),
        )
        reasons = await self._pool.fetch(
            """
            SELECT exit_reason, count(*) AS n
            FROM positions
            WHERE status = 'closed'
              AND closed_at >= now() - make_interval(days => $1::int)
            GROUP BY exit_reason
            ORDER BY n DESC;
            """,
            int(days),
        )
        summary = dict(row) if row is not None else {}
        summary["by_reason"] = [
            (str(r["exit_reason"]), int(r["n"])) for r in reasons
        ]
        return summary
