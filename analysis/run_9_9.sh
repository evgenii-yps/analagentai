#!/usr/bin/env bash
#
# Agent Trade — ЭТАП 9.9: итог версии логики 7 (замер, ТОЛЬКО ЧТЕНИЕ).
#
# Что делает: последовательно исполняет десять SQL-файлов из analysis/sql/9_9, печатает
# вывод на экран и одновременно пишет его в файл отчёта. Числа в отчёт переносит
# архитектор по шаблону analysis/REPORT_9_9.md.
#
# Чего НЕ делает (ТЗ 9.9, раздел 1):
#   * не пишет в базу: подключение ролью agenttrade_ro (только SELECT), и каждый
#     SQL-файл дополнительно переводит сессию в режим read-only;
#   * не меняет src/, .env, docker-compose.yml, миграции и не перезапускает контейнеры;
#   * не устанавливает пакеты: только bash, стандартные утилиты и
#     docker compose exec -T postgres psql (правило D-3);
#   * не печатает пароль роли: общий фильтр redact() — в analysis/lib_ro.sh.
#
# Запуск (на сервере, из любого каталога):
#   sudo bash /opt/agent-trade/analysis/run_9_9.sh
#
# Отчёт: $OUT_DIR/report_9_9_ГГГГММДД.txt, не длиннее 300 строк. Повторный запуск в
# тот же день перезаписывает файл. Все блоки считаются на ОДИН момент замера (AS_OF,
# по умолчанию — текущее время хоста); задать вручную: AS_OF='2026-10-18 12:00:00+00'.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
OUT_DIR="${OUT_DIR:-$APP_DIR/analysis_out}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
SQL_DIR="$LIB_DIR/sql/9_9"
RUN_DATE="$(date -u +%Y%m%d)"
AS_OF="${AS_OF:-$(date -u '+%Y-%m-%d %H:%M:%S+00')}"
OUT_FILE="$OUT_DIR/report_9_9_${RUN_DATE}.txt"
MAX_REPORT_LINES=300   # владелец пересылает отчёт архитектору целиком (ТЗ 9.9, раздел 2)

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

# Блоки: файл|заголовок.
BLOCKS=(
    "00_scope.sql|00 — ОХВАТ"
    "01_profile_closed.sql|01 — ПРОФИЛЬ ЗАКРЫТЫХ СДЕЛОК"
    "02_open_mtm.sql|02 — ОТКРЫТЫЕ ПО ТЕКУЩЕЙ ЦЕНЕ И ОБЩИЙ ИТОГ"
    "03_by_token.sql|03 — ПО МОНЕТАМ"
    "04_exits_and_time.sql|04 — ВЫХОДЫ И ВРЕМЯ В СДЕЛКЕ"
    "05_mae_mfe.sql|05 — ХОД ЦЕНЫ ВНУТРИ СДЕЛКИ"
    "06_time_in_market.sql|06 — ВРЕМЯ В РЫНКЕ"
    "07_entry_vs_random.sql|07 — ВХОД СИСТЕМЫ ПРОТИВ СЛУЧАЙНОГО МОМЕНТА"
    "08_weekly.sql|08 — ПО НЕДЕЛЯМ"
    "09_demo_vs_virtual.sql|09 — ДЕМО ПРОТИВ ВИРТУАЛЬНОГО"
)

main() {
    echo "AGENT TRADE — ЭТАП 9.9: ИТОГ ВЕРСИИ ЛОГИКИ 7 (замер, только чтение)"
    echo "Запуск (UTC): $(date -u '+%Y-%m-%d %H:%M:%S'); момент замера: $AS_OF"
    echo "Роль: agenttrade_ro (только SELECT, сессия read-only); контейнеры, .env и src/ не менялись"

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

    local failed=0 block file title rc
    for block in "${BLOCKS[@]}"; do
        file="${block%%|*}"
        title="${block#*|}"
        echo
        echo "===== $title ====="
        if [[ ! -r "$SQL_DIR/$file" ]]; then
            echo "ОШИБКА: файл $SQL_DIR/$file не найден или недоступен — блок пропущен."
            failed=$((failed + 1))
            continue
        fi
        psql_run "$SQL_DIR/$file" 2>&1
        rc=$?
        if [[ $rc -ne 0 ]]; then
            echo "ОШИБКА: блок «$title» ($file) завершился с кодом $rc. Работа продолжается."
            failed=$((failed + 1))
        fi
    done

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
echo "Готово. Отчёт: $OUT_FILE ($LINES строк)"
if [[ "$LINES" -gt "$MAX_REPORT_LINES" ]]; then
    echo "ВНИМАНИЕ: отчёт длиннее $MAX_REPORT_LINES строк — пересылайте файл целиком и сообщите архитектору."
fi
if [[ "$MAIN_RC" -ne 0 ]]; then
    echo "ВНИМАНИЕ: прогон завершился с ошибкой — расчёты в файле неполные."
fi
exit "$MAIN_RC"
