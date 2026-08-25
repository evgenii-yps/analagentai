#!/usr/bin/env bash
# Снятие повторных объявлений в .env и файлах cron (Этап 8.7 §5.1).
#
# Зачем отдельный скрипт. Дубль строки — уже четвёртый случай за проект (блок
# 7.3 в .env, пара EVAL_HORIZONS, комментарий о калибровочной кривой в
# /etc/cron.d/agent-trade). Причину чинит установщик (deploy/install.sh теперь
# ставит ключи через env_upsert и cron_install), но на УЖЕ РАБОТАЮЩЕМ сервере
# установщик заново не гоняют. Этот скрипт убирает накопленное, ничего больше
# не трогая.
#
# ПРАВИЛО ВЫБОРА. Для строк «КЛЮЧ=значение» остаётся ПОСЛЕДНЕЕ объявление:
# именно оно действует — и docker compose env_file, и cron читают файл сверху
# вниз. Поэтому снятие повторов НЕ МЕНЯЕТ действующую конфигурацию: оно убирает
# мёртвые строки. Для одинаковых строк без ключа (комментарии, задачи cron)
# остаётся ПЕРВАЯ, чтобы комментарий остался перед своей задачей.
#
# Запуск:
#   bash deploy/dedupe_config.sh            # только показать, что будет сделано
#   bash deploy/dedupe_config.sh --apply    # применить (нужен root для cron)
#
# Перед --apply скрипт сам кладёт копию каждого изменяемого файла рядом с ним
# (суффикс .bak-8.7). Бэкап БД к этой правке отношения не имеет, но по §9 ТЗ он
# делается до любого развёртывания: scripts/backup_db.sh.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/agent-trade}"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

FILES=("${APP_DIR}/.env"
       /etc/cron.d/agent-trade
       /etc/cron.d/agent-trade-export
       /etc/cron.d/agent-trade-risk)

# Нормализованное содержимое файла — в stdout. Тот же алгоритм, что в
# install.sh (normalize_declarations): второй реализации правила быть не должно,
# поэтому текст функции здесь и там совпадает дословно.
declarations_dedupe() {  # $1 = файл
  awk '
    function keyof(line,   k) {
        if (line !~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/) return ""
        k = line; sub(/=.*$/, "", k); gsub(/[[:space:]]/, "", k); return k
    }
    NR == FNR {
        k = keyof($0)
        if (k != "")                  last_key[k]  = FNR
        else if ($0 ~ /[^[:space:]]/) { if (!($0 in first_line)) first_line[$0] = FNR }
        next
    }
    {
        k = keyof($0)
        if (k != "")                  { if (FNR == last_key[k])   print; next }
        if ($0 !~ /[^[:space:]]/)     { print; next }
        if (FNR == first_line[$0])    print
    }
  ' "$1" "$1"
}

declarations_duplicates() {  # $1 = файл
  [[ -f "$1" ]] || return 0
  awk -v f="$1" '
    /[^[:space:]]/ {
        if ($0 ~ /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/) {
            k = $0; sub(/=.*$/, "", k); gsub(/[[:space:]]/, "", k); label = "ключ " k
        } else if ($0 ~ /^[[:space:]]*#/) { label = "комментарий: " $0
        } else                            { label = "строка: " $0 }
        n[label]++
    }
    END { for (l in n) if (n[l] > 1) printf "  %s | повторов: %d\n", l, n[l] }
  ' "$1"
}

echo "=============================================================================="
echo " СНЯТИЕ ПОВТОРОВ В КОНФИГУРАЦИИ (Этап 8.7 §5.1)"
echo " Режим: $([[ "${APPLY}" -eq 1 ]] && echo 'ПРИМЕНИТЬ' || echo 'только показать (--apply для правки)')"
echo "=============================================================================="

total=0
changed=0
for f in "${FILES[@]}"; do
  echo
  echo "── ${f}"
  if [[ ! -f "$f" ]]; then
    echo "  файла нет — пропущен (это не «повторов нет», а «проверять нечего»)"
    continue
  fi
  dup="$(declarations_duplicates "$f")"
  n="$(printf '%s' "$dup" | grep -c . || true)"
  echo "  повторов: ${n}"
  [[ "$n" -eq 0 ]] && continue
  printf '%s\n' "$dup"
  total=$((total + n))

  tmp="$(mktemp)"
  declarations_dedupe "$f" > "$tmp"
  echo "  строк было: $(wc -l < "$f"), станет: $(wc -l < "$tmp")"
  echo "  что будет удалено:"
  diff "$f" "$tmp" | grep '^<' | sed 's/^/    /' || true

  if [[ "${APPLY}" -eq 1 ]]; then
    cp -p "$f" "${f}.bak-8.7"
    # Содержимое перезаписывается в существующий файл: права и владелец
    # сохраняются (важно для .env с правами 600).
    cat "$tmp" > "$f"
    echo "  ПРИМЕНЕНО. Копия до правки: ${f}.bak-8.7"
    changed=$((changed + 1))
  fi
  rm -f "$tmp"
done

echo
echo "=============================================================================="
if [[ "$total" -eq 0 ]]; then
  echo " ИТОГ: повторов не найдено. Делать нечего."
  exit 0
fi
if [[ "${APPLY}" -eq 1 ]]; then
  echo " ИТОГ: снято повторов ${total} в ${changed} файле(ах)."
  echo " ДЕЙСТВИЕ: перечитайте конфигурацию — docker compose up -d --remove-orphans."
  echo " Cron перечитывает /etc/cron.d сам, перезапуск не нужен."
  exit 0
fi
echo " ИТОГ: найдено повторов ${total}, НИЧЕГО НЕ ИЗМЕНЕНО."
echo " ДЕЙСТВИЕ: повторите с флагом --apply, чтобы снять их."
exit 1
