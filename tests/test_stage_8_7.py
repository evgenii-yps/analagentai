"""Тесты Этапа 8.7 — гигиена конфигурации и проверочных скриптов.

  8.7-A  test_fee_*              — круговая комиссия равна 0.20 (вход + выход);
  8.7-B  test_verify_7_3_*       — окно funding берётся из .env, а не зашито;
  8.7-C  test_silence_sql_*      — набор версий в 06_silence.sql динамический;
  8.7-D  test_install_*          — установка ключей идемпотентна, дубли видны;
  8.7-E  test_readme_*           — осиротевшие контейнеры описаны в инструкции;
  8.7-F  test_funding_reserve_*  — строка запаса funding в суточной сводке.

Граница этапа: НИ ОДИН тест здесь не меняет решение системы. Проверку этого
факта закрепляет отдельный тест 8.7-1 в конце файла: перечень файлов, которых
этап касается, не пересекается с ядром принятия решения.
"""

from __future__ import annotations

import importlib.util
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.evaluate import Costs, gross_pnl_pct, net_pnl_pct
from src.health import daily_report

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")


# --- 8.7-A. Круговая комиссия = вход + выход -------------------------------

def test_fee_in_backtest_example_is_a_full_roundtrip() -> None:
    """0.20 = 2 × 0.10 % тейкера спота OKX (Lv1). Половина круга — дефект."""
    text = (ROOT / "backtest" / ".env.backtest.example").read_text(encoding="utf-8")
    assert "BT_FEE_ROUNDTRIP_PCT=0.20" in text
    assert "BT_FEE_ROUNDTRIP_PCT=0.10" not in text


def test_fee_declaration_says_what_roundtrip_means() -> None:
    """Имя параметра оказалось недостаточным — рядом обязано стоять пояснение."""
    text = (ROOT / "backtest" / ".env.backtest.example").read_text(encoding="utf-8")
    head = text.split("BT_FEE_ROUNDTRIP_PCT=")[0]
    assert "ВХОД + ВЫХОД" in head


def test_costs_total_at_the_corrected_fee() -> None:
    costs = Costs(fee_roundtrip_pct=Decimal("0.20"), slippage_pct=Decimal("0.01"))
    assert costs.total == pytest.approx(0.21)
    assert net_pnl_pct(gross_pnl_pct("buy", 100.0, 102.0), costs) == pytest.approx(1.79)


def test_covers_fees_does_not_read_the_backtest_fee() -> None:
    """Ключевая проверка §2.3 ТЗ: порог покрытия издержек взят из ДРУГОГО
    параметра, поэтому правка комиссии реплея не трогает текст сигнала.

    Если бы covers_fees считался от BT_FEE_ROUNDTRIP_PCT, этап пришлось бы
    остановить: изменение значения поменяло бы флаг в карточке сигнала.
    """
    targets = (ROOT / "src" / "risk" / "targets.py").read_text(encoding="utf-8")
    runner = (ROOT / "src" / "risk" / "runner.py").read_text(encoding="utf-8")
    assert "BT_FEE_ROUNDTRIP_PCT" not in targets
    assert "BT_FEE_ROUNDTRIP_PCT" not in runner
    assert "RISK_COST_ROUNDTRIP_PCT" in runner


def test_stage_7_4_report_marks_the_halved_costs() -> None:
    raw = (ROOT / "docs" / "STAGE_7_4_REPORT.md").read_text(encoding="utf-8")
    # Пометка стоит внутри цитаты: снимаем маркеры «>» и переносы строк, иначе
    # фраза распадётся на куски и проверка будет ловить вёрстку, а не смысл.
    text = " ".join(
        line.lstrip("> ") for line in raw.splitlines()
    )
    text = " ".join(text.split())
    assert "фактические результаты Этапа 7.4 хуже приведённых здесь" in text
    assert "УКРЕПЛЯЕТСЯ" in text
    assert "Пересчёт Этапа 7.4 НЕ ВЫПОЛНЯЛСЯ" in text


# --- 8.7-A2. RISK_COST_ROUNDTRIP_PCT объявлен там, где его обещает код ------

_RISK_COST_DEFAULT = 0.22


def test_risk_cost_is_declared_in_env_example() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nRISK_COST_ROUNDTRIP_PCT=0.22" in text


def test_risk_cost_is_written_by_the_installer() -> None:
    """Замер на сервере 25.08.2026: ключа в рабочем .env не было вовсе.

    Комментарий src/core/config.py заявлял, что значение вынесено в .env, а
    установщик его туда никогда не писал. Строка добавлена в блок write_env.
    """
    assert "RISK_COST_ROUNDTRIP_PCT=${RISK_COST_ROUNDTRIP_PCT:-0.22}" in INSTALL_SH


def test_risk_cost_matches_the_code_default_so_nothing_changes() -> None:
    """Ключевое: объявленное значение РАВНО умолчанию кода.

    Иначе добавление строки в .env было бы не гигиеной, а тихой сменой порога
    covers_fees — то есть изменением текста сигнала, запрещённым §1 ТЗ.
    """
    from src.core.config import settings

    assert settings.RISK_COST_ROUNDTRIP_PCT == _RISK_COST_DEFAULT
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = [
        line.split("=", 1)[1].split("#")[0].strip()
        for line in example.splitlines()
        if line.startswith("RISK_COST_ROUNDTRIP_PCT=")
    ]
    assert declared == ["0.22"]
    assert float(declared[0]) == _RISK_COST_DEFAULT


# --- 8.7-B. Проверочный скрипт печатает действующий параметр ---------------

def test_verify_7_3_reads_the_funding_window_from_env() -> None:
    text = (ROOT / "deploy" / "verify_7_3.sh").read_text(encoding="utf-8")
    assert 'env_value FUTURES_LOOKBACK_HOURS' in text
    # Ни одного зашитого окна не осталось: было interval '168 hours'.
    assert "168 hours" not in text
    assert "make_interval(hours => ${FUNDING_LOOKBACK})" in text


def test_verify_7_3_says_when_the_parameter_is_absent() -> None:
    """Отсутствие параметра печатается словами, а не подменяется умолчанием."""
    text = (ROOT / "deploy" / "verify_7_3.sh").read_text(encoding="utf-8")
    assert "параметр не задан" in text
    assert "${FUTURES_MIN_POINTS:-20}" not in text


# --- 8.7-C. Динамический набор версий в 06_silence.sql ---------------------

SILENCE_SQL = (ROOT / "analysis" / "sql" / "06_silence.sql").read_text(encoding="utf-8")


def test_silence_sql_has_no_hardcoded_version_columns() -> None:
    """Колонок v1…v4 не осталось: версия — строка, набор берётся из данных."""
    for column in ("AS v1", "AS v2", "AS v3", "AS v4", "v4_n", "v4_pct"):
        assert column not in SILENCE_SQL, column
    for filt in ("WHERE ver = 4", "logic_version = 4"):
        assert filt not in SILENCE_SQL, filt


def test_silence_sql_excludes_unknown_version_everywhere() -> None:
    """Версия 0 — это «неизвестно», а не версия: смешивать её нельзя."""
    assert SILENCE_SQL.count("logic_version <> 0") >= 6
    assert "WHERE ver <> 0" in SILENCE_SQL


def test_silence_sql_reports_the_excluded_count_separately() -> None:
    """Исключение обязано быть видно ЧИСЛОМ, а не подразумеваться."""
    assert "rows_excluded_ver0" in SILENCE_SQL
    assert "6.-1" in SILENCE_SQL


# --- 8.7-D. Идемпотентность установки ключей -------------------------------

def _extract_helpers() -> str:
    """Тело функций-помощников install.sh, пригодное для source в bash.

    Сам install.sh запускать нельзя: он в конце вызывает main и разворачивает
    систему. Поэтому проверяются именно функции, а не файл целиком.
    """
    wanted = ("declarations_dedupe", "declarations_duplicates",
              "normalize_declarations", "env_upsert")
    out: list[str] = ['log() { :; }']
    lines = INSTALL_SH.splitlines()
    for name in wanted:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
        end = next(i for i in range(start, len(lines)) if lines[i] == "}")
        out.extend(lines[start:end + 1])
    return "\n".join(out) + "\n"


def _bash(script: str, cwd: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_install_env_upsert_replaces_instead_of_appending(tmp_path: Path) -> None:
    """Повторный вызов не создаёт второе объявление того же ключа."""
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_extract_helpers(), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("A=1\nEVAL_HORIZONS=1h,4h\nB=2\n", encoding="utf-8")
    _bash(
        f"source {helpers}; "
        f"env_upsert {env} EVAL_HORIZONS '1h,4h,12h'; "
        f"env_upsert {env} EVAL_HORIZONS '1h,4h,12h'; "
        f"env_upsert {env} NEW_KEY 'x'; env_upsert {env} NEW_KEY 'x'",
        tmp_path,
    )
    text = env.read_text(encoding="utf-8")
    assert text.count("EVAL_HORIZONS=") == 1
    assert "EVAL_HORIZONS=1h,4h,12h" in text
    assert text.count("NEW_KEY=") == 1
    # Порядок остальных строк сохранён: ключ не уехал в конец файла.
    assert text.splitlines()[0] == "A=1"


def test_install_normalize_keeps_the_effective_value(tmp_path: Path) -> None:
    """Снятие повторов НЕ меняет действующую конфигурацию.

    И docker compose env_file, и cron читают файл сверху вниз: действует
    ПОСЛЕДНЕЕ объявление. Значит остаться должно именно оно — иначе «уборка»
    молча поменяла бы настройки системы.
    """
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_extract_helpers(), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("EVAL_HORIZONS=1h,4h\nX=1\nEVAL_HORIZONS=1h,4h,12h\n", encoding="utf-8")
    _bash(f"source {helpers}; normalize_declarations {env}", tmp_path)
    text = env.read_text(encoding="utf-8")
    assert text.count("EVAL_HORIZONS=") == 1
    assert "EVAL_HORIZONS=1h,4h,12h" in text


def test_install_normalize_removes_the_duplicated_cron_comment(tmp_path: Path) -> None:
    """Ровно тот дефект, что найден на сервере: комментарий записан дважды."""
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_extract_helpers(), encoding="utf-8")
    cron = tmp_path / "agent-trade"
    comment = "# Этап 7.3 Калибровочная кривая — ежедневно 05:30 UTC"
    cron.write_text(
        f"SHELL=/bin/bash\n\n{comment}\n30 5 * * * agent calibration\n{comment}\n",
        encoding="utf-8",
    )
    dup = _bash(f"source {helpers}; declarations_duplicates {cron}", tmp_path)
    assert "повторов: 2" in dup
    _bash(f"source {helpers}; normalize_declarations {cron}", tmp_path)
    text = cron.read_text(encoding="utf-8")
    assert text.count(comment) == 1
    assert "30 5 * * * agent calibration" in text
    assert _bash(f"source {helpers}; declarations_duplicates {cron}", tmp_path) == ""


def test_install_cron_is_written_whole_not_appended() -> None:
    assert "cron_install /etc/cron.d/agent-trade <<EOF" in INSTALL_SH
    assert "cat > /etc/cron.d/agent-trade <<EOF" not in INSTALL_SH


def test_install_fails_loudly_on_duplicates() -> None:
    """§5.3 ТЗ: список находок печатается, код возврата ненулевой."""
    assert "check_no_duplicate_declarations" in INSTALL_SH
    assert "DUPLICATE_FAIL=1" in INSTALL_SH
    assert "exit 3" in INSTALL_SH


# --- 8.7-E. Осиротевшие контейнеры -----------------------------------------

def test_deployment_instructions_mention_remove_orphans() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--remove-orphans" in readme
    assert "bt_load" in readme
    assert "--remove-orphans" in INSTALL_SH


# --- 8.7-F. Запас точек funding в суточной сводке --------------------------

def _reserve(monkeypatch, out: str, lookback: str = "336", min_points: str = "20"):
    monkeypatch.setitem(daily_report.ENV, "FUTURES_LOOKBACK_HOURS", lookback)
    monkeypatch.setitem(daily_report.ENV, "FUTURES_MIN_POINTS", min_points)
    monkeypatch.delenv("FUTURES_LOOKBACK_HOURS", raising=False)
    monkeypatch.delenv("FUTURES_MIN_POINTS", raising=False)
    monkeypatch.setattr(daily_report, "_psql", lambda *a, **k: out)
    return daily_report.section_funding_reserve()


def test_funding_reserve_prints_points_threshold_and_margin(monkeypatch) -> None:
    lines = _reserve(monkeypatch, "BTC|301\nETH|50")
    body = "\n".join(lines)
    assert "Окно 336 ч, порог 20 точек" in body
    assert "BTC: точек 301, порог 20, запас +281" in body
    assert "ETH: точек 50, порог 20, запас +30" in body


def test_funding_reserve_marks_a_margin_below_three(monkeypatch) -> None:
    lines = _reserve(monkeypatch, "BTC|301\nETH|22\nSOL|20")
    body = "\n".join(lines)
    assert "🟡 ETH: точек 22, порог 20, запас +2" in body
    assert "🟡 SOL: точек 20, порог 20, запас +0" in body
    assert "🟢 BTC" in body


def test_funding_reserve_marks_an_already_silent_instrument(monkeypatch) -> None:
    lines = _reserve(monkeypatch, "SOL|14")
    assert "🔴 SOL: точек 14, порог 20, запас -6 — агент уже молчит" in "\n".join(lines)


def test_funding_reserve_says_when_a_parameter_is_absent(monkeypatch) -> None:
    """Умолчание не подставляется: иначе строка врала бы молча (та же
    дисциплина, что в §3 — именно её нарушение и чинит этот этап)."""
    lines = _reserve(monkeypatch, "BTC|301", lookback="")
    assert "FUTURES_LOOKBACK_HOURS: параметр не задан" in "\n".join(lines)
    lines = _reserve(monkeypatch, "BTC|301", min_points="")
    assert "FUTURES_MIN_POINTS: параметр не задан" in "\n".join(lines)


def test_funding_reserve_counts_hours_not_raw_rows() -> None:
    """Агент видит окно, прорежённое до одной точки в час (get_funding_window).
    Сводка обязана считать так же, иначе запас окажется завышенным."""
    import inspect

    src = inspect.getsource(daily_report.section_funding_reserve)
    assert "count(DISTINCT date_trunc('hour', f.ts))" in src
    assert "i.type = 'swap'" in src


def test_funding_reserve_is_in_the_daily_message() -> None:
    import inspect

    assert "section_funding_reserve()" in inspect.getsource(daily_report.build_message)


# --- 8.7-1. Граница этапа: решение системы не затрагивается ----------------

def test_decision_snapshot_is_byte_for_byte_unchanged() -> None:
    """Три величины §1 ТЗ на фиксированном наборе входов не изменились.

    Отпечаток снят на ревизии ДО работ этапа (9444fca, «Этап 8.2») тем же
    скриптом и совпал побайтно — см. отчёт этапа. Здесь он закреплён числом,
    чтобы правка, меняющая решение, не прошла молча ни сейчас, ни позже.
    Перебираются все ветки make_decision: buy/sell/wait по порогу, нехватка
    свежих выводов, insufficient_data, устаревание, отсутствие агента.
    """
    spec = importlib.util.spec_from_file_location(
        "decision_parity_8_7", ROOT / "scripts" / "decision_parity_8_7.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    data = module.snapshot()
    assert data["cases"] == 323
    assert data["digest_sha256"] == (
        "1f12d5d29d64eb17911b2a20196311fdde6a66e9f932120a7d5d412aac0de18d"
    )
    # Отпечаток берётся от всех трёх величин сразу — убедимся, что каждая из
    # них реально попала в снимок, иначе совпадение ничего бы не значило.
    first = data["results"][0]
    for key in ("decision", "probability", "calibrated_probability"):
        assert key in first


def test_logic_version_stays_at_five() -> None:
    from src.core.config import settings

    assert settings.LOGIC_VERSION == 5
