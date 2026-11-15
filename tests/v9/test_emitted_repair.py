from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_offline_foundation import NOW, STREAM, WINDOW, coverage, field

from src.v9.contracts import Lifecycle
from src.v9.lifecycle import Evidence
from src.v9.offline_data import semantic_fingerprint
from src.v9.offline_replay import (
    Checkpoint,
    RepairEvidence,
    checkpoint_bytes,
    lifecycle_for_plan,
    load_checkpoint,
    resume,
)


def make_scenario(*, emitted):
    a = field(available_at=WINDOW.start)
    b = replace(a, raw_value=Decimal(12), input_ref="capture-b")
    cp = Checkpoint(STREAM, WINDOW.start, "1", None, ("initial",), ())
    if emitted:
        first = resume(cp, coverage(), (a,), NOW, cursor_supported=True)
        assert first.new_records == (a,)
        cp = load_checkpoint(checkpoint_bytes(first.checkpoint))
        records = (b,)
    else:
        records = (a, b)
    conflict = resume(cp, coverage(), records, NOW, cursor_supported=True)
    assert not conflict.new_records and conflict.checkpoint.unresolved
    return a, b, load_checkpoint(checkpoint_bytes(conflict.checkpoint))


def repair(record):
    later = NOW + timedelta(seconds=1)
    proof = coverage(evidence_ref="new-independent-proof", available_at=later)
    return later, proof, RepairEvidence(record.source_id, WINDOW, record, proof, "repair", later)


def suspended(plan, at):
    evidence = Evidence(
        "1", frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK}), True, True, True
    )
    return lifecycle_for_plan(
        plan, Lifecycle.SUSPENDED, evidence, at, freshness_ok=True, policy_present=True
    ).state


def test_emitted_changed_payload_requires_correction_and_remains_suspended():
    a, b, cp = make_scenario(emitted=True)
    before = checkpoint_bytes(cp)
    later, proof, evidence = repair(b)
    with pytest.raises(ValueError, match="CORRECTION_REQUIRED"):
        resume(cp, proof, (), later, cursor_supported=True, repairs=(evidence,))
    assert checkpoint_bytes(cp) == before
    assert dict(cp.seen)[a.source_id] == semantic_fingerprint(a) and cp.unresolved
    retry = resume(load_checkpoint(checkpoint_bytes(cp)), proof, (), later, cursor_supported=True)
    assert retry.checkpoint.unresolved and dict(retry.checkpoint.seen)[
        a.source_id
    ] == semantic_fingerprint(a)
    assert suspended(retry, later) == Lifecycle.SUSPENDED


def test_emitted_original_confirmation_never_reemits():
    a, _, cp = make_scenario(emitted=True)
    later, proof, evidence = repair(a)
    recovered = resume(cp, proof, (), later, cursor_supported=True, repairs=(evidence,))
    assert not recovered.new_records and not recovered.checkpoint.unresolved
    assert dict(recovered.checkpoint.seen)[a.source_id] == semantic_fingerprint(a)
    retry = resume(
        load_checkpoint(checkpoint_bytes(recovered.checkpoint)),
        proof,
        (a,),
        later,
        cursor_supported=True,
    )
    assert not retry.new_records and not retry.checkpoint.unresolved
    assert suspended(retry, later) == Lifecycle.DATA_CHECK


def test_never_emitted_selected_payload_emits_once():
    _, b, cp = make_scenario(emitted=False)
    later, proof, evidence = repair(b)
    recovered = resume(cp, proof, (), later, cursor_supported=True, repairs=(evidence,))
    assert recovered.new_records == (b,) and not recovered.checkpoint.unresolved
    retry = resume(
        load_checkpoint(checkpoint_bytes(recovered.checkpoint)),
        proof,
        (b,),
        later,
        cursor_supported=True,
    )
    assert not retry.new_records and not retry.checkpoint.unresolved
    assert suspended(retry, later) == Lifecycle.DATA_CHECK
