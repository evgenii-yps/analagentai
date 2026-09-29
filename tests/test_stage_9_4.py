"""Этап 9.4: дисковая гигиена. Блоки A, B, C, E (блок D ждёт подтверждения доклада D.1).

Shell-скрипты проверяются НАСТОЯЩИМ запуском в каталоге-песочнице: вместо docker,
curl и rclone подставляются поддельные программы, которые пишут свои вызовы в
журнал. Так проверяется поведение (что удалено, что отправлено, какой код
возврата), а не текст скрипта.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "true"], check=False).returncode != 0,
    reason="нужен bash",
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """APP_DIR с .env, каталогом backups/logs и поддельными docker/curl в PATH."""
    app = tmp_path / "app"
    (app / "backups").mkdir(parents=True)
    (app / "logs").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    _write_exec(
        bindir / "docker",
        f"""#!/usr/bin/env bash
echo "docker $*" >> "{calls}"
case "$*" in
  *pg_dump*) echo "DUMP-CONTENT" ;;
  *"builder prune"*) echo "Deleted build cache objects:"; printf 'Total:\\t1.5GB\\n' ;;
  *"image prune"*) echo "Total reclaimed space: 0B" ;;
  *"system df"*) echo "Images=1GB"; echo "Build Cache=0B" ;;
esac
""",
    )
    _write_exec(
        bindir / "curl",
        f"""#!/usr/bin/env bash
echo "curl $*" >> "{calls}"
""",
    )
    (app / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n"
        "BACKUP_KEEP_DAILY=7   # суточных\nBACKUP_KEEP_MONTHLY=12\nBACKUP_DIR_MAX_GB=6\n"
        "REMOTE_BACKUP_ENABLED=true\nREMOTE_BACKUP_REMOTE=gdrive\n"
        "REMOTE_BACKUP_PATH=AgentTrade/backups\nREMOTE_BACKUP_KEEP=3\n",
        encoding="utf-8",
    )
    return {"app": app, "bin": bindir, "calls": calls, "root": tmp_path}


def _run(sandbox: dict[str, Path], script: str, extra_env: dict[str, str] | None = None):
    env = {
        "PATH": f"{sandbox['bin']}:{os.environ['PATH']}",
        "APP_DIR": str(sandbox["app"]),
        "HOME": str(sandbox["root"]),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], env=env, capture_output=True, text=True, check=False
    )


def _names(app: Path) -> list[str]:
    return sorted(p.name for p in (app / "backups").iterdir())


def _touch_backups(app: Path, names: list[str]) -> None:
    for n in names:
        (app / "backups" / n).write_bytes(b"x" * 10)


DAILY_14 = [f"agenttrade_2026-09-{d:02d}.dump.gz" for d in range(16, 30)]


# --- Блок B: хранение копий -------------------------------------------------


def test_b_суточные_хранятся_ровно_keep_daily(sandbox):
    _touch_backups(sandbox["app"], DAILY_14)
    r = _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30"})
    assert r.returncode == 0, r.stdout + r.stderr
    names = _names(sandbox["app"])
    assert len(names) == 7
    assert names[-1] == "agenttrade_2026-09-30.dump.gz"
    assert names[0] == "agenttrade_2026-09-24.dump.gz"  # старше — удалены


def test_b_первого_числа_копия_месячная_и_вне_суточного_счётчика(sandbox):
    _touch_backups(sandbox["app"], [f"agenttrade_2026-09-{d:02d}.dump.gz" for d in range(24, 31)])
    r = _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-10-01"})
    assert r.returncode == 0, r.stdout + r.stderr
    names = _names(sandbox["app"])
    assert "agenttrade_2026-10-01.monthly.dump.gz" in names
    # Суточных по-прежнему 7: месячная в их счёт не входит и ничего не вытеснила.
    daily = [n for n in names if ".monthly." not in n]
    assert len(daily) == 7


def test_b_месячные_режутся_по_своему_счётчику(sandbox):
    months = [f"agenttrade_2025-{m:02d}-01.monthly.dump.gz" for m in range(1, 13)]
    _touch_backups(sandbox["app"], months)
    r = _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-01-01"})
    assert r.returncode == 0, r.stdout + r.stderr
    monthly = [n for n in _names(sandbox["app"]) if ".monthly." in n]
    assert len(monthly) == 12
    assert "agenttrade_2025-01-01.monthly.dump.gz" not in monthly  # самая старая вытеснена
    assert "agenttrade_2026-01-01.monthly.dump.gz" in monthly


def test_b_посторонние_файлы_ротацией_не_затрагиваются(sandbox):
    _touch_backups(sandbox["app"], DAILY_14)
    manual = sandbox["app"] / "backups" / "manual"
    manual.mkdir()
    (manual / "pre_9_4.dump.gz").write_bytes(b"keep")
    _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30"})
    assert (manual / "pre_9_4.dump.gz").exists()


def test_b_ручная_копия_pre_живёт_30_суток_и_потом_удаляется(sandbox):
    _touch_backups(sandbox["app"], DAILY_14)
    manual = sandbox["app"] / "backups" / "manual"
    manual.mkdir()
    fresh, old, other = (manual / "pre_9_4_d_2026-09-29.dump.gz",
                         manual / "pre_9_4_d_2026-08-01.dump.gz", manual / "note.txt")
    for f in (fresh, old, other):
        f.write_bytes(b"x")
    stale = datetime.now().timestamp() - 31 * 86400
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30"})
    assert fresh.exists() and other.exists()      # свежая и посторонний файл целы
    assert not old.exists()                        # старше 30 суток — удалена


@pytest.mark.parametrize("bad", ["0", "abc", ""])
def test_b_испорченная_настройка_ничего_не_удаляет(sandbox, bad):
    _touch_backups(sandbox["app"], DAILY_14)
    r = _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30", "BACKUP_KEEP_DAILY": bad}
             if bad else {"BACKUP_TODAY": "2026-09-30", "BACKUP_KEEP_DAILY": "abc"})
    assert r.returncode == 1
    assert len(_names(sandbox["app"])) == 14  # ни один файл не тронут, дамп не создан


def test_b_превышение_потолка_шлёт_telegram_и_ничего_не_удаляет(sandbox):
    _touch_backups(sandbox["app"], [f"agenttrade_2026-09-{d:02d}.dump.gz" for d in range(24, 29)])
    # Потолок — 1 ГБ, файл на ~1.2 ГБ (разреженный, места не занимает).
    big = sandbox["app"] / "backups" / "agenttrade_2026-09-29.dump.gz"
    with big.open("wb") as fh:
        fh.truncate(int(1.2 * 1024**3))
    r = _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30", "BACKUP_DIR_MAX_GB": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ПРЕВЫШЕНИЕ" in r.stdout
    calls = sandbox["calls"].read_text(encoding="utf-8")
    assert "api.telegram.org" in calls and "1.2" in calls  # фактический объём в сообщении
    assert big.exists()
    assert len(_names(sandbox["app"])) == 7  # ничего сверх правила не удалено


def test_b_без_превышения_telegram_молчит(sandbox):
    _touch_backups(sandbox["app"], DAILY_14)
    _run(sandbox, "backup_db.sh", {"BACKUP_TODAY": "2026-09-30"})
    assert "api.telegram.org" not in sandbox["calls"].read_text(encoding="utf-8")


# --- Блок A: очистка кэша сборки --------------------------------------------


def test_a_docker_gc_вызывает_нужные_команды_без_флага_a(sandbox):
    r = _run(sandbox, "docker_gc.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = sandbox["calls"].read_text(encoding="utf-8")
    assert "docker builder prune -af --filter until=24h" in calls
    assert "docker image prune -f" in calls
    assert "image prune -a" not in calls and "image prune -af" not in calls
    log = (sandbox["app"] / "logs" / "docker_gc.log").read_text(encoding="utf-8")
    assert "Освобождено на диске" in log and "1.5GB" in log


def test_a_docker_gc_в_коде_нет_image_prune_с_флагом_a():
    text = (SCRIPTS / "docker_gc.sh").read_text(encoding="utf-8")
    executable = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("image prune -a" in ln or "image prune -fa" in ln for ln in executable)


def test_a_daemon_json_сливается_а_не_затирается(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(json.dumps({"dns": ["1.1.1.1"], "log-opts": {"labels": "x"}}), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "merge_docker_daemon.py"), str(path)],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["dns"] == ["1.1.1.1"]
    assert data["log-driver"] == "json-file"
    assert data["log-opts"] == {"labels": "x", "max-size": "20m", "max-file": "3"}
    assert list(tmp_path.glob("daemon.json.bak-*"))
    # Повторный запуск — без изменений и без второй резервной копии.
    again = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "merge_docker_daemon.py"), str(path)],
        capture_output=True, text=True, check=False,
    )
    assert "изменений нет" in again.stdout


def test_a_daemon_json_создаётся_если_файла_нет(tmp_path):
    path = tmp_path / "daemon.json"
    subprocess.run([sys.executable, str(ROOT / "deploy" / "merge_docker_daemon.py"), str(path)],
                   capture_output=True, text=True, check=True)
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(
        (ROOT / "deploy" / "docker-daemon.json").read_text(encoding="utf-8")
    )


def test_a_cron_строки_в_установщике_и_порядок_времени():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "0 4 * * 0 ${APP_USER} ${APP_DIR}/scripts/docker_gc.sh" in text
    assert "30 4 * * 0 root ${APP_DIR}/scripts/remote_backup.sh" in text


# --- Блок C: внешняя выгрузка -----------------------------------------------

FAKE_RCLONE = r'''#!/usr/bin/env python3
"""Поддельный rclone: «хранилище» — каталог REMOTE_ROOT, имя хранилища — gdrive."""
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as fh:
    fh.write("rclone " + " ".join(args) + "\n")
root = os.environ["REMOTE_ROOT"]
def resolve(spec):
    remote, _, path = spec.partition(":")
    if remote != "gdrive":
        print(f"Failed to create file system for {spec}: no section in config", file=sys.stderr)
        sys.exit(3)
    return os.path.join(root, path)
cmd = args[0]
if cmd == "copy":
    dest = resolve(args[2]); os.makedirs(dest, exist_ok=True)
    data = open(args[1], "rb").read()
    if os.environ.get("TRUNCATE_REMOTE"):
        data = data[:-1]
    open(os.path.join(dest, os.path.basename(args[1])), "wb").write(data)
elif cmd == "lsjson":
    d = resolve(args[1])
    print(json.dumps([{"Name": n, "Size": os.path.getsize(os.path.join(d, n))}
                      for n in sorted(os.listdir(d))]))
elif cmd == "deletefile":
    spec = args[1]; remote, _, path = spec.partition(":")
    os.remove(resolve(spec))
'''


@pytest.fixture()
def remote(sandbox):
    _write_exec(sandbox["bin"] / "rclone", FAKE_RCLONE)
    remote_root = sandbox["root"] / "cloud"
    (remote_root / "AgentTrade" / "backups").mkdir(parents=True)
    conf = sandbox["root"] / "rclone.conf"
    conf.write_text("[gdrive]\ntype = drive\ntoken = SECRET-TOKEN\n", encoding="utf-8")
    env = {
        "REMOTE_ROOT": str(remote_root),
        "CALLS": str(sandbox["calls"]),
        "RCLONE_CONFIG": str(conf),
    }
    return remote_root / "AgentTrade" / "backups", env


def test_c_успешная_выгрузка_сверка_размера_и_ротация_в_облаке(sandbox, remote):
    cloud, env = remote
    for n in ("agenttrade_2026-09-01.dump.gz", "agenttrade_2026-09-08.dump.gz",
              "agenttrade_2026-09-15.dump.gz"):
        (cloud / n).write_bytes(b"old")
    _touch_backups(sandbox["app"],
                   ["agenttrade_2026-09-28.dump.gz", "agenttrade_2026-09-29.dump.gz"])
    r = _run(sandbox, "remote_backup.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in cloud.iterdir()) == [
        "agenttrade_2026-09-08.dump.gz", "agenttrade_2026-09-15.dump.gz",
        "agenttrade_2026-09-29.dump.gz",
    ]  # KEEP=3: самая старая удалена, выгружена самая свежая
    assert "api.telegram.org" not in sandbox["calls"].read_text(encoding="utf-8")
    status = (sandbox["app"] / "logs" / "remote_backup.status").read_text(encoding="utf-8")
    assert status.split(";")[1] == "ok"
    log_text = (sandbox["app"] / "logs" / "remote_backup.log").read_text(encoding="utf-8")
    assert "SECRET-TOKEN" not in r.stdout + log_text


def test_c_отключено_код_0_и_запись_в_журнал(sandbox, remote):
    _, env = remote
    r = _run(sandbox, "remote_backup.sh", {**env, "REMOTE_BACKUP_ENABLED": "false"})
    assert r.returncode == 0
    log_text = (sandbox["app"] / "logs" / "remote_backup.log").read_text(encoding="utf-8")
    assert "отключена" in log_text
    assert "rclone" not in sandbox["calls"].read_text(encoding="utf-8")


def test_c_несовпадение_размера_неудача_и_telegram(sandbox, remote):
    cloud, env = remote
    _touch_backups(sandbox["app"], ["agenttrade_2026-09-29.dump.gz"])
    r = _run(sandbox, "remote_backup.sh", {**env, "TRUNCATE_REMOTE": "1"})
    assert r.returncode == 1
    calls = sandbox["calls"].read_text(encoding="utf-8")
    assert "api.telegram.org" in calls
    assert "не совпал" in r.stdout


def test_c_ошибочное_хранилище_ненулевой_код_и_telegram(sandbox, remote):
    _, env = remote
    _touch_backups(sandbox["app"], ["agenttrade_2026-09-29.dump.gz"])
    r = _run(sandbox, "remote_backup.sh", {**env, "REMOTE_BACKUP_REMOTE": "net_takogo"})
    assert r.returncode != 0
    assert "api.telegram.org" in sandbox["calls"].read_text(encoding="utf-8")
    status = (sandbox["app"] / "logs" / "remote_backup.status").read_text(encoding="utf-8")
    assert status.split(";")[1] == "fail"


def test_c_нет_локальных_копий_неудача(sandbox, remote):
    _, env = remote
    r = _run(sandbox, "remote_backup.sh", env)
    assert r.returncode == 1
    assert "нет ни одной копии" in r.stdout


def test_c_неудача_не_удаляет_старые_в_облаке(sandbox, remote):
    cloud, env = remote
    for i in range(1, 6):
        (cloud / f"agenttrade_2026-08-0{i}.dump.gz").write_bytes(b"old")
    _touch_backups(sandbox["app"], ["agenttrade_2026-09-29.dump.gz"])
    r = _run(sandbox, "remote_backup.sh", {**env, "TRUNCATE_REMOTE": "1"})
    assert r.returncode == 1
    assert len([p for p in cloud.iterdir() if p.name.startswith("agenttrade_2026-08")]) == 5


def test_c_rclone_закреплён_версией_и_суммой():
    text = (ROOT / "deploy" / "install_rclone.sh").read_text(encoding="utf-8")
    assert 'RCLONE_VERSION="v1.75.1"' in text
    sha = "982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"
    assert f'RCLONE_SHA256="{sha}"' in text
    assert "apt-get install -y -qq rclone" not in text


# --- Блок E: прогноз диска и два порога вотчдога ----------------------------


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def forecast():
    from src.health import disk_forecast
    return disk_forecast


def _records(forecast, start_used: float, step: float, n: int):
    return [
        forecast.DiskRecord(date(2026, 9, 1 + i), start_used + step * i, 50.0) for i in range(n)
    ]


def test_e_меньше_семи_записей_недостаточно_данных(forecast):
    recs = _records(forecast, 20.0, 0.5, 6)
    assert forecast.days_to_threshold(recs, 38.0, 15.0) is None
    assert forecast.describe(None, 15.0) == "недостаточно данных"


def test_e_прирост_и_срок_до_порога(forecast):
    recs = _records(forecast, 20.0, 0.5, 8)   # к 8-й записи занято 23.5 ГБ, прирост 0.5/сут
    # Порог 15% свободных при 40 ГБ = занято 34 ГБ; осталось 10.5 ГБ / 0.5 = 21 сутки.
    days = forecast.days_to_threshold(recs, 40.0, 15.0)
    assert days == pytest.approx(21.0)
    assert forecast.describe(days, 15.0) == "до порога 15% осталось 21 суток"


def test_e_берутся_последние_14_записей(forecast):
    old = _records(forecast, 0.0, 5.0, 10)                     # давний бурный рост
    recent = [forecast.DiskRecord(date(2026, 10, 1 + i), 30.0, 50.0) for i in range(14)]
    growth = forecast.daily_growth_gb(old + recent)
    assert growth == 0.0                                       # старые записи не влияют


def test_e_пропущенные_сутки_не_завышают_прирост(forecast):
    recs = [forecast.DiskRecord(date(2026, 9, d), 20.0 + (d - 1) * 1.0, 50.0)
            for d in (1, 2, 3, 4, 5, 6, 11)]                   # 7 записей за 10 суток
    assert forecast.daily_growth_gb(recs) == pytest.approx(1.0)


def test_e_нет_прироста_и_порог_пройден(forecast):
    flat = _records(forecast, 20.0, 0.0, 8)
    assert forecast.days_to_threshold(flat, 40.0, 15.0) == float("inf")
    assert "прироста нет" in forecast.describe(float("inf"), 15.0)
    full = _records(forecast, 35.0, 0.1, 8)
    assert forecast.days_to_threshold(full, 40.0, 15.0) == 0.0


def test_e_csv_дописывается_раз_в_сутки(forecast, tmp_path):
    path = str(tmp_path / "logs" / "disk_usage.csv")
    assert forecast.append_daily(path, date(2026, 9, 29), 21.5, 43.3) is True
    assert forecast.append_daily(path, date(2026, 9, 29), 21.6, 43.2) is False
    assert forecast.append_daily(path, date(2026, 9, 30), 21.7, 43.1) is True
    assert open(path, encoding="utf-8").read().splitlines() == [
        "2026-09-29;21.50;43.30", "2026-09-30;21.70;43.10"]


def test_e_мусорные_строки_csv_пропускаются(forecast, tmp_path):
    path = tmp_path / "d.csv"
    path.write_text("мусор\n2026-09-29;21.5;43\n;;\n2026-09-30;x;1\n", encoding="utf-8")
    assert len(forecast.read_records(str(path))) == 1


@pytest.fixture()
def watchdog(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHDOG_DISK_CRIT_PCT", "15")
    monkeypatch.setenv("WATCHDOG_DISK_WARN_PCT", "25")
    return _load(SCRIPTS / "watchdog.py", "watchdog_under_test")


def test_e_два_порога_из_окружения(watchdog):
    assert watchdog.DISK_CRIT_PCT == 15.0 and watchdog.DISK_WARN_PCT == 25.0


def test_e_запасное_старое_имя_порога_crit(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    monkeypatch.delenv("WATCHDOG_DISK_CRIT_PCT", raising=False)
    monkeypatch.setenv("WATCHDOG_DISK_MIN_FREE_PCT", "12")
    mod = _load(SCRIPTS / "watchdog.py", "watchdog_legacy")
    assert mod.DISK_CRIT_PCT == 12.0


def test_e_комментарий_в_env_не_ломает_порог(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("WATCHDOG_DISK_WARN_PCT=30   # раннее\n", encoding="utf-8")
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    monkeypatch.delenv("WATCHDOG_DISK_WARN_PCT", raising=False)
    mod = _load(SCRIPTS / "watchdog.py", "watchdog_comment")
    assert mod.DISK_WARN_PCT == 30.0


def test_e_предупреждение_и_тревога_разные_ключи_и_ttl(watchdog, monkeypatch):
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(watchdog, "_alert_allowed",
                        lambda reason, up, ttl=3600: seen.append((reason, ttl)) or True)
    warn = watchdog.disk_alerts(20.0, "недостаточно данных", True)
    crit = watchdog.disk_alerts(10.0, "недостаточно данных", True)
    ok = watchdog.disk_alerts(60.0, "недостаточно данных", True)
    assert len(warn) == 1
    assert "предупреждение при 25%" in warn[0] and "недостаточно данных" in warn[0]
    assert len(crit) == 1 and "порог 15%" in crit[0]
    assert ok == []
    assert seen == [("disk_warn", 86400), ("disk", 3600)]


def test_e_предупреждение_подавляется_антиспамом(watchdog, monkeypatch):
    monkeypatch.setattr(watchdog, "_alert_allowed", lambda *a, **k: False)
    assert watchdog.disk_alerts(20.0, "x", True) == []


def test_e_ниже_crit_предупреждение_не_дублируется(watchdog, monkeypatch):
    keys: list[str] = []
    monkeypatch.setattr(watchdog, "_alert_allowed",
                        lambda reason, up, ttl=3600: keys.append(reason) or True)
    watchdog.disk_alerts(5.0, "x", True)
    assert keys == ["disk"]


def test_e_оценка_в_сообщении_по_csv(watchdog, tmp_path):
    csv = tmp_path / "disk_usage.csv"
    csv.write_text(
        "".join(f"2026-09-{d:02d};{20 + 0.5 * (d - 1):.2f};50.00\n" for d in range(1, 9)),
        encoding="utf-8")
    text = watchdog.disk_forecast_text(23.5, 41.25, 15.0, str(csv))   # ёмкость 40 ГБ
    assert text == "до порога 15% осталось 21 суток"


def test_e_суточная_сводка_несёт_ту_же_оценку(monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "disk_usage.csv").write_text(
        "".join(f"2026-09-{d:02d};{20 + 0.5 * (d - 1):.2f};41.25\n" for d in range(1, 9)),
        encoding="utf-8")
    monkeypatch.setenv("APP_DIR", str(tmp_path))
    from src.health import daily_report
    monkeypatch.setattr(daily_report, "APP_DIR", str(tmp_path))
    assert "до порога 15% осталось 21 суток" in daily_report._disk_forecast_line()
    (tmp_path / "logs" / "disk_usage.csv").write_text("2026-09-01;20.00;50.00\n", encoding="utf-8")
    assert "недостаточно данных" in daily_report._disk_forecast_line()


def test_e_строка_сводки_о_внешней_копии(monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    from src.health import daily_report
    monkeypatch.setattr(daily_report, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(daily_report, "ENV", {})
    assert "ещё не выгружалась" in daily_report._remote_backup_line()
    today = date.today().isoformat()
    (tmp_path / "logs" / "remote_backup.status").write_text(
        f"{today};ok;agenttrade_2026-09-27.dump.gz;{600 * 1024 * 1024};x\n", encoding="utf-8")
    line = daily_report._remote_backup_line()
    assert "🟢" in line and "600 МБ" in line
    (tmp_path / "logs" / "remote_backup.status").write_text(
        f"{today};fail;;0;размер не совпал\n", encoding="utf-8")
    assert "НЕ удалась" in daily_report._remote_backup_line()


# --- Границы этапа ----------------------------------------------------------


def test_граница_этап_не_трогает_сигнальный_код():
    """Каталоги src/agents, src/decision, src/positions этим этапом не меняются."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    forbidden = [f for f in out if f.startswith(("src/agents/", "src/decision/", "src/positions/"))]
    assert forbidden == []


# --- Блок A: журнал systemd; блок D: проверка снятия писателей ---------------


def _journald(tmp_path: Path, text: str) -> tuple[Path, subprocess.CompletedProcess]:
    conf = tmp_path / "journald.conf"
    conf.write_text(text, encoding="utf-8")
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "journald_limit.sh")],
        env={**os.environ, "JOURNALD_CONF": str(conf), "NO_RESTART": "1"},
        capture_output=True, text=True, check=False)
    return conf, res


def test_a_journald_закомментированная_строка_заменяется(tmp_path):
    conf, res = _journald(tmp_path, "[Journal]\n#SystemMaxUse=\n#Storage=auto\n")
    assert res.returncode == 0, res.stderr
    assert conf.read_text(encoding="utf-8").splitlines() == [
        "[Journal]", "SystemMaxUse=200M", "#Storage=auto"]
    assert list(tmp_path.glob("journald.conf.bak-*"))


def test_a_journald_чужое_значение_заменяется_и_повторный_запуск_ничего_не_меняет(tmp_path):
    conf, _ = _journald(tmp_path, "[Journal]\nSystemMaxUse=1G\nSystemMaxUse=500M\n")
    text = conf.read_text(encoding="utf-8")
    assert text.count("SystemMaxUse=200M") == 1
    assert "\nSystemMaxUse=500M" not in text                # лишнее активное — закомментировано
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "journald_limit.sh")],
        env={**os.environ, "JOURNALD_CONF": str(conf), "NO_RESTART": "1"},
        capture_output=True, text=True, check=False)
    assert "изменений нет" in res.stdout and conf.read_text(encoding="utf-8") == text


def test_a_journald_без_строки_добавляется_в_раздел_journal(tmp_path):
    conf, _ = _journald(tmp_path, "[Journal]\nStorage=persistent\n")
    assert conf.read_text(encoding="utf-8").splitlines()[:2] == ["[Journal]", "SystemMaxUse=200M"]
    other = tmp_path / "sub"
    other.mkdir()
    conf2, _ = _journald(other, "# пусто\n")
    assert conf2.read_text(encoding="utf-8").endswith("[Journal]\nSystemMaxUse=200M\n")


def _check(tmp_path: Path, files: dict[str, str]):
    d = tmp_path / "crons"
    d.mkdir(exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / "check_writers_off.sh")],
        env={**os.environ, "CHECK_DIRS": str(d), "CHECK_ALLOW_NONROOT": "1",
             "CHECK_SKIP_SYSTEM": "1"}, capture_output=True, text=True, check=False)


def test_d_проверка_писателей_закомментированное_не_считается(tmp_path):
    res = _check(tmp_path, {"agent-trade": "# 40 4 * * * agent python -m src.trailing_main\n"
                                           "  # 25 4 * * * agent python -m src.baseline_main\n"})
    assert res.returncode == 0 and "OK" in res.stdout


def test_d_проверка_писателей_находит_активную_строку_в_личном_crontab(tmp_path):
    res = _check(tmp_path, {"agent-trade": "# trailing_main выключен\n",
                            "root": "25 4 * * * cd /x && python -m src.baseline_main\n"})
    assert res.returncode == 1
    assert "АКТИВНЫЕ" in res.stdout and "baseline_main" in res.stdout and "root:1:" in res.stdout


def test_d_проверка_писателей_требует_root_иначе_ложное_ok():
    res = subprocess.run(
        ["bash", str(ROOT / "deploy" / "check_writers_off.sh")],
        env={k: v for k, v in os.environ.items() if k != "CHECK_ALLOW_NONROOT"},
        capture_output=True, text=True, check=False)
    if os.geteuid() != 0:
        assert res.returncode == 2
