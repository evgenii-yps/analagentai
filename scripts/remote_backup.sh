#!/usr/bin/env bash
#
# Недельная выгрузка резервной копии во внешнее хранилище (Этап 9.4, блок C.3).
#
# ЗАЧЕМ. Копии в /opt/agent-trade/backups лежат на том же диске, что и база, —
# от гибели сервера целиком они не защищают. Свежая копия раз в неделю
# уходит в Google Drive через rclone.
#
# ПОРЯДОК:
#   1. берёт самый свежий файл agenttrade_*.dump.gz из каталога копий;
#   2. rclone copy → REMOTE_BACKUP_REMOTE:REMOTE_BACKUP_PATH;
#   3. сверяет размер файла в хранилище с локальным (rclone lsjson);
#      несовпадение — неудача;
#   4. оставляет в удалённой папке REMOTE_BACKUP_KEEP самых свежих, остальные
#      удаляет (только после успешной сверки и только файлы по шаблону);
#   5. пишет итог в logs/remote_backup.log и в logs/remote_backup.status
#      (строку из него читает суточная сводка);
#   6. при ЛЮБОЙ неудаче — сообщение в Telegram с причиной и код возврата 1.
#      При успехе Telegram не трогает.
#
# REMOTE_BACKUP_ENABLED=false — запись в журнал и код 0, без ошибки.
#
# ПРАВА. Настройки rclone лежат в /root/.config/rclone/rclone.conf (root, 600),
# поэтому задача запускается от root (строка cron с пользователем root). Токен
# доступа в репозиторий не попадает и в журнал не печатается: скрипт ничего из
# rclone.conf не выводит.
#
# Значения из окружения перекрывают .env — так сбой проверяется одним запуском:
#   REMOTE_BACKUP_REMOTE=net_takogo scripts/remote_backup.sh    → Telegram + код 1
#
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
# shellcheck source=scripts/lib_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_common.sh"

BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
LOG_FILE="${REMOTE_BACKUP_LOG:-$APP_DIR/logs/remote_backup.log}"
STATUS_FILE="${REMOTE_BACKUP_STATUS:-$APP_DIR/logs/remote_backup.status}"
export RCLONE_CONFIG="${RCLONE_CONFIG:-/root/.config/rclone/rclone.conf}"

ENABLED="$(env_get REMOTE_BACKUP_ENABLED false)"
REMOTE="$(env_get REMOTE_BACKUP_REMOTE gdrive)"
RPATH="$(env_get REMOTE_BACKUP_PATH AgentTrade/backups)"
KEEP="$(env_get REMOTE_BACKUP_KEEP 8)"

FILE_RE='^agenttrade_[0-9]{4}-[0-9]{2}-[0-9]{2}(\.monthly)?\.dump\.gz$'

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    local line="[$(date -u '+%F %T')] $*"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
}

# Запись состояния для суточной сводки: дата;результат;файл;размер_байт;подробность
write_status() {  # $1=ok|fail $2=файл $3=размер $4=подробность
    printf '%s;%s;%s;%s;%s\n' "$(date -u '+%F')" "$1" "$2" "$3" "$4" > "$STATUS_FILE" 2>/dev/null || true
}

fail() {
    log "ОШИБКА выгрузки: $1"
    write_status fail "${LOCAL_NAME:-}" "${LOCAL_SIZE:-0}" "$1"
    telegram_alert "🔴 <b>Agent Trade — внешняя выгрузка копии не удалась</b>"$'\n'"$1"
    exit 1
}

log "=== remote_backup: старт ==="

if [[ "${ENABLED,,}" != "true" ]]; then
    log "REMOTE_BACKUP_ENABLED=$ENABLED — выгрузка отключена, ничего не делаю (не ошибка)."
    exit 0
fi

is_pos_int "$KEEP" || fail "REMOTE_BACKUP_KEEP='$KEEP' — нужно целое число не меньше 1. Ничего не выгружено и не удалено."
command -v rclone >/dev/null 2>&1 || fail "rclone не установлен (deploy/install_rclone.sh)."
[[ -r "$RCLONE_CONFIG" ]] || fail "нет файла настроек rclone: $RCLONE_CONFIG (авторизация Google Drive не выполнена)."
[[ -d "$BACKUP_DIR" ]] || fail "каталог копий $BACKUP_DIR не найден."

# 1. Самый свежий файл. Порядок по имени: в нём дата.
LOCAL_NAME="$(ls -1 "$BACKUP_DIR" | grep -E "$FILE_RE" | sort | tail -n 1 || true)"
[[ -n "$LOCAL_NAME" ]] || fail "в $BACKUP_DIR нет ни одной копии agenttrade_*.dump.gz."
LOCAL_SIZE="$(stat -c %s "$BACKUP_DIR/$LOCAL_NAME")"
DEST="${REMOTE}:${RPATH}"
log "Выгружаю $LOCAL_NAME ($LOCAL_SIZE байт) → $DEST"

# 2. Копирование. Вывод rclone не содержит токена; ошибки идут в журнал.
copy_out="$(rclone copy "$BACKUP_DIR/$LOCAL_NAME" "$DEST" 2>&1)"
rc=$?
if (( rc != 0 )); then
    reason="rclone copy завершился кодом $rc: $(echo "$copy_out" | tail -n 2 | tr '\n' ' ')"
    fail "$reason"
fi

# 3. Сверка размера по фактическому листингу хранилища.
list_json="$(rclone lsjson "$DEST" --files-only 2>&1)"
rc=$?
(( rc == 0 )) || fail "rclone lsjson завершился кодом $rc: $(echo "$list_json" | tail -n 2 | tr '\n' ' ')"

remote_size="$(printf '%s' "$list_json" | python3 -c '
import json, sys
name = sys.argv[1]
for item in json.load(sys.stdin):
    if item.get("Name") == name:
        print(item.get("Size", -1))
        break
else:
    print(-1)
' "$LOCAL_NAME" 2>/dev/null || echo -1)"

if [[ "$remote_size" != "$LOCAL_SIZE" ]]; then
    fail "размер в хранилище ($remote_size) не совпал с локальным ($LOCAL_SIZE) для $LOCAL_NAME."
fi
log "Размер в хранилище совпал с локальным: $remote_size байт."

# 4. Ротация в хранилище: только файлы по шаблону, только после успешной сверки.
mapfile -t old < <(printf '%s' "$list_json" | python3 -c '
import json, re, sys
pattern = re.compile(sys.argv[1])
names = sorted((i["Name"] for i in json.load(sys.stdin) if pattern.match(i.get("Name", ""))), reverse=True)
print("\n".join(names[int(sys.argv[2]):]))
' "$FILE_RE" "$KEEP")
for f in "${old[@]:-}"; do
    [[ -n "$f" ]] || continue
    if rclone deletefile "$DEST/$f" >/dev/null 2>&1; then
        log "Удалён старый файл в хранилище: $f"
    else
        fail "не удалось удалить старый файл в хранилище: $f (выгрузка нового прошла)."
    fi
done

write_status ok "$LOCAL_NAME" "$LOCAL_SIZE" "хранится не более $KEEP"
log "Готово: выгрузка $LOCAL_NAME прошла, хранится не более $KEEP копий."
