#!/usr/bin/env bash
# Проверка Этапа 8.8 — вторая оценка исхода по границам.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров,
# ни правки конфигурации. Разовые контейнеры для РАСЧЁТА не поднимаются: для
# снятия слепка решений поднимается уже работающий контейнер decision через
# `exec`, а не `run`.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_8_8.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — развёртывание откатывается;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ (нет файла, нет таблицы, нет
#                        параметра). Это НЕ «всё хорошо»: «ничего не найдено»
#                        само по себе находкой не является и зелёным не бывает.
#
# ГЛАВНОЕ, ЧТО ПРОВЕРЯЕТ ЭТОТ СКРИПТ. Этап обещал не менять НИ ОДНОГО решения
# системы. Разделы 1–3 проверяют именно это обещание, и только потом скрипт
# смотрит, работает ли сам расчёт. Порядок не косметический: расчёт, изменивший
# решения, не нужен, каким бы верным он ни был.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
LOG_FILE="${APP_DIR}/logs/barrier.log"
CRON_FILES=(/etc/cron.d/agent-trade /etc/cron.d/agent-trade-barrier)
EXPECTED_DIGEST="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
LOGIC_VERSION_EXPECTED=5

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
unknown=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ:     $*"; blocking=$((blocking + 1)); }
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
env_value() {  # $1 = файл, $2 = ключ
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.8 — исход по границам (вторая оценка)"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог стека: ${APP_DIR}"
echo "=============================================================================="

# ---------------------------------------------------------------------------
echo
echo "── 1. Решение системы не изменилось (§1) ─────────────────────────────────"
# Слепок снимается ВНУТРИ КОНТЕЙНЕРА: решения принимает код в образе, а не код
# в репозитории, и разойтись они могут ровно тогда, когда это важнее всего —
# когда образ не пересобран. Тот же дефект исправлен и в verify_8_7.sh.
parity="${APP_DIR}/scripts/decision_parity_8_7.py"
if [[ ! -f "${parity}" ]]; then
  note_unk "нет файла ${parity} — слепок решений не снять"
else
  # Скрипт лежит в scripts/, а в образ копируются только src/ и backtest/ —
  # поэтому он подаётся в контейнер ЧЕРЕЗ stdin, а не запускается по пути.
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
    info "ОТКАТИТЬ развёртывание Этапа 8.8 и передать эту строку исполнителю"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. Замороженные пороги и версия логики (§1) ───────────────────────────"
lv="$(env_value "${ENV_FILE}" LOGIC_VERSION)"
if [[ -z "${lv}" ]]; then
  note_unk "LOGIC_VERSION в .env не задан — действует умолчание кода, сверить не с чем"
elif [[ "${lv}" == "${LOGIC_VERSION_EXPECTED}" ]]; then
  note_ok "LOGIC_VERSION=${lv} — версия не поднята"
else
  note_block "LOGIC_VERSION=${lv} (ожидалось ${LOGIC_VERSION_EXPECTED}) — версия поднята"
fi
lv_db="$(psql_val "SELECT DISTINCT logic_version FROM signals WHERE ts > now() - interval '2 hours';")"
if [[ -z "${lv_db}" ]]; then
  note_unk "за последние 2 часа сигналов нет — версию в базе сверить не с чем"
elif [[ "${lv_db}" == "${LOGIC_VERSION_EXPECTED}" ]]; then
  note_ok "свежие сигналы пишутся версией ${lv_db}"
else
  note_block "свежие сигналы пишутся версией ${lv_db} — смешение версий"
fi

# ---------------------------------------------------------------------------
echo
echo "── 3. Существующие таблицы не дополнены колонками (§1) ───────────────────"
# Обещание «signals, signal_evaluations, signal_targets, risk_targets не
# изменяются» проверяется ЧИСЛОМ КОЛОНОК, а не чтением диффа: диффа на сервере
# нет, а таблицы есть.
declare -A EXPECTED_COLS=(
  [signals]=19 [signal_evaluations]=9 [signal_targets]=12 [risk_targets]=18
)
for t in signals signal_evaluations signal_targets risk_targets; do
  n="$(psql_val "SELECT count(*) FROM information_schema.columns WHERE table_name='${t}';")"
  if [[ -z "${n}" || "${n}" == "0" ]]; then
    note_unk "таблицы ${t} нет — сверить состав колонок нечем"
  elif [[ "${n}" == "${EXPECTED_COLS[$t]}" ]]; then
    note_ok "${t}: колонок ${n} — как до этапа"
  else
    note_warn "${t}: колонок ${n} (ожидалось ${EXPECTED_COLS[$t]}) — сверьте, чем именно дополнена"
  fi
done

# ---------------------------------------------------------------------------
echo
echo "── 4. Таблица исходов создана и ограничена (§6) ──────────────────────────"
exists="$(psql_val "SELECT to_regclass('signal_outcomes_barrier') IS NOT NULL;")"
if [[ "${exists}" != "t" ]]; then
  note_block "таблицы signal_outcomes_barrier нет — миграция не применена"
  info "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < db/migrations/015_barrier_outcomes.sql"
else
  note_ok "таблица signal_outcomes_barrier существует"
  pk="$(psql_val "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='signal_outcomes_barrier'::regclass AND contype='p';")"
  echo "    первичный ключ: ${pk:-не найден}"
  if [[ "${pk}" == *"signal_id"* && "${pk}" == *"horizon_h"* ]]; then
    note_ok "PRIMARY KEY (signal_id, horizon_h) — задвоить строку нельзя"
  else
    note_block "первичный ключ не тот — строки могут задвоиться"
  fi
  checks="$(psql_val "SELECT count(*) FROM pg_constraint WHERE conrelid='signal_outcomes_barrier'::regclass AND contype='c';")"
  echo "    ограничений CHECK: ${checks:-0}"
  if [[ "${checks:-0}" -ge 5 ]]; then
    note_ok "перечень исходов и согласованность полей закрыты ограничениями"
  else
    note_warn "ограничений CHECK меньше ожидаемого (5): проверьте миграцию 015"
  fi
  bad="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier WHERE outcome NOT IN ('target','stop','timeout','ambiguous','no_data');")"
  if [[ "${bad:-0}" == "0" ]]; then
    note_ok "исходов вне перечня §3 в таблице нет"
  else
    note_block "строк с исходом вне перечня §3: ${bad}"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. Таблица защищена от политики хранения (§6) ─────────────────────────"
if grep -q '"signal_outcomes_barrier"' "${APP_DIR}/scripts/retention.py" 2>/dev/null; then
  note_ok "signal_outcomes_barrier в PROTECTED_TABLES — не удаляется никогда"
else
  note_block "signal_outcomes_barrier НЕ защищена — политика хранения может её снести"
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. Расчёт отработал: объём и состав исходов (§3, §7) ──────────────────"
if [[ "${exists}" != "t" ]]; then
  note_unk "таблицы нет — состав исходов смотреть негде"
else
  rows="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier;")"
  echo "    строк всего: ${rows:-0}"
  if [[ "${rows:-0}" == "0" ]]; then
    note_warn "исходов не посчитано ни одного — расчёт ещё не запускался"
    info "docker compose --profile tools run --rm --no-deps barrier"
  else
    note_ok "исходы посчитаны: ${rows} строк"
    echo "    горизонт|исход|разрешение|строк"
    psql_tbl "SELECT horizon_h, outcome, resolution, count(*) FROM signal_outcomes_barrier GROUP BY 1,2,3 ORDER BY 1,2,3;" \
      | sed 's/^/      /'
    # Идемпотентность §7 проверяется НЕ повторным запуском (скрипт только
    # читает), а следствием: одна строка на пару и один момент расчёта у пары.
    dup="$(psql_val "SELECT count(*) FROM (SELECT signal_id, horizon_h FROM signal_outcomes_barrier GROUP BY 1,2 HAVING count(*) > 1) d;")"
    if [[ "${dup:-0}" == "0" ]]; then
      note_ok "задвоенных пар (сигнал, горизонт) нет"
    else
      note_block "задвоенных пар: ${dup}"
    fi
    # Горизонт обязан быть в прошлом (§7): исход у ненаступившего горизонта
    # означал бы, что система «знала» будущее.
    future="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier b JOIN signals s ON s.id=b.signal_id WHERE s.ts + make_interval(hours => b.horizon_h) > now();")"
    if [[ "${future:-0}" == "0" ]]; then
      note_ok "исходов у ненаступивших горизонтов нет"
    else
      note_block "исходов у ненаступивших горизонтов: ${future} — расчёт заглянул в будущее"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Доля ambiguous и пригодность метрики (§4) ──────────────────────────"
threshold="$(env_value "${ENV_FILE}" BARRIER_AMBIGUOUS_MAX_PCT)"
threshold="${threshold:-15}"
if [[ "${exists}" != "t" || "${rows:-0}" == "0" ]]; then
  note_unk "исходов нет — долю ambiguous считать не от чего"
else
  amb="$(psql_val "SELECT round(100.0 * count(*) FILTER (WHERE outcome='ambiguous') / NULLIF(count(*),0), 2) FROM signal_outcomes_barrier;")"
  echo "    доля ambiguous: ${amb:-·}%  (порог ${threshold}%)"
  m1="$(psql_val "SELECT round(100.0 * count(*) FILTER (WHERE resolution='1m') / NULLIF(count(*),0), 2) FROM signal_outcomes_barrier;")"
  echo "    доля строк, посчитанных по минутам: ${m1:-·}%"
  if [[ -z "${amb}" ]]; then
    note_unk "долю ambiguous получить не удалось"
  elif awk -v a="${amb}" -v t="${threshold}" 'BEGIN{exit !(a > t)}'; then
    # ОТДЕЛЬНАЯ СТРОКА, как требует §4 ТЗ. Это не сбой расчёта и не повод к
    # откату: расчёт верен, а вот метрика при такой доле неразрешимых случаев
    # отвечает на вопрос человека хуже, чем кажется.
    note_warn "МЕТРИКА В ТАКОМ ВИДЕ МАЛОПРИГОДНА: ambiguous ${amb}% > ${threshold}%"
    info "причина почти всегда одна — минутных свечей на окно не хватило;"
    info "проверьте глубину: docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} -f /dev/stdin < scripts/precheck_8_8.sql"
  else
    note_ok "доля ambiguous ${amb}% в пределах порога ${threshold}%"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Замороженные цели не подменены сегодняшними (§7) ───────────────────"
if [[ "${exists}" != "t" || "${rows:-0}" == "0" ]]; then
  note_unk "исходов нет — сверять цели не с чем"
else
  # Каждая строка исхода обязана совпадать по цели и цене с ЗАМОРОЖЕННОЙ
  # строкой signal_targets. Расхождение означает подстановку сегодняшней цели —
  # то есть подделку истории.
  mismatch="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier b JOIN signal_targets t ON t.signal_id=b.signal_id AND t.horizon_h=b.horizon_h WHERE b.target_pct <> t.target_pct OR b.price_at_signal <> t.price_at_signal::numeric OR b.direction <> t.direction;")"
  if [[ "${mismatch:-0}" == "0" ]]; then
    note_ok "цель, цена и направление в исходах совпадают с замороженными"
  else
    note_block "строк с целью, разошедшейся с замороженной: ${mismatch} — история подменена"
  fi
  orphan="$(psql_val "SELECT count(*) FROM signal_outcomes_barrier b WHERE NOT EXISTS (SELECT 1 FROM signal_targets t WHERE t.signal_id=b.signal_id AND t.horizon_h=b.horizon_h AND t.target_pct IS NOT NULL);")"
  if [[ "${orphan:-0}" == "0" ]]; then
    note_ok "исходов без замороженной цели нет"
  else
    note_block "исходов без замороженной цели: ${orphan}"
  fi
  skipped="$(psql_val "SELECT count(*) FROM signals s WHERE s.decision <> 'wait' AND s.logic_version = ${LOGIC_VERSION_EXPECTED} AND NOT EXISTS (SELECT 1 FROM signal_targets t WHERE t.signal_id = s.id AND t.target_pct IS NOT NULL);")"
  echo "    сигналов версии ${LOGIC_VERSION_EXPECTED} без замороженной цели (пропущены, §7): ${skipped:-·}"
fi

# ---------------------------------------------------------------------------
echo
echo "── 9. Снимок настроек в строках (§5, §8) ─────────────────────────────────"
stop_env="$(env_value "${ENV_FILE}" BARRIER_STOP_PCT)"
cost_env="$(env_value "${ENV_FILE}" RISK_COST_ROUNDTRIP_PCT)"
echo "    .env: BARRIER_STOP_PCT=${stop_env:-·}  RISK_COST_ROUNDTRIP_PCT=${cost_env:-·}"
if [[ -z "${cost_env}" ]]; then
  note_warn "RISK_COST_ROUNDTRIP_PCT в .env не задан — действует умолчание кода 0.22"
fi
if [[ "${exists}" == "t" && "${rows:-0}" != "0" ]]; then
  echo "    в таблице: $(psql_tbl "SELECT DISTINCT stop_pct, cost_pct FROM signal_outcomes_barrier;" | tr '\n' ' ')"
fi
# «Зашита ли комиссия в код» проверяется ПО ЧИСЛОВЫМ ЛИТЕРАЛАМ, а не поиском
# подстроки: строка «зашивать 0.22 запрещено» в пояснении — это как раз запрет,
# а не нарушение, и grep по тексту объявлял бы блокирующим само требование ТЗ.
# Разбор идёт стандартным tokenize: сторонних пакетов на хосте нет (правило D-3).
if command -v python3 >/dev/null 2>&1; then
  hard="$(python3 - "${APP_DIR}/src/barrier" <<'PYEOF' 2>/dev/null
import pathlib, sys, tokenize
root = pathlib.Path(sys.argv[1])
hits = []
for path in sorted(root.rglob("*.py")):
    with tokenize.open(path) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type == tokenize.NUMBER and token.string not in ("0", "1", "2",
                    "3", "60", "100", "100.0", "3600", "1000", "0.0", "1.0", "2.0"):
                hits.append(f"{path}:{token.start[0]}: числовой литерал {token.string}")
print("\n".join(hits))
PYEOF
)"
  suspicious="$(printf '%s' "${hard}" | grep -E "0\.2[0-9]|0\.1[0-9]" | head -1)"
  if [[ -n "${suspicious}" ]]; then
    note_block "издержки похоже зашиты числом в коде расчёта: ${suspicious}"
  else
    note_ok "числовых литералов комиссии в коде расчёта нет — она приходит параметром"
  fi
else
  note_unk "на хосте нет python3 — разобрать литералы кода расчёта нечем"
fi

# ---------------------------------------------------------------------------
echo
echo "── 10. Регламент: сервис и расписание (§7) ───────────────────────────────"
if grep -qE "^[[:space:]]*barrier:" "${APP_DIR}/docker-compose.yml" 2>/dev/null; then
  note_ok "сервис barrier объявлен в docker-compose.yml"
  if sed -n '/^  barrier:/,/^  [a-z]/p' "${APP_DIR}/docker-compose.yml" | grep -q 'profiles: \["tools"\]'; then
    note_ok 'сервис в профиле tools — обычным up -d не поднимается'
  else
    note_warn "сервис barrier вне профиля tools — появится лишний постоянный контейнер"
  fi
else
  note_block "сервиса barrier в docker-compose.yml нет"
fi
cron_found=0
for f in "${CRON_FILES[@]}"; do
  [[ -f "$f" ]] || continue
  line="$(grep -E "run --rm --no-deps barrier" "$f" 2>/dev/null | tail -1)"
  [[ -n "${line}" ]] || continue
  cron_found=1
  echo "    ${f}: ${line}"
  if [[ "${line}" =~ ^10[[:space:]]+4[[:space:]] ]]; then
    note_ok "расписание 04:10 UTC — после целей (03:40), до выгрузки (06:20)"
  else
    note_warn "расписание отличается от 04:10 UTC, заданного §7 ТЗ"
  fi
done
[[ "${cron_found}" == "1" ]] || note_block "задача barrier в cron не найдена — расчёт не запускается сам"

# ---------------------------------------------------------------------------
echo
echo "── 11. Журнал последнего запуска ─────────────────────────────────────────"
if [[ ! -f "${LOG_FILE}" ]]; then
  note_unk "нет файла ${LOG_FILE} — о запусках судить не по чему"
else
  # Поиск идёт по МАШИНОЧИТАЕМЫМ ключам: JSONRenderer хранит кириллицу
  # экранированной, и grep по русскому слову даёт ноль на ИСПРАВНОЙ системе.
  # ВНИМАНИЕ НА `|| echo 0`, КОТОРОГО ЗДЕСЬ НЕТ НАМЕРЕННО: `grep -c` при нуле
  # совпадений сам печатает 0 и возвращает 1, и запасное `echo 0` дописало бы
  # ВТОРОЙ ноль — счётчик становился «0\n0» и ломал арифметику ниже.
  done_n="$(grep -c 'barrier_compute_done=1' "${LOG_FILE}" 2>/dev/null)"
  fail_n="$(grep -c 'barrier_compute_failed=1' "${LOG_FILE}" 2>/dev/null)"
  unusable_n="$(grep -c 'barrier_metric_unusable=1' "${LOG_FILE}" 2>/dev/null)"
  done_n="${done_n:-0}"; fail_n="${fail_n:-0}"; unusable_n="${unusable_n:-0}"
  echo "    успешных прогонов: ${done_n}, сбоев: ${fail_n}, предупреждений о метрике: ${unusable_n}"
  if [[ "${done_n}" -gt 0 ]]; then
    note_ok "расчёт отрабатывал; последняя запись: $(grep 'barrier_compute_done=1' "${LOG_FILE}" | tail -1 | cut -c1-120)"
  else
    note_warn "успешных прогонов в журнале нет"
  fi
  [[ "${fail_n}" -gt 0 ]] && note_warn "в журнале есть сбои расчёта: ${fail_n}"
fi

# ---------------------------------------------------------------------------
echo
echo "── 12. Горячий путь не тронут (§1, §7) ───────────────────────────────────"
hot=0
for pkg in agents decision notify evaluator; do
  if grep -rq "src\.barrier\|src/barrier" "${APP_DIR}/src/${pkg}/" 2>/dev/null; then
    note_block "пакет src/${pkg} ссылается на расчёт исходов — это горячий путь"
    hot=1
  fi
done
[[ "${hot}" == "0" ]] && note_ok "agents, decision, notify, evaluator о расчёте исходов не знают"

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
  echo " ДЕЙСТВИЕ: развёртывание Этапа 8.8 ОТКАТИТЬ, пока 🔴 не сняты."
  echo
  echo "   раздел 1  (решения изменились)   → откат обязателен: этап нарушил §1 ТЗ."
  echo "   раздел 4  (таблицы нет)          → применить db/migrations/015_barrier_outcomes.sql"
  echo "   раздел 5  (таблица не защищена)  → вернуть её в PROTECTED_TABLES"
  echo "   раздел 8  (цели подменены)       → строки исходов удалить и посчитать заново"
  echo "   раздел 10 (нет cron)             → cp deploy/agent-trade-barrier.cron /etc/cron.d/"
  echo "   раздел 12 (горячий путь)         → откат обязателен: расчёт попал в решение."
  echo
  echo " Откат этапа целиком (данные действующей оценки не затрагиваются):"
  echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
  echo "       < db/migrations/015_barrier_outcomes_rollback.sql"
  echo "   git -C ${APP_DIR} checkout <предыдущая ревизия>"
  echo "   docker compose build --no-cache --profile \"*\" && docker compose up -d --remove-orphans"
  exit 1
elif [[ "${attention}" -gt 0 || "${unknown}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Вторая оценка исхода работает параллельно действующей."
  echo " Строки 🟡 и ⚪ прочитайте: часть из них — про пригодность метрики (§4),"
  echo " а не про исправность расчёта. Метрика может быть малопригодной, а расчёт верным."
  exit 0
else
  echo " ДЕЙСТВИЕ: не требуется. Обе оценки исхода считаются, решения системы не изменились."
  exit 0
fi
