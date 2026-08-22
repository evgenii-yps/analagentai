# Образ приложения Agent Trade (Этап 1).
FROM python:3.12-slim

# Не пишем .pyc и не буферизуем stdout (логи сразу видны).
# PYTHONPATH=/app — дефект D-6: без него модули, запускаемые как `python -m ...`
# из docker compose run, не находят пакеты проекта («No module named backtest»).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Сначала зависимости — для эффективного кэширования слоёв.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем исходный код.
COPY src/ ./src/

# По умолчанию запускаем точку входа приложения.
CMD ["python", "-m", "src.main"]
