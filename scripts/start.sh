#!/usr/bin/env sh
# Старт всего стека одной командой, без участия человека (Этап 6.4).
#
#   1) при первом запуске генерирует .env со случайными секретами (idempotent);
#   2) поднимает docker compose.
#
# Использование: ./scripts/start.sh [доп. аргументы docker compose up]
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
sh "$ROOT/scripts/init_env.sh" "$ROOT/.env"
cd "$ROOT"
exec docker compose up -d --build "$@"
