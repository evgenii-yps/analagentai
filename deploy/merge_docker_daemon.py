#!/usr/bin/env python3
"""Слияние ограничения журналов контейнеров в ``/etc/docker/daemon.json`` (Этап 9.4, A.1).

Если файл уже есть — значения СЛИВАЮТСЯ, а не затираются: чужие ключи
(``registry-mirrors``, ``dns`` и прочее) остаются как были. Перед записью
прежний файл копируется рядом с суффиксом ``.bak-<дата>``.

Скрипт ТОЛЬКО пишет файл. Перезапуск docker (он перезапускает все контейнеры)
здесь не выполняется — это делается вручную в окне обслуживания, вместе с
перезагрузкой сервера (docs/maintenance.md).

Запуск (root): ``sudo python3 deploy/merge_docker_daemon.py [путь]``.
Только стандартная библиотека.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/etc/docker/daemon.json")

# 20 МБ x 3 файла = 60 МБ на контейнер; 14 контейнеров — не более 840 МБ.
WANTED_LOG_DRIVER = "json-file"
WANTED_LOG_OPTS: dict[str, str] = {"max-size": "20m", "max-file": "3"}


def merge(current: dict[str, Any]) -> dict[str, Any]:
    """Возвращает новую конфигурацию: ``current`` плюс ограничение журналов."""
    result = dict(current)
    result["log-driver"] = WANTED_LOG_DRIVER
    opts = dict(result.get("log-opts") or {})
    opts.update(WANTED_LOG_OPTS)
    result["log-opts"] = opts
    return result


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    current: dict[str, Any] = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"ОШИБКА: {path} не является корректным JSON: {exc}. Файл не тронут.")
            return 1
        if not isinstance(current, dict):
            print(f"ОШИБКА: {path} содержит не объект JSON. Файл не тронут.")
            return 1
    merged = merge(current)
    if merged == current:
        print(f"{path}: уже содержит нужные значения, изменений нет.")
        return 0
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now(UTC):%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        print(f"Прежний файл сохранён: {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path} записан:")
    print(path.read_text(encoding="utf-8"))
    print("ВНИМАНИЕ: значения вступят в силу после `systemctl restart docker` "
          "(перезапустит ВСЕ контейнеры) — только в окне обслуживания.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
