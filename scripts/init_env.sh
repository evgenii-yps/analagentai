#!/usr/bin/env sh
# Первичная инициализация окружения (Этап 6.4).
#
# Если .env отсутствует — создаёт его из .env.example и подставляет СЛУЧАЙНЫЕ
# секреты (POSTGRES_PASSWORD, REDIS_PASSWORD), сгенерированные КРИПТОГРАФИЧЕСКИМ
# генератором (openssl / python secrets / /dev/urandom — не random). Файл
# создаётся с правами 600. Идемпотентен: существующий .env не трогает, поэтому
# при перезапуске/автозапуске на сервере пароли не меняются.
#
# Запуск (обычно через scripts/start.sh): sh scripts/init_env.sh [путь_к_.env]
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="${1:-$ROOT/.env}"
EXAMPLE="$ROOT/.env.example"

if [ -f "$ENV_FILE" ]; then
    echo "init_env: $ENV_FILE уже существует — секреты не меняются."
    exit 0
fi

if [ ! -f "$EXAMPLE" ]; then
    echo "init_env: не найден шаблон $EXAMPLE" >&2
    exit 1
fi

# 24 байта энтропии → 48 hex-символов. Только [0-9a-f]: безопасно для sed и .env.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(24))'
    else
        # /dev/urandom — криптографический источник ядра.
        od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
    fi
}

PG_PASS=$(gen_secret)
REDIS_PASS=$(gen_secret)

umask 077                       # новый файл создаётся сразу без доступа для чужих
cp "$EXAMPLE" "$ENV_FILE"
sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PASS}|" "$ENV_FILE"
sed -i.bak "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PASS}|" "$ENV_FILE"
rm -f "${ENV_FILE}.bak"
chmod 600 "$ENV_FILE"
echo "init_env: создан $ENV_FILE со сгенерированными паролями (chmod 600)."
