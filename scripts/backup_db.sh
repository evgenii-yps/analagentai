#!/usr/bin/env bash
#
# Резервное копирование БД PostgreSQL (§8 ТЗ 6.5; правило хранения — Этап 9.4, блок B).
#
# Делает дамп в кастомном формате pg_dump (-Fc) внутри контейнера postgres,
# складывает его в /opt/agent-trade/backups/agenttrade_YYYY-MM-DD.dump и сжимает
# gzip. Результат логируется; при ошибке отправляется сообщение в Telegram.
#
# ПРАВИЛО ХРАНЕНИЯ (Этап 9.4 §5):
#   * суточные копии — хранятся BACKUP_KEEP_DAILY последних;
#   * копия, созданная 1-го числа месяца, называется
#     agenttrade_YYYY-MM-01.monthly.dump.gz, помечается как месячная и под
#     суточный счётчик НЕ попадает; месячных хранится BACKUP_KEEP_MONTHLY;
#   * после удаления старых файлов считается суммарный объём каталога. Если он
#     больше BACKUP_DIR_MAX_GB — в Telegram уходит сообщение с фактическим
#     объёмом. СВЕРХ ПРАВИЛА НИЧЕГО НЕ УДАЛЯЕТСЯ: превышение означает, что
#     выросла база, и это повод для решения человека, а не для тихого удаления.
#
# Файлы вне шаблона agenttrade_<дата>[.monthly].dump.gz ротацией по числу не
# затрагиваются никогда. Исключение одно: ручные копии перед рискованными
# операциями backups/manual/pre_*.dump.gz (Этап 9.4, D.3) хранятся 30 суток по
# времени изменения файла и потом удаляются — «вне ротации на 30 суток».
#
# Запускается из cron под пользователем ``agent`` (входит в группу docker),
# ежедневно в 03:10 UTC.
#
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
# shellcheck source=scripts/lib_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib_common.sh"

BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
# Значения ЗАДАЮТСЯ в .env явно (Этап 9.4 §5). Умолчания ниже — страховка на
# случай пропажи строки, заданным значением они не считаются.
BACKUP_KEEP_DAILY="$(env_get BACKUP_KEEP_DAILY 7)"
BACKUP_KEEP_MONTHLY="$(env_get BACKUP_KEEP_MONTHLY 12)"
BACKUP_DIR_MAX_GB="$(env_get BACKUP_DIR_MAX_GB 6)"

# Для проверки правила на конкретной дате (тесты, ручная проверка 1-го числа).
TODAY="${BACKUP_TODAY:-$(date -u '+%Y-%m-%d')}"

DAILY_RE='^agenttrade_[0-9]{4}-[0-9]{2}-[0-9]{2}\.dump\.gz$'
MONTHLY_RE='^agenttrade_[0-9]{4}-[0-9]{2}-01\.monthly\.dump\.gz$'

log() { echo "[$(date -u '+%F %T')] $*"; }

fail() {
    log "ОШИБКА: $1"
    telegram_alert "🔴 <b>Agent Trade — бэкап БД не удался</b>"$'\n'"$1"
    exit 1
}

cd "$APP_DIR" || fail "каталог $APP_DIR не найден"

# Настройки хранения проверяются ДО создания дампа и ДО любого удаления:
# испорченное значение (0, «abc») не должно дойти до ротации. Значение 0 стёрло
# бы вместе со старыми и только что созданную копию.
for name in BACKUP_KEEP_DAILY BACKUP_KEEP_MONTHLY BACKUP_DIR_MAX_GB; do
    is_pos_int "${!name}" || fail "$name='${!name}' — нужно целое число не меньше 1. Ротация не выполнялась, файлы не тронуты."
done

# Значения POSTGRES_USER/POSTGRES_DB берём из .env.
PG_USER="$(env_get POSTGRES_USER agenttrade)"
PG_DB="$(env_get POSTGRES_DB agenttrade)"

mkdir -p "$BACKUP_DIR"

# 1-го числа копия помечается месячной и живёт по своему счётчику.
if [[ "$TODAY" == *-01 ]]; then
    NAME="agenttrade_${TODAY}.monthly.dump.gz"
    KIND="месячная"
else
    NAME="agenttrade_${TODAY}.dump.gz"
    KIND="суточная"
fi
OUT="$BACKUP_DIR/$NAME"
TMP="$BACKUP_DIR/agenttrade_${TODAY}.dump.part"

log "Старт бэкапа БД '$PG_DB' (pg_dump -Fc, $KIND) → $OUT"

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

# Ротация. Порядок — по ИМЕНИ (в нём стоит дата), а не по времени изменения:
# копирование или восстановление файла время меняет, имя — нет.
rotate() {  # $1 = регулярное выражение имени, $2 = сколько хранить, $3 = подпись
    local re="$1" keep="$2" label="$3" f
    local -a old
    mapfile -t old < <(ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$re" | sort -r | tail -n +"$((keep + 1))")
    for f in "${old[@]:-}"; do
        [[ -n "$f" ]] || continue
        rm -f "$BACKUP_DIR/$f" && log "Удалён старый бэкап ($label): $f"
    done
}
rotate "$DAILY_RE" "$BACKUP_KEEP_DAILY" "суточный"
rotate "$MONTHLY_RE" "$BACKUP_KEEP_MONTHLY" "месячный"

# Ручные копии перед рискованными операциями: 30 суток, потом удаляются.
MANUAL_KEEP_DAYS=30
if [[ -d "$BACKUP_DIR/manual" ]]; then
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        rm -f "$f" && log "Удалена ручная копия старше ${MANUAL_KEEP_DAYS} суток: $f"
    done < <(find "$BACKUP_DIR/manual" -maxdepth 1 -type f -name 'pre_*.dump.gz' -mtime "+${MANUAL_KEEP_DAYS}")
fi

n_daily="$(ls -1 "$BACKUP_DIR" | grep -c -E "$DAILY_RE" || true)"
n_monthly="$(ls -1 "$BACKUP_DIR" | grep -c -E "$MONTHLY_RE" || true)"

# Объём каталога после ротации. Считается ВЕСЬ каталог, включая ручные копии:
# место на диске занимают и они.
dir_bytes="$(du -sb "$BACKUP_DIR" | cut -f1)"
dir_gb="$(awk -v b="$dir_bytes" 'BEGIN {printf "%.2f", b / 1073741824}')"
log "Хранится: суточных $n_daily (правило $BACKUP_KEEP_DAILY), месячных $n_monthly (правило $BACKUP_KEEP_MONTHLY). Объём каталога $BACKUP_DIR: ${dir_gb} ГБ (потолок BACKUP_DIR_MAX_GB=${BACKUP_DIR_MAX_GB})."

if awk -v b="$dir_bytes" -v m="$BACKUP_DIR_MAX_GB" 'BEGIN {exit !(b > m * 1073741824)}'; then
    log "ПРЕВЫШЕНИЕ: ${dir_gb} ГБ > ${BACKUP_DIR_MAX_GB} ГБ. Ничего сверх правила не удаляется — нужно решение человека."
    telegram_alert "🟠 <b>Agent Trade — каталог бэкапов больше потолка</b>"$'\n'"Занято ${dir_gb} ГБ при потолке BACKUP_DIR_MAX_GB=${BACKUP_DIR_MAX_GB} ГБ (суточных ${n_daily}, месячных ${n_monthly}). Скорее всего выросла база. Ничего лишнего не удалено — нужно ваше решение."
fi

log "Готово."
