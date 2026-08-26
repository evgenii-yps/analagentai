#!/usr/bin/env bash
# Проверка Этапа 8.9 — базовые стратегии как линейка сравнения.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_8_9.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — двух видов, и они РАЗНЫЕ (правка §9.2 ТЗ):
#        (откат)      нарушена граница этапа; одной командой не чинится;
#        (устранимо)  развёртывание неполно, но запрещённого не произошло —
#                     печатается ВМЕСТЕ с командой устранения;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# В ИТОГЕ ПЕЧАТАЕТСЯ ТОЛЬКО ФАКТИЧЕСКИ СРАБОТАВШЕЕ (правка §9.1 ТЗ), а не
# справочник всех мыслимых причин: справочник заставлял человека угадывать,
# какая строка относится к делу.
#
# ЧТО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он не судит о качестве системы и не сравнивает её
# с линейкой. Сравнение — дело analysis/sql/09_baseline_compare.sql и
# scripts/baseline_bootstrap.py, и вывод там даётся с доверительным интервалом,
# а не вердиктом проверочного скрипта.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
LOG_FILE="${APP_DIR}/logs/baseline.log"
CRON_FILES=(/etc/cron.d/agent-trade /etc/cron.d/agent-trade-baseline)
EXPECTED_DIGEST="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
LOGIC_VERSION_EXPECTED=5
# Потолок времени прогона из §7 ТЗ: свыше — переписывать чтение окон пакетами.
SECONDS_LIMIT=1200

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
psql_tbl() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -A -F "|" -q -P footer=off -c "$1" 2>/dev/null
}
env_value() {
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.9 — базовые стратегии как линейка"
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
  # Слепок снимается ВНУТРИ КОНТЕЙНЕРА: решения принимает код в образе.
  # Скрипт лежит в scripts/, а в образ копируются только src/ и backtest/ —
  # поэтому он подаётся через stdin, а не запускается по пути.
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
)
for t in signals signal_evaluations signal_targets risk_targets signal_outcomes_barrier; do
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

# ---------------------------------------------------------------------------
echo
echo "── 3. Правило исхода не переписано (§2) ──────────────────────────────────"
# Линейка обязана считаться ТЕМ ЖЕ правилом, что и система. Вторая реализация
# рано или поздно разошлась бы с первой на краевом случае — и сравнение стало
# бы недействительным незаметно.
if [[ -d "${APP_DIR}/src/baseline" ]]; then
  if grep -rqE "^\s*def (resolve|_touches)\(" "${APP_DIR}/src/baseline/" 2>/dev/null; then
    note_block "в src/baseline есть собственное правило исхода — сравнение недействительно"
  else
    note_ok "src/baseline своего правила исхода не содержит"
  fi
  if grep -rq "from src.barrier.outcomes import\|from src.barrier.runner import" \
       "${APP_DIR}/src/baseline/" 2>/dev/null; then
    note_ok "линейка использует правило Этапа 8.8 напрямую"
  else
    note_warn "src/baseline не импортирует правило 8.8 — проверьте, чем он считает"
  fi
else
  note_fix "каталога src/baseline нет — код этапа не развёрнут" \
           "git -C ${APP_DIR} pull --ff-only && docker compose --profile \"*\" build --no-cache"
fi

# ---------------------------------------------------------------------------
echo
echo "── 4. Таблица линейки создана и ограничена (§6) ──────────────────────────"
exists="$(psql_val "SELECT to_regclass('strategy_outcomes') IS NOT NULL;")"
rows=0
if [[ "${exists}" != "t" ]]; then
  note_fix "таблицы strategy_outcomes нет — миграция не применена" \
           "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/016_strategy_outcomes.sql"
else
  note_ok "таблица strategy_outcomes существует"
  pk="$(psql_val "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='strategy_outcomes'::regclass AND contype='p';")"
  echo "    первичный ключ: ${pk:-не найден}"
  if [[ "${pk}" == *"strategy"* && "${pk}" == *"entry_ts"* && "${pk}" == *"horizon_h"* ]]; then
    note_ok "ключ включает стратегию — встречные стратегии не вытесняют друг друга"
  else
    note_block "первичный ключ не тот — стратегии могут вытеснять друг друга"
  fi
  checks="$(psql_val "SELECT count(*) FROM pg_constraint WHERE conrelid='strategy_outcomes'::regclass AND contype='c';")"
  echo "    ограничений CHECK: ${checks:-0}"
  if [[ "${checks:-0}" -ge 9 ]]; then
    note_ok "перечни стратегий и исходов, источник цели и форма строки закрыты ограничениями"
  else
    note_warn "ограничений CHECK меньше ожидаемого (9): проверьте миграцию 016"
  fi
  rows="$(psql_val "SELECT count(*) FROM strategy_outcomes;")"
  rows="${rows:-0}"
fi

if grep -q '"strategy_outcomes"' "${APP_DIR}/scripts/retention.py" 2>/dev/null; then
  note_ok "strategy_outcomes в PROTECTED_TABLES — не удаляется никогда"
else
  note_fix "strategy_outcomes НЕ защищена — политика хранения может её снести" \
           "вернуть 'strategy_outcomes' в PROTECTED_TABLES в ${APP_DIR}/scripts/retention.py"
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. Линейка посчитана: шесть стратегий (§4, §5) ────────────────────────"
if [[ "${exists}" != "t" || "${rows}" == "0" ]]; then
  note_fix "исходов базовых стратегий не посчитано ни одного" \
           "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.baseline_main"
else
  echo "    строк всего: ${rows}"
  echo "    стратегия|строк|с результатом|доля target,%"
  psql_tbl "SELECT strategy, count(*), count(net_pnl_pct),
                   round(100.0*count(*) FILTER (WHERE outcome='target')
                         / NULLIF(count(*) FILTER (WHERE outcome NOT IN ('ambiguous','no_data')),0), 2)
            FROM strategy_outcomes GROUP BY 1 ORDER BY 1;" | sed 's/^/      /'
  present="$(psql_val "SELECT count(DISTINCT strategy) FROM strategy_outcomes;")"
  if [[ "${present:-0}" == "6" ]]; then
    note_ok "все шесть стратегий посчитаны"
  else
    note_fix "посчитано стратегий: ${present:-0} из 6" \
             "cd ${APP_DIR} && docker compose --profile tools run --rm --no-deps barrier python -m src.baseline_main"
  fi
  # ГЛАВНАЯ ПРОВЕРКА §10.5: на одном моменте покупка и продажа обязаны
  # сосуществовать и быть встречными.
  both="$(psql_val "SELECT count(*) FROM strategy_outcomes a JOIN strategy_outcomes b
                    ON b.instrument_id=a.instrument_id AND b.entry_ts=a.entry_ts
                   AND b.horizon_h=a.horizon_h
                   WHERE a.strategy='always_buy' AND b.strategy='always_sell'
                     AND a.direction='buy' AND b.direction='sell';")"
  echo "    моментов, где есть и always_buy(buy), и always_sell(sell): ${both:-0}"
  if [[ "${both:-0}" -gt 0 ]]; then
    note_ok "встречные стратегии сосуществуют на одних и тех же моментах"
  else
    note_warn "встречных пар не найдено — проверьте, обе ли стратегии считались"
  fi
  wrong="$(psql_val "SELECT count(*) FROM strategy_outcomes
                     WHERE (strategy='always_buy' AND direction<>'buy')
                        OR (strategy='always_sell' AND direction<>'sell')
                        OR (strategy='grid_buy' AND direction<>'buy')
                        OR (strategy='grid_sell' AND direction<>'sell');")"
  if [[ "${wrong:-0}" == "0" ]]; then
    note_ok "направления стратегий соответствуют их именам"
  else
    note_block "строк, где направление противоречит имени стратегии: ${wrong}"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. Цели не подменены сегодняшними (§4) ────────────────────────────────"
if [[ "${exists}" != "t" || "${rows}" == "0" ]]; then
  note_unk "линейки нет — происхождение целей проверять не на чем"
else
  echo "    происхождение целей:"
  psql_tbl "SELECT strategy, target_source, count(*) FROM strategy_outcomes
            GROUP BY 1,2 ORDER BY 1,2;" | sed 's/^/      /' | head -20
  # Цель, взятая ПОЗЖЕ момента входа, — подделка истории. Ожидается ровно 0.
  future="$(psql_val "SELECT count(*) FROM strategy_outcomes
                      WHERE target_source <> 'frozen'
                        AND to_date(right(target_source,10),'YYYY-MM-DD') > entry_ts::date;")"
  echo "    строк с целью ПОЗЖЕ момента входа: ${future:-·} (ожидается 0)"
  if [[ "${future:-0}" == "0" ]]; then
    note_ok "историческая цель нигде не взята из будущего"
  else
    note_block "строк с целью из будущего: ${future} — история подделана"
  fi
  # Стратегия system обязана стоять на ЗАМОРОЖЕННОЙ цели: это копия решения.
  bad_sys="$(psql_val "SELECT count(*) FROM strategy_outcomes
                       WHERE strategy='system' AND target_source <> 'frozen';")"
  if [[ "${bad_sys:-0}" == "0" ]]; then
    note_ok "стратегия system везде стоит на замороженной цели"
  else
    note_block "строк system с незамороженной целью: ${bad_sys}"
  fi
  # А сетка, наоборот, замороженной цели иметь не может: у неё нет сигнала.
  bad_grid="$(psql_val "SELECT count(*) FROM strategy_outcomes
                        WHERE strategy IN ('grid_buy','grid_sell')
                          AND (target_source='frozen' OR signal_id IS NOT NULL);")"
  if [[ "${bad_grid:-0}" == "0" ]]; then
    note_ok "сетка не привязана к сигналам — это фон рынка, как и задумано"
  else
    note_block "строк сетки, привязанных к сигналам: ${bad_grid}"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Монета воспроизводима (§4) ─────────────────────────────────────────"
seed_env="$(env_value "${ENV_FILE}" BASELINE_SEED)"
echo "    .env: BASELINE_SEED=${seed_env:-· (действует умолчание кода 20260826)}"
if [[ "${exists}" == "t" && "${rows}" != "0" ]]; then
  seeds="$(psql_val "SELECT count(DISTINCT seed) FROM strategy_outcomes WHERE strategy='coin_flip';")"
  seed_db="$(psql_val "SELECT DISTINCT seed FROM strategy_outcomes WHERE strategy='coin_flip' LIMIT 1;")"
  echo "    зёрен в таблице: ${seeds:-0}, значение: ${seed_db:-·}"
  if [[ "${seeds:-0}" == "1" ]]; then
    note_ok "монета считалась одним зерном — линейка однородна"
  elif [[ "${seeds:-0}" == "0" ]]; then
    note_warn "строк coin_flip нет — монету не с чем сверять"
  else
    note_block "в таблице ${seeds} разных зерна — часть линейки снята другой монетой"
  fi
  if [[ -n "${seed_env}" && -n "${seed_db}" && "${seed_env}" != "${seed_db}" ]]; then
    note_warn "зерно в .env (${seed_env}) не совпадает с зерном в таблице (${seed_db}): следующий пересчёт заменит линейку"
  fi
  balance="$(psql_val "SELECT round(100.0*count(*) FILTER (WHERE direction='buy')/NULLIF(count(*),0),1)
                       FROM strategy_outcomes WHERE strategy='coin_flip';")"
  echo "    доля покупок у монеты: ${balance:-·}% (ожидается около 50)"
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Производительность (§7) ────────────────────────────────────────────"
if [[ ! -f "${LOG_FILE}" ]]; then
  note_unk "нет файла ${LOG_FILE} — о времени прогона судить не по чему"
else
  done_n="$(grep -c 'baseline_compute_done=1' "${LOG_FILE}" 2>/dev/null)"; done_n="${done_n:-0}"
  fail_n="$(grep -c 'baseline_compute_failed=1' "${LOG_FILE}" 2>/dev/null)"; fail_n="${fail_n:-0}"
  # Замер ищется в ОБОИХ форматах журнала. Логгеры уровня модуля создаются до
  # setup_logging() и из-за cache_logger_on_first_use навсегда остаются с
  # читаемым рендером (seconds=0.518), тогда как логгер точки входа пишет JSON
  # ("seconds": 0.518). Оба формата лежат в одном файле, и проверка, знающая
  # только один из них, объявила бы «замера нет» на исправной системе.
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
fi

# ---------------------------------------------------------------------------
echo
echo "── 9. Регламент: расписание 04:25 (§7) ───────────────────────────────────"
cron_found=0
for f in "${CRON_FILES[@]}"; do
  [[ -f "$f" ]] || continue
  line="$(grep -E "src\.baseline_main" "$f" 2>/dev/null | tail -1)"
  [[ -n "${line}" ]] || continue
  cron_found=1
  echo "    ${f}: ${line}"
  if [[ "${line}" =~ ^25[[:space:]]+4[[:space:]] ]]; then
    note_ok "расписание 04:25 UTC — после исходов системы в 04:10"
  else
    note_warn "расписание отличается от 04:25 UTC, заданного §7 ТЗ"
  fi
done
if [[ "${cron_found}" == "0" ]]; then
  note_fix "задача базовых стратегий в cron не найдена — линейка не пересчитывается сама" \
           "sudo cp ${APP_DIR}/deploy/agent-trade-baseline.cron /etc/cron.d/agent-trade-baseline && sudo chmod 644 /etc/cron.d/agent-trade-baseline"
fi
if grep -qE "^[[:space:]]*baseline:" "${APP_DIR}/docker-compose.yml" 2>/dev/null; then
  note_warn "в docker-compose.yml заведён отдельный сервис baseline — §7 ТЗ требует использовать существующий barrier"
else
  note_ok "отдельного сервиса не заведено — используется существующий barrier"
fi

# ---------------------------------------------------------------------------
echo
echo "── 10. Горячий путь не тронут (§2) ───────────────────────────────────────"
hot=0
for pkg in agents decision notify evaluator risk; do
  if grep -rq "src\.baseline\|src/baseline" "${APP_DIR}/src/${pkg}/" 2>/dev/null; then
    note_block "пакет src/${pkg} ссылается на линейку — это горячий путь"
    hot=1
  fi
done
[[ "${hot}" == "0" ]] && note_ok "agents, decision, notify, evaluator, risk о линейке не знают"

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
  # Печатается ТОЛЬКО фактически сработавшее (§9.1 ТЗ).
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
    echo "       < ${APP_DIR}/db/migrations/016_strategy_outcomes_rollback.sql"
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
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Линейка построена и считается параллельно системе."
  echo
  echo " Сравнение системы с линейкой этот скрипт НЕ делает и делать не должен:"
  echo " разница без доверительного интервала — впечатление, а не измерение."
  echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
  echo "       -f /dev/stdin < ${APP_DIR}/analysis/sql/09_baseline_compare.sql"
  echo "   docker compose --profile tools run --rm --no-deps barrier \\"
  echo "       python -m scripts.baseline_bootstrap"
  exit 0
else
  echo " ДЕЙСТВИЕ: не требуется. Линейка построена, решения системы не изменились."
  echo
  echo " Сравнение — отдельными командами, и читать их надо ВМЕСТЕ:"
  echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
  echo "       -f /dev/stdin < ${APP_DIR}/analysis/sql/09_baseline_compare.sql"
  echo "   docker compose --profile tools run --rm --no-deps barrier \\"
  echo "       python -m scripts.baseline_bootstrap"
  exit 0
fi
