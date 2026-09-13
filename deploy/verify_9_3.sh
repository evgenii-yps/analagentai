#!/usr/bin/env bash
# ПРОВЕРКА ЭТАПА 9.3 — торговый слой: снятие слотов, пауза по токену,
# уведомления по сделкам. Критерии приёмки §13, требования §14 ТЗ.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_3.sh
#
# СНАЧАЛА СЛЕПОК, ПОТОМ ПРОВЕРКА. Критерии §13.1.2, §13.1.3 и §13.1.6
# сравнивают «после» с «до», и «до» снимается ОТДЕЛЬНЫМ скриптом ДО любых
# изменений: deploy/snapshot_9_3.sh. Без его файла эти три пункта здесь
# честно печатаются как НЕ ПРОВЕРЕННЫЕ — и это НЕ «всё хорошо».
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 ОТКАТ            — НАРУШЕНА ГРАНИЦА ЭТАПА. Только критерии §13.1.1,
#                         §13.1.2, §13.1.3, §13.1.6 и расхождение отпечатка;
#   🟠 ОСТАНОВИТЬСЯ     — развёрнуто не то, что заказано, или что-то работает
#                         не так. Разобрать, ОТКАТ НЕ ПРЕДПИСЫВАЕТСЯ;
#   ⚪ НЕ ПРОВЕРЕНО     — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# ПОЧЕМУ ПРЕДПИСАНИЯ РАЗДЕЛЕНЫ ТАК (§14.4). Верный сигнал обязан
# сопровождаться верным предписанием. Откат за «сделок пока ноль» стоил бы
# этапа целиком; молчание о нарушенной границе стоило бы выборки.
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он не делает выводов о прибыльности и не
# говорит, «удался ли» замер. Он отвечает на один вопрос: развёрнуто ли ровно
# то, что описано в ТЗ. Числа он печатает; что они значат, решает не скрипт.
#
# РОСТ ПРОДАКШН-ТАБЛИЦ ВО ВРЕМЯ ПРОГОНА — НОРМА, А НЕ ТРЕВОГА (§14.1). Граница
# этапа проверяется ПО СУЩЕСТВУ — версией логики в новых строках и
# неизменностью старых, — а не по счётчикам строк.
#
# ИМЕНА КОНТЕЙНЕРОВ — ИМЕНА СЛУЖБ docker compose (§14.2): positions, notify,
# postgres, redis. Это ровно те имена, которые создаёт порядок развёртывания
# §15; несовпадение имени уже один раз сделало пункт проверки неработающим
# навсегда.
#
# ФЛАГИ ОБОЛОЧКИ (§14.5). При ``set -uo pipefail`` неуспешная команда внутри
# подстановки обнуляет её целиком, и пункт проверки молча превращается в
# «пусто». Поэтому КАЖДЫЙ конвейер, собирающий журналы, заканчивается
# ``|| true``, а пустой результат печатается как НЕ ПРОВЕРЕНО явно.
#
# ПОЧЕМУ ПОИСК В ЖУРНАЛАХ ИДЁТ ПО МАШИНОЧИТАЕМЫМ КЛЮЧАМ. Русский текст в
# журналах хранится экранированными последовательностями Unicode, и grep по
# русским словам не находит ничего.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
OUT_DIR="${APP_DIR}/analysis_out"
BEFORE="${OUT_DIR}/snapshot_9_3_before.txt"
# КЛЮЧИ ПРОГОНА ДУБЛИРУЮТСЯ В ФАЙЛ (§14.3). Журнал контейнера, запущенного
# через ``run --rm``, исчезает вместе с ним, а числа этого прогона нужны
# отчёту и через неделю.
# Имя названо §14.3 буквально, и записано здесь тоже буквально: путь,
# собранный из переменных, невозможно сверить с требованием глазами.
METRICS="${APP_DIR}/analysis_out/verify_9_3_metrics.txt"

# РАЗБОР СТРОКИ ЗАПУСКА ВЫНЕСЕН В БИБЛИОТЕКУ и подключается отсюда. Причина —
# дефект, найденный на боевом сервере 13.09.2026: проверка §2 дала девять
# ложных тревог при полностью верной конфигурации, потому что искала величины
# по образцу JSON, а служба печатает их в двух форматах и двумя строками.
# Подробности — в самом файле; вынесен он затем, чтобы его можно было прогнать
# тестом на настоящих строках обоих форматов.
LIB_DIR="$(dirname "$(readlink -f "$0")")"
LIB="${LIB_DIR}/startup_line.sh"
if [ -r "${LIB}" ]; then
  # shellcheck source=deploy/startup_line.sh
  . "${LIB}"
  HAVE_LIB=1
else
  HAVE_LIB=0
fi
# ЧТЕНИЕ СЛЕПКА — тоже отдельной библиотекой и по той же причине: ему
# предстоит читать файл, который больше никогда не будет снят заново, и
# прогнать это тестом обязательно.
SNAP_LIB="${LIB_DIR}/snapshot_read.sh"
if [ -r "${SNAP_LIB}" ]; then
  # shellcheck source=deploy/snapshot_read.sh
  . "${SNAP_LIB}"
fi

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }
mkdir -p "${OUT_DIR}" 2>/dev/null || true
# ФАЙЛ МЕТРИК НАКОПИТЕЛЬНЫЙ, А НЕ ПЕРЕЗАПИСЫВАЕМЫЙ. Первая редакция затирала
# его на каждом прогоне, и сравнить два прогона между собой было нечем — а
# именно сравнение двух прогонов и показало дефект §5 (сумма signal_targets
# сменилась за две минуты). Прогоны разделены заголовком.
printf '\n=== прогон %s ===\n' "$(date -u +%FT%TZ)" >> "${METRICS}" 2>/dev/null \
  || METRICS="/dev/null"

rollback=0; stop=0; unknown=0
note_rollback() { echo "  🔴 ОТКАТ:         $*"; rollback=$((rollback + 1)); }
note_stop()     { echo "  🟠 ОСТАНОВИТЬСЯ:  $*"; stop=$((stop + 1)); }
note_skip()     { echo "  ⚪ НЕ ПРОВЕРЕНО:  $*"; unknown=$((unknown + 1)); }
note_ok()       { echo "  🟢 $*"; }
metric()        { echo "$1=$2" >> "${METRICS}"; echo "  ⓘ $1=$2"; }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}
redis_get() {
  docker compose exec -T redis redis-cli GET "$1" 2>/dev/null | tr -d '\r' || true
}
env_val() {
  grep -E "^${1}=" "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}
before_val() {
  # Читается библиотекой snapshot_read.sh; запасной путь — на случай, когда её
  # рядом не оказалось: проверка обязана работать хуже, а не никак.
  if command -v snapshot_val >/dev/null 2>&1; then
    snapshot_val "${BEFORE}" "$1"
  else
    grep -E "^${1}=" "${BEFORE}" 2>/dev/null | head -1 | cut -d= -f2- || true
  fi
}

echo "=== Проверка этапа 9.3 (торговый слой, версия логики 7) ==="
metric "verify_started_at" "$(date -u +%FT%TZ)"
echo

# --- 1. Настройки: то ли развёрнуто, что заказано (§3 ТЗ) -------------------
echo "1. Настройки в ${ENV_FILE}"
if [ ! -f "${ENV_FILE}" ]; then
  note_skip "файла .env нет — настройки проверить нечем"
else
  check_env() { # имя ожидаемое класс(rollback|stop)
    local got; got="$(env_val "$1")"
    if [ -z "${got}" ]; then
      note_stop "$1 в .env не задан — действует умолчание кода, а §3 требует явного значения"
    elif [ "${got}" = "$2" ]; then
      note_ok "$1=${got}"
    elif [ "${3:-stop}" = "rollback" ]; then
      note_rollback "$1=${got}, а заказано $2 — нарушена граница этапа"
    else
      note_stop "$1=${got}, а заказано $2"
    fi
  }
  # КРИТЕРИЙ §13.1.1 — ЕДИНСТВЕННАЯ НАСТРОЙКА, ЗА КОТОРУЮ ПРЕДПИСЫВАЕТСЯ
  # ОТКАТ: версия логики — жёсткая граница выборки, и неверная означает, что
  # сделки пишутся не в ту выборку.
  check_env LOGIC_VERSION 7 rollback
  check_env POSITION_MAX_OPEN 0
  check_env POSITION_BUDGET_USD 0
  check_env POSITION_TOKEN_PAUSE_MIN 60
  check_env POSITION_ONE_PER_TOKEN false
  check_env NOTIFY_SIGNALS_ENABLED false
  check_env NOTIFY_DAILY_SUMMARY_UTC 06:00
  check_env NOTIFY_TRADES_MAX_PER_HOUR 0
  check_env POSITION_NOTIFY_ENABLED true
  # ЗАМОРОЖЕННЫЙ СПИСОК §3: эти величины обязаны остаться прежними, иначе
  # версии 6 и 7 перестанут отличаться ровно тем, чем их задумали отличать.
  check_env POSITION_SLOT_USD 2.0
  check_env POSITION_MIN_PROBABILITY 0.8
  check_env POSITION_MAX_HOLD_HOURS 48
  check_env POSITION_PLUS_WAIT_START_HOURS 24
fi
echo

# --- 2. Строки запуска обеих служб (§8 ТЗ) ----------------------------------
#
# ПРОВЕРЯЮТСЯ ВСЕ СТРОКИ-КАНДИДАТЫ, А НЕ ПОСЛЕДНЯЯ, и в любом из двух форматов
# вывода. Так исправлен дефект, найденный на боевом сервере 13.09.2026: служба
# позиций печатает свои величины ДВАЖДЫ (из positions_main и из runner.run), а
# формат рендера зависит от того, терминал ли stdout. Первая редакция брала
# ``tail -1`` и искала образец JSON — и дала девять ложных тревог при верных
# настройках. Достаточно, чтобы величины нашлись ХОТЯ БЫ В ОДНОЙ строке: обе
# строки собраны одной функцией ``startup_fields()`` и противоречить друг другу
# не могут.
echo "2. Строки запуска служб"
if [ "${HAVE_LIB}" != "1" ]; then
  note_skip "нет ${LIB} — разбирать строки запуска нечем"
else
  check_startup() { # служба якорь величина…
    local service="$1" anchor="$2"; shift 2
    local logs result
    logs="$(docker compose logs --since 24h --no-color "${service}" 2>/dev/null \
            | grep -F "${anchor}" || true)"
    if [ -z "${logs}" ]; then
      note_skip "в журнале ${service} за сутки нет строки запуска — перезапускалась ли служба?"
      return
    fi
    if result="$(find_startup_line "${logs}" "$@")"; then
      note_ok "${service} называет все требуемые §8 величины"
    else
      note_stop "${service}: ни одна строка запуска не несёт величин: ${result}"
    fi
  }
  # Величины заданы в КАНОНИЧЕСКОМ виде — нижний регистр, ключ=значение.
  # Числа записаны ровно так, как их печатает Python: 0.0 и 2.0, а не 0 и 2.
  check_startup positions cooldown_sec \
    logic_version=7 max_open=0 slot_usd=2.0 budget_usd=0.0 \
    min_probability=0.8 max_hold_hours=48 plus_wait_start_hours=24 \
    cooldown_sec=3600 one_per_token=false
  check_startup notify notify_daily_summary_utc \
    notify_signals_enabled=false notify_trades_enabled=true \
    notify_daily_summary_utc=06:00 notify_trades_max_per_hour=0
fi

# ВКЛЮЧЁННЫЙ ОГРАНИЧИТЕЛЬ ЧИСЛА СДЕЛОК СЛУЖБА НАЗЫВАЕТ ВСЛУХ. Если он есть в
# журнале — замер идёт не в тех условиях, на которые написан §12.
limit_on="$(docker compose logs --since 24h --no-color positions 2>/dev/null \
            | grep -cF 'positions_limit_enabled=1' || true)"
if [ "${limit_on:-0}" != "0" ]; then
  note_stop "служба сообщила о ВКЛЮЧЁННОМ ограничителе числа сделок (${limit_on} раз): предсказания §12 проверять нечем"
fi
echo

# --- 3. Граница этапа: строки версий 5 и 6 не тронуты (§13.1.2) -------------
echo "3. Граница этапа: история версий 5 и 6"
if [ ! -f "${BEFORE}" ]; then
  note_skip "нет ${BEFORE} — слепок ДО развёртывания не снимался, критерий §13.1.2 закрыть нечем"
else
  for version in 5 6; do
    was="$(before_val "v${version}_digest")"
    now_digest="$(psql_q "
      SELECT md5(string_agg(line, E'\n' ORDER BY line))
      FROM (
        SELECT p.id || '|' || i.symbol || '|' || p.opened_at || '|' ||
               COALESCE(p.closed_at::text, '') || '|' ||
               COALESCE(p.net_pnl_pct::text, '') AS line
        FROM positions p JOIN instruments i ON i.id = p.instrument_id
        WHERE p.logic_version = ${version}
      ) rows;")"
    metric "v${version}_digest_now" "${now_digest:-пусто}"
    if [ -z "${was}" ] || [ "${was}" = "НЕ_СНЯТО" ]; then
      note_skip "версия ${version}: слепок ДО не содержит контрольной суммы"
    elif [ -z "${now_digest}" ]; then
      note_skip "версия ${version}: база не ответила — сверить нечем"
    elif [ "${was}" = "${now_digest}" ]; then
      note_ok "версия ${version}: строки не изменены ни одной"
    else
      note_rollback "версия ${version}: контрольная сумма строк ИЗМЕНИЛАСЬ — прошлое пересчитано"
    fi
  done
  # ОТДЕЛЬНО — ИНВАРИАНТ ПРЕЖНИХ ВЕРСИЙ. Их правила задним числом не
  # переписываются: по токену у них по-прежнему не больше одной открытой.
  v6_multi="$(psql_q "
    SELECT count(*) FROM (
      SELECT instrument_id FROM positions
      WHERE status = 'open' AND logic_version < 7
      GROUP BY instrument_id HAVING count(*) > 1
    ) t;")"
  if [ "${v6_multi}" = "0" ]; then
    note_ok "у версий до 7 по-прежнему не больше одной открытой позиции на токен"
  elif [ -z "${v6_multi}" ]; then
    note_skip "база не ответила про инвариант прежних версий"
  else
    note_rollback "у версий до 7 нашлось ${v6_multi} токенов с несколькими открытыми позициями"
  fi
fi
echo

# --- 4. Граница этапа: отпечаток агентов и решающего слоя (§13.1.3) ---------
echo "4. Граница этапа: агенты и решающий слой"
FILES="src/agents/base.py src/agents/market.py src/agents/liquidity.py
src/agents/futures.py src/agents/runner.py src/decision/agent.py
src/decision/runner.py"
now_fp="$(cat ${FILES} 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
metric "agents_fingerprint_now" "${now_fp:-пусто}"
was_fp="$(before_val agents_fingerprint)"
if [ -z "${now_fp}" ]; then
  note_skip "отпечаток снять не удалось — файлы агентов на месте?"
elif [ -z "${was_fp}" ] || [ "${was_fp}" = "НЕ_СНЯТО" ]; then
  note_skip "слепок ДО не содержит отпечатка — критерий §13.1.3 закрыть нечем"
elif [ "${was_fp}" = "${now_fp}" ]; then
  note_ok "отпечаток агентов и решающего слоя не изменился"
else
  note_rollback "отпечаток агентов и решающего слоя ИЗМЕНИЛСЯ — граница §1.2 нарушена"
fi
echo

# --- 5. Граница этапа: цели пересчитываются по-прежнему (§13.1.6) -----------
#
# СВЕРЯЮТСЯ ТОЛЬКО СТРОКИ, СУЩЕСТВОВАВШИЕ НА МОМЕНТ СЛЕПКА. Дефект первой
# редакции, найденный на боевом сервере 13.09.2026: сумма считалась по ВСЕЙ
# таблице, а ``signal_targets`` растёт с каждым новым сигналом — два прогона с
# разницей в две минуты давали разные суммы (2b6a7d7c… → 38511cab…) при
# полностью неизменных старых строках. §14.1 прямо велит не принимать рост
# продакшн-таблиц за тревогу.
#
# ЧТО ЭТА ПРОВЕРКА УТВЕРЖДАЕТ: ни одна строка целей, существовавшая до
# развёртывания, не переписана. Ровно это и требует §13.1.6. Появление НОВЫХ
# строк нарушением не считается и считаться не может: ночной пересчёт
# 03:40 UTC для того и работает.
#
# ДВА ВИДА СЛЕПКА, И ВТОРОЙ НЕ ВЫБРАСЫВАЕТСЯ.
#
#   ТОЧНЫЙ  — снят редакцией, которая записала ``targets_boundary`` с часов
#             БАЗЫ в тот же миг, что и суммы. Сверка однозначна.
#   ПРЕЖНИЙ — снят редакцией до 13.09.2026: ключа границы в нём нет и
#             появиться не может — развёртывание состоялось, «до»
#             невоспроизводимо. Суммы в нём посчитаны по ВСЕЙ таблице.
#
# ДЛЯ ПРЕЖНЕГО СЛЕПКА ГРАНИЦА ВОССТАНАВЛИВАЕТСЯ ИЗ ``snapshot_taken_at`` — и
# восстановление это ПРОВЕРЯЕТСЯ, а не предполагается. Изъян у него ровно
# один и он известен: ``snapshot_taken_at`` снят часами ХОСТА в начале
# скрипта, а суммы посчитаны базой несколькими запросами позже. Строка,
# вставленная в этот промежуток, попала в сумму, но окажется за границей — и
# сверка показала бы расхождение там, где ничего не менялось.
#
# ПОЭТОМУ СКРИПТ ИЗМЕРЯЕТ ОПАСНОСТЬ, А НЕ РАССУЖДАЕТ О НЕЙ: считает строки,
# легшие ВБЛИЗИ восстановленной границы (час в обе стороны — с запасом на
# задержку скрипта и расхождение часов хоста и контейнера). Ноль таких строк
# означает, что никакая задержка множество не меняет, и сверка честна. Больше
# нуля — сверять нечем, и это «не проверено», а НЕ находка.
#
# Практически это само собой разделяет две таблицы, и разделяет по существу,
# а не по списку имён: ``risk_targets`` пополняется раз в сутки ночным
# пересчётом, ``signal_targets`` — с каждым сигналом.
echo "5. Граница этапа: слепок целей"
picked="$(pick_targets_boundary "${BEFORE}")"
BOUNDARY="${picked% *}"
BOUNDARY_KIND="${picked##* }"
if [ -z "${picked}" ]; then
  BOUNDARY=""
fi
if [ -z "${BOUNDARY}" ]; then
  note_skip "в слепке ДО нет ни targets_boundary, ни snapshot_taken_at — критерий §13.1.6 закрыть нечем"
else
  metric "targets_boundary" "${BOUNDARY}"
  metric "targets_boundary_kind" "${BOUNDARY_KIND}"
  [ "${BOUNDARY_KIND}" = "восстановленная" ] && echo \
    "  ⓘ слепок снят прежней редакцией: границы в нём нет, она восстановлена из snapshot_taken_at"
  for pair in "risk_targets:computed_at" "signal_targets:frozen_at"; do
    table="${pair%%:*}"; column="${pair##*:}"
    exists="$(psql_q "SELECT to_regclass('${table}') IS NOT NULL;")"
    if [ "${exists}" != "t" ]; then
      note_skip "${table}: таблицы нет"
      continue
    fi
    was="$(before_val "${table}_digest")"
    if [ -z "${was}" ] || [ "${was}" = "НЕ_СНЯТО" ] || [ "${was}" = "НЕТ_ТАБЛИЦЫ" ]; then
      note_skip "${table}: суммы в слепке ДО нет — критерий §13.1.6 закрыть нечем"
      continue
    fi
    # ПРОВЕРКА ПРИГОДНОСТИ ВОССТАНОВЛЕННОЙ ГРАНИЦЫ — измерением, а не доводом.
    if [ "${BOUNDARY_KIND}" = "восстановленная" ]; then
      near="$(psql_q "
        SELECT count(*) FROM ${table} t
        WHERE t.${column} >  '${BOUNDARY}'::timestamptz - interval '1 hour'
          AND t.${column} <= '${BOUNDARY}'::timestamptz + interval '1 hour';")"
      metric "${table}_rows_near_boundary" "${near:-неизвестно}"
      if [ -z "${near}" ]; then
        note_skip "${table}: база не ответила — пригодность восстановленной границы проверить нечем"
        continue
      fi
      if [ "${near}" != "0" ]; then
        note_skip "${table}: ${near} строк(и) легли вплотную к восстановленной границе — отличить рост таблицы от переписывания старых строк НЕЧЕМ. Критерий §13.1.6 по этой таблице закрывается только слепком новой редакции"
        continue
      fi
    fi
    now_digest="$(psql_q "
      SELECT md5(string_agg(t::text, E'\n' ORDER BY t::text))
      FROM ${table} t WHERE t.${column} <= '${BOUNDARY}'::timestamptz;")"
    metric "${table}_digest_now" "${now_digest:-пусто}"
    # Число НОВЫХ строк печатается отдельно — это наблюдение, а не находка.
    fresh="$(psql_q "
      SELECT count(*) FROM ${table} t
      WHERE t.${column} > '${BOUNDARY}'::timestamptz;")"
    metric "${table}_rows_since_snapshot" "${fresh:-неизвестно}"
    if [ -z "${now_digest}" ]; then
      note_skip "${table}: база не ответила"
    elif [ "${was}" = "${now_digest}" ]; then
      note_ok "${table}: строки на момент слепка не изменены (новых с тех пор: ${fresh:-?}; граница ${BOUNDARY_KIND})"
    elif [ "${BOUNDARY_KIND}" = "восстановленная" ]; then
      # ГРАНИЦА ВОССТАНОВЛЕНА НАМИ, А НЕ ЗАПИСАНА СЛЕПКОМ, и разница эта —
      # не формальность. Строк вблизи границы нет, то есть расхождение
      # похоже на настоящее; но предписывать откат по величине, которую
      # скрипт вывел сам, он не вправе. Человеку сказано и то, и другое.
      note_stop "${table}: строки на момент ВОССТАНОВЛЕННОЙ границы отличаются от слепка. Похоже на переписывание старых строк — на слепке новой редакции это было бы предписанием отката (§13.1.6). Разберите прежде, чем продолжать"
    else
      # СТРОКИ, СУЩЕСТВОВАВШИЕ ДО РАЗВЁРТЫВАНИЯ, ПЕРЕПИСАНЫ. Это уже не рост
      # таблицы и не ночной пересчёт: пересчёт добавляет строки с новым
      # ``computed_at``, а не правит старые. Граница §1.2 нарушена.
      note_rollback "${table}: строки, существовавшие на момент слепка, ИЗМЕНИЛИСЬ — граница §1.2 нарушена"
    fi
  done
fi
echo

# --- 6. База: журнал отказов и послабление индекса (§7 ТЗ) ------------------
echo "6. Схема базы"
rej="$(psql_q "SELECT to_regclass('public.position_rejections') IS NOT NULL;")"
if [ "${rej}" = "t" ]; then
  note_ok "журнал отказов public.position_rejections на месте (миграция 028)"
  chk="$(psql_q "
    SELECT pg_get_constraintdef(oid) FROM pg_constraint
    WHERE conname = 'position_rejections_reason_chk';")"
  if echo "${chk}" | grep -q "token_pause"; then
    note_ok "перечень причин включает token_pause"
  else
    note_stop "в перечне причин journal'а нет token_pause — отказы по паузе не запишутся"
  fi
else
  note_stop "таблицы public.position_rejections нет: миграция 028 не применена, отказы писать некуда"
fi
idx="$(psql_q "
  SELECT indexdef FROM pg_indexes
  WHERE indexname = 'ux_positions_one_open_per_instrument';")"
if [ -z "${idx}" ]; then
  note_stop "индекса ux_positions_one_open_per_instrument нет вовсе: правило снято и для прежних версий"
elif echo "${idx}" | grep -q "logic_version < 7"; then
  note_ok "индекс ослаблен ТОЛЬКО для версии 7 (миграция 027 применена)"
else
  note_stop "индекс в прежнем виде: вторая позиция по токену получит отказ базы, и сделок не прибавится"
fi
sig_idx="$(psql_q "SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_positions_signal';")"
if [ "${sig_idx}" = "1" ]; then
  note_ok "правило «один сигнал — одна позиция» на месте"
else
  note_stop "индекса ux_positions_signal нет: один сигнал сможет открыть несколько позиций"
fi
echo

# --- 7. Сделки версии 7: наблюдательные числа (§13.2) -----------------------
echo "7. Сделки версии 7 (наблюдательные числа §13.2)"
# ТРИ ЧИСЛА ОДНИМ ЗАПРОСОМ: три отдельных дали бы три разных мгновения базы в
# одной строке отчёта. Максимум позиций на токен считается отдельно ниже —
# ему нужна группировка, несовместимая с этими тремя.
v7="$(psql_q "
  SELECT count(*) FILTER (WHERE opened_at > now() - interval '24 hours')
      || '|' || count(*) FILTER (WHERE status = 'open')
      || '|' || count(DISTINCT instrument_id) FILTER (WHERE status = 'open')
  FROM positions
  WHERE logic_version = 7;")"
if [ -z "${v7}" ]; then
  note_skip "база не ответила — числа по сделкам версии 7 снять нечем"
else
  opened="$(echo "${v7}" | cut -d'|' -f1)"
  open_now="$(echo "${v7}" | cut -d'|' -f2)"
  tokens="$(echo "${v7}" | cut -d'|' -f3)"
  metric "v7_opened_24h" "${opened}"
  metric "v7_open_now" "${open_now}"
  metric "v7_open_tokens" "${tokens}"
  per_token="$(psql_q "
    SELECT COALESCE(max(n), 0) FROM (
      SELECT count(*) AS n FROM positions
      WHERE logic_version = 7 AND status = 'open'
      GROUP BY instrument_id
    ) t;")"
  metric "v7_max_per_token_now" "${per_token:-0}"
  if [ "${open_now}" != "0" ] && [ "${open_now}" -gt "${tokens}" ] 2>/dev/null; then
    note_ok "по одному токену уже больше одной одновременной позиции — правило §4.1 действительно снято"
  fi
fi

# ОТКАЗЫ: РАЗБИВКА ПО ПРИЧИНАМ (§13.2). Читается из ЖУРНАЛА ОТКАЗОВ, а не из
# счётчиков Redis: у журнала есть токен и сигнал, а у счётчика только число.
if [ "${rej}" = "t" ]; then
  breakdown="$(psql_q "
    SELECT string_agg(reason || '=' || n, ' ' ORDER BY n DESC) FROM (
      SELECT reason, count(*) AS n FROM public.position_rejections
      WHERE logic_version = 7 AND ts_utc > now() - interval '24 hours'
      GROUP BY reason
    ) t;")"
  metric "v7_rejections_24h" "${breakdown:-нет}"
  # ДВА ОГРАНИЧИТЕЛЯ ЧИСЛА ОБЯЗАНЫ ПОКАЗЫВАТЬ НОЛЬ. Ненулевые означают, что
  # поток сделок режет не то, чем его собирались резать.
  for reason in max_open no_free_capital instrument_busy; do
    n="$(psql_q "
      SELECT count(*) FROM public.position_rejections
      WHERE logic_version = 7 AND reason = '${reason}'
        AND ts_utc > now() - interval '24 hours';")"
    if [ "${n}" = "0" ]; then
      note_ok "отказов ${reason} за сутки нет — ограничитель не применяется, как и заказано"
    elif [ -z "${n}" ]; then
      note_skip "отказы ${reason} посчитать не удалось"
    else
      note_stop "отказов ${reason} за сутки ${n}: ограничитель числа сделок ВКЛЮЧЁН"
    fi
  done
fi
echo

# --- 8. Уведомления: в Telegram уходят только сделки (§13.1.7) --------------
echo "8. Уведомления"
sent="$(psql_q "SELECT count(*) FROM signals WHERE notified_at > now() - interval '24 hours';")"
metric "signal_notifications_24h" "${sent:-неизвестно}"
if [ -z "${sent}" ]; then
  note_skip "база не ответила — сигнальные уведомления посчитать нечем"
elif [ "${sent}" = "0" ]; then
  note_ok "за сутки не отправлено ни одного уведомления О СИГНАЛЕ (§13.1.7)"
else
  note_stop "за сутки отправлено ${sent} уведомлений о сигналах: §13.1.7 требует ровно нуля"
fi

# ПОТОК САМИХ СИГНАЛОВ ТРОГАТЬ НЕЛЬЗЯ (§1.2, §6.1). Сравниваются два соседних
# часа: провал до нуля означает, что вместе с отправкой отключили и сбор.
flow="$(psql_q "
  SELECT count(*) FILTER (WHERE ts > now() - interval '1 hour') || '|' ||
         count(*) FILTER (WHERE ts > now() - interval '2 hours'
                            AND ts <= now() - interval '1 hour')
  FROM signals;")"
if [ -n "${flow}" ]; then
  last="${flow%%|*}"; prev="${flow##*|}"
  metric "signals_last_hour" "${last}"
  metric "signals_prev_hour" "${prev}"
  if [ "${last}" = "0" ] && [ "${prev}" != "0" ]; then
    note_stop "поток сигналов прекратился: §1.2 требует, чтобы сигналы собирались и писались как прежде"
  fi
fi

# СООБЩЕНИЯ О СДЕЛКАХ: ОТПРАВЛЕННЫЕ И ПОТЕРЯННЫЕ (§6.4, §13.2).
today="$(date -u +%F)"
for kind in sent failed; do
  n="$(redis_get "positions:notify:7:${today}:${kind}")"
  metric "trade_messages_${kind}_today" "${n:-0}"
done
lost="$(redis_get "positions:notify:7:${today}:failed")"
if [ -n "${lost}" ] && [ "${lost}" != "0" ]; then
  note_stop "сегодня не отправлено ${lost} сообщений о сделках: сделки состоялись, владелец о них не узнал"
fi
fail_lines="$(docker compose logs --since 24h --no-color positions 2>/dev/null \
              | grep -cF 'notify_trade_failed' || true)"
metric "notify_trade_failed_log_lines" "${fail_lines:-не_проверено}"

# СУТОЧНАЯ СВОДКА ПО СДЕЛКАМ: БЕРЁТСЯ ИЗ REDIS, А НЕ ИЗ ЖУРНАЛА КОНТЕЙНЕРА.
#
# Дефект первой редакции, найденный на боевом сервере 13.09.2026: число
# читалось из журнала контейнера ``notify`` и обнулялось при
# ``--force-recreate`` — было 1, стало 0, хотя сводка отправлена и никуда не
# делась. Это тот же класс, что §14.3 ТЗ («журнал контейнера, запущенного через
# run --rm, исчезает вместе с ним»), только про другую команду: пересозданный
# контейнер начинает журнал с нуля.
#
# ИСТОЧНИК ВЗЯТ ТОТ, ЧТО ПИШЕТ САМА СЛУЖБА И ТОЛЬКО ПОСЛЕ УСПЕШНОЙ ОТПРАВКИ:
# ключ ``notify:daily_trades:last_day`` ставится в ``daily_trades.send_once``
# строкой ПОСЛЕ ``send_message``, живёт 48 часов и лежит в томе ``redis_data``
# — том переживает пересоздание любого контейнера. Журнал остаётся запасным
# путём: если Redis недоступен, лучше приблизительный ответ, чем никакого, и
# он помечается как приблизительный.
summary_day="$(redis_get "notify:daily_trades:last_day")"
if [ -n "${summary_day}" ]; then
  metric "daily_summary_last_day" "${summary_day}"
  note_ok "суточная сводка по сделкам отправлена, последняя — за ${summary_day}"
else
  summary_lines="$(docker compose logs --since 48h --no-color notify 2>/dev/null \
                   | grep -cF 'notify_daily_summary_sent=1' || true)"
  if [ "${summary_lines:-0}" != "0" ]; then
    metric "daily_summary_sent_48h_from_log" "${summary_lines}"
    note_ok "сводка отправлена (${summary_lines} раз по журналу; отметки в Redis нет — ключ мог истечь)"
  else
    metric "daily_summary_last_day" "нет"
    note_skip "отметки об отправке сводки нет ни в Redis, ни в журнале — если развёртыванию меньше суток, это норма"
  fi
fi
echo

# --- 9. Прополка журнала отказов (§7.1) -------------------------------------
echo "9. Объём журнала отказов"
if [ "${rej}" = "t" ]; then
  size="$(psql_q "
    SELECT count(*) || '|' ||
           COALESCE(min(ts_utc)::date::text, 'нет') || '|' ||
           pg_size_pretty(pg_total_relation_size('public.position_rejections'))
    FROM public.position_rejections;")"
  if [ -n "${size}" ]; then
    metric "rejections_rows" "$(echo "${size}" | cut -d'|' -f1)"
    metric "rejections_oldest" "$(echo "${size}" | cut -d'|' -f2)"
    metric "rejections_size" "$(echo "${size}" | cut -d'|' -f3)"
    stale="$(psql_q "
      SELECT count(*) FROM public.position_rejections
      WHERE ts_utc < now() - interval '90 days';")"
    if [ "${stale}" = "0" ]; then
      note_ok "строк старше 90 суток нет — прополка работает"
    elif [ -z "${stale}" ]; then
      note_skip "возраст строк журнала проверить не удалось"
    else
      note_stop "в журнале ${stale} строк старше 90 суток: суточная прополка (scripts/retention.py) не отработала"
    fi
  else
    note_skip "размер журнала отказов узнать не удалось"
  fi
else
  note_skip "журнала отказов нет — объём проверять не на чем"
fi
echo

metric "verify_finished_at" "$(date -u +%FT%TZ)"
echo "Ключи прогона продублированы в ${METRICS}"
echo
echo "=== Итог: откат ${rollback}, остановиться ${stop}, не проверено ${unknown} ==="
if [ "${rollback}" -gt 0 ]; then
  echo
  echo "ПРЕДПИСАНИЕ: ОТКАТИТЬ ЭТАП. Нарушена граница этапа (§13.1.1, §13.1.2,"
  echo "§13.1.3 или §13.1.6) — это единственный случай, когда откат предписан."
  exit 1
fi
if [ "${stop}" -gt 0 ]; then
  echo
  echo "ПРЕДПИСАНИЕ: ОСТАНОВИТЬСЯ И РАЗОБРАТЬ. Откат НЕ предписывается: граница"
  echo "этапа цела, найденное к ней не относится (§14.4)."
  exit 3
fi
if [ "${unknown}" -gt 0 ]; then
  echo
  echo "ВНИМАНИЕ: ${unknown} проверок выполнить БЫЛО НЕЧЕМ. Это НЕ «всё хорошо»:"
  echo "закрыть соответствующие критерии §13 по этому прогону нельзя."
fi
exit 0
