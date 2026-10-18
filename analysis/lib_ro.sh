#!/usr/bin/env bash
#
# Agent Trade — общая часть скриптов замера: подключение к БД ролью agenttrade_ro
# и фильтр, не пускающий пароль в вывод. Вынесено из analysis/run_7_1.sh (Этап 9.9),
# поведение не менялось.
#
# Файл подключается командой ``source`` и сам ничего не выполняет.
#
# Что ждёт от вызывающего скрипта:
#   APP_DIR     — каталог приложения (там лежат .env и docker-compose.yml);
#   PSQL_VARS   — по желанию: массив пар ``-v имя=значение``, которые попадают в psql
#                 (например, PSQL_VARS=(-v "target_version=4"));
#   die()       — по желанию: если не определена, берётся простая из этого файла.
#
# Что даёт:
#   RO_PW, DB_NAME, CONN_MODE, DC          — переменные состояния;
#   ro_init                                 — находит docker compose и читает .env;
#   redact                                  — фильтр потока: пароль → ***;
#   detect_conn_mode, psql_run <файл.sql>   — выбор способа подключения и запуск файла.
#
# Только чтение: роль agenttrade_ro прав записи не имеет, а SQL-файлы замеров сами
# переводят сессию в режим read-only.

RO_PW=""
DB_NAME="agenttrade"
CONN_MODE=""
DC=()
PSQL_VARS=()

if ! declare -F die >/dev/null 2>&1; then
    die() { echo "ОШИБКА: $*" >&2; exit 1; }
fi

# ---------------------------------------------------------------------------
# Фильтр-страховка: если пароль каким-либо образом попал в поток (например, в
# текст ошибки psql), он заменяется на ***. Чистый awk по подстроке (index/substr),
# без регулярных выражений — спецсимволы пароля не могут его сломать.
# ---------------------------------------------------------------------------
redact() {
    if [[ -n "$RO_PW" ]]; then
        # Пароль передаётся в awk через окружение (ENVIRON), а не через -v:
        # ключ -v обрабатывает escape-последовательности, и пароль с обратной
        # косой чертой исказился бы, перестав совпадать с искомой подстрокой.
        RO_PW_ENV="$RO_PW" awk '
        BEGIN { pw = ENVIRON["RO_PW_ENV"] }
        {
            line = $0; out = ""
            while (length(pw) > 0) {
                i = index(line, pw)
                if (i == 0) break
                out = out substr(line, 1, i - 1) "***"
                line = substr(line, i + length(pw))
            }
            print out line
        }'
    else
        cat
    fi
}

# ---------------------------------------------------------------------------
# docker compose (v2) или docker-compose (v1) — что есть на сервере; имя БД и пароль
# роли только на чтение из .env (значения никуда не печатаются).
# ---------------------------------------------------------------------------
ro_init() {
    if docker compose version >/dev/null 2>&1; then
        DC=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        DC=(docker-compose)
    else
        die "не найден ни 'docker compose', ни 'docker-compose'"
    fi

    local v
    if [[ -r "$APP_DIR/.env" ]]; then
        v="$(grep -E '^POSTGRES_DB=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r"' | awk '{print $1}')"
        [[ -n "${v:-}" ]] && DB_NAME="$v"
        RO_PW="$(grep -E '^POSTGRES_RO_PASSWORD=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r"' | awk '{print $1}')"
    fi
}

# ---------------------------------------------------------------------------
# Два способа подключения ролью agenttrade_ro:
#   socket — внутри контейнера через unix-сокет (в образе postgres для локальных
#            подключений действует trust): пароль вообще не нужен;
#   passwd — через 127.0.0.1 с паролем из .env (запасной путь).
# Роль в обоих случаях одна и та же и прав записи не имеет.
# ---------------------------------------------------------------------------
psql_socket() { "${DC[@]}" exec -T postgres psql -X -q \
                    ${PSQL_VARS[@]+"${PSQL_VARS[@]}"} \
                    -U agenttrade_ro -d "$DB_NAME" "$@"; }
psql_passwd() { "${DC[@]}" exec -T -e PGPASSWORD="$RO_PW" postgres \
                    psql -X -q ${PSQL_VARS[@]+"${PSQL_VARS[@]}"} \
                    -h 127.0.0.1 -U agenttrade_ro -d "$DB_NAME" "$@"; }

psql_run() {  # $1 — путь к .sql-файлу на ХОСТЕ (подаётся в psql через stdin)
    case "$CONN_MODE" in
        socket) psql_socket < "$1" ;;
        passwd) psql_passwd < "$1" ;;
        *)      echo "ОШИБКА: не установлен способ подключения к БД"; return 1 ;;
    esac
}

detect_conn_mode() {
    if psql_socket -c 'SELECT 1;' >/dev/null 2>&1; then
        CONN_MODE="socket"
    elif [[ -n "$RO_PW" ]] && psql_passwd -c 'SELECT 1;' >/dev/null 2>&1; then
        CONN_MODE="passwd"
    else
        CONN_MODE=""
    fi
}
