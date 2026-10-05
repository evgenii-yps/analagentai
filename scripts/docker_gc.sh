#!/usr/bin/env bash
#
# Очистка кэша сборки Docker (Этап 9.4, блок A.2).
#
# Причина: 29.09.2026 кэш сборки занимал 8.494 ГБ (81 слой, активных ноль) —
# он копился на каждой пересборке `build --no-cache` и сам не очищается никогда.
#
# Что делает (только стандартные средства docker, без сторонних зависимостей):
#   1. docker builder prune -af --filter until=24h — кэш старше суток. Сборки
#      последних суток остаются, чтобы повторная пересборка после развёртывания
#      не шла с нуля.
#   2. docker image prune -f — ТОЛЬКО висячие образы. Флаг -a здесь ЗАПРЕЩЁН:
#      он удалил бы образы служб профиля tools (export, calibration, backtest),
#      которые не запущены постоянно, и сломал бы ночную выгрузку.
#   3. Печатает освобождённый объём в logs/docker_gc.log (и в stdout).
#
# Запуск: cron, воскресенье 04:00 UTC (строка в /etc/cron.d/agent-trade), и
# вручную после каждой пересборки образов (см. docs/STAGE_9_4_REPORT.md).
# Пользователь — agent (входит в группу docker).
#
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
LOG_FILE="${DOCKER_GC_LOG:-$APP_DIR/logs/docker_gc.log}"

mkdir -p "$(dirname "$LOG_FILE")"

# Всё, что печатает скрипт, уходит и на экран, и в журнал: ручной запуск должен
# показывать результат сразу, а cron — оставлять след.
log() {
    local line
    line="[$(date -u '+%F %T')] $*"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
}

# Занято на корневой ФС, байт (df: колонка Used).
disk_used_bytes() { df -P -B1 / | awk 'NR==2 {print $3}'; }

# Итоговая строка docker prune: «Total:  8.494GB» / «Total reclaimed space: 12MB».
total_line() { grep -i -E '^Total' | tail -n 1; }

log "=== docker_gc: старт ==="
before="$(disk_used_bytes)"

# Шаг 1. Кэш сборки старше суток.
out1="$(docker builder prune -af --filter until=24h 2>&1)"
rc1=$?
if (( rc1 != 0 )); then
    log "ОШИБКА docker builder prune (код $rc1): $(echo "$out1" | tail -n 3 | tr '\n' ' ')"
    exit 1
fi
log "builder prune (старше 24ч): $(echo "$out1" | total_line || true)"

# Шаг 2. Только висячие образы. Без -a — см. шапку.
out2="$(docker image prune -f 2>&1)"
rc2=$?
if (( rc2 != 0 )); then
    log "ОШИБКА docker image prune (код $rc2): $(echo "$out2" | tail -n 3 | tr '\n' ' ')"
    exit 1
fi
log "image prune (висячие): $(echo "$out2" | total_line || true)"

after="$(disk_used_bytes)"
freed=$(( before - after ))
# Занятое место могло вырасти из-за записи базы во время очистки: отрицательное
# число печатается как есть, а не подменяется нулём.
freed_mb=$(( freed / 1024 / 1024 ))
log "Освобождено на диске: ${freed_mb} МБ (занято до: $(( before / 1024 / 1024 )) МБ, после: $(( after / 1024 / 1024 )) МБ)"
log "docker system df: $(docker system df --format '{{.Type}}={{.Size}}' 2>/dev/null | tr '\n' ';')"
log "=== docker_gc: готово ==="
