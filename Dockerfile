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
# Пакет backtest нужен продакшн-образу с Этапа 8.2: суточный пересчёт целей
# (src/risk_main.py) догружает свежий край часовых свечей ТЕМ ЖЕ загрузчиком
# (backtest/loader.py), которым история грузилась изначально. Второй загрузчик
# «для целей» означал бы две разные трактовки одних и тех же данных биржи.
# На поведение остальных сервисов копирование не влияет: они этот пакет
# не импортируют.
COPY backtest/ ./backtest/

# По умолчанию запускаем точку входа приложения.
CMD ["python", "-m", "src.main"]
