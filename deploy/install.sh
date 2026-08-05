#!/usr/bin/env bash
#
# Agent Trade — главный установщик (Этап 6.5 / 6.5.1).
#
# Разворачивает систему на сервере (Ubuntu 24.04, root) одной командой.
# Скрипт ИДЕМПОТЕНТЕН: повторный запуск безопасен (перед каждым изменением
# проверяется «уже сделано»). Все статусы и ошибки — на русском. В самом конце —
# явное «ГОТОВО» или «ОШИБКА».
#
# Запуск (см. deploy/bootstrap и README, раздел «Запуск установщика»):
#   curl -fsSL https://raw.githubusercontent.com/evgenii-yps/analagentai/\
#   claude/deployment-installer-script-k9e6t4/deploy/install.sh | sudo bash
#
set -Eeuo pipefail

# --- Константы развёртывания (подставлены Claude Code из подключения к репо) ---
REPO_OWNER="evgenii-yps"
REPO_NAME="analagentai"
REPO_BRANCH="claude/deployment-installer-script-k9e6t4"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

APP_DIR="/opt/agent-trade"
APP_USER="agent"
LOG_FILE="/var/log/agent-trade-install.log"

# Сервисы приложения, которые должны быть подняты.
EXPECTED_SERVICES=(postgres redis collector agents decision notify evaluator)

OVERALL_FAIL=0
CLONE_URL="$REPO_URL"   # для приватного репо будет заменён на URL с токеном

# ---------------------------------------------------------------------------
# Логирование: всё пишем и на экран, и в $LOG_FILE. Секреты сюда НЕ попадают —
# ввод токенов скрытый, а сгенерированный пароль БД печатается отдельно в
# /dev/tty (минуя лог) и кладётся только в отчёт (§13.4 ТЗ 6.5).
# ---------------------------------------------------------------------------
touch "$LOG_FILE" 2>/dev/null || true
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { echo "[$(date '+%F %T')] $*"; }
step() { echo; echo "==================================================================="; log "ШАГ: $*"; echo "==================================================================="; }
die()  { log "ОШИБКА: $*"; log "Установка прервана. Полный лог: $LOG_FILE"; exit 1; }

trap 'rc=$?; [[ $rc -ne 0 ]] && log "ОШИБКА: команда завершилась с кодом $rc (строка $LINENO)."; ' ERR

# Скрывает токен внутри URL вида https://user:token@host в потоке вывода.
redact() { sed -E 's#(https://)[^@/ ]+@#\1***@#g'; }

# ---------------------------------------------------------------------------
# Ввод от пользователя — строго из /dev/tty (скрипт запущен через curl | bash,
# поэтому обычный stdin занят телом скрипта). printf -v не печатает значение,
# чтобы секрет не попал в лог.
# ---------------------------------------------------------------------------
prompt_hidden() {  # $1 = имя переменной, $2 = текст приглашения
    local __var="$1" __text="$2" __val=""
    printf '%s' "$__text" > /dev/tty
    read -rs __val < /dev/tty
    printf '\n' > /dev/tty
    printf -v "$__var" '%s' "$__val"
}
prompt_visible() {  # $1 = имя переменной, $2 = текст приглашения
    local __var="$1" __text="$2" __val=""
    printf '%s' "$__text" > /dev/tty
    read -r __val < /dev/tty
    printf -v "$__var" '%s' "$__val"
}
tty_say() { printf '%s\n' "$*" > /dev/tty; }

# Генерирует случайный пароль из 32 буквенно-цифровых символов.
gen_password() {
    ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 )
}

# Отправляет тестовое сообщение в Telegram. 0 — успех. Токен нигде не печатается.
telegram_test() {  # $1 = token, $2 = chat_id
    local token="$1" chat="$2" resp
    resp="$(curl -fsS -m 15 "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=✅ Agent Trade: установщик подключился к боту. Разворачиваю систему." \
        2>/dev/null || true)"
    case "$resp" in *'"ok":true'*) return 0 ;; *) return 1 ;; esac
}

# ===========================================================================
# ШАГ 1. Предусловия.
# ===========================================================================
check_preconditions() {
    step "1/8 Предусловия (root, ОС, интернет)"

    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Скрипт нужно запускать под root (через sudo)."
    log "Запуск под root — OK."

    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        [[ "${ID:-}" == "ubuntu" ]] || die "Требуется Ubuntu (обнаружено: ${PRETTY_NAME:-неизвестно})."
        if [[ "${VERSION_ID:-}" != "24.04" ]]; then
            log "ВНИМАНИЕ: ожидалась Ubuntu 24.04, обнаружено ${VERSION_ID:-?}. Продолжаю."
        else
            log "ОС Ubuntu 24.04 — OK."
        fi
    else
        die "Не удалось определить ОС (нет /etc/os-release)."
    fi

    # curl уже есть (через него скачан установщик), но подстрахуемся.
    command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }
    if curl -fsS -m 15 -o /dev/null https://github.com; then
        log "Доступ в интернет — OK."
    else
        die "Нет доступа в интернет (не открывается github.com). Проверьте сеть сервера."
    fi

    export DEBIAN_FRONTEND=noninteractive
    log "Устанавливаю базовые пакеты (git, python3, ca-certificates)…"
    apt-get update -y
    apt-get install -y ca-certificates curl git python3 gnupg
    log "Базовые пакеты готовы."
}

# ===========================================================================
# ШАГ 2. Доставка кода (клонирование репозитория).
# ===========================================================================
obtain_code() {
    step "2/8 Доставка кода на сервер"

    # Определяем доступность репозитория. Публичный клонируется анонимно,
    # приватный — по fine-grained токену (запрашивается скрыто, в лог не пишется).
    if git ls-remote "$REPO_URL" >/dev/null 2>&1; then
        log "Репозиторий доступен анонимно (публичный) — клонирую по HTTPS."
        CLONE_URL="$REPO_URL"
    else
        log "Репозиторий недоступен анонимно — вероятно приватный. Нужен токен на чтение."
        tty_say "Как создать токен: GitHub → Settings → Developer settings →"
        tty_say "Fine-grained tokens → Generate new token → выбрать репозиторий"
        tty_say "${REPO_OWNER}/${REPO_NAME}, право Contents: Read-only → Generate."
        local __token=""
        prompt_hidden __token "Вставьте GitHub fine-grained токен (ввод скрыт): "
        CLONE_URL="https://x-access-token:${__token}@github.com/${REPO_OWNER}/${REPO_NAME}.git"
    fi

    if [[ -d "$APP_DIR/.git" ]]; then
        log "Каталог $APP_DIR уже содержит репозиторий — обновляю до ветки $REPO_BRANCH."
        if ! git -C "$APP_DIR" fetch --depth 1 "$CLONE_URL" "$REPO_BRANCH" 2> >(redact >&2); then
            die "Не удалось получить обновления репозитория (проверьте сеть / токен для приватного репо)."
        fi
        git -C "$APP_DIR" checkout -B "$REPO_BRANCH" FETCH_HEAD 2> >(redact >&2)
    else
        log "Клонирую $REPO_URL (ветка $REPO_BRANCH) в $APP_DIR…"
        mkdir -p "$(dirname "$APP_DIR")"
        if ! git clone --branch "$REPO_BRANCH" --depth 1 "$CLONE_URL" "$APP_DIR" 2> >(redact >&2); then
            die "Не удалось клонировать репозиторий (проверьте сеть / токен для приватного репо)."
        fi
    fi
    # Убираем возможный токен из сохранённого remote.
    git -C "$APP_DIR" remote set-url origin "$REPO_URL" 2>/dev/null || true
    log "Код на месте: $APP_DIR (ветка $REPO_BRANCH)."
}

# ===========================================================================
# ШАГ 3. Гео-тест OKX (блокирующий).
# ===========================================================================
geo_check() {
    step "3/8 Гео-тест OKX (блокирующий)"
    log "Проверяю доступность OKX из этой локации (REST + WebSocket)…"
    if python3 "$APP_DIR/scripts/geo_check.py"; then
        log "Гео-тест OKX пройден — OKX доступна. Продолжаю."
    else
        die "Гео-тест OKX ПРОВАЛЕН (см. коды ответов выше). Ничего не развёрнуто.
Сервер находится в локации, откуда OKX недоступна. Смените регион дата-центра
на разрешённый для OKX и запустите установщик заново."
    fi
}

# ===========================================================================
# ШАГ 4. Секреты (интерактивно и скрыто).
# ===========================================================================
collect_secrets() {
    step "4/8 Секреты (Telegram-токен, chat_id; пароль БД генерируется)"

    # Переиспользуем ранее сохранённые значения (идемпотентность): пароль БД
    # ОБЯЗАТЕЛЬНО должен совпадать с тем, что уже зашит в томе postgres.
    if [[ -f "$APP_DIR/.env" ]]; then
        # shellcheck disable=SC1091
        set -a; . "$APP_DIR/.env"; set +a
    fi

    if [[ -n "${POSTGRES_PASSWORD:-}" && "${POSTGRES_PASSWORD}" != "change_me" ]]; then
        log "Пароль PostgreSQL уже задан ранее — переиспользую (том БД уже инициализирован)."
    else
        POSTGRES_PASSWORD="$(gen_password)"
        log "Сгенерирован новый пароль PostgreSQL (32 символа)."
    fi

    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]] \
        && telegram_test "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID"; then
        log "Telegram уже настроен и работает — переиспользую сохранённые значения."
    else
        tty_say ""
        tty_say "Понадобятся TELEGRAM_BOT_TOKEN (от @BotFather) и TELEGRAM_CHAT_ID (от @userinfobot)."
        while true; do
            prompt_hidden TELEGRAM_BOT_TOKEN "Введите TELEGRAM_BOT_TOKEN (ввод скрыт): "
            prompt_visible TELEGRAM_CHAT_ID "Введите TELEGRAM_CHAT_ID: "
            log "Проверяю Telegram: отправляю тестовое сообщение…"
            if telegram_test "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID"; then
                log "Telegram OK: тестовое сообщение отправлено. Проверьте, что оно пришло."
                break
            fi
            tty_say "Не удалось отправить сообщение. Проверьте токен и chat_id и повторите ввод."
        done
    fi
}

# ===========================================================================
# ШАГ 5. Настройка ОС и безопасность (§5 ТЗ 6.5).
# ===========================================================================
setup_os_security() {
    step "5/8 Настройка ОС и безопасность"
    export DEBIAN_FRONTEND=noninteractive

    log "Таймзона → UTC."
    timedatectl set-timezone UTC || true

    log "Обновление списков пакетов и системы (security)…"
    apt-get update -y
    apt-get -y upgrade || log "ВНИМАНИЕ: apt upgrade завершился с предупреждениями — продолжаю."

    # Пользователь agent с sudo.
    if ! id "$APP_USER" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "$APP_USER"
        log "Создан пользователь $APP_USER."
    else
        log "Пользователь $APP_USER уже существует."
    fi
    usermod -aG sudo "$APP_USER"

    # UFW: наружу открыт только 22/tcp; 5432 и 6379 закрыты навсегда.
    log "Настраиваю firewall (UFW): разрешён только вход по 22/tcp."
    apt-get install -y ufw
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp
    ufw --force enable
    log "UFW активен. Порты 5432 и 6379 наружу НЕ открыты."

    # fail2ban (обязателен, т.к. вход по паролю не отключаем — §5.4).
    log "Устанавливаю и включаю fail2ban (защита SSH от подбора пароля)."
    apt-get install -y fail2ban
    cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
EOF
    systemctl enable fail2ban >/dev/null 2>&1 || true
    systemctl restart fail2ban

    # unattended-upgrades: только security, без автоперезагрузки.
    log "Настраиваю автоматические security-обновления (без автоперезагрузки)."
    apt-get install -y unattended-upgrades
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    cat > /etc/apt/apt.conf.d/52unattended-upgrades-agent-trade <<'EOF'
// Не перезагружать сервер автоматически после обновлений (§5 ТЗ 6.5).
Unattended-Upgrade::Automatic-Reboot "false";
EOF
    systemctl enable unattended-upgrades >/dev/null 2>&1 || true
    systemctl restart unattended-upgrades >/dev/null 2>&1 || true

    # Swap 2 ГБ.
    if swapon --show 2>/dev/null | grep -q '/swapfile'; then
        log "Swap-файл уже активен — пропуск."
    else
        log "Создаю swap-файл 2 ГБ."
        if [[ ! -f /swapfile ]]; then
            fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
        fi
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null 2>&1 || true
        swapon /swapfile
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        log "Swap 2 ГБ включён."
    fi

    # Docker Engine + Compose из официального репозитория.
    install_docker

    # agent в группе docker (чтобы cron/systemd работали без пароля).
    usermod -aG docker "$APP_USER"
    log "Пользователь $APP_USER добавлен в группу docker."
}

install_docker() {
    if docker --version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "Docker и Compose уже установлены — пропуск."
        return
    fi
    log "Устанавливаю Docker Engine + плагин Compose из официального репозитория Docker…"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    local arch codename
    arch="$(dpkg --print-architecture)"
    codename="$(. /etc/os-release; echo "${VERSION_CODENAME}")"
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    log "Docker установлен."
}

# ===========================================================================
# ШАГ 6. Развёртывание стека (§6 ТЗ 6.5).
# ===========================================================================
deploy_stack() {
    step "6/8 Развёртывание стека (docker compose)"
    cd "$APP_DIR"

    write_env
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    mkdir -p "$APP_DIR/logs" "$APP_DIR/backups"
    chown "$APP_USER:$APP_USER" "$APP_DIR/logs" "$APP_DIR/backups"

    log "Собираю и запускаю контейнеры: docker compose up -d --build (может занять пару минут)…"
    docker compose up -d --build

    log "Жду готовности PostgreSQL…"
    wait_postgres || die "PostgreSQL не поднялся за отведённое время. Смотрите: docker compose logs postgres"
    log "PostgreSQL готов."

    log "Проверяю health-check приложения…"
    if docker compose exec -T collector python -m src.healthcheck; then
        log "Health-check: OK (PostgreSQL + Redis доступны)."
    else
        die "Health-check не прошёл. Смотрите: docker compose logs"
    fi

    local ntables
    ntables="$(docker compose exec -T postgres psql -U agenttrade -d agenttrade -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d '[:space:]')"
    if [[ "${ntables:-0}" -ge 9 ]]; then
        log "В БД созданы таблицы (найдено: ${ntables})."
    else
        die "В БД ожидалось ≥9 таблиц, найдено: ${ntables:-0}. Смотрите: docker compose logs postgres"
    fi
    log "Стек развёрнут."
}

write_env() {
    log "Формирую $APP_DIR/.env (EXCHANGE=okx — снимает дефект D-2), chmod 600."
    umask 077
    cat > "$APP_DIR/.env" <<EOF
# Файл сгенерирован установщиком (deploy/install.sh). В git НЕ коммитится.
POSTGRES_USER=agenttrade
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=agenttrade
PG_HOST=postgres
PG_PORT=5432
REDIS_HOST=redis
REDIS_PORT=6379
LOG_LEVEL=INFO
EXCHANGE=okx
SYMBOL=BTC/USDT
SWAP_SYMBOL=BTC/USDT:USDT
TIMEFRAMES=1m,5m,15m,1h
OHLCV_INTERVAL=30
ORDERBOOK_INTERVAL=10
ORDERBOOK_DEPTH=50
TRADES_INTERVAL=15
FUTURES_INTERVAL=60
AGENT_TIMEFRAME=1h
AGENT_INTERVAL=60
AGENT_MIN_CANDLES=200
DECISION_INTERVAL=60
DECISION_THRESHOLD=0.3
AGENT_FRESHNESS_SEC=300
MIN_AGENTS=2
WEIGHT_MARKET=1.0
WEIGHT_LIQUIDITY=1.0
WEIGHT_FUTURES=1.0
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
NOTIFY_INTERVAL=30
NOTIFY_MIN_PROBABILITY=0.7
NOTIFY_COOLDOWN_SEC=1800
NOTIFY_TIMEZONE=Europe/Moscow
EVAL_INTERVAL=300
EVAL_HORIZONS=1h,4h
EVAL_PRIMARY_HORIZON=4h
STATS_LOG_INTERVAL=3600
EOF
    chmod 600 "$APP_DIR/.env"
}

wait_postgres() {
    local i
    for i in $(seq 1 60); do
        if docker compose exec -T postgres pg_isready -U agenttrade -d agenttrade >/dev/null 2>&1; then
            return 0
        fi
        sleep 3
    done
    return 1
}

# ===========================================================================
# ШАГ 7. Автозапуск, бэкапы, хранение, суточная сводка, вотчдог (§7–§11).
# ===========================================================================
setup_operations() {
    step "7/8 Автозапуск, бэкапы, хранение, сводка, вотчдог"

    chmod +x "$APP_DIR/scripts/"*.sh "$APP_DIR/scripts/"*.py 2>/dev/null || true

    # systemd-юнит автозапуска (§7).
    log "Устанавливаю systemd-юнит автозапуска agent-trade.service (§7)."
    install -m 644 "$APP_DIR/deploy/agent-trade.service" /etc/systemd/system/agent-trade.service
    systemctl daemon-reload
    systemctl enable agent-trade.service >/dev/null 2>&1 || true
    log "Автозапуск включён (systemctl enable agent-trade)."

    # Регламентные задачи (§8–§11) — cron под пользователем agent.
    log "Прописываю cron-задачи (бэкап §8, хранение §9, сводка §10, вотчдог §11)."
    cat > /etc/cron.d/agent-trade <<EOF
# Agent Trade — регламентные задачи (Этап 6.5). Выполняются под пользователем ${APP_USER}.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# §8 Бэкап БД — ежедневно 03:00 UTC
0 3 * * * ${APP_USER} ${APP_DIR}/scripts/backup_db.sh >> ${APP_DIR}/logs/backup.log 2>&1
# §9 Политика хранения — ежедневно 03:30 UTC
30 3 * * * ${APP_USER} /usr/bin/python3 ${APP_DIR}/scripts/retention.py >> ${APP_DIR}/logs/retention.log 2>&1
# §10 Суточная сводка в Telegram — ежедневно 09:00 UTC
0 9 * * * ${APP_USER} cd ${APP_DIR} && /usr/bin/docker compose exec -T evaluator python -m src.health.daily_report >> ${APP_DIR}/logs/daily_report.log 2>&1
# §11 Вотчдог — каждые 5 минут
*/5 * * * * ${APP_USER} /usr/bin/python3 ${APP_DIR}/scripts/watchdog.py >> ${APP_DIR}/logs/watchdog.log 2>&1
EOF
    chmod 644 /etc/cron.d/agent-trade
    log "Cron-задачи установлены (/etc/cron.d/agent-trade)."
}

# ===========================================================================
# ШАГ 8. Самопроверка и отчёт (§12–§13).
# ===========================================================================
add_check() {  # $1 = текст, $2 = код (0 — ок)
    if [[ "$2" -eq 0 ]]; then
        CHECK_LINES+=("- [x] $1")
        log "  OK  — $1"
    else
        CHECK_LINES+=("- [ ] $1")
        log "  FAIL — $1"
        OVERALL_FAIL=1
    fi
}

self_check_and_report() {
    step "8/8 Итоговая самопроверка и отчёт"
    cd "$APP_DIR"
    CHECK_LINES=()

    # Проверки намеренно возвращают ненулевой код при провале — на время
    # самопроверки отключаем errexit и ERR-trap, чтобы собрать ВСЕ результаты
    # без ложных сообщений об ошибке.
    trap - ERR
    set +e

    # Все контейнеры подняты.
    local svc up_all=0
    for svc in "${EXPECTED_SERVICES[@]}"; do
        if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$svc"; then
            up_all=1
        fi
    done
    add_check "Все контейнеры запущены (${EXPECTED_SERVICES[*]})" "$up_all"

    docker compose exec -T collector python -m src.healthcheck >/dev/null 2>&1
    add_check "Health-check приложения (PostgreSQL + Redis) = 0" "$?"

    local nt
    nt="$(docker compose exec -T postgres psql -U agenttrade -d agenttrade -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d '[:space:]')"
    [[ "${nt:-0}" -ge 9 ]]; add_check "В БД созданы все таблицы (${nt:-0} ≥ 9)" "$?"

    ufw status 2>/dev/null | grep -q "Status: active"; add_check "Firewall UFW активен (только 22/tcp наружу)" "$?"
    systemctl is-active --quiet fail2ban; add_check "fail2ban запущен" "$?"
    systemctl is-enabled --quiet docker; add_check "Docker включён в автозапуск" "$?"
    systemctl is-enabled --quiet agent-trade.service; add_check "Автозапуск стека (agent-trade.service) включён" "$?"
    swapon --show 2>/dev/null | grep -q '/swapfile'; add_check "Swap 2 ГБ активен" "$?"
    [[ "$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value)" == "UTC" ]]; add_check "Таймзона = UTC" "$?"
    [[ -f /etc/cron.d/agent-trade ]]; add_check "Регламентные задачи (cron) установлены" "$?"
    [[ -f "$APP_DIR/.env" && "$(stat -c '%a' "$APP_DIR/.env")" == "600" ]]; add_check ".env существует и имеет права 600" "$?"

    set -e
    trap 'rc=$?; [[ $rc -ne 0 ]] && log "ОШИБКА: команда завершилась с кодом $rc (строка $LINENO)."; ' ERR
    write_report

    echo
    if [[ "$OVERALL_FAIL" -eq 0 ]]; then
        log "Самопроверка: все пункты пройдены."
    else
        log "Самопроверка: есть непройденные пункты (см. отметки FAIL выше и отчёт)."
    fi
}

write_report() {
    local report="$APP_DIR/DEPLOY_REPORT.md"
    log "Формирую отчёт: $report"
    {
        echo "# Agent Trade — отчёт о развёртывании"
        echo
        echo "- Дата (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
        echo "- Сервер: $(hostname) ($(hostname -I 2>/dev/null | awk '{print $1}'))"
        echo "- Каталог приложения: \`$APP_DIR\`"
        echo "- Ветка кода: \`$REPO_BRANCH\`"
        echo "- Биржа: \`okx\` (EXCHANGE=okx — дефект D-2 закрыт)"
        echo
        echo "## Результаты самопроверки (§12)"
        echo
        printf '%s\n' "${CHECK_LINES[@]}"
        echo
        echo "## Доступ к PostgreSQL (§13.4)"
        echo
        echo "Сохраните этот пароль — он показан один раз:"
        echo
        echo '```'
        echo "POSTGRES_USER=agenttrade"
        echo "POSTGRES_DB=agenttrade"
        echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
        echo '```'
        echo
        echo "> Порт 5432 доступен только с самого сервера (127.0.0.1) — наружу закрыт."
        echo "> Секреты Telegram в отчёт не выносятся (хранятся только в \`$APP_DIR/.env\`)."
        echo
        echo "## Полезные команды"
        echo
        echo '```bash'
        echo "cd $APP_DIR"
        echo "docker compose ps                 # статус контейнеров"
        echo "docker compose logs -f --tail=50  # логи всех сервисов"
        echo "docker compose exec -T collector python -m src.healthcheck"
        echo '```'
        echo
        echo "## Дальше"
        echo
        echo "Сутки наблюдения, затем сверка по §12 и переход к §15 ТЗ 6.5"
        echo "(3 недели накопления статистики → Этап 7)."
    } > "$report"
    chmod 600 "$report"
    chown "$APP_USER:$APP_USER" "$report" 2>/dev/null || true
}

# ===========================================================================
# Финал.
# ===========================================================================
final_banner() {
    echo
    if [[ "$OVERALL_FAIL" -eq 0 ]]; then
        log "==================================================================="
        log "ГОТОВО. Система развёрнута и работает."
        log "Отчёт: $APP_DIR/DEPLOY_REPORT.md"
        log "==================================================================="
    else
        log "==================================================================="
        log "ЗАВЕРШЕНО С ЗАМЕЧАНИЯМИ. Часть проверок не пройдена — см. отметки FAIL"
        log "выше и файл $APP_DIR/DEPLOY_REPORT.md. Система, скорее всего, поднята,"
        log "но требуется внимание к отмеченным пунктам."
        log "==================================================================="
    fi

    # Пароль PostgreSQL печатаем ОТДЕЛЬНО в консоль (минуя лог-файл), один раз.
    {
        printf '\n'
        printf '========================================================\n'
        printf 'СОХРАНИТЕ ПАРОЛЬ POSTGRESQL (показывается один раз):\n'
        printf '  POSTGRES_PASSWORD=%s\n' "${POSTGRES_PASSWORD}"
        printf 'Он также записан в %s\n' "$APP_DIR/DEPLOY_REPORT.md"
        printf '========================================================\n\n'
    } > /dev/tty 2>/dev/null || true
}

main() {
    log "### Установщик Agent Trade запущен ($(date -u '+%F %T') UTC) ###"
    check_preconditions
    obtain_code
    geo_check
    collect_secrets
    setup_os_security
    deploy_stack
    setup_operations
    self_check_and_report
    final_banner
}

main "$@"
