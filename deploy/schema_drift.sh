#!/usr/bin/env bash
# Расхождение схемы сервера и кода (§8 ТЗ 8.10).
#
# ЧТО ОН ДЕЛАЕТ. Разворачивает ЧИСТУЮ базу во ВРЕМЕННОМ контейнере, применяет к
# ней db/init.sql и все миграции по порядку, снимает состав колонок и сравнивает
# его с РАБОЧЕЙ базой по именам и типам. Печатает расхождения таблицей.
#
# ЧЕГО ОН НЕ ДЕЛАЕТ — И ЭТО ГЛАВНОЕ. ОН НИЧЕГО НЕ ИСПРАВЛЯЕТ (§8 ТЗ дословно:
# «НИЧЕГО НЕ ИСПРАВЛЯТЬ — только показать»). Он не выполняет ни одного ALTER, ни
# одного DROP и ни одной миграции против рабочей базы: рабочая база читается
# ТОЛЬКО запросом к information_schema. Временный контейнер удаляется в конце,
# своего тома не создаёт и порта наружу не публикует.
#
# ЗАЧЕМ ЭТО НУЖНО. На сервере у signal_evaluations десять колонок, а в
# репозитории девять: лишняя horizon_h smallint появилась вне миграций. Пока
# расхождение не названо, любая следующая миграция пишется вслепую — и однажды
# упадёт на сервере, пройдя на стенде. Скрипт превращает «кажется, там что-то не
# так» в список из трёх видов строк.
#
# ТРИ ВИДА НАХОДОК, и они означают РАЗНОЕ:
#   [лишнее]   колонка есть в рабочей базе, но её нет ни в init.sql, ни в
#              миграциях. Так выглядит правка, сделанная руками мимо миграций;
#   [не применено] колонка есть в коде, но её нет в рабочей базе. Так выглядит
#              миграция, которую забыли применить;
#   [тип]      колонка есть и там, и там, но объявлена разными типами.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/schema_drift.sh
#
# КОДЫ ВОЗВРАТА: 0 — сравнение выполнено (расхождения могли быть найдены и
# напечатаны — это не сбой скрипта); 2 — выполнить сравнение НЕ УДАЛОСЬ
# (нет docker, не поднялась временная база, недоступна рабочая). Расхождение
# кодом возврата не сигнализируется намеренно: скрипт показывает, а решение,
# что с этим делать, принимает человек.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
# Образ берётся тот же, что у рабочей базы: сравнение схем на разных мажорных
# версиях PostgreSQL показывало бы разницу версий вперемешку с разницей кода.
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
TMP_CONTAINER="agent-trade-schema-drift-$$"
TMP_PASSWORD="drift-$$"
WORK_DIR="$(mktemp -d)"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

cleanup() {
    docker rm -f "${TMP_CONTAINER}" >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "=============================================================================="
echo " РАСХОЖДЕНИЕ СХЕМЫ: рабочая база против кода репозитория (§8 ТЗ 8.10)"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог стека: ${APP_DIR}"
echo " Эталон: db/init.sql + все db/migrations/*.sql (кроме *_rollback.sql)"
echo " НИЧЕГО НЕ ИСПРАВЛЯЕТСЯ. Только чтение рабочей базы."
echo "=============================================================================="

command -v docker >/dev/null 2>&1 || { echo "  docker не найден — сравнить нечем"; exit 2; }

# --- Запрос состава колонок. ОДИН И ТОТ ЖЕ для обеих баз ---------------------
# Сравниваются имена и типы, как требует §8. Схема public: временная база иных
# схем не содержит, а в рабочей всё прикладное лежит там же.
COLUMNS_QUERY="SELECT table_name || '|' || column_name || '|' || data_type
               FROM information_schema.columns
               WHERE table_schema = 'public'
               ORDER BY table_name, column_name;"

# --- 1. Эталон: чистая база из init.sql и миграций ---------------------------
echo
echo "── 1. Разворачиваю эталонную базу во временном контейнере ────────────────"
if ! docker run -d --rm --name "${TMP_CONTAINER}" \
        -e POSTGRES_USER="${DB_USER}" \
        -e POSTGRES_PASSWORD="${TMP_PASSWORD}" \
        -e POSTGRES_DB="${DB_NAME}" \
        "${PG_IMAGE}" >/dev/null 2>&1; then
    echo "  Временный контейнер не запустился (образ ${PG_IMAGE} недоступен?)"
    exit 2
fi

ready=0
for _ in $(seq 1 60); do
    if docker exec "${TMP_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" \
        >/dev/null 2>&1; then
        ready=1; break
    fi
    sleep 1
done
if [[ "${ready}" != "1" ]]; then
    echo "  Временная база не поднялась за 60 секунд"
    exit 2
fi
echo "  временный контейнер: ${TMP_CONTAINER} (${PG_IMAGE})"

apply() {  # $1 = путь к файлу SQL
    docker exec -i "${TMP_CONTAINER}" \
        psql -v ON_ERROR_STOP=1 -q -U "${DB_USER}" -d "${DB_NAME}" < "$1" >/dev/null 2>&1
}

if ! apply "${APP_DIR}/db/init.sql"; then
    echo "  db/init.sql не применился к чистой базе — это находка сама по себе"
    exit 2
fi
echo "  применён db/init.sql"

applied=0
for migration in $(ls "${APP_DIR}"/db/migrations/*.sql 2>/dev/null | sort); do
    case "${migration}" in
        *_rollback.sql) continue ;;
    esac
    if apply "${migration}"; then
        applied=$((applied + 1))
    else
        # Миграция, не применяющаяся к ЧИСТОЙ базе, — расхождение того же рода,
        # что ищет скрипт, только в другую сторону. Молчать о ней нельзя.
        echo "  🔴 миграция не применилась к чистой базе: $(basename "${migration}")"
    fi
done
echo "  применено миграций: ${applied}"

docker exec "${TMP_CONTAINER}" psql -X -t -A -q -U "${DB_USER}" -d "${DB_NAME}" \
    -c "${COLUMNS_QUERY}" 2>/dev/null | sed '/^$/d' | sort > "${WORK_DIR}/reference.txt"

# --- 2. Рабочая база: ТОЛЬКО ЧТЕНИЕ ------------------------------------------
echo
echo "── 2. Читаю состав колонок рабочей базы (только чтение) ──────────────────"
docker compose exec -T postgres \
    psql -X -t -A -q -U "${DB_USER}" -d "${DB_NAME}" -c "${COLUMNS_QUERY}" 2>/dev/null \
    | sed '/^$/d' | sort > "${WORK_DIR}/working.txt"

ref_n=$(wc -l < "${WORK_DIR}/reference.txt" | tr -d ' ')
work_n=$(wc -l < "${WORK_DIR}/working.txt" | tr -d ' ')
echo "  колонок в эталоне: ${ref_n}"
echo "  колонок в рабочей: ${work_n}"
if [[ "${work_n}" == "0" ]]; then
    echo "  Рабочая база недоступна (контейнер postgres не запущен?) — сравнивать не с чем"
    exit 2
fi

# --- 3. Сравнение -------------------------------------------------------------
echo
echo "── 3. Расхождения ────────────────────────────────────────────────────────"
awk -F'|' '
    FNR == NR { ref[$1 "|" $2] = $3; next }
    {
        key = $1 "|" $2
        seen[key] = 1
        if (!(key in ref)) {
            printf "  [лишнее]       %-26s %-28s %s\n", $1, $2, $3
            extra++
        } else if (ref[key] != $3) {
            printf "  [тип]          %-26s %-28s рабочая: %s / эталон: %s\n", \
                   $1, $2, $3, ref[key]
            typed++
        }
    }
    END {
        for (key in ref) {
            if (!(key in seen)) {
                split(key, part, "|")
                printf "  [не применено] %-26s %-28s %s\n", part[1], part[2], ref[key]
                missing++
            }
        }
        printf "\nИТОГ: лишнего в рабочей базе — %d; не применено из кода — %d; ", \
               extra + 0, missing + 0
        printf "тип различается — %d\n", typed + 0
        if (extra + missing + typed == 0) {
            print "\nРасхождений нет: рабочая база соответствует init.sql и миграциям."
        } else {
            print "\nНИЧЕГО НЕ ИСПРАВЛЕНО И НЕ БУДЕТ (§8 ТЗ): скрипт только показывает."
            print "Строку [лишнее] чинит не он: колонка, появившаяся мимо миграций,"
            print "удаляется отдельным решением человека — она может использоваться."
            print "Строку [не применено] чинит применение соответствующей миграции."
        }
    }
' "${WORK_DIR}/reference.txt" "${WORK_DIR}/working.txt"

echo
echo "=============================================================================="
echo " Сравнение выполнено. Рабочая база не изменялась."
echo "=============================================================================="
exit 0
