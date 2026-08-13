#!/usr/bin/env bash
# Этап 7.1 — запуск всех запросов диагностики ролью agenttrade_ro (только чтение).
#
# ВЫПОЛНЯТЬ ТОЛЬКО НА СЕРВЕРЕ, где поднят стек (есть docker + том pg_data).
# Ничего не изменяет: agenttrade_ro имеет только SELECT; контейнеры не трогаются
# (используется лишь `docker compose exec` для чтения). .env и src/ не затрагиваются.
#
# Использование:
#   cd /path/to/project && bash analysis/run_7_1.sh
# Результаты пишутся в analysis/results/<файл>.out и в общий сводный лог.

set -uo pipefail

PG_USER="${PG_RO_USER:-agenttrade_ro}"
PG_DB="${POSTGRES_DB:-agenttrade}"
SVC="${PG_SERVICE:-postgres}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/results"
mkdir -p "$OUT"

run() {
    local f="$1"
    local name; name="$(basename "$f" .sql)"
    echo "===== $name ====="
    # -T: без TTY (обязательно). Запрос подаётся через stdin — многострочный SQL допустим.
    docker compose exec -T "$SVC" psql -U "$PG_USER" -d "$PG_DB" \
        -v ON_ERROR_STOP=0 --pset=pager=off < "$f" | tee "$OUT/$name.out"
    echo
}

echo "# Этап 7.1 — прогон $(date -u +%Y-%m-%dT%H:%M:%SZ) роль=$PG_USER db=$PG_DB"
for f in "$HERE"/sql/*.sql; do
    run "$f"
done
echo "# Готово. Результаты: $OUT/"
