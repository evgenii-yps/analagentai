#!/usr/bin/env bash
# Проверка Этапа 8.10 — подвижный выход, замер на истории.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_8_10.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — двух видов, и они РАЗНЫЕ (§9.7 ТЗ):
#        (откат)      нарушена граница этапа; одной командой не чинится;
#        (устранимо)  развёртывание неполно, но запрещённого не произошло —
#                     печатается ВМЕСТЕ с командой устранения;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# В ИТОГЕ ПЕЧАТАЕТСЯ ТОЛЬКО ФАКТИЧЕСКИ СРАБОТАВШЕЕ (§9.7 ТЗ), а не справочник
# всех мыслимых причин: справочник заставлял человека угадывать, какая строка
# относится к делу.
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он НЕ СРАВНИВАЕТ ВАРИАНТЫ ВЫХОДА и не называет
# лучший. Во-первых, разница без доверительного интервала — впечатление, а не
# измерение; во-вторых, §5.4 ТЗ прямо запрещает выбирать вариант для внедрения.
# Сравнение — дело scripts/trailing_stats.py, и оно печатает вердикты §5.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
LOG_FILE="${APP_DIR}/logs/trailing.log"
CRON_FILES=(/etc/cron.d/agent-trade /etc/cron.d/agent-trade-trailing)
EXPECTED_DIGEST="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
LOGIC_VERSION_EXPECTED=5
VARIANTS_EXPECTED=13
# Потолок времени полного прогона из §7 ТЗ: 15 минут.
SECONDS_LIMIT=900

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
unknown=0
declare -a ROLLBACK_ITEMS=()
declare -a FIX_ITEMS=()
note_block() {
  echo "  🔴 БЛОКИРУЮЩЕЕ (откат):      $*"
  blocking=$((blocking + 1)); ROLLBACK_ITEMS+=("$*")
}
note_fix() {   # $1 = текст, $2 = команда устранения
  echo "  🔴 БЛОКИРУЮЩЕЕ (устранимо):  $1"
  echo "     └ команда: $2"
  blocking=$((blocking + 1)); FIX_ITEMS+=("$1"$'\n'"       $2")
}
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_unk()   { echo "  ⚪ НЕ ПРОВЕРЕНО:    $*"; unknown=$((unknown + 1)); }
note_ok()    { echo "  🟢 $*"; }
info()       { echo "  ℹ  $*"; }

psql_val() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -q -c "$1" 2>/dev/null | tr -d '[:space:]'
}
# Строки БЕЗ заголовка и без итоговой строки (-t): заголовок скрипт печатает
# сам, своими словами. Заголовок psql здесь только мешал бы — он состоит из
# служебных имён вроде "?column?", а разбор вывода принимал бы его за данные.
psql_tbl() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -F "|" -q -c "$1" 2>/dev/null \
    | sed '/^$/d'
}
env_value() {
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.10 — подвижный выход, замер на истории"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог стека: ${APP_DIR}"
echo "=============================================================================="

# ---------------------------------------------------------------------------
echo
echo "── 1. Решение системы не изменилось (§2) ─────────────────────────────────"
parity="${APP_DIR}/scripts/decision_parity_8_7.py"
if [[ ! -f "${parity}" ]]; then
  note_unk "нет файла ${parity} — слепок решений не снять"
else
  # Слепок снимается ВНУТРИ КОНТЕЙНЕРА: решения принимает код в образе, и
  # утверждение «решения не изменились» относится именно к нему. Скрипт лежит в
  # scripts/, а в образ копируются только src/ и backtest/ — поэтому он
  # подаётся через stdin, а не запускается по пути.
  digest="$(docker compose exec -T -e POSTGRES_PASSWORD=parity decision \
              python - < "${parity}" 2>/dev/null \
            | docker compose exec -T decision python -c \
              "import json,sys; print(json.load(sys.stdin)['digest_sha256'])" 2>/dev/null)"
  parity_env="в контейнере decision (код образа)"
  if [[ -z "${digest}" ]]; then
    digest="$(POSTGRES_PASSWORD=parity python3 "${parity}" 2>/dev/null \
              | python3 -c "import json,sys; print(json.load(sys.stdin)['digest_sha256'])" 2>/dev/null)"
    parity_env="системным python3 на хосте (КОД РЕПОЗИТОРИЯ, не образа)"
  fi
  echo "    отпечаток решений: ${digest:-не получен}"
  echo "    ожидается:         ${EXPECTED_DIGEST}"
  echo "    снят:              ${parity_env}"
  if [[ -z "${digest}" ]]; then
    note_unk "слепок не снят — вывод о неизменности решений сделать нельзя"
    info "проверьте, что контейнер decision работает: docker compose ps decision"
  elif [[ "${digest}" == "${EXPECTED_DIGEST}" ]]; then
    note_ok "decision, probability и calibrated_probability на 323 наборах входов не изменились"
    if [[ "${parity_env}" != "в контейнере decision (код образа)" ]]; then
      note_warn "слепок снят на хосте: он подтверждает код репозитория, а не образа"
    fi
  else
    note_block "решения ИЗМЕНИЛИСЬ — это нарушает жёсткую границу этапа"
  fi
fi

lv="$(env_value "${ENV_FILE}" LOGIC_VERSION)"
if [[ -z "${lv}" ]]; then
  note_unk "LOGIC_VERSION в .env не задан — действует умолчание кода, сверить не с чем"
elif [[ "${lv}" == "${LOGIC_VERSION_EXPECTED}" ]]; then
  note_ok "LOGIC_VERSION=${lv} — версия не поднята"
else
  note_block "LOGIC_VERSION=${lv} (ожидалось ${LOGIC_VERSION_EXPECTED}) — версия поднята"
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. Существующие таблицы не изменены (§2) ──────────────────────────────"
# Сверяются ИМЕНА обязательных колонок, а не их число: число меняется от любой
# правки схемы и ничего не говорит о том, что именно изменилось. Настоящее
# нарушение обещания «таблицы не изменяются» — исчезнувшая колонка.
declare -A REQUIRED_COLS=(
  [signals]="id instrument_id ts decision logic_version status"
  [signal_evaluations]="signal_id horizon_h price_at_signal pnl_pct success"
  [signal_targets]="signal_id horizon_h direction price_at_signal target_pct"
  [risk_targets]="instrument_id horizon_h direction computed_at target_pct"
  [signal_outcomes_barrier]="signal_id horizon_h direction price_at_signal target_pct outcome net_pnl_pct mae_pct mfe_pct resolution"
  [strategy_outcomes]="strategy instrument_id entry_ts horizon_h outcome net_pnl_pct"
)
for t in signals signal_evaluations signal_targets risk_targets \
         signal_outcomes_barrier strategy_outcomes; do
  actual="$(psql_val "SELECT string_agg(column_name, ' ' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='${t}';")"
  if [[ -z "${actual}" ]]; then
    note_unk "таблицы ${t} нет — сверить состав колонок нечем"
    continue
  fi
  missing=""
  for col in ${REQUIRED_COLS[$t]}; do
    [[ " ${actual} " == *"${col}"* ]] || missing="${missing} ${col}"
  done
  n="$(psql_val "SELECT count(*) FROM information_schema.columns WHERE table_name='${t}';")"
  if [[ -n "${missing}" ]]; then
    note_block "${t}: пропали обязательные колонки —${missing}"
  else
    note_ok "${t}: все обязательные колонки на месте (всего в таблице ${n})"
  fi
done
info "полное сравнение схемы с кодом — отдельной командой, она ничего не чинит:"
info "  sudo -u agent bash ${APP_DIR}/deploy/schema_drift.sh"

# ---------------------------------------------------------------------------
echo
echo "── 3. Правило касания не переписано (§2) ─────────────────────────────────"
# Сравнение вариантов действительно ТОЛЬКО при одинаковых правилах касания.
# Своя реализация в src/trailing рано или поздно разошлась бы с 8.8 на краевом
# случае — и сравнение стало бы недействительным незаметно.
if [[ -d "${APP_DIR}/src/trailing" ]]; then
  if grep -rqE "^\s*def (resolve|_touches|_excursions|net_pnl|levels)\(" \
       "${APP_DIR}/src/trailing/" 2>/dev/null; then
    note_block "в src/trailing есть собственное правило касания — сравнение недействительно"
  else
    note_ok "src/trailing своего правила касания не содержит"
  fi
  if grep -rq "from src.barrier.outcomes import" "${APP_DIR}/src/trailing/" 2>/dev/null; then
    note_ok "подвижный выход использует правило Этапа 8.8 напрямую"
  else
    note_warn "src/trailing не импортирует правило 8.8 — проверьте, чем он считает"
  fi
else
  note_fix "каталога src/trailing нет — код этапа не развёрнут" \
           "git -C ${APP_DIR} pull --ff-only && docker compose --profile \"*\" build --no-cache"
fi

# ---------------------------------------------------------------------------
echo
echo "── 4. Таблица создана и ограничена (§6) ──────────────────────────────────"
exists="$(psql_val "SELECT to_regclass('trailing_outcomes') IS NOT NULL;")"
rows=0
if [[ "${exists}" != "t" ]]; then
  note_fix "таблицы trailing_outcomes нет — миграция 017 не применена" \
           "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/017_trailing_outcomes.sql"
else
  note_ok "таблица trailing_outcomes существует"
  pk="$(psql_val "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='trailing_outcomes'::regclass AND contype='p';")"
  echo "    первичный ключ: ${pk:-не найден}"
  if [[ "${pk}" == *"activation_ratio"* && "${pk}" == *"retrace_ratio"* ]]; then
    note_ok "ключ включает параметры варианта — варианты не вытесняют друг друга"
  else
    note_block "первичный ключ не тот — тринадцать вариантов вытесняют друг друга"
  fi
  checks="$(psql_val "SELECT count(*) FROM pg_constraint WHERE conrelid='trailing_outcomes'::regclass AND contype='c';")"
  echo "    ограничений CHECK: ${checks:-0}"
  if [[ "${checks:-0}" -ge 7 ]]; then
    note_ok "перечень причин выхода, сетка вариантов и форма строки закрыты ограничениями"
  else
    note_warn "ограничений CHECK меньше ожидаемого (7): проверьте миграцию 017"
  fi
  rows="$(psql_val "SELECT count(*) FROM trailing_outcomes;")"
  rows="${rows:-0}"
fi

if grep -q '"trailing_outcomes"' "${APP_DIR}/scripts/retention.py" 2>/dev/null; then
  note_ok "trailing_outcomes в PROTECTED_TABLES — не удаляется никогда"
else
  note_fix "trailing_outcomes НЕ защищена — политика хранения может её снести" \
           "вернуть 'trailing_outcomes' в PROTECTED_TABLES в ${APP_DIR}/scripts/retention.py"
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. КОНТРОЛЬНЫЙ ВАРИАНТ СОВПАЛ С ЭТАПОМ 8.8 (§4) ───────────────────────"
# САМАЯ ВАЖНАЯ ПРОВЕРКА ЭТАПА. Контрольный вариант — та же фиксированная цель,
# что в 8.8, посчитанная тем же кодом. Не совпал — значит, правила касания
# разошлись, и недействительно ВСЁ сравнение вариантов, а не только эта строка.
if [[ "${exists}" != "t" || "${rows}" == "0" ]]; then
  note_fix "исходов подвижного выхода не посчитано ни одного" \
           "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.trailing_main"
else
  compare_sql="SELECT count(*) FILTER (WHERE t.signal_id IS NOT NULL),
                      count(*) FILTER (WHERE t.signal_id IS NULL),
                      count(*) FILTER (WHERE t.signal_id IS NOT NULL AND (
                             t.exit_reason     IS DISTINCT FROM b.outcome
                          OR t.hit_at          IS DISTINCT FROM b.hit_at
                          OR t.bars_to_hit     IS DISTINCT FROM b.bars_to_hit
                          OR t.net_pnl_pct     IS DISTINCT FROM b.net_pnl_pct
                          OR t.mae_pct         IS DISTINCT FROM b.mae_pct
                          OR t.mfe_pct         IS DISTINCT FROM b.mfe_pct
                          OR t.resolution      IS DISTINCT FROM b.resolution
                          OR t.direction       IS DISTINCT FROM b.direction
                          OR t.price_at_signal IS DISTINCT FROM b.price_at_signal
                          OR t.target_pct      IS DISTINCT FROM b.target_pct
                          OR t.stop_pct        IS DISTINCT FROM b.stop_pct
                          OR t.cost_pct        IS DISTINCT FROM b.cost_pct))
               FROM signal_outcomes_barrier b
               LEFT JOIN trailing_outcomes t
                 ON t.signal_id = b.signal_id AND t.horizon_h = b.horizon_h
                AND t.activation_ratio = 0 AND t.retrace_ratio = 0;"
  line="$(psql_tbl "${compare_sql}")"
  compared="$(echo "${line}" | cut -d'|' -f1 | tr -d '[:space:]')"
  missing_rows="$(echo "${line}" | cut -d'|' -f2 | tr -d '[:space:]')"
  mismatched="$(echo "${line}" | cut -d'|' -f3 | tr -d '[:space:]')"
  echo "    строк 8.8 сверено:            ${compared:-·}"
  echo "    без контрольной пары:         ${missing_rows:-·} (ожидается 0)"
  echo "    разошлось хоть одним полем:   ${mismatched:-·} (ожидается 0)"
  if [[ -z "${compared}" ]]; then
    note_unk "сверку выполнить не удалось — база не ответила"
  elif [[ "${compared}" == "0" ]]; then
    note_unk "сверять нечего: строк 8.8 нет. Это НЕ «совпало»"
  elif [[ "${mismatched}" == "0" && "${missing_rows}" == "0" ]]; then
    note_ok "контрольный вариант совпал с signal_outcomes_barrier до последнего знака"
  elif [[ "${missing_rows}" != "0" && "${mismatched}" == "0" ]]; then
    note_fix "контрольная строка посчитана не для всех пар 8.8 (${missing_rows}) — расчёт неполон" \
             "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.trailing_main"
  else
    note_block "контрольный вариант РАЗОШЁЛСЯ с 8.8 (${mismatched}) — сравнение вариантов недействительно"
    # ДИАГНОСТИКА, А НЕ СМЯГЧЕНИЕ. Допуск сравнения не расширяется ничем: пары
    # ниже остаются расхождением и остаются блокирующими. Печатаются они затем,
    # что «разошлось 2» без указания, ЧТО именно разошлось, заставляет искать
    # причину вручную — а Этап 8.10.1 показал, что причина видна из полей.
    echo "    разошедшиеся пары (сигнал, горизонт, исход, итоги 8.10 против 8.8):"
    psql_tbl "SELECT b.signal_id, b.horizon_h, b.outcome,
                     t.net_pnl_pct, b.net_pnl_pct,
                     (t.net_pnl_pct - b.net_pnl_pct) AS delta
              FROM signal_outcomes_barrier b
              JOIN trailing_outcomes t
                ON t.signal_id = b.signal_id AND t.horizon_h = b.horizon_h
               AND t.activation_ratio = 0 AND t.retrace_ratio = 0
              WHERE t.exit_reason IS DISTINCT FROM b.outcome
                 OR t.hit_at IS DISTINCT FROM b.hit_at
                 OR t.bars_to_hit IS DISTINCT FROM b.bars_to_hit
                 OR t.net_pnl_pct IS DISTINCT FROM b.net_pnl_pct
                 OR t.mae_pct IS DISTINCT FROM b.mae_pct
                 OR t.mfe_pct IS DISTINCT FROM b.mfe_pct
                 OR t.resolution IS DISTINCT FROM b.resolution
              ORDER BY b.signal_id LIMIT 20;" | sed 's/^/      /'
    # Признак ИЗВЕСТНОЙ причины (Этап 8.10.1): исход timeout, различие только в
    # итоге. Тогда 8.8 посчитал пару по ещё формировавшемуся последнему бару.
    known="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier b
                       JOIN trailing_outcomes t
                         ON t.signal_id = b.signal_id AND t.horizon_h = b.horizon_h
                        AND t.activation_ratio = 0 AND t.retrace_ratio = 0
                       WHERE b.outcome = 'timeout'
                         AND t.net_pnl_pct IS DISTINCT FROM b.net_pnl_pct
                         AND t.exit_reason = b.outcome
                         AND t.mae_pct = b.mae_pct AND t.mfe_pct = b.mfe_pct
                         AND t.resolution = b.resolution;")"
    if [[ "${known:-0}" == "${mismatched}" ]]; then
      info "все расхождения имеют признак известной причины (Этап 8.10.1):"
      info "  исход timeout, различие ТОЛЬКО в итоге — строка 8.8 посчитана по"
      info "  ещё формировавшемуся последнему бару окна. Правило годности с"
      info "  запасом не даёт этому повториться, но УЖЕ ЗАПИСАННЫЕ строки"
      info "  чинятся только пересчётом, а это правка signal_outcomes_barrier —"
      info "  решение владельца, не скрипта."
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. Правило посчитано так, как описано (§4) ────────────────────────────"
if [[ "${exists}" != "t" || "${rows}" == "0" ]]; then
  note_unk "строк нет — правило проверять не на чем"
else
  # Три следствия правила §4. Каждое проверяется числом, а не обещанием.
  bad_trail="$(psql_val "SELECT count(*) FROM trailing_outcomes
                         WHERE exit_reason='trail' AND activation_ratio=0;")"
  bad_target="$(psql_val "SELECT count(*) FROM trailing_outcomes
                          WHERE exit_reason='target' AND activation_ratio>0;")"
  # Итог подвижного выхода обязан быть равен (1 − R) × вершина − издержки.
  # Допуск 0.000001 — это последний знак колонки NUMERIC(12,6), а не «примерно».
  bad_math="$(psql_val "SELECT count(*) FROM trailing_outcomes
                        WHERE exit_reason='trail'
                          AND abs(net_pnl_pct - ((1 - retrace_ratio) * peak_pct - cost_pct))
                              > 0.000001;")"
  echo "    строк trail у контрольного варианта:        ${bad_trail:-·} (ожидается 0)"
  echo "    строк target у подвижных вариантов:         ${bad_target:-·} (ожидается 0)"
  echo "    строк trail с неверной арифметикой итога:   ${bad_math:-·} (ожидается 0)"
  if [[ "${bad_trail:-0}" == "0" && "${bad_target:-0}" == "0" ]]; then
    note_ok "причины выхода соответствуют варианту: цель не закрывает подвижный, откат — фиксированный"
  else
    note_block "причина выхода противоречит варианту — правило реализовано не так, как описано"
  fi
  if [[ "${bad_math:-0}" == "0" ]]; then
    note_ok "итог подвижного выхода сходится с вершиной и величиной отката"
  else
    note_block "итог подвижного выхода не сходится с вершиной: ${bad_math} строк"
  fi

  echo "    вариант|строк|средний итог,%|доля trail,%"
  psql_tbl "SELECT activation_ratio || '/' || retrace_ratio, count(*),
                   round(avg(net_pnl_pct), 4),
                   round(100.0*count(*) FILTER (WHERE exit_reason='trail')
                         / NULLIF(count(*), 0), 1)
            FROM trailing_outcomes GROUP BY 1 ORDER BY 1;" | sed 's/^/      /'
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Полнота: тринадцать вариантов на пару (§4) ─────────────────────────"
if [[ "${exists}" != "t" || "${rows}" == "0" ]]; then
  note_unk "строк нет — полноту проверять не на чем"
else
  variants="$(psql_val "SELECT count(*) FROM (SELECT DISTINCT activation_ratio, retrace_ratio FROM trailing_outcomes) v;")"
  partial="$(psql_val "SELECT count(*) FROM (SELECT signal_id, horizon_h FROM trailing_outcomes
                       GROUP BY 1,2 HAVING count(*) <> ${VARIANTS_EXPECTED}) p;")"
  echo "    различных вариантов в таблице: ${variants:-·} (ожидается ${VARIANTS_EXPECTED})"
  echo "    пар с неполным набором:        ${partial:-·} (ожидается 0)"
  if [[ "${variants:-0}" == "${VARIANTS_EXPECTED}" && "${partial:-0}" == "0" ]]; then
    note_ok "у каждой пары посчитаны все тринадцать вариантов"
  elif [[ "${partial:-0}" != "0" ]]; then
    note_fix "пары с неполным набором вариантов: ${partial} — расчёт прерывался" \
             "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.trailing_main"
  else
    note_fix "вариантов в таблице ${variants:-0} из ${VARIANTS_EXPECTED}" \
             "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.trailing_main"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Время прогона (§7) ─────────────────────────────────────────────────"
if [[ ! -f "${LOG_FILE}" ]]; then
  note_unk "нет файла ${LOG_FILE} — о времени прогона судить не по чему"
else
  done_n="$(grep -c 'trailing_compute_done=1' "${LOG_FILE}" 2>/dev/null)"; done_n="${done_n:-0}"
  fail_n="$(grep -c 'trailing_compute_failed=1' "${LOG_FILE}" 2>/dev/null)"; fail_n="${fail_n:-0}"
  mism_n="$(grep -c 'trailing_control_mismatch=1' "${LOG_FILE}" 2>/dev/null)"; mism_n="${mism_n:-0}"
  # Замер ищется в ОБОИХ форматах журнала: логгеры уровня модуля создаются до
  # setup_logging() и навсегда остаются с читаемым рендером (seconds=0.518),
  # логгер точки входа пишет JSON ("seconds": 0.518). Проверка, знающая только
  # один формат, объявила бы «замера нет» на исправной системе.
  last_sec="$(grep -oE '"?seconds"?[=:] ?[0-9.]+' "${LOG_FILE}" 2>/dev/null \
              | tail -1 | grep -oE '[0-9.]+$')"
  echo "    успешных прогонов: ${done_n}, сбоев: ${fail_n}"
  echo "    время последнего прогона: ${last_sec:-·} с (потолок §7 ТЗ: ${SECONDS_LIMIT} с)"
  if [[ "${done_n}" == "0" ]]; then
    note_warn "успешных прогонов в журнале нет"
  elif [[ -z "${last_sec}" ]]; then
    note_unk "в журнале нет замера времени"
  elif awk -v s="${last_sec}" -v l="${SECONDS_LIMIT}" 'BEGIN{exit !(s > l)}'; then
    note_warn "прогон дольше ${SECONDS_LIMIT} с — §7 ТЗ требует переписать чтение окон пакетами и доложить"
  else
    note_ok "прогон укладывается в отведённое окно"
  fi
  [[ "${fail_n}" -gt 0 ]] && note_warn "в журнале есть сбои расчёта: ${fail_n}"
  if [[ "${mism_n}" -gt 0 ]]; then
    note_block "в журнале есть расхождение контрольного варианта (${mism_n} раз)"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 9. Регламент: расписание 04:40 (§7) ───────────────────────────────────"
cron_found=0
for f in "${CRON_FILES[@]}"; do
  [[ -f "$f" ]] || continue
  line="$(grep -E "src\.trailing_main" "$f" 2>/dev/null | tail -1)"
  [[ -n "${line}" ]] || continue
  cron_found=1
  echo "    ${f}: ${line}"
  if [[ "${line}" =~ ^40[[:space:]]+4[[:space:]] ]]; then
    note_ok "расписание 04:40 UTC — после исходов системы (04:10) и линейки (04:25)"
  else
    note_warn "расписание отличается от 04:40 UTC, заданного §7 ТЗ"
  fi
done
if [[ "${cron_found}" == "0" ]]; then
  note_fix "задача подвижного выхода в cron не найдена — таблица не пересчитывается сама" \
           "sudo cp ${APP_DIR}/deploy/agent-trade-trailing.cron /etc/cron.d/agent-trade-trailing && sudo chmod 644 /etc/cron.d/agent-trade-trailing"
fi
if grep -qE "^[[:space:]]*trailing:" "${APP_DIR}/docker-compose.yml" 2>/dev/null; then
  note_warn "в docker-compose.yml заведён отдельный сервис trailing — §7 ТЗ требует использовать существующий barrier"
else
  note_ok "отдельного сервиса не заведено — используется существующий barrier"
fi

# ---------------------------------------------------------------------------
echo
echo "── 10. Горячий путь не тронут (§2) ───────────────────────────────────────"
hot=0
for pkg in agents decision notify evaluator risk; do
  if grep -rq "src\.trailing\|src/trailing" "${APP_DIR}/src/${pkg}/" 2>/dev/null; then
    note_block "пакет src/${pkg} ссылается на подвижный выход — это горячий путь"
    hot=1
  fi
done
[[ "${hot}" == "0" ]] && note_ok "agents, decision, notify, evaluator, risk о подвижном выходе не знают"

# ---------------------------------------------------------------------------
echo
echo "── 11. Запас на закрытие последнего бара (Этап 8.10.1 §1) ────────────────"
# Пара становится годной к расчёту не в момент срока, а после того, как её
# последний бар закрылся. Без этого запаса исход timeout считается по цене
# «пока что» — так и разошлись две строки на сервере 28.08.2026.
settle_env="$(env_value "${ENV_FILE}" BARRIER_SETTLE_MINUTES)"
echo "    .env: BARRIER_SETTLE_MINUTES=${settle_env:-· (действует умолчание кода 5)}"
if grep -rq "settle_seconds" "${APP_DIR}/src/barrier/" 2>/dev/null; then
  note_ok "правило годности учитывает закрытие последнего бара"
else
  note_fix "запас не развёрнут: расчёт допускает пары с незакрытым последним баром" \
           "git -C ${APP_DIR} pull --ff-only && docker compose --profile \"*\" build --no-cache"
fi
if [[ -f "${APP_DIR}/logs/barrier.log" ]]; then
  last_settle="$(grep -oE '\"?settle_seconds\"?[=:] ?[0-9]+' "${APP_DIR}/logs/barrier.log" \
                 2>/dev/null | tail -1 | grep -oE '[0-9]+$')"
  echo "    в журнале последнего прогона 8.8: settle_seconds=${last_settle:-·}"
  if [[ -z "${last_settle}" ]]; then
    note_warn "прогон 8.8 с запасом ещё не выполнялся — число появится после следующего"
  elif [[ "${last_settle}" -lt 3600 ]]; then
    note_warn "запас меньше часового бара (${last_settle} с): пары на часовом ряду не защищены"
  else
    note_ok "запас перекрывает часовой бар (${last_settle} с)"
  fi
else
  note_unk "нет ${APP_DIR}/logs/barrier.log — судить о запасе по журналу нечем"
fi

# ---------------------------------------------------------------------------
echo
echo "── 12. Подпись клиента при обращении к бирже (Этап 8.10.1) ───────────────"
# OKX с 28.08.2026 отбивает питоновские подписи кодом 403/1010 ещё ДО проверки
# ключа — даже на публичном эндпоинте. Требование: подпись браузерная и берётся
# из одного места, чтобы новый код не наступал на это снова.
if [[ ! -f "${APP_DIR}/src/core/http.py" ]]; then
  note_fix "нет src/core/http.py — единого места для подписи клиента" \
           "git -C ${APP_DIR} pull --ff-only && docker compose --profile \"*\" build --no-cache"
else
  ua="$(grep -oE 'Mozilla/[0-9.]+' "${APP_DIR}/src/core/http.py" | head -1)"
  echo "    подпись в едином месте: ${ua:-не найдена}"
  if [[ -n "${ua}" ]]; then
    note_ok "подпись браузерная (${ua}...)"
  else
    note_block "подпись в src/core/http.py не браузерная — OKX ответит 403/1010"
  fi
  missing_ua=""
  for f in src/core/exchange.py backtest/loader.py scripts/geo_check.py; do
    if [[ ! -f "${APP_DIR}/${f}" ]]; then continue; fi
    grep -q "exchange_headers\|EXCHANGE_USER_AGENT" "${APP_DIR}/${f}" 2>/dev/null \
      || missing_ua="${missing_ua} ${f}"
  done
  if [[ -z "${missing_ua}" ]]; then
    note_ok "ccxt-клиент, загрузчик истории и гео-тест берут заголовки из единого места"
  else
    note_block "берут подпись мимо единого места:${missing_ua}"
  fi
fi
# ЖИВАЯ ПРОВЕРКА. Публичный эндпоинт времени — самый дешёвый и не требует ключа.
probe_url="https://www.okx.com/api/v5/public/time"
if docker compose ps --status running --services 2>/dev/null | grep -qx collector; then
  code_py="$(docker compose exec -T collector python -c "
import asyncio, sys
sys.path.insert(0, '/app')
from src.core.exchange import create_exchange
async def main():
    ex = create_exchange('okx')
    try:
        r = await ex.fetch('${probe_url}')
        print(200 if r else 0)
    except Exception as exc:
        print(getattr(exc, 'http_status', 0) or 0)
    finally:
        await ex.close()
asyncio.run(main())
" 2>/dev/null | tr -d '[:space:]')"
  echo "    ответ биржи клиенту проекта: ${code_py:-нет ответа}"
  if [[ "${code_py}" == "200" ]]; then
    note_ok "биржа отвечает 200 клиенту с браузерной подписью"
  elif [[ -z "${code_py}" || "${code_py}" == "0" ]]; then
    note_unk "живой запрос не выполнен (нет сети наружу?) — вывод о подписи только по коду"
  else
    note_warn "биржа ответила ${code_py} — проверьте подпись и регион вручную:"
    info "  curl -s -o /dev/null -w '%{http_code}' ${probe_url}"
  fi
else
  note_unk "контейнер collector не запущен — живой запрос к бирже не выполнен"
fi

# ---------------------------------------------------------------------------
echo
echo "=============================================================================="
echo " ИТОГ"
echo "=============================================================================="
echo " 🔴 Блокирующих находок:                 ${blocking}"
echo " 🟡 Требующих внимания (откат не нужен): ${attention}"
echo " ⚪ Не проверено (проверять было нечем): ${unknown}"
echo
if [[ "${blocking}" -gt 0 ]]; then
  # Печатается ТОЛЬКО фактически сработавшее (§9.7 ТЗ).
  if [[ "${#FIX_ITEMS[@]}" -gt 0 ]]; then
    echo " УСТРАНИМО КОМАНДОЙ — откат НЕ нужен (${#FIX_ITEMS[@]}):"
    echo
    for item in "${FIX_ITEMS[@]}"; do
      echo "   • ${item}"
      echo
    done
  fi
  if [[ "${#ROLLBACK_ITEMS[@]}" -gt 0 ]]; then
    echo " ОТКАТ ОБЯЗАТЕЛЕН — нарушена граница этапа (${#ROLLBACK_ITEMS[@]}):"
    echo
    for item in "${ROLLBACK_ITEMS[@]}"; do
      echo "   • ${item}"
    done
    echo
    echo " Этап обещал только измерять и ничего не менять. Перечисленное выше"
    echo " означает, что обещание нарушено, и одной командой это не чинится."
    echo
    echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
    echo "       < ${APP_DIR}/db/migrations/017_trailing_outcomes_rollback.sql"
    echo "   git -C ${APP_DIR} checkout <предыдущая ревизия>"
    echo "   docker compose --profile \"*\" build --no-cache && docker compose up -d --remove-orphans"
    echo
    echo " Строки выше передайте исполнителю."
    exit 1
  fi
  echo " ДЕЙСТВИЕ: выполните команды выше и запустите проверку снова."
  echo " Откат НЕ нужен: границу этапа ничто из найденного не нарушает."
  exit 1
elif [[ "${attention}" -gt 0 || "${unknown}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Таблица вариантов посчитана параллельно системе."
  echo
  echo " Сравнение вариантов этот скрипт НЕ делает и делать не должен: разница"
  echo " без доверительного интервала — впечатление, а не измерение, а выбор"
  echo " варианта для внедрения запрещён §5.4 ТЗ."
  echo "   docker compose --profile tools run --rm --no-deps \\"
  echo "       -v ./scripts:/app/scripts:ro barrier python -m scripts.trailing_stats"
  exit 0
else
  echo " ДЕЙСТВИЕ: не требуется. Варианты посчитаны, решения системы не изменились."
  echo
  echo " Таблица и три защиты §5 — отдельной командой:"
  echo "   docker compose --profile tools run --rm --no-deps \\"
  echo "       -v ./scripts:/app/scripts:ro barrier python -m scripts.trailing_stats"
  exit 0
fi
