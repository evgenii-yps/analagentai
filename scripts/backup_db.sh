#!/usr/bin/env bash
#
# Резервное копирование БД PostgreSQL (§8 ТЗ 6.5).
#
# Делает сжатый дамп базы через контейнер postgres и складывает его в
# каталог бэкапов. Старые бэкапы автоматически ротируются (хранится
# последние BACKUP_KEEP штук). Скрипт устойчив к ошибкам сети/сервисов:
# при неудаче он логирует проблему и завершается с ненулевым кодом, но
# НЕ трогает уже существующие бэкапы.
#
# Запускается из cron под пользователем ``agent`` (входит в группу docker).
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"   # сколько последних дампов хранить

log() { echo "[$(date '+%F %T')] $*"; }

cd "$APP_DIR" || { log "ОШИБКА: каталог $APP_DIR не найден"; exit 1; }

# Значения POSTGRES_USER/POSTGRES_DB берём из .env (без инлайновых комментариев).
if [[ -f "$APP_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; . "$APP_DIR/.env"; set +a
fi
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"

mkdir -p "$BACKUP_DIR"

STAMP="$(date -u '+%Y%m%d-%H%M%S')"
OUT="$BACKUP_DIR/${PG_DB}-${STAMP}.sql.gz"
TMP="$OUT.part"

log "Старт бэкапа БД '$PG_DB' → $OUT"

# Дамп внутри контейнера postgres; поток сжимаем на хосте.
# -T: без псевдо-TTY (важно для cron).
if docker compose exec -T postgres pg_dump -U "$PG_USER" "$PG_DB" | gzip -c > "$TMP"; then
    mv -f "$TMP" "$OUT"
    SIZE="$(du -h "$OUT" | cut -f1)"
    log "Бэкап готов: $OUT ($SIZE)"
else
    rc=$?
    rm -f "$TMP"
    log "ОШИБКА: не удалось создать дамп БД (код $rc). Существующие бэкапы не тронуты."
    exit "$rc"
fi

# Ротация: удаляем всё, кроме последних BACKUP_KEEP файлов.
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/"${PG_DB}"-*.sql.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))")
if [[ ${#OLD[@]} -gt 0 ]]; then
    for f in "${OLD[@]}"; do
        rm -f "$f" && log "Удалён старый бэкап: $f"
    done
fi

log "Готово. Хранится не более $BACKUP_KEEP последних бэкапов в $BACKUP_DIR."
