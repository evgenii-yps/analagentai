#!/usr/bin/env bash
# Проверка Этапа 8.6 — заперты ли голоса ema_stack и ema_slope.
#
# ТОЛЬКО ЧТЕНИЕ. Ни INSERT/UPDATE/DELETE, ни DDL, ни перезапуска контейнеров,
# ни строки в конфигурации. Разовые контейнеры поднимаются с --rm.
#
# Запуск на сервере ОДНОЙ командой:
#   sudo -u agent bash /opt/agent-trade/deploy/verify_8_6.sh
#
# Скрипт вызывается КАК ФАЙЛ ИЗ РЕПОЗИТОРИЯ и не зависит от попадания в образ:
# SQL уходит в psql через stdin, Python — в python - через stdin.
#
# Что он решает. Замер 24.08 показал 100% значения +1 у обоих голосов на живых
# данных. Отсюда НЕ следует, что голоса заперты: агент запускается раз в минуту,
# а свечи часовые, поэтому трое суток дают 72 состояния рынка, а не 11 590
# испытаний, и все пять токенов за этот период росли. Настоящую проверку даёт
# ДЛИННАЯ реальная история с падением — она лежит в backtest.candles.
#
# Три требования к самому скрипту (нарушены в предыдущих, здесь соблюдены):
#   * вывод не противоречит собственным данным — все итоги считаются из тех же
#     чисел, что напечатаны выше;
#   * счётчики печатают ЧИСЛА (psql -t -A, без строки «(N rows)»);
#   * журнал разбирается как JSON — кириллица в нём экранирована.

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
DB_USER="${POSTGRES_USER:-agenttrade}"
DB_NAME="${POSTGRES_DB:-agenttrade}"
# Период выбран так, чтобы заведомо содержать устойчивое падение: весна-лето
# 2022 года, обвал крипторынка. Границы можно переопределить переменными.
BT_FROM="${BT_FROM:-2022-04-01}"
BT_TO="${BT_TO:-2022-07-01}"
DAYS="${DAYS:-3}"

cd "${APP_DIR}" || { echo "Нет каталога ${APP_DIR}"; exit 2; }

blocking=0
attention=0
note_block() { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
note_warn()  { echo "  🟡 ТРЕБУЕТ ВНИМАНИЯ: $*"; attention=$((attention + 1)); }
note_ok()    { echo "  🟢 $*"; }

# -t -A: только значения, без заголовка и без строки «(N rows)» — счётчик
# обязан печатать число, а не «(1 row)».
psql_val() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -t -A -q -c "$1" 2>&1 | tr -d '[:space:]'
}
psql_tbl() {
  docker compose exec -T postgres \
    psql -U "${DB_USER}" -d "${DB_NAME}" -X -A -F "|" -q -c "$1" 2>&1
}

echo "=============================================================================="
echo " ПРОВЕРКА 8.6 — заперты ли EMA-голоса Market Agent"
echo " Момент запуска (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo " Исторический период контроля: ${BT_FROM} .. ${BT_TO}"
echo "=============================================================================="

echo
echo "── 1. Есть ли историческая свеча в backtest.candles ───────────────────────"
BT_ROWS=$(psql_val "SELECT count(*) FROM backtest.candles WHERE bar='1H' AND open_time >= DATE '${BT_FROM}' AND open_time < DATE '${BT_TO}';")
echo "    часовых свечей в периоде: ${BT_ROWS}"
psql_tbl "SELECT inst_id, count(*) AS candles, min(open_time) AS ts_min, max(open_time) AS ts_max, round((100.0*(last_close-first_close)/nullif(first_close,0))::numeric, 2) AS move_pct FROM backtest.candles c JOIN LATERAL (SELECT (SELECT close FROM backtest.candles a WHERE a.inst_id=c.inst_id AND a.bar='1H' AND a.open_time >= DATE '${BT_FROM}' ORDER BY a.open_time ASC LIMIT 1) AS first_close, (SELECT close FROM backtest.candles b WHERE b.inst_id=c.inst_id AND b.bar='1H' AND b.open_time < DATE '${BT_TO}' ORDER BY b.open_time DESC LIMIT 1) AS last_close) p ON TRUE WHERE c.bar='1H' AND c.open_time >= DATE '${BT_FROM}' AND c.open_time < DATE '${BT_TO}' GROUP BY inst_id, first_close, last_close ORDER BY inst_id;" | sed 's/^/    /'

if [[ ! "${BT_ROWS}" =~ ^[0-9]+$ ]] || [[ "${BT_ROWS}" -lt 500 ]]; then
  note_block "истории в backtest.candles за период недостаточно — контроль невозможен"
  note_block "загрузите историю или задайте другой период через BT_FROM/BT_TO"
else
  note_ok "истории достаточно для контроля (${BT_ROWS} часовых свечей)"
fi

echo
echo "── 2. КОНТРОЛЬ: распределение обоих голосов на реальной истории ───────────"
echo "    (голоса считаются ШТАТНЫМ кодом src.agents.market скользящим окном 250"
echo "     свечей — ровно так, как это делает MarketAgent.analyze)"
docker compose run --rm --no-deps -T agents python - <<PYEOF 2>&1 | sed 's/^/    /'
import asyncio
import asyncpg, pandas as pd
from src.agents.market import _EMA_FAST, _EMA_MID, _EMA_SLOW, _SLOPE_LOOKBACK, ema
from src.core.config import settings

WINDOW = max(settings.AGENT_MIN_CANDLES, _EMA_SLOW) + 50

async def main():
    conn = await asyncpg.connect(dsn=settings.pg_dsn)
    insts = [r["inst_id"] for r in await conn.fetch(
        "SELECT DISTINCT inst_id FROM backtest.candles WHERE bar='1H' ORDER BY 1")]
    print("инструмент | часов | stack:+1 / 0 / -1 | slope:+1 / 0 / -1 | score<-1")
    for inst in insts:
        rows = await conn.fetch(
            "SELECT close FROM backtest.candles WHERE inst_id=\$1 AND bar='1H'"
            " AND open_time >= DATE '${BT_FROM}' AND open_time < DATE '${BT_TO}'"
            " ORDER BY open_time ASC", inst)
        close = [float(r["close"]) for r in rows]
        if len(close) < WINDOW + 1:
            print(f"{inst:<10} | свечей {len(close)} < {WINDOW + 1} — период короток")
            continue
        st = {1: 0, 0: 0, -1: 0}
        sl = {1: 0, 0: 0, -1: 0}
        for end in range(WINDOW, len(close) + 1):
            w = pd.Series(close[end - WINDOW:end])
            f = ema(w, _EMA_FAST).iloc[-1]
            m = ema(w, _EMA_MID)
            s = ema(w, _EMA_SLOW).iloc[-1]
            last, prev = m.iloc[-1], m.iloc[-1 - _SLOPE_LOOKBACK]
            st[1 if f > last > s else (-1 if f < last < s else 0)] += 1
            d = last - prev
            sl[1 if d > 0 else (-1 if d < 0 else 0)] += 1
        n = sum(st.values())
        print(f"{inst:<10} | {n:>5} | {st[1]:>5} / {st[0]:>4} / {st[-1]:>5}"
              f" | {sl[1]:>5} / {sl[0]:>4} / {sl[-1]:>5}"
              f" | достижим: {'да' if st[-1] and sl[-1] else 'НЕТ'}")
    await conn.close()

asyncio.run(main())
PYEOF

echo
echo "── 3. Живые данные: голоса по токенам за последние ${DAYS} сут ─────────────"
psql_tbl "SELECT i.base AS token, v.key AS vote, count(*) FILTER (WHERE (v.value#>>'{}')::int = 1) AS plus, count(*) FILTER (WHERE (v.value#>>'{}')::int = 0) AS zero, count(*) FILTER (WHERE (v.value#>>'{}')::int = -1) AS minus FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id CROSS JOIN LATERAL jsonb_each(a.metrics->'votes') AS v(key, value) WHERE a.agent='market' AND a.metrics ? 'votes' AND v.key IN ('ema_stack','ema_slope') AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base, v.key ORDER BY i.base, v.key;" | sed 's/^/    /'

echo
echo "── 4. Сколько РАЗЛИЧНЫХ часовых состояний стоит за этими наблюдениями ─────"
echo "    (агент считает раз в минуту, свеча часовая: делить наблюдения на 60)"
psql_tbl "SELECT i.base AS token, count(*) AS observations, count(DISTINCT date_trunc('hour', a.ts)) AS distinct_hours FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='market' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

echo
echo "── 5. Сумма голосов и движение цены рядом ─────────────────────────────────"
psql_tbl "SELECT i.base AS token, min((a.metrics->>'score')::int) AS score_min, max((a.metrics->>'score')::int) AS score_max, count(*) FILTER (WHERE (a.metrics->>'score')::int < -1) AS below_minus_one FROM agent_outputs a JOIN instruments i ON i.id=a.instrument_id WHERE a.agent='market' AND a.metrics ? 'score' AND a.ts >= now() - make_interval(days => ${DAYS}) GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'
psql_tbl "SELECT i.base AS token, round((100.0*(max(p.last_close)-min(p.first_close))/nullif(min(p.first_close),0))::numeric, 2) AS move_pct_window, round((100.0*(max(p.last_close)-min(p.win_close))/nullif(min(p.win_close),0))::numeric, 2) AS move_pct_250h FROM instruments i JOIN LATERAL (SELECT (SELECT close FROM ohlcv o1 WHERE o1.instrument_id=i.id AND o1.timeframe='1h' AND o1.ts >= now() - make_interval(days => ${DAYS}) ORDER BY o1.ts ASC LIMIT 1) AS first_close, (SELECT close FROM ohlcv o3 WHERE o3.instrument_id=i.id AND o3.timeframe='1h' AND o3.ts >= now() - interval '250 hours' ORDER BY o3.ts ASC LIMIT 1) AS win_close, (SELECT close FROM ohlcv o2 WHERE o2.instrument_id=i.id AND o2.timeframe='1h' ORDER BY o2.ts DESC LIMIT 1) AS last_close) p ON TRUE WHERE i.type='spot' GROUP BY i.base ORDER BY i.base;" | sed 's/^/    /'

BELOW=$(psql_val "SELECT count(*) FROM agent_outputs a WHERE a.agent='market' AND a.metrics ? 'score' AND (a.metrics->>'score')::int < -1 AND a.ts >= now() - make_interval(days => ${DAYS});")
echo "    выводов с суммой голосов ниже -1 за окно: ${BELOW}"

echo
echo "── 6. Граница версий логики ───────────────────────────────────────────────"
psql_tbl "SELECT logic_version, started_at, note FROM logic_version_windows ORDER BY logic_version;" | sed 's/^/    /'
V6=$(psql_val "SELECT count(*) FROM logic_version_windows WHERE logic_version = 6;")
echo "    строк версии 6: ${V6}"
if [[ "${V6}" =~ ^[0-9]+$ ]] && [[ "${V6}" -gt 0 ]]; then
  note_warn "граница версии 6 внесена — значит правка голосов была развёрнута"
else
  note_ok "границы версии 6 нет: правка голосов не разворачивалась (см. отчёт 8.6)"
fi

echo
echo "=============================================================================="
echo " ИТОГ"
echo "=============================================================================="
echo " Блокирующих находок: ${blocking}"
echo " Находок, требующих внимания без отката: ${attention}"
echo
echo " Решает раздел 2, и только он: на реальной истории с падением оба голоса"
echo " обязаны принимать все три значения."
echo " ДЕЙСТВИЕ: если в разделе 2 в столбцах stack:-1 и slope:-1 стоят НЕнулевые"
echo " числа — голоса исправны, правка не нужна, перекос объясняется рынком"
echo " периода замера (сверить с разделами 4 и 5). Если там нули — голоса"
echo " действительно заперты, и отчёт 8.6 подлежит пересмотру: передайте вывод"
echo " раздела 2 исполнителю."
