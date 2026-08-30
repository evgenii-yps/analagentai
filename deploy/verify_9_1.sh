#!/usr/bin/env bash
# Проверка Этапа 9.1 — ведение одной позиции (виртуально) + починка чтения
# незакрытой свечи в сеточных стратегиях (Задача Б).
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_9_1.sh
#
# ТРИ КЛАССА НАХОДОК, и ни один не смешивается с другим:
#   🔴 БЛОКИРУЮЩЕЕ    — двух видов, и они РАЗНЫЕ:
#        (откат)      нарушена граница этапа; одной командой не чинится;
#        (устранимо)  развёртывание неполно, но запрещённого не произошло —
#                     печатается ВМЕСТЕ с командой устранения;
#   🟡 ТРЕБУЕТ ВНИМАНИЯ — знать нужно, откат не нужен;
#   ⚪ НЕ ПРОВЕРЕНО    — проверку выполнить НЕЧЕМ. Это НЕ «всё хорошо».
#
# В ИТОГЕ ПЕЧАТАЕТСЯ ТОЛЬКО ФАКТИЧЕСКИ СРАБОТАВШЕЕ, а не справочник всех
# мыслимых причин: справочник заставлял человека угадывать, какая строка
# относится к делу.
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ. Он НЕ ДЕЛАЕТ ВЫВОДОВ О ПРИБЫЛЬНОСТИ. На десятке
# позиций разница без доверительного интервала — впечатление, а не измерение.
# Числа он печатает; что они значат, решает не скрипт.
#
# ПОЧЕМУ ПОИСК В ЖУРНАЛАХ ИДЁТ ПО МАШИНОЧИТАЕМЫМ КЛЮЧАМ. Русский текст в
# журналах хранится экранированными последовательностями Unicode, и grep по
# русским словам не находит ничего.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
ENV_FILE="${APP_DIR}/.env"
LOGIC_VERSION_EXPECTED=5
# Отпечаток решений decision на 323 наборах входов. Значение то же, что в
# verify_8_10.sh, и это и есть смысл проверки: Этап 9.1 не менял ни одного
# решения, поэтому отпечаток обязан остаться прежним. Снят на коде репозитория
# до и после развёртывания Этапа 9.1 — совпал.
EXPECTED_DIGEST="1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
# Порог §13: изменение потока сигналов больше чем на 20% — признак, что этап
# всё-таки повлиял на горячий путь.
SIGNAL_DRIFT_PCT=20
# Пороги «требует внимания».
UNCERTAIN_MAX_PCT=10
SLIPPAGE_MAX_PCT=0.05

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
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -F "|" -q -c "$1" 2>/dev/null \
    | sed '/^$/d'
}
env_value() {
  [[ -f "$1" ]] || return 0
  grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- \
    | sed 's/#.*//' | xargs || true
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 9.1 — ведение одной позиции (виртуально) + Задача Б"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Каталог стека: ${APP_DIR}"
echo "=============================================================================="

# ---------------------------------------------------------------------------
echo
echo "── 1. Версия логики не поднята (Б1) ──────────────────────────────────────"
lv="$(env_value "${ENV_FILE}" LOGIC_VERSION)"
if [[ -z "${lv}" ]]; then
  note_unk "LOGIC_VERSION в .env не задан — действует умолчание кода, сверить не с чем"
elif [[ "${lv}" == "${LOGIC_VERSION_EXPECTED}" ]]; then
  note_ok "LOGIC_VERSION=${lv} в .env — версия не поднята"
else
  note_block "LOGIC_VERSION=${lv} в .env (ожидалось ${LOGIC_VERSION_EXPECTED}) — версия поднята"
fi
# И то же самое ПО ФАКТУ: версия в .env могла остаться прежней, а код писать
# другую. Смотрится последний час — это и есть «прямо сейчас».
lv_rows="$(psql_tbl "SELECT DISTINCT logic_version FROM signals
                     WHERE ts >= now() - interval '1 hour' ORDER BY 1;")"
if [[ -z "${lv_rows}" ]]; then
  note_unk "за последний час сигналов нет — версию по факту сверить нечем"
else
  echo "    версии сигналов за час: $(echo "${lv_rows}" | tr '\n' ' ')"
  if [[ "$(echo "${lv_rows}" | tr -d '[:space:]')" == "${LOGIC_VERSION_EXPECTED}" ]]; then
    note_ok "сигналы за час пишутся версией ${LOGIC_VERSION_EXPECTED}"
  else
    note_block "в signals за час не только версия ${LOGIC_VERSION_EXPECTED} — горячий путь изменён"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. Таблица позиций создана (У1) ───────────────────────────────────────"
exists="$(psql_val "SELECT to_regclass('positions') IS NOT NULL;")"
open_n=0; closed_n=0
if [[ "${exists}" != "t" ]]; then
  note_fix "таблицы positions нет — миграция 018 не применена" \
           "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/018_positions.sql"
else
  note_ok "таблица positions существует"
  checks="$(psql_val "SELECT count(*) FROM pg_constraint WHERE conrelid='positions'::regclass AND contype='c';")"
  echo "    ограничений CHECK: ${checks:-0} (ожидается не меньше 6)"
  if [[ "${checks:-0}" -ge 6 ]]; then
    note_ok "направление, состояние, причина выхода, разрешение и форма строки закрыты ограничениями"
  else
    note_warn "ограничений CHECK меньше ожидаемого (6): проверьте миграцию 018"
  fi
  # ГЛАВНОЕ ПРАВИЛО ЭТАПА, ЗАПИСАННОЕ БАЗОЙ. Без частичного уникального индекса
  # проверка «одна позиция на инструмент» живёт только в коде и переживает ровно
  # до первой гонки.
  ux_open="$(psql_val "SELECT count(*) FROM pg_indexes WHERE tablename='positions' AND indexname='ux_positions_one_open_per_instrument';")"
  ux_sig="$(psql_val "SELECT count(*) FROM pg_indexes WHERE tablename='positions' AND indexname='ux_positions_signal';")"
  if [[ "${ux_open:-0}" == "1" && "${ux_sig:-0}" == "1" ]]; then
    note_ok "уникальные индексы на месте: одна открытая позиция на инструмент, один сигнал — одна позиция"
  else
    note_fix "уникальных индексов positions нет — правило этапа живёт только в коде" \
             "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} < ${APP_DIR}/db/migrations/018_positions.sql"
  fi
  open_n="$(psql_val "SELECT count(*) FROM positions WHERE status='open';")"; open_n="${open_n:-0}"
  closed_n="$(psql_val "SELECT count(*) FROM positions WHERE status='closed';")"; closed_n="${closed_n:-0}"
fi

# ---------------------------------------------------------------------------
echo
echo "── 3. Право чтения у роли бота (У3) ──────────────────────────────────────"
if [[ "${exists}" != "t" ]]; then
  note_unk "таблицы positions нет — права проверять не на чем"
else
  role="$(psql_val "SELECT count(*) FROM pg_roles WHERE rolname='agenttrade_ro';")"
  if [[ "${role:-0}" == "0" ]]; then
    note_unk "роли agenttrade_ro нет — бот на этом сервере не настроен"
  else
    granted="$(psql_val "SELECT has_table_privilege('agenttrade_ro','positions','SELECT');")"
    if [[ "${granted}" == "t" ]]; then
      note_ok "agenttrade_ro читает positions — команда /positions ответит"
    else
      note_fix "у agenttrade_ro нет SELECT на positions — /positions молча перестанет отвечать" \
               "docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} -c 'GRANT SELECT ON positions TO agenttrade_ro;'"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 4. Позиции виртуальные и только на покупку (Б2, Б3, Б4) ───────────────"
if [[ "${exists}" != "t" ]]; then
  note_unk "таблицы positions нет — строки проверять не на чем"
else
  real="$(psql_val "SELECT count(*) FROM positions WHERE is_virtual = FALSE;")"
  if [[ "${real:-0}" == "0" ]]; then
    note_ok "строк с is_virtual = FALSE нет — настоящих сделок не было"
  else
    note_block "в positions ${real} строк с is_virtual = FALSE — настоящих сделок на этом этапе быть не может"
  fi
  sells="$(psql_val "SELECT count(*) FROM positions WHERE side <> 'buy';")"
  if [[ "${sells:-0}" == "0" ]]; then
    note_ok "строк со стороной, отличной от 'buy', нет — спот, продавать нечего"
  else
    note_block "в positions ${sells} строк со стороной <> 'buy'"
  fi
  dupes="$(psql_val "SELECT count(*) FROM (SELECT instrument_id FROM positions WHERE status='open' GROUP BY instrument_id HAVING count(*) > 1) x;")"
  if [[ "${dupes:-0}" == "0" ]]; then
    note_ok "по каждому инструменту не более одной открытой позиции"
  else
    note_block "по ${dupes} инструментам больше одной открытой позиции — главное правило этапа нарушено"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 5. Поток сигналов не изменился (Б5) ───────────────────────────────────"
# Признак того, что этап всё-таки повлиял на горячий путь. Сравниваются СУТКИ
# против предыдущих суток: час против часа гулял бы сам по себе.
line="$(psql_tbl "SELECT count(*) FILTER (WHERE ts >= now() - interval '24 hours'),
                         count(*) FILTER (WHERE ts >= now() - interval '48 hours'
                                            AND ts <  now() - interval '24 hours')
                  FROM signals;")"
today="$(echo "${line}" | cut -d'|' -f1 | tr -d '[:space:]')"
before="$(echo "${line}" | cut -d'|' -f2 | tr -d '[:space:]')"
echo "    сигналов за сутки: ${today:-·}, за предыдущие сутки: ${before:-·}"
if [[ -z "${today}" || -z "${before}" ]]; then
  note_unk "поток сигналов не получен — база не ответила"
elif [[ "${before}" == "0" ]]; then
  note_unk "за предыдущие сутки сигналов нет — сравнивать не с чем. Это НЕ «поток не изменился»"
else
  drift="$(awk -v a="${today}" -v b="${before}" 'BEGIN{printf "%.1f", (a-b)*100.0/b}')"
  echo "    изменение: ${drift}% (порог ±${SIGNAL_DRIFT_PCT}%)"
  over="$(awk -v d="${drift}" -v t="${SIGNAL_DRIFT_PCT}" 'BEGIN{print (d<-t || d>t) ? 1 : 0}')"
  if [[ "${over}" == "1" ]]; then
    note_block "поток сигналов изменился на ${drift}% — признак вмешательства в горячий путь"
  else
    note_ok "поток сигналов изменился на ${drift}% — в пределах обычного разброса"
  fi
fi

# ---------------------------------------------------------------------------
echo
echo "── 6. ЗАДАЧА Б: подозрительных строк не осталось (Б6) ────────────────────"
# Запас берётся из настроек, а не зашит числом: разойдись он с кодом — проверка
# отвечала бы на вопрос о другом правиле.
coarse_tf="$(env_value "${ENV_FILE}" BARRIER_COARSE_TIMEFRAME)"; coarse_tf="${coarse_tf:-1h}"
settle_min="$(env_value "${ENV_FILE}" BARRIER_SETTLE_MINUTES)"; settle_min="${settle_min:-5}"
case "${coarse_tf}" in
  1h) coarse_sec=3600 ;;
  1m) coarse_sec=60 ;;
  *)  coarse_sec="" ;;
esac
if [[ -z "${coarse_sec}" ]]; then
  note_unk "BARRIER_COARSE_TIMEFRAME=${coarse_tf} неизвестен — запас не вычислить"
else
  settle_sec=$(( coarse_sec + settle_min * 60 ))
  # ЗАПАС БЕРЁТСЯ ПО ФАКТИЧЕСКОМУ РАЗРЕШЕНИЮ СТРОКИ (Этап 9.1.1 §1).
  #
  # Прежняя редакция закладывала ${settle_sec} секунд ВСЕМ строкам подряд. Это
  # верно ПРИ ОТБОРЕ кандидатов — там разрешение ещё неизвестно и приходится
  # брать худший случай, — но у УЖЕ ПОСЧИТАННОЙ строки разрешение известно и
  # записано в колонке resolution. Все строки боевой базы посчитаны по
  # МИНУТНОМУ ряду, где последний бар окна закрывается через 60 секунд после
  # срока: проверка их часовым запасом дала 7618 ложных срабатываний при 0
  # настоящих (замер 30.08.2026) и печатала «ОТКАТ ОБЯЗАТЕЛЕН» на здоровой
  # системе.
  #
  # ВЕТКА ELSE ОСТАВЛЕНА НАМЕРЕННО: появись третье разрешение, его строки
  # будут проверяться ХУДШИМ случаем, а не проскочат молча.
  #
  # Формула повторяет DB.STRATEGY_UNSETTLED_PREDICATE (src/core/db.py)
  # текстуально: разделяемого кода между bash и Python нет, поэтому копии
  # обязаны меняться вместе.
  unsettled_where="computed_at < entry_ts
                     + make_interval(hours => horizon_h::int)
                     + make_interval(secs => CASE resolution
                                               WHEN '1m' THEN 60
                                               WHEN '1h' THEN 3600
                                               ELSE ${settle_sec}
                                             END)"
  echo "    запас берётся ПО КОЛОНКЕ resolution строки:"
  echo "      60 с для '1m', 3600 с для '1h', ${settle_sec} с для неизвестного"
  echo "      (${settle_sec} с = ${coarse_tf} + ${settle_min} мин из .env — только для неизвестного)"
  so_exists="$(psql_val "SELECT to_regclass('strategy_outcomes') IS NOT NULL;")"
  if [[ "${so_exists}" != "t" ]]; then
    note_unk "таблицы strategy_outcomes нет — Задачу Б проверять не на чем"
  else
    # К ЧЕМУ ПРИМЕНЁН КРИТЕРИЙ — печатается всегда, а не только при находке:
    # без этого числа «подозрительных 0» не отличить от «строк нет вовсе».
    echo "    строк по разрешениям:"
    psql_tbl "SELECT resolution, count(*) FROM strategy_outcomes
              GROUP BY resolution ORDER BY 2 DESC;" | sed 's/^/      /'
    bad="$(psql_val "SELECT count(*) FROM strategy_outcomes
                     WHERE ${unsettled_where};")"
    echo "    подозрительных строк: ${bad:-·} (ожидается 0)"
    if [[ -z "${bad}" ]]; then
      note_unk "запрос не выполнен — база не ответила"
    elif [[ "${bad}" == "0" ]]; then
      note_ok "строк, посчитанных по незакрытому бару, не осталось"
    else
      note_block "в strategy_outcomes осталось ${bad} строк, посчитанных по незакрытому бару"
      echo "    по стратегии, горизонту и разрешению:"
      psql_tbl "SELECT strategy, horizon_h, resolution, count(*)
                FROM strategy_outcomes
                WHERE ${unsettled_where}
                GROUP BY strategy, horizon_h, resolution ORDER BY 4 DESC;" \
        | sed 's/^/      /'
      info "СНАЧАЛА посмотреть отчёт (ничего не меняет):"
      info "  docker compose --profile tools run --rm --no-deps \\"
      info "      -v ./scripts:/app/scripts:ro -v ./reports:/app/reports \\"
      info "      barrier python -m scripts.repair_9_1_strategy_settle"
      info "и только потом удалить, назвав то же число своими руками:"
      info "  docker compose --profile tools run --rm --no-deps \\"
      info "      -v ./scripts:/app/scripts:ro -v ./reports:/app/reports \\"
      info "      barrier python -m scripts.repair_9_1_strategy_settle \\"
      info "      --apply --confirm-count=${bad}"
    fi
  fi
fi
# И то, что дефект не вернётся: правило годности берётся из одного места с 8.10.1.
if [[ -f "${APP_DIR}/src/baseline/runner.py" ]]; then
  if grep -q "settle_seconds" "${APP_DIR}/src/baseline/runner.py"; then
    note_ok "сеточные стратегии ждут закрытия последнего бара (settle_seconds из 8.10.1)"
  else
    note_block "src/baseline/runner.py не использует settle_seconds — дефект Задачи Б вернётся"
  fi
else
  note_unk "нет файла src/baseline/runner.py — правило годности проверить нечем"
fi

# ---------------------------------------------------------------------------
echo
echo "── 7. Контейнер ведения позиций (У2) ─────────────────────────────────────"
running="$(docker compose ps --status running --services 2>/dev/null | grep -cx positions)"
if [[ "${running:-0}" == "1" ]]; then
  note_ok "контейнер positions работает"
  # Контейнеров стало девять — это ожидаемо: восемь прежних постоянных плюс
  # positions. Печатается числом, чтобы девятый не выглядел лишним при
  # следующей проверке.
  total="$(docker compose ps --status running --services 2>/dev/null | wc -l | tr -d ' ')"
  info "постоянных контейнеров работает: ${total} (после Этапа 9.1 ожидается 9)"
else
  note_fix "контейнер positions не запущен — позиции не ведутся" \
           "cd ${APP_DIR} && docker compose up -d positions"
fi

hb="$(docker compose exec -T redis redis-cli --no-raw GET positions:heartbeat 2>/dev/null | tr -d '"[:space:]')"
if [[ -z "${hb}" || "${hb}" == "(nil)" ]]; then
  note_unk "heartbeat positions в Redis не найден — свежесть сервиса не подтверждена"
else
  echo "    heartbeat positions: ${hb}"
  note_ok "heartbeat положен сервисом ведения позиций"
fi

# ---------------------------------------------------------------------------
echo
echo "── 8. Справочные числа по позициям ───────────────────────────────────────"
if [[ "${exists}" != "t" ]]; then
  note_unk "таблицы positions нет — печатать нечего"
else
  echo "    открытых позиций:  ${open_n}"
  echo "    закрытых позиций:  ${closed_n}"
  if [[ "${closed_n}" != "0" ]]; then
    echo "    по причинам выхода:"
    psql_tbl "SELECT exit_reason, count(*) FROM positions WHERE status='closed'
              GROUP BY exit_reason ORDER BY 2 DESC;" | sed 's/^/      /'
    # СРЕДНИЕ И СУММЫ — БЕЗ ЗАКРЫТИЙ ПО ПРОБЕЛУ В ДАННЫХ (Этап 9.1.1 §6.7).
    # У них цена выхода не наблюдалась, а восстановлена; их «итог» описывает
    # длительность сбоя сбора данных, а не поведение рынка. Те же FILTER стоят
    # в src/bot/queries.positions_summary: разойдись они — бот и проверка
    # показывали бы разные средние по одной и той же таблице.
    stat_line="$(psql_tbl "SELECT round(avg(net_pnl_pct)
                                    FILTER (WHERE exit_reason <> 'data_gap'), 4),
                                  round(sum(net_pnl_usd)
                                    FILTER (WHERE exit_reason <> 'data_gap'), 4),
                                  round(avg(entry_lag_sec), 1),
                                  round(avg(entry_slippage_pct)
                                    FILTER (WHERE exit_reason <> 'data_gap'), 5),
                                  count(*) FILTER (WHERE outcome_certain = FALSE)
                           FROM positions WHERE status='closed';")"
    echo "    средний net_pnl_pct:        $(echo "${stat_line}" | cut -d'|' -f1) (без data_gap)"
    echo "    сумма net_pnl_usd:          $(echo "${stat_line}" | cut -d'|' -f2) (без data_gap)"
    echo "    средний entry_lag_sec:      $(echo "${stat_line}" | cut -d'|' -f3)"
    echo "    средний entry_slippage_pct: $(echo "${stat_line}" | cut -d'|' -f4) (без data_gap)"
    uncertain="$(echo "${stat_line}" | cut -d'|' -f5 | tr -d '[:space:]')"
    slip="$(echo "${stat_line}" | cut -d'|' -f4 | tr -d '[:space:]')"

    # В2. Доля неопределённых выше 10%.
    share="$(awk -v u="${uncertain:-0}" -v c="${closed_n}" 'BEGIN{printf "%.1f", u*100.0/c}')"
    over="$(awk -v s="${share}" -v t="${UNCERTAIN_MAX_PCT}" 'BEGIN{print (s>t)?1:0}')"
    if [[ "${over}" == "1" ]]; then
      note_warn "доля закрытий с outcome_certain = FALSE — ${share}% (порог ${UNCERTAIN_MAX_PCT}%): у них итог взят по пределу, порядок касаний внутри минуты неизвестен"
    else
      note_ok "доля закрытий с неопределённым порядком касаний — ${share}%"
    fi

    # В3. Средний снос входа по модулю выше 0.05%.
    if [[ -n "${slip}" ]]; then
      big="$(awk -v s="${slip}" -v t="${SLIPPAGE_MAX_PCT}" 'BEGIN{s=(s<0?-s:s); print (s>t)?1:0}')"
      if [[ "${big}" == "1" ]]; then
        note_warn "средний entry_slippage_pct по модулю ${slip}% выше ${SLIPPAGE_MAX_PCT}% — задержка между решением и входом стоит заметных денег"
      else
        note_ok "средний entry_slippage_pct — ${slip}%"
      fi
    fi
  fi

  # ЗАКРЫТИЯ ПО ПРОБЕЛУ В ДАННЫХ ЗА СУТКИ (Этап 9.1.1 §6.7). Печатается всегда,
  # включая ноль: здесь ноль — это ответ «сбор данных не подводил», и он
  # содержателен ровно настолько же, насколько ненулевое число.
  gap_24h="$(psql_val "SELECT count(*) FROM positions
                       WHERE status = 'closed' AND exit_reason = 'data_gap'
                         AND closed_at >= now() - interval '24 hours';")"
  echo "    закрыто по пробелу в данных за сутки: ${gap_24h:-·}"
  if [[ -n "${gap_24h}" && "${gap_24h}" != "0" ]]; then
    note_warn "за сутки ${gap_24h} позиций закрыто по пробелу в данных (exit_reason='data_gap'): ряд свечей по инструменту прерывался дольше POSITION_GAP_GRACE_SEC. Это состояние СБОРА ДАННЫХ, а не рынка — их итоги в средние выше не входят"
    psql_tbl "SELECT i.symbol, count(*), max(p.closed_at)
              FROM positions p JOIN instruments i ON i.id = p.instrument_id
              WHERE p.status = 'closed' AND p.exit_reason = 'data_gap'
                AND p.closed_at >= now() - interval '24 hours'
              GROUP BY i.symbol ORDER BY 2 DESC;" | sed 's/^/      /'
  fi

  # В1. Открытых позиций ноль дольше суток.
  if [[ "${open_n}" == "0" ]]; then
    last_open="$(psql_val "SELECT count(*) FROM positions WHERE opened_at >= now() - interval '24 hours';")"
    if [[ "${last_open:-0}" == "0" ]]; then
      note_warn "открытых позиций нет и за сутки не открывалось ни одной — порог вероятности может оказаться недостижимым. Это результат, а не поломка, но знать надо"
      echo "    причины отказа за сутки (из журнала сервиса) — раздел 9 ниже"
    fi
  fi
  echo
  echo "    ЭТОТ СКРИПТ НЕ ДЕЛАЕТ ВЫВОДОВ О ПРИБЫЛЬНОСТИ. На десятке позиций"
  echo "    разница без доверительного интервала — впечатление, а не измерение."
fi

# ---------------------------------------------------------------------------
echo
echo "── 9. Журнал сервиса позиций (В4, Н1) ────────────────────────────────────"
# Поиск ТОЛЬКО по машиночитаемым ключам: русский текст в журналах хранится
# экранированными последовательностями Unicode, и grep по русским словам не
# находит ничего.
logs="$(docker compose logs --since 24h --no-color positions 2>/dev/null)"
if [[ -z "${logs}" ]]; then
  note_unk "журнал контейнера positions за сутки пуст или недоступен — проверку по журналу выполнить нечем"
else
  failed="$(printf '%s' "${logs}" | grep -c 'positions_iteration_failed=1')"
  races="$(printf '%s' "${logs}" | grep -c 'positions_race_skipped=1')"
  opened="$(printf '%s' "${logs}" | grep -c 'positions_opened=1')"
  closed_log="$(printf '%s' "${logs}" | grep -c 'positions_closed=1')"
  echo "    positions_opened=1:            ${opened}"
  echo "    positions_closed=1:            ${closed_log}"
  echo "    positions_race_skipped=1:      ${races} (штатный исход: кто-то опередил)"
  echo "    positions_iteration_failed=1:  ${failed}"
  if [[ "${failed}" -gt 0 ]]; then
    note_warn "в журнале ${failed} упавших итераций за сутки — сервис не падает, но причина ниже:"
    printf '%s' "${logs}" | grep 'positions_iteration_failed=1' | tail -3 | sed 's/^/      /'
  else
    note_ok "упавших итераций за сутки нет"
  fi
  # ПРИЧИНА БЕРЁТСЯ ТОЛЬКО ИЗ СТРОК ОТКАЗА (Этап 9.1.1 §3). Прежняя редакция
  # искала поле reason= по ВСЕМУ журналу за сутки, хотя заголовок обещал
  # причины отказа во входе: при трёх настоящих отказах (все три —
  # no_fresh_bar) вывод показал два счётчика с ПУСТОЙ причиной.
  #
  # И ФОРМАТОВ ЖУРНАЛА ДВА (Этап 9.1.1 §3-бис). src/core/logging.py выбирает
  # рендер по sys.stdout.isatty(). В контейнере stdout не терминал (tty в
  # docker-compose.yml не задан ни у одного сервиса), поэтому работает
  # JSONRenderer, и причина лежит в поле "reason": "no_fresh_bar" — БЕЗ ЗНАКА
  # РАВЕНСТВА. Сама строка positions_skipped=1 при этом находится, потому что
  # целиком лежит в поле event: шаблон reason= печатал бы заголовок и ни одной
  # строки под ним — ровно то состояние, ради устранения которого §3 и писался.
  # При ручном запуске в терминале работает ConsoleRenderer и формат другой:
  # reason=no_fresh_bar. Разбор понимает ОБА и не теряет ни одной строки.
  #
  # И пустой вывод обязан читаться как «отказов не было», а не как пустое
  # место: пустое место читатель толкует как поломку скрипта.
  #
  # >>> reason-block: этот блок целиком извлекает и ПРОГОНЯЕТ на синтетических
  # журналах tests/test_stage_9_1_1.py. Разбор проверяется на настоящем коде
  # проверки, а не на его пересказе в тесте: пересказ разошёлся бы с оригиналом
  # молча. Границы блока — эти две пометки; между ними нет ничего, что нельзя
  # выполнить без запущенного docker.
  skipped="$(printf '%s' "${logs}" | grep 'positions_skipped=1' || true)"
  if [[ -z "${skipped}" ]]; then
    echo "    причины отказа во входе (positions_skipped=1) за сутки: отказов не было"
  else
    skipped_n="$(printf '%s\n' "${skipped}" | grep -c 'positions_skipped=1')"
    # КАЖДАЯ строка даёт РОВНО ОДИН результат. Ветка t в sed останавливает
    # разбор строки после первого удавшегося вида записи, а последняя подстановка
    # без условия ловит всё остальное. Строка, в которой причину распознать не
    # удалось, НЕ ОТБРАСЫВАЕТСЯ: потерянный отказ выглядит как отсутствие
    # отказа, а это худший вид ошибки в проверке.
    counted="$(printf '%s\n' "${skipped}" | sed -E \
      -e 's/.*"reason"[[:space:]]*:[[:space:]]*"([a-z_]+)".*/\1/; t' \
      -e 's/.*reason=([a-z_]+).*/\1/; t' \
      -e 's/.*/(причина не распознана)/')"
    # СУММА СЧИТАЕТСЯ ДО ОБРЕЗКИ head -10: обрезка — свойство показа, а не
    # разбора, и сверка, посчитанная после неё, всегда сходилась бы «почти».
    total="$(printf '%s\n' "${counted}" | grep -c .)"
    kinds="$(printf '%s\n' "${counted}" | sort -u | grep -c .)"
    echo "    причины отказа во входе (positions_skipped=1), за сутки (всего ${skipped_n}):"
    printf '%s\n' "${counted}" | sort | uniq -c | sort -rn | head -10 \
      | sed 's/^/      /'
    if [[ "${kinds}" -gt 10 ]]; then
      echo "      показаны первые 10 из ${kinds}"
    fi
    # РАЗБОР, КОТОРЫЙ НЕ УМЕЕТ СЕБЯ ПРОВЕРИТЬ, ОДНАЖДЫ СОВРЁТ НЕЗАМЕТНО. Сумма
    # счётчиков обязана совпадать с числом строк отказа; расхождение означает,
    # что разбор потерял строку, и это находка, а не мелочь.
    if [[ "${total}" != "${skipped_n}" ]]; then
      note_warn "разбор причин расходится с числом строк отказа: ${total} против ${skipped_n} — разбор потерял строки, счётчикам выше верить нельзя"
    fi
  fi
  # <<< reason-block
fi

# ---------------------------------------------------------------------------
echo
echo "── 10. Отпечаток решений decision не изменился ───────────────────────────"
# Доказательство, что горячий путь не тронут. Слепок снимается ВНУТРИ
# КОНТЕЙНЕРА: решения принимает код в образе, и утверждение относится к нему.
parity="${APP_DIR}/scripts/decision_parity_8_7.py"
if [[ ! -f "${parity}" ]]; then
  note_unk "нет файла ${parity} — слепок решений не снять"
else
  digest="$(docker compose exec -T -e POSTGRES_PASSWORD=parity decision \
              python - < "${parity}" 2>/dev/null \
            | docker compose exec -T decision python -c \
              "import json,sys; print(json.load(sys.stdin)['digest_sha256'])" 2>/dev/null)"
  if [[ -z "${digest}" ]]; then
    note_unk "слепок не снят — вывод о неизменности решений сделать нельзя"
    info "проверьте, что контейнер decision работает: docker compose ps decision"
  else
    echo "    отпечаток решений: ${digest}"
    echo "    ожидается:         ${EXPECTED_DIGEST}"
    if [[ "${digest}" == "${EXPECTED_DIGEST}" ]]; then
      note_ok "decision, probability и calibrated_probability на 323 наборах входов не изменились"
    else
      note_block "решения ИЗМЕНИЛИСЬ — это нарушает жёсткую границу этапа"
    fi
  fi
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
    echo " Этап обещал добавить наблюдение рядом и не менять ни одного решения"
    echo " системы. Перечисленное выше означает, что обещание нарушено, и одной"
    echo " командой это не чинится."
    echo
    echo "   docker compose stop positions"
    echo "   docker compose exec -T postgres psql -U ${DB_USER} -d ${DB_NAME} \\"
    echo "       < ${APP_DIR}/db/migrations/018_positions_rollback.sql"
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
  echo " ДЕЙСТВИЕ: откат НЕ НУЖЕН. Позиции ведутся параллельно системе и ни на"
  echo " одно её решение не влияют."
  echo
  echo " Напоминание: «открытых позиций ноль» — законный результат. Цена"
  echo " выбранного правила отбора известна заранее: при высоком пороге"
  echo " вероятности сигналов может не быть несколько дней."
  exit 0
else
  echo " ДЕЙСТВИЕ: не требуется. Позиции ведутся, решения системы не изменились,"
  echo " строк, посчитанных по незакрытому бару, не осталось."
  exit 0
fi
