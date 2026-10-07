#!/usr/bin/env bash
#
# Общие функции shell-скриптов регламентных задач (Этап 9.4): чтение значений
# из окружения / .env и отправка сообщения в Telegram.
#
# Подключается командой ``source "$(dirname "$0")/lib_common.sh"`` ПОСЛЕ
# присвоения APP_DIR. Сам ничего не выполняет.

# env_get KEY DEFAULT — значение из окружения процесса, иначе из $APP_DIR/.env,
# иначе DEFAULT. Хвостовой комментарий (`KEY=7   # пояснение`) и пробелы
# отбрасываются: в .env проекта комментарии в конце строки — обычное дело.
#
# Окружение процесса ПЕРЕКРЫВАЕТ .env: так проверка сбоя (например, неверное
# имя хранилища в REMOTE_BACKUP_REMOTE) делается одним запуском, без правки .env.
env_get() {
    local key="$1" default="${2:-}" val=""
    if [[ -n "${!key+x}" ]]; then
        printf '%s' "${!key}"
        return 0
    fi
    if [[ -f "$APP_DIR/.env" ]]; then
        val="$(grep -E "^${key}=" "$APP_DIR/.env" | tail -n 1 | cut -d= -f2- || true)"
        val="$(printf '%s' "$val" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    fi
    printf '%s' "${val:-$default}"
}

# is_pos_int VALUE — целое число >= 1. Нуль и пустое значение недопустимы:
# «хранить 0 копий» удалило бы и только что созданную.
is_pos_int() { [[ "${1:-}" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 )); }

# telegram_recipients — получатели сообщений по одному в строке: TELEGRAM_CHAT_ID, затем
# BOT_ALLOWED_CHAT_IDS (через запятую) в порядке записи; пробелы срезаются, пустые и
# повторы выбрасываются. То же правило, что Settings.telegram_recipients (Этап 9.7.1).
telegram_recipients() {
    local -a parts
    local part chat seen=","
    IFS=',' read -ra parts <<< "$(env_get TELEGRAM_CHAT_ID ""),$(env_get BOT_ALLOWED_CHAT_IDS "")"
    for part in "${parts[@]}"; do
        chat="$(printf '%s' "$part" | tr -d '[:space:]')"
        [[ -z "$chat" || "$seen" == *",${chat},"* ]] && continue
        seen+="${chat},"
        printf '%s\n' "$chat"
    done
}

# telegram_alert TEXT — сообщение каждому получателю напрямую через curl, без контейнеров.
# Сбой одного получателя не мешает остальным. Отсутствие токена/получателей не считается
# ошибкой: скрипт всё равно пишет в журнал.
telegram_alert() {
    local token chat text="$1"
    local -a chats
    token="$(env_get TELEGRAM_BOT_TOKEN "")"
    mapfile -t chats < <(telegram_recipients)
    [[ -z "$token" || ${#chats[@]} -eq 0 ]] && return 0
    for chat in "${chats[@]}"; do
        curl -fsS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
            --data-urlencode "chat_id=${chat}" \
            --data-urlencode "text=${text}" \
            --data-urlencode "parse_mode=HTML" >/dev/null 2>&1 || true
    done
}
