#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.7.1 — несколько получателей: всё, что присылает бот, уходит каждому из списка.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров. Без флага НИ ОДНОГО
# сообщения в Telegram не отправляется: читаются .env, состояние контейнеров, журналы за последний
# час и состояние git.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_7_1.sh
#
# Флаг --test (явный): после проверок каждому получателю уходит ОДНО пробное сообщение
# «Проверка связи Agent Trade — можно не отвечать» той же функцией, что и все тревоги системы
# (src.notify.telegram.send_message, внутри контейнера bot). Итог по каждому получателю — ✅/❌.
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_7_1.sh --test
#
# ИТОГ: каждый пункт печатается как ✅ или ❌; пункт, который выполнить нечем, — ⚪ и в итог
# «всё хорошо» НЕ засчитывается.
#
# ФЛАГИ ОБОЛОЧКИ. При ``set -uo pipefail`` неуспешная команда внутри подстановки обнуляет её
# целиком, поэтому каждый конвейер, собирающий журналы, заканчивается ``|| true``.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
ENV_FILE="${APP_DIR}/.env"
PROBE_TEXT="Проверка связи Agent Trade — можно не отвечать"
# Постоянные контейнеры, которые шлют сообщения. export — разовая задача (профиль tools, cron):
# постоянного контейнера у неё нет, поэтому она проверяется отдельным пунктом.
SERVICES="bot demo agents positions notify"

do_test=0
for arg in "$@"; do
  case "${arg}" in
    --test) do_test=1 ;;
    *) echo "Неизвестный аргумент: ${arg} (допустим только --test)"; exit 2 ;;
  esac
done

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

fail=0; unknown=0
ok()   { echo "  ✅ $*"; }
bad()  { echo "  ❌ $*"; fail=$((fail + 1)); }
skip() { echo "  ⚪ НЕ ПРОВЕРЕНО: $*"; unknown=$((unknown + 1)); }

env_val() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//' || true
}
# Правило списка (то же, что Settings.telegram_recipients): TELEGRAM_CHAT_ID, затем
# BOT_ALLOWED_CHAT_IDS; пробелы срезаются, пустые и повторы выбрасываются. По одному в строке.
recipients_from_env() {
  local -a parts; local part chat seen=","
  IFS=',' read -ra parts <<< "$(env_val TELEGRAM_CHAT_ID),$(env_val BOT_ALLOWED_CHAT_IDS)"
  for part in "${parts[@]}"; do
    chat="$(printf '%s' "${part}" | tr -d '[:space:]')"
    [[ -z "${chat}" || "${seen}" == *",${chat},"* ]] && continue
    seen+="${chat},"; printf '%s\n' "${chat}"
  done
}

echo "=== Проверка этапа 9.7.1 (несколько получателей) ==="
echo "Момент запуска (UTC): $(date -u +%FT%TZ)"
echo

# --- 1. Получатели ------------------------------------------------------------------------
echo "1. Список получателей"
owner="$(env_val TELEGRAM_CHAT_ID)"
mapfile -t recipients < <(recipients_from_env)
echo "  ⓘ получателей: ${#recipients[@]}"
for chat in "${recipients[@]}"; do echo "  ⓘ chat_id: ${chat}"; done
if [ "${#recipients[@]}" -eq 0 ]; then
  bad "получателей нет: не заданы ни TELEGRAM_CHAT_ID, ни BOT_ALLOWED_CHAT_IDS"
else
  if [ -n "${owner}" ] && printf '%s\n' "${recipients[@]}" | grep -qxF -- "${owner}"; then
    ok "владелец (TELEGRAM_CHAT_ID=${owner}) среди получателей"
  else
    bad "TELEGRAM_CHAT_ID не задан — владельца нет среди получателей"
  fi
  [ -n "$(env_val TELEGRAM_BOT_TOKEN)" ] && ok "TELEGRAM_BOT_TOKEN задан" \
    || bad "TELEGRAM_BOT_TOKEN не задан — ничего не отправится"
  if [ "${#recipients[@]}" -ge 2 ]; then
    ok "получателей больше одного — рассылка включена"
  else
    echo "  ⓘ получатель один — поведение прежнее (второго добавляют в BOT_ALLOWED_CHAT_IDS)"
  fi
fi

# Что видит УЖЕ ЗАПУЩЕННЫЙ контейнер: не перезапущенный после правки .env покажет старый список.
live="$(docker compose exec -T bot python -c \
  'from src.core.config import settings; print(" ".join(settings.telegram_recipients))' \
  2>/dev/null | tr -d '\r' || true)"
if [ -z "${live}" ]; then
  skip "контейнер bot не ответил — список в работающем сервисе не прочитан"
else
  expected="$(printf '%s' "${recipients[*]}")"
  if [ "${live}" = "${expected}" ]; then
    ok "работающий бот видит тот же список, что .env"
  else
    bad "работающий бот видит «${live}», а в .env «${expected}» — перезапустите сервисы после правки .env"
  fi
fi

# --- 2. Контейнеры -----------------------------------------------------------------------------
echo "2. Контейнеры"
for service in ${SERVICES}; do
  state="$(docker compose ps --format '{{.State}}' "${service}" 2>/dev/null | head -1 || true)"
  if [ -z "${state}" ]; then
    bad "контейнера ${service} нет — сервис не развёрнут"
  elif [ "${state}" = "running" ]; then
    ok "${service}: запущен"
  else
    bad "${service}: состояние ${state}"
  fi
done
if docker compose --profile tools config --services 2>/dev/null | grep -qx export; then
  ok "export: описан в compose (разовая задача по cron, постоянного контейнера нет)"
else
  bad "export: сервиса нет в docker-compose.yml"
fi

# --- 3. Версия main -------------------------------------------------------------------------------
echo "3. Версия кода на сервере"
if [ ! -d "${APP_DIR}/.git" ]; then
  skip "каталог не git-репозиторий"
else
  local_head="$(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || true)"
  branch="$(git -C "${APP_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  echo "  ⓘ ветка: ${branch:-?}; коммит: $(git -C "${APP_DIR}" log -1 --format='%h %s' 2>/dev/null || true)"
  [ "${branch}" = "main" ] && ok "сервер на ветке main" || bad "сервер на ветке ${branch:-?}, а не на main"
  remote_head="$(git -C "${APP_DIR}" ls-remote origin refs/heads/main 2>/dev/null | cut -f1 || true)"
  if [ -z "${remote_head}" ]; then
    skip "GitHub не ответил — совпадение с origin/main проверить нечем"
  elif [ "${remote_head}" = "${local_head}" ]; then
    ok "коммит совпадает с origin/main"
  else
    bad "на сервере не последний main (origin/main: ${remote_head:0:7})"
  fi
  grep -q "telegram_recipients" "${APP_DIR}/src/core/config.py" 2>/dev/null \
    && ok "src/core/config.py: список получателей на месте" \
    || bad "в src/core/config.py нет telegram_recipients — этап 9.7.1 не развёрнут"
fi

# --- 4. Сбои отправки за последний час ----------------------------------------------------------
echo "4. Журналы за последний час: telegram_send_failed=1 по chat_id"
all_failed=""
for service in ${SERVICES} export; do
  all_failed+="$(docker compose logs --no-color --since 1h "${service}" 2>/dev/null \
    | grep 'telegram_send_failed=1' || true)"$'\n'
done
if [ "${#recipients[@]}" -eq 0 ]; then
  skip "получателей нет — считать нечего"
else
  for chat in "${recipients[@]}"; do
    count="$(printf '%s\n' "${all_failed}" | grep -cE "chat_id[^0-9-]{1,4}${chat}([^0-9]|\$)" || true)"
    if [ "${count:-0}" -eq 0 ]; then
      ok "chat_id ${chat}: сбоев отправки за час нет"
    else
      bad "chat_id ${chat}: сбоев отправки за час — ${count} (403 — человек не нажал Start или заблокировал бота)"
    fi
  done
fi

# --- 5. Пробное сообщение (только с --test) -----------------------------------------------------------
echo "5. Пробное сообщение"
if [ "${do_test}" -ne 1 ]; then
  echo "  ⓘ без флага --test ничего не отправляется"
elif [ "${#recipients[@]}" -eq 0 ]; then
  bad "получателей нет — пробное сообщение слать некому"
else
  probe_out="$(docker compose exec -T -e PROBE_TEXT="${PROBE_TEXT}" bot python - <<'PY' 2>/dev/null | tr -d '\r' || true
import asyncio, os
from src.core.config import settings
from src.notify.telegram import send_message

async def main() -> None:
    for chat in settings.telegram_recipients:
        sent = await send_message(os.environ["PROBE_TEXT"], chat_id=chat)
        print(f"{chat} {'ok' if sent else 'fail'}")

asyncio.run(main())
PY
)"
  if [ -z "${probe_out}" ]; then
    bad "контейнер bot не выполнил отправку — пробные сообщения не ушли"
  else
    while read -r chat result; do
      [ -z "${chat}" ] && continue
      [ "${result}" = "ok" ] && ok "chat_id ${chat}: пробное сообщение доставлено" \
        || bad "chat_id ${chat}: пробное сообщение НЕ доставлено"
    done <<< "${probe_out}"
  fi
fi

echo
if [ "${fail}" -gt 0 ]; then
  echo "ИТОГ: ❌ есть ошибки (${fail}); не проверено: ${unknown}"
  exit 1
elif [ "${unknown}" -gt 0 ]; then
  echo "ИТОГ: ⚪ ошибок нет, но не проверено пунктов: ${unknown}"
  exit 0
fi
echo "ИТОГ: ✅ всё в порядке"
