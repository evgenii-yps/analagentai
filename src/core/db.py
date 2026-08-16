"""Асинхронный слой доступа к PostgreSQL поверх пула asyncpg.

Все запросы параметризованы для защиты от SQL-инъекций.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from src.core.config import settings

if TYPE_CHECKING:
    # Импорт только для аннотаций — без циклической зависимости в рантайме.
    from src.agents.base import AgentOutput

# Потолок длины agent_failures.detail. Теперь туда пишется полная трассировка
# (Этап 7.2), а не короткое сообщение, поэтому держим больше и сохраняем ХВОСТ
# (нижние кадры трассировки = место падения). Столбец TEXT, жёсткого лимита нет.
_FAILURE_DETAIL_MAX = 4000


class DB:
    """Обёртка над ``asyncpg.Pool`` с методами доступа к данным."""

    def __init__(self) -> None:
        # Пул создаётся лениво в connect(); до этого он отсутствует.
        self._pool: asyncpg.Pool | None = None
        # Сериализует пересоздание пула (самовосстановление агента, Этап 7.2):
        # два агента в одном процессе не должны пересоздавать пул одновременно.
        self._reconnect_lock = asyncio.Lock()

    @property
    def pool(self) -> asyncpg.Pool:
        """Возвращает активный пул либо падает, если он не инициализирован."""
        if self._pool is None:
            raise RuntimeError("Пул не инициализирован: сначала вызовите connect().")
        return self._pool

    async def connect(self) -> None:
        """Создаёт пул соединений (min_size=2, max_size=10)."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=2,
            max_size=10,
        )

    async def close(self) -> None:
        """Закрывает пул соединений."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        """Проверяет доступность БД запросом ``SELECT 1``."""
        try:
            result = await self.pool.fetchval("SELECT 1;")
            return result == 1
        except Exception:
            return False

    async def reconnect(self) -> None:
        """Пересоздаёт пул соединений — «мягкий перезапуск» доступа к БД (Этап 7.2).

        Единственное долгоживущее состояние процесса агентов между итерациями —
        это пул asyncpg (и клиент Redis). Инцидент 14.08 (полная БД, но пустая
        выборка, лечится ТОЛЬКО перезапуском контейнера) указывает на испорченное
        состояние именно здесь. Метод даёт агенту способ восстановиться самому,
        не дожидаясь вотчдога: открываем новый пул ДО закрытия старого (без окна
        ``_pool is None``), атомарно подменяем ссылку, затем гасим старый пул.
        Сериализован ``_reconnect_lock``: параллельные агенты не пересоздают пул
        одновременно.
        """
        async with self._reconnect_lock:
            old = self._pool
            new = await asyncpg.create_pool(
                dsn=settings.pg_dsn,
                min_size=2,
                max_size=10,
            )
            self._pool = new
            if old is not None:
                try:
                    await old.close()
                except Exception:
                    # Старый пул мог быть уже нерабочим — это и есть причина
                    # пересоздания; ошибку закрытия глотаем, новый пул уже активен.
                    pass

    async def get_or_create_instrument(
        self,
        exchange: str,
        symbol: str,
        type: str = "spot",
        base: str | None = None,
        quote: str | None = None,
    ) -> int:
        """UPSERT инструмента в таблицу ``instruments`` и возврат его ``id``.

        Если ``base``/``quote`` не переданы, они выводятся из ``symbol``
        (поддерживаются разделители ``/`` и ``-``, напр. ``BTC/USDT``).
        """
        if base is None or quote is None:
            derived_base, derived_quote = _split_symbol(symbol)
            base = base or derived_base
            quote = quote or derived_quote

        # ON CONFLICT ... DO UPDATE нужен, чтобы RETURNING вернул id и при конфликте.
        query = """
            INSERT INTO instruments (exchange, symbol, base, quote, type)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (exchange, symbol, type)
            DO UPDATE SET base = EXCLUDED.base, quote = EXCLUDED.quote
            RETURNING id;
        """
        return await self.pool.fetchval(query, exchange, symbol, base, quote, type)

    async def upsert_ohlcv(
        self,
        instrument_id: int,
        timeframe: str,
        candles: list[list[float]],
    ) -> None:
        """Пакетный UPSERT свечей. ``candle = [ts_ms, open, high, low, close, volume]``.

        При конфликте по PK (instrument_id, timeframe, ts) текущая свеча
        обновляется — это делает повторный сбор идемпотентным.
        """
        if not candles:
            return
        query = """
            INSERT INTO ohlcv
                (instrument_id, timeframe, ts, open, high, low, close, volume)
            VALUES
                ($1, $2, to_timestamp($3::double precision / 1000.0), $4, $5, $6, $7, $8)
            ON CONFLICT (instrument_id, timeframe, ts)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume;
        """
        rows = [
            (
                instrument_id,
                timeframe,
                int(c[0]),
                float(c[1]),
                float(c[2]),
                float(c[3]),
                float(c[4]),
                float(c[5]),
            )
            for c in candles
        ]
        await self.pool.executemany(query, rows)

    async def insert_trades(
        self,
        instrument_id: int,
        trades: list[dict[str, Any]],
    ) -> None:
        """Пакетная вставка сделок с дедупликацией по (instrument_id, trade_id)."""
        if not trades:
            return
        # Убираем дубли внутри пакета; кросс-запусковую дедупликацию даёт ON CONFLICT.
        unique = dedupe_trades(trades)
        query = """
            INSERT INTO trades
                (instrument_id, trade_id, ts, price, amount, side)
            VALUES
                ($1, $2, to_timestamp($3::double precision / 1000.0), $4, $5, $6)
            ON CONFLICT (instrument_id, trade_id) DO NOTHING;
        """
        rows = [
            (
                instrument_id,
                str(t["id"]),
                int(t["timestamp"]),
                float(t["price"]),
                float(t["amount"]),
                t.get("side"),
            )
            for t in unique
            if t.get("id") is not None and t.get("timestamp") is not None
        ]
        await self.pool.executemany(query, rows)

    async def insert_orderbook(
        self,
        instrument_id: int,
        ob: dict[str, Any],
    ) -> None:
        """Вставка снимка стакана: bids/asks в JSONB, расчёт spread и объёмов."""
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        spread, bid_volume, ask_volume = compute_orderbook_metrics(bids, asks)
        ts = _ms_to_dt(ob.get("timestamp"))
        query = """
            INSERT INTO orderbook_snapshots
                (instrument_id, ts, bids, asks, spread, bid_volume, ask_volume)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7);
        """
        await self.pool.execute(
            query,
            instrument_id,
            ts,
            json.dumps(bids),
            json.dumps(asks),
            spread,
            bid_volume,
            ask_volume,
        )

    async def insert_funding(
        self,
        instrument_id: int,
        ts: datetime,
        rate: float,
    ) -> None:
        """UPSERT ставки финансирования по PK (instrument_id, ts)."""
        query = """
            INSERT INTO funding (instrument_id, ts, rate)
            VALUES ($1, $2, $3)
            ON CONFLICT (instrument_id, ts) DO UPDATE SET rate = EXCLUDED.rate;
        """
        await self.pool.execute(query, instrument_id, ts, float(rate))

    async def insert_open_interest(
        self,
        instrument_id: int,
        ts: datetime,
        value: float,
    ) -> None:
        """UPSERT открытого интереса по PK (instrument_id, ts)."""
        query = """
            INSERT INTO open_interest (instrument_id, ts, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (instrument_id, ts) DO UPDATE SET value = EXCLUDED.value;
        """
        await self.pool.execute(query, instrument_id, ts, float(value))

    # --- Чтение данных для агентов (Этап 3) ---

    async def get_ohlcv(
        self,
        instrument_id: int,
        timeframe: str,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` свечей по возрастанию ts (старые → новые)."""
        query = """
            SELECT ts, open, high, low, close, volume
            FROM (
                SELECT ts, open, high, low, close, volume
                FROM ohlcv
                WHERE instrument_id = $1 AND timeframe = $2
                ORDER BY ts DESC
                LIMIT $3
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, timeframe, limit)

    async def get_recent_orderbook(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` снимков стакана по возрастанию ts (старые → новые)."""
        query = """
            SELECT ts, bids, asks, spread, bid_volume, ask_volume
            FROM (
                SELECT ts, bids, asks, spread, bid_volume, ask_volume
                FROM orderbook_snapshots
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def get_recent_funding(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` значений funding по возрастанию ts."""
        query = """
            SELECT ts, rate
            FROM (
                SELECT ts, rate
                FROM funding
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def get_recent_open_interest(
        self,
        instrument_id: int,
        limit: int,
    ) -> list[asyncpg.Record]:
        """Последние ``limit`` значений open interest по возрастанию ts."""
        query = """
            SELECT ts, value
            FROM (
                SELECT ts, value
                FROM open_interest
                WHERE instrument_id = $1
                ORDER BY ts DESC
                LIMIT $2
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, limit)

    async def get_funding_window(
        self,
        instrument_id: int,
        hours: int,
    ) -> list[asyncpg.Record]:
        """Окно funding за последние ``hours`` часов, по одной точке на час.

        Прореживание (``DISTINCT ON`` по часу, берётся последнее значение часа)
        нужно потому, что коллектор может писать значение хоть раз в минуту: без
        него неделя дала бы более 10 000 строк на каждой итерации агента.
        Перцентиль при этом считается по «времени, проведённому ниже текущего
        уровня», что и требуется для оценки положения в распределении.
        """
        query = """
            SELECT ts, rate
            FROM (
                SELECT DISTINCT ON (date_trunc('hour', ts)) ts, rate
                FROM funding
                WHERE instrument_id = $1
                  AND ts >= now() - make_interval(hours => $2)
                ORDER BY date_trunc('hour', ts), ts DESC
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, int(hours))

    async def get_open_interest_window(
        self,
        instrument_id: int,
        hours: int,
    ) -> list[asyncpg.Record]:
        """Окно open interest за последние ``hours`` часов, по точке на час."""
        query = """
            SELECT ts, value
            FROM (
                SELECT DISTINCT ON (date_trunc('hour', ts)) ts, value
                FROM open_interest
                WHERE instrument_id = $1
                  AND ts >= now() - make_interval(hours => $2)
                ORDER BY date_trunc('hour', ts), ts DESC
            ) sub
            ORDER BY ts ASC;
        """
        return await self.pool.fetch(query, instrument_id, int(hours))

    async def ensure_agent_failure_schema(self) -> None:
        """Идемпотентно создаёт таблицу учёта сбоев агентов (Этап 7.0, Задача B).

        Сбой итерации агента раньше терялся молча (только warning в лог). Теперь
        каждый сбой — строка здесь, чтобы его можно было посчитать за период
        (суточная сводка) и отличить ошибку расчёта от ошибки записи в БД.
        """
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_failures (
                id         BIGSERIAL PRIMARY KEY,
                agent      TEXT NOT NULL,
                ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
                error_type TEXT NOT NULL,   -- 'compute' | 'db_write'
                exc_type   TEXT,            -- имя класса исключения
                detail     TEXT             -- усечённое сообщение (без секретов)
            );
            """
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_failures "
            "ON agent_failures (agent, ts DESC);"
        )

    async def record_agent_failure(
        self,
        agent: str,
        error_type: str,
        exc_type: str | None,
        detail: str,
    ) -> None:
        """INSERT записи о сбое агента.

        Для ошибок записи в БД (``error_type='db_write'``) сам этот INSERT может
        не пройти (БД недоступна) — вызывающий код обязан обернуть его в
        try/except и не падать. Такой сбой всё равно виден по устаревшему
        heartbeat и другим ошибкам БД.

        ``error_type`` — ``'compute'`` | ``'db_write'`` | ``'auto_reset'`` (Этап
        7.2: факт самовосстановления агента). ``exc_type`` может быть ``None``
        (например, для ``auto_reset`` — это не исключение, а служебное событие).

        ``detail`` — обычно ПОЛНАЯ ТРАССИРОВКА исключения (для compute/db_write),
        поэтому обрезаем не в 300 символов (это резало трассировку), а держим
        последние ``_FAILURE_DETAIL_MAX`` символов: у трассировки самое полезное —
        нижние кадры (место падения), они как раз в хвосте.
        """
        if len(detail) > _FAILURE_DETAIL_MAX:
            detail = detail[-_FAILURE_DETAIL_MAX:]
        await self.pool.execute(
            "INSERT INTO agent_failures (agent, error_type, exc_type, detail) "
            "VALUES ($1, $2, $3, $4);",
            agent,
            error_type,
            exc_type,
            detail,
        )

    async def save_agent_output(self, output: AgentOutput) -> None:
        """INSERT заключения агента в ``agent_outputs``."""
        query = """
            INSERT INTO agent_outputs
                (agent, instrument_id, signal, confidence, metrics, rationale)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6);
        """
        await self.pool.execute(
            query,
            output.agent,
            output.instrument_id,
            output.signal,
            float(output.confidence),
            json.dumps(output.metrics),
            output.rationale,
        )

    # --- Decision Agent (Этап 4) ---

    async def get_latest_agent_output(
        self,
        agent: str,
        instrument_id: int,
    ) -> dict[str, Any] | None:
        """Последний вывод агента по инструменту (ts DESC) или None."""
        query = """
            SELECT agent, instrument_id, ts, signal, confidence, metrics, rationale
            FROM agent_outputs
            WHERE agent = $1 AND instrument_id = $2
            ORDER BY ts DESC
            LIMIT 1;
        """
        row = await self.pool.fetchrow(query, agent, instrument_id)
        return dict(row) if row is not None else None

    async def ensure_signals_logic_version(self) -> None:
        """Идемпотентно добавляет колонку ``logic_version`` (Этап 7.0, Задача D).

        Граница режимов: сигналы «до» правок Этапа 7.0 остаются с версией 1
        (DEFAULT), новые Decision Agent пишет с версией из константы кода. Старые
        и новые сигналы статистически несравнимы — версия позволяет их разделять.
        """
        await self.pool.execute(
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS logic_version SMALLINT NOT NULL DEFAULT 1;"
        )

    async def ensure_signals_degraded(self) -> None:
        """Идемпотентно добавляет колонку ``degraded`` (Этап 7.2, Задача A2).

        ``degraded`` взводится, когда в решении участвовало меньше полного числа
        агентов (сейчас 3): сигнал построен на неполной картине. Миграция НЕ
        пересчитывает старые записи — у них остаётся DEFAULT false (граница режимов
        фиксируется через ``logic_version``, а не через этот флаг).
        """
        await self.pool.execute(
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS degraded BOOLEAN NOT NULL DEFAULT FALSE;"
        )

    async def ensure_calibration_schema(self) -> None:
        """Идемпотентно создаёт схему калибровки (Этап 7.3, Блок B).

        Колонка ``probability`` СОХРАНЯЕТСЯ и продолжает хранить индекс согласия:
        переименование колонки сломало бы выгрузку, бота и суточную сводку.
        Рядом появляются ``calibrated_probability`` (вероятность, выведенная из
        фактических исходов; NULL, пока кривой нет) и ссылка на кривую.
        Уникальный частичный индекс гарантирует не больше одной активной кривой
        на версию логики.
        """
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_curves (
                id              BIGSERIAL PRIMARY KEY,
                logic_version   SMALLINT    NOT NULL,
                built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                sample_size     INTEGER     NOT NULL,
                window_from     TIMESTAMPTZ NOT NULL,
                window_to       TIMESTAMPTZ NOT NULL,
                base_rate       DOUBLE PRECISION NOT NULL,
                bins            JSONB       NOT NULL,
                is_active       BOOLEAN     NOT NULL DEFAULT FALSE,
                notes           TEXT
            );
            """
        )
        await self.pool.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_active "
            "ON calibration_curves (logic_version) WHERE is_active;"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_calibration_built "
            "ON calibration_curves (logic_version, built_at DESC);"
        )
        await self.pool.execute(
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS calibrated_probability DOUBLE PRECISION;"
        )
        await self.pool.execute(
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS calibration_id BIGINT;"
        )
        # Роль только на чтение (сервис бота) должна видеть новую таблицу сразу.
        # Без этого бот, стартовавший РАНЬШЕ создания таблицы, получал бы отказ
        # в правах на /signal до следующего перезапуска: его собственный
        # GRANT ON ALL TABLES отработал, когда таблицы ещё не было.
        await self.pool.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
                    GRANT SELECT ON calibration_curves TO agenttrade_ro;
                END IF;
            END $$;
            """
        )
        # Внешний ключ добавляем отдельно: ADD CONSTRAINT не поддерживает
        # IF NOT EXISTS, поэтому проверяем наличие по каталогу (идемпотентность).
        await self.pool.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'signals_calibration_id_fkey'
                ) THEN
                    ALTER TABLE signals
                        ADD CONSTRAINT signals_calibration_id_fkey
                        FOREIGN KEY (calibration_id)
                        REFERENCES calibration_curves(id);
                END IF;
            END $$;
            """
        )

    async def ensure_signals_inertia(self) -> None:
        """Идемпотентно добавляет ``inputs_hash`` и ``is_repeat`` (Этап 7.3, Блок C).

        Частота решений не меняется: решение, принятое на том же наборе входных
        мнений, что и предыдущее, просто помечается повтором. Старые записи не
        пересчитываются — у них ``inputs_hash`` остаётся NULL.
        """
        await self.pool.execute(
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS inputs_hash TEXT;"
        )
        await self.pool.execute(
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS is_repeat BOOLEAN NOT NULL DEFAULT FALSE;"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_inputs_hash "
            "ON signals (instrument_id, inputs_hash, ts DESC);"
        )

    async def get_last_inputs_hash(self, instrument_id: int) -> str | None:
        """Хэш входов предыдущего по времени сигнала инструмента (или None)."""
        return await self.pool.fetchval(
            """
            SELECT inputs_hash FROM signals
            WHERE instrument_id = $1
            ORDER BY ts DESC
            LIMIT 1;
            """,
            instrument_id,
        )

    async def save_signal(
        self,
        instrument_id: int,
        decision: str,
        probability: float,
        agents_payload: list[dict[str, Any]],
        rationale: str,
        logic_version: int,
        degraded: bool = False,
        calibrated_probability: float | None = None,
        calibration_id: int | None = None,
        inputs_hash: str | None = None,
        is_repeat: bool = False,
    ) -> None:
        """INSERT итогового решения в ``signals`` (status остаётся 'open').

        ``logic_version`` — версия логики агрегации/агентов, фиксирует границу
        режимов Этапов 7.0/7.2/7.3.
        ``degraded`` (Этап 7.2) — участвовало меньше полного числа агентов;
        по такому сигналу уведомление НЕ отправляется, но сам сигнал сохраняется.
        ``probability`` (Этап 7.3) — ИНДЕКС СОГЛАСИЯ, формула не изменилась.
        ``calibrated_probability`` — вероятность по накопленным исходам; NULL,
        пока активной кривой нет. ``inputs_hash``/``is_repeat`` — учёт инерции
        входов (Блок C).
        """
        query = """
            INSERT INTO signals
                (instrument_id, decision, probability, agents_payload, rationale,
                 logic_version, degraded, calibrated_probability, calibration_id,
                 inputs_hash, is_repeat)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11);
        """
        await self.pool.execute(
            query,
            instrument_id,
            decision,
            float(probability),
            json.dumps(agents_payload),
            rationale,
            int(logic_version),
            bool(degraded),
            None if calibrated_probability is None else float(calibrated_probability),
            None if calibration_id is None else int(calibration_id),
            inputs_hash,
            bool(is_repeat),
        )

    # --- Калибровочные кривые (Этап 7.3, Блок B) ---

    async def get_active_calibration(
        self,
        logic_version: int,
    ) -> dict[str, Any] | None:
        """Активная кривая для версии логики (или None, если её нет)."""
        row = await self.pool.fetchrow(
            """
            SELECT id, logic_version, built_at, sample_size, window_from,
                   window_to, base_rate, bins, notes
            FROM calibration_curves
            WHERE logic_version = $1 AND is_active
            LIMIT 1;
            """,
            int(logic_version),
        )
        return dict(row) if row is not None else None

    async def get_independent_outcomes(
        self,
        logic_version: int,
        horizon: str,
    ) -> list[dict[str, Any]]:
        """Независимые наблюдения для калибровки: одно на 4-часовое окно.

        Берутся закрытые направленные сигналы указанной версии логики с
        ``degraded = false``, у которых есть оценка на нужном горизонте. Окна —
        непересекающиеся 4-часовые отрезки с границами 00/04/08/12/16/20 UTC
        (epoch кратен 14400), из окна берётся ПЕРВЫЙ по времени сигнал.
        Прореживание обязательно: решения выдаются раз в минуту, а горизонт —
        четыре часа, поэтому соседние сигналы описывают почти один и тот же
        отрезок рынка и независимыми наблюдениями не являются.
        """
        query = """
            SELECT DISTINCT ON (win) win, id, ts, probability, success
            FROM (
                SELECT s.id, s.ts, s.probability, e.success,
                       to_timestamp(floor(extract(epoch FROM s.ts) / 14400) * 14400)
                           AS win
                FROM signals s
                JOIN signal_evaluations e
                  ON e.signal_id = s.id AND e.horizon = $2
                WHERE s.logic_version = $1
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
                  AND s.probability IS NOT NULL
            ) q
            ORDER BY win, ts ASC;
        """
        rows = await self.pool.fetch(query, int(logic_version), horizon)
        return [dict(r) for r in rows]

    async def save_calibration_curve(
        self,
        logic_version: int,
        sample_size: int,
        window_from: datetime,
        window_to: datetime,
        base_rate: float,
        bins: list[dict[str, Any]],
        notes: str | None = None,
    ) -> int:
        """Сохраняет кривую и делает её активной в ОДНОЙ транзакции → id кривой.

        Прежняя активная кривая этой версии деактивируется в той же транзакции:
        частичный уникальный индекс не допускает двух активных одновременно,
        поэтому порядок «снять — поставить» обязателен.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE calibration_curves SET is_active = FALSE "
                "WHERE logic_version = $1 AND is_active;",
                int(logic_version),
            )
            curve_id = await conn.fetchval(
                """
                INSERT INTO calibration_curves
                    (logic_version, sample_size, window_from, window_to,
                     base_rate, bins, is_active, notes)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, TRUE, $7)
                RETURNING id;
                """,
                int(logic_version),
                int(sample_size),
                window_from,
                window_to,
                float(base_rate),
                json.dumps(bins),
                notes,
            )
        return int(curve_id)

    # --- Уведомления (Этап 5) ---

    async def ensure_notify_schema(self) -> None:
        """Идемпотентно добавляет колонки ``notified`` и ``notified_at``.

        ``notified`` (Этап 5) — служебный признак «сигнал обработан notify»
        (защита от повторов и поглощение дублей). ``notified_at`` (Этап 6.6) —
        отметка ФАКТИЧЕСКОЙ отправки в Telegram, нужна для честной статистики.
        """
        await self.pool.execute(
            "ALTER TABLE signals "
            "ADD COLUMN IF NOT EXISTS notified BOOLEAN NOT NULL DEFAULT FALSE;"
        )
        await self.pool.execute(
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;"
        )

    async def get_unnotified_strong_signals(
        self,
        min_probability: float,
        use_calibrated: bool = False,
        min_calibrated: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Неотправленные сильные сигналы (decision != wait, порог пройден), ts ASC.

        ``agents_payload`` включён для самодостаточного текста уведомления (ТЗ 6.7
        §6): по нему форматтер показывает мнения агентов и пересчитывает
        согласованность.

        Режим отбора (Этап 7.3, Блок B). По умолчанию (``use_calibrated=False``)
        условия ровно те же, что и были: индекс согласия ≥ NOTIFY_MIN_PROBABILITY.
        При ``use_calibrated=True`` отбор идёт по КАЛИБРОВАННОЙ вероятности; пока
        активной кривой нет, она у всех сигналов NULL — выборка пуста, и
        уведомления не уходят вовсе, как и требует ТЗ.
        """
        # Кривая присоединяется LEFT JOIN: в тексте уведомления вероятность
        # сопровождается датой кривой и размером её выборки, а при отсутствии
        # кривой обе колонки просто NULL и строка про вероятность не печатается.
        columns = """
            s.id, s.instrument_id, s.ts, s.decision, s.probability, s.rationale,
            s.agents_payload, s.calibrated_probability, s.calibration_id,
            s.is_repeat,
            c.built_at    AS calibration_built_at,
            c.sample_size AS calibration_sample_size
        """
        if use_calibrated:
            query = f"""
                SELECT {columns}
                FROM signals s
                LEFT JOIN calibration_curves c ON c.id = s.calibration_id
                WHERE s.notified = FALSE
                  AND s.decision <> 'wait'
                  AND s.calibrated_probability IS NOT NULL
                  AND s.calibrated_probability >= $1
                ORDER BY s.ts ASC;
            """
            rows = await self.pool.fetch(query, float(min_calibrated))
        else:
            query = f"""
                SELECT {columns}
                FROM signals s
                LEFT JOIN calibration_curves c ON c.id = s.calibration_id
                WHERE s.notified = FALSE
                  AND s.decision <> 'wait'
                  AND s.probability >= $1
                ORDER BY s.ts ASC;
            """
            rows = await self.pool.fetch(query, float(min_probability))
        return [dict(r) for r in rows]

    async def mark_signal_notified(self, signal_id: int) -> None:
        """Фиксирует ФАКТ отправки уведомления в Telegram.

        Вызывается ТОЛЬКО после успешного ответа Telegram API. Проставляет
        ``notified_at = now()`` (для статистики качества сигналов) и служебный
        признак ``notified`` (защита от повторов).
        """
        await self.pool.execute(
            "UPDATE signals SET notified = TRUE, notified_at = now() WHERE id = $1;",
            signal_id,
        )

    async def mark_signal_absorbed(self, signal_id: int) -> None:
        """Поглощает сигнал (дубль/в пределах cooldown), НЕ фиксируя отправку.

        Уведомление в Telegram по нему не ушло, поэтому ``notified_at`` остаётся
        пустым — иначе статистика «отправлено уведомлений» была бы завышена.
        """
        await self.pool.execute(
            "UPDATE signals SET notified = TRUE WHERE id = $1;", signal_id
        )

    # --- Оценка результатов (Этап 6) ---

    async def ensure_evaluator_schema(self) -> None:
        """Идемпотентно создаёт таблицу ``signal_evaluations`` (на старом томе)."""
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_evaluations (
                id              BIGSERIAL PRIMARY KEY,
                signal_id       BIGINT NOT NULL REFERENCES signals(id),
                horizon         TEXT NOT NULL,
                price_at_signal DOUBLE PRECISION NOT NULL,
                price_at_close  DOUBLE PRECISION NOT NULL,
                pnl_pct         DOUBLE PRECISION NOT NULL,
                drawdown_pct    DOUBLE PRECISION NOT NULL,
                success         BOOLEAN NOT NULL,
                evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (signal_id, horizon)
            );
            """
        )

    async def get_signals_to_evaluate(
        self,
        horizon: str,
        horizon_sec: float,
    ) -> list[dict[str, Any]]:
        """Направленные сигналы, у которых прошёл горизонт и нет оценки по нему."""
        query = """
            SELECT s.id, s.instrument_id, s.ts, s.decision
            FROM signals s
            WHERE s.decision <> 'wait'
              AND (now() - s.ts) >= make_interval(secs => $2)
              AND NOT EXISTS (
                  SELECT 1 FROM signal_evaluations e
                  WHERE e.signal_id = s.id AND e.horizon = $1
              )
            ORDER BY s.ts ASC;
        """
        rows = await self.pool.fetch(query, horizon, float(horizon_sec))
        return [dict(r) for r in rows]

    async def get_ohlcv_window(
        self,
        instrument_id: int,
        start_ts: datetime,
        end_ts: datetime,
        timeframe: str = "1m",
    ) -> list[dict[str, Any]]:
        """Свечи окна (start_ts, end_ts] по возрастанию ts."""
        query = """
            SELECT ts, open, high, low, close, volume
            FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = $2
              AND ts > $3 AND ts <= $4
            ORDER BY ts ASC;
        """
        rows = await self.pool.fetch(query, instrument_id, timeframe, start_ts, end_ts)
        return [dict(r) for r in rows]

    async def get_price_at(
        self,
        instrument_id: int,
        ts: datetime,
        timeframe: str = "1m",
    ) -> float | None:
        """Close ближайшей свечи на/до ``ts`` (цена на момент сигнала) или None."""
        query = """
            SELECT close
            FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = $2 AND ts <= $3
            ORDER BY ts DESC
            LIMIT 1;
        """
        return await self.pool.fetchval(query, instrument_id, timeframe, ts)

    async def save_evaluation(
        self,
        signal_id: int,
        horizon: str,
        price_at_signal: float,
        price_at_close: float,
        pnl_pct: float,
        drawdown_pct: float,
        success: bool,
    ) -> None:
        """INSERT оценки (идемпотентно по UNIQUE (signal_id, horizon))."""
        query = """
            INSERT INTO signal_evaluations
                (signal_id, horizon, price_at_signal, price_at_close,
                 pnl_pct, drawdown_pct, success)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (signal_id, horizon) DO NOTHING;
        """
        await self.pool.execute(
            query,
            signal_id,
            horizon,
            float(price_at_signal),
            float(price_at_close),
            float(pnl_pct),
            float(drawdown_pct),
            success,
        )

    async def finalize_signal(
        self,
        signal_id: int,
        pnl_pct: float,
        drawdown_pct: float,
        success: bool,
    ) -> None:
        """Записывает сводку по главному горизонту в signals и закрывает сигнал."""
        query = """
            UPDATE signals
            SET pnl_pct = $2, drawdown_pct = $3, success = $4, status = 'closed'
            WHERE id = $1;
        """
        await self.pool.execute(
            query, signal_id, float(pnl_pct), float(drawdown_pct), success
        )

    async def get_success_stats(self) -> list[dict[str, Any]]:
        """Статистика по decision×horizon: доля success и средний pnl_pct."""
        query = """
            SELECT s.decision,
                   e.horizon,
                   count(*) AS n,
                   avg(CASE WHEN e.success THEN 1.0 ELSE 0.0 END) AS success_rate,
                   avg(e.pnl_pct) AS avg_pnl_pct
            FROM signal_evaluations e
            JOIN signals s ON s.id = e.signal_id
            GROUP BY s.decision, e.horizon
            ORDER BY s.decision, e.horizon;
        """
        rows = await self.pool.fetch(query)
        return [dict(r) for r in rows]


def _ms_to_dt(ms: int | None) -> datetime:
    """Преобразует Unix-время в мс (UTC-aware datetime). None → текущее время."""
    if ms is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def compute_orderbook_metrics(
    bids: list[list[float]],
    asks: list[list[float]],
) -> tuple[float | None, float, float]:
    """Считает spread (best_ask - best_bid) и суммарные объёмы по сторонам.

    ``bids``/``asks`` — списки пар ``[price, amount]``. Если одна из сторон
    пуста, spread не определён (None).
    """
    bid_volume = sum(float(level[1]) for level in bids)
    ask_volume = sum(float(level[1]) for level in asks)
    spread: float | None = None
    if bids and asks:
        spread = float(asks[0][0]) - float(bids[0][0])
    return spread, bid_volume, ask_volume


def dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Удаляет дубли сделок по ``id`` внутри пакета, сохраняя порядок."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in trades:
        tid = t.get("id")
        if tid is None:
            continue
        key = str(tid)
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    return unique


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Разбивает символ инструмента на базовый и котируемый активы."""
    for sep in ("/", "-"):
        if sep in symbol:
            base, quote = symbol.split(sep, 1)
            # Для деривативов символ вида BTC/USDT:USDT — отбрасываем settle-суффикс.
            quote = quote.split(":", 1)[0]
            return base, quote
    # Разделитель не найден — считаем весь символ базовым активом.
    return symbol, ""


# Глобальный синглтон слоя доступа к БД.
db = DB()
