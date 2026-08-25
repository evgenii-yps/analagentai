#!/usr/bin/env bash
# Проверка Этапа 8.7 — гигиена конфигурации и подготовка окна наблюдения.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров,
# ни правки конфигурации. Разовые контейнеры не поднимаются вовсе.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_8_7.sh
#
# Скрипт вызывается КАК ФАЙЛ ИЗ РЕПОЗИТОРИЯ и не зависит от попадания в образ:
# SQL уходит в psql через stdin, Python — через python3 на хосте.
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — развёртывание откатывается;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ (нет файла, нет таблицы, нет
#                        параметра). Это НЕ «всё хорошо»: «ничего не найдено»
#                        само по себе находкой не является и зелёным не бывает.
#
# Дисциплина вывода (нарушалась в прежних скриптах, здесь соблюдена):
#   * счётчики печатают ЧИСЛА (psql -t -A), а не «(1 row)»;
#   * поиск в журналах идёт по МАШИНОЧИТАЕМЫМ ключам либо разбором JSON:
#     JSONRenderer хранит кириллицу экранированной (ц…), и grep по русскому
#     слову даёт ноль на ИСПРАВНОЙ системе;
#   * итоговая строка прямо говорит, что делать.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
BT_ENV="${APP_DIR}/backtest/.env.backtest"
CRON_FILES=(/etc/cron.d/agent-trade /etc/cron.d/agent-trade-export /etc/cron.d/agent-trade-risk)

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
unknown=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ:     $*"; blocking=$((blocking + 1)); }
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_unk()   { echo "  ⚪ НЕ ПРОВЕРЕНО:    $*"; unknown=$((unknown + 1)); }
note_ok()    { echo "  🟢 $*"; }
info()       { echo "  ℹ  $*"; }

# -t -A: только значения, без заголовка и без строки «(N rows)».
psql_val() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -q -c "$1" 2>/dev/null | tr -d '[:space:]'
}
# Таблицы печатаются с заголовком (иначе колонки не назвать), но БЕЗ строки
# «(N rows)»: счётчик обязан быть числом в колонке, а не подписью под таблицей.
psql_tbl() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -A -F "|" -q -P footer=off -c "$1" 2>/dev/null
}
# Значение ключа из файла. Умолчание НЕ подставляется: пусто = «не задано».
env_value() {  # $1 = файл, $2 = ключ
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}
# Повторные объявления в файле: «ключ | повторов: N». Пусто = повторов нет.
duplicates_in() {  # $1 = файл
  [[ -f "$1" ]] || return 0
  awk -v f="$1" '
    /[^[:space:]]/ {
      if ($0 ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/) {
        k = $0; sub(/=.*$/, "", k); gsub(/[[:space:]]/, "", k); label = "ключ " k
      } else if ($0 ~ /^[[:space:]]*#/) { label = "комментарий: " $0
      } else                            { label = "строка: " $0 }
      n[label]++
    }
    END { for (l in n) if (n[l] > 1) printf "%s | %s | повторов: %d\n", f, l, n[l] }
  ' "$1"
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.7 — гигиена конфигурации"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог стека: ${APP_DIR}"
echo "=============================================================================="

# ---------------------------------------------------------------------------
echo
echo "── 1. Повторные объявления в .env и файлах cron (§5) ─────────────────────"
checked_any=0
dup_total=0
for f in "${ENV_FILE}" "${CRON_FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    note_unk "файла нет, проверить нечего: $f"
    continue
  fi
  checked_any=1
  dup="$(duplicates_in "$f")"
  n_dup="$(printf '%s' "$dup" | grep -c . || true)"
  echo "    ${f}: повторов ${n_dup}"
  if [[ "${n_dup}" -gt 0 ]]; then
    printf '%s\n' "$dup" | sed 's/^/      /'
    dup_total=$((dup_total + n_dup))
  fi
done
if [[ "${checked_any}" -eq 0 ]]; then
  note_unk "ни одного файла конфигурации не найдено — вывод о повторах невозможен"
elif [[ "${dup_total}" -gt 0 ]]; then
  note_block "повторных объявлений всего: ${dup_total} (список выше)"
  info "устраните повторы: bash ${APP_DIR}/deploy/dedupe_config.sh --apply"
  info "действующим считается ПОСЛЕДНЕЕ объявление ключа — остаётся именно оно"
else
  note_ok "повторных объявлений нет ни в .env, ни в файлах cron"
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. Круговая комиссия реплея BT_FEE_ROUNDTRIP_PCT (§2) ─────────────────"
bt_fee="$(env_value "${BT_ENV}" BT_FEE_ROUNDTRIP_PCT)"
if [[ ! -f "${BT_ENV}" ]]; then
  note_unk "нет файла ${BT_ENV} — значение не прочитано (это не «всё хорошо»)"
  info "создайте его: cp backtest/.env.backtest.example backtest/.env.backtest"
elif [[ -z "${bt_fee}" ]]; then
  note_unk "в ${BT_ENV} нет ключа BT_FEE_ROUNDTRIP_PCT — сравнивать нечего"
else
  echo "    BT_FEE_ROUNDTRIP_PCT: ${bt_fee}  (ожидается 0.20 = 2 × 0.10 % тейкера)"
  if [[ "${bt_fee}" == "0.20" || "${bt_fee}" == "0.2" ]]; then
    note_ok "круговая комиссия равна полному кругу (вход + выход)"
  elif awk -v v="${bt_fee}" 'BEGIN{exit !(v+0 < 0.20)}'; then
    note_warn "BT_FEE_ROUNDTRIP_PCT=${bt_fee} меньше круговой комиссии 0.20 — издержки занижены"
    info "откат не нужен: параметр читает только пакет backtest/, продакшн его не видит"
  else
    note_warn "BT_FEE_ROUNDTRIP_PCT=${bt_fee} отличается от ожидаемых 0.20 — проверьте намеренно ли"
  fi
fi
# Порог покрытия издержек в сигнале берётся из ДРУГОГО ключа. Показываем оба
# рядом: именно их смешение и было предметом остановки §2.3.
risk_cost="$(env_value "${ENV_FILE}" RISK_COST_ROUNDTRIP_PCT)"
if [[ -z "${risk_cost}" ]]; then
  note_unk "RISK_COST_ROUNDTRIP_PCT в .env не задан — порог covers_fees не показать"
else
  echo "    RISK_COST_ROUNDTRIP_PCT: ${risk_cost}  → порог covers_fees = 3 × ${risk_cost} = $(awk -v c="${risk_cost}" 'BEGIN{printf "%.4f", c*3}')"
  note_ok "порог покрытия издержек не зависит от BT_FEE_ROUNDTRIP_PCT (ключи разные)"
fi

# ---------------------------------------------------------------------------
echo
echo "── 3. verify_7_3.sh печатает ДЕЙСТВУЮЩЕЕ окно funding (§3) ───────────────"
lookback="$(env_value "${ENV_FILE}" FUTURES_LOOKBACK_HOURS)"
min_points="$(env_value "${ENV_FILE}" FUTURES_MIN_POINTS)"
v73="${APP_DIR}/deploy/verify_7_3.sh"
if [[ ! -f "${v73}" ]]; then
  note_unk "нет файла ${v73}"
else
  if grep -q "168 hours" "${v73}"; then
    note_block "в verify_7_3.sh осталось зашитое окно 168 часов"
  elif grep -q 'env_value FUTURES_LOOKBACK_HOURS' "${v73}"; then
    note_ok "verify_7_3.sh читает FUTURES_LOOKBACK_HOURS из .env"
  else
    note_block "verify_7_3.sh не читает FUTURES_LOOKBACK_HOURS — печатает не тот параметр"
  fi
  if [[ -z "${lookback}" ]]; then
    note_warn "FUTURES_LOOKBACK_HOURS в .env не задан — скрипт напечатает «параметр не задан»"
    grep -q "параметр не задан" "${v73}" \
      && note_ok "и он умеет это сказать словами, а не подставить умолчание" \
      || note_block "и сказать об этом он не умеет — подставится умолчание"
  else
    echo "    FUTURES_LOOKBACK_HOURS в .env: ${lookback} ч"
    note_ok "скрипт напечатает именно это число"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 4. Версия 5 видна в выдаче 06_silence.sql (§4) ────────────────────────"
silence_sql="${APP_DIR}/analysis/sql/06_silence.sql"
if [[ ! -f "${silence_sql}" ]]; then
  note_unk "нет файла ${silence_sql}"
else
  if grep -qE 'AS v[1-4]\b|v4_n|v4_pct' "${silence_sql}"; then
    note_block "в 06_silence.sql остались колонки v1…v4 — новые версии в них не попадут"
  else
    note_ok "жёстко перечисленных колонок версий в запросе нет"
  fi
  # Версии, реально присутствующие в данных, — числами.
  echo "    версии в signals (версия|решений):"
  psql_tbl "SELECT logic_version, count(*) FROM signals WHERE logic_version <> 0 GROUP BY 1 ORDER BY 1;" | sed 's/^/      /'
  n_v5="$(psql_val "SELECT count(*) FROM signals WHERE logic_version = 5;")"
  echo "    решений версии 5 в базе: ${n_v5:-нет ответа}"
  if [[ ! "${n_v5}" =~ ^[0-9]+$ ]]; then
    note_unk "база не ответила — присутствие версии 5 в выдаче не проверено"
  elif [[ "${n_v5}" -eq 0 ]]; then
    note_unk "решений версии 5 в базе нет — показать её в выдаче нечем (не дефект запроса)"
  else
    # Запрос гоняется ЦЕЛИКОМ и в его собственной выдаче ищется строка версии 5.
    out="$(docker compose exec -T postgres psql -U "${DB_USER}" -d "${DB_NAME}" -X -q -f - < "${silence_sql}" 2>&1)"
    hits="$(printf '%s\n' "${out}" | grep -cE '^[[:space:]]+5[[:space:]]*\|' || true)"
    echo "    строк с версией 5 в выдаче запроса: ${hits}"
    if [[ "${hits}" -gt 0 ]]; then
      note_ok "версия 5 присутствует в выдаче 06_silence.sql"
    else
      note_block "версия 5 в базе есть, но в выдаче запроса её нет — запрос снова молчит"
    fi
    # Число исключённых записей «версия неизвестна» — тоже числом.
    excluded="$(printf '%s\n' "${out}" | grep -A 3 'rows_excluded_ver0' | tail -2)"
    [[ -n "${excluded}" ]] && { echo "    исключено как «версия неизвестна»:"; printf '%s\n' "${excluded}" | sed 's/^/      /'; }
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. Осиротевший контейнер bt_load (§6) ─────────────────────────────────"
# Пустой список сам по себе НЕ доказательство: сначала убеждаемся, что docker
# вообще ответил. Иначе «контейнеров 0» означало бы лишь «спросить не удалось».
orphans="$(docker ps -a --filter "name=bt_load" --format '{{.Names}} {{.Status}}' 2>&1)"
docker_rc=$?
if [[ "${docker_rc}" -ne 0 ]]; then
  note_unk "docker не ответил — существование bt_load не проверено"
  info "ответ docker: $(printf '%s' "${orphans}" | head -1)"
else
  n_orphans="$(printf '%s' "${orphans}" | grep -c . || true)"
  echo "    контейнеров с именем bt_load: ${n_orphans}"
  if [[ "${n_orphans}" -gt 0 ]]; then
    printf '%s\n' "${orphans}" | sed 's/^/      /'
    note_warn "контейнер bt_load ещё существует — он даёт предупреждение при каждой команде"
    info "удалить: docker rm bt_load   (или: docker compose up -d --remove-orphans)"
  else
    note_ok "контейнера bt_load нет"
  fi
fi
ps_out="$(docker compose ps 2>&1)"
ps_rc=$?
if [[ "${ps_rc}" -ne 0 ]]; then
  note_unk "docker compose ps завершился с ошибкой — предупреждение об осиротевших не проверено"
  info "ответ: $(printf '%s' "${ps_out}" | tail -1)"
else
  warn_line="$(printf '%s\n' "${ps_out}" | grep -i "orphan" || true)"
  if [[ -n "${warn_line}" ]]; then
    note_warn "docker compose всё ещё сообщает об осиротевших контейнерах:"
    printf '%s\n' "${warn_line}" | sed 's/^/      /'
  else
    note_ok "предупреждения об осиротевших контейнерах нет (compose ответил успешно)"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. Строки запаса funding в суточной сводке (§7) ───────────────────────"
report_py="${APP_DIR}/src/health/daily_report.py"
if [[ ! -f "${report_py}" ]]; then
  note_unk "нет файла ${report_py}"
elif ! grep -q "section_funding_reserve" "${report_py}"; then
  note_block "в суточной сводке нет секции запаса funding"
else
  note_ok "секция запаса funding есть в коде сводки"
  if [[ -z "${lookback}" || -z "${min_points}" ]]; then
    note_warn "FUTURES_LOOKBACK_HOURS и/или FUTURES_MIN_POINTS не заданы — сводка так и напишет"
  fi
  # Секция вызывается НАПРЯМУЮ: сводка целиком ушла бы в Telegram, а это
  # проверка, а не рассылка. Зависимостей сверх стандартной библиотеки нет.
  echo "    фактический вывод секции:"
  APP_DIR="${APP_DIR}" python3 -c "
import sys
sys.path.insert(0, '${APP_DIR}')
from src.health import daily_report as d
print('\n'.join(d.section_funding_reserve()))
" 2>&1 | sed 's/^/      /'
  # Независимая сверка тем же способом, каким считает агент: одна точка в час.
  if [[ -n "${lookback}" ]]; then
    echo "    контрольный счёт по базе (инструмент|часов с funding):"
    psql_tbl "SELECT i.base, count(DISTINCT date_trunc('hour', f.ts))
                FROM instruments i
                LEFT JOIN funding f ON f.instrument_id = i.id
                 AND f.ts >= now() - make_interval(hours => ${lookback})
               WHERE i.type = 'swap' AND i.active
               GROUP BY i.base ORDER BY i.base;" | sed 's/^/      /'
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Образ пересобран и несёт новый код ─────────────────────────────────"
# Проверка отвечает на вопрос «пересборка была?» ФАКТОМ из образа, а не верой
# в то, что команду выполнили. Маркер — имя, появившееся только в Этапе 8.7.
img_out="$(docker compose exec -T decision grep -c 'section_funding_reserve' /app/src/health/daily_report.py 2>&1)"
if [[ "${img_out}" =~ ^[0-9]+$ ]]; then
  echo "    вхождений маркера 8.7 в коде внутри контейнера decision: ${img_out}"
  if [[ "${img_out}" -gt 0 ]]; then
    note_ok "контейнер работает на коде Этапа 8.7"
  else
    note_block "контейнер работает на СТАРОМ коде — пересоберите образы"
    info "docker compose build --no-cache --profile \"*\" && docker compose up -d --remove-orphans"
  fi
else
  note_unk "контейнер decision недоступен — версию кода в образе не прочитать"
  info "ответ docker: $(printf '%s' "${img_out}" | tail -1)"
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Решение системы не изменилось (§1) ─────────────────────────────────"
parity="${APP_DIR}/scripts/decision_parity_8_7.py"
if [[ ! -f "${parity}" ]]; then
  note_unk "нет файла ${parity} — слепок решений не снять"
else
  digest="$(POSTGRES_PASSWORD=parity python3 "${parity}" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin)['digest_sha256'])" 2>/dev/null)"
  expected="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
  echo "    отпечаток решений: ${digest:-не получен}"
  echo "    ожидается:         ${expected}"
  if [[ -z "${digest}" ]]; then
    note_unk "слепок не снят — вывод о неизменности решений сделать нельзя"
  elif [[ "${digest}" == "${expected}" ]]; then
    note_ok "decision, probability и calibrated_probability на 323 наборах входов не изменились"
  else
    note_block "решения ИЗМЕНИЛИСЬ — это нарушает жёсткую границу этапа"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 9. Замороженные пороги не сдвинулись ──────────────────────────────────"
declare -A FROZEN=(
  [LOGIC_VERSION]=5 [FUTURES_MIN_POINTS]="${min_points}"
  [DECISION_THRESHOLD]=0.3 [MIN_AGENTS]=2 [NOTIFY_MIN_AGENTS]=3
  [NOTIFY_MIN_PROBABILITY]=0.7 [AGENT_FRESHNESS_SEC]=300
)
for key in LOGIC_VERSION DECISION_THRESHOLD MIN_AGENTS NOTIFY_MIN_AGENTS NOTIFY_MIN_PROBABILITY AGENT_FRESHNESS_SEC; do
  actual="$(env_value "${ENV_FILE}" "${key}")"
  if [[ -z "${actual}" ]]; then
    note_unk "${key} в .env не задан — действует умолчание кода, сверить не с чем"
  elif [[ "${actual}" == "${FROZEN[$key]}" ]]; then
    note_ok "${key}=${actual}"
  else
    note_block "${key}=${actual} (ожидалось ${FROZEN[$key]}) — порог сдвинут"
  fi
done

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
  echo " ДЕЙСТВИЕ: окно наблюдения НЕ ОТКРЫВАТЬ, пока 🔴 не сняты."
  echo
  echo " Под каждой 🔴 строкой выше стоит ℹ с точным действием — выполните их."
  echo " Кратко, по разделам:"
  echo "   раздел 1 (повторы в конфигурации) → bash ${APP_DIR}/deploy/dedupe_config.sh --apply"
  echo "   раздел 7 (образ на старом коде)   → docker compose build --no-cache --profile \"*\" \\"
  echo "                                        && docker compose up -d --remove-orphans"
  echo "   разделы 3, 4, 8, 9 (код и пороги) → ОТКАТИТЬ развёртывание Этапа 8.7:"
  echo "                                        git -C ${APP_DIR} checkout <предыдущая ревизия>"
  echo "                                        docker compose build --no-cache --profile \"*\""
  echo "                                        docker compose up -d --remove-orphans"
  echo " Строки разделов 3, 4, 8 и 9 передайте исполнителю: по ним видно, что именно"
  echo " разошлось. Раздел 8 отдельно: расхождение отпечатка означает, что этап"
  echo " изменил решение системы, а это прямо запрещено §1 ТЗ."
  exit 1
elif [[ "${attention}" -gt 0 || "${unknown}" -gt 0 ]]; then
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Окно наблюдения можно открывать."
  echo " Разберите 🟡 и ⚪ до начала окна: ⚪ означает, что проверку выполнить было"
  echo " НЕЧЕМ (нет файла, нет параметра, нет данных), а не что всё в порядке."
  exit 0
else
  echo " ДЕЙСТВИЕ: ничего делать не нужно. Все проверки выполнены и пройдены,"
  echo " окно наблюдения можно открывать."
  exit 0
fi
