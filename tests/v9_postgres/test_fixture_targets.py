"""Real PostgreSQL checks for read-only preflight and owned-container cleanup."""

import os

import fixture_targets
import pytest
from fixture_targets import connect_targets, disposable_instance

from src.v9.durable import DDL, initialize

try:
    import psycopg
except ImportError:
    psycopg = None

pytestmark = pytest.mark.skipif(psycopg is None, reason="optional PostgreSQL dependency missing")


def dsns():
    source, restore = os.environ.get("STAGE1B2_SOURCE"), os.environ.get("STAGE1B2_RESTORE")
    if not source and not restore:
        pytest.skip("explicit disposable PostgreSQL DSNs required")
    return source, restore


def preflight_dsn(pinned_dsn):
    # Caller validation deliberately forbids a supplied hostaddr.
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(pinned_dsn)
    del params["hostaddr"]
    return make_conninfo(**params)


def schema_oid(conn):
    return conn.execute("SELECT oid FROM pg_namespace WHERE nspname = 'stage1b2'").fetchone()


def removed(container_id):
    return fixture_targets._docker("inspect", container_id, check=False).returncode != 0


@pytest.mark.parametrize("occupied", ["source", "restore"])
def test_unverified_schema_rejects_pair_without_mutation(occupied):
    dsns()
    # This server is itself owned by this test; no caller-owned schema is changed.
    with disposable_instance() as server:
        with (
            psycopg.connect(server.source_dsn, autocommit=True) as a,
            psycopg.connect(server.restore_dsn, autocommit=True) as b,
        ):
            existing, other = (a, b) if occupied == "source" else (b, a)
            existing.execute("CREATE SCHEMA stage1b2")
            existing.execute("CREATE TABLE stage1b2.sentinel(value text NOT NULL)")
            existing.execute("INSERT INTO stage1b2.sentinel VALUES ('retain-me')")
            oid = schema_oid(existing)
            before = other.execute("SELECT oid, nspname FROM pg_namespace ORDER BY oid").fetchall()
            with pytest.raises(ValueError, match="schema.*exists"):
                with connect_targets(
                    preflight_dsn(server.source_dsn), preflight_dsn(server.restore_dsn)
                ):
                    pytest.fail("existing schema must reject both targets")
            assert schema_oid(other) is None
            assert (
                other.execute("SELECT oid, nspname FROM pg_namespace ORDER BY oid").fetchall()
                == before
            )
            assert schema_oid(existing) == oid
            assert existing.execute("SELECT value FROM stage1b2.sentinel").fetchall() == [
                ("retain-me",)
            ]
    assert removed(server.container_id)


@pytest.mark.parametrize("create_restore", [False, True])
def test_created_server_cleaned_after_partial_setup_failure(create_restore):
    source, restore = dsns()
    with pytest.raises(RuntimeError, match="setup failure"):
        with connect_targets(source, restore) as (a, b, instance):
            initialize(a)
            if create_restore:
                initialize(b)
            raise RuntimeError("setup failure")
    assert removed(instance.container_id)
    with connect_targets(source, restore) as (a, b, following):
        assert following.container_id != instance.container_id
        assert schema_oid(a) is None and schema_oid(b) is None
    assert removed(following.container_id)


def test_replacement_during_cleanup_survives(monkeypatch):
    dsns()
    with disposable_instance() as caller:
        with psycopg.connect(caller.source_dsn, autocommit=True) as source:
            with connect_targets(
                preflight_dsn(caller.source_dsn), preflight_dsn(caller.restore_dsn)
            ) as (
                a,
                _,
                worker,
            ):
                initialize(a)
                # A schema appears after read-only preflight; it belongs to the caller.
                source.execute("CREATE SCHEMA stage1b2")
                old_oid = schema_oid(source)
                real_run = fixture_targets.subprocess.run
                injected = []

                def replace_on_cleanup(command, *args, **kwargs):
                    if "rm" in command and worker.container_id in command and not injected:
                        source.execute("ALTER SCHEMA stage1b2 RENAME TO old_stage1b2")
                        source.execute("CREATE SCHEMA stage1b2")
                        source.execute("CREATE TABLE stage1b2.sentinel(value text)")
                        source.execute("INSERT INTO stage1b2.sentinel VALUES ('replacement')")
                        injected.append(schema_oid(source))
                    return real_run(command, *args, **kwargs)

                monkeypatch.setattr(fixture_targets.subprocess, "run", replace_on_cleanup)
            assert injected and injected[0] != old_oid
            assert schema_oid(source) == injected[0]
            assert source.execute("SELECT value FROM stage1b2.sentinel").fetchall() == [
                ("replacement",)
            ]
            assert removed(worker.container_id)
        assert not removed(caller.container_id)
    assert removed(caller.container_id)


def test_rolled_back_schema_creation_leaves_both_targets_empty():
    source, restore = dsns()
    with connect_targets(source, restore) as (a, b, instance):
        with pytest.raises(RuntimeError, match="rollback fixture"):
            with a.transaction():
                a.execute(DDL)
                assert schema_oid(a) is not None
                raise RuntimeError("rollback fixture")
        assert schema_oid(a) is None and schema_oid(b) is None
    assert removed(instance.container_id)


def test_container_removed_when_start_fails(monkeypatch):
    dsns()
    real_docker = fixture_targets._docker
    created = []

    def fail_start(*args, **kwargs):
        if args[0] == "start":
            created.append(args[1])
            raise RuntimeError("injected start failure")
        return real_docker(*args, **kwargs)

    monkeypatch.setattr(fixture_targets, "_docker", fail_start)
    with pytest.raises(RuntimeError, match="injected start failure"):
        with disposable_instance():
            pytest.fail("server start must fail")
    assert len(created) == 1 and removed(created[0])


def test_malformed_cidfile_never_triggers_cleanup(monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            from pathlib import Path

            Path(args[args.index("--cidfile") + 1]).write_text("not-a-valid-container-id")

    monkeypatch.setattr(fixture_targets, "_docker", fake_docker)
    with pytest.raises(RuntimeError, match="invalid Docker container ID"):
        with disposable_instance():
            pytest.fail("malformed ID must not be accepted")
    assert [args[0] for args in calls] == ["create"]


def test_docker_rm_rejects_unvalidated_id(monkeypatch):
    monkeypatch.setattr(
        fixture_targets.subprocess, "run", lambda *a, **k: pytest.fail("docker must not run")
    )
    with pytest.raises(ValueError, match="validated container ID"):
        fixture_targets._docker("rm", "--force", "--volumes", "not-an-id")


def test_container_removed_when_create_partially_fails(monkeypatch):
    dsns()
    real_docker = fixture_targets._docker
    created = []

    def fail_after_create(*args, **kwargs):
        result = real_docker(*args, **kwargs)
        if args[0] == "create":
            from pathlib import Path

            created.append(Path(args[args.index("--cidfile") + 1]).read_text().strip())
            raise RuntimeError("injected partial create failure")
        return result

    monkeypatch.setattr(fixture_targets, "_docker", fail_after_create)
    with pytest.raises(RuntimeError, match="injected partial create failure"):
        with disposable_instance():
            pytest.fail("creation must fail")
    assert len(created) == 1 and removed(created[0])


def test_ambient_hostaddr_rejected_before_preflight_side_effects(monkeypatch):
    source, restore = dsns()
    monkeypatch.setenv("PGHOSTADDR", "127.0.0.2")
    monkeypatch.setattr(
        fixture_targets, "_docker", lambda *a, **k: pytest.fail("docker must not run")
    )
    with pytest.raises(ValueError, match="PG environment"):
        with connect_targets(source, restore):
            pytest.fail("ambient libpq setting must be rejected")


def test_owned_connections_pin_hostaddr_after_preflight(monkeypatch):
    import subprocess
    import sys
    from contextlib import contextmanager

    source, restore = dsns()
    original = fixture_targets.disposable_instance

    @contextmanager
    def inject_after_preflight():
        # A late environment change must not redirect any worker connection.
        monkeypatch.setenv("PGHOSTADDR", "127.0.0.2")
        with original() as instance:
            yield instance

    monkeypatch.setattr(fixture_targets, "disposable_instance", inject_after_preflight)
    with connect_targets(source, restore) as (a, b, instance):
        assert a.info.hostaddr == b.info.hostaddr == "127.0.0.1"
        with psycopg.connect(instance.source_dsn, autocommit=True) as direct:
            assert direct.info.hostaddr == "127.0.0.1"
            assert direct.execute("SELECT 1").fetchone() == (1,)
        code = (
            "import os, psycopg; "
            "c=psycopg.connect(os.environ['TEST_SOURCE']); "
            "print(c.info.hostaddr); c.close()"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=dict(os.environ, TEST_SOURCE=instance.source_dsn),
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "127.0.0.1"
