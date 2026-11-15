from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.v9.contracts import (
    Identity,
    InputContext,
    Lifecycle,
    Mapping,
    MarketType,
    Status,
    Window,
)
from src.v9.dependencies import Graph, Node
from src.v9.lifecycle import Evidence
from src.v9.offline_data import (
    Coverage,
    RawField,
    Revision,
    Stream,
    adapt,
    capability,
    correct,
    revision_at,
    semantic_fingerprint,
)
from src.v9.offline_replay import (
    Checkpoint,
    checkpoint_bytes,
    lifecycle_for_plan,
    load_checkpoint,
    resume,
    sample_replay,
)
from src.v9.quality import FixturePolicy, assess

NOW = datetime(2026, 1, 2, tzinfo=UTC)
WINDOW = Window(NOW - timedelta(minutes=1), NOW)
ID = Identity("venue", "1", MarketType.SPOT, "USD", "1")
STREAM = Stream(ID, "trade", "fixture")
POLICY = FixturePolicy("fixture", "1", False, True, True, 120)


def field(**changes):
    args = dict(
        stream=STREAM,
        source_id="id1",
        input_ref="input1",
        field="amount",
        raw_value=Decimal("0"),
        raw_unit="base",
        event_time=WINDOW.start,
        received_at=WINDOW.start,
        available_at=NOW,
        unknown_reason=None,
        side=None,
        closed=True,
        mapping_version=None,
    )
    args.update(changes)
    return RawField(**args)


def coverage(**changes):
    args = dict(
        stream=STREAM,
        window=WINDOW,
        status="verified_complete",
        proof_kind="independently_verified",
        evidence_ref="proof",
        available_at=NOW,
    )
    args.update(changes)
    return Coverage(**args)


def evaluate(record, **changes):
    args = dict(
        window=WINDOW,
        evaluated_at=NOW,
        declared_unit="base",
        declared_field="amount",
        require_closed=True,
        enabled=True,
    )
    args.update(changes)
    return adapt(record, **args)


def test_explicit_normalization_and_capabilities():
    with pytest.raises(RuntimeError, match="disabled"):
        evaluate(field(), enabled=False)
    result = evaluate(field())
    assert result.observation.value == 0 and result.diagnostics == ("SIDE_UNKNOWN",)
    assert evaluate(field(raw_unit=None)).observation.unit is None
    assert (
        evaluate(
            field(field="openInterestValue"), declared_field="openInterestAmount"
        ).observation.value
        is None
    )
    assert capability("trades_last_n")[0] == "UNKNOWN"
    assert capability("book_snapshot")[1] == "NO_SEQUENCED_CONTINUOUS_BOOK"
    assert capability("liquidations")[0] == "UNSUPPORTED"
    assert capability("oi_ambiguous")[0] == "UNSUPPORTED"


def test_time_cutoff_unknown_and_candle():
    assert evaluate(
        field(received_at=None, available_at=None, unknown_reason="legacy")
    ).exclusions == ("AVAILABILITY_UNKNOWN",)
    assert evaluate(
        field(received_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1))
    ).exclusions == ("AVAILABLE_AFTER_CUTOFF",)
    assert evaluate(field(kind="candle", candle_close_at=NOW, closed=False)).exclusions == (
        "CANDLE_OPEN",
    )
    assert evaluate(field(kind="candle", candle_close_at=NOW, closed=True)).observation is not None
    assert evaluate(field(event_time=WINDOW.end, received_at=NOW, available_at=NOW)).exclusions == (
        "EVENT_OUTSIDE_WINDOW",
    )


def test_coverage_quality_and_optional_scope():
    proof = coverage()
    ctx = InputContext(ID, "trade", "base")

    def check(cov, policy=POLICY):
        return assess(
            (),
            coverage=cov.for_window(STREAM, WINDOW, NOW),
            context=ctx,
            policy=policy,
            evaluated_at=NOW,
            window=WINDOW,
            evidence_refs=(cov.evidence_ref,),
        )

    assert check(proof).activity == 0
    assert check(coverage(status="unknown", proof_kind="last_n")).status == Status.MISSING
    assert check(coverage(status="gap", proof_kind="unknown")).status == Status.GAP
    with pytest.raises(ValueError, match="prove"):
        coverage(proof_kind="last_n")
    with pytest.raises(ValueError, match="scope"):
        proof.for_window(replace(STREAM, role="other"), WINDOW, NOW)
    good, gap = check(proof), check(coverage(status="gap", proof_kind="unknown"))
    graph = Graph(
        "g", (Node("required"), Node("optional"), Node("result", ("required",), ("optional",)))
    )
    assert graph.eligible("result", {"required": good, "optional": gap, "result": good}, ())[0]
    assert not graph.eligible("result", {"required": gap, "result": good}, ())[0]


def test_immutable_revision_cutoff():
    original = Revision(STREAM, WINDOW, "v1", None, Decimal(5), ("old",), NOW)
    late = NOW + timedelta(seconds=10)
    with pytest.raises(ValueError, match="COVERAGE"):
        correct(
            original,
            coverage=coverage(status="unknown", proof_kind="rollup"),
            version="v2",
            value=Decimal(6),
            input_refs=("late",),
            input_available_at=(late,),
            completed_at=late,
        )
    newer = correct(
        original,
        coverage=coverage(),
        version="v2",
        value=Decimal(6),
        input_refs=("complete", "late"),
        input_available_at=(NOW, late),
        completed_at=late,
    )
    assert newer.predecessor == "v1" and original.value == 5
    assert revision_at((original, newer), NOW) == original
    assert revision_at((original, newer), late) == newer


def replay(records, **changes):
    args = dict(
        stream=STREAM,
        window=WINDOW,
        coverage=coverage(),
        records=records,
        cutoff=NOW,
        generated_at=NOW,
        schema_version="1",
        calculation_version="c1",
        policy=POLICY,
        unit="base",
        field="amount",
        require_closed=True,
    )
    args.update(changes)
    return sample_replay(**args)


def test_manifest_replay_reproducibility_and_revision_cutoff():
    one = field()
    m, output = replay((one,))
    assert (m.content_digest, m.output_digest, output) == (
        replay((one,))[0].content_digest,
        replay((one,))[0].output_digest,
        replay((one,))[1],
    )
    changed = replay((replace(one, raw_value=Decimal(3)),))[0]
    assert changed.content_digest != m.content_digest and changed.output_digest != m.output_digest
    assert (
        replay((one,), policy=replace(POLICY, version="p2"))[0].content_digest != m.content_digest
    )
    later = replace(
        one, received_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1)
    )
    assert replay((later,))[1] == ()
    revised_later = replace(later, raw_value=Decimal(42), input_ref="revision2")
    assert replay((one, revised_later)) == replay((one,))
    assert replay((later,))[0].exclusions[0][1][0] == "AVAILABLE_AFTER_CUTOFF"


def test_restart_overlap_conflict_gap_and_recovery():
    first = field(available_at=WINDOW.start)
    cp = Checkpoint(
        STREAM,
        WINDOW.start,
        "v1",
        None,
        ("earlier",),
        ((first.source_id, semantic_fingerprint(first)),),
    )
    loaded = load_checkpoint(checkpoint_bytes(cp))
    assert loaded == cp
    plan = resume(loaded, coverage(), (first,), NOW, cursor_supported=False)
    assert not plan.new_records and plan.checkpoint.covered_until == NOW
    assert "CURSOR_UNSUPPORTED" in plan.reasons
    conflict = resume(
        loaded, coverage(), (replace(first, raw_value=Decimal(7)),), NOW, cursor_supported=False
    )
    assert conflict.checkpoint.covered_until == WINDOW.start
    assert "CONFLICTING_DUPLICATE:id1" in conflict.reasons
    earlier = replace(cp, covered_until=WINDOW.start - timedelta(minutes=1))
    gap = resume(earlier, coverage(), (), NOW, cursor_supported=True)
    assert gap.repair_intervals and gap.checkpoint.covered_until == earlier.covered_until
    states = frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK})
    ev = Evidence("e1", states, True, True, True)
    suspended = lifecycle_for_plan(
        gap, Lifecycle.READY, ev, NOW, freshness_ok=True, policy_present=True
    )
    assert suspended.state == Lifecycle.SUSPENDED and not suspended.grants_authorization
    recovered = lifecycle_for_plan(
        plan, Lifecycle.SUSPENDED, ev, NOW, freshness_ok=True, policy_present=True
    )
    assert recovered.state == Lifecycle.DATA_CHECK and not recovered.grants_authorization
    assert (
        lifecycle_for_plan(
            plan, Lifecycle.SUSPENDED, ev, NOW, freshness_ok=True, policy_present=False
        ).state
        == Lifecycle.SUSPENDED
    )


def test_six_generic_instruments_scoped():
    for i in range(6):
        stream = replace(STREAM, identity=replace(ID, instrument_id=str(i)))
        raw = field(stream=stream, source_id=f"id{i}")
        cov = coverage(stream=stream)
        assert cov.for_window(stream, WINDOW, NOW) is True
        assert evaluate(raw).observation.identity == stream.identity


def test_open_interest_zero_and_mapping_diagnostics():
    swap = Identity("venue", "2", MarketType.DERIVATIVE, "USD", "1")
    derivative = Stream(swap, "open_interest", "fixture")
    raw = field(
        stream=derivative,
        field="openInterestAmount",
        raw_unit="contracts",
        raw_value=Decimal(0),
        mapping_version="v1",
    )
    mapping = Mapping(
        ID,
        swap,
        "v1",
        "source",
        Window(WINDOW.start, NOW + timedelta(days=1)),
        "spot-symbol",
        "swap-symbol",
    )
    args = dict(
        window=WINDOW,
        evaluated_at=NOW,
        declared_unit="contracts",
        declared_field="openInterestAmount",
        require_closed=False,
        enabled=True,
    )
    assert evaluate(raw, **args).observation.value == 0
    assert "MAPPING_UNVERIFIED" in evaluate(raw, **args).diagnostics
    assert "MAPPING_UNVERIFIED" not in evaluate(raw, mapping=mapping, **args).diagnostics
    assert "UNIT_UNKNOWN_OR_MISMATCH" in evaluate(replace(raw, raw_unit=None), **args).diagnostics


def test_coverage_cannot_use_late_evidence():
    late = coverage(available_at=NOW + timedelta(seconds=1))
    assert late.for_window(STREAM, WINDOW, NOW) is None
    cp = Checkpoint(STREAM, WINDOW.start, "1", None, ("source",), ())
    plan = resume(cp, late, (), NOW, cursor_supported=True)
    assert plan.checkpoint.covered_until == WINDOW.start
    assert "COVERAGE_UNVERIFIED" in plan.reasons


def test_legacy_parser_unchanged_when_adapter_disabled(monkeypatch):
    import os

    from src.core.instruments import parse_symbol_pairs

    raw = "AAA/USD:AAA-PERP,BBB/USD:BBB-PERP"
    before = parse_symbol_pairs(raw)
    environment = dict(os.environ)
    with pytest.raises(RuntimeError, match="disabled"):
        adapt(
            field(),
            window=WINDOW,
            evaluated_at=NOW,
            declared_unit="base",
            declared_field="amount",
            require_closed=True,
        )
    assert parse_symbol_pairs(raw) == before
    assert dict(os.environ) == environment


def test_manifest_marks_unknown_coverage_ineligible():
    m, output = replay((field(),), coverage=coverage(status="unknown", proof_kind="last_n"))
    assert output and not m.eligible
    assert not replay((field(raw_unit=None),))[0].eligible


def test_expiry_and_freshness_fail_closed():
    ev = Evidence("e1", frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK}), True, False, True)
    cp = Checkpoint(STREAM, WINDOW.start, "1", None, ("proof",), ())
    plan = resume(cp, coverage(), (), NOW, cursor_supported=True)
    assert (
        lifecycle_for_plan(
            plan, Lifecycle.READY, ev, NOW, freshness_ok=True, policy_present=True
        ).state
        == Lifecycle.SUSPENDED
    )
    old_window = Window(WINDOW.start - timedelta(days=1), WINDOW.end - timedelta(days=1))
    old_coverage = coverage(window=old_window, available_at=old_window.end)
    manifest, _ = replay((), window=old_window, coverage=old_coverage)
    assert not manifest.eligible
