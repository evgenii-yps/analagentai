"""Exercise imports and a real fixture build in a fresh guarded interpreter."""

import os
import subprocess
import sys


def test_fresh_import_and_build_without_side_effects():
    code = r"""
import os, sys, threading, sched
before_env = dict(os.environ)
before_threads = set(threading.enumerate())
def audit(event, args):
    if event.startswith(("socket.", "os.exec", "os.spawn")) or event in {
        "subprocess.Popen", "os.system", "os.fork", "ctypes.dlopen"
    }:
        raise AssertionError("forbidden side effect: " + event)
sys.addaudithook(audit)
def deny_schedule(*args, **kwargs):
    raise AssertionError("background scheduling forbidden")
threading.Thread.start = deny_schedule
sched.scheduler.enter = deny_schedule
sched.scheduler.enterabs = deny_schedule
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from src.v9.contracts import Identity, MarketType, Observation, Window
from src.v9.quality import FixturePolicy, InputContext, assess
from src.v9.profiles import Measurement, build_profile
import src.v9.registry, src.v9.dependencies, src.v9.lifecycle
from src.v9.offline_data import Coverage, RawField, Stream
from src.v9.offline_replay import (Checkpoint, RepairEvidence, checkpoint_bytes,
                                   evaluate_replays, load_checkpoint, resume, sample_replay)
from src.v9.dependencies import Graph, Node
from dataclasses import replace
now = datetime(2026, 1, 2, tzinfo=UTC)
w = Window(now - timedelta(minutes=1), now)
i = Identity("fixture", "1", MarketType.SPOT, "USD", "1")
p = FixturePolicy("fixture", "1", False, True, True, None)
o = Observation(i, "fixture", "input", Decimal(1), "USD", None, w.start, w.start,
                now, now, w, "spot_price")
stream = Stream(i, "spot_price", "fixture")
raw = RawField(stream, "event", "input", "price", Decimal(1), "USD", w.start,
               w.start, now, None, "buy", True, None)
cov = Coverage(stream, w, "verified_complete", "independently_verified", "proof", now)
manifest, result = sample_replay(stream=stream, window=w, coverage=cov, records=(raw,),
    cutoff=now, generated_at=now, schema_version="1", calculation_version="1",
    policy=p, unit="USD", field="price", require_closed=True)
assert manifest.eligible and result
graph = Graph("1", (Node("source"), Node("target", ("source",))))
assert evaluate_replays(graph, "target", {"source": manifest, "target": manifest}, ())[0]
cp = Checkpoint(stream, w.start, "1", None, ("proof",), ())
plan = resume(load_checkpoint(checkpoint_bytes(cp)), cov, (raw,), now, cursor_supported=True)
assert plan.new_records
conflict = resume(cp, cov, (raw, replace(raw, raw_value=Decimal(2))), now, cursor_supported=True)
restored = load_checkpoint(checkpoint_bytes(conflict.checkpoint))
assert resume(restored, cov, (), now, cursor_supported=True).checkpoint.unresolved
later = now + timedelta(seconds=1)
proof = replace(cov, evidence_ref="repair-proof", available_at=later)
fix = RepairEvidence(raw.source_id, w, raw, proof, "repair", later)
repaired = resume(restored, proof, (), later, cursor_supported=True, repairs=(fix,))
assert not repaired.checkpoint.unresolved
q = assess((o,), coverage=True, policy=p, evaluated_at=now, window=w,
           evidence_refs=("coverage",), context=InputContext(i, "spot_price", "USD"))
m = Measurement("price", Decimal(1), w, q, ("input",), now, now, (now,), "closed")
build_profile(enabled=True, identity=i, mapping_ref="mapping", schema_version="1",
    policy_version="1", profile_version="1", calculation_version="1", data_cutoff=now,
    evaluated_at=now, applicable_from=now, expires_at=now+timedelta(days=1), sufficiency="ok",
    provenance=("input",), measurements=(m,), policy=p)
assert dict(os.environ) == before_env
assert set(threading.enumerate()) == before_threads
assert not any(n.startswith(("asyncpg", "psycopg", "ccxt", "src.core.db", "src.core.config",
                             "src.demo", "src.execution")) for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert result.returncode == 0, result.stderr
