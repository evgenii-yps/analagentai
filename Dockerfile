# Образ приложения Agent Trade (Этап 1).
FROM python:3.12-slim

# Не пишем .pyc и не буферизуем stdout (логи сразу видны).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала зависимости — для эффективного кэширования слоёв.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем исходный код и служебные скрипты (аудит бирж запускается из образа).
COPY src/ ./src/
COPY scripts/ ./scripts/

# По умолчанию запускаем точку входа приложения.
CMD ["python", "-m", "src.main"]
