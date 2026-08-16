# Образ прогона исторического реплея (Этап 7.4).
#
# Собирается из того же базового образа и тех же закреплённых зависимостей, что
# и продакшн-сервисы: новых внешних библиотек этап не вводит (§12.1 ТЗ).
# Контейнер поднимается ТОЛЬКО вручную, профилем backtest, и ограничен по
# ресурсам в docker-compose.yml — продакшн-стек работает на том же сервере.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY backtest ./backtest
COPY scripts ./scripts

# По умолчанию — полный прогон по конфигурации. Зонд запускается явной
# командой: docker compose --profile backtest run --rm backtest \
#     python scripts/probe_history_depth.py
CMD ["python", "-m", "backtest.run", "--config", "backtest/.env.backtest"]
