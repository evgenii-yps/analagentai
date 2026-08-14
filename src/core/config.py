"""Типизированная конфигурация приложения на pydantic-settings.

Значения читаются из переменных окружения и файла ``.env``.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения, считываемые из окружения / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL ---
    POSTGRES_USER: str = "agenttrade"
    # Пароль обязателен: при его отсутствии приложение падает на старте
    # с понятной ошибкой валидации (field required).
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str = "agenttrade"
    PG_HOST: str = "postgres"
    PG_PORT: int = 5432

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    # --- Логирование ---
    LOG_LEVEL: str = "INFO"

    # --- Сбор данных (Этап 2) ---
    EXCHANGE: str = "binance"
    SYMBOL: str = "BTC/USDT"            # спотовый символ
    SWAP_SYMBOL: str = "BTC/USDT:USDT"  # бессрочный фьючерс (swap)
    TIMEFRAMES: str = "1m,5m,15m,1h"    # таймфреймы свечей через запятую
    OHLCV_INTERVAL: int = 30            # сек между опросами свечей
    ORDERBOOK_INTERVAL: int = 10        # сек между снимками стакана
    ORDERBOOK_DEPTH: int = 50           # глубина стакана
    TRADES_INTERVAL: int = 15           # сек между опросами сделок
    FUTURES_INTERVAL: int = 60          # сек между опросами funding/OI

    # --- Аналитические агенты (Этап 3) ---
    AGENT_TIMEFRAME: str = "1h"   # таймфрейм для Market Agent
    AGENT_INTERVAL: int = 60      # сек между запусками агентов
    AGENT_MIN_CANDLES: int = 200  # минимум свечей для анализа

    # --- Наблюдаемость сбоев агентов (Этап 7.0, Задача B) ---
    # Сколько подряд сбойных итераций одного агента вызывают алерт в Telegram.
    # По умолчанию 5: агент работает раз в 60 c, 5 подряд ≈ 5 мин «слепоты» —
    # достаточно рано, но не срабатывает на единичной случайной ошибке.
    AGENT_FAILURE_ALERT_STREAK: int = 5

    # --- Самовосстановление агентов (Этап 7.2, Задача A1) ---
    # Сколько сбоев ИЛИ пустых выборок подряд у одного агента запускают авто-сброс
    # (сброс внутреннего состояния + переоткрытие пула БД и клиента Redis).
    # По умолчанию 20: при цикле 60 c это ≈ 20 мин «слепоты» до самолечения —
    # заметно раньше 8 часов ожидания вотчдога в инциденте 14.08, но не срабатывает
    # на короткой серии случайных сбоев (алерт по AGENT_FAILURE_ALERT_STREAK=5
    # уходит раньше). 0 отключает авто-сброс.
    AGENT_AUTO_RESET_STREAK: int = 20

    # --- Деривативы: порог экстремума funding (Этап 7.0, Задача C) ---
    # Прежнее 0.0005 недостижимо (наблюдаемый |funding| ≤ 0.0001 при базовом
    # уровне ~0.0001). 0.0003 ≈ 3× базового: срабатывает при реальных всплесках
    # плеча, но молчит на спокойном базовом funding. Меняет ТОЛЬКО ветку разворота
    # (is_extreme), масштаб уверенности вынесен отдельно (см. futures.py).
    FUNDING_EXTREME_THRESHOLD: float = 0.0003

    # --- Decision Agent (Этап 4) ---
    DECISION_INTERVAL: int = 60       # сек между решениями
    DECISION_THRESHOLD: float = 0.3   # порог балла для buy/sell
    AGENT_FRESHNESS_SEC: int = 300    # макс. возраст вывода агента
    MIN_AGENTS: int = 2               # минимум свежих агентов для решения
    WEIGHT_MARKET: float = 1.0        # вес Market Agent
    WEIGHT_LIQUIDITY: float = 1.0     # вес Liquidity Agent
    WEIGHT_FUTURES: float = 1.0       # вес Futures Agent

    # --- Уведомления (Этап 5) ---
    TELEGRAM_BOT_TOKEN: str = ""      # токен Telegram-бота (пусто → сервис простаивает)
    TELEGRAM_CHAT_ID: str = ""        # ID чата получателя
    NOTIFY_INTERVAL: int = 30         # сек между проверками новых сигналов
    NOTIFY_MIN_PROBABILITY: float = 0.7  # минимальная вероятность для отправки
    # Минимум агентов со свежим содержательным выводом для ОТПРАВКИ уведомления
    # (Этап 7.2, Задача A2). insufficient_data содержательным не считается. Если
    # агентов меньше — сигнал сохраняется (и помечается degraded), но в Telegram
    # НЕ уходит: решение на неполной картине не должно выглядеть полноценным.
    NOTIFY_MIN_AGENTS: int = 3
    NOTIFY_COOLDOWN_SEC: int = 1800   # пауза перед повтором того же решения
    NOTIFY_TIMEZONE: str = "Europe/Moscow"  # часовой пояс времени в уведомлениях

    # --- Оценка результатов (Этап 6) ---
    EVAL_INTERVAL: int = 300          # сек между прогонами оценщика
    EVAL_HORIZONS: str = "1h,4h"      # горизонты оценки через запятую
    EVAL_PRIMARY_HORIZON: str = "4h"  # главный горизонт (сводка в signals)
    STATS_LOG_INTERVAL: int = 3600    # сек между логами статистики

    # --- Выгрузка сигналов (Этап 6.6) ---
    # Секреты без значения по умолчанию заданы пустой строкой умышленно: это
    # позволяет остальным сервисам стека и CI импортировать Settings без их
    # наличия. Обязательность проверяется в самом скрипте выгрузки в рантайме
    # (пустое значение → алерт и выход, «значение по умолчанию» не подставляется).
    EXPORT_ENABLED: bool = True       # общий выключатель выгрузки
    EXPORT_BATCH_SIZE: int = 500      # строк в одном запросе к Google Таблице
    SHEETS_WEBAPP_URL: str = ""       # URL веб-приложения Apps Script (обязателен)
    SHEETS_SHARED_SECRET: str = ""    # общий секрет Apps Script ↔ .env (обязателен)
    NOTION_API_TOKEN: str = ""        # внутренний токен интеграции Notion (обязателен)
    # База «Журнал сигналов» — ID известен и зафиксирован в ТЗ.
    NOTION_SIGNALS_DB_ID: str = "dacf5b37-f606-40cb-b0b9-89c51762e464"
    EXPORT_NOTION_ONLY_NOTIFIED: bool = True  # в Notion только сигналы с notified_at

    # --- Телеграм-бот только на чтение (Этап 6.7) ---
    BOT_ENABLED: bool = True          # выключатель сервиса бота
    BOT_POLL_TIMEOUT: int = 30        # сек, таймаут long polling getUpdates
    # Белый список chat_id через запятую. Пусто → берётся TELEGRAM_CHAT_ID.
    BOT_ALLOWED_CHAT_IDS: str = ""
    BOT_MAX_ROWS: int = 20            # потолок строк в /last
    BOT_RATE_LIMIT_SEC: int = 3       # мин. пауза между командами одного чата
    # Пароль роли БД только на чтение (agenttrade_ro). Генерируется установщиком;
    # пусто → сервис бота простаивает (не подключается основным пользователем).
    POSTGRES_RO_PASSWORD: str = ""

    @property
    def bot_allowed_chat_ids(self) -> set[str]:
        """Белый список chat_id (строки). По умолчанию — единственный чат владельца.

        Значения хранятся строками: chat_id из Telegram приходит числом, но
        сравнение ведём по строковому представлению, чтобы не зависеть от типа.
        """
        raw = self.BOT_ALLOWED_CHAT_IDS.strip() or self.TELEGRAM_CHAT_ID
        return {part.strip() for part in raw.split(",") if part.strip()}

    @property
    def pg_dsn_ro(self) -> str:
        """DSN подключения ролью только на чтение (agenttrade_ro)."""
        return (
            f"postgresql://agenttrade_ro:{self.POSTGRES_RO_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def eval_horizons_list(self) -> list[str]:
        """Разбирает строку EVAL_HORIZONS в список горизонтов."""
        return [h.strip() for h in self.EVAL_HORIZONS.split(",") if h.strip()]

    @property
    def agent_weights(self) -> dict[str, float]:
        """Веса агентов для взвешенной агрегации."""
        return {
            "market": self.WEIGHT_MARKET,
            "liquidity": self.WEIGHT_LIQUIDITY,
            "futures": self.WEIGHT_FUTURES,
        }

    @property
    def telegram_configured(self) -> bool:
        """Заданы ли токен и chat_id для отправки в Telegram."""
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def timeframes_list(self) -> list[str]:
        """Разбирает строку TIMEFRAMES в список таймфреймов."""
        return [tf.strip() for tf in self.TIMEFRAMES.split(",") if tf.strip()]

    @property
    def pg_dsn(self) -> str:
        """Собирает строку подключения (DSN) к PostgreSQL."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.POSTGRES_DB}"
        )


def mask_secret(value: str) -> str:
    """Маскирует секрет, оставляя видимыми только последние 4 символа.

    Пустая строка → ``<пусто>``. Короткие значения (≤4) маскируются целиком,
    чтобы не раскрывать их полностью в логах и отчёте.
    """
    if not value:
        return "<пусто>"
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


# Глобальный синглтон конфигурации.
settings = Settings()
