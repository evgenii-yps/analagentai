#!/usr/bin/env bash
# Проверка развёртывания Этапа 8.1 (пять токенов, четыре горизонта оценки).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: docker compose ps / SELECT. Ничего не меняет и не перезапускает.
#
#   cd /opt/agent-trade && bash deploy/verify_8_1.sh
#
# ТРЕБОВАНИЕ §11.7 ТЗ: замечания делятся на два класса — БЛОКИРУЮЩИЕ и
# «требует внимания, откат не нужен». Итоговая строка прямо говорит, что делать.
# Единый вердикт «плохо» на разнородные замечания недопустим: он заставляет
# откатывать то, что откатывать не нужно.
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

blocking=0     # мешает работе или искажает данные
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
env_value() {
  local key="$1" default="${2:-}"
  local value
  value="$(grep -E "^${key}=" "${APP_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
           | sed 's/#.*//' | xargs || true)"
  echo "${value:-$default}"
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.1 — пять токенов, четыре горизонта оценки"
echo " Время (UTC): $(date -u +%FT%TZ)"
echo "=============================================================================="

echo
echo "=== 1. Состав инструментов (§1) ==="
SYMBOLS_RAW="$(env_value SYMBOLS "")"
if [ -z "$SYMBOLS_RAW" ]; then
  warn "SYMBOLS не задан — работает одна пара из SYMBOL/SWAP_SYMBOL (поведение до 8.1)"
else
  pairs_ok=true
  IFS=',' read -ra items <<< "$SYMBOLS_RAW"
  for item in "${items[@]}"; do
    spot="${item%%:*}"
    swap="${item#*:}"
    if [ "$spot" = "$item" ] || [ -z "$swap" ]; then
      block "SYMBOLS: «$item» не пара «спот:контракт». Достраивание имени контракта запрещено (§1)"
      pairs_ok=false
    fi
  done
  $pairs_ok && ok "SYMBOLS: ${#items[@]} пар — $SYMBOLS_RAW"
fi

# Свечи собираются ТОЛЬКО по споту, funding/OI — ТОЛЬКО по контракту.
split_bad="$(psql_q "
  SELECT count(*) FROM (
    SELECT i.id FROM instruments i JOIN ohlcv o ON o.instrument_id = i.id
     WHERE i.type = 'swap'
    UNION ALL
    SELECT i.id FROM instruments i JOIN funding f ON f.instrument_id = i.id
     WHERE i.type = 'spot'
  ) q;")"
if [ "${split_bad:-0}" != "0" ]; then
  block "рынки перепутаны: свечи на контракте или funding на споте (${split_bad} строк)"
else
  ok "рынки разведены: свечи — спот, funding/открытый интерес — контракт"
fi

echo
echo "=== 2. Поэтапное включение (§2) ==="
tokens_live="$(psql_q "
  SELECT count(DISTINCT i.id) FROM instruments i
   WHERE i.type = 'spot'
     AND EXISTS (SELECT 1 FROM ohlcv o
                  WHERE o.instrument_id = i.id
                    AND o.ts > now() - interval '30 minutes');")"
info "инструментов со свежими свечами (30 мин): ${tokens_live:-0}"
if [ "${tokens_live:-0}" -gt 2 ]; then
  info "состав расширен сверх двух токенов — замеры §3 обязаны быть приложены за оба шага"
fi

echo
echo "=== 3. Пороги ресурсов (§3) ==="
mem_pct="$(free -m 2>/dev/null | awk '/^Mem:/ {printf "%.0f", $3/$2*100}')"
if [ -n "$mem_pct" ] && [ "$mem_pct" -gt 75 ]; then
  block "память сервера ${mem_pct}% > 75% — расширять состав нельзя"
else
  ok "память сервера ${mem_pct:-н/д}% (порог 75%)"
fi
disk_used="$(df --output=pcent "$APP_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')"
disk_free=$((100 - ${disk_used:-100}))
if [ "$disk_free" -lt 40 ]; then
  block "свободно ${disk_free}% < 40% — расширять состав нельзя"
else
  ok "свободного места ${disk_free}% (порог 40%)"
fi
rate_limit="$(docker compose logs --since 60m collector 2>/dev/null | grep -c '50011' || true)"
if [ "${rate_limit:-0}" -gt 0 ]; then
  block "ответов 50011 за час: ${rate_limit} — темп запросов превышен"
else
  ok "ответов 50011 за час: 0"
fi
tf="$(env_value AGENT_TIMEFRAME 1h)"
stale="$(psql_rows "
  SELECT i.symbol, round(extract(epoch FROM (now() - max(o.ts))))
    FROM ohlcv o JOIN instruments i ON i.id = o.instrument_id
   WHERE o.timeframe = '${tf}'
   GROUP BY i.symbol
  HAVING extract(epoch FROM (now() - max(o.ts))) >
         2 * CASE '${tf}' WHEN '1m' THEN 60 WHEN '5m' THEN 300 WHEN '15m' THEN 900
                          WHEN '1h' THEN 3600 WHEN '4h' THEN 14400 ELSE 3600 END;")"
if [ -n "$stale" ]; then
  block "свежая свеча старше двух интервалов: $(echo "$stale" | tr '\n' ' ')"
else
  ok "возраст свежей свечи в пределах двух интервалов у всех инструментов"
fi

echo
echo "=== 4. Срок хранения (§4) ==="
ret_1m="$(env_value RETENTION_1M_DAYS "")"
ret_ob="$(env_value RETENTION_ORDERBOOK_DAYS "")"
[ "$ret_1m" = "30" ] && ok "RETENTION_1M_DAYS=30" \
  || warn "RETENTION_1M_DAYS=${ret_1m:-не задан} (ТЗ 8.1 §4 требует 30)"
[ "$ret_ob" = "14" ] && ok "RETENTION_ORDERBOOK_DAYS=14" \
  || warn "RETENTION_ORDERBOOK_DAYS=${ret_ob:-не задан} (ТЗ 8.1 §4 требует 14)"
if crontab -l -u "${APP_USER:-agent}" 2>/dev/null | grep -q "retention.py" \
   || grep -rqs "retention.py" /etc/cron.d/ 2>/dev/null; then
  ok "ежесуточная задача очистки найдена в cron"
else
  warn "задача retention.py в cron не найдена — очистка не выполняется автоматически"
fi
# Защищённое не удаляется: часовые свечи старше срока обязаны быть на месте.
old_1h="$(psql_q "SELECT count(*) FROM ohlcv
                   WHERE timeframe <> '1m' AND ts < now() - interval '31 days';")"
info "часовых (и прочих не 1m) свечей старше 31 дня: ${old_1h:-0} — они не удаляются никогда"

# Лента сделок: сырьё живёт трое суток, итоги минут — навсегда (решение по §4.3).
ret_tr="$(env_value RETENTION_TRADES_DAYS "")"
[ "$ret_tr" = "2" ] && ok "RETENTION_TRADES_DAYS=2" \
  || warn "RETENTION_TRADES_DAYS=${ret_tr:-не задан} (решение по §4.3 — 2 суток)"
has_flow="$(psql_q "SELECT count(*) FROM information_schema.tables
                     WHERE table_schema='public' AND table_name='trade_flow_1m';")"
if [ "${has_flow:-0}" != "1" ]; then
  block "таблицы trade_flow_1m нет — миграция 010 не применена, а сырьё сделок"
  info  "удаляется через трое суток: содержательная часть ленты потеряется"
else
  ok "таблица поминутных итогов trade_flow_1m есть"
  # Свёртка не должна отставать: сырьё старше трёх суток удаляется, и если
  # итоги отстают больше чем на сутки, часть ленты уйдёт несвёрнутой.
  flow_lag="$(psql_q "SELECT coalesce(round(extract(epoch FROM
                        (now() - max(ts))) / 3600.0), 999) FROM trade_flow_1m;")"
  raw_from="$(psql_q "SELECT coalesce(round(extract(epoch FROM
                        (now() - min(ts))) / 3600.0), 0) FROM trades;")"
  if [ "${flow_lag:-999}" -gt 48 ] && [ "${raw_from:-0}" -gt 48 ]; then
    block "поминутные итоги отстают на ${flow_lag} ч при сырье глубиной ${raw_from} ч:"
    info  "ежесуточная задача не выполняется — сырьё удалится несвёрнутым"
  else
    ok "поминутные итоги свежие (отставание ${flow_lag} ч; свёртка идёт до удаления)"
  fi
fi
ret_ao="$(env_value RETENTION_AGENT_OUTPUTS_DAYS "")"
info "RETENTION_AGENT_OUTPUTS_DAYS=${ret_ao:-не задан} (журнал выводов агентов)"
has_daily="$(psql_q "SELECT count(*) FROM information_schema.tables
                      WHERE table_schema='public' AND table_name='agent_outputs_daily';")"
if [ "${has_daily:-0}" != "1" ]; then
  block "таблицы agent_outputs_daily нет — миграция 011 не применена, а журнал"
  info  "выводов удаляется через ${ret_ao:-90} суток: история поведения агентов потеряется"
else
  ok "таблица суточных итогов agent_outputs_daily есть"
  daily_lag="$(psql_q "SELECT coalesce((CURRENT_DATE - max(day)), 999)
                         FROM agent_outputs_daily;")"
  raw_days="$(psql_q "SELECT coalesce((CURRENT_DATE - min(ts)::date), 0)
                        FROM agent_outputs;")"
  if [ "${daily_lag:-999}" -gt 3 ] && [ "${raw_days:-0}" -gt 3 ]; then
    block "суточные итоги отстают на ${daily_lag} дн. при журнале глубиной ${raw_days} дн.:"
    info  "ежесуточная задача не выполняется — журнал удалится несвёрнутым"
  else
    ok "суточные итоги свежие (отставание ${daily_lag} дн.)"
  fi
  # Сутки на границе версий обязаны давать ДВЕ строки, а не одну смешанную.
  mixed_days="$(psql_q "SELECT count(*) FROM (
                          SELECT day, agent, instrument_id FROM agent_outputs_daily
                           GROUP BY day, agent, instrument_id
                          HAVING count(DISTINCT logic_version) > 1) q;")"
  info "суток с несколькими версиями логики: ${mixed_days:-0} (они разделены по строкам)"

  # Дефект 22.08.2026: выводам раньше самой ранней записанной границы
  # подставлялась минимальная известная версия. Строка, целиком лежащая раньше
  # начала СВОЕЙ версии, — признак этой подстановки. Таблица вечная, сырьё
  # живёт 90 суток: не поймав это сейчас, проверить будет уже нечем.
  false_version="$(psql_q "SELECT count(*) FROM agent_outputs_daily d
                             JOIN logic_version_windows w USING (logic_version)
                            WHERE d.logic_version > 0
                              AND d.day < w.started_at::date;")"
  if [ "${false_version:-0}" != "0" ]; then
    block "строк итогов с ЗАВЕДОМО ЛОЖНОЙ версией: ${false_version}"
    info  "сутки целиком раньше начала версии, которой они помечены — примените"
    info  "миграцию 012 (db/migrations/012_unknown_logic_version.sql)"
  else
    ok "ложных версий в суточных итогах нет"
  fi
  unknown_rows="$(psql_q "SELECT count(*) FROM agent_outputs_daily
                           WHERE logic_version = 0;")"
  info "строк с признаком «версия неизвестна» (logic_version = 0): ${unknown_rows:-0}"
  info "это честное «неизвестно» для выводов раньше первой записанной границы,"
  info "а не сбой: подстановка ближайшей версии запрещена"
  has_check="$(psql_q "SELECT count(*) FROM pg_constraint
                        WHERE conname = 'logic_version_windows_version_positive';")"
  if [ "${has_check:-0}" != "1" ]; then
    block "нет ограничения logic_version > 0 на logic_version_windows:"
    info  "ноль перестанет быть отличим от реальной версии — миграция 012 не применена"
  else
    ok "ноль зарезервирован под «неизвестно» ограничением на logic_version_windows"
  fi
fi
info "мнения, участвовавшие в решении, остаются навсегда в signals.agents_payload"

echo
echo "=== 5. Версия логики и граница данных (§6) ==="
lv_env="$(env_value LOGIC_VERSION "")"
lv_db="$(psql_q "SELECT max(logic_version) FROM signals;")"
# Формат с 'T' вместо пробела: psql_q сжимает пробелы, и «2026-08-21 06:30»
# превратилось бы в «2026-08-2106:30».
boundary="$(psql_q "SELECT to_char(started_at,'YYYY-MM-DD\"T\"HH24:MI') FROM logic_version_windows WHERE logic_version = ${lv_env:-5};")"
[ "$lv_env" = "5" ] && ok "LOGIC_VERSION=5 в .env" \
  || block "LOGIC_VERSION=${lv_env:-не задан}, ожидается 5 (§6)"
[ "${lv_db:-0}" = "${lv_env:-5}" ] && ok "сигналы пишутся с logic_version=${lv_db}" \
  || warn "в signals максимальная версия ${lv_db:-нет данных}, в .env ${lv_env}"
if [ -n "$boundary" ]; then
  ok "граница версии зафиксирована: ${boundary} UTC"
else
  block "граница версии логики не зафиксирована (таблица logic_version_windows пуста)"
fi
mixed="$(psql_q "SELECT count(DISTINCT logic_version) FROM signals
                  WHERE ts > now() - interval '24 hours';")"
[ "${mixed:-1}" -le 1 ] && ok "за сутки сигналы одной версии логики" \
  || warn "за сутки встречаются версии: смешивать их в анализе нельзя (§6)"

echo
echo "=== 6. Четыре горизонта оценки (§5) ==="
has_col="$(psql_q "SELECT count(*) FROM information_schema.columns
                    WHERE table_name='signal_evaluations' AND column_name='horizon_h';")"
if [ "${has_col:-0}" = "1" ]; then
  ok "колонка horizon_h есть"
else
  block "колонки horizon_h нет — миграция 009 не применена"
fi
pk="$(psql_q "SELECT count(*) FROM pg_constraint
               WHERE conrelid='signal_evaluations'::regclass AND contype='p';")"
info "первичный ключ таблицы оценок: $(psql_rows "
  SELECT string_agg(a.attname, ',') FROM pg_constraint c
  JOIN unnest(c.conkey) k ON TRUE
  JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k
  WHERE c.conrelid='signal_evaluations'::regclass AND c.contype='p';" | head -1)"
[ "${pk:-0}" = "1" ] || warn "первичный ключ таблицы оценок не найден"
nulls="$(psql_q "SELECT count(*) FROM signal_evaluations WHERE horizon_h IS NULL;")"
[ "${nulls:-0}" = "0" ] && ok "пустых horizon_h нет" \
  || block "строк с пустым horizon_h: ${nulls}"
echo "  Оценок по горизонтам (за всё время):"
psql_rows "SELECT horizon_h, count(*) FROM signal_evaluations
            GROUP BY horizon_h ORDER BY horizon_h;" \
  | awk -F'|' '{printf "     %4s ч: %s\n", $1, $2}'
horizons_env="$(env_value EVAL_HORIZONS "1,4,12,24")"
info "EVAL_HORIZONS=${horizons_env}"

echo
echo "=== 7. Незавершённый бар (§8) ==="
closed_only="$(env_value MARKET_CLOSED_BARS_ONLY "false")"
if [ "$closed_only" = "false" ]; then
  ok "MARKET_CLOSED_BARS_ONLY=false — поведение системы не изменено (так и требуется в 8.1)"
else
  warn "MARKET_CLOSED_BARS_ONLY=${closed_only}: в Этапе 8.1 значение менять НЕ требовалось"
fi

echo
echo "=== 8. Контейнеры ==="
docker compose ps --format 'table {{.Service}}\t{{.Status}}' 2>/dev/null | sed 's/^/  /'
restarting="$(docker compose ps --format json 2>/dev/null | grep -c '"State":"restarting"' || true)"
[ "${restarting:-0}" = "0" ] && ok "перезапускающихся контейнеров нет" \
  || block "контейнеров в состоянии restarting: ${restarting}"

echo
echo "=============================================================================="
echo " ИТОГ"
echo "=============================================================================="
echo "  Блокирующих замечаний:              ${blocking}"
echo "  «Требует внимания, откат не нужен»: ${attention}"
if [ "$blocking" -gt 0 ]; then
  echo
  echo "  ЧТО ДЕЛАТЬ: расширение состава ОСТАНОВИТЬ. Вернуть прежний SYMBOLS"
  echo "  (docker compose up -d после правки .env), устранить блокирующие замечания"
  echo "  и повторить проверку. Откатывать миграцию 009 при этом НЕ требуется:"
  echo "  она совместима с прежним составом инструментов."
  exit 1
fi
if [ "$attention" -gt 0 ]; then
  echo
  echo "  ЧТО ДЕЛАТЬ: работать можно, откат не нужен. Пункты «ВНИМАНИЕ» выше"
  echo "  устранить в рабочем порядке."
  exit 0
fi
echo
echo "  ЧТО ДЕЛАТЬ: ничего. Развёртывание Этапа 8.1 в порядке."
exit 0
