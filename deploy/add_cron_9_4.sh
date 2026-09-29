#!/usr/bin/env bash
#
# Добавляет строки Этапа 9.4 в /etc/cron.d/agent-trade на УЖЕ работающем сервере
# (установщик заново не гоняют). Идемпотентен: строка, которая уже есть, второй
# раз не добавляется — именно ручное дописывание поверх установленного давало
# повторы (Этап 8.7 §5).
#
# Запуск: sudo bash deploy/add_cron_9_4.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
APP_USER="${APP_USER:-agent}"
CRON_FILE="/etc/cron.d/agent-trade"

[[ -f "$CRON_FILE" ]] || { echo "ОШИБКА: $CRON_FILE не найден." >&2; exit 1; }

add_line() {  # $1 = комментарий, $2 = строка cron
    if grep -qF -- "$2" "$CRON_FILE"; then
        echo "уже есть: $2"
    else
        printf '%s\n%s\n' "$1" "$2" >> "$CRON_FILE"
        echo "добавлено: $2"
    fi
}

add_line "# Этап 9.4 A.2 Очистка кэша сборки Docker — воскресенье 04:00 UTC." \
    "0 4 * * 0 ${APP_USER} ${APP_DIR}/scripts/docker_gc.sh >/dev/null 2>&1"
add_line "# Этап 9.4 C.3 Недельная выгрузка копии наружу — воскресенье 04:30 UTC, после docker_gc; от root (rclone.conf)." \
    "30 4 * * 0 root ${APP_DIR}/scripts/remote_backup.sh >/dev/null 2>&1"

chmod 644 "$CRON_FILE"
echo "--- $CRON_FILE (строки этапа 9.4) ---"
grep -n -E "docker_gc|remote_backup" "$CRON_FILE"
