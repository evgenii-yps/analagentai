#!/usr/bin/env bash
# Проверка развёртывания Этапа 7.3 (симметрия агентов, калибровка, инерция входов).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: docker compose ps / SELECT (флаг -T обязателен).
#
#   cd /opt/agent-trade && bash deploy/verify_7_3.sh
#
# Проверяет по пунктам:
#   1. восемь контейнеров healthy;
#   2. колонки calibrated_probability, calibration_id, inputs_hash, is_repeat;
#   3. таблица calibration_curves и уникальность активной кривой;
#   4. сигналы с logic_version = 4 появляются;
#   5. все три агента пишут выводы;
#   6. у Futures есть bearish ЛИБО объяснение по фактическому funding;
#   7. inputs_hash заполняется, повторы считаются;
#   8. cron калибровки установлен;
#   9. замороженные параметры .env не изменились.
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

CONTAINERS=(postgres redis collector agents decision notify evaluator bot)
fail=0

psql_q() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -c "$1" 2>/dev/null \
    | tr -d '[:space:]'
}
psql_rows() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -F '|' -c "$1" 2>/dev/null
}

echo "=== 1. Контейнеры (ожидается 8 healthy/running) ==="
ps_json="$(docker compose ps --format json 2>/dev/null)"
for c in "${CONTAINERS[@]}"; do
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

echo "=== 2. Миграция: новые колонки в signals ==="
for col in calibrated_probability calibration_id inputs_hash is_repeat; do
  has="$(psql_q "SELECT 1 FROM information_schema.columns WHERE table_name='signals' AND column_name='$col';")"
  if [[ "$has" == "1" ]]; then echo "  🟢 колонка $col есть"; else echo "  🔴 колонки $col нет"; fail=1; fi
done
# Колонка probability обязана СОХРАНИТЬСЯ: её читают выгрузка, бот и сводка.
has_prob="$(psql_q "SELECT 1 FROM information_schema.columns WHERE table_name='signals' AND column_name='probability';")"
if [[ "$has_prob" == "1" ]]; then
  echo "  🟢 колонка probability на месте (переименования не было)"
else
  echo "  🔴 колонки probability нет — сломается выгрузка, бот и сводка"; fail=1
fi

echo "=== 3. Таблица calibration_curves и уникальность активной кривой ==="
has_tbl="$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_name='calibration_curves';")"
if [[ "$has_tbl" == "1" ]]; then echo "  🟢 таблица calibration_curves создана"; else echo "  🔴 таблицы calibration_curves нет"; fail=1; fi
has_idx="$(psql_q "SELECT 1 FROM pg_indexes WHERE indexname='idx_calibration_active';")"
if [[ "$has_idx" == "1" ]]; then
  echo "  🟢 уникальный индекс активной кривой на месте"
else
  echo "  🔴 нет индекса idx_calibration_active (возможны две активные кривые)"; fail=1
fi
dupes="$(psql_q "SELECT count(*) FROM (SELECT logic_version FROM calibration_curves WHERE is_active GROUP BY logic_version HAVING count(*) > 1) q;")"
if [[ "${dupes:-0}" == "0" ]]; then echo "  🟢 активных кривых не более одной на версию"; else echo "  🔴 найдено несколько активных кривых"; fail=1; fi
curves="$(psql_rows "SELECT logic_version, to_char(built_at,'YYYY-MM-DD HH24:MI'), sample_size, is_active FROM calibration_curves ORDER BY built_at DESC LIMIT 3;")"
if [[ -n "$curves" ]]; then
  echo "  ℹ последние кривые (версия|построена|N|активна):"; echo "$curves" | sed 's/^/     /'
else
  echo "  ℹ кривых пока нет — это ШТАТНО: нужно ${CALIBRATION_MIN_SAMPLES:-60} независимых наблюдений"
  echo "     (≈6 в сутки), то есть около 10 суток работы. Вероятность до этого не показывается."
fi

echo "=== 4. Decision Agent пишет logic_version = 4 ==="
n_v4="$(psql_q "SELECT count(*) FROM signals WHERE logic_version=4 AND ts > now() - interval '10 minutes';")"
if [[ "${n_v4:-0}" -ge 1 ]]; then
  echo "  🟢 сигналов v4 за 10 мин: $n_v4"
else
  echo "  🔴 сигналов v4 за 10 мин нет (Decision Agent молчит или версия не поднята)"; fail=1
fi
first_v4="$(psql_q "SELECT to_char(min(ts) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') FROM signals WHERE logic_version=4;")"
[[ -n "$first_v4" ]] && echo "  ℹ первая запись v4 (граница режимов, UTC): $first_v4"

echo "=== 5. Все три агента пишут выводы (за 3 минуты) ==="
for a in market liquidity futures; do
  n="$(psql_q "SELECT count(*) FROM agent_outputs WHERE agent='$a' AND ts > now() - interval '3 minutes';")"
  if [[ "${n:-0}" -ge 1 ]]; then echo "  🟢 $a: $n выв. за 3 мин"; else echo "  🔴 $a: нет выводов за 3 мин"; fail=1; fi
done

echo "=== 6. Симметрия Futures: есть ли bearish за первые 30 минут ==="
n_bear="$(psql_q "SELECT count(*) FROM agent_outputs WHERE agent='futures' AND signal='bearish' AND ts > now() - interval '30 minutes';")"
n_fut="$(psql_q "SELECT count(*) FROM agent_outputs WHERE agent='futures' AND ts > now() - interval '30 minutes';")"
if [[ "${n_bear:-0}" -ge 1 ]]; then
  echo "  🟢 bearish за 30 мин: $n_bear из ${n_fut:-0} выводов — ветка достижима на живых данных"
else
  # Отсутствие bearish само по себе НЕ дефект: оно должно объясняться данными.
  echo "  ⚠ bearish за 30 мин: 0 из ${n_fut:-0}. Это не обязательно дефект — проверяем данные."
  pct="$(psql_q "SELECT round((count(*) FILTER (WHERE rate <= (SELECT percentile_cont(0.2) WITHIN GROUP (ORDER BY rate) FROM funding WHERE ts > now() - interval '168 hours'))::numeric * 100 / NULLIF(count(*),0), 1) FROM funding WHERE ts > now() - interval '1 hour';")"
  last="$(psql_rows "SELECT round(rate::numeric, 8), to_char(ts,'YYYY-MM-DD HH24:MI') FROM funding ORDER BY ts DESC LIMIT 1;")"
  win="$(psql_rows "SELECT count(*), round(min(rate)::numeric,8), round(percentile_cont(0.2) WITHIN GROUP (ORDER BY rate)::numeric,8), round(percentile_cont(0.8) WITHIN GROUP (ORDER BY rate)::numeric,8), round(max(rate)::numeric,8) FROM funding WHERE ts > now() - interval '168 hours';")"
  echo "     текущий funding (значение|время): ${last:-нет данных}"
  echo "     окно 168 ч (N|min|p20|p80|max):   ${win:-нет данных}"
  echo "     доля времени в нижних 20% за час: ${pct:-нет данных}%"
  echo "     Вывод: bearish появится, когда текущий funding опустится в нижние 20% своего"
  echo "     недельного распределения. Если текущее значение выше p20 — рыночные условия"
  echo "     его сейчас не порождают, и это ожидаемое поведение, а не асимметрия."
  n_points="$(psql_q "SELECT count(*) FROM funding WHERE ts > now() - interval '168 hours';")"
  if [[ "${n_points:-0}" -lt "${FUTURES_MIN_POINTS:-20}" ]]; then
    echo "  🔴 точек funding в окне: ${n_points:-0} < ${FUTURES_MIN_POINTS:-20} — агент отдаёт insufficient_data"
    fail=1
  fi
fi
echo "  ℹ распределение выводов Futures за сутки:"
psql_rows "SELECT signal, count(*) FROM agent_outputs WHERE agent='futures' AND ts > now() - interval '24 hours' GROUP BY signal ORDER BY signal;" | sed 's/^/     /'

echo "=== 7. Инерция входов: inputs_hash и повторы ==="
hashed="$(psql_q "SELECT count(*) FROM signals WHERE inputs_hash IS NOT NULL AND ts > now() - interval '30 minutes';")"
if [[ "${hashed:-0}" -ge 1 ]]; then
  echo "  🟢 сигналов с inputs_hash за 30 мин: $hashed"
  stats="$(psql_rows "SELECT count(*), count(*) FILTER (WHERE is_repeat), count(DISTINCT inputs_hash) FROM signals WHERE ts > now() - interval '24 hours';")"
  echo "  ℹ за сутки (решений|повторов|уникальных наборов): ${stats:-нет данных}"
else
  echo "  🔴 inputs_hash не заполняется"; fail=1
fi

echo "=== 8. Cron калибровки ==="
if grep -q "calibration" /etc/cron.d/agent-trade 2>/dev/null; then
  echo "  🟢 задача установлена:"
  grep "calibration" /etc/cron.d/agent-trade | sed 's/^/     /'
else
  echo "  🔴 в /etc/cron.d/agent-trade нет задачи калибровки"; fail=1
fi

echo "=== 9. Замороженные параметры .env (Этап 7.3 §7) ==="
declare -A FROZEN=(
  [NOTIFY_MIN_PROBABILITY]=0.7 [NOTIFY_COOLDOWN_SEC]=1800 [DECISION_THRESHOLD]=0.3
  [DECISION_INTERVAL]=60 [MIN_AGENTS]=2 [NOTIFY_MIN_AGENTS]=3
  [AGENT_FRESHNESS_SEC]=300 [WEIGHT_MARKET]=1.0 [WEIGHT_LIQUIDITY]=1.0
  [WEIGHT_FUTURES]=1.0 [EVAL_HORIZONS]=1h,4h [EVAL_PRIMARY_HORIZON]=4h
  [NOTIFY_USE_CALIBRATED]=false
)
for key in "${!FROZEN[@]}"; do
  actual="$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- | awk '{print $1}')"
  if [[ "$actual" == "${FROZEN[$key]}" ]]; then
    echo "  🟢 $key=$actual"
  else
    echo "  🔴 $key=${actual:-<нет>} (ожидалось ${FROZEN[$key]})"; fail=1
  fi
done

echo
if [[ "$fail" == "0" ]]; then
  echo "ИТОГ: ✅ проверки пройдены."
  exit 0
else
  echo "ИТОГ: ❌ есть замечания (см. 🔴 выше)."
  echo "Диагностика: docker compose ps; docker compose logs --tail=100 agents decision"
  exit 1
fi
