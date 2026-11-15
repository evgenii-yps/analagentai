from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from test_contracts import NOW, POLICY, WINDOW, good_quality, observation

from src.v9.contracts import Identity, Lifecycle, Mapping, MarketType, Status, digest
from src.v9.dependencies import Graph, Node
from src.v9.lifecycle import Evidence, transition
from src.v9.profiles import Measurement, build_profile
from src.v9.quality import InputContext, assess
from src.v9.registry import Registry


def test_decimal_digest_ignores_ambient_context():
    value = Decimal("123456789.123456789000")
    with localcontext() as ctx:
        ctx.prec = 3
        first = digest(value)
    with localcontext() as ctx:
        ctx.prec = 30
        assert digest(value) == first == digest(Decimal("123456789.123456789"))


def test_registry_rejects_malformed_and_source_conflicts():
    row = dict(id=1, exchange="venue", symbol="A/USD", base="A", quote="USD", type="spot")
    for changed in ({"id": None}, {"id": True}, {"exchange": None}, {"quote": 123}):
        with pytest.raises(ValueError):
            Registry([row | changed], [], schema_version="1")
    spot = Identity("venue", "1", MarketType.SPOT, "USD", "1")
    swap = Identity("venue", "2", MarketType.DERIVATIVE, "USD", "1")
    other = row | {"id": 2, "symbol": "PERP", "type": "swap"}
    mapping = Mapping(spot, swap, "v", "fixture", WINDOW, "wrong", "PERP")
    with pytest.raises(ValueError, match="source identity"):
        Registry([row, other], [mapping], schema_version="1")
    with pytest.raises(ValueError, match="unknown identity"):
        Registry([row], [mapping], schema_version="1")


def test_time_and_freshness_are_explicit():
    with pytest.raises(ValueError, match="receipt"):
        observation(received_at=None, unknown_reason="unknown")
    with pytest.raises(ValueError):
        observation(event_time=NOW + timedelta(seconds=1))
    kw = dict(
        coverage=True,
        policy=replace(POLICY, max_age_seconds=30),
        evaluated_at=NOW,
        window=WINDOW,
        evidence_refs=("coverage",),
        context=InputContext(observation().identity, "spot_price", "base"),
    )
    assert assess((observation(),), **kw).status == Status.STALE
    with pytest.raises(ValueError, match="mismatch"):
        assess((observation(),), **(kw | {"evaluated_at": NOW + timedelta(seconds=1)}))


def test_optional_defect_is_reported_without_veto():
    graph = Graph(
        "p1",
        (Node("required"), Node("optional"), Node("result", ("required",), ("optional",), ("p",))),
    )
    ok = good_quality()
    assert graph.eligible("result", {"required": ok, "result": ok}, (POLICY,))[0]
    assert graph.optional_diagnostics("result", {}) == ("optional:optional:MISSING",)
    assert not graph.eligible("result", {"result": ok}, (POLICY,))[0]


def test_recovery_requires_full_evidence():
    all_states = frozenset(
        {
            Lifecycle.DISCOVERED,
            Lifecycle.DATA_CHECK,
            Lifecycle.WARMUP,
            Lifecycle.SHADOW_VALIDATION,
            Lifecycle.READY,
        }
    )
    ev = Evidence("e1", all_states, True, True, True)
    assert transition(Lifecycle.SUSPENDED, Lifecycle.READY, ev, NOW).state == Lifecycle.SUSPENDED
    result = transition(Lifecycle.SUSPENDED, Lifecycle.WARMUP, ev, NOW)
    assert result.state == Lifecycle.WARMUP and not result.grants_authorization
    assert (
        transition(Lifecycle.READY, Lifecycle.READY, replace(ev, unexpired=False), NOW).state
        == Lifecycle.SUSPENDED
    )


def test_six_mapped_instruments_and_pinned_profiles():
    rows, mappings, identities = [], [], []
    for i in range(6):
        spot = Identity("venue", str(2 * i + 1), MarketType.SPOT, "USD", "1")
        swap = Identity("venue", str(2 * i + 2), MarketType.DERIVATIVE, "USD", "1")
        a, b = f"asset{i}/USD", f"contract-{i}"
        rows.extend(
            [
                dict(
                    id=2 * i + 1, exchange="venue", symbol=a, base=f"A{i}", quote="USD", type="spot"
                ),
                dict(
                    id=2 * i + 2, exchange="venue", symbol=b, base=f"A{i}", quote="USD", type="swap"
                ),
            ]
        )
        mappings.append(Mapping(spot, swap, "v1", "fixture", WINDOW, a, b))
        identities.append(spot)
    registry = Registry(rows, mappings, schema_version="1")

    def measurement(identity):
        q = assess(
            (observation(identity=identity),),
            coverage=True,
            policy=POLICY,
            evaluated_at=NOW,
            window=WINDOW,
            evidence_refs=("coverage",),
            context=InputContext(identity, "spot_price", "base"),
        )
        return Measurement("price", Decimal("1"), WINDOW, q, ("one",), NOW, NOW, (NOW,), "closed")

    limits = (Decimal("100"), Decimal("10"))
    profiles = [
        build_profile(
            enabled=True,
            identity=i,
            mapping_ref="v1",
            schema_version="1",
            policy_version="p",
            profile_version="v1",
            calculation_version="c",
            data_cutoff=NOW,
            evaluated_at=NOW,
            applicable_from=NOW,
            expires_at=NOW + timedelta(days=1),
            sufficiency="ok",
            provenance=("ref",),
            measurements=(measurement(i),),
            policy=POLICY,
        )
        for i in identities
    ]
    assert all(registry.resolve(i) for i in identities)
    assert len({p.content_digest for p in profiles}) == 6
    old = profiles[0].content_digest
    newer = replace(profiles[0], profile_version="v2")
    assert profiles[0].content_digest == old != newer.content_digest
    assert limits == (Decimal("100"), Decimal("10"))
    with pytest.raises((TypeError, ValueError)):
        replace(profiles[0], measurements=([measurement(identities[0])],))


def test_quality_provenance_and_policy_validation():
    from src.v9.quality import FixturePolicy

    quality = good_quality()
    assert (quality.evaluated_at, quality.window, quality.evidence_refs) == (
        NOW,
        WINDOW,
        ("coverage",),
    )
    assert (quality.policy_name, quality.policy_version, quality.policy_digest) == (
        POLICY.name,
        POLICY.version,
        digest(POLICY),
    )
    with pytest.raises(ValueError, match="POLICY_INVALID_FLAGS"):
        replace(POLICY, require_side="yes")
    with pytest.raises(ValueError, match="POLICY_INVALID_MAX_AGE"):
        replace(POLICY, max_age_seconds=True)
    with pytest.raises(ValueError, match="POLICY_INVALID_MAX_AGE"):
        FixturePolicy("p", "1", False, False, True, -1)
    with pytest.raises(ValueError, match="window"):
        assess(
            (),
            coverage=True,
            policy=POLICY,
            evaluated_at=WINDOW.start,
            window=WINDOW,
            evidence_refs=("coverage",),
            context=InputContext(observation().identity, "spot_price", "base"),
        )
    with pytest.raises(ValueError, match="evidence"):
        assess(
            (),
            coverage=True,
            policy=POLICY,
            evaluated_at=NOW,
            window=WINDOW,
            evidence_refs=(),
            context=InputContext(observation().identity, "spot_price", "base"),
        )
    unknown = observation(received_at=None, available_at=None, unknown_reason="HISTORY")
    result = assess(
        (unknown,),
        coverage=True,
        policy=POLICY,
        evaluated_at=NOW,
        window=WINDOW,
        evidence_refs=("coverage",),
        context=InputContext(observation().identity, "spot_price", "base"),
    )
    assert result.reason == "TIME_UNKNOWN" and unknown.available_at is None


def test_policy_content_conflict_is_scoped():
    graph = Graph("graph", (Node("a", required_policy=("p",)), Node("b")))
    good = good_quality()
    changed = replace(POLICY, require_side=True)
    ok, reasons = graph.eligible("a", {"a": good}, (changed,))
    assert not ok and reasons == ("policy_mismatch:a:p",)
    assert graph.eligible("b", {"b": good}, (changed,))[0]
    assert not graph.eligible("a", {"a": good}, (POLICY, changed))[0]
    with pytest.raises(ValueError, match="POLICY_INVALID"):
        graph.eligible("a", {"a": good}, ("p",))


def test_profile_rejects_same_version_changed_policy():
    q = good_quality()
    m = Measurement("price", Decimal(1), WINDOW, q, ("one",), NOW, NOW, (NOW,), "closed")
    fields = dict(
        identity=Identity("venue", "1", MarketType.SPOT, "USD", "1"),
        mapping_ref="mapping",
        schema_version="1",
        policy_version=POLICY.version,
        profile_version="1",
        calculation_version="1",
        data_cutoff=NOW,
        evaluated_at=NOW,
        applicable_from=NOW,
        expires_at=NOW + timedelta(days=1),
        sufficiency="ok",
        provenance=("one",),
        measurements=(m,),
        policy=replace(POLICY, require_side=True),
    )
    with pytest.raises(ValueError, match="QUALITY_POLICY_OR_CONTEXT_MISMATCH"):
        build_profile(enabled=True, **fields)
