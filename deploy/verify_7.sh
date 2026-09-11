#!/usr/bin/env bash
# Проверка версии логики 7 — снятие слотов, пауза по токену, уведомления
# только по сделкам. Критерии приёмки §9 ТЗ, по одному на раздел.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_7.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ     — развёрнуто не то, что заказано;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО     — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он не делает выводов о прибыльности и не говорит,
# «удался ли» замер. Он отвечает на один вопрос: развёрнуто ли ровно то, что
# описано в ТЗ. Числа он печатает; что они значат, решает не скрипт.
#
# ПОЧЕМУ ПОИСК В ЖУРНАЛАХ ИДЁТ ПО МАШИНОЧИТАЕМЫМ КЛЮЧАМ. Русский текст в
# журналах хранится экранированными последовательностями Unicode, и grep по
# русским словам не находит ничего.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"

# ИТОГ ВЕРСИИ 5 — КОНТРОЛЬНАЯ СУММА ИСТОРИИ (§9 ТЗ). 88 сделок, −$0.68088 до
# цента. Эти строки не менялись ни этапом 9.2, ни этим; изменись они — значит,
# кто-то пересчитал прошлое, и это блокирующая находка, а не расхождение.
V5_TRADES_EXPECTED=88
V5_PNL_EXPECTED="-0.68088"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0; attention=0; unknown=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ:      $*"; blocking=$((blocking + 1)); }
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_skip()  { echo "  ⚪ НЕ ПРОВЕРЕНО:     $*"; unknown=$((unknown + 1)); }
note_ok()    { echo "  🟢 $*"; }

psql_q() { docker compose exec -T postgres psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null; }
env_val() { grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/#.*//' | xargs; }

echo "=== Проверка версии логики 7 ==="
echo

# --- 1. Настройки: то ли развёрнуто, что заказано (§9, §6 ТЗ) ---------------
echo "1. Настройки в ${ENV_FILE}"
if [ ! -f "${ENV_FILE}" ]; then
  note_skip "файла .env нет — настройки проверить нечем"
else
  check_env() { # имя ожидаемое
    local got; got="$(env_val "$1")"
    if [ -z "${got}" ]; then
      note_warn "$1 в .env не задан — действует умолчание кода"
    elif [ "${got}" = "$2" ]; then
      note_ok "$1=${got}"
    else
      note_block "$1=${got}, а заказано $2"
    fi
  }
  check_env LOGIC_VERSION 7
  check_env POSITION_MAX_OPEN 0
  check_env POSITION_TOKEN_PAUSE_MIN 60
  check_env POSITION_BUDGET_USD 0
  # ЗАМОРОЖЕННЫЙ СПИСОК §6: эти величины обязаны остаться прежними, иначе
  # версии 6 и 7 перестанут отличаться ровно тем, чем их задумали отличать.
  check_env POSITION_SLOT_USD 2.0
  check_env POSITION_MIN_PROBABILITY 0.8
  check_env POSITION_MAX_HOLD_HOURS 48
  check_env POSITION_PLUS_WAIT_START_HOURS 24
  check_env POSITION_SETTLE_SEC 90
  check_env POSITION_MAX_SIGNAL_AGE_SEC 180
  # ТОНКАЯ СВЯЗЬ §6 ТЗ: 60 + SETTLE < MAX_SIGNAL_AGE. Порви её — и позиций не
  # откроется НИ ОДНОЙ при совершенно честном виде журнала.
  settle="$(env_val POSITION_SETTLE_SEC)"; settle="${settle:-90}"
  max_age="$(env_val POSITION_MAX_SIGNAL_AGE_SEC)"; max_age="${max_age:-180}"
  if [ "$((60 + settle))" -lt "${max_age}" ]; then
    note_ok "60 + ${settle} < ${max_age}: бар входа успевает стать годным"
  else
    note_block "60 + ${settle} >= ${max_age}: позиции не откроются НИКОГДА"
  fi
fi
echo

# --- 2. Строка запуска службы позиций (§9 ТЗ) -------------------------------
echo "2. Строка запуска службы позиций"
start_line="$(docker compose logs --since 24h --no-color positions 2>/dev/null | grep -F 'token_pause_min' | tail -1)"
if [ -z "${start_line}" ]; then
  note_skip "в журнале за сутки нет строки запуска — перезапускалась ли служба?"
else
  ok=1
  for field in '"logic_version": 7' '"max_open": 0' '"token_pause_min": 60' \
               '"budget_usd": 0' '"slot_usd": 2' '"max_hold_hours": 48' \
               '"plus_wait_start_hours": 24' '"min_probability": 0.8'; do
    echo "${start_line}" | grep -qF "${field}" || { note_block "в строке запуска нет ${field}"; ok=0; }
  done
  [ "${ok}" = 1 ] && note_ok "строка запуска называет все восемь величин §9"
fi
echo

# --- 3. База: ослабление только для версии 7 (§7 ТЗ) ------------------------
echo "3. Правило «один инструмент — одна позиция» в базе"
idx="$(psql_q "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_positions_one_open_per_instrument';")"
if [ -z "${idx}" ]; then
  note_block "индекса ux_positions_one_open_per_instrument нет вовсе: правило снято и для прежних версий"
elif echo "${idx}" | grep -q "logic_version < 7"; then
  note_ok "индекс ослаблен ТОЛЬКО для версии 7 (миграция 027 применена)"
else
  note_block "индекс в прежнем виде: миграция 027 не применена, вторая позиция по токену получит отказ базы"
fi
sig_idx="$(psql_q "SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_positions_signal';")"
if [ "${sig_idx}" = "1" ]; then
  note_ok "правило «один сигнал — одна позиция» на месте"
else
  note_block "индекса ux_positions_signal нет: один сигнал сможет открыть несколько позиций"
fi
echo

# --- 4. История версий 5 и 6 не изменилась (§7.1, §9 ТЗ) --------------------
echo "4. Позиции прежних версий"
v5="$(psql_q "SELECT count(*) || '|' || COALESCE(round(sum(net_pnl_usd), 5), 0) FROM positions WHERE logic_version = 5 AND status = 'closed' AND exit_reason <> 'data_gap';")"
if [ -z "${v5}" ]; then
  note_skip "база не ответила — итог версии 5 проверить нечем"
else
  v5_n="${v5%%|*}"; v5_pnl="${v5##*|}"
  if [ "${v5_n}" = "${V5_TRADES_EXPECTED}" ] && [ "${v5_pnl}" = "${V5_PNL_EXPECTED}" ]; then
    note_ok "версия 5: ${v5_n} сделок, ${v5_pnl} USDT — до цента как было"
  else
    note_block "версия 5: ${v5_n} сделок, ${v5_pnl} USDT; ожидалось ${V5_TRADES_EXPECTED} и ${V5_PNL_EXPECTED}"
  fi
fi
v6_multi="$(psql_q "SELECT count(*) FROM (SELECT instrument_id FROM positions WHERE status = 'open' AND logic_version < 7 GROUP BY instrument_id HAVING count(*) > 1) t;")"
if [ "${v6_multi}" = "0" ]; then
  note_ok "у версий до 7 по-прежнему не больше одной открытой позиции на токен"
else
  note_block "у версий до 7 нашлось ${v6_multi} токенов с несколькими открытыми позициями"
fi
echo

# --- 5. Сделки версии 7: то, ради чего этап делался -------------------------
echo "5. Сделки версии 7 за сутки"
v7="$(psql_q "SELECT count(*) FILTER (WHERE opened_at > now() - interval '24 hours') || '|' || count(*) FILTER (WHERE status = 'open') || '|' || count(DISTINCT instrument_id) FILTER (WHERE status = 'open') FROM positions WHERE logic_version = 7;")"
if [ -z "${v7}" ]; then
  note_skip "база не ответила"
else
  opened="$(echo "${v7}" | cut -d'|' -f1)"
  open_now="$(echo "${v7}" | cut -d'|' -f2)"
  tokens="$(echo "${v7}" | cut -d'|' -f3)"
  note_ok "открыто за сутки: ${opened}; открыто сейчас: ${open_now} по ${tokens} токенам"
  if [ "${open_now}" -gt 0 ] && [ "${open_now}" -gt "${tokens}" ]; then
    note_ok "по одному токену уже больше одной позиции — правило действительно снято"
  fi
fi
# ОТКАЗЫ ПО ДЕНЬГАМ ОБЯЗАНЫ БЫТЬ НУЛЕВЫМИ (§3 A3 ТЗ). Ненулевые означают, что
# бюджет снова конечен, и поток сделок режет не то, чем его собирались резать.
today="$(date -u +%F)"
cap="$(docker compose exec -T redis redis-cli GET "positions:refused:7:${today}:no_free_capital" 2>/dev/null | tr -d '\r')"
pause="$(docker compose exec -T redis redis-cli GET "positions:refused:7:${today}:token_pause" 2>/dev/null | tr -d '\r')"
if [ -z "${cap}" ] || [ "${cap}" = "" ]; then
  note_ok "отказов no_free_capital за сегодня нет — бюджет не ограничивает"
else
  note_block "отказов no_free_capital за сегодня ${cap}: бюджет снова конечен"
fi
echo "  ⓘ отказов token_pause за сегодня: ${pause:-0}"
echo

# --- 6. Уведомления: в Telegram уходят только сделки (§5 ТЗ) ----------------
echo "6. Уведомления"
sent="$(psql_q "SELECT count(*) FROM signals WHERE notified_at > now() - interval '24 hours';")"
if [ -z "${sent}" ]; then
  note_skip "база не ответила"
elif [ "${sent}" = "0" ]; then
  note_ok "за сутки не отправлено ни одного уведомления О СИГНАЛЕ"
else
  note_block "за сутки отправлено ${sent} уведомлений о сигналах: §5 C1 требует нуля"
fi
# ПОТОК САМИХ СИГНАЛОВ ТРОГАТЬ НЕЛЬЗЯ (§5 C3). Сравниваются два соседних часа:
# провал вдвое означает, что вместе с отправкой отключили и сбор.
flow="$(psql_q "SELECT count(*) FILTER (WHERE ts > now() - interval '1 hour') || '|' || count(*) FILTER (WHERE ts > now() - interval '2 hours' AND ts <= now() - interval '1 hour') FROM signals;")"
if [ -n "${flow}" ]; then
  last="${flow%%|*}"; prev="${flow##*|}"
  echo "  ⓘ сигналов записано: за последний час ${last}, за предыдущий ${prev}"
  if [ "${last}" = "0" ] && [ "${prev}" != "0" ]; then
    note_block "поток сигналов прекратился: §5 C3 требует, чтобы сигналы писались как прежде"
  fi
fi
echo

echo "=== Итог: блокирующих ${blocking}, требует внимания ${attention}, не проверено ${unknown} ==="
[ "${blocking}" -gt 0 ] && exit 1
exit 0
