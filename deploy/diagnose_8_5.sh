#!/usr/bin/env bash
# Диагностика Этапа 8.5 — перекос решений Market Agent и молчание Futures Agent.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров,
# ни строки в конфигурации. Разовые контейнеры поднимаются с --rm и работающие
# сервисы не трогают.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/diagnose_8_5.sh
#
# Скрипт вызывается КАК ФАЙЛ ИЗ РЕПОЗИТОРИЯ и не зависит от того, попал ли он в
# собранный образ: весь SQL уходит в psql через stdin, весь Python — через
# python -c. Разведка 8.2.0 сорвалась именно на этом.
#
# ПРАВИЛО (§3 ТЗ 8.5): проверка, ищущая текст в журнале, обязана искать его в
# том виде, в каком журнал его хранит. В проде structlog пишет JSON с
# ЭКРАНИРОВАННОЙ кириллицей, поэтому журналы здесь разбираются как JSON, а
# признаки берутся машиночитаемыми ключами.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
DAYS="${DAYS:-3}"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_ok()    { echo "  🟢 $*"; }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -A -F "|" -q -c "$1" 2>&1
}
psql_val() { psql_q "$1" | tail -1; }

echo "=============================================================================="
echo " ДИАГНОСТИКА 8.5 — перекос Market и молчание Futures"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Окно разбора: последние ${DAYS} сут"
echo "=============================================================================="

echo
echo "── 0. Действующие пороги (из контейнера, а не из .env.example) ────────────"
CFG=$(docker compose run --rm --no-deps -T agents python -c "
from src.core.config import settings
print('AGENT_MIN_CANDLES=%d' % settings.AGENT_MIN_CANDLES)
print('AGENT_TIMEFRAME=%s' % settings.AGENT_TIMEFRAME)
print('FUTURES_MIN_POINTS=%d' % settings.FUTURES_MIN_POINTS)
print('FUTURES_LOOKBACK_HOURS=%d' % settings.FUTURES_LOOKBACK_HOURS)
print('FUTURES_PCT_LOW=%s' % settings.FUTURES_PCT_LOW)
print('FUTURES_PCT_HIGH=%s' % settings.FUTURES_PCT_HIGH)
print('MIN_AGENTS=%d' % settings.MIN_AGENTS)
" 2>&1)
echo "${CFG}" | sed 's/^/    /'
MIN_POINTS=$(echo "${CFG}" | sed -n 's/^FUTURES_MIN_POINTS=//p'); MIN_POINTS=${MIN_POINTS:-20}
LOOKBACK=$(echo "${CFG}" | sed -n 's/^FUTURES_LOOKBACK_HOURS=//p'); LOOKBACK=${LOOKBACK:-168}

echo
echo "=============================================================================="
echo " §1. ПОЧЕМУ MARKET AGENT НЕ ДАЁТ bearish"
echo "=============================================================================="

echo
echo "── 1A. Распределение выводов Market по токенам ────────────────────────────"
psql_q "SELECT i.base AS token, count(*) AS outputs, count(*) FILTER (WHERE a.signal='bullish') AS bullish, count(*) FILTER (WHERE a.signal='bearish') AS bearish, count(*) FILTER (WHERE a.signal='neutral') AS neutral, count(*) FILTER (WHERE a.signal='insufficient_data') AS insufficient, min((a.metrics->>'n_candles')::int) AS n_candles_min, max((a.metrics->>'n_candles')::int) AS n_candles_max FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='market' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 1B. Голоса: какой из пяти НИ РАЗУ не уходит в минус ─────────────────────"
echo "    (bearish требует суммы голосов < 0; голос со столбцом minus = 0 и есть"
echo "     невыполняющееся условие)"
psql_q "SELECT i.base AS token, v.key AS vote, count(*) FILTER (WHERE (v.value#>>'{}')::int = -1) AS minus, count(*) FILTER (WHERE (v.value#>>'{}')::int = 0) AS zero, count(*) FILTER (WHERE (v.value#>>'{}')::int = 1) AS plus FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id CROSS JOIN LATERAL jsonb_each(a.metrics->'votes') AS v(key, value) WHERE a.agent='market' AND a.metrics ? 'votes' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base, v.key ORDER BY i.base, v.key;" | sed 's/^/    /'

echo
echo "── 1C. Распределение суммы голосов (score) по токенам ─────────────────────"
psql_q "SELECT i.base AS token, (a.metrics->>'score')::int AS score, count(*) AS n FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='market' AND a.metrics ? 'score' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base, 2 ORDER BY i.base, 2;" | sed 's/^/    /'

NEG=$(psql_val "SELECT count(*) FROM agent_outputs a WHERE a.agent='market' AND a.metrics ? 'score' AND (a.metrics->>'score')::int < 0 AND a.ts >= now() - make_interval(days => ${DAYS});")
if [[ "${NEG}" =~ ^[0-9]+$ ]] && [[ "${NEG}" -eq 0 ]]; then
  note_block "сумма голосов Market НИ РАЗУ не была отрицательной — bearish недостижим"
else
  note_ok "отрицательная сумма голосов встречается (${NEG} раз) — механика жива"
fi

echo
echo "── 1D. Слабый тренд: ADX ниже порога 20 гасит направление в neutral ───────"
psql_q "SELECT i.base AS token, count(*) AS outputs, count(*) FILTER (WHERE (a.metrics->>'adx14')::numeric < 20) AS adx_below_20, round(avg((a.metrics->>'adx14')::numeric), 2) AS adx_avg, round(avg((a.metrics->>'rsi14')::numeric), 2) AS rsi_avg FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='market' AND a.metrics ? 'adx14' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 1E. Движение цены против распределения решений ──────────────────────────"
echo "    (перекос НЕ объясняется рынком, пока движение не измерено — §5 ТЗ)"
psql_q "SELECT i.base AS token, count(*) FILTER (WHERE s.decision='buy') AS buy, count(*) FILTER (WHERE s.decision='sell') AS sell, count(*) FILTER (WHERE s.decision='wait') AS wait, round((100.0*(max(p.last_close)-min(p.first_close))/nullif(min(p.first_close),0))::numeric, 2) AS price_move_pct, min(p.first_close) AS price_first, max(p.last_close) AS price_last FROM signals s JOIN instruments i ON i.id=s.instrument_id JOIN LATERAL (SELECT (SELECT close FROM ohlcv o1 WHERE o1.instrument_id=i.id AND o1.timeframe='1h' AND o1.ts >= now() - make_interval(days => ${DAYS}) ORDER BY o1.ts ASC LIMIT 1) AS first_close, (SELECT close FROM ohlcv o2 WHERE o2.instrument_id=i.id AND o2.timeframe='1h' ORDER BY o2.ts DESC LIMIT 1) AS last_close) p ON TRUE WHERE s.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 1F. Глубина часового ряда по каждому спотовому инструменту ─────────────"
psql_q "SELECT i.base AS token, i.symbol, count(*) AS candles_1h, min(o.ts) AS ts_min, max(o.ts) AS ts_max, round(extract(epoch FROM max(o.ts)-min(o.ts))/3600.0) AS span_hours FROM ohlcv o JOIN instruments i ON i.id=o.instrument_id WHERE o.timeframe='1h' AND i.type='spot' GROUP BY i.base, i.symbol ORDER BY i.base;" | sed 's/^/    /'

echo
echo "=============================================================================="
echo " §2. ПОЧЕМУ FUTURES AGENT МОЛЧИТ"
echo "=============================================================================="

echo
echo "── 2A. Распределение выводов Futures по токенам ───────────────────────────"
psql_q "SELECT i.base AS token, count(*) AS outputs, count(*) FILTER (WHERE a.signal='insufficient_data') AS insufficient, count(*) FILTER (WHERE a.signal<>'insufficient_data') AS meaningful, min((a.metrics->>'n_funding')::int) AS n_funding_min, max((a.metrics->>'n_funding')::int) AS n_funding_max FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='futures' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 2B. Ряд funding по каждому КОНТРАКТУ и расчётная дата перехода порога ───"
echo "    порог: точек в окне ${LOOKBACK} ч должно быть не меньше ${MIN_POINTS}"
psql_q "WITH gaps AS (SELECT f.instrument_id, f.ts - lag(f.ts) OVER (PARTITION BY f.instrument_id ORDER BY f.ts) AS gap FROM funding f), med AS (SELECT instrument_id, percentile_disc(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap FROM gaps WHERE gap IS NOT NULL GROUP BY instrument_id) SELECT i.base AS token, i.symbol AS contract, count(*) AS rows_total, count(*) FILTER (WHERE f.ts >= now() - make_interval(hours => ${LOOKBACK})) AS in_window, greatest(${MIN_POINTS} - count(*) FILTER (WHERE f.ts >= now() - make_interval(hours => ${LOOKBACK})), 0) AS short_by, min(f.ts) AS first_ts, max(f.ts) AS last_ts, m.median_gap AS interval, CASE WHEN count(*) FILTER (WHERE f.ts >= now() - make_interval(hours => ${LOOKBACK})) >= ${MIN_POINTS} THEN 'порог пройден' ELSE to_char(min(f.ts) + (${MIN_POINTS} - 1) * m.median_gap, 'YYYY-MM-DD HH24:MI UTC') END AS threshold_at FROM funding f JOIN instruments i ON i.id=f.instrument_id LEFT JOIN med m ON m.instrument_id=f.instrument_id GROUP BY i.base, i.symbol, m.median_gap ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 2C. Ряд открытого интереса (короткое окно OI сигнал не отменяет) ───────"
psql_q "SELECT i.base AS token, i.symbol AS contract, count(*) AS rows_total, count(*) FILTER (WHERE o.ts >= now() - make_interval(hours => ${LOOKBACK})) AS in_window, min(o.ts) AS first_ts, max(o.ts) AS last_ts FROM open_interest o JOIN instruments i ON i.id=o.instrument_id GROUP BY i.base, i.symbol ORDER BY i.base;" | sed 's/^/    /'

SHORT_N=$(psql_val "SELECT count(*) FROM (SELECT f.instrument_id FROM funding f GROUP BY f.instrument_id HAVING count(*) FILTER (WHERE f.ts >= now() - make_interval(hours => ${LOOKBACK})) < ${MIN_POINTS}) q;")
if [[ "${SHORT_N}" =~ ^[0-9]+$ ]] && [[ "${SHORT_N}" -gt 0 ]]; then
  note_warn "контрактов ниже порога funding: ${SHORT_N} — даты перехода в таблице 2B"
else
  note_ok "все контракты набрали ${MIN_POINTS} точек funding в окне"
fi

echo
echo "── 2D. Учёт отказов: попадает ли молчание агента в agent_failures ─────────"
echo "    записи agent_failures за последние ${DAYS} сут:"
psql_q "SELECT agent, error_type, count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts FROM agent_failures WHERE ts >= now() - make_interval(days => ${DAYS}) GROUP BY agent, error_type ORDER BY agent, error_type;" | sed 's/^/    /'
FUT_FAIL=$(psql_val "SELECT count(*) FROM agent_failures WHERE agent LIKE 'futures%' AND ts >= now() - make_interval(days => ${DAYS});")
FUT_INSUF=$(psql_val "SELECT count(*) FROM agent_outputs WHERE agent='futures' AND signal='insufficient_data' AND ts >= now() - make_interval(days => ${DAYS});")
echo "    выводов futures = insufficient_data: ${FUT_INSUF}; записей в agent_failures: ${FUT_FAIL}"
if [[ "${FUT_INSUF}" =~ ^[0-9]+$ ]] && [[ "${FUT_INSUF}" -gt 0 ]] \
   && [[ "${FUT_FAIL}" =~ ^[0-9]+$ ]] && [[ "${FUT_FAIL}" -eq 0 ]]; then
  note_warn "молчание агента НЕ попадает в счётчик отказов и не даёт оповещения:"
  note_warn "insufficient_data — штатный исход, исключения нет, agent_failures пуст"
fi

echo
echo "=============================================================================="
echo " ИТОГ"
echo "=============================================================================="
echo " Блокирующих находок: ${blocking}"
echo " Находок, требующих внимания без отката: ${attention}"
echo
if [[ "${blocking}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: сумма голосов Market не уходит в минус — передать таблицы 1B и 1C"
  echo " исполнителю: невыполняющееся условие видно по столбцу minus."
elif [[ "${attention}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: правок не требуется, дождаться дат перехода порога из таблицы 2B"
  echo " и перепроверить этим же скриптом; при молчании агента оповещения не будет."
else
  echo " ДЕЙСТВИЕ: отклонений не найдено — перекос объясняется данными, не кодом."
fi
