#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.10 — замер «вето при нисходящем тренде BTC» и полнота минутных свечей.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров. Читает права роли
# в базе, отчёт и файл фактов, которые оставил analysis/run_9_10.sh.
#
# Запуск на сервере ОДНОЙ командой (после sudo bash /opt/agent-trade/analysis/run_9_10.sh):
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_10.sh
#
# Пункты:
#   1. миграция 032 применена: agenttrade_ro читает backtest.candles;
#   2. роль не может писать в backtest.candles (INSERT/UPDATE/DELETE/TRUNCATE);
#   3. отчёт report_9_10_<дата>.txt существует, не длиннее 300 строк, пароля роли в нём нет;
#   4. в отчёте контроли данных §4.4 п. 1–3 ✅ и контрольный опыт со сдвигом суток дал расхождения;
#   5. число строк positions / signals / demo_orders до и после прогона совпало (замер ничего не пишет).
#
# ИТОГ: каждый выполненный пункт — ✅, ⚠️ или ❌. Пункт, который выполнить нечем (нет отчёта, база не
# отвечает), — ⚪: он напечатан как «не проверено» и в «ВСЕ ✅» НЕ засчитывается.
#
# Пункт 5 (правило ТЗ 9.12 §8): оба снимка делает сам run_9_10.sh (в начале и в конце прогона, ролью
# только на чтение). positions и demo_orders до и после обязаны совпасть (иначе ❌). Служба decision
# пишет в signals около 5 строк в минуту, поэтому рост signals допустим, если он не больше
# 5 × (минут прогона, округлённо вверх) + 5; больше — ⚠️ (не ❌) с числами. Длительность берётся из
# времени запуска и окончания в отчёте. Что замер сам ничего не пишет, гарантируют пункты 1–2.
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

fail=0; unknown=0; warns=0
todo=()
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=$((fail + 1)); }
skip() { echo "  ⚪ НЕ ПРОВЕРЕНО: $*"; unknown=$((unknown + 1)); }
warn() { echo "  ⚠️ $*"; warns=$((warns + 1)); }
advice() { todo+=("$*"); }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}
env_val() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/\r$//' | tr -d '"' || true
}

echo "=== Проверка этапа 9.10 (вето при нисходящем тренде BTC, полнота минутных свечей) ==="
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
echo "3. Отчёт report_9_10_<дата>.txt"
report="$(ls -1t "${OUT_DIR}"/report_9_10_*.txt 2>/dev/null | head -1 || true)"
if [ -z "${report}" ]; then
  skip "в ${OUT_DIR} нет report_9_10_*.txt — сначала sudo bash ${APP_DIR}/analysis/run_9_10.sh"
  advice "запустите замер: sudo bash ${APP_DIR}/analysis/run_9_10.sh"
else
  lines="$(wc -l < "${report}" | tr -d ' ')"
  ro_pw="$(env_val POSTGRES_RO_PASSWORD)"
  problems=""
  [ "${lines}" -le "${MAX_REPORT_LINES}" ] || problems="${problems} длина ${lines} строк (допустимо ${MAX_REPORT_LINES});"
  if [ -n "${ro_pw}" ] && grep -qF -- "${ro_pw}" "${report}" 2>/dev/null; then
    problems="${problems} в отчёте найден пароль роли;"
  fi
  for head in "00 —" "01 —" "02 —" "03 —" "04 —" "05 —" "06 —" "07 —"; do
    grep -q "^===== ${head}" "${report}" || problems="${problems} нет блока ${head%% —};"
  done
  grep -q "с ошибками 0" "${report}" || problems="${problems} в итоге прогона есть блоки с ошибками;"
  if [ -z "${problems}" ]; then
    ok "$(basename "${report}"): ${lines} строк, блоки 00–07 на месте, ошибок нет, пароля нет"
  else
    bad "$(basename "${report}"):${problems}"
    advice "перезапустите замер и перешлите архитектору вывод целиком; пароль в отчёте — сразу сообщите разработчику"
  fi
  [ -z "${ro_pw}" ] && echo "  ⓘ POSTGRES_RO_PASSWORD в .env пуст — искать пароль в отчёте было нечего"
fi

# --- 4. Контроли данных --------------------------------------------------------------------------
echo "4. Контроли данных ТЗ §4.4 в отчёте"
if [ -z "${report:-}" ]; then
  skip "отчёта нет — контроли проверить нечем"
else
  c1="$(grep -E '^ *1\. ' "${report}" | tail -1 || true)"
  c2="$(grep -E '^ *2\. ' "${report}" | tail -1 || true)"
  c3="$(grep -E '^ *3\. ' "${report}" | tail -1 || true)"
  shifted="$(printf '%s\n' "${c2}" | awk -F'|' '{ gsub(/[[:space:]]/, "", $3); print $3 }')"
  if [ -z "${c1}" ] || [ -z "${c2}" ] || [ -z "${c3}" ]; then
    bad "в отчёте нет строк контролей 1–3"
    advice "блок 00 не отработал: откройте начало отчёта"
  else
    miss=""
    printf '%s\n' "${c1}" | grep -q '✅' || miss="${miss} 1 (сутки из часов = 1Dutc биржи);"
    printf '%s\n' "${c2}" | grep -q '✅' || miss="${miss} 2 (контрольный опыт обязан разойтись);"
    printf '%s\n' "${c3}" | grep -q '✅' || miss="${miss} 3 (шов источников);"
    if [[ "${shifted}" =~ ^[0-9]+$ ]] && [ "${shifted}" -gt 0 ]; then :; else
      miss="${miss} контрольный опыт не дал расхождений (${shifted:-нет числа});"
    fi
    if [ -z "${miss}" ]; then
      ok "контроли 1–3 ✅; при сдвиге границы суток на час расхождений: ${shifted}"
    else
      bad "контроли не пройдены:${miss}"
      advice "данные не прошли контроль — вердикт блока 07 не печатался; перешлите отчёт архитектору"
    fi
  fi
fi

# --- 5. Замер ничего не писал --------------------------------------------------------------------
# Правило ТЗ 9.12 §8: positions и demo_orders до и после обязаны совпасть (иначе ❌); signals — рост
# допустим, если не больше 5 × (минут прогона, округлённо вверх) + 5 (служба decision пишет в signals
# по 5 строк в минуту), больше — ⚠️ с числами; длительность прогона — по времени запуска и окончания
# в отчёте. Что замер сам ничего не пишет, гарантируют пункты 1–2 (у роли нет прав записи).
echo "5. Число строк positions / signals / demo_orders до и после прогона"
facts="$(ls -1t "${OUT_DIR}"/facts_9_10_*.txt 2>/dev/null | head -1 || true)"
before=""; after=""
if [ -n "${facts}" ]; then
  before="$(grep -E '^@@F\|rows_before\|' "${facts}" | tail -1 | cut -d'|' -f3 || true)"
  after="$(grep -E '^@@F\|rows_after\|' "${facts}" | tail -1 | cut -d'|' -f3 || true)"
fi
if [[ ! "${before}" =~ ^[0-9]+/[0-9]+/[0-9]+$ ]] || [[ ! "${after}" =~ ^[0-9]+/[0-9]+/[0-9]+$ ]]; then
  skip "в файле фактов нет снимков числа строк (${facts:-файла нет}) — перезапустите замер"
else
  IFS=/ read -r pos_b sig_b ord_b <<< "${before}"
  IFS=/ read -r pos_a sig_a ord_a <<< "${after}"
  counts="positions ${pos_b}→${pos_a}, signals ${sig_b}→${sig_a}, demo_orders ${ord_b}→${ord_a}"
  run_min=""
  if [ -n "${report:-}" ]; then
    t_start="$(sed -nE 's/.*Запуск \(UTC\): ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}).*/\1/p' "${report}" | head -1 || true)"
    t_end="$(sed -nE 's/.*окончание \(UTC\): ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}).*/\1/p' "${report}" | tail -1 || true)"
    if [ -n "${t_start}" ] && [ -n "${t_end}" ]; then
      s_ep="$(date -u -d "${t_start}" +%s 2>/dev/null || true)"
      e_ep="$(date -u -d "${t_end}" +%s 2>/dev/null || true)"
      if [[ "${s_ep}" =~ ^[0-9]+$ ]] && [[ "${e_ep}" =~ ^[0-9]+$ ]] && [ "${e_ep}" -ge "${s_ep}" ]; then
        run_min=$(( (e_ep - s_ep + 59) / 60 ))
      fi
    fi
  fi
  if [ "${pos_b}" != "${pos_a}" ] || [ "${ord_b}" != "${ord_a}" ]; then
    bad "число строк разошлось: ${counts}"
    advice "positions и demo_orders должны совпасть до и после прогона; повторите прогон и проверку (замер писать не умеет — пункты 1–2)"
  elif [ "${sig_a}" -lt "${sig_b}" ]; then
    warn "signals уменьшилось: ${counts}"
    advice "signals не должны убывать за время прогона; повторите прогон и проверку и перешлите вывод архитектору"
  elif [ "${sig_a}" -eq "${sig_b}" ]; then
    ok "${counts}: совпало"
  elif [ -z "${run_min}" ]; then
    skip "signals выросли на $((sig_a - sig_b)) (${counts}), а время запуска и окончания в отчёте не найдено — допустимый рост не вычислить"
    advice "перезапустите замер, чтобы в отчёте были строки «Запуск (UTC)» и «окончание (UTC)»"
  else
    growth=$((sig_a - sig_b)); limit=$((5 * run_min + 5))
    if [ "${growth}" -le "${limit}" ]; then
      ok "${counts}: рост signals +${growth} не больше допустимых ${limit} (5 × ${run_min} мин + 5) — это живая служба decision"
    else
      warn "signals выросли на ${growth} при допустимых ${limit} (5 × ${run_min} мин + 5): ${counts}"
      advice "рост signals больше обычного для службы decision — повторите прогон и проверку; если повторится, перешлите вывод архитектору"
    fi
  fi
fi

echo
if [ "${fail}" -gt 0 ]; then
  echo "ИТОГ: ❌ есть замечания (${fail}); предупреждений: ${warns}; не проверено: ${unknown}"
  echo "ЧТО ДЕЛАТЬ:"
  for line in "${todo[@]}"; do echo "  - ${line}"; done
  exit 1
elif [ "${unknown}" -gt 0 ]; then
  echo "ИТОГ: ⚪ замечаний нет, но часть пунктов не проверена (${unknown}) — это не «ВСЕ ✅»"
  for line in "${todo[@]+"${todo[@]}"}"; do echo "  - ${line}"; done
  exit 0
elif [ "${warns}" -gt 0 ]; then
  echo "ИТОГ: ⚠️ ошибок (❌) нет, но есть предупреждения (${warns}) — это не «ВСЕ ✅»"
  echo "ЧТО ДЕЛАТЬ:"
  for line in "${todo[@]+"${todo[@]}"}"; do echo "  - ${line}"; done
  exit 0
else
  echo "ИТОГ: ВСЕ ✅"
  echo "ЧТО ДЕЛАТЬ: перешлите архитектору файл ${report} целиком"
  exit 0
fi
