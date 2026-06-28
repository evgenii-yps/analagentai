"""Smoke-тест: конфиг импортируется и собирает корректный DSN."""

import os

# Пароль обязателен для инстанцирования Settings — задаём до импорта модуля.
os.environ.setdefault("POSTGRES_PASSWORD", "test_password")


def test_settings_import_and_dsn() -> None:
    """Конфиг импортируется, дефолты на месте, pg_dsn собирается корректно."""
    from src.core.config import settings

    assert settings.POSTGRES_USER == "agenttrade"
    assert settings.POSTGRES_DB == "agenttrade"
    assert settings.PG_PORT == 5432
    assert settings.pg_dsn.startswith("postgresql://")
    # Пароль из окружения должен попасть в собранный DSN.
    assert settings.POSTGRES_PASSWORD in settings.pg_dsn
