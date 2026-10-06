#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.7 — Telegram-бот: исправления и интерфейс с кнопками.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров, ни одного
# сообщения в Telegram. Читает базу (SELECT), Redis (GET), журналы контейнеров bot и demo
# и состояние git (без изменения рабочего каталога).
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_7.sh
#
# ИТОГ: каждый пункт печатается как ✅ или ❌; в конце — общий итог. Пункт, который
# выполнить нечем, печатается как ⚪ и в итог «всё хорошо» НЕ засчитывается.
#
# ФЛАГИ ОБОЛОЧКИ. При ``set -uo pipefail`` неуспешная команда внутри подстановки обнуляет её
# целиком, поэтому каждый конвейер, собирающий журналы, заканчивается ``|| true``, а пустой
# результат печатается как ⚪ явно.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
# Экран должен собираться быстрее этого порога (миллисекунд) — ТЗ 9.7 §8.
SCREEN_LIMIT_MS=1000

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
# Свежесть ключа Redis с ISO-временем: печатает возраст в секундах или пусто.
heartbeat_age() {
  local value epoch
  value="$(docker compose exec -T redis redis-cli GET "$1" 2>/dev/null | tr -d '\r' || true)"
  [ -z "${value}" ] && return 0
  epoch="$(date -u -d "${value}" +%s 2>/dev/null || true)"
  [ -z "${epoch}" ] && { echo "bad"; return 0; }
  echo $(( $(date -u +%s) - epoch ))
}

echo "=== Проверка этапа 9.7 (бот: исправления и кнопки) ==="
echo "Момент запуска (UTC): $(date -u +%FT%TZ)"
echo

# --- 1. Контейнеры bot и demo запущены ---------------------------------------
echo "1. Контейнеры bot и demo"
for service in bot demo; do
  state="$(docker compose ps --format '{{.State}}' "${service}" 2>/dev/null | head -1 || true)"
  if [ -z "${state}" ]; then
    bad "контейнера ${service} нет — сервис не развёрнут"
  elif [ "${state}" = "running" ]; then
    ok "${service}: запущен"
  else
    bad "${service}: состояние ${state}"
  fi
done

# --- 2. Heartbeat bot и demo свежие ---------------------------------------------
echo "2. Heartbeat bot и demo"
bot_age="$(heartbeat_age bot:heartbeat)"
if [ -z "${bot_age}" ]; then
  bad "bot:heartbeat: отметки нет — бот не отвечает (ключ живёт 300 секунд)"
elif [ "${bot_age}" = "bad" ]; then
  bad "bot:heartbeat: отметка нечитаема"
elif [ "${bot_age}" -le 120 ]; then
  ok "bot: ${bot_age} с назад (порог 120 с: цикл ожидания апдейтов — до BOT_POLL_TIMEOUT)"
else
  bad "bot: устарел, ${bot_age} с назад"
fi
interval="$(env_val DEMO_INTERVAL)"; interval="${interval:-15}"
demo_age="$(heartbeat_age demo:heartbeat)"
if [ -z "${demo_age}" ]; then
  bad "demo:heartbeat: отметки нет — служба не отвечает"
elif [ "${demo_age}" = "bad" ]; then
  bad "demo:heartbeat: отметка нечитаема"
elif [ "${demo_age}" -le $(( 5 * interval )) ]; then
  ok "demo: ${demo_age} с назад (порог 5 × ${interval} с)"
else
  bad "demo: устарел, ${demo_age} с назад (порог 5 × ${interval} с)"
fi

# --- 3. Меню команд Telegram задано ------------------------------------------------
echo "3. Журнал bot: меню команд (set_my_commands)"
bot_logs="$(docker compose logs --no-color --tail 3000 bot 2>/dev/null || true)"
last_ok="$(printf '%s\n' "${bot_logs}" | grep -n 'bot_set_my_commands=1' | tail -1 | cut -d: -f1 || true)"
last_fail="$(printf '%s\n' "${bot_logs}" | grep -n 'bot_set_my_commands_failed=1' | tail -1 | cut -d: -f1 || true)"
if [ -z "${bot_logs}" ]; then
  skip "журнал bot пуст или недоступен"
elif [ -z "${last_ok}" ] && [ -z "${last_fail}" ]; then
  skip "строки о set_my_commands нет в последних 3000 строках журнала (бот давно не перезапускался?)"
elif [ -n "${last_fail}" ] && { [ -z "${last_ok}" ] || [ "${last_fail}" -gt "${last_ok}" ]; }; then
  bad "последняя попытка задать меню команд не удалась (bot_set_my_commands_failed=1)"
else
  ok "меню команд задано (bot_set_my_commands=1)"
fi

# --- 4. Время сборки экранов ------------------------------------------------------------
echo "4. Время сборки экранов (bot_screen=1, мс)"
slowest="$(printf '%s\n' "${bot_logs}" | grep 'bot_screen=1' \
  | grep -oE '"ms": ?[0-9]+' | grep -oE '[0-9]+' | sort -n | tail -1 || true)"
shown="$(printf '%s\n' "${bot_logs}" | grep -c 'bot_screen=1' || true)"
if [ -z "${slowest}" ]; then
  skip "экраны ещё не открывались (нажмите кнопки в боте и повторите проверку)"
elif [ "${slowest}" -le "${SCREEN_LIMIT_MS}" ]; then
  ok "самый долгий из ${shown} экранов в журнале: ${slowest} мс (порог ${SCREEN_LIMIT_MS} мс)"
else
  bad "экран собирался ${slowest} мс (порог ${SCREEN_LIMIT_MS} мс)"
fi

# --- 5. Предусловия §0.4: индекс и права -------------------------------------------------
echo "5. Индекс и права (ТЗ 9.7 §0.4)"
idx="$(psql_q "SELECT count(*) FROM pg_indexes WHERE tablename = 'agent_outputs' AND indexname = 'idx_agent_outputs';")"
col="$(psql_q "SELECT count(*) FROM information_schema.columns WHERE table_name = 'agent_outputs' AND column_name = 'instrument_id';")"
if [ -z "${idx}" ]; then
  skip "база не отвечает — индекс и права проверить нечем"
else
  [ "${col}" = "1" ] && ok "agent_outputs.instrument_id есть" || bad "нет колонки agent_outputs.instrument_id"
  [ "${idx}" = "1" ] && ok "индекс idx_agent_outputs есть" || bad "нет индекса idx_agent_outputs"
  missing=""
  for table in demo_state demo_orders demo_balance_daily agent_outputs signals instruments; do
    granted="$(psql_q "SELECT has_table_privilege('agenttrade_ro', 'public.${table}', 'SELECT');")"
    [ "${granted}" = "t" ] || missing="${missing} ${table}"
  done
  if [ -z "${missing}" ]; then
    ok "роль agenttrade_ro читает demo_state, demo_orders, demo_balance_daily, agent_outputs, signals, instruments"
  else
    bad "у agenttrade_ro нет SELECT на:${missing}"
  fi
  can_settings="$(psql_q "SELECT has_table_privilege(current_user, 'public.user_settings', 'SELECT');")"
  [ "${can_settings}" = "t" ] && ok "служба demo читает user_settings (тихие часы)" \
    || bad "служба demo не читает user_settings — сообщения о сделках всегда со звуком"
fi

# --- 6. Режим и флаги, от которых зависят тексты -----------------------------------------
echo "6. Флаги в .env (тексты бота строятся от них)"
echo "  ⓘ DEMO_ENABLED: $(env_val DEMO_ENABLED); DEMO_NOTIFY_ENABLED: $(env_val DEMO_NOTIFY_ENABLED); NOTIFY_SIGNALS_ENABLED: $(env_val NOTIFY_SIGNALS_ENABLED); VIRTUAL_OUTPUT_ENABLED: $(env_val VIRTUAL_OUTPUT_ENABLED)"
echo "  ⓘ (не задан = действует умолчание кода: DEMO_NOTIFY_ENABLED=true, NOTIFY_SIGNALS_ENABLED=false)"

# --- 7. Версия main на сервере --------------------------------------------------------------
echo "7. Версия кода на сервере"
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
  for file in src/bot/screens.py src/bot/nav.py src/core/fmt.py; do
    [ -f "${APP_DIR}/${file}" ] && ok "${file} на месте" || bad "нет файла ${file} — этап 9.7 не развёрнут"
  done
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
echo
echo "Осталось проверить глазами на телефоне: /menu, восемь кнопок, «🏠 Меню», «🔄 Обновить», меню команд слева от поля ввода."
