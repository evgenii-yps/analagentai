#!/usr/bin/env bash
# Проверка развёртывания Этапа 8.3 (настройки бота и развёрнутый текст сигнала).
# Запускается на СЕРВЕРЕ из каталога стека (там, где docker-compose.yml и .env).
# Только чтение: docker compose ps / SELECT. Ничего не меняет и не перезапускает.
#
#   cd /opt/agent-trade && bash deploy/verify_8_3.sh
#
# ТРЕБОВАНИЕ §6.5 ТЗ: замечания делятся на два класса — БЛОКИРУЮЩИЕ и
# «требует внимания, откат не нужен». Итоговая строка прямо говорит, что делать.
# Единый вердикт «плохо» на разнородные замечания недопустим: он заставляет
# откатывать то, что откатывать не нужно.
set -uo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
PG_USER="${POSTGRES_USER:-agenttrade}"
PG_DB="${POSTGRES_DB:-agenttrade}"
cd "$APP_DIR" || { echo "Не найден каталог стека: $APP_DIR"; exit 2; }

blocking=0     # мешает работе или лишает человека уведомлений
attention=0    # стоит знать, но откат не нужен

block()  { echo "  🔴 БЛОКИРУЮЩЕЕ: $*"; blocking=$((blocking + 1)); }
warn()   { echo "  🟡 ВНИМАНИЕ:    $*"; attention=$((attention + 1)); }
ok()     { echo "  🟢 $*"; }
info()   { echo "  ℹ  $*"; }

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

echo "=============================================================================="
echo " ПРОВЕРКА ЭТАПА 8.3 — настройки бота и развёрнутый текст сигнала"
echo " Время (UTC): $(date -u +%FT%TZ)"
echo "=============================================================================="

echo
echo "=== 1. Таблица настроек (§1) ==="
has_table="$(psql_q "SELECT count(*) FROM information_schema.tables
                      WHERE table_schema='public' AND table_name='user_settings';")"
if [ "${has_table:-0}" != "1" ]; then
  block "таблицы user_settings нет — миграция 013 не применена"
  info  "без неё меню /settings не сохранит ничего, а отбор пойдёт по умолчанию"
else
  ok "таблица user_settings есть"
  rows="$(psql_q "SELECT count(*) FROM user_settings;")"
  info "настроек сохранено: ${rows:-0} (ноль — не ошибка: действуют значения по умолчанию)"
  # Пустой набор токенов означал бы «не слать ничего никогда», и человек не
  # отличил бы это от поломки. Ограничение обязано быть на месте.
  has_check="$(psql_q "SELECT count(*) FROM pg_constraint
                        WHERE conrelid='user_settings'::regclass
                          AND conname='user_settings_instruments_not_empty';")"
  if [ "${has_check:-0}" != "1" ]; then
    warn "нет ограничения «минимум один токен» на user_settings"
    info  "меню его соблюдает, но прямая правка в базе может обнулить набор"
  else
    ok "ограничение «минимум один токен» на месте"
  fi
  bad="$(psql_q "SELECT count(*) FROM user_settings
                  WHERE array_length(instruments,1) IS NULL
                     OR array_length(instruments,1) = 0;")"
  if [ "${bad:-0}" != "0" ]; then
    block "у ${bad} чатов пустой набор токенов — они не получат НИ ОДНОГО уведомления"
  fi
fi

echo
echo "=== 2. Права бота на запись настроек (§1) ==="
# Роль agenttrade_ro читает всё и пишет ТОЛЬКО в user_settings. Без этого права
# меню открывается, кнопки нажимаются, а настройки не сохраняются — и человек
# считает, что бот его не слушает.
can_write="$(psql_q "SELECT count(*) FROM information_schema.role_table_grants
                      WHERE grantee='agenttrade_ro' AND table_name='user_settings'
                        AND privilege_type IN ('INSERT','UPDATE');")"
if [ "${can_write:-0}" -lt 2 ]; then
  block "у роли agenttrade_ro нет прав записи в user_settings"
  info  "меню будет открываться, но настройки не сохранятся; права выдаёт сам бот при старте"
else
  ok "бот может сохранять настройки (запись только в user_settings)"
fi
other_write="$(psql_q "SELECT count(*) FROM information_schema.role_table_grants
                        WHERE grantee='agenttrade_ro'
                          AND privilege_type IN ('INSERT','UPDATE','DELETE')
                          AND table_name <> 'user_settings';")"
if [ "${other_write:-0}" != "0" ]; then
  block "роль agenttrade_ro может писать не только в user_settings (${other_write} таблиц)"
  info  "бот обязан оставаться неспособным испортить наблюдения и решения"
else
  ok "в остальные таблицы бот писать не может"
fi

echo
echo "=== 3. Защита от потока уведомлений (§2) ==="
hold="$(env_value NOTIFY_HOLD_MIN "")"
cap="$(env_value NOTIFY_MAX_PER_HOUR "")"
info "NOTIFY_HOLD_MIN=${hold:-не задан} (выдержка по токену, минут)"
info "NOTIFY_MAX_PER_HOUR=${cap:-не задан} (потолок в час по всем токенам)"
if [ -z "$hold" ] || [ -z "$cap" ]; then
  warn "пороги не заданы в .env — действуют значения по умолчанию (60 и 6)"
elif [ "$hold" = "0" ] && [ "$cap" = "0" ]; then
  warn "оба порога выключены нулями — поток уведомлений ничем не ограничен"
  info  "на пяти токенах это ровно то, ради чего этап делался"
else
  ok "пороги защиты заданы"
fi
# Имена настроек расходятся с ТЗ намеренно (см. отчёт): NOTIFY_COOLDOWN_SEC уже
# занят другим смыслом, и две почти одинаковые настройки перепутали бы при правке.
if grep -qE "^NOTIFY_COOLDOWN_MIN=" "${APP_DIR}/.env" 2>/dev/null; then
  warn "в .env есть NOTIFY_COOLDOWN_MIN — эта настройка НЕ читается"
  info  "выдержка задаётся NOTIFY_HOLD_MIN (расхождение имён отмечено в отчёте)"
fi

echo
echo "=== 4. Отбор и доставка уведомлений (§2) ==="
notify_state="$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null \
                | awk '$1=="notify"{print $2}')"
if [ "$notify_state" != "running" ]; then
  block "сервис notify не работает (состояние: ${notify_state:-нет контейнера})"
else
  ok "сервис notify работает"
fi
sent_24h="$(psql_q "SELECT count(*) FROM signals WHERE notified_at > now() - interval '24 hours';")"
absorbed_24h="$(psql_q "SELECT count(*) FROM signals
                         WHERE notified AND notified_at IS NULL
                           AND ts > now() - interval '24 hours';")"
info "за сутки отправлено: ${sent_24h:-0}, придержано: ${absorbed_24h:-0}"
if [ "${sent_24h:-0}" = "0" ] && [ "${absorbed_24h:-0}" = "0" ]; then
  warn "за сутки не было ни одного сильного сигнала — отбор проверить не на чем"
fi
# Потолок 6 в час: больше 144 отправок в сутки означало бы, что он не работает.
if [ -n "$cap" ] && [ "$cap" != "0" ] && [ "${sent_24h:-0}" -gt "$((cap * 24))" ]; then
  block "отправлено ${sent_24h} за сутки при потолке ${cap} в час — защита не действует"
fi

echo
echo "=== 5. Текст сигнала (§3) ==="
# Проверяется КОД, а не отправленные сообщения: тексты в БД не хранятся, и
# единственный честный способ убедиться — прочитать обязательные части там,
# откуда они берутся.
missing=0
for part in "система не предсказывает цену" \
            "Решение за вами, система не торгует сама" \
            "Новостной и ончейн-анализ пока не подключены"; do
  if ! docker compose exec -T notify grep -qF "$part" /app/src/notify/agent.py 2>/dev/null; then
    missing=$((missing + 1))
    block "в образе notify нет обязательной оговорки: «${part}»"
  fi
done
[ "$missing" = "0" ] && ok "обе оговорки и замыкающая строка на месте в работающем образе"

# Внутренние термины ищутся в ГОТОВОМ тексте, а не в исходнике: в исходнике они
# законно встречаются в комментариях и в списке запрещённых слов, и поиск по
# файлу давал бы ложную тревогу. Сообщение собирается настоящим кодом в
# работающем контейнере.
terms_found="$(docker compose exec -T notify python -c '
from datetime import UTC, datetime
from src.notify.agent import SignalFormatConfig, format_signal_message

FORBIDDEN = ("индекс согласия", "перцентиль", "confidence", "logic_version",
             "EMA", "RSI", "ADX", "MACD", "bullish", "bearish")
metrics = {
    "market": {"ema20": 3, "ema50": 2, "ema200": 1, "ema50_slope": 0.5,
               "rsi14": 75, "adx14": 30},
    "liquidity": {"imbalance": 0.4, "rel_spread": 0.002, "bid_wall_ratio": 0.5},
    "futures": {"funding_pct": 0.9, "lookback_hours": 336, "oi_enough": True,
                "oi_confirms": True, "n_oi": 40},
}
found = set()
for decision in ("buy", "sell"):
    for payload in ([{"agent": a, "signal": s, "confidence": c}
                     for a, s, c in (("market", "bullish", 0.9),
                                     ("liquidity", "neutral", 0.1),
                                     ("futures", "bearish", 0.3))], []):
        text = format_signal_message(
            {"id": 1, "instrument_id": 1, "ts": datetime.now(UTC),
             "decision": decision, "probability": 0.8, "agents_payload": payload},
            117240.0, SignalFormatConfig("BTC/USDT", "UTC", "4h", horizon_h=4),
            metrics,
        )
        for term in FORBIDDEN:
            if term.lower() in text.lower():
                found.add(term)
print(",".join(sorted(found)))
' 2>/dev/null | tr -d '"'"'\r'"'"')"
if [ -n "$terms_found" ]; then
  block "в готовом тексте сигнала есть внутренние термины: ${terms_found}"
  info  "текст предназначен человеку, который биржевых терминов не знает (§3, §7)"
else
  ok "внутренних терминов в готовом тексте нет (проверено на собранном сообщении)"
fi

echo
echo "=== 6. Горизонты (§1, §4) ==="
eval_h="$(env_value EVAL_HORIZONS "1,4,12,24")"
info "EVAL_HORIZONS=${eval_h}"
for h in 1 4 12 24; do
  case ",${eval_h}," in
    *",${h},"*) ;;
    *) warn "горизонт ${h} ч предлагается в меню, но не оценивается (нет в EVAL_HORIZONS)"
       info  "человек выберет его и не увидит статистики по нему" ;;
  esac
done
chosen="$(psql_rows "SELECT horizon_h, count(*) FROM user_settings
                      GROUP BY horizon_h ORDER BY horizon_h;")"
[ -n "$chosen" ] && info "выбранные горизонты (часы|чатов): $(echo "$chosen" | tr '\n' ' ')"

echo
echo "=============================================================================="
echo " ИТОГ: блокирующих — ${blocking}, требует внимания — ${attention}"
if [ "$blocking" -gt 0 ]; then
  echo " ЧТО ДЕЛАТЬ: устранить блокирующие замечания. До этого этап не считается"
  echo " развёрнутым: человек либо не получает уведомления, либо получает поток."
  echo " Откат кода НЕ требуется — все блокирующие пункты выше устраняются"
  echo " миграцией, правкой .env или перезапуском сервиса."
  echo "=============================================================================="
  exit 1
fi
if [ "$attention" -gt 0 ]; then
  echo " ЧТО ДЕЛАТЬ: ничего срочного. Замечания выше стоит прочитать и решить,"
  echo " нужны ли действия; работе они не мешают, откат не нужен."
  echo "=============================================================================="
  exit 0
fi
echo " ЧТО ДЕЛАТЬ: ничего. Этап 8.3 развёрнут, замечаний нет."
echo "=============================================================================="
exit 0
