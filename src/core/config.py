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
    # Дефолт нужен для старта в ноль ручных шагов (стек и автоперезапуск на
    # сервере не должны требовать ручного ввода пароля). В проде переопределяется
    # через .env или реальную переменную окружения; тот же дефолт задан для
    # контейнера postgres в docker-compose, поэтому значения согласованы.
    POSTGRES_PASSWORD: str = "agenttrade"
    POSTGRES_DB: str = "agenttrade"
    PG_HOST: str = "postgres"
    PG_PORT: int = 5432

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

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


# Глобальный синглтон конфигурации.
settings = Settings()
