from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.v9.contracts import Identity, Lifecycle, MarketType, Observation, Status, Window
from src.v9.dependencies import Graph, Node
from src.v9.lifecycle import Evidence, transition
from src.v9.profiles import Measurement, build_profile
from src.v9.quality import FixturePolicy, InputContext, assess
from src.v9.registry import Registry

NOW = datetime(2026, 1, 2, tzinfo=UTC)
WINDOW = Window(NOW - timedelta(minutes=1), NOW)
ID = Identity("okx", "1", MarketType.SPOT, "USDT", "1")
POLICY = FixturePolicy("p", "p", False, True, True, None)


def good_quality():
    return assess(
        (observation(),),
        coverage=True,
        policy=POLICY,
        evaluated_at=NOW,
        window=WINDOW,
        evidence_refs=("coverage",),
        context=InputContext(ID, "spot_price", "base"),
    )


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def observation(**changes):
    values = dict(
        identity=ID,
        source="fixture",
        input_ref="one",
        value=Decimal("1"),
        unit="base",
        side=None,
        event_time=WINDOW.start,
        received_at=WINDOW.start,
        available_at=NOW,
        evaluated_at=NOW,
        window=WINDOW,
        input_role="spot_price",
    )
    values.update(changes)
    return Observation(**values)


def test_identity_and_registry_schema_upgrade():
    other = replace(ID, schema_version="2")
    assert other.business_key == ID.business_key
    registry = Registry(
        [dict(id=1, exchange="okx", symbol="A/USDT", base="A", quote="USDT", type="spot")],
        [],
        schema_version="1",
    )
    assert registry.resolve(other)[1] == "A/USDT"
    with pytest.raises(ValueError, match="duplicate"):
        Registry(
            [dict(id=1, exchange="okx", symbol="A/USDT", base="A", quote="USDT", type="spot")] * 2,
            [],
            schema_version="1",
        )


def test_times_late_and_unknown():
    assert observation().received_at == WINDOW.start
    assert observation(received_at=NOW, available_at=NOW).received_at == NOW
    assert (
        observation(received_at=None, available_at=None, unknown_reason="HISTORY").received_at
        is None
    )
    with pytest.raises(ValueError):
        observation(received_at=None)
    with pytest.raises(ValueError):
        observation(available_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError):
        Window(NOW.replace(tzinfo=None), NOW)


def test_quality_scoped_and_zero():
    kw = dict(
        policy=POLICY,
        evaluated_at=NOW,
        window=WINDOW,
        evidence_refs=("coverage",),
        context=InputContext(ID, "spot_price", "base"),
    )
    assert assess((), coverage=True, **kw).activity == 0
    assert assess((), coverage=None, **kw).status == Status.MISSING
    assert assess((observation(),), coverage=True, **kw).status == Status.VALID
    assert (
        assess(
            (observation(),), coverage=True, **(kw | {"policy": replace(POLICY, require_side=True)})
        ).status
        == Status.UNSUPPORTED
    )
    assert (
        assess(
            (observation(unit=None),),
            coverage=True,
            **(kw | {"context": InputContext(ID, "spot_price", None)}),
        ).status
        == Status.UNSUPPORTED
    )
    assert (
        assess((observation(closed=False),), coverage=True, **kw).status
        == Status.INSUFFICIENT_HISTORY
    )
    assert assess((), coverage=False, **kw).status == Status.GAP
    assert (
        assess((), coverage=True, **(kw | {"policy": None})).status == Status.INSUFFICIENT_HISTORY
    )


def test_graph_validation_and_optional():
    with pytest.raises(ValueError, match="cycle"):
        Graph("1", (Node("a", ("b",)), Node("b", ("a",))))
    with pytest.raises(ValueError, match="unresolved"):
        Graph("1", (Node("a", ("absent",)),))
    graph = Graph("1", (Node("a"), Node("b"), Node("c", ("a",), ("b",), ("p",))))
    good = good_quality()
    assert graph.eligible("c", {"a": good, "c": good}, (POLICY,))[0]
    assert not graph.eligible("c", {"a": good, "c": good}, ())[0]


def test_lifecycle_no_automatic_authorization():
    ev = Evidence("1", frozenset({Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK}), True, True)
    result = transition(Lifecycle.DISCOVERED, Lifecycle.DATA_CHECK, ev, NOW)
    assert result.state == Lifecycle.DATA_CHECK and not result.grants_authorization
    assert (
        transition(Lifecycle.READY, Lifecycle.READY, replace(ev, unexpired=False), NOW).state
        == Lifecycle.SUSPENDED
    )
    assert transition(Lifecycle.SUSPENDED, Lifecycle.READY, ev, NOW).state == Lifecycle.SUSPENDED


def test_profile_pinning_and_canonical_digest():
    quality = good_quality()
    m = Measurement("price", Decimal("1.00"), WINDOW, quality, ("one",), NOW, NOW, (NOW,), "closed")
    fields = dict(
        identity=ID,
        mapping_ref="m",
        schema_version="1",
        policy_version="p",
        profile_version="v1",
        calculation_version="c",
        data_cutoff=NOW,
        evaluated_at=NOW,
        applicable_from=NOW,
        expires_at=NOW + timedelta(days=1),
        sufficiency="ok",
        provenance=("one",),
        measurements=(m,),
        policy=POLICY,
    )
    with pytest.raises(RuntimeError):
        build_profile(**fields)
    profile = build_profile(enabled=True, **fields)
    assert profile.content_digest == build_profile(enabled=True, **fields).content_digest
    newer = replace(profile, profile_version="v2")
    assert profile.profile_version == "v1" and newer.content_digest != profile.content_digest
    with pytest.raises(FrozenInstanceError):
        profile.profile_version = "x"
    with pytest.raises(ValueError):
        replace(m, input_available_at=(NOW + timedelta(seconds=1),))


def test_six_instruments_do_not_mutate_supplied_limits():
    limits = (Decimal("100"), Decimal("10"))
    before = limits
    ids = tuple(Identity("venue", str(i), MarketType.SPOT, "USD", "1") for i in range(6))
    assert len({x.business_key for x in ids}) == 6
    assert limits == before


def test_mapping_ambiguity():
    from src.v9.contracts import Mapping

    derivative = Identity("okx", "2", MarketType.DERIVATIVE, "USDT", "1")
    mapping = Mapping(ID, derivative, "1", "fixture", WINDOW, "A/USDT", "A/USDT:USDT")
    rows = [
        dict(id=1, exchange="okx", symbol="A/USDT", base="A", quote="USDT", type="spot"),
        dict(id=2, exchange="okx", symbol="A/USDT:USDT", base="A", quote="USDT", type="swap"),
    ]
    registry = Registry(rows, (mapping,), schema_version="1")
    assert registry.resolve(derivative)[1] == "A/USDT:USDT"
    with pytest.raises(ValueError, match="ambiguous"):
        Registry(rows, (mapping, mapping), schema_version="1")


def test_import_has_no_network_or_config_side_effect(monkeypatch):
    import importlib
    import os
    import sys

    before = dict(os.environ)
    for name in ("contracts", "registry", "quality", "dependencies", "lifecycle", "profiles"):
        sys.modules.pop(f"src.v9.{name}", None)
        importlib.import_module(f"src.v9.{name}")
    assert dict(os.environ) == before
