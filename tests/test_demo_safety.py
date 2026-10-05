"""Этап 9.5, §1.2 и §12 п. 10: границы этапа, проверяемые по коду (без сети и базы)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_FILES = sorted((ROOT / "src" / "demo").glob("*.py")) + [ROOT / "src" / "demo_main.py"]
ALLOWED_TARGETS = {"demo_orders", "demo_state"}

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


def test_the_demo_service_writes_only_to_demo_orders_and_demo_state() -> None:
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


def test_env_example_declares_every_new_setting_and_leaves_keys_empty() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("DEMO_ENABLED", "OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY",
                "OKX_DEMO_PASSPHRASE", "OKX_DEMO_HOST", "DEMO_INTERVAL",
                "DEMO_MAX_ENTRY_DELAY_SEC", "DEMO_FILL_WAIT_SEC", "DEMO_MIN_USDT_BALANCE",
                "DEMO_REPORT_HOUR_UTC"):
        assert re.search(rf"^{key}=", env, re.M), key
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
