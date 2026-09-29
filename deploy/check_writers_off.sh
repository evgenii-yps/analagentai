#!/usr/bin/env bash
#
# Механическая проверка: писатели замеров (baseline_main, trailing_main) нигде не
# запланированы и не запущены (Этап 9.4, блок D, шаг 1). Заменяет «посмотрите
# глазами» на команду с однозначным ответом.
#
#   sudo bash deploy/check_writers_off.sh          # весь сервер
#
# Ищет НЕзакомментированные строки в /etc/cron.d, /etc/crontab, /etc/cron.{hourly,
# daily,weekly,monthly}, личных crontab ВСЕХ пользователей (/var/spool/cron/crontabs),
# таймеры systemd и запущенные процессы. Строки с «#» в начале не считаются.
#
# Код возврата: 0 — писателей нет нигде; 1 — найдены (строки напечатаны);
# 2 — запуск не от root (личные crontab прочитать нельзя: «ничего не найдено» было бы ложью).
#
# Для проверок: CHECK_DIRS="каталог1 файл2" CHECK_ALLOW_NONROOT=1 CHECK_SKIP_SYSTEM=1.
set -uo pipefail

PATTERN='baseline_main|trailing_main'
DIRS=${CHECK_DIRS:-"/etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly /var/spool/cron/crontabs"}

if [[ $EUID -ne 0 && -z "${CHECK_ALLOW_NONROOT:-}" ]]; then
    echo "ОШИБКА: запускайте через sudo — личные crontab читает только root." >&2
    exit 2
fi

found=0
# shellcheck disable=SC2086
hits="$(grep -rHnE "$PATTERN" $DIRS 2>/dev/null | grep -vE ':[0-9]+:[[:space:]]*#' || true)"
if [[ -n "$hits" ]]; then
    echo "АКТИВНЫЕ строки расписания:"
    echo "$hits"
    found=1
fi

if [[ -z "${CHECK_SKIP_SYSTEM:-}" ]]; then
    timers="$(systemctl list-timers --all --no-pager 2>/dev/null | grep -E 'baseline|trailing' || true)"
    if [[ -n "$timers" ]]; then
        echo "ТАЙМЕРЫ systemd:"; echo "$timers"; found=1
    fi
    procs="$(pgrep -af "$PATTERN" | grep -v "check_writers_off" || true)"
    if [[ -n "$procs" ]]; then
        echo "ЗАПУЩЕННЫЕ процессы:"; echo "$procs"; found=1
    fi
fi

if (( found == 0 )); then
    echo "OK: писателей замеров нет ни в одном расписании, таймере и процессе."
fi
exit "$found"
