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
    # Дефолта НЕТ намеренно: константный пароль в коде недопустим для сервера с
    # публичным IP. Секрет генерируется криптографически при первом старте
    # (scripts/init_env.sh) и передаётся через .env. Без него приложение падает
    # на старте с понятной ошибкой валидации (field required).
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str = "agenttrade"
    PG_HOST: str = "postgres"
    PG_PORT: int = 5432

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    # Пароль Redis (defense in depth). Генерируется тем же scripts/init_env.sh.
    # Пусто → подключение без пароля (порт при этом не публикуется наружу).
    REDIS_PASSWORD: str = ""

    # --- Логирование ---
    LOG_LEVEL: str = "INFO"

    # --- Сбор данных (Этап 2) ---
    EXCHANGE: str = "okx"               # пилотная биржа (Binance/Bybit отдают 451 из EU)
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
