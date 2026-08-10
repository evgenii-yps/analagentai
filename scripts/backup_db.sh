#!/usr/bin/env bash
#
# Резервное копирование БД PostgreSQL (§8 ТЗ 6.5).
#
# Делает дамп в кастомном формате pg_dump (-Fc) внутри контейнера postgres,
# складывает его в /opt/agent-trade/backups/agenttrade_YYYY-MM-DD.dump и сжимает
# gzip. Хранятся последние 14 файлов, более старые удаляются. Результат
# логируется; при ошибке отправляется сообщение в Telegram.
#
# Запускается из cron под пользователем ``agent`` (входит в группу docker),
# ежедневно в 03:10 UTC.
#
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"   # сколько последних дампов хранить

log() { echo "[$(date -u '+%F %T')] $*"; }

# Отправляет сообщение об ошибке в Telegram (напрямую, без контейнеров).
telegram_alert() {
    local token="" chat="" text="$1"
    if [[ -f "$APP_DIR/.env" ]]; then
        token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$APP_DIR/.env" | cut -d= -f2-)"
        chat="$(grep -E '^TELEGRAM_CHAT_ID=' "$APP_DIR/.env" | cut -d= -f2-)"
    fi
    [[ -z "$token" || -z "$chat" ]] && return 0
    curl -fsS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=HTML" >/dev/null 2>&1 || true
}

fail() {
    log "ОШИБКА: $1"
    telegram_alert "🔴 <b>Agent Trade — бэкап БД не удался</b>%0A$1"
    exit 1
}

cd "$APP_DIR" || fail "каталог $APP_DIR не найден"

# Значения POSTGRES_USER/POSTGRES_DB берём из .env.
PG_USER="$(grep -E '^POSTGRES_USER=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2-)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2-)"
PG_USER="${PG_USER:-agenttrade}"
PG_DB="${PG_DB:-agenttrade}"

mkdir -p "$BACKUP_DIR"

DATE="$(date -u '+%Y-%m-%d')"
DUMP="$BACKUP_DIR/agenttrade_${DATE}.dump"
OUT="${DUMP}.gz"
TMP="${DUMP}.part"

log "Старт бэкапа БД '$PG_DB' (pg_dump -Fc) → $OUT"

# Дамп в кастомном формате внутри контейнера postgres (-T: без псевдо-TTY).
if ! docker compose exec -T postgres pg_dump -U "$PG_USER" -Fc "$PG_DB" > "$TMP" 2>/dev/null; then
    rm -f "$TMP"
    fail "не удалось создать дамп БД (pg_dump). Существующие бэкапы не тронуты."
fi

# Сжимаем и атомарно переименовываем.
if ! gzip -c "$TMP" > "${OUT}.part"; then
    rm -f "$TMP" "${OUT}.part"
    fail "не удалось сжать дамп (gzip)."
fi
rm -f "$TMP"
mv -f "${OUT}.part" "$OUT"

SIZE="$(du -h "$OUT" | cut -f1)"
log "Бэкап готов: $OUT ($SIZE)"

# Ротация: оставляем последние BACKUP_KEEP файлов.
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/agenttrade_*.dump.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))")
for f in "${OLD[@]:-}"; do
    [[ -n "$f" ]] || continue
    rm -f "$f" && log "Удалён старый бэкап: $f"
done

log "Готово. Хранится не более $BACKUP_KEEP последних бэкапов в $BACKUP_DIR."
