#!/usr/bin/env bash
# Проверка развёртывания Этапа 7.4 (исторический реплей ядра).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: docker compose ps / SELECT. Прогон этим скриптом НЕ запускается.
#
#   cd /opt/agent-trade && bash deploy/verify_7_4.sh
#
# ТРЕБОВАНИЕ §12.5 ТЗ (введено после инцидента 16.08): замечания делятся на два
# класса — БЛОКИРУЮЩИЕ и «требует внимания, откат не нужен». Итоговая строка
# прямо говорит, что делать. Единый вердикт «плохо» на разнородные замечания
# недопустим: он заставляет откатывать то, что откатывать не нужно.
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

PROD_CONTAINERS=(postgres redis collector agents decision notify evaluator bot)

blocking=0     # мешает прогону или угрожает продакшну
attention=0    # стоит знать, но откат не нужен

block()  { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
warn()   { echo "  🟡 ВНИМАНИЕ:    $*"; attention=$((attention + 1)); }
ok()     { echo "  🟢 $*"; }
info()   { echo "  ℹ  $*"; }

psql_q() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -c "$1" 2>/dev/null \
    | tr -d '[:space:]'
}
psql_rows() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -F '|' -c "$1" 2>/dev/null
}

echo "=== 1. Продакшн-стек не затронут (8 контейнеров) ==="
ps_json="$(docker compose ps --format json 2>/dev/null)"
for c in "${PROD_CONTAINERS[@]}"; do
  line="$(echo "$ps_json" | tr '}' '}\n' | grep "\"Service\":\"$c\"" | head -1)"
  health="$(echo "$line" | grep -o '"Health":"[^"]*"' | cut -d'"' -f4)"
  state="$(echo "$line" | grep -o '"State":"[^"]*"' | cut -d'"' -f4)"
  status="${health:-$state}"
  if [[ "$status" == "healthy" || ( -z "$health" && "$state" == "running" ) ]]; then
    ok "$c: ${status:-running}"
  else
    block "$c не работает (${status:-не запущен}) — сначала продакшн, потом реплей"
  fi
done

echo "=== 2. Контейнер backtest НЕ поднят вместе со стеком ==="
if echo "$ps_json" | grep -q '"Service":"backtest"'; then
  block "контейнер backtest запущен постоянно; он обязан работать только вручную"
else
  ok "backtest в постоянном составе отсутствует (профиль backtest, §12.2)"
fi
if grep -A3 '^  backtest:' docker-compose.yml | grep -q 'profiles: \["backtest"\]'; then
  ok "профиль backtest объявлен в docker-compose.yml"
else
  block "в docker-compose.yml у сервиса backtest нет profiles: [\"backtest\"]"
fi
if grep -A12 '^  backtest:' docker-compose.yml | grep -q 'mem_limit: 1g'; then
  ok "ограничение памяти 1g задано (§12.3)"
else
  warn "не вижу mem_limit: 1g у сервиса backtest"
fi

echo "=== 3. Схема backtest ==="
has_schema="$(psql_q "SELECT 1 FROM information_schema.schemata WHERE schema_name='backtest';")"
if [[ "$has_schema" == "1" ]]; then
  ok "схема backtest создана"
  for t in candles funding gaps runs decisions outcomes; do
    exists="$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='backtest' AND table_name='$t';")"
    [[ "$exists" == "1" ]] && ok "  таблица backtest.$t" || block "нет таблицы backtest.$t"
  done
else
  block "схема backtest отсутствует: примените db/migrations/008_backtest_schema.sql"
fi

echo "=== 4. Роль прогона (запись только в backtest, чтение продакшна) ==="
has_role="$(psql_q "SELECT 1 FROM pg_roles WHERE rolname='agenttrade_bt';")"
if [[ "$has_role" == "1" ]]; then
  ok "роль agenttrade_bt существует"
  can_write_prod="$(psql_q "SELECT has_table_privilege('agenttrade_bt','public.signals','INSERT')::text;")"
  if [[ "$can_write_prod" == "false" ]]; then
    ok "у роли НЕТ права записи в продакшн-таблицу signals"
  else
    block "роль agenttrade_bt может писать в public.signals — отзовите права"
  fi
  can_read_prod="$(psql_q "SELECT has_table_privilege('agenttrade_bt','public.signals','SELECT')::text;")"
  [[ "$can_read_prod" == "true" ]] && ok "чтение продакшна доступно (нужно для сверки §13.2)" \
    || warn "роль не может читать public.signals — сверка §13.2 не выполнится"
else
  warn "роли agenttrade_bt нет: прогон подключится продакшн-пользователем."
  info "создать: CREATE ROLE agenttrade_bt LOGIN PASSWORD '<пароль>';"
  info "затем повторно применить db/migrations/008_backtest_schema.sql (выдаст права)"
fi

echo "=== 5. Конфигурация прогона ==="
if [[ -d backtest/.env.backtest ]]; then
  # Дефект D-9: docker создаёт КАТАЛОГ на месте файла, указанного в volumes,
  # если файла на хосте не было. Дальше конфигурация «не читается» без причины.
  block "backtest/.env.backtest — это КАТАЛОГ (его создал docker, когда файла не было)."
  info "удалите каталог, создайте файл из backtest/.env.backtest.example"
  info "и пересоберите образ: docker compose --profile backtest build --no-cache backtest"
elif [[ -f backtest/.env.backtest ]]; then
  ok "backtest/.env.backtest на месте"

  # Рынки разведены: свечи берутся со спота, funding — с контракта. Пара
  # задаётся явно, достраивание имени контракта запрещено.
  agents_value="$(grep -E '^BT_AGENTS=' backtest/.env.backtest | tail -1 | cut -d= -f2- | awk '{print $1}')"
  if [[ -z "$agents_value" ]]; then
    block "BT_AGENTS пуст: допустимо «market» или «market,futures»"
  elif [[ "$agents_value" == "market" || "$agents_value" == "market,futures" ]]; then
    ok "BT_AGENTS=$agents_value"
  else
    block "BT_AGENTS=$agents_value: допустимо только «market» или «market,futures»"
  fi

  inst_value="$(grep -E '^BT_INSTRUMENTS=' backtest/.env.backtest | tail -1 | cut -d= -f2- | awk '{print $1}')"
  if [[ -z "$inst_value" ]]; then
    block "BT_INSTRUMENTS пуст"
  else
    pairs_ok=true
    IFS=',' read -ra items <<< "$inst_value"
    for item in "${items[@]}"; do
      if [[ "$item" != *:* ]]; then
        if [[ "$agents_value" == *futures* ]]; then
          block "BT_INSTRUMENTS: «$item» без контракта, а BT_AGENTS включает futures."
          info "формат СПОТ:КОНТРАКТ, например BTC-USDT:BTC-USDT-SWAP;"
          info "у спота истории funding нет — биржа отвечает 51000 Parameter instId error"
          pairs_ok=false
        else
          warn "BT_INSTRUMENTS: «$item» без контракта (при BT_AGENTS=market это допустимо)"
        fi
      fi
    done
    [[ "$pairs_ok" == "true" ]] && ok "BT_INSTRUMENTS=$inst_value"
  fi

  for key in BT_PERIOD_FROM BT_REQUEST_PAUSE_MS; do
    value="$(grep -E "^${key}=" backtest/.env.backtest | tail -1 | cut -d= -f2- | awk '{print $1}')"
    if [[ -n "$value" ]]; then
      ok "$key=$value"
    else
      block "$key пуст: его значение определяется зондом scripts/probe_history_depth.py"
    fi
  done
  from_value="$(grep -E '^BT_PERIOD_FROM=' backtest/.env.backtest | cut -d= -f2- | awk '{print $1}')"
  if [[ -n "$from_value" ]]; then
    months="$(psql_q "SELECT round(extract(epoch FROM (now() - '${from_value}'::timestamptz))/2592000.0, 1);")"
    if [[ -n "$months" ]]; then
      awk -v m="$months" 'BEGIN{exit !(m >= 24)}' \
        && ok "глубина периода ${months} мес (>= 24)" \
        || block "глубина периода ${months} мес < 24: инструмент исключается (§4.2)"
    fi
  fi
else
  warn "backtest/.env.backtest не создан — скопируйте backtest/.env.backtest.example"
fi
if [[ -f .env ]] && grep -qE '^BT_' .env; then
  block "параметры BT_* попали в продакшн-.env — их место в backtest/.env.backtest"
else
  ok "продакшн-.env не содержит параметров прогона"
fi

echo "=== 6. Состояние прогонов ==="
if [[ "$has_schema" == "1" ]]; then
  runs="$(psql_rows "SELECT run_id, status, agents_used, to_char(started_at,'YYYY-MM-DD HH24:MI'), coalesce(to_char(finished_at,'YYYY-MM-DD HH24:MI'),'—') FROM backtest.runs ORDER BY run_id DESC LIMIT 5;")"
  if [[ -n "$runs" ]]; then
    info "последние прогоны (id|статус|агенты|начало|конец):"
    echo "$runs" | sed 's/^/     /'
    stuck="$(psql_q "SELECT count(*) FROM backtest.runs WHERE status='running' AND started_at < now() - interval '6 hours';")"
    [[ "${stuck:-0}" == "0" ]] && ok "зависших прогонов нет" \
      || warn "прогонов в статусе running дольше 6 часов: $stuck"
    # Предрегистрация: критерий обязан лежать в config_json.
    without_criterion="$(psql_q "SELECT count(*) FROM backtest.runs WHERE config_json->'criterion' IS NULL;")"
    [[ "${without_criterion:-0}" == "0" ]] && ok "у всех прогонов зафиксирован критерий (§6)" \
      || block "есть прогоны без предрегистрированного критерия: $without_criterion"
    # Сверка §13.2 — блокирующая.
    unverified="$(psql_q "SELECT count(*) FROM backtest.runs WHERE status='ok' AND coalesce((config_json->'criterion'->'parity'->>'blocking_ok')::boolean, false) = false;")"
    if [[ "${unverified:-0}" == "0" ]]; then
      ok "все завершённые прогоны прошли сверку с продакшном (§13.2)"
    else
      block "прогонов без пройденной сверки §13.2: ${unverified} — их результаты публиковать нельзя"
    fi
  else
    info "прогонов ещё не было — это нормально до первого запуска"
  fi
fi

echo "=== 7. Продакшн-данные не тронуты прогоном ==="
if [[ "$has_schema" == "1" ]]; then
  bt_rows="$(psql_q "SELECT coalesce(sum(n),0) FROM (SELECT count(*) AS n FROM backtest.decisions UNION ALL SELECT count(*) FROM backtest.candles) q;")"
  info "строк в схеме backtest: ${bt_rows:-0}"
fi
prod_writable="$(psql_q "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='signals';")"
[[ "${prod_writable:-0}" -gt 0 ]] && ok "продакшн-таблица signals на месте" \
  || block "не вижу public.signals — проверьте, к той ли БД подключились"

echo
echo "==================================================================="
if [[ "$blocking" -eq 0 && "$attention" -eq 0 ]]; then
  echo "ИТОГ: ✅ всё в порядке. Можно запускать прогон:"
  echo "  docker compose --profile backtest run --rm backtest"
  exit 0
elif [[ "$blocking" -eq 0 ]]; then
  echo "ИТОГ: 🟡 замечаний: $attention, все — «требует внимания». ОТКАТ НЕ НУЖЕН."
  echo "Прогон запускать можно; перечисленное выше стоит поправить, когда будет время."
  exit 0
else
  echo "ИТОГ: 🔴 БЛОКИРУЮЩИХ замечаний: $blocking (плюс 'внимание': $attention)."
  echo "Прогон запускать НЕЛЬЗЯ, пока блокирующие не устранены."
  echo "Откат продакшна при этом НЕ требуется: Этап 7.4 ничего в нём не менял."
  exit 1
fi
