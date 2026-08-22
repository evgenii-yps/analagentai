#!/usr/bin/env bash
# Замеры ресурсов при расширении состава инструментов (§3 ТЗ 8.1).
#
# Печатает в stdout И сохраняет в файл:
#   * процессор и память по каждому контейнеру;
#   * размер каждой таблицы БД и общий размер базы;
#   * свободное место на диске;
#   * число обращений к OKX в минуту и число ответов 50011;
#   * задержку ответа биржи: медиана и 95-й процентиль;
#   * возраст самой свежей свечи по каждому инструменту.
#
# Пороги ОСТАНОВКИ расширения (§3 ТЗ 8.1) проверяются здесь же, и итоговая
# строка говорит прямо: расширяться можно или нельзя.
#
# Запуск:  bash scripts/measure_load.sh [метка]
# Файл:    logs/measure_load_<метка>_<дата-время>.txt
#
# Только стандартные средства сервера: docker, psql в контейнере, df, awk.

set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LABEL="${1:-run}"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
OUT_DIR="${MEASURE_OUT_DIR:-${APP_DIR}/logs}"
OUT_FILE="${OUT_DIR}/measure_load_${LABEL}_${STAMP}.txt"

# Окно, за которое считаются обращения к бирже и задержки (минут).
WINDOW_MIN="${MEASURE_WINDOW_MIN:-10}"

# Пороги остановки расширения (§3 ТЗ 8.1).
MEM_LIMIT_PCT="${MEASURE_MEM_LIMIT_PCT:-75}"
DISK_FREE_MIN_PCT="${MEASURE_DISK_FREE_MIN_PCT:-40}"
RATE_LIMIT_MAX="${MEASURE_RATE_LIMIT_MAX:-0}"      # 50011 за час: допускается 0
CANDLE_AGE_INTERVALS="${MEASURE_CANDLE_AGE_INTERVALS:-2}"

mkdir -p "$OUT_DIR"

_env() {  # значение из .env (или значение по умолчанию)
  local key="$1" default="${2:-}"
  local value
  value="$(grep -E "^${key}=" "${APP_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
           | sed 's/#.*//' | xargs || true)"
  echo "${value:-$default}"
}

PG_USER="$(_env POSTGRES_USER agenttrade)"
PG_DB="$(_env POSTGRES_DB agenttrade)"
AGENT_TIMEFRAME="$(_env AGENT_TIMEFRAME 1h)"
COMPOSE="docker compose"

psql_q() {  # тихий psql: одна строка ответа
  $COMPOSE exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -t -A -F'|' -c "$1" 2>/dev/null
}

BLOCKING=0
WARNINGS=0
block() { echo "БЛОКИРУЮЩЕЕ: $*"; BLOCKING=$((BLOCKING + 1)); }
warn()  { echo "ВНИМАНИЕ:    $*"; WARNINGS=$((WARNINGS + 1)); }
ok()    { echo "норма:       $*"; }

{
echo "=============================================================================="
echo " ЗАМЕР РЕСУРСОВ (Этап 8.1 §3) — метка «${LABEL}»"
echo "=============================================================================="
echo " Время (UTC):    $(date -u +%FT%TZ)"
echo " Каталог:        ${APP_DIR}"
echo " Окно счётчиков: ${WINDOW_MIN} мин"
echo " Состав:         SYMBOLS=$(_env SYMBOLS "$(_env SYMBOL BTC/USDT) (одна пара)")"
echo " Горизонты:      EVAL_HORIZONS=$(_env EVAL_HORIZONS 1,4,12,24)"
echo " Версия логики:  LOGIC_VERSION=$(_env LOGIC_VERSION 5)"

echo
echo "=== 1. Процессор и память по контейнерам ==="
docker stats --no-stream \
  --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
  || echo "docker stats недоступен"

echo
echo "=== 2. Память сервера ==="
free -m 2>/dev/null | awk 'NR<=2'
MEM_USED_PCT="$(free -m 2>/dev/null | awk '/^Mem:/ {printf "%.1f", $3/$2*100}')"
echo "Использовано памяти: ${MEM_USED_PCT:-н/д}% (порог остановки: ${MEM_LIMIT_PCT}%)"

echo
echo "=== 3. Размер таблиц и базы ==="
psql_q "
  SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)),
         pg_total_relation_size(c.oid),
         (SELECT reltuples::bigint FROM pg_class WHERE oid = c.oid)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'r'
   ORDER BY pg_total_relation_size(c.oid) DESC;" \
| awk -F'|' 'BEGIN{printf "%-24s %12s %14s %14s\n","таблица","размер","байт","строк(оценка)"}
             {printf "%-24s %12s %14s %14s\n",$1,$2,$3,$4}'
echo "Размер базы целиком: $(psql_q "SELECT pg_size_pretty(pg_database_size(current_database()));")"

echo
echo "=== 4. Свободное место на диске ==="
df -h "$APP_DIR" 2>/dev/null | awk 'NR<=2'
DISK_FREE_PCT="$(df --output=pcent "$APP_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')"
DISK_FREE_PCT=$((100 - ${DISK_FREE_PCT:-100}))
echo "Свободно: ${DISK_FREE_PCT}% (порог остановки: меньше ${DISK_FREE_MIN_PCT}%)"

echo
echo "=== 5. Обращения к OKX и ответы 50011 ==="
# Считаются по поминутным сводкам коллектора («Сбор: сводка окна»): успешная
# итерация логируется на DEBUG, который в продакшне выключен, поэтому сводка
# и введена (см. src/collectors/base.py). Числа берутся из лога, а не из догадок.
LOG_WINDOW="$($COMPOSE logs --since "${WINDOW_MIN}m" collector 2>/dev/null || true)"
LOG_HOUR="$($COMPOSE logs --since 60m collector 2>/dev/null || true)"
read -r REQ_TOTAL RATE_LIMIT_WINDOW <<EOF_STATS
$(printf '%s' "$LOG_WINDOW" | python3 -c '
import json, sys
requests = limited = 0
for line in sys.stdin:
    part = line.split("| ", 1)[-1].strip()
    if not part.startswith("{"):
        continue
    try:
        row = json.loads(part)
    except ValueError:
        continue
    if row.get("event") == "Сбор: сводка окна":
        requests += int(row.get("requests") or 0)
        limited += int(row.get("rate_limited") or 0)
print(requests, limited)
' 2>/dev/null || echo "0 0")
EOF_STATS
RATE_LIMIT_1H="$(printf '%s' "$LOG_HOUR" | grep -c '50011' || true)"
echo "Успешных обращений за ${WINDOW_MIN} мин: ${REQ_TOTAL:-0}"
if [ "${WINDOW_MIN}" -gt 0 ]; then
  echo "В минуту (среднее):                  $(( ${REQ_TOTAL:-0} / WINDOW_MIN ))"
fi
echo "Ответов 50011 за ${WINDOW_MIN} мин:   ${RATE_LIMIT_WINDOW:-0}"
echo "Ответов 50011 за час:                 ${RATE_LIMIT_1H} (порог остановки: больше ${RATE_LIMIT_MAX})"

echo
echo "=== 6. Задержка ответа биржи (медиана и 95-й процентиль) ==="
# Измеряется отдельными запросами к публичному эндпоинту тикера: логи времени
# ответа не содержат, а выдумывать его нельзя.
LAT_FILE="$(mktemp)"
API_BASE="${MEASURE_API_BASE:-https://www.okx.com}"
for _ in $(seq 1 "${MEASURE_LATENCY_SAMPLES:-15}"); do
  # MEASURE_CURL_EXTRA — дополнительные ключи curl (например, --cacert для
  # частного CA или --resolve). На сервере не нужны и по умолчанию пусты.
  # shellcheck disable=SC2086
  curl -s ${MEASURE_CURL_EXTRA:-} -o /dev/null -w '%{time_total}\n' \
       "${API_BASE}/api/v5/market/ticker?instId=BTC-USDT" >> "$LAT_FILE" 2>/dev/null || true
  sleep 0.3
done
if [ -s "$LAT_FILE" ]; then
  sort -n "$LAT_FILE" | awk '
    {v[NR] = $1}
    END {
      if (NR == 0) { print "нет измерений"; exit }
      medi = int((NR + 1) / 2); if (medi < 1) medi = 1
      p95i = int(NR * 0.95 + 0.999); if (p95i < 1) p95i = 1; if (p95i > NR) p95i = NR
      printf "Измерений: %d, медиана: %.0f мс, 95-й процентиль: %.0f мс\n",
             NR, v[medi] * 1000, v[p95i] * 1000
    }'
else
  echo "Задержка не измерена (биржа недоступна из этой среды)"
fi
rm -f "$LAT_FILE"

echo
echo "=== 7. Возраст самой свежей свечи по каждому инструменту ==="
psql_q "
  SELECT i.symbol, o.timeframe,
         to_char(max(o.ts), 'YYYY-MM-DD HH24:MI:SS'),
         round(extract(epoch FROM (now() - max(o.ts))))
    FROM ohlcv o JOIN instruments i ON i.id = o.instrument_id
   WHERE o.timeframe = '${AGENT_TIMEFRAME}'
   GROUP BY i.symbol, o.timeframe ORDER BY i.symbol;" \
| awk -F'|' 'BEGIN{printf "%-16s %-6s %-21s %12s\n","инструмент","тф","свежая свеча","возраст, с"}
             {printf "%-16s %-6s %-21s %12s\n",$1,$2,$3,$4}'

echo
echo "=== 8. Прогноз объёма БД (§4 ТЗ 8.1) ==="
# Прогноз строится на ФАКТИЧЕСКОМ притоке за последний час и фактическом
# размере строки, а не на предположениях: сколько будет сделок, решает биржа.
# Стационарный объём = приток в сутки × срок хранения; для вечных таблиц
# приведён месячный прирост, который не останавливается никогда.
RET_1M="$(_env RETENTION_1M_DAYS 30)"
RET_OB="$(_env RETENTION_ORDERBOOK_DAYS 14)"
RET_TR="$(_env RETENTION_TRADES_DAYS 30)"
psql_q "
  WITH rate AS (
    SELECT 'ohlcv_1m' AS t,
           (SELECT count(*) FROM ohlcv WHERE timeframe='1m' AND ts > now()-interval '1 hour') AS per_hour,
           ${RET_1M} AS keep_days,
           (SELECT CASE WHEN sum(n_live_tup) > 0
                        THEN pg_total_relation_size('ohlcv')::numeric / sum(n_live_tup)
                        ELSE 0 END
              FROM pg_stat_user_tables WHERE relname='ohlcv') AS bytes_row
    UNION ALL
    SELECT 'orderbook_snapshots',
           (SELECT count(*) FROM orderbook_snapshots WHERE ts > now()-interval '1 hour'),
           ${RET_OB},
           (SELECT CASE WHEN sum(n_live_tup) > 0
                        THEN pg_total_relation_size('orderbook_snapshots')::numeric / sum(n_live_tup)
                        ELSE 0 END
              FROM pg_stat_user_tables WHERE relname='orderbook_snapshots')
    UNION ALL
    SELECT 'trades',
           (SELECT count(*) FROM trades WHERE ts > now()-interval '1 hour'),
           ${RET_TR},
           (SELECT CASE WHEN sum(n_live_tup) > 0
                        THEN pg_total_relation_size('trades')::numeric / sum(n_live_tup)
                        ELSE 0 END
              FROM pg_stat_user_tables WHERE relname='trades')
  )
  SELECT t,
         (per_hour * 24)::bigint,
         round(bytes_row),
         round(per_hour * 24 * bytes_row / 1048576.0, 1),
         round(per_hour * 24 * keep_days * bytes_row / 1073741824.0, 2)
    FROM rate ORDER BY 5 DESC NULLS LAST;" | awk -F'|' 'BEGIN{printf "%-22s %14s %12s %14s %18s\n","таблица","строк/сут","байт/строка","МБ/сут","стационар, ГБ"}
             {printf "%-22s %14s %12s %14s %18s\n",$1,$2,$3,$4,$5}'
echo "Вечные таблицы (не удаляются никогда): часовые свечи, funding, открытый"
echo "интерес, сигналы, оценки — их объём растёт линейно и в стационар не входит."

echo
echo "=== 9. Пороги остановки расширения (§3 ТЗ 8.1) ==="
if [ -n "${MEM_USED_PCT:-}" ] && awk -v m="${MEM_USED_PCT:-0}" -v l="$MEM_LIMIT_PCT" 'BEGIN{exit !(m>l)}'; then
  block "память сервера ${MEM_USED_PCT}% > ${MEM_LIMIT_PCT}% — расширять состав НЕЛЬЗЯ"
else
  ok "память сервера ${MEM_USED_PCT:-н/д}% (порог ${MEM_LIMIT_PCT}%)"
fi
if [ "${DISK_FREE_PCT:-100}" -lt "$DISK_FREE_MIN_PCT" ]; then
  block "свободно ${DISK_FREE_PCT}% < ${DISK_FREE_MIN_PCT}% — расширять состав НЕЛЬЗЯ"
else
  ok "свободного места ${DISK_FREE_PCT}% (порог ${DISK_FREE_MIN_PCT}%)"
fi
if [ "${RATE_LIMIT_1H:-0}" -gt "$RATE_LIMIT_MAX" ]; then
  block "ответов 50011 за час: ${RATE_LIMIT_1H} — расширять состав НЕЛЬЗЯ"
else
  ok "ответов 50011 за час: ${RATE_LIMIT_1H:-0}"
fi

# Возраст свежей свечи: порог — два интервала таймфрейма.
TF_SEC="$(psql_q "SELECT CASE '${AGENT_TIMEFRAME}'
                    WHEN '1m' THEN 60 WHEN '5m' THEN 300 WHEN '15m' THEN 900
                    WHEN '30m' THEN 1800 WHEN '1h' THEN 3600 WHEN '4h' THEN 14400
                    ELSE 3600 END;")"
MAX_AGE=$(( ${TF_SEC:-3600} * CANDLE_AGE_INTERVALS ))
STALE="$(psql_q "
  SELECT count(*) FROM (
    SELECT i.symbol, max(o.ts) AS last_ts
      FROM ohlcv o JOIN instruments i ON i.id = o.instrument_id
     WHERE o.timeframe = '${AGENT_TIMEFRAME}'
     GROUP BY i.symbol
  ) q WHERE extract(epoch FROM (now() - last_ts)) > ${MAX_AGE};")"
if [ "${STALE:-0}" -gt 0 ]; then
  block "у ${STALE} инструментов свежая свеча старше ${MAX_AGE} c (два интервала) — расширять состав НЕЛЬЗЯ"
else
  ok "возраст свежей свечи в пределах ${MAX_AGE} c у всех инструментов"
fi

echo
echo "=============================================================================="
if [ "$BLOCKING" -gt 0 ]; then
  echo " ИТОГ: пороги НАРУШЕНЫ (${BLOCKING}). Расширение состава ОСТАНОВИТЬ,"
  echo "       вернуться к предыдущему числу токенов и разбираться с причиной."
else
  echo " ИТОГ: пороги НЕ нарушены. Расширение состава допустимо."
fi
echo " Замечаний «требует внимания»: ${WARNINGS}"
echo "=============================================================================="
} 2>&1 | tee "$OUT_FILE"

echo "Замер сохранён: ${OUT_FILE}"
exit 0
