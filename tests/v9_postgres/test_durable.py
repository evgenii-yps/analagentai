"""Explicit opt-in tests: only two local disposable database names are accepted."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from fixture_targets import connect_targets
from test_emitted_repair import make_scenario, repair
from test_offline_foundation import NOW, POLICY, STREAM, WINDOW, coverage, field, replay

from src.v9.contracts import Identity, InputContext, Mapping, MarketType, Observation, Window
from src.v9.durable import (
    PostgresPublisher,
    StaleGeneration,
    archive,
    dumps,
    get_evidence,
    initialize,
    loads,
    put_evidence,
    restore,
    sha,
)
from src.v9.offline_data import Revision
from src.v9.offline_replay import resume
from src.v9.profiles import Measurement, build_profile
from src.v9.quality import assess

try:
    import psycopg
except ImportError:
    psycopg = None

pytestmark = pytest.mark.skipif(psycopg is None, reason="optional PostgreSQL dependency missing")


@pytest.fixture
def dbs():
    source = os.environ.get("STAGE1B2_SOURCE")
    target = os.environ.get("STAGE1B2_RESTORE")
    if not source and not target:
        pytest.skip("explicit disposable PostgreSQL DSNs required")
    with connect_targets(source, target) as (a, b, instance):
        initialize(a)
        yield a, b, instance.source_dsn, instance.restore_dsn


def profile(version="v1"):
    ident = STREAM.identity
    obs = Observation(
        ident,
        "fixture",
        "input1",
        Decimal("12.3400"),
        "base",
        None,
        WINDOW.start,
        WINDOW.start,
        NOW,
        NOW,
        WINDOW,
        "trade",
    )
    q = assess(
        (obs,),
        coverage=True,
        context=InputContext(ident, "trade", "base"),
        policy=POLICY,
        evaluated_at=NOW,
        window=WINDOW,
        evidence_refs=("proof",),
    )
    m = Measurement(
        "amount", Decimal("12.3400"), WINDOW, q, ("input1",), NOW, NOW, (NOW,), "closed", (q,)
    )
    derivative = Identity(ident.venue, "swap", MarketType.DERIVATIVE, ident.quote_currency, "1")
    mapping = Mapping(
        ident,
        derivative,
        "map",
        "mapping-proof",
        Window(WINDOW.start, NOW + timedelta(days=1)),
        "spot",
        "swap",
    )
    return build_profile(
        enabled=True,
        identity=ident,
        mapping_ref="map",
        schema_version="1",
        policy_version=POLICY.version,
        profile_version=version,
        calculation_version="calc",
        data_cutoff=NOW,
        evaluated_at=NOW,
        applicable_from=NOW,
        expires_at=NOW + timedelta(days=1),
        sufficiency="ok",
        provenance=("input1",),
        measurements=(m,),
        policy=POLICY,
        mappings=(mapping,),
    )


def test_atomic_cas_and_fresh_process(dbs):
    a, _, source, _ = dbs
    pub = PostgresPublisher(a)
    p = profile()
    assert loads(dumps(p)) == p
    assert pub.publish(p, 0) == 1
    with pytest.raises(StaleGeneration):
        pub.publish(profile("stale"), 0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        pub.publish(p, 1)

    def fail():
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        PostgresPublisher(a, after_insert=fail).publish(profile("failed"), 1)
    assert pub.read_current(p.identity) == (1, p)
    assert pub.read_version("stale") is None and pub.read_version("failed") is None
    assert a.execute("select count(*) from stage1b2.versions").fetchone()[0] == 1
    barrier = Barrier(2)

    def writer(version):
        with psycopg.connect(source, hostaddr="127.0.0.1", autocommit=True) as c:
            barrier.wait()
            try:
                return PostgresPublisher(c).publish(profile(version), 1)
            except StaleGeneration:
                return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, ("race1", "race2")))
    assert sorted(map(str, results)) == ["2", "stale"]
    assert a.execute("select count(*) from stage1b2.versions").fetchone()[0] == 2
    winner = pub.read_current(p.identity)[1]
    script = """import os, psycopg
from src.v9.durable import PostgresPublisher
with psycopg.connect(os.environ['TEST_SOURCE'], hostaddr='127.0.0.1', autocommit=True) as c:
 p=PostgresPublisher(c); old=p.read_version('v1'); current=p.read_current(old.identity)
 assert current[0]==2 and current[1].profile_version != old.profile_version
 assert old.measurements[0].value == __import__('decimal').Decimal('12.3400')
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, TEST_SOURCE=source),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert winner.profile_version in ("race1", "race2")
    with pytest.raises(psycopg.errors.RaiseException):
        a.execute("delete from stage1b2.versions where version='v1'")


def test_restore_replay_conflicts_and_corruption(dbs):
    a, b, _, _ = dbs
    pub = PostgresPublisher(a)
    p = profile()
    pub.publish(p, 0)
    pub.publish(profile("v2"), 1)
    raw = field(available_at=WINDOW.start)
    manifest, output = replay((raw,))
    rev1 = Revision(STREAM, WINDOW, "r1", None, Decimal(5), ("input1",), NOW)
    rev2 = Revision(
        STREAM, WINDOW, "r2", "r1", Decimal(6), ("input1", "proof"), NOW + timedelta(seconds=1)
    )
    original, changed, cp = make_scenario(emitted=True)
    for kind, key, value in [
        ("raw", "raw1", raw),
        ("coverage", "coverage", coverage()),
        ("policy", "policy", POLICY),
        ("manifest", "manifest", manifest),
        ("revision", "r1", rev1),
        ("revision", "r2", rev2),
        ("checkpoint", "cp", cp),
    ]:
        put_evidence(a, kind, key, value)
    with pytest.raises(psycopg.errors.UniqueViolation):
        put_evidence(a, "revision", "r1", rev2)
    old = replace(cp, integrity_format="legacy-untracked-v0")
    with pytest.raises(ValueError, match="unsupported checkpoint"):
        put_evidence(a, "checkpoint", "old", old)
    with pytest.raises(ValueError, match="CHECKPOINT_CONFLICT_STATE_MIGRATION_REQUIRED"):
        resume(old, coverage(), (), NOW, cursor_supported=True)
    arc = archive(a)
    for bad in (arc[:-1], json.dumps({"data": {"format": "future"}, "sha256": "bad"}).encode()):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            restore(b, bad)
        assert b.execute("select to_regnamespace('stage1b2')").fetchone()[0] is None
    wrapper = json.loads(arc)
    wrapper["data"]["format"] = "future"
    wrapper["sha256"] = sha(
        json.dumps(
            wrapper["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    with pytest.raises(ValueError, match="unsupported archive"):
        restore(b, json.dumps(wrapper).encode())
    assert b.execute("select to_regnamespace('stage1b2')").fetchone()[0] is None
    # Valid checksums but duplicate keys fail inside the restore transaction, after DDL.
    wrapper = json.loads(arc)
    wrapper["data"]["evidence"].append(wrapper["data"]["evidence"][0])
    wrapper["sha256"] = sha(
        json.dumps(
            wrapper["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        restore(b, json.dumps(wrapper).encode())
    assert b.execute("select to_regnamespace('stage1b2')").fetchone()[0] is None
    restore(b, arc)
    assert archive(b) == arc
    restored = PostgresPublisher(b)
    assert restored.read_current(p.identity) == (2, profile("v2"))
    assert restored.read_version("v1") == p
    assert get_evidence(b, "revision", "r1") == rev1
    assert get_evidence(b, "revision", "r2") == rev2
    manifest2, output2 = replay(
        (get_evidence(b, "raw", "raw1"),),
        coverage=get_evidence(b, "coverage", "coverage"),
        policy=get_evidence(b, "policy", "policy"),
    )
    assert output2 == output and manifest2.content_digest == manifest.content_digest
    assert get_evidence(b, "manifest", "manifest") == manifest
    cp2 = get_evidence(b, "checkpoint", "cp")
    assert cp2 == cp and cp2.unresolved
    later, proof, repair_evidence = repair(changed)
    with pytest.raises(ValueError, match="EMITTED_RECORD_CORRECTION_REQUIRED"):
        resume(cp2, proof, (), later, cursor_supported=True, repairs=(repair_evidence,))
    assert get_evidence(b, "checkpoint", "cp") == cp
    assert original.source_id in dict(cp2.seen)


@pytest.mark.parametrize(
    ("source", "target", "ambient"),
    [
        ("host=127.0.0.1 hostaddr=192.0.2.1 port=5432 dbname=stage1b2_source user=x", "ok", {}),
        ("ok", "host=127.0.0.1 hostaddr=127.0.0.1 port=5432 dbname=stage1b2_restore user=x", {}),
        ("ok", "ok", {"PGHOSTADDR": "192.0.2.1"}),
        ("ok", "ok", {"PGSERVICE": "unsafe"}),
        ("ok", "ok", {"PGOPTIONS": "-c search_path=public"}),
        ("host=127.0.0.1 port=5432 dbname=stage1b2_source user=x service=bad", "ok", {}),
        ("host=127.0.0.1 port=5432 dbname=stage1b2_source user=x options=-cfoo", "ok", {}),
        ("host=localhost port=5432 dbname=stage1b2_source user=x", "ok", {}),
        ("ok", "host=127.0.0.1 port=5432 dbname=stage1b2_source user=x", {}),
        ("ok", "host=127.0.0.1 port=5433 dbname=stage1b2_restore user=x", {}),
        ("host=127.0.0.1 port=5432,5433 dbname=stage1b2_source user=x", "ok", {}),
        ("postgresql://x@127.0.0.1:5432/stage1b2_source?hostaddr=192.0.2.2", "ok", {}),
    ],
)
def test_unsafe_targets_never_connect_or_execute_sql(monkeypatch, source, target, ambient):
    for key in list(os.environ):
        if key.upper().startswith("PG"):
            monkeypatch.delenv(key)
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    source_ok = "host=127.0.0.1 port=5432 dbname=stage1b2_source user=x"
    target_ok = "host=127.0.0.1 port=5432 dbname=stage1b2_restore user=x"
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    with pytest.raises((ValueError, psycopg.Error)):
        with connect_targets(
            source_ok if source == "ok" else source, target_ok if target == "ok" else target
        ):
            pytest.fail("unsafe endpoint accepted")
    assert calls == []  # Neither a connection nor destructive SQL can be issued.


def test_owned_transactions_require_idle_and_commit_independently(dbs):
    a, b, source, target = dbs
    pub = PostgresPublisher(a)
    p = profile()
    with a.transaction():
        with pytest.raises(ValueError, match="idle connection required"):
            pub.publish(p, 0)
        with pytest.raises(ValueError, match="idle connection required"):
            put_evidence(a, "raw", "raw", field())
        with pytest.raises(ValueError, match="idle connection required"):
            archive(a)
    with psycopg.connect(source, hostaddr="127.0.0.1", autocommit=True) as independent:
        assert PostgresPublisher(independent).read_version("v1") is None
        assert pub.publish(p, 0) == 1
        assert PostgresPublisher(independent).read_current(p.identity) == (1, p)
    data = archive(a)
    with b.transaction():
        with pytest.raises(ValueError, match="idle connection required"):
            restore(b, data)
        with pytest.raises(ValueError, match="idle connection required"):
            initialize(b)
    with psycopg.connect(target, hostaddr="127.0.0.1", autocommit=True) as independent:
        assert independent.execute("select to_regnamespace('stage1b2')").fetchone()[0] is None
        restore(b, data)
        assert PostgresPublisher(independent).read_current(p.identity) == (1, p)


@pytest.mark.parametrize("defect", ["missing_pointer", "wrong_generation"])
def test_invalid_archive_inventory_rejected_before_schema(dbs, defect):
    a, b, _, _ = dbs
    pub = PostgresPublisher(a)
    pub.publish(profile(), 0)
    pub.publish(profile("v2"), 1)
    wrapper = json.loads(archive(a))
    if defect == "missing_pointer":
        wrapper["data"]["current"] = []
    else:
        wrapper["data"]["current"][0][1] = 3
    wrapper["sha256"] = sha(
        json.dumps(
            wrapper["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    with pytest.raises(ValueError, match="pointer"):
        restore(b, json.dumps(wrapper).encode())
    assert b.execute("select to_regnamespace('stage1b2')").fetchone()[0] is None


def test_legacy_rollup_keeps_aggregate_after_partial_loss_and_late_trade(dbs):
    """Execute the real legacy SQL only against temporary tables on our owned server."""
    from scripts.retention import rollup_sql

    a, _, _, _ = dbs
    a.execute(
        "CREATE TEMP TABLE trades (instrument_id integer, trade_id text, ts timestamptz, "
        "price double precision, amount double precision, side text)"
    )
    a.execute(
        "CREATE TEMP TABLE trade_flow_1m (instrument_id integer, ts timestamptz, "
        "trades_n integer, buy_volume numeric(30,8), sell_volume numeric(30,8), "
        "buy_n integer, sell_n integer, vwap numeric(20,8), PRIMARY KEY (instrument_id, ts))"
    )
    a.execute(
        "INSERT INTO trades VALUES "
        "(1, 'a', '2020-01-01 00:00:01+00', 100, 1, 'buy'), "
        "(1, 'b', '2020-01-01 00:00:02+00', 110, 2, 'sell')"
    )
    a.execute(rollup_sql())
    original = a.execute("SELECT * FROM trade_flow_1m").fetchall()
    assert len(original) == 1 and original[0][2] == 2
    a.execute("DELETE FROM trades WHERE trade_id = 'a'")
    a.execute(
        "INSERT INTO trades VALUES (1, 'late', '2020-01-01 00:00:03+00', 120, 3, 'buy')"
    )
    a.execute(rollup_sql())
    assert a.execute("SELECT * FROM trade_flow_1m").fetchall() == original
