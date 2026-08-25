# Образ прогона исторического реплея (Этап 7.4).
#
# Собирается из того же базового образа и тех же закреплённых зависимостей, что
# и продакшн-сервисы: новых внешних библиотек этап не вводит (§12.1 ТЗ).
# Контейнер поднимается ТОЛЬКО вручную, профилем backtest, и ограничен по
# ресурсам в docker-compose.yml — продакшн-стек работает на том же сервере.
FROM python:3.12-slim

# PYTHONPATH=/app — дефект D-6. Без него `python -m backtest.run` падает с
# «No module named backtest», и приходится каждый раз добавлять -e PYTHONPATH=/app
# руками. Рабочий каталог сам по себе в sys.path не попадает, когда команда
# приходит из docker compose run.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# CODE_REV ломает кэш слоёв с кодом — дефект D-9. Наблюдалось, что сборка брала
# COPY backtest ./backtest из кэша при переписанном коде, и в контейнере
# работала старая версия. Передавайте ревизию при сборке:
#     docker compose --profile backtest build --no-cache \
#         --build-arg CODE_REV=$(git rev-parse --short HEAD) backtest
# ПРАВИЛО: после любого изменения кода — пересборка с --no-cache. CODE_REV — это
# страховка на случай, если правило забыли, а не замена ему.
ARG CODE_REV=dev
ENV CODE_REV=$CODE_REV

COPY src ./src
COPY backtest ./backtest
COPY scripts ./scripts

# По умолчанию — полный прогон по конфигурации. Зонд запускается явной
# командой: docker compose --profile backtest run --rm backtest \
#     python scripts/probe_history_depth.py
CMD ["python", "-m", "backtest.run", "--config", "backtest/.env.backtest"]
