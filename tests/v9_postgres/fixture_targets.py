"""Conservative validation for destructive disposable-database drills."""

import os
import re
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path


def validate_targets(source, restore, environ=None):
    """Validate both endpoints before any connection; forbid libpq ambient settings."""
    from psycopg.conninfo import conninfo_to_dict

    env = os.environ if environ is None else environ
    if any(key.upper().startswith("PG") for key in env):
        raise ValueError("inherited libpq PG environment settings are forbidden")
    targets = []
    allowed = {"host", "port", "dbname", "user", "password"}
    for name, dsn in (("source", source), ("restore", restore)):
        if not dsn:
            raise ValueError("both explicit test DSNs are required")
        params = conninfo_to_dict(dsn)
        if set(params) - allowed:
            raise ValueError("alternate libpq connection settings are forbidden")
        port = params.get("port", "")
        if (
            params.get("host") != "127.0.0.1"
            or params.get("dbname") != "stage1b2_" + name
            or not params.get("user")
            or not port.isascii()
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError("expected explicit loopback disposable test database")
        # Pin libpq's actual network address, regardless of DNS or later environment changes.
        targets.append(params | {"hostaddr": "127.0.0.1"})
    if targets[0]["port"] != targets[1]["port"] or targets[0]["dbname"] == targets[1]["dbname"]:
        raise ValueError("distinct databases on one disposable server are required")
    return tuple(targets)


def _schema_oid(conn):
    row = conn.execute("SELECT oid FROM pg_namespace WHERE nspname = 'stage1b2'").fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True)
class FixtureInstance:
    source_dsn: str
    restore_dsn: str
    container_id: str


def _docker(*args, check=True):
    if args and args[0] == "rm" and not re.fullmatch(r"[0-9a-f]{64}", args[-1]):
        raise ValueError("refusing cleanup without a full validated container ID")
    # Pin the managed local daemon; never inherit a caller's remote Docker context.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    return subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


@contextmanager
def disposable_instance():
    """Create and destroy one isolated server, addressing it only by its captured ID."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    with tempfile.TemporaryDirectory(prefix="stage1b2-cid-") as directory:
        cidfile = Path(directory) / "container.id"
        container_id = None
        try:
            # The cidfile also identifies a container if create reports a partial failure.
            _docker(
                "create",
                "--cidfile",
                str(cidfile),
                "--pull=never",
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_PASSWORD=fixture-only",
                "postgres:16-alpine",
            )
            candidate = cidfile.read_text().strip()
            if not re.fullmatch(r"[0-9a-f]{64}", candidate):
                raise RuntimeError("invalid Docker container ID")
            container_id = candidate
            _docker("start", container_id)
            endpoint = _docker("port", container_id, "5432/tcp").stdout.strip()
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", endpoint)
            if not match:
                raise RuntimeError("unexpected fixture endpoint")
            params = dict(
                host="127.0.0.1", port=match.group(1), user="postgres", password="fixture-only"
            )
            deadline = time.monotonic() + 45
            while True:
                try:
                    admin = psycopg.connect(
                        **params,
                        hostaddr="127.0.0.1",
                        dbname="postgres",
                        connect_timeout=1,
                        autocommit=True,
                    )
                    break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            with admin:
                admin.execute("CREATE DATABASE stage1b2_source")
                admin.execute("CREATE DATABASE stage1b2_restore")
            yield FixtureInstance(
                make_conninfo(**params, hostaddr="127.0.0.1", dbname="stage1b2_source"),
                make_conninfo(**params, hostaddr="127.0.0.1", dbname="stage1b2_restore"),
                container_id,
            )
        finally:
            if container_id is None and cidfile.exists():
                candidate = cidfile.read_text().strip()
                if re.fullmatch(r"[0-9a-f]{64}", candidate):
                    container_id = candidate
            if container_id:
                # -v deletes only anonymous volumes belonging to this container.
                _docker("rm", "--force", "--volumes", container_id)


@contextmanager
def connect_targets(source, restore):
    """Read-only preflight of both caller targets, then use a fresh owned server."""
    import psycopg

    targets = validate_targets(source, restore)
    with ExitStack() as stack:
        connections = tuple(
            stack.enter_context(psycopg.connect(**target, autocommit=True)) for target in targets
        )
        existing = tuple(_schema_oid(conn) for conn in connections)
        if any(oid is not None for oid in existing):
            raise ValueError("stage1b2 schema already exists on a fixture target")
    with disposable_instance() as instance:
        with ExitStack() as stack:
            a = stack.enter_context(psycopg.connect(instance.source_dsn, autocommit=True))
            b = stack.enter_context(psycopg.connect(instance.restore_dsn, autocommit=True))
            yield a, b, instance
