"""Общие настройки для тестов.

Пароль БД обязателен для инстанцирования Settings — задаём дефолт
до импорта любого модуля приложения при сборе тестов.
"""

import os

os.environ.setdefault("POSTGRES_PASSWORD", "test_password")
