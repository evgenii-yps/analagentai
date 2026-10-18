#!/usr/bin/env bash
#
# Agent Trade — ЭТАП 7.1: диагностика предсказательной способности ядра.
#
# Что делает: последовательно исполняет шесть SQL-файлов из analysis/sql,
# печатает вывод на экран и одновременно пишет его в файл отчёта.
#
# Чего НЕ делает (ТЗ 7.1, разделы 2 и 14):
#   * не меняет ни одной строки в src/ и ни одного параметра .env;
#   * не перезапускает, не пересобирает и не останавливает контейнеры;
#   * не пишет в базу: подключение ролью agenttrade_ro (только SELECT),
#     и дополнительно каждая сессия переводится в режим read-only;
#   * не устанавливает пакеты: только bash, стандартные утилиты и
#     docker compose exec -T postgres psql (правило D-3).
#
# Запуск (на сервере, из любого каталога):
#   sudo bash /opt/agent-trade/analysis/run_7_1.sh
#
# Повторный запуск безопасен: файл отчёта за текущую дату перезаписывается.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
OUT_DIR="${OUT_DIR:-$APP_DIR/analysis_out}"
SQL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/sql"
RUN_DATE="$(date -u +%Y%m%d)"
# Целевая версия логики для расчётов 1–5 и 7 (Этап 7.3, Блок D). По умолчанию 4 —
# режим, начатый развёртыванием 7.3. Для повторения измерения Этапа 7.1 задать
# TARGET_LOGIC_VERSION=1. Расчёт 6 всегда идёт по всем версиям сразу.
TARGET_LOGIC_VERSION="${TARGET_LOGIC_VERSION:-4}"
OUT_FILE="$OUT_DIR/report_7_1_v${TARGET_LOGIC_VERSION}_${RUN_DATE}.txt"
MAX_LINES="${MAX_LINES:-2000}"   # порог, после которого отчёт делится на две части

# Пароль роли только на чтение. Читается из .env, НИКОГДА не печатается и не
# попадает в файл отчёта: значение хранится только в переменной, а весь поток
# вывода дополнительно проходит через фильтр redact() ниже.
RO_PW=""
DB_NAME="agenttrade"
CONN_MODE=""

die() { echo "ОШИБКА: $*" >&2; exit 1; }

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
# Подготовка окружения.
# ---------------------------------------------------------------------------
[[ -d "$APP_DIR" ]]  || die "не найден каталог приложения $APP_DIR (задайте APP_DIR=…)"
[[ -d "$SQL_DIR" ]]  || die "не найден каталог с запросами $SQL_DIR"
cd "$APP_DIR"        || die "не удалось перейти в $APP_DIR"
mkdir -p "$OUT_DIR"  || die "не удалось создать каталог вывода $OUT_DIR"

# docker compose (v2) или docker-compose (v1) — что есть на сервере.
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    die "не найден ни 'docker compose', ни 'docker-compose'"
fi

# Имя БД и пароль роли только на чтение из .env (значения никуда не печатаются).
if [[ -r "$APP_DIR/.env" ]]; then
    _v="$(grep -E '^POSTGRES_DB=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r"' | awk '{print $1}')"
    [[ -n "${_v:-}" ]] && DB_NAME="$_v"
    RO_PW="$(grep -E '^POSTGRES_RO_PASSWORD=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r"' | awk '{print $1}')"
    unset _v
fi

# ---------------------------------------------------------------------------
# Два способа подключения ролью agenttrade_ro:
#   socket — внутри контейнера через unix-сокет (в образе postgres для локальных
#            подключений действует trust): пароль вообще не нужен;
#   passwd — через 127.0.0.1 с паролем из .env (запасной путь).
# Роль в обоих случаях одна и та же и прав записи не имеет.
# ---------------------------------------------------------------------------
psql_socket() { "${DC[@]}" exec -T postgres psql -X -q \
                    -v target_version="$TARGET_LOGIC_VERSION" \
                    -U agenttrade_ro -d "$DB_NAME" "$@"; }
psql_passwd() { "${DC[@]}" exec -T -e PGPASSWORD="$RO_PW" postgres \
                    psql -X -q -v target_version="$TARGET_LOGIC_VERSION" \
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

# ---------------------------------------------------------------------------
# Блоки расчётов: файл|заголовок.
# ---------------------------------------------------------------------------
BLOCKS=(
    "00_schema.sql|СВЕРКА СХЕМЫ БД"
    "01_baseline.sql|РАСЧЁТ 1: БАЗОВАЯ ЛИНИЯ И ОБЩАЯ РЕЗУЛЬТАТИВНОСТЬ"
    "02_agents.sql|РАСЧЁТ 2: ВКЛАД КАЖДОГО АГЕНТА"
    "03_formula.sql|РАСЧЁТ 3: ИНФОРМАТИВНОСТЬ БАЛЛА И СОГЛАСОВАННОСТИ"
    "04_inertia.sql|РАСЧЁТ 4: ЧАСТОТА ОБНОВЛЕНИЯ ВХОДНЫХ ДАННЫХ"
    "05_correlation.sql|РАСЧЁТ 5: КОРРЕЛЯЦИЯ МЕЖДУ АГЕНТАМИ"
    "06_silence.sql|РАСЧЁТ 6: ПОЧЕМУ СИСТЕМА ЗАМОЛЧАЛА"
    "07_compare.sql|РАСЧЁТ 7: ЦЕЛЕВАЯ ВЕРСИЯ ПРОТИВ ВЕРСИИ 1"
)

# ---------------------------------------------------------------------------
# Основная часть. Весь вывод (и stdout, и stderr) идёт через redact в tee.
# ---------------------------------------------------------------------------
main() {
    echo "==================================================================="
    echo " AGENT TRADE — ЭТАП 7.1: ДИАГНОСТИКА ПРЕДСКАЗАТЕЛЬНОЙ СПОСОБНОСТИ"
    echo "==================================================================="
    echo " Дата запуска (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo " Каталог приложения: $APP_DIR"
    echo " Запросы:            $SQL_DIR"
    echo " Целевая версия:     logic_version = $TARGET_LOGIC_VERSION (расчёты 1–5 и 7)"
    echo " Роль подключения:   agenttrade_ro (только SELECT)"
    echo " Режим записи:       отсутствует (default_transaction_read_only = on)"
    echo " Контейнеры:         не перезапускались, .env и src/ не изменялись"
    echo "==================================================================="

    detect_conn_mode
    case "$CONN_MODE" in
        socket) echo " Подключение: через unix-сокет контейнера postgres (пароль не требуется)" ;;
        passwd) echo " Подключение: через 127.0.0.1 с паролем роли из .env (значение не выводится)" ;;
        *)
            echo
            echo "ОШИБКА: не удалось подключиться к БД ролью agenttrade_ro."
            echo "Проверьте: 1) контейнер postgres запущен  —  docker compose ps postgres"
            echo "           2) роль существует             —  она создаётся сервисом бота (Этап 6.7)"
            echo "           3) в $APP_DIR/.env заполнен POSTGRES_RO_PASSWORD"
            echo "Расчёты не выполнены."
            return 1
            ;;
    esac
    echo

    local failed=0
    local block file title rc
    for block in "${BLOCKS[@]}"; do
        file="${block%%|*}"
        title="${block#*|}"
        echo
        echo "===== $title ====="
        echo "(файл запросов: analysis/sql/$file)"
        echo
        if [[ ! -r "$SQL_DIR/$file" ]]; then
            echo "ОШИБКА: файл $SQL_DIR/$file не найден или недоступен для чтения — блок пропущен."
            failed=$((failed + 1))
            continue
        fi
        psql_run "$SQL_DIR/$file" 2>&1
        rc=$?
        if [[ $rc -ne 0 ]]; then
            echo
            echo "ОШИБКА: блок «$title» ($file) завершился с кодом $rc. Работа продолжается."
            failed=$((failed + 1))
        fi
    done

    echo
    echo "==================================================================="
    echo " ИТОГ ПРОГОНА: блоков всего ${#BLOCKS[@]}, с ошибками $failed"
    echo " Время окончания (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "==================================================================="
    return 0
}

# Старые части предыдущего прогона за эту же дату удаляем — иначе можно спутать
# половинки разных запусков (идемпотентность).
rm -f "${OUT_FILE%.txt}_part"*.txt 2>/dev/null

main 2>&1 | redact | tee "$OUT_FILE"
MAIN_RC="${PIPESTATUS[0]}"

# ---------------------------------------------------------------------------
# Контроль: пароля в файле отчёта быть не должно ни при каких обстоятельствах.
# ---------------------------------------------------------------------------
if [[ -n "$RO_PW" ]] && grep -qF -- "$RO_PW" "$OUT_FILE" 2>/dev/null; then
    echo "ВНИМАНИЕ: в отчёте обнаружено значение пароля — файл удалён, сообщите разработчику."
    rm -f "$OUT_FILE"
    exit 2
fi

# ---------------------------------------------------------------------------
# Финал: путь, размер и — при необходимости — деление на две части.
# ---------------------------------------------------------------------------
if [[ ! -s "$OUT_FILE" ]]; then
    echo "ОШИБКА: файл отчёта пуст: $OUT_FILE"
    exit 1
fi

LINES="$(wc -l < "$OUT_FILE" | tr -d ' ')"
SIZE="$(du -h "$OUT_FILE" | cut -f1)"

echo
if [[ "$LINES" -gt "$MAX_LINES" ]]; then
    split -n l/2 -d --additional-suffix=.txt "$OUT_FILE" "${OUT_FILE%.txt}_part" 2>/dev/null
    if [[ -s "${OUT_FILE%.txt}_part00.txt" && -s "${OUT_FILE%.txt}_part01.txt" ]]; then
        echo "Вывод получился длинным ($LINES строк) — он разделён на две части:"
        echo "  ${OUT_FILE%.txt}_part00.txt ($(du -h "${OUT_FILE%.txt}_part00.txt" | cut -f1), $(wc -l < "${OUT_FILE%.txt}_part00.txt" | tr -d ' ') строк)"
        echo "  ${OUT_FILE%.txt}_part01.txt ($(du -h "${OUT_FILE%.txt}_part01.txt" | cut -f1), $(wc -l < "${OUT_FILE%.txt}_part01.txt" | tr -d ' ') строк)"
        echo "Полный отчёт целиком: $OUT_FILE ($SIZE, $LINES строк)"
    else
        echo "Готово. Отчёт: $OUT_FILE ($SIZE, $LINES строк)"
    fi
else
    echo "Готово. Отчёт: $OUT_FILE ($SIZE, $LINES строк)"
fi

if [[ "$MAIN_RC" -ne 0 ]]; then
    echo "ВНИМАНИЕ: прогон завершился с ошибкой — расчёты в файле неполные."
fi

exit "$MAIN_RC"
