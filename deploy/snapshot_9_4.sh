#!/usr/bin/env bash
# СЛЕПОК ДО / ПОСЛЕ РАЗВЁРТЫВАНИЯ ЭТАПА 9.4 (критерий 10.13 и «итог по диску»).
#
#   sudo -u agent bash /opt/agent-trade/deploy/snapshot_9_4.sh before   # ПЕРВОЙ командой
#   sudo -u agent bash /opt/agent-trade/deploy/snapshot_9_4.sh after    # после развёртывания
#
# Этап не меняет ни одного значения, влияющего на сигналы. Слепок это доказывает:
#   * LOGIC_VERSION и отпечаток файлов агентов/решающего слоя (тот же состав
#     файлов, что в snapshot_9_3.sh) — до и после совпадают;
#   * число открытых виртуальных позиций и контрольная сумма ПАРАМЕТРОВ позиций
#     (не исходов): параметры позиции после открытия не меняются. В сумму входят
#     только позиции, открытые не позже момента слепка «до», — новые позиции
#     после него законно появляются;
#   * занято/свободно на диске и объём docker (для отчёта «до / после»).
#
# «after» дополнительно сверяет строки с файлом «before» и печатает
# OK / РАСХОЖДЕНИЕ по каждому критерию. ТОЛЬКО ЧТЕНИЕ.
set -uo pipefail

MODE="${1:-}"
[[ "$MODE" == "before" || "$MODE" == "after" ]] || { echo "Использование: $0 before|after" >&2; exit 2; }

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
OUT_DIR="${APP_DIR}/analysis_out"
OUT="${OUT_DIR}/snapshot_9_4_${MODE}.txt"
BEFORE="${OUT_DIR}/snapshot_9_4_before.txt"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }
mkdir -p "${OUT_DIR}" || { echo "Не создать ${OUT_DIR}"; exit 2; }

psql_q() {
  docker compose exec -T postgres psql -U "${DB_USER}" -d "${DB_NAME}" -tA -c "$1" 2>/dev/null || true
}
say() { echo "$*" | tee -a "${OUT}"; }
val() { local v; v="$(grep -E "^$1=" "$2" 2>/dev/null | head -n 1 | cut -d= -f2-)"; echo "${v:-НЕТ}"; }

: > "${OUT}"
say "# Слепок ${MODE} этапа 9.4"
TAKEN="$(date -u +%FT%TZ)"
say "snapshot_taken_at=${TAKEN}"
say "git_head=$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo неизвестно)"

# --- LOGIC_VERSION: из работающего образа (то, что реально исполняется) -------
lv="$(docker compose exec -T decision python -c 'from src.core.config import settings; print(settings.LOGIC_VERSION)' 2>/dev/null | tr -d '\r' || true)"
say "logic_version=${lv:-НЕ_СНЯТО}"

# --- Отпечаток агентов и решающего слоя (состав как в snapshot_9_3.sh) --------
FILES="src/agents/base.py src/agents/market.py src/agents/liquidity.py
src/agents/futures.py src/agents/runner.py src/decision/agent.py
src/decision/runner.py"
fp="$(cat ${FILES} 2>/dev/null | sha256sum | cut -d' ' -f1)"
say "agents_fingerprint=${fp:-НЕ_СНЯТО}"

# --- Позиции ------------------------------------------------------------------
say "open_virtual_positions=$(psql_q "SELECT count(*) FROM positions WHERE status='open' AND is_virtual;")"
if [[ "$MODE" == "after" && -f "$BEFORE" ]]; then
  BOUND="$(val snapshot_taken_at "$BEFORE")"
else
  BOUND="$TAKEN"
fi
say "params_bound_opened_at=${BOUND}"
say "positions_params_digest=$(psql_q "
  SELECT md5(string_agg(line, E'\n' ORDER BY line)) FROM (
    SELECT id || '|' || instrument_id || '|' || signal_id || '|' || logic_version || '|' ||
           horizon_h || '|' || opened_at || '|' || entry_price || '|' || qty || '|' ||
           notional_usd || '|' || target_pct || '|' || target_price || '|' || stop_pct || '|' ||
           stop_price || '|' || cost_pct || '|' || deadline_at AS line
    FROM positions WHERE opened_at <= '${BOUND}'::timestamptz
  ) t;")"

# --- Диск и docker ------------------------------------------------------------
read -r _ _ used avail usepct _ < <(df -P -B1 / | tail -n 1)
say "disk_used_gb=$(awk -v u="$used" 'BEGIN {printf "%.2f", u/1073741824}')"
say "disk_free_gb=$(awk -v a="$avail" 'BEGIN {printf "%.2f", a/1073741824}')"
say "disk_use_pct=${usepct}"
say "docker_system_df=$(docker system df --format '{{.Type}}={{.Size}}' 2>/dev/null | tr '\n' ';')"
say "backups_dir_bytes=$(du -sb "${APP_DIR}/backups" 2>/dev/null | cut -f1)"
say "db_size=$(psql_q "SELECT pg_size_pretty(pg_database_size(current_database()));")"

if [[ "$MODE" == "after" ]]; then
  [[ -f "$BEFORE" ]] || { echo; echo "Файла «before» нет — сверять не с чем."; exit 1; }
  echo; echo "--- Сверка с «before» ---"
  bad=0
  for k in logic_version agents_fingerprint positions_params_digest; do
    b="$(val "$k" "$BEFORE")"; a="$(val "$k" "$OUT")"
    if [[ "$b" == "$a" && "$a" != "НЕ_СНЯТО" && "$a" != "НЕТ" && -n "$a" ]]; then
      echo "OK           $k: $a"
    else
      echo "РАСХОЖДЕНИЕ  $k: до=$b после=$a"; bad=1
    fi
  done
  echo "справочно (может меняться законно): open_virtual_positions до=$(val open_virtual_positions "$BEFORE") после=$(val open_virtual_positions "$OUT")"
  echo "диск: занято до=$(val disk_used_gb "$BEFORE") ГБ, после=$(val disk_used_gb "$OUT") ГБ; свободно до=$(val disk_free_gb "$BEFORE") ГБ, после=$(val disk_free_gb "$OUT") ГБ"
  exit "$bad"
fi
echo; echo "Слепок записан в ${OUT}. Храните его до конца этапа."
