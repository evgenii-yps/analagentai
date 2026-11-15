from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_offline_foundation import ID, NOW, STREAM, WINDOW, coverage, field, replay

from src.v9.contracts import Lifecycle, Mapping, MarketType, Window
from src.v9.dependencies import Graph, Node
from src.v9.lifecycle import Evidence
from src.v9.offline_data import Stream
from src.v9.offline_replay import (
    Checkpoint,
    MappingRequirement,
    RepairEvidence,
    checkpoint_bytes,
    evaluate_replays,
    lifecycle_for_plan,
    load_checkpoint,
    resume,
)


def test_graph_binds_mapping_to_each_actual_edge():
    spot_a = ID
    spot_b = replace(ID, instrument_id="spot-b")
    swap = replace(ID, instrument_id="swap", market_type=MarketType.DERIVATIVE)
    stream_a = Stream(spot_a, "trade", "fixture")
    stream_b = Stream(spot_b, "trade", "fixture")
    derivative = Stream(swap, "oi", "fixture")
    mapping_a = Mapping(
        spot_a, swap, "v1", "registry", Window(WINDOW.start, NOW + timedelta(days=1)), "a", "swap"
    )
    mapping_b = Mapping(
        spot_b, swap, "v1", "registry", Window(WINDOW.start, NOW + timedelta(days=1)), "b", "swap"
    )

    def make(stream, mapping=None, consumer=None):
        raw = field(stream=stream, mapping_version="v1" if mapping else None)
        requirement = MappingRequirement(consumer, "oi", "swap", mapping) if mapping else None
        return replay(
            (raw,), stream=stream, coverage=coverage(stream=stream), mapping_requirement=requirement
        )[0]

    a, b = make(stream_a), make(stream_b)
    source_none = make(derivative)
    source_a = make(derivative, mapping_a, spot_a)
    source_b = make(derivative, mapping_b, spot_b)
    graph = Graph(
        "v1",
        (
            Node("source"),
            Node("a", ("source",)),
            Node("b", ("source",)),
            Node("outer", ("a",)),
            Node("optional", optional=("source",)),
        ),
    )
    for source in (source_none, source_b):
        ok, why = evaluate_replays(graph, "a", {"a": a, "source": source}, ())
        assert not ok and any("mapping" in x.lower() for x in why)
    assert evaluate_replays(graph, "a", {"a": a, "source": source_a}, ())[0]
    assert evaluate_replays(graph, "b", {"b": b, "source": source_b}, ())[0]
    ok, why = evaluate_replays(graph, "outer", {"outer": a, "a": a, "source": source_b}, ())
    assert not ok and any("a" in x and "source" in x for x in why)
    ok, why = evaluate_replays(graph, "optional", {"optional": a, "source": source_b}, ())
    assert ok and any("mapping" in x.lower() for x in why)


def test_conflict_persists_until_explicit_verified_repair():
    original = field(available_at=WINDOW.start)
    altered = replace(original, raw_value=Decimal(5))
    cp = Checkpoint(STREAM, WINDOW.start, "v1", None, ("old",), ())
    first = resume(cp, coverage(), (original, altered), NOW, cursor_supported=True)
    assert first.checkpoint.covered_until == WINDOW.start and first.checkpoint.unresolved
    reloaded = load_checkpoint(checkpoint_bytes(first.checkpoint))
    ev = Evidence("e1", frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK}), True, True, True)
    for batch in ((), (replace(original, source_id="different"),)):
        retry = resume(reloaded, coverage(), batch, NOW, cursor_supported=True)
        assert retry.checkpoint.covered_until == WINDOW.start and retry.checkpoint.unresolved
        assert (
            lifecycle_for_plan(
                retry, Lifecycle.SUSPENDED, ev, NOW, freshness_ok=True, policy_present=True
            ).state
            == Lifecycle.SUSPENDED
        )
    repaired_at = NOW + timedelta(seconds=1)
    elapsed = resume(reloaded, coverage(), (), repaired_at, cursor_supported=True)
    assert elapsed.checkpoint.unresolved and elapsed.checkpoint.covered_until == WINDOW.start
    assert (
        lifecycle_for_plan(
            elapsed, Lifecycle.SUSPENDED, ev, repaired_at, freshness_ok=True, policy_present=True
        ).state
        == Lifecycle.SUSPENDED
    )
    proof = coverage(evidence_ref="independent-repair-proof", available_at=repaired_at)
    evidence = RepairEvidence(
        original.source_id, WINDOW, original, proof, "repair-record", repaired_at
    )
    with pytest.raises(ValueError, match="REPAIR_BATCH_MUST_BE_SEPARATE"):
        resume(
            reloaded, proof, (original,), repaired_at, cursor_supported=True, repairs=(evidence,)
        )
    recovered = resume(reloaded, proof, (), repaired_at, cursor_supported=True, repairs=(evidence,))
    assert not recovered.checkpoint.unresolved and recovered.checkpoint.covered_until == NOW
    assert (
        lifecycle_for_plan(
            recovered, Lifecycle.SUSPENDED, ev, repaired_at, freshness_ok=True, policy_present=True
        ).state
        == Lifecycle.DATA_CHECK
    )
    with pytest.raises(ValueError, match="REPAIR"):
        resume(
            reloaded,
            coverage(),
            (),
            NOW,
            cursor_supported=True,
            repairs=(replace(evidence, coverage=coverage(), verified_at=NOW),),
        )


def test_prior_checkpoint_without_conflict_state_fails_closed():
    import json

    cp = Checkpoint(STREAM, WINDOW.start, "v1", None, ("proof",), ())
    obj = json.loads(checkpoint_bytes(cp))
    del obj["integrity_format"]
    del obj["unresolved"]
    legacy = load_checkpoint(json.dumps(obj).encode())
    with pytest.raises(ValueError, match="CONFLICT_STATE_MIGRATION_REQUIRED"):
        resume(legacy, coverage(), (), NOW, cursor_supported=True)


def test_repair_requires_matching_id_interval_candidate_and_new_evidence():
    first = field(available_at=WINDOW.start)
    other = replace(first, raw_value=Decimal(5), input_ref="capture-b")
    cp = Checkpoint(STREAM, WINDOW.start, "v1", None, ("old",), ())
    failed = resume(cp, coverage(), (first, other), NOW, cursor_supported=True)
    conflict = failed.checkpoint.unresolved[0]
    assert {"input1", "capture-b", "proof"} <= set(conflict.evidence_refs)
    assert len(failed.checkpoint.captures) == 2
    later = NOW + timedelta(seconds=1)
    proof = coverage(evidence_ref="new-proof", available_at=later)
    repair = RepairEvidence(first.source_id, WINDOW, first, proof, "repair-ref", later)
    bad = (
        replace(repair, source_id="other-id"),
        replace(repair, window=Window(WINDOW.start - timedelta(minutes=1), WINDOW.end)),
        replace(repair, record=replace(first, raw_value=Decimal(99))),
        replace(repair, coverage=coverage(available_at=later)),
        replace(repair, verified_at=NOW),
    )
    for candidate in bad:
        with pytest.raises(ValueError, match="REPAIR"):
            resume(failed.checkpoint, proof, (), later, cursor_supported=True, repairs=(candidate,))
    assert failed.checkpoint.covered_until == WINDOW.start and failed.checkpoint.unresolved


def test_mapping_revalidated_at_consumer_cutoff():
    spot = ID
    swap = replace(ID, instrument_id="swap", market_type=MarketType.DERIVATIVE)
    derivative = Stream(swap, "oi", "fixture")
    until = NOW + timedelta(seconds=5)
    mapping = Mapping(spot, swap, "v1", "registry", Window(WINDOW.start, until), "spot", "swap")
    source, _ = replay(
        (field(stream=derivative, mapping_version="v1"),),
        stream=derivative,
        coverage=coverage(stream=derivative),
        mapping_requirement=MappingRequirement(spot, "oi", "swap", mapping),
    )
    consumer, _ = replay((field(),), cutoff=until, generated_at=until)
    graph = Graph("g", (Node("source"), Node("consumer", ("source",))))
    assert not evaluate_replays(graph, "consumer", {"source": source, "consumer": consumer}, ())[0]
