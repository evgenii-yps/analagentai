#!/usr/bin/env bash
#
# Agent Trade — ЭТАП 9.12: «резкое падение + удержание» — выбор на старой истории, подтверждение
# на новой (ТОЛЬКО ЧТЕНИЕ). Ни одно решение системы не меняется.
#
# Что делает: SQL-блок 00 (охват; analysis/sql/9_11/00_scope.sql), выгрузка часового ряда в CSV
# (analysis/sql/9_11/01_series.sql), расчёт hold_9_12.py (блоки 01–04: выбор на первой половине,
# подтверждение на второй, тест сдвигом, сетка целиком), вердикт verdict_9_12.py (блок 05). Вывод —
# на экран и в файл отчёта. Строки «@@…» (машинные факты) в отчёт не идут: они уводятся в файл фактов
# $OUT_DIR/facts_9_12_ГГГГММДД.txt; ряд — в $OUT_DIR/series_9_12_ГГГГММДД.csv.
#
# Чего НЕ делает (ТЗ 9.12, раздел 1): не пишет в базу (роль agenttrade_ro, только SELECT, сессия
# read-only в каждом SQL-файле); не меняет src/, .env, compose, миграции; не перезапускает контейнеры;
# не ставит пакеты: bash, стандартные утилиты, python3 (только стандартная библиотека) и
# docker compose exec -T postgres psql; не печатает пароль роли (общий фильтр redact() из lib_ro.sh).
#
# ТРЕБУЕТСЯ миграция 032 (право agenttrade_ro читать backtest.candles) — она применена в Этапе 9.10.
#
# Запуск (на сервере, из любого каталога):
#   sudo bash /opt/agent-trade/analysis/run_9_12.sh
#
# Отчёт: $OUT_DIR/report_9_12_ГГГГММДД.txt, не длиннее 200 строк. Повторный запуск в тот же день
# перезаписывает файлы. Все блоки считаются на ОДИН момент замера (AS_OF, по умолчанию — текущее
# время хоста); задать вручную: AS_OF='2026-10-19 12:00:00+00'.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
OUT_DIR="${OUT_DIR:-$APP_DIR/analysis_out}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
SQL_DIR="$LIB_DIR/sql/9_11"   # данные те же, что в 9.11 (ТЗ 9.12, §1)
RUN_DATE="$(date -u +%Y%m%d)"
AS_OF="${AS_OF:-$(date -u '+%Y-%m-%d %H:%M:%S+00')}"
OUT_FILE="$OUT_DIR/report_9_12_${RUN_DATE}.txt"
FACTS_FILE="$OUT_DIR/facts_9_12_${RUN_DATE}.txt"
SERIES_FILE="$OUT_DIR/series_9_12_${RUN_DATE}.csv"
MAX_REPORT_LINES=200   # владелец пересылает отчёт архитектору целиком (ТЗ 9.12, §9)
PYTHON="${PYTHON:-python3}"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

[[ -r "$LIB_DIR/lib_ro.sh" ]] || die "не найден $LIB_DIR/lib_ro.sh"
# shellcheck source=analysis/lib_ro.sh
source "$LIB_DIR/lib_ro.sh"
# ON_ERROR_STOP: ошибка SQL даёт ненулевой код возврата psql и попадает в итог прогона.
PSQL_VARS=(-v ON_ERROR_STOP=1 -v "as_of=$AS_OF")

[[ -d "$APP_DIR" ]]  || die "не найден каталог приложения $APP_DIR (задайте APP_DIR=…)"
[[ -d "$SQL_DIR" ]]  || die "не найден каталог с запросами $SQL_DIR"
cd "$APP_DIR"        || die "не удалось перейти в $APP_DIR"
mkdir -p "$OUT_DIR"  || die "не удалось создать каталог вывода $OUT_DIR"

ro_init   # docker compose и имя БД / пароль роли из .env (lib_ro.sh)

# Один запрос вне файла блока — только проверка прав и снимок числа строк; вывод без заголовков.
psql_c() {
    case "$CONN_MODE" in
        socket) psql_socket -tA -c "$1" ;;
        passwd) psql_passwd -tA -c "$1" ;;
    esac
}

# Число строк в таблицах, в которые замер писать не должен (проверяет deploy/verify_9_12.sh).
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

# Выгрузка ряда: стандартный вывод psql — в CSV, сообщения об ошибках — в отчёт.
run_series_block() {
    local err rc
    err="$(mktemp)"
    # В файл попадают только заголовок и строки данных: любое постороннее сообщение psql в стандартном
    # выводе (предупреждение, уведомление) не должно ломать CSV.
    psql_run "$SQL_DIR/01_series.sql" 2> "$err" \
        | awk '/^token,open_time_utc,/ || /^[A-Za-z0-9]+,[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+,/' \
        > "$SERIES_FILE"
    rc="${PIPESTATUS[0]}"
    if [[ $rc -ne 0 ]]; then
        cat "$err"
        rm -f "$err"
        return "$rc"
    fi
    rm -f "$err"
    awk -F, 'NR > 1 { c[$1]++ } END { for (k in c) print k, c[k] }' "$SERIES_FILE" | sort \
        | awk 'BEGIN { s = "" } { s = s (s == "" ? "" : ", ") $1 " " $2 } END { print "Ряд выгружен в CSV (строк по монетам): " s }'
    return 0
}

main() {
    echo "AGENT TRADE — ЭТАП 9.12: РЕЗКОЕ ПАДЕНИЕ + УДЕРЖАНИЕ, ВЫБОР НА СТАРОЙ ИСТОРИИ (замер, только чтение)"
    echo "Запуск (UTC): $(date -u '+%Y-%m-%d %H:%M:%S'); момент замера: $AS_OF"
    echo "Роль: agenttrade_ro (только SELECT, сессия read-only); контейнеры, .env, src/ и логика не менялись"

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
    local failed=0 rc

    echo
    echo "===== 00 — ДАННЫЕ И ОХВАТ ====="
    run_sql_block 00_scope.sql
    rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "ОШИБКА: блок 00 (00_scope.sql) завершился с кодом $rc."
        failed=$((failed + 1))
    fi

    run_series_block
    rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "ОШИБКА: выгрузка ряда (01_series.sql) завершилась с кодом $rc."
        failed=$((failed + 1))
    fi

    echo
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        echo "ОШИБКА: не найден $PYTHON — блоки 01–05 пропущены (нужен только python3, без пакетов)."
        failed=$((failed + 2))
    elif [[ $failed -ne 0 ]]; then
        echo "ОШИБКА: расчёт 01–04 пропущен — нет данных блока 00 или ряда."
        failed=$((failed + 1))
    else
        "$PYTHON" "$LIB_DIR/hold_9_12.py" --series "$SERIES_FILE" --facts "$FACTS_FILE" \
            --as-of "$AS_OF" --append 2>&1
        rc=$?
        if [[ $rc -ne 0 ]]; then
            echo "ОШИБКА: расчёт 01–04 (hold_9_12.py) завершился с кодом $rc."
            failed=$((failed + 1))
        else
            echo
            echo "===== 05 — ВЕРДИКТ ПО КРИТЕРИЯМ §6 ====="
            "$PYTHON" "$LIB_DIR/verdict_9_12.py" "$FACTS_FILE" 2>&1
            rc=$?
            if [[ $rc -ne 0 ]]; then
                echo "ОШИБКА: вердикт (verdict_9_12.py) завершился с кодом $rc."
                failed=$((failed + 1))
            fi
        fi
    fi

    rows_snapshot after
    echo
    echo "ИТОГ ПРОГОНА: блоков 6, с ошибками $failed; окончание (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
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
echo "Готово. Отчёт: $OUT_FILE ($LINES строк); факты: $FACTS_FILE; ряд: $SERIES_FILE"
if [[ "$LINES" -gt "$MAX_REPORT_LINES" ]]; then
    echo "ВНИМАНИЕ: отчёт длиннее $MAX_REPORT_LINES строк — пересылайте файл целиком и сообщите архитектору."
fi
if [[ "$MAIN_RC" -ne 0 ]]; then
    echo "ВНИМАНИЕ: прогон завершился с ошибкой — расчёты в файле неполные."
fi
exit "$MAIN_RC"
