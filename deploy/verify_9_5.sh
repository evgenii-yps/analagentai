#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.5 — демо-исполнение зеркалом (OKX, спот, демо-счёт).
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров, ни
# одного запроса к бирже. Читает базу (SELECT), Redis (GET), журнал контейнера
# и исходный код сервиса.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_5.sh
#
# ИТОГ: каждый пункт печатается как ✅ или ❌; в конце — общий итог. Пункт,
# который выполнить нечем, печатается как ⚪ и в итог «всё хорошо» НЕ засчитывается.
#
# ФЛАГИ ОБОЛОЧКИ. При ``set -uo pipefail`` неуспешная команда внутри подстановки
# обнуляет её целиком, поэтому каждый конвейер, собирающий журналы, заканчивается
# ``|| true``, а пустой результат печатается как ⚪ явно.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
HEARTBEAT_KEY="demo:heartbeat"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

fail=0; unknown=0
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=$((fail + 1)); }
skip() { echo "  ⚪ НЕ ПРОВЕРЕНО: $*"; unknown=$((unknown + 1)); }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}
env_val() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=== Проверка этапа 9.5 (демо-исполнение зеркалом, OKX спот) ==="
echo "Момент запуска (UTC): $(date -u +%FT%TZ)"
echo

# --- 1. Миграция применена ---------------------------------------------------
echo "1. Миграция 030 (таблицы demo_orders, demo_state, demo_balance_daily)"
present="$(psql_q "SELECT (to_regclass('public.demo_orders') IS NOT NULL)::int + (to_regclass('public.demo_state') IS NOT NULL)::int + (to_regclass('public.demo_balance_daily') IS NOT NULL)::int;")"
if [ -z "${present}" ]; then
  skip "база не отвечает — наличие таблиц проверить нечем"
elif [ "${present}" = "3" ]; then
  ok "все три таблицы есть"
else
  bad "таблиц меньше трёх (найдено ${present}) — примените db/migrations/030_demo_orders.sql"
fi

# --- 2. Контейнер demo запущен -----------------------------------------------
echo "2. Контейнер demo"
state="$(docker compose ps --format '{{.State}}' demo 2>/dev/null | head -1 || true)"
if [ -z "${state}" ]; then
  bad "контейнера demo нет — сервис не развёрнут (запустите его по инструкции развёртывания)"
elif [ "${state}" = "running" ]; then
  ok "запущен"
else
  bad "состояние: ${state}"
fi

# --- 3. Heartbeat свежий -----------------------------------------------------
echo "3. Heartbeat (${HEARTBEAT_KEY})"
interval="$(env_val DEMO_INTERVAL)"; interval="${interval:-15}"
hb="$(docker compose exec -T redis redis-cli GET "${HEARTBEAT_KEY}" 2>/dev/null | tr -d '\r' || true)"
if [ -z "${hb}" ]; then
  bad "отметки нет: сервис не отвечает (ключ живёт 300 секунд)"
else
  hb_epoch="$(date -u -d "${hb}" +%s 2>/dev/null || true)"
  if [ -z "${hb_epoch}" ]; then
    bad "отметка нечитаема: ${hb}"
  else
    age=$(( $(date -u +%s) - hb_epoch ))
    if [ "${age}" -le $(( 5 * interval )) ]; then
      ok "свежий: ${age} с назад (порог 5 × ${interval} с)"
    else
      bad "устарел: ${age} с назад (порог 5 × ${interval} с)"
    fi
  fi
fi

# --- 4. Режим и хост из журнала запуска ---------------------------------------
echo "4. Строка запуска в журнале (enabled и host)"
logs="$(docker compose logs --no-color --tail 2000 demo 2>/dev/null || true)"
startup="$(printf '%s\n' "${logs}" | grep -E '"mirror_since"|demo выключен|demo_config_error' | tail -1 || true)"
if [ -z "${startup}" ]; then
  skip "строки запуска нет в последних 2000 строках журнала"
else
  enabled="$(printf '%s' "${startup}" | grep -oE '"enabled": ?(true|false)' | head -1 || true)"
  host="$(printf '%s' "${startup}" | grep -oE '"host": ?"[^"]*"' | head -1 || true)"
  echo "  ⓘ ${enabled:-enabled: не найдено}; ${host:-host: не найден}"
  if printf '%s' "${startup}" | grep -q demo_config_error; then
    bad "сервис остановлен из-за неверных настроек — смотрите журнал: docker compose logs demo"
  else
    ok "строка запуска найдена"
  fi
fi
echo "  ⓘ DEMO_ENABLED в .env: $(env_val DEMO_ENABLED)"

# --- 5. Границы по коду: запись только в demo_orders, demo_state, demo_balance_daily ---
echo "5. Запись сервиса в чужие таблицы (по коду src/demo и src/demo_main.py)"
if [ ! -d "${APP_DIR}/src/demo" ]; then
  skip "каталога src/demo нет"
else
  targets="$(grep -rhoiE 'INSERT[[:space:]]+INTO[[:space:]]+[a-z_.]+|UPDATE[[:space:]]+[a-z_.]+[[:space:]]+SET|DELETE[[:space:]]+FROM[[:space:]]+[a-z_.]+' \
      "${APP_DIR}/src/demo" "${APP_DIR}/src/demo_main.py" "${APP_DIR}/src/export/demo_sheets.py" 2>/dev/null \
    | sed -E 's/^(insert[[:space:]]+into|update|delete[[:space:]]+from)[[:space:]]+//I; s/[[:space:]]+set$//I; s/^public\.//' \
    | tr 'A-Z' 'a-z' | sort -u || true)"
  foreign="$(printf '%s\n' "${targets}" | grep -vxE 'demo_orders|demo_state|demo_balance_daily|' || true)"
  echo "  ⓘ цели записи в коде: $(printf '%s' "${targets}" | tr '\n' ' ')"
  if [ -n "${foreign}" ]; then
    bad "код пишет в чужие таблицы: $(printf '%s' "${foreign}" | tr '\n' ' ')"
  else
    ok "пишет только в demo_orders, demo_state и demo_balance_daily"
  fi
fi

# --- 6. Ключи не попали в журнал ----------------------------------------------
echo "6. Демо-ключи в журнале контейнера"
leaked=0; checked=0
for name in OKX_DEMO_API_KEY OKX_DEMO_SECRET_KEY OKX_DEMO_PASSPHRASE; do
  val="$(env_val "${name}")"
  [ -z "${val}" ] && continue
  checked=$((checked + 1))
  if printf '%s' "${logs}" | grep -qF -- "${val}"; then leaked=$((leaked + 1)); fi
done
if [ "${checked}" -eq 0 ]; then
  skip "демо-ключей в .env нет (сервис выключен?) — проверять нечего"
elif [ "${leaked}" -gt 0 ]; then
  bad "значение демо-ключа найдено в журнале (${leaked} из ${checked}) — немедленно перевыпустите ключ"
else
  ok "значений ключей в журнале нет (проверено ${checked})"
fi

# --- 7. Последние ордера ------------------------------------------------------
echo "7. Последние 10 строк demo_orders"
rows="$(psql_q "SELECT id, position_id, leg, status, coalesce(skip_reason,''), symbol, coalesce(avg_price::text,''), coalesce(round(slippage_pct,4)::text,''), coalesce(lag_sec::text,''), to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') FROM demo_orders ORDER BY id DESC LIMIT 10;")"
if [ -z "${rows}" ]; then
  echo "  ⓘ строк пока нет"
else
  echo "  id|позиция|нога|статус|причина|символ|цена|проскальз.%|задержка с|создана"
  printf '%s\n' "${rows}" | sed 's/^/  /'
fi

# --- 8. Потерянные и отклонённые за сутки ---------------------------------------
echo "8. lost и rejected за последние сутки"
lost="$(psql_q "SELECT count(*) FROM demo_orders WHERE status = 'lost' AND created_at >= now() - interval '24 hours';")"
rej="$(psql_q "SELECT count(*) FROM demo_orders WHERE status = 'rejected' AND created_at >= now() - interval '24 hours';")"
if [ -z "${lost}" ] || [ -z "${rej}" ]; then
  skip "счётчики прочитать не удалось"
else
  echo "  ⓘ lost=${lost}, rejected=${rej}"
  if [ "${lost}" = "0" ]; then ok "lost = 0"; else bad "lost = ${lost}: ордер не найден на бирже по clOrdId"; fi
  if [ "${rej}" = "0" ]; then ok "rejected = 0"; else echo "  ⚠️ rejected = ${rej}: разберите error_text в demo_orders"; fi
fi

# --- 9. Снимки конца суток -------------------------------------------------------
echo "9. Снимки конца суток (demo_balance_daily)"
snaps="$(psql_q "SELECT count(*) FROM demo_balance_daily;")"
last="$(psql_q "SELECT to_char(max(day), 'YYYY-MM-DD') FROM demo_balance_daily;")"
dups="$(psql_q "SELECT count(*) FROM (SELECT day FROM demo_balance_daily GROUP BY day HAVING count(*) > 1) d;")"
if [ -z "${snaps}" ]; then
  skip "таблицу снимков прочитать не удалось"
else
  echo "  ⓘ снимков: ${snaps}; последний за: ${last:-нет}; поздних (late): $(psql_q "SELECT count(*) FROM demo_balance_daily WHERE late;")"
  if [ "${dups}" = "0" ]; then ok "по одному снимку на сутки"; else bad "есть сутки с несколькими снимками"; fi
fi

echo
if [ "${fail}" -gt 0 ]; then
  echo "ИТОГ: ❌ есть замечания (${fail}); не проверено: ${unknown}"
  exit 1
elif [ "${unknown}" -gt 0 ]; then
  echo "ИТОГ: ⚪ замечаний нет, но часть пунктов не проверена (${unknown}) — это не «всё хорошо»"
  exit 0
else
  echo "ИТОГ: ✅ всё в порядке"
  exit 0
fi
