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

    @property
    def pg_dsn(self) -> str:
        """Собирает строку подключения (DSN) к PostgreSQL."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.POSTGRES_DB}"
        )


# Глобальный синглтон конфигурации.
settings = Settings()
