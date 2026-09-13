#!/usr/bin/env bash
# СЛЕПОК ДО РАЗВЁРТЫВАНИЯ (§15.1, критерии §13.1.2, §13.1.3, §13.1.6).
#
# ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ ПУНКТ В ПРОВЕРКЕ. Три критерия приёмки
# сравнивают состояние ПОСЛЕ развёртывания с состоянием ДО, и «до» снять
# задним числом нельзя ни при каких условиях. Скрипт запускается ПЕРВОЙ
# командой порядка развёртывания, до слияния ветки, и кладёт числа в файл,
# который потом читает verify_9_3.sh.
#
# ШАГ 0 — ДОСТАВКА САМОГО СКРИПТА. Здесь курица и яйцо: §15.1 велит снять
# слепок ДО слияния ветки, а сам скрипт лежит В ЭТОЙ ВЕТКЕ, то есть на сервере
# его ещё нет. Рабочий склад в `main` при этом трогать нельзя — слияние и есть
# то изменение, которое слепок обязан опередить.
#
# Поэтому файл достаётся из ветки БЕЗ переключения рабочего дерева:
#
#   cd /opt/agent-trade
#   sudo -u agent git fetch origin claude/bold-faraday-im8uy8
#   sudo -u agent git show origin/claude/bold-faraday-im8uy8:deploy/snapshot_9_3.sh \
#       > /tmp/snapshot_9_3.sh
#   sudo -u agent bash /tmp/snapshot_9_3.sh
#
# ``git show <ветка>:<файл>`` печатает содержимое файла из ветки в стандартный
# вывод и НЕ ТРОГАЕТ ни рабочее дерево, ни индекс, ни текущую ветку: после него
# `git status` так же чист, как до. Ни `checkout`, ни `merge`, ни `stash` для
# этого не нужны, и делать их нельзя — любой из них меняет то, что слепок
# измеряет.
#
# ЗАПУСК ИЗ /tmp — НЕ НЕБРЕЖНОСТЬ. Класть скрипт в /opt/agent-trade/deploy
# значило бы внести в рабочий склад файл, которого нет ни в одной ветке: он
# пережил бы слияние неотслеживаемым и однажды разошёлся бы с тем, что в
# репозитории. Слепок пишется всё равно в ``analysis_out`` рабочего каталога.
#
# ПОСЛЕ СЛИЯНИЯ скрипт оказывается на своём месте, и вся эта оговорка перестаёт
# быть нужной:
#   sudo -u agent bash /opt/agent-trade/deploy/snapshot_9_3.sh
#
# ТОЛЬКО ЧТЕНИЕ: ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров.
#
# ЧТО СНИМАЕТСЯ:
#   1. Контрольная сумма строк версий 5 и 6 по столбцам, названным §13.1.2
#      (id, token, opened_at, closed_at, pnl_pct). Её задача — поймать
#      ПЕРЕСЧЁТ прошлого, а не рост таблицы: новые строки версии 7 в сумму не
#      входят по построению (§14.1: «не предполагать, что продакшн молчит»).
#   2. Слепок целей risk_targets / signal_targets (§13.1.6).
#   3. Отпечаток файлов агентов и решающего слоя (§13.1.3).
#
# ФЛАГИ ОБОЛОЧКИ. ``set -uo pipefail`` без ``-e`` намеренно: неудача одного
# пункта не должна отменять остальные — слепок, снятый наполовину, хуже
# бесполезен, потому что выглядит снятым. Каждая подстановка имеет запасной
# путь и печатает «не снято» явно (§14.5).
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
OUT_DIR="${APP_DIR}/analysis_out"
OUT="${OUT_DIR}/snapshot_9_3_before.txt"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }
mkdir -p "${OUT_DIR}" || { echo "Не создать ${OUT_DIR}"; exit 2; }

psql_q() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}

say() { echo "$*" | tee -a "${OUT}"; }

: > "${OUT}"
say "# Слепок ДО развёртывания этапа 9.3"
say "snapshot_taken_at=$(date -u +%FT%TZ)"
say "snapshot_git_head=$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo неизвестно)"

# --- 1. Строки версий 5 и 6 (§13.1.2) ---------------------------------------
#
# СУММА СЧИТАЕТСЯ ПО УПОРЯДОЧЕННОМУ ПЕРЕЧНЮ СТРОК, а не по агрегату: две
# разные выборки легко дают одну сумму, а вот одинаковый md5 от
# упорядоченного перечня — уже совпадение строк.
for version in 5 6; do
  digest="$(psql_q "
    SELECT md5(string_agg(line, E'\n' ORDER BY line))
    FROM (
      SELECT p.id || '|' || i.symbol || '|' || p.opened_at || '|' ||
             COALESCE(p.closed_at::text, '') || '|' ||
             COALESCE(p.net_pnl_pct::text, '') AS line
      FROM positions p JOIN instruments i ON i.id = p.instrument_id
      WHERE p.logic_version = ${version}
    ) rows;")"
  count="$(psql_q "SELECT count(*) FROM positions WHERE logic_version = ${version};")"
  if [ -z "${digest}" ]; then
    say "v${version}_digest=НЕ_СНЯТО"
  else
    say "v${version}_digest=${digest}"
  fi
  say "v${version}_rows=${count:-НЕ_СНЯТО}"
  pnl="$(psql_q "
    SELECT COALESCE(round(sum(net_pnl_usd), 5), 0) FROM positions
    WHERE logic_version = ${version} AND status = 'closed'
      AND exit_reason <> 'data_gap';")"
  say "v${version}_pnl_usd=${pnl:-НЕ_СНЯТО}"
done

# --- 2. Слепок целей (§13.1.6) ----------------------------------------------
#
# ЦЕЛИ ПЕРЕСЧИТЫВАЮТСЯ НОЧЬЮ В 03:40 UTC, и §13.1.6 требует, чтобы первый же
# пересчёт ПОСЛЕ развёртывания дал побайтно совпадающий результат: этап не
# трогает расчёт целей ни одной строкой.
#
# СУММА СЧИТАЕТСЯ ТОЛЬКО ПО СТРОКАМ, СУЩЕСТВОВАВШИМ НА МОМЕНТ СЛЕПКА, и это
# исправление дефекта, найденного на боевом сервере 13.09.2026: первая
# редакция считала сумму по ВСЕЙ таблице, а ``signal_targets`` растёт с каждым
# новым сигналом (``frozen_at DEFAULT now()``). Два прогона с разницей в две
# минуты давали разные суммы — при том, что ночного пересчёта между ними не
# было и ни одна старая строка не менялась. Проверка сообщала о нарушении
# границы там, где §14.1 прямо велит считать рост продакшн-таблиц НОРМОЙ.
#
# ГРАНИЦА БЕРЁТСЯ С ЧАСОВ БАЗЫ, А НЕ ХОСТА. Расхождение часов контейнера и
# хоста на секунду сдвинуло бы выборку — и сумма «до» оказалась бы посчитана
# по другому множеству строк, чем сумма «после», при полной их неизменности.
BOUNDARY="$(psql_q "SELECT now();")"
if [ -z "${BOUNDARY}" ]; then
  say "targets_boundary=НЕ_СНЯТО"
else
  say "targets_boundary=${BOUNDARY}"
fi

# У каждой таблицы СВОЙ столбец времени появления строки, и подставлять один
# вместо другого нельзя: ``computed_at`` — когда цель посчитана ночным
# прогоном, ``frozen_at`` — когда цель приморожена к сигналу.
for pair in "risk_targets:computed_at" "signal_targets:frozen_at"; do
  table="${pair%%:*}"; column="${pair##*:}"
  exists="$(psql_q "SELECT to_regclass('${table}') IS NOT NULL;")"
  if [ "${exists}" != "t" ]; then
    say "${table}_digest=НЕТ_ТАБЛИЦЫ"
    continue
  fi
  if [ -z "${BOUNDARY}" ]; then
    say "${table}_digest=НЕ_СНЯТО"
    continue
  fi
  digest="$(psql_q "
    SELECT md5(string_agg(t::text, E'\n' ORDER BY t::text))
    FROM ${table} t WHERE t.${column} <= '${BOUNDARY}'::timestamptz;")"
  say "${table}_digest=${digest:-НЕ_СНЯТО}"
  say "${table}_rows=$(psql_q "
    SELECT count(*) FROM ${table} t
    WHERE t.${column} <= '${BOUNDARY}'::timestamptz;")"
done

# --- 3. Отпечаток агентов и решающего слоя (§13.1.3) ------------------------
#
# СОСТАВ ФАЙЛОВ ЗАКРЫТ И НАЗВАН ПОИМЁННО. Маска ``src/agents/*.py`` считала бы
# отпечаток и от файла, добавленного вместе с нарушением границы, — то есть
# нарушение вошло бы в эталон.
FILES="src/agents/base.py src/agents/market.py src/agents/liquidity.py
src/agents/futures.py src/agents/runner.py src/decision/agent.py
src/decision/runner.py"
fingerprint="$(cd "${APP_DIR}" && cat ${FILES} 2>/dev/null | sha256sum | cut -d' ' -f1)"
if [ -z "${fingerprint}" ]; then
  say "agents_fingerprint=НЕ_СНЯТО"
else
  say "agents_fingerprint=${fingerprint}"
fi

echo
echo "Слепок записан в ${OUT}."
echo "ХРАНИТЕ ЕГО ДО КОНЦА ЭТАПА: без него критерии §13.1.2, §13.1.3 и §13.1.6"
echo "закрыть будет НЕЧЕМ, а снять слепок задним числом нельзя."
