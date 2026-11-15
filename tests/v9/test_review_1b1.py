from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from itertools import permutations

import pytest
from test_offline_foundation import ID, NOW, STREAM, WINDOW, coverage, field, replay

from src.v9.contracts import Lifecycle, Mapping, MarketType, Window
from src.v9.dependencies import Graph, Node
from src.v9.lifecycle import Evidence
from src.v9.offline_data import Revision, Stream, adapt, correct, revision_at, semantic_fingerprint
from src.v9.offline_replay import (
    Checkpoint,
    MappingRequirement,
    checkpoint_bytes,
    evaluate_replays,
    lifecycle_for_plan,
    load_checkpoint,
    resume,
)


def test_required_mapping_and_graph_from_actual_replay():
    swap = replace(ID, instrument_id="2", market_type=MarketType.DERIVATIVE)
    stream = Stream(swap, "oi", "fixture")
    raw = field(stream=stream, mapping_version="v1")
    cov = coverage(stream=stream)
    mapping = Mapping(
        ID, swap, "v1", "registry", Window(WINDOW.start, NOW + timedelta(days=1)), "spot", "swap"
    )
    required = MappingRequirement(ID, "oi", "swap", mapping)
    good, output = replay((raw,), stream=stream, coverage=cov, mapping_requirement=required)
    assert output and good.eligible and good.mapping_requirement == required
    for rule in (
        replace(required, mapping=None),
        replace(required, source_identity="wrong"),
        replace(required, role="wrong"),
        replace(required, consumer_identity=replace(ID, instrument_id="unrelated")),
        replace(required, mapping=replace(mapping, version="v2")),
        replace(required, mapping=replace(mapping, validity=WINDOW)),
    ):
        manifest, output = replay((raw,), stream=stream, coverage=cov, mapping_requirement=rule)
        assert not output and not manifest.eligible
        assert any("MAPPING_UNVERIFIED" in reason for _, reason in manifest.exclusions)
        graph = Graph(
            "g",
            (
                Node("source"),
                Node("target", ("source",)),
                Node("independent"),
                Node("optional_target", optional=("source",)),
            ),
        )
        assert not evaluate_replays(graph, "target", {"target": good, "source": manifest}, ())[0]
        assert evaluate_replays(
            graph, "independent", {"independent": good, "source": manifest}, ()
        )[0]
        assert evaluate_replays(
            graph, "optional_target", {"optional_target": good, "source": manifest}, ()
        )[0]
    missing, output = replay((raw,), stream=stream, coverage=cov)
    assert not output and not missing.eligible


def test_replay_visible_conflicts_and_tied_exclusions_are_order_independent():
    a = field(available_at=WINDOW.start)
    b = replace(a, raw_value=Decimal(2))
    newer = replace(a, available_at=NOW, raw_value=Decimal(3), input_ref="newer")
    for items in permutations((a, b, newer)):
        with pytest.raises(ValueError, match="conflicting revision"):
            replay(items)
    assert replay((a, a)) == replay((a,))
    unknown = replace(a, available_at=None, received_at=None, unknown_reason="unknown")
    other = replace(unknown, raw_value=Decimal(5))
    assert replay((unknown, other)) == replay((other, unknown))
    later = replace(
        newer, available_at=NOW + timedelta(seconds=1), received_at=NOW + timedelta(seconds=1)
    )
    assert replay((a, later)) == replay((a,))
    assert (
        replay((a,), field="amount")[0].content_digest
        != replay((a,), field="different")[0].content_digest
    )
    assert (
        replay((a,), require_closed=True)[0].content_digest
        != replay((a,), require_closed=False)[0].content_digest
    )


def test_restart_deduplication_conflicts_and_scoped_repairs():
    first = field(available_at=WINDOW.start)
    cp = Checkpoint(
        STREAM,
        WINDOW.start,
        "1",
        None,
        ("proof",),
        ((first.source_id, semantic_fingerprint(first)),),
    )
    refetch = replace(first, input_ref="new-capture", received_at=NOW, available_at=NOW)
    plan = resume(
        load_checkpoint(checkpoint_bytes(cp)), coverage(), (refetch,), NOW, cursor_supported=True
    )
    assert not plan.new_records and not plan.reasons and plan.checkpoint.covered_until == NOW
    assert plan.checkpoint.captures
    empty = replace(cp, seen=())
    different = replace(first, raw_value=Decimal(4))
    for batch in ((first, different), (different, first)):
        conflict = resume(empty, coverage(), batch, NOW, cursor_supported=True)
        assert not conflict.new_records and not conflict.checkpoint.seen
        assert conflict.checkpoint.covered_until == WINDOW.start
    outside = replace(
        first,
        event_time=WINDOW.start - timedelta(seconds=1),
        received_at=WINDOW.start,
        available_at=WINDOW.start,
    )
    out = resume(empty, coverage(), (outside,), NOW, cursor_supported=True)
    assert not out.new_records and not out.checkpoint.seen
    gap = resume(
        empty, coverage(status="gap", proof_kind="unknown"), (), NOW, cursor_supported=True
    )
    assert gap.repair_intervals == (WINDOW,) and gap.checkpoint.covered_until == WINDOW.start
    ev = Evidence("e1", frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK}), True, True, True)
    assert (
        lifecycle_for_plan(
            gap, Lifecycle.READY, ev, NOW, freshness_ok=True, policy_present=True
        ).state
        == Lifecycle.SUSPENDED
    )
    assert (
        lifecycle_for_plan(
            plan, Lifecycle.SUSPENDED, ev, NOW, freshness_ok=True, policy_present=True
        ).state
        == Lifecycle.DATA_CHECK
    )


def test_candle_close_and_strict_revision_chronology():
    candle = field(
        kind="candle",
        candle_close_at=NOW,
        closed=True,
        received_at=WINDOW.start,
        available_at=WINDOW.start,
    )
    kw = dict(
        window=WINDOW,
        evaluated_at=NOW,
        declared_unit="base",
        declared_field="amount",
        require_closed=True,
        enabled=True,
    )
    assert "CANDLE_AVAILABLE_BEFORE_CLOSE" in adapt(candle, **kw).exclusions
    assert "CANDLE_OPEN" in adapt(replace(candle, closed=False), **kw).exclusions
    assert adapt(replace(candle, received_at=NOW, available_at=NOW), **kw).observation is not None
    assert (
        "AVAILABLE_AFTER_CUTOFF"
        in adapt(
            replace(
                candle,
                received_at=NOW + timedelta(seconds=1),
                available_at=NOW + timedelta(seconds=1),
            ),
            **kw,
        ).exclusions
    )
    assert adapt(field(available_at=WINDOW.start), **kw).observation is not None
    original = Revision(STREAM, WINDOW, "v1", None, Decimal(5), ("old",), NOW)
    with pytest.raises(ValueError, match="PREDECESSOR"):
        correct(
            original,
            coverage=coverage(),
            version="v2",
            value=Decimal(6),
            input_refs=("a",),
            input_available_at=(NOW,),
            completed_at=NOW,
        )
    assert revision_at((original,), NOW) == original


def test_graph_uses_replay_quality_for_exclusions_and_optional_gaps():
    good, _ = replay((field(),))
    gap, _ = replay((), coverage=coverage(status="gap", proof_kind="unknown"))
    late = replace(
        field(), received_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1)
    )
    unavailable, _ = replay((late,))
    graph = Graph(
        "g",
        (Node("input"), Node("target", ("input",)), Node("optional_target", optional=("input",))),
    )
    assert not evaluate_replays(graph, "target", {"target": good, "input": gap}, ())[0]
    assert not evaluate_replays(graph, "target", {"target": good, "input": unavailable}, ())[0]
    ok, notes = evaluate_replays(
        graph, "optional_target", {"optional_target": good, "input": gap}, ()
    )
    assert ok and "optional:input:GAP" in notes


def test_legacy_checkpoint_requires_explicit_migration():
    import json

    cp = Checkpoint(STREAM, WINDOW.start, "old", None, ("proof",), (("id", "old-digest"),))
    obj = json.loads(checkpoint_bytes(cp))
    del obj["fingerprint_format"]
    loaded = load_checkpoint(json.dumps(obj).encode())
    with pytest.raises(ValueError, match="MIGRATION_REQUIRED"):
        resume(loaded, coverage(), (), NOW, cursor_supported=True)


def test_closed_candle_at_and_after_close_and_valid_revision():
    close = WINDOW.end
    candle = field(
        kind="candle", candle_close_at=close, closed=True, received_at=close, available_at=close
    )
    kw = dict(
        window=WINDOW,
        evaluated_at=NOW,
        declared_unit="base",
        declared_field="amount",
        require_closed=True,
        enabled=True,
    )
    assert adapt(candle, **kw).observation is not None
    after = replace(
        candle, received_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1)
    )
    assert adapt(after, **kw).observation is None
    assert adapt(after, **(kw | {"evaluated_at": NOW + timedelta(seconds=1)})).observation
    original = Revision(STREAM, WINDOW, "v1", None, Decimal(5), ("old",), NOW)
    later = NOW + timedelta(seconds=1)
    corrected = correct(
        original,
        coverage=coverage(),
        version="v2",
        value=Decimal(6),
        input_refs=("a",),
        input_available_at=(NOW,),
        completed_at=later,
    )
    assert revision_at((original, corrected), NOW) == original
    assert revision_at((original, corrected), later) == corrected
