"""Этап 9.5, §1.2 и §12 п. 10: границы этапа, проверяемые по коду (без сети и базы)."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_FILES = sorted((ROOT / "src" / "demo").glob("*.py")) + [ROOT / "src" / "demo_main.py"]
ALLOWED_TARGETS = {"demo_orders", "demo_state", "demo_balance_daily"}

_WRITE_RES = (
    re.compile(r"\bINSERT\s+INTO\s+(?:public\.)?([a-z_]+)", re.I),
    re.compile(r"\bUPDATE\s+(?:public\.)?([a-z_]+)\s+SET\b", re.I),
    re.compile(r"\bDELETE\s+FROM\s+(?:public\.)?([a-z_]+)", re.I),
    re.compile(r"\bMERGE\s+INTO\s+(?:public\.)?([a-z_]+)", re.I),
    re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?(?:public\.)?([a-z_]+)", re.I),
)
_DDL_RE = re.compile(r"\b(ALTER|DROP|CREATE)\s+(TABLE|INDEX|SCHEMA|ROLE|TYPE)\b", re.I)


def sql_write_targets(source: str) -> set[str]:
    """Цели записи во ВСЕХ строковых литералах исходника (разбор AST, не текст).

    Строки, склеенные из нескольких литералов подряд, Python склеивает уже при
    разборе, поэтому ``"INSERT INTO " "positions"`` не ускользнёт.
    """
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for pattern in _WRITE_RES:
                targets.update(m.lower() for m in pattern.findall(node.value))
    return targets


def test_the_scanner_itself_finds_writes_split_across_literals() -> None:
    bad = 'q = ("INSERT INTO " "positions (a) VALUES (1)")\nr = "update  signals set x=1"'
    assert sql_write_targets(bad) == {"positions", "signals"}
    assert sql_write_targets('q = "DELETE FROM public.risk_targets"') == {"risk_targets"}
    assert sql_write_targets('q = "SELECT * FROM positions"') == set()


def test_the_demo_service_writes_only_to_its_three_tables() -> None:
    found: set[str] = set()
    for path in DEMO_FILES:
        found |= sql_write_targets(path.read_text(encoding="utf-8"))
    assert found, "сканер не нашёл ни одной записи — проверка ничего не проверяет"
    assert found <= ALLOWED_TARGETS, f"запись в чужие таблицы: {found - ALLOWED_TARGETS}"
    assert found == ALLOWED_TARGETS      # обе таблицы действительно используются


def test_the_demo_service_has_no_ddl_and_no_writes_to_analysis_tables() -> None:
    for path in DEMO_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not _DDL_RE.search(node.value), f"DDL в {path.name}: {node.value[:60]}"


def test_the_demo_code_does_not_import_the_trading_or_analysis_code() -> None:
    for path in DEMO_FILES:
        text = path.read_text(encoding="utf-8")
        # Единственное исключение — чистый модуль переводов причин выхода
        # (src.positions.messages): ТЗ требует «тот же перевод, что в positions».
        text = text.replace("src.positions.messages", "")
        for forbidden in ("src.positions", "src.agents", "src.decision", "src.collectors",
                          "src.evaluator", "src.risk"):
            assert forbidden not in text, f"{path.name} импортирует {forbidden}"


def test_demo_keys_are_read_only_by_the_demo_service() -> None:
    allowed = {ROOT / "src" / "core" / "config.py", ROOT / "src" / "demo_main.py"}
    allowed |= set((ROOT / "src" / "demo").glob("*.py"))
    for path in (ROOT / "src").rglob("*.py"):
        if path in allowed:
            continue
        assert "OKX_DEMO_" not in path.read_text(encoding="utf-8"), path
    # внутри сервиса настройки читают только точка входа и предпроверка; остальное
    # получает значения аргументами
    for name in ("exchange.py", "rules.py", "runner.py", "report.py"):
        text = (ROOT / "src" / "demo" / name).read_text(encoding="utf-8")
        assert "src.core.config" not in text, name


def test_the_exchange_client_does_not_import_settings() -> None:
    tree = ast.parse((ROOT / "src" / "demo" / "exchange.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("src.core.config")
            assert not any(a.name == "settings" for a in node.names)


def test_private_calls_in_the_service_go_through_the_guarded_functions() -> None:
    """Ни runner, ни report, ни preflight не вызывают приватные методы ccxt напрямую."""
    for name in ("runner.py", "report.py", "preflight.py"):
        text = (ROOT / "src" / "demo" / name).read_text(encoding="utf-8")
        for method in ("fetch_balance(", "create_order(", "create_market_buy_order_with_cost(",
                       ".fetch_order("):
            assert method not in text.replace("fetch_order_by_cl(", ""), (name, method)


def test_only_spot_cash_orders_exist_in_the_code() -> None:
    text = (ROOT / "src" / "demo" / "exchange.py").read_text(encoding="utf-8")
    assert '"tdMode": "cash"' in text
    for forbidden in ("leverage", "marginMode", "reduceOnly", "posSide", "swap", "margin"):
        assert forbidden not in re.sub(r"#.*|\"\"\".*?\"\"\"", "", text, flags=re.S), forbidden


# --- проводка: compose, вотчдог, .env.example, структура файлов -----------------


def _service_block(compose: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^volumes:|\Z)", compose, re.M | re.S)
    assert match, f"сервиса {name} нет в docker-compose.yml"
    return match.group(1)


def test_compose_has_the_demo_service_like_positions() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    demo, positions = _service_block(compose, "demo"), _service_block(compose, "positions")
    assert 'command: ["python", "-m", "src.demo_main"]' in demo
    assert "build: ." in demo and "restart: unless-stopped" in demo
    assert "env_file: .env" in demo
    for line in ('test: ["CMD", "python", "-m", "src.healthcheck"]', "interval: 30s",
                 "timeout: 10s", "retries: 3", "start_period: 20s",
                 'max-size: "10m"', 'max-file: "3"'):
        assert line in demo and line in positions, line
    code = "\n".join(ln for ln in demo.splitlines() if not ln.strip().startswith("#"))
    for forbidden in ("profiles:", "network_mode", "networks:", "ports:", "/var/run/docker.sock"):
        assert forbidden not in code, forbidden      # выход в интернет есть, сокета docker нет


def test_watchdog_knows_the_demo_heartbeat() -> None:
    text = (ROOT / "scripts" / "watchdog.py").read_text(encoding="utf-8")
    assert '("demo:heartbeat", "DEMO_INTERVAL", 15, "demo")' in text


def _containers(path: str) -> list[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    block = text.split("CONTAINERS = ", 1)[1].split("]", 1)[0]
    return re.findall(r'"(\w+)"', block)


def test_demo_is_watched_by_state_in_the_watchdog_and_in_the_health_summary() -> None:
    """Упавший контейнер demo замечается по состоянию, а не только по heartbeat (§9)."""
    for path in ("scripts/watchdog.py", "src/health/daily_report.py"):
        containers = _containers(path)
        assert "demo" in containers and "positions" in containers, path
    health = (ROOT / "src" / "health" / "daily_report.py").read_text(encoding="utf-8")
    assert '("demo:heartbeat", "DEMO_INTERVAL", 15)' in health
    compose_services = set(re.findall(r"^  (\w+):$", (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8"), re.M))
    assert "demo" in compose_services


def test_the_sheets_and_the_bot_only_read_the_demo_tables() -> None:
    """Листы (service export) и /demo (роль только для чтения) в базу не пишут."""
    for rel in ("src/export/demo_sheets.py", "src/demo/ledger.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        targets = sql_write_targets(text)
        if rel.endswith("demo_sheets.py"):
            assert targets == set(), targets
        else:
            # единственная запись ledger — снимок суток
            assert targets == {"demo_balance_daily"}
    queries = (ROOT / "src" / "bot" / "queries.py").read_text(encoding="utf-8")
    # Этап 9.7: блок демо-запросов бота — от demo_state до positions_capital (несколько
    # методов), поэтому он берётся с начала строки и выравнивается.
    block = textwrap.dedent(queries[queries.index("    async def demo_state"):queries.index(
        "    async def positions_capital")])
    assert sql_write_targets(block) == set()


def test_env_example_declares_every_new_setting_and_leaves_keys_empty() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("DEMO_ENABLED", "OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY",
                "OKX_DEMO_PASSPHRASE", "OKX_DEMO_HOST", "DEMO_INTERVAL",
                "DEMO_MAX_ENTRY_DELAY_SEC", "DEMO_FILL_WAIT_SEC", "DEMO_MIN_USDT_BALANCE",
                "DEMO_START_CAPITAL_USD", "DEMO_NOTIFY_ENABLED", "DEMO_SHEETS_ENABLED"):
        assert re.search(rf"^{key}=", env, re.M), key
    # параметр удалён: в примере его нет (только слова о том, что он удалён)
    assert not re.search(r"^DEMO_REPORT_HOUR_UTC=", env, re.M)
    for key in ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE"):
        assert re.search(rf"^{key}=\s*$", env, re.M), f"{key} в .env.example должен быть пуст"
    assert re.search(r"^DEMO_ENABLED=false", env, re.M)


def test_the_files_of_the_stage_exist() -> None:
    for rel in ("src/demo/__init__.py", "src/demo/exchange.py", "src/demo/rules.py",
                "src/demo/runner.py", "src/demo/preflight.py", "src/demo/report.py",
                "src/demo_main.py", "db/migrations/030_demo_orders.sql",
                "db/migrations/030_demo_orders_rollback.sql", "deploy/verify_9_5.sh"):
        assert (ROOT / rel).is_file(), rel


def test_verify_script_is_read_only() -> None:
    text = (ROOT / "deploy" / "verify_9_5.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE|"
                         r"ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+TABLE)\b", code, re.I)
    verbs = set(re.findall(r"docker compose (\w+)", code))
    assert verbs <= {"exec", "ps", "logs"}, verbs      # ничего не запускает и не перезапускает
    assert "redis-cli GET" in code and not re.search(r"redis-cli (SET|DEL|FLUSH)", code)
    assert not re.search(r"\brm\s+-|\bkill\b|systemctl", code)
    assert "set -uo pipefail" in text and "ИТОГ" in text and "✅" in text and "❌" in text


def test_the_package_is_listed_for_the_build() -> None:
    assert '"src.demo"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_verify_script_knows_all_three_tables() -> None:
    text = (ROOT / "deploy" / "verify_9_5.sh").read_text(encoding="utf-8")
    assert "grep -vxE 'demo_orders|demo_state|demo_balance_daily|'" in text
    assert '"${present}" = "3"' in text
    # то, что скрипт считает «чужим», по коду сервиса и листов пусто
    found: set[str] = set()
    for path in [*DEMO_FILES, ROOT / "src" / "export" / "demo_sheets.py"]:
        found |= sql_write_targets(path.read_text(encoding="utf-8"))
    assert found - ALLOWED_TARGETS == set()


def test_verify_script_prints_the_virtual_output_switch() -> None:
    """Часть 3, §4.6: значение VIRTUAL_OUTPUT_ENABLED выводится, а не только читается."""
    text = (ROOT / "deploy" / "verify_9_5.sh").read_text(encoding="utf-8")
    assert 'env_val VIRTUAL_OUTPUT_ENABLED' in text
    assert "VIRTUAL_OUTPUT_ENABLED в .env:" in text and "по умолчанию true" in text
