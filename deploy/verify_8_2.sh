#!/usr/bin/env bash
# Проверка развёртывания Этапа 8.2 (цели по вероятности).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: docker compose ps / SELECT / grep по журналам. Ничего не меняет.
#
#   cd /opt/agent-trade && bash deploy/verify_8_2.sh
#
# ЧЕТЫРЕ ТРЕБОВАНИЯ §11 ТЗ, нарушавшиеся в прежних скриптах:
#   1) находки разделены на БЛОКИРУЮЩИЕ (нужен откат) и ВНИМАНИЕ (откат не нужен),
#      в конце — одна строка ДЕЙСТВИЕ;
#   2) счётчики печатают ЧИСЛА, а не «(1 row)»;
#   3) поиск в журнале — по МАШИНОЧИТАЕМЫМ ключам: журнал хранит кириллицу
#      экранированной (ц...), и grep по русскому слову даёт ноль на
#      ИСПРАВНОЙ системе;
#   4) «ничего не найдено» само по себе находкой не считается: скрипт отличает
#      «признака нет» от «искать не умеем» (нет файла журнала, нет таблицы).
# И пятое: там, где вывод зависит от данных, вердикт не выносится — названы оба
# исхода и указано, на какие столбцы смотреть.
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
RISK_LOG="${RISK_LOG:-${APP_DIR}/logs/risk.log}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

blocking=0     # мешает работе или показывает человеку неверное число
attention=0    # стоит знать, но откат не нужен
unknown=0      # проверка не выполнена: искать нечем (нет файла, нет таблицы)

block()   { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
warn()    { echo "  🟡 ВНИМАНИЕ:    $*"; attention=$((attention + 1)); }
unknw()   { echo "  ⚪ НЕ ПРОВЕРЕНО: $*"; unknown=$((unknown + 1)); }
ok()      { echo "  🟢 $*"; }
info()    { echo "  ℹ  $*"; }

psql_q() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -c "$1" 2>/dev/null \
    | tr -d '[:space:]'
}
psql_rows() {
  docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -F '|' -c "$1" 2>/dev/null
}
env_value() {
  local key="$1" default="${2:-}"
  local value
  value="$(grep -E "^${key}=" "${APP_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
           | sed 's/#.*//' | xargs || true)"
  echo "${value:-$default}"
}
table_exists() {
  local name="$1"
  [ "$(psql_q "SELECT count(*) FROM information_schema.tables
                WHERE table_schema='public' AND table_name='${name}';")" = "1" ]
}

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.2 — цели по вероятности"
echo " Время (UTC): $(date -u +%FT%TZ)"
echo "=============================================================================="

echo
echo "=== 1. Схема: миграция 014 (§3, критерий A) ==="
have_risk=0
have_signal=0
table_exists risk_targets   && have_risk=1
table_exists signal_targets && have_signal=1
echo "  таблиц из двух на месте: $((have_risk + have_signal)) из 2"
if [ "$have_risk" != "1" ] || [ "$have_signal" != "1" ]; then
  block "миграция 014 не применена (risk_targets=${have_risk}, signal_targets=${have_signal})"
  info  "применить: docker compose exec -T postgres psql -U ${PG_USER} -d ${PG_DB} < db/migrations/014_risk_targets.sql"
else
  ok "обе таблицы существуют"
  frozen_rule="$(psql_q "SELECT count(*) FROM pg_constraint
                          WHERE conrelid='risk_targets'::regclass
                            AND conname='risk_targets_reason_matches_target';")"
  echo "  ограничение «цель и причина взаимно исключают друг друга»: ${frozen_rule:-0} из 1"
  if [ "${frozen_rule:-0}" != "1" ]; then
    warn "нет ограничения risk_targets_reason_matches_target"
    info  "без него возможна строка и с целью, и с причиной её отсутствия — читается двояко"
  fi
fi

echo
echo "=== 2. Наполнение risk_targets (§4, критерии C и D) ==="
if [ "$have_risk" != "1" ]; then
  unknw "таблицы risk_targets нет — проверять нечего (это следствие находки выше, а не отдельная)"
else
  last_at="$(psql_q "SELECT coalesce(to_char(max(computed_at),'YYYY-MM-DD_HH24:MI'),'нет') FROM risk_targets;")"
  total="$(psql_q "SELECT count(*) FROM risk_targets;")"
  echo "  строк всего: ${total:-0}"
  echo "  последний пересчёт (UTC): ${last_at:-нет}"
  if [ "${total:-0}" = "0" ]; then
    block "risk_targets пуста — первый расчёт не запускался"
    info  "запустить: docker compose --profile tools run --rm --no-deps risk"
  else
    # Ожидается 5 инструментов x 4 горизонта x 2 направления = 40 строк на прогон.
    last_rows="$(psql_q "SELECT count(*) FROM risk_targets
                          WHERE computed_at = (SELECT max(computed_at) FROM risk_targets);")"
    instr="$(psql_q "SELECT count(DISTINCT instrument_id) FROM risk_targets
                      WHERE computed_at = (SELECT max(computed_at) FROM risk_targets);")"
    hors="$(psql_q "SELECT count(DISTINCT horizon_h) FROM risk_targets
                     WHERE computed_at = (SELECT max(computed_at) FROM risk_targets);")"
    dirs="$(psql_q "SELECT count(DISTINCT direction) FROM risk_targets
                     WHERE computed_at = (SELECT max(computed_at) FROM risk_targets);")"
    echo "  в последнем пересчёте: строк ${last_rows:-0}, инструментов ${instr:-0}, горизонтов ${hors:-0}, направлений ${dirs:-0}"
    if [ "${last_rows:-0}" -lt 40 ]; then
      warn "в последнем пересчёте меньше 40 строк (5 x 4 x 2)"
      info  "неполный состав — не обязательно поломка: инструмент без истории строку всё равно получает, а вот пропуск инструмента целиком означает сбой"
    else
      ok "состав пересчёта полный: ${last_rows} строк"
    fi

    with_target="$(psql_q "SELECT count(*) FROM risk_targets
                            WHERE computed_at=(SELECT max(computed_at) FROM risk_targets)
                              AND target_pct IS NOT NULL;")"
    echo "  из них с целью: ${with_target:-0}, без цели: $(( ${last_rows:-0} - ${with_target:-0} ))"
    echo "  причины отсутствия цели:"
    psql_rows "SELECT coalesce(no_target_reason,'(цель есть)'), count(*)
                 FROM risk_targets
                WHERE computed_at=(SELECT max(computed_at) FROM risk_targets)
                GROUP BY 1 ORDER BY 1;" | while IFS='|' read -r reason n; do
      [ -n "$reason" ] && echo "    ${reason}: ${n}"
    done
    info "строка без цели — это НЕ поломка сама по себе: 'few_observations' и 'data_gap' означают, что данных не хватило, и цель честно не выдана"

    # §4.5: доля касаний считается фактически. Отклонение больше 0.02 — ошибка расчёта.
    hit_off="$(psql_q "SELECT count(*) FROM risk_targets
                        WHERE target_pct IS NOT NULL AND (hit_rate IS NULL
                              OR abs(hit_rate - 0.60) > 0.02);")"
    echo "  строк с |hit_rate − 0.60| > 0.02: ${hit_off:-0}"
    if [ "${hit_off:-0}" != "0" ]; then
      block "доля касаний расходится с 40-м процентилем более чем на 0.02 в ${hit_off} строках"
      info  "это признак ошибки в расчёте цели (§4.5 ТЗ), а не свойство рынка: по построению процентиля должно быть ≈0.60"
    else
      ok "доля касаний согласуется с 40-м процентилем во всех строках с целью"
    fi

    neg="$(psql_q "SELECT count(*) FROM risk_targets WHERE target_pct <= 0;")"
    echo "  строк с целью ≤ 0: ${neg:-0}"
    [ "${neg:-0}" != "0" ] && block "цель не может быть неположительной: уровень ниже цены не является целью покупки"
  fi
fi

echo
echo "=== 3. Издержки и покрытие комиссии (§5) ==="
cost="$(env_value RISK_COST_ROUNDTRIP_PCT 0.22)"
echo "  RISK_COST_ROUNDTRIP_PCT: ${cost}"
echo "  порог покрытия (3 × издержки): $(awk -v c="$cost" 'BEGIN{printf "%.4f", c*3}')"
bt_fee="$(grep -E '^BT_FEE_ROUNDTRIP_PCT=' "${APP_DIR}/backtest/.env.backtest" 2>/dev/null \
          | tail -1 | cut -d= -f2- | sed 's/#.*//' | xargs || true)"
if [ -z "$bt_fee" ]; then
  unknw "backtest/.env.backtest не найден или не содержит BT_FEE_ROUNDTRIP_PCT — сравнить не с чем"
  info  "это не «всё хорошо»: значение просто не прочитано"
else
  echo "  BT_FEE_ROUNDTRIP_PCT (реплей 7.4): ${bt_fee}"
  # 0.10 — половина круговой комиссии тейкера (0.10% × 2 сделки).
  if awk -v v="$bt_fee" 'BEGIN{exit !(v < 0.20)}'; then
    warn "BT_FEE_ROUNDTRIP_PCT=${bt_fee} меньше круговой комиссии тейкера 0.20"
    info  "это ЗАНИЖАЕТ издержки в уже опубликованных результатах Этапа 7.4; менять значение молча нельзя — решение принимает владелец (§5 ТЗ 8.2)"
  fi
fi
if [ "$have_risk" = "1" ]; then
  covers="$(psql_q "SELECT count(*) FROM risk_targets
                     WHERE computed_at=(SELECT max(computed_at) FROM risk_targets)
                       AND covers_fees;")"
  echo "  целей, покрывающих тройную комиссию, в последнем пересчёте: ${covers:-0}"
  info "малое число здесь — свойство РЫНКА, а не поломка: на часовых горизонтах ход часто меньше издержек"
fi

echo
echo "=== 4. Заморозка целей при сигнале (§6, критерии E и F) ==="
if [ "$have_signal" != "1" ]; then
  unknw "таблицы signal_targets нет — проверять нечего"
else
  st_rows="$(psql_q "SELECT count(*) FROM signal_targets;")"
  st_signals="$(psql_q "SELECT count(DISTINCT signal_id) FROM signal_targets;")"
  echo "  строк в signal_targets: ${st_rows:-0} по ${st_signals:-0} сигналам"
  directed="$(psql_q "SELECT count(*) FROM signals WHERE decision <> 'wait'
                       AND ts > now() - interval '24 hours';")"
  covered="$(psql_q "SELECT count(DISTINCT s.id) FROM signals s
                      JOIN signal_targets t ON t.signal_id = s.id
                     WHERE s.decision <> 'wait' AND s.ts > now() - interval '24 hours';")"
  echo "  направленных сигналов за сутки: ${directed:-0}, из них с целями: ${covered:-0}"
  if [ "${directed:-0}" = "0" ]; then
    info "направленных сигналов за сутки не было — это состояние рынка и порогов, а не признак поломки; проверку заморозки повторите после первого сигнала buy/sell"
  elif [ "${covered:-0}" -lt "${directed:-0}" ]; then
    warn "у $(( ${directed:-0} - ${covered:-0} )) сигналов целей нет"
    info  "сигнал без цели — штатное поведение при сбое расчёта (§6 ТЗ: сигнал важнее цели). Причину ищите в agent_failures (agent='risk_targets')"
  else
    ok "у всех направленных сигналов за сутки цели заморожены"
  fi
  wrong_h="$(psql_q "SELECT count(*) FROM (
                        SELECT signal_id, count(*) AS n FROM signal_targets
                        GROUP BY signal_id HAVING count(*) <> 4) q;")"
  echo "  сигналов, у которых горизонтов не четыре: ${wrong_h:-0}"
  [ "${wrong_h:-0}" != "0" ] && warn "у ${wrong_h} сигналов набор горизонтов неполон (ожидаются 1, 4, 12, 24)"

  # Неизменность: frozen_at обязан совпадать с моментом сигнала, а не с моментом
  # последнего пересчёта. Расхождение больше часа означает, что строки трогали.
  touched="$(psql_q "SELECT count(*) FROM signal_targets t
                      JOIN signals s ON s.id = t.signal_id
                     WHERE t.frozen_at > s.ts + interval '1 hour';")"
  echo "  строк, замороженных сильно позже своего сигнала: ${touched:-0}"
  if [ "${touched:-0}" != "0" ]; then
    block "строки signal_targets переписывались после выдачи сигнала"
    info  "правило §3 ТЗ: они не обновляются НИКОГДА, иначе проверить систему постфактум невозможно"
  fi
fi

echo
echo "=== 5. Сбои расчёта целей (§6) ==="
if table_exists agent_failures; then
  fails="$(psql_q "SELECT count(*) FROM agent_failures
                    WHERE agent='risk_targets' AND ts > now() - interval '24 hours';")"
  echo "  сбоев risk_targets за сутки: ${fails:-0}"
  if [ "${fails:-0}" != "0" ]; then
    warn "расчёт целей падал ${fails} раз за сутки"
    info  "сигналы при этом выдавались без цели — это заложенное поведение, но причину надо посмотреть:"
    psql_rows "SELECT to_char(ts,'MM-DD HH24:MI'), error_type, coalesce(exc_type,'—')
                 FROM agent_failures WHERE agent='risk_targets'
                  AND ts > now() - interval '24 hours' ORDER BY ts DESC LIMIT 5;" \
      | while IFS='|' read -r ts kind exc; do
          [ -n "$ts" ] && echo "    ${ts} ${kind} ${exc}"
        done
  else
    ok "сбоев расчёта целей за сутки нет"
  fi
else
  unknw "таблицы agent_failures нет — сбои посчитать нечем"
fi

echo
echo "=== 6. Суточный пересчёт: cron и журнал (§7) ==="
cron_line="$(grep -rhE '^\s*40 3 .*src\.risk_main|^\s*40 3 .*run --rm --no-deps risk' \
              /etc/cron.d/ 2>/dev/null | head -1)"
if [ -z "$cron_line" ]; then
  # ВНИМАНИЕ, а не БЛОКИРУЮЩЕЕ: откатывать нечего. Без cron цели просто перестают
  # обновляться — уже выданные остаются верными, новые считаются по вчерашнему
  # окну. Устраняется установкой строки, а не откатом миграции.
  warn "в /etc/cron.d нет задачи пересчёта целей (03:40 UTC) — цели не будут обновляться"
  info  "поставить: sudo cp deploy/agent-trade-risk.cron /etc/cron.d/agent-trade-risk && sudo chmod 644 /etc/cron.d/agent-trade-risk"
else
  ok "cron-задача пересчёта найдена"
  info "$(echo "$cron_line" | sed 's/^[[:space:]]*//')"
fi

if [ ! -f "$RISK_LOG" ]; then
  unknw "журнала ${RISK_LOG} нет — пересчёт из cron ещё ни разу не запускался ЛИБО журнал пишется в другое место"
  info  "это разные вещи: путь задаётся строкой cron, проверьте её"
else
  # grep -c печатает 0 И возвращает код 1, поэтому «|| echo 0» дало бы ДВА нуля
  # в одной переменной и сломало бы сравнение. Код возврата здесь не нужен вовсе.
  done_n="$(grep -c 'risk_targets_recompute_done=1' "$RISK_LOG" 2>/dev/null)"; done_n="${done_n:-0}"
  fail_n="$(grep -c 'risk_targets_recompute_failed=1' "$RISK_LOG" 2>/dev/null)"; fail_n="${fail_n:-0}"
  gapn="$(grep -c 'risk_targets_precheck=1' "$RISK_LOG" 2>/dev/null)"; gapn="${gapn:-0}"
  echo "  записей risk_targets_recompute_done=1: ${done_n}"
  echo "  записей risk_targets_recompute_failed=1: ${fail_n}"
  echo "  записей risk_targets_precheck=1: ${gapn}"
  # Ключи латинские намеренно: журнал хранит кириллицу экранированной, и поиск
  # по русскому слову дал бы ноль на ИСПРАВНОЙ системе (§11 ТЗ).
  if [ "${done_n}" = "0" ] && [ "${fail_n}" = "0" ]; then
    warn "в журнале нет ни одной отметки о пересчёте"
    info  "журнал существует, но пересчёт в нём не отражён: либо он ни разу не отработал, либо пишет в другой файл"
  elif [ "${fail_n}" != "0" ]; then
    warn "пересчёт завершался сбоем ${fail_n} раз (ключ risk_targets_recompute_failed=1)"
  else
    ok "пересчёт отработал ${done_n} раз без сбоев"
  fi
  last_ok="$(grep 'risk_targets_recompute_done=1' "$RISK_LOG" 2>/dev/null | tail -1)"
  [ -n "$last_ok" ] && info "последняя отметка: $(echo "$last_ok" | tr -d '\n' | tail -c 200)"
fi

echo
echo "=== 7. Глубина истории свечей (§1, §2) ==="
bar="$(env_value RISK_BAR 1H)"
need_hours="$(env_value RISK_MIN_RUN_HOURS 2160)"
echo "  bar=${bar}, порог непрерывного ряда: ${need_hours} ч"
rows="$(psql_rows "WITH islands AS (
                     SELECT inst_id, open_time,
                            open_time - (row_number() OVER (PARTITION BY inst_id
                                          ORDER BY open_time)) * interval '1 hour' AS grp
                     FROM backtest.candles WHERE bar='${bar}'),
                   runs AS (SELECT inst_id, count(*) AS n FROM islands GROUP BY inst_id, grp)
                   SELECT c.inst_id, count(*),
                          (SELECT max(n) FROM runs r WHERE r.inst_id=c.inst_id),
                          round(extract(epoch FROM (now()-max(c.open_time)))/3600.0, 1)
                     FROM backtest.candles c WHERE c.bar='${bar}'
                    GROUP BY c.inst_id ORDER BY c.inst_id;")"
if [ -z "$rows" ]; then
  unknw "в backtest.candles нет ни одной свечи с bar=${bar} — либо история не загружена, либо bar другой"
else
  echo "  инструмент | свечей | непрерывный ряд, ч | возраст последней, ч"
  short_n=0
  stale_n=0
  while IFS='|' read -r inst n run age; do
    [ -z "$inst" ] && continue
    printf "    %-10s %8s %20s %20s\n" "$inst" "$n" "$run" "$age"
    awk -v r="${run:-0}" -v need="$need_hours" 'BEGIN{exit !(r < need)}' && short_n=$((short_n+1))
    awk -v a="${age:-999}" 'BEGIN{exit !(a > 3)}' && stale_n=$((stale_n+1))
  done <<< "$rows"
  echo "  инструментов с коротким рядом: ${short_n}; с устаревшей последней свечой: ${stale_n}"
  if [ "$short_n" != "0" ] || [ "$stale_n" != "0" ]; then
    warn "часть инструментов порогов §1 не проходит — по ним цель не выдаётся (причина data_gap)"
    info  "догрузить: docker compose --profile backtest run --rm backtest python -m backtest.loader — или дождаться суточного пересчёта, он догружает свежий край сам"
  else
    ok "все инструменты проходят пороги §1 по глубине и свежести"
  fi
fi

echo
echo "=== 8. Версия логики в выгрузке (§9, критерий H) ==="
export_lv="$(env_value EXPORT_LOGIC_VERSION current)"
echo "  EXPORT_LOGIC_VERSION: ${export_lv}"
lv_rows="$(psql_q "SELECT count(*) FROM logic_version_windows WHERE logic_version > 0;")"
echo "  версий в logic_version_windows: ${lv_rows:-0}"
if [ "${lv_rows:-0}" = "0" ] && [ "$export_lv" = "current" ]; then
  block "EXPORT_LOGIC_VERSION=current, но граница версии в БД не зафиксирована — выгрузка остановится"
  info  "границу пишет Decision Agent при старте; проверьте, что он поднялся на этой версии"
fi
if [ "$export_lv" = "all" ]; then
  warn "выгрузка собирает лист по ВСЕМ версиям логики"
  info  "это допустимо только осознанно: лист будет начинаться оговоркой о смешивании, и сравнивать доли попаданий по нему нельзя"
fi
mixed="$(psql_q "SELECT count(DISTINCT logic_version) FROM signals
                  WHERE status='closed' AND decision <> 'wait';")"
echo "  версий среди закрытых направленных сигналов в БД: ${mixed:-0}"
info "число больше единицы — нормально: в базе живут все версии. Вопрос в том, ОДНА ли версия попадает в лист, а это определяется параметром выше"
zero_v="$(psql_q "SELECT count(*) FROM signals WHERE logic_version = 0;")"
echo "  сигналов с версией 0 («неизвестна»): ${zero_v:-0}"
info "они не попадают в выгрузку ни при каком значении параметра (§9.5)"

echo
echo "=============================================================================="
echo " ИТОГ: блокирующих ${blocking}, требует внимания ${attention}, не проверено ${unknown}"
if [ "$blocking" -gt 0 ]; then
  echo " ДЕЙСТВИЕ: откатить этап (db/migrations/014_risk_targets_rollback.sql) ЛИБО"
  echo "           устранить блокирующие находки выше и повторить проверку."
elif [ "$attention" -gt 0 ] || [ "$unknown" -gt 0 ]; then
  echo " ДЕЙСТВИЕ: откат НЕ требуется. Разобрать находки «ВНИМАНИЕ» и «НЕ ПРОВЕРЕНО»:"
  echo "           первые — известные состояния, вторые — проверки, которые нечем было выполнить."
else
  echo " ДЕЙСТВИЕ: ничего не требуется. Все проверки выполнены, находок нет."
fi
echo "=============================================================================="
exit 0
