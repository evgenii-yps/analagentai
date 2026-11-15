"""Deterministic fixture manifest, replay, and conservative restart assessment."""

import json
from dataclasses import dataclass, replace
from datetime import datetime

from .contracts import (
    Identity,
    InputContext,
    Lifecycle,
    Mapping,
    MarketType,
    Quality,
    Status,
    Window,
    digest,
    encode,
    text,
    utc,
)
from .dependencies import Graph
from .lifecycle import Evidence, Result, transition
from .offline_data import Coverage, RawField, Stream, adapt, semantic_fingerprint
from .quality import FixturePolicy, assess


@dataclass(frozen=True, slots=True)
class MappingRequirement:
    consumer_identity: Identity
    role: str
    source_identity: str
    mapping: Mapping | None

    def __post_init__(self):
        if type(self.consumer_identity) is not Identity:
            raise TypeError("consumer identity required")
        text(self.role)
        text(self.source_identity)
        if self.mapping is not None and type(self.mapping) is not Mapping:
            raise TypeError("immutable mapping required")


def mapping_valid(
    rule: MappingRequirement | None, stream: Stream, cutoff: datetime, raw: RawField | None
) -> bool:
    if type(rule) is not MappingRequirement or rule.mapping is None or rule.role != stream.role:
        return False
    mapping = rule.mapping
    source_key = stream.identity.business_key
    if {source_key, rule.consumer_identity.business_key} != {
        mapping.spot.business_key,
        mapping.derivative.business_key,
    }:
        return False
    source_identity = (
        mapping.spot_source_identity
        if source_key == mapping.spot.business_key
        else mapping.derivative_source_identity
    )
    return (
        source_identity == rule.source_identity
        and mapping.validity.start <= cutoff < mapping.validity.end
        and (raw is None or raw.mapping_version == mapping.version)
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    stream: Stream
    window: Window
    schema_version: str
    calculation_version: str
    policy_version: str
    policy_digest: str
    cutoff: datetime
    generated_at: datetime
    coverage: Coverage
    input_checksums: tuple[tuple[str, str], ...]
    normalized_unit: str | None
    declared_field: str
    require_closed: bool
    mapping_requirement: MappingRequirement | None
    quality: Quality
    exclusions: tuple[tuple[str, tuple[str, ...]], ...]
    output_digest: str
    eligible: bool

    def __post_init__(self):
        if type(self.stream) is not Stream or type(self.window) is not Window:
            raise TypeError("stream and window required")
        if (
            type(self.coverage) is not Coverage
            or self.coverage.stream.business_key != self.stream.business_key
            or self.coverage.window != self.window
        ):
            raise ValueError("coverage scope mismatch")
        for item in (
            self.schema_version,
            self.calculation_version,
            self.policy_version,
            self.policy_digest,
            self.output_digest,
        ):
            text(item)
        if self.normalized_unit is not None:
            text(self.normalized_unit)
        text(self.declared_field)
        if type(self.require_closed) is not bool or type(self.quality) is not Quality:
            raise TypeError("replay policy and quality required")
        if (
            self.mapping_requirement is not None
            and type(self.mapping_requirement) is not MappingRequirement
        ):
            raise TypeError("immutable mapping requirement required")
        utc(self.cutoff)
        utc(self.generated_at)
        if self.generated_at < self.cutoff:
            raise ValueError("generation before cutoff")
        if type(self.eligible) is not bool:
            raise TypeError("eligibility must be boolean")
        if type(self.input_checksums) is not tuple or type(self.exclusions) is not tuple:
            raise TypeError("immutable manifest fields required")
        for entry in self.input_checksums:
            if type(entry) is not tuple or len(entry) != 2:
                raise TypeError("immutable checksum pair required")
            for value in entry:
                text(value)
        for entry in self.exclusions:
            if type(entry) is not tuple or len(entry) != 2 or type(entry[1]) is not tuple:
                raise TypeError("immutable exclusion pair required")
            text(entry[0])
            for reason in entry[1]:
                text(reason)

    @property
    def content_digest(self) -> str:
        return digest(self)


def sample_replay(
    *,
    stream: Stream,
    window: Window,
    coverage: Coverage,
    records: tuple[RawField, ...],
    cutoff: datetime,
    generated_at: datetime,
    schema_version: str,
    calculation_version: str,
    policy: FixturePolicy,
    unit: str | None,
    field: str,
    require_closed: bool,
    mapping_requirement: MappingRequirement | None = None,
) -> tuple[Manifest, tuple]:
    if type(records) is not tuple or any(
        type(r) is not RawField or r.stream.business_key != stream.business_key for r in records
    ):
        raise ValueError("record scope mismatch")
    if type(policy) is not FixturePolicy:
        raise ValueError("POLICY_MISSING_OR_INVALID")
    coverage_value = coverage.for_window(stream, window, cutoff)
    visible = {}
    unknown = []
    future = []
    for record in records:
        if record.available_at is None or record.received_at is None:
            unknown.append(record)
        elif record.available_at > cutoff or record.received_at > cutoff:
            future.append(record)
        else:
            key = (record.source_id, record.available_at)
            visible.setdefault(key, []).append(record)
    selected = {}
    for (source_id, availability), group in sorted(visible.items()):
        if len({(semantic_fingerprint(item), item.mapping_version) for item in group}) != 1:
            raise ValueError("conflicting revision at same availability")
        candidate = min(group, key=digest)
        if source_id not in selected or availability > selected[source_id].available_at:
            selected[source_id] = candidate
    excluded_future = [r for r in future if r.source_id not in selected]
    ordered = tuple(
        sorted(
            (*selected.values(), *unknown, *excluded_future),
            key=lambda r: (r.event_time, r.source_id, r.input_ref, digest(r)),
        )
    )
    adapted = tuple(
        adapt(
            r,
            window=window,
            evaluated_at=cutoff,
            declared_unit=unit,
            declared_field=field,
            require_closed=require_closed,
            mapping=mapping_requirement.mapping if mapping_requirement else None,
            enabled=True,
        )
        for r in ordered
    )
    mapping_bad = tuple(
        a.raw.input_ref
        for a in adapted
        if (a.raw.mapping_version is not None or mapping_requirement is not None)
        and not mapping_valid(mapping_requirement, stream, cutoff, a.raw)
    )
    empty_mapping_bad = (
        not adapted
        and mapping_requirement is not None
        and not mapping_valid(mapping_requirement, stream, cutoff, None)
    )
    bad_reasons = ("FIELD_SEMANTICS_UNSUPPORTED", "UNIT_UNKNOWN_OR_MISMATCH")
    usable = tuple(
        a
        for a in adapted
        if a.observation is not None
        and a.raw.input_ref not in mapping_bad
        and not any(r in a.diagnostics for r in bad_reasons)
    )
    output = tuple(
        (a.raw.source_id, a.observation.value, a.observation.unit, a.observation.side)
        for a in usable
    )
    exclusions = tuple(
        (
            a.raw.input_ref,
            a.exclusions
            + a.diagnostics
            + (
                ("MAPPING_UNVERIFIED",)
                if a.raw.input_ref in mapping_bad and "MAPPING_UNVERIFIED" not in a.diagnostics
                else ()
            ),
        )
        for a in adapted
        if a.exclusions or a.diagnostics or a.raw.input_ref in mapping_bad
    )
    if empty_mapping_bad:
        exclusions += ((coverage.evidence_ref, ("MAPPING_UNVERIFIED",)),)
    quality = assess(
        tuple(a.observation for a in usable),
        coverage=coverage_value,
        context=InputContext(stream.identity, stream.role, unit),
        policy=policy,
        evaluated_at=cutoff,
        window=window,
        evidence_refs=(coverage.evidence_ref,),
    )
    if mapping_bad or empty_mapping_bad:
        quality = replace(
            quality, status=Status.UNSUPPORTED, reason="MAPPING_UNVERIFIED", activity=None
        )
    elif any(a.exclusions or any(r in a.diagnostics for r in bad_reasons) for a in adapted):
        quality = replace(
            quality, status=Status.UNSUPPORTED, reason="REPLAY_INPUT_EXCLUDED", activity=None
        )
    manifest = Manifest(
        stream,
        window,
        schema_version,
        calculation_version,
        policy.version,
        digest(policy),
        cutoff,
        generated_at,
        coverage,
        tuple((r.input_ref, digest(r)) for r in ordered),
        unit,
        field,
        require_closed,
        mapping_requirement,
        quality,
        exclusions,
        digest(output),
        quality.status == Status.VALID
        and not mapping_bad
        and not empty_mapping_bad
        and all(
            not a.exclusions
            and not any(
                reason in a.diagnostics
                for reason in ("FIELD_SEMANTICS_UNSUPPORTED", "UNIT_UNKNOWN_OR_MISMATCH")
            )
            for a in adapted
        ),
    )
    return manifest, output


def evaluate_replays(
    graph: Graph, name: str, manifests: dict[str, Manifest], policy: tuple[FixturePolicy, ...]
) -> tuple[bool, tuple[str, ...]]:
    if type(graph) is not Graph or any(type(m) is not Manifest for m in manifests.values()):
        raise TypeError("graph and replay manifests required")
    evidence = {key: manifest.quality for key, manifest in manifests.items()}
    eligible, reasons = graph.eligible(name, evidence, policy)
    details = tuple(
        f"quality:{key}:{evidence[key].reason}" for key in evidence if f"invalid:{key}" in reasons
    )
    nodes = {node.name: node for node in graph.nodes}
    edge_failures = []
    optional_notes = []
    visited = set()

    def edge_valid(consumer: str, source: str) -> bool:
        if consumer not in manifests or source not in manifests:
            return False
        downstream, upstream = manifests[consumer], manifests[source]
        if upstream.cutoff > downstream.cutoff:
            return False
        if downstream.stream.identity.business_key == upstream.stream.identity.business_key:
            return True
        requirement = upstream.mapping_requirement
        return (
            requirement is not None
            and requirement.consumer_identity.business_key
            == downstream.stream.identity.business_key
            and mapping_valid(requirement, upstream.stream, downstream.cutoff, None)
        )

    def visit(consumer: str) -> None:
        if consumer in visited:
            return
        visited.add(consumer)
        node = nodes[consumer]
        for source in node.mandatory:
            if consumer in manifests and source in manifests and not edge_valid(consumer, source):
                edge_failures.append(f"edge_mapping:{consumer}:{source}")
            visit(source)
        for source in node.optional:
            if consumer in manifests and source in manifests and not edge_valid(consumer, source):
                optional_notes.append(f"optional_edge_mapping:{consumer}:{source}")

    visit(name)
    return (
        eligible and not edge_failures,
        reasons
        + details
        + tuple(edge_failures)
        + graph.optional_diagnostics(name, evidence)
        + tuple(optional_notes),
    )


@dataclass(frozen=True, slots=True)
class UnresolvedConflict:
    source_id: str
    window: Window
    fingerprints: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    detected_at: datetime

    def __post_init__(self):
        text(self.source_id)
        if type(self.window) is not Window:
            raise TypeError("conflict window required")
        if type(self.fingerprints) is not tuple or len(self.fingerprints) < 2:
            raise ValueError("conflicting fingerprints required")
        if len(set(self.fingerprints)) != len(self.fingerprints):
            raise ValueError("duplicate conflict fingerprint")
        if type(self.evidence_refs) is not tuple or not self.evidence_refs:
            raise ValueError("conflict evidence required")
        for value in (*self.fingerprints, *self.evidence_refs):
            text(value)
        utc(self.detected_at)


@dataclass(frozen=True, slots=True)
class RepairEvidence:
    source_id: str
    window: Window
    record: RawField
    coverage: Coverage
    evidence_ref: str
    verified_at: datetime

    def __post_init__(self):
        text(self.source_id)
        text(self.evidence_ref)
        if (
            type(self.window) is not Window
            or type(self.record) is not RawField
            or type(self.coverage) is not Coverage
        ):
            raise TypeError("immutable repair inputs required")
        utc(self.verified_at)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    stream: Stream
    covered_until: datetime
    version: str
    cursor: str | None
    evidence_refs: tuple[str, ...]
    seen: tuple[tuple[str, str], ...]
    captures: tuple[tuple[str, str, datetime, datetime], ...] = ()
    fingerprint_format: str = "semantic-v1"
    unresolved: tuple[UnresolvedConflict, ...] = ()
    integrity_format: str = "conflict-v1"

    def __post_init__(self):
        if type(self.stream) is not Stream:
            raise TypeError("stream required")
        utc(self.covered_until)
        text(self.version)
        if self.fingerprint_format not in ("semantic-v1", "legacy-raw-v0"):
            raise ValueError("unsupported fingerprint format")
        if self.integrity_format not in ("conflict-v1", "legacy-untracked-v0"):
            raise ValueError("unsupported integrity format")
        if type(self.unresolved) is not tuple or any(
            type(x) is not UnresolvedConflict for x in self.unresolved
        ):
            raise TypeError("immutable unresolved conflicts required")
        keys = [(x.source_id, x.window.start, x.window.end) for x in self.unresolved]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate unresolved conflict")
        if self.cursor is not None:
            text(self.cursor)
        if (
            type(self.evidence_refs) is not tuple
            or not self.evidence_refs
            or type(self.seen) is not tuple
        ):
            raise ValueError("immutable checkpoint evidence required")
        for ref in self.evidence_refs:
            text(ref)
        if any(type(item) is not tuple or len(item) != 2 for item in self.seen):
            raise TypeError("seen entries must be immutable pairs")
        for source_id, checksum in self.seen:
            text(source_id)
            text(checksum)
        if len({x[0] for x in self.seen}) != len(self.seen):
            raise ValueError("duplicate checkpoint identity")
        if type(self.captures) is not tuple:
            raise TypeError("immutable captures required")
        for item in self.captures:
            if type(item) is not tuple or len(item) != 4:
                raise TypeError("immutable capture required")
            text(item[0])
            text(item[1])
            utc(item[2])
            utc(item[3])
            if item[3] < item[2]:
                raise ValueError("capture availability before receipt")


def checkpoint_bytes(checkpoint: Checkpoint) -> bytes:
    if type(checkpoint) is not Checkpoint:
        raise TypeError("checkpoint required")
    return encode(checkpoint)


def load_checkpoint(data: bytes) -> Checkpoint:
    raw = json.loads(data)
    stream = raw["stream"]
    identity = stream["identity"]
    instrument = Identity(
        identity["venue"],
        identity["instrument_id"],
        MarketType(identity["market_type"]),
        identity["quote_currency"],
        identity["schema_version"],
    )
    return Checkpoint(
        Stream(instrument, stream["role"], stream["source"]),
        datetime.fromisoformat(raw["covered_until"].replace("Z", "+00:00")),
        raw["version"],
        raw["cursor"],
        tuple(raw["evidence_refs"]),
        tuple(tuple(item) for item in raw["seen"]),
        tuple(
            (
                x[0],
                x[1],
                datetime.fromisoformat(x[2].replace("Z", "+00:00")),
                datetime.fromisoformat(x[3].replace("Z", "+00:00")),
            )
            for x in raw.get("captures", ())
        ),
        raw.get("fingerprint_format", "legacy-raw-v0"),
        tuple(
            UnresolvedConflict(
                x["source_id"],
                Window(
                    datetime.fromisoformat(x["window"]["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(x["window"]["end"].replace("Z", "+00:00")),
                ),
                tuple(x["fingerprints"]),
                tuple(x["evidence_refs"]),
                datetime.fromisoformat(x["detected_at"].replace("Z", "+00:00")),
            )
            for x in raw.get("unresolved", ())
        ),
        raw.get("integrity_format", "legacy-untracked-v0"),
    )


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    checkpoint: Checkpoint
    new_records: tuple[RawField, ...]
    reasons: tuple[str, ...]
    repair_intervals: tuple[Window, ...]


def resume(
    checkpoint: Checkpoint,
    coverage: Coverage,
    records: tuple[RawField, ...],
    cutoff: datetime,
    *,
    cursor_supported: bool,
    repairs: tuple[RepairEvidence, ...] = (),
) -> RecoveryPlan:
    if type(checkpoint) is not Checkpoint or type(coverage) is not Coverage:
        raise TypeError("checkpoint and coverage required")
    utc(cutoff)
    if type(records) is not tuple or any(type(r) is not RawField for r in records):
        raise TypeError("immutable records required")
    if type(repairs) is not tuple or any(type(r) is not RepairEvidence for r in repairs):
        raise TypeError("immutable repair evidence required")
    if coverage.stream.business_key != checkpoint.stream.business_key or any(
        r.stream.business_key != checkpoint.stream.business_key for r in records
    ):
        raise ValueError("stream mismatch")
    if type(cursor_supported) is not bool:
        raise TypeError("explicit cursor capability required")
    if {r.source_id for r in repairs} & {r.source_id for r in records}:
        raise ValueError("REPAIR_BATCH_MUST_BE_SEPARATE")
    if checkpoint.fingerprint_format != "semantic-v1" and checkpoint.seen:
        raise ValueError("CHECKPOINT_FINGERPRINT_MIGRATION_REQUIRED")
    if checkpoint.integrity_format != "conflict-v1":
        raise ValueError("CHECKPOINT_CONFLICT_STATE_MIGRATION_REQUIRED")
    seen = dict(checkpoint.seen)
    captures = set(checkpoint.captures)
    unresolved = {(x.source_id, x.window.start, x.window.end): x for x in checkpoint.unresolved}
    evidence_refs = list(checkpoint.evidence_refs)
    new, reasons, repair_intervals = [], [], []
    for repair in repairs:
        key = (repair.source_id, repair.window.start, repair.window.end)
        conflict = unresolved.get(key)
        record = repair.record
        proof = repair.coverage
        if (
            conflict is None
            or proof.stream.business_key != checkpoint.stream.business_key
            or proof.window != repair.window
            or proof.evidence_ref in conflict.evidence_refs
            or repair.evidence_ref in conflict.evidence_refs
            or proof.available_at is None
            or proof.available_at <= conflict.detected_at
            or repair.verified_at < proof.available_at
            or repair.verified_at > cutoff
            or proof.for_window(checkpoint.stream, repair.window, repair.verified_at) is not True
            or record.stream.business_key != checkpoint.stream.business_key
            or record.source_id != repair.source_id
            or not repair.window.start <= record.event_time < repair.window.end
            or record.received_at is None
            or record.available_at is None
            or record.available_at > repair.verified_at
            or semantic_fingerprint(record) not in conflict.fingerprints
        ):
            raise ValueError("REPAIR_EVIDENCE_INVALID")
        fingerprint = semantic_fingerprint(record)
        was_seen = record.source_id in seen
        if was_seen and seen[record.source_id] != fingerprint:
            raise ValueError("EMITTED_RECORD_CORRECTION_REQUIRED")
        seen[record.source_id] = fingerprint
        captures.add((record.source_id, record.input_ref, record.received_at, record.available_at))
        if not was_seen:
            new.append(record)
        evidence_refs.extend((repair.evidence_ref, proof.evidence_ref))
        del unresolved[key]
    groups = {}
    for record in records:
        if not coverage.window.start <= record.event_time < coverage.window.end:
            reasons.append("RECORD_OUTSIDE_INTERVAL:" + record.source_id)
        elif (
            record.received_at is None
            or record.available_at is None
            or record.available_at > cutoff
        ):
            reasons.append("RECORD_UNAVAILABLE:" + record.source_id)
        else:
            groups.setdefault(record.source_id, []).append(record)
    for source_id, group in sorted(groups.items()):
        if any(c.source_id == source_id for c in unresolved.values()):
            continue
        fingerprints = {semantic_fingerprint(r) for r in group}
        previous = seen.get(source_id)
        if previous is not None:
            fingerprints.add(previous)
        if len(fingerprints) != 1:
            for record in group:
                captures.add((source_id, record.input_ref, record.received_at, record.available_at))
            conflict = UnresolvedConflict(
                source_id,
                coverage.window,
                tuple(sorted(fingerprints)),
                (coverage.evidence_ref, *sorted({record.input_ref for record in group})),
                cutoff,
            )
            unresolved[(source_id, coverage.window.start, coverage.window.end)] = conflict
            continue
        fingerprint = next(iter(fingerprints))
        for record in group:
            captures.add((source_id, record.input_ref, record.received_at, record.available_at))
        if previous is None:
            seen[source_id] = fingerprint
            new.append(
                min(group, key=lambda r: (r.available_at, r.received_at, r.input_ref, digest(r)))
            )
    if not cursor_supported:
        reasons.append("CURSOR_UNSUPPORTED")
    if coverage.window.start > checkpoint.covered_until:
        reasons.append("COVERAGE_GAP")
        repair_intervals.append(Window(checkpoint.covered_until, coverage.window.start))
    coverage_value = coverage.for_window(checkpoint.stream, coverage.window, cutoff)
    verified = coverage_value is True
    if coverage_value is False and coverage.window.end > checkpoint.covered_until:
        start = max(coverage.window.start, checkpoint.covered_until)
        repair_intervals.append(Window(start, coverage.window.end))
        reasons.append("COVERAGE_GAP")
    if not verified:
        reasons.append("COVERAGE_UNVERIFIED")
    for conflict in unresolved.values():
        reasons.append("CONFLICTING_DUPLICATE:" + conflict.source_id)
        repair_intervals.append(conflict.window)
    boundary = checkpoint.covered_until
    if (
        verified
        and not repair_intervals
        and not any(r.startswith("RECORD_UNAVAILABLE:") for r in reasons)
    ):
        boundary = max(boundary, coverage.window.end)
    evidence_refs.append(coverage.evidence_ref)
    next_checkpoint = Checkpoint(
        checkpoint.stream,
        boundary,
        checkpoint.version,
        checkpoint.cursor,
        tuple(evidence_refs),
        tuple(sorted(seen.items())),
        tuple(sorted(captures)),
        "semantic-v1",
        tuple(
            sorted(unresolved.values(), key=lambda x: (x.source_id, x.window.start, x.window.end))
        ),
        "conflict-v1",
    )
    intervals = tuple(sorted(set(repair_intervals), key=lambda w: (w.start, w.end)))
    return RecoveryPlan(next_checkpoint, tuple(new), tuple(sorted(set(reasons))), intervals)


def lifecycle_for_plan(
    plan: RecoveryPlan,
    current: Lifecycle,
    evidence: Evidence,
    evaluated_at: datetime,
    *,
    freshness_ok: bool,
    policy_present: bool,
) -> Result:
    if type(freshness_ok) is not bool or type(policy_present) is not bool:
        raise TypeError("explicit flags required")
    ok = (
        not plan.repair_intervals
        and not plan.checkpoint.unresolved
        and not any(
            r == "COVERAGE_UNVERIFIED"
            or r.startswith(("CONFLICTING_DUPLICATE:", "RECORD_UNAVAILABLE:"))
            for r in plan.reasons
        )
        and freshness_ok
        and policy_present
        and evidence.mandatory_ok
        and evidence.unexpired
    )
    checked = Evidence(
        evidence.version, evidence.passed, ok, evidence.unexpired, evidence.recovery_checked and ok
    )
    if ok and current != Lifecycle.SUSPENDED:
        return Result(current, "EVIDENCE_RETAINED", evidence.version)
    target = Lifecycle.DATA_CHECK if current == Lifecycle.SUSPENDED else Lifecycle.SUSPENDED
    return transition(current, target, checked, evaluated_at)
