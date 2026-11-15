from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_contracts import ID, NOW, POLICY, WINDOW, good_quality, observation

from src.v9.contracts import Identity, Mapping, MarketType, Window, digest
from src.v9.dependencies import Graph, Node
from src.v9.profiles import Measurement, build_profile
from src.v9.quality import InputContext, assess


def assess_for(obs, context, **kwargs):
    return assess(
        obs,
        coverage=kwargs.get("coverage", True),
        policy=kwargs.get("policy", POLICY),
        evaluated_at=kwargs.get("evaluated_at", NOW),
        window=kwargs.get("window", WINDOW),
        evidence_refs=("coverage",),
        context=context,
    )


def test_r1_rejects_pooled_instruments_and_units():
    context = InputContext(ID, "spot_price", "base")
    other = replace(ID, instrument_id="2")
    with pytest.raises(ValueError, match="CONTEXT"):
        assess_for((observation(), observation(identity=other)), context)
    with pytest.raises(ValueError, match="CONTEXT"):
        assess_for((observation(), observation(unit="contracts")), context)
    with pytest.raises(ValueError, match="CONTEXT"):
        assess_for((observation(input_role="other"),), context)


def test_r1_rejects_relabel_and_unlinked_references():
    context = InputContext(ID, "spot_price", "base")
    q = assess_for((observation(),), context)
    with pytest.raises(ValueError, match="REFERENCE"):
        Measurement("m", Decimal(1), WINDOW, q, ("unrelated",), NOW, NOW, (NOW,), "closed")
    m = Measurement("m", Decimal(1), WINDOW, q, ("one", "extra"), NOW, NOW, (NOW,), "closed")
    with pytest.raises(ValueError, match="INSTRUMENT"):
        make_profile(replace(ID, instrument_id="2"), m)


def make_profile(identity, measurement, mappings=()):
    return build_profile(
        enabled=True,
        identity=identity,
        mapping_ref="mapping-v1",
        schema_version="1",
        policy_version=POLICY.version,
        profile_version="v1",
        calculation_version="c",
        data_cutoff=NOW,
        evaluated_at=NOW,
        applicable_from=NOW,
        expires_at=NOW + timedelta(days=1),
        sufficiency="ok",
        provenance=("profile",),
        measurements=(measurement,),
        policy=POLICY,
        mappings=mappings,
    )


def test_r1_explicit_cross_market_and_multiple_sources():
    swap = Identity(ID.venue, "2", MarketType.DERIVATIVE, ID.quote_currency, "1")
    mapping = Mapping(
        ID,
        swap,
        "mapping-v1",
        "registry",
        Window(WINDOW.start, NOW + timedelta(days=1)),
        "spot-symbol",
        "swap-symbol",
    )
    spot_q = assess_for((observation(),), InputContext(ID, "spot_price", "base"))
    swap_q = assess_for(
        (observation(identity=swap, input_ref="swap-input", input_role="derivative_price"),),
        InputContext(swap, "derivative_price", "base"),
    )
    m = Measurement(
        "combined",
        Decimal(1),
        WINDOW,
        spot_q,
        ("one", "swap-input", "derived"),
        NOW,
        NOW,
        (NOW,),
        "closed",
        additional_quality=(swap_q,),
    )
    assert make_profile(ID, m, (mapping,)).content_digest
    with pytest.raises(ValueError, match="MAPPING"):
        make_profile(ID, m)
    with pytest.raises(ValueError, match="REFERENCE"):
        replace(m, source_refs=("one",))
    expired = replace(mapping, validity=WINDOW)
    with pytest.raises(ValueError, match="MAPPING"):
        make_profile(ID, m, (expired,))
    incompatible = assess_for(
        (
            observation(
                identity=swap,
                input_ref="swap-input",
                input_role="derivative_price",
                unit="contracts",
            ),
        ),
        InputContext(swap, "derivative_price", "contracts"),
    )
    with pytest.raises(ValueError, match="UNITS"):
        replace(m, additional_quality=(incompatible,))


def test_r2_fresh_and_stale_covered_zero():
    context = InputContext(ID, "spot_price", "base")
    policy = replace(POLICY, max_age_seconds=120)
    assert assess_for((), context, policy=policy).activity == 0
    old = Window(WINDOW.start - timedelta(days=10), WINDOW.end - timedelta(days=10))
    assert assess_for((), context, policy=policy, window=old).reason == "FRESHNESS_EXCEEDED"
    assert (
        assess_for((), context, policy=policy, window=old, coverage=None).reason
        == "COVERAGE_UNKNOWN"
    )


def test_r3_nested_mutability_rejected():
    swap = Identity(ID.venue, "2", MarketType.DERIVATIVE, ID.quote_currency, "1")
    with pytest.raises((TypeError, ValueError)):
        Mapping(ID, swap, "v", "source", [], "spot", "swap")
    with pytest.raises((TypeError, ValueError)):
        observation(identity={})
    with pytest.raises((TypeError, ValueError)):
        observation(window=[])
    assert digest(InputContext(ID, "spot_price", "base")) == digest(
        InputContext(ID, "spot_price", "base")
    )


def test_r4_policy_conflicts_only_affect_required_closure():
    conflict = replace(POLICY, require_side=True)
    graph = Graph(
        "1",
        (
            Node("independent"),
            Node("required", required_policy=("p",)),
            Node("depends", ("required",)),
            Node("optional", optional=("required",)),
        ),
    )
    q = good_quality()
    policies = (POLICY, conflict)
    assert graph.eligible("independent", {"independent": q}, policies)[0]
    assert graph.eligible("optional", {"optional": q}, policies)[0]
    ok, reasons = graph.eligible("depends", {"depends": q, "required": q}, policies)
    assert not ok and "conflicting_policy:p" in reasons
    assert "conflicting_policy:p" in graph.policy_diagnostics(policies)
