#!/usr/bin/env bash
# Диагностика Этапа 8.4 — РАСХОЖДЕНИЕ §1: почему лист «Независимые окна» не
# растёт с числом токенов.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров,
# ни строки в конфигурации. Разовый контейнер профиля tools поднимается с --rm
# и работающие сервисы не трогает.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/diagnose_8_4.sh
#
# Скрипт вызывается КАК ФАЙЛ ИЗ РЕПОЗИТОРИЯ и не зависит от того, попал ли он в
# собранный образ: весь SQL передаётся в psql через stdin, весь Python — через
# python -c. Предыдущая разведка сорвалась именно на этом (recon_8_2_0_okx.py
# отсутствовал в образе backtest, собранном раньше).
#
# Скрипт отвечает на ОДИН вопрос: совпадает ли код выгрузки, работающий на
# сервере, с кодом в репозитории. От ответа зависит, что именно чинить.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
LOG="${APP_DIR}/logs/export.log"
BOUNDARY="2026-08-22 22:59:00+00"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_ok()    { echo "  🟢 $*"; }

psql_q() {  # один запрос, одной строкой, через stdin (флаг -T обязателен)
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -A -F "|" -q -c "$1" 2>&1
}

echo "=============================================================================="
echo " ДИАГНОСТИКА 8.4 — какой код выгрузки работает на сервере"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог: ${APP_DIR}"
echo "=============================================================================="

echo
echo "── A. Что лежит В ОБРАЗЕ, из которого запускается выгрузка ────────────────"
# Ключевой признак: функция и оговорка появились в Этапе 8.1. Их отсутствие
# означает, что образ собран РАНЬШЕ 8.1 и весь разбор по токенам не работает.
IMG_PROBE=$(docker compose --profile tools run --rm --no-deps -T export python -c "
import inspect
from src.export import queries
from src.export import transform
import src.export_main as m
has_new = hasattr(queries, 'fetch_independent_by_token_horizon')
has_disc = hasattr(transform, 'INDEPENDENT_DISCLAIMER')
src = inspect.getsource(m)
print('fetch_independent_by_token_horizon:', has_new)
print('INDEPENDENT_DISCLAIMER:', has_disc)
print('вызов по токену и горизонту:', 'fetch_independent_by_token_horizon(' in src)
print('оговорка первой строкой:', 'INDEPENDENT_DISCLAIMER,' in src)
print('ширина заголовка:', len(getattr(transform, 'INDEPENDENT_HEADER', [])))
print('ширина оговорки:', len(getattr(transform, 'INDEPENDENT_DISCLAIMER', [])))
" 2>&1)
echo "${IMG_PROBE}" | sed 's/^/    /'

if echo "${IMG_PROBE}" | grep -q "вызов по токену и горизонту: True"; then
  note_ok "образ содержит код Этапа 8.1 (разбор по токену и горизонту)"
  IMAGE_IS_81=1
else
  note_block "в образе НЕТ кода Этапа 8.1 — выгрузка на сервере работает по старому"
  note_block "правилу. Разбор по токенам никогда не выполнялся в продакшне."
  IMAGE_IS_81=0
fi

echo
echo "── B. Горизонты и состав инструментов, действующие в контейнере ───────────"
docker compose --profile tools run --rm --no-deps -T export python -c "
from src.core.config import settings
print('EVAL_HORIZONS (сырое):', settings.EVAL_HORIZONS)
print('горизонты в часах:', settings.eval_horizons_hours)
print('главный горизонт:', settings.eval_primary_horizon_h)
print('LOGIC_VERSION:', settings.LOGIC_VERSION)
print('пар инструментов:', len(settings.symbol_pairs))
for p in settings.symbol_pairs:
    print('   ', p.token, '| спот', p.spot, '| контракт', p.swap)
" 2>&1 | sed 's/^/    /'

echo
echo "── C. Свидетельство в журнале выгрузки ────────────────────────────────────"
# ПРАВИЛО (Этап 8.5 §3): проверка, ищущая текст в журнале, обязана искать его в
# том виде, в каком журнал его хранит. В проде stdout не TTY, structlog пишет
# JSONRenderer, а он экранирует кириллицу: «Служебные листы пересобраны» лежит
# в файле как "\u0421\u043b\u0443...". Поиск русского текста подстрокой не
# находил НИЧЕГО и объявлял блокирующую находку на полностью исправной системе.
# Теперь строка сначала разбирается как JSON, а признак версии берётся
# машиночитаемым ключом horizons_h, который в файле лежит как есть.
if [[ -r "${LOG}" ]]; then
  SUMMARY=$(python3 - "${LOG}" <<'PYEOF'
import json
import sys

has_horizons = 0
rebuilds = []
errors = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    if "horizons_h" in rec:
        has_horizons = 1
        rebuilds.append(rec)
    if rec.get("level") == "error":
        errors.append(rec)

print("HAS_HORIZONS=%d" % has_horizons)
print("ERRORS=%d" % len(errors))
print("REBUILDS=%d" % len(rebuilds))
for rec in rebuilds[-3:]:
    keep = {k: rec.get(k) for k in
            ("timestamp", "event", "windows", "horizons_h", "correlation_pairs")}
    print("REBUILD=" + json.dumps(keep, ensure_ascii=False))
for rec in errors[-3:]:
    keep = {k: rec.get(k) for k in ("timestamp", "event", "error")}
    print("ERROR=" + json.dumps(keep, ensure_ascii=False))
PYEOF
)
  echo "    последние пересборки служебных листов (строки с ключом horizons_h):"
  echo "${SUMMARY}" | sed -n 's/^REBUILD=/      /p'
  if [[ "$(echo "${SUMMARY}" | sed -n 's/^HAS_HORIZONS=//p')" == "1" ]]; then
    note_ok "ключ horizons_h в журнале есть — писавший код не старше Этапа 8.1"
  else
    note_block "ключа horizons_h в журнале нет: строку писал код СТАРШЕ Этапа 8.1"
  fi
  ERR_N=$(echo "${SUMMARY}" | sed -n 's/^ERRORS=//p')
  echo "    записей уровня error за всё время журнала: ${ERR_N:-0}"
  echo "${SUMMARY}" | sed -n 's/^ERROR=/      /p'
  if [[ "${ERR_N:-0}" -gt 0 ]]; then
    note_warn "в журнале есть записи уровня error — разобрать их выше"
  else
    note_ok "записей уровня error в журнале нет"
  fi
else
  note_warn "журнал ${LOG} недоступен для чтения — свидетельство не получено"
fi

echo "── D. Истина по базе: независимые окна в разрезе токен × горизонт × версия ─"
echo "    (окно равно длине горизонта, выравнивание по началу суток UTC)"
psql_q "SELECT i.base AS token, e.horizon_h, s.logic_version, count(*) AS independent_windows, min(s.ts) AS ts_min, max(s.ts) AS ts_max FROM (SELECT DISTINCT ON (e2.horizon_h, s2.instrument_id, s2.logic_version, to_timestamp(floor(extract(epoch FROM s2.ts)/(e2.horizon_h*3600))*(e2.horizon_h*3600))) s2.id, s2.instrument_id, s2.logic_version, s2.ts, e2.horizon_h FROM signals s2 JOIN signal_evaluations e2 ON e2.signal_id = s2.id WHERE s2.decision <> 'wait' ORDER BY e2.horizon_h, s2.instrument_id, s2.logic_version, to_timestamp(floor(extract(epoch FROM s2.ts)/(e2.horizon_h*3600))*(e2.horizon_h*3600)), s2.ts ASC, s2.id ASC) q JOIN signals s ON s.id = q.id JOIN instruments i ON i.id = q.instrument_id JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon_h = q.horizon_h GROUP BY i.base, e.horizon_h, s.logic_version ORDER BY e.horizon_h, s.logic_version, i.base;" | sed 's/^/    /'

echo
echo "── E. Сколько строк вернёт запрос выгрузки СЕЙЧАС (по всем горизонтам) ────"
psql_q "SELECT e.horizon_h, i.base AS token, s.logic_version, count(*) AS rows_in_sheet FROM (SELECT DISTINCT ON (e2.horizon_h, i2.id, to_timestamp(floor(extract(epoch FROM s2.ts)/(e2.horizon_h*3600))*(e2.horizon_h*3600))) s2.id, e2.horizon_h FROM signals s2 JOIN instruments i2 ON i2.id = s2.instrument_id JOIN signal_evaluations e2 ON e2.signal_id = s2.id WHERE s2.decision <> 'wait' ORDER BY e2.horizon_h, i2.id, to_timestamp(floor(extract(epoch FROM s2.ts)/(e2.horizon_h*3600))*(e2.horizon_h*3600)), s2.ts ASC) q JOIN signals s ON s.id = q.id JOIN instruments i ON i.id = s.instrument_id JOIN signal_evaluations e ON e.signal_id = s.id AND e.horizon_h = q.horizon_h GROUP BY e.horizon_h, i.base, s.logic_version ORDER BY e.horizon_h, s.logic_version, i.base;" | sed 's/^/    /'

echo
echo "── F. Строки с НЕИЗВЕСТНОЙ версией логики (logic_version = 0) ─────────────"
ZERO=$(psql_q "SELECT count(*) FROM signals s JOIN signal_evaluations e ON e.signal_id = s.id WHERE s.logic_version = 0 AND s.decision <> 'wait';" | tail -1)
echo "    сигналов с версией 0, имеющих оценку: ${ZERO}"
if [[ "${ZERO}" =~ ^[0-9]+$ ]] && [[ "${ZERO}" -gt 0 ]]; then
  note_block "версия 0 попадает в выборку листа — §4 ТЗ 8.4 это запрещает"
else
  note_ok "строк с версией 0 в выборке нет"
fi

echo
echo "── G. Почему наблюдений мало: покрытие оценками после границы версии 5 ────"
echo "    сигналы против оценок по каждому токену (после ${BOUNDARY}):"
psql_q "SELECT i.base AS token, count(DISTINCT s.id) FILTER (WHERE s.decision <> 'wait') AS signals_directional, count(DISTINCT e.signal_id) AS signals_evaluated, count(e.*) AS evaluation_rows FROM signals s JOIN instruments i ON i.id = s.instrument_id LEFT JOIN signal_evaluations e ON e.signal_id = s.id WHERE s.ts >= TIMESTAMPTZ '${BOUNDARY}' GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'
echo "    глубина 1-минутных свечей (оценщик без них не считает исход):"
psql_q "SELECT i.base AS token, i.type, min(o.ts) AS ts_min, max(o.ts) AS ts_max, count(*) AS candles FROM ohlcv o JOIN instruments i ON i.id = o.instrument_id WHERE o.timeframe = '1m' GROUP BY i.base, i.type ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── H. §6: движение цены против распределения решений по токенам ───────────"
psql_q "SELECT i.base AS token, count(*) FILTER (WHERE s.decision='buy') AS buy, count(*) FILTER (WHERE s.decision='sell') AS sell, count(*) FILTER (WHERE s.decision='wait') AS wait, round((100.0*(max(p.last_close)-min(p.first_close))/nullif(min(p.first_close),0))::numeric,2) AS price_move_pct FROM signals s JOIN instruments i ON i.id = s.instrument_id JOIN LATERAL (SELECT (SELECT close FROM ohlcv o1 WHERE o1.instrument_id=i.id AND o1.timeframe='1h' AND o1.ts >= TIMESTAMPTZ '${BOUNDARY}' ORDER BY o1.ts ASC LIMIT 1) AS first_close, (SELECT close FROM ohlcv o2 WHERE o2.instrument_id=i.id AND o2.timeframe='1h' ORDER BY o2.ts DESC LIMIT 1) AS last_close) p ON TRUE WHERE s.ts >= TIMESTAMPTZ '${BOUNDARY}' GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'
echo "    выводы агентов по токенам (insufficient_data виден отдельно):"
psql_q "SELECT i.base AS token, a.agent, a.signal, count(*) AS n FROM agent_outputs a JOIN instruments i ON i.id = a.instrument_id WHERE a.ts >= TIMESTAMPTZ '${BOUNDARY}' GROUP BY i.base, a.agent, a.signal ORDER BY i.base, a.agent, a.signal;" | sed 's/^/    /'

echo
echo "=============================================================================="
echo " ИТОГ"
echo "=============================================================================="
echo " Блокирующих находок: ${blocking}"
echo " Находок, требующих внимания без отката: ${attention}"
echo
if [[ "${IMAGE_IS_81}" -eq 0 ]]; then
  echo " ДЕЙСТВИЕ: пересобрать образ и перезапустить выгрузку — на сервере работает"
  echo " код старше Этапа 8.1; правку кода начинать НЕЛЬЗЯ, она уйдёт вслепую."
elif [[ "${blocking}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: код 8.1 на сервере есть, блокирующие находки выше — передать их"
  echo " исполнителю Этапа 8.4 вместе с разделами D и E."
else
  echo " ДЕЙСТВИЕ: код 8.1 на сервере есть, блокирующих находок нет — сравнить"
  echo " разделы D и E; расхождение между ними и есть предмет Этапа 8.4."
fi
