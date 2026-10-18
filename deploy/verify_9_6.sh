#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.6 — манименеджмент демо-покупок (процент от баланса, резерв, потолок на монету).
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров, ни одного запроса
# к бирже. Читает базу (SELECT), журнал контейнера demo и файл .env.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_6.sh            # окно 24 часа
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_6.sh --hours 6  # окно 6 часов
#
# ИТОГ: каждый пункт печатается как ✅ или ❌; пункт, который выполнить нечем (покупок ещё не
# было, снимка баланса ещё нет), — ⚪ и в итог «всё хорошо» НЕ засчитывается. В конце — строка
# «ЧТО ДЕЛАТЬ».
#
# КАК ЧИТАЮТСЯ ГРАНИЦЫ. Потолок на монету и резерв считаются от баланса НА МОМЕНТ РЕШЕНИЯ, а
# баланс плавает: вырос после покупки — потолок выше, упал — вложенное остаётся и потолок
# «догоняет» его вниз (продаж по потолку правило не делает). Поэтому «вложено не больше потолка
# от последнего снимка баланса» проверяется от БОЛЬШЕГО из двух балансов: последнего снимка
# конца суток и баланса, который сервис записал в самих строках покупок этой монеты
# (demo_orders.sizing_equity_usd). Превышение над ним — не колебание баланса, а нарушение
# правила. Тот же баланс решения проверяется построчно: шаг и потолок каждой покупки.
#
# ФЛАГИ ОБОЛОЧКИ. При ``set -uo pipefail`` неуспешная команда внутри подстановки обнуляет её
# целиком, поэтому каждый конвейер, собирающий журналы и базу, заканчивается ``|| true``.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
# Общий помощник (Этап 9.9): строка запуска читается в обоих форматах журнала — JSON и key=value.
# shellcheck source=deploy/lib_verify.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_verify.sh"
HOURS=24
TOLERANCE="0.01"          # допуск проверки потолка, долларов (ТЗ 9.6, п. 7 критериев приёмки)
RESERVE_TOLERANCE="0.05"  # допуск резерва: сумма покупки на бирже расходится с заявленной на центы

while [ $# -gt 0 ]; do
  case "$1" in
    --hours)
      shift
      if ! [[ "${1:-}" =~ ^[0-9]+$ ]] || [ "${1}" -lt 1 ]; then
        echo "--hours требует целое число часов >= 1"; exit 2
      fi
      HOURS="$1" ;;
    *) echo "Неизвестный аргумент: $1 (допустим только --hours N)"; exit 2 ;;
  esac
  shift
done

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
    | sed 's/#.*//' | xargs || true
}
# Числа сравниваются как числа: «25» и «25.0» — одно и то же значение.
same_num() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a != "" && b != "" && a + 0 == b + 0) }'; }
gt_num()   { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 > b + 0) }'; }

# Значения правила из .env; не заданные берутся умолчаниями кода (src/core/config.py).
mm_enabled_env="$(env_val DEMO_MM_ENABLED | tr 'A-Z' 'a-z')"; mm_enabled_env="${mm_enabled_env:-false}"
reserve_env="$(env_val DEMO_MM_RESERVE_PCT)";      reserve_env="${reserve_env:-25}"
step_env="$(env_val DEMO_MM_STEP_PCT)";            step_env="${step_env:-5}"
cap_env="$(env_val DEMO_MM_TOKEN_CAP_PCT)";        cap_env="${cap_env:-15}"
min_env="$(env_val DEMO_MM_MIN_ORDER_USD)";        min_env="${min_env:-5}"
if [ "${mm_enabled_env}" = "true" ]; then expected_rule="mm1"; else expected_rule="fixed"; fi

echo "=== Проверка этапа 9.6 (манименеджмент демо-покупок) ==="
echo "Момент запуска (UTC): $(date -u +%FT%TZ); окно: ${HOURS} ч"
echo "  ⓘ .env: DEMO_MM_ENABLED=${mm_enabled_env}, резерв ${reserve_env}%, шаг ${step_env}%, потолок ${cap_env}%, минимум \$${min_env}"
echo

# --- 1. Миграция 031 ---------------------------------------------------------------------------
echo "1. Миграция 031 (колонки размера покупки в demo_orders)"
cols="$(psql_q "SELECT count(*) FROM information_schema.columns WHERE table_name = 'demo_orders' AND column_name IN ('sizing_rule', 'sizing_equity_usd', 'sizing_exposure_usd');")"
cons="$(psql_q "SELECT count(*) FROM pg_constraint WHERE conname = 'demo_orders_sizing_rule_chk';")"
if [ -z "${cols}" ]; then
  skip "база не отвечает — наличие колонок проверить нечем"
elif [ "${cols}" = "3" ] && [ "${cons}" = "1" ]; then
  ok "три колонки и ограничение demo_orders_sizing_rule_chk на месте"
else
  bad "колонок ${cols} из 3, ограничений ${cons:-0} из 1"
  advice "примените миграцию: cd ${APP_DIR} && docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < db/migrations/031_demo_sizing.sql"
fi

# --- 2. Контейнер -----------------------------------------------------------------------------
echo "2. Контейнер demo"
state="$(docker compose ps --format '{{.State}}' demo 2>/dev/null | head -1 || true)"
if [ -z "${state}" ]; then
  bad "контейнера demo нет — сервис не развёрнут"
  advice "запустите сервис: docker compose up -d --force-recreate demo bot"
elif [ "${state}" = "running" ]; then
  ok "запущен"
else
  bad "состояние: ${state}"
  advice "смотрите журнал: docker compose logs --tail 100 demo"
fi

# --- 3. Строка запуска: пять величин правила совпадают с .env --------------------------------
echo "3. Строка запуска demo: пять величин правила и .env"
logs="$(docker compose logs --no-color --tail 20000 demo 2>/dev/null || true)"
# Служба пишет строку запуска консольным форматом (mm_enabled=True ...), а при JSON-рендерере —
# "mm_enabled": true; lib_verify.sh находит последнюю строку и читает значения в обоих форматах.
startup="$(startup_line "${logs}" mm_enabled)"
started_at=""
if [ -z "${startup}" ]; then
  skip "строки запуска с mm_enabled нет в журнале — контейнер не пересоздан после обновления кода?"
  advice "пересоздайте сервис: docker compose --profile \"*\" build --no-cache && docker compose up -d --force-recreate demo bot"
else
  started_at="$(startup_ts "${startup}")"
  l_enabled="$(startup_val "${startup}" mm_enabled)"; l_reserve="$(startup_val "${startup}" mm_reserve_pct)"
  l_step="$(startup_val "${startup}" mm_step_pct)"; l_cap="$(startup_val "${startup}" mm_token_cap_pct)"
  l_min="$(startup_val "${startup}" mm_min_order_usd)"
  echo "  ⓘ в журнале: mm_enabled=${l_enabled}, резерв ${l_reserve}, шаг ${l_step}, потолок ${l_cap}, минимум ${l_min} (запуск ${started_at:-?})"
  mismatch=""
  [ "${l_enabled}" = "${mm_enabled_env}" ] || mismatch+=" DEMO_MM_ENABLED"
  same_num "${l_reserve}" "${reserve_env}" || mismatch+=" DEMO_MM_RESERVE_PCT"
  same_num "${l_step}" "${step_env}" || mismatch+=" DEMO_MM_STEP_PCT"
  same_num "${l_cap}" "${cap_env}" || mismatch+=" DEMO_MM_TOKEN_CAP_PCT"
  same_num "${l_min}" "${min_env}" || mismatch+=" DEMO_MM_MIN_ORDER_USD"
  if [ -z "${mismatch}" ]; then
    ok "все пять величин в журнале совпадают с .env"
  else
    bad "расходятся с .env:${mismatch}"
    advice "сервис работает со старыми настройками: docker compose up -d --force-recreate demo bot"
  fi
fi
if [ "${mm_enabled_env}" != "true" ]; then
  echo "  ⓘ DEMO_MM_ENABLED не true — правило выключено: покупки идут фиксированной суммой positions.notional_usd"
fi

# Окно проверки: не раньше последнего запуска сервиса — строки до него сделаны прежним кодом.
if [ -n "${started_at}" ]; then
  since_sql="GREATEST(now() - interval '${HOURS} hours', '${started_at}'::timestamptz)"
else
  since_sql="now() - interval '${HOURS} hours'"
fi

# --- 4. sizing_rule новых строк покупки -------------------------------------------------------
echo "4. Правило размера в новых строках покупки (с запуска сервиса, не дальше ${HOURS} ч)"
by_rule="$(psql_q "SELECT rule || '=' || n FROM (SELECT coalesce(sizing_rule, 'NULL') AS rule, count(*) AS n FROM demo_orders WHERE leg = 'buy' AND created_at >= ${since_sql} GROUP BY 1) t ORDER BY 1;")"
total="$(psql_q "SELECT count(*) FROM demo_orders WHERE leg = 'buy' AND created_at >= ${since_sql};")"
wrong="$(psql_q "SELECT count(*) FROM demo_orders WHERE leg = 'buy' AND created_at >= ${since_sql} AND sizing_rule IS DISTINCT FROM '${expected_rule}';")"
if [ -z "${total}" ]; then
  skip "база не отвечает"
elif [ "${total}" = "0" ]; then
  skip "новых строк покупки в окне нет — сигналов ещё не было; повторите позже"
else
  echo "  ⓘ строк покупки: ${total}; по правилу: $(printf '%s' "${by_rule}" | tr '\n' ' ')"
  if [ "${wrong}" = "0" ]; then
    ok "у всех ${total} новых строк sizing_rule='${expected_rule}'"
  else
    bad "у ${wrong} из ${total} строк sizing_rule не '${expected_rule}'"
    advice "проверьте, что запущен контейнер с кодом 9.6 и совпадает DEMO_MM_ENABLED: docker compose up -d --force-recreate demo"
  fi
fi

# --- 5. Шаг и потолок каждой покупки в момент решения -----------------------------------------
echo "5. Шаг ${step_env}% и потолок ${cap_env}% в момент каждого решения"
if [ "${mm_enabled_env}" != "true" ]; then
  skip "правило выключено — проверять нечего"
else
  decided="$(psql_q "SELECT count(*) FROM demo_orders WHERE leg = 'buy' AND sizing_rule = 'mm1' AND status IN ('filled', 'pending', 'sent') AND created_at >= ${since_sql};")"
  over_step="$(psql_q "SELECT count(*) FROM demo_orders WHERE leg = 'buy' AND sizing_rule = 'mm1' AND status IN ('filled', 'pending', 'sent') AND created_at >= ${since_sql} AND requested_cost_usd > sizing_equity_usd * ${step_env} / 100 + ${TOLERANCE};")"
  over_cap="$(psql_q "SELECT count(*) FROM demo_orders WHERE leg = 'buy' AND sizing_rule = 'mm1' AND status IN ('filled', 'pending', 'sent') AND created_at >= ${since_sql} AND sizing_exposure_usd + requested_cost_usd > sizing_equity_usd * ${cap_env} / 100 + ${TOLERANCE};")"
  if [ -z "${decided}" ]; then
    skip "база не отвечает"
  elif [ "${decided}" = "0" ]; then
    skip "покупок по правилу в окне ещё не было"
  else
    echo "  ⓘ покупок по правилу в окне: ${decided}"
    echo "  ⓘ последние покупки (позиция, монета, сумма, баланс решения, было в монете):"
    psql_q "SELECT '     #' || position_id || ' ' || symbol || ' $' || round(requested_cost_usd, 2) || ' из баланса $' || round(sizing_equity_usd, 2) || ', в монете было $' || round(sizing_exposure_usd, 2) || ' [' || status || ']' FROM demo_orders WHERE leg = 'buy' AND sizing_rule = 'mm1' AND status IN ('filled', 'pending', 'sent') ORDER BY id DESC LIMIT 5;"
    if [ "${over_step}" = "0" ]; then ok "ни одна покупка не больше шага"; else
      bad "покупок больше шага: ${over_step}"; advice "сумма покупки превысила ${step_env}% баланса — сохраните строки demo_orders и покажите архитектору"; fi
    if [ "${over_cap}" = "0" ]; then ok "ни одна покупка не вывела монету за потолок"; else
      bad "покупок, выведших монету за потолок: ${over_cap}"; advice "потолок на монету нарушен — сохраните строки demo_orders и покажите архитектору"; fi
  fi
fi

# --- 6. Вложено по монетам не больше потолка (тот же SQL, что fetch_token_exposure) --------------
echo "6. Вложено по монетам и потолок ${cap_env}% (допуск \$${TOLERANCE})"
snap_equity="$(psql_q "SELECT equity_close FROM demo_balance_daily ORDER BY day DESC LIMIT 1;")"
exposure_rows="$(psql_q "WITH held AS (SELECT b.instrument_id, b.sizing_rule, b.sizing_equity_usd, CASE WHEN b.status = 'filled' THEN COALESCE(b.cost_usd, b.requested_cost_usd) ELSE b.requested_cost_usd END AS cost FROM demo_orders b WHERE b.leg = 'buy' AND ((b.status = 'filled' AND NOT EXISTS (SELECT 1 FROM demo_orders s WHERE s.position_id = b.position_id AND s.leg = 'sell' AND s.status = 'filled')) OR b.status IN ('pending', 'sent'))) SELECT i.symbol || '|' || coalesce(sum(h.cost), 0) || '|' || coalesce(max(h.sizing_equity_usd) FILTER (WHERE h.sizing_rule = 'mm1'), 0) FROM instruments i LEFT JOIN held h ON h.instrument_id = i.id WHERE i.symbol NOT LIKE '%:%' AND i.active GROUP BY i.symbol, i.id ORDER BY i.id;")"
if [ -z "${exposure_rows}" ]; then
  skip "база не отвечает или инструментов нет"
elif [ "${mm_enabled_env}" != "true" ]; then
  echo "  ⓘ вложено по монетам:"
  printf '%s\n' "${exposure_rows}" | awk -F'|' '{ printf("     %s  $%.2f\n", $1, $2) }'
  skip "правило выключено — потолок не проверяется"
else
  over_coins=""; ref_missing=0
  echo "  ⓘ вложено по монетам (вложено / потолок от баланса сравнения):"
  while IFS='|' read -r symbol invested decision_equity; do
    [ -z "${symbol}" ] && continue
    ref="$(awk -v s="${snap_equity:-0}" -v d="${decision_equity:-0}" 'BEGIN { print (s + 0 > d + 0) ? s : d }')"
    if ! gt_num "${ref}" 0; then ref_missing=$((ref_missing + 1)); fi
    cap_usd="$(awk -v r="${ref}" -v p="${cap_env}" 'BEGIN { printf("%.2f", r * p / 100) }')"
    if gt_num "${ref}" 0; then
      printf '     %s  $%.2f / $%s\n' "${symbol}" "${invested}" "${cap_usd}"
    else
      printf '     %s  $%.2f / баланса для сравнения ещё нет\n' "${symbol}" "${invested}"
    fi
    if gt_num "${ref}" 0 && awk -v i="${invested}" -v c="${cap_usd}" -v t="${TOLERANCE}" 'BEGIN { exit !(i + 0 > c + t) }'; then
      over_coins+=" ${symbol}"
    fi
  done <<< "${exposure_rows}"
  if [ -n "${over_coins}" ]; then
    bad "вложено больше потолка:${over_coins}"
    advice "потолок на монету нарушен (${over_coins# }) — сохраните вывод этого скрипта и строки demo_orders и покажите архитектору"
  elif [ -z "${snap_equity}" ] && ! psql_q "SELECT 1 FROM demo_orders WHERE sizing_equity_usd IS NOT NULL LIMIT 1;" | grep -q 1; then
    skip "нет ни снимка баланса, ни покупок по правилу — сравнивать не с чем"
  else
    ok "ни одна монета не выше потолка (баланс сравнения — больший из снимка ${snap_equity:-нет} и баланса решения)"
  fi
fi

# --- 7. Свободные деньги не ниже резерва ------------------------------------------------------
echo "7. Свободные деньги не ниже резерва ${reserve_env}% (допуск \$${RESERVE_TOLERANCE})"
cash="$(psql_q "SELECT s.start_capital - coalesce(sum(o.cost_usd) FILTER (WHERE o.leg = 'buy'), 0) - coalesce(sum(abs(o.fee_usd)) FILTER (WHERE o.leg = 'buy' AND upper(o.fee_ccy) = 'USDT'), 0) + coalesce(sum(o.cost_usd) FILTER (WHERE o.leg = 'sell'), 0) - coalesce(sum(abs(o.fee_usd)) FILTER (WHERE o.leg = 'sell' AND upper(o.fee_ccy) = 'USDT'), 0) FROM demo_state s LEFT JOIN demo_orders o ON o.status = 'filled' WHERE s.id = 1 GROUP BY s.start_capital;")"
reserved="$(psql_q "SELECT coalesce(sum(requested_cost_usd), 0) FROM demo_orders WHERE leg = 'buy' AND status IN ('pending', 'sent');")"
last_equity="$(psql_q "SELECT round(sizing_equity_usd, 2) FROM demo_orders WHERE leg = 'buy' AND sizing_rule = 'mm1' AND status IN ('filled', 'pending', 'sent') ORDER BY id DESC LIMIT 1;")"
if [ "${mm_enabled_env}" != "true" ]; then
  skip "правило выключено — резерв не проверяется"
elif [ -z "${cash}" ]; then
  skip "учёт не прочитан (нет demo_state?)"
elif [ -z "${last_equity}" ]; then
  skip "покупок по правилу ещё не было — резерв ещё ничем не проверен (свободно \$${cash})"
else
  free="$(awk -v c="${cash}" -v r="${reserved:-0}" 'BEGIN { printf("%.2f", c - r) }')"
  reserve_usd="$(awk -v e="${last_equity}" -v p="${reserve_env}" 'BEGIN { printf("%.2f", e * p / 100) }')"
  spare="$(awk -v f="${free}" -v r="${reserve_usd}" 'BEGIN { printf("%.2f", f - r) }')"
  echo "  ⓘ свободно \$${free} (cash \$$(printf '%.2f' "${cash}") минус под ордерами \$$(printf '%.2f' "${reserved:-0}")); резерв от баланса последнего решения \$${last_equity}: \$${reserve_usd}; сверх резерва \$${spare}"
  if awk -v f="${free}" -v r="${reserve_usd}" -v t="${RESERVE_TOLERANCE}" 'BEGIN { exit !(f + 0 >= r - t) }'; then
    ok "свободные деньги не ниже резерва"
  else
    bad "свободно \$${free} — ниже резерва \$${reserve_usd}"
    advice "резерв нарушен — сохраните вывод скрипта и строки demo_orders и покажите архитектору"
  fi
fi

# --- 8. Как часто правило останавливало покупки ------------------------------------------------
echo "8. Пропуски покупок за ${HOURS} ч (справка)"
skips="$(psql_q "SELECT reason || '=' || n FROM (SELECT coalesce(skip_reason, '-') AS reason, count(*) AS n FROM demo_orders WHERE leg = 'buy' AND status = 'skipped' AND created_at >= now() - interval '${HOURS} hours' GROUP BY 1) t ORDER BY 1;")"
if [ -z "${skips}" ]; then
  echo "  ⓘ пропусков нет"
else
  echo "  ⓘ $(printf '%s' "${skips}" | tr '\n' ' ') (token_cap — монета на потолке, reserve — сработал резерв)"
fi

echo
if [ "${fail}" -gt 0 ]; then
  echo "ИТОГ: ❌ есть замечания (${fail}); не проверено: ${unknown}"
  echo "ЧТО ДЕЛАТЬ:"
  for line in "${todo[@]}"; do echo "  - ${line}"; done
  exit 1
elif [ "${unknown}" -gt 0 ]; then
  echo "ИТОГ: ⚪ замечаний нет, но часть пунктов не проверена (${unknown}) — это не «всё хорошо»"
  echo "ЧТО ДЕЛАТЬ: дождитесь покупок (сигнал → покупка ≈ ${step_env}% баланса в Telegram) и запустите скрипт снова"
  for line in "${todo[@]}"; do echo "  - ${line}"; done
  exit 0
else
  echo "ИТОГ: ✅ всё в порядке"
  echo "ЧТО ДЕЛАТЬ: ничего. Повторите проверку через сутки; через месяц по sizing_* в demo_orders можно посчитать, как часто правило упиралось в резерв и потолок"
  exit 0
fi
