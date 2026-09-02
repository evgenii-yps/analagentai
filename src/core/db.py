"""Асинхронный слой доступа к PostgreSQL поверх пула asyncpg.

Все запросы параметризованы для защиты от SQL-инъекций.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import asyncpg

from src.core.config import settings
from src.core.instruments import horizon_label
from src.core.user_settings import USER_SETTINGS_DDL

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
        targets: list[dict[str, Any]] | None = None,
    ) -> int:
        """INSERT итогового решения в ``signals`` (status остаётся 'open').

        Возвращает идентификатор записанного сигнала.

        ``logic_version`` — версия логики агрегации/агентов, фиксирует границу
        режимов Этапов 7.0/7.2/7.3.
        ``degraded`` (Этап 7.2) — участвовало меньше полного числа агентов;
        по такому сигналу уведомление НЕ отправляется, но сам сигнал сохраняется.
        ``probability`` (Этап 7.3) — ИНДЕКС СОГЛАСИЯ, формула не изменилась.
        ``calibrated_probability`` — вероятность по накопленным исходам; NULL,
        пока активной кривой нет. ``inputs_hash``/``is_repeat`` — учёт инерции
        входов (Блок C).

        ``targets`` (Этап 8.2 §6) — замороженные цели по горизонтам. Пишутся В
        ТОЙ ЖЕ ТРАНЗАКЦИИ, что и сам сигнал: иначе возможна пара «сигнал есть,
        цели нет» или наоборот, и постфактум нельзя восстановить, что именно
        было сказано человеку. Все значения целей вычисляются ДО транзакции,
        внутри неё только вставки — чтобы расчёт целей не мог удержать
        транзакцию сигнала открытой.
        """
        query = """
            INSERT INTO signals
                (instrument_id, decision, probability, agents_payload, rationale,
                 logic_version, degraded, calibrated_probability, calibration_id,
                 inputs_hash, is_repeat)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id;
        """
        args = (
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
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                signal_id = int(await conn.fetchval(query, *args))
                for target in targets or ():
                    await conn.execute(SIGNAL_TARGET_INSERT, *_signal_target_args(
                        signal_id, target
                    ))
        return signal_id

    # --- Цели по вероятности (Этап 8.2) ---

    async def ensure_risk_targets_schema(self) -> None:
        """Идемпотентно создаёт таблицы целей (миграция 014).

        Сервисы проекта гарантируют свою схему при старте: миграция могла быть
        не применена на уже работающем томе, а падать из-за отсутствия
        УКРАШЕНИЯ сигнал не должен (§6 ТЗ 8.2 — сигнал важнее цели).
        """
        await self.pool.execute(RISK_TARGETS_DDL)
        await self.pool.execute(SIGNAL_TARGETS_DDL)
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_risk_targets_latest "
            "ON risk_targets (instrument_id, horizon_h, direction, computed_at DESC);"
        )

    async def save_risk_target(self, row: dict[str, Any]) -> None:
        """INSERT строки risk_targets. Существующие строки НЕ обновляются.

        Ежесуточный пересчёт пишет НОВУЮ строку с новым ``computed_at``: старые
        остаются историей изменения целей. ``ON CONFLICT DO NOTHING`` защищает
        только от повторного запуска в ту же микросекунду.
        """
        await self.pool.execute(
            """
            INSERT INTO risk_targets
                (instrument_id, horizon_h, direction, computed_at, window_days,
                 data_from, data_to, n_observations, target_pct, hit_rate,
                 mfe_p25, mfe_p50, mfe_p75, cost_roundtrip_pct, covers_fees,
                 no_target_reason, source, targets_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18)
            ON CONFLICT (instrument_id, horizon_h, direction, computed_at)
            DO NOTHING;
            """,
            int(row["instrument_id"]),
            int(row["horizon_h"]),
            str(row["direction"]),
            row["computed_at"],
            int(row["window_days"]),
            row["data_from"],
            row["data_to"],
            int(row["n_observations"]),
            _num(row.get("target_pct")),
            _num(row.get("hit_rate")),
            _num(row.get("mfe_p25")),
            _num(row.get("mfe_p50")),
            _num(row.get("mfe_p75")),
            _num(row["cost_roundtrip_pct"]),
            bool(row.get("covers_fees", False)),
            row.get("no_target_reason"),
            str(row["source"]),
            int(row["targets_version"]),
        )

    async def get_latest_risk_target(
        self, instrument_id: int, horizon_h: int, direction: str
    ) -> dict[str, Any] | None:
        """Самая свежая строка risk_targets по (инструмент, горизонт, направление)."""
        row = await self.pool.fetchrow(
            """
            SELECT instrument_id, horizon_h, direction, computed_at, window_days,
                   n_observations, target_pct, hit_rate, cost_roundtrip_pct,
                   covers_fees, no_target_reason, targets_version
            FROM risk_targets
            WHERE instrument_id = $1 AND horizon_h = $2 AND direction = $3
            ORDER BY computed_at DESC
            LIMIT 1;
            """,
            int(instrument_id), int(horizon_h), str(direction),
        )
        return dict(row) if row is not None else None

    async def get_signal_target(
        self, signal_id: int, horizon_h: int
    ) -> dict[str, Any] | None:
        """Замороженная цель сигнала по горизонту (или None, если её не писали)."""
        row = await self.pool.fetchrow(
            """
            SELECT signal_id, horizon_h, direction, price_at_signal, target_pct,
                   target_price, hit_rate, covers_fees, no_target_reason,
                   risk_target_computed_at, targets_version, frozen_at
            FROM signal_targets
            WHERE signal_id = $1 AND horizon_h = $2;
            """,
            int(signal_id), int(horizon_h),
        )
        return dict(row) if row is not None else None

    async def get_backtest_candles(
        self, inst_id: str, bar: str, since: datetime
    ) -> list[dict[str, Any]]:
        """Часовые свечи спота из ``backtest.candles`` по возрастанию времени.

        Читается ИМЕННО эта таблица, а не ``public.ohlcv``: в ней лежит история
        нужной глубины (90 суток), и она хранит внутрисвечные максимум и
        минимум, без которых цель по касанию посчитать нельзя.
        """
        rows = await self.pool.fetch(
            """
            SELECT open_time, open, high, low, close
            FROM backtest.candles
            WHERE inst_id = $1 AND bar = $2 AND open_time >= $3
            ORDER BY open_time ASC;
            """,
            inst_id, bar, since,
        )
        return [dict(r) for r in rows]

    # --- Исход по границам (Этап 8.8) ---

    async def ensure_barrier_schema(self) -> None:
        """Идемпотентно создаёт таблицу исходов по границам (миграция 015).

        Сервисы проекта гарантируют свою схему при старте: миграция могла быть
        не применена на уже работающем томе. Расчёт исходов при этом НЕ
        является горячим путём — он не влияет ни на одно решение системы.
        """
        await self.pool.execute(BARRIER_OUTCOMES_DDL)
        await self.pool.execute(BARRIER_OUTCOMES_CHECKS)
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_barrier_horizon_outcome "
            "ON signal_outcomes_barrier (logic_version, horizon_h, outcome);"
        )

    async def get_barrier_candidates(
        self,
        *,
        logic_version: int,
        horizon_h: int,
        now: datetime,
        settle_seconds: int = 0,
        recompute: bool = False,
    ) -> list[dict[str, Any]]:
        """Сигналы, готовые к оценке по границам на этом горизонте.

        Отбор ЖЁСТКИЙ и полностью выражен в запросе, а не в коде поверх него:

        * направленные сигналы (``decision <> 'wait'``) заданной версии логики —
          версии не смешиваются (правило проекта);
        * ``t + h`` уже в прошлом (§7), И СВЕРХ ТОГО прошёл ``settle_seconds``:
          у сигнала, чей горизонт не наступил, исхода ещё не существует, а у
          сигнала, чей горизонт наступил только что, ПОСЛЕДНИЙ БАР ОКНА ЕЩЁ НЕ
          ЗАКРЫТ. Второе условие добавлено Этапом 8.10.1 после разбора двух
          расхождений на сервере: окно ``t+1 … t+h`` кончается баром, который
          ОТКРЫВАЕТСЯ в момент срока, а закрывается через целый бар после него
          (минуту или час). Расчёт, допущенный к паре в момент срока, читал
          формирующийся бар: его ``close`` — цена «пока что», коллектор
          перезапишет её следующим опросом (UPSERT с DO UPDATE), и записанный
          исход ``timeout`` окажется посчитанным по цене, которой на срок не
          было. Это не ускорение и не задержка ради удобства: это разница между
          измерением и черновиком;
        * есть ЗАМОРОЖЕННАЯ цель ``signal_targets.target_pct`` на ЭТОТ горизонт.
          Сигналы без неё пропускаются, и подставлять им сегодняшнюю цель из
          ``risk_targets`` запрещено (§7 ТЗ): это подделка истории.

        ``recompute=False`` (по умолчанию) отдаёт только НЕПОСЧИТАННЫЕ пары.
        Так суточный запуск идемпотентен по построению и, главное, не понижает
        уже снятое разрешение: минутные свечи удаляются политикой хранения
        через ``RETENTION_1M_DAYS`` суток, и пересчёт старого сигнала выдал бы
        ``resolution='1h'`` там, где однажды было измерено ``'1m'``.
        """
        query = """
            SELECT s.id, s.instrument_id, s.ts, s.decision, s.logic_version,
                   t.direction, t.price_at_signal, t.target_pct
            FROM signals s
            JOIN signal_targets t
              ON t.signal_id = s.id AND t.horizon_h = $2
            WHERE s.decision <> 'wait'
              AND s.logic_version = $1
              AND t.target_pct IS NOT NULL
              AND s.ts + make_interval(hours => $2)
                       + make_interval(secs => $5) <= $3
              AND ($4::boolean OR NOT EXISTS (
                      SELECT 1 FROM signal_outcomes_barrier b
                      WHERE b.signal_id = s.id AND b.horizon_h = $2
                  ))
            ORDER BY s.ts ASC, s.id ASC;
        """
        rows = await self.pool.fetch(
            query, int(logic_version), int(horizon_h), now, bool(recompute),
            float(settle_seconds),
        )
        return [dict(r) for r in rows]

    async def count_barrier_skipped(
        self, *, logic_version: int, horizon_h: int, now: datetime,
        settle_seconds: int = 0,
    ) -> int:
        """Сигналы, пропущенные из-за ОТСУТСТВИЯ замороженной цели (§7).

        Число выводится отдельно и в журнал, и в отчёт: «цели не было» и
        «исход не посчитан» — разные состояния, и молчание сделало бы их
        неотличимыми.

        Годность здесь определяется ТЕМ ЖЕ условием, что в
        ``get_barrier_candidates``, включая запас на закрытие последнего бара.
        Разные определения годности в двух запросах дали бы счётчик, который
        считает не то, что считает расчёт, — и он врал бы ровно в те дни, когда
        на границе окна что-то происходит.
        """
        value = await self.pool.fetchval(
            """
            SELECT count(*)
            FROM signals s
            LEFT JOIN signal_targets t
              ON t.signal_id = s.id AND t.horizon_h = $2
            WHERE s.decision <> 'wait'
              AND s.logic_version = $1
              AND s.ts + make_interval(hours => $2)
                       + make_interval(secs => $4) <= $3
              AND (t.signal_id IS NULL OR t.target_pct IS NULL);
            """,
            int(logic_version), int(horizon_h), now, float(settle_seconds),
        )
        return int(value or 0)

    async def get_ohlcv_bars(
        self,
        instrument_id: int,
        timeframe: str,
        ts_from: datetime,
        ts_to: datetime,
    ) -> list[dict[str, Any]]:
        """Свечи по ВКЛЮЧИТЕЛЬНЫМ границам времени открытия, по возрастанию.

        Отдельный метод рядом с ``get_ohlcv_window`` заведён намеренно: там
        границы полуоткрытые ``(start, end]`` и это часть поведения оценщика,
        а окно §3 задано включительно по обоим концам. Переиспользовать чужие
        границы, «поправив» аргумент на один бар, значит спрятать правило окна
        в арифметике вызова.
        """
        rows = await self.pool.fetch(
            """
            SELECT ts, open, high, low, close
            FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = $2
              AND ts >= $3 AND ts <= $4
            ORDER BY ts ASC;
            """,
            int(instrument_id), str(timeframe), ts_from, ts_to,
        )
        return [dict(r) for r in rows]

    async def save_barrier_outcome(self, row: dict[str, Any]) -> None:
        """Запись исхода по границам. Существующая строка НЕ переписывается.

        ``ON CONFLICT DO NOTHING`` по первичному ключу (signal_id, horizon_h) —
        это и есть идемпотентность §7: повторный запуск на тех же данных не
        меняет ни одной строки, а значит, снимок таблицы совпадает побайтно.
        Принудительный пересчёт идёт отдельным путём (``--recompute``), который
        сначала удаляет строку: переписывание «на всякий случай» скрыло бы
        расхождение расчётов вместо того, чтобы его показать.
        """
        await self.pool.execute(
            """
            INSERT INTO signal_outcomes_barrier
                (signal_id, horizon_h, logic_version, direction, price_at_signal,
                 target_pct, stop_pct, cost_pct, outcome, hit_at, bars_to_hit,
                 net_pnl_pct, mae_pct, mfe_pct, resolution, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16)
            ON CONFLICT (signal_id, horizon_h) DO NOTHING;
            """,
            int(row["signal_id"]),
            int(row["horizon_h"]),
            int(row["logic_version"]),
            str(row["direction"]),
            _num(row["price_at_signal"]),
            _num(row["target_pct"]),
            _num(row["stop_pct"]),
            _num(row["cost_pct"]),
            str(row["outcome"]),
            row.get("hit_at"),
            None if row.get("bars_to_hit") is None else int(row["bars_to_hit"]),
            _num(row.get("net_pnl_pct")),
            _num(row["mae_pct"]),
            _num(row["mfe_pct"]),
            str(row["resolution"]),
            row["computed_at"],
        )

    async def delete_barrier_outcomes(
        self, *, logic_version: int, horizon_h: int
    ) -> int:
        """Удаляет посчитанные исходы одного горизонта (только ``--recompute``).

        Возвращает число удалённых строк — оно идёт в журнал: пересчёт, стёрший
        больше, чем ожидалось, обязан быть виден.
        """
        status = await self.pool.execute(
            "DELETE FROM signal_outcomes_barrier "
            "WHERE logic_version = $1 AND horizon_h = $2;",
            int(logic_version), int(horizon_h),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    # --- Базовые стратегии (Этап 8.9) ---

    async def ensure_strategy_schema(self) -> None:
        """Идемпотентно создаёт таблицу базовых стратегий (миграция 016)."""
        await self.pool.execute(STRATEGY_OUTCOMES_DDL)
        await self.pool.execute(STRATEGY_OUTCOMES_CHECKS)
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_strategy_outcomes_signal "
            "ON strategy_outcomes (signal_id, horizon_h, strategy);"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_strategy_outcomes_strategy "
            "ON strategy_outcomes (strategy, horizon_h, outcome);"
        )

    async def get_strategy_anchors(
        self,
        *,
        logic_version: int,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Моменты входа для стратегий §4 — ИЗ ``signal_outcomes_barrier``.

        Источник выбран не произвольно: базовые стратегии обязаны считаться на
        ТЕХ ЖЕ моментах и ТЕХ ЖЕ ценах входа, что и решения системы. Взяв их из
        таблицы исходов системы, мы получаем это по построению, а не по
        совпадению — и заодно исключаем пары, которые система сама не считала.

        Возвращается ОДНА строка на пару (сигнал, горизонт): все четыре
        стратегии §4 считаются на одном и том же окне свечей, и читать его
        четырежды незачем.
        """
        query = """
            SELECT b.signal_id, b.horizon_h, b.logic_version, b.direction,
                   b.price_at_signal, b.target_pct, s.instrument_id, s.ts
            FROM signal_outcomes_barrier b
            JOIN signals s ON s.id = b.signal_id
            WHERE b.logic_version = $1
              AND ($2::timestamptz IS NULL OR s.ts >= $2)
            ORDER BY s.ts ASC, b.signal_id ASC, b.horizon_h ASC
        """
        args: list[Any] = [int(logic_version), since]
        if limit is not None:
            query += " LIMIT $3"
            args.append(int(limit))
        rows = await self.pool.fetch(query + ";", *args)
        return [dict(r) for r in rows]

    async def get_risk_target_asof(
        self,
        instrument_id: int,
        horizon_h: int,
        direction: str,
        as_of: datetime,
    ) -> dict[str, Any] | None:
        """Историческая цель на дату входа: последняя строка НЕ ПОЗЖЕ ``as_of``.

        Это единственный законный источник цели для ВСТРЕЧНОГО направления:
        замороженной цели для него не существует — замораживалось только
        направление выданного сигнала. Условие ``computed_at <= as_of``
        обязательно: взять сегодняшнюю цель для вчерашнего входа значило бы
        утверждать, что в тот момент система знала то, чего не знала.
        """
        row = await self.pool.fetchrow(
            """
            SELECT instrument_id, horizon_h, direction, computed_at, target_pct
            FROM risk_targets
            WHERE instrument_id = $1 AND horizon_h = $2 AND direction = $3
              AND computed_at <= $4 AND target_pct IS NOT NULL
            ORDER BY computed_at DESC
            LIMIT 1;
            """,
            int(instrument_id), int(horizon_h), str(direction), as_of,
        )
        return dict(row) if row is not None else None

    async def get_grid_prices(
        self,
        instrument_id: int,
        timeframe: str,
        since: datetime,
        until: datetime,
    ) -> list[dict[str, Any]]:
        """Цены входа сетки §5: закрытия свечей ровно в 00 минут каждого часа.

        Отбор минуты выполняет БАЗА, а не код: тянуть все минутные свечи окна,
        чтобы оставить каждую шестидесятую, значило бы прочитать в шестьдесят
        раз больше строк ради того же ответа.
        """
        rows = await self.pool.fetch(
            """
            SELECT ts, close
            FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = $2
              AND ts >= $3 AND ts <= $4
              AND EXTRACT(minute FROM ts) = 0
              AND EXTRACT(second FROM ts) = 0
            ORDER BY ts ASC;
            """,
            int(instrument_id), str(timeframe), since, until,
        )
        return [dict(r) for r in rows]

    async def get_barrier_window(
        self, *, logic_version: int
    ) -> dict[str, Any] | None:
        """Границы окна наблюдения системы: первый и последний момент сигнала.

        Нужны сетке §5. Сетка обязана лежать РОВНО в том же отрезке рынка, на
        котором считалась система: фон, снятый на другом отрезке, отвечал бы на
        вопрос о другом рынке, и разница между ним и системой отражала бы смену
        отрезка, а не разницу правил.
        """
        row = await self.pool.fetchrow(
            """
            SELECT min(s.ts) AS ts_from, max(s.ts) AS ts_to, count(*) AS rows
            FROM signal_outcomes_barrier b
            JOIN signals s ON s.id = b.signal_id
            WHERE b.logic_version = $1;
            """,
            int(logic_version),
        )
        return dict(row) if row is not None else None

    async def save_strategy_outcome(self, row: dict[str, Any]) -> None:
        """Запись исхода стратегии. Существующая строка НЕ переписывается.

        ``ON CONFLICT DO NOTHING`` по первичному ключу — это и есть
        идемпотентность §7: повторный запуск на тех же данных не меняет ни
        одной строки, и снимок таблицы совпадает побайтно.
        """
        await self.pool.execute(
            """
            INSERT INTO strategy_outcomes
                (strategy, instrument_id, entry_ts, horizon_h, signal_id,
                 logic_version, direction, price_at_entry, target_pct,
                 target_source, stop_pct, cost_pct, outcome, hit_at,
                 net_pnl_pct, mae_pct, mfe_pct, resolution, seed, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20)
            ON CONFLICT (strategy, instrument_id, entry_ts, horizon_h)
            DO NOTHING;
            """,
            str(row["strategy"]),
            int(row["instrument_id"]),
            row["entry_ts"],
            int(row["horizon_h"]),
            None if row.get("signal_id") is None else int(row["signal_id"]),
            int(row["logic_version"]),
            str(row["direction"]),
            _num(row["price_at_entry"]),
            _num(row["target_pct"]),
            str(row["target_source"]),
            _num(row["stop_pct"]),
            _num(row["cost_pct"]),
            str(row["outcome"]),
            row.get("hit_at"),
            _num(row.get("net_pnl_pct")),
            _num(row["mae_pct"]),
            _num(row["mfe_pct"]),
            str(row["resolution"]),
            None if row.get("seed") is None else int(row["seed"]),
            row["computed_at"],
        )

    async def delete_strategy_outcomes(
        self, *, strategy: str, logic_version: int
    ) -> int:
        """Удаляет посчитанные строки одной стратегии (только ``--recompute``)."""
        status = await self.pool.execute(
            "DELETE FROM strategy_outcomes "
            "WHERE strategy = $1 AND logic_version = $2;",
            str(strategy), int(logic_version),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    # --- Починка строк, посчитанных по незакрытому бару (Этап 9.1, Задача Б) ---
    #
    # ПРИЗНАК ИСПОРЧЕННОЙ СТРОКИ ОДИН: расчёт произошёл РАНЬШЕ, чем закрылся
    # последний бар окна. До Этапа 9.1 сеточные стратегии считали годность
    # входа условием «срок наступил», без ожидания закрытия последнего бара
    # (см. src/baseline/runner.py). Такая строка получила итог из ``close``
    # ещё формировавшейся свечи — цены «пока что», которую коллектор
    # перезаписал следующим опросом.
    #
    # ПЕРЕПИСАТЬ ЕЁ НЕЛЬЗЯ НИКАКИМ ПОСЛЕДУЮЩИМ ПРОГОНОМ: запись идёт через
    # ON CONFLICT DO NOTHING, а ключи уже посчитанных строк отсеиваются до
    # чтения окна (get_strategy_pairs_done). Поэтому единственный способ
    # пересчитать — сначала удалить.

    # ЗАПАС БЕРЁТСЯ ПО ФАКТИЧЕСКОМУ РАЗРЕШЕНИЮ СТРОКИ (Этап 9.1.1 §2).
    #
    # ПОЧЕМУ НЕ ОДНО ЧИСЛО НА ВСЕХ, как было раньше. Запас в 3900 секунд (час
    # грубого бара плюс BARRIER_SETTLE_MINUTES) верен ПРИ ОТБОРЕ кандидатов:
    # там разрешение ещё неизвестно, оно выяснится по факту покрытия окна
    # минутным рядом, и ждать приходится по худшему случаю. Но у УЖЕ
    # ПОСЧИТАННОЙ строки разрешение известно и записано в колонке resolution
    # (ограничение strategy_outcomes_resolution_chk, миграция 016). Все
    # 449 764 строки боевой базы посчитаны по МИНУТНОМУ ряду, где последний бар
    # окна закрывается через 60 секунд после срока, — и проверка их запасом в
    # 3900 секунд объявила подозрительными 7618 ИСПРАВНЫХ строк при 0
    # настоящих (замер 30.08.2026).
    #
    # ВЕТКА ELSE — НЕ ФОРМАЛЬНОСТЬ. Появись в проекте третье разрешение, его
    # строки будут проверяться ХУДШИМ случаем ($1, то есть settle_seconds()),
    # а не проскочат молча: неизвестное разрешение обязано вызывать подозрение,
    # а не доверие.
    STRATEGY_UNSETTLED_PREDICATE = (
        "computed_at < entry_ts "
        "+ make_interval(hours => horizon_h::int) "
        "+ make_interval(secs => CASE resolution "
        "                          WHEN '1m' THEN 60 "
        "                          WHEN '1h' THEN 3600 "
        "                          ELSE $1::int END)"
    )

    async def count_strategy_outcomes_unsettled(
        self, *, settle_seconds: int
    ) -> int:
        """Сколько строк посчитано раньше, чем закрылся последний бар окна.

        ``settle_seconds`` — запас для НЕИЗВЕСТНОГО разрешения (ветка ELSE
        предиката). У строк с ``resolution`` из перечня 016 запас берётся по
        самому разрешению, и этот параметр их не касается. Имя параметра
        оставлено прежним намеренно: его значение по-прежнему приходит из
        ``barrier.runner.settle_seconds()``, и переименование заставило бы
        править вызовы ради того же самого числа.
        """
        value = await self.pool.fetchval(
            "SELECT count(*) FROM strategy_outcomes "
            f"WHERE {self.STRATEGY_UNSETTLED_PREDICATE};",
            int(settle_seconds),
        )
        return int(value or 0)

    async def get_strategy_outcomes_unsettled(
        self, *, settle_seconds: int, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Подозрительные строки целиком — для разбивок и примеров в отчёте.

        ``resolution`` возвращается наравне с остальным: именно оно задаёт,
        сколько строке полагалось ждать, и разбор находки без него начинался бы
        с выяснения, чем эти строки вообще меряли.
        """
        query = (
            "SELECT strategy, instrument_id, entry_ts, horizon_h, computed_at, "
            "       outcome, net_pnl_pct, logic_version, resolution "
            "FROM strategy_outcomes "
            f"WHERE {self.STRATEGY_UNSETTLED_PREDICATE} "
            "ORDER BY entry_ts DESC, strategy ASC"
        )
        args: list[Any] = [int(settle_seconds)]
        if limit is not None:
            query += " LIMIT $2"
            args.append(int(limit))
        rows = await self.pool.fetch(query + ";", *args)
        return [dict(r) for r in rows]

    async def delete_strategy_outcomes_unsettled(
        self, *, settle_seconds: int
    ) -> int:
        """Удаляет ТОЛЬКО подозрительные строки. Возвращает число удалённых.

        ЗВАТЬ ЕГО НАПРЯМУЮ НЕЛЬЗЯ. Единственный путь к нему —
        ``scripts/repair_9_1_strategy_settle.py --apply``, и там перед вызовом
        стоят три ограждения (§2.2 ТЗ 9.1.1): снятый снимок «до», совпавшее
        подтверждение числом и ненулевой счёт. Удаление безвозвратно, а
        пересчёт возможен ТОЛЬКО после удаления — обратного хода нет.

        Пересчёт этот метод НЕ выполняет и выполнять не должен: удалённые ключи
        исчезают из множества «уже посчитано», и очередной штатный прогон
        базовых стратегий посчитает их заново — уже исправленным правилом
        годности. Отдельный путь пересчёта означал бы второй код, считающий то
        же самое, и однажды он разошёлся бы со штатным.
        """
        status = await self.pool.execute(
            "DELETE FROM strategy_outcomes "
            f"WHERE {self.STRATEGY_UNSETTLED_PREDICATE};",
            int(settle_seconds),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def get_strategy_stats_snapshot(self) -> list[dict[str, Any]]:
        """Снимок «до/после» по каждой паре (стратегия, горизонт).

        Три величины на пару: число строк, доля исходов ``target`` и средний
        ``net_pnl_pct``. Строки без итога (``no_data``, ``ambiguous``) в среднее
        не входят — среднее по неизвестному не определено, — но в общее число
        строк входят: иначе по снимку нельзя было бы понять, изменился ли
        состав выборки или только её среднее.
        """
        rows = await self.pool.fetch(
            """
            SELECT strategy, horizon_h,
                   count(*) AS rows,
                   count(*) FILTER (WHERE outcome = 'target') AS targets,
                   avg(net_pnl_pct) AS avg_net_pnl_pct
            FROM strategy_outcomes
            GROUP BY strategy, horizon_h
            ORDER BY strategy ASC, horizon_h ASC;
            """
        )
        return [dict(r) for r in rows]

    async def get_strategy_pairs_done(
        self, *, logic_version: int
    ) -> set[tuple[str, int, datetime, int]]:
        """Уже посчитанные ключи — чтобы не читать окно свечей ради ничего.

        Без этого множества идемпотентный повторный прогон всё равно прочитал
        бы все окна заново и только потом узнал от ``ON CONFLICT``, что писать
        нечего. На десятках тысяч пар это разница между секундами и минутами.
        """
        rows = await self.pool.fetch(
            "SELECT strategy, instrument_id, entry_ts, horizon_h "
            "FROM strategy_outcomes WHERE logic_version = $1;",
            int(logic_version),
        )
        return {
            (r["strategy"], r["instrument_id"], r["entry_ts"], r["horizon_h"])
            for r in rows
        }

    # --- Ведение одной позиции, ВИРТУАЛЬНО (Этап 9.1) ---

    async def ensure_positions_schema(self) -> None:
        """Идемпотентно создаёт таблицу позиций (миграция 018).

        ДВА ИНДЕКСА ЗДЕСЬ — НЕ УСКОРЕНИЕ, А ПРАВИЛО ЭТАПА, ЗАПИСАННОЕ БАЗОЙ.
        Проверка «нет открытой позиции» в коде переживает ровно до первой
        гонки: сервис перезапустили, две итерации наложились — и позиций стало
        две. База такого не допустит вовсе.
        """
        await self.pool.execute(POSITIONS_DDL)
        await self.pool.execute(POSITIONS_CHECKS)
        await self.pool.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_one_open_per_instrument "
            "ON positions (instrument_id) WHERE status = 'open';"
        )
        await self.pool.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_signal "
            "ON positions (signal_id);"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_positions_status_opened "
            "ON positions (status, opened_at DESC);"
        )
        # ПЯТОЕ ЗНАЧЕНИЕ ПРИЧИНЫ ВЫХОДА (миграция 019) — на уже существующей
        # таблице. Блок POSITIONS_CHECKS выше трогает ограничение только если
        # его нет вовсе, а здесь оно есть и, возможно, ещё из четырёх значений:
        # миграция 019 могла быть не применена на работающем томе.
        #
        # ЧЕМ ЭТО ГРОЗИТ, ЕСЛИ НЕ ЧИНИТЬ. Закрытие по пробелу в данных упало бы
        # на нарушении ограничения, сервис (он не падает по построению) записал
        # бы это предупреждением в журнал, и позиция осталась бы висеть вечно —
        # ровно то, ради устранения чего §6 и написан.
        #
        # Ограничение пересоздаётся ТОЛЬКО если оно ещё не знает data_gap:
        # безусловный DROP/ADD на каждом старте оставлял бы таблицу на доли
        # секунды без закрытого перечня причин.
        await self.pool.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'positions_reason_chk'
                      AND conrelid = 'positions'::regclass
                      AND pg_get_constraintdef(oid) NOT LIKE '%data_gap%'
                ) THEN
                    ALTER TABLE positions DROP CONSTRAINT positions_reason_chk;
                    ALTER TABLE positions ADD CONSTRAINT positions_reason_chk
                        CHECK (exit_reason IS NULL OR exit_reason IN
                               ('target', 'stop', 'timeout', 'ambiguous',
                                'data_gap'));
                END IF;
            END $$;
            """
        )
        # Роль только на чтение (сервис бота) должна видеть таблицу сразу:
        # её GRANT ON ALL TABLES отработал, когда таблицы ещё не было, и без
        # явного права бот молча перестал бы отвечать на /positions.
        await self.pool.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agenttrade_ro') THEN
                    GRANT SELECT ON positions TO agenttrade_ro;
                END IF;
            END $$;
            """
        )

    async def get_position_candidates(
        self,
        *,
        logic_version: int,
        horizon_h: int,
        min_probability: float,
        max_signal_age_sec: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Сигналы-кандидаты на открытие позиции (§4.1 ТЗ 9.1).

        УСЛОВИЯ ВЫРАЖЕНЫ В ЗАПРОСЕ, А НЕ В КОДЕ ПОВЕРХ НЕГО. Иначе сервис тянул
        бы все сигналы за сутки, чтобы отбросить 99% из них в питоне, и — что
        хуже — правило отбора существовало бы в двух местах: в запросе, который
        «примерно» сужает выборку, и в коде, который решает на самом деле.

        ЗДЕСЬ ПРОВЕРЯЕТСЯ ШЕСТЬ УСЛОВИЙ ИЗ ДЕВЯТИ: решение, версия логики,
        полнота кворума (``degraded = FALSE`` — это и есть признак полного
        состава трёх агентов, его ставит сам decision), порог вероятности,
        возраст сигнала и наличие ЗАМОРОЖЕННОЙ цели на нужный горизонт. Плюс
        два условия единственности: по инструменту нет открытой позиции и по
        этому сигналу позиция НИКОГДА не открывалась.

        Оставшееся условие — свежая закрытая свеча — в запрос НЕ вносится: оно
        требует чтения ряда с учётом запаса на закрытие бара, и попытка выразить
        его здесь превратила бы отбор в соединение с ``ohlcv`` ради одной
        строки на инструмент.

        ЦЕНА РЕШЕНИЯ БЕРЁТСЯ ИЗ ``signal_targets.price_at_signal``, а не из
        ``signals``: в ``signals`` цены нет вовсе, и именно замороженная строка
        хранит ту цену, от которой система назвала цель человеку.

        ОТБОР НЕ СМОТРИТ НА ``notified``. У уведомлений своя защита от потока —
        она бережёт внимание человека, а не моделирует торговлю; привязка
        позиций к отправке внесла бы в замер ограничения, не имеющие отношения
        к рынку.
        """
        rows = await self.pool.fetch(
            """
            SELECT s.id AS signal_id, s.instrument_id, s.ts AS signal_ts,
                   s.decision, s.probability, s.degraded, s.logic_version,
                   t.price_at_signal, t.target_pct,
                   i.symbol,
                   EXTRACT(EPOCH FROM ($5::timestamptz - s.ts)) AS age_sec
            FROM signals s
            JOIN signal_targets t
              ON t.signal_id = s.id AND t.horizon_h = $2
            JOIN instruments i ON i.id = s.instrument_id
            WHERE s.decision = 'buy'
              AND s.logic_version = $1
              AND s.degraded = FALSE
              AND s.probability IS NOT NULL
              AND s.probability >= $3
              AND t.target_pct IS NOT NULL
              AND t.price_at_signal > 0
              AND s.ts >= $5::timestamptz - make_interval(secs => $4::int)
              AND NOT EXISTS (
                    SELECT 1 FROM positions p WHERE p.signal_id = s.id
              )
              AND NOT EXISTS (
                    SELECT 1 FROM positions p
                    WHERE p.instrument_id = s.instrument_id
                      AND p.status = 'open'
              )
            ORDER BY s.ts DESC, s.id DESC;
            """,
            int(logic_version), int(horizon_h), float(min_probability),
            int(max_signal_age_sec), now,
        )
        return [dict(r) for r in rows]

    async def count_open_positions(self) -> int:
        """Сколько слотов занято прямо сейчас."""
        value = await self.pool.fetchval(
            "SELECT count(*) FROM positions WHERE status = 'open';"
        )
        return int(value or 0)

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """Все открытые позиции с символом инструмента, старейшие первыми."""
        rows = await self.pool.fetch(
            """
            SELECT p.*, i.symbol
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.status = 'open'
            ORDER BY p.opened_at ASC;
            """
        )
        return [dict(r) for r in rows]

    async def get_last_closed_bar(
        self, instrument_id: int, timeframe: str, not_after: datetime
    ) -> dict[str, Any] | None:
        """Последняя свеча, ОТКРЫВШАЯСЯ не позже ``not_after``.

        Границу «что считается закрытым» задаёт вызывающий, передавая
        ``not_after`` уже с вычтенным запасом: коллектор перезаписывает
        формирующуюся свечу (UPSERT с DO UPDATE), и бар, взятый в момент его
        закрытия, ещё может измениться. Прятать этот запас внутрь запроса
        нельзя — тогда правило существовало бы в двух местах.
        """
        row = await self.pool.fetchrow(
            """
            SELECT ts, open, high, low, close
            FROM ohlcv
            WHERE instrument_id = $1 AND timeframe = $2 AND ts <= $3
            ORDER BY ts DESC
            LIMIT 1;
            """,
            int(instrument_id), str(timeframe), not_after,
        )
        return dict(row) if row is not None else None

    async def open_position(self, row: dict[str, Any]) -> int | None:
        """Открывает позицию. ``None`` означает «кто-то опередил», а не сбой.

        ГОНКА ЗАКРЫВАЕТСЯ БАЗОЙ. Нарушение ``ux_positions_one_open_per_instrument``
        или ``ux_positions_signal`` — это ШТАТНЫЙ исход: две итерации сервиса
        наложились (перезапуск, длинная итерация), и вторая обязана уступить,
        а не уронить весь цикл. Возвращённый ``None`` вызывающий пишет в журнал
        ключом ``positions_race_skipped=1``.
        """
        try:
            value = await self.pool.fetchval(
                """
                INSERT INTO positions
                    (instrument_id, signal_id, logic_version, horizon_h, side,
                     is_virtual, status, signal_ts, signal_price, opened_at,
                     entry_price, entry_lag_sec, entry_slippage_pct, qty,
                     notional_usd, target_pct, target_price, stop_pct,
                     stop_price, cost_pct, deadline_at, last_checked_ts,
                     resolution)
                VALUES ($1, $2, $3, $4, $5, TRUE, 'open', $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
                RETURNING id;
                """,
                int(row["instrument_id"]),
                int(row["signal_id"]),
                int(row["logic_version"]),
                int(row["horizon_h"]),
                str(row["side"]),
                row["signal_ts"],
                _num(row["signal_price"]),
                row["opened_at"],
                _num(row["entry_price"]),
                int(row["entry_lag_sec"]),
                _num(row["entry_slippage_pct"]),
                _num(row["qty"]),
                _num(row["notional_usd"]),
                _num(row["target_pct"]),
                _num(row["target_price"]),
                _num(row["stop_pct"]),
                _num(row["stop_price"]),
                _num(row["cost_pct"]),
                row["deadline_at"],
                row.get("last_checked_ts"),
                str(row["resolution"]),
            )
            return int(value) if value is not None else None
        except asyncpg.UniqueViolationError:
            return None

    async def touch_position(
        self, position_id: int, last_checked_ts: datetime
    ) -> None:
        """Двигает отметку «докуда разобрано». Позиция остаётся открытой."""
        await self.pool.execute(
            "UPDATE positions SET last_checked_ts = $2, updated_at = now() "
            "WHERE id = $1;",
            int(position_id), last_checked_ts,
        )

    async def close_position(
        self,
        position_id: int,
        *,
        closed_at: datetime,
        exit_price: float,
        exit_reason: str,
        outcome_certain: bool,
        net_pnl_pct: float,
        net_pnl_usd: float,
        bars_held: int,
        mae_pct: float,
        mfe_pct: float,
        last_checked_ts: datetime,
    ) -> bool:
        """Закрывает позицию ОДНИМ UPDATE. Возвращает, изменилась ли строка.

        Условие ``status = 'open'`` в запросе обязательно: закрыть уже закрытую
        позицию значило бы переписать её итог задним числом, и сделала бы это
        итерация, которая просто отстала. Ноль изменённых строк — это ответ
        «уже закрыта», а не ошибка.
        """
        status = await self.pool.execute(
            """
            UPDATE positions
               SET status = 'closed', closed_at = $2, exit_price = $3,
                   exit_reason = $4, outcome_certain = $5, net_pnl_pct = $6,
                   net_pnl_usd = $7, bars_held = $8, mae_pct = $9,
                   mfe_pct = $10, last_checked_ts = $11, updated_at = now()
             WHERE id = $1 AND status = 'open';
            """,
            int(position_id), closed_at, _num(exit_price), str(exit_reason),
            bool(outcome_certain), _num(net_pnl_pct), _num(net_pnl_usd),
            int(bars_held), _num(mae_pct), _num(mfe_pct), last_checked_ts,
        )
        return bool(status and status.rsplit(" ", 1)[-1] != "0")

    # --- Этап 9.1.3: теневой подвижный выход на фактических позициях ---
    #
    # ЧИТАЮЩИЕ МЕТОДЫ НЕ ТРОГАЮТ НИ ОДНОЙ ТАБЛИЦЫ ФАКТА. Пишет этап только в
    # position_trailing_shadow; positions, signals, signal_evaluations,
    # signal_targets, risk_targets, signal_outcomes_barrier, strategy_outcomes и
    # trailing_outcomes этим этапом не изменяются ни одной строкой (§6.5 ТЗ).

    async def position_trailing_shadow_exists(self) -> bool:
        """Есть ли таблица теневого замера (миграция 021 могла быть не применена).

        СХЕМА ЗДЕСЬ НЕ ДУБЛИРУЕТСЯ, в отличие от ``ensure_trailing_schema``
        Этапа 8.10. Второй экземпляр той же схемы — это два места, знающих одно
        и то же, и они однажды разойдутся; в этом проекте так уже было. Пусть
        лучше скрипт скажет «примените миграцию 021», чем заведёт таблицу,
        которая чуть-чуть не такая, как в файле миграции.
        """
        return bool(
            await self.pool.fetchval(
                "SELECT to_regclass('position_trailing_shadow') IS NOT NULL;"
            )
        )

    async def get_positions_for_shadow(
        self, *, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Закрытые позиции для теневого замера, БЕЗ ``data_gap`` (§3.1 ТЗ).

        ``data_gap`` ИСКЛЮЧАЕТСЯ, а не помечается: цена выхода у таких позиций
        не наблюдалась, а восстановлена по последней известной свече, и в
        статистику они по решению владельца от 30.08.2026 не идут. Открытые
        позиции не берутся вовсе — их исход ещё не наступил, и теневая цифра по
        ним была бы прогнозом, а не замером.

        Токен берётся СОЕДИНЕНИЕМ с ``instruments``: колонки ``symbol`` в
        ``positions`` нет, есть ``instrument_id`` с внешним ключом.
        """
        rows = await self.pool.fetch(
            """
            SELECT p.id, p.instrument_id, i.symbol, i.base,
                   p.logic_version, p.horizon_h, p.side, p.status,
                   p.opened_at, p.deadline_at, p.closed_at,
                   p.entry_price, p.notional_usd,
                   p.target_pct, p.target_price, p.stop_pct, p.stop_price,
                   p.cost_pct, p.resolution,
                   p.exit_price, p.exit_reason, p.net_pnl_pct, p.net_pnl_usd,
                   p.bars_held, p.outcome_certain
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.status = 'closed'
              AND p.exit_reason IS DISTINCT FROM 'data_gap'
              AND ($1::timestamptz IS NULL OR p.opened_at >= $1)
            ORDER BY p.id;
            """,
            since,
        )
        return [dict(row) for row in rows]

    async def count_positions_for_shadow(
        self, *, since: datetime | None = None
    ) -> dict[str, int]:
        """Справочные счётчики выборки: всего закрытых, из них ``data_gap``, открытых.

        Число исключённых печатается отдельной строкой (§3.1 ТЗ): выборка, из
        которой что-то молча выпало, неотличима от выборки, в которой этого не
        было.
        """
        row = await self.pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'closed') AS closed_total,
                count(*) FILTER (WHERE status = 'closed'
                                   AND exit_reason = 'data_gap') AS data_gap,
                count(*) FILTER (WHERE status = 'open') AS still_open
            FROM positions
            WHERE ($1::timestamptz IS NULL OR opened_at >= $1);
            """,
            since,
        )
        return {
            "closed_total": int(row["closed_total"] or 0),
            "data_gap": int(row["data_gap"] or 0),
            "still_open": int(row["still_open"] or 0),
        }

    async def save_position_trailing_shadow(
        self, rows: list[dict[str, Any]]
    ) -> int:
        """Пачка строк теневого замера. Возвращает число отправленных строк.

        ``ON CONFLICT DO UPDATE`` — это и есть идемпотентность §5.1 ТЗ: повторный
        прогон на тех же данных перезаписывает строку теми же числами и не
        создаёт дублей. Здесь выбрано DO UPDATE, а не DO NOTHING Этапа 8.10,
        по прямому требованию ТЗ; смысл тот же — «повторный прогон не меняет
        числа, если не изменились данные».
        """
        if not rows:
            return 0
        await self.pool.executemany(
            """
            INSERT INTO position_trailing_shadow (
                position_id, variant, activation_frac, pullback_frac,
                armed, armed_at, exit_reason, exit_bar_ts, exit_price,
                net_pnl_pct, net_pnl_usd, bars_used, resolution, logic_version
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (position_id, variant) DO UPDATE SET
                activation_frac = EXCLUDED.activation_frac,
                pullback_frac   = EXCLUDED.pullback_frac,
                armed           = EXCLUDED.armed,
                armed_at        = EXCLUDED.armed_at,
                exit_reason     = EXCLUDED.exit_reason,
                exit_bar_ts     = EXCLUDED.exit_bar_ts,
                exit_price      = EXCLUDED.exit_price,
                net_pnl_pct     = EXCLUDED.net_pnl_pct,
                net_pnl_usd     = EXCLUDED.net_pnl_usd,
                bars_used       = EXCLUDED.bars_used,
                resolution      = EXCLUDED.resolution,
                logic_version   = EXCLUDED.logic_version,
                computed_at     = now();
            """,
            [
                (
                    int(r["position_id"]), str(r["variant"]),
                    _num(r.get("activation_frac")), _num(r.get("pullback_frac")),
                    bool(r["armed"]), r.get("armed_at"),
                    str(r["exit_reason"]), r.get("exit_bar_ts"),
                    _num(r.get("exit_price")),
                    _num(r.get("net_pnl_pct")), _num(r.get("net_pnl_usd")),
                    int(r["bars_used"]), str(r["resolution"]),
                    int(r["logic_version"]),
                )
                for r in rows
            ],
        )
        return len(rows)

    # --- Этап 9.1.4: пересчёт исхода при других уровнях предела убытка ---
    #
    # ВЫБОРКА ПОЗИЦИЙ ЗДЕСЬ НЕ ЗАВОДИТСЯ ЗАНОВО. Она та же, что у Этапа 9.1.3:
    # ``get_positions_for_shadow`` и ``count_positions_for_shadow`` отбирают
    # закрытые позиции без ``data_gap`` — ровно тот состав, который требует §2
    # ТЗ 9.1.4. Второй запрос с тем же смыслом однажды разошёлся бы с первым,
    # и разошёлся бы молча: два замера на «одной и той же» выборке дали бы
    # несравнимые числа, и заметить это было бы нечем.
    #
    # ПИШЕТ ЭТАП ТОЛЬКО В ``position_stop_shadow``. ``positions``, ``signals``,
    # ``signal_evaluations``, ``signal_targets``, ``risk_targets``,
    # ``trailing_outcomes`` и ``position_trailing_shadow`` не изменяются ни
    # одной строкой (§7.8 ТЗ).

    async def position_stop_shadow_exists(self) -> bool:
        """Есть ли таблица замера (миграция 022 могла быть не применена).

        СХЕМА ЗДЕСЬ НЕ ДУБЛИРУЕТСЯ, как и в 9.1.3. Второй экземпляр той же
        схемы — это два места, знающих одно и то же, и они однажды разойдутся.
        Пусть лучше скрипт скажет «примените миграцию 022», чем заведёт
        таблицу, которая чуть-чуть не такая, как в файле миграции.
        """
        return bool(
            await self.pool.fetchval(
                "SELECT to_regclass('position_stop_shadow') IS NOT NULL;"
            )
        )

    async def count_blocked_signals(
        self,
        *,
        instrument_id: int,
        min_probability: float,
        ts_from: datetime,
        ts_to: datetime,
    ) -> int:
        """ЧИСЛО 3 §3 ТЗ: годные входы, попавшие в окно ЛИШНЕГО удержания слота.

        ГОДНЫЙ ВХОД — ЭТО ТО, ЧТО ПЕРЕЧИСЛЕНО В §3 ТЗ, И РОВНО ОНО: тот же
        инструмент, ``decision = 'buy'``, вероятность не ниже порога открытия
        позиций (``POSITION_MIN_PROBABILITY``), ``degraded = FALSE``, момент
        внутри окна. Живой отбор (``get_position_candidates``) проверяет сверх
        этого ещё четыре условия — версию логики, наличие замороженной цели,
        свежесть свечи и свободный слот, — и НЕ ПРОВЕРЯТЬ их здесь означает
        считать ВЕРХНЮЮ ГРАНИЦУ числа заблокированных входов. Это сказано
        прямо и в выводе скрипта: сузить перечень по своему усмотрению значило
        бы ответить на вопрос, которого §3 ТЗ не задавал.

        ОКНО ПОЛУОТКРЫТОЕ: ``[ts_from, ts_to)``. Границы содержательны, а не
        удобны. В момент ФАКТИЧЕСКОГО закрытия слот уже свободен, и сигнал,
        пришедший ровно тогда, вошёл бы в позицию — а при более широком пределе
        не вошёл бы, потому что позиция ещё висит. В момент ПЕРЕСЧЁТНОГО
        закрытия слот освобождается и там, поэтому правый конец не включается.

        Пустое или вывернутое окно — ноль без обращения к базе: при пределе уже
        фактического позиция закрылась бы РАНЬШЕ, лишнего удержания нет вовсе,
        и запрос с ``ts_from >= ts_to`` вернул бы ноль, но заодно скрыл бы, что
        случай этот разобран намеренно.
        """
        if ts_to <= ts_from:
            return 0
        value = await self.pool.fetchval(
            """
            SELECT count(*)
            FROM signals
            WHERE instrument_id = $1
              AND decision = 'buy'
              AND degraded = FALSE
              AND probability IS NOT NULL
              AND probability >= $2
              AND ts >= $3
              AND ts < $4;
            """,
            int(instrument_id), float(min_probability), ts_from, ts_to,
        )
        return int(value or 0)

    async def save_position_stop_shadow(self, rows: list[dict[str, Any]]) -> int:
        """Пачка строк замера. Возвращает число отправленных строк.

        ``ON CONFLICT DO UPDATE`` — это и есть идемпотентность §5 ТЗ: повторный
        прогон на тех же данных перезаписывает строку теми же числами и не
        создаёт дублей.
        """
        if not rows:
            return 0
        await self.pool.executemany(
            """
            INSERT INTO position_stop_shadow (
                position_id, variant, stop_pct, exit_reason, exit_bar_ts,
                exit_price, net_pnl_pct, net_pnl_usd, held_sec, extra_held_sec,
                blocked_signals, bars_used, resolution, logic_version
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (position_id, variant) DO UPDATE SET
                stop_pct        = EXCLUDED.stop_pct,
                exit_reason     = EXCLUDED.exit_reason,
                exit_bar_ts     = EXCLUDED.exit_bar_ts,
                exit_price      = EXCLUDED.exit_price,
                net_pnl_pct     = EXCLUDED.net_pnl_pct,
                net_pnl_usd     = EXCLUDED.net_pnl_usd,
                held_sec        = EXCLUDED.held_sec,
                extra_held_sec  = EXCLUDED.extra_held_sec,
                blocked_signals = EXCLUDED.blocked_signals,
                bars_used       = EXCLUDED.bars_used,
                resolution      = EXCLUDED.resolution,
                logic_version   = EXCLUDED.logic_version,
                computed_at     = now();
            """,
            [
                (
                    int(r["position_id"]), str(r["variant"]),
                    _num(r.get("stop_pct")), str(r["exit_reason"]),
                    r.get("exit_bar_ts"), _num(r.get("exit_price")),
                    _num(r.get("net_pnl_pct")), _num(r.get("net_pnl_usd")),
                    int(r["held_sec"]), int(r["extra_held_sec"]),
                    int(r["blocked_signals"]), int(r["bars_used"]),
                    str(r["resolution"]), int(r["logic_version"]),
                )
                for r in rows
            ],
        )
        return len(rows)

    # Порция чтения ``trailing_outcomes`` — в СТРОКАХ, не в парах. Тринадцать
    # тысяч строк это примерно тысяча пар и около 13 МБ Python-объектов: мало,
    # чтобы поместиться в любой разумный лимит, и много, чтобы обращений к базе
    # было около сотни на полтора миллиона строк, а не десятки тысяч.
    TRAILING_RESAMPLE_BATCH = 13_000

    async def fetch_trailing_resample_batch(
        self,
        *,
        after: tuple[datetime, int, int] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Очередная ПОРЦИЯ строк ``trailing_outcomes`` для части А. ТОЛЬКО ЧТЕНИЕ.

        ПОЧЕМУ ПОРЦИЯМИ, А НЕ ЦЕЛИКОМ. Прежняя редакция читала всю таблицу одним
        запросом. На боевой базе это 1 707 940 строк, и ядро убило процесс по
        лимиту контейнера (anon-rss 811 МБ и 919 МБ в двух прогонах подряд,
        ``mem_limit: 1g``). Считаемые числа при этом занимают ТРИНАДЦАТЬ
        МЕГАБАЙТ: всё остальное — Python-объекты строк, которые нужны ровно на
        время сборки одной пары. Порционное чтение убирает именно их, не трогая
        ни одного числа.

        ГРАНИЦА ПОРЦИИ — КЛЮЧ ПАРЫ ``(ts, signal_id, horizon_h)``, а не смещение.
        ``OFFSET`` заставил бы базу каждый раз перечитывать пропущенное, и сотая
        порция стоила бы как сто первых. Ключ-граница читает ровно нужное.

        ПОРЯДОК — ПО ВРЕМЕНИ СИГНАЛА, затем по ключу пары. Это тот же порядок, в
        котором пары оказывались у ``scripts.trailing_stats.collect`` после его
        сортировки, и он значим: от него зависят и деление выборки пополам, и
        последовательность случайных чисел в перестановочной проверке. Сменить
        порядок значило бы получить другие числа при том же расчёте.

        Все тринадцать строк одной пары лежат подряд (ключ у них общий), поэтому
        вызывающий может собирать пару из подряд идущих строк и не держать в
        памяти больше одной пары зараз.
        """
        # Размер порции берётся В МОМЕНТ ВЫЗОВА, а не значением по умолчанию:
        # значение по умолчанию вычисляется один раз при создании класса, и
        # тогда его нельзя было бы ни настроить, ни проверить на малой порции.
        limit = self.TRAILING_RESAMPLE_BATCH if limit is None else int(limit)
        if limit <= 13:
            # Порция меньше пары сделала бы невозможной сборку пары целиком:
            # граница всегда отсекала бы её середину, и чтение не двигалось бы.
            raise ValueError(f"порция обязана вмещать пару целиком: {limit}")
        rows = await self.pool.fetch(
            """
            SELECT t.signal_id, t.horizon_h, t.activation_ratio, t.retrace_ratio,
                   t.logic_version, t.exit_reason, t.net_pnl_pct, t.computed_at,
                   s.ts, i.base AS token
            FROM trailing_outcomes t
            JOIN signals s ON s.id = t.signal_id
            JOIN instruments i ON i.id = s.instrument_id
            WHERE $1::timestamptz IS NULL
               OR (s.ts, t.signal_id, t.horizon_h) > ($1, $2, $3)
            ORDER BY s.ts, t.signal_id, t.horizon_h,
                     t.activation_ratio, t.retrace_ratio
            LIMIT $4;
            """,
            None if after is None else after[0],
            None if after is None else int(after[1]),
            None if after is None else int(after[2]),
            int(limit),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Этап 9.1.5: положение цены сигнала в НЕДЕЛЬНОМ РАЗМАХЕ. ЗАМЕР.
    #
    # Все методы ниже ЧИТАЮТ факт и пишут ровно одну производную таблицу
    # signal_range_position. Ни signals, ни ohlcv, ни signal_targets, ни
    # signal_outcomes_barrier ими не изменяются.
    # ------------------------------------------------------------------

    async def signal_range_position_exists(self) -> bool:
        """Есть ли таблица замера (миграция 023 могла быть не применена).

        СХЕМА ЗДЕСЬ НЕ ДУБЛИРУЕТСЯ, как и в 9.1.3 и 9.1.4. Второй экземпляр той
        же схемы — это два места, знающих одно и то же, и они однажды
        разойдутся. Пусть лучше скрипт скажет «примените миграцию 023», чем
        заведёт таблицу, которая чуть-чуть не такая, как в файле миграции.
        """
        return bool(
            await self.pool.fetchval(
                "SELECT to_regclass('signal_range_position') IS NOT NULL;"
            )
        )

    async def get_range_position_instruments(
        self, *, timeframe: str
    ) -> list[dict[str, Any]]:
        """Инструменты с направленными сигналами и САМЫЙ РАННИЙ бар каждого.

        ЗАЧЕМ ЗДЕСЬ САМЫЙ РАННИЙ БАР. Окно замера — семь суток НАЗАД от
        сигнала, а минутные свечи живут ``RETENTION_1M_DAYS`` суток. У сигнала,
        чьё окно начинается раньше первого сохранившегося бара, окна нет вовсе,
        и посчитать по обрезку значило бы выдать размах трёх суток за размах
        семи. Первый бар инструмента — это ровно та граница, левее которой
        полного окна не бывает; она читается ОДИН РАЗ на инструмент, а не на
        каждый из десятков тысяч сигналов.
        """
        rows = await self.pool.fetch(
            """
            SELECT i.id AS instrument_id, i.base AS token,
                   min(o.ts) AS first_bar_ts,
                   max(o.ts) AS last_bar_ts
            FROM instruments i
            LEFT JOIN ohlcv o
              ON o.instrument_id = i.id AND o.timeframe = $1
            WHERE EXISTS (
                SELECT 1 FROM signals s
                WHERE s.instrument_id = i.id AND s.decision <> 'wait'
            )
            GROUP BY i.id, i.base
            ORDER BY i.id;
            """,
            str(timeframe),
        )
        return [dict(row) for row in rows]

    async def count_range_position_signals(self) -> dict[str, int]:
        """Сколько направленных сигналов есть всего и сколько из них с исходом.

        ДВА ЧИСЛА, А НЕ ОДНО (§4 ТЗ, ЧИСЛО 1). «Сигналов всего» и «сигналов, по
        которым Этап 8.8 успел посчитать исход» — разные величины, и разница
        между ними это не потеря замера, а состояние расчёта исходов. Печатать
        одно вместо другого значило бы объявить недосчитанное несуществующим.
        """
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS directional_total,
                   count(*) FILTER (
                       WHERE EXISTS (
                           SELECT 1 FROM signal_outcomes_barrier b
                           WHERE b.signal_id = s.id
                       )
                   ) AS with_outcome
            FROM signals s
            WHERE s.decision <> 'wait';
            """
        )
        if row is None:
            return {"directional_total": 0, "with_outcome": 0}
        return {
            "directional_total": int(row["directional_total"] or 0),
            "with_outcome": int(row["with_outcome"] or 0),
        }

    # Порции чтения. Подобраны так, чтобы обращений к базе были сотни, а не
    # десятки тысяч, и чтобы ни одна порция не стоила больше нескольких
    # десятков мегабайт Python-объектов: на Этапе 9.1.3 полная загрузка
    # выборки была убита ядром по ``mem_limit: 1g`` дважды подряд.
    RANGE_POSITION_SIGNALS_BATCH = 10_000
    RANGE_POSITION_BARS_BATCH = 20_000
    RANGE_POSITION_OUTCOMES_BATCH = 20_000

    async def fetch_range_position_signals_batch(
        self,
        *,
        instrument_id: int,
        after: tuple[datetime, int] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Порция сигналов ОДНОГО инструмента в порядке времени. ТОЛЬКО ЧТЕНИЕ.

        ЦЕНА СИГНАЛА БЕРЁТСЯ ИЗ ЗАМОРОЖЕННОЙ СТРОКИ ``signal_targets``, а не из
        свечи и не из цены входа позиции (§2.2 ТЗ). В самой таблице ``signals``
        колонки с ценой нет вовсе — это проверяемый факт схемы, а не мнение, —
        и единственная цена, ЗАПИСАННАЯ В МОМЕНТ РЕШЕНИЯ, лежит именно там.
        Цена входа позиции не годится принципиально: позиция открывается ПОСЛЕ
        сигнала и по другой цене, и подстановка её сюда сдвинула бы положение в
        размахе на величину, которой в момент решения ещё не существовало.

        ``DISTINCT ON (s.id)`` с ``ORDER BY s.id, t.horizon_h`` берёт строку
        МЛАДШЕГО горизонта. Цена решения у всех горизонтов одна и та же —
        она замораживается один раз на сигнал, — но выбирать её надо
        ОПРЕДЕЛЁННЫМ правилом, а не «какая попадётся»: молчаливая зависимость
        от порядка строк однажды дала бы два разных числа на одних данных.

        ПОРЯДОК — ПО ВРЕМЕНИ СИГНАЛА, затем по идентификатору. Расчёт положения
        идёт ОДНИМ проходом по барам с подвижными минимумом и максимумом, и
        этот проход требует, чтобы сигналы приходили строго по возрастанию
        времени: окно следующего сигнала не может начинаться раньше окна
        предыдущего. Сменить порядок значило бы сломать сам способ счёта.

        Граница порции — ключ ``(ts, id)``, а не ``OFFSET``: ``OFFSET``
        заставил бы базу перечитывать пропущенное, и сотая порция стоила бы как
        сто первых.
        """
        limit = self.RANGE_POSITION_SIGNALS_BATCH if limit is None else int(limit)
        rows = await self.pool.fetch(
            """
            SELECT q.signal_id, q.ts, q.logic_version, q.decision,
                   q.price_at_signal
            FROM (
                SELECT DISTINCT ON (s.id)
                       s.id AS signal_id, s.ts, s.logic_version, s.decision,
                       t.price_at_signal
                FROM signals s
                JOIN signal_targets t ON t.signal_id = s.id
                WHERE s.instrument_id = $1
                  AND s.decision <> 'wait'
                  AND EXISTS (
                      SELECT 1 FROM signal_outcomes_barrier b
                      WHERE b.signal_id = s.id
                  )
                ORDER BY s.id, t.horizon_h
            ) q
            WHERE $2::timestamptz IS NULL OR (q.ts, q.signal_id) > ($2, $3)
            ORDER BY q.ts, q.signal_id
            LIMIT $4;
            """,
            int(instrument_id),
            None if after is None else after[0],
            None if after is None else int(after[1]),
            int(limit),
        )
        return [dict(row) for row in rows]

    async def fetch_range_position_bars_batch(
        self,
        *,
        instrument_id: int,
        timeframe: str,
        ts_from: datetime,
        after_ts: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Порция баров ОДНОГО инструмента в порядке времени. ТОЛЬКО ЧТЕНИЕ.

        ПОЧЕМУ ПОРЦИЯМИ, А НЕ ЦЕЛИКОМ, И ПОЧЕМУ ЭТО ВАЖНО ИМЕННО ЗДЕСЬ. Окно
        замера — семь суток минутных баров, то есть около 10 080 штук НА КАЖДЫЙ
        сигнал. Прочитать окно отдельным запросом на сигнал значило бы прочитать
        сотни миллионов строк и пересчитать минимум с максимумом заново каждый
        раз. Здесь бары читаются ОДИН РАЗ по инструменту в порядке времени, а
        окно ведётся подвижными минимумом и максимумом поверх этого потока.

        Возвращаются ровно ``ts``, ``low`` и ``high``: ни ``open``, ни
        ``close``, ни ``volume`` в размах не входят, и читать их значило бы
        втрое увеличить объём порции ради полей, которые никто не смотрит.
        """
        limit = self.RANGE_POSITION_BARS_BATCH if limit is None else int(limit)
        rows = await self.pool.fetch(
            """
            SELECT ts, low, high
            FROM ohlcv
            WHERE instrument_id = $1
              AND timeframe = $2
              AND ts >= $3
              AND ($4::timestamptz IS NULL OR ts > $4)
            ORDER BY ts
            LIMIT $5;
            """,
            int(instrument_id), str(timeframe), ts_from, after_ts, int(limit),
        )
        return [dict(row) for row in rows]

    async def fetch_range_position_outcomes_batch(
        self,
        *,
        after: tuple[int, int] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Порция ИСХОДОВ Этапа 8.8 для таблиц §4. ТОЛЬКО ЧТЕНИЕ.

        ИСХОД НЕ СЧИТАЕТСЯ ЗАНОВО. Таблица ``signal_outcomes_barrier`` и её
        колонка ``outcome`` — это уже посчитанное правило «цель–предел–срок»
        Этапа 8.8, а ``net_pnl_pct`` — чистый итог С ТЕМИ ЖЕ ИЗДЕРЖКАМИ, что в
        живом расчёте (они записаны в ту же строку колонкой ``cost_pct``).
        Второй экземпляр этой арифметики однажды разошёлся бы с первым, и тогда
        замер мерил бы собственную ошибку.

        ``ambiguous`` и ``no_data`` отсюда НЕ отфильтрованы намеренно: у них
        ``net_pnl_pct IS NULL``, их численность обязана быть напечатана в
        составе выборки, и решение, что с ними делать, принимает расчёт, а не
        запрос. Тихая фильтрация в SQL сделала бы их несуществующими.
        """
        limit = self.RANGE_POSITION_OUTCOMES_BATCH if limit is None else int(limit)
        rows = await self.pool.fetch(
            """
            SELECT b.signal_id, b.horizon_h, b.direction, b.outcome,
                   b.net_pnl_pct, b.logic_version, s.ts, s.instrument_id,
                   i.base AS token
            FROM signal_outcomes_barrier b
            JOIN signals s ON s.id = b.signal_id
            JOIN instruments i ON i.id = s.instrument_id
            WHERE $1::bigint IS NULL
               OR (b.signal_id, b.horizon_h) > ($1, $2)
            ORDER BY b.signal_id, b.horizon_h
            LIMIT $3;
            """,
            None if after is None else int(after[0]),
            None if after is None else int(after[1]),
            int(limit),
        )
        return [dict(row) for row in rows]

    async def save_signal_range_position(self, rows: list[dict[str, Any]]) -> int:
        """Пачка строк замера. Возвращает число отправленных строк.

        ИДЕМПОТЕНТНОСТЬ ЗДЕСЬ СТРОЖЕ, ЧЕМ В 9.1.4, И НАМЕРЕННО. Условие
        ``WHERE`` при ``DO UPDATE`` не даёт перезаписать строку, у которой ВСЕ
        значения совпали, — а значит, повторный прогон на тех же данных не
        двигает даже ``computed_at``. Простой ``DO UPDATE`` без условия
        переписывал бы метку времени каждым прогоном, и требование §7 ТЗ
        «повторный прогон не меняет ни числа строк, ни значений» выполнялось бы
        только на словах.
        """
        if not rows:
            return 0
        await self.pool.executemany(
            """
            INSERT INTO signal_range_position (
                signal_id, window_days, range_low, range_high,
                range_width_pct, pos, last_bar_ts, bars_in_window, resolution
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (signal_id, window_days) DO UPDATE SET
                range_low       = EXCLUDED.range_low,
                range_high      = EXCLUDED.range_high,
                range_width_pct = EXCLUDED.range_width_pct,
                pos             = EXCLUDED.pos,
                last_bar_ts     = EXCLUDED.last_bar_ts,
                bars_in_window  = EXCLUDED.bars_in_window,
                resolution      = EXCLUDED.resolution,
                computed_at     = now()
            WHERE signal_range_position.range_low
                      IS DISTINCT FROM EXCLUDED.range_low
               OR signal_range_position.range_high
                      IS DISTINCT FROM EXCLUDED.range_high
               OR signal_range_position.range_width_pct
                      IS DISTINCT FROM EXCLUDED.range_width_pct
               OR signal_range_position.pos IS DISTINCT FROM EXCLUDED.pos
               OR signal_range_position.last_bar_ts
                      IS DISTINCT FROM EXCLUDED.last_bar_ts
               OR signal_range_position.bars_in_window
                      IS DISTINCT FROM EXCLUDED.bars_in_window
               OR signal_range_position.resolution
                      IS DISTINCT FROM EXCLUDED.resolution;
            """,
            [
                (
                    int(r["signal_id"]), int(r["window_days"]),
                    _num(r["range_low"]), _num(r["range_high"]),
                    _num(r["range_width_pct"]), _num(r["pos"]),
                    r["last_bar_ts"], int(r["bars_in_window"]),
                    str(r["resolution"]),
                )
                for r in rows
            ],
        )
        return len(rows)

    async def get_positions_sheet_marks(
        self, position_ids: list[int]
    ) -> list[dict[str, Any]]:
        """Отметки выгрузки в лист по перечню позиций — для отчёта репарации.

        Возвращает ТОЛЬКО существующие строки и в порядке ``id``: несуществующие
        идентификаторы вызывающий обязан заметить сам, сравнив длину ответа с
        длиной запроса. Молча вернуть меньше строк, чем спросили, и продолжить
        значило бы сбрасывать отметки не тому набору, который назвал человек.

        ТОКЕН БЕРЁТСЯ ИЗ ``instruments``, А НЕ ИЗ ``positions``. В самой таблице
        позиций названия инструмента нет вовсе — есть ``instrument_id`` и внешний
        ключ; ``symbol`` (``ETH/USDT``) лежит в ``instruments``. Первая редакция
        этого метода спрашивала ``symbol`` прямо у ``positions`` и падала на
        боевой базе с ``UndefinedColumnError``. Все остальные запросы к позициям
        в проекте соединяются с ``instruments`` именно так — этот выбился из
        общего строя, и ровно в нём и была ошибка.

        КОЛОНКИ КВАЛИФИЦИРОВАНЫ ПСЕВДОНИМАМИ (``p.``/``i.``) намеренно: по
        неквалифицированному имени не видно, из какой оно таблицы, и опечатка
        вроде той же ``symbol`` выглядит совершенно правдоподобно до самого
        запуска на настоящей базе.
        """
        rows = await self.pool.fetch(
            """
            SELECT p.id, i.symbol, p.status, p.opened_at, p.closed_at,
                   p.exit_reason, p.sheet_opened_at, p.sheet_closed_at
            FROM positions p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.id = ANY($1::bigint[])
            ORDER BY p.id;
            """,
            [int(i) for i in position_ids],
        )
        return [dict(row) for row in rows]

    async def reset_positions_sheet_marks(self, position_ids: list[int]) -> int:
        """Снимает ОБЕ отметки выгрузки в лист. Возвращает число строк.

        ЗВАТЬ НАПРЯМУЮ НЕЛЬЗЯ. Единственный путь сюда —
        ``scripts/repair_9_1_2_2_marks.py --apply``, и там перед вызовом стоит
        подтверждение числом: сброс возможен только тогда, когда оператор своими
        глазами видел отчёт и назвал то же самое число.

        МЕНЯЮТСЯ РОВНО ДВЕ КОЛОНКИ, И НИ ОДНОЙ БОЛЬШЕ. Ни строк не удаляется, ни
        решений не пересчитывается: снятая отметка означает лишь «эту позицию
        выгрузка увидит снова», и всю работу дальше делает штатный прогон.

        ``updated_at`` НЕ ТРОГАЕТСЯ НАМЕРЕННО, в отличие от остальных методов
        этого класса. Здесь чинится не сама позиция, а состояние её ВЫГРУЗКИ;
        сдвинутое время правки говорило бы, что решение по сделке пересматривали,
        а его не пересматривали.
        """
        status = await self.pool.execute(
            "UPDATE positions SET sheet_opened_at = NULL, sheet_closed_at = NULL "
            "WHERE id = ANY($1::bigint[]);",
            [int(i) for i in position_ids],
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def get_positions_summary(self, *, days: int = 7) -> dict[str, Any]:
        """Итог по закрытым позициям за окно — для бота (§10) и отчёта.

        Средний ``net_pnl_pct`` и сумма ``net_pnl_usd`` считаются по закрытым
        позициям окна, включая ``ambiguous``: у тех итог определён (он взят по
        пределу, пессимистично), просто менее достоверен. ЗАКРЫТИЯ ПО ПРОБЕЛУ В
        ДАННЫХ (``data_gap``, Этап 9.1.1 §6.7) в средние и суммы НЕ ВХОДЯТ: у
        них цена выхода не наблюдалась, а восстановлена, и их итог описывает
        длительность сбоя сбора данных, а не поведение рынка. Их число
        возвращается отдельным полем. Их число
        печатается ОТДЕЛЬНОЙ строкой — так видно, велика ли их доля, и при этом
        средний итог не оказывается посчитанным по выборке, отличной от той,
        которую человек видит в разбивке по причинам.
        """
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS closed,
                   count(*) FILTER (WHERE outcome_certain = FALSE) AS uncertain,
                   count(*) FILTER (WHERE exit_reason = 'data_gap') AS data_gap,
                   avg(net_pnl_pct) FILTER (WHERE exit_reason <> 'data_gap')
                       AS avg_net_pnl_pct,
                   sum(net_pnl_usd) FILTER (WHERE exit_reason <> 'data_gap')
                       AS sum_net_pnl_usd,
                   avg(entry_slippage_pct) FILTER (WHERE exit_reason <> 'data_gap')
                       AS avg_slippage_pct,
                   avg(entry_lag_sec) AS avg_lag_sec
            FROM positions
            WHERE status = 'closed'
              AND closed_at >= now() - make_interval(days => $1::int);
            """,
            int(days),
        )
        reasons = await self.pool.fetch(
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
        summary["by_reason"] = {
            str(r["exit_reason"]): int(r["n"]) for r in reasons
        }
        return summary

    # --- Подвижный выход (Этап 8.10) ---

    async def ensure_trailing_schema(self) -> None:
        """Идемпотентно создаёт таблицу исходов подвижного выхода (миграция 017)."""
        await self.pool.execute(TRAILING_OUTCOMES_DDL)
        await self.pool.execute(TRAILING_OUTCOMES_CHECKS)
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_trailing_outcomes_variant "
            "ON trailing_outcomes "
            "(logic_version, activation_ratio, retrace_ratio, horizon_h);"
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_trailing_outcomes_reason "
            "ON trailing_outcomes (exit_reason, horizon_h);"
        )

    async def get_trailing_anchors(
        self,
        *,
        logic_version: int,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Пары (сигнал, горизонт) для расчёта — ИЗ ``signal_outcomes_barrier``.

        Источник выбран не произвольно, и это ключевое решение этапа. Вместе с
        парой оттуда берутся ВСЕ входные числа: направление, цена решения,
        замороженная цель, предел и издержки. Собери мы их заново из
        ``signal_targets`` и ``.env``, контрольный вариант разошёлся бы с
        Этапом 8.8 в тот день, когда на сервере изменится ``BARRIER_STOP_PCT``
        или ``RISK_COST_ROUNDTRIP_PCT``, — причём разошёлся бы молча, и §4 ТЗ
        («совпасть до последнего знака») было бы нарушено не ошибкой расчёта,
        а сменой настройки.
        """
        query = """
            SELECT b.signal_id, b.horizon_h, b.logic_version, b.direction,
                   b.price_at_signal, b.target_pct, b.stop_pct, b.cost_pct,
                   s.instrument_id, s.ts
            FROM signal_outcomes_barrier b
            JOIN signals s ON s.id = b.signal_id
            WHERE b.logic_version = $1
              AND ($2::timestamptz IS NULL OR s.ts >= $2)
            ORDER BY s.ts ASC, b.signal_id ASC, b.horizon_h ASC
        """
        args: list[Any] = [int(logic_version), since]
        if limit is not None:
            query += " LIMIT $3"
            args.append(int(limit))
        rows = await self.pool.fetch(query + ";", *args)
        return [dict(r) for r in rows]

    async def get_trailing_pairs_done(
        self, *, logic_version: int, variants: int
    ) -> set[tuple[int, int]]:
        """Пары, у которых посчитаны ВСЕ варианты — их окно читать незачем.

        Условие ``count(*) = variants`` намеренно жёстче, чем «есть хоть одна
        строка»: пара, недосчитанная из-за прерванного прогона, обязана быть
        досчитана следующим запуском, а не остаться навсегда с четырьмя
        вариантами из тринадцати. Без этого множества идемпотентный повторный
        прогон всё равно прочитал бы все окна и только потом узнал бы от
        ``ON CONFLICT``, что писать нечего.
        """
        rows = await self.pool.fetch(
            "SELECT signal_id, horizon_h FROM trailing_outcomes "
            "WHERE logic_version = $1 "
            "GROUP BY signal_id, horizon_h HAVING count(*) >= $2;",
            int(logic_version), int(variants),
        )
        return {(int(r["signal_id"]), int(r["horizon_h"])) for r in rows}

    async def save_trailing_outcomes(self, rows: list[dict[str, Any]]) -> int:
        """Запись исходов пачкой. Существующие строки НЕ переписываются.

        Пачкой, а не по одной, потому что на пару приходится тринадцать строк, а
        пар — десятки тысяч: ``executemany`` превращает тринадцать обращений к
        базе в одно. ``ON CONFLICT DO NOTHING`` по первичному ключу — это и есть
        идемпотентность §7: повторный запуск на тех же данных не меняет ни одной
        строки. Принудительный пересчёт идёт отдельным путём (``--recompute``),
        который сначала удаляет строки: переписывание «на всякий случай» скрыло
        бы расхождение расчётов вместо того, чтобы его показать.
        """
        if not rows:
            return 0
        await self.pool.executemany(
            """
            INSERT INTO trailing_outcomes
                (signal_id, horizon_h, activation_ratio, retrace_ratio,
                 logic_version, direction, price_at_signal, target_pct,
                 stop_pct, cost_pct, exit_reason, hit_at, bars_to_hit,
                 net_pnl_pct, peak_pct, mae_pct, mfe_pct, resolution,
                 computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19)
            ON CONFLICT (signal_id, horizon_h, activation_ratio, retrace_ratio)
            DO NOTHING;
            """,
            [
                (
                    int(row["signal_id"]),
                    int(row["horizon_h"]),
                    _num(row["activation_ratio"]),
                    _num(row["retrace_ratio"]),
                    int(row["logic_version"]),
                    str(row["direction"]),
                    _num(row["price_at_signal"]),
                    _num(row["target_pct"]),
                    _num(row["stop_pct"]),
                    _num(row["cost_pct"]),
                    str(row["exit_reason"]),
                    row.get("hit_at"),
                    None if row.get("bars_to_hit") is None else int(row["bars_to_hit"]),
                    _num(row.get("net_pnl_pct")),
                    _num(row["peak_pct"]),
                    _num(row["mae_pct"]),
                    _num(row["mfe_pct"]),
                    str(row["resolution"]),
                    row["computed_at"],
                )
                for row in rows
            ],
        )
        return len(rows)

    async def delete_trailing_outcomes(self, *, logic_version: int) -> int:
        """Удаляет посчитанные исходы подвижного выхода (только ``--recompute``).

        Возвращает число удалённых строк — оно идёт в журнал: пересчёт, стёрший
        больше, чем ожидалось, обязан быть виден.
        """
        status = await self.pool.execute(
            "DELETE FROM trailing_outcomes WHERE logic_version = $1;",
            int(logic_version),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def check_trailing_control(
        self, *, logic_version: int
    ) -> dict[str, int]:
        """Сверка КОНТРОЛЬНОГО варианта с ``signal_outcomes_barrier`` (§4 ТЗ).

        Контрольный вариант — та же фиксированная цель, что в Этапе 8.8, и
        считается он прямым вызовом того же кода. Значит, его строки обязаны
        совпасть с таблицей 8.8 ДО ПОСЛЕДНЕГО ЗНАКА. Сверка идёт в базе, а не в
        памяти: сравниваются значения, которые РЕАЛЬНО ЗАПИСАНЫ, вместе со всеми
        округлениями типа NUMERIC. Сравнение в памяти пропустило бы расхождение,
        возникшее при записи.

        ``IS DISTINCT FROM`` вместо ``<>`` обязателен: NULL <> NULL даёт NULL, и
        расхождение по полю, где с одной стороны NULL, осталось бы незамеченным.

        Возвращает три числа: сколько строк сверено, сколько разошлось и сколько
        строк 8.8 не получили контрольной пары вовсе. Несовпадение — БЛОКИРУЮЩЕЕ
        (§4 ТЗ): оно означает, что правила касания разошлись, и тогда
        недействительно ВСЁ сравнение вариантов, а не только контрольная строка.
        """
        row = await self.pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE t.signal_id IS NOT NULL) AS compared,
                count(*) FILTER (WHERE t.signal_id IS NULL)     AS missing,
                count(*) FILTER (WHERE t.signal_id IS NOT NULL AND (
                        t.exit_reason     IS DISTINCT FROM b.outcome
                     OR t.hit_at          IS DISTINCT FROM b.hit_at
                     OR t.bars_to_hit     IS DISTINCT FROM b.bars_to_hit
                     OR t.net_pnl_pct     IS DISTINCT FROM b.net_pnl_pct
                     OR t.mae_pct         IS DISTINCT FROM b.mae_pct
                     OR t.mfe_pct         IS DISTINCT FROM b.mfe_pct
                     OR t.resolution      IS DISTINCT FROM b.resolution
                     OR t.direction       IS DISTINCT FROM b.direction
                     OR t.price_at_signal IS DISTINCT FROM b.price_at_signal
                     OR t.target_pct      IS DISTINCT FROM b.target_pct
                     OR t.stop_pct        IS DISTINCT FROM b.stop_pct
                     OR t.cost_pct        IS DISTINCT FROM b.cost_pct
                )) AS mismatched
            FROM signal_outcomes_barrier b
            LEFT JOIN trailing_outcomes t
              ON t.signal_id = b.signal_id AND t.horizon_h = b.horizon_h
             AND t.activation_ratio = 0 AND t.retrace_ratio = 0
            WHERE b.logic_version = $1;
            """,
            int(logic_version),
        )
        return {
            "compared": int(row["compared"] or 0),
            "missing": int(row["missing"] or 0),
            "mismatched": int(row["mismatched"] or 0),
        }

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

    async def get_instrument_symbol(self, instrument_id: int) -> str | None:
        """Символ инструмента по идентификатору (для подписи уведомления).

        Введено Этапом 8.1: токенов пять, и сообщение обязано называть тот
        инструмент, по которому выдан сигнал, а не символ из настройки SYMBOL.
        """
        return await self.pool.fetchval(
            "SELECT symbol FROM instruments WHERE id = $1;", int(instrument_id)
        )

    async def get_independent_outcomes(
        self,
        logic_version: int,
        horizon_h: int,
    ) -> list[dict[str, Any]]:
        """Независимые наблюдения для калибровки: одно на окно И НА ТОКЕН.

        Берутся закрытые направленные сигналы указанной версии логики с
        ``degraded = false``, у которых есть оценка на нужном горизонте. Окна —
        непересекающиеся отрезки длиной в горизонт, из окна берётся ПЕРВЫЙ по
        времени сигнал. Прореживание обязательно: решения выдаются раз в минуту,
        а горизонт — часы, поэтому соседние сигналы описывают почти один и тот
        же отрезок рынка и независимыми наблюдениями не являются.

        ЭТАП 8.1: ключ прореживания включает ИНСТРУМЕНТ. Без него пять токенов
        конкурировали бы за одно окно, и от каждого окна оставался бы ровно один
        сигнал — четыре пятых наблюдений исчезли бы молча. Это НЕ означает, что
        пять токенов дают пятикратную мощность: криптовалюты сильно
        коррелированы, и корреляция исходов считается отдельно (§7 ТЗ 8.1).
        """
        query = """
            SELECT DISTINCT ON (instrument_id, win)
                   win, instrument_id, id, ts, probability, success
            FROM (
                SELECT s.id, s.ts, s.instrument_id, s.probability, e.success,
                       to_timestamp(
                           floor(extract(epoch FROM s.ts) / ($2 * 3600)) * ($2 * 3600)
                       ) AS win
                FROM signals s
                JOIN signal_evaluations e
                  ON e.signal_id = s.id AND e.horizon_h = $2
                WHERE s.logic_version = $1
                  AND s.decision <> 'wait'
                  AND s.degraded = FALSE
                  AND s.probability IS NOT NULL
            ) q
            ORDER BY instrument_id, win, ts ASC;
        """
        rows = await self.pool.fetch(query, int(logic_version), int(horizon_h))
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

    async def ensure_user_settings_schema(self) -> None:
        """Идемпотентно создаёт таблицу настроек пользователя (§1 ТЗ 8.3).

        Порядок старта сервисов не задан, а таблица нужна и боту (пишет), и
        сервису уведомлений (читает), — поэтому её наличие гарантирует каждый.
        """
        await self.pool.execute(USER_SETTINGS_DDL)

    async def get_user_settings(self, chat_id: int) -> dict[str, Any] | None:
        """Настройки чата или ``None``, если человек их ни разу не открывал.

        ``None`` — не ошибка и не повод создавать строку: значения по умолчанию
        задаёт код (:mod:`src.core.user_settings`). Запись настроек, которых
        человек не задавал, позже выглядела бы как его собственный выбор.
        """
        row = await self.pool.fetchrow(
            "SELECT chat_id, instruments, horizon_h, min_score, quiet_from, "
            "       quiet_to, updated_at "
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
        """Сохраняет настройки чата целиком (создаёт или заменяет)."""
        await self.pool.execute(
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

    async def get_instrument_id(self, symbol: str) -> int | None:
        """Идентификатор инструмента по символу или ``None``, если его нет."""
        return await self.pool.fetchval(
            "SELECT id FROM instruments WHERE symbol = $1;", symbol
        )

    async def get_agent_metrics(
        self, agent: str, instrument_id: int, ts: datetime
    ) -> dict[str, Any] | None:
        """Метрики вывода агента ровно на момент ``ts`` (§3 ТЗ 8.3).

        Текст сигнала объясняет мнение агента ЕГО СОБСТВЕННЫМИ метриками, и
        брать их можно только у того самого вывода, который участвовал в
        решении: ``agents_payload`` хранит момент каждого мнения, поэтому поиск
        точный, а не «последний по этому агенту». Взять свежайший вывод значило
        бы объяснять одно решение показаниями, которых в нём не было.

        Метрики НЕ добавляются в ``agents_payload``: это меняло бы то, что
        пишет Decision Agent, а §7 ТЗ 8.3 запрещает его трогать.
        """
        row = await self.pool.fetchrow(
            "SELECT metrics FROM agent_outputs "
            " WHERE agent = $1 AND instrument_id = $2 AND ts = $3 LIMIT 1;",
            agent, int(instrument_id), ts,
        )
        if not row or row["metrics"] is None:
            return None
        metrics = row["metrics"]
        if isinstance(metrics, str):
            try:
                return json.loads(metrics)
            except (TypeError, ValueError):
                return None
        return dict(metrics)

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
        """Идемпотентно приводит схему оценок к виду Этапа 8.1 (§5).

        Повторяет миграцию ``009_stage_8_1_horizons.sql`` для случая, когда том
        БД старше init.sql: сервисы поднимаются в произвольном порядке, и
        оценщик обязан работать сразу, а не после ручного применения миграции.
        Горизонт существующих записей берётся из уже записанного текста
        (``'4h'`` → 4), НОВЫХ строк не создаётся — досчёт горизонтов задним
        числом запрещён (§12 ТЗ 8.1).
        """
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_evaluations (
                signal_id       BIGINT NOT NULL REFERENCES signals(id),
                horizon         TEXT NOT NULL,
                horizon_h       SMALLINT NOT NULL,
                price_at_signal DOUBLE PRECISION NOT NULL,
                price_at_close  DOUBLE PRECISION NOT NULL,
                pnl_pct         DOUBLE PRECISION NOT NULL,
                drawdown_pct    DOUBLE PRECISION NOT NULL,
                success         BOOLEAN NOT NULL,
                evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (signal_id, horizon_h)
            );
            """
        )
        await self.pool.execute(
            "ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS "
            "horizon_h SMALLINT;"
        )
        await self.pool.execute(
            """
            UPDATE signal_evaluations
               SET horizon_h = CASE
                    WHEN horizon ~ '^[0-9]+h$'
                        THEN (regexp_replace(horizon, 'h$', ''))::smallint
                    WHEN horizon ~ '^[0-9]+$' THEN horizon::smallint
                    WHEN horizon ~ '^[0-9]+d$'
                        THEN ((regexp_replace(horizon, 'd$', ''))::int * 24)::smallint
                    ELSE 4::smallint
                   END
             WHERE horizon_h IS NULL;
            """
        )
        await self.pool.execute(
            "ALTER TABLE signal_evaluations ALTER COLUMN horizon_h SET NOT NULL;"
        )
        # Та же защита, что в миграции 009: у одного сигнала не может быть двух
        # оценок на один горизонт. Если это случилось (в колонке horizon было
        # значение, которое не разбирается, — оно получает 4 и сталкивается с
        # настоящей строкой '4h'), сервис обязан сказать это прямо, а не падать
        # на «could not create unique index» при смене ключа.
        duplicates = await self.pool.fetchval(
            """
            SELECT string_agg(DISTINCT signal_id::text, ', ')
              FROM (
                  SELECT signal_id FROM signal_evaluations
                   GROUP BY signal_id, horizon_h HAVING count(*) > 1
              ) q;
            """
        )
        if duplicates:
            raise RuntimeError(
                f"У сигналов {duplicates} есть по две оценки на один горизонт. "
                "Схема оценок не может быть приведена к виду Этапа 8.1, пока эти "
                "строки не разобраны вручную: удалять данные об оценках "
                "автоматически нельзя."
            )
        await self.pool.execute(
            "ALTER TABLE signal_evaluations DROP CONSTRAINT IF EXISTS "
            "signal_evaluations_signal_id_horizon_key;"
        )
        # Прежний первичный ключ — суррогатный id. Заменяем на (signal_id,
        # horizon_h) только если текущий ключ не является нужным.
        await self.pool.execute(
            """
            DO $$
            DECLARE current_pk TEXT;
            BEGIN
                SELECT conname INTO current_pk FROM pg_constraint
                 WHERE conrelid = 'signal_evaluations'::regclass AND contype = 'p';
                IF current_pk IS NULL THEN
                    ALTER TABLE signal_evaluations
                        ADD PRIMARY KEY (signal_id, horizon_h);
                ELSIF (
                    SELECT count(*) FROM pg_constraint c
                    JOIN unnest(c.conkey) k ON TRUE
                    JOIN pg_attribute a
                      ON a.attrelid = c.conrelid AND a.attnum = k
                    WHERE c.conname = current_pk
                      AND a.attname IN ('signal_id', 'horizon_h')
                ) <> 2 THEN
                    EXECUTE format(
                        'ALTER TABLE signal_evaluations DROP CONSTRAINT %I', current_pk
                    );
                    ALTER TABLE signal_evaluations
                        ADD PRIMARY KEY (signal_id, horizon_h);
                END IF;
            END $$;
            """
        )
        await self.pool.execute(
            "CREATE INDEX IF NOT EXISTS ix_eval_horizon "
            "ON signal_evaluations (horizon_h, evaluated_at);"
        )

    async def ensure_logic_version_schema(self) -> None:
        """Идемпотентно создаёт таблицу границ версии логики (§6 ТЗ 8.1).

        Ограничение ``logic_version > 0`` обязательно: ноль зарезервирован под
        признак «версия неизвестна» в ``agent_outputs_daily``. Без запрета этот
        признак нельзя было бы отличить от реальной версии, а вечная таблица
        итогов не должна допускать двусмысленности. Для уже созданных таблиц
        ограничение добавляется отдельно — ``CREATE TABLE IF NOT EXISTS``
        существующую таблицу не меняет.
        """
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS logic_version_windows (
                logic_version SMALLINT    PRIMARY KEY CHECK (logic_version > 0),
                started_at    TIMESTAMPTZ NOT NULL,
                note          TEXT
            );
            """
        )
        await self.pool.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conrelid = 'logic_version_windows'::regclass
                       AND conname = 'logic_version_windows_version_positive'
                ) AND NOT EXISTS (
                    SELECT 1 FROM logic_version_windows WHERE logic_version <= 0
                ) THEN
                    ALTER TABLE logic_version_windows
                        ADD CONSTRAINT logic_version_windows_version_positive
                        CHECK (logic_version > 0);
                END IF;
            END $$;
            """
        )

    async def record_logic_version_start(
        self, logic_version: int, note: str | None = None
    ) -> datetime:
        """Фиксирует момент начала версии логики и возвращает его.

        Запись делается ОДИН РАЗ: повторный старт сервиса границу не сдвигает
        (``ON CONFLICT DO NOTHING``), иначе после каждого перезапуска «начало
        версии» уезжало бы вперёд и данные до перезапуска выпадали бы из окна.
        Точность — минута: этого требует §6 ТЗ 8.1.
        """
        await self.ensure_logic_version_schema()
        await self.pool.execute(
            """
            INSERT INTO logic_version_windows (logic_version, started_at, note)
            VALUES ($1, date_trunc('minute', now()), $2)
            ON CONFLICT (logic_version) DO NOTHING;
            """,
            int(logic_version), note,
        )
        return await self.pool.fetchval(
            "SELECT started_at FROM logic_version_windows WHERE logic_version = $1;",
            int(logic_version),
        )

    async def get_logic_version_start(self, logic_version: int) -> datetime | None:
        """Момент начала версии логики или None, если он не зафиксирован."""
        return await self.pool.fetchval(
            "SELECT started_at FROM logic_version_windows WHERE logic_version = $1;",
            int(logic_version),
        )

    async def get_signals_to_evaluate(
        self,
        horizon_h: int,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Направленные сигналы, у которых прошёл горизонт и нет оценки по нему.

        ``since`` (Этап 8.1 §5) отсекает сигналы, выданные ДО перехода на
        текущую версию логики: досчитывать им новые горизонты задним числом
        запрещено — этих горизонтов не существовало в момент оценки.
        """
        query = """
            SELECT s.id, s.instrument_id, s.ts, s.decision
            FROM signals s
            WHERE s.decision <> 'wait'
              AND (now() - s.ts) >= make_interval(hours => $1)
              AND ($2::timestamptz IS NULL OR s.ts >= $2)
              AND NOT EXISTS (
                  SELECT 1 FROM signal_evaluations e
                  WHERE e.signal_id = s.id AND e.horizon_h = $1
              )
            ORDER BY s.ts ASC;
        """
        rows = await self.pool.fetch(query, int(horizon_h), since)
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
        horizon_h: int,
        price_at_signal: float,
        price_at_close: float,
        pnl_pct: float,
        drawdown_pct: float,
        success: bool,
    ) -> None:
        """INSERT оценки (идемпотентно по ключу (signal_id, horizon_h)).

        Текстовая колонка ``horizon`` заполняется подписью того же горизонта
        (``4`` → ``4h``): её читают выгрузка, бот и суточная сводка, и ломать их
        ради переименования незачем.
        """
        query = """
            INSERT INTO signal_evaluations
                (signal_id, horizon, horizon_h, price_at_signal, price_at_close,
                 pnl_pct, drawdown_pct, success)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (signal_id, horizon_h) DO NOTHING;
        """
        await self.pool.execute(
            query,
            signal_id,
            horizon_label(int(horizon_h)),
            int(horizon_h),
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
                   e.horizon_h AS horizon,
                   count(*) AS n,
                   avg(CASE WHEN e.success THEN 1.0 ELSE 0.0 END) AS success_rate,
                   avg(e.pnl_pct) AS avg_pnl_pct
            FROM signal_evaluations e
            JOIN signals s ON s.id = e.signal_id
            GROUP BY s.decision, e.horizon_h
            ORDER BY s.decision, e.horizon_h;
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


# --- DDL и вспомогательные функции целей по вероятности (Этап 8.2) ---
# Повторяют миграцию 014 дословно. Дублирование намеренное и принято в проекте:
# миграция применяется руками, а сервис обязан подниматься и на томе, где её
# ещё не применили, — иначе отсутствие УКРАШЕНИЯ (цели) останавливало бы выдачу
# сигналов, что прямо запрещено §6 ТЗ 8.2.

RISK_TARGETS_DDL = """
CREATE TABLE IF NOT EXISTS risk_targets (
    instrument_id      INT         NOT NULL REFERENCES instruments(id),
    horizon_h          SMALLINT    NOT NULL,
    direction          TEXT        NOT NULL CHECK (direction IN ('buy','sell')),
    computed_at        TIMESTAMPTZ NOT NULL,
    window_days        SMALLINT    NOT NULL,
    data_from          TIMESTAMPTZ NOT NULL,
    data_to            TIMESTAMPTZ NOT NULL,
    n_observations     INT         NOT NULL,
    target_pct         NUMERIC(10,5),
    hit_rate           NUMERIC(6,5),
    mfe_p25            NUMERIC(10,5),
    mfe_p50            NUMERIC(10,5),
    mfe_p75            NUMERIC(10,5),
    cost_roundtrip_pct NUMERIC(6,4)  NOT NULL,
    covers_fees        BOOLEAN       NOT NULL DEFAULT FALSE,
    no_target_reason   TEXT,
    source             TEXT        NOT NULL,
    targets_version    SMALLINT    NOT NULL,
    PRIMARY KEY (instrument_id, horizon_h, direction, computed_at)
);
"""

SIGNAL_TARGETS_DDL = """
CREATE TABLE IF NOT EXISTS signal_targets (
    signal_id            BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h            SMALLINT    NOT NULL,
    direction            TEXT        NOT NULL CHECK (direction IN ('buy','sell')),
    price_at_signal      DOUBLE PRECISION NOT NULL,
    target_pct           NUMERIC(10,5),
    target_price         DOUBLE PRECISION,
    hit_rate             NUMERIC(6,5),
    covers_fees          BOOLEAN     NOT NULL DEFAULT FALSE,
    no_target_reason     TEXT,
    risk_target_computed_at TIMESTAMPTZ,
    targets_version      SMALLINT,
    frozen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h)
);
"""

# --- Этап 8.8 §6: исход по границам ---
#
# Схема повторяет миграцию 015_barrier_outcomes.sql. Дубль намеренный и того же
# рода, что у risk_targets: сервис гарантирует свою схему при старте, потому что
# миграция могла быть не применена на уже работающем томе. Расхождение этих двух
# описаний ловит раздел 3 deploy/verify_8_8.sh — он сверяет их построчно.
BARRIER_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS signal_outcomes_barrier (
    signal_id       BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT    NOT NULL,
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    price_at_signal NUMERIC(20,8) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    outcome         TEXT        NOT NULL,
    hit_at          TIMESTAMPTZ,
    bars_to_hit     INTEGER,
    net_pnl_pct     NUMERIC(12,6),
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    resolution      TEXT        NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h)
);
"""

# Ограничения заводятся отдельно от CREATE TABLE по той же причине, что в
# миграции: на томе, где таблица уже создана, CREATE TABLE IF NOT EXISTS её
# не меняет, и ограничения иначе не появились бы никогда.
BARRIER_OUTCOMES_CHECKS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_outcome_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_outcome_chk
            CHECK (outcome IN ('target', 'stop', 'timeout', 'ambiguous', 'no_data'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_resolution_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_direction_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_logic_version_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_logic_version_chk
            CHECK (logic_version > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_bounds_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_bounds_chk
            CHECK (horizon_h > 0 AND price_at_signal > 0 AND stop_pct > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'signal_outcomes_barrier_shape_chk') THEN
        ALTER TABLE signal_outcomes_barrier
            ADD CONSTRAINT signal_outcomes_barrier_shape_chk
            CHECK (
                CASE outcome
                    WHEN 'target'    THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                          AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'      THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                          AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout'   THEN hit_at IS NULL AND bars_to_hit IS NULL
                                          AND net_pnl_pct IS NOT NULL
                    ELSE                  hit_at IS NULL AND bars_to_hit IS NULL
                                          AND net_pnl_pct IS NULL
                END
            );
    END IF;
END $$;
"""

# --- Этап 8.9 §6: исходы базовых стратегий ---
#
# Схема повторяет миграцию 016_strategy_outcomes.sql по той же причине, что и
# у двух предыдущих таблиц: сервис гарантирует свою схему при старте, потому
# что миграция могла быть не применена на уже работающем томе.
STRATEGY_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS strategy_outcomes (
    strategy        TEXT        NOT NULL,
    instrument_id   INT         NOT NULL REFERENCES instruments(id),
    entry_ts        TIMESTAMPTZ NOT NULL,
    horizon_h       SMALLINT    NOT NULL,
    signal_id       BIGINT      REFERENCES signals(id),
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    price_at_entry  NUMERIC(20,8) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    target_source   TEXT        NOT NULL,
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    outcome         TEXT        NOT NULL,
    hit_at          TIMESTAMPTZ,
    net_pnl_pct     NUMERIC(12,6),
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    resolution      TEXT        NOT NULL,
    seed            BIGINT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy, instrument_id, entry_ts, horizon_h)
);
"""

STRATEGY_OUTCOMES_CHECKS = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_strategy_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_strategy_chk
            CHECK (strategy IN ('always_buy', 'always_sell', 'coin_flip',
                                'system', 'grid_buy', 'grid_sell'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_outcome_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_outcome_chk
            CHECK (outcome IN ('target', 'stop', 'timeout', 'ambiguous', 'no_data'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_resolution_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_direction_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_bounds_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_bounds_chk
            CHECK (horizon_h > 0 AND price_at_entry > 0 AND stop_pct > 0
                   AND logic_version > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_target_source_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_target_source_chk
            CHECK (target_source = 'frozen'
                   OR target_source ~ '^risk_targets:\d{4}-\d{2}-\d{2}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_shape_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_shape_chk
            CHECK (
                CASE outcome
                    WHEN 'target'  THEN hit_at IS NOT NULL AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'    THEN hit_at IS NOT NULL AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout' THEN hit_at IS NULL AND net_pnl_pct IS NOT NULL
                    ELSE                hit_at IS NULL AND net_pnl_pct IS NULL
                END
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_signal_link_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_signal_link_chk
            CHECK (
                CASE WHEN strategy IN ('grid_buy', 'grid_sell')
                     THEN signal_id IS NULL
                     ELSE signal_id IS NOT NULL
                END
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'strategy_outcomes_seed_chk') THEN
        ALTER TABLE strategy_outcomes
            ADD CONSTRAINT strategy_outcomes_seed_chk
            CHECK ((strategy = 'coin_flip') = (seed IS NOT NULL));
    END IF;
END $$;
"""

# --- Этап 8.10 §6: подвижный выход ---
#
# Схема повторяет миграцию 017_trailing_outcomes.sql по той же причине, что и
# две предыдущие: расчёт гарантирует свою схему при старте, потому что миграция
# могла быть не применена на уже работающем томе. Расхождение этих двух описаний
# ловит раздел 4 deploy/verify_8_10.sh.
TRAILING_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS trailing_outcomes (
    signal_id       BIGINT      NOT NULL REFERENCES signals(id),
    horizon_h       SMALLINT    NOT NULL,
    activation_ratio NUMERIC(4,2) NOT NULL,
    retrace_ratio   NUMERIC(4,2) NOT NULL,
    logic_version   SMALLINT    NOT NULL,
    direction       TEXT        NOT NULL,
    price_at_signal NUMERIC(20,8) NOT NULL,
    target_pct      NUMERIC(10,6) NOT NULL,
    stop_pct        NUMERIC(10,6) NOT NULL,
    cost_pct        NUMERIC(10,6) NOT NULL,
    exit_reason     TEXT        NOT NULL,
    hit_at          TIMESTAMPTZ,
    bars_to_hit     INTEGER,
    net_pnl_pct     NUMERIC(12,6),
    peak_pct        NUMERIC(12,6) NOT NULL,
    mae_pct         NUMERIC(12,6) NOT NULL,
    mfe_pct         NUMERIC(12,6) NOT NULL,
    resolution      TEXT        NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, horizon_h, activation_ratio, retrace_ratio)
);
"""

TRAILING_OUTCOMES_CHECKS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_exit_reason_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_exit_reason_chk
            CHECK (exit_reason IN ('target', 'stop', 'trail', 'timeout',
                                   'ambiguous', 'no_data'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_variant_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_variant_chk
            CHECK ((activation_ratio, retrace_ratio) IN (
                (0.00, 0.00),
                (0.25, 0.20), (0.25, 0.33), (0.25, 0.50),
                (0.50, 0.20), (0.50, 0.33), (0.50, 0.50),
                (0.75, 0.20), (0.75, 0.33), (0.75, 0.50),
                (1.00, 0.20), (1.00, 0.33), (1.00, 0.50)
            ));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_resolution_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_resolution_chk
            CHECK (resolution IN ('1m', '1h'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_direction_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_direction_chk
            CHECK (direction IN ('buy', 'sell'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_bounds_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_bounds_chk
            CHECK (horizon_h > 0 AND price_at_signal > 0 AND stop_pct > 0
                   AND logic_version > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_shape_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_shape_chk
            CHECK (
                CASE exit_reason
                    WHEN 'target'  THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'stop'    THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'trail'   THEN hit_at IS NOT NULL AND bars_to_hit IS NOT NULL
                                        AND net_pnl_pct IS NOT NULL
                    WHEN 'timeout' THEN hit_at IS NULL AND bars_to_hit IS NULL
                                        AND net_pnl_pct IS NOT NULL
                    ELSE                hit_at IS NULL AND bars_to_hit IS NULL
                                        AND net_pnl_pct IS NULL
                END
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'trailing_outcomes_reason_variant_chk') THEN
        ALTER TABLE trailing_outcomes
            ADD CONSTRAINT trailing_outcomes_reason_variant_chk
            CHECK (
                (exit_reason <> 'trail'  OR activation_ratio > 0)
                AND
                (exit_reason <> 'target' OR activation_ratio = 0)
            );
    END IF;
END $$;
"""


# Строки signal_targets НИКОГДА не обновляются (§3, §12 ТЗ 8.2), поэтому
# запрос — чистый INSERT без UPDATE в конфликте. ``DO NOTHING`` нужен на случай
# повторной попытки записи того же сигнала после сбоя сети: он оставляет
# ПЕРВУЮ версию строки, а не переписывает её второй.
# --- Этап 9.1 §5: ведение одной позиции (ВИРТУАЛЬНО) ---
#
# Схема повторяет миграцию 018_positions.sql по той же причине, что и четыре
# предыдущие таблицы: сервис гарантирует свою схему при старте, потому что
# миграция могла быть не применена на уже работающем томе. Расхождение этих
# двух описаний ловит deploy/schema_drift.sh.
POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INT           NOT NULL REFERENCES instruments(id),
    signal_id           BIGINT        NOT NULL REFERENCES signals(id),
    logic_version       SMALLINT      NOT NULL,
    horizon_h           SMALLINT      NOT NULL,
    side                TEXT          NOT NULL,
    is_virtual          BOOLEAN       NOT NULL DEFAULT TRUE,
    status              TEXT          NOT NULL,
    signal_ts           TIMESTAMPTZ   NOT NULL,
    signal_price        NUMERIC(20,8) NOT NULL,
    opened_at           TIMESTAMPTZ   NOT NULL,
    entry_price         NUMERIC(20,8) NOT NULL,
    entry_lag_sec       INTEGER       NOT NULL,
    entry_slippage_pct  NUMERIC(12,6) NOT NULL,
    qty                 NUMERIC(28,12) NOT NULL,
    notional_usd        NUMERIC(12,4) NOT NULL,
    target_pct          NUMERIC(10,6) NOT NULL,
    target_price        NUMERIC(20,8) NOT NULL,
    stop_pct            NUMERIC(10,6) NOT NULL,
    stop_price          NUMERIC(20,8) NOT NULL,
    cost_pct            NUMERIC(10,6) NOT NULL,
    deadline_at         TIMESTAMPTZ   NOT NULL,
    last_checked_ts     TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    exit_price          NUMERIC(20,8),
    exit_reason         TEXT,
    outcome_certain     BOOLEAN,
    net_pnl_pct         NUMERIC(12,6),
    net_pnl_usd         NUMERIC(14,6),
    bars_held           INTEGER,
    mae_pct             NUMERIC(12,6),
    mfe_pct             NUMERIC(12,6),
    resolution          TEXT          NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);
"""

POSITIONS_CHECKS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_side_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_side_chk CHECK (side = 'buy');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_status_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_status_chk
            CHECK (status IN ('open', 'closed'));
    END IF;
    -- Этап 9.1.1 §6: пятое значение data_gap. Сервис гарантирует свою схему
    -- при старте, и на ЧИСТОМ томе (где миграции 018 и 019 ещё не применялись)
    -- перечень из четырёх значений отверг бы закрытие по пробелу в данных —
    -- сервис падал бы на первой же такой позиции. На уже работающей базе этот
    -- блок ничего не делает: ограничение там есть, и его правит миграция 019.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_reason_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_reason_chk
            CHECK (exit_reason IS NULL OR exit_reason IN
                   ('target', 'stop', 'timeout', 'ambiguous', 'data_gap'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_resolution_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_resolution_chk CHECK (resolution = '1m');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_bounds_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_bounds_chk
            CHECK (horizon_h > 0 AND entry_price > 0
                   AND signal_price > 0 AND stop_pct > 0
                   AND qty > 0 AND notional_usd > 0
                   AND logic_version > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'positions_shape_chk') THEN
        ALTER TABLE positions
            ADD CONSTRAINT positions_shape_chk
            CHECK (
                CASE status
                    WHEN 'open'   THEN closed_at IS NULL AND exit_price IS NULL
                                       AND exit_reason IS NULL
                                       AND net_pnl_pct IS NULL
                                       AND outcome_certain IS NULL
                    ELSE               closed_at IS NOT NULL
                                       AND exit_price IS NOT NULL
                                       AND exit_reason IS NOT NULL
                                       AND net_pnl_pct IS NOT NULL
                                       AND outcome_certain IS NOT NULL
                END
            );
    END IF;
END $$;
"""


SIGNAL_TARGET_INSERT = """
    INSERT INTO signal_targets
        (signal_id, horizon_h, direction, price_at_signal, target_pct,
         target_price, hit_rate, covers_fees, no_target_reason,
         risk_target_computed_at, targets_version)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (signal_id, horizon_h) DO NOTHING;
"""


def _num(value: Any) -> Any:
    """Число для колонки NUMERIC: asyncpg принимает Decimal, но не float.

    Округление до знаков, заданных типом колонки, выполняет сама база.
    """
    if value is None:
        return None
    return Decimal(str(float(value)))


def _signal_target_args(signal_id: int, target: dict[str, Any]) -> tuple[Any, ...]:
    """Аргументы INSERT замороженной цели в порядке SIGNAL_TARGET_INSERT."""
    return (
        int(signal_id),
        int(target["horizon_h"]),
        str(target["direction"]),
        float(target["price_at_signal"]),
        _num(target.get("target_pct")),
        None if target.get("target_price") is None else float(target["target_price"]),
        _num(target.get("hit_rate")),
        bool(target.get("covers_fees", False)),
        target.get("no_target_reason"),
        target.get("risk_target_computed_at"),
        None if target.get("targets_version") is None
        else int(target["targets_version"]),
    )
