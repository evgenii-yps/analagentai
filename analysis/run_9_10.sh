#!/usr/bin/env bash
#
# Agent Trade — ЭТАП 9.10: замер «вето на покупку при нисходящем тренде BTC» и полнота
# минутных свечей (ТОЛЬКО ЧТЕНИЕ). Ни одно решение системы не меняется.
#
# Что делает: исполняет SQL-блоки из analysis/sql/9_10, два блока на python3 (тест сдвигом —
# analysis/shift_test.py, вердикт — analysis/verdict_9_10.py), печатает вывод на экран и одновременно
# пишет его в файл отчёта. Строки «@@…» из SQL (машинные факты для блоков 05 и 07) в отчёт не идут:
# они уводятся в файл фактов $OUT_DIR/facts_9_10_ГГГГММДД.txt.
#
# Чего НЕ делает (ТЗ 9.10, раздел 1):
#   * не пишет в базу: подключение ролью agenttrade_ro (только SELECT), и каждый SQL-файл
#     дополнительно переводит сессию в режим read-only;
#   * не меняет src/, .env, docker-compose.yml, миграции и не перезапускает контейнеры;
#   * не устанавливает пакеты: bash, стандартные утилиты, python3 (только стандартная
#     библиотека) и docker compose exec -T postgres psql;
#   * не печатает пароль роли: общий фильтр redact() — в analysis/lib_ro.sh.
#
# ТРЕБУЕТСЯ миграция 032 (право роли agenttrade_ro читать backtest.candles):
#   docker compose exec -T postgres psql -U agenttrade -d agenttrade < db/migrations/032_ro_backtest_read.sql
#
# Запуск (на сервере, из любого каталога):
#   sudo bash /opt/agent-trade/analysis/run_9_10.sh
#
# Отчёт: $OUT_DIR/report_9_10_ГГГГММДД.txt, не длиннее 300 строк. Повторный запуск в тот же
# день перезаписывает файл. Все блоки считаются на ОДИН момент замера (AS_OF, по умолчанию —
# текущее время хоста); задать вручную: AS_OF='2026-10-18 12:00:00+00'.
# Срок хранения минутных свечей берётся из RETENTION_1M_DAYS в .env (нет строки — 30).

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
OUT_DIR="${OUT_DIR:-$APP_DIR/analysis_out}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
SQL_DIR="$LIB_DIR/sql/9_10"
RUN_DATE="$(date -u +%Y%m%d)"
AS_OF="${AS_OF:-$(date -u '+%Y-%m-%d %H:%M:%S+00')}"
OUT_FILE="$OUT_DIR/report_9_10_${RUN_DATE}.txt"
FACTS_FILE="$OUT_DIR/facts_9_10_${RUN_DATE}.txt"
MAX_REPORT_LINES=300   # владелец пересылает отчёт архитектору целиком (ТЗ 9.10, раздел 5)
PYTHON="${PYTHON:-python3}"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

[[ -r "$LIB_DIR/lib_ro.sh" ]] || die "не найден $LIB_DIR/lib_ro.sh"
# shellcheck source=analysis/lib_ro.sh
source "$LIB_DIR/lib_ro.sh"

[[ -d "$APP_DIR" ]]  || die "не найден каталог приложения $APP_DIR (задайте APP_DIR=…)"
[[ -d "$SQL_DIR" ]]  || die "не найден каталог с запросами $SQL_DIR"
cd "$APP_DIR"        || die "не удалось перейти в $APP_DIR"
mkdir -p "$OUT_DIR"  || die "не удалось создать каталог вывода $OUT_DIR"

ro_init   # docker compose и имя БД / пароль роли из .env (lib_ro.sh)

# Срок хранения минутных свечей: RETENTION_1M_DAYS из .env; нет строки или значение не число — 30.
RETENTION_DAYS=30
RETENTION_NOTE=""
if [[ -r "$APP_DIR/.env" ]]; then
    v="$(grep -E '^RETENTION_1M_DAYS=' "$APP_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '\r"' | awk '{print $1}')"
    if [[ -n "${v:-}" ]]; then
        if [[ "$v" =~ ^[0-9]+$ && "$v" -ge 2 ]]; then
            RETENTION_DAYS="$v"
        else
            RETENTION_NOTE="значение RETENTION_1M_DAYS='$v' в .env не подходит — взято 30"
        fi
    fi
fi
# ON_ERROR_STOP: ошибка SQL даёт ненулевой код возврата psql и попадает в итог прогона.
PSQL_VARS=(-v ON_ERROR_STOP=1 -v "as_of=$AS_OF" -v "retention_days=$RETENTION_DAYS")

# Блоки: файл|заголовок. «@…» — не SQL, а python-блок (05: тест сдвигом, 07: вердикт).
BLOCKS=(
    "00_data.sql|00 — ДАННЫЕ И КОНТРОЛИ"
    "01_veto_share.sql|01 — ДОЛЯ ДНЕЙ ПОД ВЕТО"
    "02_live_trades.sql|02 — СДЕЛКИ ВЕРСИИ 7: С ВЕТО И БЕЗ"
    "03_weekly.sql|03 — СДЕЛКИ ВЕРСИИ 7 ПО НЕДЕЛЯМ × V1"
    "04_history_returns.sql|04 — ИСТОРИЯ: ДНЕВНАЯ ДОХОДНОСТЬ ПОСЛЕ ФЛАГА"
    "@shift_test|05 — ТЕСТ СДВИГОМ"
    "06_minute_gaps.sql|06 — ПОЛНОТА МИНУТНЫХ СВЕЧЕЙ"
    "@verdict|07 — ВЕРДИКТ ПО КРИТЕРИЯМ §6"
)

# Один запрос вне файла блока — только проверка прав; результат без заголовков.
psql_c() {
    case "$CONN_MODE" in
        socket) psql_socket -tA -c "$1" ;;
        passwd) psql_passwd -tA -c "$1" ;;
    esac
}

# Число строк в таблицах, в которые замер писать не должен (проверяет deploy/verify_9_10.sh):
# снимок до первого блока и после последнего, в файл фактов.
rows_snapshot() {  # $1 — метка: before / after
    local r
    r="$(psql_c "SELECT (SELECT count(*) FROM positions) || '/' || (SELECT count(*) FROM signals) || '/' || (SELECT count(*) FROM demo_orders);" 2>/dev/null | tr -d '[:space:]')"
    echo "@@F|rows_$1|${r:-нет}" >> "$FACTS_FILE"
}

# SQL-блок: строки «@@…» — в файл фактов, остальное — в отчёт. Код возврата — psql.
run_sql_block() {
    psql_run "$SQL_DIR/$1" 2>&1 | awk -v facts="$FACTS_FILE" '
        /^@@/ { print >> facts; next }
        { print }'
    return "${PIPESTATUS[0]}"
}

run_py_block() {  # $1 — @shift_test или @verdict
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        echo "ОШИБКА: не найден $PYTHON — блок пропущен (нужен только python3, без пакетов)."
        return 1
    fi
    case "$1" in
        @shift_test) "$PYTHON" "$LIB_DIR/shift_test.py" "$FACTS_FILE" --append 2>&1 ;;
        @verdict)    "$PYTHON" "$LIB_DIR/verdict_9_10.py" "$FACTS_FILE" 2>&1 ;;
        *) echo "ОШИБКА: неизвестный python-блок $1"; return 1 ;;
    esac
}

main() {
    echo "AGENT TRADE — ЭТАП 9.10: ВЕТО ПРИ НИСХОДЯЩЕМ ТРЕНДЕ BTC И ПОЛНОТА МИНУТНЫХ СВЕЧЕЙ (замер, только чтение)"
    echo "Запуск (UTC): $(date -u '+%Y-%m-%d %H:%M:%S'); момент замера: $AS_OF; RETENTION_1M_DAYS=$RETENTION_DAYS"
    echo "Роль: agenttrade_ro (только SELECT, сессия read-only); контейнеры, .env, src/ и логика не менялись"
    [[ -z "$RETENTION_NOTE" ]] || echo "ВНИМАНИЕ: $RETENTION_NOTE"

    detect_conn_mode
    case "$CONN_MODE" in
        socket) echo "Подключение: unix-сокет контейнера postgres (пароль не требуется)" ;;
        passwd) echo "Подключение: 127.0.0.1 с паролем роли из .env (значение не выводится)" ;;
        *)
            echo
            echo "ОШИБКА: не удалось подключиться к БД ролью agenttrade_ro."
            echo "Проверьте: 1) контейнер postgres запущен  —  docker compose ps postgres"
            echo "           2) роль существует             —  её создаёт сервис бота (Этап 6.7)"
            echo "           3) в $APP_DIR/.env заполнен POSTGRES_RO_PASSWORD"
            echo "Расчёты не выполнены."
            return 1
            ;;
    esac

    local can
    can="$(psql_c "SELECT has_schema_privilege('agenttrade_ro', 'backtest', 'USAGE') AND has_table_privilege('agenttrade_ro', 'backtest.candles', 'SELECT');" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$can" != "t" ]]; then
        echo
        echo "ОШИБКА: у роли agenttrade_ro нет права читать backtest.candles — история свечей недоступна."
        echo "Примените миграцию 032:  cd $APP_DIR && docker compose exec -T postgres psql -U agenttrade -d $DB_NAME < db/migrations/032_ro_backtest_read.sql"
        echo "Расчёты не выполнены."
        return 1
    fi

    : > "$FACTS_FILE"
    rows_snapshot before
    local failed=0 block file title rc
    for block in "${BLOCKS[@]}"; do
        file="${block%%|*}"
        title="${block#*|}"
        echo
        echo "===== $title ====="
        if [[ "$file" == @* ]]; then
            run_py_block "$file"
            rc=$?
        elif [[ ! -r "$SQL_DIR/$file" ]]; then
            echo "ОШИБКА: файл $SQL_DIR/$file не найден или недоступен — блок пропущен."
            failed=$((failed + 1))
            continue
        else
            run_sql_block "$file"
            rc=$?
        fi
        if [[ $rc -ne 0 ]]; then
            echo "ОШИБКА: блок «$title» ($file) завершился с кодом $rc. Работа продолжается."
            failed=$((failed + 1))
        fi
    done

    rows_snapshot after
    echo
    echo "ИТОГ ПРОГОНА: блоков ${#BLOCKS[@]}, с ошибками $failed; окончание (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
    [[ $failed -eq 0 ]]
}

main 2>&1 | redact | tee "$OUT_FILE"
MAIN_RC="${PIPESTATUS[0]}"

# Контроль: пароля в файле отчёта быть не должно ни при каких обстоятельствах.
if [[ -n "$RO_PW" ]] && grep -qF -- "$RO_PW" "$OUT_FILE" 2>/dev/null; then
    echo "ВНИМАНИЕ: в отчёте обнаружено значение пароля — файл удалён, сообщите разработчику."
    rm -f "$OUT_FILE"
    exit 2
fi

LINES="$(wc -l < "$OUT_FILE" | tr -d ' ')"
echo
echo "Готово. Отчёт: $OUT_FILE ($LINES строк); факты: $FACTS_FILE"
if [[ "$LINES" -gt "$MAX_REPORT_LINES" ]]; then
    echo "ВНИМАНИЕ: отчёт длиннее $MAX_REPORT_LINES строк — пересылайте файл целиком и сообщите архитектору."
fi
if [[ "$MAIN_RC" -ne 0 ]]; then
    echo "ВНИМАНИЕ: прогон завершился с ошибкой — расчёты в файле неполные."
fi
exit "$MAIN_RC"
