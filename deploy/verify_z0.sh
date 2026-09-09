#!/usr/bin/env bash
# Проверка ЗАМЕРА 0 (шаг 1 версии 7): история старших таймфреймов.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_z0.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — двух видов, и они РАЗНЫЕ:
#        (откат)      нарушена граница этапа; одной командой не чинится;
#        (устранимо)  развёртывание неполно, но запрещённого не произошло —
#                     печатается ВМЕСТЕ с командой устранения;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# ПЕЧАТАЕТСЯ ТОЛЬКО ФАКТИЧЕСКИ СРАБОТАВШЕЕ, а не справочник всех мыслимых
# причин: это дефект verify_8_8.sh, где человеку предлагалось угадать, какая
# из двенадцати перечисленных причин относится к его случаю.
#
# ПОЧЕМУ ПОИСК В ЖУРНАЛАХ ИДЁТ ПО МАШИНОЧИТАЕМЫМ КЛЮЧАМ. Русский текст в
# журналах хранится экранированными последовательностями Unicode, и grep по
# русским словам не находит ничего (урок diagnose_8_4.sh).
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он не судит о глубине истории: «мало баров» — это
# факт для Замера 1, а не находка проверки. Ни один инструмент по его итогам из
# состава не выбрасывается.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
BT_ENV_FILE="${APP_DIR}/backtest/.env.backtest"

# Замер 0 — ЭТАП ЗАМЕРНЫЙ: версия логики остаётся 6, ни одно решение системы
# не меняется.
LOGIC_VERSION_EXPECTED=6

# Отпечаток решений decision на 323 наборах входов. Значение то же, что в
# verify_9_1.sh и verify_8_10.sh, и в этом весь смысл проверки: Замер 0 не
# менял ни одного решения, поэтому отпечаток обязан остаться прежним.
EXPECTED_DIGEST="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"

# Масштабы этапа. ТОЛЬКО с суффиксом utc: у OKX дневные, недельные и месячные
# свечи без суффикса открываются по гонконгскому времени (UTC+8).
HTF_BARS="'1Dutc','1Wutc','1Mutc'"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
unknown=0
declare -a ROLLBACK_ITEMS=()
declare -a FIX_ITEMS=()
note_block() {
  echo "  🔴 БЛОКИРУЮЩЕЕ (откат):      $*"
  blocking=$((blocking + 1)); ROLLBACK_ITEMS+=("$*")
}
note_fix() {   # $1 = текст, $2 = команда устранения
  echo "  🔴 БЛОКИРУЮЩЕЕ (устранимо):  $1"
  echo "     └ команда: $2"
  blocking=$((blocking + 1)); FIX_ITEMS+=("$1"$'\n'"       $2")
}
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_unk()   { echo "  ⚪ НЕ ПРОВЕРЕНО:    $*"; unknown=$((unknown + 1)); }
note_ok()    { echo "  🟢 $*"; }
info()       { echo "  ℹ  $*"; }

psql_val() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -q -c "$1" 2>/dev/null | tr -d '[:space:]'
}
psql_tbl() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -F "|" -q -c "$1" 2>/dev/null \
    | sed '/^$/d'
}
env_value() {
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=============================================================================="
echo " ПРОВЕРКА ЗАМЕРА 0 — история старших таймфреймов"
echo " $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC), каталог ${APP_DIR}"
echo "=============================================================================="

# ---------------------------------------------------------------------------
echo
echo "── 1. Имена службы и профиля Docker (из docker-compose.yml, не из ТЗ) ─────"
if [[ ! -f "${APP_DIR}/docker-compose.yml" ]]; then
  note_unk "нет docker-compose.yml — имена службы и профиля назвать нечем"
else
  Z0_SERVICE="$(awk '
    /^  [a-z_-]+:/ { svc = $1; sub(":", "", svc) }
    /dockerfile:[[:space:]]*backtest\/Dockerfile/ { print svc; exit }
  ' "${APP_DIR}/docker-compose.yml")"
  Z0_PROFILE="$(awk -v want="${Z0_SERVICE}" '
    /^  [a-z_-]+:/ { svc = $1; sub(":", "", svc) }
    svc == want && /profiles:/ { gsub(/[^a-zA-Z0-9_,-]/, "", $2); print $2; exit }
  ' "${APP_DIR}/docker-compose.yml")"
  if [[ -z "${Z0_SERVICE}" ]]; then
    note_unk "в docker-compose.yml нет службы, собираемой из backtest/Dockerfile"
  else
    info "служба: ${Z0_SERVICE}; профиль: ${Z0_PROFILE:-<нет>}"
    info "запуск: docker compose --profile ${Z0_PROFILE:-backtest} run --rm --no-deps ${Z0_SERVICE} python scripts/probe_htf_depth.py"
    MEM="$(awk -v want="${Z0_SERVICE}" '
      /^  [a-z_-]+:/ { svc = $1; sub(":", "", svc) }
      svc == want && /mem_limit:/ { print $2; exit }
    ' "${APP_DIR}/docker-compose.yml")"
    if [[ "${MEM}" != "1g" ]]; then
      note_block "лимит памяти службы ${Z0_SERVICE} равен «${MEM:-не задан}», а не 1g — поднимать его этап запрещает"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. Миграция 026 применена ──────────────────────────────────────────────"
HAS_SOURCE="$(psql_val "SELECT count(*) FROM information_schema.columns WHERE table_schema='backtest' AND table_name='candles' AND column_name='source';")"
HAS_CHECKS="$(psql_val "SELECT count(*) FROM pg_constraint WHERE conname IN ('candles_htf_bar','candles_htf_ohlc','candles_htf_period');")"
if [[ -z "${HAS_SOURCE}" ]]; then
  note_unk "база не отвечает — состояние схемы неизвестно"
elif [[ "${HAS_SOURCE}" != "1" ]]; then
  note_fix "миграция 026 НЕ применена: в backtest.candles нет колонки source" \
    "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/026_backtest_candles_htf.sql"
elif [[ "${HAS_CHECKS}" != "3" ]]; then
  note_fix "миграция 026 применена не полностью: ограничений ${HAS_CHECKS} из 3" \
    "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/026_backtest_candles_htf.sql"
else
  note_ok "миграция 026 применена: колонка source и три ограничения на месте"
fi

# РЕШЕНИЕ §6: отдельная таблица НЕ заводится, используется backtest.candles.
SECOND_TABLE="$(psql_val "SELECT count(*) FROM information_schema.tables WHERE table_schema='backtest' AND table_name='candles_htf';")"
if [[ "${SECOND_TABLE}" == "1" ]]; then
  note_block "существует ВТОРАЯ таблица backtest.candles_htf: читатели старших баров разойдутся по двум хранилищам молча"
fi

# ---------------------------------------------------------------------------
echo
echo "── 3. Что фактически загружено: инструмент × масштаб ──────────────────────"
DEPTH="$(psql_tbl "SELECT inst_id, bar, count(*), min(open_time), max(open_time) FROM backtest.candles WHERE bar IN (${HTF_BARS}) OR bar='1H' GROUP BY inst_id, bar ORDER BY inst_id, bar;")"
if [[ -z "${DEPTH}" ]]; then
  note_block "в backtest.candles нет ни одной строки — загрузка не выполнялась"
else
  printf '    %-12s %-8s %10s  %-26s %s\n' "инструмент" "масштаб" "баров" "самая ранняя" "самая поздняя"
  while IFS='|' read -r inst bar n lo hi; do
    printf '    %-12s %-8s %10s  %-26s %s\n' "${inst}" "${bar}" "${n}" "${lo}" "${hi}"
  done <<< "${DEPTH}"
  HTF_ROWS="$(psql_val "SELECT count(*) FROM backtest.candles WHERE bar IN (${HTF_BARS});")"
  if [[ "${HTF_ROWS}" == "0" ]]; then
    note_fix "старших баров не загружено ни одного" \
      "docker compose --profile ${Z0_PROFILE:-backtest} run -d --name z0_load -e PYTHONUNBUFFERED=1 ${Z0_SERVICE:-backtest} python scripts/load_htf_z0.py --apply"
  else
    note_ok "старших баров в хранилище: ${HTF_ROWS}"
  fi
  # Ряд без суффикса utc не используется НИГДЕ (§3). Его присутствие означает,
  # что где-то загружался гонконгский календарь.
  PLAIN="$(psql_val "SELECT count(*) FROM backtest.candles WHERE bar IN ('1D','1W','1M');")"
  if [[ -n "${PLAIN}" && "${PLAIN}" != "0" ]]; then
    note_block "в хранилище ${PLAIN} баров с масштабом БЕЗ суффикса utc (1D/1W/1M) — это гонконгский календарь (UTC+8), он не используется нигде"
  fi
  # Кратность метки суткам UTC. Проверка живёт в коде (выражение над timestamptz
  # не IMMUTABLE и в CHECK не принимается), поэтому здесь она повторяется
  # запросом — это единственное место, где её можно снять с ФАКТА в базе.
  MISALIGNED="$(psql_val "SELECT count(*) FROM backtest.candles WHERE bar IN (${HTF_BARS}) AND extract(epoch FROM open_time)::bigint % 86400 <> 0;")"
  if [[ -n "${MISALIGNED}" && "${MISALIGNED}" != "0" ]]; then
    note_block "у ${MISALIGNED} старших баров метка НЕ кратна суткам UTC — ряд снят без суффикса utc (§3 ТЗ)"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 4. Незакрытые бары в хранилище (§8) ────────────────────────────────────"
# ЗДЕСЬ ПРОВЕРКА НАМЕРЕННО СЛАБЕЕ БОЕВОЙ, и это сказано прямо. Запас
# settle_seconds() живёт в коде (src/barrier/runner.py) и здесь не
# переписывается: копия формулы однажды разошлась бы с оригиналом молча (урок
# Этапа 8.10.1). Запрос ловит только грубое нарушение — конец периода В
# БУДУЩЕМ. Точное число даёт scripts/htf_parity_z0.py, и оно берётся ниже из
# журнала по машиночитаемому ключу.
FUTURE="$(psql_val "SELECT count(*) FROM backtest.candles WHERE bar IN (${HTF_BARS}) AND close_time > now();")"
if [[ -z "${FUTURE}" ]]; then
  note_unk "база не отвечает — незакрытые бары не проверены"
elif [[ "${FUTURE}" != "0" ]]; then
  note_block "в хранилище ${FUTURE} баров, период которых ещё НЕ ЗАКОНЧИЛСЯ"
else
  info "баров с концом периода в будущем нет (грубая проверка; точная — по ключу z0_unclosed_rows ниже)"
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. Итоги прогонов по машиночитаемым ключам журнала ─────────────────────"
LOGS="$(docker compose logs --no-log-prefix --tail 20000 2>/dev/null; docker logs z0_load 2>&1 | tail -20000; docker logs z0_parity 2>&1 | tail -20000)"
key_value() { grep -o "$1=[-0-9.e+]*" <<< "${LOGS}" | tail -1 | cut -d= -f2; }
FOUND_ANY=0
for key in z0_hourly_appended z0_hourly_gaps z0_htf_rows_written \
           z0_htf_dropped_unconfirmed z0_parity_days_compared \
           z0_parity_days_skipped_incomplete z0_parity_ohlc_mismatches \
           z0_parity_volume_max_dev z0_unclosed_rows peak_rss_mb; do
  value="$(key_value "${key}")"
  if [[ -n "${value}" ]]; then
    FOUND_ANY=1
    printf '    %-34s %s\n' "${key}" "${value}"
  fi
done
if [[ "${FOUND_ANY}" == "0" ]]; then
  note_unk "в журналах нет ни одного ключа Замера 0 — прогоны либо не выполнялись, либо их вывод уже вытеснен"
  info "запустите: docker compose --profile ${Z0_PROFILE:-backtest} run --rm ${Z0_SERVICE:-backtest} python scripts/htf_parity_z0.py"
else
  MISMATCH="$(key_value z0_parity_ohlc_mismatches)"
  UNCLOSED="$(key_value z0_unclosed_rows)"
  DROPPED="$(key_value z0_htf_dropped_unconfirmed)"
  PEAK="$(key_value peak_rss_mb)"
  [[ -n "${MISMATCH}" && "${MISMATCH}" != "0" ]] && \
    note_block "z0_parity_ohlc_mismatches=${MISMATCH}: собранная свеча не совпала с биржевой. ПЕРВЫМ ДЕЛОМ проверять часовой пояс (§3), а не арифметику сборки"
  [[ -n "${UNCLOSED}" && "${UNCLOSED}" != "0" ]] && \
    note_block "z0_unclosed_rows=${UNCLOSED}: в хранилище есть незакрытые бары"
  [[ -n "${DROPPED}" && "${DROPPED}" == "0" ]] && \
    note_warn "z0_htf_dropped_unconfirmed=0: биржа не вернула НИ ОДНОГО незакрытого бара. §8 ТЗ называет это подозрительным — проверьте, что свежий край берётся с /market/candles, а не только с history-candles"
  if [[ -n "${PEAK}" ]] && awk "BEGIN{exit !(${PEAK} > 1024)}"; then
    note_block "peak_rss_mb=${PEAK}: превышен лимит контейнера 1 ГБ"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. Граница этапа: продакшн не тронут ───────────────────────────────────"
LV="$(env_value "${ENV_FILE}" LOGIC_VERSION)"
if [[ -z "${LV}" ]]; then
  note_unk "LOGIC_VERSION не найден в ${ENV_FILE}"
elif [[ "${LV}" != "${LOGIC_VERSION_EXPECTED}" ]]; then
  note_block "LOGIC_VERSION=${LV}, ожидалось ${LOGIC_VERSION_EXPECTED} — замерный этап версию не поднимает"
else
  note_ok "LOGIC_VERSION=${LV} — не изменён"
fi

# Старшие масштабы в ПРОДАКШН-таблице свечей означали бы запись вне схемы
# backtest — прямой запрет §1 ТЗ.
PROD_HTF="$(psql_val "SELECT count(*) FROM ohlcv WHERE timeframe IN ('1d','1w','1M','1Dutc','1Wutc','1Mutc');")"
if [[ -n "${PROD_HTF}" && "${PROD_HTF}" != "0" ]]; then
  note_block "в ПРОДАКШН-таблице ohlcv ${PROD_HTF} строк со старшим масштабом — этап обязан писать только в схему backtest"
elif [[ -n "${PROD_HTF}" ]]; then
  note_ok "в продакшн-таблице ohlcv старших масштабов нет"
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Отпечаток решений decision не изменился ─────────────────────────────"
parity="${APP_DIR}/scripts/decision_parity_8_7.py"
if [[ ! -f "${parity}" ]]; then
  note_unk "нет файла ${parity} — слепок решений не снять"
else
  digest="$(docker compose exec -T -e POSTGRES_PASSWORD=parity decision \
              python - < "${parity}" 2>/dev/null \
            | docker compose exec -T decision python -c \
              "import json,sys; print(json.load(sys.stdin)['digest_sha256'])" 2>/dev/null)"
  if [[ -z "${digest}" ]]; then
    note_unk "слепок не снят — вывод о неизменности решений сделать нельзя"
    info "проверьте, что контейнер decision работает: docker compose ps decision"
  else
    echo "    отпечаток: ${digest}"
    echo "    ожидается: ${EXPECTED_DIGEST}"
    if [[ "${digest}" == "${EXPECTED_DIGEST}" ]]; then
      note_ok "решения системы на 323 наборах входов не изменились"
    else
      note_block "решения ИЗМЕНИЛИСЬ — это нарушает жёсткую границу этапа"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Конфигурация прогона на СЕРВЕРЕ ─────────────────────────────────────"
# Файл в репозиторий не входит и правится ВРУЧНУЮ. На Этапе 8.7 из-за этого
# BT_FEE_ROUNDTRIP_PCT остался неисправленным: правка коснулась файла-образца,
# а рабочего — нет. Поэтому проверяется именно рабочий файл.
if [[ -d "${BT_ENV_FILE}" ]]; then
  note_fix "на месте ${BT_ENV_FILE} КАТАЛОГ, а не файл (дефект D-9): docker создал его вместо отсутствующего файла" \
    "rm -rf ${BT_ENV_FILE} && cp ${APP_DIR}/backtest/.env.backtest.example ${BT_ENV_FILE} && docker compose --profile \"*\" build --no-cache"
elif [[ ! -f "${BT_ENV_FILE}" ]]; then
  note_fix "нет ${BT_ENV_FILE} — прогон не запустится" \
    "cp ${APP_DIR}/backtest/.env.backtest.example ${BT_ENV_FILE} && \$EDITOR ${BT_ENV_FILE}"
else
  for key in BT_PERIOD_FROM BT_HTF_BARS BT_HTF_INSTRUMENTS BT_REQUEST_PAUSE_MS; do
    value="$(env_value "${BT_ENV_FILE}" "${key}")"
    if [[ -z "${value}" ]]; then
      note_fix "в ${BT_ENV_FILE} не задан ${key}" \
        "впишите ${key} в ${BT_ENV_FILE} (значение BT_REQUEST_PAUSE_MS берётся ИЗ ЗОНДА, а не из памяти)"
    else
      printf '    %-22s %s\n' "${key}" "${value}"
    fi
  done
  BARS="$(env_value "${BT_ENV_FILE}" BT_HTF_BARS)"
  if [[ -n "${BARS}" ]] && grep -qE '(^|,)1[DWM](,|$)' <<< "${BARS}"; then
    note_block "BT_HTF_BARS содержит масштаб БЕЗ суффикса utc: у OKX это гонконгский календарь (UTC+8), и контроль §7 не сойдётся никогда"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "=============================================================================="
echo " ИТОГ: блокирующих ${blocking}, требует внимания ${attention}, не проверено ${unknown}"
echo "=============================================================================="
if [[ "${blocking}" -gt 0 ]]; then
  if [[ "${#FIX_ITEMS[@]}" -gt 0 ]]; then
    echo
    echo " УСТРАНИМОЕ (${#FIX_ITEMS[@]}) — откат НЕ нужен:"
    echo
    for item in "${FIX_ITEMS[@]}"; do
      echo "   • ${item}"
      echo
    done
  fi
  if [[ "${#ROLLBACK_ITEMS[@]}" -gt 0 ]]; then
    echo " ОТКАТ ОБЯЗАТЕЛЕН — нарушена граница этапа (${#ROLLBACK_ITEMS[@]}):"
    echo
    for item in "${ROLLBACK_ITEMS[@]}"; do
      echo "   • ${item}"
    done
    echo
    echo " Замер 0 обещал добавить данные рядом и не изменить ни одного решения"
    echo " системы. Перечисленное выше означает, что обещание нарушено."
    echo
    echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
    echo "       < ${APP_DIR}/db/migrations/026_backtest_candles_htf_rollback.sql"
    echo "   git -C ${APP_DIR} checkout <предыдущая ревизия>"
    echo "   docker compose --profile \"*\" build --no-cache && docker compose up -d --remove-orphans"
    echo
    echo " Загруженные строки откат схемы НЕ удаляет — это отдельное решение."
    echo " Если их надо убрать тоже:"
    echo "   DELETE FROM backtest.candles WHERE bar IN (${HTF_BARS});"
    echo
    echo " Строки выше передайте исполнителю."
    exit 1
  fi
  echo " ДЕЙСТВИЕ: выполните команды выше и запустите проверку снова."
  echo " Откат НЕ нужен: границу этапа ничто из найденного не нарушает."
  exit 1
elif [[ "${attention}" -gt 0 || "${unknown}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Разберитесь с отмеченным выше."
  echo
  echo " Напоминание: «⚪ НЕ ПРОВЕРЕНО» — это НЕ «всё хорошо». Такую строку"
  echo " нельзя засчитывать за пройденную проверку."
  exit 0
else
  echo " ДЕЙСТВИЕ: не требуется. Старшие ряды загружены, собранная свеча совпала"
  echo " с биржевой, незакрытых баров в хранилище нет, решения системы не"
  echo " изменились."
  exit 0
fi
