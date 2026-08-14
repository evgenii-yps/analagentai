#!/usr/bin/env bash
# Проверка развёртывания Этапа 7.2 (Устойчивость агентов и агрегация).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: команды — docker compose ps / SELECT (флаг -T по ТЗ §2.4).
#
#   cd /opt/agent-trade && bash deploy/verify_7_2.sh
#
# Критерии приёмки, которые проверяет скрипт:
#   * все 8 контейнеров healthy;
#   * колонка signals.degraded существует;
#   * Decision Agent пишет сигналы с logic_version=3;
#   * все три агента пишут выводы КАЖДУЮ минуту (строки за последние 3 минуты).
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

CONTAINERS=(postgres redis collector agents decision notify evaluator bot)
fail=0

psql_q() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -c "$1" 2>/dev/null | tr -d '[:space:]'
}

echo "=== 1. Контейнеры (ожидается 8 healthy/running) ==="
ps_json="$(docker compose ps --format json 2>/dev/null)"
for c in "${CONTAINERS[@]}"; do
  # health при наличии healthcheck, иначе state (у bot healthcheck нет).
  line="$(echo "$ps_json" | tr '}' '}\n' | grep "\"Service\":\"$c\"" | head -1)"
  health="$(echo "$line" | grep -o '"Health":"[^"]*"' | cut -d'"' -f4)"
  state="$(echo "$line" | grep -o '"State":"[^"]*"' | cut -d'"' -f4)"
  status="${health:-$state}"
  if [[ "$status" == "healthy" || ( -z "$health" && "$state" == "running" ) ]]; then
    echo "  🟢 $c: ${status:-running}"
  else
    echo "  🔴 $c: ${status:-не запущен}"; fail=1
  fi
done

echo "=== 2. Миграция: колонка signals.degraded ==="
has_col="$(psql_q "SELECT 1 FROM information_schema.columns WHERE table_name='signals' AND column_name='degraded';")"
if [[ "$has_col" == "1" ]]; then echo "  🟢 колонка degraded есть"; else echo "  🔴 колонки degraded нет"; fail=1; fi

echo "=== 3. Decision Agent пишет logic_version=3 ==="
n_v3="$(psql_q "SELECT count(*) FROM signals WHERE logic_version=3 AND ts > now() - interval '10 minutes';")"
if [[ "${n_v3:-0}" -ge 1 ]]; then
  echo "  🟢 сигналов v3 за 10 мин: $n_v3"
else
  echo "  🔴 сигналов v3 за 10 мин не найдено (Decision Agent молчит или версия не поднята)"; fail=1
fi
first_v3="$(psql_q "SELECT to_char(min(ts) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') FROM signals WHERE logic_version=3;")"
[[ -n "$first_v3" ]] && echo "  ℹ первая запись v3 (граница режимов, UTC): $first_v3"

echo "=== 4. Все три агента пишут выводы каждую минуту (строки за 3 минуты) ==="
for a in market liquidity futures; do
  n="$(psql_q "SELECT count(*) FROM agent_outputs WHERE agent='$a' AND ts > now() - interval '3 minutes';")"
  if [[ "${n:-0}" -ge 1 ]]; then
    echo "  🟢 $a: $n выв. за 3 мин"
  else
    echo "  🔴 $a: нет выводов за 3 мин"; fail=1
  fi
done

echo "=== 5. (инфо) Самовосстановления и сбои агентов за сутки ==="
psql_out="$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -F '|' -c \
  "SELECT agent, error_type, count(*) FROM agent_failures WHERE ts > now() - interval '24 hours' GROUP BY agent, error_type ORDER BY agent, error_type;" 2>/dev/null)"
if [[ -n "$psql_out" ]]; then echo "$psql_out" | sed 's/^/  /'; else echo "  сбоев за сутки не зафиксировано"; fi

echo
if [[ "$fail" == "0" ]]; then
  echo "ИТОГ: ✅ проверки пройдены."
  exit 0
else
  echo "ИТОГ: ❌ есть замечания (см. 🔴 выше). Диагностика: docker compose ps; docker compose logs --tail=100 agents decision"
  exit 1
fi
