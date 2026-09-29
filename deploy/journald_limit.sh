#!/usr/bin/env bash
#
# Ограничение журнала systemd: SystemMaxUse=200M (Этап 9.4, блок A; решение заказчика).
#
# Причина: 29.09.2026 журнал systemd занимал 575 МБ и ничем не ограничивался.
#
# Правит /etc/systemd/journald.conf идемпотентно: раскомментирует или заменяет
# строку SystemMaxUse, а если её нет — добавляет в раздел [Journal]. Перезапускает
# systemd-journald (контейнеры Docker это не затрагивает: их журналы ведёт
# json-file) и сразу подрезает уже накопленное до 200 МБ.
#
# Запуск (root): sudo bash deploy/journald_limit.sh
# Для проверок: JOURNALD_CONF=<путь> NO_RESTART=1.
#
set -euo pipefail

CONF="${JOURNALD_CONF:-/etc/systemd/journald.conf}"
LIMIT="200M"

[[ -f "$CONF" ]] || { echo "ОШИБКА: $CONF не найден." >&2; exit 1; }

if grep -qE "^[[:space:]]*SystemMaxUse=${LIMIT}[[:space:]]*$" "$CONF"; then
    echo "$CONF: SystemMaxUse=${LIMIT} уже задан, изменений нет."
else
    cp -a "$CONF" "${CONF}.bak-$(date -u +%Y%m%d%H%M%S)"
    if grep -qE "^[[:space:]]*#?[[:space:]]*SystemMaxUse=" "$CONF"; then
        # Первое вхождение (закомментированное или нет) заменяется, остальные
        # активные — комментируются: последнее присваивание перекрыло бы наше.
        awk -v limit="$LIMIT" '
            /^[[:space:]]*#?[[:space:]]*SystemMaxUse=/ {
                if (!done) { print "SystemMaxUse=" limit; done = 1 }
                else if ($0 !~ /^[[:space:]]*#/) { print "#" $0 }
                else { print }
                next
            }
            { print }' "$CONF" > "${CONF}.new"
        mv "${CONF}.new" "$CONF"
    elif grep -qE "^\[Journal\]" "$CONF"; then
        sed -i "/^\[Journal\]/a SystemMaxUse=${LIMIT}" "$CONF"
    else
        printf '\n[Journal]\nSystemMaxUse=%s\n' "$LIMIT" >> "$CONF"
    fi
    echo "$CONF обновлён: SystemMaxUse=${LIMIT}."
fi

grep -nE "^[[:space:]]*SystemMaxUse=" "$CONF"

if [[ -z "${NO_RESTART:-}" ]]; then
    systemctl restart systemd-journald
    journalctl --vacuum-size="$LIMIT"
    journalctl --disk-usage
fi
