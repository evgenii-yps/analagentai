#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.11 — замер входа «на откате» (mean reversion) по истории.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров. Читает права роли
# в базе, отчёт и файл фактов, которые оставил analysis/run_9_11.sh.
#
# Запуск на сервере ОДНОЙ командой (после sudo bash /opt/agent-trade/analysis/run_9_11.sh):
#   sudo bash /opt/agent-trade/deploy/verify_9_11.sh
#
# Пункты:
#   1. agenttrade_ro читает backtest.candles (миграция 032 из Этапа 9.10 на месте);
#   2. роль не может писать в backtest.candles;
#   3. отчёт report_9_11_<дата>.txt существует, не длиннее 300 строк, пароля роли в нём нет;
#   4. все блоки отработали без ошибок: заголовки 00 и 02–06 на месте, «с ошибками 0»;
#   5. число строк positions / signals / demo_orders до и после прогона совпало (замер ничего не пишет);
#   6. CSV ряда series_9_11_<дата>.csv существует и содержит все 5 монет (BTC, ETH, SOL, XRP, DOGE).
#
# ИТОГ: каждый выполненный пункт — ✅ или ❌. Пункт, который выполнить нечем (нет отчёта, база не
# отвечает), — ⚪: он напечатан как «не проверено» и в «ВСЕ ✅» НЕ засчитывается.
#
# Пункт 5: оба снимка делает сам run_9_11.sh (в начале и в конце прогона, ролью только на чтение).
# Живые сервисы пишут в signals и демо-таблицы независимо от замера: если счётчик разошёлся на
# несколько строк, повторите прогон и проверку — замер писать не умеет (пункты 1–2).
#
# ФЛАГИ ОБОЛОЧКИ. При ``set -uo pipefail`` неуспешная команда внутри подстановки обнуляет её
# целиком, поэтому каждый конвейер, собирающий данные, заканчивается ``|| true``.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
OUT_DIR="${OUT_DIR:-$APP_DIR/analysis_out}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
MAX_REPORT_LINES=300
# shellcheck source=deploy/lib_verify.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_verify.sh"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

fail=0; unknown=0
todo=()
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=$((fail + 1)); }
skip() { echo "  ⚪ НЕ ПРОВЕРЕНО: $*"; unknown=$((unknown + 1)); }
advice() { todo+=("$*"); }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}
env_val() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/\r$//' | tr -d '"' || true
}

echo "=== Проверка этапа 9.11 (вход «на откате» по истории) ==="
echo "Момент запуска (UTC): $(date -u +%FT%TZ)"
echo

# --- 1. Право чтения -----------------------------------------------------------------------------
echo "1. Миграция 032: agenttrade_ro читает backtest.candles"
can_read="$(psql_q "SELECT has_schema_privilege('agenttrade_ro', 'backtest', 'USAGE') AND has_table_privilege('agenttrade_ro', 'backtest.candles', 'SELECT');" | tr -d '[:space:]')"
case "${can_read}" in
  t) ok "USAGE на схему backtest и SELECT на backtest.candles выданы" ;;
  f) bad "у роли agenttrade_ro нет права читать backtest.candles"
     advice "примените миграцию: cd ${APP_DIR} && docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < db/migrations/032_ro_backtest_read.sql" ;;
  *) skip "база не ответила — права роли проверить нечем (или роли agenttrade_ro ещё нет)" ;;
esac

# --- 2. Права записи -----------------------------------------------------------------------------
echo "2. Роль не может писать в backtest.candles"
writes="$(psql_q "SELECT string_agg(p, ',') FROM unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE']) p WHERE has_table_privilege('agenttrade_ro', 'backtest.candles', p);" | tr -d '[:space:]')"
if [ -z "${can_read}" ]; then
  skip "база не ответила — права записи проверить нечем"
elif [ -z "${writes}" ]; then
  ok "INSERT / UPDATE / DELETE / TRUNCATE у роли нет"
else
  bad "у роли agenttrade_ro есть права записи на backtest.candles: ${writes}"
  advice "откатите лишние права: REVOKE ${writes} ON backtest.candles FROM agenttrade_ro; миграция 032 их не выдаёт"
fi

# --- 3. Отчёт ------------------------------------------------------------------------------------
echo "3. Отчёт report_9_11_<дата>.txt"
report="$(ls -1t "${OUT_DIR}"/report_9_11_*.txt 2>/dev/null | head -1 || true)"
if [ -z "${report}" ]; then
  skip "в ${OUT_DIR} нет report_9_11_*.txt — сначала sudo bash ${APP_DIR}/analysis/run_9_11.sh"
  advice "запустите замер: sudo bash ${APP_DIR}/analysis/run_9_11.sh"
else
  lines="$(wc -l < "${report}" | tr -d ' ')"
  ro_pw="$(env_val POSTGRES_RO_PASSWORD)"
  problems=""
  [ "${lines}" -le "${MAX_REPORT_LINES}" ] || problems="${problems} длина ${lines} строк (допустимо ${MAX_REPORT_LINES});"
  if [ -n "${ro_pw}" ] && grep -qF -- "${ro_pw}" "${report}" 2>/dev/null; then
    problems="${problems} в отчёте найден пароль роли;"
  fi
  if [ -z "${problems}" ]; then
    ok "$(basename "${report}"): ${lines} строк, пароля нет"
  else
    bad "$(basename "${report}"):${problems}"
    advice "перезапустите замер и перешлите архитектору вывод целиком; пароль в отчёте — сразу сообщите разработчику"
  fi
  [ -z "${ro_pw}" ] && echo "  ⓘ POSTGRES_RO_PASSWORD в .env пуст — искать пароль в отчёте было нечего"
fi

# --- 4. Блоки без ошибок -------------------------------------------------------------------------
echo "4. Все блоки отработали без ошибок"
if [ -z "${report:-}" ]; then
  skip "отчёта нет — блоки проверить нечем"
else
  problems=""
  for head in "00 —" "02 —" "03 —" "04 —" "05 —" "06 —"; do
    grep -q "^===== ${head}" "${report}" || problems="${problems} нет блока ${head%% —};"
  done
  grep -q "с ошибками 0" "${report}" || problems="${problems} в итоге прогона есть блоки с ошибками;"
  grep -q "^ОШИБКА" "${report}" && problems="${problems} в отчёте есть строки ОШИБКА;"
  if [ -z "${problems}" ]; then
    ok "блоки 00, 02–06 на месте, «с ошибками 0»"
  else
    bad "блоки:${problems}"
    advice "откройте отчёт: строки ОШИБКА показывают, какой блок упал"
  fi
fi

# --- 5. Замер ничего не писал --------------------------------------------------------------------
echo "5. Число строк positions / signals / demo_orders до и после прогона"
facts="$(ls -1t "${OUT_DIR}"/facts_9_11_*.txt 2>/dev/null | head -1 || true)"
before=""; after=""
if [ -n "${facts}" ]; then
  before="$(grep -E '^@@F\|rows_before\|' "${facts}" | tail -1 | cut -d'|' -f3 || true)"
  after="$(grep -E '^@@F\|rows_after\|' "${facts}" | tail -1 | cut -d'|' -f3 || true)"
fi
if [[ ! "${before}" =~ ^[0-9]+/[0-9]+/[0-9]+$ ]] || [[ ! "${after}" =~ ^[0-9]+/[0-9]+/[0-9]+$ ]]; then
  skip "в файле фактов нет снимков числа строк (${facts:-файла нет}) — перезапустите замер"
elif [ "${before}" = "${after}" ]; then
  ok "строк (positions/signals/demo_orders) до ${before} и после ${after}: совпало"
else
  bad "число строк разошлось: до ${before}, после ${after}"
  advice "повторите прогон и проверку (живые сервисы пишут в signals сами); замер писать не умеет — пункты 1–2"
fi

# --- 6. CSV ряда ---------------------------------------------------------------------------------
echo "6. CSV ряда series_9_11_<дата>.csv"
series="$(ls -1t "${OUT_DIR}"/series_9_11_*.csv 2>/dev/null | head -1 || true)"
if [ -z "${series}" ]; then
  skip "в ${OUT_DIR} нет series_9_11_*.csv — замер не запускался"
else
  counts="$(awk -F, 'NR > 1 { c[$1]++ } END { for (k in c) print k, c[k] }' "${series}" | sort || true)"
  missing=""
  for coin in BTC ETH SOL XRP DOGE; do
    n="$(printf '%s\n' "${counts}" | awk -v k="${coin}" '$1 == k { print $2 }')"
    [[ "${n:-0}" =~ ^[0-9]+$ ]] && [ "${n:-0}" -gt 0 ] || missing="${missing} ${coin}"
  done
  if [ -z "${missing}" ]; then
    ok "$(basename "${series}"): $(printf '%s' "${counts}" | tr '\n' ' ')"
  else
    bad "в ряде нет монет:${missing}"
    advice "проверьте блок 00 отчёта (диапазоны H-hist / H-live): по этим монетам нет ни истории, ни живых часов"
  fi
fi

echo
if [ "${fail}" -gt 0 ]; then
  echo "ИТОГ: ❌ есть замечания (${fail}); не проверено: ${unknown}"
  echo "ЧТО ДЕЛАТЬ:"
  for line in "${todo[@]}"; do echo "  - ${line}"; done
  exit 1
elif [ "${unknown}" -gt 0 ]; then
  echo "ИТОГ: ⚪ замечаний нет, но часть пунктов не проверена (${unknown}) — это не «ВСЕ ✅»"
  for line in "${todo[@]+"${todo[@]}"}"; do echo "  - ${line}"; done
  exit 0
else
  echo "ИТОГ: ВСЕ ✅"
  echo "ЧТО ДЕЛАТЬ: перешлите архитектору файл ${report} целиком"
  exit 0
fi
