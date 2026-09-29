#!/usr/bin/env bash
#
# Установка rclone закреплённой версии из официального zip-архива (Этап 9.4, C.1).
#
# НЕ из apt: в репозитории Ubuntu 24.04 версия устаревшая. Версия и sha256
# архива закреплены ниже; при несовпадении суммы установка прерывается и в
# систему ничего не попадает.
#
# Сумма взята из официального файла SHA256SUMS релиза v1.75.1
# (https://github.com/rclone/rclone/releases/download/v1.75.1/SHA256SUMS) и
# совпала с суммой фактически скачанного архива.
#
# Запуск (root): sudo bash deploy/install_rclone.sh
#
set -euo pipefail

RCLONE_VERSION="v1.75.1"
RCLONE_ZIP="rclone-${RCLONE_VERSION}-linux-amd64.zip"
RCLONE_SHA256="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"
RCLONE_URL="https://github.com/rclone/rclone/releases/download/${RCLONE_VERSION}/${RCLONE_ZIP}"

if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "ОШИБКА: скрипт рассчитан на x86_64 (Hetzner CX23), а здесь $(uname -m)." >&2
    exit 1
fi

if command -v rclone >/dev/null 2>&1 && rclone version 2>/dev/null | head -n 1 | grep -q "${RCLONE_VERSION}"; then
    echo "rclone ${RCLONE_VERSION} уже установлен: $(command -v rclone)"
    exit 0
fi

command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Скачиваю ${RCLONE_URL}"
curl -fsSL -o "${WORK}/${RCLONE_ZIP}" "${RCLONE_URL}"

actual="$(sha256sum "${WORK}/${RCLONE_ZIP}" | cut -d' ' -f1)"
if [[ "$actual" != "$RCLONE_SHA256" ]]; then
    echo "ОШИБКА: sha256 не совпал." >&2
    echo "  ожидалось: $RCLONE_SHA256" >&2
    echo "  получено:  $actual" >&2
    exit 1
fi
echo "sha256 совпал: $actual"

unzip -q -o "${WORK}/${RCLONE_ZIP}" -d "$WORK"
install -m 0755 "${WORK}/rclone-${RCLONE_VERSION}-linux-amd64/rclone" /usr/local/bin/rclone
rclone version | head -n 2
